"""Best-effort "a chunk finished" command hook.

After each chunk (success or failure) the encoders call `ChunkHook.fire(...)`,
which runs a user-configured argv LIST with `shell=False` (subprocess-discipline
invariant: no shell, no string concatenation, no injection) and a timeout,
passing per-chunk context via X265_* environment variables.

`fire()` is contractually NO-RAISE. It runs inside the parallel worker thread
(from `_attempt_chunk`'s `finally`), where an escaping exception would either be
misread as a chunk failure (tripping the choke / needs-fix path on a chunk that
actually succeeded) or kill the worker slot outright. The catch set
(TimeoutExpired, OSError, ValueError, SubprocessError) is exhaustive for
`subprocess.run` BY CONSTRUCTION — it does NOT rely on upstream validation.
ValueError matters specifically: `subprocess.run` raises it for an embedded NUL
in the argv, and `text=True` can raise it (UnicodeDecodeError) decoding the
hook's stderr. `parse_hook_spec`/`load_hook_sidecar` additionally reject NUL up
front so bad config fails loud before any encode, but fire() stays safe even if
something slips through.

Overall-progress contract (X265_CHUNKS_DONE / X265_DURATION_DONE_SEC /
X265_PROGRESS_PERCENT) is derived from GROUND TRUTH — which `enc_<stem>.mkv`
files actually exist on disk — so it stays honest in parallel mode where chunks
finish out of order. The just-finished chunk's positional index is preserved in
X265_CHUNK_INDEX for back-compat, but it is NOT a progress signal on its own.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .hook_base import record_hook_outcome, run_hook_command


HOOK_TIMEOUT_SEC = 30.0
HOOK_EVENT = "chunk-done"
# Config-key name used in the durable hook log (logs/<stem>.hooks.log).
HOOK_NAME = "on_chunk_done"


class ChunkHook:
    """Runs the on_chunk_done command. Static context (source, workdir, total,
    chunks, total duration) is bound at construction; per-chunk context is
    passed to `fire`. `runner`, `timeout`, and `duration_probe` are injectable
    so tests never spawn a real process or shell out to ffprobe."""

    def __init__(self, command: Optional[list[str]], *,
                 source: Path, workdir: Path, total: int,
                 chunks: Optional[Iterable[Path]] = None,
                 total_duration_sec: float = 0.0,
                 duration_probe: Optional[Callable[[Path], float]] = None,
                 runner: Callable[..., object] = subprocess.run,
                 timeout: float = HOOK_TIMEOUT_SEC,
                 event_log: Callable[..., object] = record_hook_outcome) -> None:
        self._command = list(command) if command else None
        self._source = source
        self._workdir = workdir
        self._total = total
        # Snapshot the chunk list so adding entries upstream after construction
        # can't change the progress math mid-run. Empty list when the caller
        # has no chunks list to share (degraded — count-based progress only).
        self._chunks: list[Path] = list(chunks) if chunks else []
        self._total_duration_sec = float(total_duration_sec)
        self._duration_probe = duration_probe
        # Lazy per-chunk duration cache. Probed once per chunk in the typical
        # case — 30 chunks × ~50 ms ffprobe across an encode is invisible vs.
        # hours of compute, and probing all upfront would add startup latency
        # on slow / network storage. Concurrent workers MAY first-see the same
        # chunk simultaneously and probe twice; the probe is idempotent, GIL
        # protects the dict write, and the second writer just overwrites with
        # the same value — bounded, harmless, deliberately unlocked.
        self._duration_cache: dict[Path, float] = {}
        self._runner = runner
        self._timeout = timeout
        self._event_log = event_log

    @property
    def enabled(self) -> bool:
        return self._command is not None

    def fire(self, *, chunk_name: str, index: int, status: str,
             output: Optional[Path], elapsed_sec: float) -> Optional[str]:
        """Run the hook for one finished chunk. Returns a log line on
        failure/timeout/non-zero exit, else None. NEVER raises."""
        if self._command is None:
            return None
        # The chunk-done message names the chunk in every variant; pass it as
        # the shared suffix (placed before the ": <tail>" continuation, so the
        # rendered strings stay byte-identical to the pre-refactor versions).
        return run_hook_command(
            command=self._command,
            env_overrides=self._build_env(chunk_name, index, status, output,
                                          elapsed_sec),
            timeout=self._timeout, runner=self._runner,
            event_log=self._event_log, source=self._source,
            hook_name=HOOK_NAME, log_suffix=f" ({chunk_name})",
        )

    def _done_chunks(self) -> list[Path]:
        """Source chunks whose encoded counterpart exists on disk RIGHT NOW.
        Ground-truth: any other definition (a counter, a "this chunk just
        finished" flag) would lie in parallel mode where chunks finish out of
        order and the worker that calls fire() may not be the most-recent
        completer by the time the env is built."""
        return [c for c in self._chunks
                if (self._workdir / f"enc_{c.stem}.mkv").exists()]

    def _duration_of(self, chunk: Path) -> float:
        """Cached chunk duration in seconds. Returns 0.0 when no probe is
        configured (degraded mode) so the progress math falls back cleanly to
        count-based percentages without raising."""
        if chunk in self._duration_cache:
            return self._duration_cache[chunk]
        if self._duration_probe is None:
            return 0.0
        try:
            d = float(self._duration_probe(chunk))
        except (OSError, ValueError, TypeError):
            # A probe that raises must NEVER bubble out — fire() is no-raise by
            # contract. Treat as unknown duration; progress degrades to the
            # count-based fallback rather than aborting the encode.
            d = 0.0
        self._duration_cache[chunk] = d
        return d

    def _progress_fields(self) -> dict[str, str]:
        """X265_CHUNKS_DONE / X265_DURATION_DONE_SEC / X265_DURATION_TOTAL_SEC
        / X265_PROGRESS_PERCENT — all derived from GROUND TRUTH (which
        enc_*.mkv files exist) so parallel/serial paths agree.

        Two fallbacks layered for robustness:
          * If duration_probe isn't wired (or every probe failed), progress
            falls back to count_done / total * 100.
          * If `total` is 0 (paranoid guard — `total=0` would mean "no chunks
            were planned"), percent is 0.0 rather than ZeroDivisionError."""
        done = self._done_chunks()
        count_done = len(done)
        total_dur = self._total_duration_sec
        if total_dur > 0 and self._duration_probe is not None:
            done_dur = sum(self._duration_of(c) for c in done)
            pct = (done_dur / total_dur) * 100.0
        else:
            done_dur = 0.0
            pct = (count_done / self._total * 100.0) if self._total else 0.0
        # Clamp at 100 — duration probes that sum slightly higher than the
        # source duration (rounding in mkvmerge segments) would otherwise emit
        # "100.4%" and confuse downstream alerts.
        pct = max(0.0, min(100.0, pct))
        return {
            "X265_CHUNKS_DONE": str(count_done),
            "X265_DURATION_DONE_SEC": f"{done_dur:.2f}",
            "X265_DURATION_TOTAL_SEC": f"{total_dur:.2f}",
            "X265_PROGRESS_PERCENT": f"{pct:.1f}",
        }

    def _build_env(self, chunk_name: str, index: int, status: str,
                   output: Optional[Path], elapsed_sec: float) -> dict[str, str]:
        """The X265_* contract. Every value is a string (env vars must be)."""
        env = {
            "X265_HOOK_EVENT": HOOK_EVENT,
            "X265_CHUNK_STATUS": status,
            "X265_SOURCE": str(self._source),
            "X265_WORKDIR": str(self._workdir),
            "X265_CHUNK_NAME": chunk_name,
            "X265_CHUNK_INDEX": str(index),
            "X265_CHUNK_TOTAL": str(self._total),
            "X265_CHUNK_OUTPUT": str(output) if output else "",
            "X265_CHUNK_ELAPSED_SEC": f"{elapsed_sec:.2f}",
        }
        env.update(self._progress_fields())
        return env


def fire_for_chunk(hook: Optional[ChunkHook], *, chunk: Path, workdir: Path,
                   position_of: Mapping[Path, int], elapsed: float,
                   log: Callable[[str], object]) -> None:
    """Fire `hook` for a just-finished chunk, the single seam both encoders use.

    Status + output are derived from GROUND TRUTH — whether `enc_<stem>.mkv`
    exists on disk — so success, autofix-success, choke, and exception all map
    correctly without each caller reasoning about return codes. A failure log
    line (if any) is routed to the caller's `log` (events queue in parallel,
    print in serial). No-op when no hook is configured."""
    if hook is None or not hook.enabled:
        return
    out = workdir / f"enc_{chunk.stem}.mkv"
    produced = out.exists()
    msg = hook.fire(
        chunk_name=chunk.name,
        index=position_of.get(chunk, 0),
        status="ok" if produced else "failed",
        output=out if produced else None,
        elapsed_sec=elapsed,
    )
    if msg:
        log(msg)

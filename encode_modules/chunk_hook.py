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
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional


HOOK_TIMEOUT_SEC = 30.0
HOOK_EVENT = "chunk-done"


class ChunkHook:
    """Runs the on_chunk_done command. Static context (source, workdir, total)
    is bound at construction; per-chunk context is passed to `fire`. `runner`
    and `timeout` are injectable so tests never spawn a real process."""

    def __init__(self, command: Optional[list[str]], *,
                 source: Path, workdir: Path, total: int,
                 runner: Callable[..., object] = subprocess.run,
                 timeout: float = HOOK_TIMEOUT_SEC) -> None:
        self._command = list(command) if command else None
        self._source = source
        self._workdir = workdir
        self._total = total
        self._runner = runner
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return self._command is not None

    def fire(self, *, chunk_name: str, index: int, status: str,
             output: Optional[Path], elapsed_sec: float) -> Optional[str]:
        """Run the hook for one finished chunk. Returns a log line on
        failure/timeout/non-zero exit, else None. NEVER raises."""
        if self._command is None:
            return None
        env = dict(os.environ)
        env.update(self._build_env(chunk_name, index, status, output,
                                   elapsed_sec))
        try:
            proc = self._runner(
                self._command, env=env, timeout=self._timeout,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
        except subprocess.TimeoutExpired:
            return (f"  ! on_chunk_done hook timed out after "
                    f"{self._timeout:g}s ({chunk_name})")
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            # ValueError: embedded NUL in the argv, or undecodable stderr under
            # text=True. Must be caught — this runs in the worker's finally, so
            # a raise here would kill the slot.
            return (f"  ! on_chunk_done hook failed ({chunk_name}): "
                    f"{type(e).__name__}: {e}")
        rc = getattr(proc, "returncode", 0)
        if rc:
            tail = (getattr(proc, "stderr", "") or "").strip()
            tail = tail.replace("\n", " ")[-200:]
            return (f"  ! on_chunk_done hook exited {rc} ({chunk_name})"
                    + (f": {tail}" if tail else ""))
        return None

    def _build_env(self, chunk_name: str, index: int, status: str,
                   output: Optional[Path], elapsed_sec: float) -> dict[str, str]:
        """The X265_* contract. Every value is a string (env vars must be)."""
        return {
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

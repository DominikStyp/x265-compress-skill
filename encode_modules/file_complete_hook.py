"""Best-effort "the file is on disk and verified" command hook.

Fires SUCCESS-ONLY, exactly once per encoder run — only when the job's
terminal status is `ok` AND the final `.mkv` exists on disk. Companion to
`JobEndHook` (which fires for ANY terminal status, including stops and
failures). Use-case split:

  on_chunk_done     → progress bar updates (every chunk, ok or failed)
  on_job_end        → "the run is done — here's why" alerts (every status)
  on_file_complete  → "the file is finished and ready" celebrations (ok only,
                      with queue-level counters mixed in)

The queue-level env vars (`X265_QUEUE_*`) are set by `run_queue.py` on the
child encoder's environment before spawning compress.py; they inherit
straight through to this hook's subprocess via `os.environ`. Single-file
invocations see no such vars and fall back to degraded 1/1/0/0 defaults — so
the same notification script works in both modes without an
"if `X265_QUEUE_INDEX` in os.environ" branch.

The hook fires from `HistoryRecorder.flush()` BEFORE the job-end hook, so a
slow file_complete script can't delay the job_end audit. Same NO-RAISE
discipline as the other hooks.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional


HOOK_TIMEOUT_SEC = 30.0
HOOK_EVENT = "file-complete"


class FileCompleteHook:
    """Runs the on_file_complete command. Static context (source, workdir)
    bound at construction; per-file context (status, sizes, …) passed to
    `fire`. `runner` and `timeout` are injectable so tests never spawn a
    real process."""

    def __init__(self, command: Optional[list[str]], *,
                 source: Path, workdir: Path,
                 runner: Callable[..., object] = subprocess.run,
                 timeout: float = HOOK_TIMEOUT_SEC) -> None:
        self._command = list(command) if command else None
        self._source = source
        self._workdir = workdir
        self._runner = runner
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return self._command is not None

    def fire(self, *,
             status: str,
             output: Optional[Path],
             output_bytes_final: Optional[int] = None,
             source_bytes: Optional[int] = None,
             wall_seconds: Optional[float] = None,
             pct_saved: Optional[float] = None,
             crf: Optional[int] = None,
             crf_retry_chain: str = "",
             vmaf_mean: Optional[float] = None) -> Optional[str]:
        """Run the hook for one successful, on-disk file. Returns a log line
        on failure/timeout/non-zero exit, else None. NEVER raises.

        Refuses to fire on any status other than `ok`, or when `output` is
        None (the file isn't actually on disk). That filter is the whole
        contract — notification scripts get to assume "if it fired, the
        file is ready"."""
        if self._command is None:
            return None
        if status != "ok" or output is None:
            return None
        env = dict(os.environ)
        env.update(self._build_env(
            output=output, output_bytes_final=output_bytes_final,
            source_bytes=source_bytes, wall_seconds=wall_seconds,
            pct_saved=pct_saved, crf=crf, crf_retry_chain=crf_retry_chain,
            vmaf_mean=vmaf_mean,
        ))
        try:
            proc = self._runner(
                self._command, env=env, timeout=self._timeout,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
        except subprocess.TimeoutExpired:
            return (f"  ! on_file_complete hook timed out after "
                    f"{self._timeout:g}s")
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return (f"  ! on_file_complete hook failed: "
                    f"{type(e).__name__}: {e}")
        rc = getattr(proc, "returncode", 0)
        if rc:
            tail = (getattr(proc, "stderr", "") or "").strip()
            tail = tail.replace("\n", " ")[-200:]
            return (f"  ! on_file_complete hook exited {rc}"
                    + (f": {tail}" if tail else ""))
        return None

    def _build_env(self, *,
                   output: Path,
                   output_bytes_final: Optional[int],
                   source_bytes: Optional[int],
                   wall_seconds: Optional[float],
                   pct_saved: Optional[float],
                   crf: Optional[int],
                   crf_retry_chain: str,
                   vmaf_mean: Optional[float]) -> dict[str, str]:
        """Build the X265_* env contract. Per-file fields come from the
        recorder; queue-counter fields fall through from os.environ via
        `_queue_counter_defaults` (which substitutes 1/1/this-file values
        when nothing's been set by run_queue.py)."""
        env: dict[str, str] = {
            "X265_HOOK_EVENT": HOOK_EVENT,
            "X265_SOURCE": str(self._source),
            "X265_WORKDIR": str(self._workdir),
            "X265_OUTPUT": str(output),
            "X265_SOURCE_BYTES": (
                "" if source_bytes is None else str(source_bytes)),
            "X265_OUTPUT_BYTES": (
                "" if output_bytes_final is None else str(output_bytes_final)),
            "X265_WALL_SECONDS": (
                "" if wall_seconds is None else f"{wall_seconds:.2f}"),
            "X265_PCT_SAVED": (
                "" if pct_saved is None else f"{pct_saved:.2f}"),
            "X265_CRF": "" if crf is None else str(crf),
            "X265_CRF_RETRY_CHAIN": crf_retry_chain,
            "X265_VMAF_MEAN": (
                "" if vmaf_mean is None else f"{vmaf_mean:.2f}"),
        }
        env.update(_queue_counter_defaults(
            source_bytes=source_bytes,
            output_bytes_final=output_bytes_final,
            wall_seconds=wall_seconds, pct_saved=pct_saved,
        ))
        return env


def _queue_counter_defaults(*,
                            source_bytes: Optional[int],
                            output_bytes_final: Optional[int],
                            wall_seconds: Optional[float],
                            pct_saved: Optional[float]) -> dict[str, str]:
    """Populate X265_QUEUE_* env vars. Reads the overlay set by run_queue.py
    (count of jobs ALREADY done before this one); single-file `compress.py`
    runs see no overlay and fall back to `1 / 0 / 0` defaults.

    For X265_QUEUE_ITEMS_FINISHED the FileCompleteHook applies the "+1 for
    this success" itself: run_queue's overlay deliberately publishes the
    pre-this-job count so JobEndHook (which also inherits from os.environ
    and fires on FAILURES too) reports an honest number on stops/failures.
    Adding 1 here is correct by construction — FileCompleteHook only fires
    on `ok`."""
    # Past oks observed by the queue runner (default 0 in single-file mode).
    past_finished = _int_env("X265_QUEUE_ITEMS_FINISHED", default=0)
    this_finished = past_finished + 1
    defaults: dict[str, str] = {
        "X265_QUEUE_INDEX": "1",
        "X265_QUEUE_TOTAL": "1",
        "X265_QUEUE_ITEMS_REMAINING": "0",
        "X265_QUEUE_ITEMS_FAILED": "0",
        "X265_QUEUE_ITEMS_STOPPED": "0",
        "X265_QUEUE_ITEMS_SKIPPED": "0",
        "X265_QUEUE_BYTES_IN_SO_FAR": (
            "" if source_bytes is None else str(source_bytes)),
        "X265_QUEUE_BYTES_OUT_SO_FAR": (
            "" if output_bytes_final is None else str(output_bytes_final)),
        "X265_QUEUE_PCT_SAVED_SO_FAR": (
            "" if pct_saved is None else f"{pct_saved:.2f}"),
        "X265_QUEUE_WALL_SECONDS": (
            "" if wall_seconds is None else f"{wall_seconds:.2f}"),
    }
    # Real queue values (when present) take precedence — defaults only fill
    # in keys the parent hasn't already set.
    out = {k: os.environ.get(k, v) for k, v in defaults.items()}
    # Inclusive-of-this-job override for FINISHED. NOT a pass-through from
    # env: run_queue's overlay value is exclusive by design.
    out["X265_QUEUE_ITEMS_FINISHED"] = str(this_finished)
    return out


def _int_env(name: str, *, default: int) -> int:
    """Read an int env var with a clean fallback. Empty / non-numeric → default,
    so a malformed overlay value never crashes the hook's no-raise contract."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

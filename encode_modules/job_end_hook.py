"""Best-effort "the job ended" command hook.

Fires EXACTLY ONCE per encoder run, at the terminal-status chokepoint inside
`HistoryRecorder.flush()` — that's where every exit path (success, threshold
stop, choke, pre-flight fail, verify fail, stopped-by-user, atexit) converges
to write the JSONL audit record. Wiring the hook there guarantees:

  * It fires for every terminal status, not just success.
  * It fires at most once per process lifetime — the recorder's `written` flag
    is the same idempotency guard the JSONL write uses.
  * The payload is built from the same `self.current` dict that just landed
    on disk, so the hook and the audit trail can never disagree.

The contract mirrors `ChunkHook`'s NO-RAISE discipline. `fire()` runs inside
the recorder's flush path, which may be triggered from atexit on an
already-failing exit; a raising hook would either crash the audit-trail flush
or leak a stack trace over the user's terminal at the worst possible moment.
Every exception subprocess.run can raise (TimeoutExpired, OSError, ValueError,
SubprocessError) is caught and returned as an optional log line.

Env vars are stringified per the project's convention; absent values become
`""` (never missing) so hook scripts can rely on `os.environ[...]` without
KeyError.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .hook_logging import record_hook_outcome


HOOK_TIMEOUT_SEC = 30.0
HOOK_EVENT = "job-end"
# Config-key name used in the durable hook log (logs/<stem>.hooks.log).
HOOK_NAME = "on_job_end"


class JobEndHook:
    """Runs the on_job_end command. Static context (source, workdir) is bound
    at construction; per-job context (status, sizes, CRF chain, ...) is passed
    to `fire`. `runner` and `timeout` are injectable so tests never spawn a
    real process."""

    def __init__(self, command: Optional[list[str]], *,
                 source: Path, workdir: Path,
                 runner: Callable[..., object] = subprocess.run,
                 timeout: float = HOOK_TIMEOUT_SEC,
                 event_log: Callable[..., object] = record_hook_outcome) -> None:
        self._command = list(command) if command else None
        self._source = source
        self._workdir = workdir
        self._runner = runner
        self._timeout = timeout
        self._event_log = event_log

    @property
    def enabled(self) -> bool:
        return self._command is not None

    def fire(self, *,
             status: str,
             stop_reason: str = "",
             stop_detail: str = "",
             crf: Optional[int] = None,
             crf_retry_chain: str = "",
             output: Optional[Path] = None,
             output_bytes_final: Optional[int] = None,
             source_bytes: Optional[int] = None,
             output_bytes_projected: Optional[int] = None,
             output_bytes_threshold: Optional[int] = None,
             wall_seconds: Optional[float] = None,
             pct_saved: Optional[float] = None) -> Optional[str]:
        """Run the hook for one finished job. Returns a log line on
        failure/timeout/non-zero exit, else None. NEVER raises."""
        if self._command is None:
            return None
        env = dict(os.environ)
        env.update(self._build_env(
            status=status, stop_reason=stop_reason, stop_detail=stop_detail,
            crf=crf, crf_retry_chain=crf_retry_chain,
            output=output, output_bytes_final=output_bytes_final,
            source_bytes=source_bytes,
            output_bytes_projected=output_bytes_projected,
            output_bytes_threshold=output_bytes_threshold,
            wall_seconds=wall_seconds, pct_saved=pct_saved,
        ))
        try:
            proc = self._runner(
                self._command, env=env, timeout=self._timeout,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
        except subprocess.TimeoutExpired:
            self._log("timeout")
            return (f"  ! on_job_end hook timed out after "
                    f"{self._timeout:g}s")
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            # Same exhaustive catch as ChunkHook.fire — fire() must not raise
            # out of the flush path, where a SystemExit or atexit frame is
            # often already unwinding.
            self._log("spawn-error", f"{type(e).__name__}: {e}")
            return (f"  ! on_job_end hook failed: "
                    f"{type(e).__name__}: {e}")
        rc = getattr(proc, "returncode", 0)
        if rc:
            tail = (getattr(proc, "stderr", "") or "").strip().replace("\n", " ")
            self._log(f"exited {rc}", tail[-500:])
            return (f"  ! on_job_end hook exited {rc}"
                    + (f": {tail[-200:]}" if tail else ""))
        self._log("ok")
        return None

    def _log(self, outcome: str, stderr_tail: str = "") -> None:
        """Persist this fire's outcome to the durable hook log. No-raise."""
        self._event_log(source=self._source, event=HOOK_NAME,
                        command=self._command, outcome=outcome,
                        stderr_tail=stderr_tail)

    def _build_env(self, *,
                   status: str, stop_reason: str, stop_detail: str,
                   crf: Optional[int], crf_retry_chain: str,
                   output: Optional[Path],
                   output_bytes_final: Optional[int],
                   source_bytes: Optional[int],
                   output_bytes_projected: Optional[int],
                   output_bytes_threshold: Optional[int],
                   wall_seconds: Optional[float],
                   pct_saved: Optional[float]) -> dict[str, str]:
        """The X265_* job-end contract. Every value is a string — None
        becomes `""` rather than the literal `"None"`, so scripts can rely
        on `os.environ[var]` without KeyError AND can detect "absent" via
        empty-string check."""
        return {
            "X265_HOOK_EVENT": HOOK_EVENT,
            "X265_JOB_STATUS": status,
            "X265_JOB_STOP_REASON": stop_reason,
            "X265_JOB_STOP_DETAIL": stop_detail,
            "X265_SOURCE": str(self._source),
            "X265_WORKDIR": str(self._workdir),
            "X265_CRF": "" if crf is None else str(crf),
            "X265_CRF_RETRY_CHAIN": crf_retry_chain,
            "X265_OUTPUT": str(output) if output else "",
            "X265_OUTPUT_BYTES_FINAL": (
                "" if output_bytes_final is None else str(output_bytes_final)),
            "X265_SOURCE_BYTES": (
                "" if source_bytes is None else str(source_bytes)),
            "X265_OUTPUT_BYTES_PROJECTED": (
                "" if output_bytes_projected is None
                else str(output_bytes_projected)),
            "X265_OUTPUT_BYTES_THRESHOLD": (
                "" if output_bytes_threshold is None
                else str(output_bytes_threshold)),
            "X265_WALL_SECONDS": (
                "" if wall_seconds is None else f"{wall_seconds:.2f}"),
            "X265_PCT_SAVED": (
                "" if pct_saved is None else f"{pct_saved:.2f}"),
        }

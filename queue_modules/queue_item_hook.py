"""Best-effort "the queue just finished a job" command hook.

Fires from `run_queue.py` AFTER each `run_job_with_crf_retry(...)` returns
and the row is appended to `job_reports` — i.e. for every terminal status
the encoder produced (ok, failed-gen, stopped-threshold, chunk-choked,
pre-flight-failed, verify-failed, stopped-by-user, …). Does NOT fire on
skip rows: the feature spec is "fully processed OR failed", which a skip
(input missing, output already exists, prior-run completion) is neither.

Companion to the in-encoder hooks. Use-case split:

  on_chunk_done        (per-chunk, in encoder)  -> live progress
  on_job_end           (per-job,  in encoder)   -> "this source ended"
  on_file_complete     (per-job,  in encoder)   -> "this file is ready" (ok)
  on_queue_item_end    (per-job,  in QUEUE)     -> "queue snapshot after
                                                    this job — every other
                                                    job's [OK]/[FAILED]
                                                    too"

NO-RAISE discipline: every exception subprocess.run can raise
(TimeoutExpired, OSError, ValueError, SubprocessError) is swallowed and
returned as an optional log line; the queue's per-job loop never aborts
over a notification hiccup. Identical defensive band as JobEndHook.

Env contract:

  X265_HOOK_EVENT             = "queue-item-end"
  X265_JOB_STATUS             = the just-finished job's terminal status
  X265_JOB_MARKER             = "[OK]" / "[FAILED]" (matches the summary)
  X265_SOURCE                 = absolute input path of the just-finished job
  X265_OUTPUT                 = absolute output path, or "" if not produced
  X265_QUEUE_STATUS_SUMMARY   = multi-line text, one job per line

The X265_QUEUE_* counter overlay set by run_queue.py is inherited via
os.environ unchanged — same passthrough JobEndHook uses.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from encode_modules.hook_base import record_hook_outcome, run_hook_command

from .queue_status_format import classify_marker, render_queue_summary


HOOK_TIMEOUT_SEC = 30.0
HOOK_EVENT = "queue-item-end"
# Config-key name used in the durable hook log (logs/<stem>.hooks.log).
HOOK_NAME = "on_queue_item_end"


class QueueItemEndHook:
    """Runs the on_queue_item_end command. `runner` and `timeout` are
    injectable so tests never spawn a real process; the default `runner`
    is `subprocess.run`. `event_log` is the durable-log seam (default the
    shared `record_hook_outcome`)."""

    def __init__(self, command: Optional[list[str]], *,
                 runner: Callable[..., object] = subprocess.run,
                 timeout: float = HOOK_TIMEOUT_SEC,
                 event_log: Callable[..., object] = record_hook_outcome) -> None:
        self._command = list(command) if command else None
        self._runner = runner
        self._timeout = timeout
        self._event_log = event_log

    @property
    def enabled(self) -> bool:
        return self._command is not None

    def fire(self, *,
             status: str,
             source: Path,
             output: Optional[Path],
             summary: str) -> Optional[str]:
        """Run the hook for one finished queue job. Returns a log line on
        failure / timeout / non-zero exit, else None. NEVER raises."""
        if self._command is None:
            return None
        # source is per-fire (the just-finished job's input) — not bound at
        # construction like the in-encoder hooks — so the durable log lands
        # next to that source's other hook logs.
        return run_hook_command(
            command=self._command,
            env_overrides=self._build_env(
                status=status, source=source, output=output, summary=summary,
            ),
            timeout=self._timeout, runner=self._runner,
            event_log=self._event_log, source=source,
            hook_name=HOOK_NAME,
        )

    def _build_env(self, *,
                   status: str,
                   source: Path,
                   output: Optional[Path],
                   summary: str) -> dict[str, str]:
        """The X265_* queue-item-end contract. Every value is a string;
        None becomes "" (never the literal "None") so hook scripts can
        rely on os.environ[var] without KeyError and can detect "absent"
        via empty-string check — same convention as JobEndHook."""
        return {
            "X265_HOOK_EVENT": HOOK_EVENT,
            "X265_JOB_STATUS": status,
            "X265_JOB_MARKER": classify_marker(status),
            "X265_SOURCE": str(source),
            "X265_OUTPUT": str(output) if output else "",
            "X265_QUEUE_STATUS_SUMMARY": summary,
        }


def build_dispatch_payload(*, merged: dict,
                           jobs_snapshot: list[dict],
                           job_reports: list[dict],
                           status: str, row: dict
                           ) -> Optional[dict]:
    """Pure: build the queue snapshot + summary + hook command from the
    queue runner's state. Returns None when no `on_queue_item_end` is
    configured (or it's disabled via the falsy-override convention).

    Kept separate from the firing wrapper so it can be exhaustively
    unit-tested without any subprocess machinery — every shape concern
    (snapshot order, status lookup, bare-string-vs-list, falsy disable)
    lives here.

    The hook command is read from `merged` (which already overlays
    queue.json `defaults` with the per-job override). A bare-string
    command is wrapped to a one-element list so both spellings reach the
    subprocess identically — mirrors the existing on_chunk_done /
    on_job_end argv-build pattern.

    Snapshot ordering follows `jobs_snapshot` (the queue's current
    reload). Lookup of past statuses is by absolute input path. We
    DEFENSIVELY re-resolve via `Path(...).resolve()` on both sides
    rather than trust upstream `expand_jobs` resolution — that costs
    nothing (resolve is idempotent on absolute paths) and guards against
    any pre-resolution drift that future refactors could introduce.
    """
    cmd = merged.get("on_queue_item_end")
    if not cmd:
        return None
    cmd_argv = cmd if isinstance(cmd, list) else [cmd]
    snapshot = [str(Path(raw["input"]).resolve()) for raw in jobs_snapshot
                if "input" in raw]
    reports_by_input = {str(Path(r["input"]).resolve()): r
                        for r in job_reports if r.get("input")}
    summary = render_queue_summary(snapshot, reports_by_input)
    source_str = row.get("input") or merged.get("input") or ""
    out_str = row.get("output")
    return {
        "cmd_argv": cmd_argv,
        "source": Path(source_str),
        "output": Path(out_str) if out_str else None,
        "summary": summary,
        "status": status,
    }


def dispatch_on_queue_item_end(*, merged: dict,
                               jobs_snapshot: list[dict],
                               job_reports: list[dict],
                               status: str, row: dict
                               ) -> Optional[str]:
    """Thin firing wrapper around `build_dispatch_payload`. Best-effort —
    returns the optional log line from the hook (None when disabled or
    when the hook succeeded). Called by run_queue.py after each finished
    job (deliberately NOT for skipped rows: the spec is "fully processed
    OR failed", and a skip is neither)."""
    payload = build_dispatch_payload(
        merged=merged, jobs_snapshot=jobs_snapshot,
        job_reports=job_reports, status=status, row=row,
    )
    if payload is None:
        return None
    return QueueItemEndHook(payload["cmd_argv"]).fire(
        status=payload["status"], source=payload["source"],
        output=payload["output"], summary=payload["summary"])

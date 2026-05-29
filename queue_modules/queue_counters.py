"""Queue-level counter env vars for the on_file_complete hook.

run_queue.py overlays these onto `os.environ` BEFORE spawning each per-job
encoder subprocess. The values inherit straight through cmd → bat → python →
hook subprocess via standard env inheritance — no IPC, no shared state file.

Semantics:
  * Per-RUN scope. Counters reset every `run_queue.py` invocation; lifetime
    history lives in encoding_history.jsonl.
  * EXCLUSIVE of the just-starting job — values reflect the state of the
    queue BEFORE this encode runs. Why exclusive: the overlay env is set
    before spawn, and the same overlay is inherited by both
    on_file_complete (success-only) AND on_job_end (every status). If we
    pre-incremented `ITEMS_FINISHED` here we would publish a FALSE +1 on
    every failure / stop / choke. on_file_complete restores the inclusive
    "+1 for this success" itself, in FileCompleteHook's env builder, where
    the +1 is correct by construction.
  * Values are stringified per the project convention. Missing aggregates
    (e.g. zero successful jobs so far) become `0` or `0.00` rather than empty
    strings — the queue contract treats them as numbers, not "absent".
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterable


# Status -> aggregate category. Mirror of run_queue.py's _CLEAN_STATUSES and
# _ATTENTION_STATUSES — kept here so this module is self-contained for tests.
#
# `skipped-done` is included in FINISHED because the state-sidecar record
# carries faithful prior-run input/output byte measurements; aggregating
# those preserves the file_complete hook's "cumulative savings" contract
# across sessions. `skipped-exists` deliberately stays in SKIPPED — the
# output file happened to exist on disk but we have no prior-run metadata
# attesting to its bytes, so claiming them as our savings would be a lie.
_FINISHED_STATUSES = {"ok", "skipped-done"}
_FAILED_STATUSES_PREFIXES = ("failed",)  # "failed-gen" / "failed-exit-N" / ...
_STOPPED_STATUSES = {
    "stopped-threshold", "stopped-threshold-crf-exhausted",
    "chunk-choked", "awaiting-chunk-fix", "stopped-by-user",
    "pre-flight-failed",
}
_SKIPPED_STATUSES = {"skipped-exists", "skipped-not-found"}


def compute_queue_counters(job_reports: list[dict], *,
                           total_jobs: int, queue_wall_seconds: float,
                           upcoming_index: int) -> dict[str, str]:
    """Build the X265_QUEUE_* dict for the job about to start.

    `upcoming_index` is the 1-based position the about-to-start job will get
    in the report. `job_reports` is the list of every job already attempted
    (each dict has at least `status`, `input_bytes`, `output_bytes`)."""
    finished = sum(1 for j in job_reports
                   if j.get("status") in _FINISHED_STATUSES)
    failed = sum(1 for j in job_reports
                 if any(j.get("status", "").startswith(p)
                        for p in _FAILED_STATUSES_PREFIXES))
    stopped = sum(1 for j in job_reports
                  if j.get("status") in _STOPPED_STATUSES)
    skipped = sum(1 for j in job_reports
                  if j.get("status") in _SKIPPED_STATUSES)

    bytes_in = sum(_safe_int(j.get("input_bytes")) for j in job_reports
                   if j.get("status") in _FINISHED_STATUSES)
    bytes_out = sum(_safe_int(j.get("output_bytes")) for j in job_reports
                    if j.get("status") in _FINISHED_STATUSES)
    pct_saved = ((bytes_in - bytes_out) / bytes_in * 100.0
                 if bytes_in > 0 else 0.0)

    # remaining = total_jobs - upcoming_index works when total_jobs is the
    # CURRENT snapshot size. The about-to-start job is included in the count
    # of "in flight or pending", so `remaining` here means "AFTER this one
    # completes, how many more in the queue?" — matches the spec.
    remaining = max(0, total_jobs - upcoming_index)

    return {
        "X265_QUEUE_INDEX": str(upcoming_index),
        "X265_QUEUE_TOTAL": str(total_jobs),
        # FINISHED is the count of past ok jobs — NOT including this one.
        # FileCompleteHook (success-only) adds 1 in its env builder so the
        # downstream script sees "3 of 8 done"; JobEndHook inherits as-is,
        # which is correct on failure/stop paths where this job didn't
        # actually finish.
        "X265_QUEUE_ITEMS_FINISHED": str(finished),
        "X265_QUEUE_ITEMS_REMAINING": str(remaining),
        "X265_QUEUE_ITEMS_FAILED": str(failed),
        "X265_QUEUE_ITEMS_STOPPED": str(stopped),
        "X265_QUEUE_ITEMS_SKIPPED": str(skipped),
        "X265_QUEUE_BYTES_IN_SO_FAR": str(bytes_in),
        "X265_QUEUE_BYTES_OUT_SO_FAR": str(bytes_out),
        "X265_QUEUE_PCT_SAVED_SO_FAR": f"{pct_saved:.2f}",
        "X265_QUEUE_WALL_SECONDS": f"{queue_wall_seconds:.2f}",
    }


def _safe_int(v) -> int:
    """Treat None / non-numeric as 0 in aggregate math. The aggregates would
    otherwise corrupt under a placeholder row with `output_bytes: None`."""
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


@contextmanager
def overlay_env(overrides: dict[str, str]):
    """Context manager: set `overrides` on os.environ for the duration, then
    restore. Subprocesses spawned in the block inherit the overlay; the queue
    runner's own env is untouched on exit. Use around the per-job
    `run_one_job` call — the child encoder + its hook subprocesses then see
    the live counters via standard env inheritance."""
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for k, original in saved.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original


def hook_keys() -> Iterable[str]:
    """Public list of every X265_QUEUE_* env key the overlay touches. Used by
    tests to assert hermetic restoration (no key leaks out of the overlay)."""
    return (
        "X265_QUEUE_INDEX", "X265_QUEUE_TOTAL",
        "X265_QUEUE_ITEMS_FINISHED", "X265_QUEUE_ITEMS_REMAINING",
        "X265_QUEUE_ITEMS_FAILED", "X265_QUEUE_ITEMS_STOPPED",
        "X265_QUEUE_ITEMS_SKIPPED",
        "X265_QUEUE_BYTES_IN_SO_FAR", "X265_QUEUE_BYTES_OUT_SO_FAR",
        "X265_QUEUE_PCT_SAVED_SO_FAR", "X265_QUEUE_WALL_SECONDS",
    )

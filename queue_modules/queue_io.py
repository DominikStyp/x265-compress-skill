"""Queue JSON I/O with a small mid-edit retry. Editing queue.json while the
runner is mid-flight is supported; the retry tolerates catching the file
in an inconsistent state for a fraction of a second (most editors use
atomic rename, but a few don't).

Three functions:

  load_queue            -- single-shot read, returns (defaults, raw_jobs).
                           Caller is responsible for `expand_jobs` on
                           the raw list to handle globs + relative paths.

  reload_queue_with_retry  -- wraps load_queue + expand_jobs with a single
                              retry after a short pause. Returns mtime
                              alongside the data so the runner can detect
                              whether the file actually changed.

  emit_status_record    -- append one NDJSON record for a finished job so a
                           fleet monitor can `tail -f` the queue's
                           progress. Best-effort: a logging hiccup must
                           never break the run.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .job_schema import expand_jobs


def load_queue(queue_path: Path) -> tuple[dict, list[dict]]:
    """Parse the JSON. Accepts either a flat list of jobs or an object
    with `defaults` + `jobs`. Returns (defaults, jobs); defaults is an
    empty dict for the flat-list form."""
    with queue_path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {}, data
    if isinstance(data, dict):
        return data.get("defaults", {}), data.get("jobs", [])
    raise SystemExit(
        f"ERROR: queue file must contain a list or an object, "
        f"got {type(data).__name__}"
    )


def reload_queue_with_retry(queue_path: Path
                           ) -> tuple[float, dict, list[dict]]:
    """Load + expand the queue. One retry after a short pause to tolerate
    a partial-write mid-edit. Re-raises the second exception so the
    caller decides whether to abort or fall back.

    Returns (mtime, defaults, expanded_jobs). The mtime lets the runner
    detect whether the file actually changed between calls."""
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            mtime = queue_path.stat().st_mtime
            defaults, raw = load_queue(queue_path)
            return mtime, defaults, expand_jobs(raw, queue_path.parent)
        except Exception as e:
            last_exc = e
            if attempt == 1:
                time.sleep(0.4)
    assert last_exc is not None
    raise last_exc


def emit_status_record(path, row: dict) -> None:
    """Append one NDJSON record for a finished job so a fleet monitor can
    `tail -f` the queue's progress. Kept off stdout (which stays human-
    readable). Best-effort — a logging hiccup must never break the run."""
    try:
        rec = {
            "input": row.get("input"),
            "status": row.get("status"),
            "output": row.get("output"),
            "input_bytes": row.get("input_bytes"),
            "output_bytes": row.get("output_bytes"),
            "elapsed_seconds": row.get("elapsed_seconds"),
            "vmaf_mean": row.get("vmaf_mean"),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
    except Exception as e:
        print(f"WARNING: --json-status write failed: {e}", file=sys.stderr)

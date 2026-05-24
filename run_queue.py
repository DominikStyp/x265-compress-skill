"""
Queue runner: process a JSON list of encoding jobs sequentially.

Usage:
    python run_queue.py <queue.json>
    python run_queue.py <queue.json> --stop-on-failure
    python run_queue.py <queue.json> --no-skip-existing
    python run_queue.py <queue.json> --json-status status.ndjson

Exit code: 0 = all jobs clean, 1 = at least one real failure, 2 = no hard
failure but a job needs attention (size-guard abort, chunks awaiting a
manual fix, missing input, corrupt source).

Live-reload: re-reads queue.json before every job, so edits to pending jobs
or `defaults` apply at the next job boundary — no restart needed.

JSON shapes accepted, per-job key schema, and the threshold/exit-code
behaviour are all documented in the skill's SKILL.md. Implementation is
split into queue_modules:
    job_schema -- VALID_KEYS + merge_job + build_compress_argv + globs.
    queue_io   -- load_queue + reload_queue_with_retry.
    job_runner -- generate_bat + run_bat + build_job_row + run_one_job.

This file owns ONLY the orchestration loop: live-reload, skip rules,
summary stats, aggregate-report writer dispatch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from platform_compat import enable_utf8_io
from queue_modules.job_runner import run_one_job
from queue_modules.job_schema import derive_output_path, merge_job
from queue_modules.queue_io import reload_queue_with_retry


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queue_file", help="Path to JSON queue file")
    ap.add_argument("--stop-on-failure", action="store_true",
                    help="Stop the queue on the first real failure. "
                         "Threshold-aborts (exit 3) never stop the queue "
                         "regardless of this flag.")
    ap.add_argument("--no-skip-existing", action="store_true",
                    help="Don't skip jobs whose final .mkv output already exists.")
    ap.add_argument("--json-status", metavar="PATH", default=None,
                    help="Append one NDJSON record per finished job to PATH "
                         "(machine-readable; stdout stays human-readable). "
                         "Tail it to watch a queue run live.")
    return ap.parse_args()


def _skip_if_missing_or_existing(merged: dict, *, i: int, n: int,
                                no_skip_existing: bool) -> dict | None:
    """Pre-encode skip checks. Returns a placeholder report row if the job
    should be skipped (and prints the SKIP line); None if encoding should
    proceed."""
    input_path = Path(merged["input"])
    if not input_path.is_file():
        print(f"[{i}/{n}] SKIP — input not found: {input_path}")
        return {
            "input": str(input_path), "output": None,
            "input_bytes": 0, "output_bytes": None,
            "crf": merged.get("crf"), "preset": merged.get("preset"),
            "parallel": merged.get("parallel"),
            "max_size_percent": merged.get("max_size_percent"),
            "elapsed_seconds": None, "status": "skipped-not-found",
        }
    out_path = derive_output_path(input_path)
    if out_path.exists() and not no_skip_existing:
        print(f"[{i}/{n}] SKIP — output exists: {out_path.name}")
        return {
            "input": str(input_path), "output": str(out_path),
            "input_bytes": input_path.stat().st_size,
            "output_bytes": out_path.stat().st_size,
            "crf": merged.get("crf"), "preset": merged.get("preset"),
            "parallel": merged.get("parallel"),
            "max_size_percent": merged.get("max_size_percent"),
            "elapsed_seconds": None, "status": "skipped-exists",
        }
    return None


def _pick_next_job(jobs: list[dict], seen_inputs: set[str],
                  attempted_inputs: set[str]) -> dict | None:
    """Walk the current snapshot in order, growing seen_inputs (which is
    the report-table denominator — never shrinks) and returning the first
    unattempted job. Returns None if every job has been attempted."""
    for raw in jobs:
        input_str = str(Path(raw["input"]).resolve())
        seen_inputs.add(input_str)
        if input_str not in attempted_inputs:
            return raw
    return None


def _print_summary_table(job_reports: list[dict]) -> None:
    print()
    print("=" * 70)
    print("QUEUE COMPLETE")
    print("=" * 70)
    name_w = max((len(j["input"]) for j in job_reports), default=20)
    for idx, j in enumerate(job_reports, 1):
        print(f"  [{idx:>3}] {j['status']:<22} {j['input']:<{name_w}}")


def _write_aggregate_reports(skill_dir: Path, queue_path: Path,
                            job_reports: list[dict]) -> None:
    """Write the per-run + incremental markdown reports under <queue_dir>/.tmp.
    Failures are warned, not fatal — queue completion shouldn't depend on
    report generation."""
    try:
        sys.path.insert(0, str(skill_dir))
        import report  # noqa: WPS433
        tmp_dir = queue_path.parent / ".tmp"
        per_run_path, incremental_path = report.write_run_pair(
            tmp_dir,
            queue_stem=queue_path.stem,
            queue_name=queue_path.name,
            jobs=job_reports,
        )
        print(f"\nPer-run report:    {per_run_path}")
        print(f"Incremental report: {incremental_path}")
    except Exception as e:
        print(f"WARNING: failed to write aggregate report: {e}",
              file=sys.stderr)


# Per-job status -> aggregate category for the process exit code. "clean":
# nothing to do. "attention": the encode ran but left a state needing a human
# decision. Anything else is treated as a real failure (fail-safe).
_CLEAN_STATUSES = {"ok", "skipped-exists"}
_ATTENTION_STATUSES = {
    "stopped-threshold", "awaiting-chunk-fix", "skipped-not-found",
    "pre-flight-failed", "chunk-choked", "stopped-by-user",
}


def _aggregate_exit_code(job_reports: list[dict]) -> int:
    """Map the run's per-job statuses to a process exit code so a fleet
    runner can branch on $?:

        0  every job clean (ok, or output already existed)
        1  at least one real failure (compress.py crashed, bad output, ...)
        2  no hard failure, but a job needs attention (size-guard abort,
           chunks awaiting a manual fix, missing input, corrupt source)

    A hard failure (1) outranks a needs-attention (2). Unknown statuses are
    treated as failures so a new state surfaces loudly, not as 'clean'."""
    has_failure = False
    has_attention = False
    for j in job_reports:
        status = j.get("status", "")
        if status in _CLEAN_STATUSES:
            continue
        if status in _ATTENTION_STATUSES:
            has_attention = True
        else:
            has_failure = True
    if has_failure:
        return 1
    if has_attention:
        return 2
    return 0


def _emit_json_status(path, row: dict) -> None:
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


def _should_halt_after(status: str, stop_on_failure: bool) -> bool:
    """Whether to stop launching further queue jobs after a job ended with
    `status`. A user-requested finish (`stopped-by-user`) always halts the
    queue — that's the point of the feature ('stop the queue too'). A real
    failure halts only under --stop-on-failure."""
    if status == "stopped-by-user":
        return True
    return stop_on_failure and status.startswith("failed")


def main() -> int:
    enable_utf8_io()  # status/report output -> utf-8 even when redirected
    args = _parse_args()
    queue_path = Path(args.queue_file).resolve()
    if not queue_path.is_file():
        sys.exit(f"ERROR: queue file not found: {queue_path}")

    skill_dir = Path(__file__).resolve().parent
    compress_py = skill_dir / "compress.py"
    if not compress_py.is_file():
        sys.exit(f"ERROR: compress.py missing at {compress_py}")

    print(f"Queue: live-reloading {queue_path} before each job.")
    if args.json_status:
        # Truncate up front so a re-run starts a clean per-run status stream.
        try:
            open(args.json_status, "w", encoding="utf-8").close()
        except OSError as e:
            sys.exit(f"ERROR: cannot write --json-status file "
                     f"{args.json_status}: {e}")

    # `attempted_inputs` = jobs we've already started (by absolute resolved
    # path), so a job that already ran — ok, failed, threshold-aborted, or
    # skipped — never loops back even if it still appears in queue.json.
    # `seen_inputs` = union of every input observed across reloads; its
    # size is the [i/n] denominator so the count stays stable when the
    # user removes rows mid-run.
    job_reports: list[dict] = []
    attempted_inputs: set[str] = set()
    seen_inputs: set[str] = set()
    last_mtime: float | None = None
    job_counter = 0

    while True:
        # JSON parse errors mid-edit are tolerated only after we've
        # successfully read it at least once; at startup a bad file is
        # fatal so a user typo doesn't silently kill the queue.
        try:
            cur_mtime, defaults, jobs = reload_queue_with_retry(queue_path)
        except Exception as e:
            if last_mtime is None:
                sys.exit(f"ERROR: failed to read queue: {e}")
            print(f"WARNING: queue.json reload failed ({e}); ending queue.",
                  file=sys.stderr)
            break

        if not jobs:
            if last_mtime is None:
                sys.exit("ERROR: no jobs in queue.")
            print("Queue empty after reload; ending.")
            break

        if last_mtime is not None and cur_mtime != last_mtime:
            print(f"  Queue updated: reloaded {queue_path.name} "
                  f"(now {len(jobs)} job(s)).")
        last_mtime = cur_mtime

        next_job = _pick_next_job(jobs, seen_inputs, attempted_inputs)
        if next_job is None:
            break

        # Mark attempted up-front so a transient failure can't put us in
        # an infinite loop on the same job.
        input_str = str(Path(next_job["input"]).resolve())
        attempted_inputs.add(input_str)
        job_counter += 1
        merged = merge_job(defaults, next_job)

        skip_row = _skip_if_missing_or_existing(
            merged, i=job_counter, n=len(seen_inputs),
            no_skip_existing=args.no_skip_existing,
        )
        if skip_row is not None:
            job_reports.append(skip_row)
            if args.json_status:
                _emit_json_status(args.json_status, skip_row)
            continue

        status, row = run_one_job(
            compress_py=compress_py, merged=merged,
            i=job_counter, n=len(seen_inputs),
        )
        job_reports.append(row)
        if args.json_status:
            _emit_json_status(args.json_status, row)

        if _should_halt_after(status, args.stop_on_failure):
            if status == "stopped-by-user":
                print("Queue halted: stopped by user — re-run to resume.")
            break

    _print_summary_table(job_reports)
    _write_aggregate_reports(skill_dir, queue_path, job_reports)
    return _aggregate_exit_code(job_reports)


if __name__ == "__main__":
    sys.exit(main())

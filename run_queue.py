"""
Queue runner: process a JSON list of encoding jobs sequentially.

Usage:
    python run_queue.py <queue.json>
    python run_queue.py <queue.json> --stop-on-failure
    python run_queue.py <queue.json> --no-skip-existing

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
import sys
from pathlib import Path

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


def main() -> int:
    args = _parse_args()
    queue_path = Path(args.queue_file).resolve()
    if not queue_path.is_file():
        sys.exit(f"ERROR: queue file not found: {queue_path}")

    skill_dir = Path(__file__).resolve().parent
    compress_py = skill_dir / "compress.py"
    if not compress_py.is_file():
        sys.exit(f"ERROR: compress.py missing at {compress_py}")

    print(f"Queue: live-reloading {queue_path} before each job.")

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
            continue

        status, row = run_one_job(
            compress_py=compress_py, merged=merged,
            i=job_counter, n=len(seen_inputs),
        )
        job_reports.append(row)

        if status.startswith("failed") and args.stop_on_failure:
            break

    _print_summary_table(job_reports)
    _write_aggregate_reports(skill_dir, queue_path, job_reports)

    real_failures = sum(1 for j in job_reports
                        if j["status"].startswith("failed"))
    return 0 if not real_failures else 1


if __name__ == "__main__":
    sys.exit(main())

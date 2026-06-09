"""Run-end reporting helpers extracted from ``run_queue.py`` so neither
module exceeds the project's 500-line cap.

Both helpers are best-effort: a report write failure must NEVER make the
queue runner report a different exit code than it would have without the
reporting step — encoding correctness is the load-bearing work."""
from __future__ import annotations

import sys
from pathlib import Path

from encode_modules.log_paths import logs_dir


def print_summary_table(job_reports: list[dict]) -> None:
    """Console banner at end of queue run. Single column of
    ``[idx] status<22 input``. Pure formatting; no failure modes."""
    print()
    print("=" * 70)
    print("QUEUE COMPLETE")
    print("=" * 70)
    name_w = max((len(j["input"]) for j in job_reports), default=20)
    for idx, j in enumerate(job_reports, 1):
        print(f"  [{idx:>3}] {j['status']:<22} {j['input']:<{name_w}}")


def write_aggregate_reports(skill_dir: Path, queue_path: Path,
                            job_reports: list[dict]) -> None:
    """Write the per-run + incremental markdown reports under
    ``<queue_dir>/logs/`` (v1.19.0 layout). Failures are warned, not
    fatal — queue completion must not depend on report generation."""
    try:
        sys.path.insert(0, str(skill_dir))
        import report  # noqa: WPS433
        target_dir = logs_dir(queue_path.parent)
        per_run_path, incremental_path = report.write_run_pair(
            target_dir,
            queue_stem=queue_path.stem,
            queue_name=queue_path.name,
            jobs=job_reports,
        )
        print(f"\nPer-run report:    {per_run_path}")
        print(f"Incremental report: {incremental_path}")
    except Exception as e:
        print(f"WARNING: failed to write aggregate report: {e}",
              file=sys.stderr)

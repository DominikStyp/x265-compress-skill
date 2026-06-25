"""Finding #6 (v1.20.1): queue_modules/queue_reporting.py had no by-name test —
and write_aggregate_reports is an untested I/O seam (it writes markdown report
files to disk). These cover the console summary, the on-disk report emission,
and the best-effort never-raise contract (a report failure must not change the
queue's exit behaviour).
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.log_paths import logs_dir  # noqa: E402
from queue_modules import queue_reporting  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _jobs() -> list[dict]:
    return [
        {"input": "a.mp4", "status": "ok", "input_bytes": 1000,
         "output_bytes": 600, "elapsed_seconds": 12.0, "crf": 22,
         "preset": "slow", "vmaf_mean": 96.5, "quality_method": "libvmaf"},
        {"input": "b.mkv", "status": "stopped-threshold",
         "input_bytes": 2000, "output_bytes": None},
    ]


class PrintSummaryTableTest(unittest.TestCase):
    def test_banner_lists_each_job(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            queue_reporting.print_summary_table(_jobs())
        out = buf.getvalue()
        self.assertIn("QUEUE COMPLETE", out)
        self.assertIn("a.mp4", out)
        self.assertIn("b.mkv", out)
        self.assertIn("stopped-threshold", out)

    def test_empty_reports_does_not_crash(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            queue_reporting.print_summary_table([])
        self.assertIn("QUEUE COMPLETE", buf.getvalue())


class WriteAggregateReportsTest(unittest.TestCase):
    def test_emits_report_files_under_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            queue_path = Path(td) / "queue.json"
            queue_path.write_text("{}", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                queue_reporting.write_aggregate_reports(
                    REPO_ROOT, queue_path, _jobs())
            # The per-run + incremental markdown reports land under logs/.
            mds = list(logs_dir(queue_path.parent).glob("*.md"))
            self.assertTrue(mds, "expected at least one .md report written")
            self.assertIn("report", buf.getvalue().lower())

    def test_failure_is_swallowed_not_fatal(self) -> None:
        # A report-write failure must NEVER raise out of the queue runner —
        # it warns to stderr and returns. Patch the underlying report writer.
        import report
        with tempfile.TemporaryDirectory() as td:
            queue_path = Path(td) / "queue.json"
            queue_path.write_text("{}", encoding="utf-8")
            err = io.StringIO()
            with mock.patch.object(report, "write_run_pair",
                                   side_effect=OSError("disk full")), \
                 redirect_stderr(err), redirect_stdout(io.StringIO()):
                # Must not raise.
                queue_reporting.write_aggregate_reports(
                    REPO_ROOT, queue_path, _jobs())
            self.assertIn("WARNING", err.getvalue())


if __name__ == "__main__":
    unittest.main()

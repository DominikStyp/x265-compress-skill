"""Finding #6 (v1.20.1): encode_modules/messages.py had no direct test — its
print blocks were only exercised transitively through the big encode entry
points, where their output was never asserted. These pin the user-facing
blocks that carry ACTIONABLE detail (workdir paths, recovery instructions,
the chunk that failed) so a regression in that text is caught.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import messages  # noqa: E402


def _stdout(fn, *a, **k) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


class EncodePlanTest(unittest.TestCase):
    def test_header_counts_and_size_guard(self) -> None:
        out = _stdout(messages.print_encode_plan, ["a", "b"], 10, 3,
                      parallel=4, cores_per_chunk=6, first_pos=4,
                      max_output_bytes=800 * 1024 * 1024, source_bytes=10**9)
        self.assertIn("2 of 10", out)
        self.assertIn("parallel=4", out)
        self.assertIn("Max-size guard", out)
        self.assertIn("next chunk: 4/10", out)

    def test_no_size_guard_line_when_unset(self) -> None:
        out = _stdout(messages.print_encode_plan, ["a"], 1, 0,
                      parallel=1, cores_per_chunk=8, first_pos=None,
                      max_output_bytes=None, source_bytes=10**9)
        self.assertNotIn("Max-size guard", out)


class RuntimeProtectionsTest(unittest.TestCase):
    def test_job_protection_present(self) -> None:
        out = _stdout(messages.print_runtime_protections, True)
        self.assertIn("Job Object", out)

    def test_missing_job_protection_warns_on_stderr(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            messages.print_runtime_protections(False)
        self.assertIn("orphan", buf.getvalue().lower())


class SkippedBlockTest(unittest.TestCase):
    def test_carries_workdir_and_chunk_detail(self) -> None:
        wd = Path("/v/.tmp/.compress_movie")
        skipped = [{
            "chunk_name": "src_0007.mkv",
            "time_range_seconds": [120, 180],
            "choke_speed": 0.0041,
            "choke_wall_seconds": 95,
            "error_count": 12,
        }]
        out = _stdout(messages.print_chunks_skipped_block, wd, skipped)
        self.assertIn("ENCODE INCOMPLETE", out)
        self.assertIn("src_0007.mkv", out)
        self.assertIn(str(wd), out)
        self.assertIn("12 decode errors", out)


class ThresholdAbortBlockTest(unittest.TestCase):
    def test_shows_reason_and_workdir(self) -> None:
        wd = Path("/v/.tmp/.compress_movie")
        out = _stdout(messages.print_threshold_abort_block, wd,
                      "Estimated output 850 MB (85%) exceeds threshold.")
        self.assertIn("ENCODING STOPPED", out)
        self.assertIn("850 MB", out)
        self.assertIn(str(wd), out)


class QualityThresholdAbortBlockTest(unittest.TestCase):
    def test_shows_chunk_vmaf_and_threshold(self) -> None:
        wd = Path("/v/.tmp/.compress_movie")
        out = _stdout(messages.print_quality_threshold_abort_block, wd,
                      chunk_idx=3, chunk_name="src_0004.mkv",
                      vmaf_mean=84.5, threshold=90.0)
        self.assertIn("QUALITY THRESHOLD FAILED", out)
        self.assertIn("src_0004.mkv", out)
        self.assertIn("84.50", out)
        # chunk_idx is 0-based internally; the block shows the 1-based number.
        self.assertIn("Chunk 4", out)


class FinishStoppedBlockTest(unittest.TestCase):
    def test_shows_done_remaining_and_workdir(self) -> None:
        wd = Path("/v/.tmp/.compress_movie")
        out = _stdout(messages.print_finish_stopped_block, wd, 3, 10)
        self.assertIn("STOPPED BY USER", out)
        self.assertIn("7/10", out)      # done = total - remaining
        self.assertIn("3 remaining", out)
        self.assertIn(str(wd), out)


if __name__ == "__main__":
    unittest.main()

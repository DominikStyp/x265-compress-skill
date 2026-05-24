"""Tier 1-D: archival-confidence surfacing.

(1) format_quality_summary grades the WORST frame, not just the mean — the
    decision to delete an original rides on the quality floor.
(2) print_summary, given a transparent VMAF, tells the user the source is
    untouched and safe for *them* to delete by hand (the tool never deletes
    it). Below transparent, it stays quiet about deletion.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.quality_format import format_quality_summary  # noqa: E402
from encode_modules.reporting import print_summary  # noqa: E402


class WorstFrameGradeTest(unittest.TestCase):
    def test_worst_frame_graded_separately_from_mean(self) -> None:
        s = format_quality_summary({
            "vmaf_mean": 97.0, "vmaf_min": 82.0,
            "sampling_mode": "chunks", "frames_evaluated": 100,
        })
        # Mean 97 → TRANSPARENT; worst 82 → GOOD. Both grades present proves
        # the floor is graded on its own, not just shown as a bare number.
        self.assertIn("TRANSPARENT", s)
        self.assertIn("GOOD", s)
        self.assertIn("82.0", s)

    def test_absent_worst_frame_is_handled(self) -> None:
        s = format_quality_summary({
            "vmaf_mean": 97.0, "vmaf_min": None,
            "sampling_mode": "full", "frames_evaluated": 50,
        })
        self.assertIn("97", s)  # no crash, mean still shown


class SafeToDeleteTest(unittest.TestCase):
    def _run(self, vmaf) -> str:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.mp4"
            src.write_bytes(b"x" * 2_000_000)
            dst = Path(td) / "out.mkv"
            dst.write_bytes(b"y" * 1_000_000)
            buf = io.StringIO()
            with redirect_stdout(buf):
                scores = {"vmaf_mean": vmaf} if vmaf is not None else None
                print_summary(src, dst, scores)
            return buf.getvalue()

    def test_transparent_vmaf_reports_safe_to_delete(self) -> None:
        out = self._run(97.3)
        self.assertIn("untouched", out.lower())
        self.assertIn("safe to delete", out.lower())

    def test_below_transparent_stays_quiet_about_deletion(self) -> None:
        out = self._run(80.0)
        self.assertNotIn("safe to delete", out.lower())

    def test_no_quality_scores_is_backward_compatible(self) -> None:
        out = self._run(None)
        self.assertIn("Saved", out)
        self.assertNotIn("safe to delete", out.lower())


if __name__ == "__main__":
    unittest.main()

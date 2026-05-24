"""Tier 1-E: extracting the size-projection/threshold logic out of display.py
(to stay under the 500-line module cap) must NOT change behavior.

These are characterization tests: they pin the projection math and the
threshold-abort trigger, and pass both before and after the extraction.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.display import ParallelDisplay  # noqa: E402

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _display(wd: Path, enc_bytes: int, **kw) -> ParallelDisplay:
    (wd / "enc_0001.mkv").write_bytes(b"\0" * enc_bytes)
    d = ParallelDisplay(parallel=1, total=10, already_done=1, workdir=wd, **kw)
    d.completed_duration_sum = 50.0  # 50 of 100s encoded => 50% progress
    return d


class ProjectionMathTest(unittest.TestCase):
    def test_projection_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _display(Path(td), 1_000_000, total_duration_sec=100.0)
            proj = d._compute_projection()
            self.assertEqual(proj["enc_bytes"], 1_000_000)
            self.assertAlmostEqual(proj["encoded_s"], 50.0)
            self.assertAlmostEqual(proj["progress_frac"], 0.5)
            # bytes_per_sec = 1e6/50 = 20000 ; projected = *100 = 2e6
            self.assertAlmostEqual(proj["projected_bytes"], 2_000_000)

    def test_quarantined_part_is_not_counted(self) -> None:
        # The projection must count finished chunks (enc_*.mkv) and the LIVE
        # in-progress partial (enc_*.part.mkv), but NOT quarantined partials
        # (enc_*.part.<tag>-<ts>.mkv) left by a prior choked run — those are
        # abandoned bytes and would inflate the size estimate on resume.
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "enc_0001.mkv").write_bytes(b"\0" * 1_000_000)        # final
            (wd / "enc_0002.part.mkv").write_bytes(b"\0" * 500_000)     # live
            (wd / "enc_0003.part.choked-aside-123.mkv").write_bytes(
                b"\0" * 9_000_000)                                      # quarantine
            d = ParallelDisplay(parallel=1, total=10, already_done=1,
                                workdir=wd, total_duration_sec=100.0)
            d.completed_duration_sum = 50.0
            self.assertEqual(d._compute_projection()["enc_bytes"], 1_500_000)

    def test_projection_gated_below_5pct(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "enc_0001.mkv").write_bytes(b"\0" * 1_000_000)
            d = ParallelDisplay(parallel=1, total=100, already_done=1,
                                workdir=Path(td), total_duration_sec=1000.0)
            d.completed_duration_sum = 10.0  # 10/1000 = 1% < 5% gate
            self.assertIsNone(d._compute_projection()["projected_bytes"])


class ThresholdAbortTest(unittest.TestCase):
    def test_over_threshold_aborts_and_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _display(Path(td), 1_000_000, total_duration_sec=100.0,
                         source_bytes=4_000_000, max_output_bytes=1_000_000)
            proc = subprocess.Popen(_SLEEPER)  # projected 2e6 >> 1e6 budget
            try:
                d.register_proc(0, proc)
                d.check_threshold()
                self.assertTrue(d.abort_event.is_set())
                self.assertNotEqual(d.abort_reason, "")
                self.assertIsNotNone(proc.wait(timeout=10))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

    def test_under_threshold_does_not_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _display(Path(td), 1_000_000, total_duration_sec=100.0,
                         source_bytes=100_000_000, max_output_bytes=5_000_000)
            d.check_threshold()  # projected 2e6 < 5e6 budget
            self.assertFalse(d.abort_event.is_set())


if __name__ == "__main__":
    unittest.main()

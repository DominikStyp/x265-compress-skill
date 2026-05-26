"""The quality check renders ONE overall progress bar across the sampled
chunks (0→100% weighted by chunk duration, annotated `chunk i/N`), rather than
a fresh per-chunk bar. `_quality_check_run` is reduced to firing an
`on_progress(out_s, fps, speed)` callback; the dispatcher owns the bar.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import quality  # noqa: E402

_SCORES = {"vmaf_mean": 97.0, "vmaf_min": 90.0, "vmaf_harmonic_mean": 96.0,
           "psnr_y_mean": 45.0, "ssim_mean": 0.99, "frames_evaluated": 100}


def _workdir_with_chunks(td: str, n: int) -> Path:
    wd = Path(td)
    for i in range(n):
        (wd / f"src_{i:04d}.mkv").write_bytes(b"s")
        (wd / f"enc_src_{i:04d}.mkv").write_bytes(b"e")
    return wd


class QualityOverallBarTest(unittest.TestCase):
    def test_overall_bar_accumulates_across_sampled_chunks(self) -> None:
        captured_kwargs: list[dict] = []

        def fake_run(src, dst, *, subsample=10, seek_start=None,
                     duration=None, on_progress=None):
            captured_kwargs.append({"on_progress": on_progress})
            if on_progress is not None:
                on_progress(5.0, "30", "1.0x")    # halfway through this chunk
                on_progress(10.0, "30", "1.0x")   # this chunk done
            return dict(_SCORES)

        with tempfile.TemporaryDirectory() as td:
            wd = _workdir_with_chunks(td, 10)      # indices [1,5,9] for n=3
            with mock.patch.object(quality, "probe_duration",
                                   return_value=10.0), \
                 mock.patch.object(quality, "_quality_check_run",
                                   side_effect=fake_run):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    scores = quality.quality_check_chunks(
                        wd, n_chunks=3, progress_prefix="  Quality check:")
                out = buf.getvalue()

        # Aggregation still works.
        self.assertIsNotNone(scores)
        self.assertEqual(scores["method"], "chunks")
        # 3 sampled chunks × 10s = 30s total. The bar must reach 100% only at
        # the LAST chunk's completion — i.e. it's an OVERALL bar, not per-chunk.
        self.assertIn("100.0%", out)
        self.assertIn("chunk 1/3", out)
        self.assertIn("chunk 3/3", out)
        # Mid-pass value proving accumulation (chunk 1 done = 10/30 = 33.3%).
        self.assertIn("33.3%", out)
        # The per-chunk runner no longer renders itself — it's handed a callback.
        self.assertTrue(all(k["on_progress"] is not None for k in captured_kwargs))

    def test_no_prefix_means_no_progress_callback(self) -> None:
        seen: list[dict] = []

        def fake_run(src, dst, *, subsample=10, seek_start=None,
                     duration=None, on_progress=None):
            seen.append({"on_progress": on_progress})
            return dict(_SCORES)

        with tempfile.TemporaryDirectory() as td:
            wd = _workdir_with_chunks(td, 10)
            with mock.patch.object(quality, "probe_duration",
                                   return_value=10.0), \
                 mock.patch.object(quality, "_quality_check_run",
                                   side_effect=fake_run):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    quality.quality_check_chunks(wd, n_chunks=3)  # no prefix
                self.assertEqual(buf.getvalue(), "")               # silent
        self.assertTrue(all(k["on_progress"] is None for k in seen))


if __name__ == "__main__":
    unittest.main()

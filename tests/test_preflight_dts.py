"""Pre-flight must NOT fail an otherwise-fine source whose only decode-walk
output is benign dup-DTS muxer warnings (exit 0, "non monotonically increasing
dts"). Real-world trigger: sources cut with Machete carry duplicate DTS at the
join points; the file plays fine and the chunked x265 pass re-stamps timestamps.

The codebase already treats dup-DTS as non-fatal POST-encode (verify_loop ->
is_dts_only_verify_failure); these tests pin that the INPUT/pre-flight path now
applies the same carve-out — while a window mixing dup-DTS with a real decode
error still fails (no masking of genuine corruption).
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import pre_flight, verify  # noqa: E402

_DTS_LINE = ("[null @ 0x] Application provided invalid, non monotonically "
             "increasing dts to muxer in stream 0: 3022 >= 3021")
_REAL_ERR = "[hevc @ 0x] Invalid NAL unit size (123) > remaining (45)"


class RunDecodeWalkNonDtsCount(unittest.TestCase):
    """_run_decode_walk counts REAL (non-DTS) errors over EVERY stderr line,
    so a real error past the sample-truncation limit can't be classified away."""

    def _fake_run(self, returncode: int, stderr: str):
        return lambda *a, **k: types.SimpleNamespace(returncode=returncode,
                                                     stderr=stderr)

    def test_dts_only_lines_yield_zero_non_dts(self) -> None:
        orig = verify.subprocess.run
        verify.subprocess.run = self._fake_run(0, _DTS_LINE + "\n" + _DTS_LINE)
        try:
            r = verify._run_decode_walk(Path("x.mkv"), timeout_s=10)
        finally:
            verify.subprocess.run = orig
        self.assertEqual(r["error_count"], 2)
        self.assertEqual(r["non_dts_error_count"], 0)

    def test_real_error_among_dts_is_counted(self) -> None:
        orig = verify.subprocess.run
        verify.subprocess.run = self._fake_run(1, _DTS_LINE + "\n" + _REAL_ERR)
        try:
            r = verify._run_decode_walk(Path("x.mkv"), timeout_s=10)
        finally:
            verify.subprocess.run = orig
        self.assertEqual(r["error_count"], 2)
        self.assertEqual(r["non_dts_error_count"], 1)


class PreFlightDtsCarveOut(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_walk = pre_flight._run_decode_walk
        self._orig_probe = pre_flight._probe_duration
        self._orig_write = pre_flight._write_cache
        pre_flight._probe_duration = lambda src: 120.0   # 2 windows @ 60s
        pre_flight._write_cache = lambda src, result: None

    def tearDown(self) -> None:
        pre_flight._run_decode_walk = self._orig_walk
        pre_flight._probe_duration = self._orig_probe
        pre_flight._write_cache = self._orig_write

    def _walk_returning(self, **fields):
        base = {"decode_exit_code": 0, "error_count": 0,
                "non_dts_error_count": 0, "error_samples": [],
                "elapsed_seconds": 1.0, "timed_out": False}
        base.update(fields)
        return lambda src, **kw: base

    def test_dts_only_window_passes_as_dts_warn(self) -> None:
        pre_flight._run_decode_walk = self._walk_returning(
            decode_exit_code=0, error_count=4, non_dts_error_count=0,
            error_samples=[_DTS_LINE])
        res = pre_flight.pre_flight_scan(Path("src.mp4"), seg_sec=60,
                                        use_cache=False)
        self.assertTrue(res["passed"])
        self.assertEqual(res["bad_windows"], [])
        self.assertEqual(res["dts_warn_windows"], 2)
        self.assertIn("dup-DTS", pre_flight.format_pre_flight_summary(res))

    def test_window_mixing_dts_with_real_error_still_fails(self) -> None:
        pre_flight._run_decode_walk = self._walk_returning(
            decode_exit_code=0, error_count=2, non_dts_error_count=1,
            error_samples=[_DTS_LINE, _REAL_ERR])
        res = pre_flight.pre_flight_scan(Path("src.mp4"), seg_sec=60,
                                        use_cache=False)
        self.assertFalse(res["passed"])
        self.assertTrue(res["bad_windows"])

    def test_nonzero_exit_still_fails_even_if_all_dts_lines(self) -> None:
        # A crash exit with only DTS lines is NOT the benign case (carve-out
        # requires exit 0). Fail loud.
        pre_flight._run_decode_walk = self._walk_returning(
            decode_exit_code=1, error_count=1, non_dts_error_count=0,
            error_samples=[_DTS_LINE])
        res = pre_flight.pre_flight_scan(Path("src.mp4"), seg_sec=60,
                                        use_cache=False)
        self.assertFalse(res["passed"])

    def test_fully_clean_source_has_zero_dts_warn(self) -> None:
        pre_flight._run_decode_walk = self._walk_returning()
        res = pre_flight.pre_flight_scan(Path("src.mp4"), seg_sec=60,
                                        use_cache=False)
        self.assertTrue(res["passed"])
        self.assertEqual(res["dts_warn_windows"], 0)


if __name__ == "__main__":
    unittest.main()

"""The post-encode decode walk's timeout now scales with the output's duration
instead of a flat 600 s. Regression this guards: a ~32.7-min (1959.7 s) 4K
output decoded clean but its honest low-priority decode took longer than the old
600 s cap, so it timed out and got renamed damaged_* despite being bit-perfect.
A duration-scaled budget passes any "slow but progressing" decode and only trips
on a genuine decoder hang.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import verify  # noqa: E402
from encode_modules.verify import (  # noqa: E402
    DECODE_WALK_MAX_TIMEOUT_S,
    DECODE_WALK_MIN_TIMEOUT_S,
    DECODE_WALK_TIMEOUT_FACTOR,
    decode_walk_timeout_s,
)


def _clean_walk(path, *, timeout_s, max_samples=4, **_):
    """A fake _run_decode_walk that records nothing and reports a clean pass."""
    return {"ok": True, "decode_exit_code": 0, "error_count": 0,
            "error_samples": [], "elapsed_seconds": 1.0, "timed_out": False}


class DecodeWalkTimeoutScaling(unittest.TestCase):
    def test_floor_for_unknown_or_nonpositive_duration(self) -> None:
        # A failed/zero probe must never yield a tiny — or zero, which
        # subprocess.run reads as "no timeout" — budget.
        for d in (None, 0, 0.0, -5):
            self.assertEqual(decode_walk_timeout_s(d), DECODE_WALK_MIN_TIMEOUT_S)

    def test_short_clip_stays_at_floor(self) -> None:
        # 60 s * 6 = 360 < 900 floor, so short clips still get headroom.
        self.assertEqual(decode_walk_timeout_s(60), DECODE_WALK_MIN_TIMEOUT_S)

    def test_long_file_scales_with_duration(self) -> None:
        # The reported file: ~32.7 min (1959.7 s) 4K that timed out at 600 s.
        cap = decode_walk_timeout_s(1959.7)
        self.assertEqual(cap, 11759)              # ceil(1959.7 * 6)
        self.assertGreater(cap, 600)              # old flat cap would have failed

    def test_budget_is_well_above_realtime(self) -> None:
        # The whole point: budget must exceed realtime by a wide margin so an
        # honest decode never trips it.
        self.assertGreaterEqual(DECODE_WALK_TIMEOUT_FACTOR, 4.0)
        self.assertGreater(decode_walk_timeout_s(3600), 3600)

    def test_ceiling_bounds_hang_waste_on_long_sources(self) -> None:
        # A multi-hour source must not arm a multi-hour (6x) hang wait — clamp
        # to the ceiling so a TRUE decoder hang fails in bounded time.
        self.assertEqual(decode_walk_timeout_s(36000), DECODE_WALK_MAX_TIMEOUT_S)
        # The reported 32.7-min case is well under the ceiling — not clamped.
        self.assertLess(decode_walk_timeout_s(1959.7), DECODE_WALK_MAX_TIMEOUT_S)
        # Ceiling never drops below the floor (sanity on the constants).
        self.assertGreater(DECODE_WALK_MAX_TIMEOUT_S, DECODE_WALK_MIN_TIMEOUT_S)


class DecodeCheckUsesScaledTimeout(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_walk = verify._run_decode_walk
        self._orig_probe = verify.probe_duration

    def tearDown(self) -> None:
        verify._run_decode_walk = self._orig_walk
        verify.probe_duration = self._orig_probe

    def test_probes_duration_and_passes_scaled_timeout(self) -> None:
        captured: dict = {}

        def fake_walk(path, *, timeout_s, max_samples=4, **_):
            captured["timeout_s"] = timeout_s
            return _clean_walk(path, timeout_s=timeout_s)

        verify._run_decode_walk = fake_walk
        verify.probe_duration = lambda p: 1959.7
        self.assertIsNone(verify._decode_check(Path("out.mkv")))
        self.assertEqual(captured["timeout_s"], decode_walk_timeout_s(1959.7))

    def test_provided_duration_skips_reprobe(self) -> None:
        captured: dict = {}

        def fake_walk(path, *, timeout_s, max_samples=4, **_):
            captured["timeout_s"] = timeout_s
            return _clean_walk(path, timeout_s=timeout_s)

        def boom(_p):
            raise AssertionError(
                "probe_duration must not run when duration_s is provided")

        verify._run_decode_walk = fake_walk
        verify.probe_duration = boom
        verify._decode_check(Path("out.mkv"), duration_s=1959.7)
        self.assertEqual(captured["timeout_s"], decode_walk_timeout_s(1959.7))

    def test_timeout_message_names_cap_and_blames_hang(self) -> None:
        def fake_walk(path, *, timeout_s, max_samples=4, **_):
            return {"ok": False, "decode_exit_code": -1, "error_count": 0,
                    "error_samples": [], "elapsed_seconds": float(timeout_s) + 0.1,
                    "timed_out": True}

        verify._run_decode_walk = fake_walk
        msg = verify._decode_check(Path("out.mkv"), duration_s=1959.7)
        self.assertIsNotNone(msg)
        self.assertIn("hang", msg)
        self.assertIn(str(decode_walk_timeout_s(1959.7)), msg)

    def test_real_decode_errors_still_reported(self) -> None:
        # The scaling change must not mask genuine corruption.
        def fake_walk(path, *, timeout_s, max_samples=4, **_):
            return {"ok": False, "decode_exit_code": 1, "error_count": 2,
                    "error_samples": ["bad NAL", "concealing errors"],
                    "elapsed_seconds": 3.0, "timed_out": False}

        verify._run_decode_walk = fake_walk
        msg = verify._decode_check(Path("out.mkv"), duration_s=120.0)
        self.assertIsNotNone(msg)
        self.assertIn("ffmpeg exit 1", msg)


if __name__ == "__main__":
    unittest.main()

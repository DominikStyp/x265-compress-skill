"""Characterization of compress_modules.probe._video_bitrate_kbps.

The estimator picks stream bitrate → format bitrate (minus a 192 kbps audio
allowance) → size/duration. The "minus audio allowance" path has a non-obvious
guard: if subtracting the allowance would yield ~0, it falls back to the gross
rate rather than reporting an implausible near-zero. These tests pin every
branch so the readability rewrite is provably behavior-preserving.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress_modules.probe import _video_bitrate_kbps  # noqa: E402


class VideoBitrateTest(unittest.TestCase):
    def test_stream_bitrate_wins(self) -> None:
        self.assertEqual(
            _video_bitrate_kbps({"bit_rate": "5000000"}, {}, 2, 100.0, 999), 5000)

    def test_format_bitrate_minus_audio_allowance(self) -> None:
        self.assertEqual(
            _video_bitrate_kbps({}, {"bit_rate": "8192000"}, 1, 100.0, 999), 8000)

    def test_format_bitrate_allowance_is_single_regardless_of_track_count(self) -> None:
        # The format-level path historically subtracts ONE 192 kbps allowance
        # even with multiple audio tracks (unlike the size/duration fallback,
        # which is per-track). Pin that asymmetry so it isn't "tidied" away.
        self.assertEqual(
            _video_bitrate_kbps({}, {"bit_rate": "8192000"}, 3, 100.0, 999), 8000)

    def test_format_bitrate_no_audio_is_untouched(self) -> None:
        self.assertEqual(
            _video_bitrate_kbps({}, {"bit_rate": "8192000"}, 0, 100.0, 999), 8192)

    def test_format_bitrate_below_allowance_falls_back_to_gross(self) -> None:
        # total=100 kbps, minus 192 would go negative → keep the gross rate.
        self.assertEqual(
            _video_bitrate_kbps({}, {"bit_rate": "100000"}, 1, 100.0, 999), 100)

    def test_size_duration_fallback_minus_per_track_allowance(self) -> None:
        # (125000 * 8) / 1.0 / 1000 = 1000 kbps; 2 audio tracks → 1000 - 384.
        self.assertEqual(
            _video_bitrate_kbps({}, {}, 2, 1.0, 125000), 616)

    def test_size_duration_fallback_below_allowance_keeps_gross(self) -> None:
        # total=100 kbps, 1 track → 100-192 negative → fall back to 100.
        self.assertEqual(
            _video_bitrate_kbps({}, {}, 1, 1.0, 12500), 100)

    def test_no_information_returns_zero(self) -> None:
        self.assertEqual(_video_bitrate_kbps({}, {}, 0, 0.0, 0), 0)


if __name__ == "__main__":
    unittest.main()

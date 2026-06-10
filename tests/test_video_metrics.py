"""Contract + drift-pin tests for the consolidated video-metrics helpers.

`video_metrics.py` exists so the three independent fps/BPP derivations that
used to live inline in history.build_input_block, compress_modules.probe.analyse
and the encode_resumable per-chunk-metrics block can no longer silently drift
apart. These tests pin:

  * parse_fps_fraction edge cases (the rational string the encoder feeds back to
    ffmpeg as -r) — including the plain-float-no-slash case the consolidated
    helper now understands;
  * video_stream_metrics first-video-stream extraction on canned probe dicts;
  * a DRIFT-PIN: history's adapter and compress-probe's adapter must derive the
    SAME decimal fps for the same r_frame_rate string. If a future edit perturbs
    one derivation, this test breaks before the JSONL/BPP numbers can diverge.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history as history_mod  # noqa: E402
from compress_modules import probe as cprobe  # noqa: E402
from video_metrics import (  # noqa: E402
    bits_per_pixel,
    parse_fps_fraction,
    video_stream_metrics,
)


class ParseFpsFractionTest(unittest.TestCase):
    def test_ntsc_fraction(self) -> None:
        self.assertAlmostEqual(parse_fps_fraction("30000/1001"),
                               30000 / 1001, places=9)

    def test_integer_fraction(self) -> None:
        self.assertEqual(parse_fps_fraction("50/1"), 50.0)

    def test_zero_denominator_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction("30/0"))

    def test_zero_over_zero_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction("0/0"))

    def test_plain_float_string_no_slash(self) -> None:
        # The consolidated helper understands a bare numeric string (ffprobe
        # can emit avg_frame_rate as a plain number on some containers).
        self.assertEqual(parse_fps_fraction("30"), 30.0)
        self.assertAlmostEqual(parse_fps_fraction("23.976"), 23.976, places=6)

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction("abc"))

    def test_too_many_slashes_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction("30/1/2"))

    def test_empty_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction(""))

    def test_none_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction(None))

    def test_whitespace_is_none(self) -> None:
        self.assertIsNone(parse_fps_fraction("   "))


class VideoStreamMetricsTest(unittest.TestCase):
    def _probe(self, **vid) -> dict:
        return {"streams": [{"codec_type": "audio", "codec_name": "aac"},
                            {"codec_type": "video", **vid}]}

    def test_picks_first_video_stream(self) -> None:
        m = video_stream_metrics(self._probe(
            width=3840, height=2160, codec_name="h264",
            pix_fmt="yuv420p", r_frame_rate="30000/1001"))
        self.assertEqual(m["width"], 3840)
        self.assertEqual(m["height"], 2160)
        self.assertEqual(m["codec_name"], "h264")
        self.assertEqual(m["pix_fmt"], "yuv420p")
        self.assertEqual(m["r_frame_rate"], "30000/1001")
        self.assertAlmostEqual(m["fps_decimal"], 30000 / 1001, places=9)

    def test_no_video_stream(self) -> None:
        m = video_stream_metrics({"streams": [
            {"codec_type": "audio", "codec_name": "aac"}]})
        self.assertIsNone(m["width"])
        self.assertIsNone(m["fps_decimal"])
        self.assertIsNone(m["codec_name"])

    def test_empty_probe(self) -> None:
        m = video_stream_metrics({})
        self.assertIsNone(m["width"])
        self.assertIsNone(m["fps_decimal"])

    def test_avg_frame_rate_fallback_when_requested(self) -> None:
        # r_frame_rate unusable (0/0) → fall back to avg_frame_rate when the
        # caller opts in.
        m = video_stream_metrics(
            self._probe(width=1920, height=1080, r_frame_rate="0/0",
                        avg_frame_rate="25/1"),
            avg_fps_fallback=True)
        self.assertEqual(m["fps_decimal"], 25.0)

    def test_no_avg_fallback_by_default(self) -> None:
        # Default precedence (history / encode_resumable) uses ONLY
        # r_frame_rate — no avg_frame_rate fallback.
        m = video_stream_metrics(
            self._probe(width=1920, height=1080, r_frame_rate="0/0",
                        avg_frame_rate="25/1"))
        self.assertIsNone(m["fps_decimal"])


class BitsPerPixelTest(unittest.TestCase):
    def test_basic(self) -> None:
        # 8 Mbps over 1920x1080 @ 25fps = 8e6 / (1920*1080*25).
        self.assertAlmostEqual(
            bits_per_pixel(8_000_000, 1920, 1080, 25.0),
            8_000_000 / (1920 * 1080 * 25.0), places=9)

    def test_rounding_optional(self) -> None:
        # history rounds to 6 dp; compress does not. The helper honours an
        # explicit ndigits and leaves the value unrounded when ndigits is None.
        unrounded = bits_per_pixel(8_000_000, 1920, 1080, 23.976)
        rounded = bits_per_pixel(8_000_000, 1920, 1080, 23.976, ndigits=6)
        self.assertEqual(rounded, round(unrounded, 6))

    def test_zero_fps_is_none(self) -> None:
        self.assertIsNone(bits_per_pixel(8_000_000, 1920, 1080, 0.0))

    def test_missing_bitrate_is_none(self) -> None:
        self.assertIsNone(bits_per_pixel(None, 1920, 1080, 25.0))
        self.assertIsNone(bits_per_pixel(0, 1920, 1080, 25.0))

    def test_missing_dimensions_is_none(self) -> None:
        self.assertIsNone(bits_per_pixel(8_000_000, None, 1080, 25.0))
        self.assertIsNone(bits_per_pixel(8_000_000, 1920, 0, 25.0))


class DriftPinTest(unittest.TestCase):
    """The two surviving heavy call sites (history JSONL + compress planner)
    must agree on decimal fps for the same source. They reach it through
    different adapters; this asserts they can't drift."""

    FRACTIONS = ["30000/1001", "50/1", "24/1", "60000/1001", "25/1"]

    def test_history_and_compress_agree_on_fps(self) -> None:
        for rfr in self.FRACTIONS:
            probe_json = {
                "format": {"duration": "10.0"},
                "streams": [{
                    "codec_type": "video",
                    "width": 1920, "height": 1080,
                    "codec_name": "h264", "pix_fmt": "yuv420p",
                    "r_frame_rate": rfr,
                    "bit_rate": "8000000",
                }],
            }
            hist = history_mod.build_input_block(Path("x.mp4"), probe_json)
            compress_fps = cprobe.parse_fps(rfr)
            self.assertAlmostEqual(
                hist["fps_decimal"], compress_fps, places=9,
                msg=f"history vs compress fps drift for {rfr!r}")

    def test_parse_fps_alias_still_importable(self) -> None:
        # Tests/other code may import compress_modules.probe.parse_fps; keep the
        # public name working after folding it into video_metrics.
        self.assertEqual(cprobe.parse_fps("30/1"), 30.0)
        # Old contract: failure → 0.0 (NOT None) for the compress planner.
        self.assertEqual(cprobe.parse_fps("garbage"), 0.0)
        self.assertEqual(cprobe.parse_fps("30/0"), 0.0)


if __name__ == "__main__":
    unittest.main()

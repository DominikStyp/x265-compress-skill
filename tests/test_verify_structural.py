"""verify_output: cheap structural checks gate the expensive decode pass.

`verify_output` is the post-encode gate. Its contract is a strict ordering:
existence → non-zero size → probe both → duration → video streams → audio
streams, and ONLY when all of that is clean does it pay for the full-decode
walk. These tests pin two things that are easy to regress:

  * every structural mismatch (duration, codec, stream count, resolution,
    audio codec/channels/sample-rate) surfaces as a distinct problem string;
  * the decode pass is short-circuited the moment a structural problem
    exists — spending minutes decoding a file already known broken is exactly
    the waste the layering exists to avoid, and conversely a clean pair must
    run the decode check exactly once.

ffprobe and the decode walk are patched (`mock.patch.object`) so no real
ffmpeg ever runs; the dst is a real 1-byte file so the existence/size gates
see a genuine path.
"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import verify  # noqa: E402
from encode_modules.verify import verify_output  # noqa: E402


def _probe(*, duration="100.0", video=None, audio=None) -> dict:
    """Build a canned ffprobe dict. Defaults model a clean 4K HEVC + stereo
    AAC output; callers override individual fields to inject a mismatch."""
    if video is None:
        video = [{"codec_type": "video", "codec_name": "hevc",
                  "width": 3840, "height": 2160}]
    if audio is None:
        audio = [{"codec_type": "audio", "codec_name": "aac",
                  "channels": 2, "sample_rate": "48000"}]
    return {"format": {"duration": duration}, "streams": video + audio}


class VerifyOutputStructuralTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.src = Path(self._td.name) / "src.mkv"
        self.dst = Path(self._td.name) / "out.mkv"
        # A real non-empty dst so the existence + size gates pass; the probe
        # is mocked so the bytes themselves never get parsed.
        self.dst.write_bytes(b"X")
        self.src.write_bytes(b"X")

    def _run(self, src_info, dst_info, decode_ret=None):
        """Patch probe_full (src then dst) and _decode_check, run verify."""
        with mock.patch.object(verify, "probe_full",
                               side_effect=[src_info, dst_info]):
            with mock.patch.object(verify, "_decode_check",
                                   return_value=decode_ret) as dc:
                problems = verify_output(self.src, self.dst)
        return problems, dc

    def test_missing_output_reported(self) -> None:
        self.dst.unlink()
        # Nothing should be probed; the existence gate fires first.
        with mock.patch.object(verify, "probe_full") as probe:
            with mock.patch.object(verify, "_decode_check") as dc:
                problems = verify_output(self.src, self.dst)
        self.assertEqual(len(problems), 1)
        self.assertIn("does not exist", problems[0])
        probe.assert_not_called()
        dc.assert_not_called()

    def test_zero_byte_output_reported(self) -> None:
        self.dst.write_bytes(b"")
        with mock.patch.object(verify, "probe_full") as probe:
            with mock.patch.object(verify, "_decode_check") as dc:
                problems = verify_output(self.src, self.dst)
        self.assertEqual(len(problems), 1)
        self.assertIn("0 bytes", problems[0])
        probe.assert_not_called()
        dc.assert_not_called()

    def test_unprobeable_output_reported(self) -> None:
        # src probes fine, dst probe returns None (corrupt/unreadable mux).
        problems, dc = self._run(_probe(), None)
        self.assertEqual(len(problems), 1)
        self.assertIn("could not probe output", problems[0])
        dc.assert_not_called()

    def test_duration_mismatch_flagged(self) -> None:
        problems, _ = self._run(_probe(duration="100.0"),
                                _probe(duration="90.0"))
        self.assertTrue(any("duration mismatch" in p for p in problems))

    def test_duration_within_tolerance_is_clean(self) -> None:
        # 100.0 vs 98.5 is within the default ±2.0s tolerance.
        problems, _ = self._run(_probe(duration="100.0"),
                                _probe(duration="98.5"))
        self.assertEqual(problems, [])

    def test_wrong_codec_flagged(self) -> None:
        dst = _probe(video=[{"codec_type": "video", "codec_name": "h264",
                             "width": 3840, "height": 2160}])
        problems, _ = self._run(_probe(), dst)
        self.assertTrue(any("video codec" in p for p in problems))

    def test_multi_video_stream_flagged(self) -> None:
        two_video = [
            {"codec_type": "video", "codec_name": "hevc",
             "width": 3840, "height": 2160},
            {"codec_type": "video", "codec_name": "hevc",
             "width": 3840, "height": 2160},
        ]
        problems, _ = self._run(_probe(), _probe(video=two_video))
        self.assertTrue(
            any("expected exactly 1 video stream" in p for p in problems))

    def test_resolution_drift_flagged(self) -> None:
        dst = _probe(video=[{"codec_type": "video", "codec_name": "hevc",
                             "width": 1920, "height": 1080}])
        problems, _ = self._run(_probe(), dst)
        self.assertTrue(any("resolution mismatch" in p for p in problems))

    def test_audio_count_mismatch_flagged(self) -> None:
        # dst has zero audio streams; src has one.
        problems, _ = self._run(_probe(), _probe(audio=[]))
        self.assertTrue(
            any("audio stream count mismatch" in p for p in problems))

    def test_audio_codec_channels_samplerate_mismatch_flagged(self) -> None:
        # One audio stream differing in all three compared fields => exactly
        # three distinct problems.
        dst_audio = [{"codec_type": "audio", "codec_name": "opus",
                      "channels": 6, "sample_rate": "44100"}]
        problems, _ = self._run(_probe(), _probe(audio=dst_audio))
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("codec mismatch" in p for p in problems))
        self.assertTrue(any("channel count mismatch" in p for p in problems))
        self.assertTrue(any("sample rate mismatch" in p for p in problems))

    def test_clean_pair_runs_decode_check_once(self) -> None:
        info = _probe()
        problems, dc = self._run(copy.deepcopy(info), copy.deepcopy(info))
        self.assertEqual(problems, [])
        dc.assert_called_once()

    def test_decode_check_skipped_when_structural_fails(self) -> None:
        # A duration mismatch is a structural problem; the decode pass must
        # NOT run — that's the "don't spend minutes on a known-broken file"
        # short-circuit.
        problems, dc = self._run(_probe(duration="100.0"),
                                 _probe(duration="80.0"))
        self.assertTrue(problems)
        dc.assert_not_called()

    def test_decode_failure_appended(self) -> None:
        info = _probe()
        problems, dc = self._run(copy.deepcopy(info), copy.deepcopy(info),
                                 decode_ret="boom")
        self.assertEqual(problems, ["decode pass failed: boom"])
        dc.assert_called_once()


if __name__ == "__main__":
    unittest.main()

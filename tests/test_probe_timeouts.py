"""Regression: probe/build subprocess calls must be bounded by a timeout.

AGENTS.md subprocess discipline: "Put a timeout on probe-style subprocess.run
calls." A corrupt/stalled source (exactly the population pre_flight/source_patcher
operate on) can wedge ffprobe/ffmpeg; with no timeout the encode hangs forever
with no recovery. These tests pin that each call (a) passes a timeout and
(b) degrades to its documented safe default when the timeout fires, rather than
letting TimeoutExpired escape.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress_modules import probe as cprobe  # noqa: E402
from encode_modules import probes, source_patcher  # noqa: E402


def _timeout(*_a, **_k):
    raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)


def _ok_run(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")


class ProbesTimeoutTest(unittest.TestCase):
    def test_probe_duration_degrades_on_timeout(self) -> None:
        with mock.patch.object(probes.subprocess, "run", side_effect=_timeout):
            self.assertEqual(probes.probe_duration(Path("x.mkv")), 0.0)

    def test_probe_duration_or_none_degrades_on_timeout(self) -> None:
        # The consolidated single subprocess site: None on timeout (probe_duration
        # is just its 0.0-on-failure adapter).
        with mock.patch.object(probes.subprocess, "run", side_effect=_timeout):
            self.assertIsNone(probes.probe_duration_or_none(Path("x.mkv")))

    def test_probe_duration_or_none_honours_timeout_kwarg(self) -> None:
        spy = mock.MagicMock(side_effect=_ok_run)
        with mock.patch.object(probes.subprocess, "run", spy):
            probes.probe_duration_or_none(Path("x.mkv"), timeout_s=10)
        self.assertEqual(spy.call_args.kwargs.get("timeout"), 10)

    def test_probe_duration_forwards_timeout_kwarg(self) -> None:
        # probe_duration must thread timeout_s through to the single site.
        spy = mock.MagicMock(side_effect=_ok_run)
        with mock.patch.object(probes.subprocess, "run", spy):
            probes.probe_duration(Path("x.mkv"), timeout_s=7)
        self.assertEqual(spy.call_args.kwargs.get("timeout"), 7)

    def test_probe_full_degrades_on_timeout(self) -> None:
        with mock.patch.object(probes.subprocess, "run", side_effect=_timeout):
            self.assertIsNone(probes.probe_full(Path("x.mkv")))

    def test_probe_fps_degrades_on_timeout(self) -> None:
        with mock.patch.object(probes.subprocess, "run", side_effect=_timeout):
            self.assertIsNone(probes.probe_fps(Path("x.mkv")))

    def test_probe_duration_passes_a_timeout(self) -> None:
        spy = mock.MagicMock(side_effect=_ok_run)
        with mock.patch.object(probes.subprocess, "run", spy):
            probes.probe_duration(Path("x.mkv"))
        self.assertIsNotNone(spy.call_args.kwargs.get("timeout"),
                             "probe_duration must request a timeout")


class CompressProbeTimeoutTest(unittest.TestCase):
    def test_run_ffprobe_exits_cleanly_on_timeout(self) -> None:
        with mock.patch.object(cprobe.shutil, "which", return_value="ffprobe"), \
             mock.patch.object(cprobe.subprocess, "run", side_effect=_timeout):
            with self.assertRaises(SystemExit):
                cprobe.run_ffprobe(Path("x.mkv"))


class SourcePatcherTimeoutTest(unittest.TestCase):
    def test_build_copy_segment_returns_false_on_timeout(self) -> None:
        with mock.patch.object(source_patcher.subprocess, "run",
                               side_effect=_timeout):
            ok = source_patcher._build_copy_segment(
                Path("src.mp4"), 0.0, 5.0, Path("out.ts"))
        self.assertFalse(ok)

    def test_build_copy_segment_passes_a_timeout(self) -> None:
        spy = mock.MagicMock(side_effect=lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""))
        with mock.patch.object(source_patcher.subprocess, "run", spy):
            source_patcher._build_copy_segment(
                Path("src.mp4"), 0.0, 5.0, Path("out.ts"))
        self.assertIsNotNone(spy.call_args.kwargs.get("timeout"),
                             "_build_copy_segment must request a timeout")


class AllSitesPassATimeoutTest(unittest.TestCase):
    """Every probe/build site must actually request a timeout. The degrade
    tests above prove the TimeoutExpired handler works, but not that a timeout
    is requested in real use — so a removed `timeout=` would slip past them.
    These spy each remaining site and assert the kwarg is present."""

    @staticmethod
    def _spy(stdout: str):
        return mock.MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""))

    def _assert_timeout(self, spy) -> None:
        self.assertIsNotNone(spy.call_args.kwargs.get("timeout"))

    def test_probe_duration_or_none(self) -> None:
        spy = self._spy('{"format":{"duration":"10.0"}}')
        with mock.patch.object(probes.subprocess, "run", spy):
            probes.probe_duration_or_none(Path("x.mkv"))
        self._assert_timeout(spy)

    def test_probe_full(self) -> None:
        spy = self._spy("{}")
        with mock.patch.object(probes.subprocess, "run", spy):
            probes.probe_full(Path("x.mkv"))
        self._assert_timeout(spy)

    def test_probe_fps(self) -> None:
        spy = self._spy("30/1")
        with mock.patch.object(probes.subprocess, "run", spy):
            probes.probe_fps(Path("x.mkv"))
        self._assert_timeout(spy)

    def test_run_ffprobe(self) -> None:
        spy = self._spy("{}")
        with mock.patch.object(cprobe.shutil, "which", return_value="ffprobe"), \
             mock.patch.object(cprobe.subprocess, "run", spy):
            cprobe.run_ffprobe(Path("x.mkv"))
        self._assert_timeout(spy)

    def test_probe_keyframes(self) -> None:
        spy = self._spy("")
        with mock.patch.object(source_patcher.subprocess, "run", spy):
            source_patcher._probe_keyframes(Path("x.mp4"), 0.0, 5.0)
        self._assert_timeout(spy)

    def test_probe_video_codec(self) -> None:
        spy = self._spy('{"streams":[{"codec_name":"h264"}]}')
        with mock.patch.object(source_patcher.subprocess, "run", spy):
            source_patcher._probe_video_codec(Path("x.mp4"))
        self._assert_timeout(spy)

    def test_build_encode_segment(self) -> None:
        spy = self._spy("")
        with mock.patch.object(source_patcher.subprocess, "run", spy):
            source_patcher._build_encode_segment(
                Path("s.mp4"), 0.0, 5.0, Path("o.ts"))
        self._assert_timeout(spy)


if __name__ == "__main__":
    unittest.main()


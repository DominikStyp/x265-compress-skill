"""Regression: the VMAF measurement must never leak its ffmpeg+libvmaf child.

`_quality_check_run` spawns a long-running `subprocess.Popen` (a full-file VMAF
pass can run for minutes). Unlike `subprocess.run`, `Popen` does NOT reap the
child if the surrounding code raises — so a decode error mid-stream, or a
Ctrl-C during the pass, would leave ffmpeg running detached. AGENTS.md: "Every
spawned ffmpeg must be terminated on abort/error so encoders are never leaked."

This pins that the child is terminated when the read loop blows up.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import quality  # noqa: E402


class _RaisingStdout:
    """Iterating the proc's stdout raises — simulates a decode error / Ctrl-C
    partway through reading ffmpeg's -progress stream."""

    def __iter__(self):
        raise RuntimeError("decode blew up mid-stream")


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = _RaisingStdout()
        self.stderr = None
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        # Report "still running" until terminated, so the teardown engages.
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False


class QualityChildTerminatedOnErrorTest(unittest.TestCase):
    def test_child_is_terminated_when_read_loop_raises(self) -> None:
        fake = _FakeProc()
        with mock.patch.object(quality, "probe_fps", return_value="30/1"), \
             mock.patch.object(quality.subprocess, "Popen", return_value=fake):
            result = quality._quality_check_run(
                Path("src.mkv"), Path("dst.mkv"), expected_dur=0.0,
            )
        # The error path still returns "no score"...
        self.assertIsNone(result)
        # ...but the ffmpeg child must have been reaped, not leaked.
        self.assertTrue(fake.terminated,
                        "ffmpeg+libvmaf child was leaked on the error path")


if __name__ == "__main__":
    unittest.main()

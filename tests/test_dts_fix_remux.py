"""attempt_dts_fix_remux: muxer-format regression + swap/rollback safety.

The headline regression: leg 2 writes the rebuilt mkv to a `*.dtsfix.tmp`
temp name. ffmpeg infers the output muxer from the file extension, and
`.tmp` is not a registered format — without an explicit `-f matroska` the
leg ALWAYS fails ("Unable to choose an output format"), which silently
killed the whole DTS auto-recovery feature (it just fell through to the
verify-failed diagnostic). Verified empirically against real ffmpeg.

Also pinned here:
  * every subprocess call (probe + both legs) carries a timeout — a wedged
    ffmpeg on an already-suspect file must not hang the pipeline forever;
  * the swap is rename-aside (NEVER unlink — pre-fix bytes are forensic)
    and rolls back if the new file can't move in, so the user is never left
    with no output at `dst`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import dts_recovery  # noqa: E402


def _fake_run_factory(calls, *, leg1_rc=0, leg2_rc=0, raise_timeout_on=None):
    """Return a subprocess.run stand-in that recognizes the probe and the
    two remux legs by their distinguishing argv, records each call, and
    creates the output file the way ffmpeg would."""

    def fake_run(cmd, **kwargs):
        cmd = list(cmd)
        calls.append({"cmd": cmd, "kwargs": kwargs})
        if raise_timeout_on is not None and _leg_of(cmd) == raise_timeout_on:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
        if "ffprobe" in cmd[0] or "ffprobe" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout=json.dumps({"streams": [{"codec_name": "hevc"}]}),
                stderr="")
        rc = leg1_rc if _leg_of(cmd) == 1 else leg2_rc
        if rc == 0:
            Path(cmd[-1]).write_bytes(b"LEG%d-OUTPUT" % _leg_of(cmd))
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    return fake_run


def _leg_of(cmd) -> int:
    """1 = mkv→mpegts pack, 2 = mpegts→mkv rebuild, 0 = probe."""
    if "mpegts" in cmd:
        return 1
    if "+genpts" in cmd:
        return 2
    return 0


class DtsFixRemuxTest(unittest.TestCase):
    def _dst(self, td: str) -> Path:
        dst = Path(td) / "out.mkv"
        dst.write_bytes(b"OLD-DST-BYTES")
        return dst

    def test_leg2_declares_matroska_muxer_explicitly(self):
        """The .dtsfix.tmp extension can't drive muxer inference — leg 2
        must pass -f matroska or it fails on every real ffmpeg."""
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(dts_recovery.subprocess, "run",
                                   _fake_run_factory(calls)):
                self.assertTrue(dts_recovery.attempt_dts_fix_remux(dst))
        leg2 = [c["cmd"] for c in calls if _leg_of(c["cmd"]) == 2]
        self.assertEqual(len(leg2), 1)
        self.assertIn("-f", leg2[0])
        self.assertIn("matroska", leg2[0])

    def test_every_subprocess_call_has_a_timeout(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(dts_recovery.subprocess, "run",
                                   _fake_run_factory(calls)):
                dts_recovery.attempt_dts_fix_remux(dst)
        self.assertGreaterEqual(len(calls), 3)  # probe + 2 legs
        for c in calls:
            self.assertIsNotNone(
                c["kwargs"].get("timeout"),
                f"missing timeout on: {c['cmd'][:4]}...")

    def test_success_swaps_files_and_keeps_old_dst_aside(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(dts_recovery.subprocess, "run",
                                   _fake_run_factory(calls)):
                self.assertTrue(dts_recovery.attempt_dts_fix_remux(dst))
            self.assertEqual(dst.read_bytes(), b"LEG2-OUTPUT")
            asides = list(Path(td).glob("*.pre-dts-fix-*"))
            self.assertEqual(len(asides), 1, "old dst must be renamed aside")
            self.assertEqual(asides[0].read_bytes(), b"OLD-DST-BYTES")
            self.assertFalse(list(Path(td).glob("*.dtsfix.ts")),
                             "ts intermediate cleaned on success")

    def test_leg1_failure_returns_false_dst_untouched(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(dts_recovery.subprocess, "run",
                                   _fake_run_factory(calls, leg1_rc=1)):
                self.assertFalse(dts_recovery.attempt_dts_fix_remux(dst))
            self.assertEqual(dst.read_bytes(), b"OLD-DST-BYTES")
            self.assertFalse(list(Path(td).glob("*.pre-dts-fix-*")))

    def test_timeout_is_caught_not_propagated(self):
        """A wedged ffmpeg trips the timeout; the recovery must degrade to
        False (fall through to the verify-failed diagnostic), not crash the
        encode with an unhandled TimeoutExpired."""
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(
                    dts_recovery.subprocess, "run",
                    _fake_run_factory(calls, raise_timeout_on=1)):
                self.assertFalse(dts_recovery.attempt_dts_fix_remux(dst))
            self.assertEqual(dst.read_bytes(), b"OLD-DST-BYTES")

    def test_move_in_failure_rolls_back_aside(self):
        """If the new file can't move onto dst, the aside rename must be
        rolled back — the user is never left with NO file at dst."""
        calls: list = []
        real_rename = Path.rename

        def flaky_rename(self, target):
            if self.name.endswith(".dtsfix.tmp"):
                raise OSError(13, "simulated lock on move-in")
            return real_rename(self, target)

        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(dts_recovery.subprocess, "run",
                                   _fake_run_factory(calls)):
                with mock.patch.object(Path, "rename", flaky_rename):
                    self.assertFalse(dts_recovery.attempt_dts_fix_remux(dst))
            self.assertTrue(dst.exists(), "rollback must restore dst")
            self.assertEqual(dst.read_bytes(), b"OLD-DST-BYTES")

    def test_unknown_video_codec_bails_before_any_remux(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            dst = self._dst(td)
            with mock.patch.object(dts_recovery, "_probe_codec",
                                   return_value="av1"):
                with mock.patch.object(dts_recovery.subprocess, "run",
                                       _fake_run_factory(calls)):
                    self.assertFalse(dts_recovery.attempt_dts_fix_remux(dst))
        self.assertEqual(calls, [], "no remux leg may run for unknown codecs")

    def test_missing_dst_returns_false_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(
                dts_recovery.attempt_dts_fix_remux(Path(td) / "absent.mkv"))


if __name__ == "__main__":
    unittest.main()

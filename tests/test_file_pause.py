"""File-based PAUSE sentinel: the no-keyboard counterpart to the Space/1-9 keys.
Creating <workdir>/PAUSE suspends every active slot; deleting it resumes them.
This is what makes pause usable in headless / over-SSH runs where the keyboard
listener is off (no TTY) — the same shape as the existing FINISH stop-file.

suspend_pid/resume_pid are monkeypatched so no real process is signalled.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import pause_control  # noqa: E402
from encode_modules.display import PAUSE_FILENAME, ParallelDisplay  # noqa: E402


class _FakeProc:
    def __init__(self, pid: int):
        self.pid = pid


class _SignalRecorder:
    """Stand-in for suspend_pid/resume_pid: records pids, always succeeds."""

    def __init__(self):
        self.suspended: list[int] = []
        self.resumed: list[int] = []


class FilePauseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self._tmp.name)
        self._orig_suspend = pause_control.suspend_pid
        self._orig_resume = pause_control.resume_pid
        self.rec = _SignalRecorder()
        pause_control.suspend_pid = lambda pid: (self.rec.suspended.append(pid)
                                                 or True)
        pause_control.resume_pid = lambda pid: (self.rec.resumed.append(pid)
                                                or True)
        self.d = ParallelDisplay(parallel=3, total=3, already_done=0,
                                workdir=self.workdir)
        # Two active slots, one idle.
        self.d.active_procs = {0: _FakeProc(101), 1: _FakeProc(102)}

    def tearDown(self) -> None:
        pause_control.suspend_pid = self._orig_suspend
        pause_control.resume_pid = self._orig_resume
        self._tmp.cleanup()

    def _pause_file(self) -> Path:
        return self.workdir / PAUSE_FILENAME

    def test_pause_all_suspends_active_unpaused_slots(self) -> None:
        msgs = self.d.pause_all()
        self.assertEqual(sorted(self.rec.suspended), [101, 102])
        self.assertEqual(self.d.paused_slots, {0, 1})
        self.assertTrue(any("PAUSED" in m for m in msgs))

    def test_pause_all_skips_already_paused(self) -> None:
        self.d.paused_slots = {0}
        self.d.pause_all()
        self.assertEqual(self.rec.suspended, [102])  # slot 0 left alone

    def test_file_appearing_suspends_all(self) -> None:
        self._pause_file().write_text("", encoding="utf-8")
        self.d.sync_file_pause()
        self.assertEqual(sorted(self.rec.suspended), [101, 102])
        self.assertTrue(self.d._file_paused)

    def test_file_removal_resumes_all(self) -> None:
        self._pause_file().write_text("", encoding="utf-8")
        self.d.sync_file_pause()                 # -> paused
        self._pause_file().unlink()
        self.d.sync_file_pause()                 # -> resumed
        self.assertEqual(sorted(self.rec.resumed), [101, 102])
        self.assertFalse(self.d._file_paused)
        self.assertEqual(self.d.paused_slots, set())

    def test_no_repeat_suspend_of_already_paused_slots(self) -> None:
        self._pause_file().write_text("", encoding="utf-8")
        self.d.sync_file_pause()
        self.d.sync_file_pause()
        self.d.sync_file_pause()
        # pause_all skips already-paused slots, so each ffmpeg is SIGSTOP'd once
        # despite three ticks with the file present.
        self.assertEqual(sorted(self.rec.suspended), [101, 102])

    def test_fresh_chunk_is_repaused_while_file_persists(self) -> None:
        # The level-trigger fix: a chunk boundary starts a NEW ffmpeg in a slot
        # (register_proc clears that slot's paused state). While PAUSE persists,
        # the next tick must suspend that fresh process too — otherwise the new
        # chunk runs at full speed despite the PAUSE file.
        self._pause_file().write_text("", encoding="utf-8")
        self.d.sync_file_pause()                      # slots 0,1 suspended
        self.assertEqual(sorted(self.rec.suspended), [101, 102])
        # Slot 0 finishes its chunk; a fresh ffmpeg (pid 201) starts.
        self.d.active_procs[0] = _FakeProc(201)
        self.d.paused_slots.discard(0)
        self.d.sync_file_pause()                      # must re-pause pid 201
        self.assertIn(201, self.rec.suspended)
        self.assertEqual(self.d.paused_slots, {0, 1})
        # No slot was SIGSTOP'd twice (slot 1 already paused, not re-signalled).
        self.assertEqual(sorted(self.rec.suspended), [101, 102, 201])

    def test_steady_state_tick_is_silent(self) -> None:
        # Load-bearing: while the file persists and everything is already
        # paused, a tick must emit NOTHING (the banner fired once on the edge).
        # Guards the "PAUSED"-substring filter against message-wording drift —
        # if pause_all's "(no active slots to pause)" leaked through, this fails.
        self._pause_file().write_text("", encoding="utf-8")
        self.d.sync_file_pause()                      # edge: banner + 2 PAUSED
        self._drain_events()
        self.d.sync_file_pause()                      # steady state: silent
        self.d.sync_file_pause()
        self.assertEqual(self._drain_events(), [])

    def _drain_events(self) -> list:
        out = []
        while not self.d.events.empty():
            out.append(self.d.events.get_nowait())
        return out

    def test_no_workdir_is_safe_noop(self) -> None:
        d = ParallelDisplay(parallel=2, total=2, already_done=0, workdir=None)
        d.active_procs = {0: _FakeProc(1)}
        d.sync_file_pause()  # must not raise
        self.assertEqual(self.rec.suspended, [])


if __name__ == "__main__":
    unittest.main()

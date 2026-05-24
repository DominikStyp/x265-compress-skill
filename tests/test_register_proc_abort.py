"""Tier 0.2 regression: the abort race in register_proc.

A worker launches its ffmpeg (chunk_worker `subprocess.Popen`) and only
then calls `display.register_proc`. If a threshold/choke abort fires in
that window, the terminate sweep in check_threshold/check_choke (which
snapshots active_procs under the lock) can run *before* this proc is
registered, leaving it to run to completion unsupervised.

register_proc must therefore refuse to adopt a proc once abort_event is
set, and terminate it instead.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.display import ParallelDisplay  # noqa: E402

# A child that would run far longer than the test if left unsupervised.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


class RegisterProcAbortTest(unittest.TestCase):
    def test_register_after_abort_terminates_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            display = ParallelDisplay(parallel=2, total=2, already_done=0,
                                      workdir=Path(td))
            display.abort_event.set()  # threshold/choke already fired
            proc = subprocess.Popen(_SLEEPER)
            try:
                display.register_proc(0, proc)
                # Not adopted into the tracked set (clean fail on the bug).
                with display.lock:
                    self.assertNotIn(0, display.active_procs)
                # And actually killed: wait returns promptly instead of
                # blocking for the full 30s sleep.
                self.assertIsNotNone(proc.wait(timeout=10))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

    def test_register_without_abort_still_tracks(self) -> None:
        """Sanity: the normal path is unchanged — the proc is adopted."""
        with tempfile.TemporaryDirectory() as td:
            display = ParallelDisplay(parallel=2, total=2, already_done=0,
                                      workdir=Path(td))
            proc = subprocess.Popen(_SLEEPER)
            try:
                display.register_proc(0, proc)
                with display.lock:
                    self.assertIs(display.active_procs.get(0), proc)
            finally:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

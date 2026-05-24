"""Tier 2.1: headless / non-tty output must not spew ANSI control codes.

When stdout isn't a terminal (piped to a log, nohup, systemd journal, CI),
the parallel renderer's cursor-up/clear-line escapes and the serial bar's
carriage returns corrupt the log. Both must fall back to plain lines. The
interactive (tty) path is unchanged — that's the default and stays exactly
as before.
"""
from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.display import ParallelDisplay  # noqa: E402

_PROGRESS_PY = str(Path(__file__).resolve().parent.parent / "progress.py")


class ParallelRenderHeadlessTest(unittest.TestCase):
    def test_non_tty_render_emits_no_ansi_but_logs_events(self) -> None:
        d = ParallelDisplay(parallel=2, total=4, already_done=0)
        d._is_tty = False
        d.events.put("  + src_0001.mkv: done in 12.3s")
        buf = io.StringIO()
        with redirect_stdout(buf):
            d.render()
        out = buf.getvalue()
        self.assertNotIn("\033[", out)          # no cursor/clear escapes
        self.assertIn("src_0001.mkv", out)       # event still logged

    def test_tty_render_still_uses_ansi(self) -> None:
        d = ParallelDisplay(parallel=2, total=4, already_done=0)
        d._is_tty = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            d.render()
        self.assertIn("\033[", buf.getvalue())   # default path unchanged


class SerialProgressHeadlessTest(unittest.TestCase):
    def test_piped_progress_has_no_carriage_returns(self) -> None:
        feed = (
            "frame=10\nout_time_us=10000000\nfps=25\nspeed=1.0x\n"
            "progress=continue\n"
            "frame=20\nout_time_us=20000000\nfps=25\nspeed=1.0x\n"
            "progress=end\n"
        )
        # NOTE: capture as bytes — text=True applies universal-newline
        # translation that would silently rewrite \r to \n and hide the bug.
        proc = subprocess.run(
            [sys.executable, _PROGRESS_PY, "--duration", "100"],
            input=feed.encode(), capture_output=True, timeout=30,
        )
        # The bug is BARE \r (in-place overwrite with no newline). Windows
        # text-mode stdout legitimately turns the trailing "\n" into "\r\n" —
        # that's a normal line ending, not the bug — so normalize CRLF first,
        # then assert no carriage returns remain.
        normalized = proc.stdout.replace(b"\r\n", b"\n")
        self.assertNotIn(b"\r", normalized)
        self.assertTrue(proc.stdout.strip(), "expected at least one progress line")


if __name__ == "__main__":
    unittest.main()

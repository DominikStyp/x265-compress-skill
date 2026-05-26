"""The shared single-line progress bar used by the post-display phases
(concat, quality). It must match the encode display's `[#####-----] NN.N%`
look, rewrite in place on a TTY, and throttle to occasional lines on a pipe so
headless logs aren't drowned. Stream + is_tty are injected so tests never touch
a real terminal.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.progress_bar import ProgressBar, read_ffmpeg_progress  # noqa: E402


class ProgressBarTtyTest(unittest.TestCase):
    def _render_one(self, **kw) -> str:
        buf = io.StringIO()
        bar = ProgressBar("Quality check", is_tty=True, stream=buf)
        bar.update(**kw)
        return buf.getvalue()

    def test_bar_fill_and_fields_at_50pct(self) -> None:
        out = self._render_one(done_s=5.0, total_s=10.0)
        self.assertIn("#######-------", out)          # 7 of 14 filled
        self.assertIn("50.0%", out)
        self.assertIn("0:00:05 / 0:00:10", out)
        self.assertIn("Quality check", out)

    def test_empty_and_full_bar(self) -> None:
        self.assertIn("--------------", self._render_one(done_s=0.0, total_s=10.0))
        self.assertIn("##############", self._render_one(done_s=10.0, total_s=10.0))

    def test_tty_rewrites_in_place(self) -> None:
        out = self._render_one(done_s=1.0, total_s=10.0)
        self.assertTrue(out.startswith("\r"))
        self.assertIn("\033[K", out)                  # clear-to-eol

    def test_optional_fields_appear(self) -> None:
        out = self._render_one(done_s=5.0, total_s=10.0,
                               fps="31", speed="0.42x", suffix="chunk 2/3")
        self.assertIn("chunk 2/3", out)
        self.assertIn("31 fps", out)
        self.assertIn("0.42x", out)

    def test_pct_capped_at_100(self) -> None:
        out = self._render_one(done_s=12.0, total_s=10.0)
        self.assertIn("100.0%", out)
        self.assertIn("##############", out)

    def test_zero_total_does_not_crash(self) -> None:
        out = self._render_one(done_s=0.0, total_s=0.0)
        self.assertIn("0.0%", out)

    def test_finish_clears_the_line(self) -> None:
        buf = io.StringIO()
        bar = ProgressBar("x", is_tty=True, stream=buf)
        bar.update(done_s=1.0, total_s=10.0)
        buf.truncate(0); buf.seek(0)
        bar.finish()
        self.assertEqual(buf.getvalue(), "\r\033[K")


class ProgressBarPipeTest(unittest.TestCase):
    def test_pipe_throttles_to_10pp_steps(self) -> None:
        buf = io.StringIO()
        bar = ProgressBar("Concat", is_tty=False, stream=buf)
        bar.update(done_s=1.0, total_s=10.0)   # 10% — first, prints
        bar.update(done_s=1.5, total_s=10.0)   # 15% — <10pp gain, skipped
        bar.update(done_s=2.0, total_s=10.0)   # 20% — prints
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        # No in-place carriage returns on a pipe.
        self.assertNotIn("\r", buf.getvalue())

    def test_pipe_finish_is_silent(self) -> None:
        buf = io.StringIO()
        bar = ProgressBar("Concat", is_tty=False, stream=buf)
        bar.update(done_s=1.0, total_s=10.0)
        before = buf.getvalue()
        bar.finish()
        self.assertEqual(buf.getvalue(), before)   # nothing extra on a pipe


class ReadFfmpegProgressTest(unittest.TestCase):
    def test_ticks_once_per_block_with_accumulated_state(self) -> None:
        lines = [
            "frame=1\n", "out_time_us=5000000\n", "speed=2.0x\n",
            "progress=continue\n",
            "frame=2\n", "out_time_us=9000000\n", "speed=2.0x\n",
            "progress=end\n",
        ]
        seen: list[tuple[str, str]] = []
        read_ffmpeg_progress(
            iter(lines),
            lambda st: seen.append((st.get("out_time_us"), st.get("speed"))))
        self.assertEqual(seen, [("5000000", "2.0x"), ("9000000", "2.0x")])

    def test_ignores_non_keyvalue_lines(self) -> None:
        ticks = []
        read_ffmpeg_progress(iter(["garbage\n", "progress=end\n"]),
                             lambda st: ticks.append(st))
        self.assertEqual(len(ticks), 1)


if __name__ == "__main__":
    unittest.main()

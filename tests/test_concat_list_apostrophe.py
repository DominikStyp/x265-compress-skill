"""Concat-list writers must escape apostrophes in path components.

ffmpeg's concat demuxer parses each line as `file '<path>'`. Inside a
single-quoted path an apostrophe must be written as `'\\''` — close the quote,
emit an escaped apostrophe, reopen. Forgetting that closes the quote at the
first `'` in the path, so a path like `/movies/.../O'Reilly DP'd .../enc_0.mkv`
is read as `/movies/.../O` plus garbage, ffmpeg reports "No such file or
directory", and the concat phase fails with exit 254.

This bit a real job in v1.8.0 / v1.8.1 (`DP'd in Gym`). The fix lives in a
single shared helper — `concat_list_lines` — so the two list writers
(`chunking.concat_chunks` and `source_patcher`) can't drift.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.concat_list import (  # noqa: E402
    concat_list_lines,
    escape_concat_path,
)


class EscapeConcatPathTest(unittest.TestCase):
    """The atomic escape rule — used wherever a path is interpolated into a
    `file '…'` line."""

    def test_plain_path_is_unchanged(self) -> None:
        self.assertEqual(escape_concat_path("/tmp/a.mkv"), "/tmp/a.mkv")

    def test_single_apostrophe_becomes_quote_dance(self) -> None:
        # 'DP\'d' must become 'DP'\''d' — close quote, escaped apostrophe,
        # reopen quote. That's how the concat demuxer documents it.
        self.assertEqual(escape_concat_path("/tmp/DP'd.mkv"),
                         "/tmp/DP'\\''d.mkv")

    def test_multiple_apostrophes_all_escaped(self) -> None:
        # Real workdirs have plural possessives ("O'Reilly's O'Brien").
        self.assertEqual(escape_concat_path("/a'/b'/c.mkv"),
                         "/a'\\''/b'\\''/c.mkv")

    def test_other_shell_metas_are_left_alone(self) -> None:
        # The concat demuxer's quoting only cares about `'`. Spaces, `&`,
        # `;`, `$`, `!` and so on are literal inside single quotes, so any
        # additional escaping would corrupt the path.
        s = "/movies/A & B ; C $D !.mkv"
        self.assertEqual(escape_concat_path(s), s)


class ConcatListLinesTest(unittest.TestCase):
    """The full line builder. Output is what gets written to concat.txt."""

    def test_lines_wrap_each_path_in_single_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "enc_0.mkv"
            p.write_bytes(b"x")
            lines = concat_list_lines([p])
            # One line, terminated with \n.
            self.assertEqual(lines, f"file '{p.resolve().as_posix()}'\n")

    def test_apostrophe_in_workdir_does_not_break_quoting(self) -> None:
        # The real bug: workdir name contains `'` so the chunk path inherits
        # it. Before the fix this wrote `file '…DP'd….mkv'` — broken. After
        # the fix it must write `file '…DP'\''d….mkv'` — parseable.
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "DP'd workdir"
            wd.mkdir()
            p = wd / "enc_0.mkv"
            p.write_bytes(b"x")
            line = concat_list_lines([p])

            # Single trailing newline; exactly one balanced `file '…'` line.
            self.assertTrue(line.endswith("\n"))
            self.assertTrue(line.startswith("file '"))
            self.assertTrue(line.rstrip("\n").endswith("'"))

            # The apostrophe in the workdir must appear as `'\''` inside the
            # quoted segment — the demuxer's documented escape.
            self.assertIn("DP'\\''d workdir", line)
            # And the raw broken form must NOT appear.
            self.assertNotIn("DP'd workdir", line)

    def test_multiple_chunks_one_line_each_with_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "O'Brien"
            wd.mkdir()
            chunks = []
            for i in range(3):
                c = wd / f"enc_{i}.mkv"
                c.write_bytes(b"x")
                chunks.append(c)
            text = concat_list_lines(chunks)
            self.assertEqual(text.count("\n"), 3)
            for c in chunks:
                self.assertIn(c.resolve().as_posix().replace("'", "'\\''"),
                              text)


class IntegrationWithRealWritersTest(unittest.TestCase):
    """Both list writers (chunking.concat_chunks and source_patcher's
    segment-list builder) must route through the shared helper, so neither
    can regress in isolation. Tests assert the modules import the helper —
    if anyone re-introduces a hand-rolled `file '{path}'` interpolation,
    these go red and the apostrophe bug is back."""

    def test_chunking_imports_the_shared_helper(self) -> None:
        import encode_modules.chunking as chunking
        # The function name must be reachable from chunking — proves the
        # writer routes through it rather than rolling its own f-string.
        self.assertIs(chunking.concat_list_lines, concat_list_lines)

    def test_source_patcher_imports_the_shared_helper(self) -> None:
        import encode_modules.source_patcher as source_patcher
        self.assertIs(source_patcher.concat_list_lines, concat_list_lines)

    def test_chunking_no_handrolled_file_line_format(self) -> None:
        # Belt-and-suspenders: scan the source for the broken interpolation
        # pattern. Any future code that writes `file '{...}'` directly into
        # a string template trips this; the only correct call site is
        # `concat_list_lines(...)`.
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parent.parent
               / "encode_modules" / "chunking.py").read_text(encoding="utf-8")
        # Allow the comment that references the broken form, but not real code.
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        for ln in code_lines:
            self.assertNotIn("f\"file '", ln,
                             "chunking.py must not hand-roll concat lines")

    def test_source_patcher_no_handrolled_file_line_format(self) -> None:
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parent.parent
               / "encode_modules" / "source_patcher.py").read_text(
                   encoding="utf-8")
        code_lines = [ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#")]
        for ln in code_lines:
            self.assertNotIn("f\"file '", ln,
                             "source_patcher.py must not hand-roll concat lines")


if __name__ == "__main__":
    unittest.main()

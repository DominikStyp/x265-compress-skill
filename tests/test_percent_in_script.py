"""Regression: a `%` in the source/output filename must survive into the
generated encoder script intact, on every `%`-sensitive sink.

`%` is special in three independent places the generator feeds:
  - bash `printf` FORMAT strings (the terminal-title line) — `%` starts a
    conversion spec regardless of shell quoting;
  - Windows `.bat` text — cmd expands `%VAR%` and STRIPS a lone `%`, so
    `set "_SKILL_IN=C:\50%PATH%.mkv"` would expand PATH (corrupting the actual
    path the encoder runs on), and `70% Hell` would lose its `%`;
  - (already covered elsewhere) the ffmpeg segment muxer output template —
    see tests/test_split_percent_escape.py.

This pins the script-generation sinks: the POSIX title is passed as a printf
DATA argument (so no filename char is ever parsed as format), and every Windows
value embedded in `set "..."` / `title` doubles its `%`.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress_modules import script_writer  # noqa: E402
from compress_modules.plan import EncodePlan  # noqa: E402
from compress_modules.probe import SourceInfo  # noqa: E402

# Distinct `%`-bearing markers for source vs output so each sink is pinned.
SRC_STEM = "70% Hell"
OUT_STEM = "90% Done"


def _info() -> SourceInfo:
    return SourceInfo(
        codec="h264", width=1920, height=1080, fps=23.976, pix_fmt="yuv420p",
        bit_depth=8, color_primaries=None, color_transfer=None,
        video_bitrate_kbps=8000, duration_sec=120.0,
        file_size_bytes=100_000_000, bits_per_pixel=0.1, is_hdr=False,
        audio_codecs=["aac"],
    )


def _plan(out_dir: str) -> EncodePlan:
    return EncodePlan(
        crf=22, preset="slow", pix_fmt_out="yuv420p10le",
        x265_params=["psy-rd=2.0", "me=star"],
        output_path=f"{out_dir}/{OUT_STEM}.mkv",
        script_path=f"{out_dir}/.tmp/compress_{OUT_STEM}.sh",
        warnings=[], estimated_reduction="30-45%", notes=[],
    )


def _render(fn, td: str, *, resumable: bool) -> str:
    return fn(
        _info(), _plan(td), Path(f"/src/{SRC_STEM}.mp4"), Path("/skill"),
        Path(td), Path(td) / f"{OUT_STEM}.report.md",
        resumable=resumable, segment_seconds=60, parallel=1,
        max_output_bytes=None, max_size_percent=None, auto_fix_choke=False,
        no_pre_flight_scan=False, auto_patch_source=False,
        max_patch_seconds=10.0, no_report=False, no_pause=True,
    )


class PosixPrintfTitleTest(unittest.TestCase):
    def test_title_is_a_printf_data_arg_not_format(self) -> None:
        for resumable in (True, False):
            with tempfile.TemporaryDirectory() as td:
                s = _render(script_writer._render_posix_script, td,
                            resumable=resumable)
            # The format string carries a literal %s placeholder, terminated by
            # the BEL (\007) — the filename is NOT spliced into the format.
            self.assertIn(": %s\\007'", s)
            # The title rides as a single-quoted DATA argument after the format.
            self.assertIn(f"'{SRC_STEM}'", s)
            # The filename must NOT appear jammed inside the format string
            # (i.e. immediately before the BEL) — that's the old broken shape.
            self.assertNotIn(f"{SRC_STEM}\\007'", s)


class WindowsPercentEscapeTest(unittest.TestCase):
    def test_no_unescaped_percent_in_set_or_title(self) -> None:
        for resumable in (True, False):
            with tempfile.TemporaryDirectory() as td:
                s = _render(script_writer._render_windows_script, td,
                            resumable=resumable)
            # Every embedded `%` is doubled so cmd treats it as literal...
            self.assertIn(f"70%% Hell", s)
            self.assertIn(f"90%% Done", s)
            # ...and no single-`%` (cmd would strip it or expand %VAR%) survives
            # for either the source or output marker, in any sink.
            self.assertNotIn(f"{SRC_STEM}", s)   # "70% Hell" (single %)
            self.assertNotIn(f"{OUT_STEM}", s)   # "90% Done" (single %)

    def test_input_and_output_paths_are_escaped_in_set_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = _render(script_writer._render_windows_script, td,
                        resumable=True)
        # Each stash line carries the escaped marker, not the raw single-%.
        for prefix, marker in (('set "_SKILL_IN=', "70%% Hell"),
                               ('set "_SKILL_OUT=', "90%% Done"),
                               ('set "_SKILL_WORKDIR=', "70%% Hell")):
            line = next(ln for ln in s.splitlines() if ln.startswith(prefix))
            self.assertIn(marker, line)
            self.assertNotIn(SRC_STEM, line)
            self.assertNotIn(OUT_STEM, line)

    def test_hook_sidecar_path_is_escaped(self) -> None:
        # The on_chunk_done sidecar path embeds the source stem
        # (".compress_70% Hell.hooks.json") into a `set "..."`, so its `%` must
        # be doubled too — a sink the other tests (on_chunk_done=None) don't hit.
        with tempfile.TemporaryDirectory() as td:
            s = script_writer._render_windows_script(
                _info(), _plan(td), Path(f"/src/{SRC_STEM}.mp4"), Path("/skill"),
                Path(td), Path(td) / f"{OUT_STEM}.report.md",
                resumable=True, segment_seconds=60, parallel=1,
                max_output_bytes=None, max_size_percent=None,
                auto_fix_choke=False, no_pre_flight_scan=False,
                auto_patch_source=False, max_patch_seconds=10.0,
                no_report=False, no_pause=True, on_chunk_done=["notify"],
            )
        hooks_line = next(ln for ln in s.splitlines()
                          if ln.startswith('set "_SKILL_HOOKS='))
        self.assertIn("70%% Hell", hooks_line)
        self.assertNotIn(SRC_STEM, hooks_line)


if __name__ == "__main__":
    unittest.main()

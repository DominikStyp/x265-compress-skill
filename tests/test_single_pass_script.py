"""Characterization pins for the LEGACY single-pass (resumable=False) script.

The single-pass path (`BAT_TEMPLATE` / `SH_TEMPLATE` + `_legacy_report_call` +
report.py's `single` subcommand) is reachable only via bare
`compress.py <file>` WITHOUT `--resumable`. The queue never picks it, and no
prior test asserted its generated script is sane — so it could silently rot
(a template typo, a dropped flag, a broken exit-code capture) without any test
turning red.

These are characterization pins, not a spec rewrite: they nail the shape the
path produces TODAY so any future change to it is a deliberate, visible diff.

Both .bat and .sh are rendered via the per-OS render functions directly (the
same seam the other script tests use), so this file pins both shells
regardless of the host OS the suite runs on.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress_modules import script_writer  # noqa: E402
from compress_modules.plan import EncodePlan  # noqa: E402
from compress_modules.probe import SourceInfo  # noqa: E402
from compress_modules.script_options import ScriptOptions  # noqa: E402

# x265 params chosen to be recognisable in the rendered `-x265-params "..."`.
X265_PARAMS = ["psy-rd=2.0", "me=star", "merange=57"]


def _info() -> SourceInfo:
    return SourceInfo(
        codec="h264", width=1920, height=1080, fps=23.976, pix_fmt="yuv420p",
        bit_depth=8, color_primaries=None, color_transfer=None,
        video_bitrate_kbps=8000, duration_sec=120.0,
        file_size_bytes=100_000_000, bits_per_pixel=0.1, is_hdr=False,
        audio_codecs=["aac"],
    )


def _plan() -> EncodePlan:
    return EncodePlan(
        crf=22, preset="slow", pix_fmt_out="yuv420p10le",
        x265_params=list(X265_PARAMS),
        output_path="/out/clip.mkv", script_path="/out/.tmp/compress_clip.sh",
        warnings=[], estimated_reduction="30-45%", notes=[],
    )


def _render(fn, td: str, *, info=None, plan=None, source=None) -> str:
    """Render a NON-resumable script through the per-OS render seam."""
    return fn(
        info or _info(), plan or _plan(), source or Path("/src/clip.mp4"),
        Path("/skill"), Path(td), Path(td), Path(td) / "clip.report.md",
        ScriptOptions(resumable=False, no_pause=True),
    )


class WindowsSinglePassTest(unittest.TestCase):
    """Pins for the .bat single-pass body."""

    def setUp(self) -> None:
        self.s = _render(script_writer._render_windows_script, "/tmp")

    def test_ffmpeg_libx265_invocation_present(self) -> None:
        # The encode is a single direct ffmpeg call (no resumable worker).
        self.assertIn("ffmpeg -hide_banner", self.s)
        self.assertIn("-c:v libx265", self.s)
        self.assertNotIn("encode_resumable.py", self.s)

    def test_x265_params_and_preset_crf_pixfmt_threaded(self) -> None:
        self.assertIn(f'-x265-params "{":".join(X265_PARAMS)}"', self.s)
        self.assertIn("-preset slow", self.s)
        self.assertIn("-crf 22", self.s)
        self.assertIn("-pix_fmt yuv420p10le", self.s)

    def test_audio_and_subs_copied_not_reencoded(self) -> None:
        self.assertIn("-c:a copy", self.s)
        self.assertIn("-c:s copy", self.s)

    def test_report_single_call_plumbing_present(self) -> None:
        # The single-pass path has no Python orchestrator inside it, so it
        # injects a report.py `single` CLI call at its success branch.
        self.assertIn('"%_SKILL_REPORT%" single "%_SKILL_REPORT_MD%"', self.s)
        self.assertIn("--crf 22", self.s)
        self.assertIn("--preset slow", self.s)
        self.assertIn("--status ok", self.s)

    def test_exit_code_captured_before_trailing_statement(self) -> None:
        # The repo's known .bat `pause` gotcha: errorlevel MUST be captured
        # into a var BEFORE any later statement (echo / pause) can clobber it,
        # and the script must `exit /b` with that captured code.
        self.assertIn("set ENCODE_RC=%errorlevel%", self.s)
        self.assertIn("exit /b %ENCODE_RC%", self.s)
        ec = self.s.index("set ENCODE_RC=%errorlevel%")
        # The capture comes immediately after the ffmpeg pipe, before the
        # success `echo === Done`, so a non-zero exit is preserved.
        self.assertLess(ec, self.s.index("=== Done."))

    def test_no_pause_suppressed(self) -> None:
        # no_pause=True in the fixture — the trailing `pause` must be gone.
        self.assertNotIn("\npause", self.s)


class PosixSinglePassTest(unittest.TestCase):
    """Pins for the .sh single-pass body."""

    def setUp(self) -> None:
        self.s = _render(script_writer._render_posix_script, "/tmp")

    def test_ffmpeg_libx265_invocation_present(self) -> None:
        self.assertIn("ffmpeg -hide_banner", self.s)
        self.assertIn("-c:v libx265", self.s)
        self.assertNotIn("encode_resumable.py", self.s)

    def test_x265_params_and_preset_crf_pixfmt_threaded(self) -> None:
        self.assertIn(f'-x265-params "{":".join(X265_PARAMS)}"', self.s)
        self.assertIn("-preset slow", self.s)
        self.assertIn("-crf 22", self.s)
        self.assertIn("-pix_fmt yuv420p10le", self.s)

    def test_audio_and_subs_copied_not_reencoded(self) -> None:
        self.assertIn("-c:a copy", self.s)
        self.assertIn("-c:s copy", self.s)

    def test_report_single_call_plumbing_present(self) -> None:
        self.assertIn('"${_SKILL_REPORT}" single "${_SKILL_REPORT_MD}"', self.s)
        self.assertIn("--crf 22", self.s)
        self.assertIn("--preset slow", self.s)
        self.assertIn("--status ok", self.s)

    def test_exit_code_captured_from_pipestatus(self) -> None:
        # ffmpeg is piped into the progress reader, so `$?` would be the
        # READER's exit, not ffmpeg's — the script must read PIPESTATUS[0].
        self.assertIn("ENCODE_RC=${PIPESTATUS[0]}", self.s)
        self.assertIn("exit ${ENCODE_RC}", self.s)
        ec = self.s.index("ENCODE_RC=${PIPESTATUS[0]}")
        self.assertLess(ec, self.s.index("=== Done."))

    def test_no_pause_suppressed(self) -> None:
        self.assertNotIn("read -n1", self.s)


class SinglePassPercentEscapeTest(unittest.TestCase):
    """A `%`-bearing stem must still be escaped in the single-pass body
    (the same sink resumable already covers, but pinned for this path too)."""

    def _percent_plan(self) -> EncodePlan:
        return EncodePlan(
            crf=22, preset="slow", pix_fmt_out="yuv420p10le",
            x265_params=list(X265_PARAMS),
            output_path="/out/90% Done.mkv",
            script_path="/out/.tmp/compress_90_pct_ Done.sh",
            warnings=[], estimated_reduction="30-45%", notes=[],
        )

    def test_win_percent_doubled_in_set_lines(self) -> None:
        s = _render(script_writer._render_windows_script, "/tmp",
                    plan=self._percent_plan(),
                    source=Path("/src/70% Hell.mp4"))
        # cmd would expand %VAR% / strip a lone %, so every embedded % is doubled.
        self.assertIn("70%% Hell", s)
        self.assertIn("90%% Done", s)
        self.assertNotIn("70% Hell", s)
        self.assertNotIn("90% Done", s)

    def test_posix_title_is_printf_data_arg(self) -> None:
        s = _render(script_writer._render_posix_script, "/tmp",
                    plan=self._percent_plan(),
                    source=Path("/src/70% Hell.mp4"))
        # Title rides as a single-quoted DATA arg, never spliced into the
        # printf format — so its % can't be read as a conversion spec.
        self.assertIn(": %s\\007'", s)
        self.assertIn("'70% Hell'", s)


class SinglePassWritePathTest(unittest.TestCase):
    """End-to-end through the public write_script: a non-resumable script is
    written to disk for THIS host's shell, and its core invocation survives the
    per-OS line-ending normalization."""

    def test_write_script_emits_single_pass_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "clip.mkv"
            tmp = Path(td) / ".tmp"
            tmp.mkdir()
            script_path = tmp / "compress_clip.sh"  # ext is cosmetic for read
            plan = EncodePlan(
                crf=22, preset="slow", pix_fmt_out="yuv420p10le",
                x265_params=list(X265_PARAMS), output_path=str(out),
                script_path=str(script_path), warnings=[],
                estimated_reduction="30-45%", notes=[],
            )
            script_writer.write_script(
                _info(), plan, Path(td) / "clip.mp4",
                resumable=False, no_pause=True,
            )
            written = script_path.read_bytes().decode("utf-8")
        self.assertIn("-c:v libx265", written)
        self.assertIn("-c:a copy", written)
        self.assertNotIn("encode_resumable.py", written)


if __name__ == "__main__":
    unittest.main()

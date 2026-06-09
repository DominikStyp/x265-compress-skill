"""Tier 1-B: generated encoder scripts must (a) preflight-check that ffmpeg
and Python are on PATH and fail with a readable message, and (b) on POSIX
resolve python3-or-python rather than hard-coding python3.

Renders both templates through the real script_writer render functions, so a
str.format brace mistake surfaces here too (a KeyError would fail the render).
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
from encode_modules.hook_config import load_hook_sidecar  # noqa: E402


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
        x265_params=["psy-rd=2.0", "me=star"],
        output_path="/tmp/out.mkv", script_path="/tmp/.tmp/compress_out.sh",
        warnings=[], estimated_reduction="30-45%", notes=[],
    )


def _render(fn, *, resumable: bool, no_pause: bool) -> str:
    # v1.19.0 signature: tmp_dir (chunked-workdir parent) + sidecar_dir
    # (logs/) + report_md_path are now passed independently.
    return fn(
        _info(), _plan(), Path("/tmp/in.mp4"), Path("/skill"),
        Path("/tmp/.tmp"), Path("/tmp/logs"), Path("/tmp/logs/out.report.md"),
        resumable=resumable, segment_seconds=60, parallel=1,
        max_output_bytes=None, max_size_percent=None, auto_fix_choke=False,
        no_pre_flight_scan=False, auto_patch_source=False,
        max_patch_seconds=10.0, no_report=False, no_pause=no_pause,
    )


class BatDepGuardTest(unittest.TestCase):
    def test_both_bat_variants_guard_ffmpeg_and_python(self) -> None:
        for resumable in (True, False):
            s = _render(script_writer._render_windows_script,
                        resumable=resumable, no_pause=False)
            self.assertIn("where ffmpeg >nul", s)
            self.assertIn("where python >nul", s)
            self.assertIn(":_NODEP_FFMPEG", s)
            self.assertIn(":_NODEP_PYTHON", s)

    def test_no_pause_suppresses_pause(self) -> None:
        with_pause = _render(script_writer._render_windows_script,
                             resumable=True, no_pause=False)
        without = _render(script_writer._render_windows_script,
                          resumable=True, no_pause=True)
        self.assertIn("pause", with_pause)
        self.assertNotIn("pause", without)


class ShDepGuardTest(unittest.TestCase):
    def test_both_sh_variants_resolve_python_and_guard(self) -> None:
        for resumable in (True, False):
            s = _render(script_writer._render_posix_script,
                        resumable=resumable, no_pause=False)
            self.assertIn("command -v ffmpeg", s)
            self.assertIn('PY="$(command -v python3 || command -v python)"', s)
            self.assertIn('"${PY}" -u', s)
            # The hard-coded interpreter must be gone (command -v python3 in the
            # resolver is fine; the *invocation* python3 -u is what we replaced).
            self.assertNotIn("python3 -u", s)

    def test_no_pause_suppresses_read_prompt(self) -> None:
        with_pause = _render(script_writer._render_posix_script,
                             resumable=True, no_pause=False)
        without = _render(script_writer._render_posix_script,
                          resumable=True, no_pause=True)
        self.assertIn("read -n1", with_pause)
        self.assertNotIn("read -n1", without)


def _render_hooked(fn, tmp_dir, on_chunk_done):
    """Render the resumable script with a real tmp_dir so the hook sidecar can
    actually be written, and an explicit on_chunk_done.

    v1.19.0: sidecar_dir = tmp_dir for tests — keeps the existing fixture's
    sidecar-lookup paths (``tmp_dir / "in.hooks.json"``) working without
    spinning up a separate logs/ dir per test."""
    return fn(
        _info(), _plan(), Path("/tmp/in.mp4"), Path("/skill"),
        Path(tmp_dir), Path(tmp_dir), Path(tmp_dir) / "out.report.md",
        resumable=True, segment_seconds=60, parallel=2,
        max_output_bytes=None, max_size_percent=None, auto_fix_choke=False,
        no_pre_flight_scan=False, auto_patch_source=False,
        max_patch_seconds=10.0, no_report=False, no_pause=False,
        on_chunk_done=on_chunk_done,
    )


class HookFragmentTest(unittest.TestCase):
    """on_chunk_done: the generated script must carry only the sidecar PATH
    (via the proven stash var), never the quote-bearing argv inline — and the
    sidecar file must hold the real command."""

    def test_win_resumable_stashes_sidecar_path_and_passes_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = _render_hooked(script_writer._render_windows_script, td,
                               ["bash", "/x/n.sh"])
            self.assertIn('set "_SKILL_HOOKS=', s)
            self.assertIn('--hooks-config "%_SKILL_HOOKS%"', s)
            # The argv (with its quotes) never appears inline in the script.
            self.assertNotIn('["bash"', s)
            self.assertEqual(load_hook_sidecar(Path(td) / "in.hooks.json"),
                             ["bash", "/x/n.sh"])

    def test_posix_resumable_stashes_sidecar_path_and_passes_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = _render_hooked(script_writer._render_posix_script, td,
                               ["bash", "/x/n.sh"])
            self.assertIn("_SKILL_HOOKS=", s)
            self.assertIn('--hooks-config "${_SKILL_HOOKS}"', s)
            self.assertNotIn('["bash"', s)  # argv never inline (sidecar only)
            self.assertEqual(load_hook_sidecar(Path(td) / "in.hooks.json"),
                             ["bash", "/x/n.sh"])

    def test_brace_in_path_does_not_break_format(self) -> None:
        # The sidecar path is inserted as a .format() VALUE, never re-formatted,
        # so a literal { / } in the path must NOT raise KeyError at render time.
        with tempfile.TemporaryDirectory() as td:
            braced = Path(td) / "te{st}dir"
            braced.mkdir()
            for fn in (script_writer._render_windows_script,
                       script_writer._render_posix_script):
                s = _render_hooked(fn, braced, ["bash", "/x/n.sh"])
                self.assertIn("--hooks-config", s)
            self.assertEqual(load_hook_sidecar(braced / "in.hooks.json"),
                             ["bash", "/x/n.sh"])

    def test_no_hook_means_no_fragment_and_no_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for fn in (script_writer._render_windows_script,
                       script_writer._render_posix_script):
                s = _render_hooked(fn, td, None)
                self.assertNotIn("_SKILL_HOOKS", s)
                self.assertNotIn("--hooks-config", s)
            self.assertFalse((Path(td) / "in.hooks.json").exists())


if __name__ == "__main__":
    unittest.main()

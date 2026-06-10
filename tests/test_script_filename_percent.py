"""A `%` in the source stem must NOT survive into the generated script's
FILENAME (as opposed to its contents, which are already escaped — see
tests/test_percent_in_script.py).

Why this is a real bug, not cosmetic: the queue runner launches the script via
``subprocess.call(["cmd.exe", "/c", "call", script_path], ...)``. cmd.exe
re-parses that command line and EXPANDS any ``%VAR%`` in ``script_path``. A
source named ``50%PATH%off.mkv`` would yield a script named
``compress_50%PATH%off.bat``; cmd would expand ``%PATH%`` and look for a
DIFFERENT, non-existent file → the encode never runs (or worse, resume/skip
logic keys off a path that doesn't match what was written).

The fix sanitizes ``%`` out of the stem (``%`` -> ``_pct_``) at the single
derive site, behind a shared helper so the write side (plan.py) and any
queue-side derivation can never disagree. Sources WITHOUT ``%`` must keep
byte-identical names (zero churn for the normal case).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress_modules.plan import (  # noqa: E402
    SCRIPT_EXTENSION,
    plan_encode,
    script_filename_for,
)
from compress_modules.probe import SourceInfo  # noqa: E402


def _info() -> SourceInfo:
    return SourceInfo(
        codec="h264", width=1920, height=1080, fps=24.0, pix_fmt="yuv420p",
        bit_depth=8, color_primaries=None, color_transfer=None,
        video_bitrate_kbps=8000, duration_sec=120.0,
        file_size_bytes=10_000_000, bits_per_pixel=0.1, is_hdr=False,
        audio_codecs=["aac"],
    )


class ScriptFilenameHelperTest(unittest.TestCase):
    def test_percent_stem_is_sanitized(self) -> None:
        # The dangerous case: a lone `%` (and a `%VAR%` pair) in the stem.
        name = script_filename_for(Path("/src/50%PATH%off.mkv"), ".bat")
        self.assertNotIn("%", name)
        self.assertEqual(name, "compress_50_pct_PATH_pct_off.bat")

    def test_normal_stem_is_byte_identical_to_legacy(self) -> None:
        # Zero churn for the overwhelmingly common no-`%` case — the name must
        # match the old `f"compress_{stem}{ext}"` formula exactly.
        for stem, ext in (("My Movie", ".bat"),
                          ("clip", ".sh"),
                          ("a & b (1)", ".bat")):
            self.assertEqual(
                script_filename_for(Path(f"/src/{stem}.mp4"), ext),
                f"compress_{stem}{ext}",
            )

    def test_sanitization_is_cross_os_identical(self) -> None:
        # Same stem must sanitize identically regardless of extension, so a
        # workdir resumed on a different OS still finds the same script name.
        bat = script_filename_for(Path("/src/70% Hell.mp4"), ".bat")
        sh = script_filename_for(Path("/src/70% Hell.mp4"), ".sh")
        self.assertEqual(bat[:-4], sh[:-3])  # strip ".bat" / ".sh"
        self.assertNotIn("%", bat)
        self.assertNotIn("%", sh)


class WorkdirNotReparsedTest(unittest.TestCase):
    """The `.compress_<stem>` WORKDIR keeps its `%` (no sanitization) and is
    correct to do so: unlike the script filename, the workdir is never put on
    a command line cmd.exe re-parses — it goes into the script CONTENT via
    `set "_SKILL_WORKDIR=..."`, where `_cmd_set_escape` already doubles `%`.
    This pins that intentional asymmetry so a future refactor doesn't "helpfully"
    sanitize the workdir too (which would break resume: the encoder's workdir
    on disk still has the literal `%`)."""

    def test_workdir_keeps_literal_percent(self) -> None:
        from compress_modules.plan import compress_workdir

        wd = compress_workdir(Path("/out/.tmp"), Path("/src/70% Hell.mp4"))
        self.assertEqual(wd.name, ".compress_70% Hell")
        self.assertIn("%", wd.name)


class PlanUsesHelperTest(unittest.TestCase):
    """The write side (plan_encode) must route through the helper so the
    script_path it bakes carries no `%`."""

    def test_plan_encode_script_path_has_no_percent(self) -> None:
        src = Path("/src/50%PATH%off.mp4").resolve()
        plan = plan_encode(
            _info(), src,
            override_crf=None, override_preset=None,
            anime=False, grain=False, eight_bit=False,
        )
        script_name = Path(plan.script_path).name
        self.assertNotIn("%", script_name)
        # And it agrees with the shared helper exactly.
        self.assertEqual(script_name, script_filename_for(src, SCRIPT_EXTENSION))

    def test_normal_plan_script_path_unchanged(self) -> None:
        src = Path("/src/clip.mp4").resolve()
        plan = plan_encode(
            _info(), src,
            override_crf=None, override_preset=None,
            anime=False, grain=False, eight_bit=False,
        )
        self.assertEqual(Path(plan.script_path).name,
                         f"compress_clip{SCRIPT_EXTENSION}")


if __name__ == "__main__":
    unittest.main()

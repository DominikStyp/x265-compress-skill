"""The `.compress_<stem>` workdir name is a convention SHARED by two packages:
the script generator (compress_modules.script_writer) creates the directory, and
the queue's CRF-retry logic (queue_modules.job_schema.derive_workdir) locates
already-encoded chunks inside it. If the two formulas drift, CRF-retry silently
looks in the wrong place and re-encodes everything. This pins them to one helper.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress_modules.plan import compress_workdir  # noqa: E402
from queue_modules.job_schema import derive_output_path, derive_workdir  # noqa: E402


class CompressWorkdirTest(unittest.TestCase):
    def test_formula(self) -> None:
        wd = compress_workdir(Path("/out/.tmp"), Path("/src/My Movie.mp4"))
        self.assertEqual(wd, Path("/out/.tmp/.compress_My Movie"))

    def test_derive_workdir_uses_the_shared_helper(self) -> None:
        # The queue locator must produce exactly what the shared helper does for
        # the tmp_dir the generated script uses (output's parent / .tmp).
        for inp in (Path("/v/clip.mp4"), Path("/v/already.mkv")):
            tmp_dir = derive_output_path(inp).parent / ".tmp"
            self.assertEqual(derive_workdir(inp),
                             compress_workdir(tmp_dir, inp))

    def test_script_writer_emits_the_shared_workdir(self) -> None:
        # Render a resumable script and confirm the workdir baked into it is the
        # one compress_workdir produces — i.e. the generator uses the helper too.
        from compress_modules import script_writer
        from compress_modules.plan import EncodePlan
        from compress_modules.probe import SourceInfo

        info = SourceInfo(
            codec="h264", width=1920, height=1080, fps=24.0, pix_fmt="yuv420p",
            bit_depth=8, color_primaries=None, color_transfer=None,
            video_bitrate_kbps=8000, duration_sec=120.0,
            file_size_bytes=10_000_000, bits_per_pixel=0.1, is_hdr=False,
            audio_codecs=["aac"],
        )
        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            source = Path("/src/clip.mp4")
            plan = EncodePlan(
                crf=22, preset="slow", pix_fmt_out="yuv420p10le",
                x265_params=["psy-rd=2.0"], output_path=f"{td}/clip.mkv",
                script_path=f"{td}/.tmp/compress_clip.sh", warnings=[],
                estimated_reduction="30%", notes=[],
            )
            expected = str(compress_workdir(tmp_dir, source))
            for render in (script_writer._render_windows_script,
                           script_writer._render_posix_script):
                s = render(
                    info, plan, source, Path("/skill"), tmp_dir,
                    tmp_dir, Path(td) / "clip.report.md",
                    resumable=True, segment_seconds=60, parallel=1,
                    max_output_bytes=None, max_size_percent=None,
                    auto_fix_choke=False, no_pre_flight_scan=False,
                    auto_patch_source=False, max_patch_seconds=10.0,
                    no_report=False, no_pause=True,
                )
                self.assertIn(".compress_clip", s)
                self.assertIn(expected, s)


if __name__ == "__main__":
    unittest.main()

"""Regression: a source name with a literal % broke the segment muxer.

ffmpeg's `segment` muxer parses the ENTIRE output path for printf-style
conversion tokens, not just the basename. A workdir like `.compress_70% Hell`
(derived from a source named "70% Hell") makes ffmpeg read `% H` as an invalid
conversion specifier and reject the whole template ("Invalid segment filename
template", exit 234), aborting the split phase before any chunk is written.

split_source must escape literal % in the workdir portion to %% while leaving
the intended src_%04d.mkv pattern intact. Without this guard, any source whose
name contains % fails to encode at all. Escaping only the ffmpeg argument (not
the on-disk workdir name) keeps the `.compress_<stem>` / `.split_done` resume
convention unchanged.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import chunking  # noqa: E402


class SplitSourcePercentEscapeTest(unittest.TestCase):
    def _capture_segment_template(self, workdir_name: str) -> str:
        """Run split_source with a faked ffmpeg and return the segment muxer's
        output-template argument (the cmd element ending in src_%04d.mkv)."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td) / workdir_name
            with mock.patch.object(chunking.subprocess, "run", fake_run):
                chunking.split_source(Path("in.mkv"), workdir, 60)
        return next(a for a in captured["cmd"] if a.endswith("src_%04d.mkv"))

    def test_percent_in_workdir_is_escaped_in_segment_template(self) -> None:
        template = self._capture_segment_template(".compress_70% Hell")

        # The intended chunk-numbering pattern survives untouched.
        self.assertTrue(template.endswith("src_%04d.mkv"))
        # The literal % in the workdir name is escaped to %%, so ffmpeg won't
        # parse "% H" as a conversion spec and reject the template.
        self.assertIn("70%% Hell", template)
        # Belt-and-suspenders: in the workdir portion (everything before the
        # trailing src_%04d.mkv pattern) every % must be half of a %% pair —
        # no stray single % that ffmpeg would read as a conversion spec.
        head = template[: -len("src_%04d.mkv")]
        self.assertEqual(head.count("%"), head.count("%%") * 2)

    def test_percent_token_in_workdir_does_not_collide_with_pattern(self) -> None:
        # The nastiest case: the source stem itself looks like a conversion
        # spec (e.g. a file named "clip_%04d"). The workdir's literal %04d must
        # be escaped to %%04d so ffmpeg treats it as text, while the REAL
        # src_%04d.mkv counter (appended after the escape) stays a live token.
        template = self._capture_segment_template(".compress_clip_%04d")
        self.assertTrue(template.endswith("src_%04d.mkv"))
        self.assertIn("clip_%%04d", template)
        head = template[: -len("src_%04d.mkv")]
        self.assertEqual(head.count("%"), head.count("%%") * 2)

    def test_clean_workdir_template_is_unchanged(self) -> None:
        # No % in the workdir: the template is the workdir joined to the
        # unmodified pattern, with no spurious escaping introduced.
        template = self._capture_segment_template(".compress_clean name")
        self.assertTrue(template.endswith("src_%04d.mkv"))
        self.assertNotIn("%%", template)
        self.assertIn("clean name", template)


if __name__ == "__main__":
    unittest.main()

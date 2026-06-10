"""needs_fix sidecar must be written atomically (temp + os.replace).

The sidecar is the contract a follow-up fixer (Claude or the user) reads to
produce a replacement chunk. A kill mid-write must never leave a truncated,
unparseable JSON at the final path — every other load-bearing JSON in the
repo (quality sidecar, preflight cache, queue state) already uses the
temp-then-replace pattern.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.chunk_recovery import write_needs_fix_sidecar  # noqa: E402


class NeedsFixSidecarAtomicTest(unittest.TestCase):
    def test_sidecar_written_via_os_replace(self):
        """Structural pin: the write path must go through os.replace, not a
        direct write_text at the final name."""
        body = inspect.getsource(write_needs_fix_sidecar)
        body = re.sub(r"#[^\n]*", "", body)  # ignore comments
        self.assertIn("os.replace", body)

    def test_sidecar_lands_parseable_with_expected_contract_fields(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            chunk = workdir / "src_0007.mkv"
            chunk.write_bytes(b"src")
            sidecar = write_needs_fix_sidecar(
                workdir, chunk,
                chunk_index=6, seg_sec=60,
                choke_info={"speed": 0.01, "wall_seconds": 400.0},
                errors=None,
                original_x265_params="me=star:subme=4",
                original_preset="slow", original_crf=22,
                original_pix_fmt="yuv420p10le",
            )
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(data["choked_chunk"], "src_0007.mkv")
            self.assertTrue(data["expected_output_path"].endswith(
                "enc_src_0007.mkv"))
            # No temp residue next to the sidecar.
            self.assertEqual(
                [p.name for p in workdir.glob("*.needs_fix.json.tmp*")], [])


if __name__ == "__main__":
    unittest.main()

"""Tier 0.1 regression: a choked chunk's `enc_*.part.mkv` must be
QUARANTINED (renamed aside), never unlinked.

Partial encoded bytes are user data per the never-delete-encoded-chunks
rule that `chunk_worker._encode_one_chunk_with_display` and
`chunk_recovery._quarantine_part` already honor. `skipped_collector` was
the one place that deleted them.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Make the skill package importable whether run standalone or via
# `python -m unittest discover -s tests -t .` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.display import ParallelDisplay  # noqa: E402
from encode_modules.skipped_collector import collect_skipped  # noqa: E402


class QuarantineChokedPartTest(unittest.TestCase):
    def test_choked_part_is_quarantined_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            # A source chunk plus the partial encode left behind when its
            # ffmpeg was choke-killed. No final enc_*.mkv => genuinely skipped.
            chunk = workdir / "src_0003.mkv"
            chunk.write_bytes(b"fake source chunk bytes")
            part = workdir / "enc_src_0003.part.mkv"
            part_bytes = b"PARTIAL-ENCODE-BYTES-worth-real-CPU-time"
            part.write_bytes(part_bytes)

            display = ParallelDisplay(parallel=1, total=1, already_done=0,
                                      workdir=workdir)
            display.choked_chunks = {
                "src_0003.mkv": {"speed": 0.01, "wall_seconds": 400.0},
            }

            skipped = collect_skipped(
                [chunk], workdir, display,
                x265_params="me=star:subme=4:merange=57", preset="slow",
                crf=22, pix_fmt="yuv420p10le", segment_seconds=60,
            )

            # The chunk was recorded as skipped.
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["chunk_name"], "src_0003.mkv")

            # The original .part no longer sits where the next encode scans.
            self.assertFalse(
                part.exists(),
                "stale .part should be moved aside so a re-encode starts clean",
            )

            # Its bytes are PRESERVED in a quarantine file, not destroyed.
            quarantined = list(workdir.glob("enc_src_0003.part*"))
            self.assertEqual(
                len(quarantined), 1,
                f"expected exactly one quarantined part, found {quarantined}",
            )
            self.assertNotEqual(
                quarantined[0].name, "enc_src_0003.part.mkv",
                "quarantine must rename the part aside, not keep the live name",
            )
            self.assertEqual(
                quarantined[0].read_bytes(), part_bytes,
                "quarantined bytes must be byte-identical to the original part",
            )


if __name__ == "__main__":
    unittest.main()

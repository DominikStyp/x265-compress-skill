"""Serial encoder: a FAILED chunk's `.part.mkv` must be quarantined, not
unlinked.

The parallel path (`chunk_worker`, `chunk_recovery._quarantine_part`,
`skipped_collector`) honors the never-delete-encoded-chunks rule on every
failure path. The serial path was the one remaining place that `unlink()`ed
the partial bytes of the attempt that just failed — destroying encoded work
the user may want to inspect (and that the rule declares to be user data).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import encode_serial  # noqa: E402


class _FakeFF:
    """The chunk ffmpeg: 'writes' some partial output, then fails."""

    def __init__(self, part: Path):
        self._part = part

        class _Out:
            def close(self_inner):
                pass

        self.stdout = _Out()

    def wait(self):
        self._part.write_bytes(b"PARTIAL-ENCODED-BYTES")
        return 1  # encode failure


class _FakeProg:
    def wait(self):
        return 0


class SerialFailedPartQuarantineTest(unittest.TestCase):
    def test_failed_part_is_quarantined_not_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            chunk = workdir / "src_0001.mkv"
            chunk.write_bytes(b"src")
            part = workdir / "enc_src_0001.part.mkv"

            popens = []

            def fake_popen(cmd, **kwargs):
                # First Popen per chunk is ffmpeg, second is progress.py.
                popens.append(cmd)
                return _FakeFF(part) if len(popens) % 2 == 1 else _FakeProg()

            with mock.patch.object(encode_serial.subprocess, "Popen",
                                   side_effect=fake_popen):
                with mock.patch.object(encode_serial, "probe_duration",
                                       return_value=10.0):
                    with self.assertRaises(SystemExit):
                        encode_serial.encode_chunks_serial(
                            [chunk], workdir,
                            crf=22, preset="slow", pix_fmt="yuv420p10le",
                            x265_params="me=star:subme=4",
                        )

            # The partial bytes survive in a quarantine rename...
            quarantined = list(workdir.glob("enc_src_0001.part.*.mkv"))
            self.assertEqual(
                len(quarantined), 1,
                "failed .part must be renamed aside (quarantined), found: "
                f"{[p.name for p in workdir.iterdir()]}")
            self.assertEqual(quarantined[0].read_bytes(),
                             b"PARTIAL-ENCODED-BYTES")
            # ...and the live .part slot is clear for the next attempt.
            self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()

"""Concat-stage data-safety regressions.

Three invariants pinned here:

1. The concat list is built from the EXACT expected set (one enc_src_NNNN.mkv
   per src_NNNN.mkv) — never from a broad `enc_*.mkv` glob. A broad glob
   matches `enc_src_NNNN.broken-<stamp>.mkv` quarantine files left by
   `verify_loop.quarantine_chunk`, splicing a corrupt segment INTO the final
   video (and doubling that position) on any quarantine→re-encode retry.

2. The final output is written ATOMICALLY: ffmpeg muxes into a `.concat-tmp`
   temp name (with an explicit `-f matroska`, since the temp extension can't
   drive muxer inference) and the temp is `os.replace`d onto `dst` only after
   ffmpeg exits 0. Without this, a kill mid-concat leaves a truncated `dst`
   that the `dst.exists()` short-circuit in encode_resumable.main() then
   mistakes for a finished encode on the next run.

3. `cleanup()` (shutil.rmtree — the most destructive call in the pipeline)
   carries its own `ensure_not_source` guard instead of relying on every
   caller to remember it.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import chunking, source_guard  # noqa: E402


class _FakeConcatPopen:
    """Stands in for the concat ffmpeg: records the command, then 'writes'
    the output (last argv element) when waited on, like ffmpeg would."""

    calls: list[list[str]] = []
    returncode_to_use = 0

    def __init__(self, cmd, **kwargs):
        type(self).calls.append(list(cmd))
        self._out = Path(cmd[-1])
        self.stdout = iter(())  # read_ffmpeg_progress just iterates lines

    def wait(self):
        self._out.write_bytes(b"CONCAT-OUTPUT-BYTES")
        return type(self).returncode_to_use

    def poll(self):
        return type(self).returncode_to_use


class ConcatListExclusionTest(unittest.TestCase):
    def setUp(self):
        _FakeConcatPopen.calls = []
        _FakeConcatPopen.returncode_to_use = 0

    def _make_workdir(self, td: str) -> tuple[Path, Path]:
        workdir = Path(td) / ".compress_x"
        workdir.mkdir()
        for i in (1, 2):
            (workdir / f"src_{i:04d}.mkv").write_bytes(b"src")
            (workdir / f"enc_src_{i:04d}.mkv").write_bytes(b"enc")
        return workdir, Path(td) / "out.mkv"

    def test_quarantined_broken_chunk_not_in_concat_list(self):
        """A `.broken-<stamp>.mkv` quarantine (no `.part` in its suffixes!)
        must never appear in concat.txt — splicing it in corrupts the
        final video silently."""
        with tempfile.TemporaryDirectory() as td:
            workdir, dst = self._make_workdir(td)
            (workdir / "enc_src_0001.broken-2026-01-01_00-00-00.mkv"
             ).write_bytes(b"CORRUPT")
            # .part-derived quarantines + in-flight parts were already
            # excluded by the old suffix check; keep them pinned too.
            (workdir / "enc_src_0002.part.autofix-broken-123.mkv"
             ).write_bytes(b"CORRUPT")
            (workdir / "enc_src_0002.part.mkv").write_bytes(b"INFLIGHT")

            with mock.patch.object(chunking.subprocess, "Popen",
                                   _FakeConcatPopen):
                chunking.concat_chunks(workdir, dst, total_dur=10.0)

            listed = (workdir / "concat.txt").read_text(encoding="utf-8")
            self.assertNotIn(".broken-", listed)
            self.assertNotIn(".part", listed)
            self.assertIn("enc_src_0001.mkv", listed)
            self.assertIn("enc_src_0002.mkv", listed)
            # Exactly one line per expected chunk — no duplicated positions.
            self.assertEqual(
                len([l for l in listed.splitlines() if l.strip()]), 2)

    def test_stray_enc_file_from_other_naming_not_included(self):
        """Only the paired enc_<src>.mkv set is eligible — any other
        enc_*.mkv stray (e.g. from an older run layout) stays out."""
        with tempfile.TemporaryDirectory() as td:
            workdir, dst = self._make_workdir(td)
            (workdir / "enc_src_9999.mkv").write_bytes(b"STRAY")  # no src pair

            with mock.patch.object(chunking.subprocess, "Popen",
                                   _FakeConcatPopen):
                chunking.concat_chunks(workdir, dst, total_dur=10.0)

            listed = (workdir / "concat.txt").read_text(encoding="utf-8")
            self.assertNotIn("enc_src_9999.mkv", listed)


class ConcatAtomicWriteTest(unittest.TestCase):
    def setUp(self):
        _FakeConcatPopen.calls = []
        _FakeConcatPopen.returncode_to_use = 0

    def _make_workdir(self, td: str) -> tuple[Path, Path]:
        workdir = Path(td) / ".compress_x"
        workdir.mkdir()
        (workdir / "src_0001.mkv").write_bytes(b"src")
        (workdir / "enc_src_0001.mkv").write_bytes(b"enc")
        return workdir, Path(td) / "out.mkv"

    def test_ffmpeg_writes_temp_then_replaced_onto_dst(self):
        with tempfile.TemporaryDirectory() as td:
            workdir, dst = self._make_workdir(td)
            with mock.patch.object(chunking.subprocess, "Popen",
                                   _FakeConcatPopen):
                chunking.concat_chunks(workdir, dst, total_dur=10.0)

            cmd = _FakeConcatPopen.calls[0]
            out_arg = cmd[-1]
            self.assertNotEqual(out_arg, str(dst),
                                "ffmpeg must NOT write the final dst in place")
            self.assertTrue(out_arg.endswith(".concat-tmp"))
            # The temp extension can't drive ffmpeg's muxer inference, so the
            # format must be explicit (same bug class as the dtsfix.tmp leg).
            self.assertIn("-f", cmd)
            self.assertIn("matroska", cmd)
            # Success: temp promoted onto dst, temp gone.
            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_bytes(), b"CONCAT-OUTPUT-BYTES")
            self.assertFalse(Path(out_arg).exists())

    def test_failed_concat_leaves_no_dst(self):
        """rc != 0 → dst must not exist, so a re-run's dst.exists()
        short-circuit can't mistake a failed/partial mux for success."""
        with tempfile.TemporaryDirectory() as td:
            workdir, dst = self._make_workdir(td)
            _FakeConcatPopen.returncode_to_use = 1
            with mock.patch.object(chunking.subprocess, "Popen",
                                   _FakeConcatPopen):
                with self.assertRaises(SystemExit):
                    chunking.concat_chunks(workdir, dst, total_dur=10.0)
            self.assertFalse(
                dst.exists(),
                "a failed concat must never leave bytes at the final path")


class CleanupSourceGuardTest(unittest.TestCase):
    def tearDown(self):
        source_guard._protected_sources.clear()

    def test_cleanup_refuses_protected_path(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "guarded"
            target.mkdir()
            (target / "x.txt").write_text("data", encoding="utf-8")
            source_guard.protect_source(target)
            with self.assertRaises(RuntimeError):
                chunking.cleanup(target)
            self.assertTrue((target / "x.txt").exists())

    def test_cleanup_removes_unprotected_workdir(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td) / ".compress_x"
            workdir.mkdir()
            (workdir / "enc_src_0001.mkv").write_bytes(b"enc")
            chunking.cleanup(workdir)
            self.assertFalse(workdir.exists())


if __name__ == "__main__":
    unittest.main()

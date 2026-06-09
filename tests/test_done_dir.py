"""done_dir resolution + the move-after-success step.

The data-safety invariant is non-negotiable: a user's source file must NEVER
be lost. The move sequence is output-first → source-second so that a
mid-failure leaves either (source still in place + output in done_dir) — a
recoverable state — or (both moved) — the happy path. We refuse to overwrite
an existing destination, refuse to move into the workdir (cleanup would eat
the result), and treat a same-directory done_dir as a no-op.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.done_dir import (  # noqa: E402
    DoneDirRefusedError,
    move_to_done_dir,
    resolve_done_dir,
)


class ResolveDoneDirTest(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(resolve_done_dir(None, base_dir=Path("/tmp")))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(resolve_done_dir("", base_dir=Path("/tmp")))

    def test_absolute_path_used_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            abs_path = Path(td) / "archive"
            resolved = resolve_done_dir(str(abs_path),
                                        base_dir=Path("/elsewhere"))
            self.assertEqual(resolved, abs_path.resolve())
            self.assertTrue(resolved.is_dir())  # mkdir parents

    def test_relative_path_resolves_against_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            resolved = resolve_done_dir("./done", base_dir=base)
            self.assertEqual(resolved, (base / "done").resolve())
            self.assertTrue(resolved.is_dir())

    def test_tilde_expansion(self) -> None:
        # ~ should resolve to the user's home — exact value depends on host
        # but the resolved Path must be absolute and not contain a literal ~.
        resolved = resolve_done_dir("~/x265-archive", base_dir=Path("/tmp"))
        self.assertTrue(resolved.is_absolute())
        self.assertNotIn("~", str(resolved))


class MoveToDoneDirTest(unittest.TestCase):
    def _setup_pair(self, td: Path):
        src = td / "movie.mp4"
        src.write_bytes(b"src")
        out = td / "movie.mkv"
        out.write_bytes(b"out")
        return src, out

    def test_happy_path_moves_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            done = td / "done"
            done.mkdir()
            result = move_to_done_dir(source=src, output=out,
                                      done_dir=done, workdir=td / ".tmp")
            self.assertEqual(result.source_final, done / "movie.mp4")
            self.assertEqual(result.output_final, done / "movie.mkv")
            self.assertTrue((done / "movie.mp4").exists())
            self.assertTrue((done / "movie.mkv").exists())
            # Originals gone.
            self.assertFalse(src.exists())
            self.assertFalse(out.exists())

    def test_refuses_if_destination_source_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            done = td / "done"
            done.mkdir()
            (done / "movie.mp4").write_bytes(b"OLD")
            with self.assertRaises(DoneDirRefusedError):
                move_to_done_dir(source=src, output=out, done_dir=done,
                                 workdir=td / ".tmp")
            # Neither original was touched.
            self.assertTrue(src.exists())
            self.assertTrue(out.exists())
            # Pre-existing was not overwritten.
            self.assertEqual((done / "movie.mp4").read_bytes(), b"OLD")

    def test_refuses_if_destination_output_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            done = td / "done"
            done.mkdir()
            (done / "movie.mkv").write_bytes(b"OLD")
            with self.assertRaises(DoneDirRefusedError):
                move_to_done_dir(source=src, output=out, done_dir=done,
                                 workdir=td / ".tmp")
            self.assertTrue(src.exists())
            self.assertTrue(out.exists())

    def test_refuses_when_BOTH_destinations_exist(self) -> None:
        # User manually copied source back AND a prior output exists. The
        # OR-guard fires on the first hit, but the test asserts neither
        # original gets moved AND neither pre-existing destination gets
        # touched. Closes a refuse-test hole the reviewer flagged.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            done = td / "done"
            done.mkdir()
            (done / "movie.mp4").write_bytes(b"OLD-SRC")
            (done / "movie.mkv").write_bytes(b"OLD-OUT")
            with self.assertRaises(DoneDirRefusedError):
                move_to_done_dir(source=src, output=out, done_dir=done,
                                 workdir=td / ".tmp")
            # All four files survive untouched.
            self.assertTrue(src.exists())
            self.assertTrue(out.exists())
            self.assertEqual((done / "movie.mp4").read_bytes(), b"OLD-SRC")
            self.assertEqual((done / "movie.mkv").read_bytes(), b"OLD-OUT")

    def test_refuses_if_done_dir_inside_workdir(self) -> None:
        # cleanup() would then delete the moved files. Refuse early.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            workdir = td / ".tmp"
            workdir.mkdir()
            done = workdir / "done"
            done.mkdir()
            with self.assertRaises(DoneDirRefusedError):
                move_to_done_dir(source=src, output=out, done_dir=done,
                                 workdir=workdir)

    def test_same_dir_as_source_is_noop_not_error(self) -> None:
        # The user pointed done_dir at the source's own directory — there's
        # nothing to move, but it's not a misconfiguration. Return the same
        # paths so callers can treat the result uniformly.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            result = move_to_done_dir(source=src, output=out, done_dir=td,
                                      workdir=td / ".tmp")
            self.assertEqual(result.source_final, src)
            self.assertEqual(result.output_final, out)
            self.assertEqual(result.moved, False)
            self.assertTrue(src.exists())
            self.assertTrue(out.exists())

    def test_cross_volume_handled_via_shutil_move(self) -> None:
        # shutil.move falls back to copy+delete on a different filesystem.
        # We can't easily simulate a cross-volume move in tests, but we CAN
        # verify the implementation uses shutil.move (not Path.rename which
        # would EXDEV on a real cross-volume move).
        import inspect

        import encode_modules.done_dir as dd
        src_text = inspect.getsource(dd)
        self.assertIn("shutil.move", src_text,
                      "move_to_done_dir must use shutil.move for "
                      "cross-volume safety")

    def test_sidecars_quality_stays_preflight_and_hooks_deleted(self) -> None:
        # v1.18.1: preflight is now treated as a per-job artifact (deleted
        # on success) — the prior "content-keyed cache, useful on a future
        # re-encode" rationale was scrapped because the dst-exists guard at
        # the top of encode_resumable.main already short-circuits re-runs,
        # so the preflight cache only added clutter.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, out = self._setup_pair(td)
            # Sidecars live next to the source/output in .tmp/.
            tmp = td / ".tmp"
            tmp.mkdir()
            (tmp / "movie.quality.json").write_text("{}", encoding="utf-8")
            (tmp / "movie.preflight.json").write_text("{}", encoding="utf-8")
            (tmp / "movie.hooks.json").write_text(
                '{"on_chunk_done": ["x"]}', encoding="utf-8")
            done = td / "done"
            done.mkdir()
            move_to_done_dir(source=src, output=out, done_dir=done,
                             workdir=tmp, sidecar_dir=tmp)
            # hooks + preflight are per-job — both removed.
            self.assertFalse((tmp / "movie.hooks.json").exists())
            self.assertFalse((tmp / "movie.preflight.json").exists())
            # quality stays — downstream tooling still reads it.
            self.assertTrue((tmp / "movie.quality.json").exists())

    def test_hooks_sidecar_deleted_for_mkv_source(self) -> None:
        # Reviewer-flagged bug: when the source is `.mkv`, derive_output_path
        # produces `<stem>.x265.mkv`, so `output.stem` is `<stem>.x265` and
        # the cleanup looked for `<stem>.x265.hooks.json` — but the file is
        # written by script_writer as `<source.stem>.hooks.json` (i.e.
        # `<stem>.hooks.json`). The sidecar leaked into archive.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mkv"
            src.write_bytes(b"src")
            out = td / "movie.x265.mkv"   # derive_output_path's name
            out.write_bytes(b"out")
            tmp = td / ".tmp"
            tmp.mkdir()
            # Hooks sidecar keyed on SOURCE stem (per script_writer).
            (tmp / "movie.hooks.json").write_text(
                '{"on_chunk_done": ["x"]}', encoding="utf-8")
            done = td / "done"
            done.mkdir()
            move_to_done_dir(source=src, output=out, done_dir=done,
                             workdir=tmp, sidecar_dir=tmp)
            self.assertFalse((tmp / "movie.hooks.json").exists(),
                             "hooks sidecar should have been deleted")


if __name__ == "__main__":
    unittest.main()

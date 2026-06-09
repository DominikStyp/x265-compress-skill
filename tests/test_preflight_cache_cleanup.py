"""After a successful encode, the per-source ``<src>.preflight.json`` cache
sidecar must be removed — keeping it forever was clutter (the next encode
of the same source re-scans anyway, and the cache only helps when the
source is unchanged and is being re-attempted from scratch, which is
already short-circuited by the ``dst.exists()`` early-return at the top
of ``encode_resumable.main``).

These tests pin the small helper ``delete_preflight_cache(src)`` and the
parity behaviour in ``done_dir._cleanup_sidecars`` (the preflight cache
that may have been written into the sidecar dir for the same stem is
also wiped).
"""
from __future__ import annotations

import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import encode_resumable  # noqa: E402
from encode_modules import pre_flight  # noqa: E402
from encode_modules.done_dir import _cleanup_sidecars  # noqa: E402


class DeletePreflightCacheTest(unittest.TestCase):
    def test_removes_sidecar_in_logs(self) -> None:
        # v1.19.0: cache lives at <src.parent>/logs/<src.name>.preflight.json.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mp4"
            src.write_bytes(b"src")
            cache = td / "logs" / f"{src.name}.preflight.json"
            cache.parent.mkdir()
            cache.write_text("{}", encoding="utf-8")
            self.assertTrue(cache.exists())

            self.assertTrue(pre_flight.delete_preflight_cache(src))
            self.assertFalse(cache.exists())

    def test_returns_false_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mp4"
            src.write_bytes(b"src")
            # no preflight sidecar exists — best-effort helper must not raise.
            self.assertFalse(pre_flight.delete_preflight_cache(src))

    def test_silent_on_oserror(self) -> None:
        # File path that can never be unlinked — verify swallow, not crash.
        # We can't easily mint a real permission failure cross-platform, so
        # patch Path.unlink to raise OSError. Use mock.patch.object so the
        # restore is exception-safe AND scope-limited to this block — a raw
        # `Path.unlink = boom` would explode any concurrent Path cleanup
        # inside the same `tempfile.TemporaryDirectory.__exit__` (reviewer
        # caught this — Windows / pytest-xdist could trip it).
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mp4"
            src.write_bytes(b"src")
            cache = td / "logs" / f"{src.name}.preflight.json"
            cache.parent.mkdir()
            cache.write_text("{}", encoding="utf-8")

            def boom(self, *a, **k):
                raise OSError("simulated")

            with mock.patch.object(Path, "unlink", boom):
                # Must not raise; returns False (the unlink failed).
                self.assertFalse(pre_flight.delete_preflight_cache(src))
            # File still on disk — confirms the patch took effect.
            self.assertTrue(cache.exists())


class DoneDirSidecarCleanupRemovesPreflightTest(unittest.TestCase):
    """Defensive parity: if a preflight sidecar landed in the workdir / .tmp
    sidecar dir (legacy / future caller), the move-to-done_dir cleanup
    deletes it just like it deletes the hooks sidecar. This test used to
    assert preflight STAYS — that was the pre-v1.18.1 behaviour. Flipped
    when we moved to "preflight is per-job, not per-source"."""

    def test_preflight_sidecar_removed_alongside_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            preflight = tmp / "movie.preflight.json"
            hooks = tmp / "movie.hooks.json"
            preflight.write_text("{}", encoding="utf-8")
            hooks.write_text('{"on_chunk_done": ["x"]}', encoding="utf-8")

            _cleanup_sidecars(tmp, "movie")

            self.assertFalse(preflight.exists(),
                             "preflight cache must be cleaned up after success")
            self.assertFalse(hooks.exists(),
                             "hooks sidecar cleanup must still fire")

    def test_no_preflight_sidecar_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            # No sidecars at all — cleanup must not raise.
            _cleanup_sidecars(tmp, "movie")
            self.assertEqual(list(tmp.iterdir()), [])


class SuccessBranchOnlyTest(unittest.TestCase):
    """`delete_preflight_cache` must only fire on the SUCCESS path of
    `encode_resumable.main` — never during abort/exit paths. The cache is
    valuable post-abort: re-running the encode skips re-scanning the source.

    Structural pin: the call appears exactly once in `main`, and that one
    call sits AFTER `finalize_history_state(` AND BEFORE `cleanup(workdir`.
    A future refactor that hoists the call above any `sys.exit` will trip
    this test."""

    @staticmethod
    def _code_only(source: str) -> str:
        # Strip `# ...` comments so substring searches don't match inline
        # rationale text. Survives backtick-quoted symbols in docstring
        # comments (e.g. "`cleanup(workdir)` below wipes …").
        return re.sub(r"#[^\n]*", "", source)

    def test_called_exactly_once_in_main(self) -> None:
        body = self._code_only(inspect.getsource(encode_resumable.main))
        self.assertEqual(body.count("delete_preflight_cache("), 1,
                         "delete_preflight_cache must appear exactly once "
                         "in main(); a second call site likely sits on an "
                         "abort path and would discard valuable cache state")

    def test_call_sits_between_finalize_and_cleanup(self) -> None:
        body = self._code_only(inspect.getsource(encode_resumable.main))
        finalize_pos = body.find("finalize_history_state(")
        delete_pos = body.find("delete_preflight_cache(")
        cleanup_pos = body.find("cleanup(workdir)")
        self.assertGreater(finalize_pos, -1,
                           "finalize_history_state call missing")
        self.assertGreater(delete_pos, -1,
                           "delete_preflight_cache call missing")
        self.assertGreater(cleanup_pos, -1, "cleanup(workdir) call missing")
        self.assertLess(finalize_pos, delete_pos,
                        "delete must run AFTER history is flushed")
        self.assertLess(delete_pos, cleanup_pos,
                        "delete must run BEFORE workdir cleanup so the "
                        "comment's rationale about src vs encode_src stays "
                        "correct")


if __name__ == "__main__":
    unittest.main()

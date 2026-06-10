"""source_guard: the original source stays off-limits even after a patch.

HARD RULE — the encoder must NEVER delete or rename the user-supplied
source file. `source_guard` is the defense-in-depth backstop: it holds a
SET of protected paths and `ensure_not_source` raises before any
unlink/rename/rmtree whose target matches one of them.

The invariant these tests pin is that `protect_source` ADDS — it never
replaces. With `--auto-patch-source` two sources are live at once (the
user's original AND the patched copy the pipeline encodes). The old
single-slot design silently dropped the guard on the original the instant
a patch was adopted — exactly the path this module exists to backstop. We
also pin the OS-aware comparison (case-insensitive on Windows) and that a
relative path resolves to the same protected absolute path, because a
real call site can hand `ensure_not_source` either form.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make the skill package importable whether run standalone or via
# `python -m unittest discover -s tests -t .` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import source_guard  # noqa: E402
from encode_modules.source_guard import (  # noqa: E402
    ensure_not_source,
    get_protected_sources,
    protect_source,
)


class SourceGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        # The registry is module-global; isolate every test from leakage.
        source_guard._protected_sources.clear()

    def tearDown(self) -> None:
        source_guard._protected_sources.clear()

    def test_ensure_not_source_raises_on_protected_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "original.mp4"
            src.write_bytes(b"src")
            protect_source(src)
            with self.assertRaises(RuntimeError) as ctx:
                ensure_not_source(src)
            msg = str(ctx.exception)
            self.assertIn("SOURCE FILE PROTECTION TRIGGERED", msg)
            # The offending path is in the message so the call site is findable.
            self.assertIn(str(src.resolve()), msg)

    def test_noop_for_other_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "original.mp4"
            other = Path(td) / "workdir_chunk.mkv"
            src.write_bytes(b"src")
            protect_source(src)
            # A path that isn't the protected source must pass through silently.
            ensure_not_source(other)

    def test_noop_when_nothing_protected(self) -> None:
        # Empty set => no-op (unit tests that never call protect_source must
        # not blow up on the guard).
        self.assertEqual(get_protected_sources(), frozenset())
        ensure_not_source(Path("anything_at_all.mkv"))

    def test_protect_adds_never_replaces(self) -> None:
        # Pins the auto-patch fix: protecting the patched copy must NOT drop
        # the guard on the original — both stay off-limits simultaneously.
        with tempfile.TemporaryDirectory() as td:
            original = Path(td) / "original.mp4"
            patched = Path(td) / "original.patched.mp4"
            original.write_bytes(b"orig")
            patched.write_bytes(b"patched")

            protect_source(original)
            protect_source(patched)

            with self.assertRaises(RuntimeError):
                ensure_not_source(original)
            with self.assertRaises(RuntimeError):
                ensure_not_source(patched)

    @unittest.skipUnless(os.name == "nt", "Windows-only case-insensitive path")
    def test_case_insensitive_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Force a mixed-case name so lower-casing the query is a real change.
            src = Path(td) / "MixedCase_Source.MP4"
            src.write_bytes(b"src")
            protect_source(src)
            # A differently-cased string for the same file must still trip.
            with self.assertRaises(RuntimeError):
                ensure_not_source(str(src).lower())

    def test_relative_path_resolves_to_protected_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = (Path(td) / "original.mp4").resolve()
            src.write_bytes(b"src")
            protect_source(src)
            try:
                rel = os.path.relpath(src)
            except ValueError:
                # On Windows a relpath across drive letters is impossible.
                self.skipTest("source on a different drive than cwd")
            # A relative path that resolves to the protected absolute path
            # must trip the guard too — call sites can hand either form.
            with self.assertRaises(RuntimeError):
                ensure_not_source(Path(rel))

    def test_get_protected_sources_returns_registered_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.mp4"
            b = Path(td) / "b.mp4"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            protect_source(a)
            protect_source(b)
            got = get_protected_sources()
            self.assertEqual(got, frozenset({a.resolve(), b.resolve()}))


if __name__ == "__main__":
    unittest.main()

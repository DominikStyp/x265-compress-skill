"""Regression: a successful encode of an AUTO-PATCHED source must not crash
in the end-of-run summary (and must not report a false failure).

When `--auto-patch-source` rebuilds a broken h264 source, the patched file
lives INSIDE the workdir (`.tmp/.compress_<stem>/source-patched.mp4`). The
workdir is wiped by `cleanup()` once the encode is verified. If the pipeline
keeps treating that patched path as the "source", the post-cleanup reporters
(`print_summary`, `write_single_file_report`) `stat()` a file that no longer
exists → FileNotFoundError → the process exits 1 even though the encode
succeeded (output produced, verified, history flushed).

The original input lives OUTSIDE the workdir and is never deleted (data-safety
invariant + `_validate_paths` rejects workdir == src.parent), so the reporters
must reference IT, not the throwaway patched copy. This pins that contract.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import encode_resumable  # noqa: E402
from encode_modules import reporting  # noqa: E402


class PatchedSourceCleanupTest(unittest.TestCase):
    def test_summary_survives_workdir_cleanup_after_auto_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            original = base / "broken source.mp4"
            original.write_bytes(b"o" * 2_000_000)
            dst = base / "out.mkv"
            workdir = base / ".tmp" / ".compress_broken source"
            workdir.mkdir(parents=True)
            # The patched copy that auto-patch produces, living in the workdir
            # that cleanup() will delete.
            patched = workdir / "source-patched.mp4"
            patched.write_bytes(b"p" * 1_900_000)

            args = SimpleNamespace(
                input=str(original), output=str(dst), workdir=str(workdir),
                segment_seconds=60, hooks_config=None,
                total_duration_seconds=10.0, source_bytes=None,
                auto_patch_source=True, no_report=True,
            )

            def fake_verify_loop(src, chunks, wd, out, **kwargs):
                # The real loop concats the verified output into place; mimic
                # just enough that the summary has a dst to stat.
                out.write_bytes(b"y" * 1_000_000)
                return []

            with mock.patch.object(encode_resumable, "parse_args",
                                   return_value=args), \
                 mock.patch.object(encode_resumable, "handle_preflight",
                                   return_value=("patched", patched,
                                                 {"passed": False},
                                                 {"passed": True})), \
                 mock.patch.object(encode_resumable, "split_source",
                                   return_value=[patched]), \
                 mock.patch.object(encode_resumable, "reorder_middle_first",
                                   return_value=[patched]), \
                 mock.patch.object(encode_resumable, "probe_duration",
                                   return_value=5.0), \
                 mock.patch.object(encode_resumable, "init_history_state"), \
                 mock.patch.object(encode_resumable, "finalize_history_state"), \
                 mock.patch.object(encode_resumable, "mark_status"), \
                 mock.patch.object(encode_resumable, "ensure_not_source"), \
                 mock.patch.object(encode_resumable, "run_encode_verify_loop",
                                   side_effect=fake_verify_loop), \
                 mock.patch.object(encode_resumable,
                                   "measure_quality_and_write_sidecar",
                                   return_value={"vmaf_mean": 97.0}), \
                 mock.patch("sys.stdout"):
                rc = encode_resumable.main()

            # A verified encode must report success, not a false exit-1.
            self.assertEqual(rc, 0)
            # cleanup() wiped the workdir (and the patched copy with it)...
            self.assertFalse(patched.exists())
            # ...but the user's original is untouched and still statable.
            self.assertTrue(original.is_file())


class PatchedSourceHookNamesOriginalTest(unittest.TestCase):
    """The on_chunk_done hook (e.g. Pushbullet notifications) must report the
    ORIGINAL source filename the user recognizes — never the auto-patch's
    `source-patched.mp4` working copy. The hook exposes the source via
    X265_SOURCE, bound at ChunkHook construction, so main() must build it with
    the original `src`, not the patched `encode_src` the pipeline encodes."""

    def test_chunk_hook_is_built_with_the_original_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            original = base / "broken source.mp4"
            original.write_bytes(b"o" * 1000)
            dst = base / "out.mkv"
            workdir = base / ".tmp" / ".compress_broken source"
            workdir.mkdir(parents=True)
            patched = workdir / "source-patched.mp4"
            patched.write_bytes(b"p" * 900)

            args = SimpleNamespace(
                input=str(original), output=str(dst), workdir=str(workdir),
                segment_seconds=60, hooks_config=None,
                total_duration_seconds=10.0, source_bytes=None,
                auto_patch_source=True, no_report=True,
            )

            captured: dict = {}

            class _CapturingHook:
                def __init__(self, command, *, source, workdir, total,
                             **kwargs) -> None:
                    captured["source"] = source
                    self.enabled = False

            def fake_verify_loop(src, chunks, wd, out, **kwargs):
                out.write_bytes(b"y" * 100)
                return []

            with mock.patch.object(encode_resumable, "parse_args",
                                   return_value=args), \
                 mock.patch.object(encode_resumable, "handle_preflight",
                                   return_value=("patched", patched,
                                                 {"passed": False},
                                                 {"passed": True})), \
                 mock.patch.object(encode_resumable, "ChunkHook",
                                   _CapturingHook), \
                 mock.patch.object(encode_resumable, "split_source",
                                   return_value=[patched]), \
                 mock.patch.object(encode_resumable, "reorder_middle_first",
                                   return_value=[patched]), \
                 mock.patch.object(encode_resumable, "probe_duration",
                                   return_value=5.0), \
                 mock.patch.object(encode_resumable, "init_history_state"), \
                 mock.patch.object(encode_resumable, "finalize_history_state"), \
                 mock.patch.object(encode_resumable, "mark_status"), \
                 mock.patch.object(encode_resumable, "ensure_not_source"), \
                 mock.patch.object(encode_resumable, "run_encode_verify_loop",
                                   side_effect=fake_verify_loop), \
                 mock.patch.object(encode_resumable,
                                   "measure_quality_and_write_sidecar",
                                   return_value=None), \
                 mock.patch("sys.stdout"):
                rc = encode_resumable.main()

            self.assertEqual(rc, 0)
            self.assertEqual(captured["source"], original)
            self.assertNotIn("source-patched", str(captured["source"]))


class ReportUsesPassedSourceBytesTest(unittest.TestCase):
    """`write_single_file_report` must record the caller's pre-cleanup
    `source_bytes`, not a fresh `src.stat()`. Re-statting would disagree with
    the `max_size_percent` denominator and could touch disk after the workdir
    is gone (the same class of failure as the print_summary crash)."""

    def test_input_bytes_comes_from_source_bytes_not_a_fresh_stat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.mp4"
            src.write_bytes(b"x" * 1234)        # on-disk size 1234...
            dst = Path(td) / "out.mkv"
            dst.write_bytes(b"y" * 500)
            args = SimpleNamespace(crf=22, preset="slow", parallel=1,
                                   max_output_bytes=None)

            captured: dict = {}

            def fake_write_report(md_path, rows, *, title):
                captured["rows"] = rows

            fake_report = SimpleNamespace(write_report=fake_write_report)
            with mock.patch.dict(sys.modules, {"report": fake_report}):
                reporting.write_single_file_report(
                    src, dst, args=args,
                    source_bytes=999_999,        # ...but caller says 999999
                    elapsed_s=1.0, quality_scores=None,
                )

            row = captured["rows"][0]
            self.assertEqual(row["input_bytes"], 999_999)


if __name__ == "__main__":
    unittest.main()

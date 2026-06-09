"""v1.19.0 layout: every log / sidecar artefact routes into a ``logs/``
subdirectory at the appropriate level (video folder, queue folder, or
history root). One central module — ``encode_modules.log_paths`` —
resolves every path, so writers, readers, and migration share a single
source of truth.

Tests cover:
  * Path resolution for every artefact (the *new* layout).
  * One-shot ``migrate_legacy_logs`` helper that moves extant files from
    the pre-v1.19.0 locations into ``logs/`` once, idempotently.
  * Best-effort semantics: a missing legacy file is a no-op; an OSError
    during a single move never aborts the rest.
  * Concurrent-writer race: if a parallel encoder already migrated a
    file, the second caller refuses to overwrite (keeps the legacy in
    place for the user to audit).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import log_paths  # noqa: E402


class PathResolutionTest(unittest.TestCase):
    def test_logs_dir_appends_logs_subdir(self) -> None:
        self.assertEqual(log_paths.logs_dir(Path("/v")), Path("/v/logs"))

    def test_quality_sidecar_lives_under_logs(self) -> None:
        out = Path("/v/movie.x265.mkv")
        self.assertEqual(log_paths.quality_sidecar_path(out),
                         Path("/v/logs/movie.x265.quality.json"))

    def test_chunk_metrics_lives_under_logs(self) -> None:
        out = Path("/v/movie.x265.mkv")
        self.assertEqual(log_paths.chunk_metrics_path(out),
                         Path("/v/logs/movie.x265.chunk_metrics.jsonl"))

    def test_per_encode_report_lives_under_logs(self) -> None:
        out = Path("/v/movie.x265.mkv")
        self.assertEqual(log_paths.per_encode_report_path(out),
                         Path("/v/logs/movie.x265.report.md"))

    def test_hooks_sidecar_keyed_on_source_name(self) -> None:
        # script_writer + done_dir + encode_resumable all key the hooks
        # sidecar on the SOURCE name (.mkv source → "<name>.hooks.json",
        # NOT "<name>.x265.hooks.json"). Same convention under logs/.
        src = Path("/v/movie.mkv")
        self.assertEqual(log_paths.hooks_sidecar_path(src),
                         Path("/v/logs/movie.hooks.json"))

    def test_preflight_cache_keyed_on_source_full_name(self) -> None:
        # Production preflight used the full source name + ".preflight.json"
        # (e.g. "movie.mp4.preflight.json"). Preserve that key so existing
        # readers / cleaners (delete_preflight_cache) keep working unchanged.
        src = Path("/v/movie.mp4")
        self.assertEqual(log_paths.preflight_cache_path(src),
                         Path("/v/logs/movie.mp4.preflight.json"))

    def test_queue_report_paths(self) -> None:
        q = Path("/q/queue.json")
        self.assertEqual(log_paths.queue_report_path(q),
                         Path("/q/logs/queue_report.md"))
        self.assertEqual(log_paths.queue_report_history_path(q),
                         Path("/q/logs/queue_report.history.json"))

    def test_queue_state_under_logs(self) -> None:
        q = Path("/q/queue.json")
        self.assertEqual(log_paths.queue_state_path(q),
                         Path("/q/logs/queue.state.json"))

    def test_queue_json_status_default(self) -> None:
        q = Path("/q/queue.json")
        self.assertEqual(log_paths.queue_json_status_default_path(q),
                         Path("/q/logs/queue.json-status.ndjson"))

    def test_history_jsonl_path_under_root(self) -> None:
        root = Path("/root")
        self.assertEqual(log_paths.history_jsonl_path(root),
                         Path("/root/logs/encoding_history.jsonl"))


class MigrateVideoFolderTest(unittest.TestCase):
    """`.tmp/<stem>.quality.json`, `<stem>.chunk_metrics.jsonl`,
    `<stem>.report.md`, `<stem>.hooks.json` move into `logs/`.
    `<source>.preflight.json` (next to source) also moves into `logs/`."""

    def test_moves_known_sidecars_in_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            (tmp / "movie.quality.json").write_text("{}", encoding="utf-8")
            (tmp / "movie.chunk_metrics.jsonl").write_text("", encoding="utf-8")
            (tmp / "movie.report.md").write_text("# r", encoding="utf-8")
            (tmp / "movie.hooks.json").write_text("{}", encoding="utf-8")

            moved = log_paths.migrate_video_folder(td)

            self.assertEqual(len(moved), 4)
            for name in ("movie.quality.json", "movie.chunk_metrics.jsonl",
                         "movie.report.md", "movie.hooks.json"):
                self.assertFalse((tmp / name).exists())
                self.assertTrue((td / "logs" / name).exists())

    def test_moves_preflight_caches_next_to_sources(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "movie.mp4").write_bytes(b"src")
            (td / "movie.mp4.preflight.json").write_text(
                "{}", encoding="utf-8")
            (td / "other.mkv").write_bytes(b"src2")
            (td / "other.mkv.preflight.json").write_text(
                "{}", encoding="utf-8")

            moved = log_paths.migrate_video_folder(td)

            self.assertEqual(len(moved), 2)
            self.assertFalse((td / "movie.mp4.preflight.json").exists())
            self.assertFalse((td / "other.mkv.preflight.json").exists())
            self.assertTrue(
                (td / "logs" / "movie.mp4.preflight.json").exists())
            self.assertTrue(
                (td / "logs" / "other.mkv.preflight.json").exists())
            # Source videos themselves are untouched.
            self.assertTrue((td / "movie.mp4").exists())
            self.assertTrue((td / "other.mkv").exists())

    def test_idempotent_second_call_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            (tmp / "movie.quality.json").write_text("{}", encoding="utf-8")

            first = log_paths.migrate_video_folder(td)
            second = log_paths.migrate_video_folder(td)

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])

    def test_refuses_to_overwrite_existing_new_path(self) -> None:
        # Concurrent encoder (or a prior partial migration) already wrote
        # the new location — refuse the move so we don't clobber it. The
        # legacy file stays in place for the user to audit / resolve.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            logs = td / "logs"
            logs.mkdir()
            (tmp / "movie.quality.json").write_text("LEGACY",
                                                    encoding="utf-8")
            (logs / "movie.quality.json").write_text("NEW",
                                                     encoding="utf-8")

            moved = log_paths.migrate_video_folder(td)

            self.assertEqual(moved, [])
            self.assertEqual((tmp / "movie.quality.json").read_text(),
                             "LEGACY")
            self.assertEqual((logs / "movie.quality.json").read_text(),
                             "NEW")

    def test_no_legacy_files_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self.assertEqual(log_paths.migrate_video_folder(td), [])
            # logs/ is created lazily — must NOT materialize on a no-op
            # call (otherwise every read-only inspection creates dirs).
            self.assertFalse((td / "logs").exists())

    def test_swallows_oserror_per_file(self) -> None:
        # If one move fails, the rest must still succeed. The helper
        # returns the files it DID move; the failed one stays in place.
        # v1.19.0 implementation uses os.link first (race-safe), so we
        # patch that primitive.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            (tmp / "good.quality.json").write_text("g", encoding="utf-8")
            (tmp / "bad.quality.json").write_text("b", encoding="utf-8")

            real_link = log_paths.os.link

            def maybe_fail(src, dst, *a, **k):
                if "bad" in str(src):
                    # Non-EXDEV/EPERM OSError → migration treats as a
                    # real failure (per _is_crossdev_or_perm gate) and
                    # leaves the legacy file in place.
                    raise OSError(13, "simulated")
                return real_link(src, dst, *a, **k)

            with mock.patch.object(log_paths.os, "link", maybe_fail):
                moved = log_paths.migrate_video_folder(td)

            self.assertEqual(len(moved), 1)
            self.assertTrue((td / "logs" / "good.quality.json").exists())
            self.assertTrue((tmp / "bad.quality.json").exists())


class ConcurrentRaceTest(unittest.TestCase):
    """v1.19.0 race-safety: two encoder processes targeting the same
    folder must never lose data even when both pass the existence check
    before either move completes."""

    def test_loser_of_link_race_leaves_legacy_in_place(self) -> None:
        # Simulate the race: caller passes the dst-exists check, then
        # ANOTHER caller wins os.link and we get FileExistsError. The
        # loser must not delete the legacy (that would lose the audit
        # trail) and must return None.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tmp = td / ".tmp"
            tmp.mkdir()
            legacy = tmp / "movie.quality.json"
            legacy.write_text("LOSER", encoding="utf-8")

            def always_exists_collision(src, dst, *a, **k):
                # Pretend the dst already exists — simulates a concurrent
                # winner that linked between our exists() check and our
                # link() call.
                raise FileExistsError("concurrent migration won")

            with mock.patch.object(log_paths.os, "link",
                                   always_exists_collision):
                moved = log_paths.migrate_video_folder(td)

            self.assertEqual(moved, [],
                             "loser of the link race must not return a "
                             "fake-success path")
            self.assertTrue(legacy.exists(),
                            "legacy file must stay in place so the next "
                            "migration run can pick it up")


class MigrateQueueFolderTest(unittest.TestCase):
    def test_moves_state_and_queue_report_into_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            queue = td / "queue.json"
            queue.write_text("[]", encoding="utf-8")
            # State sidecar at the queue's level.
            (td / "queue.state.json").write_text("{}", encoding="utf-8")
            # Reports under .tmp/.
            tmp = td / ".tmp"
            tmp.mkdir()
            (tmp / "queue_report.md").write_text("# r", encoding="utf-8")
            (tmp / "queue_report.history.json").write_text(
                "[]", encoding="utf-8")

            moved = log_paths.migrate_queue_folder(queue)

            names = {p.name for p in moved}
            self.assertEqual(names, {"queue.state.json",
                                     "queue_report.md",
                                     "queue_report.history.json"})
            for name in names:
                self.assertTrue((td / "logs" / name).exists())

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            queue = td / "queue.json"
            queue.write_text("[]", encoding="utf-8")
            (td / "queue.state.json").write_text("{}", encoding="utf-8")
            log_paths.migrate_queue_folder(queue)
            self.assertEqual(log_paths.migrate_queue_folder(queue), [])


class MigrateHistoryRootTest(unittest.TestCase):
    def test_moves_encoding_history_into_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            legacy = td / "encoding_history.jsonl"
            legacy.write_text('{"x":1}\n', encoding="utf-8")

            moved = log_paths.migrate_history_root(td)

            self.assertEqual(moved, [td / "logs" / "encoding_history.jsonl"])
            self.assertFalse(legacy.exists())
            self.assertEqual(
                (td / "logs" / "encoding_history.jsonl").read_text(),
                '{"x":1}\n')

    def test_no_history_at_root_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self.assertEqual(log_paths.migrate_history_root(td), [])
            self.assertFalse((td / "logs").exists())


if __name__ == "__main__":
    unittest.main()

"""Render the queue-status summary line-block the on_queue_item_end hook
ships in `X265_QUEUE_STATUS_SUMMARY`.

Pure renderer — takes the current queue snapshot (ordered list of input
paths) plus the `{input -> row}` map of jobs already attempted, returns a
string like:

    [ 1] [OK]    short_filename.mp4
    [ 2] [OK]    another_filename.mp4
    [ 3] [FAILED] this_one_failed.mp4
    [ 4] [..]    pending_job.mp4

Markers (per the feature spec — just OK / FAILED / pending):

    [OK]      ok, skipped-done, skipped-exists  (output is on disk one way
                                                  or another)
    [FAILED]  every other terminal status (failed-*, stopped-*,
              chunk-choked, awaiting-chunk-fix, pre-flight-failed,
              stopped-by-user, skipped-not-found, AND unknown statuses —
              fail-safe so a new status surfaces loudly rather than silently
              looking healthy).
    [..]      not yet attempted (in the snapshot but not in job_reports).

Index is 1-based and right-padded to the width of the total count so a
12-job queue lines up cleanly.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.queue_status_format import (  # noqa: E402
    OK_MARKER, FAILED_MARKER, PENDING_MARKER,
    classify_marker, render_queue_summary,
)


class ClassifyMarkerTest(unittest.TestCase):
    """Single-source-of-truth status -> marker mapping. The renderer and the
    hook env builder both consume this so a future status addition only
    needs one update."""

    def test_ok_is_ok(self) -> None:
        self.assertEqual(classify_marker("ok"), OK_MARKER)

    def test_skipped_done_is_ok(self) -> None:
        # Prior-run successful encode recorded in the state sidecar -> output
        # still exists / is accounted for.
        self.assertEqual(classify_marker("skipped-done"), OK_MARKER)

    def test_skipped_exists_is_ok(self) -> None:
        # Output file already on disk before the queue started -> the user's
        # goal (have the encoded file) is satisfied.
        self.assertEqual(classify_marker("skipped-exists"), OK_MARKER)

    def test_failed_gen_is_failed(self) -> None:
        self.assertEqual(classify_marker("failed-gen"), FAILED_MARKER)

    def test_failed_exit_n_is_failed(self) -> None:
        # status_for_exit returns `failed-exit-<N>` for unknown encoder exit
        # codes — any failed-prefix string must map to FAILED.
        self.assertEqual(classify_marker("failed-exit-42"), FAILED_MARKER)

    def test_stopped_threshold_is_failed(self) -> None:
        self.assertEqual(classify_marker("stopped-threshold"), FAILED_MARKER)

    def test_stopped_threshold_crf_exhausted_is_failed(self) -> None:
        self.assertEqual(
            classify_marker("stopped-threshold-crf-exhausted"), FAILED_MARKER)

    def test_chunk_choked_is_failed(self) -> None:
        self.assertEqual(classify_marker("chunk-choked"), FAILED_MARKER)

    def test_awaiting_chunk_fix_is_failed(self) -> None:
        self.assertEqual(classify_marker("awaiting-chunk-fix"), FAILED_MARKER)

    def test_pre_flight_failed_is_failed(self) -> None:
        self.assertEqual(classify_marker("pre-flight-failed"), FAILED_MARKER)

    def test_stopped_by_user_is_failed(self) -> None:
        self.assertEqual(classify_marker("stopped-by-user"), FAILED_MARKER)

    def test_skipped_not_found_is_failed(self) -> None:
        # The user expected the queue to process this file; the source is
        # missing -> from the queue's perspective, this is a failure.
        self.assertEqual(classify_marker("skipped-not-found"), FAILED_MARKER)

    def test_unknown_status_defaults_to_failed(self) -> None:
        # Fail-safe: a new status added upstream surfaces as [FAILED] in
        # notifications rather than silently looking healthy.
        self.assertEqual(classify_marker("brand-new-status"), FAILED_MARKER)

    def test_none_is_pending(self) -> None:
        # None means the job hasn't been attempted yet.
        self.assertEqual(classify_marker(None), PENDING_MARKER)


class RenderQueueSummaryTest(unittest.TestCase):
    def test_mixed_queue_emits_one_line_per_job_in_snapshot_order(self) -> None:
        snapshot = ["/q/a.mp4", "/q/b.mp4", "/q/c.mp4", "/q/d.mp4"]
        reports = {
            "/q/a.mp4": {"status": "ok"},
            "/q/b.mp4": {"status": "ok"},
            "/q/c.mp4": {"status": "failed-gen"},
            # d is pending — not in reports.
        }
        text = render_queue_summary(snapshot, reports)
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("[OK]", lines[0])
        self.assertIn("a.mp4", lines[0])
        self.assertIn("[OK]", lines[1])
        self.assertIn("b.mp4", lines[1])
        self.assertIn("[FAILED]", lines[2])
        self.assertIn("c.mp4", lines[2])
        self.assertIn("[..]", lines[3])
        self.assertIn("d.mp4", lines[3])

    def test_index_is_one_based_and_padded_to_total(self) -> None:
        snapshot = [f"/q/file_{i:02d}.mp4" for i in range(1, 13)]
        reports = {snapshot[0]: {"status": "ok"}}
        text = render_queue_summary(snapshot, reports)
        lines = text.splitlines()
        # 12 jobs -> width 2 -> first line starts with "[ 1]", last with "[12]".
        self.assertTrue(lines[0].startswith("[ 1]"),
                        f"first line: {lines[0]!r}")
        self.assertTrue(lines[-1].startswith("[12]"),
                        f"last line: {lines[-1]!r}")

    def test_only_basename_shown_not_full_path(self) -> None:
        # Full paths in queue.json are typically huge — the notification
        # body wants the basename for readability. The full path is
        # available via X265_SOURCE for the just-finished job; the summary
        # is a quick-glance overview.
        snapshot = ["/very/deep/absolute/path/to/the_movie.mp4"]
        text = render_queue_summary(snapshot, {snapshot[0]: {"status": "ok"}})
        self.assertIn("the_movie.mp4", text)
        self.assertNotIn("/very/deep/", text)

    def test_empty_snapshot_returns_empty_string(self) -> None:
        # Defensive: an empty snapshot must not crash the hook builder.
        self.assertEqual(render_queue_summary([], {}), "")

    def test_report_key_normalized_to_resolved_path(self) -> None:
        # The snapshot carries absolute paths (Path.resolve()'d in
        # run_queue); job_reports' `input` field is also resolved. Lookup
        # uses the snapshot key verbatim — no surprise normalization
        # divergence between them.
        snapshot = ["/q/a.mp4"]
        reports = {"/q/a.mp4": {"status": "ok"}}
        self.assertIn("[OK]", render_queue_summary(snapshot, reports))

    def test_unknown_path_in_reports_is_ignored(self) -> None:
        # If job_reports carries an entry not in the current snapshot
        # (queue.json edited mid-run, row dropped), the summary follows
        # the snapshot — the dropped row doesn't get its own line.
        snapshot = ["/q/a.mp4"]
        reports = {
            "/q/a.mp4": {"status": "ok"},
            "/q/dropped.mp4": {"status": "ok"},
        }
        text = render_queue_summary(snapshot, reports)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn("dropped", text)


if __name__ == "__main__":
    unittest.main()

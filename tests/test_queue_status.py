"""`run_queue.py --status` read-only inspector.

Single source of truth on "what is the queue doing right now?": every job
classified DONE / PROCESSING / QUEUED, with sizes / CRF / wall time / savings.
Reconciles four data sources (queue.json + encoding_history.jsonl + workdir
state + on-disk outputs) so the user doesn't have to.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.queue_state import QueueState  # noqa: E402
from queue_modules.status import (  # noqa: E402
    StatusRow,
    classify,
    history_by_input,
    render_json,
    render_table,
)


def _job(input_str: str, **extra):
    return {"input": input_str, **extra}


class ClassifyDoneTest(unittest.TestCase):
    """A job is DONE when its derived output exists on disk (the existing
    skip-existing predicate) OR when the queue state sidecar records the
    completion (handles done_dir-moved sources whose output is no longer
    at the derived path)."""

    def test_output_on_disk_is_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            out = td / "movie.mkv"
            out.write_bytes(b"out" * 100)
            row = classify(_job(str(inp)), queue_dir=td, history={},
                           state=QueueState())
            self.assertEqual(row.status, "DONE")
            self.assertEqual(row.source_bytes, len(b"in"))
            self.assertEqual(row.output_bytes, 300)

    def test_state_record_is_done_even_when_input_missing(self) -> None:
        # done_dir-moved scenario: input no longer at the queue-listed path.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"   # never created
            state = QueueState()
            state.add_completed(
                input_original=inp,
                output_original=td / "movie.mkv",
                moved_to_dir=td / "done",
                input_final=td / "done" / "movie.mp4",
                output_final=td / "done" / "movie.mkv",
                bytes_in=1234, bytes_out=500, crf_final=23,
                wall_seconds=11.0)
            row = classify(_job(str(inp)), queue_dir=td, history={},
                           state=state)
            self.assertEqual(row.status, "DONE")
            self.assertEqual(row.crf_chain, "23")
            self.assertEqual(row.source_bytes, 1234)
            self.assertEqual(row.output_bytes, 500)


class ClassifyProcessingTest(unittest.TestCase):
    """A workdir with at least one `enc_src_*.mkv` plus an in_progress
    history record for the input → PROCESSING. Without the in_progress
    record we'd label every half-finished crashed-run workdir as live."""

    def test_workdir_with_chunks_and_in_progress_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            # Workdir with two encoded chunks: file IS being processed.
            workdir = td / ".tmp" / ".compress_movie"
            workdir.mkdir(parents=True)
            (workdir / "enc_src_0001.mkv").write_bytes(b"x")
            (workdir / "enc_src_0002.mkv").write_bytes(b"x")
            history = {str(inp.resolve()): {"status": "in_progress",
                                            "settings": {"crf": 21}}}
            row = classify(_job(str(inp)), queue_dir=td, history=history,
                           state=QueueState())
            self.assertEqual(row.status, "PROCESSING")
            self.assertIn("chunks done", row.notes)

    def test_workdir_without_in_progress_history_is_queued_with_stale_note(
            self) -> None:
        # Workdir exists but the history says the last run failed/aborted.
        # That's a stale workdir, not a live encode — classify QUEUED so
        # the user doesn't think the queue is mid-flight.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            workdir = td / ".tmp" / ".compress_movie"
            workdir.mkdir(parents=True)
            (workdir / "enc_src_0001.mkv").write_bytes(b"x")
            history = {str(inp.resolve()): {"status": "stopped-threshold"}}
            row = classify(_job(str(inp)), queue_dir=td, history=history,
                           state=QueueState())
            self.assertEqual(row.status, "QUEUED")
            self.assertIn("stale", row.notes.lower())


class ClassifyQueuedTest(unittest.TestCase):
    def test_no_workdir_no_history_no_output_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            row = classify(_job(str(inp), crf=21), queue_dir=td, history={},
                           state=QueueState())
            self.assertEqual(row.status, "QUEUED")
            self.assertEqual(row.source_bytes, len(b"in"))
            self.assertEqual(row.crf_chain, "21 (start)")

    def test_missing_input_classified_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "ghost.mp4"  # never created
            row = classify(_job(str(inp)), queue_dir=td, history={},
                           state=QueueState())
            self.assertEqual(row.status, "MISSING INPUT")


class HistoryByInputTest(unittest.TestCase):
    def test_takes_most_recent_per_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "encoding_history.jsonl"
            # Two records for the same input, newest last (append-only log).
            recs = [
                {"input": {"path": "/v/a.mp4"},
                 "status": "stopped-threshold",
                 "settings": {"crf": 21}},
                {"input": {"path": "/v/a.mp4"},
                 "status": "ok",
                 "settings": {"crf": 22}},
            ]
            path.write_text("\n".join(json.dumps(r) for r in recs) + "\n",
                            encoding="utf-8")
            by = history_by_input(path)
            self.assertEqual(by[str(Path("/v/a.mp4").resolve())]["status"],
                             "ok")

    def test_missing_file_yields_empty_dict(self) -> None:
        by = history_by_input(Path("/nonexistent"))
        self.assertEqual(by, {})


class RenderTableTest(unittest.TestCase):
    def test_table_has_header_and_one_row_per_job(self) -> None:
        rows = [
            StatusRow(index=1, name="a.mp4", status="DONE",
                      crf_chain="23", source_bytes=1000, output_bytes=600,
                      saved_pct=40.0, wall_seconds=120.0,
                      notes="first try"),
            StatusRow(index=2, name="b.mp4", status="QUEUED",
                      crf_chain="21 (start)",
                      source_bytes=2000, output_bytes=None,
                      saved_pct=None, wall_seconds=None,
                      notes="next after a"),
        ]
        text = render_table(rows, totals=True)
        self.assertIn("DONE", text)
        self.assertIn("QUEUED", text)
        self.assertIn("a.mp4", text)
        self.assertIn("b.mp4", text)
        # Totals line surfaces the aggregate.
        self.assertIn("Totals", text)


class RenderJsonTest(unittest.TestCase):
    def test_json_round_trips_to_a_list_of_dicts(self) -> None:
        rows = [StatusRow(index=1, name="a.mp4", status="DONE",
                          crf_chain="23", source_bytes=1, output_bytes=1,
                          saved_pct=0.0, wall_seconds=1.0, notes="")]
        data = json.loads(render_json(rows))
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["name"], "a.mp4")
        self.assertEqual(data[0]["status"], "DONE")


class LivenessAndCrfExhaustedTest(unittest.TestCase):
    """Reviewer-flagged regressions:
      * PROCESSING requires both `in_progress` history AND a recently-
        touched workdir (mtime-based liveness, no PID dep).
      * stopped-threshold-crf-exhausted surfaces as Notes='crf-exhausted
        (last tried CRF N)' instead of generic stale-workdir blob.
      * input-missing-but-output-on-disk classifies DONE, not MISSING INPUT.
    """

    def test_in_progress_with_stale_workdir_is_queued_with_stale_marker(
            self) -> None:
        import os
        from queue_modules import status as st
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            workdir = td / ".tmp" / ".compress_movie"
            workdir.mkdir(parents=True)
            chunk = workdir / "enc_src_0001.mkv"
            chunk.write_bytes(b"x")
            # Force a stale mtime (1 hour ago) on both the chunk and the
            # workdir so neither passes the liveness window.
            stale_t = time.time() - (st.LIVENESS_WINDOW_SEC + 3600)
            for p in (workdir, chunk):
                os.utime(p, (stale_t, stale_t))
            history = {str(inp.resolve()): {"status": "in_progress"}}
            row = classify(_job(str(inp)), queue_dir=td, history=history,
                           state=QueueState())
            self.assertEqual(row.status, "QUEUED")
            self.assertIn("stale in_progress", row.notes)

    def test_crf_exhausted_status_surfaces_with_last_crf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            history = {str(inp.resolve()): {
                "status": "stopped-threshold-crf-exhausted",
                "settings": {"crf": 28},
            }}
            row = classify(_job(str(inp), crf=21), queue_dir=td,
                           history=history, state=QueueState())
            self.assertEqual(row.status, "QUEUED")
            self.assertEqual(row.notes, "crf-exhausted (last tried CRF 28)")

    def test_output_on_disk_with_input_missing_is_done(self) -> None:
        # Reviewer #1 CRITICAL: a user who deleted the source but kept
        # the output should see DONE, not MISSING INPUT.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # NO inp file created. Only the output .mkv.
            inp = td / "movie.mp4"
            (td / "movie.mkv").write_bytes(b"out" * 100)
            row = classify(_job(str(inp)), queue_dir=td, history={},
                           state=QueueState())
            self.assertEqual(row.status, "DONE")
            self.assertIsNone(row.source_bytes)
            self.assertEqual(row.output_bytes, 300)


class ProcessingWallClockTest(unittest.TestCase):
    """PROCESSING rows compute live wall-clock seconds from the history
    record's `timestamp_start_utc` so the user sees how long the encode
    has been running."""

    def test_wall_seconds_from_timestamp_start_utc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inp = td / "movie.mp4"
            inp.write_bytes(b"in")
            workdir = td / ".tmp" / ".compress_movie"
            workdir.mkdir(parents=True)
            (workdir / "enc_src_0001.mkv").write_bytes(b"x")
            # Start time = 2 minutes ago — fresh enough to be live.
            from datetime import datetime, timezone, timedelta
            started = (datetime.now(timezone.utc)
                       - timedelta(seconds=120))
            history = {str(inp.resolve()): {
                "status": "in_progress",
                "timestamp_start_utc":
                    started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "settings": {"crf": 21},
                "chunks": [{}, {}, {}, {}, {}],  # 5 total
            }}
            row = classify(_job(str(inp)), queue_dir=td, history=history,
                           state=QueueState())
            self.assertEqual(row.status, "PROCESSING")
            self.assertIsNotNone(row.wall_seconds)
            # Approx 120s ± clock skew.
            self.assertGreater(row.wall_seconds, 100)
            self.assertLess(row.wall_seconds, 200)
            self.assertIn("/5 chunks", row.notes)


class EmptyQueueTest(unittest.TestCase):
    def test_render_table_on_empty_list_is_a_friendly_message(self) -> None:
        text = render_table([])
        self.assertIn("empty", text.lower())


class CliStatusEndToEndTest(unittest.TestCase):
    """`--status` exits 0 without encoding, printing a table of all queue
    jobs. Drives `run_queue.main()` directly with a small queue JSON."""

    def test_status_prints_table_and_exits_zero(self) -> None:
        import io
        import os
        import contextlib
        import run_queue
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # Two inputs: one with output on disk (DONE), one without (QUEUED).
            inp_a = td / "a.mp4"; inp_a.write_bytes(b"a" * 100)
            inp_b = td / "b.mp4"; inp_b.write_bytes(b"b" * 200)
            (td / "a.mkv").write_bytes(b"x" * 60)  # a is DONE
            queue = td / "queue.json"
            queue.write_text(json.dumps({
                "defaults": {},
                "jobs": [{"input": "a.mp4"}, {"input": "b.mp4"}],
            }), encoding="utf-8")
            # Redirect history to a temp path so no real encoding_history.jsonl
            # is consulted.
            saved = os.environ.get("CLAUDE_ENCODING_HISTORY_PATH")
            os.environ["CLAUDE_ENCODING_HISTORY_PATH"] = str(
                td / "history.jsonl")
            saved_argv = sys.argv
            sys.argv = ["run_queue.py", str(queue), "--status"]
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = run_queue.main()
            finally:
                sys.argv = saved_argv
                if saved is None:
                    os.environ.pop("CLAUDE_ENCODING_HISTORY_PATH", None)
                else:
                    os.environ["CLAUDE_ENCODING_HISTORY_PATH"] = saved
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("a.mp4", text)
            self.assertIn("b.mp4", text)
            self.assertIn("DONE", text)
            self.assertIn("QUEUED", text)

    def test_status_json_mode_outputs_valid_json(self) -> None:
        import io
        import os
        import contextlib
        import run_queue
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "a.mp4").write_bytes(b"a")
            queue = td / "queue.json"
            queue.write_text(json.dumps({"jobs": [{"input": "a.mp4"}]}),
                              encoding="utf-8")
            saved = os.environ.get("CLAUDE_ENCODING_HISTORY_PATH")
            os.environ["CLAUDE_ENCODING_HISTORY_PATH"] = str(
                td / "history.jsonl")
            saved_argv = sys.argv
            sys.argv = ["run_queue.py", str(queue), "--status",
                        "--status-json"]
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = run_queue.main()
            finally:
                sys.argv = saved_argv
                if saved is None:
                    os.environ.pop("CLAUDE_ENCODING_HISTORY_PATH", None)
                else:
                    os.environ["CLAUDE_ENCODING_HISTORY_PATH"] = saved
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data[0]["name"], "a.mp4")


if __name__ == "__main__":
    unittest.main()

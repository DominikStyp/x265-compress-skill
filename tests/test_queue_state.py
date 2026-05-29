"""Persistent queue state sidecar (`<queue_stem>.state.json`).

Needed because `done_dir` moves the source AWAY from the path in
`queue.json`. On the next run, the input-missing skip would emit
`skipped-not-found` (which bumps the queue's aggregate exit code), making
clean completions look like errors. The state file records `input_original
→ moved_to_dir` per ok job, so the skip logic can recognize "this job is
already done, move along".

Schema is versioned + atomic-write so a mid-write kill never leaves a torn
file. Foreign inputs (state entries for jobs no longer in queue.json) are
PRESERVED — the user may re-add them.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.queue_state import (  # noqa: E402
    QueueState,
    delete_queue_state,
    load_queue_state,
    state_path_for,
)


class StatePathForTest(unittest.TestCase):
    def test_sidecar_lives_next_to_queue_with_stem(self) -> None:
        self.assertEqual(state_path_for(Path("/x/queue.json")),
                         Path("/x/queue.state.json"))


class LoadStateTest(unittest.TestCase):
    def test_missing_file_yields_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = load_queue_state(Path(td) / "queue.json")
            self.assertEqual(state.completed, {})
            self.assertEqual(state.schema_version, 1)

    def test_load_existing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sidecar = Path(td) / "queue.state.json"
            sidecar.write_text(json.dumps({
                "schema_version": 1,
                "queue_file": "queue.json",
                "completed": [
                    {"input_original": "/v/a.mp4",
                     "output_original": "/v/a.mkv",
                     "moved_to_dir": "/v/done",
                     "input_final": "/v/done/a.mp4",
                     "output_final": "/v/done/a.mkv",
                     "crf_final": 23, "bytes_in": 100, "bytes_out": 50,
                     "wall_seconds": 12.0,
                     "completed_utc": "2026-05-29T00:00:00Z"},
                ],
            }), encoding="utf-8")
            state = load_queue_state(Path(td) / "queue.json")
            self.assertTrue(state.is_completed(Path("/v/a.mp4")))
            rec = state.get(Path("/v/a.mp4"))
            self.assertEqual(rec["moved_to_dir"], "/v/done")

    def test_corrupt_json_yields_empty_state(self) -> None:
        # Mirrors hook_config's degrade-to-empty discipline: a corrupt
        # state sidecar should never kill a queue run.
        with tempfile.TemporaryDirectory() as td:
            sidecar = Path(td) / "queue.state.json"
            sidecar.write_text("{not json", encoding="utf-8")
            state = load_queue_state(Path(td) / "queue.json")
            self.assertEqual(state.completed, {})

    def test_unknown_schema_version_yields_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sidecar = Path(td) / "queue.state.json"
            sidecar.write_text(json.dumps({"schema_version": 99,
                                            "completed": []}),
                                encoding="utf-8")
            state = load_queue_state(Path(td) / "queue.json")
            self.assertEqual(state.completed, {})


class AddAndSaveTest(unittest.TestCase):
    def test_add_then_save_then_reload_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "queue.json"
            state = QueueState()
            state.add_completed(
                input_original=Path("/v/a.mp4"),
                output_original=Path("/v/a.mkv"),
                moved_to_dir=Path("/v/done"),
                input_final=Path("/v/done/a.mp4"),
                output_final=Path("/v/done/a.mkv"),
                crf_final=23, bytes_in=100, bytes_out=50,
                wall_seconds=12.0,
                completed_utc="2026-05-29T00:00:00Z",
            )
            state.save_atomically(queue)
            sidecar = state_path_for(queue)
            self.assertTrue(sidecar.exists())
            # Re-load and check.
            again = load_queue_state(queue)
            self.assertTrue(again.is_completed(Path("/v/a.mp4")))

    def test_save_is_atomic_no_temp_file_left(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "queue.json"
            state = QueueState()
            state.add_completed(input_original=Path("/v/x.mp4"))
            state.save_atomically(queue)
            leftovers = [p.name for p in Path(td).iterdir()
                         if p.name != "queue.state.json"]
            self.assertEqual(leftovers, [])

    def test_add_without_done_dir_records_in_place_completion(self) -> None:
        # When no done_dir was set, input/output stay where they were —
        # state record reflects "completed in place".
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "queue.json"
            state = QueueState()
            state.add_completed(
                input_original=Path("/v/a.mp4"),
                output_original=Path("/v/a.mkv"),
                # no moved_to_dir / input_final / output_final
            )
            state.save_atomically(queue)
            again = load_queue_state(queue)
            rec = again.get(Path("/v/a.mp4"))
            self.assertNotIn("moved_to_dir", rec)


class ResetTest(unittest.TestCase):
    def test_delete_queue_state_removes_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "queue.json"
            state = QueueState()
            state.add_completed(input_original=Path("/v/a.mp4"))
            state.save_atomically(queue)
            self.assertTrue(state_path_for(queue).exists())
            delete_queue_state(queue)
            self.assertFalse(state_path_for(queue).exists())

    def test_delete_missing_sidecar_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # No state file exists yet — delete must be silent.
            delete_queue_state(Path(td) / "queue.json")


class IsCompletedTest(unittest.TestCase):
    def test_resolves_path_for_comparison(self) -> None:
        # Queue.json input is `"a.mp4"`; the runner resolves it before
        # asking is_completed — we must accept either spelling so a future
        # caller can pass an unresolved path safely.
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "queue.json"
            real = Path(td) / "a.mp4"
            real.write_bytes(b"x")
            state = QueueState()
            state.add_completed(input_original=real.resolve())
            state.save_atomically(queue)
            again = load_queue_state(queue)
            self.assertTrue(again.is_completed(real))
            self.assertTrue(again.is_completed(real.resolve()))


class QueueArgvBuildsDoneDir(unittest.TestCase):
    """job_schema's build_compress_argv must emit `--done-dir <path>` when
    the merged job carries one."""

    def test_emits_done_dir_when_present(self) -> None:
        from queue_modules.job_schema import build_compress_argv
        argv = build_compress_argv({"input": "/v/a.mp4", "crf": 21,
                                    "done_dir": "/v/done", "resumable": True})
        self.assertIn("--done-dir", argv)
        self.assertEqual(argv[argv.index("--done-dir") + 1], "/v/done")

    def test_omits_when_absent(self) -> None:
        from queue_modules.job_schema import build_compress_argv
        argv = build_compress_argv({"input": "/v/a.mp4", "resumable": True})
        self.assertNotIn("--done-dir", argv)

    def test_done_dir_in_valid_keys(self) -> None:
        from queue_modules.job_schema import VALID_KEYS
        self.assertIn("done_dir", VALID_KEYS)


class VerifyMoveOutcomeTest(unittest.TestCase):
    """`_record_completion` must verify the move actually happened before
    persisting `moved_to_dir` in the state sidecar — the encoder still
    exits 0 even when a move was refused (because the encode itself
    succeeded), so disk truth is the only honest source."""

    def test_records_move_only_when_files_arrived(self) -> None:
        import run_queue
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mp4"
            out_orig = td / "movie.mkv"  # derive_output_path's result
            done = td / "done"
            done.mkdir()
            # Simulate successful move: files now at done/.
            (done / "movie.mp4").write_bytes(b"src")
            (done / "movie.mkv").write_bytes(b"out")
            moved, in_f, out_f = run_queue._verify_move_outcome(
                str(done), src, out_orig)
            self.assertEqual(moved, done)
            self.assertEqual(in_f, done / "movie.mp4")
            self.assertEqual(out_f, done / "movie.mkv")

    def test_records_no_move_when_files_still_at_origin(self) -> None:
        # Move refused / OSError; source+output stayed at original location.
        import run_queue
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mp4"
            out_orig = td / "movie.mkv"
            done = td / "done"
            done.mkdir()
            # done is empty — files never arrived.
            moved, in_f, out_f = run_queue._verify_move_outcome(
                str(done), src, out_orig)
            self.assertIsNone(moved)
            self.assertIsNone(in_f)
            self.assertIsNone(out_f)

    def test_partial_move_treated_as_no_move(self) -> None:
        # Step-2 failure: output moved, source still at origin. Recording
        # `moved_to_dir = done` would lie. Record as no-move so the next
        # run finds the source and re-encodes (or refuses by destination
        # check, which the user can resolve).
        import run_queue
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "movie.mp4"
            out_orig = td / "movie.mkv"
            done = td / "done"
            done.mkdir()
            (done / "movie.mkv").write_bytes(b"out")  # only output arrived
            moved, in_f, out_f = run_queue._verify_move_outcome(
                str(done), src, out_orig)
            self.assertIsNone(moved)

    def test_no_done_dir_configured_means_no_move(self) -> None:
        import run_queue
        moved, in_f, out_f = run_queue._verify_move_outcome(
            None, Path("/v/a.mp4"), Path("/v/a.mkv"))
        self.assertIsNone(moved)


class JobRunnerResolvesMovedOutputBytesTest(unittest.TestCase):
    """`build_job_row` must find the output's size at the moved location
    when done_dir relocated it, not silently emit `output_bytes: None`."""

    def test_output_bytes_from_moved_location(self) -> None:
        from queue_modules import job_runner
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            done = td / "done"
            done.mkdir()
            # Out file is at done_dir, not at the original path.
            (done / "movie.mkv").write_bytes(b"X" * 1234)
            out_path = td / "movie.mkv"   # original path — does NOT exist
            merged = {"done_dir": str(done)}
            self.assertEqual(
                job_runner._resolve_output_bytes(out_path, merged), 1234)

    def test_output_bytes_falls_back_to_original_when_no_move(self) -> None:
        from queue_modules import job_runner
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out_path = td / "movie.mkv"
            out_path.write_bytes(b"Y" * 500)
            self.assertEqual(
                job_runner._resolve_output_bytes(out_path, {}), 500)

    def test_output_bytes_none_when_neither_location_has_file(self) -> None:
        from queue_modules import job_runner
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out_path = td / "movie.mkv"
            merged = {"done_dir": str(td / "done_empty")}
            (td / "done_empty").mkdir()
            self.assertIsNone(
                job_runner._resolve_output_bytes(out_path, merged))


class SkipDoneIntegrationTest(unittest.TestCase):
    """`run_queue.py`'s skip path: a job recorded in the state sidecar is
    skipped silently with status `skipped-done`, not `skipped-not-found`."""

    def test_skip_done_takes_precedence_over_input_missing(self) -> None:
        import run_queue
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "queue.json"
            # State recording a completion; the actual input file does NOT
            # exist (it was moved to done_dir last run).
            state = QueueState()
            real = Path(td) / "moved-away.mp4"
            state.add_completed(input_original=real,
                                moved_to_dir=Path(td) / "done")
            state.save_atomically(queue)
            loaded = load_queue_state(queue)
            row = run_queue._skip_if_missing_or_existing(
                {"input": str(real)},
                i=1, n=1, no_skip_existing=False, state=loaded,
            )
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "skipped-done")

    def test_skip_done_is_a_clean_status_exit_code_zero(self) -> None:
        # The aggregate exit code must treat skipped-done as clean, not
        # attention. Otherwise a done_dir-moved queue exits 2 on every re-run.
        import run_queue
        self.assertIn("skipped-done", run_queue._CLEAN_STATUSES)


if __name__ == "__main__":
    unittest.main()

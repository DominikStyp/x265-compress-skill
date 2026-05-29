"""Queue-counter env overlay. run_queue.py overlays these onto os.environ
right before spawning each per-job encoder; the X265_QUEUE_* values
inherit through cmd → bat → python → on_file_complete hook subprocess.

The counters are PER-RUN (reset every `run_queue.py` invocation) and
INCLUSIVE of the just-finished job (so a "3 of 8 done" notification at
fire time is exact). compute_queue_counters pre-increments FINISHED for
the about-to-start job — the encoder runs to completion before the hook
fires, so by then it really is +1.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.queue_counters import (  # noqa: E402
    compute_queue_counters,
    hook_keys,
    overlay_env,
)


def _row(status, in_b=0, out_b=0):
    return {"status": status, "input_bytes": in_b, "output_bytes": out_b}


class ComputeQueueCountersTest(unittest.TestCase):
    def test_empty_history_first_job_about_to_start(self) -> None:
        env = compute_queue_counters([], total_jobs=8,
                                     queue_wall_seconds=0.0,
                                     upcoming_index=1)
        self.assertEqual(env["X265_QUEUE_INDEX"], "1")
        self.assertEqual(env["X265_QUEUE_TOTAL"], "8")
        # FINISHED is the count of past oks — exclusive of the about-to-run
        # job, so JobEndHook reports honest numbers on failure paths.
        # FileCompleteHook applies the +1 itself (success-only).
        self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "0")
        self.assertEqual(env["X265_QUEUE_ITEMS_REMAINING"], "7")
        self.assertEqual(env["X265_QUEUE_ITEMS_FAILED"], "0")
        self.assertEqual(env["X265_QUEUE_ITEMS_STOPPED"], "0")
        self.assertEqual(env["X265_QUEUE_ITEMS_SKIPPED"], "0")
        self.assertEqual(env["X265_QUEUE_BYTES_IN_SO_FAR"], "0")
        self.assertEqual(env["X265_QUEUE_BYTES_OUT_SO_FAR"], "0")
        self.assertEqual(env["X265_QUEUE_PCT_SAVED_SO_FAR"], "0.00")
        self.assertEqual(env["X265_QUEUE_WALL_SECONDS"], "0.00")

    def test_third_job_after_two_oks_and_one_failure(self) -> None:
        history = [
            _row("ok", 1_000_000_000, 600_000_000),
            _row("ok", 2_000_000_000, 1_400_000_000),
            _row("failed-exit-1"),
        ]
        env = compute_queue_counters(history, total_jobs=8,
                                     queue_wall_seconds=123.45,
                                     upcoming_index=4)
        # 2 ok already; exclusive of about-to-start job → 2.
        self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "2")
        self.assertEqual(env["X265_QUEUE_ITEMS_REMAINING"], "4")
        self.assertEqual(env["X265_QUEUE_ITEMS_FAILED"], "1")
        self.assertEqual(env["X265_QUEUE_BYTES_IN_SO_FAR"],
                         str(3_000_000_000))
        self.assertEqual(env["X265_QUEUE_BYTES_OUT_SO_FAR"],
                         str(2_000_000_000))
        # (3e9 - 2e9) / 3e9 * 100 ≈ 33.33
        self.assertEqual(env["X265_QUEUE_PCT_SAVED_SO_FAR"], "33.33")
        self.assertEqual(env["X265_QUEUE_WALL_SECONDS"], "123.45")

    def test_skipped_and_stopped_counted_separately(self) -> None:
        history = [
            _row("skipped-exists", 500, 400),
            _row("skipped-not-found"),
            _row("stopped-threshold", 800),
            _row("stopped-by-user"),
            _row("ok", 1000, 700),
        ]
        env = compute_queue_counters(history, total_jobs=10,
                                     queue_wall_seconds=10.0,
                                     upcoming_index=6)
        # 1 ok already (exclusive of about-to-start).
        self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "1")
        self.assertEqual(env["X265_QUEUE_ITEMS_FAILED"], "0")
        self.assertEqual(env["X265_QUEUE_ITEMS_STOPPED"], "2")
        self.assertEqual(env["X265_QUEUE_ITEMS_SKIPPED"], "2")

    def test_pct_saved_clamps_to_zero_when_no_bytes_finished(self) -> None:
        env = compute_queue_counters([_row("failed-exit-1")],
                                     total_jobs=3,
                                     queue_wall_seconds=1.0,
                                     upcoming_index=2)
        self.assertEqual(env["X265_QUEUE_PCT_SAVED_SO_FAR"], "0.00")
        # No div-by-zero / NaN.
        self.assertNotIn("inf", env["X265_QUEUE_PCT_SAVED_SO_FAR"].lower())

    def test_skipped_done_counts_toward_finished_and_bytes(self) -> None:
        # Reviewer-flagged bug: after v1.11.0's state sidecar work, a
        # re-run records previously-completed jobs as `skipped-done` with
        # the original bytes_in/bytes_out (faithful prior-run metadata).
        # Excluding them from the aggregate made X265_QUEUE_BYTES_*_SO_FAR
        # / PCT_SAVED_SO_FAR reset to 0 on every relaunch — the
        # file_complete hook's "cumulative savings" contract was broken
        # across sessions.
        #
        # Distinct from `skipped-exists` (which means "output happened to
        # be on disk, provenance unknown" — kept in the SKIPPED category
        # because we shouldn't claim those byte counts as our work).
        history = [
            {"status": "skipped-done", "input_bytes": 1_000_000_000,
             "output_bytes": 600_000_000},
            {"status": "skipped-done", "input_bytes": 2_000_000_000,
             "output_bytes": 1_400_000_000},
            # An ok in this run on top — should still aggregate.
            {"status": "ok", "input_bytes": 500_000_000,
             "output_bytes": 300_000_000},
        ]
        env = compute_queue_counters(history, total_jobs=5,
                                     queue_wall_seconds=0.0,
                                     upcoming_index=4)
        # All three count as finished (two prior, one current).
        self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "3")
        self.assertEqual(env["X265_QUEUE_BYTES_IN_SO_FAR"],
                         str(3_500_000_000))
        self.assertEqual(env["X265_QUEUE_BYTES_OUT_SO_FAR"],
                         str(2_300_000_000))
        self.assertEqual(env["X265_QUEUE_ITEMS_SKIPPED"], "0")

    def test_skipped_exists_still_counts_as_skipped_not_finished(self
                                                                  ) -> None:
        # Boundary case: deliberately NOT moved into FINISHED. The output
        # was on disk for reasons we can't attribute to a prior queue run,
        # so we don't claim its bytes as savings.
        history = [_row("skipped-exists", 500_000_000, 300_000_000)]
        env = compute_queue_counters(history, total_jobs=2,
                                     queue_wall_seconds=0.0,
                                     upcoming_index=2)
        self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "0")
        self.assertEqual(env["X265_QUEUE_ITEMS_SKIPPED"], "1")
        self.assertEqual(env["X265_QUEUE_BYTES_IN_SO_FAR"], "0")

    def test_safe_int_handles_none_output_bytes(self) -> None:
        # build_job_row returns output_bytes=None for placeholder rows.
        # That must not blow up the aggregate sum.
        history = [_row("ok", 100, 50),
                   {"status": "ok", "input_bytes": None,
                    "output_bytes": None}]
        env = compute_queue_counters(history, total_jobs=3,
                                     queue_wall_seconds=0.0,
                                     upcoming_index=3)
        self.assertEqual(env["X265_QUEUE_BYTES_IN_SO_FAR"], "100")
        self.assertEqual(env["X265_QUEUE_BYTES_OUT_SO_FAR"], "50")

    def test_remaining_zero_when_last_job(self) -> None:
        env = compute_queue_counters(
            [_row("ok", 1, 1)] * 7,
            total_jobs=8, queue_wall_seconds=0.0, upcoming_index=8)
        self.assertEqual(env["X265_QUEUE_ITEMS_REMAINING"], "0")


class OverlayEnvHermeticTest(unittest.TestCase):
    """The overlay MUST restore os.environ on exit — a leaked counter would
    poison a later non-queue process running in the same shell."""

    def test_keys_set_inside_unset_outside(self) -> None:
        for k in hook_keys():
            os.environ.pop(k, None)
        with overlay_env({"X265_QUEUE_INDEX": "3"}):
            self.assertEqual(os.environ["X265_QUEUE_INDEX"], "3")
        self.assertNotIn("X265_QUEUE_INDEX", os.environ)

    def test_preexisting_value_restored(self) -> None:
        os.environ["X265_QUEUE_INDEX"] = "PRESET"
        try:
            with overlay_env({"X265_QUEUE_INDEX": "OVERLAY"}):
                self.assertEqual(os.environ["X265_QUEUE_INDEX"], "OVERLAY")
            self.assertEqual(os.environ["X265_QUEUE_INDEX"], "PRESET")
        finally:
            os.environ.pop("X265_QUEUE_INDEX", None)

    def test_restoration_runs_even_on_exception(self) -> None:
        os.environ.pop("X265_QUEUE_INDEX", None)
        with self.assertRaises(RuntimeError):
            with overlay_env({"X265_QUEUE_INDEX": "X"}):
                raise RuntimeError("boom")
        self.assertNotIn("X265_QUEUE_INDEX", os.environ)


if __name__ == "__main__":
    unittest.main()

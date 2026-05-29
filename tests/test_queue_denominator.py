"""run_queue's [i/n] denominator + queue counter totals must reflect the
WHOLE queue, not just the prefix walked so far.

Pre-fix bug (caught by the code-reviewer audit): `_pick_next_job` walked
the snapshot and returned at the first unattempted job — `seen_inputs`
only grew up to that point. On job 1 of a 5-job queue, `len(seen_inputs)`
was 1, so the `[i/n]` banner read `[1/1]` and `compute_queue_counters`
got `total_jobs=1`, `remaining=0`. Every queue under-reported its size
during the early phase.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_queue  # noqa: E402


class PickNextJobGrowsSeenInputsToFullSnapshotTest(unittest.TestCase):
    """`_pick_next_job` MUST observe every job in the snapshot before
    returning the first unattempted one, so the report-table denominator
    (which everything else keys off) is the queue's true size from the
    very first pick."""

    def test_first_pick_populates_full_seen_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # Five distinct inputs — none attempted yet.
            inputs = [td / f"v{i}.mp4" for i in range(5)]
            for p in inputs:
                p.write_bytes(b"x")
            jobs = [{"input": str(p)} for p in inputs]
            seen: set[str] = set()
            attempted: set[str] = set()
            picked = run_queue._pick_next_job(jobs, seen, attempted)
            self.assertIsNotNone(picked)
            self.assertEqual(picked["input"], str(inputs[0]))
            # Bug repro guard: full queue MUST be in seen, not just job 1.
            self.assertEqual(len(seen), 5)

    def test_seen_grows_when_new_jobs_appended_between_reloads(self) -> None:
        # Live-reload scenario: user appends two more rows mid-run. After
        # the reload, _pick_next_job should add ALL of the new inputs to
        # seen, even though the next-to-attempt may be early in the list.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            inputs = [td / f"v{i}.mp4" for i in range(3)]
            for p in inputs:
                p.write_bytes(b"x")
            seen = {str(inputs[0].resolve()),
                    str(inputs[1].resolve())}  # observed in earlier reload
            attempted = {str(inputs[0].resolve())}
            # Now the queue grows by two more rows.
            new_inputs = [td / f"v{i}.mp4" for i in range(3, 5)]
            for p in new_inputs:
                p.write_bytes(b"x")
            jobs = [{"input": str(p)} for p in inputs + new_inputs]
            picked = run_queue._pick_next_job(jobs, seen, attempted)
            self.assertIsNotNone(picked)
            self.assertEqual(picked["input"], str(inputs[1]))
            # All 5 must be in seen now, not just the prefix up to the
            # second job.
            self.assertEqual(len(seen), 5)


class CounterTotalsReflectFullQueueOnFirstJobTest(unittest.TestCase):
    """compute_queue_counters at job 1 must report TOTAL = full queue
    size, REMAINING = N-1 — never `total=1, remaining=0`."""

    def test_first_job_in_five_reports_total_5_remaining_4(self) -> None:
        from queue_modules.queue_counters import compute_queue_counters
        env = compute_queue_counters([], total_jobs=5,
                                     queue_wall_seconds=0.0,
                                     upcoming_index=1)
        self.assertEqual(env["X265_QUEUE_TOTAL"], "5")
        self.assertEqual(env["X265_QUEUE_ITEMS_REMAINING"], "4")


if __name__ == "__main__":
    unittest.main()

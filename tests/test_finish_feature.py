"""finish-after-current-chunk: the trigger chain and the exit/queue plumbing.

- ParallelDisplay.toggle_finish() flips the request and returns an ON/OFF line.
- A worker pulls no new chunk once finish is requested (the in-flight chunk
  having already finished — workers only check at the top of the loop).
- exit code 8 maps to the "stopped-by-user" status.
- run_queue halts the whole queue on "stopped-by-user" and reports it as a
  needs-attention (resumable) outcome, not a failure.
"""
from __future__ import annotations

import queue
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.display import ParallelDisplay  # noqa: E402
from encode_modules.encode_parallel import _worker, _WorkerContext  # noqa: E402
from queue_modules.job_runner import status_for_exit  # noqa: E402
from run_queue import _aggregate_exit_code, _should_halt_after  # noqa: E402


class ToggleFinishTest(unittest.TestCase):
    def test_toggle_finish_flips_and_messages(self) -> None:
        d = ParallelDisplay(parallel=1, total=1, already_done=0)
        on = d.toggle_finish()
        self.assertTrue(d.finish_signal.requested)
        self.assertIn("ON", on)
        off = d.toggle_finish()
        self.assertFalse(d.finish_signal.requested)
        self.assertIn("OFF", off)


class WorkerHonorsFinishTest(unittest.TestCase):
    def test_worker_pulls_nothing_once_finish_requested(self) -> None:
        d = ParallelDisplay(parallel=1, total=1, already_done=0)
        d.finish_signal.request()  # as if 'f' was pressed before this worker ran
        work_q: "queue.Queue[Path]" = queue.Queue()
        work_q.put(Path("src_0001.mkv"))
        ctx = _WorkerContext(
            display=d, work_q=work_q, results=[],
            results_lock=threading.Lock(), workdir=Path("."),
            crf=22, preset="slow", pix_fmt="yuv420p10le",
            x265_params="x", x265_params_for_autofix="x", auto_fix_choke=False,
        )
        _worker(0, ctx)
        # finish was requested at the top of the loop -> no chunk pulled/encoded
        self.assertEqual(work_q.qsize(), 1)
        self.assertEqual(ctx.results, [])


class StoppedByUserPlumbingTest(unittest.TestCase):
    def test_exit_8_maps_to_stopped_by_user(self) -> None:
        self.assertEqual(status_for_exit(8), "stopped-by-user")

    def test_aggregate_stopped_by_user_is_needs_attention(self) -> None:
        self.assertEqual(
            _aggregate_exit_code([{"status": "stopped-by-user"}]), 2)

    def test_stopped_by_user_halts_queue(self) -> None:
        self.assertTrue(_should_halt_after("stopped-by-user",
                                           stop_on_failure=False))

    def test_ok_does_not_halt(self) -> None:
        self.assertFalse(_should_halt_after("ok", stop_on_failure=False))

    def test_failed_halts_only_with_flag(self) -> None:
        self.assertFalse(_should_halt_after("failed-gen", stop_on_failure=False))
        self.assertTrue(_should_halt_after("failed-gen", stop_on_failure=True))


if __name__ == "__main__":
    unittest.main()

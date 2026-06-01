"""End-to-end coverage of `crf_retry.run_job_with_crf_retry` with the
v1.15.0 adaptive-jump path active.

`tests/test_crf_retry.py` covers the blind-walk (Fix-1-disabled) path;
`tests/test_crf_jump.py` covers the pure math + the projection reader
in isolation; `tests/test_queue_state_in_progress.py` covers the
sidecar's new field. None of those exercises the retry loop with
`crf_jump: true` plus a real `encoding_history.jsonl` file written
between scripted probes. This file fills that gap so the cross-process
projection channel, the floor detector wiring, and the state-sidecar
write/clear lifecycle are all proved together.

Each test scripts the (status, row) sequence that `run_one_job` would
return for each probe AND, between probes, appends a matching record
to a temp `encoding_history.jsonl` so the next iteration's
`read_last_projection` finds the projection the encoder "wrote". A
real subprocess is never spawned.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules import job_runner  # noqa: E402
from queue_modules.crf_retry import (  # noqa: E402
    CRF_EXHAUSTED_STATUS, run_job_with_crf_retry,
)
from queue_modules.queue_state import QueueState  # noqa: E402


class _HistoryAndProbeScripter:
    """Fake `run_one_job` that, for each scripted (status, projected_pct)
    tuple, records the CRF the queue picked, appends a matching record
    to the temp history JSONL so the next iteration's
    `read_last_projection` finds it, and returns (status, row)."""

    def __init__(self, *, source: Path, history_path: Path,
                 script: list[tuple[str, float]],
                 threshold_pct: float = 80.0,
                 source_bytes: int = 1_000_000_000) -> None:
        self._source = source
        self._history = history_path
        self._script = list(script)
        self._threshold_pct = threshold_pct
        self._source_bytes = source_bytes
        self.crfs_seen: list[int] = []

    def __call__(self, *, compress_py, merged, i, n):
        crf = int(merged["crf"])
        self.crfs_seen.append(crf)
        status, projected_pct = self._script.pop(0)
        # Write a history row for this probe so the queue can read it
        # back via read_last_projection on the next iteration. Format
        # matches what `encode_modules/history_state.py::_project_into_record`
        # plants for a stopped-threshold abort.
        rec = {
            "input": {"path": str(self._source.resolve()),
                      "size_bytes": self._source_bytes},
            "status": status,
            "settings": {"crf": crf},
            "output": {
                "bytes_projected": int(
                    self._source_bytes * projected_pct / 100),
                "bytes_threshold": int(
                    self._source_bytes * self._threshold_pct / 100),
                "projected_pct": projected_pct,
                "threshold_pct": self._threshold_pct,
            },
        }
        with self._history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return status, {"status": status, "crf": crf}


class _Td:
    """Helper: temp dir with queue.json + history.jsonl scaffolding."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.queue_path = d / "q.json"
        self.queue_path.write_text("[]", encoding="utf-8")
        self.history_path = d / "encoding_history.jsonl"
        self.source = d / "movie.mp4"
        # Touch the source so Path.resolve() doesn't get confused on
        # Windows (resolve() of a non-existent path on POSIX is fine).
        self.source.write_bytes(b"x" * 1024)

    def close(self) -> None:
        self._tmp.cleanup()


class CrfJumpEscalationTest(unittest.TestCase):
    """Adaptive jump replaces the blind walk."""

    def setUp(self) -> None:
        self.td = _Td()
        self.addCleanup(self.td.close)
        self._orig_run = job_runner.run_one_job
        self._orig_supersede = job_runner.supersede_encoded_chunks
        job_runner.supersede_encoded_chunks = lambda *a, **k: 0

    def tearDown(self) -> None:
        job_runner.run_one_job = self._orig_run
        job_runner.supersede_encoded_chunks = self._orig_supersede

    def test_emily_19_at_122pct_jumps_to_23(self) -> None:
        # Spec's flagship example. With the BLIND walk, this is
        # 19 → 20 → 21 → 22 → 23 (4 aborts). With crf_jump, the first
        # probe at 19 projects 122%, the math says jump+4 → 23, and 23
        # encodes cleanly.
        # threshold_pct is what the ENCODER records as its hard cap
        # (`max_size_percent`), NOT the queue runner's computed
        # target T = max_size_percent - margin. The queue's
        # compute_next_crf applies the margin itself.
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            threshold_pct=85.0,
            script=[("stopped-threshold", 122.0),  # at CRF 19
                    ("ok",                 75.0)]) # at CRF 23
        job_runner.run_one_job = fake
        state = QueueState()
        status, row = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 19,
                    "retry_with_bigger_crf": True, "crf_jump": True,
                    "crf_jump_k": 6.0, "crf_jump_margin": 5.0,
                    "crf_step": 1, "crf_max": 28,
                    "max_size_percent": 85},
            i=1, n=1,
            queue_state=state, queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        self.assertEqual(status, "ok")
        self.assertEqual(fake.crfs_seen, [19, 23])
        self.assertEqual(row["crf"], 23)
        # ok clears the in-progress entry.
        self.assertIsNone(state.get_escalation(self.td.source))

    def test_state_persisted_between_probes(self) -> None:
        # After the first failed probe, the state sidecar must record
        # last_crf_tried (the just-tried CRF) and attempts=1. After ok,
        # it must be cleared.
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            script=[("stopped-threshold", 122.0),
                    ("ok",                 75.0)])
        job_runner.run_one_job = fake
        state = QueueState()
        # Patch save_atomically to capture mid-flight state snapshots.
        snapshots: list[dict | None] = []
        orig_save = state.save_atomically

        def _spy(queue_path):
            snapshots.append(state.get_escalation(self.td.source))
            return orig_save(queue_path)
        state.save_atomically = _spy  # type: ignore[assignment]
        run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 19,
                    "retry_with_bigger_crf": True, "crf_jump": True,
                    "max_size_percent": 85},
            i=1, n=1,
            queue_state=state, queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        # Persist after probe 1 (last_crf_tried=19, attempts=1), clear
        # after probe 2 (ok).
        self.assertGreaterEqual(len(snapshots), 2)
        first = snapshots[0]
        self.assertIsNotNone(first)
        self.assertEqual(first["last_crf_tried"], 19)
        self.assertEqual(first["attempts"], 1)
        self.assertIsNone(snapshots[-1])


class CrfJumpResumeFromStateTest(unittest.TestCase):
    """A queue restart with pre-seeded in_progress_escalations resumes
    at last_crf_tried + step, not at the configured CRF."""

    def setUp(self) -> None:
        self.td = _Td()
        self.addCleanup(self.td.close)
        self._orig_run = job_runner.run_one_job
        self._orig_supersede = job_runner.supersede_encoded_chunks
        job_runner.supersede_encoded_chunks = lambda *a, **k: 0

    def tearDown(self) -> None:
        job_runner.run_one_job = self._orig_run
        job_runner.supersede_encoded_chunks = self._orig_supersede

    def test_resume_skips_already_walked_crfs(self) -> None:
        # Pre-existing state: April Bigass got to CRF 24 in a prior
        # run (88.5% projected, target 80). Configured CRF is still
        # 23 — but we resume at 25 (= 24 + step), NOT replay 23 → 24.
        state = QueueState()
        state.set_escalation(
            input_path=self.td.source, last_crf_tried=24,
            last_projected_pct=88.5, last_threshold_pct=80.0,
            attempts=2)
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            script=[("ok", 75.0)])  # the one probe at the resumed CRF
        job_runner.run_one_job = fake
        status, row = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 23,
                    "retry_with_bigger_crf": True, "crf_jump": True,
                    "crf_step": 1, "max_size_percent": 85},
            i=1, n=1,
            queue_state=state, queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        self.assertEqual(status, "ok")
        self.assertEqual(fake.crfs_seen, [25])

    def test_higher_configured_crf_wins_over_stale_state(self) -> None:
        # If the user raised the configured CRF beyond where state left
        # off, the configured value wins (the user's intent overrides
        # stale state).
        state = QueueState()
        state.set_escalation(
            input_path=self.td.source, last_crf_tried=20,
            last_projected_pct=85.5, last_threshold_pct=80.0,
            attempts=1)
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            script=[("ok", 75.0)])
        job_runner.run_one_job = fake
        run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 25,  # > 20+1
                    "retry_with_bigger_crf": True, "crf_jump": True,
                    "crf_step": 1, "max_size_percent": 85},
            i=1, n=1,
            queue_state=state, queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        self.assertEqual(fake.crfs_seen, [25])


class CrfJumpFloorDetectorTest(unittest.TestCase):
    """The Alyssa 21 → 22 (85.3 → 85.2, Δ 0.1 pt) case from the spec —
    stops as crf-exhausted after the second probe instead of walking
    the whole ladder to crf_max."""

    def setUp(self) -> None:
        self.td = _Td()
        self.addCleanup(self.td.close)
        self._orig_run = job_runner.run_one_job
        self._orig_supersede = job_runner.supersede_encoded_chunks
        job_runner.supersede_encoded_chunks = lambda *a, **k: 0

    def tearDown(self) -> None:
        job_runner.run_one_job = self._orig_run
        job_runner.supersede_encoded_chunks = self._orig_supersede

    def test_floor_bound_source_stops_after_two_probes(self) -> None:
        # Two probes both over threshold (85.0 cap), Δ 0.1 pt — well
        # under the default min_gain of 2.0. Detector fires; no third
        # probe is even attempted.
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            threshold_pct=85.0,
            # Configured to land on the floor case: 21 -> 85.3,
            # jumped-to -> 85.2 (Δ 0.1)
            script=[("stopped-threshold", 85.3),
                    ("stopped-threshold", 85.2)])
        job_runner.run_one_job = fake
        status, row = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 21,
                    "retry_with_bigger_crf": True, "crf_jump": True,
                    "crf_step": 1, "crf_max": 28,
                    "max_size_percent": 85,
                    "crf_floor_min_gain": 2.0},
            i=1, n=1,
            queue_state=QueueState(), queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        self.assertEqual(status, CRF_EXHAUSTED_STATUS)
        self.assertEqual(row["status"], CRF_EXHAUSTED_STATUS)
        # Two probes, NOT seven (21 -> 22 -> 23 -> ... -> 28).
        self.assertEqual(len(fake.crfs_seen), 2)

    def test_min_gain_zero_disables_floor_walks_to_cap(self) -> None:
        # Opt-out path — same flat projections, but min_gain=0 so the
        # detector never fires; walks to crf_max as in v1.13.x.
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            threshold_pct=85.0,
            script=[("stopped-threshold", 85.3)] * 3)
        job_runner.run_one_job = fake
        status, _ = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 21,
                    "retry_with_bigger_crf": True, "crf_jump": True,
                    "crf_step": 1, "crf_max": 23,
                    "max_size_percent": 85,
                    "crf_floor_min_gain": 0},
            i=1, n=1,
            queue_state=QueueState(), queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        self.assertEqual(status, CRF_EXHAUSTED_STATUS)
        # 21, 22, 23 — full walk to cap.
        self.assertEqual(fake.crfs_seen, [21, 22, 23])

    def test_floor_detector_fires_on_blind_walk_too(self) -> None:
        # Per the spec, Fix 3 is independent of Fix 1. A user on the
        # blind +crf_step walk who sets crf_floor_min_gain still gets
        # early-stopped on a floor-bound source.
        fake = _HistoryAndProbeScripter(
            source=self.td.source, history_path=self.td.history_path,
            threshold_pct=85.0,
            script=[("stopped-threshold", 85.3),
                    ("stopped-threshold", 85.2)])
        job_runner.run_one_job = fake
        status, _ = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": str(self.td.source), "crf": 21,
                    "retry_with_bigger_crf": True,
                    # crf_jump deliberately OFF (blind walk).
                    "crf_step": 1, "crf_max": 28,
                    "max_size_percent": 85,
                    "crf_floor_min_gain": 2.0},
            i=1, n=1,
            queue_state=QueueState(), queue_path=self.td.queue_path,
            history_path=self.td.history_path)
        self.assertEqual(status, CRF_EXHAUSTED_STATUS)
        self.assertEqual(len(fake.crfs_seen), 2)


if __name__ == "__main__":
    unittest.main()

"""F1 — `in_progress_escalations` field on the queue-state sidecar.

Stores per-source CRF-escalation state so a queue restart resumes at
`last_crf_tried + step` instead of re-walking the ladder from the
configured CRF. Without this, a stop-restart cycle re-tries the same
failed CRFs every time — the `23->24->[restart]->23->24->25` pattern
the spec calls out.

Backward-compat invariant: schema_version stays at 1; the new field is
additive at the top level. An OLD reader (no knowledge of in-progress
escalations) silently ignores the field — its existing `completed`
data is read unchanged. A NEW reader consumes both. An OLD sidecar
without the field reads as "no in-progress entries" (today's
behaviour). No migration; no version bump.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.queue_state import (  # noqa: E402
    SCHEMA_VERSION, QueueState, load_queue_state,
)


class _Td:
    """Helper: temp dir with a queue.json + state.json path."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.queue_path = d / "myq.json"
        self.queue_path.write_text("[]", encoding="utf-8")
        # v1.19.0: state sidecar lives in <queue>/logs/.
        self.state_path = d / "logs" / "myq.state.json"

    def write_sidecar(self, data: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2),
                                   encoding="utf-8")

    def read_sidecar(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self._tmp.cleanup()


class AddAndPersistEscalationTest(unittest.TestCase):
    """Round-trip: set an in-progress entry, save_atomically, reload,
    confirm the data is intact."""

    def setUp(self) -> None:
        self.td = _Td()
        self.addCleanup(self.td.close)

    def test_round_trip_single_entry(self) -> None:
        state = QueueState()
        state.set_escalation(
            input_path=Path("/v/movie.mp4"),
            last_crf_tried=24, last_projected_pct=88.5,
            last_threshold_pct=80.0, attempts=2)
        state.save_atomically(self.td.queue_path)
        loaded = load_queue_state(self.td.queue_path)
        rec = loaded.get_escalation(Path("/v/movie.mp4"))
        self.assertIsNotNone(rec)
        self.assertEqual(rec["last_crf_tried"], 24)
        self.assertEqual(rec["last_projected_pct"], 88.5)
        self.assertEqual(rec["last_threshold_pct"], 80.0)
        self.assertEqual(rec["attempts"], 2)

    def test_setting_again_overwrites(self) -> None:
        state = QueueState()
        state.set_escalation(input_path=Path("/v/x.mp4"),
                             last_crf_tried=23, last_projected_pct=95.0,
                             last_threshold_pct=80.0, attempts=1)
        state.set_escalation(input_path=Path("/v/x.mp4"),
                             last_crf_tried=24, last_projected_pct=88.0,
                             last_threshold_pct=80.0, attempts=2)
        rec = state.get_escalation(Path("/v/x.mp4"))
        self.assertEqual(rec["last_crf_tried"], 24)
        self.assertEqual(rec["attempts"], 2)

    def test_clear_on_ok_removes_entry(self) -> None:
        state = QueueState()
        state.set_escalation(input_path=Path("/v/x.mp4"),
                             last_crf_tried=24, last_projected_pct=88.0,
                             last_threshold_pct=80.0, attempts=2)
        state.clear_escalation(Path("/v/x.mp4"))
        self.assertIsNone(state.get_escalation(Path("/v/x.mp4")))

    def test_clear_on_missing_input_is_noop(self) -> None:
        # Defensive: ok-status code paths blindly call clear; missing
        # entries must NOT raise.
        QueueState().clear_escalation(Path("/v/missing.mp4"))  # no raise

    def test_round_trip_independent_of_completed(self) -> None:
        # Adding completion + escalation entries should coexist;
        # neither should clobber the other on save/load.
        state = QueueState()
        state.add_completed(input_original=Path("/v/done.mp4"),
                            crf_final=22, bytes_in=1000, bytes_out=600)
        state.set_escalation(input_path=Path("/v/wip.mp4"),
                             last_crf_tried=23, last_projected_pct=95.0,
                             last_threshold_pct=80.0, attempts=1)
        state.save_atomically(self.td.queue_path)
        loaded = load_queue_state(self.td.queue_path)
        self.assertTrue(loaded.is_completed(Path("/v/done.mp4")))
        self.assertIsNotNone(loaded.get_escalation(Path("/v/wip.mp4")))


class SidecarShapeTest(unittest.TestCase):
    """The on-disk shape — locks the schema so a reader in another
    language / tool can rely on it. Key contract: `schema_version`
    stays at 1 and `in_progress_escalations` is a TOP-LEVEL dict
    keyed by resolved input path."""

    def setUp(self) -> None:
        self.td = _Td()
        self.addCleanup(self.td.close)

    def test_schema_version_unchanged(self) -> None:
        state = QueueState()
        state.set_escalation(input_path=Path("/v/x.mp4"),
                             last_crf_tried=24, last_projected_pct=88.0,
                             last_threshold_pct=80.0, attempts=1)
        state.save_atomically(self.td.queue_path)
        data = self.td.read_sidecar()
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1,
                         "no schema bump — additive forward-compat")

    def test_in_progress_field_only_written_when_nonempty(self) -> None:
        # No escalation entries -> field is OMITTED (not an empty dict).
        # Keeps the file tidy and makes "no in-progress work" visually
        # obvious to a human reader.
        state = QueueState()
        state.add_completed(input_original=Path("/v/a.mp4"))
        state.save_atomically(self.td.queue_path)
        data = self.td.read_sidecar()
        self.assertNotIn("in_progress_escalations", data)

    def test_in_progress_field_keyed_by_resolved_path(self) -> None:
        # Same key style as the in-memory completed dict — absolute
        # resolved path string. Predictable across machines / cwd.
        state = QueueState()
        state.set_escalation(input_path=Path("/v/movie.mp4"),
                             last_crf_tried=24, last_projected_pct=88.0,
                             last_threshold_pct=80.0, attempts=2)
        state.save_atomically(self.td.queue_path)
        data = self.td.read_sidecar()
        ip = data.get("in_progress_escalations")
        self.assertIsInstance(ip, dict)
        # The key matches Path.resolve() output — on POSIX = "/v/movie.mp4",
        # on Windows = "C:\\v\\movie.mp4". Match the same transform.
        expected_key = str(Path("/v/movie.mp4").resolve())
        self.assertIn(expected_key, ip)


class BackwardCompatTest(unittest.TestCase):
    """v1.13.x sidecars (no `in_progress_escalations` field) must keep
    working unchanged. v1.15.0 sidecars must be readable by v1.13.x
    code (which only iterates `completed`)."""

    def setUp(self) -> None:
        self.td = _Td()
        self.addCleanup(self.td.close)

    def test_v1_13_sidecar_loads_with_empty_escalations(self) -> None:
        # Hand-craft a v1.13-shape sidecar.
        self.td.write_sidecar({
            "schema_version": 1,
            "queue_file": "myq.json",
            "completed": [
                {"input_original": "/v/a.mp4", "crf_final": 22,
                 "bytes_in": 1000, "bytes_out": 600},
            ],
        })
        state = load_queue_state(self.td.queue_path)
        self.assertTrue(state.is_completed(Path("/v/a.mp4")))
        self.assertIsNone(state.get_escalation(Path("/v/a.mp4")))

    def test_v1_15_sidecar_still_has_completed_field_for_old_readers(
            self) -> None:
        # If an old v1.13 reader picks up a v1.15-written sidecar, it
        # must still find `completed` at the top level. (We don't run
        # the v1.13 reader here; we just confirm the field is present
        # in the written form.)
        state = QueueState()
        state.add_completed(input_original=Path("/v/a.mp4"))
        state.set_escalation(input_path=Path("/v/b.mp4"),
                             last_crf_tried=23, last_projected_pct=90.0,
                             last_threshold_pct=80.0, attempts=1)
        state.save_atomically(self.td.queue_path)
        data = self.td.read_sidecar()
        self.assertIn("completed", data)
        self.assertEqual(len(data["completed"]), 1)
        self.assertIn("in_progress_escalations", data)

    def test_corrupt_escalations_field_degrades_silently(self) -> None:
        # Per the existing degrade-don't-crash discipline: an
        # in_progress_escalations field that's not a dict (e.g. a future
        # schema variant or a hand-edit gone wrong) reads as "no
        # escalations" rather than raising.
        self.td.write_sidecar({
            "schema_version": 1, "queue_file": "myq.json",
            "completed": [],
            "in_progress_escalations": "not a dict",
        })
        state = load_queue_state(self.td.queue_path)
        self.assertIsNone(state.get_escalation(Path("/v/x.mp4")))


if __name__ == "__main__":
    unittest.main()

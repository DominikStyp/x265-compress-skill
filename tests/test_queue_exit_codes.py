"""Tier 2.2: a fleet runner must be able to tell three outcomes apart from
run_queue's exit code, instead of "all aborts collapse to 1" / "threshold
aborts look like success (0)":

    0  every job clean (ok, or output already existed)
    1  at least one real failure (compress.py crashed, bad output, ...)
    2  no hard failure, but something needs attention (size-guard abort,
       chunks awaiting a manual fix, missing input, corrupt source)

Plus an opt-in --json-status NDJSON stream for machine consumption, kept off
stdout (stdout stays human-readable).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_queue import _aggregate_exit_code, _emit_json_status  # noqa: E402


def _rows(*statuses: str) -> list[dict]:
    return [{"status": s, "input": f"{s}.mp4"} for s in statuses]


class AggregateExitCodeTest(unittest.TestCase):
    def test_all_clean_is_zero(self) -> None:
        self.assertEqual(_aggregate_exit_code(_rows("ok", "skipped-exists")), 0)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(_aggregate_exit_code([]), 0)

    def test_real_failure_is_one(self) -> None:
        self.assertEqual(_aggregate_exit_code(_rows("ok", "failed-gen")), 1)
        self.assertEqual(_aggregate_exit_code(_rows("failed-exit-9")), 1)

    def test_needs_attention_is_two(self) -> None:
        self.assertEqual(_aggregate_exit_code(_rows("ok", "stopped-threshold")), 2)
        self.assertEqual(_aggregate_exit_code(_rows("awaiting-chunk-fix")), 2)
        self.assertEqual(_aggregate_exit_code(_rows("skipped-not-found")), 2)

    def test_failure_outranks_attention(self) -> None:
        self.assertEqual(
            _aggregate_exit_code(_rows("stopped-threshold", "failed-gen")), 1)

    def test_unknown_status_is_failure_failsafe(self) -> None:
        self.assertEqual(_aggregate_exit_code(_rows("ok", "weird-new-state")), 1)


class JsonStatusTest(unittest.TestCase):
    def test_emits_one_ndjson_record_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "status.ndjson"
            _emit_json_status(path, {
                "input": "a.mp4", "status": "ok", "output": "a.mkv",
                "input_bytes": 100, "output_bytes": 60,
                "elapsed_seconds": 12.5, "vmaf_mean": 97.1,
            })
            _emit_json_status(path, {"input": "b.mp4", "status": "failed-gen"})
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["input"], "a.mp4")
            self.assertEqual(first["status"], "ok")
            self.assertEqual(first["output_bytes"], 60)
            self.assertEqual(json.loads(lines[1])["status"], "failed-gen")


if __name__ == "__main__":
    unittest.main()

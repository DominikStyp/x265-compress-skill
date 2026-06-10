"""queue_io: mid-edit resilience contract.

Pins three behaviours that let the user hand-edit queue.json while the
runner is live:

  * a transient parse failure is retried exactly once after a short pause;
  * a persistent failure re-raises so the CALLER decides (startup = fatal,
    mid-run = graceful "ending queue" with summary + reports);
  * a wrong top-level JSON type raises a normal ValueError — NOT SystemExit.
    SystemExit derives from BaseException, so it would sail past the
    `except Exception` seams in reload_queue_with_retry and run_queue.main's
    reload loop, killing the runner mid-run WITHOUT the end-of-queue summary
    or aggregate report.

Plus: emit_status_record must never raise (logging can't break the run).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules import queue_io  # noqa: E402


class LoadQueueTest(unittest.TestCase):
    def _write(self, td: str, payload: str) -> Path:
        p = Path(td) / "queue.json"
        p.write_text(payload, encoding="utf-8")
        return p

    def test_object_form_returns_defaults_and_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, json.dumps(
                {"defaults": {"crf": 22}, "jobs": [{"input": "a.mkv"}]}))
            defaults, jobs = queue_io.load_queue(p)
            self.assertEqual(defaults, {"crf": 22})
            self.assertEqual(jobs, [{"input": "a.mkv"}])

    def test_flat_list_form_yields_empty_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, json.dumps([{"input": "a.mkv"}]))
            defaults, jobs = queue_io.load_queue(p)
            self.assertEqual(defaults, {})
            self.assertEqual(jobs, [{"input": "a.mkv"}])

    def test_wrong_top_level_type_raises_value_error_not_systemexit(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "123")
            with self.assertRaises(ValueError) as ctx:
                queue_io.load_queue(p)
            self.assertNotIsInstance(ctx.exception, SystemExit)
            self.assertIn("list or an object", str(ctx.exception))


class ReloadRetryTest(unittest.TestCase):
    def test_transient_corruption_retries_once_then_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "queue.json"
            p.write_text("[]", encoding="utf-8")
            boom = json.JSONDecodeError("mid-edit", "{", 0)
            sleeps: list[float] = []
            with mock.patch.object(
                    queue_io, "load_queue",
                    side_effect=[boom, ({}, [{"input": "a.mkv"}])]):
                with mock.patch.object(queue_io, "expand_jobs",
                                       side_effect=lambda raw, base: raw):
                    with mock.patch.object(queue_io.time, "sleep",
                                           side_effect=sleeps.append):
                        mtime, defaults, jobs = \
                            queue_io.reload_queue_with_retry(p)
            self.assertEqual(jobs, [{"input": "a.mkv"}])
            self.assertEqual(sleeps, [0.4])

    def test_persistent_corruption_reraises_original_type(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "queue.json"
            p.write_text("{not json", encoding="utf-8")
            with mock.patch.object(queue_io.time, "sleep"):
                with self.assertRaises(json.JSONDecodeError):
                    queue_io.reload_queue_with_retry(p)

    def test_bad_root_type_is_catchable_by_except_exception(self):
        """The mid-run reload seam catches `Exception`. A queue.json whose
        root is a bare string/number must surface as something that seam
        can catch, or the runner dies without its summary/report."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "queue.json"
            p.write_text('"oops"', encoding="utf-8")
            with mock.patch.object(queue_io.time, "sleep"):
                try:
                    queue_io.reload_queue_with_retry(p)
                except Exception:
                    pass  # the contract: catchable as Exception
                else:
                    self.fail("bad root type must raise")


class EmitStatusRecordTest(unittest.TestCase):
    def test_write_failure_is_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            # A directory path can't be opened for append -> OSError inside.
            queue_io.emit_status_record(td, {"input": "a", "status": "ok"})
            # Reaching here without an exception IS the assertion.


if __name__ == "__main__":
    unittest.main()

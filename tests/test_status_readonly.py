"""`run_queue.py --status` must be READ-ONLY — zero side effects.

The flag is documented (and queue_modules/status.py promises) "no encoding,
no side effects". Regression pinned here: the v1.19.0 one-shot log migration
ran BEFORE the --status early-exit, so merely *inspecting* a queue on a
pre-v1.19.0 workspace physically relocated state/report/history files into
logs/. Migration belongs only on the encoding path.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_queue  # noqa: E402


class StatusIsReadOnlyTest(unittest.TestCase):
    def _make_queue(self, td: str) -> Path:
        q = Path(td) / "queue.json"
        q.write_text(json.dumps(
            {"defaults": {}, "jobs": [{"input": "a.mkv"}]}),
            encoding="utf-8")
        return q

    def test_status_does_not_run_migration(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._make_queue(td)
            migrate_spy = mock.Mock(return_value=[])
            with mock.patch.object(run_queue, "migrate_for_queue_run",
                                   migrate_spy):
                with mock.patch.object(sys, "argv",
                                       ["run_queue.py", str(q), "--status"]):
                    with redirect_stdout(io.StringIO()):
                        rc = run_queue.main()
            self.assertIsInstance(rc, int)
            migrate_spy.assert_not_called()

    def test_encoding_path_still_migrates(self):
        """The migration must keep running for REAL queue runs — pin it so
        the --status fix can't accidentally remove migration entirely."""
        with tempfile.TemporaryDirectory() as td:
            q = self._make_queue(td)
            # Empty the queue so the main loop ends immediately after startup.
            q.write_text("[]", encoding="utf-8")
            migrate_spy = mock.Mock(return_value=[])
            with mock.patch.object(run_queue, "migrate_for_queue_run",
                                   migrate_spy):
                with mock.patch.object(sys, "argv",
                                       ["run_queue.py", str(q)]):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            run_queue.main()  # "no jobs" startup exit
            migrate_spy.assert_called_once()


if __name__ == "__main__":
    unittest.main()

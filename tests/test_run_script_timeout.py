"""`run_script` accepts an optional timeout — a deliberate exception to
AGENTS.md's "every subprocess has a timeout" rule, scoped to probe-style
calls. Default is unbounded (encodes legitimately run for hours);
explicit callers can pass `timeout=N` for a hard backstop.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules import job_runner  # noqa: E402


class TimeoutParameterTest(unittest.TestCase):
    def test_default_is_unbounded(self) -> None:
        # Backward-compat: existing call sites that don't pass timeout
        # must still see `timeout=None` arrive at subprocess.call.
        with mock.patch.object(job_runner.subprocess, "call",
                               return_value=0) as m:
            job_runner.run_script("/tmp/x.bat")
        self.assertEqual(m.call_count, 1)
        kwargs = m.call_args.kwargs
        self.assertIn("timeout", kwargs)
        self.assertIsNone(kwargs["timeout"])

    def test_explicit_timeout_threaded_through(self) -> None:
        with mock.patch.object(job_runner.subprocess, "call",
                               return_value=0) as m:
            job_runner.run_script("/tmp/x.bat", timeout=3600.0)
        self.assertEqual(m.call_args.kwargs["timeout"], 3600.0)

    def test_timeout_expired_returns_124_not_raise(self) -> None:
        # Encodes that hit the timeout should be reported as failures
        # via the exit code so the queue runner's normal status-mapping
        # logic handles them. Raising TimeoutExpired would crash the
        # queue main loop without any per-job report row.
        with mock.patch.object(
                job_runner.subprocess, "call",
                side_effect=subprocess.TimeoutExpired(["bash"], 1.0)):
            rc, elapsed = job_runner.run_script("/tmp/x.bat", timeout=1.0)
        self.assertEqual(rc, 124)  # GNU coreutils convention
        self.assertGreaterEqual(elapsed, 0.0)


if __name__ == "__main__":
    unittest.main()

"""Shared hook-execution core (v1.20.1, finding #1/#2 consolidation).

`encode_modules.hook_base` factors the byte-for-byte-identical `fire()` body
out of the four hook classes into one `run_hook_command`, plus the env-var
stringification helpers (`env_str`/`env_float`/`env_int`) and the named
stderr-tail caps. These tests pin the shared core directly so the per-hook
test suites can trust it; the no-raise catch band and the exact message
formatting (label + suffix placement + tail slicing) are the load-bearing
contract.
"""
from __future__ import annotations

import subprocess
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import hook_base  # noqa: E402


class _Log:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _Runner:
    def __init__(self, returncode=0, stderr="", raises=None):
        self._ns = types.SimpleNamespace(returncode=returncode, stderr=stderr)
        self._raises = raises
        self.calls: list[tuple] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._ns


def _run(runner, log, **over):
    kw = dict(command=["notify"], env_overrides={"X265_X": "1"},
              timeout=30.0, runner=runner, event_log=log,
              source=Path("/abs/a.mp4"), hook_name="on_job_end")
    kw.update(over)
    return hook_base.run_hook_command(**kw)


class EnvHelpersTest(unittest.TestCase):
    def test_env_str(self) -> None:
        self.assertEqual(hook_base.env_str(None), "")
        self.assertEqual(hook_base.env_str(7), "7")
        self.assertEqual(hook_base.env_str("x"), "x")
        # Must never emit the literal "None".
        self.assertNotEqual(hook_base.env_str(None), "None")

    def test_env_float(self) -> None:
        self.assertEqual(hook_base.env_float(None), "")
        self.assertEqual(hook_base.env_float(50.0), "50.00")
        self.assertEqual(hook_base.env_float(35.054), "35.05")

    def test_env_int(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"X": "5"}):
            self.assertEqual(hook_base.env_int("X", default=0), 5)
        with mock.patch.dict(os.environ, {"X": ""}):
            self.assertEqual(hook_base.env_int("X", default=9), 9)
        with mock.patch.dict(os.environ, {"X": "nope"}):
            self.assertEqual(hook_base.env_int("X", default=3), 3)
        # Absent -> default.
        self.assertEqual(hook_base.env_int("_X265_NOPE_", default=2), 2)


class RunHookCommandTest(unittest.TestCase):
    def test_disabled_command_is_noop_and_unlogged(self) -> None:
        log = _Log()
        runner = _Runner()
        self.assertIsNone(_run(runner, log, command=None))
        self.assertEqual(runner.calls, [])
        self.assertEqual(log.calls, [])

    def test_ok_returns_none_and_logs_ok(self) -> None:
        log = _Log()
        self.assertIsNone(_run(_Runner(returncode=0), log))
        self.assertEqual(log.calls[-1]["outcome"], "ok")
        self.assertEqual(log.calls[-1]["event"], "on_job_end")
        self.assertEqual(log.calls[-1]["source"], Path("/abs/a.mp4"))

    def test_env_overrides_merged_over_process_env(self) -> None:
        runner = _Runner()
        _run(runner, _Log(), env_overrides={"X265_FOO": "bar"})
        self.assertEqual(runner.calls[0][1]["env"]["X265_FOO"], "bar")

    def test_nonzero_exit_message_and_log(self) -> None:
        log = _Log()
        msg = _run(_Runner(returncode=3, stderr="boom"), log)
        self.assertEqual(msg, "  ! on_job_end hook exited 3: boom")
        self.assertEqual(log.calls[-1]["outcome"], "exited 3")
        self.assertEqual(log.calls[-1]["stderr_tail"], "boom")

    def test_suffix_placed_before_tail_continuation(self) -> None:
        # ChunkHook-style suffix must render exactly like the pre-refactor
        # string: "...exited 3 (src.mkv): tail".
        msg = _run(_Runner(returncode=3, stderr="boom"), _Log(),
                   hook_name="on_chunk_done", log_suffix=" (src.mkv)")
        self.assertEqual(msg, "  ! on_chunk_done hook exited 3 (src.mkv): boom")

    def test_timeout_message_and_log(self) -> None:
        log = _Log()
        msg = _run(_Runner(raises=subprocess.TimeoutExpired(["x"], 30)), log,
                   log_suffix=" (c.mkv)")
        self.assertEqual(msg, "  ! on_job_end hook timed out after 30s (c.mkv)")
        self.assertEqual(log.calls[-1]["outcome"], "timeout")

    def test_spawn_error_message_and_log(self) -> None:
        log = _Log()
        msg = _run(_Runner(raises=FileNotFoundError("nope")), log)
        self.assertTrue(msg.startswith("  ! on_job_end hook failed: "))
        self.assertIn("FileNotFoundError", msg)
        self.assertEqual(log.calls[-1]["outcome"], "spawn-error")
        self.assertIn("FileNotFoundError", log.calls[-1]["stderr_tail"])

    def test_tail_slicing_log_keeps_more_than_message(self) -> None:
        # Durable log keeps up to STDERR_LOG_TAIL (500); the live message
        # keeps up to STDERR_MSG_TAIL (200). A 600-char stderr exercises both.
        long = "E" * 600
        log = _Log()
        msg = _run(_Runner(returncode=1, stderr=long), log)
        self.assertEqual(len(log.calls[-1]["stderr_tail"]),
                         hook_base.STDERR_LOG_TAIL)
        # Message tail is the last 200 chars after the ": " separator.
        self.assertTrue(msg.endswith("E" * hook_base.STDERR_MSG_TAIL))
        self.assertNotIn("E" * (hook_base.STDERR_MSG_TAIL + 1), msg)

    def test_exited_with_empty_stderr_omits_colon(self) -> None:
        msg = _run(_Runner(returncode=2, stderr=""), _Log())
        self.assertEqual(msg, "  ! on_job_end hook exited 2")


if __name__ == "__main__":
    unittest.main()

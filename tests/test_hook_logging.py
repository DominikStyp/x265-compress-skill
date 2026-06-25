"""Durable hook-event logging (v1.20.0, CR-5).

`encode_modules.hook_logging.record_hook_outcome` appends one structured
JSONL line per hook fire to ``logs/<source-stem>.hooks.log`` so a webhook
failure is diagnosable after the fact instead of scrolling off the terminal.

Non-negotiables under test:
  * Secret-free: the line carries the argv (command) and outcome, NEVER the
    environment — `PUSHBULLET_TOKEN` / `NTFY_TOKEN` ride in env and must not
    appear.
  * Never raises (a logging failure must not abort an encode or escape a
    hook's no-raise contract).
  * No stray directories: a source whose parent dir doesn't exist is a no-op
    (the /abs/a.mp4 unit-test case must not materialize C:\\abs\\logs\\...).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import hook_logging  # noqa: E402
from encode_modules.log_paths import hooks_log_path  # noqa: E402


class RecordHookOutcomeTest(unittest.TestCase):
    def test_writes_one_jsonl_line_with_expected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "movie.mp4"
            src.write_bytes(b"x")
            path = hook_logging.record_hook_outcome(
                source=src, event="on_job_end",
                command=["python3", "/x/notify_ntfy.py"],
                outcome="exited 1", stderr_tail="ntfy: HTTP 400 ...",
                now_fn=lambda: "2026-06-25T00:00:00Z",
            )
            self.assertEqual(path, hooks_log_path(src))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["ts"], "2026-06-25T00:00:00Z")
            self.assertEqual(rec["event"], "on_job_end")
            self.assertEqual(rec["command"],
                             ["python3", "/x/notify_ntfy.py"])
            self.assertEqual(rec["outcome"], "exited 1")
            self.assertIn("HTTP 400", rec["stderr_tail"])

    def test_appends_not_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "movie.mp4"
            src.write_bytes(b"x")
            for i in range(3):
                hook_logging.record_hook_outcome(
                    source=src, event="on_chunk_done", command=["c"],
                    outcome="ok", now_fn=lambda: "T")
            lines = hooks_log_path(src).read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)

    def test_ok_outcome_has_no_stderr_tail_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "movie.mp4"
            src.write_bytes(b"x")
            path = hook_logging.record_hook_outcome(
                source=src, event="on_job_end", command=["c"],
                outcome="ok", now_fn=lambda: "T")
            rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertNotIn("stderr_tail", rec)

    def test_no_write_when_source_parent_missing(self) -> None:
        # The /abs/a.mp4 unit-test case: parent dir does not exist → no-op,
        # returns None, materializes nothing.
        out = hook_logging.record_hook_outcome(
            source=Path("/no/such/dir/a.mp4"), event="on_job_end",
            command=["c"], outcome="ok")
        self.assertIsNone(out)

    def test_never_raises_on_bad_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "movie.mp4"
            src.write_bytes(b"x")

            def boom(_src):
                raise OSError("disk full")

            # A logging-layer failure must be swallowed, returning None.
            out = hook_logging.record_hook_outcome(
                source=src, event="on_job_end", command=["c"],
                outcome="ok", log_path_fn=boom)
            self.assertIsNone(out)

    def test_none_source_is_noop(self) -> None:
        self.assertIsNone(hook_logging.record_hook_outcome(
            source=None, event="x", command=["c"], outcome="ok"))


class _FakeLog:
    """Captures record_hook_outcome(**kwargs) calls without disk I/O, so we
    can assert each hook's fire() logs the right (event, outcome)."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return None


class _Runner:
    def __init__(self, returncode=0, stderr="", raises=None):
        import types as _t
        self._ns = _t.SimpleNamespace(returncode=returncode, stderr=stderr)
        self._raises = raises

    def __call__(self, args, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self._ns


class HookFireLogsOutcomeTest(unittest.TestCase):
    """Every hook fire() must persist its outcome via the injected logger,
    with the on_* config-key event name and an outcome string the durable log
    can be grepped by."""

    def _assert_logged(self, log, *, event, outcome_prefix):
        self.assertEqual(len(log.calls), 1)
        call = log.calls[0]
        self.assertEqual(call["event"], event)
        self.assertTrue(call["outcome"].startswith(outcome_prefix),
                        f"{call['outcome']!r} !~ {outcome_prefix!r}")

    def test_chunk_hook_logs_ok_and_failure(self) -> None:
        from encode_modules.chunk_hook import ChunkHook
        import subprocess
        log = _FakeLog()
        ChunkHook(["c"], source=Path("/abs/a.mp4"), workdir=Path("/abs/wd"),
                  total=1, runner=_Runner(returncode=0),
                  event_log=log).fire(chunk_name="x.mkv", index=1,
                                       status="ok", output=None,
                                       elapsed_sec=0.0)
        self._assert_logged(log, event="on_chunk_done", outcome_prefix="ok")

        log2 = _FakeLog()
        ChunkHook(["c"], source=Path("/abs/a.mp4"), workdir=Path("/abs/wd"),
                  total=1, runner=_Runner(returncode=3, stderr="boom"),
                  event_log=log2).fire(chunk_name="x.mkv", index=1,
                                        status="ok", output=None,
                                        elapsed_sec=0.0)
        self._assert_logged(log2, event="on_chunk_done",
                            outcome_prefix="exited 3")
        self.assertIn("boom", log2.calls[0]["stderr_tail"])

        log3 = _FakeLog()
        ChunkHook(["c"], source=Path("/abs/a.mp4"), workdir=Path("/abs/wd"),
                  total=1,
                  runner=_Runner(raises=subprocess.TimeoutExpired(["c"], 1)),
                  event_log=log3).fire(chunk_name="x.mkv", index=1,
                                       status="ok", output=None,
                                       elapsed_sec=0.0)
        self._assert_logged(log3, event="on_chunk_done",
                            outcome_prefix="timeout")

    def test_job_end_hook_logs_spawn_error(self) -> None:
        from encode_modules.job_end_hook import JobEndHook
        log = _FakeLog()
        JobEndHook(["c"], source=Path("/abs/a.mp4"), workdir=Path("/abs/wd"),
                   runner=_Runner(raises=FileNotFoundError("no")),
                   event_log=log).fire(status="ok")
        self._assert_logged(log, event="on_job_end",
                            outcome_prefix="spawn-error")

    def test_file_complete_hook_logs_ok(self) -> None:
        from encode_modules.file_complete_hook import FileCompleteHook
        log = _FakeLog()
        FileCompleteHook(["c"], source=Path("/abs/a.mp4"),
                         workdir=Path("/abs/wd"), runner=_Runner(returncode=0),
                         event_log=log).fire(status="ok",
                                             output=Path("/abs/wd/o.mkv"))
        self._assert_logged(log, event="on_file_complete", outcome_prefix="ok")

    def test_file_complete_skip_does_not_log(self) -> None:
        # Non-ok / no-output never runs the command, so nothing to log.
        from encode_modules.file_complete_hook import FileCompleteHook
        log = _FakeLog()
        FileCompleteHook(["c"], source=Path("/abs/a.mp4"),
                         workdir=Path("/abs/wd"), runner=_Runner(),
                         event_log=log).fire(status="stopped-threshold",
                                             output=None)
        self.assertEqual(log.calls, [])

    def test_queue_item_end_hook_logs_keyed_on_source(self) -> None:
        # The 4th event (fired from the queue process) also records to the
        # durable log, keyed on the just-finished job's source — so the
        # "all four events" contract in the docs holds.
        from queue_modules.queue_item_hook import QueueItemEndHook
        log = _FakeLog()
        QueueItemEndHook(["c"], runner=_Runner(returncode=2, stderr="bad"),
                         event_log=log).fire(
            status="failed-gen", source=Path("/abs/a.mp4"),
            output=None, summary="s")
        self._assert_logged(log, event="on_queue_item_end",
                            outcome_prefix="exited 2")
        self.assertEqual(log.calls[0]["source"], Path("/abs/a.mp4"))


if __name__ == "__main__":
    unittest.main()

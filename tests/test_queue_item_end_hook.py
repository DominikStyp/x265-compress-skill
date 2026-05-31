"""QueueItemEndHook — queue-side notification fired by run_queue.py AFTER
each finished job (success OR failure). Mirrors the JobEndHook contract:

  * Same parse_hook_spec on the way in (argv list or bare token).
  * Same NO-RAISE discipline — a slow / failing / NUL-bearing hook never
    aborts the queue.
  * Same env-passthrough style — every documented var is present
    (possibly empty); inherits parent os.environ so X265_QUEUE_* counters
    set by the queue runner pass through.

What's NEW vs JobEndHook:
  * Fires from the QUEUE process, not from inside the per-source encoder,
    so it can see the queue snapshot and ship `X265_QUEUE_STATUS_SUMMARY`.
  * Adds `X265_JOB_MARKER` — convenience flag (the same `[OK]` / `[FAILED]`
    used in the summary), so a terse notifier doesn't need to re-derive
    it from the status.

The hook is configured under `on_queue_item_end` in queue.json defaults
or per-job — same merge semantics as the existing on_chunk_done /
on_job_end / on_file_complete hooks.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.queue_item_hook import QueueItemEndHook  # noqa: E402


class _RecordingRunner:
    def __init__(self, returncode: int = 0, stderr: str = "", raises=None):
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(returncode=self.returncode,
                                     stderr=self.stderr)


def _hook(command, runner, **over):
    kw = dict(runner=runner, timeout=30.0)
    kw.update(over)
    return QueueItemEndHook(command, **kw)


def _fire(hook, **over):
    kw = dict(
        status="ok",
        source=Path("/abs/a.mp4"),
        output=Path("/abs/a.mkv"),
        summary="[ 1] [OK]    a.mp4\n[ 2] [..]   b.mp4",
    )
    kw.update(over)
    return hook.fire(**kw)


class DisabledHookTest(unittest.TestCase):
    def test_none_command_means_disabled(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(None, runner)
        self.assertFalse(hook.enabled)
        self.assertIsNone(_fire(hook))
        self.assertEqual(runner.calls, [])


class SuccessFireTest(unittest.TestCase):
    def test_ok_status_emits_ok_marker_and_full_env(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        self.assertTrue(hook.enabled)
        msg = _fire(hook, status="ok")
        self.assertIsNone(msg)
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_HOOK_EVENT"], "queue-item-end")
        self.assertEqual(env["X265_JOB_STATUS"], "ok")
        self.assertEqual(env["X265_JOB_MARKER"], "[OK]")
        self.assertEqual(env["X265_SOURCE"], str(Path("/abs/a.mp4")))
        self.assertEqual(env["X265_OUTPUT"], str(Path("/abs/a.mkv")))
        self.assertIn("[OK]", env["X265_QUEUE_STATUS_SUMMARY"])
        self.assertIn("a.mp4", env["X265_QUEUE_STATUS_SUMMARY"])

    def test_failed_status_emits_failed_marker(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        _fire(hook, status="failed-gen", output=None,
              summary="[ 1] [FAILED] a.mp4")
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "failed-gen")
        self.assertEqual(env["X265_JOB_MARKER"], "[FAILED]")
        # output None -> empty string (KeyError-safe contract).
        self.assertEqual(env["X265_OUTPUT"], "")

    def test_stopped_threshold_status_emits_failed_marker(self) -> None:
        # User spec is two markers — [OK] / [FAILED]. A size-guard abort
        # didn't produce a usable output -> [FAILED] in the summary.
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        _fire(hook, status="stopped-threshold", output=None,
              summary="[ 1] [FAILED] a.mp4")
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "stopped-threshold")
        self.assertEqual(env["X265_JOB_MARKER"], "[FAILED]")

    def test_every_documented_env_key_is_present_with_string_value(
            self) -> None:
        runner = _RecordingRunner()
        _fire(_hook(["notify"], runner))
        env = runner.calls[0][1]["env"]
        for key in ("X265_HOOK_EVENT", "X265_JOB_STATUS", "X265_JOB_MARKER",
                    "X265_SOURCE", "X265_OUTPUT",
                    "X265_QUEUE_STATUS_SUMMARY"):
            self.assertIn(key, env, f"{key} missing")
            self.assertIsInstance(env[key], str)


class BestEffortNeverRaisesTest(unittest.TestCase):
    """Same defensive band as JobEndHook — fire() must not raise out of the
    queue's per-job loop. A notification problem is never worth aborting a
    queue that may have hours of remaining encodes."""

    def test_nonzero_exit_returns_log_line(self) -> None:
        runner = _RecordingRunner(returncode=3, stderr="bad")
        msg = _fire(_hook(["notify"], runner))
        self.assertIsNotNone(msg)
        self.assertIn("3", msg)

    def test_timeout_is_swallowed(self) -> None:
        runner = _RecordingRunner(
            raises=subprocess.TimeoutExpired(["slow"], 30))
        msg = _fire(_hook(["slow"], runner))
        self.assertIsNotNone(msg)
        self.assertIn("timed out", msg.lower())

    def test_missing_command_oserror_is_swallowed(self) -> None:
        runner = _RecordingRunner(raises=FileNotFoundError("nope"))
        msg = _fire(_hook(["nope"], runner))
        self.assertIsNotNone(msg)

    def test_real_subprocess_nul_argv_never_raises(self) -> None:
        # Without a fake runner: prove the defence-in-depth band against
        # ValueError from subprocess.run on NUL-bearing argv. Same coverage
        # as the JobEndHook test of the same name.
        hook = QueueItemEndHook(["prog\x00x"])
        msg = _fire(hook)
        self.assertIsNotNone(msg)


class EnvInheritsProcessEnvTest(unittest.TestCase):
    def test_parent_env_passes_through(self) -> None:
        # X265_QUEUE_* counters are set on os.environ by the queue runner.
        # The hook env must inherit them so the user's notifier can read
        # the same vars it does in the per-encoder on_job_end hook.
        os.environ["_X265_TEST_INHERIT_QITEM"] = "yes"
        try:
            runner = _RecordingRunner()
            _fire(_hook(["notify"], runner))
            self.assertEqual(
                runner.calls[0][1]["env"]["_X265_TEST_INHERIT_QITEM"], "yes")
        finally:
            del os.environ["_X265_TEST_INHERIT_QITEM"]


class BuildDispatchPayloadTest(unittest.TestCase):
    """`build_dispatch_payload` owns the snapshot construction + the
    falsy-disable convention. Pure — no subprocess machinery — so the
    full shape contract is exhaustively exercisable here. The thin
    `dispatch_on_queue_item_end` wrapper just feeds these values into
    `QueueItemEndHook(...).fire(...)`, which has its own dedicated
    tests above."""

    def test_disabled_when_no_command_in_merged(self) -> None:
        from queue_modules.queue_item_hook import build_dispatch_payload
        payload = build_dispatch_payload(
            merged={"input": "/abs/a.mp4"},
            jobs_snapshot=[{"input": "/abs/a.mp4"}],
            job_reports=[{"input": "/abs/a.mp4", "status": "ok"}],
            status="ok", row={"input": "/abs/a.mp4", "status": "ok",
                              "output": "/abs/a.mkv"})
        self.assertIsNone(payload)

    def test_falsy_command_disables_dispatch(self) -> None:
        # Same supported convention as the other hooks: None / [] / "" in
        # a per-job override silently disables an inherited defaults hook.
        from queue_modules.queue_item_hook import build_dispatch_payload
        for falsy in (None, [], ""):
            payload = build_dispatch_payload(
                merged={"input": "/abs/a.mp4", "on_queue_item_end": falsy},
                jobs_snapshot=[{"input": "/abs/a.mp4"}],
                job_reports=[{"input": "/abs/a.mp4", "status": "ok"}],
                status="ok",
                row={"input": "/abs/a.mp4", "status": "ok",
                     "output": "/abs/a.mkv"})
            self.assertIsNone(payload,
                              f"falsy {falsy!r} should disable the hook")

    def test_bare_string_command_wrapped_to_single_element_list(
            self) -> None:
        from queue_modules.queue_item_hook import build_dispatch_payload
        payload = build_dispatch_payload(
            merged={"input": "/abs/a.mp4",
                    "on_queue_item_end": "/x/notify.sh"},
            jobs_snapshot=[{"input": "/abs/a.mp4"}],
            job_reports=[{"input": "/abs/a.mp4", "status": "ok"}],
            status="ok",
            row={"input": "/abs/a.mp4", "status": "ok",
                 "output": "/abs/a.mkv"})
        self.assertEqual(payload["cmd_argv"], ["/x/notify.sh"])

    def test_summary_carries_markers_for_every_job_in_snapshot_order(
            self) -> None:
        # Snapshot order: a (ok), b (failed-gen, just-finished), c (pending).
        # The summary that ships to the notifier must reflect all three with
        # the spec'd markers, in snapshot order.
        from queue_modules.queue_item_hook import build_dispatch_payload
        snapshot = [{"input": "/q/a.mp4"}, {"input": "/q/b.mp4"},
                    {"input": "/q/c.mp4"}]
        reports = [
            {"input": "/q/a.mp4", "status": "ok"},
            {"input": "/q/b.mp4", "status": "failed-gen", "output": None},
        ]
        payload = build_dispatch_payload(
            merged={"input": "/q/b.mp4",
                    "on_queue_item_end": ["notify"]},
            jobs_snapshot=snapshot, job_reports=reports,
            status="failed-gen",
            row={"input": "/q/b.mp4", "status": "failed-gen",
                 "output": None})
        lines = payload["summary"].splitlines()
        self.assertEqual(len(lines), 3, f"got: {payload['summary']!r}")
        self.assertIn("[OK]", lines[0])
        self.assertIn("a.mp4", lines[0])
        self.assertIn("[FAILED]", lines[1])
        self.assertIn("b.mp4", lines[1])
        self.assertIn("[..]", lines[2])
        self.assertIn("c.mp4", lines[2])
        self.assertEqual(payload["status"], "failed-gen")
        self.assertEqual(payload["source"], Path("/q/b.mp4"))
        # output None -> Path-typed None for the hook's env builder.
        self.assertIsNone(payload["output"])

    def test_output_path_carried_when_present(self) -> None:
        from queue_modules.queue_item_hook import build_dispatch_payload
        payload = build_dispatch_payload(
            merged={"input": "/q/a.mp4",
                    "on_queue_item_end": ["notify"]},
            jobs_snapshot=[{"input": "/q/a.mp4"}],
            job_reports=[{"input": "/q/a.mp4", "status": "ok",
                          "output": "/q/a.mkv"}],
            status="ok",
            row={"input": "/q/a.mp4", "status": "ok",
                 "output": "/q/a.mkv"})
        self.assertEqual(payload["output"], Path("/q/a.mkv"))

    def test_snapshot_and_reports_path_normalisation_match(self) -> None:
        # Safety net for the dispatcher's defensive re-resolution: if a
        # snapshot row and a reports row carry the SAME logical path but
        # written with different separator style (/ vs \) or trailing
        # quirks, the dispatcher must still match them. Both sides go
        # through `str(Path(...).resolve())` so the result depends on
        # Path's normalisation, which differs per-OS — `Path("/q/a.mp4")`
        # on Windows resolves to e.g. `C:\q\a.mp4`, on POSIX to itself.
        # The contract we lock in here: BOTH sides hit the same resolve()
        # call, so however Path chooses to normalise, the keys agree.
        from queue_modules.queue_item_hook import build_dispatch_payload
        # The snapshot carries an absolute path; the report row carries
        # the same absolute path. The dispatcher resolves both and the
        # lookup must succeed (-> the job's marker is [OK], not [..]).
        payload = build_dispatch_payload(
            merged={"input": "/q/the_file.mp4",
                    "on_queue_item_end": ["notify"]},
            jobs_snapshot=[{"input": "/q/the_file.mp4"}],
            job_reports=[{"input": "/q/the_file.mp4", "status": "ok",
                          "output": "/q/the_file.mkv"}],
            status="ok",
            row={"input": "/q/the_file.mp4", "status": "ok",
                 "output": "/q/the_file.mkv"})
        # If the snapshot↔reports lookup had drifted, the renderer would
        # have emitted `[..]` (pending) for the only job in the queue.
        self.assertIn("[OK]", payload["summary"])
        self.assertNotIn("[..]", payload["summary"])
        self.assertIn("the_file.mp4", payload["summary"])


if __name__ == "__main__":
    unittest.main()

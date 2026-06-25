"""JobEndHook fires once per job at the terminal-status chokepoint.

ChunkHook fires per chunk and only knows chunk-level facts. JobEndHook fires
exactly once per job with the FINAL status (ok, stopped-threshold,
chunk-choked, pre-flight-failed, verify-failed, stopped-by-user, ...) and
the rich detail line the on-screen banner already prints — so a phone-
notification script can distinguish "size guard tripped at CRF 21 with
projected 87%" from "real crash". Mirror of ChunkHook's no-raise discipline:
a slow/failing hook never aborts the encoder.
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.job_end_hook import JobEndHook  # noqa: E402
from tests._helpers import RecordingRunner as _RecordingRunner  # noqa: E402


def _hook(command, runner, **over):
    kw = dict(source=Path("/abs/a.mp4"), workdir=Path("/abs/wd"),
              runner=runner, timeout=30.0)
    kw.update(over)
    return JobEndHook(command, **kw)


def _fire(hook, **over):
    kw = dict(
        status="ok", stop_reason="", stop_detail="",
        crf=21, crf_retry_chain="21",
        output=Path("/abs/wd/out.mkv"),
        output_bytes_final=1_000_000, source_bytes=2_000_000,
        output_bytes_projected=None, output_bytes_threshold=None,
        wall_seconds=123.456, pct_saved=50.0,
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
    def test_ok_status_carries_all_env_vars(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        self.assertTrue(hook.enabled)
        self.assertIsNone(_fire(hook, status="ok", crf=21,
                                crf_retry_chain="21,22"))
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_HOOK_EVENT"], "job-end")
        self.assertEqual(env["X265_JOB_STATUS"], "ok")
        self.assertEqual(env["X265_JOB_STOP_REASON"], "")
        self.assertEqual(env["X265_JOB_STOP_DETAIL"], "")
        self.assertEqual(env["X265_SOURCE"], str(Path("/abs/a.mp4")))
        self.assertEqual(env["X265_WORKDIR"], str(Path("/abs/wd")))
        self.assertEqual(env["X265_CRF"], "21")
        self.assertEqual(env["X265_CRF_RETRY_CHAIN"], "21,22")
        self.assertEqual(env["X265_OUTPUT"], str(Path("/abs/wd/out.mkv")))
        self.assertEqual(env["X265_OUTPUT_BYTES_FINAL"], "1000000")
        self.assertEqual(env["X265_SOURCE_BYTES"], "2000000")
        self.assertEqual(env["X265_OUTPUT_BYTES_PROJECTED"], "")
        self.assertEqual(env["X265_OUTPUT_BYTES_THRESHOLD"], "")
        # 123.456 -> 2 dp string, matching the X265_CHUNK_ELAPSED_SEC style.
        self.assertEqual(env["X265_WALL_SECONDS"], "123.46")
        self.assertEqual(env["X265_PCT_SAVED"], "50.00")
        for k, v in env.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)

    def test_threshold_stop_passes_projection_and_detail(self) -> None:
        # The motivating use case — the threshold banner text + the CRF
        # chain become a structured push.
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        detail = ("Estimated output 622.4 MB (85.2% of source) exceeds "
                  "threshold 621.0 MB (85.0%). Stopped at 16.5% overall "
                  "progress.")
        _fire(hook, status="stopped-threshold",
              stop_reason="stopped-threshold",
              stop_detail=detail, crf=21, crf_retry_chain="21",
              output_bytes_projected=622_400_000,
              output_bytes_threshold=621_000_000,
              output=None, output_bytes_final=None, pct_saved=None)
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "stopped-threshold")
        self.assertEqual(env["X265_JOB_STOP_REASON"], "stopped-threshold")
        self.assertEqual(env["X265_JOB_STOP_DETAIL"], detail)
        self.assertEqual(env["X265_OUTPUT_BYTES_PROJECTED"], "622400000")
        self.assertEqual(env["X265_OUTPUT_BYTES_THRESHOLD"], "621000000")
        # OUTPUT and OUTPUT_BYTES_FINAL are empty (no final file produced).
        self.assertEqual(env["X265_OUTPUT"], "")
        self.assertEqual(env["X265_OUTPUT_BYTES_FINAL"], "")
        # PCT_SAVED empty when None — not a misleading "0".
        self.assertEqual(env["X265_PCT_SAVED"], "")

    def test_pre_flight_failed_empty_fields_are_present(self) -> None:
        # Every variable MUST be present so hook scripts can use
        # os.environ[...] without KeyError. Missing values become "".
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        _fire(hook, status="pre-flight-failed",
              stop_reason="pre-flight-failed",
              stop_detail="source-corruption pre-scan failed",
              crf=21, crf_retry_chain="21",
              output=None, output_bytes_final=None,
              pct_saved=None)
        env = runner.calls[0][1]["env"]
        # All the always-present keys are there with strings (possibly empty).
        for key in ("X265_HOOK_EVENT", "X265_JOB_STATUS", "X265_JOB_STOP_REASON",
                    "X265_JOB_STOP_DETAIL", "X265_SOURCE", "X265_WORKDIR",
                    "X265_CRF", "X265_CRF_RETRY_CHAIN", "X265_OUTPUT",
                    "X265_OUTPUT_BYTES_FINAL", "X265_SOURCE_BYTES",
                    "X265_OUTPUT_BYTES_PROJECTED", "X265_OUTPUT_BYTES_THRESHOLD",
                    "X265_WALL_SECONDS", "X265_PCT_SAVED"):
            self.assertIn(key, env, f"{key} missing")
            self.assertIsInstance(env[key], str)


class BestEffortNeverRaisesTest(unittest.TestCase):
    """fire() runs in the encoder's terminal-status flush path. A raising
    hook would either crash the JSONL audit-trail flush or leak a partial
    encode summary. NEVER raises — every exception class subprocess.run
    can produce is swallowed and returned as an optional log line."""

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
        # Same defensive band as ChunkHook — the worker-killing ValueError
        # from a NUL argv must not escape.
        hook = JobEndHook(["prog\x00x"], source=Path("/a"),
                          workdir=Path("/wd"))
        msg = _fire(hook)
        self.assertIsNotNone(msg)


class EnvInheritsProcessEnvTest(unittest.TestCase):
    def test_parent_env_passes_through(self) -> None:
        os.environ["_X265_TEST_INHERIT_JOB"] = "yes"
        try:
            runner = _RecordingRunner()
            _fire(_hook(["notify"], runner))
            self.assertEqual(
                runner.calls[0][1]["env"]["_X265_TEST_INHERIT_JOB"], "yes")
        finally:
            del os.environ["_X265_TEST_INHERIT_JOB"]


class RecorderFiresHookOnFlushTest(unittest.TestCase):
    """The HistoryRecorder is the single chokepoint for terminal-status
    flushes. Wiring on_job_end there guarantees exactly-once fire across all
    exit paths (success, threshold, choke, verify-fail, ctrl-C, atexit)."""

    def setUp(self) -> None:
        import encode_modules.history_state as hs
        hs._reset_for_tests()

    def test_flush_fires_attached_hook_with_status_from_record(self) -> None:
        from unittest import mock
        import encode_modules.history_state as hs
        import tempfile as _tmp

        with _tmp.TemporaryDirectory() as td:
            src = Path(td) / "movie.mp4"
            src.write_bytes(b"X" * 2_000_000)
            runner = _RecordingRunner()
            hook = JobEndHook(["notify"], source=src,
                              workdir=Path("/abs/wd"), runner=runner)
            hs._recorder.attach_job_end_hook(hook)
            # Hand-build the record. Skip init() so we don't ffprobe.
            hs._recorder.current = {
                "status": "ok",
                "input": {"size_bytes": 2_000_000},
                "output": {"path": str(src.with_suffix(".mkv")),
                           "size_bytes": 1_000_000},
                "reduction": {"pct_saved": 50.0},
                "settings": {"crf": 21},
                "wall_seconds": 42.0,
            }
            with mock.patch("history.append_record", lambda *_a, **_kw: None):
                hs._recorder.flush()
            self.assertEqual(len(runner.calls), 1)
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_JOB_STATUS"], "ok")
            self.assertEqual(env["X265_SOURCE_BYTES"], "2000000")
            self.assertEqual(env["X265_OUTPUT_BYTES_FINAL"], "1000000")
            self.assertEqual(env["X265_CRF"], "21")
            self.assertEqual(env["X265_WALL_SECONDS"], "42.00")
            self.assertEqual(env["X265_PCT_SAVED"], "50.00")

    def test_flush_passes_stop_context_for_threshold_abort(self) -> None:
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        hook = JobEndHook(["notify"], source=Path("/a"), workdir=Path("/wd"),
                          runner=runner)
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {
            "status": "stopped-threshold",
            "input": {"size_bytes": 1_000_000_000},
            "output": {"path": "/wd/out.mkv"},  # no size_bytes yet
            "settings": {"crf": 21},
        }
        detail = ("Estimated output 850.0 MB (85.0% of source) exceeds "
                  "threshold 800.0 MB (80.0%). Stopped at 12.3% overall "
                  "progress.")
        hs._recorder.set_stop_context(
            reason="stopped-threshold", detail=detail,
            output_bytes_projected=891_289_600,
            output_bytes_threshold=838_860_800,
        )
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "stopped-threshold")
        self.assertEqual(env["X265_JOB_STOP_REASON"], "stopped-threshold")
        self.assertEqual(env["X265_JOB_STOP_DETAIL"], detail)
        self.assertEqual(env["X265_OUTPUT_BYTES_PROJECTED"], "891289600")
        self.assertEqual(env["X265_OUTPUT_BYTES_THRESHOLD"], "838860800")

    def test_flush_is_idempotent_hook_fires_at_most_once(self) -> None:
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        hook = JobEndHook(["notify"], source=Path("/a"), workdir=Path("/wd"),
                          runner=runner)
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {"status": "ok",
                                 "input": {}, "output": {}, "settings": {}}
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
            hs._recorder.flush()  # atexit may re-trigger
        self.assertEqual(len(runner.calls), 1)

    def test_flush_without_hook_attached_is_silent(self) -> None:
        from unittest import mock
        import encode_modules.history_state as hs

        hs._recorder.current = {"status": "ok",
                                 "input": {}, "output": {}, "settings": {}}
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()  # No hook -> just JSONL, no crash.

    def test_chunk_failed_status_derives_reason_and_detail(self) -> None:
        # The encode_parallel chunk-failed path calls mark_status with
        # `failed_chunks=[...]` but no set_stop_context — the recorder must
        # derive reason from status and detail from the failed_chunks list,
        # so the hook still ships actionable info.
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        hook = JobEndHook(["notify"], source=Path("/abs/a.mp4"),
                          workdir=Path("/abs/wd"), runner=runner)
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {
            "status": "chunk-failed",
            "failed_chunks": ["src_0003.mkv", "src_0007.mkv"],
            "input": {"size_bytes": 1}, "output": {"path": "/abs/wd/out.mkv"},
            "settings": {"crf": 21},
        }
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "chunk-failed")
        self.assertEqual(env["X265_JOB_STOP_REASON"], "chunk-failed")
        self.assertEqual(env["X265_JOB_STOP_DETAIL"],
                         "src_0003.mkv, src_0007.mkv")
        # X265_OUTPUT empty: status != ok → no final file was produced.
        self.assertEqual(env["X265_OUTPUT"], "")
        self.assertEqual(env["X265_OUTPUT_BYTES_FINAL"], "")

    def test_verify_failed_uses_verify_problems_for_detail(self) -> None:
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        hook = JobEndHook(["notify"], source=Path("/abs/a.mp4"),
                          workdir=Path("/abs/wd"), runner=runner)
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {
            "status": "verify-failed",
            "verify_problems": ["dts non-monotonic at chunk 3"],
            "input": {"size_bytes": 1}, "output": {"path": "/abs/wd/out.mkv"},
            "settings": {"crf": 21},
        }
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "verify-failed")
        self.assertEqual(env["X265_JOB_STOP_REASON"], "verify-failed")
        self.assertEqual(env["X265_JOB_STOP_DETAIL"],
                         "dts non-monotonic at chunk 3")

    def test_pre_flight_failed_status_fires_hook(self) -> None:
        # The pre-flight failure path that drove the reviewer's CRITICAL #1
        # bug — must fire the hook now that attach happens before preflight.
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        hook = JobEndHook(["notify"], source=Path("/abs/a.mp4"),
                          workdir=Path("/abs/wd"), runner=runner)
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {
            "status": "pre-flight-failed",
            "pre_flight_scan": {"errors": 12},
            "input": {"size_bytes": 999}, "output": {"path": "/abs/wd/x.mkv"},
            "settings": {"crf": 21},
        }
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_JOB_STATUS"], "pre-flight-failed")
        self.assertEqual(env["X265_JOB_STOP_REASON"], "pre-flight-failed")
        self.assertEqual(env["X265_OUTPUT"], "")

    def test_source_bytes_uses_user_original_not_patched_record(self) -> None:
        # Per project invariant: every user-facing surface (hooks included)
        # reports the ORIGINAL src, never the auto-patch's encode_src. The
        # JSONL record's input.size_bytes IS encode_src (feeds size-projection
        # math) — the hook must stat the hook's bound source instead.
        from unittest import mock
        import encode_modules.history_state as hs
        import tempfile as _tmp

        with _tmp.TemporaryDirectory() as td:
            real_src = Path(td) / "orig.mp4"
            real_src.write_bytes(b"X" * 1234)
            runner = _RecordingRunner()
            hook = JobEndHook(["notify"], source=real_src,
                              workdir=Path("/abs/wd"), runner=runner)
            hs._recorder.attach_job_end_hook(hook)
            hs._recorder.current = {
                "status": "ok",
                "input": {"size_bytes": 999999},  # patched value — wrong
                "output": {"path": str(real_src.with_suffix(".mkv")),
                           "size_bytes": 500},
                "reduction": {"pct_saved": 50.0},
                "settings": {"crf": 21},
            }
            with mock.patch("history.append_record", lambda *_a, **_kw: None):
                hs._recorder.flush()
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_SOURCE_BYTES"], "1234")

    def test_output_path_empty_when_threshold_aborted(self) -> None:
        # init() seeds output.path even before the encode runs. On a
        # threshold abort the file at that path never exists — X265_OUTPUT
        # must be "", not the planned destination.
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        hook = JobEndHook(["notify"], source=Path("/a"), workdir=Path("/wd"),
                          runner=runner)
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {
            "status": "stopped-threshold",
            "input": {"size_bytes": 1},
            "output": {"path": "/wd/never_existed.mkv"},
            "settings": {"crf": 21},
        }
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_OUTPUT"], "")
        self.assertEqual(env["X265_OUTPUT_BYTES_FINAL"], "")

    def test_jsonl_write_runs_before_hook_fires(self) -> None:
        # Audit trail ALWAYS on disk before the hook can hang/fail.
        from unittest import mock
        import encode_modules.history_state as hs

        order: list[str] = []
        runner = _RecordingRunner()

        class _OrderedRunner:
            def __call__(self, *_a, **_kw):
                order.append("hook")
                return types.SimpleNamespace(returncode=0, stderr="")

        hook = JobEndHook(["notify"], source=Path("/a"), workdir=Path("/wd"),
                          runner=_OrderedRunner())
        hs._recorder.attach_job_end_hook(hook)
        hs._recorder.current = {"status": "ok",
                                 "input": {}, "output": {}, "settings": {}}

        def _append(*_a, **_kw):
            order.append("jsonl")
        with mock.patch("history.append_record", _append):
            hs._recorder.flush()
        self.assertEqual(order, ["jsonl", "hook"])


if __name__ == "__main__":
    unittest.main()

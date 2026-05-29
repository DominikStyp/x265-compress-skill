"""FileCompleteHook fires success-only with queue-level counter context.

Companion to JobEndHook (which fires for ANY terminal status with per-job
detail) and to the queue counter-overlay machinery (which run_queue.py
populates per spawn). The hook's role is "the file is on disk, validated,
and ready" — for archival pushes, move-to-done notifications, "queue is N/M
done" status panels.

Two non-negotiables:
1. Fires ONCE, only on status `ok` AND only when the final mkv exists.
2. Counters inherit from os.environ — single-file invocations of compress.py
   still get sensible 1/1 defaults; queue invocations get the real numbers
   set by run_queue.py before spawning the encoder.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.file_complete_hook import FileCompleteHook  # noqa: E402


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
    kw = dict(source=Path("/abs/a.mp4"), workdir=Path("/abs/wd"),
              runner=runner, timeout=30.0)
    kw.update(over)
    return FileCompleteHook(command, **kw)


def _fire(hook, **over):
    kw = dict(
        status="ok",
        output=Path("/abs/wd/out.mkv"),
        output_bytes_final=1_000_000, source_bytes=2_000_000,
        wall_seconds=42.0, pct_saved=50.0,
        crf=21, crf_retry_chain="21",
        vmaf_mean=None,
    )
    kw.update(over)
    return hook.fire(**kw)


class DisabledHookTest(unittest.TestCase):
    def test_none_command_means_disabled(self) -> None:
        hook = _hook(None, _RecordingRunner())
        self.assertFalse(hook.enabled)
        self.assertIsNone(_fire(hook))


class FiresOnlyOnSuccessTest(unittest.TestCase):
    """The whole point of FileCompleteHook vs JobEndHook — success-only,
    so notification scripts don't need to filter status themselves."""

    def test_non_ok_status_does_not_fire(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        for status in ("stopped-threshold", "chunk-failed",
                       "pre-flight-failed", "verify-failed",
                       "stopped-by-user", "awaiting-chunk-fix"):
            self.assertIsNone(_fire(hook, status=status))
        self.assertEqual(runner.calls, [])

    def test_ok_without_output_file_does_not_fire(self) -> None:
        # status=ok but the encoder never produced the final mkv — the
        # contract says "ready for the next step", so don't lie.
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        self.assertIsNone(_fire(hook, status="ok", output=None))
        self.assertEqual(runner.calls, [])


class EnvContractTest(unittest.TestCase):
    def test_per_file_env_vars_present(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        _fire(hook, crf=21, crf_retry_chain="21,22", vmaf_mean=97.45)
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_HOOK_EVENT"], "file-complete")
        self.assertEqual(env["X265_SOURCE"], str(Path("/abs/a.mp4")))
        self.assertEqual(env["X265_OUTPUT"], str(Path("/abs/wd/out.mkv")))
        self.assertEqual(env["X265_CRF"], "21")
        self.assertEqual(env["X265_CRF_RETRY_CHAIN"], "21,22")
        self.assertEqual(env["X265_SOURCE_BYTES"], "2000000")
        self.assertEqual(env["X265_OUTPUT_BYTES"], "1000000")
        self.assertEqual(env["X265_PCT_SAVED"], "50.00")
        self.assertEqual(env["X265_WALL_SECONDS"], "42.00")
        self.assertEqual(env["X265_VMAF_MEAN"], "97.45")

    def test_vmaf_empty_when_not_computed(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        _fire(hook, vmaf_mean=None)
        self.assertEqual(runner.calls[0][1]["env"]["X265_VMAF_MEAN"], "")


class QueueCountersInheritFromEnvTest(unittest.TestCase):
    """run_queue.py sets X265_QUEUE_* on the child encoder's env. They
    inherit straight through to the hook subprocess via os.environ. Single-
    file compress.py runs see no counters → degrade to 1/1/0/0 defaults so
    the same hook script works in both modes without `if "X265_QUEUE_INDEX"
    in os.environ` branches."""

    def _with_env(self, overrides: dict) -> dict:
        saved = {k: os.environ.get(k) for k in overrides}
        os.environ.update({k: str(v) for k, v in overrides.items()})
        return saved

    def _restore_env(self, saved: dict, overrides: dict) -> None:
        for k in overrides:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]

    def test_queue_counters_pass_through_from_env(self) -> None:
        # Overlay values from run_queue.py: FINISHED is EXCLUSIVE (count of
        # past oks), and FileCompleteHook adds 1 because it only fires on
        # success. All other counters pass through verbatim.
        overrides = {
            "X265_QUEUE_INDEX": "3",
            "X265_QUEUE_TOTAL": "8",
            "X265_QUEUE_ITEMS_FINISHED": "2",  # 2 past oks
            "X265_QUEUE_ITEMS_REMAINING": "5",
            "X265_QUEUE_ITEMS_FAILED": "0",
            "X265_QUEUE_ITEMS_STOPPED": "0",
            "X265_QUEUE_ITEMS_SKIPPED": "0",
            "X265_QUEUE_BYTES_IN_SO_FAR": "4500000000",
            "X265_QUEUE_BYTES_OUT_SO_FAR": "3200000000",
            "X265_QUEUE_PCT_SAVED_SO_FAR": "28.9",
            "X265_QUEUE_WALL_SECONDS": "12345.6",
        }
        saved = self._with_env(overrides)
        try:
            runner = _RecordingRunner()
            _fire(_hook(["notify"], runner))
            env = runner.calls[0][1]["env"]
            # FINISHED gets +1 (now-success) → "3 of 8 done" at fire time.
            self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "3")
            for k, v in overrides.items():
                if k == "X265_QUEUE_ITEMS_FINISHED":
                    continue
                self.assertEqual(env[k], str(v),
                                 f"{k} should inherit from os.environ")
        finally:
            self._restore_env(saved, overrides)

    def test_single_file_mode_degrades_to_one_of_one(self) -> None:
        # Strip any queue env first so the test is hermetic.
        keys = ("X265_QUEUE_INDEX", "X265_QUEUE_TOTAL",
                "X265_QUEUE_ITEMS_FINISHED", "X265_QUEUE_ITEMS_REMAINING",
                "X265_QUEUE_ITEMS_FAILED", "X265_QUEUE_ITEMS_STOPPED",
                "X265_QUEUE_ITEMS_SKIPPED", "X265_QUEUE_BYTES_IN_SO_FAR",
                "X265_QUEUE_BYTES_OUT_SO_FAR",
                "X265_QUEUE_PCT_SAVED_SO_FAR", "X265_QUEUE_WALL_SECONDS")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            runner = _RecordingRunner()
            _fire(_hook(["notify"], runner),
                  source_bytes=2_000_000, output_bytes_final=1_000_000,
                  pct_saved=50.0, wall_seconds=42.0)
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_QUEUE_INDEX"], "1")
            self.assertEqual(env["X265_QUEUE_TOTAL"], "1")
            self.assertEqual(env["X265_QUEUE_ITEMS_FINISHED"], "1")
            self.assertEqual(env["X265_QUEUE_ITEMS_REMAINING"], "0")
            self.assertEqual(env["X265_QUEUE_ITEMS_FAILED"], "0")
            self.assertEqual(env["X265_QUEUE_ITEMS_STOPPED"], "0")
            self.assertEqual(env["X265_QUEUE_ITEMS_SKIPPED"], "0")
            # bytes/pct/wall mirror this single file's stats — matches the
            # design note in the request: "single-file mode populates the
            # same env vars with degraded values".
            self.assertEqual(env["X265_QUEUE_BYTES_IN_SO_FAR"], "2000000")
            self.assertEqual(env["X265_QUEUE_BYTES_OUT_SO_FAR"], "1000000")
            self.assertEqual(env["X265_QUEUE_PCT_SAVED_SO_FAR"], "50.00")
            self.assertEqual(env["X265_QUEUE_WALL_SECONDS"], "42.00")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class BestEffortNeverRaisesTest(unittest.TestCase):
    def test_timeout_swallowed(self) -> None:
        runner = _RecordingRunner(raises=subprocess.TimeoutExpired(
            ["slow"], 30))
        msg = _fire(_hook(["slow"], runner))
        self.assertIsNotNone(msg)
        self.assertIn("timed out", msg.lower())

    def test_nonzero_exit_returns_log(self) -> None:
        runner = _RecordingRunner(returncode=2, stderr="boom")
        msg = _fire(_hook(["notify"], runner))
        self.assertIsNotNone(msg)
        self.assertIn("2", msg)

    def test_oserror_swallowed(self) -> None:
        runner = _RecordingRunner(raises=FileNotFoundError("no"))
        msg = _fire(_hook(["notify"], runner))
        self.assertIsNotNone(msg)

    def test_real_subprocess_nul_argv_never_raises(self) -> None:
        hook = FileCompleteHook(["prog\x00x"], source=Path("/a"),
                                workdir=Path("/wd"))
        msg = _fire(hook)
        self.assertIsNotNone(msg)


class RecorderFiresFileCompleteAfterJobEndTest(unittest.TestCase):
    """The file_complete hook fires AFTER the job_end hook so a slow
    file-complete celebration can't delay the job-end alert."""

    def setUp(self) -> None:
        import encode_modules.history_state as hs
        hs._reset_for_tests()

    def test_file_complete_fires_after_job_end_on_ok(self) -> None:
        from unittest import mock
        from encode_modules.job_end_hook import JobEndHook
        import encode_modules.history_state as hs

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "movie.mp4"
            src.write_bytes(b"X" * 100)
            out = src.with_suffix(".mkv")
            out.write_bytes(b"Y" * 50)
            order: list[str] = []

            class Run:
                def __init__(self, label):
                    self.label = label
                def __call__(self, *_a, **_kw):
                    order.append(self.label)
                    return types.SimpleNamespace(returncode=0, stderr="")

            fc_hook = FileCompleteHook(["fc"], source=src, workdir=Path(td),
                                       runner=Run("file_complete"))
            je_hook = JobEndHook(["je"], source=src, workdir=Path(td),
                                 runner=Run("job_end"))
            hs._recorder.attach_job_end_hook(je_hook)
            hs._recorder.attach_file_complete_hook(fc_hook)
            hs._recorder.current = {
                "status": "ok",
                "input": {"size_bytes": 100},
                "output": {"path": str(out), "size_bytes": 50},
                "reduction": {"pct_saved": 50.0},
                "settings": {"crf": 21},
                "wall_seconds": 10.0,
            }
            with mock.patch("history.append_record",
                            lambda *_a, **_kw: None):
                hs._recorder.flush()
            # job_end fires FIRST (rich-status alert), file_complete SECOND
            # (success-only celebration). A slow celebration can't delay the
            # job-end audit/alert.
            self.assertEqual(order, ["job_end", "file_complete"])

    def test_file_complete_skipped_on_non_ok(self) -> None:
        from unittest import mock
        import encode_modules.history_state as hs

        runner = _RecordingRunner()
        fc_hook = FileCompleteHook(["fc"], source=Path("/a"),
                                   workdir=Path("/wd"), runner=runner)
        hs._recorder.attach_file_complete_hook(fc_hook)
        hs._recorder.current = {
            "status": "stopped-threshold",
            "input": {"size_bytes": 1},
            "output": {"path": "/wd/never.mkv"},
            "settings": {"crf": 21},
        }
        with mock.patch("history.append_record", lambda *_a, **_kw: None):
            hs._recorder.flush()
        self.assertEqual(runner.calls, [])


class SidecarCarriesThreeHookKeysTest(unittest.TestCase):
    """write_hooks_sidecar accepts on_file_complete alongside the other two,
    and load_hooks_sidecar returns it. Same back-compat behaviour as
    on_job_end's introduction."""

    def test_write_then_load_all_three(self) -> None:
        from encode_modules.hook_config import (
            load_hooks_sidecar, write_hooks_sidecar)
        with tempfile.TemporaryDirectory() as td:
            path = write_hooks_sidecar(
                Path(td), "v",
                on_chunk_done=["a"], on_job_end=["b"],
                on_file_complete=["c"],
            )
            self.assertEqual(load_hooks_sidecar(path),
                             {"on_chunk_done": ["a"],
                              "on_job_end": ["b"],
                              "on_file_complete": ["c"]})

    def test_load_drops_invalid_file_complete_keeps_others(self) -> None:
        import json
        from encode_modules.hook_config import load_hooks_sidecar
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v.hooks.json"
            path.write_text(json.dumps({
                "on_chunk_done": ["ok"],
                "on_file_complete": ["a\x00b"],  # NUL → invalid
            }), encoding="utf-8")
            self.assertEqual(load_hooks_sidecar(path),
                             {"on_chunk_done": ["ok"]})


if __name__ == "__main__":
    unittest.main()

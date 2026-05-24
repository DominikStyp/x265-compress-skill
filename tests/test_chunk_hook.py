"""ChunkHook fires the user's argv list after a chunk finishes, passing context
via X265_* env vars. The non-negotiable property: fire() is BEST-EFFORT and
NEVER raises — it runs inside the parallel worker, where a raising hook would
trip the choke/needs-fix path or kill a worker slot. Timeouts, missing commands,
and non-zero exits are caught and returned as a log line, not propagated.
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

from encode_modules.chunk_hook import ChunkHook, fire_for_chunk  # noqa: E402


class _RecordingRunner:
    """Stand-in for subprocess.run: records calls, returns a fake completed
    process, or raises a preset exception — so tests never spawn a process."""

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


def _hook(command, runner, *, total=12, timeout=30.0):
    return ChunkHook(command, source=Path("/abs/a.mp4"),
                     workdir=Path("/abs/wd"), total=total,
                     runner=runner, timeout=timeout)


def _fire_ok(hook, **over):
    kw = dict(chunk_name="src_0003.mkv", index=3, status="ok",
              output=Path("/abs/wd/enc_src_0003.mkv"), elapsed_sec=84.214)
    kw.update(over)
    return hook.fire(**kw)


class DisabledHookTest(unittest.TestCase):
    def test_none_command_is_disabled_and_never_runs(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(None, runner)
        self.assertFalse(hook.enabled)
        self.assertIsNone(_fire_ok(hook))
        self.assertEqual(runner.calls, [])


class SuccessFiringTest(unittest.TestCase):
    def test_runs_exact_command_and_returns_none(self) -> None:
        runner = _RecordingRunner(returncode=0)
        hook = _hook(["bash", "/x/notify.sh"], runner)
        self.assertTrue(hook.enabled)
        self.assertIsNone(_fire_ok(hook))
        self.assertEqual(len(runner.calls), 1)
        args, kwargs = runner.calls[0]
        self.assertEqual(args, ["bash", "/x/notify.sh"])
        self.assertEqual(kwargs["timeout"], 30.0)

    def test_env_carries_every_x265_var_as_strings(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner, total=12)
        _fire_ok(hook)
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_HOOK_EVENT"], "chunk-done")
        self.assertEqual(env["X265_CHUNK_STATUS"], "ok")
        self.assertEqual(env["X265_SOURCE"], str(Path("/abs/a.mp4")))
        self.assertEqual(env["X265_WORKDIR"], str(Path("/abs/wd")))
        self.assertEqual(env["X265_CHUNK_NAME"], "src_0003.mkv")
        self.assertEqual(env["X265_CHUNK_INDEX"], "3")
        self.assertEqual(env["X265_CHUNK_TOTAL"], "12")
        self.assertEqual(env["X265_CHUNK_OUTPUT"],
                         str(Path("/abs/wd/enc_src_0003.mkv")))
        self.assertEqual(env["X265_CHUNK_ELAPSED_SEC"], "84.21")
        for k, v in env.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)

    def test_env_inherits_process_environment(self) -> None:
        os.environ["_X265_TEST_INHERIT"] = "yes"
        try:
            runner = _RecordingRunner()
            _fire_ok(_hook(["notify"], runner))
            self.assertEqual(
                runner.calls[0][1]["env"]["_X265_TEST_INHERIT"], "yes")
        finally:
            del os.environ["_X265_TEST_INHERIT"]

    def test_failed_status_blanks_output(self) -> None:
        runner = _RecordingRunner()
        hook = _hook(["notify"], runner)
        hook.fire(chunk_name="src_0003.mkv", index=3, status="failed",
                  output=None, elapsed_sec=0.0)
        env = runner.calls[0][1]["env"]
        self.assertEqual(env["X265_CHUNK_STATUS"], "failed")
        self.assertEqual(env["X265_CHUNK_OUTPUT"], "")


class BestEffortNeverRaisesTest(unittest.TestCase):
    def test_nonzero_exit_returns_message(self) -> None:
        hook = _hook(["notify"], _RecordingRunner(returncode=2, stderr="boom"))
        msg = _fire_ok(hook)
        self.assertIsNotNone(msg)
        self.assertIn("2", msg)
        self.assertIn("src_0003.mkv", msg)

    def test_missing_command_oserror_is_swallowed(self) -> None:
        hook = _hook(["nope"],
                     _RecordingRunner(raises=FileNotFoundError("no nope")))
        msg = _fire_ok(hook)  # must not raise
        self.assertIsNotNone(msg)
        self.assertIn("src_0003.mkv", msg)

    def test_timeout_is_swallowed_and_reported(self) -> None:
        hook = _hook(
            ["slow"],
            _RecordingRunner(raises=subprocess.TimeoutExpired(["slow"], 30)))
        msg = _fire_ok(hook)  # must not raise
        self.assertIsNotNone(msg)
        self.assertIn("timed out", msg.lower())

    def test_real_subprocess_nul_argv_never_raises(self) -> None:
        # No injected runner -> the REAL subprocess.run, which raises ValueError
        # ("embedded null character") on a NUL in the argv. fire() runs in the
        # worker's finally, so it MUST catch this rather than kill the slot.
        # (Every other test injects a fake runner, so this is the only check of
        # what the real subprocess.run actually raises.)
        hook = ChunkHook(["prog\x00x"], source=Path("/a.mp4"),
                         workdir=Path("/wd"), total=1)
        msg = hook.fire(chunk_name="src_0001.mkv", index=1, status="ok",
                        output=None, elapsed_sec=0.0)
        self.assertIsNotNone(msg)


class FireForChunkTest(unittest.TestCase):
    """The shared seam both encoders call. It derives ok/failed + output from
    ground truth (does enc_<stem>.mkv exist?) so success / autofix-success /
    choke / exception are handled identically, and routes failure log lines to
    the encoder's own logger."""

    def _hook(self, runner, workdir):
        return ChunkHook(["notify"], source=Path("/abs/a.mp4"),
                         workdir=workdir, total=5, runner=runner)

    def test_status_ok_with_output_when_enc_file_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0002.mkv"
            (wd / "enc_src_0002.mkv").write_bytes(b"x")  # produced
            runner = _RecordingRunner()
            fire_for_chunk(self._hook(runner, wd), chunk=chunk, workdir=wd,
                           position_of={chunk: 2}, elapsed=12.5,
                           log=[].append)
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_CHUNK_STATUS"], "ok")
            self.assertEqual(env["X265_CHUNK_OUTPUT"],
                             str(wd / "enc_src_0002.mkv"))
            self.assertEqual(env["X265_CHUNK_INDEX"], "2")

    def test_status_failed_blank_output_when_enc_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0002.mkv"  # no enc_*.mkv created
            runner = _RecordingRunner()
            fire_for_chunk(self._hook(runner, wd), chunk=chunk, workdir=wd,
                           position_of={chunk: 2}, elapsed=0.0, log=[].append)
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_CHUNK_STATUS"], "failed")
            self.assertEqual(env["X265_CHUNK_OUTPUT"], "")

    def test_disabled_hook_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            runner = _RecordingRunner()
            hook = ChunkHook(None, source=Path("/a"), workdir=wd, total=1,
                             runner=runner)
            fire_for_chunk(hook, chunk=wd / "c.mkv", workdir=wd,
                           position_of={}, elapsed=0.0, log=[].append)
            self.assertEqual(runner.calls, [])

    def test_none_hook_is_noop_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            fire_for_chunk(None, chunk=wd / "c.mkv", workdir=wd,
                           position_of={}, elapsed=0.0, log=[].append)

    def test_failure_message_routed_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0002.mkv"
            (wd / "enc_src_0002.mkv").write_bytes(b"x")
            runner = _RecordingRunner(returncode=3, stderr="bad")
            logs: list[str] = []
            fire_for_chunk(self._hook(runner, wd), chunk=chunk, workdir=wd,
                           position_of={chunk: 2}, elapsed=1.0,
                           log=logs.append)
            self.assertTrue(logs)
            self.assertIn("3", logs[0])


if __name__ == "__main__":
    unittest.main()

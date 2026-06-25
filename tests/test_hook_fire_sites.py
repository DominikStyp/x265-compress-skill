"""The on_chunk_done hook fires from BOTH encoders, once per finished chunk.

These drive the real fire sites with faked subprocess work:
  * parallel: monkeypatch the per-chunk encode helper so _attempt_chunk's
    `finally` fire runs without spawning ffmpeg.
  * serial:   fake subprocess.Popen so a chunk "encodes" (the .part is created
    then renamed) and probe_duration so no ffprobe runs.

A successful chunk fires ok (output set); a failed chunk fires failed (no
output) — the encode itself must still proceed/exit exactly as before.
"""
from __future__ import annotations

import io
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import encode_modules.encode_parallel as ep  # noqa: E402
import encode_modules.encode_serial as es  # noqa: E402
from encode_modules.chunk_hook import ChunkHook  # noqa: E402
from encode_modules.display import ParallelDisplay  # noqa: E402
from tests._helpers import RecordingRunner as _RecordingRunner  # noqa: E402


def _ctx(display, workdir, chunk, hook):
    return ep._WorkerContext(
        display=display, work_q=queue.Queue(), results=[],
        results_lock=threading.Lock(), workdir=workdir,
        crf=22, preset="slow", pix_fmt="yuv420p10le",
        x265_params="x", x265_params_for_autofix="x", auto_fix_choke=False,
        chunk_hook=hook, position_of={chunk: 3},
    )


class ParallelFireTest(unittest.TestCase):
    def _hook(self, workdir, runner):
        return ChunkHook(["notify"], source=Path("/a.mp4"), workdir=workdir,
                         total=10, runner=runner)

    def test_fires_ok_after_successful_chunk(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0003.mkv"

            def fake_encode(slot, c, workdir, display, **kw):
                (workdir / f"enc_{c.stem}.mkv").write_bytes(b"x")  # produced
                return c, 0, 7.5, ""

            display = ParallelDisplay(parallel=1, total=1, already_done=0)
            runner = _RecordingRunner()
            ctx = _ctx(display, wd, chunk, self._hook(wd, runner))
            with mock.patch.object(ep, "_encode_one_chunk_with_display",
                                   fake_encode):
                ep._attempt_chunk(0, chunk, ctx)
            self.assertEqual(len(runner.calls), 1)
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_CHUNK_STATUS"], "ok")
            self.assertEqual(env["X265_CHUNK_INDEX"], "3")
            self.assertEqual(env["X265_CHUNK_OUTPUT"],
                             str(wd / "enc_src_0003.mkv"))

    def test_fires_failed_after_failed_chunk(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0003.mkv"

            def fake_encode(slot, c, workdir, display, **kw):
                return c, 1, 2.0, "boom"  # no enc_*.mkv produced

            display = ParallelDisplay(parallel=1, total=1, already_done=0)
            runner = _RecordingRunner()
            ctx = _ctx(display, wd, chunk, self._hook(wd, runner))
            with mock.patch.object(ep, "_encode_one_chunk_with_display",
                                   fake_encode):
                ep._attempt_chunk(0, chunk, ctx)
            env = runner.calls[0][1]["env"]
            self.assertEqual(env["X265_CHUNK_STATUS"], "failed")
            self.assertEqual(env["X265_CHUNK_OUTPUT"], "")

    def test_disabled_hook_does_not_fire(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0003.mkv"

            def fake_encode(slot, c, workdir, display, **kw):
                (workdir / f"enc_{c.stem}.mkv").write_bytes(b"x")
                return c, 0, 1.0, ""

            display = ParallelDisplay(parallel=1, total=1, already_done=0)
            runner = _RecordingRunner()
            hook = ChunkHook(None, source=Path("/a.mp4"), workdir=wd, total=1,
                             runner=runner)
            ctx = _ctx(display, wd, chunk, hook)
            with mock.patch.object(ep, "_encode_one_chunk_with_display",
                                   fake_encode):
                ep._attempt_chunk(0, chunk, ctx)
            self.assertEqual(runner.calls, [])


class _FakePopen:
    """Stands in for both the ffmpeg Popen (creates the .part output it's told
    to write) and the progress Popen (no .part token -> no file)."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdout = io.BytesIO()
        self.returncode = _FakePopen.rc
        if _FakePopen.rc == 0:
            for tok in cmd:
                if str(tok).endswith(".part.mkv"):
                    Path(tok).write_bytes(b"x")

    def wait(self):
        return _FakePopen.rc

    def poll(self):
        # The encoder's reap-finally checks poll(); these fakes are only polled
        # post-wait, so report the final exit code (a finished proc, not None).
        return _FakePopen.rc


class SerialFireTest(unittest.TestCase):
    def _run(self, rc: int, runner: _RecordingRunner) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0001.mkv"
            chunk.write_bytes(b"src")
            hook = ChunkHook(["notify"], source=Path("/a.mp4"), workdir=wd,
                             total=1, runner=runner)
            _FakePopen.rc = rc
            with mock.patch.object(es.subprocess, "Popen", _FakePopen), \
                 mock.patch.object(es, "probe_duration", lambda p: 10.0), \
                 mock.patch.object(es, "record_chunk_elapsed",
                                   lambda *a, **k: None):
                es.encode_chunks_serial(
                    [chunk], wd, crf=22, preset="slow",
                    pix_fmt="yuv420p10le", x265_params="x", chunk_hook=hook)

    def test_fires_ok_after_successful_chunk(self) -> None:
        runner = _RecordingRunner()
        self._run(0, runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["env"]["X265_CHUNK_STATUS"], "ok")

    def test_fires_failed_then_exits_on_encode_failure(self) -> None:
        runner = _RecordingRunner()
        with self.assertRaises(SystemExit):
            self._run(1, runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1]["env"]["X265_CHUNK_STATUS"],
                         "failed")


if __name__ == "__main__":
    unittest.main()

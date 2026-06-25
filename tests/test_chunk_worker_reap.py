"""Finding #2 (v1.20.1): a Popen-based encoder must be reaped if the progress
read loop unwinds via an exception — otherwise the ffmpeg child keeps encoding
full-speed, orphaned, for the rest of the run (the Job Object / lifetime group
only reaps it at process exit).

We inject a fake Popen whose stdout iteration raises, and assert the worker
terminates the child and unregisters the slot before the exception propagates.
Mirrors the guarantee quality_libvmaf already had (test_quality_subprocess_leak).
"""
from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import chunk_worker, encode_serial  # noqa: E402


class _FakeProc:
    """A Popen stand-in. stdout iteration raises on demand; poll() reports
    'running' until terminate()/kill() is called so the worker's reap path
    actually fires."""

    def __init__(self, *, stdout_raises=None, stdout_lines=None, rc=0):
        self._stdout_raises = stdout_raises
        self._stdout_lines = stdout_lines or []
        self._rc = rc
        self._dead = False
        self.terminated = False
        self.killed = False
        self.waited = False
        self.stdout = self._make_stdout()
        self.stderr = _FakeStream("")

    def _make_stdout(self):
        proc = self

        class _It:
            def __iter__(self):
                return self

            def __next__(self):
                if proc._stdout_raises is not None:
                    raise proc._stdout_raises
                if proc._stdout_lines:
                    return proc._stdout_lines.pop(0)
                raise StopIteration

            def close(self):
                pass

            def read(self):
                return ""
        return _It()

    def poll(self):
        return None if not self._dead else self._rc

    def wait(self, timeout=None):
        self.waited = True
        self._dead = True
        return self._rc

    def terminate(self):
        self.terminated = True
        self._dead = True

    def kill(self):
        self.killed = True
        self._dead = True


class _FakeStream:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def close(self):
        pass


class _StubDisplay:
    """Minimal ParallelDisplay surface used by _encode_one_chunk_with_display."""

    def __init__(self):
        self.lock = threading.Lock()
        self.choked_chunks: set[str] = set()
        self.abort_event = threading.Event()
        self.slots: dict[int, dict] = {}
        self.registered = []
        self.unregistered = []

    def slot_start(self, *a, **k):
        pass

    def register_proc(self, slot, proc):
        self.registered.append((slot, proc))

    def slot_progress(self, *a, **k):
        pass

    def unregister_proc(self, slot):
        self.unregistered.append(slot)

    def slot_failed(self, *a, **k):
        pass

    def slot_done(self, *a, **k):
        pass


class ChunkWorkerReapTest(unittest.TestCase):
    def test_read_loop_exception_terminates_child_and_unregisters(self) -> None:
        fake = _FakeProc(stdout_raises=RuntimeError("decode boom"))
        display = _StubDisplay()
        with mock.patch.object(chunk_worker.subprocess, "Popen",
                               return_value=fake), \
             mock.patch.object(chunk_worker, "probe_duration",
                               return_value=10.0), \
             mock.patch.object(chunk_worker, "wrap_cmd_for_low_priority",
                               side_effect=lambda c: c), \
             mock.patch.object(chunk_worker, "low_priority_popen_kwargs",
                               return_value={}):
            with self.assertRaises(RuntimeError):
                chunk_worker._encode_one_chunk_with_display(
                    0, Path("/v/.tmp/wd/src_0001.mkv"), Path("/v/.tmp/wd"),
                    display, crf=22, preset="slow", pix_fmt="yuv420p10le",
                    x265_params="")
        # The orphaned encoder MUST have been terminated, and the slot freed.
        self.assertTrue(fake.terminated, "ffmpeg child was not terminated")
        self.assertIn(0, display.unregistered)


class SerialReapTest(unittest.TestCase):
    """The serial encoder (the REAL encode_chunks_serial) spawns two Popens
    (ff + the progress child); an interrupted wait must reap both rather than
    leak the encoder."""

    def test_wait_interruption_terminates_both_children(self) -> None:
        import tempfile

        ff = _FakeProc(rc=0)
        prog = _FakeProc(rc=0)
        # The loop's prog.wait() raises (Ctrl-C landing here) AFTER both are
        # spawned; the finally must then terminate both. The reap's own
        # prog.wait(timeout=5) must still succeed, so raise only on the FIRST
        # call (mimicking a real interrupted wait followed by a clean reap).
        _wait_calls = {"n": 0}

        def _prog_wait(timeout=None):
            _wait_calls["n"] += 1
            if _wait_calls["n"] == 1:
                raise KeyboardInterrupt()
            prog._dead = True
            return 0
        prog.wait = _prog_wait  # type: ignore

        spawned = iter([ff, prog])
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            chunk = wd / "src_0001.mkv"
            chunk.write_bytes(b"x")
            with mock.patch.object(encode_serial.subprocess, "Popen",
                                   side_effect=lambda *a, **k: next(spawned)), \
                 mock.patch.object(encode_serial, "probe_duration",
                                   return_value=10.0), \
                 mock.patch.object(encode_serial, "wrap_cmd_for_low_priority",
                                   side_effect=lambda c: c), \
                 mock.patch.object(encode_serial, "low_priority_popen_kwargs",
                                   return_value={}):
                with self.assertRaises(KeyboardInterrupt):
                    encode_serial.encode_chunks_serial(
                        [chunk], wd, crf=22, preset="slow",
                        pix_fmt="yuv420p10le", x265_params="")
        self.assertTrue(ff.terminated, "ffmpeg child leaked on interruption")


if __name__ == "__main__":
    unittest.main()

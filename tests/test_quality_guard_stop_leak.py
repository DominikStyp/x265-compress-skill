"""Regression: QualityGuard.stop() must not leak the in-flight VMAF ffmpeg.

Root cause: the guard's worker thread runs ``vmaf_pair_fn`` (production
``vmaf_pair`` -> ``_quality_check_run`` with ``low_priority=False``), which
spawns an ffmpeg+libvmaf child that is NOT in the display's lifetime Job
Object / process group. ``_quality_check_run``'s own ``finally`` reaps that
child only when the function returns or raises. But ``stop()`` joins the
worker with a timeout; if a libvmaf pass is mid-flight and exceeds that
timeout, the join returns with the worker still blocked in the read loop and
nobody terminates its ffmpeg -> the child leaks past teardown.

The fix tracks the current in-flight subprocess (registered on spawn, cleared
on reap, under the guard's lock). On a timed-out join, ``stop()`` terminates
-> short wait -> kills the tracked proc, then re-joins briefly.

This test injects a fake ``vmaf_pair_fn`` that registers a fake long-running
proc with the guard and then blocks, so a ``stop(timeout=tiny)`` elapses with
the worker still 'running'. It asserts the in-flight proc gets terminate()d
(and kill()ed after the grace) by stop().
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.quality_guard import QualityGuard  # noqa: E402


class _FakeInflightProc:
    """A VMAF ffmpeg child that never exits on its own (poll() stays None
    until something terminate()s / kill()s it). Mirrors the _FakeProc style
    in test_quality_subprocess_leak.py: poll / terminate / wait / kill."""

    def __init__(self) -> None:
        self.terminated = threading.Event()
        self.killed = threading.Event()
        self._alive = True
        self._lock = threading.Lock()
        # wait(timeout) after terminate() must report the proc as still
        # alive so the guard's teardown escalates to kill() (mirrors a
        # Windows ffmpeg that ignores the first TerminateProcess race).
        self.wait_after_terminate_times_out = True

    def poll(self):
        with self._lock:
            return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated.set()
        # NOTE: stays alive on purpose -- forces the kill() escalation path.

    def wait(self, timeout=None) -> int:
        if self.wait_after_terminate_times_out and not self.killed.is_set():
            import subprocess
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)
        with self._lock:
            self._alive = False
        return 0

    def kill(self) -> None:
        self.killed.set()
        with self._lock:
            self._alive = False


class _BlockingVmafFn:
    """Fake vmaf_pair_fn: registers a never-ending proc with the guard via the
    injected ``register_proc`` seam, signals it has started, then blocks until
    released. Models a libvmaf pass mid-flight when stop() is called."""

    def __init__(self) -> None:
        self.proc = _FakeInflightProc()
        self.started = threading.Event()
        self.release = threading.Event()
        self.registered: list = []

    def __call__(self, src: Path, dst: Path, *, register_proc=None) -> dict | None:
        # The guard hands us a registration callback; publish our in-flight
        # proc so stop() can find and terminate it on a timed-out join.
        if register_proc is not None:
            register_proc(self.proc)
            self.registered.append(self.proc)
        self.started.set()
        try:
            # Block as if reading ffmpeg's -progress stream for minutes.
            self.release.wait(timeout=10.0)
            return {"vmaf_mean": 95.0}
        finally:
            if register_proc is not None:
                register_proc(None)


class StopTerminatesInflightProcTest(unittest.TestCase):
    """stop() with a short timeout, worker still mid-pass -> the tracked
    ffmpeg child must be terminate()d then kill()ed, not leaked."""

    def test_timed_out_stop_terminates_then_kills_inflight_proc(self) -> None:
        events: queue.Queue = queue.Queue()
        fake = _BlockingVmafFn()
        guard = QualityGuard(
            threshold=90.0,
            skip_first_chunk=True,
            vmaf_pair_fn=fake,
            events_queue=events,
            on_abort=lambda info: None,
        )
        # chunk_idx=1 so warmup grace does not skip the (blocking) measurement.
        guard.submit(chunk_idx=1, src=Path("src_0002.mkv"),
                     dst=Path("enc_src_0002.mkv"))
        # Wait until the worker is actually inside the blocking pass.
        self.assertTrue(fake.started.wait(timeout=2.0),
                        "worker never entered the VMAF pass")

        # The pass is still in flight; a small-timeout stop() join WILL elapse
        # with the worker alive. stop() must then reap the tracked proc.
        guard.stop(timeout=0.2)

        self.assertTrue(
            fake.proc.terminated.is_set(),
            "stop() left the in-flight VMAF ffmpeg running (not terminate()d) "
            "after a timed-out join -- the child leaks past teardown")
        self.assertTrue(
            fake.proc.killed.is_set(),
            "stop() terminate()d but never kill()ed the in-flight ffmpeg that "
            "ignored terminate within the grace window")

        # Let the worker unwind so the daemon thread doesn't dangle.
        fake.release.set()

    def test_stop_with_no_inflight_proc_is_unaffected(self) -> None:
        # No submission -> worker idle on the queue, no proc registered.
        # stop() must still be the quick, clean path (no terminate/kill of a
        # nonexistent proc, no exception).
        events: queue.Queue = queue.Queue()
        fake = _BlockingVmafFn()
        guard = QualityGuard(
            threshold=90.0,
            skip_first_chunk=True,
            vmaf_pair_fn=fake,
            events_queue=events,
            on_abort=lambda info: None,
        )
        start = time.monotonic()
        guard.stop(timeout=1.0)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(fake.proc.terminated.is_set())
        self.assertFalse(fake.proc.killed.is_set())


class RegisterSeamWiringTest(unittest.TestCase):
    """Verify the production runner is detected + driven by the seam, and that
    a legacy bare-signature runner falls back to the no-register path."""

    def test_production_vmaf_pair_is_detected_as_register_capable(self) -> None:
        from encode_modules.quality_libvmaf import vmaf_pair
        self.assertTrue(
            QualityGuard._fn_accepts_register(vmaf_pair),
            "vmaf_pair must advertise register_proc so the guard can reap it")

    def test_bare_signature_fn_is_not_register_capable(self) -> None:
        def legacy(src, dst):  # noqa: ANN001 — mirrors old fakes
            return None
        self.assertFalse(QualityGuard._fn_accepts_register(legacy))

    def test_kwargs_fn_is_register_capable(self) -> None:
        def flexible(src, dst, **kw):  # noqa: ANN001
            return None
        self.assertTrue(QualityGuard._fn_accepts_register(flexible))

    def test_vmaf_pair_registers_then_clears_proc(self) -> None:
        """End-to-end through real vmaf_pair (Popen faked): it must call
        register_proc(proc) after spawn and register_proc(None) on reap."""
        from unittest import mock

        from encode_modules import quality_libvmaf

        seen: list = []

        class _QuickProc:
            def __init__(self) -> None:
                self.stdout = iter(())  # empty progress stream -> loop ends
                self.stderr = None
                self._alive = True

            def poll(self):
                return None if self._alive else 0

            def wait(self, timeout=None) -> int:
                self._alive = False
                return 0

            def terminate(self) -> None:
                self._alive = False

            def kill(self) -> None:
                self._alive = False

        proc = _QuickProc()
        with mock.patch.object(quality_libvmaf, "probe_fps",
                               return_value="30/1"), \
             mock.patch.object(quality_libvmaf.subprocess, "Popen",
                               return_value=proc), \
             mock.patch.object(quality_libvmaf, "read_ffmpeg_progress",
                               lambda *a, **k: None), \
             mock.patch.object(quality_libvmaf, "_parse_vmaf_log",
                               return_value={"vmaf_mean": 95.0}):
            quality_libvmaf.vmaf_pair(
                Path("src.mkv"), Path("dst.mkv"),
                register_proc=seen.append,
            )
        # First register is the live proc, last is the None clear in finally.
        self.assertEqual(seen[0], proc,
                         "vmaf_pair must register the live proc after spawn")
        self.assertIsNone(seen[-1],
                          "vmaf_pair must clear the registration in finally")


if __name__ == "__main__":
    unittest.main()

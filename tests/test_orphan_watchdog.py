"""BUG 2 regression: ffmpeg orphaned when the orchestrator dies on POSIX.

Win32 Job Objects kill assigned children in-kernel when the parent dies, for
ANY reason (clean exit, Ctrl+C, taskkill /F). POSIX has no equivalent: the
orchestrator's atexit/SIGTERM cleanup covers graceful exits, but closing the
terminal (SIGHUP) or a hard `kill -9` (SIGKILL) bypasses it, leaving the chunk
ffmpeg processes running with ppid==1 and nothing to concat/verify them.

Two defenses are tested here:
  - A clock-/signal level: the lifetime group now also reaps on SIGHUP/SIGQUIT.
  - A sidecar watchdog process that reaps the tracked process-groups when the
    orchestrator dies for any reason (incl. SIGKILL), detected via getppid().

The pure helpers (`kill_groups`, `run`) and the lifetime-group wiring are tested
with injected fakes so they run on every OS; the genuine orphan-reaping behaviour
(real processes, real getppid reparenting, real killpg) is exercised by a
POSIX-only end-to-end test.
"""
from __future__ import annotations

import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from platform_compat import _posix  # noqa: E402
from platform_compat import _posix_watchdog as wd  # noqa: E402


class _RecordingStdin:
    """A stand-in for the watchdog's stdin pipe that records what the
    orchestrator writes and rejects writes after close (like a real pipe)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, s: str) -> None:
        if self.closed:
            raise ValueError("write to closed stream")
        self.lines.append(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeWatchdog:
    def __init__(self) -> None:
        self.stdin = _RecordingStdin()

    def poll(self):
        return None


class KillGroupsTest(unittest.TestCase):
    def test_sigterm_then_sigkill_each_group(self) -> None:
        calls: list[tuple[int, int]] = []
        slept: list[float] = []
        wd.kill_groups(
            [101, 202],
            killpg=lambda pg, sig: calls.append((pg, sig)),
            sleep=slept.append,
        )
        # SIGTERM to both first, one grace sleep, then SIGKILL to both.
        self.assertEqual(calls[0], (101, wd._SIGTERM))
        self.assertEqual(calls[1], (202, wd._SIGTERM))
        self.assertEqual(slept, [wd._GRACE_SECONDS])
        self.assertIn((101, wd._SIGKILL), calls)
        self.assertIn((202, wd._SIGKILL), calls)

    def test_dead_group_is_skipped_for_sigkill(self) -> None:
        # A group whose SIGTERM fails (already gone) must NOT be SIGKILLed.
        def killpg(pg, sig):
            if pg == 999:
                raise ProcessLookupError
        calls: list[tuple[int, int]] = []

        def tracking_killpg(pg, sig):
            killpg(pg, sig)
            calls.append((pg, sig))

        wd.kill_groups([999, 7], killpg=tracking_killpg, sleep=lambda s: None)
        self.assertNotIn((999, wd._SIGKILL), calls)
        self.assertIn((7, wd._SIGKILL), calls)

    def test_empty_does_not_sleep_or_kill(self) -> None:
        slept: list[float] = []
        wd.kill_groups([], killpg=lambda pg, sig: None, sleep=slept.append)
        self.assertEqual(slept, [])


class _ScriptedStdin:
    """Yields queued lines from readline(); '' means EOF."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


class WatchdogRunTest(unittest.TestCase):
    def test_reaps_tracked_groups_when_parent_dies(self) -> None:
        # getppid returns the original parent until the 3rd call, then changes
        # (orchestrator died -> reparented).
        ppids = iter([1000, 1000, 4321, 4321])
        stdin = _ScriptedStdin(["55\n", "66\n"])
        # select reports the stdin readable while lines remain.
        remaining = {"n": 2}

        def fake_select(rl, wl, xl, timeout):
            if remaining["n"] > 0:
                remaining["n"] -= 1
                return (rl, [], [])
            return ([], [], [])

        reaped: list[list[int]] = []
        with mock.patch.object(wd, "kill_groups",
                               lambda pgids, **k: reaped.append(sorted(pgids))):
            rc = wd.run(1000, stdin, getppid=lambda: next(ppids),
                        poll_seconds=0.0, select_fn=fake_select)
        self.assertEqual(rc, 2)
        self.assertEqual(reaped, [[55, 66]])

    def test_quit_sentinel_returns_without_reaping(self) -> None:
        stdin = _ScriptedStdin([f"{wd.QUIT_SENTINEL}\n"])

        def fake_select(rl, wl, xl, timeout):
            return (rl, [], [])

        with mock.patch.object(wd, "kill_groups",
                               side_effect=AssertionError("must not reap")):
            rc = wd.run(1000, stdin, getppid=lambda: 1000,
                        poll_seconds=0.0, select_fn=fake_select)
        self.assertEqual(rc, 0)


class LifetimeGroupWatchdogTest(unittest.TestCase):
    def _make_group(self):
        fake = _FakeWatchdog()
        killed: list[tuple[int, int]] = []
        group = _posix._LifetimeGroup(
            spawn_watchdog=lambda: fake,
            killpg=lambda pg, sig: killed.append((pg, sig)),
        )
        # The group registers an atexit _cleanup; clear its tracked pids at test
        # end so that handler is a no-op (no real 1 s grace-sleep at exit).
        self.addCleanup(group.pids.clear)
        return group, fake, killed

    def test_add_streams_pgid_to_watchdog(self) -> None:
        group, fake, _ = self._make_group()
        group.add(1234)
        self.assertIn("1234\n", fake.stdin.lines)

    def test_cleanup_reaps_groups_and_quits_watchdog(self) -> None:
        group, fake, killed = self._make_group()
        group.add(1234)
        with mock.patch.object(wd.time, "sleep", lambda s: None):
            group._cleanup()
        # The tracked group was reaped (SIGTERM at least)...
        self.assertIn((1234, wd._SIGTERM), killed)
        # ...and the watchdog was told to stand down (no double-reap on exit).
        self.assertIn(f"{wd.QUIT_SENTINEL}\n", fake.stdin.lines)

    def test_cleanup_is_idempotent(self) -> None:
        group, fake, killed = self._make_group()
        group.add(1234)
        with mock.patch.object(wd.time, "sleep", lambda s: None):
            group._cleanup()
            group._cleanup()  # second call must not raise (closed pipe etc.)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"),
                         "SIGHUP only exists on POSIX")
    def test_sighup_handler_installed(self) -> None:
        group, _, _ = self._make_group()
        handler = signal.getsignal(signal.SIGHUP)
        self.assertTrue(callable(handler),
                        "SIGHUP must be handled so terminal-close reaps ffmpeg")
        self.assertNotIn(handler, (signal.SIG_DFL, signal.SIG_IGN))


@unittest.skipIf(sys.platform == "win32",
                 "orphan reaping uses POSIX getppid reparenting + killpg")
class WatchdogEndToEndTest(unittest.TestCase):
    def test_real_watchdog_reaps_orphaned_process_on_parent_death(self) -> None:
        import os
        import subprocess
        import textwrap
        import time as _t

        py = sys.executable
        script = str(Path(_posix.__file__).resolve().parent / "_posix_watchdog.py")
        # A real victim in its own session, so its pid == its pgid and it is
        # only killable via killpg by something that knows that pgid.
        sleeper = subprocess.Popen([py, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True)
        pgid = sleeper.pid
        try:
            launcher = textwrap.dedent(f"""
                import os, subprocess, time
                w = subprocess.Popen(
                    [{py!r}, {script!r}, str(os.getpid())],
                    stdin=subprocess.PIPE, text=True, start_new_session=True)
                w.stdin.write("{pgid}\\n"); w.stdin.flush()
                time.sleep(0.5)   # then exit WITHOUT quitting -> simulated death
            """)
            subprocess.run([py, "-c", launcher], timeout=15)
            deadline = _t.time() + 10
            while _t.time() < deadline and sleeper.poll() is None:
                _t.sleep(0.2)
            self.assertIsNotNone(
                sleeper.poll(),
                "watchdog should have reaped the orphaned process group")
        finally:
            if sleeper.poll() is None:
                sleeper.kill()


if __name__ == "__main__":
    unittest.main()

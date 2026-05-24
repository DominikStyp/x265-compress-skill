"""POSIX backend for platform_compat. Covers macOS and Linux.

Almost everything maps to standard POSIX primitives:
  - Subprocess priority: `nice -n 19` cmd wrapper + start_new_session=True.
                         Intentionally NOT preexec_fn — that's flagged as
                         unsafe with threads in Python's docs and macOS is
                         particularly sensitive to fork+threads.
  - Suspend / resume:    SIGSTOP / SIGCONT
  - ANSI:                native — no-op
  - Lifetime tracking:   process group + atexit/signal handlers
  - Keyboard input:      termios cbreak + select.select on stdin

The one weak spot is lifetime tracking. Win32 Job Objects are kernel-managed
and survive SIGKILL of the parent — there is no exact POSIX equivalent.
Linux has prctl(PR_SET_PDEATHSIG), but it doesn't work for grandchildren
and macOS doesn't have prctl at all. We use a process-group + atexit/
SIGTERM handler approach that handles graceful shutdowns + most signal-
driven exits. A hard SIGKILL of the parent will still orphan children —
documented gap.
"""
from __future__ import annotations

import atexit
import os
import select
import signal
import subprocess
import sys
from typing import Callable, Optional

from ._posix_watchdog import QUIT_SENTINEL, kill_groups


# --- Subprocess priority ----------------------------------------------------

def low_priority_popen_kwargs() -> dict:
    """Returns subprocess.Popen kwargs paired with `wrap_cmd_for_low_priority`.

    `start_new_session=True` puts the child in its own session+pgid (via
    setsid in posix_spawn) so the parent can clean it up with killpg. This
    AVOIDS preexec_fn — Python's docs flag preexec_fn as unsafe in the
    presence of threads (and the encoder is heavily multi-threaded:
    display, workers, keyboard listener). macOS specifically is sensitive
    to fork+threads, where the child can inherit locked state from the
    parent. start_new_session goes through posix_spawn instead, which is
    thread-safe.

    The nice-19 priority is applied by `wrap_cmd_for_low_priority` (which
    prepends a `nice -n 19` to the cmd) — also no preexec_fn involved."""
    return {"start_new_session": True}


def wrap_cmd_for_low_priority(cmd: list) -> list:
    """Prepend `nice -n 19` so the child runs at idle CPU priority.

    `nice` is a POSIX standard utility — always available on macOS and
    every Linux distro. Wrapping at the command level (rather than calling
    os.nice in preexec_fn) sidesteps the thread-safety issue documented
    on `low_priority_popen_kwargs`.

    Returns a new list; doesn't mutate the input. If `nice` is somehow
    unavailable, the wrapped command will fail with ENOENT and the caller
    will see the error — better than silently running at normal priority."""
    return ["nice", "-n", "19", *cmd]


# --- Process suspend/resume -------------------------------------------------

def suspend_pid(pid: int) -> bool:
    """SIGSTOP — pause without losing state. Preserves the target's full
    RAM state (x265 lookahead, reference frames, rate-control); resume
    picks up at the exact same frame."""
    try:
        os.kill(int(pid), signal.SIGSTOP)
        return True
    except (OSError, ProcessLookupError):
        return False


def resume_pid(pid: int) -> bool:
    """SIGCONT — counterpart to suspend_pid."""
    try:
        os.kill(int(pid), signal.SIGCONT)
        return True
    except (OSError, ProcessLookupError):
        return False


# --- ANSI escape support ---------------------------------------------------

def enable_ansi() -> None:
    """No-op on POSIX. Modern terminals (Terminal.app, iTerm2, xterm, etc.)
    support ANSI escape sequences natively without per-handle setup."""
    return None


# --- Lifetime tracking (process group + atexit/signal handlers) -------------

class _LifetimeGroup:
    """Tracks child PIDs so they can be killed when the parent exits.

    Each registered PID is the leader of its own process group (start_new_session
    in the spawn kwargs). On parent exit — graceful, SIGTERM, SIGHUP (terminal
    close), or SIGQUIT — we send SIGTERM to each pgid (kills the child + any
    sub-children it spawned), then SIGKILL survivors after a grace period.

    The one case no in-process handler can cover is SIGKILL (`kill -9`) of the
    parent, which skips atexit AND every signal handler. For that we spawn a
    sidecar watchdog process (see _posix_watchdog) that reaps the tracked groups
    when it observes the orchestrator die. Together these give parity with the
    Win32 Job Object's kernel-enforced cleanup. SIGINT is intentionally left
    alone so Python's normal KeyboardInterrupt still drives the encoder's
    resume_all() path."""

    def __init__(self, *,
                 spawn_watchdog: Optional[Callable[[], Optional[object]]] = None,
                 killpg: Optional[Callable[[int, int], None]] = None) -> None:
        self.pids: set[int] = set()
        self._killpg = killpg
        self._watchdog = (spawn_watchdog or self._spawn_watchdog)()
        atexit.register(self._cleanup)
        self._install_signal_handlers()

    def _spawn_watchdog(self) -> Optional[object]:
        """Launch the sidecar watchdog as a fresh interpreter (posix_spawn —
        thread-safe, unlike os.fork in this multi-threaded process). Best-effort:
        on any failure we degrade to atexit/signal cleanup only (covers
        everything except parent SIGKILL), never breaking the encode."""
        script = os.path.join(os.path.dirname(__file__), "_posix_watchdog.py")
        try:
            return subprocess.Popen(
                [sys.executable, script, str(os.getpid())],
                stdin=subprocess.PIPE, text=True,
                # Own session so a terminal SIGHUP doesn't kill the watchdog
                # before it can act — it must outlive the orchestrator briefly.
                start_new_session=True,
            )
        except (OSError, ValueError):
            # Spawn failed (e.g. sandboxed, no exec). The watchdog is a backstop
            # for parent-SIGKILL only; degrade to atexit/signal cleanup rather
            # than ever breaking the encode over it.
            return None

    def _install_signal_handlers(self) -> None:
        """Chain a cleanup handler in front of the existing handler for each
        termination signal we can catch. SIGHUP (terminal/window close) and
        SIGQUIT are added because closing the launching terminal otherwise
        bypasses cleanup and orphans the ffmpeg children. getattr-guarded so the
        module still imports on Windows (no SIGHUP/SIGQUIT there)."""
        for signame in ("SIGTERM", "SIGHUP", "SIGQUIT"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            prev = signal.getsignal(sig)

            def handler(signum, frame, _prev=prev):
                self._cleanup()
                if callable(_prev):
                    _prev(signum, frame)
                else:
                    sys.exit(128 + signum)

            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # ValueError if not main thread; OSError on some sandboxed envs.
                pass

    def add(self, pid: int) -> None:
        # Must be called while the orchestrator is live (before teardown begins)
        # — workers register their ffmpeg here as they spawn it, never during
        # _cleanup. pgids accumulate across a multi-file queue run and are never
        # pruned; that's intentional (pruning would need liveness tracking the
        # design avoids), and reaping an already-dead pgid is a harmless no-op.
        self.pids.add(int(pid))
        # Stream the pgid to the watchdog so it can reap this group if we die
        # without running cleanup (parent SIGKILL). Best-effort.
        self._tell_watchdog(f"{int(pid)}\n")

    def _tell_watchdog(self, message: str) -> None:
        wd = self._watchdog
        stdin = getattr(wd, "stdin", None) if wd is not None else None
        if stdin is None:
            return
        try:
            stdin.write(message)
            stdin.flush()
        except (OSError, ValueError):
            # Watchdog gone or pipe closed — fall back to in-process cleanup.
            pass

    def _cleanup(self) -> None:
        """Reap every tracked process group (SIGTERM, grace, SIGKILL), then
        stand the watchdog down. Idempotent — safe to call multiple times
        (atexit + signal handler may both fire)."""
        pids = list(self.pids)
        self.pids.clear()
        kill_groups(pids, killpg=self._killpg)
        # Tell the watchdog to exit without re-reaping (groups already gone),
        # then close the pipe. If we were SIGKILLed this never runs and the
        # watchdog reaps via getppid instead.
        self._tell_watchdog(f"{QUIT_SENTINEL}\n")
        wd = self._watchdog
        stdin = getattr(wd, "stdin", None) if wd is not None else None
        if stdin is not None:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass


_singleton_group: Optional[_LifetimeGroup] = None


def create_lifetime_group() -> Optional[_LifetimeGroup]:
    """Lazily build the singleton lifetime group + register cleanup hooks.
    Returns the same instance on subsequent calls so multiple parallel
    encoders share one cleanup path."""
    global _singleton_group
    if _singleton_group is None:
        try:
            _singleton_group = _LifetimeGroup()
        except Exception:
            return None
    return _singleton_group


def assign_to_lifetime_group(pid: int, group: Optional[_LifetimeGroup]) -> bool:
    """Track a PID under the lifetime group. Returns True on success.

    The child must have been started with `low_priority_popen_kwargs()`
    (or otherwise called `os.setpgrp()` in its spawn-prep hook) so the
    PID is the leader of its own process group — that's what killpg targets."""
    if group is None:
        return False
    group.add(pid)
    return True


# --- Keyboard input (termios cbreak + select.select) ------------------------

# Interactive pause/resume needs a real terminal. HAS_KEY_INPUT goes False —
# the Space/1-9/r keys silently disappear while the encode itself runs on
# unaffected — whenever stdin isn't a tty (piped, nohup, systemd, or a queue
# runner with stdin redirected) or termios/tty is unavailable (e.g. TERM=dumb
# on some stripped-down builds, or a non-POSIX stdin).
try:
    import termios  # type: ignore
    import tty      # type: ignore
    HAS_KEY_INPUT = sys.stdin.isatty()
except (ImportError, AttributeError):
    HAS_KEY_INPUT = False


_saved_termios = None


def _enter_cbreak_mode() -> None:
    """Switch stdin into cbreak so we can read single chars without enter.
    Idempotent — second call is a no-op."""
    global _saved_termios
    if _saved_termios is not None or not HAS_KEY_INPUT:
        return
    try:
        fd = sys.stdin.fileno()
        _saved_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:
        _saved_termios = None


def _restore_termios() -> None:
    """Atexit hook — put the terminal back the way we found it."""
    global _saved_termios
    if _saved_termios is None:
        return
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _saved_termios)
    except Exception:
        pass
    _saved_termios = None


if HAS_KEY_INPUT:
    atexit.register(_restore_termios)


def read_key_byte(timeout_s: float) -> Optional[bytes]:
    """Wait up to `timeout_s` for one keypress. Returns the raw byte
    (e.g. b'A', b'\\x1b') or None on timeout. The caller is responsible
    for assembling multi-byte sequences like ESC [ A.

    First call lazily enters cbreak mode + queues a restore hook. We don't
    enter cbreak at import time because the script may run in non-
    interactive mode (e.g. inside a queue runner with stdin redirected).
    """
    if not HAS_KEY_INPUT:
        return None
    if _saved_termios is None:
        _enter_cbreak_mode()
    if _saved_termios is None:
        return None  # Couldn't enter cbreak (e.g. not a real TTY).
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if not ready:
            return None
        ch = os.read(sys.stdin.fileno(), 1)
        return ch or None
    except (OSError, IOError, ValueError):
        return None

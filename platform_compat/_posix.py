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
import sys
import time
from typing import Optional


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

    Each registered PID is the leader of its own process group (set up
    in the spawn-prep hook via os.setpgrp). On parent exit (graceful or
    SIGTERM) we send SIGTERM to each pgid — kills the child + any sub-
    children it spawned. After a brief grace period unhandled survivors
    get SIGKILL.

    Known gap: SIGKILL of the parent (kill -9) skips the handlers and
    will orphan children. No POSIX equivalent of Win32 Job Object's
    kernel-enforced cleanup. The render thread's resume_all() on
    KeyboardInterrupt covers the common Ctrl+C path."""

    def __init__(self) -> None:
        self.pids: set[int] = set()
        atexit.register(self._cleanup)
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        """Chain a cleanup handler in front of the existing SIGTERM
        handler. SIGINT is intentionally left alone so Python's normal
        KeyboardInterrupt mechanics still drive the resume_all() path
        in the encoder."""
        prev_term = signal.getsignal(signal.SIGTERM)

        def term_handler(signum, frame):
            self._cleanup()
            if callable(prev_term):
                prev_term(signum, frame)
            else:
                sys.exit(128 + signum)

        try:
            signal.signal(signal.SIGTERM, term_handler)
        except (ValueError, OSError):
            # ValueError if not main thread; OSError on some sandboxed envs.
            pass

    def add(self, pid: int) -> None:
        self.pids.add(int(pid))

    def _cleanup(self) -> None:
        """Send SIGTERM to every tracked process group, then SIGKILL the
        survivors after 1 s grace. Idempotent — safe to call multiple
        times (atexit + signal handler may both fire)."""
        survivors: list[int] = []
        for pid in list(self.pids):
            try:
                os.killpg(pid, signal.SIGTERM)
                survivors.append(pid)
            except (OSError, ProcessLookupError):
                pass
            self.pids.discard(pid)
        if survivors:
            # Give well-behaved children a moment to flush + exit.
            time.sleep(1.0)
            for pid in survivors:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
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

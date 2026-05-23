"""OS-portable primitives. The ONE module that decides which backend to use.

The rest of the codebase imports from `platform_compat` and never knows
whether the underlying implementation is Win32 or POSIX. Adding a new
platform means adding a new `_<name>.py` module that provides the same
public symbols and dispatching to it here.

Exported names (all backends provide them):

  Detection flags
      IS_WINDOWS, IS_MACOS, IS_LINUX, IS_POSIX

  Subprocess priority — use the two helpers TOGETHER:
      wrap_cmd_for_low_priority(cmd) -> list
          Win32: returns cmd unchanged (priority is set via creationflags).
          POSIX: returns ['nice', '-n', '19'] + cmd. Avoids preexec_fn so
                 we sidestep Python's "preexec_fn is not safe with threads"
                 caveat (especially relevant on macOS).
      low_priority_popen_kwargs() -> dict
          Win32: {creationflags: IDLE_PRIORITY_CLASS}
          POSIX: {start_new_session: True}  -- new pgid so killpg works.

      Idiomatic spawn:
          subprocess.Popen(
              wrap_cmd_for_low_priority(cmd),
              **low_priority_popen_kwargs(),
              ...,
          )

  Process suspend/resume (htop-style pause)
      suspend_pid(pid) -> bool
      resume_pid(pid) -> bool

  Terminal
      enable_ansi() -> None
          Win32: enables VT processing on the current stdout handle.
          POSIX: no-op (modern terminals support ANSI natively).

  Lifetime tracking — children die with parent
      create_lifetime_group() -> opaque handle (or None on failure)
      assign_to_lifetime_group(pid, group) -> bool
          Win32: a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
                 Bulletproof — survives taskkill /F and BSOD.
          POSIX: a process-group + atexit/SIGTERM handler. Covers graceful
                 shutdown + most signal-driven exits. SIGKILL of the parent
                 still orphans children (no POSIX equivalent of Win32's
                 kernel-managed Job Object — known gap, documented).

  Keyboard input — single-char non-blocking reads for htop-style controls
      HAS_KEY_INPUT: bool          False if input source isn't a TTY.
      read_key_byte(timeout) -> bytes | None
                                   Returns one byte (b'A') or None on timeout.
"""
from __future__ import annotations

import sys


IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_POSIX = IS_MACOS or IS_LINUX


if IS_WINDOWS:
    from . import _windows as _backend
elif IS_POSIX:
    from . import _posix as _backend
else:
    raise RuntimeError(
        f"Unsupported platform: {sys.platform!r}. Supported: win32, darwin, linux."
    )


# Re-export the backend's API. Every backend module provides these exact names.
low_priority_popen_kwargs = _backend.low_priority_popen_kwargs
wrap_cmd_for_low_priority = _backend.wrap_cmd_for_low_priority
suspend_pid = _backend.suspend_pid
resume_pid = _backend.resume_pid
enable_ansi = _backend.enable_ansi
create_lifetime_group = _backend.create_lifetime_group
assign_to_lifetime_group = _backend.assign_to_lifetime_group
HAS_KEY_INPUT = _backend.HAS_KEY_INPUT
read_key_byte = _backend.read_key_byte


def os_name() -> str:
    """Short human-readable OS name for diagnostic output."""
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    if IS_LINUX:
        return "Linux"
    return sys.platform

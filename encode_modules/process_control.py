"""Windows process-control primitives shared across the encode pipeline.

Three responsibilities — all Win32, all best-effort on other platforms:

  1. **Suspend / resume PIDs** via NtSuspendProcess / NtResumeProcess. Used by
     the htop-style pause/resume of individual encode slots. Preserves the
     target's full RAM state (x265 lookahead, reference frames, rate-control)
     bit-for-bit — resume picks up at the exact same frame.

  2. **IDLE_PRIORITY_FLAGS** — a creationflags value applied to every ffmpeg
     spawn so the encoder only gets CPU when foreground apps (browser, editor,
     video playback) don't want it. Lower than `start /low`.

  3. **Job Object kill-on-close** — every ffmpeg child is AssignProcessToJob'd
     to a job created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. When this Python
     process exits for ANY reason (clean, Ctrl+C, taskkill /F, BSOD), the
     kernel TerminateProcess's every assigned ffmpeg the moment our last
     handle to the job closes. Eliminates the orphaned-paused-ffmpeg problem.

Plus a tiny helper that enables ANSI virtual-terminal mode on the current
stdout handle so the live progress block's escape codes render correctly in
cmd.exe.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys


_PROCESS_SUSPEND_RESUME = 0x0800


def suspend_pid(pid: int) -> bool:
    """Win32 NtSuspendProcess — freeze a process and all its threads.
    Returns True on success. No-op on non-Windows."""
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
        if not h:
            return False
        try:
            return ctypes.windll.ntdll.NtSuspendProcess(h) == 0
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def resume_pid(pid: int) -> bool:
    """Win32 NtResumeProcess — counterpart to suspend_pid."""
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
        if not h:
            return False
        try:
            return ctypes.windll.ntdll.NtResumeProcess(h) == 0
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# Idle CPU priority for every ffmpeg child. Encoding shares the laptop with
# interactive apps (browser, editor, video playback). At IDLE_PRIORITY_CLASS
# (Windows scheduler priority 4), the kernel only schedules ffmpeg when no
# NORMAL/HIGH process wants CPU — foreground work always wins, even under
# full load. Lower than `start /low` (which is BELOW_NORMAL, priority 6).
# Non-Windows: evaluates to 0 (no-op). The script is Windows-first anyway.
IDLE_PRIORITY_FLAGS = (
    subprocess.IDLE_PRIORITY_CLASS if sys.platform == "win32" else 0
)


# Job Object constants & structs — match Win32 headers exactly.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def create_kill_on_close_job():
    """Create a Windows Job Object with the KILL_ON_JOB_CLOSE limit set.
    Every process AssignProcessToJobObject'd into it is automatically
    TerminateProcess'd by the kernel when the last handle to the job closes
    — which happens when this Python process dies, for any reason.

    Returns the job handle (an integer). Caller must keep the handle alive
    for the lifetime of the protection; closing it kills the children. The
    handle is auto-closed by the OS when the parent process exits, which is
    exactly the trigger we want. Returns None on non-Windows / API failure."""
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.windll.kernel32
        # CreateJobObjectW returns HANDLE (treat as ctypes.c_void_p so we
        # get the full pointer width on 64-bit Python).
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        hjob = k32.CreateJobObjectW(None, None)
        if not hjob:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        ok = k32.SetInformationJobObject(
            hjob, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            k32.CloseHandle(hjob)
            return None
        return hjob
    except Exception:
        return None


def assign_pid_to_job(pid: int, hjob) -> bool:
    """AssignProcessToJobObject the target PID into our kill-on-close job."""
    if sys.platform != "win32" or not hjob:
        return False
    try:
        k32 = ctypes.windll.kernel32
        access = _PROCESS_TERMINATE | _PROCESS_SET_QUOTA
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        h = k32.OpenProcess(access, False, int(pid))
        if not h:
            return False
        try:
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            return bool(k32.AssignProcessToJobObject(hjob, h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def enable_windows_ansi() -> None:
    """Cmd.exe on Win10+ supports ANSI escapes, but virtual terminal processing
    must be turned on for the current stdout handle. We do it via the kernel32
    SetConsoleMode call directly so we never shell out."""
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass  # If this fails, ANSI escapes appear as raw text — degraded but not fatal.

"""Windows backend for platform_compat. Win32 only — imported only on
sys.platform == 'win32'.

Heavy use of ctypes for direct kernel32/ntdll calls. We never shell out
for any of this work — the dependencies are kept minimal so the skill
boots without external installs."""
from __future__ import annotations

import ctypes
import subprocess
import time
from typing import Optional


# --- Subprocess priority ----------------------------------------------------

# IDLE_PRIORITY_CLASS = Windows scheduler priority 4. The kernel only runs
# ffmpeg when no NORMAL/HIGH process wants CPU — foreground work (browser,
# editor, video playback) always wins, even under full encode load. Lower
# than `start /low` (which is BELOW_NORMAL, priority 6).
_IDLE_PRIORITY_FLAGS = subprocess.IDLE_PRIORITY_CLASS


def low_priority_popen_kwargs() -> dict:
    """Subprocess.Popen kwargs that lower the child's CPU priority. Pass
    via ``**low_priority_popen_kwargs()`` so the call site doesn't have to
    know which kwarg is platform-specific."""
    return {"creationflags": _IDLE_PRIORITY_FLAGS}


# --- Process suspend/resume -------------------------------------------------

_PROCESS_SUSPEND_RESUME = 0x0800


def suspend_pid(pid: int) -> bool:
    """NtSuspendProcess — freeze a process and all its threads in place.
    Preserves the target's full RAM state (x265 lookahead, reference
    frames, rate-control) bit-for-bit; resume picks up at the exact same
    frame. Returns True on success."""
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
    """NtResumeProcess — counterpart to suspend_pid."""
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


# --- ANSI escape support ---------------------------------------------------

def enable_ansi() -> None:
    """Enable ANSI virtual-terminal processing on the current stdout handle.
    Cmd.exe on Win10+ supports ANSI, but VT mode must be turned on per-handle.
    Failures are silent — ANSI codes appearing as raw text is degraded but
    not fatal."""
    try:
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


# --- Lifetime tracking (Job Object) ----------------------------------------

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


def create_lifetime_group() -> Optional[int]:
    """Create a Win32 Job Object with the KILL_ON_JOB_CLOSE limit set.
    Every process AssignProcessToJobObject'd into it is automatically
    TerminateProcess'd by the kernel when the last handle to the job
    closes — which happens when this Python process dies, for any reason
    (clean exit, Ctrl+C, taskkill /F, BSOD).

    Returns the job handle (an integer). None on API failure."""
    try:
        k32 = ctypes.windll.kernel32
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


def assign_to_lifetime_group(pid: int, group) -> bool:
    """AssignProcessToJobObject the target PID into our kill-on-close job."""
    if not group:
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
            k32.AssignProcessToJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p]
            return bool(k32.AssignProcessToJobObject(group, h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# --- Keyboard input (msvcrt) ------------------------------------------------

try:
    import msvcrt  # type: ignore
    HAS_KEY_INPUT = True
except ImportError:
    HAS_KEY_INPUT = False


def read_key_byte(timeout_s: float) -> Optional[bytes]:
    """Wait up to `timeout_s` for one keypress. Returns the raw byte
    (e.g. b'A', b'\\x1b', b'\\xe0') or None on timeout. The caller is
    responsible for assembling multi-byte sequences."""
    if not HAS_KEY_INPUT:
        return None
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return msvcrt.getch()
            time.sleep(0.002)
    except Exception:
        return None
    return None

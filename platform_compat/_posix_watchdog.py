"""Sidecar watchdog: reap orphaned ffmpeg if the POSIX orchestrator dies.

Win32 Job Objects kill assigned children in-kernel when the parent dies, for
ANY reason — clean exit, Ctrl+C, taskkill /F (see platform_compat/_windows.py).
POSIX has no equivalent. The orchestrator's atexit/signal cleanup
(`_posix._LifetimeGroup`) covers graceful exits and SIGTERM/SIGHUP/SIGQUIT, but
a hard `kill -9` (SIGKILL) bypasses every handler and leaves the chunk ffmpeg
processes running — reparented to init/launchd — burning CPU with nothing left
to concat/verify the output.

This module runs as a tiny SEPARATE process spawned by `_LifetimeGroup`. The
orchestrator streams the process-group ids of its ffmpeg children to this
process's stdin (one decimal int per line) as it spawns them. The watchdog
polls `os.getppid()`: while the orchestrator lives it stays the watchdog's
parent; the instant the orchestrator dies the watchdog is reparented (getppid
changes), and it SIGTERMs then SIGKILLs every group it was told about, then
exits. This closes the SIGKILL gap that no signal handler can.

Graceful path: the orchestrator's own cleanup sends the "QUIT" sentinel (after
it has already reaped the groups), so a normal exit neither double-reaps nor
leaves this watchdog lingering.

It is spawned as a fresh interpreter (posix_spawn) rather than via os.fork on
purpose: the orchestrator is heavily multi-threaded and forking a threaded
process is unsafe on macOS (see _posix.low_priority_popen_kwargs).
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Callable, Iterable, Optional

QUIT_SENTINEL = "QUIT"
_GRACE_SECONDS = 1.0

# Bound via getattr so this module imports on Windows (which has SIGTERM but no
# SIGKILL) — the reap logic itself only ever runs on POSIX, but unit tests
# import and drive it with a fake killpg on every OS. 15/9 are the universal
# POSIX signal numbers.
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)


def kill_groups(pgids: Iterable[int], *,
                killpg: Optional[Callable[[int, int], None]] = None,
                sleep: Callable[[float], None] = time.sleep) -> None:
    """SIGTERM every process group, then SIGKILL the survivors after a short
    grace. Shared by both reap paths (`_LifetimeGroup._cleanup` and this
    watchdog) so they behave identically.

    `killpg` defaults to os.killpg, bound lazily so this module imports cleanly
    on Windows (os.killpg is POSIX-only) for unit tests that inject a fake."""
    killpg = killpg or os.killpg
    sent: list[int] = []
    for pg in (int(p) for p in pgids):
        try:
            killpg(pg, _SIGTERM)
            sent.append(pg)
        except (OSError, ProcessLookupError):
            # Group already gone — nothing to SIGKILL later.
            pass
    if sent:
        # Give well-behaved encoders a moment to flush and exit on SIGTERM
        # before the hard kill.
        sleep(_GRACE_SECONDS)
        for pg in sent:
            try:
                killpg(pg, _SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def run(orig_ppid: int, stdin, *,
        getppid: Optional[Callable[[], int]] = None,
        poll_seconds: float = 1.0,
        select_fn: Optional[Callable] = None) -> int:
    """Watch `orig_ppid`. Learn process-group ids from `stdin` (one int per
    line) while the orchestrator is alive; the moment it dies
    (`getppid() != orig_ppid`) reap every learned group and return the count.
    A "QUIT" line returns 0 WITHOUT reaping — the graceful-shutdown signal from
    an orchestrator that has already cleaned up its own children.

    Detection latency is bounded by `poll_seconds` (default 1 s) plus the reap
    grace. The pgid-reuse window (a tracked pgid recycled by the OS between the
    orchestrator's death and the reap, then wrongly killed) is the same accepted
    risk as _LifetimeGroup._cleanup and is negligible given the pid space vs the
    sub-second window."""
    getppid = getppid or os.getppid
    if select_fn is None:
        import select as _select
        select_fn = _select.select

    pgids: set[int] = set()
    stdin_open = True
    while True:
        if stdin_open:
            try:
                ready, _, _ = select_fn([stdin], [], [], poll_seconds)
            except (OSError, ValueError):
                ready, stdin_open = [], False
            if ready:
                line = stdin.readline()
                if line == "":
                    stdin_open = False          # write-end closed (EOF)
                else:
                    token = line.strip()
                    if token == QUIT_SENTINEL:
                        return 0                # graceful: do not reap
                    try:
                        pgids.add(int(token))
                    except ValueError:
                        pass                    # ignore garbage lines
                    continue                    # drain queued lines promptly
        else:
            time.sleep(poll_seconds)
        # Authoritative death signal: a child reparents (getppid changes) the
        # instant its parent dies, even on SIGKILL where no handler can run.
        if getppid() != orig_ppid:
            break
    kill_groups(pgids)
    return len(pgids)


if __name__ == "__main__":
    run(int(sys.argv[1]), sys.stdin)

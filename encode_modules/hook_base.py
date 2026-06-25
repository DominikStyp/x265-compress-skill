"""Shared execution core for the four notification hooks.

Before this module the four hook classes — ``ChunkHook`` (chunk-done),
``JobEndHook`` (job-end), ``FileCompleteHook`` (file-complete) and
``queue_modules.QueueItemEndHook`` (queue-item-end) — each carried a
byte-for-byte copy of the same ``fire()`` body: build env → run the user's
argv with ``shell=False`` + a timeout → catch the no-raise exception band →
slice the stderr tail → record the durable outcome → return a one-line
display string. That meant the load-bearing **no-raise catch band** (the one
the AGENTS.md "broad-except only at guard seams" rule cares about) lived in
four places and had to be kept identical by hand.

``run_hook_command`` is that body, extracted once. Each hook now owns only its
*distinct* ``_build_env`` and its event constants; ``fire()`` builds the env
overrides and delegates here. The per-hook variation is captured by two
parameters:

  * ``hook_name`` — the ``on_*`` config key. Used BOTH as the durable-log
    ``event`` field and as the human label in the live-display string, exactly
    as the originals did (they were always equal).
  * ``log_suffix`` — appended right after the core of every message (e.g.
    ``" (src_0003.mkv)"`` for ChunkHook, ``""`` for the others). Placed before
    the ``": <tail>"`` / ``": <type>: <e>"`` continuation so the rendered
    strings stay byte-identical to the pre-refactor versions.

``source`` is passed per call (not bound) so the same core serves both the
source-bound hooks and ``QueueItemEndHook`` (which only knows the source at
fire time).

NO-RAISE CONTRACT: this function never raises. It runs inside the parallel
worker's ``finally`` (ChunkHook), the history-flush/atexit path (JobEnd /
FileComplete) and the queue's per-job loop (QueueItemEnd) — an escaping
exception there would trip the choke path, crash the audit flush, or abort the
queue. The catch band ``(OSError, ValueError, SubprocessError)`` is exhaustive
for ``subprocess.run`` by construction (``ValueError`` = embedded NUL in argv
or undecodable stderr under ``text=True``); ``TimeoutExpired`` is handled
separately so the message can name the timeout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .hook_logging import record_hook_outcome

# Stderr-tail caps, named once instead of repeated as bare slice literals in
# every hook. The durable log keeps more context (HTTP error bodies like the
# Pushbullet 400 JSON run long); the live one-liner stays terminal-friendly.
STDERR_LOG_TAIL = 500   # chars persisted to logs/<source>.hooks.log
STDERR_MSG_TAIL = 200   # chars shown in the returned live-display string


def env_str(value: object) -> str:
    """Stringify an env-var value: ``None`` becomes ``""`` (never the literal
    ``"None"``) so hook scripts can read ``os.environ[var]`` without KeyError
    AND detect "absent" via an empty-string check."""
    return "" if value is None else str(value)


def env_float(value: Optional[float]) -> str:
    """Format a float env-var value to 2 dp, ``None`` → ``""``. Matches the
    ``X265_*`` numeric convention (e.g. ``X265_PCT_SAVED`` = ``"35.05"``)."""
    return "" if value is None else f"{value:.2f}"


def env_int(name: str, *, default: int) -> int:
    """Read an int env var with a clean fallback. Empty / non-numeric → the
    default, so a malformed overlay value never crashes a hook's no-raise
    contract."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def run_hook_command(*, command: Optional[Sequence[str]],
                     env_overrides: Mapping[str, str],
                     timeout: float,
                     runner: Callable[..., object],
                     event_log: Callable[..., object],
                     source: Optional[Path],
                     hook_name: str,
                     log_suffix: str = "") -> Optional[str]:
    """Run one configured hook command, no-raise. Returns a one-line display
    string on failure/timeout/non-zero exit, else ``None``. Records the
    outcome to the durable hook log via ``event_log`` on EVERY path (incl.
    success). See the module docstring for the parameter contract.

    ``command is None`` (hook disabled) is a defensive no-op; callers normally
    short-circuit before building ``env_overrides`` so disabled hooks do no
    work at all."""
    if command is None:
        return None
    env = dict(os.environ)
    env.update(env_overrides)

    def _log(outcome: str, stderr_tail: str = "") -> None:
        event_log(source=source, event=hook_name, command=command,
                  outcome=outcome, stderr_tail=stderr_tail)

    try:
        proc = runner(command, env=env, timeout=timeout,
                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                      text=True)
    except subprocess.TimeoutExpired:
        _log("timeout")
        return f"  ! {hook_name} hook timed out after {timeout:g}s{log_suffix}"
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        _log("spawn-error", f"{type(e).__name__}: {e}")
        return (f"  ! {hook_name} hook failed{log_suffix}: "
                f"{type(e).__name__}: {e}")
    rc = getattr(proc, "returncode", 0)
    if rc:
        tail = (getattr(proc, "stderr", "") or "").strip().replace("\n", " ")
        _log(f"exited {rc}", tail[-STDERR_LOG_TAIL:])
        return (f"  ! {hook_name} hook exited {rc}{log_suffix}"
                + (f": {tail[-STDERR_MSG_TAIL:]}" if tail else ""))
    _log("ok")
    return None


# Re-exported so existing imports of record_hook_outcome via the hook modules
# keep working and the four hooks share one default.
__all__ = [
    "STDERR_LOG_TAIL", "STDERR_MSG_TAIL",
    "env_str", "env_float", "env_int",
    "run_hook_command", "record_hook_outcome",
]

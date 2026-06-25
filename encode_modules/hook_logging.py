"""Durable, append-only logging of every hook FIRE outcome (v1.20.0).

The three encode hooks (``ChunkHook``, ``JobEndHook``, ``FileCompleteHook``)
already capture each hook command's exit code + a stderr tail and return it as
a one-line string for LIVE terminal display. Pre-v1.20.0 that string was never
written anywhere, so a webhook failure (Pushbullet ``pushbullet_pro_required``
400, DNS/TLS error, timeout, non-zero exit, or an un-spawnable script) scrolled
off the terminal and was lost — diagnosing one required reproducing the push by
hand. This module persists ONE structured JSONL line per fire to
``logs/<source-stem>.hooks.log`` so the evidence survives.

Two hard rules, both checked by tests:

  * **Secret-free.** The line carries the timestamp, event, argv (command
    list), outcome, and — on failure — the stderr tail. It NEVER carries the
    environment: ``PUSHBULLET_TOKEN`` / ``NTFY_TOKEN`` ride in env and must not
    land in a log.
  * **Never raises.** ``fire()`` is no-raise by contract (it runs in the
    parallel worker's ``finally`` and in the history-flush/atexit path); a
    logging failure must not change that. Every error here is swallowed and
    surfaced only as a ``None`` return.

It also refuses to write when the source's parent directory does not already
exist, so a unit test binding a hook to ``/abs/a.mp4`` does not materialize a
stray ``logs/`` tree — real encodes always have a real source folder.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from .log_paths import hooks_log_path


def now_iso_utc() -> str:
    """UTC ISO-8601 timestamp, matching ``history.now_iso_utc`` so the hook
    log and the encoding history bucket by the same wall-clock convention."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_hook_outcome(*, source: Optional[Path], event: str,
                        command: Optional[Sequence[str]],
                        outcome: str, stderr_tail: str = "",
                        now_fn: Callable[[], str] = now_iso_utc,
                        log_path_fn: Callable[[Path], Path] = hooks_log_path,
                        ) -> Optional[Path]:
    """Append one JSONL line describing a single hook fire. Returns the log
    path on a successful write, else ``None`` (no-op or swallowed error).
    NEVER raises.

    ``stderr_tail`` is included only when non-empty (so ``ok`` lines stay
    lean). The append is plain ``open(..., "a")`` — matching the project's
    history-JSONL writer — because a single ``write`` of one short line is the
    failure-recording path itself; a temp-file+rename dance here would add a
    second thing that can fail while recording that something failed."""
    try:
        if source is None:
            return None
        src = Path(source)
        if not src.parent.is_dir():
            return None
        path = log_path_fn(src)
        record = {
            "ts": now_fn(),
            "event": event,
            "command": list(command) if command else [],
            "outcome": outcome,
        }
        if stderr_tail:
            record["stderr_tail"] = stderr_tail
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return path
    except (OSError, ValueError, TypeError):
        # Swallow every failure mode: unwritable dir, undecodable command,
        # a now_fn/log_path_fn that raised. Recording must never break encode.
        return None

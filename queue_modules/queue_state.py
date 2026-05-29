"""Persistent queue state — `<queue_stem>.state.json` sidecar next to queue.json.

Records which queue jobs have already completed successfully (and where their
files ended up if `done_dir` moved them). Without this, an `ok` job whose
source was moved to `done_dir` reappears next run as
`skipped-not-found` — which clutters the run log AND bumps the queue's
aggregate exit code to 2 (`_ATTENTION_STATUSES`). The state file lets the
runner say "already done, skip silently" with status `skipped-done` instead.

Schema:
  {"schema_version": 1,
   "queue_file": "queue.json",
   "completed": [
       {"input_original": "/v/a.mp4",
        "output_original": "/v/a.mkv",
        "moved_to_dir":    "/v/done",        # optional
        "input_final":     "/v/done/a.mp4",  # optional
        "output_final":    "/v/done/a.mkv",  # optional
        "crf_final": 23, "bytes_in": ..., "bytes_out": ...,
        "wall_seconds": ..., "completed_utc": "..."},
       ...]
  }

Why a sidecar instead of editing queue.json:
  * queue.json is user-authored config; mutating it on disk surprises the
    user and breaks VCS diff workflows.
  * Separates intent (config) from progress (state) — the standard pattern.
  * Atomic write doesn't risk truncating the user's queue.

Atomic writes via temp + os.replace — same pattern as hook_config.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 1


def state_path_for(queue_path: Path) -> Path:
    """Sidecar path for a given queue file: `<queue_stem>.state.json` in the
    same directory. Same convention the existing reports use."""
    return queue_path.with_name(f"{queue_path.stem}.state.json")


@dataclass
class QueueState:
    """In-memory queue state. `completed` is a dict keyed by absolute
    resolved input path → record dict; the list-shape in the on-disk schema
    is denormalized to a dict in memory for O(1) lookups."""
    schema_version: int = SCHEMA_VERSION
    completed: dict[str, dict] = field(default_factory=dict)

    def is_completed(self, input_path: Path) -> bool:
        """True iff the given source has a completion record. Resolves the
        path so callers can pass either resolved or unresolved Paths."""
        return _key(input_path) in self.completed

    def get(self, input_path: Path) -> Optional[dict]:
        """Return the completion record for `input_path`, or None."""
        return self.completed.get(_key(input_path))

    def add_completed(self, *,
                      input_original: Path,
                      output_original: Optional[Path] = None,
                      moved_to_dir: Optional[Path] = None,
                      input_final: Optional[Path] = None,
                      output_final: Optional[Path] = None,
                      crf_final: Optional[int] = None,
                      bytes_in: Optional[int] = None,
                      bytes_out: Optional[int] = None,
                      wall_seconds: Optional[float] = None,
                      completed_utc: Optional[str] = None) -> None:
        """Mark `input_original` as completed. Fields that aren't provided
        are omitted from the on-disk record (rather than serialized as
        null) so foreign tooling reading the file gets a tidy shape."""
        record: dict = {"input_original": str(input_original)}
        for k, v in (
            ("output_original", output_original),
            ("moved_to_dir", moved_to_dir),
            ("input_final", input_final),
            ("output_final", output_final),
        ):
            if v is not None:
                record[k] = str(v)
        for k, v in (
            ("crf_final", crf_final),
            ("bytes_in", bytes_in),
            ("bytes_out", bytes_out),
            ("wall_seconds", wall_seconds),
            ("completed_utc", completed_utc),
        ):
            if v is not None:
                record[k] = v
        self.completed[_key(input_original)] = record

    def save_atomically(self, queue_path: Path) -> Path:
        """Write the state to its sidecar (temp + os.replace). Returns the
        path. Re-creates the parent if needed (defensive — usually exists
        because the queue.json itself does)."""
        dst = state_path_for(queue_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "queue_file": queue_path.name,
            "completed": list(self.completed.values()),
        }
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, dst)
        return dst


def load_queue_state(queue_path: Path) -> QueueState:
    """Read the state sidecar, returning an empty state if missing /
    unreadable / corrupt / on an unknown schema version. Degrades — never
    raises — so a queue run can't be killed by a state-file glitch.

    Schema-version-mismatch is treated as "unknown" rather than auto-
    upgraded: a future version may store fields this version doesn't
    understand, and silently dropping them risks losing audit trail. The
    user can `--reset-state` if they really want a fresh start."""
    sidecar = state_path_for(queue_path)
    if not sidecar.exists():
        return QueueState()
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return QueueState()
    if not isinstance(data, dict):
        return QueueState()
    if data.get("schema_version") != SCHEMA_VERSION:
        return QueueState()
    state = QueueState()
    for rec in data.get("completed", []) or []:
        if not isinstance(rec, dict):
            continue
        input_original = rec.get("input_original")
        if not isinstance(input_original, str) or not input_original:
            continue
        state.completed[_key(Path(input_original))] = rec
    return state


def delete_queue_state(queue_path: Path) -> None:
    """`--reset-state` implementation: silently drop the sidecar. No-op if
    missing. OSError on a permissions/locked-file issue is intentionally
    NOT swallowed — the user asked to reset, so a silent failure would be
    misleading."""
    sidecar = state_path_for(queue_path)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        return


def _key(input_path: Path) -> str:
    """Canonical key for the completed-dict — absolute resolved path string.
    Path.resolve() is case-folded by NTFS on Windows but we don't depend on
    that here: str equality of the resolved path is what matters."""
    try:
        return str(input_path.resolve())
    except OSError:
        return str(input_path)

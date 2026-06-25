"""Single source of truth for where every log / sidecar artefact lives
(v1.19.0 layout).

Before v1.19.0, log-style files were scattered across three locations:

  * ``<video_folder>/.tmp/<output_stem>.{quality.json,chunk_metrics.jsonl,
    report.md,hooks.json}`` — per-encode sidecars under the workdir's
    parent ``.tmp/``.
  * ``<video_folder>/<source.name>.preflight.json`` — preflight cache
    next to the user-supplied source.
  * ``<queue_folder>/<queue_stem>.state.json`` next to ``queue.json``;
    ``<queue_folder>/.tmp/<queue_stem>_report.md`` for the aggregate
    queue report.
  * ``<history_root>/encoding_history.jsonl`` — the global append-only
    history log.

v1.19.0 routes EVERY log into a ``logs/`` subdirectory at the appropriate
level. ``logs_dir(folder)`` returns the location; the dedicated helpers
below build the per-artefact paths so writers, readers, and the migration
helper share one canonical computation.

Migration: ``migrate_video_folder`` / ``migrate_queue_folder`` /
``migrate_history_root`` walk the legacy locations once and ``shutil.move``
each artefact into the new ``logs/``. They are idempotent (the new path
already existing → skip + leave the legacy in place for the user to
audit), safe under concurrent callers (encoder + queue runner can both
fire migration at startup), and best-effort (an ``OSError`` per file is
swallowed; the rest of the migration continues).

User overrides always win: ``CLAUDE_ENCODING_HISTORY_PATH`` (history) and
the explicit ``--json-status PATH`` flag (queue NDJSON) are honoured
verbatim — only the *default* moves into ``logs/``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


LOGS_DIRNAME = "logs"


def logs_dir(folder: Path) -> Path:
    """The logs/ subdir under the given folder. mkdir is NOT called here —
    writers create it lazily so a read-only inspection never materializes
    an empty directory."""
    return folder / LOGS_DIRNAME


def quality_sidecar_path(output: Path) -> Path:
    """Per-encode VMAF sidecar. Keyed on output stem (the encoded file)."""
    return logs_dir(output.parent) / f"{output.stem}.quality.json"


def chunk_metrics_path(output: Path) -> Path:
    """Per-chunk metrics log for the encode. Keyed on output stem."""
    return logs_dir(output.parent) / f"{output.stem}.chunk_metrics.jsonl"


def per_encode_report_path(output: Path) -> Path:
    """Per-encode markdown report. Keyed on output stem."""
    return logs_dir(output.parent) / f"{output.stem}.report.md"


def hooks_sidecar_path(source: Path) -> Path:
    """Per-job hooks sidecar — keyed on SOURCE name (not output stem) so
    .mkv sources don't drift to ``<name>.x265.hooks.json`` (the bug fixed
    in done_dir test_hooks_sidecar_deleted_for_mkv_source). Note: keyed
    on the source *stem*, matching script_writer's convention."""
    return logs_dir(source.parent) / f"{source.stem}.hooks.json"


def hooks_log_path(source: Path) -> Path:
    """Durable, append-only log of every hook FIRE outcome (one JSONL line
    per fire). Sibling of ``hooks_sidecar_path`` — keyed on the SOURCE stem so
    a job's hook config (``<stem>.hooks.json``) and its hook event log
    (``<stem>.hooks.log``) sit next to each other under ``logs/``. Added in
    v1.20.0 so a webhook failure (Pushbullet 400, DNS error, timeout, non-zero
    exit, un-spawnable script) is recorded instead of scrolling off the
    terminal unrecorded."""
    return logs_dir(source.parent) / f"{source.stem}.hooks.log"


def preflight_cache_path(source: Path) -> Path:
    """Preflight cache. Keyed on the source's FULL name (incl. extension)
    so two sources sharing a stem but differing only in extension
    (``foo.mp4`` + ``foo.mkv``) get distinct caches — same key used pre-
    v1.19.0 by ``src.with_suffix(src.suffix + ".preflight.json")``."""
    return logs_dir(source.parent) / f"{source.name}.preflight.json"


def queue_report_path(queue_path: Path) -> Path:
    """Aggregate queue report. Stem of the queue file + ``_report.md``."""
    return logs_dir(queue_path.parent) / f"{queue_path.stem}_report.md"


def queue_report_history_path(queue_path: Path) -> Path:
    """Queue report's history sidecar (carries prior job dicts so
    incremental re-aggregation across resumed runs works)."""
    return logs_dir(queue_path.parent) / f"{queue_path.stem}_report.history.json"


def queue_state_path(queue_path: Path) -> Path:
    """Queue runner state sidecar (completed + in-progress escalations)."""
    return logs_dir(queue_path.parent) / f"{queue_path.stem}.state.json"


def queue_json_status_default_path(queue_path: Path) -> Path:
    """Default location for the queue's --json-status NDJSON when the user
    didn't specify a path. An explicit ``--json-status PATH`` flag is
    honoured verbatim; this is only the default."""
    return logs_dir(queue_path.parent) / f"{queue_path.stem}.json-status.ndjson"


def history_jsonl_path(history_root: Path) -> Path:
    """Encoding history JSONL location under a given history root. The
    ``CLAUDE_ENCODING_HISTORY_PATH`` env var still overrides — see
    ``history.default_history_path``."""
    return logs_dir(history_root) / "encoding_history.jsonl"


# ----------------------------------------------------------------------
# Migration helpers
# ----------------------------------------------------------------------

# Per-encode sidecar file suffixes that live under ``.tmp/`` pre-v1.19.0.
# Listed as suffixes (not stems) so we sweep every file matching ``*.X``.
_VIDEO_TMP_SUFFIXES = (
    ".quality.json",
    ".chunk_metrics.jsonl",
    ".report.md",
    ".hooks.json",
)

# Queue artefacts that live in ``.tmp/`` (reports) pre-v1.19.0.
_QUEUE_TMP_SUFFIXES = (
    "_report.md",
    "_report.history.json",
)


def _move_into_logs(legacy: Path, logs_target_dir: Path) -> Path | None:
    """Move ``legacy`` into ``logs_target_dir``. Returns the new path on
    success, None on any refusal (target already exists) or OSError.

    Race-safe vs concurrent callers (two encoder processes running
    ``migrate_for_encode_run`` against the same folder, the
    ``parallel: 2`` queue case Dominik runs in production):

      1. Try the rename via a hand-rolled atomic create:
         ``os.link(legacy, new_path)`` + ``os.unlink(legacy)``. ``os.link``
         FAILS LOUDLY with ``FileExistsError`` when ``new_path`` already
         exists — the kernel guarantees one winner. The loser's link
         attempt raises, we swallow it, the loser leaves the legacy file
         in place (the user can audit).
      2. ``os.link`` doesn't work cross-volume — fall back to
         ``shutil.copy2`` + ``os.unlink`` only after a SECOND
         existence-check. Cross-volume migrations are rare (logs/ usually
         sits on the same mount as the source), so the small remaining
         race window there is acceptable.

    Creates ``logs_target_dir`` lazily — only when there's at least one
    file to actually move."""
    new_path = logs_target_dir / legacy.name
    if new_path.exists():
        return None
    try:
        logs_target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    # Step 1: try the atomic hard-link path. EXDEV → cross-volume → fall
    # back below.
    try:
        os.link(str(legacy), str(new_path))
    except FileExistsError:
        # A concurrent caller won the race. The legacy file is still
        # ours to delete, but only AFTER we re-confirm new_path is
        # populated (defensive: a partial cross-link could in theory leave
        # both sides empty). Keep the legacy file in place; the next
        # migration run will pick it up if new_path was somehow torn.
        return None
    except OSError as e:
        # EXDEV / EPERM / file-system-doesn't-support-hardlinks → fall
        # through to the cross-volume copy+unlink path.
        if not _is_crossdev_or_perm(e):
            return None
        try:
            if new_path.exists():
                # Another process beat us to it in this small window.
                return None
            shutil.copy2(str(legacy), str(new_path))
            os.unlink(str(legacy))
            return new_path
        except OSError:
            # Best-effort: clean up a partial copy if we wrote one.
            try:
                if new_path.exists() and not legacy.exists():
                    pass  # rare: copy succeeded, unlink failed — leave duplicate
                elif new_path.exists():
                    new_path.unlink()
            except OSError:
                pass
            return None
    # link() succeeded — unlink the legacy entry. If unlink fails (rare,
    # AV lock), the new path is intact; the duplicate's worst case is the
    # next migration sees an already-existing target and refuses.
    try:
        os.unlink(str(legacy))
    except OSError:
        pass
    return new_path


def _is_crossdev_or_perm(e: OSError) -> bool:
    """Decide whether an ``os.link`` failure indicates we should fall back
    to the cross-volume copy path. EXDEV is the canonical cross-device
    error; EPERM appears on filesystems that don't support hardlinks
    (FAT32, some network mounts, macOS APFS in rare configurations)."""
    import errno
    return e.errno in (errno.EXDEV, errno.EPERM, errno.ENOSYS)


def migrate_video_folder(video_folder: Path) -> list[Path]:
    """Move every per-encode sidecar in ``<video_folder>/.tmp/`` and every
    ``<source.name>.preflight.json`` next to a source into
    ``<video_folder>/logs/``. Idempotent, best-effort, safe under
    concurrent callers. Returns the list of NEW paths that were actually
    written this call (empty = no-op).

    Does NOT touch:
      * the chunked workdir (``.tmp/.compress_<stem>/``) — that's the
        encoder's mid-flight scratch, wiped by ``cleanup(workdir)`` on
        success.
      * the generated ``.bat`` / ``.sh`` script — still in ``.tmp/``.
      * source video files. The data-safety invariant: NEVER move /
        rename / delete a source. Only the user does that."""
    if not video_folder.is_dir():
        return []
    target = logs_dir(video_folder)
    moved: list[Path] = []

    # Sweep .tmp/ for known sidecar suffixes.
    tmp = video_folder / ".tmp"
    if tmp.is_dir():
        for legacy in sorted(tmp.iterdir()):
            if not legacy.is_file():
                continue
            if not any(legacy.name.endswith(suf)
                       for suf in _VIDEO_TMP_SUFFIXES):
                continue
            new_path = _move_into_logs(legacy, target)
            if new_path is not None:
                moved.append(new_path)

    # Sweep video_folder root for ``*.preflight.json`` (next-to-source).
    for legacy in sorted(video_folder.iterdir()):
        if not legacy.is_file():
            continue
        if not legacy.name.endswith(".preflight.json"):
            continue
        new_path = _move_into_logs(legacy, target)
        if new_path is not None:
            moved.append(new_path)

    return moved


def migrate_queue_folder(queue_path: Path) -> list[Path]:
    """Move the queue's per-run artefacts into
    ``<queue_folder>/logs/``. Idempotent, best-effort. Returns the list
    of NEW paths actually written this call.

    Migrates:
      * ``<queue_folder>/<queue_stem>.state.json``
      * ``<queue_folder>/.tmp/<queue_stem>_report.md``
      * ``<queue_folder>/.tmp/<queue_stem>_report.history.json``"""
    queue_folder = queue_path.parent
    if not queue_folder.is_dir():
        return []
    target = logs_dir(queue_folder)
    moved: list[Path] = []

    # State sidecar at the queue's level.
    legacy_state = queue_folder / f"{queue_path.stem}.state.json"
    if legacy_state.is_file():
        new_path = _move_into_logs(legacy_state, target)
        if new_path is not None:
            moved.append(new_path)

    # Queue report files under .tmp/.
    tmp = queue_folder / ".tmp"
    if tmp.is_dir():
        for suffix in _QUEUE_TMP_SUFFIXES:
            legacy = tmp / f"{queue_path.stem}{suffix}"
            if legacy.is_file():
                new_path = _move_into_logs(legacy, target)
                if new_path is not None:
                    moved.append(new_path)

    return moved


def migrate_for_queue_run(queue_path: Path,
                           history_root: Path) -> list[Path]:
    """Convenience wrapper run_queue.main calls at startup: migrate the
    queue's state + report sidecars AND the default encoding history
    JSONL in one shot. Returns the combined list of files moved.

    Centralized here so neither run_queue.py nor encode_resumable.py
    needs its own wrapper function (both were pushing modules past the
    500-line cap)."""
    moved: list[Path] = []
    try:
        moved.extend(migrate_queue_folder(queue_path))
    except OSError:
        pass
    try:
        moved.extend(migrate_history_root(history_root))
    except OSError:
        pass
    return moved


def migrate_for_encode_run(video_folder: Path,
                            history_root: Path) -> list[Path]:
    """Convenience wrapper encode_resumable.main calls at startup: video-
    folder sidecars + the default encoding history JSONL."""
    moved: list[Path] = []
    try:
        moved.extend(migrate_video_folder(video_folder))
    except OSError:
        pass
    try:
        moved.extend(migrate_history_root(history_root))
    except OSError:
        pass
    return moved


def migrate_history_root(history_root: Path) -> list[Path]:
    """Move ``<history_root>/encoding_history.jsonl`` into
    ``<history_root>/logs/encoding_history.jsonl``. Idempotent,
    best-effort. Returns the new path in a single-element list, or empty
    on no-op / failure."""
    if not history_root.is_dir():
        return []
    legacy = history_root / "encoding_history.jsonl"
    if not legacy.is_file():
        return []
    new_path = _move_into_logs(legacy, logs_dir(history_root))
    return [new_path] if new_path is not None else []

"""Defense-in-depth: never delete the user-supplied source file.

The current code paths in this skill don't target the source — the audit
on 2026-05-22 verified that every `unlink`/`rename`/`rmtree` operates on
intermediate `.part.mkv`, split chunks, or output `.mkv` files. This
module exists so a FUTURE refactor that accidentally points one of those
operations at the source raises immediately instead of silently destroying
the user's original.

User rule (verbatim): "script or your skill should NEVER DELETE THE
SOURCE FILE, only user (me) can do that!!!"

Usage:
    from .source_guard import protect_source, ensure_not_source
    protect_source(args.input)         # once, at startup
    protect_source(patched_copy)       # ADDS — original stays protected
    ensure_not_source(some_path)       # before any unlink/rename/rmtree
                                       # whose target was computed from
                                       # user-controlled input

The check uses resolved absolute paths and case-insensitive comparison
on Windows. Returns silently when nothing is protected (e.g. unit tests
that don't call protect_source)."""
from __future__ import annotations

import os
from pathlib import Path


# Module-level state. A SET, not a single path: with --auto-patch-source
# two sources are live simultaneously — the user's original AND the patched
# copy the pipeline encodes from. Protecting only the most recent one (the
# old single-slot behaviour) silently dropped the guard on the original the
# moment a patch was adopted, which is exactly the path this module exists
# to backstop. The queue runner re-invokes compress.py per file (fresh
# process per job), so the registry still resets between jobs.
_protected_sources: set[Path] = set()


def protect_source(source_path: Path | str) -> None:
    """ADD `source_path` to the off-limits set checked by every subsequent
    `ensure_not_source` call. Never replaces earlier registrations — the
    original source stays protected after a patched copy is adopted. Must
    be called BEFORE the encoder kicks off the chunking pipeline."""
    _protected_sources.add(Path(source_path).resolve())


def get_protected_sources() -> frozenset[Path]:
    """Return the currently-protected source paths (possibly empty)."""
    return frozenset(_protected_sources)


def _paths_match(a: Path, b: Path) -> bool:
    """OS-aware path equality. Windows is case-insensitive."""
    try:
        ra = a.resolve()
    except OSError:
        ra = a
    try:
        rb = b.resolve()
    except OSError:
        rb = b
    if os.name == "nt":
        return str(ra).lower() == str(rb).lower()
    return ra == rb


def ensure_not_source(path: Path | str) -> None:
    """Raise RuntimeError if `path` IS the protected source. No-op when no
    source has been registered. Call this immediately before any
    `path.unlink()`, `path.rename(...)`, `shutil.move(path, ...)`, or
    `shutil.rmtree(path)` whose target was derived from user-controlled
    state. The error message includes the source path so the offending
    call site can be fixed."""
    if not _protected_sources:
        return
    p = Path(path)
    for protected in _protected_sources:
        if _paths_match(p, protected):
            raise RuntimeError(
                f"SOURCE FILE PROTECTION TRIGGERED: a code path tried to "
                f"modify or delete the user-supplied source file:\n"
                f"  {protected}\n"
                f"This is forbidden. Only the user is allowed to delete "
                f"the source. If you see this error, find the calling code "
                f"and route it to the workdir or output path instead."
            )

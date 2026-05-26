"""Tiny shared formatting helpers with no project dependencies.

Lives at the repo root (not inside a package) so both the encode pipeline
(`encode_modules`) and the script generator (`compress_modules`) can import it
without creating a cross-package dependency. Stdlib-only; safe to import from
anywhere the packages are importable.
"""
from __future__ import annotations


def format_hms(seconds: float) -> str:
    """Seconds → ``"H:MM:SS"`` (hours always shown, fractional seconds
    truncated). The canonical "always-hours" duration format; callers that
    need a blank/placeholder sentinel or a compact no-hours form wrap their
    own sentinel logic around this (see report._fmt_dur, progress.fmt_time)."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"

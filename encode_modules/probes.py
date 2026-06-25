"""ffprobe wrappers — duration, full metadata, frame-rate fraction.

Three callers downstream rely on these:
  - `chunking` / `encoder`: chunk durations for the % progress / ETA math.
  - `quality`:              source fps (preserved as fraction string) to feed
                            back into ffmpeg as -r for the VMAF fps-match fix.
  - `history_state`:        full ffprobe JSON to derive BPP / resolution /
                            codec for the JSONL history record.

The probes return safe defaults (0.0 / None) on failure rather than raising —
ffprobe failing on a single chunk should never abort an encode that's been
running for hours.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from formatting import format_hms

# A metadata probe normally returns in milliseconds; this ceiling only ever
# fires when ffprobe wedges on a corrupt/stalled source (or a dead network
# mount). It must be generous enough never to false-trip a slow-but-working
# probe, while still bounding what would otherwise be an unrecoverable hang.
_PROBE_TIMEOUT_S = 120

# Full-metadata ffprobe argv (everything after the "ffprobe" program name and
# before the path). Single source of truth: both this module's best-effort
# ``probe_full`` and ``compress_modules.probe.run_ffprobe`` (which exits the
# process on failure) build the SAME probe — only their failure POLICY differs,
# so they share the argv + the subprocess call here and each applies its own
# policy to the result.
_FFPROBE_FULL_ARGS = ["-v", "error", "-print_format", "json",
                      "-show_format", "-show_streams"]


def run_ffprobe_json(path: Path, *,
                     timeout_s: int = _PROBE_TIMEOUT_S
                     ) -> subprocess.CompletedProcess:
    """Run the full-metadata (format + streams) ffprobe and return the raw
    ``CompletedProcess`` (stdout is JSON text). Raises ``TimeoutExpired`` on a
    wedged probe; does NOT inspect the return code or parse the JSON — the
    caller applies its own failure policy (best-effort None vs. fail-fast
    ``sys.exit``). The single subprocess site for the full-metadata probe."""
    return subprocess.run(
        ["ffprobe", *_FFPROBE_FULL_ARGS, str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
    )


def fmt_dur(seconds: float) -> str:
    """Seconds → 'H:MM:SS'. Re-exported here (delegating to the canonical
    formatting.format_hms) because every consumer of probe_duration needs it
    for display alongside the duration value."""
    return format_hms(seconds)


def probe_duration_or_none(path: Path, *,
                           timeout_s: int = _PROBE_TIMEOUT_S) -> Optional[float]:
    """Container-level duration in seconds, or None on ANY probe failure
    (timeout, non-zero exit, unparseable / missing duration).

    This is the single ffprobe-duration subprocess site for the whole package:
    ``probe_duration`` below is just the None→0.0 adapter. Callers that want to
    *distinguish* "probe failed" from a genuine zero-length duration (e.g.
    history.build_chunk_records, which omits speed_factor rather than dividing
    by a bogus 0.0) use this directly; callers that just want a number with a
    safe default use ``probe_duration``.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)["format"].get("duration")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        # Best-effort: unparseable JSON, no "format" key, or "format" isn't a
        # dict → treat as "no duration". (Probe helpers in this module catch
        # broadly *by design* — a failed metadata probe must never abort an
        # hours-long encode — but we name the expected failure modes rather
        # than a bare `except`.)
        return None
    if d is None:
        return None
    try:
        return float(d)
    except (TypeError, ValueError):
        return None


def probe_duration(path: Path, *, timeout_s: int = _PROBE_TIMEOUT_S) -> float:
    """Container-level duration in seconds, 0.0 on failure (incl. timeout).

    Thin 0.0-on-failure adapter over :func:`probe_duration_or_none` — preserves
    the historical contract for callers (chunking / encoder ETA math) that want
    a plain number and treat a failed probe as 0.0.
    """
    dur = probe_duration_or_none(path, timeout_s=timeout_s)
    return dur if dur is not None else 0.0


def probe_full(path: Path) -> dict | None:
    """ffprobe a file and return parsed format + streams JSON, or None on
    failure (incl. timeout). Best-effort: the in-encode callers must degrade
    gracefully, never abort, on a bad probe."""
    try:
        r = run_ffprobe_json(path)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def probe_fps(path: Path) -> str | None:
    """Return the first video stream's r_frame_rate as the original
    fraction string (e.g. "50/1" or "30000/1001"). Preserving the
    fraction avoids float-rounding artifacts when passed back to ffmpeg
    as -r. Returns None on failure (incl. timeout)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    val = (r.stdout or "").strip()
    if not val or val == "0/0":
        return None
    return val

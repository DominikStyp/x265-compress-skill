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


def fmt_dur(seconds: float) -> str:
    """Seconds → 'H:MM:SS'. Lives here because every consumer of probe_duration
    needs it for display alongside the duration value."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def probe_duration(path: Path) -> float:
    """Container-level duration in seconds, 0.0 on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        return 0.0
    try:
        return float(json.loads(r.stdout)["format"].get("duration", 0) or 0)
    except Exception:
        return 0.0


def probe_full(path: Path) -> dict | None:
    """ffprobe a file and return parsed format + streams JSON, or None on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def probe_fps(path: Path) -> str | None:
    """Return the first video stream's r_frame_rate as the original
    fraction string (e.g. "50/1" or "30000/1001"). Preserving the
    fraction avoids float-rounding artifacts when passed back to ffmpeg
    as -r. Returns None on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        return None
    val = (r.stdout or "").strip()
    if not val or val == "0/0":
        return None
    return val

"""Pre-flight scan: catch source-file decode errors BEFORE chunking + encoding.

Some upstream `.mp4` / `.mkv` files have bitstream corruption — broken NAL
units, missing access-unit pictures, AAC channel-element mismatches — that
ffmpeg's decoder will silently conceal at encode time. When x265 then runs
on the concealed garbage, the encoder either chokes (90 threads stuck in
Wait, ~1 core used) or produces output that fails verify_output's decode
pass. Either way we burn hours of CPU before the real cause shows up.

The pre-flight scan walks the source in `seg_sec`-second windows up front
with `ffmpeg -ss N -t seg_sec -xerror -f null -`. Any window that returns
non-zero is recorded with its time range + first error lines. If ANY window
is bad, encode_resumable.py exits with status `pre-flight-failed` (code 6)
and the queue runner moves to the next file — no chunking, no encode CPU
wasted.

Cache: result is written to `<source>.preflight.json` keyed on `(file_size,
file_mtime)`. Re-runs of the same source skip the rescan as long as the
source bytes haven't changed (this also handles the case where Claude or
PC rebooted in the middle of a queue and we restart from the top).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority


def _probe_duration(src: Path) -> float:
    """Local copy to avoid a circular probes <-> pre_flight import dependency."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", str(src)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        return 0.0
    try:
        return float(json.loads(r.stdout)["format"].get("duration", 0) or 0)
    except Exception:
        return 0.0


def _cache_path_for(src: Path) -> Path:
    """Pre-flight cache sidecar path. Lives next to the source so it follows
    the file around if moved; (size, mtime) check below catches replacements."""
    return src.with_suffix(src.suffix + ".preflight.json")


def _read_cache(src: Path) -> Optional[dict]:
    """Load cached pre-flight result if the source's (size, mtime) match.
    Returns None on any mismatch / corruption / missing file — caller re-scans."""
    cache_path = _cache_path_for(src)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        st = src.stat()
        if cached.get("src_size") != st.st_size:
            return None
        # Allow tiny mtime drift (e.g. ntfs/fat resolution differences when
        # moving files): treat differences <1.5s as same file.
        if abs(cached.get("src_mtime", 0) - st.st_mtime) > 1.5:
            return None
        return cached.get("result")
    except Exception:
        return None


def _write_cache(src: Path, result: dict) -> None:
    """Persist scan result so re-runs skip the work. Failures are silent —
    a missing cache just means we'll re-scan next time."""
    cache_path = _cache_path_for(src)
    try:
        st = src.stat()
        cache_path.write_text(
            json.dumps({
                "src_size": st.st_size,
                "src_mtime": st.st_mtime,
                "scanned_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "result": result,
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _walk_one_window(src: Path, start_s: float, dur_s: float,
                    timeout_s: int) -> dict:
    """Decode-walk one [start, start+dur) window with `-xerror`. Returns
    {decode_exit_code, error_count, error_samples} so the caller can decide
    whether THIS window is safe. Designed to be cheap on clean content
    (~1-2s for a 60-s 4K window) and fail-fast on broken content."""
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            wrap_cmd_for_low_priority(
                ["ffmpeg", "-v", "error", "-hide_banner", "-xerror",
                 "-ss", f"{start_s}", "-i", str(src),
                 "-t", f"{dur_s}",
                 "-map", "0:v?", "-map", "0:a?",
                 "-f", "null", "-"]
            ),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
            **low_priority_popen_kwargs(),
        )
        rc = r.returncode
        err_text = r.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc = -1  # sentinel for timeout
        err_text = (e.stderr or b"").decode("utf-8", errors="replace") \
            if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
    lines = [l.strip() for l in err_text.splitlines() if l.strip()]
    return {
        "decode_exit_code": rc,
        "error_count": len(lines),
        "error_samples": [l[:240] for l in lines[:8]],
        "elapsed_seconds": round(time.monotonic() - t0, 2),
    }


def pre_flight_scan(src: Path, *, seg_sec: int = 60,
                   use_cache: bool = True,
                   per_window_timeout_s: int = 90) -> dict:
    """Scan the source file for bitstream corruption that would trip the
    encoder. Returns a dict the caller can inspect AND log to history.jsonl:

        {
          "passed": bool,                          # False if any window broke
          "src_duration_seconds": float,
          "window_seconds": int,                   # seg_sec used
          "windows_total": int,
          "windows_clean": int,
          "bad_windows": [
            {"start_sec": float, "end_sec": float, "error_count": int,
             "error_samples": [str, ...], "decode_exit_code": int|-1}
          ],
          "elapsed_seconds": float,
          "cache_hit": bool,
        }

    Use windows of `seg_sec` matching the encoder's chunk size so a bad
    window aligns naturally with the chunk that would have choked.

    The walk uses `-ss N -t seg_sec` (input-level seek to keyframe before
    N, then decode N's window). Keyframe seek means windows don't start
    precisely at N but slightly before — that's fine since we only care
    about whether ANY frame in [N, N+seg_sec) has bitstream errors."""
    if use_cache:
        cached = _read_cache(src)
        if cached is not None:
            return {**cached, "cache_hit": True}

    overall_start = time.monotonic()
    src_dur = _probe_duration(src)
    if src_dur <= 0:
        # Can't probe duration → can't walk windows → assume bad.
        result = {
            "passed": False,
            "src_duration_seconds": 0,
            "window_seconds": seg_sec,
            "windows_total": 0,
            "windows_clean": 0,
            "bad_windows": [{"start_sec": 0, "end_sec": 0,
                            "error_count": 1, "decode_exit_code": -1,
                            "error_samples": ["could not probe source duration"]}],
            "elapsed_seconds": round(time.monotonic() - overall_start, 2),
        }
        _write_cache(src, result)
        return {**result, "cache_hit": False}

    # Walk overlapping windows: each window covers [N, N+seg_sec). The last
    # window is clamped to src_dur.
    starts: list[float] = []
    t = 0.0
    while t < src_dur:
        starts.append(t)
        t += seg_sec

    bad_windows: list[dict] = []
    windows_clean = 0
    for start in starts:
        end = min(src_dur, start + seg_sec)
        dur = end - start
        if dur <= 0.1:
            continue
        walk = _walk_one_window(src, start, dur, per_window_timeout_s)
        if walk["decode_exit_code"] == 0 and walk["error_count"] == 0:
            windows_clean += 1
            continue
        bad_windows.append({
            "start_sec": round(start, 2),
            "end_sec": round(end, 2),
            "error_count": walk["error_count"],
            "error_samples": walk["error_samples"],
            "decode_exit_code": walk["decode_exit_code"],
            "elapsed_seconds": walk["elapsed_seconds"],
        })

    result = {
        "passed": len(bad_windows) == 0,
        "src_duration_seconds": round(src_dur, 2),
        "window_seconds": seg_sec,
        "windows_total": len(starts),
        "windows_clean": windows_clean,
        "bad_windows": bad_windows,
        "elapsed_seconds": round(time.monotonic() - overall_start, 1),
    }
    _write_cache(src, result)
    return {**result, "cache_hit": False}


def format_pre_flight_summary(result: dict) -> str:
    """Pretty-print the scan result for terminal output. Single function so
    the formatting stays consistent between encode_resumable.py's pre-encode
    summary and any future report writers."""
    if result.get("passed"):
        return (f"  Pre-flight OK ({result['windows_clean']}/"
                f"{result['windows_total']} windows clean, "
                f"{result['elapsed_seconds']:.0f}s"
                f"{' — cached' if result.get('cache_hit') else ''})")
    lines = [f"  Pre-flight FAILED ({len(result['bad_windows'])} bad "
            f"window(s) in {result['elapsed_seconds']:.0f}s"
            f"{' — cached' if result.get('cache_hit') else ''}):"]
    for w in result.get("bad_windows", []):
        lines.append(f"    sec {w['start_sec']:.1f}-{w['end_sec']:.1f}: "
                    f"{w['error_count']} decode errors "
                    f"(exit {w['decode_exit_code']})")
        for sample in (w.get("error_samples") or [])[:3]:
            lines.append(f"      {sample[:140]}")
    return "\n".join(lines)

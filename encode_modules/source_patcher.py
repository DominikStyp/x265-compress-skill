"""Auto-patch a source with localized h264 corruption so the rest of the
pipeline can proceed.

Why this exists
---------------
Pre-flight catches sources with bad h264 references and bails (exit 6).
Without intervention, those files are dead until an operator runs the
surgical-patch recipe manually (see SKILL.md "Manual recovery recipes").
This module is the automated equivalent: triggered by --auto-patch-source,
it consumes a failed pre-flight scan_result, localizes the bad GOPs,
re-encodes JUST those GOPs through ffmpeg's error concealment, and
concats the result with stream-copy parts into a clean
`source-patched.mp4` in the workdir. The encode pipeline then continues
with this patched file as its source.

Design choices (per user agreement, 2026-05-22):
  - OPT-IN via --auto-patch-source. Source modification (even into workdir)
    is significant; explicit consent prevents surprise.
  - Patched file lives in WORKDIR (`source-patched.mp4`). Cleanup wipes
    it with the rest of the workdir on full success — user's source
    directory stays untouched.
  - LOSS BUDGET defaults to 10 s of cumulative re-encode under concealment
    (matches the user's stated "max ~10 seconds missing" tolerance).
    --max-patch-seconds overrides it.
  - h264 SOURCES ONLY. Non-h264 sources skip the patch — the bsf list and
    re-encode codec are h264-specific. Bail back to pre-flight-failed.

Validated end-to-end against the manual Emily PATCHED 2026-05-22 case:
the same 35.82-35.98 s broken-ref zone, same 34.04-36.04 s patch GOP,
identical decode-walk outcome on the patched output.

Related: [[reference-h264-surgical-patch]] for the manual recipe this
module automates; [[reference-dts-collision-concat-remux]] for the
verify-failure follow-on that's auto-cleared at the end of the pipeline.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

from platform_compat import low_priority_popen_kwargs


def _decode_walk_count(src: Path, start: float, dur: float,
                      timeout_s: int = 60) -> int:
    """Return the number of stderr error lines from a -xerror decode walk
    of `src` over the window [start, start+dur). Used to localize bad
    zones by progressively narrowing the window."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-hide_banner", "-xerror",
             "-ss", f"{start}", "-i", str(src),
             "-t", f"{dur}",
             "-map", "0:v?",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
            **low_priority_popen_kwargs(),
        )
        return len([l for l in (r.stderr or "").splitlines() if l.strip()])
    except subprocess.TimeoutExpired:
        return -1


def _refine_bad_zone(src: Path, win_start: float, win_end: float) -> list[tuple[float, float]]:
    """Narrow a coarse bad window from pre-flight (e.g. 0-173 s) to a
    list of second-resolution bad zones inside it. Two-stage: 10 s
    sub-windows to find which 10-second blocks contain errors, then 1 s
    sub-windows inside each hit to pinpoint the bad seconds."""
    bad_zones: list[tuple[float, float]] = []
    cur = win_start
    while cur < win_end:
        sub_end = min(win_end, cur + 10.0)
        if _decode_walk_count(src, cur, sub_end - cur) > 0:
            sec = cur
            while sec < sub_end:
                s_end = min(sub_end, sec + 1.0)
                if _decode_walk_count(src, sec, s_end - sec) > 0:
                    bad_zones.append((sec, s_end))
                sec = s_end
        cur = sub_end
    # Merge adjacent 1 s zones into single ranges (e.g. seconds 35 and 36
    # collapse to (35, 37) so we cut one continuous re-encode segment).
    if not bad_zones:
        return []
    merged: list[tuple[float, float]] = [bad_zones[0]]
    for s, e in bad_zones[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _probe_keyframes(src: Path, probe_start: float, probe_end: float) -> list[float]:
    """Return I-frame pts_times in [probe_start, probe_end] from `src`.
    Use a -read_intervals seek to avoid scanning the whole file."""
    rng = f"{probe_start}%{probe_end}"
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-read_intervals", rng,
             "-select_streams", "v:0",
             "-show_frames",
             "-show_entries", "frame=pts_time,key_frame",
             "-of", "csv", str(src)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
        out: list[float] = []
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 3 and parts[0] == "frame" and parts[1] == "1":
                try:
                    out.append(float(parts[2]))
                except ValueError:
                    continue
        return out
    except Exception:
        return []


def _gop_align_zones(src: Path, zones: list[tuple[float, float]]) -> Optional[list[tuple[float, float]]]:
    """For each bad zone, snap to the nearest I-frame BEFORE its start and
    the next I-frame AT-OR-AFTER its end. Returns None if any zone can't
    find clean keyframe boundaries (rare; usually means corruption
    extends past the last keyframe before EOF). Also merges any GOP-
    aligned zones that ended up adjacent or overlapping."""
    aligned: list[tuple[float, float]] = []
    for bz_start, bz_end in zones:
        probe_start = max(0.0, bz_start - 5.0)
        probe_end = bz_end + 5.0
        kfs = _probe_keyframes(src, probe_start, probe_end)
        kf_before = max((k for k in kfs if k <= bz_start), default=None)
        kf_after = min((k for k in kfs if k >= bz_end), default=None)
        if kf_before is None or kf_after is None:
            return None
        aligned.append((kf_before, kf_after))
    # Merge overlapping / adjacent GOP zones
    merged: list[tuple[float, float]] = [aligned[0]]
    for s, e in aligned[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _probe_video_codec(src: Path) -> Optional[str]:
    """Codec_name of the first video stream, or None on probe failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", str(src)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            return None
        streams = json.loads(r.stdout).get("streams") or []
        return streams[0].get("codec_name") if streams else None
    except Exception:
        return None


def _probe_duration(src: Path) -> float:
    """Duration in seconds, or 0.0 on probe failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", str(src)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            return 0.0
        return float(json.loads(r.stdout)["format"].get("duration", 0) or 0)
    except Exception:
        return 0.0


def _build_copy_segment(src: Path, start: float, end: float,
                       out: Path) -> bool:
    """Stream-copy [start, end) of src into out as MPEG-TS."""
    args = ["ffmpeg", "-v", "error", "-hide_banner", "-y"]
    if start > 0:
        args += ["-ss", f"{start}"]
    args += ["-i", str(src)]
    if end > start:
        args += ["-t", f"{end - start}"]
    args += [
        "-c", "copy",
        "-bsf:v", "h264_mp4toannexb",
        "-f", "mpegts",
        str(out),
    ]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       **low_priority_popen_kwargs())
    return r.returncode == 0


def _build_encode_segment(src: Path, start: float, end: float,
                         out: Path) -> bool:
    """Re-encode [start, end) of src into out as MPEG-TS. ffmpeg's decoder
    uses error concealment to fill in the missing refs; x264 veryfast
    CRF 14 then produces a clean near-visually-lossless segment."""
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-hide_banner", "-y",
        "-ss", f"{start}",
        "-i", str(src),
        "-t", f"{end - start}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "5.1",
        "-c:a", "copy",
        "-bsf:v", "h264_mp4toannexb",
        "-f", "mpegts",
        str(out),
    ], capture_output=True, text=True, encoding="utf-8",
       errors="replace", **low_priority_popen_kwargs())
    return r.returncode == 0


def auto_patch_source(
    src: Path,
    scan_result: dict,
    workdir: Path,
    *,
    max_patch_seconds: float = 10.0,
) -> Optional[Path]:
    """Attempt to surgically patch `src` based on `scan_result` from a
    failed pre-flight. Returns the path to a clean `source-patched.mp4`
    in `workdir` on success, or None if:

      - Source codec isn't h264 (this module's BSFs and concealment path
        are h264-specific).
      - Refining bad zones to second resolution turned up no errors (the
        original scan was a transient probe issue, not real corruption —
        caller can retry the scan instead).
      - GOP boundaries around any bad zone can't be found (corruption
        spans EOF or some odd structural case).
      - Total re-encoded duration exceeds `max_patch_seconds` (loss
        budget; default matches the user's stated 10 s tolerance).
      - Any ffmpeg invocation fails.

    On success, the workdir contains:
      - `source-patched.mp4` (the patched source — used downstream)
      - `_source_patch/` (intermediate `.ts` parts + concat list,
        forensic, not used after concat)

    The original `src` is NEVER touched — protected by source-guard
    elsewhere in the pipeline anyway."""
    codec = _probe_video_codec(src)
    if codec != "h264":
        return None

    bad_windows = scan_result.get("bad_windows") or []
    if not bad_windows:
        return None

    # Stage 1: refine each coarse bad window to second-resolution zones.
    refined: list[tuple[float, float]] = []
    for w in bad_windows:
        refined.extend(_refine_bad_zone(
            src, float(w.get("start_sec", 0)), float(w.get("end_sec", 0))
        ))
    if not refined:
        return None

    # Stage 2: snap to GOP boundaries (clean cut points for stream-copy).
    aligned = _gop_align_zones(src, refined)
    if not aligned:
        return None

    # Stage 3: loss-budget check. Total re-encoded duration across all
    # GOP-aligned patch zones must not exceed max_patch_seconds.
    total_patch_s = sum(e - s for s, e in aligned)
    if total_patch_s > max_patch_seconds:
        print(f"  ! auto-patch declined: total patch duration "
              f"{total_patch_s:.1f}s exceeds budget {max_patch_seconds}s")
        return None

    src_dur = _probe_duration(src)
    if src_dur <= 0:
        return None

    # Stage 4: build segment plan (alternating copy/encode segments
    # covering the full source duration).
    plan: list[tuple[str, float, float]] = []
    last_end = 0.0
    for s, e in aligned:
        if s > last_end:
            plan.append(("copy", last_end, s))
        plan.append(("encode", s, e))
        last_end = e
    if last_end < src_dur:
        plan.append(("copy", last_end, src_dur))

    # Stage 5: execute. Each segment becomes one .ts file; concat via
    # the concat *demuxer* (not protocol — protocol stitches blindly
    # and produces backwards DTS at the seam).
    patch_dir = workdir / "_source_patch"
    patch_dir.mkdir(parents=True, exist_ok=True)

    print(f"  > auto-patching {len(aligned)} bad GOP zone(s), total "
          f"{total_patch_s:.1f}s of re-encode")

    ts_files: list[Path] = []
    for i, (kind, seg_start, seg_end) in enumerate(plan):
        ts_path = patch_dir / f"part_{i:03d}_{kind}.ts"
        ts_files.append(ts_path)
        if kind == "copy":
            ok = _build_copy_segment(src, seg_start, seg_end, ts_path)
        else:
            ok = _build_encode_segment(src, seg_start, seg_end, ts_path)
        if not ok:
            print(f"  ! auto-patch failed building segment {i} "
                  f"({kind} {seg_start:.2f}-{seg_end:.2f}s)")
            return None

    list_file = patch_dir / "concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{t.resolve().as_posix()}'" for t in ts_files) + "\n",
        encoding="utf-8",
    )

    out_mp4 = workdir / "source-patched.mp4"
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(out_mp4),
    ], capture_output=True, text=True, encoding="utf-8",
       errors="replace", **low_priority_popen_kwargs())
    if r.returncode != 0:
        print(f"  ! auto-patch concat failed (rc={r.returncode})")
        return None

    return out_mp4


def format_patch_zones(aligned: list[tuple[float, float]]) -> str:
    """Pretty-print GOP-aligned patch zones for the history record."""
    return ", ".join(f"{s:.2f}-{e:.2f}s" for s, e in aligned)

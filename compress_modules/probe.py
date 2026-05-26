"""Source-file analysis: ffprobe wrapper + SourceInfo dataclass + analyse().

Single responsibility: read metadata off disk and return a SourceInfo. No
encoding decisions, no .bat generation — those live in `plan` and
`bat_writer`."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SourceInfo:
    codec: str
    width: int
    height: int
    fps: float
    pix_fmt: str
    bit_depth: int
    color_primaries: str | None
    color_transfer: str | None
    video_bitrate_kbps: int
    duration_sec: float
    file_size_bytes: int
    bits_per_pixel: float
    is_hdr: bool
    audio_codecs: list[str]


def run_ffprobe(path: Path) -> dict:
    """Probe `path` with ffprobe, returning the parsed JSON dict.
    Exits the process on any failure — the caller has nothing useful to do
    without source metadata."""
    if not shutil.which("ffprobe"):
        sys.exit(
            "ERROR: ffprobe not found on PATH (it ships with ffmpeg).\n"
            "       Install ffmpeg, then restart your shell so PATH updates:\n"
            "         macOS:   brew install ffmpeg\n"
            "         Windows: winget install Gyan.FFmpeg\n"
            "         Linux:   sudo apt install ffmpeg  (or dnf/pacman/zypper/apk)")
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            # Generous ceiling: a healthy probe returns in well under a second,
            # so this only trips on a wedged ffprobe (corrupt source / dead
            # mount) — better a clear error than an indefinite hang at startup.
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        sys.exit(f"ffprobe timed out after 120s probing {path} "
                 "(corrupt source or unresponsive storage?)")
    if proc.returncode != 0:
        sys.exit(f"ffprobe failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def parse_fps(r_frame_rate: str) -> float:
    """ffprobe emits fps as a rational like "30000/1001" or "25/1". Reduce to
    float; return 0.0 on any parse failure."""
    try:
        num, den = r_frame_rate.split("/")
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    except Exception:
        return 0.0


def bit_depth_from_pix_fmt(pix_fmt: str) -> int:
    """Crude but reliable: look for `p10`, `p12`, `p16` markers in the pix_fmt
    name. Not exhaustive but covers every format we encounter in practice."""
    if "p16" in pix_fmt or "16le" in pix_fmt or "16be" in pix_fmt:
        return 16
    if "p12" in pix_fmt or "12le" in pix_fmt or "12be" in pix_fmt:
        return 12
    if "p10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt:
        return 10
    return 8


def _video_bitrate_kbps(video: dict, fmt: dict, audio_count: int,
                       duration_sec: float, file_size_bytes: int) -> int:
    """Pick the best available video-bitrate estimate. Tries stream-level
    first (most accurate), then format-level minus a 192 kbps per audio
    track allowance, then derives from size/duration as a last resort."""
    if video.get("bit_rate"):
        return int(int(video["bit_rate"]) / 1000)
    if fmt.get("bit_rate"):
        total = int(int(fmt["bit_rate"]) / 1000)
        # Format-level bit_rate includes audio; subtract a single 192 kbps
        # allowance if any audio is present (intentionally NOT per-track here —
        # only the size/duration fallback below scales by track count).
        return _minus_audio_allowance(total, 1) if audio_count else total
    if duration_sec > 0:
        total = int((file_size_bytes * 8) / duration_sec / 1000)
        return _minus_audio_allowance(total, max(audio_count, 1))
    return 0


def _minus_audio_allowance(total_kbps: int, tracks: int) -> int:
    """Subtract a 192 kbps allowance per `tracks`, but never return a near-zero
    rate — fall back to the gross total if the subtraction would."""
    net = total_kbps - 192 * tracks
    return net if net > 0 else total_kbps


def analyse(path: Path) -> SourceInfo:
    """Probe `path` and return a fully-populated SourceInfo. The bits-per-pixel
    field downstream consumers care about most is computed last so it can
    incorporate every other field."""
    data = run_ffprobe(path)

    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if not video:
        sys.exit("ERROR: no video stream found in source.")

    audio_codecs = [
        s.get("codec_name", "?")
        for s in data.get("streams", [])
        if s.get("codec_type") == "audio"
    ]

    width = int(video.get("width", 0))
    height = int(video.get("height", 0))
    fps = parse_fps(video.get("r_frame_rate", "0/1"))
    pix_fmt = video.get("pix_fmt", "yuv420p")
    bit_depth = bit_depth_from_pix_fmt(pix_fmt)

    color_primaries = video.get("color_primaries")
    color_transfer = video.get("color_transfer")
    is_hdr = (
        color_primaries == "bt2020"
        or color_transfer in {"smpte2084", "arib-std-b67"}
    )

    fmt = data.get("format", {})
    duration_sec = float(fmt.get("duration", 0) or 0)
    file_size_bytes = int(fmt.get("size", 0) or path.stat().st_size)

    video_bitrate_kbps = _video_bitrate_kbps(
        video, fmt, len(audio_codecs), duration_sec, file_size_bytes,
    )

    if width and height and fps and video_bitrate_kbps:
        bpp = (video_bitrate_kbps * 1000.0) / (width * height * fps)
    else:
        bpp = 0.0

    return SourceInfo(
        codec=(video.get("codec_name") or "unknown").lower(),
        width=width, height=height, fps=fps,
        pix_fmt=pix_fmt, bit_depth=bit_depth,
        color_primaries=color_primaries, color_transfer=color_transfer,
        video_bitrate_kbps=video_bitrate_kbps,
        duration_sec=duration_sec, file_size_bytes=file_size_bytes,
        bits_per_pixel=bpp, is_hdr=is_hdr,
        audio_codecs=audio_codecs,
    )

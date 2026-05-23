"""Dispatcher: pick serial or parallel chunk encode based on what guards
are in effect. Both implementations live in their own modules — this file
is intentionally thin so the routing is easy to audit.

Routing rules:
  --parallel >= 2                                 -> parallel
  --max-size-percent set (any parallel)           -> parallel (threshold lives there)
  choke detection enabled (any parallel)          -> parallel (choke lives there)
  --parallel == 1, no threshold, no choke         -> serial (lightest UX)

The serial path uses the standalone progress.py bar; the parallel path
uses the htop-style live block with per-slot fps/speed/ETA and key controls.
Both flavors produce identical enc_*.mkv files — only the rendering and
scheduling differ.
"""
from __future__ import annotations

from pathlib import Path

from .encode_parallel import encode_chunks_parallel
from .encode_serial import encode_chunks_serial


def encode_chunks(chunks: list[Path], workdir: Path, *, parallel: int,
                 crf: int, preset: str, pix_fmt: str, x265_params: str,
                 total_duration_sec: float = 0,
                 source_bytes: int = 0,
                 max_output_bytes: int | None = None,
                 choke_threshold_speed: float = 0.05,
                 choke_grace_seconds: float = 300.0,
                 auto_fix_choke: bool = False,
                 segment_seconds: int = 60) -> list[dict]:
    """Top-level dispatcher — routes to the encoder that owns the guards
    we need. Returns the list of skipped chunks (empty if all chunks
    encoded cleanly). The serial path can't produce skips (no choke
    detection there).
    """
    needs_parallel_path = (max_output_bytes is not None
                           or (choke_threshold_speed > 0
                               and choke_grace_seconds > 0))
    if parallel <= 1 and not needs_parallel_path:
        encode_chunks_serial(chunks, workdir,
                            crf=crf, preset=preset,
                            pix_fmt=pix_fmt, x265_params=x265_params)
        return []
    return encode_chunks_parallel(
        chunks, workdir, parallel=max(1, parallel),
        crf=crf, preset=preset,
        pix_fmt=pix_fmt, x265_params=x265_params,
        total_duration_sec=total_duration_sec,
        source_bytes=source_bytes,
        max_output_bytes=max_output_bytes,
        choke_threshold_speed=choke_threshold_speed,
        choke_grace_seconds=choke_grace_seconds,
        auto_fix_choke=auto_fix_choke,
        segment_seconds=segment_seconds,
    )

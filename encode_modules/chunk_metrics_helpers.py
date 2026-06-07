"""Pure helpers extracted from ``chunk_metrics_log.py`` so that module
stays under the 500-line cap.

All functions here are dependency-free (stdlib only) and side-effect-free
except the obvious filesystem reads in ``workdir_has_no_enc_chunks``.
They form the small math + path-probing layer ``ChunkMetricsLog`` and
its callers build on.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def workdir_has_no_enc_chunks(workdir: Optional[Path]) -> bool:
    """True iff the workdir argument is non-None, exists, and contains no
    ``enc_*.mkv`` files (the marker of a successfully encoded chunk).
    Returned True triggers a JSONL truncate at init — the standard
    "this is a fresh encode" heuristic the chunking layer already uses.

    None / unreadable workdir returns False (be conservative: don't nuke
    the JSONL on a path we can't verify is empty)."""
    if workdir is None:
        return False
    try:
        for p in workdir.iterdir():
            if p.name.startswith("enc_") and p.suffix == ".mkv":
                return False
        return True
    except OSError:
        return False


def compute_size_percent(src_bytes: Optional[int],
                         out_bytes: Optional[int]) -> Optional[float]:
    """output / source as a percentage rounded to 2 dp, or None when either
    operand is missing / zero. Shared by reporting.py and history_state.py
    so the rounding rule lives in one place."""
    if (isinstance(src_bytes, (int, float)) and src_bytes > 0
            and isinstance(out_bytes, (int, float))):
        return round(out_bytes / src_bytes * 100, 2)
    return None


def safe_mean(values: list[float]) -> Optional[float]:
    """Mean, or None for an empty list. Avoids ``statistics.StatisticsError``
    at the aggregate site for the no-rows / guard-disabled case."""
    return (sum(values) / len(values)) if values else None


def safe_min(values: list[float]) -> Optional[float]:
    return min(values) if values else None


def safe_max(values: list[float]) -> Optional[float]:
    return max(values) if values else None


def stat_block(values: list[float], *, want_total: bool) -> dict:
    """Build a stats dict with total/mean/min/max for the JSONL summary.
    When no values are recorded yet, every stat is None so the shape is
    stable across "no rows" and "many rows" callers."""
    block: dict = {
        "mean": safe_mean(values),
        "min": safe_min(values),
        "max": safe_max(values),
    }
    if want_total:
        block["total"] = sum(values) if values else None
    return block


def fold_rows_into_encode_block(rows: dict, *,
                                width: Optional[int],
                                height: Optional[int],
                                fps: Optional[float],
                                crf: Optional[int],
                                preset: Optional[str],
                                quality_threshold: Optional[float],
                                source_codec: Optional[str],
                                source_bytes: Optional[int],
                                output_bytes: Optional[int],
                                duration_s: Optional[float],
                                size_percent: Optional[float],
                                quality_aborted: bool,
                                quality_aborted_chunk: Optional[str]
                                ) -> dict:
    """Fold a chunk_name -> row dict into the per-file ``encode`` summary
    block. Extracted from ``ChunkMetricsLog.aggregate_summary`` so the log
    module stays under the 500-line cap.

    n_chunks counts UNIQUE chunk_names (the rows dict is already deduped
    last-wins by the caller). overall bitrate is derived from
    output_bytes/duration_s when both are positive, else None."""
    n_chunks = len(rows)
    elapsed_values = [r["encode_elapsed_s"] for r in rows.values()
                      if isinstance(r.get("encode_elapsed_s"), (int, float))]
    chunk_bitrates = [r["output_bitrate_kbps"] for r in rows.values()
                      if isinstance(r.get("output_bitrate_kbps"),
                                    (int, float))]
    vmaf_values = [r["vmaf_mean"] for r in rows.values()
                   if isinstance(r.get("vmaf_mean"), (int, float))]

    overall_kbps = None
    if (isinstance(output_bytes, (int, float)) and output_bytes > 0
            and isinstance(duration_s, (int, float)) and duration_s > 0):
        overall_kbps = output_bytes * 8 / duration_s / 1000

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "source_codec": source_codec,
        # The encoder is always x265 (HEVC) — hardcoded constant. If we
        # ever add a second codec backend this gets parameterized.
        "output_codec": "hevc",
        "crf": crf,
        "preset": preset,
        "n_chunks": n_chunks,
        "source_bytes": source_bytes,
        "output_bytes": output_bytes,
        "size_percent": size_percent,
        "duration_s": duration_s,
        "elapsed_s": stat_block(elapsed_values, want_total=True),
        "output_bitrate_kbps": {
            "overall": overall_kbps,
            "chunk_mean": safe_mean(chunk_bitrates),
            "chunk_min": safe_min(chunk_bitrates),
            "chunk_max": safe_max(chunk_bitrates),
        },
        "vmaf_chunk": {
            "count": len(vmaf_values),
            "mean": safe_mean(vmaf_values),
            "min": safe_min(vmaf_values),
            "max": safe_max(vmaf_values),
        },
        "quality_threshold": quality_threshold,
        "quality_aborted": bool(quality_aborted),
        "quality_aborted_chunk": quality_aborted_chunk,
    }

"""Post-parallel-encode aggregation of chunks that didn't produce a clean
enc_*.mkv. Extracted from `encode_parallel.py` so the orchestration body
stays focused.

A chunk counts as "skipped" if it's in `display.choked_chunks` AND no clean
enc_*.mkv landed on disk. Auto-fix successes already removed themselves
from `display.choked_chunks`, so they don't get collected here. For each
true skip we:
  * quarantine the chunk's .part.mkv aside (tagged) — partial encoded
    bytes are user data per the never-delete-encoded-chunks rule, the same
    rule chunk_worker + chunk_recovery follow. Nothing here unlinks encoded
    bytes; a stale .part is harmless on resume anyway (the re-encode path
    quarantines it first, and resume keys off the final enc_*.mkv),
  * walk the source chunk to capture decode-error diagnostics,
  * write the per-chunk needs_fix.json sidecar via chunk_recovery.

Returns the structured `skipped` list main() exits 7 with.
"""
from __future__ import annotations

from pathlib import Path

from .chunk_recovery import _quarantine_part, write_needs_fix_sidecar
from .display import ParallelDisplay
from .verify import analyze_chunk_errors


def collect_skipped(chunks: list[Path], workdir: Path,
                   display: ParallelDisplay, *,
                   x265_params: str, preset: str, crf: int, pix_fmt: str,
                   segment_seconds: int) -> list[dict]:
    """Walk display.choked_chunks and build the per-skip dicts. Side effects:
    quarantines the leftover .part of each choked chunk (never deletes
    encoded bytes), writes needs_fix.json sidecars.

    Original encoded chunks (enc_*.mkv) are NEVER touched here — choked
    chunks don't have one to begin with, and clean chunks don't appear in
    choked_chunks."""
    skipped: list[dict] = []
    chunk_by_name = {c.name: c for c in chunks}
    with display.lock:
        choked_snapshot = dict(display.choked_chunks)

    for chunk_name, info in choked_snapshot.items():
        chunk_path = chunk_by_name.get(chunk_name)
        if chunk_path is None:
            continue
        final_out = workdir / f"enc_{chunk_path.stem}.mkv"
        if final_out.exists():
            # Auto-fix wrote it after all — not actually skipped.
            continue

        errors = _safe_analyze(chunk_path)
        _quarantine_stale_part(workdir, chunk_path)
        chunk_index = next((i for i, c in enumerate(chunks)
                           if c.name == chunk_name), -1)
        sidecar = write_needs_fix_sidecar(
            workdir, chunk_path,
            chunk_index=chunk_index, seg_sec=segment_seconds,
            choke_info=info, errors=errors,
            original_x265_params=x265_params,
            original_preset=preset, original_crf=crf,
            original_pix_fmt=pix_fmt,
        )
        skipped.append({
            "chunk_name": chunk_name,
            "chunk_index": chunk_index,
            "time_range_seconds": [chunk_index * segment_seconds,
                                  (chunk_index + 1) * segment_seconds],
            "choke_speed": round(info.get("speed", 0.0), 5),
            "choke_wall_seconds": round(info.get("wall_seconds", 0.0), 1),
            "error_count": (errors or {}).get("error_count", 0),
            "error_samples": (errors or {}).get("error_samples", []),
            "needs_fix_sidecar": str(sidecar),
        })
    return skipped


def _safe_analyze(chunk_path: Path) -> dict | None:
    """Decode-walk wrapper that never raises — capture the diagnostic for
    the sidecar but don't let a crashed ffprobe abort the whole collection."""
    try:
        return analyze_chunk_errors(chunk_path)
    except Exception as e:
        return {"error_count": 0,
                "error_samples": [f"(probe crashed: {e})"]}


def _quarantine_stale_part(workdir: Path, chunk_path: Path) -> None:
    """Move the choked chunk's leftover .part.mkv aside (tagged) instead of
    deleting it. Partial encoded bytes are the user's data — the same
    never-delete-encoded-chunks rule chunk_worker + chunk_recovery follow;
    `_quarantine_part` renames and NEVER unlinks.

    (The old behavior here unlinked the .part "so a future resume doesn't
    reuse a garbage partial." That rationale was false: resume keys off the
    final enc_*.mkv (encode_parallel.encode_chunks_parallel), and the
    re-encode path quarantines any stale .part before writing — nothing
    ever reuses it.)"""
    part = workdir / f"enc_{chunk_path.stem}.part.mkv"
    _quarantine_part(part, "choked-aside")

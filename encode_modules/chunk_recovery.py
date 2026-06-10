"""Chunk-level recovery helpers — auto-fix retry + needs_fix sidecar writer.

Split out of `encoder.py` so the main worker/render loop stays focused. Two
exported functions:

  try_auto_fix_chunk(...)
      One relaxed-x265-params re-encode attempt + decode-walk verification.
      Activated when the encoder runs with `--auto-fix-choke`. Returns True
      iff the produced enc_*.mkv passes a strict decode walk and is therefore
      safe to concat with the other chunks.

  write_needs_fix_sidecar(...)
      Drops a `enc_{chunk.stem}.needs_fix.json` next to the source chunk so
      a follow-up Claude conversation OR a user-driven manual fix has every
      piece of context (original params, error samples, time range, expected
      output path) needed to produce a valid replacement.

Both functions are conservative about preserving the user's data: nothing
deletes the original source chunk; failed auto-fix outputs are quarantined
rather than discarded (per the "never delete encoded chunks" memory rule).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority

from .chunking import ffmpeg_chunk_cmd
from .display import ParallelDisplay
from .chunk_metrics_log import record_chunk_metrics
from .history_state import record_chunk_elapsed
from .probes import probe_duration
from .verify import decode_walk_chunk


def relax_x265_params(x265_params: str) -> str:
    """Build a less-expensive x265 params string for the auto-fix retry.

    Drops the three knobs that triggered x265's WPP dependency-chain
    pathology on the chunk-0008 incident: `me=star → me=umh`, `subme=4 →
    subme=3`, `merange=57 → 32`. These are the most expensive motion-search
    settings; relaxing them lets the encoder make progress on error-
    concealed frames while keeping the rest of the slow-preset quality
    knobs intact. Quality cost on a single chunk of a multi-chunk encode
    is ~0.2 VMAF — invisible at CRF 25."""
    out_parts: list[str] = []
    for part_str in x265_params.split(":"):
        if part_str.startswith("me="):
            out_parts.append("me=umh")
        elif part_str.startswith("subme="):
            try:
                cur = int(part_str.split("=", 1)[1])
                out_parts.append(f"subme={min(3, max(1, cur))}")
            except ValueError:
                out_parts.append("subme=3")
        elif part_str.startswith("merange="):
            try:
                cur = int(part_str.split("=", 1)[1])
                out_parts.append(f"merange={min(32, cur)}")
            except ValueError:
                out_parts.append("merange=32")
        else:
            out_parts.append(part_str)
    return ":".join(out_parts)


def _quarantine_part(part: Path, tag: str) -> Optional[Path]:
    """Rename a `.part.mkv` aside so the user can inspect it. NEVER unlinks
    on failure paths — preserves encoded bytes per the never-delete rule.
    Returns the new path on success, None if the rename itself failed."""
    if not part.exists():
        return None
    try:
        broken = part.with_suffix(f".{tag}-{int(time.time())}.mkv")
        part.rename(broken)
        return broken
    except OSError:
        # Rename failed (file lock, permission). DO NOT fall back to unlink.
        # The .part stays on disk for the user to handle manually.
        return None


def try_auto_fix_chunk(chunk: Path, workdir: Path,
                      display: ParallelDisplay, *,
                      slot: int,
                      crf: int, preset: str, pix_fmt: str,
                      x265_params: str) -> bool:
    """Re-encode `chunk` in `slot` with relaxed motion-search params, then
    verify the result via a decode walk before accepting it. Returns True
    iff the produced enc_{chunk.stem}.mkv is decodable end-to-end and
    therefore safe to concat with the other chunks.

    Runs through the same Popen + -progress - + ParallelDisplay slot pipeline
    as a normal chunk encode, so the user sees a live progress bar with the
    `(AUTO-FIX)` label suffix instead of the prior silent block during the
    relaxed-params retry. The slot's choke guard remains inert for this
    chunk (already in choked_chunks) so a slow auto-fix isn't double-killed.

    Critical safety:
      - A relaxed-params encode that itself fails decode walk would corrupt
        the final merged output. We MUST verify before promoting.
      - On ANY failure path the `.part.mkv` is renamed to a tagged
        `.{reason}-{ts}.mkv` quarantine. NEVER unlinked — per the
        "never delete encoded chunks" rule, partial encoded bytes are
        the user's data."""
    relaxed = relax_x265_params(x265_params)
    out = workdir / f"enc_{chunk.stem}.mkv"
    part = workdir / f"enc_{chunk.stem}.part.mkv"
    # Pre-existing .part from a previous (failed) attempt in this session.
    # Quarantine instead of overwrite/delete.
    if part.exists():
        _quarantine_part(part, "preretry-aside")
    duration = probe_duration(chunk)
    display.events.put(f"  > {chunk.name}: auto-fix retry with relaxed params...")
    display.slot_start(slot, chunk.name, duration, label_suffix="AUTO-FIX")

    t0 = time.monotonic()
    proc = subprocess.Popen(
        wrap_cmd_for_low_priority(
            ffmpeg_chunk_cmd(chunk, part, crf=crf, preset=preset,
                            pix_fmt=pix_fmt, x265_params=relaxed,
                            extra_progress=["-progress", "-"])
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **low_priority_popen_kwargs(),
    )
    display.register_proc(slot, proc)

    state: dict[str, str] = {}
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        state[k] = v
        if k == "progress":
            try:
                out_us = float(state.get("out_time_us", "0") or 0)
            except ValueError:
                out_us = 0
            try:
                frame = int(state.get("frame", "0") or 0)
            except ValueError:
                frame = 0
            display.slot_progress(
                slot,
                out_time_s=out_us / 1_000_000,
                frame=frame,
                fps=state.get("fps", "?"),
                speed=state.get("speed", "?"),
            )
    rc = proc.wait()
    elapsed = time.monotonic() - t0
    err = (proc.stderr.read() if proc.stderr else "") or ""
    display.unregister_proc(slot)
    with display.lock:
        display.slots.pop(slot, None)

    if rc != 0 or not part.exists():
        _quarantine_part(part, "autofix-encode-failed")
        display.events.put(
            f"  ! {chunk.name}: auto-fix encode failed (rc={rc}) — needs manual fix")
        return False

    walk = decode_walk_chunk(part)
    if not walk["ok"]:
        # Quarantine the bad auto-fix attempt rather than deleting.
        _quarantine_part(part, "autofix-broken")
        display.events.put(
            f"  ! {chunk.name}: auto-fix output failed decode walk "
            f"({walk['error_count']} errors) — needs manual fix")
        return False

    # Auto-fix succeeded — promote .part to final, clear choke state.
    part.rename(out)
    duration_check = abs(walk.get("duration_seconds", 0) - duration)
    display.events.put(
        f"  + {chunk.name}: auto-fix OK ({elapsed:.0f}s, dur drift "
        f"{duration_check:.2f}s)")
    with display.lock:
        display.choked_chunks.pop(chunk.name, None)
        if not display.choked_chunks:
            display.has_choked_chunks.clear()
    record_chunk_elapsed(chunk.name, elapsed)
    # v1.18.0 fix: also emit the chunk_metrics base row. Without this, the
    # QualityGuard's later update_chunk_quality falls into the stub branch
    # (encode_elapsed_s=0, output_bytes=0) and last-wins aggregation
    # poisons elapsed/bitrate min for any auto-fix run. stat() failure
    # falls back to 0 the same way chunk_worker / encode_serial do.
    try:
        out_bytes = out.stat().st_size
    except OSError:
        out_bytes = 0
    record_chunk_metrics(
        chunk_name=chunk.name,
        encode_elapsed_s=elapsed,
        chunk_duration_s=duration,
        output_bytes=out_bytes,
    )
    return True


def write_needs_fix_sidecar(workdir: Path, chunk: Path, *,
                            chunk_index: int, seg_sec: int,
                            choke_info: dict, errors: Optional[dict],
                            original_x265_params: str,
                            original_preset: str, original_crf: int,
                            original_pix_fmt: str) -> Path:
    """Drop `enc_{chunk.stem}.needs_fix.json` next to the source chunk so a
    manual fix or a follow-up Claude conversation knows EXACTLY what needs
    to happen: which chunk, time range, error info, and the params the
    original encode used (so a fix attempt can start from the same baseline
    and relax selectively).

    This sidecar is the contract between the encoder ("here's a chunk I
    couldn't process and here's everything I know about why") and a fixer
    (Claude, the user, or a future automation): drop enc_{stem}.mkv at
    `expected_output_path` and re-run the .bat; resumable logic picks it
    up."""
    sidecar = workdir / f"enc_{chunk.stem}.needs_fix.json"
    payload = {
        "choked_chunk": chunk.name,
        "chunk_source_path": str(chunk.resolve()),
        "chunk_index": chunk_index,
        "expected_output_path": str((workdir / f"enc_{chunk.stem}.mkv").resolve()),
        "time_range_seconds": [chunk_index * seg_sec, (chunk_index + 1) * seg_sec],
        "choke_speed": round(choke_info.get("speed", 0.0), 5),
        "choke_wall_seconds": round(choke_info.get("wall_seconds", 0.0), 1),
        "original_params": {
            "crf": original_crf,
            "preset": original_preset,
            "pix_fmt": original_pix_fmt,
            "x265_params": original_x265_params,
        },
        "source_decode_errors": errors,
        "audio_handling_hint": (
            "if source has AAC corruption alongside h264, try "
            "`-c:a aac -af aresample=async=1` instead of `-c:a copy`"
        ),
    }
    # Atomic temp + os.replace, like every other load-bearing JSON sidecar
    # (quality, preflight cache, queue state): a kill mid-write must never
    # leave a truncated, unparseable contract file at the final name.
    try:
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, sidecar)
    except OSError as e:
        print(f"WARNING: failed to write needs_fix sidecar: {e}",
              file=sys.stderr)
    return sidecar

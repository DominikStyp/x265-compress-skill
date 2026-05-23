"""One-chunk encoding via ffmpeg + live display slot. Extracted from
`encoder.py` so the orchestration module stays thin.

`_encode_one_chunk_with_display` runs the per-chunk ffmpeg in a Popen with
`-progress -`, parses every progress line, pushes fps/speed/out_time +
**frame count** into the display slot so the live block can render honest
encode rates (rates are derived from a trailing-window of samples — see
`display._compute_live_rates_from_samples` — so they survive hibernation
gracefully instead of inheriting ffmpeg's broken cumulative averages).

On success the `.part.mkv` is renamed to the final `enc_*.mkv`. On failure
the `.part` is preserved verbatim — encoded bytes are user data per the
never-delete-encoded-chunks rule; `chunk_recovery._quarantine_part` is the
only sanctioned path for moving partial bytes aside, and only with a tag.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from platform_compat import low_priority_popen_kwargs

from .chunking import ffmpeg_chunk_cmd
from .chunk_recovery import _quarantine_part
from .display import ParallelDisplay
from .history_state import record_chunk_elapsed
from .probes import probe_duration


def _parse_progress_state(state: dict[str, str]) -> tuple[float, int]:
    """Pull out_time_s (seconds of encoded video) and frame count from the
    accumulated ffmpeg `-progress` key/value dict. Resilient to missing/empty
    values — falls back to 0."""
    try:
        out_us = float(state.get("out_time_us", "0") or 0)
    except ValueError:
        out_us = 0
    try:
        frame = int(state.get("frame", "0") or 0)
    except ValueError:
        frame = 0
    return out_us / 1_000_000, frame


def _encode_one_chunk_with_display(slot: int, chunk: Path, workdir: Path,
                                  display: ParallelDisplay, *,
                                  crf: int, preset: str, pix_fmt: str,
                                  x265_params: str
                                  ) -> tuple[Path, int, float, str]:
    """Encode `chunk` into the workdir, updating the live `slot` as ffmpeg
    progresses. Returns (chunk, exit_code, elapsed_s, stderr_tail).

    On success the .part.mkv is renamed to enc_*.mkv. On failure the .part
    is preserved — choke / threshold / encode-failed paths all reach this
    return; the worker's outer try/except decides whether the chunk gets
    quarantined."""
    out = workdir / f"enc_{chunk.stem}.mkv"
    part = workdir / f"enc_{chunk.stem}.part.mkv"
    if part.exists():
        # NEVER delete encoded bytes — quarantine the stale .part as forensic
        # evidence per the never-delete-encoded-chunks rule.
        _quarantine_part(part, "stale-pre-encode")
    duration = probe_duration(chunk)
    display.slot_start(slot, chunk.name, duration)

    start = time.monotonic()
    proc = subprocess.Popen(
        ffmpeg_chunk_cmd(chunk, part, crf=crf, preset=preset,
                         pix_fmt=pix_fmt, x265_params=x265_params,
                         extra_progress=["-progress", "-"]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
            out_time_s, frame = _parse_progress_state(state)
            display.slot_progress(
                slot,
                out_time_s=out_time_s,
                frame=frame,
                fps=state.get("fps", "?"),
                speed=state.get("speed", "?"),
            )

    rc = proc.wait()
    elapsed = time.monotonic() - start
    err = (proc.stderr.read() if proc.stderr else "") or ""
    display.unregister_proc(slot)

    if rc != 0:
        # NEVER unlink the .part on failure — encoded bytes are user data.
        # Threshold-abort and choke both reach this branch; the auto-fix
        # path needs the .part preserved so it can be quarantined as
        # preretry-aside (see chunk_recovery._quarantine_part).
        with display.lock:
            chunk_was_choked = chunk.name in display.choked_chunks
        if not (display.abort_event.is_set() or chunk_was_choked):
            display.slot_failed(slot, chunk.name, rc, err)
        else:
            with display.lock:
                display.slots.pop(slot, None)
        return chunk, rc, elapsed, err[-400:]
    part.rename(out)
    display.slot_done(slot, chunk.name, elapsed, chunk_duration=duration)
    # Record this chunk's elapsed wall time into the in-progress history
    # state so the JSONL log captures it.
    record_chunk_elapsed(chunk.name, elapsed)
    return chunk, 0, elapsed, ""

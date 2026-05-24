"""Size projection + max-size threshold abort for the parallel encoder.

Extracted from `display.py` (to keep that module under the 500-line cap) the
same way `choke_detection` was: `ParallelDisplay` keeps thin
`_compute_projection()` / `check_threshold()` methods that delegate here,
passing themselves in. All the shared state — workdir, slots, lock, the
projection cache, the abort plumbing — still lives on the ParallelDisplay
instance; these are free functions over it.
"""
from __future__ import annotations

import time
from typing import Optional


def compute_projection(display) -> dict:
    """Sum enc_*.mkv bytes + completed/active source seconds, project the
    final output size. Cached on the display for ~0.4 s so the render thread
    and check_threshold share one workdir walk per refresh cycle.

    Returned dict keys (always present, may be None / 0 if too early):
        encoded_s         source seconds finished or in flight
        progress_frac     encoded_s / total_duration   (0..)
        enc_bytes         bytes on disk in enc_*.mkv + .part.mkv
        bytes_per_sec     None until we have any encoded data
        projected_bytes   None until >=5% progress (estimate is too noisy
                          before that, both for the threshold check and
                          for the user-facing size bar)
    """
    now = time.monotonic()
    cached = display._projection_cache
    if cached is not None and (now - cached["ts"]) < display._projection_ttl_s:
        return cached

    enc_bytes = 0
    if display.workdir:
        try:
            for p in display.workdir.iterdir():
                name = p.name
                # Count finished chunks (enc_*.mkv) and the live in-progress
                # partial (enc_*.part.mkv), but NOT quarantined partials
                # (enc_*.part.<tag>-<ts>.mkv) left by a prior choked run —
                # those are abandoned bytes that would inflate the projection
                # (and the threshold check) on a resumed run.
                if name.startswith("enc_") and name.endswith(".mkv") and (
                    ".part." not in name or name.endswith(".part.mkv")
                ):
                    try:
                        enc_bytes += p.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass

    with display.lock:
        active_source_s = sum(s.get("out_time_s", 0)
                              for s in display.slots.values())
        completed_s = display.completed_duration_sum
    encoded_s = completed_s + active_source_s
    progress_frac = ((encoded_s / display.total_duration)
                     if display.total_duration > 0 else 0.0)

    bytes_per_sec: Optional[float] = None
    projected_bytes: Optional[float] = None
    if encoded_s > 0 and enc_bytes > 0:
        bytes_per_sec = enc_bytes / encoded_s
        # Same 5% gate that the threshold check used to apply inline. Below
        # that the byte-rate is dominated by initial-keyframe overhead and
        # the projection is misleading.
        if progress_frac >= 0.05 and display.total_duration > 0:
            projected_bytes = bytes_per_sec * display.total_duration

    proj = {
        "ts": now,
        "encoded_s": encoded_s,
        "progress_frac": progress_frac,
        "enc_bytes": enc_bytes,
        "bytes_per_sec": bytes_per_sec,
        "projected_bytes": projected_bytes,
    }
    display._projection_cache = proj
    return proj


def check_threshold(display) -> None:
    """Project final output size from work-so-far and abort if it exceeds the
    user's threshold. Only acts once >=5% of total source has been encoded —
    earlier than that the estimate is noisy."""
    if (not display.max_output_bytes or not display.workdir
            or display.abort_event.is_set()):
        return

    proj = compute_projection(display)
    estimated_total = proj["projected_bytes"]
    if estimated_total is None:           # too early (or no bytes yet)
        return
    if estimated_total <= display.max_output_bytes:
        return

    # Threshold exceeded — flag abort, kill running ffmpegs.
    progress = proj["progress_frac"]
    est_mb = estimated_total / (1024 * 1024)
    thr_mb = display.max_output_bytes / (1024 * 1024)
    pct_of_src = ((estimated_total / display.source_bytes * 100)
                  if display.source_bytes else 0)
    thr_pct = ((display.max_output_bytes / display.source_bytes * 100)
               if display.source_bytes else 0)
    display.abort_reason = (
        f"Estimated output {est_mb:.1f} MB ({pct_of_src:.1f}% of source) "
        f"exceeds threshold {thr_mb:.1f} MB ({thr_pct:.1f}%). "
        f"Stopped at {progress*100:.1f}% overall progress."
    )
    display.abort_event.set()
    with display.lock:
        procs = list(display.active_procs.values())
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass

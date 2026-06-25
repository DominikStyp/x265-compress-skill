"""Per-chunk choke detection — delta-based.

Split out of `display.py` to keep that module under the 500-line cap. The
detector identifies slots whose encoder is making no forward progress and
terminates ONLY that slot's ffmpeg (other slots keep encoding). The slot's
worker thread sees rc != 0, looks the chunk up in `display.choked_chunks`,
records a skip, pulls the next chunk. Skip-and-continue model — no global
abort.

The function is a free function (not a method) so unit tests can drive it
with synthetic snapshot dicts and a lightweight `Display`-like stub that
exposes the few attributes it touches (`abort_event`, `lock`, `slots`,
`active_procs`, `choked_chunks`, `has_choked_chunks`, `events`, plus the
choke tunables). In production it's called from `ParallelDisplay.check_choke`.
"""
from __future__ import annotations

import time
from typing import Optional, TYPE_CHECKING

from ._sample_window import select_window_endpoints

if TYPE_CHECKING:
    from .display import ParallelDisplay


def check_choke(display: "ParallelDisplay") -> Optional[tuple[int, str]]:
    """Detect a slot whose encoder is making no forward progress.

    Two-stage detection (after `choke_grace_seconds` of wall time):
      1. Look at the rolling `out_time_samples` deque populated by
         `slot_progress`.
      2. Compute the delta of `out_time_s` over the trailing
         `choke_window_seconds` (default 60s). If less than
         `choke_min_delta_seconds` (default 1.0s) of *video* was produced
         in that 60s wall window, declare choke.

    Delta metric — NOT cumulative-since-start. Matters because x265 slow
    preset on 4K can spend 60-120s of wall building its lookahead before
    flushing any `out_time_s` progress. The old cumulative check fired on
    such healthy warmups; the delta check fires only when the encoder
    actually stops producing video frames AFTER warming up.

    Returns (slot_id, chunk_name) on detection, or None. On detection:
    records the chunk in `display.choked_chunks` and terminates ONLY that
    slot's ffmpeg.

    Setting `choke_threshold_speed` OR `choke_grace_seconds` to 0 disables
    the detector entirely."""
    if (display.abort_event.is_set()
            or display.choke_threshold_speed <= 0
            or display.choke_grace_seconds <= 0):
        return None
    now = time.monotonic()
    now_wall = time.time()

    # System-sleep / hibernation guard. The render loop normally fires every
    # ~500 ms; a multi-minute gap between consecutive check_choke calls means
    # the whole process was suspended (the also-suspended ffmpeg produced no
    # fresh out_time samples during the nap, so every slot would otherwise look
    # choked on wake). Reset the slot bookkeeping and skip the verdict for THIS
    # cycle so ffmpeg can settle post-wake.
    #
    # Detect suspend clock-agnostically by tracking BOTH clocks: across a system
    # sleep the wall clock (time.time()) always advances by the real elapsed
    # time, but time.monotonic() FREEZES on macOS/Linux and KEEPS COUNTING on
    # Windows. A single-clock check only caught the Windows case and let macOS
    # fall through to a false choke. Tripping on max(monotonic_gap, wall_gap)
    # catches the suspend on every platform regardless of which clock froze.
    #
    # Wall time is non-monotonic, so a forward clock step (NTP/manual) >
    # sleep_threshold could trip this without a real suspend — but the only cost
    # is masking a genuine choke for ONE cycle (it reasserts on the next check
    # after the grace window re-elapses), and steady-state NTP slews rather than
    # steps. A backward step makes wall_gap negative, so max() falls back to the
    # monotonic gap — no spurious trip. The rare, self-healing false-positive is
    # far cheaper than the false-negative (every chunk restarted on every wake).
    last_check = getattr(display, "_last_choke_check_at", None)
    last_wall = getattr(display, "_last_choke_check_wall", None)
    sleep_threshold = getattr(display, "sleep_detect_seconds", 120.0)
    display._last_choke_check_at = now
    display._last_choke_check_wall = now_wall
    mono_gap = (now - last_check) if last_check is not None else 0.0
    wall_gap = (now_wall - last_wall) if last_wall is not None else 0.0
    gap = max(mono_gap, wall_gap)
    if (last_check is not None or last_wall is not None) and gap > sleep_threshold:
        with display.lock:
            for s in display.slots.values():
                s.t_start = now
                s.paused_s = 0.0
                if s.paused_at is not None:
                    s.paused_at = now
                s.out_time_samples.clear()
        display.events.put(
            f"  ~ Sleep/hibernation detected ({gap:.0f}s gap between checks) "
            f"— slot grace windows reset"
        )
        return None

    with display.lock:
        slots_snap = {k: v.copy() for k, v in display.slots.items()}
        active_procs = dict(display.active_procs)
        already_choked = set(display.choked_chunks.keys())
    for slot_id, s in slots_snap.items():
        chunk_name = s.chunk
        if chunk_name in already_choked:
            continue
        t_start = s.t_start
        if not t_start:
            continue
        paused_s = s.paused_s
        paused_at = s.paused_at
        # Same "real" wall calc as the per-slot ETA display: pause time
        # doesn't count against the choke grace window.
        if paused_at is not None:
            wall = max(0.0, paused_at - t_start - paused_s)
        else:
            wall = max(0.0, now - t_start - paused_s)
        if wall < display.choke_grace_seconds:
            continue
        # Delta check over the trailing window.
        samples_list = list(s.out_time_samples)
        if len(samples_list) < 2:
            # No or near-no progress reports despite passing grace.
            # That IS a real choke — fall through to terminate.
            delta_s = 0.0
            wall_delta = 0.0
            speed = 0.0
        else:
            # Anchor the trailing window on the current monotonic clock (NOT
            # the newest sample's timestamp — a stalled encoder stops emitting
            # samples, so "now" is what reveals the lack of progress).
            older, newer = select_window_endpoints(
                samples_list, now - display.choke_window_seconds)
            delta_s = max(0.0, newer[1] - older[1])
            wall_delta = max(0.0001, newer[0] - older[0])
            # Scale the required min-delta proportionally if we don't yet
            # have a full window of samples — don't kill on partial data.
            effective_min = (
                display.choke_min_delta_seconds
                * min(1.0, wall_delta / display.choke_window_seconds)
            )
            speed = delta_s / wall_delta if wall_delta > 0 else 0.0
            if delta_s >= effective_min:
                continue
        # Choke detected — record + tear down JUST this slot's ffmpeg.
        with display.lock:
            display.choked_chunks[chunk_name] = {
                "slot_id": slot_id,
                "speed": speed,
                "wall_seconds": wall,
                "delta_video_seconds": delta_s,
                "delta_wall_seconds": wall_delta,
            }
        display.has_choked_chunks.set()
        proc = active_procs.get(slot_id)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        display.events.put(
            f"  ! {chunk_name}: CHOKED ({delta_s:.2f}s video produced in "
            f"last {wall_delta:.0f}s wall, after {wall:.0f}s total) — "
            f"slot {slot_id+1} freed"
        )
        return (slot_id, chunk_name)
    return None

"""Pause/resume controls for the parallel encoder display.

Extracted from `display.py` (which is at the 500-line cap) the same way
`size_projection` and `choke_detection` were — as free functions that take the
`ParallelDisplay` instance. `ParallelDisplay` keeps thin delegate methods so the
public API (and every caller: the keyboard listener, the render loop, tests)
is unchanged.

Three ways to drive a pause, all landing here:
  * a slot key (Space / 1-9)  -> toggle_pause (per-slot)
  * `r`                        -> resume_all
  * a <workdir>/PAUSE file     -> sync_file_pause -> pause_all / resume_all
                                  (the no-keyboard path for headless/SSH runs)

Lock discipline mirrors the original methods: read/write of slots /
paused_slots / active_procs happens under `display.lock`; the suspend/resume
syscalls (which can block briefly) happen OUTSIDE the lock.
"""
from __future__ import annotations

import time

from platform_compat import resume_pid, suspend_pid


def mark_pause_start(display, slot: int) -> None:
    """Stamp the slot's `paused_at` with the current wall time. Caller must
    hold `display.lock`. Pairs with `settle_pause_elapsed` on resume."""
    s = display.slots.get(slot)
    if s:
        s["paused_at"] = time.monotonic()


def settle_pause_elapsed(display, slot: int) -> None:
    """Fold the in-flight pause window (since `paused_at`) into the slot's
    `paused_s` counter so per-chunk elapsed never counts suspended time. Caller
    must hold `display.lock`. No-op if the slot wasn't paused."""
    s = display.slots.get(slot)
    if s and s.get("paused_at") is not None:
        s["paused_s"] = s.get("paused_s", 0.0) + (
            time.monotonic() - s["paused_at"])
        s["paused_at"] = None


def toggle_pause(display, slot: int) -> str:
    """Toggle suspension of the ffmpeg currently in `slot`. Returns a one-line
    status message for the events log.

    `slot` is the internal 0-based index; messages show the 1-based human label
    (slot 0 -> "slot 1") so the display, the keys (1-9), and the event log all
    agree."""
    label = slot + 1
    with display.lock:
        if slot < 0 or slot >= display.parallel:
            return f"  ! no such slot: {label}"
        proc = display.active_procs.get(slot)
        if not proc:
            return f"  ! slot {label}: idle, no ffmpeg to pause"
        pid = proc.pid
        currently_paused = slot in display.paused_slots
    if currently_paused:
        if resume_pid(pid):
            with display.lock:
                display.paused_slots.discard(slot)
                settle_pause_elapsed(display, slot)
            return f"  > slot {label}: RESUMED (PID {pid})"
        return f"  ! slot {label}: resume failed for PID {pid}"
    if suspend_pid(pid):
        with display.lock:
            display.paused_slots.add(slot)
            mark_pause_start(display, slot)
        return f"  || slot {label}: PAUSED (PID {pid})"
    return f"  ! slot {label}: suspend failed for PID {pid}"


def pause_all(display) -> list[str]:
    """Suspend every active, not-already-paused slot. The aggregate counterpart
    to resume_all(), used by the file-based PAUSE sentinel. Symmetric with
    resume_all (snapshot under lock, then act) rather than looping toggle_pause
    — that avoids a race where a slot paused between the snapshot and the call
    would get flipped back to running."""
    with display.lock:
        targets = [s for s in display.active_procs
                   if s not in display.paused_slots]
        procs = {s: display.active_procs.get(s) for s in targets}
    msgs: list[str] = []
    for slot in targets:
        proc = procs.get(slot)
        if not proc:
            continue
        if suspend_pid(proc.pid):
            with display.lock:
                display.paused_slots.add(slot)
                mark_pause_start(display, slot)
            msgs.append(f"  || slot {slot + 1}: PAUSED (PID {proc.pid})")
        else:
            msgs.append(f"  ! slot {slot + 1}: suspend failed for "
                        f"PID {proc.pid}")
    if not msgs:
        msgs.append("  (no active slots to pause)")
    return msgs


def resume_all(display) -> list[str]:
    """Resume every paused slot. Returns one status message per slot."""
    with display.lock:
        paused = list(display.paused_slots)
        procs = {s: display.active_procs.get(s) for s in paused}
    msgs: list[str] = []
    for slot in paused:
        label = slot + 1
        proc = procs.get(slot)
        if not proc:
            with display.lock:
                display.paused_slots.discard(slot)
            continue
        if resume_pid(proc.pid):
            with display.lock:
                display.paused_slots.discard(slot)
                settle_pause_elapsed(display, slot)
            msgs.append(f"  > slot {label}: RESUMED (PID {proc.pid})")
        else:
            msgs.append(f"  ! slot {label}: resume failed for PID {proc.pid}")
    if not msgs:
        msgs.append("  (no paused slots)")
    return msgs


def sync_file_pause(display) -> None:
    """Honor the <workdir>/PAUSE sentinel: keep all active slots suspended while
    it exists, resume them once it's removed. Polled every render tick.

    The SUSPEND side is LEVEL-triggered, not edge-triggered: every tick that the
    file exists we re-run pause_all, which suspends any active slot that isn't
    already paused. This matters because a chunk boundary starts a FRESH ffmpeg
    in a slot (register_proc clears that slot's paused state) — an edge-triggered
    "pause once when the file appears" would let that new chunk run at full speed
    while PAUSE is still present, defeating the feature. pause_all skips
    already-paused slots, so re-running it each tick is idempotent (one SIGSTOP
    per ffmpeg). It also self-heals the state if a keyboard `r` resumed a slot
    while the file is still present — the file wins on the next tick.

    The banner is printed once per file appearance (`_file_paused` edge); after
    that only genuinely-new suspensions are logged, so steady-state ticks are
    silent.

    NOTE: while the file exists the encode stays suspended — including at the
    point it would otherwise finish. Remove the file to let it complete (or use
    FINISH for a graceful stop-after-current-chunk).

    NEVER raises — it runs inside the render thread, where an escape would also
    disable the size guard + choke killer for that tick (see `_render_tick`)."""
    pause_file = display._pause_file
    if pause_file is None:
        return
    try:
        present = pause_file.exists()
    except OSError:
        return
    if present:
        newly_paused = [m for m in pause_all(display) if "PAUSED" in m]
        if not display._file_paused:
            display.events.put("  || PAUSE file present — all active slots "
                               "suspended (delete the PAUSE file to resume).")
            display._file_paused = True
        else:
            for msg in newly_paused:   # a fresh chunk got re-paused this tick
                display.events.put(msg)
    elif display._file_paused:
        for msg in resume_all(display):
            display.events.put(msg)
        display.events.put("  > PAUSE file removed — all slots resumed.")
        display._file_paused = False


def move_focus(display, delta: int) -> None:
    with display.lock:
        display.focused_slot = (display.focused_slot + delta) % display.parallel

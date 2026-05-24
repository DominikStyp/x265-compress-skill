"""Pure render-string helpers for the parallel-encode live display.

Extracted from `_ParallelDisplay` so the class stays under 500 lines and its
state/threading machinery isn't tangled with the formatting code. Every
function here is **pure**: takes plain dicts / primitives, returns a string.
No locks, no I/O, no side effects.

Hard width rule: every emitted line stays under 80 characters when stripped
of ANSI escapes. Wider lines wrap in default-config cmd.exe, and the wrap
silently corrupts the cursor-up math `_ParallelDisplay.render()` uses to
overdraw the previous frame. The constants below (BAR_WIDTH=14, separator
width 75) are chosen against that budget — change them only after measuring
the resulting line widths.
"""
from __future__ import annotations

import time
from typing import Optional

from .probes import fmt_dur


# Kept narrow so every live-block line fits inside default 80-col cmd.exe.
# Wider bars caused terminal line-wrapping, which silently broke the
# cursor-up math used to overdraw the previous frame (leaving ghost rows).
BAR_WIDTH = 14

# Box-drawing horizontal rule visually splits per-chunk metrics (above)
# from overall-file metrics (below). U+2500; the .bat sets chcp 65001.
# Kept at 75 cols so it stays inside the default 80-col cmd.exe width.
SEPARATOR_LINE = "  " + ("─" * 75)


# ANSI SGR codes — bright bold so they pop in cmd.exe's default palette.
# Reset is applied right after the colored span so surrounding text /
# subsequent lines render in default colors.
C_GREEN = "\033[32;1m"
C_RED = "\033[31;1m"
C_RESET = "\033[0m"


def slot_elapsed_seconds(state: dict) -> Optional[float]:
    """Wall-clock seconds since this chunk began encoding, with any time
    the slot was suspended (via NtSuspendProcess) subtracted out.

    Returns None when the slot hasn't started yet (no t_start). When the
    slot is currently paused (paused_at set), elapsed freezes at the
    instant of suspension — repeated calls return the same value until
    the slot resumes."""
    t_start = state.get("t_start")
    if t_start is None:
        return None
    paused_s = state.get("paused_s", 0.0)
    paused_at = state.get("paused_at")
    if paused_at is not None:
        return max(0.0, paused_at - t_start - paused_s)
    return max(0.0, time.monotonic() - t_start - paused_s)


def slot_eta_str(elapsed_s: Optional[float], pct: float, *, paused: bool) -> str:
    """Per-chunk ETA: 'paused' while suspended; '—' before the chunk has
    accumulated enough progress to extrapolate (>0.5%); otherwise
    fmt_dur of a linear extrapolation from current elapsed / pct."""
    if paused:
        return "paused"
    if elapsed_s is None or pct <= 0.5:
        return "—"
    return fmt_dur(max(0.0, elapsed_s * (100.0 - pct) / pct))


def slot_bar(pct: float, *, paused: bool) -> str:
    """The colored progress bar shown inside `[slot N] ... [###---]` —
    red while paused so the eye snaps to it, green during normal encode."""
    filled = max(0, min(BAR_WIDTH, int(round(pct / 100 * BAR_WIDTH))))
    bar_inner = "#" * filled + "-" * (BAR_WIDTH - filled)
    bar_color = C_RED if paused else C_GREEN
    return f"{bar_color}{bar_inner}{C_RESET}"


def render_slot_main(slot_id: int, state: Optional[dict],
                    *, paused: bool, focused: bool) -> str:
    """First of two rows per slot — chunk name, bar, percent, fps, speed.
    Stays well under 80 chars so it doesn't wrap in default cmd.exe (line
    wrapping silently corrupts the cursor-up math used to overdraw the
    previous frame). When paused, the red bar color is the indicator;
    the [PAUSED] label moves to the timing row."""
    # 1-based label so it lines up with the digit keys (key '1' = slot 1).
    label = slot_id + 1
    cursor = "> " if focused else "  "
    if not state:
        return f"{cursor}[slot {label}] idle"
    dur = state["duration"]
    pct = min(100.0, state["out_time_s"] / dur * 100) if dur else 0
    bar = slot_bar(pct, paused=paused)
    name = state["chunk"]
    suffix = state.get("label_suffix", "")
    # Reserve space for "(AUTO-FIX)" so the bar still lines up — name+suffix
    # together fit in a 14-char slot (chunk names like "src_0008.mkv" are
    # 12 chars, leaving room).
    if suffix:
        name = f"{name} ({suffix})"
        max_len = 24
    else:
        max_len = 14
    if len(name) > max_len:
        name = name[:max_len - 1] + "…"
    # Prefer the locally-computed live rates (rolling-window derivative
    # from the samples deque) over ffmpeg's cumulative averages. ffmpeg's
    # numbers get permanently corrupted by hibernation — its wall clock
    # advances during sleep but the frame counter doesn't, so post-wake
    # the cumulative average reads ~0 forever. See
    # `display._compute_live_rates_from_samples` for the algorithm.
    fps = state.get("live_fps") or state["fps"]
    speed = state.get("live_speed") or state["speed"]
    return (f"{cursor}[slot {label}] {name:<{max_len}} [{bar}] "
            f"{pct:5.1f}%  fps={fps:>5}  speed={speed:>6}")


def render_slot_timing(slot_id: int, state: Optional[dict],
                      *, paused: bool) -> str:
    """Second of two rows per slot — per-chunk elapsed wall time and
    extrapolated ETA, indented under the slot row above. Carries the
    [PAUSED] label too (kept off the main row to preserve its width
    budget). Separate row because cramming this onto the main row
    pushes it past 80 cols and triggers terminal wrapping."""
    if not state:
        return "             (idle)"
    dur = state["duration"]
    pct = min(100.0, state["out_time_s"] / dur * 100) if dur else 0
    elapsed_s = slot_elapsed_seconds(state)
    elapsed_str = fmt_dur(elapsed_s) if elapsed_s is not None else "—"
    eta_str = slot_eta_str(elapsed_s, pct, paused=paused)
    pause_label = f" {C_RED}[PAUSED]{C_RESET}" if paused else ""
    return (f"             elapsed {elapsed_str:>7}    "
            f"ETA {eta_str:>7}{pause_label}")


def render_help(has_key_input: bool, finish_requested: bool = False) -> str:
    if finish_requested:
        # Persistent banner so the pending stop is visible on every frame.
        return ("  FINISH AFTER CHUNK: ON — stopping once in-flight chunks "
                "finish (f=cancel)")
    if not has_key_input:
        return "  (keyboard pause/resume unavailable on this platform)"
    return ("  Keys: ↑↓ focus  Space toggle  1-9 slot N  "
            "r resume all  f finish  ? help")


def render_chunks_line(already: int, completed_now: int, total: int,
                      start_time: float) -> str:
    """Discrete chunk-count progress: only ticks when a chunk finishes.
    Kept alongside the smooth bar so it's easy to see at a glance how
    many of N chunks are physically on disk."""
    overall_done = already + completed_now
    pct = overall_done / total * 100 if total else 100
    wall = time.monotonic() - start_time
    if completed_now > 0:
        rate = completed_now / wall  # chunks per second (this run only)
        remaining = total - overall_done
        eta = remaining / rate if rate > 0 else 0
    else:
        eta = 0
    return (f"  Chunks:   {overall_done:>3} / {total} done ({pct:5.1f}%)  "
            f"elapsed {fmt_dur(wall)}  ETA {fmt_dur(eta)}")


def render_progress_line(projection: dict, total: int, start_time: float) -> str:
    """Smooth full-file progress: counts completed chunks AND the partial
    progress of in-flight chunks. With --parallel 1 this gives a bar that
    moves continuously (the chunks-line above only ticks every chunk)."""
    frac = projection["progress_frac"]
    pct = frac * 100
    pct_capped = min(100.0, pct)
    filled = max(0, min(BAR_WIDTH, int(round(pct_capped / 100 * BAR_WIDTH))))
    bar_inner = "#" * filled + "-" * (BAR_WIDTH - filled)
    bar = f"{C_GREEN}{bar_inner}{C_RESET}"

    # "X.XX of N chunks" — gives the user an intuitive feel for where the
    # smooth bar sits relative to the discrete one above.
    chunks_equiv = frac * total if total else 0

    # ETA based on the smooth fraction is much steadier than the chunk-based
    # one above (which only updates at chunk completion). Both are shown so
    # you can sanity-check one against the other when they diverge.
    wall = time.monotonic() - start_time
    if frac > 0.005:
        eta = wall * (1 - frac) / frac
        eta_str = fmt_dur(max(0, eta))
    else:
        eta_str = "—"

    return (f"  Progress: [{bar}] {pct:5.1f}%  "
            f"({chunks_equiv:5.2f} of {total})  ETA {eta_str}")


def render_size_line(projection: dict, source_bytes: int,
                    max_output_bytes: Optional[int]) -> str:
    """Live size projection: bar fill = projected_output / source_bytes.
    Threshold (--max-size-percent) is shown as a `|` marker inside the bar.
    Bar turns red once the projection exceeds the threshold; the encode
    is aborted by check_threshold() on the same render tick, so the red
    state is visible for at most one frame before the abort fires."""
    src_bytes = source_bytes or 0
    src_mb = src_bytes / (1024 * 1024)

    threshold_pct: Optional[float] = None
    if max_output_bytes and src_bytes:
        threshold_pct = max_output_bytes / src_bytes * 100

    def _apply_threshold_marker(bar: str) -> str:
        if threshold_pct is None:
            return bar
        col = max(0, min(BAR_WIDTH - 1,
                        int(round(threshold_pct / 100 * BAR_WIDTH))))
        return bar[:col] + "|" + bar[col + 1:]

    thr_text = f"  thr {threshold_pct:4.1f}%" if threshold_pct is not None else ""

    if projection["projected_bytes"] is None:
        # No estimate yet — show an empty bar with the threshold marker
        # still in place so the user can see where the budget sits.
        bar_inner = _apply_threshold_marker("-" * BAR_WIDTH)
        bar = f"{C_GREEN}{bar_inner}{C_RESET}"
        return (f"  Size:     [{bar}]    (est)  "
                f"src {src_mb:7.1f} MB{thr_text}")

    projected_bytes = projection["projected_bytes"]
    proj_mb = projected_bytes / (1024 * 1024)
    pct_of_src = (projected_bytes / src_bytes * 100) if src_bytes else 0
    pct_capped = min(100.0, pct_of_src)
    filled = max(0, min(BAR_WIDTH, int(round(pct_capped / 100 * BAR_WIDTH))))
    bar_inner = _apply_threshold_marker("#" * filled + "-" * (BAR_WIDTH - filled))
    over_threshold = (threshold_pct is not None) and (pct_of_src > threshold_pct)
    bar_color = C_RED if over_threshold else C_GREEN
    bar = f"{bar_color}{bar_inner}{C_RESET}"

    return (f"  Size:     [{bar}] {pct_of_src:5.1f}%  "
            f"{proj_mb:7.1f}/{src_mb:7.1f} MB{thr_text}")

"""Live parallel-encode display: thread-safe state, threshold projection,
ANSI in-place rendering. Pairs with `display_render` (pure render-string
helpers) and `keyboard_input` (htop-style pause/resume listener).

The `ParallelDisplay` class is the central shared state across:
  - N worker threads (one per encode slot) that mutate slot dicts.
  - The render thread that snapshots state under self.lock and redraws.
  - The keyboard listener (in `keyboard_input`) that toggles pause/focus.
  - The encoder's threshold check that may abort all in-flight ffmpegs.

Lock discipline: every read or write of self.slots / self.paused_slots /
self.active_procs / self.focused_slot is wrapped in `with self.lock`.
self.events is its own thread-safe queue.Queue.

Layout of one rendered frame (per render call):
       [slot 1] <chunk>          [#####------]  42.3%  fps=15  speed=0.31x
                elapsed 0:01:12  ETA 0:01:38
       [slot 2] <chunk>          [###--------]  21.5%  fps=14  speed=0.29x
                elapsed 0:00:34  ETA 0:02:05
       ─────────────────────────────────────────────────────────────────────
       Chunks:    7 / 31 done (22.6%)  elapsed 0:03:12  ETA 0:08:45
       Progress: [#####------] 42.3%  (13.10 of 31)  ETA 0:08:45
       Size:     [###--|-----] 28.4%  1234.5/4350.0 MB  thr 80.0%
       Keys: ↑↓ focus  Space toggle  1-9 slot N  r resume all  ? help

That's `2*N + 5` lines, which is the cursor-up offset on the next redraw.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from platform_compat import (
    HAS_KEY_INPUT,
    assign_to_lifetime_group,
    create_lifetime_group,
)

from .finish_signal import FINISH_FILENAME, FinishSignal
from .probes import probe_duration
from . import display_render as render


# Re-export for callers that previously read these from the class. Module-level
# now since the rendering helpers live in display_render.
BAR_WIDTH = render.BAR_WIDTH

# Sentinel filename inside the encode workdir. Creating <workdir>/PAUSE suspends
# every active slot; deleting it resumes them. The file-based counterpart to the
# Space/1-9 keys for headless / over-SSH runs (mirrors FINISH_FILENAME).
PAUSE_FILENAME = "PAUSE"


def _compute_live_rates_from_samples(samples,
                                     window_s: float = 5.0) -> tuple[str, str]:
    """Compute (live_speed_str, live_fps_str) from the trailing-window
    `(t, out_time_s, frame)` samples deque.

    Robust to hibernation: choke_detection clears the deque on sleep
    detect, so right after wake we'd see < 2 samples and return ("?", "?")
    — leaving the slot row showing question marks instead of ffmpeg's
    poisoned 0.0041x. Once ffmpeg emits 2+ post-wake progress lines (~1 s)
    real rates appear again.

    `window_s=5.0` is chosen to balance smoothness vs responsiveness:
    a 5 s sliding window irons out per-frame jitter (especially with
    --parallel where pool contention spikes briefly) while still
    reacting visibly to a real speed change."""
    samples_list = list(samples) if samples else []
    if len(samples_list) < 2:
        return "?", "?"
    now_t, now_out, now_frame = samples_list[-1]
    window_start = now_t - window_s
    older = None
    for sample in samples_list:
        if sample[0] >= window_start:
            older = sample
            break
    if older is None:
        older = samples_list[0]
    older_t, older_out, older_frame = older
    dt = now_t - older_t
    if dt <= 0.1:
        return "?", "?"
    speed = (now_out - older_out) / dt
    fps = (now_frame - older_frame) / dt
    return f"{speed:.3f}x", f"{fps:.1f}"


# HAS_KEY_INPUT is imported from platform_compat (above) — the SINGLE source of
# truth that the keyboard listener (keyboard_input.py) also gates on. It is True
# only when interactive key input is actually available (Windows console, or a
# POSIX TTY with termios). This module previously re-derived it via `import
# msvcrt`, which is Windows-only — so on macOS/Linux it was always False and the
# help footer wrongly claimed "keyboard pause/resume unavailable on this
# platform" even though the listener was running and the pause keys worked.


class ParallelDisplay:
    """ANSI-based in-place renderer for parallel encoding — see module
    docstring for the rendered layout. Owns all shared state for the
    encoder/render/keyboard threads."""

    def __init__(self, parallel: int, total: int, already_done: int,
                 *, workdir: Optional[Path] = None,
                 total_duration_sec: float = 0,
                 source_bytes: int = 0,
                 max_output_bytes: Optional[int] = None,
                 choke_threshold_speed: float = 0.05,
                 choke_grace_seconds: float = 300.0) -> None:
        self.parallel = parallel
        self.total = total
        self.already = already_done
        self.completed = 0  # done *during this run*
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.slots: dict[int, dict] = {}  # slot_id -> {chunk, duration, out_time_s, fps, speed}
        self.events: queue.Queue[str] = queue.Queue()
        self.printed_live = False
        # Headless / non-tty output: when stdout isn't a terminal (piped to a
        # log, nohup, systemd journal, CI), the ANSI in-place redraw corrupts
        # the log — render() then switches to plain appended event lines + a
        # throttled progress summary. Mirrors quality.py's isatty() gate.
        self._is_tty = sys.stdout.isatty()
        self._last_plain_summary = 0.0
        self._plain_summary_interval_s = 30.0

        # Threshold-abort plumbing
        self.workdir = workdir
        # 'Finish after current chunk' request — set by the `f` key (below) or
        # by a <workdir>/FINISH stop-file (headless/serial). Workers consult it
        # between chunks; it never interrupts an in-flight chunk.
        self.finish_signal = FinishSignal(
            (workdir / FINISH_FILENAME) if workdir else None)
        # File-based pause: the no-keyboard counterpart to Space/1-9. While a
        # <workdir>/PAUSE sentinel exists, all active slots are suspended;
        # removing it resumes them. Polled from the render loop (sync_file_pause)
        # so it works in headless/over-SSH runs where the key listener is off.
        # `_file_paused` debounces so we only suspend/resume on the file's edges.
        self._pause_file = (workdir / PAUSE_FILENAME) if workdir else None
        self._file_paused = False
        self.total_duration = total_duration_sec
        self.source_bytes = source_bytes
        self.max_output_bytes = max_output_bytes
        self.abort_event = threading.Event()
        self.abort_reason = ""
        self.active_procs: dict[int, subprocess.Popen] = {}

        # Interactive pause/resume state. paused_slots tracks which slot indices
        # currently hold a suspended ffmpeg; focused_slot is the htop-style
        # cursor position for the Space key.
        self.paused_slots: set[int] = set()
        self.focused_slot: int = 0
        # The keyboard listener sets this after every handled keypress so the
        # render thread wakes immediately and the cursor / [PAUSED] tag move
        # without the 500 ms periodic-refresh delay.
        self.input_event = threading.Event()
        # Windows Job Object: any ffmpeg assigned to this job dies the moment
        # this Python process exits — clean, Ctrl+C, taskkill /F, BSOD, all of
        # them. Without it, paused ffmpegs orphaned by a hard kill would stay
        # suspended forever and need manual cleanup. None on non-Windows or if
        # the API fails (we fall back to the resume_all-on-exit safety net).
        # OS-portable kill-on-parent-exit guarantee. On Windows this is a
        # Job Object (bulletproof — survives taskkill /F and BSOD). On
        # POSIX it's a process-group + atexit/SIGTERM handler; covers
        # graceful exits and Ctrl+C, but SIGKILL of the parent still
        # orphans children (no POSIX equivalent of Win32 Job Objects).
        self._lifetime_group = create_lifetime_group()
        # Source seconds already encoded in *previous* runs (i.e. existing enc_*.mkv).
        # Probed from the matching src_*.mkv files so the size projection starts
        # from a correct baseline even on resume.
        self.completed_duration_sum = 0.0
        if workdir and workdir.exists():
            for enc in workdir.glob("enc_*.mkv"):
                src = workdir / enc.name[len("enc_"):]
                if src.exists():
                    self.completed_duration_sum += probe_duration(src)

        # Shared projection cache. Both check_threshold() (every render) and the
        # new size / smooth-progress bars (every render) need the same numbers
        # (sum of enc_*.mkv bytes + sum of source-seconds encoded). Caching with
        # a TTL < the 500 ms render interval means we walk the workdir at most
        # once per render even though three callers ask for the values.
        self._projection_cache: Optional[dict] = None
        self._projection_ttl_s = 0.4

        # Per-chunk choke detection (NOT a file-level abort). When check_choke
        # identifies a slot whose chunk's encode speed stays below
        # choke_threshold_speed past choke_grace_seconds, ONLY that slot's
        # ffmpeg is terminated. The worker thread sees rc!=0, looks the chunk
        # up in choked_chunks, records the skip, pulls the next chunk. Other
        # slots keep encoding. Either threshold = 0 disables the guard.
        self.choke_threshold_speed = max(0.0, choke_threshold_speed)
        self.choke_grace_seconds = max(0.0, choke_grace_seconds)
        # Delta-based choke parameters. After grace expires, the detector
        # looks at out_time_s growth over the trailing `choke_window_seconds`
        # window; if growth is below `choke_min_delta_seconds`, the chunk
        # is considered stuck. This sidesteps the old cumulative-average
        # false-positive where x265's lookahead delay made healthy 4K slow
        # encodes look choked at the 300s mark.
        self.choke_window_seconds = 60.0
        self.choke_min_delta_seconds = 1.0
        # System-sleep / hibernation guard. The render loop calls check_choke
        # at ~2 Hz (every 500 ms). A huge gap between two consecutive calls
        # means the whole process was suspended (sleep / hibernate) — without
        # this guard, every slot looks "choked for hours" with an empty samples
        # deque (no ffmpeg progress flowed during sleep). check_choke detects
        # the gap clock-agnostically from BOTH clocks (see choke_detection):
        # across suspend the wall clock always advances, while time.monotonic()
        # freezes on macOS/Linux and keeps counting on Windows. When the larger
        # gap exceeds `sleep_detect_seconds`, it resets every slot's t_start to
        # `now` and clears out_time_samples so the grace window restarts cleanly.
        self.sleep_detect_seconds = 120.0
        self._last_choke_check_at: Optional[float] = None
        self._last_choke_check_wall: Optional[float] = None
        self.choked_chunks: dict[str, dict] = {}
        self.has_choked_chunks = threading.Event()

    @property
    def has_job_protection(self) -> bool:
        """True iff the lifetime-group cleanup is wired up (Win32 Job
        Object on Windows, atexit-managed process-group on POSIX). False
        means parent-death cleanup is unavailable on this platform."""
        return bool(self._lifetime_group)

    # --- worker-side updates (called from worker threads) ---

    def slot_start(self, slot: int, chunk_name: str, duration: float,
                  *, label_suffix: str = "") -> None:
        with self.lock:
            self.slots[slot] = {
                "chunk": chunk_name,
                # Optional rendered tag e.g. "AUTO-FIX" when an auto-fix
                # retry re-occupies a slot. The render layer appends
                # " ({label_suffix})" after the chunk name.
                "label_suffix": label_suffix,
                "duration": max(0.001, duration),
                "out_time_s": 0.0,
                "fps": "?",
                "speed": "?",
                # Wall-clock start for per-chunk elapsed + ETA. Excludes time
                # the slot spent paused — `paused_at` accumulates into `paused_s`
                # on resume, and elapsed = (now - t_start) - paused_s.
                "t_start": time.monotonic(),
                "paused_s": 0.0,
                "paused_at": None,
                # Rolling (monotonic_t, out_time_s) samples for delta-based
                # choke detection. Bounded deque keeps memory constant per
                # slot; check_choke trims by time when reading.
                "out_time_samples": deque(maxlen=256),
            }

    def slot_progress(self, slot: int, **fields) -> None:
        """Apply a progress update from ffmpeg's `-progress -` stream.

        Recognised fields: `out_time_s` (float), `frame` (int), `fps`,
        `speed`. Whenever `out_time_s` is included we append a fresh
        sample to the rolling deque and recompute the slot's live rates
        — that's what the render layer displays, NOT ffmpeg's cumulative
        averages (which are baked-in-broken after a hibernation gap,
        since ffmpeg's internal wall-clock advances during S3 sleep but
        its frame counter doesn't)."""
        with self.lock:
            s = self.slots.get(slot)
            if s is None:
                return
            s.update(fields)
            if "out_time_s" in fields:
                samples = s.get("out_time_samples")
                if samples is not None:
                    samples.append((
                        time.monotonic(),
                        float(fields["out_time_s"]),
                        int(fields.get("frame", 0) or 0),
                    ))
                    live_speed, live_fps = _compute_live_rates_from_samples(
                        samples)
                    s["live_speed"] = live_speed
                    s["live_fps"] = live_fps

    def slot_done(self, slot: int, chunk_name: str, elapsed: float,
                  chunk_duration: float = 0.0) -> None:
        with self.lock:
            self.slots.pop(slot, None)
            self.completed += 1
            self.completed_duration_sum += chunk_duration
            self.active_procs.pop(slot, None)
        self.events.put(f"  + {chunk_name}: done in {elapsed:5.1f}s")

    def register_proc(self, slot: int, proc: subprocess.Popen) -> None:
        with self.lock:
            # Abort race: a worker launches its ffmpeg (chunk_worker Popen)
            # and only THEN calls register_proc. If a threshold/choke abort
            # fired in that window, its terminate sweep already snapshotted
            # active_procs under this same lock — and abort_event is set
            # BEFORE that snapshot — so adopting this proc now would leave a
            # live ffmpeg the sweep never sees, running to completion
            # unsupervised. Refuse it; kill it below (outside the lock).
            aborting = self.abort_event.is_set()
            if not aborting:
                self.active_procs[slot] = proc
                # New ffmpeg in this slot starts unsuspended; clear any stale
                # paused-state from a prior chunk in the same slot.
                self.paused_slots.discard(slot)
        if aborting:
            try:
                proc.terminate()
            except Exception:
                pass
            return
        # Tie the child to our lifetime group so it dies with us. Done
        # OUTSIDE the lock — both backends do platform syscalls (Win32
        # AssignProcessToJobObject; POSIX set-add to the tracked group);
        # the lock only needs to cover the dict mutation above.
        if self._lifetime_group:
            assign_to_lifetime_group(proc.pid, self._lifetime_group)

    def unregister_proc(self, slot: int) -> None:
        with self.lock:
            self.active_procs.pop(slot, None)

    def slot_failed(self, slot: int, chunk_name: str, rc: int, err: str) -> None:
        with self.lock:
            self.slots.pop(slot, None)
        snippet = err.strip().replace("\n", " ")[:120]
        self.events.put(f"  ! {chunk_name}: FAILED (exit {rc}): {snippet}")

    # --- interactive pause/resume controls ---
    #
    # The implementations live in `pause_control` (display.py is at the 500-line
    # cap); these are thin delegates so the public API + every caller (keyboard
    # listener, render loop, tests) is unchanged. Same pattern as check_threshold
    # / check_choke below.

    def toggle_pause(self, slot: int) -> str:
        from . import pause_control
        return pause_control.toggle_pause(self, slot)

    def pause_all(self) -> list[str]:
        from . import pause_control
        return pause_control.pause_all(self)

    def resume_all(self) -> list[str]:
        from . import pause_control
        return pause_control.resume_all(self)

    def sync_file_pause(self) -> None:
        from . import pause_control
        return pause_control.sync_file_pause(self)

    def move_focus(self, delta: int) -> None:
        from . import pause_control
        return pause_control.move_focus(self, delta)

    def toggle_finish(self) -> str:
        """Toggle the 'finish after current chunk' request (keyboard `f`).
        Returns a one-line status for the event log. When ON, workers stop
        pulling new chunks once their current chunk completes, and the encode
        exits resumably (re-run to continue)."""
        if self.finish_signal.toggle():
            return ("  >> FINISH AFTER CURRENT CHUNK: ON — no new chunks will "
                    "start; in-flight chunks finish, then it stops "
                    "(re-run to resume).")
        return "  >> FINISH AFTER CURRENT CHUNK: OFF — continuing normally."

    # --- size projection + threshold ---

    def _compute_projection(self) -> dict:
        """Sum enc_*.mkv bytes + project the final output size. Thin delegate
        to size_projection.compute_projection — kept thin to honor the
        500-line module cap. The projection cache and every input still live
        on this instance; see that module for the math + returned keys."""
        from . import size_projection
        return size_projection.compute_projection(self)

    def check_choke(self) -> Optional[tuple[int, str]]:
        """Delegate to choke_detection.check_choke — kept thin to honor the
        500-line module cap. See that module for the algorithm + tunables."""
        from . import choke_detection
        return choke_detection.check_choke(self)

    def check_threshold(self) -> None:
        """Project output size and abort the encode if it exceeds the user's
        threshold. Thin delegate to size_projection.check_threshold — kept
        thin to honor the 500-line module cap; see that module."""
        from . import size_projection
        return size_projection.check_threshold(self)

    # --- rendering (called from a single render thread) ---

    def render(self) -> None:
        events_now: list[str] = []
        while True:
            try:
                events_now.append(self.events.get_nowait())
            except queue.Empty:
                break

        if not self._is_tty:
            self._render_plain(events_now)
            return

        if self.printed_live:
            # Move cursor up by the size of the block we drew last time:
            #   2 rows per slot (main + timing) + 1 divider + chunks line
            #   + smooth-progress line + size line + help footer
            #   = 2*N + 5 lines. Splitting each slot into two rows keeps each
            #   row under ~85 chars so it doesn't wrap in default 80-col cmd.exe;
            #   line-wrapping in the terminal silently corrupted the cursor-up
            #   math (each wrap added a visual row the math didn't count).
            sys.stdout.write(f"\033[{2 * self.parallel + 5}A")

        # Each event overwrites one line of the (about-to-be-redrawn) live
        # block; then we re-draw the block fresh below. Net effect: events
        # scroll up out of the live area into history.
        for evt in events_now:
            sys.stdout.write("\033[K" + evt + "\n")

        with self.lock:
            slots = {k: dict(v) for k, v in self.slots.items()}
            paused = set(self.paused_slots)
            focused = self.focused_slot
            completed_now = self.completed

        for slot_id in range(self.parallel):
            state = slots.get(slot_id)
            is_paused = slot_id in paused
            is_focused = slot_id == focused
            main = render.render_slot_main(slot_id, state,
                                          paused=is_paused, focused=is_focused)
            timing = render.render_slot_timing(slot_id, state, paused=is_paused)
            sys.stdout.write("\033[K" + main + "\n")
            sys.stdout.write("\033[K" + timing + "\n")
        sys.stdout.write("\033[K" + render.SEPARATOR_LINE + "\n")
        sys.stdout.write("\033[K" + render.render_chunks_line(
            self.already, completed_now, self.total, self.start) + "\n")
        proj = self._compute_projection()
        sys.stdout.write("\033[K" + render.render_progress_line(
            proj, self.total, self.start) + "\n")
        sys.stdout.write("\033[K" + render.render_size_line(
            proj, self.source_bytes, self.max_output_bytes) + "\n")
        sys.stdout.write("\033[K" + render.render_help(
            HAS_KEY_INPUT, self.finish_signal.requested) + "\n")
        sys.stdout.flush()
        self.printed_live = True

    def _render_plain(self, events_now: list[str]) -> None:
        """Headless render: no ANSI in-place redraw (it corrupts logs/journals).
        Emit new events as plain appended lines, plus a throttled one-line
        progress summary so a long encode still shows life in the log."""
        for evt in events_now:
            print(evt)
        now = time.monotonic()
        if now - self._last_plain_summary >= self._plain_summary_interval_s:
            self._last_plain_summary = now
            with self.lock:
                completed_now = self.completed
            proj = self._compute_projection()
            overall_done = self.already + completed_now
            print(f"  progress: {overall_done}/{self.total} chunks done, "
                  f"{min(100.0, proj['progress_frac'] * 100):.1f}% overall",
                  flush=True)

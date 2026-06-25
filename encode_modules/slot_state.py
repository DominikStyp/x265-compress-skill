"""Typed per-slot encode state for the parallel live display.

Before this module the live state of each encoding slot was a free-form
``dict`` whose key set was split across ``display.slot_start`` (initial keys)
and ``display.slot_progress`` (``live_fps`` / ``live_speed`` added later), then
indexed by raw string in three places (``display``, ``display_render``,
``choke_detection``). A typo in any consumer failed silently (``.get`` →
``None``) and the full shape was never visible in one place.

``SlotState`` makes the shape a single typed definition with attribute access,
so a bad field name is an ``AttributeError`` at the call site instead of a
silent ``None``. Every field carries a default so partial consumers (e.g. the
choke detector, which only needs the timing + samples fields) can construct a
minimal instance in tests.

Thread note: a live ``SlotState`` is mutated by worker threads under
``ParallelDisplay.lock``. The render and choke paths take an under-lock
``copy()`` snapshot and read it outside the lock — a SHALLOW copy, so the
``out_time_samples`` deque is shared by reference (consumers only iterate it),
exactly matching the prior ``dict(state)`` snapshot semantics.
"""
from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SlotState:
    # Identity / display.
    chunk: str = ""
    label_suffix: str = ""          # e.g. "AUTO-FIX"; rendered as " (…)"
    duration: float = 0.001         # source-chunk seconds (>0 for the % math)
    # Live ffmpeg progress.
    out_time_s: float = 0.0         # seconds of video encoded so far
    fps: str = "?"                  # ffmpeg's cumulative fps (fallback only)
    speed: str = "?"                # ffmpeg's cumulative speed (fallback only)
    frame: int = 0                  # last frame count (feeds the samples deque)
    # Locally-computed rolling rates (preferred over ffmpeg's cumulative
    # averages, which hibernation permanently corrupts). None until enough
    # samples accumulate.
    live_fps: Optional[float] = None
    live_speed: Optional[float] = None
    # Wall-clock timing. elapsed = (now - t_start) - paused_s; while suspended
    # paused_at holds the suspension instant so elapsed freezes.
    t_start: float = 0.0
    paused_s: float = 0.0
    paused_at: Optional[float] = None
    # Rolling (monotonic_t, out_time_s, frame) samples for delta-based choke
    # detection; bounded so memory stays constant per slot.
    out_time_samples: deque = field(default_factory=lambda: deque(maxlen=256))

    def copy(self) -> "SlotState":
        """Shallow snapshot for the under-lock render/choke read. The samples
        deque is shared by reference (consumers only read it) — same semantics
        as the prior ``dict(state)`` snapshot."""
        return copy.copy(self)

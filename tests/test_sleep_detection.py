"""Regression: the choke detector's system-sleep guard must fire on macOS
and Linux, not just Windows.

The guard exists so that a laptop sleeping mid-encode doesn't get every
in-flight chunk flagged as "choked" on wake (the suspended ffmpeg produced no
progress during the nap). The original guard inferred sleep purely from a jump
in `time.monotonic()`. That works on Windows (its monotonic clock keeps
counting suspended wall time) but NOT on macOS/Linux, where `time.monotonic()`
FREEZES across system sleep — so the jump never appears, the guard never trips,
and the detector proceeds to a false choke + needless chunk restart. Observed
on a real macOS run (chunks restarted right after each `pmset` wake).

The portable signal is clock divergence: wall-clock (`time.time()`) ALWAYS
advances across suspend, so when the wall gap between two consecutive checks
greatly exceeds the monotonic gap (or vice-versa on Windows), the process was
suspended. The guard now trips on max(monotonic_gap, wall_gap).
"""
from __future__ import annotations

import collections
import queue
import threading
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.choke_detection import check_choke  # noqa: E402
from encode_modules.slot_state import SlotState  # noqa: E402


class _StubDisplay:
    """Minimal stand-in exposing only what check_choke touches."""

    def __init__(self) -> None:
        self.abort_event = threading.Event()
        self.has_choked_chunks = threading.Event()
        self.lock = threading.Lock()
        self.events: "queue.Queue[str]" = queue.Queue()
        self.choke_threshold_speed = 1.0
        self.choke_grace_seconds = 30.0
        self.choke_window_seconds = 60.0
        self.choke_min_delta_seconds = 1.0
        self.sleep_detect_seconds = 120.0
        self.slots: dict = {}
        self.active_procs: dict = {}
        self.choked_chunks: dict = {}

    def add_choked_slot(self) -> None:
        """Add one slot that WOULD be declared choked: long past the grace
        window with no progress samples."""
        now = time.monotonic()
        self.slots[0] = SlotState(
            chunk="src_0004.mkv",
            t_start=now - 600.0,        # 10 min in — well past grace
            out_time_samples=collections.deque(),  # no progress -> choke
        )

    def drain_events(self) -> list[str]:
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out


class SleepDetectionTest(unittest.TestCase):
    def test_macos_sleep_small_monotonic_gap_large_wall_gap(self) -> None:
        # The macOS/Linux case: monotonic barely moved (frozen across sleep)
        # but the wall clock jumped minutes. The guard MUST trip.
        d = _StubDisplay()
        d.add_choked_slot()
        d._last_choke_check_at = time.monotonic() - 0.5   # tiny monotonic gap
        d._last_choke_check_wall = time.time() - 300.0     # 5 min wall gap

        result = check_choke(d)

        self.assertIsNone(result, "sleep guard should skip the choke verdict")
        events = d.drain_events()
        self.assertTrue(any("Sleep" in e for e in events),
                        f"expected a sleep-detected event, got {events}")

    def test_windows_sleep_large_monotonic_gap_still_trips(self) -> None:
        # The Windows case (monotonic counts suspended time) must keep working.
        d = _StubDisplay()
        d.add_choked_slot()
        d._last_choke_check_at = time.monotonic() - 300.0  # 5 min monotonic gap
        d._last_choke_check_wall = time.time() - 300.0

        result = check_choke(d)

        self.assertIsNone(result)
        self.assertTrue(any("Sleep" in e for e in d.drain_events()))

    def test_normal_cadence_does_not_trip_sleep_guard(self) -> None:
        # Both clocks advanced ~0.5s (normal render cadence): NO false sleep
        # detection. (The slot is choked, so check_choke returns the choke —
        # what matters is that it's a real verdict, not the sleep skip.)
        d = _StubDisplay()
        d.add_choked_slot()
        d._last_choke_check_at = time.monotonic() - 0.5
        d._last_choke_check_wall = time.time() - 0.5

        result = check_choke(d)

        self.assertEqual(result, (0, "src_0004.mkv"))
        self.assertFalse(any("Sleep" in e for e in d.drain_events()),
                         "must not report sleep on normal cadence")

    def test_first_call_does_not_trip_sleep_guard(self) -> None:
        # No prior check recorded (both last-values unset): the gap is 0, so the
        # guard must not fire on the very first cycle.
        d = _StubDisplay()
        d.add_choked_slot()

        result = check_choke(d)

        self.assertEqual(result, (0, "src_0004.mkv"))
        self.assertFalse(any("Sleep" in e for e in d.drain_events()))

    def test_backward_wall_jump_does_not_trip(self) -> None:
        # A backward wall-clock step (NTP/manual) makes wall_gap negative;
        # max(monotonic_gap, wall_gap) falls back to the small monotonic gap, so
        # no spurious sleep detection. Pins the max() semantics.
        d = _StubDisplay()
        d.add_choked_slot()
        d._last_choke_check_at = time.monotonic() - 0.5
        d._last_choke_check_wall = time.time() + 300.0   # clock jumped backward

        result = check_choke(d)

        self.assertEqual(result, (0, "src_0004.mkv"))
        self.assertFalse(any("Sleep" in e for e in d.drain_events()))


if __name__ == "__main__":
    unittest.main()

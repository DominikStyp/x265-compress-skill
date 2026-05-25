"""Tier 0.3 regression: the render thread also runs the size-guard
(check_threshold) and the choke killer (check_choke). If a redraw raises,
an unguarded loop body kills the daemon thread and silently disables BOTH
safety mechanisms, not just the live display.

`_render_tick` must run the three steps, swallow any exception, and surface
it via the events log so the thread (and the guards) survive.
"""
from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.encode_parallel import _render_tick  # noqa: E402


class _FakeDisplay:
    """Controllable stand-in. `render` is injected as the seam under test."""

    def __init__(self, render_fn) -> None:
        self.events: "queue.Queue[str]" = queue.Queue()
        self.checked_threshold = False
        self.checked_choke = False
        self.synced_file_pause = False
        self._render_fn = render_fn

    def sync_file_pause(self) -> None:
        self.synced_file_pause = True

    def check_threshold(self) -> None:
        self.checked_threshold = True

    def check_choke(self):
        self.checked_choke = True

    def render(self) -> None:
        self._render_fn()

    def drain_events(self) -> list:
        out = []
        try:
            while True:
                out.append(self.events.get_nowait())
        except queue.Empty:
            return out


class RenderTickGuardTest(unittest.TestCase):
    def test_tick_swallows_render_exception_and_records_it(self) -> None:
        def boom():
            raise RuntimeError("boom: malformed slot dict")

        display = _FakeDisplay(boom)
        # Must NOT propagate — a raised exception here would kill the render
        # thread and with it the threshold + choke guards.
        _render_tick(display)
        events = display.drain_events()
        self.assertTrue(
            any("boom" in m for m in events),
            f"the failure should be surfaced via events, got {events}",
        )

    def test_tick_runs_guards_on_happy_path(self) -> None:
        display = _FakeDisplay(lambda: None)
        _render_tick(display)
        self.assertTrue(display.synced_file_pause)
        self.assertTrue(display.checked_threshold)
        self.assertTrue(display.checked_choke)
        self.assertEqual(display.drain_events(), [])


if __name__ == "__main__":
    unittest.main()

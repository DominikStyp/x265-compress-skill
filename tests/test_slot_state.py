"""Finding #3 (v1.20.1): the per-slot live state is a typed SlotState dataclass
instead of a stringly-keyed dict. These pin (a) the dataclass semantics that
the threaded display/choke code relies on — independent per-instance deques and
a shallow copy() that shares the samples deque — and (b) that display.slot_start
/ slot_progress build and mutate it correctly and the render layer reads it
(the slot render functions were previously untested by name).
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import display_render as render  # noqa: E402
from encode_modules.slot_state import SlotState  # noqa: E402


class SlotStateDataclassTest(unittest.TestCase):
    def test_each_instance_gets_its_own_samples_deque(self) -> None:
        # A mutable default (the deque) must NOT be shared across instances —
        # otherwise every slot's choke samples would alias. default_factory.
        a = SlotState(chunk="a")
        b = SlotState(chunk="b")
        a.out_time_samples.append((1.0, 2.0, 3))
        self.assertEqual(len(a.out_time_samples), 1)
        self.assertEqual(len(b.out_time_samples), 0)

    def test_copy_is_shallow_scalars_independent_deque_shared(self) -> None:
        s = SlotState(chunk="x", out_time_s=5.0)
        s.out_time_samples.append((1.0, 2.0, 3))
        snap = s.copy()
        # Scalars are independent: mutating the live slot doesn't change the snap.
        s.out_time_s = 99.0
        self.assertEqual(snap.out_time_s, 5.0)
        # The samples deque is shared by reference (consumers only read it),
        # matching the prior dict(state) snapshot semantics.
        self.assertIs(snap.out_time_samples, s.out_time_samples)

    def test_defaults_for_partial_construction(self) -> None:
        # The choke detector constructs minimal instances (no duration/fps).
        s = SlotState(chunk="c", t_start=123.0)
        self.assertEqual(s.duration, 0.001)
        self.assertEqual(s.fps, "?")
        self.assertIsNone(s.live_fps)
        self.assertIsNone(s.paused_at)


class SlotElapsedTest(unittest.TestCase):
    def test_unstarted_slot_returns_none(self) -> None:
        # Default t_start (0.0) means "not started" -> None, not a huge number.
        self.assertIsNone(render.slot_elapsed_seconds(SlotState(chunk="x")))

    def test_running_slot_elapsed(self) -> None:
        s = SlotState(chunk="x", t_start=time.monotonic() - 10.0)
        elapsed = render.slot_elapsed_seconds(s)
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 9.0)

    def test_paused_slot_freezes_elapsed(self) -> None:
        now = time.monotonic()
        s = SlotState(chunk="x", t_start=now - 100.0, paused_at=now - 40.0)
        # Frozen at paused_at - t_start - paused_s = 60s, regardless of "now".
        self.assertAlmostEqual(render.slot_elapsed_seconds(s), 60.0, delta=0.5)


class RenderSlotTest(unittest.TestCase):
    def test_idle_slot_renders_idle(self) -> None:
        out = render.render_slot_main(0, None, paused=False, focused=False)
        self.assertIn("idle", out)

    def test_active_slot_shows_name_and_live_rate(self) -> None:
        s = SlotState(chunk="src_0003.mkv", duration=100.0, out_time_s=50.0,
                      fps="24", speed="1.2x", live_fps=30.0, live_speed=1.5)
        out = render.render_slot_main(0, s, paused=False, focused=False)
        self.assertIn("src_0003.mkv", out)
        self.assertIn("50.0%", out)
        # Live rate is preferred over ffmpeg's cumulative fps/speed.
        self.assertIn("30.0", out)

    def test_label_suffix_rendered(self) -> None:
        s = SlotState(chunk="src_0003.mkv", duration=100.0, label_suffix="AUTO-FIX")
        out = render.render_slot_main(0, s, paused=False, focused=False)
        self.assertIn("AUTO-FIX", out)


class DisplayBuildsSlotStateTest(unittest.TestCase):
    """slot_start/slot_progress operate on a real ParallelDisplay and produce a
    correctly-typed, correctly-mutated SlotState."""

    def _display(self):
        from encode_modules.display import ParallelDisplay
        return ParallelDisplay(parallel=1, total=1, already_done=0)

    def test_slot_start_creates_slotstate(self) -> None:
        d = self._display()
        d.slot_start(0, "src_0001.mkv", 42.0)
        s = d.slots[0]
        self.assertIsInstance(s, SlotState)
        self.assertEqual(s.chunk, "src_0001.mkv")
        self.assertEqual(s.duration, 42.0)
        self.assertGreater(s.t_start, 0.0)

    def test_slot_progress_mutates_and_samples(self) -> None:
        d = self._display()
        d.slot_start(0, "src_0001.mkv", 100.0)
        d.slot_progress(0, out_time_s=12.5, frame=300, fps="25", speed="1.1x")
        s = d.slots[0]
        self.assertEqual(s.out_time_s, 12.5)
        self.assertEqual(s.fps, "25")
        self.assertEqual(s.frame, 300)
        self.assertEqual(len(s.out_time_samples), 1)

    def test_slot_progress_ignores_unknown_slot(self) -> None:
        d = self._display()
        d.slot_progress(99, out_time_s=1.0)  # no such slot -> no-op, no raise
        self.assertNotIn(99, d.slots)


class SlotStateConsumersTest(unittest.TestCase):
    """Regression for the v1.20.1 review: the SlotState conversion must reach
    EVERY display.slots consumer. size_projection and pause_control read the
    slots and were missed in the first pass; their tests never called
    slot_start (so display.slots was empty and the dict-access lines never
    ran). These drive the real consumers WITH a populated SlotState slot, so a
    dict-access regression there fails loudly instead of silently."""

    def _display(self):
        from encode_modules.display import ParallelDisplay
        d = ParallelDisplay(parallel=1, total=1, already_done=0)
        d.total_duration = 100.0
        return d

    def test_compute_projection_sums_active_slot_out_time(self) -> None:
        # size_projection.compute_projection reads s.out_time_s for every
        # active slot — the line that crashed with `.get()` on a SlotState.
        d = self._display()
        d.slot_start(0, "src_0001.mkv", 100.0)
        d.slot_progress(0, out_time_s=40.0, frame=1000, fps="25", speed="1x")
        proj = d._compute_projection()       # must not raise AttributeError
        # 40s of 100s total encoded via the active slot -> ~0.4 progress.
        self.assertAlmostEqual(proj["progress_frac"], 0.4, delta=0.01)

    def test_pause_helpers_operate_on_slotstate(self) -> None:
        # pause_control.mark_pause_start / settle_pause_elapsed mutate the slot
        # — the lines that crashed with item assignment / `.get()` on SlotState.
        from encode_modules import pause_control
        d = self._display()
        d.slot_start(0, "src_0001.mkv", 100.0)
        with d.lock:
            pause_control.mark_pause_start(d, 0)
            self.assertIsNotNone(d.slots[0].paused_at)
            pause_control.settle_pause_elapsed(d, 0)
        # After settling, the pause window folded into paused_s and cleared.
        self.assertIsNone(d.slots[0].paused_at)
        self.assertGreaterEqual(d.slots[0].paused_s, 0.0)


if __name__ == "__main__":
    unittest.main()

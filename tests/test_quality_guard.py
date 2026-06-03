"""Per-chunk VMAF quality guard: aborts the whole file when any encoded chunk
falls below a configured ``visual_quality_threshold`` VMAF score.

The guard runs in its own background thread so quality measurement does NOT
block the next chunk encode — the encoder keeps producing while VMAF drains
chunks off a queue. Chunk index 0 (the first chunk in temporal order) is
measured but NOT compared against the threshold — single-chunk VMAF is noisier
than aggregate VMAF, so the warmup grace avoids false-positive aborts on the
first encode-and-measure cycle. From chunk 1 onward a `vmaf_mean <
threshold` posts a `QualityAbortEvent`-shaped signal that the encoder reads to
kill in-flight workers and exit code 9 (stopped-quality-threshold).

Tests use a fake `vmaf_pair_fn` so they don't shell out to libvmaf — each test
specifies the score per chunk submitted, and exercises only the guard's queue,
threshold logic, warmup grace, and shutdown semantics."""
from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.quality_guard import (  # noqa: E402
    QualityAbortInfo,
    QualityGuard,
)


def _drain(events: queue.Queue, timeout: float = 1.0) -> list:
    """Pop everything from `events` within `timeout`. Returns a list."""
    out = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            out.append(events.get(timeout=0.05))
        except queue.Empty:
            if not out:
                continue
            break
    return out


class _FakeVmafSequence:
    """Returns a pre-programmed VMAF score per (src, dst) pair, in submission
    order. Lets each test pin exact scores for chunk 0, chunk 1, etc. None
    return value simulates libvmaf failure."""

    def __init__(self, scores: list[float | None]) -> None:
        self.scores = list(scores)
        self.calls: list[tuple[Path, Path]] = []
        self._lock = threading.Lock()

    def __call__(self, src: Path, dst: Path) -> dict | None:
        with self._lock:
            self.calls.append((src, dst))
            if not self.scores:
                return None
            score = self.scores.pop(0)
        if score is None:
            return None
        return {"vmaf_mean": score, "vmaf_min": score - 1, "vmaf_harmonic_mean": score}


class _GuardLifecycleTestMixin:
    """Shared setUp: create a guard with a fake VMAF function. Tests provide
    `scores` as the per-call sequence."""

    threshold: float = 90.0
    skip_first_chunk: bool = True

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self.events: queue.Queue = queue.Queue()
        self.aborts: queue.Queue = queue.Queue()
        self.abort_holder: list[QualityAbortInfo] = []

    def _make_guard(self, scores: list[float | None]) -> QualityGuard:
        self.fake = _FakeVmafSequence(scores)
        return QualityGuard(
            threshold=self.threshold,
            skip_first_chunk=self.skip_first_chunk,
            vmaf_pair_fn=self.fake,
            events_queue=self.events,
            on_abort=self.abort_holder.append,
        )

    def _submit_and_wait(self, guard: QualityGuard,
                        items: list[tuple[int, Path, Path]],
                        wait_s: float = 2.0) -> None:
        for chunk_idx, src, dst in items:
            guard.submit(chunk_idx=chunk_idx, src=src, dst=dst)
        guard.stop(timeout=wait_s)


class DefaultBehaviourTest(_GuardLifecycleTestMixin, unittest.TestCase):
    """All-clean path: every chunk scores above threshold → no abort, all
    submissions processed, guard shuts down cleanly."""

    def test_all_above_threshold_no_abort(self) -> None:
        guard = self._make_guard([95.0, 92.5, 91.0])
        self._submit_and_wait(guard, [
            (0, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
            (1, Path("src_0002.mkv"), Path("enc_src_0002.mkv")),
            (2, Path("src_0003.mkv"), Path("enc_src_0003.mkv")),
        ])
        self.assertEqual(self.abort_holder, [])
        self.assertEqual(len(self.fake.calls), 3)

    def test_chunk_zero_below_threshold_does_not_abort_warmup(self) -> None:
        # The single chunk submitted is at index 0 with score 50 (way below 90),
        # but warmup grace must let it pass to avoid first-chunk noise aborts.
        guard = self._make_guard([50.0])
        self._submit_and_wait(guard, [
            (0, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
        ])
        self.assertEqual(self.abort_holder, [],
                        "chunk_idx=0 must skip threshold check (warmup grace)")

    def test_chunk_one_below_threshold_aborts(self) -> None:
        guard = self._make_guard([95.0, 87.3])  # chunk 0 ok, chunk 1 fails
        self._submit_and_wait(guard, [
            (0, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
            (1, Path("src_0002.mkv"), Path("enc_src_0002.mkv")),
        ])
        self.assertEqual(len(self.abort_holder), 1)
        info = self.abort_holder[0]
        self.assertEqual(info.chunk_idx, 1)
        self.assertEqual(info.chunk_name, "src_0002.mkv")
        self.assertAlmostEqual(info.vmaf_mean, 87.3, places=2)
        self.assertEqual(info.threshold, 90.0)

    def test_chunk_at_exactly_threshold_does_not_abort(self) -> None:
        # Strict < comparison: equal to threshold passes.
        guard = self._make_guard([90.0])
        self._submit_and_wait(guard, [
            (1, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
        ])
        self.assertEqual(self.abort_holder, [])

    def test_libvmaf_failure_does_not_abort(self) -> None:
        # Best-effort tolerance: if libvmaf returns None (process crashed,
        # log unparseable), do NOT abort. Log a warning to events queue.
        # Single failure is below the consecutive-fails threshold (3).
        guard = self._make_guard([None])
        self._submit_and_wait(guard, [
            (1, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
        ])
        self.assertEqual(self.abort_holder, [])
        events = _drain(self.events, timeout=0.5)
        self.assertTrue(
            any("quality" in e.lower() or "vmaf" in e.lower() for e in events),
            f"expected a warning event mentioning quality/vmaf, got: {events}",
        )

    def test_consecutive_libvmaf_failures_trigger_loud_abort(self) -> None:
        # If libvmaf returns None for 3 consecutive chunks, infrastructure is
        # broken (missing model file, wrong ffmpeg build, etc.). Without this
        # escalation the guard would silently disable and the user would
        # believe the threshold was protecting them. Fire a loud abort with
        # NaN vmaf_mean to signal the infra failure (vs. a real low score).
        guard = self._make_guard([None, None, None])
        self._submit_and_wait(guard, [
            (1, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
            (2, Path("src_0002.mkv"), Path("enc_src_0002.mkv")),
            (3, Path("src_0003.mkv"), Path("enc_src_0003.mkv")),
        ])
        self.assertEqual(len(self.abort_holder), 1,
                        "3rd consecutive infra failure must fire abort")
        info = self.abort_holder[0]
        # NaN is the sentinel for "no score available — infra broken"
        import math
        self.assertTrue(math.isnan(info.vmaf_mean),
                       f"expected NaN vmaf_mean for infra-abort, got "
                       f"{info.vmaf_mean}")

    def test_intermittent_libvmaf_failure_does_not_abort(self) -> None:
        # 2 fails, then a success, then 2 more fails: counter resets on
        # success so we should NEVER hit the 3-consecutive trigger.
        guard = self._make_guard([None, None, 95.0, None, None])
        self._submit_and_wait(guard, [
            (1, Path("src_0001.mkv"), Path("enc_src_0001.mkv")),
            (2, Path("src_0002.mkv"), Path("enc_src_0002.mkv")),
            (3, Path("src_0003.mkv"), Path("enc_src_0003.mkv")),
            (4, Path("src_0004.mkv"), Path("enc_src_0004.mkv")),
            (5, Path("src_0005.mkv"), Path("enc_src_0005.mkv")),
        ])
        self.assertEqual(self.abort_holder, [],
                        "intermittent infra failures must NOT trigger abort")


class FirstFailureStopsFurtherSubmissionsTest(_GuardLifecycleTestMixin,
                                              unittest.TestCase):
    """Once abort fires, the guard should not invoke vmaf_pair_fn on later
    submissions — they're moot, the encoder will be killed."""

    def test_post_abort_submissions_skipped(self) -> None:
        # Chunk 1 fails (87 < 90). Chunks 2 and 3 are submitted afterwards but
        # the guard should NOT call vmaf_pair on them.
        #
        # Synchronize on an Event set by the abort callback rather than a
        # sleep — under coverage instrumentation / a loaded CI box a fixed
        # delay is flaky. Event.wait gives the worker as much time as it
        # needs and fails fast in the deadline case.
        abort_seen = threading.Event()
        self.abort_holder = []  # local override of mixin's list-based holder

        def _on_abort(info: QualityAbortInfo) -> None:
            self.abort_holder.append(info)
            abort_seen.set()

        self.fake = _FakeVmafSequence([95.0, 87.0, 99.0, 99.0])
        guard = QualityGuard(
            threshold=self.threshold,
            skip_first_chunk=self.skip_first_chunk,
            vmaf_pair_fn=self.fake,
            events_queue=self.events,
            on_abort=_on_abort,
        )

        guard.submit(chunk_idx=0, src=Path("src_0001.mkv"),
                    dst=Path("enc_src_0001.mkv"))
        guard.submit(chunk_idx=1, src=Path("src_0002.mkv"),
                    dst=Path("enc_src_0002.mkv"))
        # Wait deterministically for abort to fire before the late submits.
        self.assertTrue(abort_seen.wait(timeout=2.0),
                       "abort callback never fired within deadline")
        guard.submit(chunk_idx=2, src=Path("src_0003.mkv"),
                    dst=Path("enc_src_0003.mkv"))
        guard.submit(chunk_idx=3, src=Path("src_0004.mkv"),
                    dst=Path("enc_src_0004.mkv"))
        guard.stop(timeout=2.0)

        self.assertEqual(len(self.abort_holder), 1)
        # 2 calls: chunk 0 (passes warmup) + chunk 1 (fails). Chunks 2+ skipped.
        self.assertLessEqual(len(self.fake.calls), 2,
                            f"post-abort calls should be skipped, got "
                            f"{[c[0].name for c in self.fake.calls]}")


class ShutdownTest(_GuardLifecycleTestMixin, unittest.TestCase):
    """Lifecycle: guard.stop() must drain pending work and join the thread."""

    def test_stop_with_no_submissions_returns_quickly(self) -> None:
        guard = self._make_guard([])
        start = time.monotonic()
        guard.stop(timeout=1.0)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_stop_drains_all_pending(self) -> None:
        guard = self._make_guard([95.0, 95.0, 95.0])
        for i in range(3):
            guard.submit(chunk_idx=i + 1,
                        src=Path(f"src_000{i + 1}.mkv"),
                        dst=Path(f"enc_src_000{i + 1}.mkv"))
        guard.stop(timeout=2.0)
        self.assertEqual(len(self.fake.calls), 3)


class DisabledGuardTest(unittest.TestCase):
    """Threshold=None means feature is off — guard returned by the factory must
    be a no-op pass-through. (The encoder still creates it unconditionally for
    a uniform call-site interface.)"""

    def test_threshold_none_is_noop(self) -> None:
        events: queue.Queue = queue.Queue()
        aborts: list = []
        fake = _FakeVmafSequence([50.0, 50.0])  # would abort if active
        guard = QualityGuard(
            threshold=None,
            skip_first_chunk=True,
            vmaf_pair_fn=fake,
            events_queue=events,
            on_abort=aborts.append,
        )
        guard.submit(chunk_idx=0, src=Path("src_0001.mkv"),
                    dst=Path("enc_src_0001.mkv"))
        guard.submit(chunk_idx=1, src=Path("src_0002.mkv"),
                    dst=Path("enc_src_0002.mkv"))
        guard.stop(timeout=1.0)
        # vmaf_pair_fn never invoked, no abort recorded.
        self.assertEqual(fake.calls, [])
        self.assertEqual(aborts, [])


if __name__ == "__main__":
    unittest.main()

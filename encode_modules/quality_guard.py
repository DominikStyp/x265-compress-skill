"""Per-chunk VMAF quality guard for the parallel encode pipeline.

Why this exists: ``--max-size-percent`` already aborts a file early when the
projected output exceeds a SIZE budget. There was no symmetric guard for
QUALITY — a file could encode all the way through, pass verification, and
only at the end reveal a VMAF in the 70s on a source with heavy grain /
banding / hentai-style flat areas that confound x265's RDOQ. By that point
hours of CPU are gone.

``visual_quality_threshold: 90`` (queue setting / ``--visual-quality-threshold``
CLI arg) wires up a background worker that VMAF-checks each finalized chunk
against its source as soon as the encoder renames ``enc_*.part.mkv`` to
``enc_*.mkv``. If any chunk scores below the threshold the worker fires an
``on_abort`` callback, which the encoder wires to ``display.abort_event`` +
a ``quality_abort_info`` field; ``encode_chunks_parallel`` then exits code 9
(``stopped-quality-threshold``) and the queue runner skips the file.

Design choices:

  * **Warmup grace on chunk 0.** Single-chunk VMAF is noisier than aggregate
    VMAF (per-frame RDOQ noise averages out over more frames). The first chunk
    in temporal order is still MEASURED — its score appears in any later log
    record — but it does not trigger an abort. From chunk 1 onward all chunks
    are judged. (Configurable via ``skip_first_chunk=False``.)

  * **Best-effort tolerance.** If ``vmaf_pair_fn`` returns ``None`` (libvmaf
    crashed, JSON unparseable, timeout) the guard logs a warning to the events
    queue and CONTINUES. An infrastructure problem must not falsely doom a
    file that may have perfectly fine encoded chunks.

  * **One worker thread.** Single drain of the submission queue keeps the
    implementation small and protects single-file abort latency. If quality
    measurement is slower than encoding produces chunks, abort detection
    lags by a few chunks — acceptable; the encoder still spends less wall
    time on a quality-failing file than a no-guard run would.

  * **CPU priority.** The guard's ``vmaf_pair_fn`` is expected to spawn
    ffmpeg WITHOUT the low-priority wrap (passes ``low_priority=False`` to
    ``encode_modules.quality._quality_check_run``) so the quality check
    preempts the encoder when both compete for cores. On a system where the
    encoder runs at idle priority (default) and quality at normal, quality
    finishes faster than libvmaf would otherwise contend for time slices.

  * **Post-abort short-circuit.** Once an abort has fired, further
    ``submit()`` calls are accepted but the worker drops them without calling
    ``vmaf_pair_fn`` — the encoder is being torn down and additional
    measurements would be wasted CPU.

  * **Disabled mode.** ``threshold=None`` makes ``submit()`` a no-op so the
    encoder can build the guard unconditionally without an ``if`` at every
    call site. ``stop()`` then also returns immediately.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# Public sentinel: an abort decision packaged with everything the encoder
# needs to print + log + notify. Lives at module scope (not nested in the
# class) so encode_parallel.py can import it for type hints.
@dataclass(frozen=True)
class QualityAbortInfo:
    """Captures one quality-threshold failure: which chunk, what it scored,
    and what threshold it failed against. Frozen so it can flow safely
    across threads without mutation hazards."""
    chunk_idx: int
    chunk_name: str
    vmaf_mean: float
    threshold: float


# Type alias for the injected libvmaf runner. Returns a dict with at least
# ``vmaf_mean`` on success, ``None`` on any failure (subprocess error, log
# parse failure, timeout). Tests pass a fake; production passes
# ``encode_modules.quality.vmaf_pair`` (a thin wrapper around
# ``_quality_check_run`` with low_priority=False).
VmafPairFn = Callable[[Path, Path], Optional[dict]]


# How many consecutive `vmaf_pair_fn` -> None returns before the guard
# concludes libvmaf itself is broken and emits a loud abort. Below this
# count we log each failure and continue (best-effort tolerance); at or
# above we fire an `infra-broken` flavour of QualityAbortInfo so the
# encoder doesn't silently complete with the guard effectively disabled.
_CONSECUTIVE_INFRA_FAILS_BEFORE_ABORT = 3


# Type alias for the abort sink — encoder wires this to mark display +
# kick the existing abort_event so worker threads exit.
OnAbortFn = Callable[[QualityAbortInfo], None]


# Worker queue item: chunk_idx + src path + dst path. Sentinel `None` tells
# the worker to exit (drained or stop()ped).
_WorkItem = Optional[tuple[int, Path, Path]]


class QualityGuard:
    """Per-chunk VMAF guard. Constructed once per file; ``submit()``-ed once
    per finalized encoded chunk; ``stop()``-ed when the encode loop exits.

    Disabled-mode shortcut: ``threshold=None`` makes ``submit`` and ``stop``
    cheap no-ops so the encoder can build a guard unconditionally."""

    def __init__(self, *,
                 threshold: Optional[float],
                 skip_first_chunk: bool,
                 vmaf_pair_fn: VmafPairFn,
                 events_queue: "queue.Queue[str]",
                 on_abort: OnAbortFn) -> None:
        self._threshold = threshold
        self._skip_first_chunk = skip_first_chunk
        self._vmaf_pair_fn = vmaf_pair_fn
        self._events = events_queue
        self._on_abort = on_abort
        self._aborted = threading.Event()
        self._stopped = threading.Event()
        # Consecutive None returns from vmaf_pair_fn. After
        # _CONSECUTIVE_INFRA_FAILS_BEFORE_ABORT we treat libvmaf as broken
        # and fire a loud abort so the encoder doesn't silently disable.
        self._consecutive_infra_fails = 0

        if threshold is None:
            # Disabled mode: no worker, no queue. submit/stop become no-ops.
            self._work_q: "queue.Queue[_WorkItem]" = queue.Queue()
            self._worker: Optional[threading.Thread] = None
            return

        self._work_q = queue.Queue()
        self._worker = threading.Thread(
            target=self._run, name="quality-guard", daemon=True,
        )
        self._worker.start()

    def submit(self, *, chunk_idx: int, src: Path, dst: Path) -> None:
        """Queue a chunk for VMAF check. No-op when threshold is None or
        after an abort has already fired (further checks are moot — encoder
        is being torn down)."""
        if self._threshold is None or self._aborted.is_set():
            return
        self._work_q.put((chunk_idx, src, dst))

    def stop(self, timeout: float = 60.0) -> None:
        """Signal the worker to drain pending items and exit. Joins the
        worker thread with the given timeout. Idempotent — second call is
        a quick no-op."""
        if self._worker is None or self._stopped.is_set():
            self._stopped.set()
            return
        self._stopped.set()
        # Sentinel ensures the worker wakes from a blocking get() and exits
        # cleanly even if the queue is empty.
        self._work_q.put(None)
        self._worker.join(timeout=timeout)

    def has_aborted(self) -> bool:
        """True iff a chunk has failed the threshold and ``on_abort`` has
        already been invoked. Useful for the encoder to check after a slot
        finishes whether the abort came from quality (not size guard)."""
        return self._aborted.is_set()

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Pump the submission queue. Each iteration:

        1. Pop the next ``(chunk_idx, src, dst)`` tuple (or sentinel ``None``).
        2. If sentinel, exit.
        3. If we've already fired an abort, drop the item without calling
           ``vmaf_pair_fn`` — encoder is being torn down.
        4. Run ``vmaf_pair_fn``. On ``None`` (libvmaf error): log a warning
           and continue (best-effort tolerance).
        5. If ``skip_first_chunk`` and ``chunk_idx == 0``: don't compare,
           just log and continue (warmup grace).
        6. If ``vmaf_mean < threshold``: build ``QualityAbortInfo``, set
           the abort flag, call ``on_abort``, exit the worker loop. The
           ``_aborted`` flag also makes any later ``submit()`` calls a no-op.

        Any unexpected exception is caught and logged so a guard bug never
        kills the encoder silently."""
        while True:
            item = self._work_q.get()
            if item is None:
                return
            try:
                self._process_one(item)
            except Exception as ex:  # noqa: BLE001 — guard seam, must log not raise
                self._events.put(
                    f"  ! quality guard: unhandled "
                    f"{type(ex).__name__}: {ex}",
                )

    def _process_one(self, item: tuple[int, Path, Path]) -> None:
        """Handle one (chunk_idx, src, dst) submission. See ``_run`` docstring
        for the full state machine."""
        chunk_idx, src, dst = item
        if self._aborted.is_set():
            return  # short-circuit after abort

        # Threshold is non-None here — checked in submit() and the worker is
        # only started when threshold is set.
        assert self._threshold is not None

        scores = self._vmaf_pair_fn(src, dst)
        if scores is None or scores.get("vmaf_mean") is None:
            self._consecutive_infra_fails += 1
            reason = ("libvmaf returned no scores" if scores is None
                      else "vmaf scores lacked 'vmaf_mean' key")
            if (self._consecutive_infra_fails
                    >= _CONSECUTIVE_INFRA_FAILS_BEFORE_ABORT):
                self._events.put(
                    f"  ! quality guard: {self._consecutive_infra_fails} "
                    f"consecutive infrastructure failures ({reason}) — "
                    f"libvmaf is broken; aborting file loudly instead of "
                    f"silently disabling the guard")
                info = QualityAbortInfo(
                    chunk_idx=chunk_idx,
                    chunk_name=src.name,
                    vmaf_mean=float("nan"),
                    threshold=float(self._threshold),
                )
                self._aborted.set()
                self._on_abort(info)
                return
            self._events.put(
                f"  ! quality guard: vmaf measurement failed for "
                f"{dst.name} ({reason}) — continuing "
                f"[{self._consecutive_infra_fails}/"
                f"{_CONSECUTIVE_INFRA_FAILS_BEFORE_ABORT} before loud abort]")
            return

        # Successful measurement — reset the infra-failure counter.
        self._consecutive_infra_fails = 0
        vmaf_mean = scores["vmaf_mean"]

        if self._skip_first_chunk and chunk_idx == 0:
            self._events.put(
                f"  . quality guard: chunk {chunk_idx + 1} ({src.name}) "
                f"VMAF={vmaf_mean:.2f} (warmup grace — not compared to "
                f"threshold {self._threshold:g})")
            return

        if vmaf_mean < self._threshold:
            info = QualityAbortInfo(
                chunk_idx=chunk_idx,
                chunk_name=src.name,
                vmaf_mean=float(vmaf_mean),
                threshold=float(self._threshold),
            )
            # Set abort flag BEFORE calling on_abort so any concurrent
            # submit() short-circuits immediately.
            self._aborted.set()
            self._on_abort(info)
            return

        self._events.put(
            f"  . quality guard: chunk {chunk_idx + 1} ({src.name}) "
            f"VMAF={vmaf_mean:.2f} >= {self._threshold:g} ok")

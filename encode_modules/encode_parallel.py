"""Parallel chunk encoding: N concurrent ffmpegs, a render thread, a key
listener, with threshold + choke + auto-fix all woven in.

This is the most operationally fragile part of the pipeline — multiple
threads share state, a kill of Python must take ffmpeg children with it,
and the user can pause/resume slots interactively. Each concern is a
separate function (worker, render loop, dispatch) so the orchestration
body in `encode_chunks_parallel` reads as a recipe.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .chunk_hook import ChunkHook, fire_for_chunk
from .chunk_metrics_log import update_chunk_quality
from .chunk_recovery import try_auto_fix_chunk
from .chunk_worker import _encode_one_chunk_with_display
from .chunking import reorder_middle_first, x265_params_with_pools
from .display import ParallelDisplay
from .history_state import mark_status, set_stop_context
from .keyboard_input import keyboard_listener
from .messages import (
    print_choke_guard_announcement,
    print_encode_plan,
    print_finish_stopped_block,
    print_quality_threshold_abort_block,
    print_runtime_protections,
    print_threshold_abort_block,
)
from .quality_guard import QualityAbortInfo, QualityGuard
from .quality_libvmaf import vmaf_pair
from .skipped_collector import collect_skipped


@dataclass
class _WorkerContext:
    """All the state a worker thread needs to encode a chunk + handle the
    auto-fix retry. Bundled so the worker fn signature stays sane."""
    display: ParallelDisplay
    work_q: "queue.Queue[Path]"
    results: list
    results_lock: threading.Lock
    workdir: Path
    crf: int
    preset: str
    pix_fmt: str
    x265_params: str
    x265_params_for_autofix: str  # original (no pools= override) for relaxing
    auto_fix_choke: bool
    chunk_hook: ChunkHook | None = None
    position_of: dict[Path, int] = field(default_factory=dict)
    # Per-chunk VMAF guard (v1.17.0). Built unconditionally; threshold=None
    # makes submit() a cheap no-op so the wiring stays uniform.
    quality_guard: QualityGuard | None = None


def _worker(slot: int, ctx: _WorkerContext) -> None:
    """Pull chunks off the queue until empty or abort. Each chunk attempt is
    wrapped in its own try/except so a bug in the encode helper OR auto-fix
    can't kill the worker (which historically left the queue dead with no
    needs_fix sidecar — the Linda 0003 incident)."""
    display = ctx.display
    while True:
        if display.abort_event.is_set() or display.finish_signal.requested:
            return
        try:
            chunk = ctx.work_q.get_nowait()
        except queue.Empty:
            return
        try:
            _attempt_chunk(slot, chunk, ctx)
        finally:
            ctx.work_q.task_done()


def _attempt_chunk(slot: int, chunk: Path, ctx: _WorkerContext) -> None:
    """Encode one chunk, then (if it choked) optionally try the auto-fix
    retry. On unhandled exception, log + mark choked + push a synthetic
    result so the post-loop skip aggregation picks it up."""
    display = ctx.display
    elapsed = 0.0
    try:
        r = _encode_one_chunk_with_display(
            slot, chunk, ctx.workdir, display,
            crf=ctx.crf, preset=ctx.preset, pix_fmt=ctx.pix_fmt,
            x265_params=ctx.x265_params,
        )
        chunk_path, rc, elapsed, _err_tail = r
        with display.lock:
            chunk_was_choked = chunk.name in display.choked_chunks

        if (rc != 0 and chunk_was_choked and ctx.auto_fix_choke
                and _try_autofix(slot, chunk_path, ctx, elapsed)):
            _submit_quality_check(ctx, chunk_path)
            return

        with ctx.results_lock:
            ctx.results.append(r)
        if rc == 0:
            # Submit to the per-chunk quality guard (no-op when threshold
            # is unset). Done OUTSIDE the results_lock so the guard's
            # background queue doesn't serialize result accumulation.
            _submit_quality_check(ctx, chunk_path)
    except Exception as ex:
        _record_worker_exception(slot, chunk, ctx, ex)
    finally:
        # Fire on_chunk_done exactly once per attempt, from ground truth (does
        # enc_<stem>.mkv exist?). In `finally` so it also covers the autofix-
        # success early return and the exception path. fire is no-raise, so it
        # can never turn a real chunk success into a worker-killing error.
        fire_for_chunk(ctx.chunk_hook, chunk=chunk, workdir=ctx.workdir,
                       position_of=ctx.position_of, elapsed=elapsed,
                       log=display.events.put)


def _submit_quality_check(ctx: _WorkerContext, chunk_path: Path) -> None:
    """Hand a freshly-finalized chunk to the QualityGuard. The encoded chunk
    lives at ``workdir/enc_<stem>.mkv``; the source it was encoded from is
    ``workdir/<chunk.name>``. chunk_idx is the 0-based position in temporal
    order (NOT encode order) — position_of is 1-based, so subtract 1. The
    warmup grace inside QualityGuard uses chunk_idx==0 to spare the first
    temporal chunk from threshold comparison."""
    if ctx.quality_guard is None:
        return
    enc_path = ctx.workdir / f"enc_{chunk_path.stem}.mkv"
    chunk_idx_0_based = ctx.position_of.get(chunk_path, 1) - 1
    ctx.quality_guard.submit(
        chunk_idx=chunk_idx_0_based,
        src=chunk_path,
        dst=enc_path,
    )


def _try_autofix(slot: int, chunk_path: Path, ctx: _WorkerContext,
                elapsed: float) -> bool:
    """Run try_auto_fix_chunk on a choked chunk. Returns True on success
    (a clean enc_*.mkv was produced and the chunk has been removed from
    display.choked_chunks); False otherwise. Exceptions are caught and
    logged so the worker survives a buggy auto-fix path."""
    try:
        ok = try_auto_fix_chunk(
            chunk_path, ctx.workdir, ctx.display,
            slot=slot, crf=ctx.crf, preset=ctx.preset,
            pix_fmt=ctx.pix_fmt,
            x265_params=ctx.x265_params_for_autofix,
        )
    except Exception as ex:
        ctx.display.events.put(
            f"  ! {chunk_path.name}: auto-fix raised "
            f"{type(ex).__name__}: {ex} — chunk left for needs_fix")
        return False
    if not ok:
        return False
    # Promote the auto-fix result into results so it counts as a success.
    # try_auto_fix_chunk already removed the chunk from choked_chunks.
    with ctx.results_lock:
        ctx.results.append((chunk_path, 0, elapsed, ""))
    return True


def _record_worker_exception(slot: int, chunk: Path,
                            ctx: _WorkerContext, ex: Exception) -> None:
    """An exception escaped the encode helper. Log it, mark the chunk as
    choked so the post-encode collector writes a needs_fix sidecar, and
    let the worker keep going on the next chunk."""
    display = ctx.display
    display.events.put(
        f"  ! {chunk.name}: encode raised {type(ex).__name__}: "
        f"{ex} — chunk left for needs_fix")
    with display.lock:
        display.choked_chunks.setdefault(chunk.name, {
            "slot_id": slot,
            "speed": 0.0,
            "wall_seconds": 0.0,
            "delta_video_seconds": 0.0,
            "delta_wall_seconds": 0.0,
            "exception": f"{type(ex).__name__}: {ex}",
        })
        display.has_choked_chunks.set()
        display.slots.pop(slot, None)
    with ctx.results_lock:
        ctx.results.append((chunk, 1, 0.0, str(ex)[-400:]))


def _render_tick(display: ParallelDisplay) -> None:
    """One periodic tick: size-threshold projection, choke detection, redraw.

    Wrapped so a bug in ANY of the three can't propagate out of the render
    thread. That thread is also where check_threshold (the size guard) and
    check_choke (the choke killer) run — so a render thread that dies on an
    unexpected exception silently disables BOTH safety mechanisms, not just
    the live display. The failure is surfaced via the events log instead."""
    try:
        display.sync_file_pause()
        display.check_threshold()
        display.check_choke()
        display.render()
    except Exception:
        import traceback
        display.events.put(
            "  ! render tick failed (continuing):\n" + traceback.format_exc())


def _render_loop(display: ParallelDisplay,
                stop_render: threading.Event) -> None:
    """Periodic threshold-projection + choke-detection + redraw. Wakes on
    a keypress via display.input_event for instant feedback; otherwise
    ticks at 2 Hz (every 500 ms). The final paint after stop_render is set
    keeps the screen showing the final state until shutdown completes.

    The 0.5 s wait runs every iteration even when a tick fails, so a
    persistent render error can't turn the loop into a busy-spin."""
    while not stop_render.is_set():
        _render_tick(display)
        display.input_event.wait(0.5)
        display.input_event.clear()
    # Final paint — also guarded so a render bug can't crash shutdown.
    try:
        display.render()
    except Exception:
        pass


def encode_chunks_parallel(chunks: list[Path], workdir: Path, *,
                          parallel: int,
                          crf: int, preset: str, pix_fmt: str,
                          x265_params: str,
                          total_duration_sec: float = 0,
                          source_bytes: int = 0,
                          max_output_bytes: int | None = None,
                          choke_threshold_speed: float = 0.05,
                          choke_grace_seconds: float = 300.0,
                          auto_fix_choke: bool = False,
                          segment_seconds: int = 60,
                          chunk_hook: ChunkHook | None = None,
                          visual_quality_threshold: float | None = None
                          ) -> list[dict]:
    """N concurrent ffmpegs encoding median-first chunks, with live render,
    threshold guard, choke detector, htop-style pause/resume. Returns the
    list of chunks that didn't produce a clean enc_*.mkv (empty = success).

    See encode_parallel.py module docstring for the per-thread responsibility
    breakdown. The orchestration here is intentionally thin — every concern
    is delegated to a free function or to the display layer."""
    total = len(chunks)
    encode_order = reorder_middle_first(chunks)
    todo = [c for c in encode_order
            if not (workdir / f"enc_{c.stem}.mkv").exists()]
    pos_of = {c: i for i, c in enumerate(chunks, 1)}
    already = total - len(todo)
    cores_per_chunk = max(2, (os.cpu_count() or 8) // parallel)

    print_encode_plan(
        todo, total, already,
        parallel=parallel, cores_per_chunk=cores_per_chunk,
        first_pos=pos_of[todo[0]] if todo else None,
        max_output_bytes=max_output_bytes, source_bytes=source_bytes,
    )
    if not todo:
        return []

    params_with_pools = x265_params_with_pools(x265_params, parallel)
    display = ParallelDisplay(
        parallel, total, already,
        workdir=workdir,
        total_duration_sec=total_duration_sec,
        source_bytes=source_bytes,
        max_output_bytes=max_output_bytes,
        choke_threshold_speed=choke_threshold_speed,
        choke_grace_seconds=choke_grace_seconds,
    )
    print_runtime_protections(display.has_job_protection)
    print_choke_guard_announcement(display.choke_threshold_speed,
                                   display.choke_grace_seconds)

    work_q: queue.Queue[Path] = queue.Queue()
    for c in todo:
        work_q.put(c)

    # Build the QualityGuard unconditionally. Threshold=None disables the
    # worker thread + makes submit() a no-op, so per-chunk wiring doesn't
    # need an `if` at every call site.
    def _on_quality_abort(info: QualityAbortInfo) -> None:
        # Set the abort flag FIRST so workers stop spawning new ffmpegs as
        # they finish their current chunk; the orchestrator post-join check
        # then routes to the quality branch (which has precedence over the
        # size-threshold branch in the post-loop check).
        display.quality_abort_info = info
        display.events.put(
            f"  ! quality threshold failed: chunk "
            f"{info.chunk_idx + 1} ({info.chunk_name}) "
            f"VMAF={info.vmaf_mean:.2f} < {info.threshold:g}")
        display.abort_event.set()
        # Terminate every in-flight encoder ffmpeg — mirror size_projection.
        # check_threshold's terminate sweep. Without this, with parallel=4 on
        # a slow 4K encode we'd let up to four 5–10-minute chunks finish
        # naturally after the abort decision, defeating the whole point of
        # the guard ("abort decisions land before the encoder wastes more
        # CPU"). Guard each terminate so a Windows handle race on one proc
        # doesn't skip the others.
        with display.lock:
            procs = list(display.active_procs.values())
        for proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 — guard seam, must not raise
                pass
        display.input_event.set()  # wake render loop for instant feedback

    quality_guard = QualityGuard(
        threshold=visual_quality_threshold,
        skip_first_chunk=True,
        vmaf_pair_fn=vmaf_pair,
        events_queue=display.events,
        on_abort=_on_quality_abort,
        # Merge each VMAF decision (ok / warmup-grace / abort / infra-fail)
        # into the v1.18.0 per-chunk metrics JSONL log. update_chunk_quality
        # is a module-level shim — no-op if no log was initialized, so the
        # wiring is harmless for legacy / test paths.
        metrics_update_fn=update_chunk_quality,
    )
    if visual_quality_threshold is not None:
        print(f"      Quality guard: stop file if any chunk's VMAF mean < "
              f"{visual_quality_threshold:g} (first chunk graced)")

    ctx = _WorkerContext(
        display=display, work_q=work_q,
        results=[], results_lock=threading.Lock(),
        workdir=workdir,
        crf=crf, preset=preset, pix_fmt=pix_fmt,
        x265_params=params_with_pools,
        x265_params_for_autofix=x265_params,
        auto_fix_choke=auto_fix_choke,
        chunk_hook=chunk_hook,
        position_of=pos_of,
        quality_guard=quality_guard,
    )

    stop_render = threading.Event()
    render_thread = threading.Thread(
        target=_render_loop, args=(display, stop_render), daemon=True,
    )
    render_thread.start()

    stop_keys = threading.Event()
    key_thread = threading.Thread(
        target=keyboard_listener, args=(display, stop_keys), daemon=True,
    )
    key_thread.start()

    worker_threads = [
        threading.Thread(target=_worker, args=(slot, ctx), daemon=True)
        for slot in range(parallel)
    ]
    for t in worker_threads:
        t.start()
    try:
        for t in worker_threads:
            t.join()
    finally:
        # If the script exits while any slot is paused, the corresponding
        # worker thread is blocked on subprocess.wait() forever. Resume on
        # the way out so workers can finish their wait() and ffmpeg can
        # clean up its own buffers.
        for msg in display.resume_all():
            if "RESUMED" in msg:
                print(msg)

    # Tear down render + key threads. input_event.set() unblocks the render
    # thread's 500 ms wait so shutdown isn't bottlenecked on that timer.
    stop_render.set()
    display.input_event.set()
    render_thread.join(timeout=2)
    stop_keys.set()
    key_thread.join(timeout=1)
    # Drain any pending quality checks BEFORE the post-loop branching. If a
    # check was already in-flight on the last chunk it may still flip the
    # abort flag — wait for it so the quality branch wins over a clean exit.
    quality_guard.stop(timeout=90)

    skipped = collect_skipped(
        chunks, workdir, display,
        x265_params=x265_params, preset=preset, crf=crf, pix_fmt=pix_fmt,
        segment_seconds=segment_seconds,
    )

    # Quality-threshold abort: take precedence over size-threshold so that
    # if BOTH guards fire near-simultaneously (size guard at chunk N, quality
    # guard at chunk N+1) we report the quality failure — it's the user-
    # actionable signal (size abort is "good source, raise CRF"; quality
    # abort is "source is uncompressible at this quality, skip").
    if display.quality_abort_info is not None:
        info: QualityAbortInfo = display.quality_abort_info  # type: ignore[assignment]
        reason = (f"chunk {info.chunk_idx + 1} ({info.chunk_name}) "
                  f"VMAF={info.vmaf_mean:.2f} < {info.threshold:g}")
        mark_status("stopped-quality-threshold",
                    chunk_idx=info.chunk_idx,
                    chunk_name=info.chunk_name,
                    vmaf_mean=info.vmaf_mean,
                    threshold=info.threshold)
        set_stop_context(
            reason="stopped-quality-threshold",
            detail=reason,
        )
        print_quality_threshold_abort_block(
            workdir,
            chunk_idx=info.chunk_idx,
            chunk_name=info.chunk_name,
            vmaf_mean=info.vmaf_mean,
            threshold=info.threshold,
        )
        sys.exit(9)

    if display.abort_event.is_set():
        # Mark the in-progress history record as threshold-aborted before
        # the atexit hook flushes it. Also publish the human-readable
        # reason + projection/threshold byte counts to the on_job_end hook
        # via the recorder's stop context, since the atexit flush path can't
        # reach the display's state after sys.exit hands off.
        mark_status("stopped-threshold", abort_reason=display.abort_reason)
        set_stop_context(
            reason="stopped-threshold",
            detail=display.abort_reason,
            output_bytes_projected=getattr(display, "last_projection_bytes",
                                           None),
            output_bytes_threshold=display.max_output_bytes,
        )
        print_threshold_abort_block(workdir, display.abort_reason)
        sys.exit(3)

    # User asked to finish after the current chunk(s): stop resumably if any
    # chunks remain. The threshold abort (above) takes precedence.
    if display.finish_signal.requested:
        # Clear the sentinel whenever a finish was requested — even if every
        # chunk happened to finish first — so a stale FINISH file can't stop
        # the next run in this workdir.
        display.finish_signal.consume_stop_file()
        remaining = [c for c in chunks
                     if not (workdir / f"enc_{c.stem}.mkv").exists()]
        if remaining:
            mark_status("stopped-by-user",
                        remaining_chunks=len(remaining),
                        total_chunks=len(chunks))
            print_finish_stopped_block(workdir, len(remaining), len(chunks))
            sys.exit(8)

    # Real failures = non-zero rc that isn't a choke skip (those are
    # already tallied above and don't sys.exit — they cause concat to be
    # skipped by the caller's awaiting-chunk-fix branch).
    skipped_names = {s["chunk_name"] for s in skipped}
    failures = [(c, rc, err) for c, rc, _, err in ctx.results
                if rc != 0 and c.name not in skipped_names]
    if failures:
        mark_status("chunk-failed",
                    failed_chunks=[f.name for f, _, _ in failures])
        names = ", ".join(f.name for f, _, _ in failures)
        sys.exit(f"ERROR: {len(failures)} chunk(s) failed: {names}. "
                 "Re-run to retry the failed chunks.")

    return skipped

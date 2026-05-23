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
from dataclasses import dataclass
from pathlib import Path

from .chunk_recovery import try_auto_fix_chunk
from .chunk_worker import _encode_one_chunk_with_display
from .chunking import reorder_middle_first, x265_params_with_pools
from .display import ParallelDisplay
from .history_state import mark_status
from .keyboard_input import keyboard_listener
from .messages import (
    print_choke_guard_announcement,
    print_encode_plan,
    print_runtime_protections,
    print_threshold_abort_block,
)
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


def _worker(slot: int, ctx: _WorkerContext) -> None:
    """Pull chunks off the queue until empty or abort. Each chunk attempt is
    wrapped in its own try/except so a bug in the encode helper OR auto-fix
    can't kill the worker (which historically left the queue dead with no
    needs_fix sidecar — the Linda 0003 incident)."""
    display = ctx.display
    while True:
        if display.abort_event.is_set():
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
    try:
        r = _encode_one_chunk_with_display(
            slot, chunk, ctx.workdir, display,
            crf=ctx.crf, preset=ctx.preset, pix_fmt=ctx.pix_fmt,
            x265_params=ctx.x265_params,
        )
        chunk_path, rc, elapsed, _err_tail = r
        with display.lock:
            chunk_was_choked = chunk.name in display.choked_chunks

        if rc != 0 and chunk_was_choked and ctx.auto_fix_choke:
            if _try_autofix(slot, chunk_path, ctx, elapsed):
                return

        with ctx.results_lock:
            ctx.results.append(r)
    except Exception as ex:
        _record_worker_exception(slot, chunk, ctx, ex)


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


def _render_loop(display: ParallelDisplay,
                stop_render: threading.Event) -> None:
    """Periodic threshold-projection + choke-detection + redraw. Wakes on
    a keypress via display.input_event for instant feedback; otherwise
    ticks at 2 Hz (every 500 ms). The final paint after stop_render is set
    keeps the screen showing the final state until shutdown completes."""
    while not stop_render.is_set():
        display.check_threshold()
        display.check_choke()
        display.render()
        display.input_event.wait(0.5)
        display.input_event.clear()
    display.render()


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
                          segment_seconds: int = 60) -> list[dict]:
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

    ctx = _WorkerContext(
        display=display, work_q=work_q,
        results=[], results_lock=threading.Lock(),
        workdir=workdir,
        crf=crf, preset=preset, pix_fmt=pix_fmt,
        x265_params=params_with_pools,
        x265_params_for_autofix=x265_params,
        auto_fix_choke=auto_fix_choke,
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

    skipped = collect_skipped(
        chunks, workdir, display,
        x265_params=x265_params, preset=preset, crf=crf, pix_fmt=pix_fmt,
        segment_seconds=segment_seconds,
    )

    if display.abort_event.is_set():
        # Mark the in-progress history record as threshold-aborted before
        # the atexit hook flushes it.
        mark_status("stopped-threshold", abort_reason=display.abort_reason)
        print_threshold_abort_block(workdir, display.abort_reason)
        sys.exit(3)

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

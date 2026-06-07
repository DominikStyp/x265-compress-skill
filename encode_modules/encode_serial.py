"""Serial (one-at-a-time) chunk encoding loop.

Picked over the parallel encoder when --parallel=1 AND no threshold/choke
guards (those live in the parallel-mode display, so any threshold-aware
encode is routed through the parallel path even at parallel=1). Uses the
standalone `progress.py` percentage bar that ships with the skill — same
UX as the legacy non-resumable .bat.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from platform_compat import (
    IS_POSIX,
    IS_WINDOWS,
    low_priority_popen_kwargs,
    wrap_cmd_for_low_priority,
)

from .chunk_hook import ChunkHook, fire_for_chunk
from .chunk_metrics_log import record_chunk_metrics
from .chunking import ffmpeg_chunk_cmd, reorder_middle_first
from .finish_signal import FINISH_FILENAME, FinishSignal
from .history_state import mark_status, record_chunk_elapsed
from .messages import print_finish_stopped_block
from .probes import fmt_dur, probe_duration


def encode_chunks_serial(chunks: list[Path], workdir: Path, *,
                        crf: int, preset: str, pix_fmt: str,
                        x265_params: str,
                        chunk_hook: ChunkHook | None = None) -> None:
    """One chunk at a time, with the standalone progress.py percentage bar.

    Chunks already on disk as enc_*.mkv are skipped (resumability). Encode
    order is median-first via `reorder_middle_first` so the projection cache
    used by --max-size-percent — when it applies — is fed a representative
    high-bitrate sample early. Chunk filenames preserve original positions,
    so concat reassembles correctly regardless of encode order."""
    total = len(chunks)
    already = sum(1 for c in chunks if (workdir / f"enc_{c.stem}.mkv").exists())
    print(f"[2/4] Encoding chunks (serial). {already}/{total} already done — resuming.")
    if IS_WINDOWS or IS_POSIX:
        # Same effective behaviour on both OSes — foreground apps preempt
        # the encode. Win32 IDLE_PRIORITY_CLASS / POSIX nice 19.
        print("      CPU priority: ffmpeg runs at low priority — "
              "foreground apps always preempt encode.")
    progress_script = str(Path(__file__).resolve().parent.parent / "progress.py")

    encode_order = reorder_middle_first(chunks)
    pos_of = {c: i for i, c in enumerate(chunks, 1)}
    todo_preview = [c for c in encode_order
                    if not (workdir / f"enc_{c.stem}.mkv").exists()]
    if todo_preview and total > 1:
        first_pos = pos_of[todo_preview[0]]
        print(f"      Encoding order: middle-first "
              f"(next chunk: {first_pos}/{total})")

    finish_signal = FinishSignal(workdir / FINISH_FILENAME)
    for chunk in encode_order:
        out = workdir / f"enc_{chunk.stem}.mkv"
        if out.exists():
            continue
        if finish_signal.requested:
            # User asked to finish after the current chunk; the previous chunk
            # has already completed (we're at the top of the next iteration).
            # Stop resumably — a re-run picks up from here.
            finish_signal.consume_stop_file()
            remaining = sum(1 for c in encode_order
                            if not (workdir / f"enc_{c.stem}.mkv").exists())
            mark_status("stopped-by-user",
                        remaining_chunks=remaining, total_chunks=total)
            print_finish_stopped_block(workdir, remaining, total)
            sys.exit(8)
        part = workdir / f"enc_{chunk.stem}.part.mkv"
        if part.exists():
            # Serial path doesn't have choke detection so it can safely
            # unlink the stale .part — there's no other concurrent writer
            # and we're about to overwrite it anyway.
            part.unlink()
        chunk_dur = probe_duration(chunk)
        i = pos_of[chunk]
        print(f"      Chunk {i}/{total}: {chunk.name}  ({fmt_dur(chunk_dur)})")

        chunk_start = time.monotonic()
        ff = subprocess.Popen(
            wrap_cmd_for_low_priority(
                ffmpeg_chunk_cmd(chunk, part, crf=crf, preset=preset,
                                pix_fmt=pix_fmt, x265_params=x265_params,
                                extra_progress=["-progress", "-"])
            ),
            stdout=subprocess.PIPE,
            **low_priority_popen_kwargs(),
        )
        prog = subprocess.Popen(
            [sys.executable, "-u", progress_script,
             "--duration", str(max(0.001, chunk_dur))],
            stdin=ff.stdout,
        )
        ff.stdout.close()
        prog.wait()
        rc = ff.wait()
        chunk_elapsed = time.monotonic() - chunk_start

        if rc != 0:
            if part.exists():
                part.unlink()
            # Fire the failure hook before exiting (enc_*.mkv absent -> status
            # "failed"), so an alerting hook learns about the failed chunk too.
            fire_for_chunk(chunk_hook, chunk=chunk, workdir=workdir,
                           position_of=pos_of, elapsed=chunk_elapsed,
                           log=print)
            sys.exit(f"ERROR: encode failed on {chunk.name} (exit {rc}). "
                     "Re-run to resume from this chunk.")
        part.rename(out)
        # Mirror the parallel encoder's history hook so the JSONL log
        # carries per-chunk wall times regardless of encoder path.
        record_chunk_elapsed(chunk.name, chunk_elapsed)
        # Same v1.18.0 chunk_metrics_log emission as the parallel path —
        # the queue-runner aggregator works identically for both encoders.
        try:
            out_bytes = out.stat().st_size
        except OSError:
            out_bytes = 0
        record_chunk_metrics(
            chunk_name=chunk.name,
            encode_elapsed_s=chunk_elapsed,
            chunk_duration_s=chunk_dur,
            output_bytes=out_bytes,
        )
        fire_for_chunk(chunk_hook, chunk=chunk, workdir=workdir,
                       position_of=pos_of, elapsed=chunk_elapsed, log=print)

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

from .chunking import ffmpeg_chunk_cmd, reorder_middle_first
from .history_state import record_chunk_elapsed
from .probes import fmt_dur, probe_duration
from .process_control import IDLE_PRIORITY_FLAGS


def encode_chunks_serial(chunks: list[Path], workdir: Path, *,
                        crf: int, preset: str, pix_fmt: str,
                        x265_params: str) -> None:
    """One chunk at a time, with the standalone progress.py percentage bar.

    Chunks already on disk as enc_*.mkv are skipped (resumability). Encode
    order is median-first via `reorder_middle_first` so the projection cache
    used by --max-size-percent — when it applies — is fed a representative
    high-bitrate sample early. Chunk filenames preserve original positions,
    so concat reassembles correctly regardless of encode order."""
    total = len(chunks)
    already = sum(1 for c in chunks if (workdir / f"enc_{c.stem}.mkv").exists())
    print(f"[2/4] Encoding chunks (serial). {already}/{total} already done — resuming.")
    if IDLE_PRIORITY_FLAGS:
        print("      CPU priority: ffmpeg runs at IDLE — foreground apps "
              "(browser, editor) always preempt encode.")
    progress_script = str(Path(__file__).resolve().parent.parent / "progress.py")

    encode_order = reorder_middle_first(chunks)
    pos_of = {c: i for i, c in enumerate(chunks, 1)}
    todo_preview = [c for c in encode_order
                    if not (workdir / f"enc_{c.stem}.mkv").exists()]
    if todo_preview and total > 1:
        first_pos = pos_of[todo_preview[0]]
        print(f"      Encoding order: middle-first "
              f"(next chunk: {first_pos}/{total})")

    for chunk in encode_order:
        out = workdir / f"enc_{chunk.stem}.mkv"
        if out.exists():
            continue
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
            ffmpeg_chunk_cmd(chunk, part, crf=crf, preset=preset,
                            pix_fmt=pix_fmt, x265_params=x265_params,
                            extra_progress=["-progress", "-"]),
            stdout=subprocess.PIPE,
            creationflags=IDLE_PRIORITY_FLAGS,
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
            sys.exit(f"ERROR: encode failed on {chunk.name} (exit {rc}). "
                     "Re-run to resume from this chunk.")
        part.rename(out)
        # Mirror the parallel encoder's history hook so the JSONL log
        # carries per-chunk wall times regardless of encoder path.
        record_chunk_elapsed(chunk.name, chunk_elapsed)

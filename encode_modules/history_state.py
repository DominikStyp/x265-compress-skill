"""Live encode-record state that feeds `history.py`'s JSONL append.

Bridges encoder events → the on-disk history log. The module-level
`_current_encode_state` dict accumulates input metadata, settings, per-chunk
timings, and outcome as the encode progresses. `_flush_history()` (registered
via atexit) guarantees a record lands on disk even on `sys.exit(3)` (threshold
abort) or unexpected crashes — partial data beats no data for the
stats-tracking use case.

Why module-level state rather than passing a context object through every
layer: the chunk-encoder worker threads sit ~4 call frames deep inside the
encode pipeline. Threading a `history_ctx` parameter through every helper
just so worker threads can stash one elapsed time would add noise without
adding clarity. Module state with a couple of well-named entry points
(`record_chunk_elapsed`, `mark_status`) keeps the call sites tiny.

Idempotency: a `_history_written` flag prevents the synchronous flush at the
end of `main()` AND the atexit hook from emitting two records for the same
encode.
"""
from __future__ import annotations

import argparse
import atexit
import sys
from pathlib import Path

import history as _hist
from .probes import probe_full


_current_encode_state: dict | None = None
_history_written = False


def _flush_history() -> None:
    """Append the in-progress encode record to encoding_history.jsonl.
    Idempotent — second calls are no-ops. Never raises (encoding mustn't
    fail because the side-channel log misbehaved)."""
    global _history_written
    if _history_written or _current_encode_state is None:
        return
    try:
        _current_encode_state.setdefault("timestamp_end_utc", _hist.now_iso_utc())
        _hist.append_record(_current_encode_state)
        _history_written = True
    except Exception as e:
        print(f"WARNING: history flush failed: {e}", file=sys.stderr)


# Registered once at import time. Fires on clean exit, sys.exit(N), and
# unhandled exceptions (but NOT on os._exit / SIGKILL / taskkill /F).
atexit.register(_flush_history)


def init_history_state(src: Path, dst: Path, args: argparse.Namespace,
                      source_bytes: int) -> None:
    """Seed the module-level encode history record with input metadata,
    settings, and environment fingerprint. Populated EARLY (before encode
    starts) so a mid-encode crash / threshold abort still produces a JSONL
    record with usable context — chunk timings and outcomes fill in later."""
    global _current_encode_state, _history_written
    _history_written = False
    try:
        _current_encode_state = {
            "schema_version": _hist.SCHEMA_VERSION,
            "timestamp_start_utc": _hist.now_iso_utc(),
            "status": "in_progress",
            "input": _hist.build_input_block(src, probe_full(src)),
            "output": {"path": str(dst)},
            "settings": {
                "crf": args.crf,
                "preset": args.preset,
                "pix_fmt": args.pix_fmt,
                "x265_params": args.x265_params,
                "parallel": args.parallel,
                "segment_seconds": args.segment_seconds,
                "max_size_percent": (
                    round(args.max_output_bytes / source_bytes * 100, 1)
                    if args.max_output_bytes and source_bytes else None
                ),
                "max_output_bytes": args.max_output_bytes,
            },
            "environment": _hist.collect_environment(),
            "chunk_elapsed": {},
        }
    except Exception as e:
        # History setup must never block the encode.
        print(f"WARNING: history state init failed: {e}", file=sys.stderr)
        _current_encode_state = None


def record_chunk_elapsed(chunk_name: str, elapsed_s: float) -> None:
    """Stash one chunk's wall-clock encode time into the in-progress record.
    Called by both the serial and parallel encoders the instant a chunk
    successfully renames from .part.mkv to .mkv. No-op if history state
    wasn't initialized."""
    if _current_encode_state is not None:
        _current_encode_state.setdefault("chunk_elapsed", {})[chunk_name] = elapsed_s


def mark_status(status: str, **extra) -> None:
    """Set the outcome status on the in-progress record so the atexit flush
    (or a later finalize_history_state on the happy path) records it
    accurately. Common values:
        "stopped-threshold"   max-size-percent guard fired
        "chunk-failed"        ffmpeg returned non-zero on a chunk
        "verify-failed"       output failed verify_output() after retries
        "ok"                  (set by finalize_history_state on success)
    `extra` keys (e.g. `abort_reason`, `failed_chunks`, `verify_problems`)
    are merged into the record verbatim."""
    if _current_encode_state is None:
        return
    _current_encode_state["status"] = status
    _current_encode_state.update(extra)


def finalize_history_state(src: Path, dst: Path, workdir: Path,
                          chunks: list[Path],
                          wall_seconds: float,
                          quality_scores: dict | None,
                          encode_order: list[Path] | None = None) -> None:
    """Fill in the success-path fields of the history record (output size,
    reduction, per-chunk records, quality scores) and flush synchronously.
    Idempotent — atexit's hook is a no-op after this runs.

    Must run BEFORE the workdir is cleaned up: `_hist.build_chunk_records`
    probes each chunk's duration and reads file sizes off disk."""
    if _current_encode_state is None:
        return
    try:
        src_size = src.stat().st_size if src.exists() else None
        out_size = dst.stat().st_size if dst.exists() else None
        _current_encode_state["status"] = "ok"
        _current_encode_state["wall_seconds"] = round(wall_seconds, 3)
        _current_encode_state["output"] = {
            "path": str(dst),
            "size_bytes": out_size,
            "container": "matroska",
        }
        if src_size and out_size is not None:
            _current_encode_state["reduction"] = {
                "bytes_saved": src_size - out_size,
                "pct_saved": round((1 - out_size / src_size) * 100, 2),
            }
        _current_encode_state["chunks"] = _hist.build_chunk_records(
            workdir, chunks,
            elapsed_by_chunk=_current_encode_state.get("chunk_elapsed", {}),
            encode_order=encode_order,
        )
        if quality_scores:
            _current_encode_state["quality"] = dict(quality_scores)
    except Exception as e:
        print(f"WARNING: history finalize failed: {e}", file=sys.stderr)
    _flush_history()

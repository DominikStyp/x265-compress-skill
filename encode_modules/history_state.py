"""Live encode-record state that feeds `history.py`'s JSONL append.

Bridges encoder events → the on-disk history log. A singleton
`HistoryRecorder` instance accumulates input metadata, settings, per-chunk
timings, and outcome as the encode progresses. `_flush_history()`
(registered via atexit) guarantees a record lands on disk even on
`sys.exit(3)` (threshold abort) or unexpected crashes — partial data
beats no data for the stats-tracking use case.

Why a class wrapped behind module-level functions: the chunk-encoder
worker threads sit ~4 call frames deep inside the encode pipeline.
Threading a `recorder` parameter through every helper just so workers can
stash one elapsed time would add noise without adding clarity. Module
functions (`record_chunk_elapsed`, `mark_status`, etc.) keep call sites
tiny, while the `HistoryRecorder` class behind them prevents the latent
bug where two concurrent encodes in the same Python process would have
corrupted each other's records (and gives a future test suite a clean
seam — instantiate a fresh recorder per test instead of leaking globals).

Idempotency: a `_history_written` flag prevents the synchronous flush at
the end of `main()` AND the atexit hook from emitting two records for
the same encode.
"""
from __future__ import annotations

import argparse
import atexit
import sys
import threading
from pathlib import Path

import history as _hist
from .probes import probe_full


class HistoryRecorder:
    """Encapsulates the in-progress encode record. Replaces the previous
    pair of module-level globals (`_current_encode_state`, `_history_written`).

    Single-encode-per-process expected: the encoder pipeline creates one
    `HistoryRecorder` for the duration of one run via `init()`, then
    `finalize()`s it at success or relies on the `atexit` hook to flush
    a partial record on early exit."""

    def __init__(self) -> None:
        self.current: dict | None = None
        self.written: bool = False
        # Guards record_chunk_elapsed, the one method N worker threads call
        # concurrently. Defensive only: each call writes a distinct key, so
        # it's already safe under CPython's GIL and under PEP 703
        # free-threading. The lock keeps this class consistent with the
        # codebase's lock-all-shared-mutable-state discipline (cf.
        # ParallelDisplay) and stays correct if the update ever grows
        # non-atomic. init/mark_status/finalize run single-threaded (before
        # workers start / after they join), so they don't need it.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, src: Path, dst: Path, args: argparse.Namespace,
             source_bytes: int) -> None:
        """Seed the in-progress record with input metadata + settings.

        Populated EARLY (before any encode work) so a mid-encode crash
        or threshold abort still produces a JSONL row with usable
        context — chunk timings and outcome fill in later."""
        self.written = False
        try:
            self.current = {
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
            self.current = None

    def record_chunk_elapsed(self, chunk_name: str, elapsed_s: float) -> None:
        """Stash one chunk's wall-clock encode time. Called the instant a
        chunk's .part.mkv renames to .mkv (serial and parallel paths), from
        any worker thread. No-op if `init()` failed."""
        with self._lock:
            if self.current is not None:
                self.current.setdefault("chunk_elapsed", {})[chunk_name] = elapsed_s

    def mark_status(self, status: str, **extra) -> None:
        """Set the outcome status on the in-progress record. Used by exit
        paths so the atexit flush captures the reason an encode stopped.

        Common values:
            "stopped-threshold"   max-size-percent guard fired
            "chunk-failed"        ffmpeg returned non-zero on a chunk
            "verify-failed"       output failed verify_output() after retries
            "awaiting-chunk-fix"  per-chunk choke → needs_fix sidecar dropped
            "pre-flight-failed"   source-corruption pre-scan failed
            "ok"                  set by finalize() on the success path
        Extra keys (e.g. `abort_reason`, `failed_chunks`, `verify_problems`)
        merge verbatim into the record."""
        if self.current is None:
            return
        self.current["status"] = status
        self.current.update(extra)

    def finalize(self, src: Path, dst: Path, workdir: Path,
                 chunks: list[Path],
                 wall_seconds: float,
                 quality_scores: dict | None,
                 encode_order: list[Path] | None = None) -> None:
        """Fill in the success-path fields (output size, reduction,
        per-chunk records, quality scores) and flush synchronously.
        Idempotent — the atexit hook is a no-op after this runs.

        Must run BEFORE the workdir is cleaned up: `_hist.build_chunk_records`
        probes each chunk's duration and reads file sizes off disk."""
        if self.current is None:
            return
        try:
            src_size = src.stat().st_size if src.exists() else None
            out_size = dst.stat().st_size if dst.exists() else None
            self.current["status"] = "ok"
            self.current["wall_seconds"] = round(wall_seconds, 3)
            self.current["output"] = {
                "path": str(dst),
                "size_bytes": out_size,
                "container": "matroska",
            }
            if src_size and out_size is not None:
                self.current["reduction"] = {
                    "bytes_saved": src_size - out_size,
                    "pct_saved": round((1 - out_size / src_size) * 100, 2),
                }
            self.current["chunks"] = _hist.build_chunk_records(
                workdir, chunks,
                elapsed_by_chunk=self.current.get("chunk_elapsed", {}),
                encode_order=encode_order,
            )
            if quality_scores:
                self.current["quality"] = dict(quality_scores)
        except Exception as e:
            print(f"WARNING: history finalize failed: {e}", file=sys.stderr)
        self.flush()

    def flush(self) -> None:
        """Append the in-progress record to encoding_history.jsonl.
        Idempotent — second calls are no-ops. Never raises (encoding
        mustn't fail because the side-channel log misbehaved)."""
        if self.written or self.current is None:
            return
        try:
            self.current.setdefault("timestamp_end_utc", _hist.now_iso_utc())
            _hist.append_record(self.current)
            self.written = True
        except Exception as e:
            print(f"WARNING: history flush failed: {e}", file=sys.stderr)


# Singleton instance — one HistoryRecorder per process lifetime.
# Tests can mutate `_recorder` to reset state between cases.
_recorder = HistoryRecorder()
atexit.register(_recorder.flush)


# Module-level API: forward to the singleton so call sites stay terse.
# (Bound methods are captured at module load; if a test replaces `_recorder`
# it should also re-bind these names — see `_reset_for_tests` below.)
def init_history_state(src: Path, dst: Path, args: argparse.Namespace,
                      source_bytes: int) -> None:
    _recorder.init(src, dst, args, source_bytes)


def record_chunk_elapsed(chunk_name: str, elapsed_s: float) -> None:
    _recorder.record_chunk_elapsed(chunk_name, elapsed_s)


def mark_status(status: str, **extra) -> None:
    _recorder.mark_status(status, **extra)


def finalize_history_state(src: Path, dst: Path, workdir: Path,
                          chunks: list[Path],
                          wall_seconds: float,
                          quality_scores: dict | None,
                          encode_order: list[Path] | None = None) -> None:
    _recorder.finalize(src, dst, workdir, chunks, wall_seconds,
                       quality_scores, encode_order)


def _reset_for_tests() -> None:
    """Replace the singleton + re-register its atexit flush. Only meant for
    tests — production code never resets mid-encode."""
    global _recorder
    _recorder = HistoryRecorder()
    atexit.register(_recorder.flush)

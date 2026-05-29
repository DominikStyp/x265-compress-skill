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
from .file_complete_hook import FileCompleteHook
from .job_end_hook import JobEndHook
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
        # on_job_end hook (optional). Bound by encode_resumable.main() after
        # reading the sidecar; None means "no job-end hook configured". Fires
        # exactly once, right after the JSONL record lands in flush().
        self._job_end_hook: JobEndHook | None = None
        # on_file_complete hook (optional). Success-only — fires from flush()
        # BEFORE the job-end hook when status == "ok" AND output exists.
        self._file_complete_hook: FileCompleteHook | None = None
        # Static job-end context the encoder hands in alongside the hook.
        # Most fields are derivable from `self.current` after finalize, but
        # threshold-aborts populate them earlier (so they survive the atexit
        # flush even when finalize() never ran).
        self._stop_reason: str = ""
        self._stop_detail: str = ""
        self._crf_retry_chain: str = ""
        self._output_bytes_projected: int | None = None
        self._output_bytes_threshold: int | None = None

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

    def attach_job_end_hook(self, hook: JobEndHook | None) -> None:
        """Wire the on_job_end hook so flush() can fire it exactly once with
        the final status. None disables the hook (the default)."""
        self._job_end_hook = hook

    def attach_file_complete_hook(self,
                                  hook: FileCompleteHook | None) -> None:
        """Wire the on_file_complete hook so flush() fires it BEFORE
        on_job_end on the `ok` path (and skips it otherwise). None disables."""
        self._file_complete_hook = hook

    def set_stop_context(self, *,
                         reason: str = "",
                         detail: str = "",
                         output_bytes_projected: int | None = None,
                         output_bytes_threshold: int | None = None) -> None:
        """Stash threshold/stop context BEFORE the exit path runs the flush.

        Called from the threshold-abort site so the job-end hook can report
        the projection + threshold even when sys.exit fires straight into
        the atexit flush. Empty / None values clear nothing — they're the
        caller's default, applied only when explicitly passed in."""
        if reason:
            self._stop_reason = reason
        if detail:
            self._stop_detail = detail
        if output_bytes_projected is not None:
            self._output_bytes_projected = output_bytes_projected
        if output_bytes_threshold is not None:
            self._output_bytes_threshold = output_bytes_threshold

    def flush(self) -> None:
        """Append the in-progress record to encoding_history.jsonl, then fire
        the on_job_end hook if one is attached. Idempotent — second calls are
        no-ops. Never raises (encoding mustn't fail because the side-channel
        log misbehaved)."""
        if self.written or self.current is None:
            return
        try:
            self.current.setdefault("timestamp_end_utc", _hist.now_iso_utc())
            _hist.append_record(self.current)
            self.written = True
        except Exception as e:
            print(f"WARNING: history flush failed: {e}", file=sys.stderr)
        # Fire hooks AFTER the audit row is on disk. Order: job_end first
        # (fires for every terminal status; carries reason/detail) so a slow
        # file-complete celebration push can't delay the job-end alert.
        # file_complete second (success-only; the "ready for next step"
        # notification). Each wrapped in its own try because the hooks'
        # no-raise contracts are belt-and-suspenders — anything escaping
        # here would crash atexit.
        try:
            self._fire_job_end_hook()
        except Exception as e:
            print(f"WARNING: on_job_end hook fire failed: {e}",
                  file=sys.stderr)
        try:
            self._fire_file_complete_hook()
        except Exception as e:
            print(f"WARNING: on_file_complete hook fire failed: {e}",
                  file=sys.stderr)

    def _fire_job_end_hook(self) -> None:
        """Build the per-job context from `self.current` + stored stop fields
        and invoke the attached JobEndHook. No-op when no hook is attached.

        Derives stop_reason/stop_detail from the JSONL record itself when the
        threshold-abort path didn't pre-populate them (the common case for
        chunk-failed, verify-failed, awaiting-chunk-fix, stopped-by-user) —
        so every non-ok terminal status carries enough context for a
        notification script to dispatch on."""
        hook = self._job_end_hook
        if hook is None or not hook.enabled or self.current is None:
            return
        rec = self.current
        status = rec.get("status", "")
        # Same field locations as the JSONL schema, kept as the single source
        # of truth — the hook just surfaces them via env vars.
        output = rec.get("output") or {}
        reduction = rec.get("reduction") or {}
        settings = rec.get("settings") or {}
        # X265_OUTPUT must point at a real final file: empty on every status
        # except "ok" so a notification script can use "X265_OUTPUT != ''" as
        # "the encoded mkv is on disk". init() seeds output.path early, so we
        # gate on status here, not on path presence.
        output_path = output.get("path") if status == "ok" else None
        out_bytes = output.get("size_bytes") if status == "ok" else None
        stop_reason, stop_detail = self._derive_stop_fields(rec, status)
        msg = hook.fire(
            status=status,
            stop_reason=stop_reason,
            stop_detail=stop_detail,
            crf=settings.get("crf"),
            crf_retry_chain=self._crf_retry_chain or str(
                settings.get("crf") or ""),
            output=Path(str(output_path)) if output_path else None,
            output_bytes_final=out_bytes,
            # The user's original source is what the hook reports — NOT the
            # auto-patched encode_src that the JSONL `input.size_bytes` holds
            # (that field feeds the size-projection guard which needs the
            # patched-size denominator). Same invariant as X265_SOURCE.
            source_bytes=self._stat_source_bytes(hook),
            output_bytes_projected=self._output_bytes_projected,
            output_bytes_threshold=self._output_bytes_threshold,
            wall_seconds=rec.get("wall_seconds"),
            pct_saved=reduction.get("pct_saved"),
        )
        if msg:
            print(msg, file=sys.stderr)

    def _derive_stop_fields(self, rec: dict, status: str) -> tuple[str, str]:
        """Build (stop_reason, stop_detail) from the record when the
        threshold-abort path didn't pre-populate them. `stop_reason` defaults
        to the JSONL status (so chunk-failed → reason='chunk-failed');
        `stop_detail` digs through the structured failure fields the encoder
        stashed under mark_status(..., **extras). Empty on the ok path so a
        notifier can detect "no problem" via stop_reason == ""."""
        if status == "ok":
            return "", ""
        reason = self._stop_reason or status
        if self._stop_detail:
            return reason, self._stop_detail
        # Best-effort detail digger — surface whichever structured field the
        # encoder actually populated for this status class.
        for key in ("abort_reason",):
            value = rec.get(key)
            if isinstance(value, str) and value:
                return reason, value
        for key in ("failed_chunks", "verify_problems", "skipped_chunks",
                    "remaining_chunks"):
            value = rec.get(key)
            if isinstance(value, list) and value:
                return reason, ", ".join(str(v) for v in value)
            if isinstance(value, int):
                return reason, f"{key}={value}"
        return reason, ""

    def _fire_file_complete_hook(self) -> None:
        """Fire on_file_complete from `self.current` + a fresh stat of the
        output. Success-only by contract — the hook itself filters
        status != "ok" or missing output, so this method just hands over
        the data."""
        hook = self._file_complete_hook
        if hook is None or not hook.enabled or self.current is None:
            return
        rec = self.current
        status = rec.get("status", "")
        if status != "ok":
            return
        output = rec.get("output") or {}
        reduction = rec.get("reduction") or {}
        settings = rec.get("settings") or {}
        quality = rec.get("quality") or {}
        output_path = output.get("path")
        # Refuse to fire when the file isn't actually on disk — the contract
        # is "ready for next step", not "we think we wrote it".
        if not output_path or not Path(str(output_path)).exists():
            return
        msg = hook.fire(
            status=status,
            output=Path(str(output_path)),
            output_bytes_final=output.get("size_bytes"),
            source_bytes=self._stat_source_bytes(hook),
            wall_seconds=rec.get("wall_seconds"),
            pct_saved=reduction.get("pct_saved"),
            crf=settings.get("crf"),
            crf_retry_chain=self._crf_retry_chain or str(
                settings.get("crf") or ""),
            vmaf_mean=quality.get("vmaf_mean"),
        )
        if msg:
            print(msg, file=sys.stderr)

    def _stat_source_bytes(self, hook) -> int | None:
        """Return the user's original source size in bytes. Stats the path the
        hook was bound to (the user's `src`, not the patched `encode_src`).
        Returns None on stat failure — the hook then emits an empty
        X265_SOURCE_BYTES rather than a stale or wrong value."""
        try:
            src = getattr(hook, "_source", None)
            if src is None:
                return None
            return Path(str(src)).stat().st_size
        except OSError:
            return None


# Singleton instance — one HistoryRecorder per process lifetime.
# Tests can mutate `_recorder` to reset state between cases.
_recorder = HistoryRecorder()
atexit.register(_recorder.flush)


# Module-level API: forward to the singleton so call sites stay terse. Each
# shim dereferences `_recorder` at call time, so `_reset_for_tests()` swapping
# the global is sufficient — the shims don't need rebinding.
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


def attach_job_end_hook(hook: JobEndHook | None) -> None:
    """Module-level shim for the recorder.attach_job_end_hook seam."""
    _recorder.attach_job_end_hook(hook)


def attach_file_complete_hook(hook: FileCompleteHook | None) -> None:
    """Module-level shim for the recorder.attach_file_complete_hook seam."""
    _recorder.attach_file_complete_hook(hook)


def set_stop_context(**kwargs) -> None:
    """Module-level shim for the recorder.set_stop_context seam (used by
    threshold-abort sites in encode_parallel)."""
    _recorder.set_stop_context(**kwargs)


def _reset_for_tests() -> None:
    """Replace the singleton + re-register its atexit flush. Only meant for
    tests — production code never resets mid-encode."""
    global _recorder
    _recorder = HistoryRecorder()
    atexit.register(_recorder.flush)

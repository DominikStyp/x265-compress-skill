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

Module layout: this file owns the in-memory record state (init / chunk
timings / mark_status / finalize / flush) and the singleton + shims. The
terminal-status hook-emission cluster (`fire_job_end_hook`,
`fire_file_complete_hook`, `derive_stop_fields`, `stat_source_bytes`) lives
in the sibling `history_hooks` module; `HistoryRecorder._fire_*_hook` are
thin wrappers delegating there (split out to stay under the 500-line cap).
"""
from __future__ import annotations

import argparse
import atexit
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING, TypedDict

import history as _hist
from .chunk_metrics_log import (
    build_summary_for_history_record as _build_chunk_metrics_summary,
)
from .history_hooks import (
    fire_file_complete_hook as _fire_file_complete_hook_impl,
    fire_job_end_hook as _fire_job_end_hook_impl,
)
from .probes import probe_full

if TYPE_CHECKING:
    # Annotation-only: the recorder holds opaque hook references and delegates
    # all firing to history_hooks' free functions, so it does NOT need the
    # concrete hook classes at runtime. Keeping these under TYPE_CHECKING
    # breaks the runtime coupling (the recorder is a state+flush coordinator,
    # not a hook factory) — `from __future__ import annotations` makes the
    # annotations lazy strings so this is safe.
    from .file_complete_hook import FileCompleteHook
    from .job_end_hook import JobEndHook


class HistoryRecord(TypedDict, total=False):
    """The shape of one encoding-history JSONL row (what ``self.current``
    accumulates and ``history.append_record`` serialises). ``total=False``:
    every key is optional because the record is built incrementally — ``init``
    seeds the input/settings block, chunk timings stream in, and the outcome
    fields land at ``finalize``/``flush``.

    This is **living documentation of the on-disk schema**, the single place
    the stable top-level keys are enumerated; it is not type-checker-enforced
    (the project ships no type checker) and does NOT capture the status-
    specific extras ``mark_status(status, **extra)`` adds (e.g. ``failed_chunks``,
    ``verify_problems``, ``abort_reason``, ``remaining_chunks``, ``total_chunks``,
    ``pre_flight_scan``) — those are deliberately open-ended. The on-disk format
    is an invariant (AGENTS.md): adding/removing/renaming a key here means
    bumping ``history.SCHEMA_VERSION`` and updating the backward-compat tests."""
    schema_version: int
    timestamp_start_utc: str
    status: str                 # in_progress → ok / stopped-* / *-failed / ...
    input: Dict[str, Any]       # build_input_block: codec/res/fps/bpp/size/...
    output: Dict[str, Any]      # path, size_bytes, container
    settings: Dict[str, Any]    # crf/preset/pix_fmt/x265_params/parallel/...
    environment: Dict[str, Any]
    chunk_elapsed: Dict[str, float]   # chunk_name -> wall seconds
    chunks: list                 # per-chunk records (build_chunk_records)
    reduction: Dict[str, Any]   # bytes_saved, pct_saved
    quality: Dict[str, Any]     # VMAF/PSNR/SSIM means (success path)
    chunk_metrics_summary: Dict[str, Any]
    wall_seconds: float


class HistoryRecorder:
    """Encapsulates the in-progress encode record. Replaces the previous
    pair of module-level globals (`_current_encode_state`, `_history_written`).

    Single-encode-per-process expected: the encoder pipeline creates one
    `HistoryRecorder` for the duration of one run via `init()`, then
    `finalize()`s it at success or relies on the `atexit` hook to flush
    a partial record on early exit."""

    def __init__(self) -> None:
        self.current: Optional[HistoryRecord] = None
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
        # AFTER the job-end hook, when status == "ok" AND output exists.
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
            # NOTE: the chunk_metrics_summary mirror lives in flush() (not
            # here) so it runs on EVERY terminal status — abort post-mortems
            # are the spec's headline use case (FEATURE-REQUEST_persist-...
            # lines 79-80). finalize() only runs on the success path.
        except Exception as e:
            print(f"WARNING: history finalize failed: {e}", file=sys.stderr)
        self.flush()

    def _mirror_chunk_metrics(self) -> None:
        """Fold the per-chunk metrics log into ``self.current`` under
        ``chunk_metrics_summary``. Called from ``flush()`` so the rollup
        lands in the JSONL row for EVERY terminal status — that's the
        spec's headline use case: post-mortem analysis of an abort.

        Logic lives in ``chunk_metrics_log.build_summary_for_history_record``
        so all chunk_metrics business lives in one module; this method is
        just the wiring + the never-raise contract."""
        if self.current is None:
            return
        try:
            summary = _build_chunk_metrics_summary(self.current)
            if summary is not None:
                self.current["chunk_metrics_summary"] = summary
        except Exception as e:
            print(f"WARNING: chunk_metrics mirror failed: {e}",
                  file=sys.stderr)

    def attach_job_end_hook(self, hook: JobEndHook | None) -> None:
        """Wire the on_job_end hook so flush() can fire it exactly once with
        the final status. None disables the hook (the default)."""
        self._job_end_hook = hook

    def attach_file_complete_hook(self,
                                  hook: FileCompleteHook | None) -> None:
        """Wire the on_file_complete hook so flush() fires it BEFORE
        on_job_end on the `ok` path (and skips it otherwise). None disables."""
        self._file_complete_hook = hook

    def _project_into_record(self) -> None:
        """Plant the threshold-stop projection numbers into
        `self.current["output"]` so they land in the JSONL audit row.

        Both raw byte counts AND % of source are emitted. The queue
        runner's adaptive-CRF-jump reader (since 1.15.0) consumes the
        pct values; downstream analysis / future tooling can use the
        byte counts. No-op when no projection was captured (the common
        non-threshold-stop case)."""
        if self._output_bytes_projected is None:
            return
        if not isinstance(self.current, dict):
            return
        out = self.current.setdefault("output", {})
        # Denominator: the encode_src size as recorded under input —
        # the SAME denominator the encoder's own size-projection guard
        # used to fire. Using a different denominator (e.g. the user's
        # original src) would make queue-side jump math disagree with
        # the encoder's own threshold logic.
        input_block = self.current.get("input") or {}
        source_bytes = input_block.get("size_bytes")
        out["bytes_projected"] = self._output_bytes_projected
        if self._output_bytes_threshold is not None:
            out["bytes_threshold"] = self._output_bytes_threshold
        if isinstance(source_bytes, (int, float)) and source_bytes > 0:
            out["projected_pct"] = round(
                self._output_bytes_projected / source_bytes * 100.0, 2)
            if self._output_bytes_threshold is not None:
                out["threshold_pct"] = round(
                    self._output_bytes_threshold / source_bytes * 100.0, 2)

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
            self._project_into_record()
            # v1.18.0: mirror BEFORE the record is appended so the rollup
            # lands in the JSONL row for EVERY terminal status (the spec's
            # headline post-mortem use case). Called here (not from finalize)
            # because abort paths reach flush via sys.exit + atexit and skip
            # finalize entirely.
            self._mirror_chunk_metrics()
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
        """Thin wrapper: delegate job-end hook firing to
        ``history_hooks.fire_job_end_hook``, passing the finalized record +
        the stop-context this recorder stashed. Behaviour is unchanged; the
        logic moved to ``history_hooks`` to keep this module under the cap."""
        if self.current is None:
            return
        _fire_job_end_hook_impl(
            self.current, self._job_end_hook,
            stop_reason_override=self._stop_reason,
            stop_detail_override=self._stop_detail,
            crf_retry_chain=self._crf_retry_chain,
            output_bytes_projected=self._output_bytes_projected,
            output_bytes_threshold=self._output_bytes_threshold,
        )

    def _fire_file_complete_hook(self) -> None:
        """Thin wrapper: delegate file-complete hook firing to
        ``history_hooks.fire_file_complete_hook``. Success-only by contract
        (the impl + the hook both filter status != "ok")."""
        if self.current is None:
            return
        _fire_file_complete_hook_impl(
            self.current, self._file_complete_hook,
            crf_retry_chain=self._crf_retry_chain,
        )


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

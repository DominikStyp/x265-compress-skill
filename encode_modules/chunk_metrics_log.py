"""Per-chunk + per-file encode metrics log (v1.18.0).

v1.17.0 had encode-elapsed + chunk-duration + output-size + per-chunk VMAF
in hand but persisted none of it — the per-chunk signal lived only on the
live terminal and evaporated on any unattended queue run. This module
captures it to a JSONL the queue runner folds into the per-file summary
AND the encoding_history.jsonl record.

On-disk: ``<dst.parent>/.tmp/<dst.stem>.chunk_metrics.jsonl`` — one
self-contained JSON line per chunk event. Two events per chunk:
  1. WORKER BASE ROW from chunk_worker / encode_serial / chunk_recovery
     after the ``.part`` -> ``.mkv`` rename — carries chunk_name,
     encode_elapsed_s, chunk_duration_s, output_bytes, derived
     output_bitrate_kbps, and static file context (w/h/fps/crf/preset).
  2. GUARD UPDATE ROW from ``QualityGuard`` at each decision
     (``ok`` / ``warmup-grace`` / ``abort`` / ``infra-fail``) — same
     chunk_name, vmaf_mean + decision filled in.

Append-only + last-wins-per-chunk_name. The aggregator reads line by line,
skips any unparseable line (torn-line tolerance for kill-safety), and keeps
the latest row per chunk_name. Out-of-order safe (workers + guard run on
different threads); resume-after-kill safe (re-emitted rows simply overwrite
the previous run's). Smallest possible critical section AND survives a hard
kill mid-encode with at most one torn line at EOF.

Summary lands in TWO stores (per spec — different consumers):
  * ``<dst.stem>.quality.json`` gets a top-level ``encode`` block alongside
    libvmaf's existing ``vmaf_mean``/``psnr``/``ssim``. Folded by
    ``reporting.measure_quality_and_write_sidecar``.
  * ``encoding_history.jsonl`` row gets the same block under
    ``chunk_metrics_summary``. Folded by ``history_state.flush()`` —
    runs for EVERY terminal status (including aborts; the post-mortem
    use case the spec was written for).

Toggle: ``--no-log-chunk-metrics`` CLI (opt-out, default ON) /
``"log_chunk_metrics": false`` queue key. Independent of
``visual_quality_threshold`` — time/size/bitrate log even with the guard
off. When disabled the log is a complete no-op; ``aggregate_summary``
returns the empty shape so consumers don't branch.

Concurrency: N worker threads call ``record_chunk``; the single guard
thread calls ``update_chunk_quality``. ``_lock`` serializes the cache
update + the file append so each JSONL line is atomic.

Singleton + shims mirror ``history_state.py`` (worker threads call
``record_chunk_metrics`` 3 frames deep — threading a recorder parameter
through every site would noise the call sites without value). Tests
construct ``ChunkMetricsLog`` directly to bypass the singleton.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .chunk_metrics_helpers import (
    compute_size_percent,
    fold_rows_into_encode_block as _fold_rows_into_encode_block,
    workdir_has_no_enc_chunks as _workdir_has_no_enc_chunks,
)

# Re-export so existing callers (history_state, reporting) keep importing
# ``compute_size_percent`` from this module without a churn-only edit.
__all__ = ["ChunkMetricsLog", "compute_size_percent",
           "init_chunk_metrics_log", "record_chunk_metrics",
           "update_chunk_quality", "aggregate_summary", "get_log",
           "DECISION_OK", "DECISION_WARMUP_GRACE",
           "DECISION_ABORT", "DECISION_INFRA_FAIL"]


# Decision strings the guard may emit. Documented here so consumers (queue
# runner aggregation, future stats tooling) have a stable contract.
DECISION_OK = "ok"
DECISION_WARMUP_GRACE = "warmup-grace"
DECISION_ABORT = "abort"
DECISION_INFRA_FAIL = "infra-fail"


class ChunkMetricsLog:
    """Append-only JSONL sink with an in-memory cache for the aggregate fold.

    Constructed once per encode. Workers call ``record_chunk`` after every
    chunk's ``.part`` -> ``.mkv`` rename; the quality guard calls
    ``update_chunk_quality`` at each decision. ``aggregate_summary`` reads
    the file back and folds last-wins-per-chunk_name into the per-file
    ``encode`` block.

    Disabled mode: when ``enabled=False`` every method is a cheap no-op so
    the encoder can build the log unconditionally without an ``if`` at
    every call site.
    """

    def __init__(self, jsonl_path: Path, *,
                 enabled: bool,
                 position_of: dict[str, int] | None,
                 width: int | None, height: int | None,
                 fps: float | None,
                 crf: int | None, preset: str | None,
                 quality_threshold: float | None,
                 workdir: Path | None = None) -> None:
        self._jsonl_path = jsonl_path
        self._enabled = bool(enabled)
        # chunk_name -> 1-based position in temporal order. chunk_idx in the
        # JSONL row is 0-based (position - 1). Unknown names yield ``None``
        # so downstream analytics can distinguish "no idx known" from a real
        # index (using a sentinel like -1 would be silently lossy).
        self._position_of = dict(position_of or {})
        self._width = width
        self._height = height
        self._fps = fps
        self._crf = crf
        self._preset = preset
        self._quality_threshold = quality_threshold
        self._lock = threading.Lock()
        # In-memory cache so update_chunk_quality knows the chunk's existing
        # fields without reading the file back. Defensive only — currently
        # only matters if the guard ever races ahead of the worker's base-row
        # write; the production sequence (rename -> record -> submit-guard)
        # guarantees the base row lands first, but the cache lets the merged
        # row carry real numbers even if that order ever breaks.
        self._cache: dict[str, dict] = {}
        # Truncate the JSONL on a FRESH encode (workdir contains no
        # enc_*.mkv chunks = phase 1 about to run). Without this the file
        # grows unbounded across re-encode attempts of the same source
        # (CRF sweeps, retry after manual delete) and external analytics
        # see the union of all runs interleaved.
        if self._enabled and self._jsonl_path.exists():
            if _workdir_has_no_enc_chunks(workdir):
                try:
                    self._jsonl_path.unlink()
                except OSError as e:
                    print(f"WARNING: chunk_metrics_log truncate failed: "
                          f"{e}", file=sys.stderr)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def jsonl_path(self) -> Path:
        return self._jsonl_path

    # ------------------------------------------------------------------
    # Worker-side ingestion
    # ------------------------------------------------------------------

    def record_chunk(self, *,
                     chunk_name: str,
                     encode_elapsed_s: float,
                     chunk_duration_s: float,
                     output_bytes: int) -> None:
        """Emit the worker-side base row for one finalized chunk. Idempotent
        per chunk_name only in the sense that subsequent calls APPEND
        additional rows that will overwrite earlier ones during aggregation —
        the encoder doesn't call this twice for the same chunk in normal
        operation, but a resume-after-kill might re-encode a chunk and that's
        fine: the second row wins."""
        if not self._enabled:
            return
        row = self._build_base_row(
            chunk_name=chunk_name,
            encode_elapsed_s=encode_elapsed_s,
            chunk_duration_s=chunk_duration_s,
            output_bytes=output_bytes,
        )
        self._append(row)

    def update_chunk_quality(self, *,
                             chunk_name: str,
                             vmaf_mean: Optional[float],
                             decision: str) -> None:
        """Merge the quality guard's verdict for a chunk. Appends a new row
        that overrides the base row's null vmaf/decision when the aggregator
        folds.

        ``vmaf_mean=None`` is valid (and expected) for ``decision=infra-fail``:
        libvmaf returned nothing, but the chunk should still be visible in
        the log with the decision set so an analyst can see the guard fired.
        """
        if not self._enabled:
            return
        # Start from the cached base row (so static context width/height etc.
        # rides along) if we have one; otherwise build a placeholder.
        with self._lock:
            base = dict(self._cache.get(chunk_name, {}))
        if not base:
            base = self._build_base_row(
                chunk_name=chunk_name,
                encode_elapsed_s=0.0,
                chunk_duration_s=0.0,
                output_bytes=0,
            )
            # Mark as a stub so the aggregate fold knows the worker never
            # produced a base row. (Doesn't affect correctness today, but
            # leaves a forensic breadcrumb for future analysis.)
            base["_stub"] = True
        # _stub bookkeeping flag never leaks into the on-disk row.
        base.pop("_stub", None)
        base["ts"] = time.time()
        base["vmaf_mean"] = (None if vmaf_mean is None else float(vmaf_mean))
        base["decision"] = decision
        self._append(base)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def aggregate_summary(self, *,
                          source_codec: Optional[str],
                          source_bytes: Optional[int],
                          output_bytes: Optional[int],
                          duration_s: Optional[float],
                          size_percent: Optional[float],
                          quality_aborted: bool,
                          quality_aborted_chunk: Optional[str]) -> dict:
        """Read the JSONL back and fold per-chunk rows into the per-file
        ``encode`` summary block.

        Last-write-wins per ``chunk_name``: the aggregator iterates rows in
        file order and keeps the last one seen for each name. This is what
        merges the worker's base row with the guard's update row.

        Torn-line tolerance: any line that fails ``json.loads`` is skipped.
        A kill mid-write can produce at most one such line at EOF.

        Returns a dict with a single key ``encode`` so callers can ``.update()``
        it straight into ``.quality.json`` without nesting decisions.
        """
        encode = _fold_rows_into_encode_block(
            self._read_rows(),
            width=self._width, height=self._height, fps=self._fps,
            crf=self._crf, preset=self._preset,
            quality_threshold=self._quality_threshold,
            source_codec=source_codec, source_bytes=source_bytes,
            output_bytes=output_bytes, duration_s=duration_s,
            size_percent=size_percent,
            quality_aborted=quality_aborted,
            quality_aborted_chunk=quality_aborted_chunk,
        )
        return {"encode": encode}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_base_row(self, *, chunk_name: str,
                        encode_elapsed_s: float,
                        chunk_duration_s: float,
                        output_bytes: int) -> dict:
        """Construct the canonical per-chunk record. Mutated by
        ``update_chunk_quality`` (vmaf/decision) before re-emission."""
        # Derived bitrate. Guard against zero / negative durations — a 0-byte
        # chunk file from a corrupted source or a degenerate probe must not
        # crash the recorder with ZeroDivisionError.
        bitrate_kbps: Optional[float]
        if chunk_duration_s and chunk_duration_s > 0 and output_bytes > 0:
            bitrate_kbps = output_bytes * 8 / chunk_duration_s / 1000
        else:
            bitrate_kbps = None

        # Position is 1-based in the encoder's mapping (it's the ordinal the
        # user sees as "chunk 5/10"); chunk_idx is 0-based per the spec.
        # Unknown names yield None (not -1) so consumers can tell "missing"
        # from a real index.
        position = self._position_of.get(chunk_name)
        chunk_idx: Optional[int] = ((position - 1) if isinstance(position, int)
                                    else None)

        return {
            "ts": time.time(),
            "chunk_idx": chunk_idx,
            "chunk_name": chunk_name,
            "encode_elapsed_s": float(encode_elapsed_s),
            "chunk_duration_s": float(chunk_duration_s),
            "output_bytes": int(output_bytes),
            "output_bitrate_kbps": bitrate_kbps,
            "width": self._width,
            "height": self._height,
            "fps": self._fps,
            "crf": self._crf,
            "preset": self._preset,
            "vmaf_mean": None,
            "threshold": self._quality_threshold,
            "decision": None,
        }

    def _append(self, row: dict) -> None:
        """Lock + serialize + write one JSONL line + update cache. The lock
        is held across the json.dumps to keep the critical section tight."""
        with self._lock:
            self._cache[row["chunk_name"]] = dict(row)
            try:
                self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                # Append mode + per-line flush. Each write is one
                # ``json.dumps(row) + "\n"`` string — small enough that POSIX
                # write(2) on append-only mode is atomic w.r.t. concurrent
                # writers in the same process, and Windows append-mode is
                # documented to seek-to-end + write atomically per call.
                line = json.dumps(row, ensure_ascii=False) + "\n"
                with self._jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                # The metrics log is auxiliary — never let a disk-full / read-
                # only filesystem abort an encode. Print to stderr so the
                # user notices without crashing.
                print(f"WARNING: chunk_metrics_log append failed: {e}",
                      file=sys.stderr)

    def _read_rows(self) -> dict[str, dict]:
        """Return a chunk_name -> latest-row dict, skipping torn / unparseable
        lines. Empty dict if the file doesn't exist (disabled, or aggregate
        called before any chunk finalized)."""
        out: dict[str, dict] = {}
        if not self._jsonl_path.exists():
            return out
        try:
            text = self._jsonl_path.read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Torn line (kill mid-write) — skip. Spec requires this
                # tolerance so a hard-killed encoder leaves a partly-readable
                # log instead of a useless one.
                continue
            name = row.get("chunk_name")
            if not isinstance(name, str) or not name:
                continue
            out[name] = row
        return out


def build_summary_for_history_record(rec: dict) -> Optional[dict]:
    """Read the per-chunk JSONL + fold into the ``encode`` summary block,
    extracting the input/output/status fields from a HistoryRecorder
    record. Returns the inner ``encode`` dict ready to store under
    ``chunk_metrics_summary`` on the record, or None when no log is
    initialized or no chunks were recorded yet.

    Lives here (not in history_state) so all chunk_metrics-related logic
    sits in one module — history_state only needs to call this and store
    the result. Best-effort: any exception is caught at the call site so
    a JSONL parse / aggregation glitch never crashes history flush.

    The ``quality_aborted`` flag is derived from the record's status
    (``"stopped-quality-threshold"`` set by ``mark_status`` on the
    QualityGuard abort path); ``quality_aborted_chunk`` is pulled from
    the structured extras the same mark_status call stashed."""
    if _log is None:
        return None
    input_block = rec.get("input") or {}
    output_block = rec.get("output") or {}
    src_size = input_block.get("size_bytes")
    out_size = output_block.get("size_bytes")
    status = rec.get("status", "")
    quality_aborted = (status == "stopped-quality-threshold")
    quality_aborted_chunk = (rec.get("chunk_name") if quality_aborted
                             else None)
    summary = aggregate_summary(
        source_codec=input_block.get("codec"),
        source_bytes=src_size,
        output_bytes=out_size,
        duration_s=input_block.get("duration_s"),
        size_percent=compute_size_percent(src_size, out_size),
        quality_aborted=quality_aborted,
        quality_aborted_chunk=quality_aborted_chunk,
    )
    inner = summary.get("encode")
    # Init-but-no-work paths emit n_chunks=0 — skip so the JSONL record
    # stays byte-identical to pre-v1.18.0 in those cases.
    if not inner or inner.get("n_chunks", 0) == 0:
        return None
    return inner


# ----- Singleton + module-level shims ---------------------------------------
# Mirrors the history_state pattern so worker thread call sites can stay
# terse (one function call instead of threading a recorder parameter through
# four call frames).

_log: Optional[ChunkMetricsLog] = None


def init_chunk_metrics_log(jsonl_path: Path, *,
                           enabled: bool,
                           position_of: dict[str, int] | None,
                           width: int | None, height: int | None,
                           fps: float | None,
                           crf: int | None, preset: str | None,
                           quality_threshold: float | None,
                           workdir: Path | None = None) -> ChunkMetricsLog:
    """Replace the singleton. ``workdir`` is the per-source ``.compress_*``
    directory; if it contains no ``enc_*.mkv`` chunks at init, the JSONL
    is truncated so a fresh encode of the same source doesn't accumulate
    rows from prior attempts. Pass None to disable truncate (safer when
    the caller can't be sure)."""
    global _log
    _log = ChunkMetricsLog(
        jsonl_path,
        enabled=enabled,
        position_of=position_of,
        width=width, height=height, fps=fps,
        crf=crf, preset=preset,
        quality_threshold=quality_threshold,
        workdir=workdir,
    )
    return _log


def record_chunk_metrics(*,
                         chunk_name: str,
                         encode_elapsed_s: float,
                         chunk_duration_s: float,
                         output_bytes: int) -> None:
    """Shim: call from chunk_worker / encode_serial after the rename. No-op
    when ``init_chunk_metrics_log`` was never called (compress.py single-file
    runs without the feature wired, smoke tests)."""
    if _log is None:
        return
    _log.record_chunk(
        chunk_name=chunk_name,
        encode_elapsed_s=encode_elapsed_s,
        chunk_duration_s=chunk_duration_s,
        output_bytes=output_bytes,
    )


def update_chunk_quality(*,
                         chunk_name: str,
                         vmaf_mean: Optional[float],
                         decision: str) -> None:
    """Shim: call from the QualityGuard. No-op when no log is registered."""
    if _log is None:
        return
    _log.update_chunk_quality(
        chunk_name=chunk_name, vmaf_mean=vmaf_mean, decision=decision,
    )


def aggregate_summary(*,
                      source_codec: Optional[str],
                      source_bytes: Optional[int],
                      output_bytes: Optional[int],
                      duration_s: Optional[float],
                      size_percent: Optional[float],
                      quality_aborted: bool,
                      quality_aborted_chunk: Optional[str]) -> dict:
    """Shim: read JSONL + fold to the per-file ``encode`` summary block.
    Explicit keyword list so a typo at the call site fails here, not deep
    inside ``ChunkMetricsLog.aggregate_summary``. Returns the empty shape
    (n_chunks=0) when no log is registered so consumers can ``.update``
    without an ``if`` branch."""
    if _log is None:
        return {"encode": {
            "n_chunks": 0,
            "elapsed_s": {"mean": None, "min": None, "max": None, "total": None},
            "output_bitrate_kbps": {"overall": None, "chunk_mean": None,
                                    "chunk_min": None, "chunk_max": None},
            "vmaf_chunk": {"count": 0, "mean": None, "min": None, "max": None},
        }}
    return _log.aggregate_summary(
        source_codec=source_codec,
        source_bytes=source_bytes,
        output_bytes=output_bytes,
        duration_s=duration_s,
        size_percent=size_percent,
        quality_aborted=quality_aborted,
        quality_aborted_chunk=quality_aborted_chunk,
    )


def get_log() -> Optional[ChunkMetricsLog]:
    """Read access to the singleton (used by encode_parallel to bind the
    QualityGuard's metrics_update_fn)."""
    return _log


def _reset_for_tests() -> None:
    """Drop the singleton. Production code never resets mid-encode."""
    global _log
    _log = None

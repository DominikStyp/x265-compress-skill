"""Process-wide singleton + module-level shims for the chunk metrics log.

Split out of ``chunk_metrics_log.py`` (which now owns only the
``ChunkMetricsLog`` class: per-chunk record/emission + per-file
``aggregate_summary`` rollup) so that module stays under the 500-line cap.

This module holds the integration surface the rest of the encoder talks to:

  * ``build_summary_for_history_record`` — read the per-chunk JSONL + fold
    into the ``encode`` block, extracting input/output/status from a
    HistoryRecorder record (called by ``history_state.flush()``).
  * the singleton ``_log`` + its shims (``init_chunk_metrics_log``,
    ``record_chunk_metrics``, ``update_chunk_quality``, ``aggregate_summary``,
    ``get_log``) so worker call sites 3+ frames deep stay terse — one
    function call instead of threading a recorder through every frame.

Mirrors the ``history_state.py`` singleton pattern. Tests construct
``ChunkMetricsLog`` directly to bypass the singleton; production wires it
once per encode via ``init_chunk_metrics_log``.

The names are re-exported from ``chunk_metrics_log`` so existing importers
(``from .chunk_metrics_log import record_chunk_metrics`` and friends in
chunk_worker / chunk_recovery / encode_serial / encode_parallel /
encode_resumable / reporting / history_state) keep working unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .chunk_metrics_helpers import compute_size_percent

if TYPE_CHECKING:  # type checkers / IDEs only — no runtime import cycle
    from .chunk_metrics_log import ChunkMetricsLog

# NB: ``ChunkMetricsLog`` is NOT imported at module load. ``chunk_metrics_log``
# re-exports this module's shims at the BOTTOM of its own body, so eagerly
# importing from it here would deadlock the import cycle whenever this module
# is the entry point (its names wouldn't be bound yet). ``init_chunk_metrics_log``
# resolves the class lazily at call time via ``_chunk_metrics_log_module()``;
# by then both modules are fully loaded.


def _chunk_metrics_log_module():
    """Lazy accessor for the sibling module that owns ``ChunkMetricsLog``.
    Imported on first use (always after both modules finished loading) to
    keep the chunk_metrics_log <-> chunk_metrics_singleton re-export cycle
    from failing at import time."""
    from . import chunk_metrics_log
    return chunk_metrics_log


def build_summary_for_history_record(rec: dict) -> Optional[dict]:
    """Read the per-chunk JSONL + fold into the ``encode`` summary block,
    extracting the input/output/status fields from a HistoryRecorder
    record. Returns the inner ``encode`` dict ready to store under
    ``chunk_metrics_summary`` on the record, or None when no log is
    initialized or no chunks were recorded yet.

    Lives here (with the singleton) so all chunk_metrics-related glue sits
    in one module — history_state only needs to call this and store the
    result. Best-effort: any exception is caught at the call site so a JSONL
    parse / aggregation glitch never crashes history flush.

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
    _log = _chunk_metrics_log_module().ChunkMetricsLog(
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

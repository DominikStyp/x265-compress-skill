"""Terminal-status hook firing for the history recorder.

Split out of ``history_state.py`` (which owns the in-memory record state +
JSONL flush) so the hook-emission cluster lives in one cohesive unit and the
recorder module stays under the 500-line cap.

These are free functions, not methods: each reads the already-finalized
history record dict (``rec``) plus the attached hook and the stop-context the
recorder stashed, builds the per-event context, and fires the hook. The
``HistoryRecorder`` methods ``_fire_job_end_hook`` / ``_fire_file_complete_hook``
are now thin wrappers that delegate here — behaviour is unchanged.

Both hooks fire from ``HistoryRecorder.flush()`` AFTER the JSONL audit row is
on disk. ``on_job_end`` fires for every terminal status (carries
reason/detail); ``on_file_complete`` fires success-only. Order: job_end
first, file_complete second (see flush()).
"""
from __future__ import annotations

import sys
from pathlib import Path


def stat_source_bytes(hook) -> int | None:
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


def derive_stop_fields(rec: dict, status: str, *,
                       stop_reason_override: str,
                       stop_detail_override: str) -> tuple[str, str]:
    """Build (stop_reason, stop_detail) from the record when the
    threshold-abort path didn't pre-populate them. `stop_reason` defaults
    to the JSONL status (so chunk-failed → reason='chunk-failed');
    `stop_detail` digs through the structured failure fields the encoder
    stashed under mark_status(..., **extras). Empty on the ok path so a
    notifier can detect "no problem" via stop_reason == ""."""
    if status == "ok":
        return "", ""
    reason = stop_reason_override or status
    if stop_detail_override:
        return reason, stop_detail_override
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


def fire_job_end_hook(rec: dict, hook, *,
                      stop_reason_override: str,
                      stop_detail_override: str,
                      crf_retry_chain: str,
                      output_bytes_projected: int | None,
                      output_bytes_threshold: int | None) -> None:
    """Build the per-job context from `rec` + stored stop fields and invoke
    the attached JobEndHook. No-op when no hook is attached.

    Derives stop_reason/stop_detail from the JSONL record itself when the
    threshold-abort path didn't pre-populate them (the common case for
    chunk-failed, verify-failed, awaiting-chunk-fix, stopped-by-user) — so
    every non-ok terminal status carries enough context for a notification
    script to dispatch on."""
    if hook is None or not hook.enabled or rec is None:
        return
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
    stop_reason, stop_detail = derive_stop_fields(
        rec, status,
        stop_reason_override=stop_reason_override,
        stop_detail_override=stop_detail_override,
    )
    msg = hook.fire(
        status=status,
        stop_reason=stop_reason,
        stop_detail=stop_detail,
        crf=settings.get("crf"),
        crf_retry_chain=crf_retry_chain or str(
            settings.get("crf") or ""),
        output=Path(str(output_path)) if output_path else None,
        output_bytes_final=out_bytes,
        # The user's original source is what the hook reports — NOT the
        # auto-patched encode_src that the JSONL `input.size_bytes` holds
        # (that field feeds the size-projection guard which needs the
        # patched-size denominator). Same invariant as X265_SOURCE.
        source_bytes=stat_source_bytes(hook),
        output_bytes_projected=output_bytes_projected,
        output_bytes_threshold=output_bytes_threshold,
        wall_seconds=rec.get("wall_seconds"),
        pct_saved=reduction.get("pct_saved"),
    )
    if msg:
        print(msg, file=sys.stderr)


def fire_file_complete_hook(rec: dict, hook, *,
                            crf_retry_chain: str) -> None:
    """Fire on_file_complete from `rec` + a fresh stat of the output.
    Success-only by contract — the hook itself filters status != "ok" or
    missing output, so this function just hands over the data."""
    if hook is None or not hook.enabled or rec is None:
        return
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
        source_bytes=stat_source_bytes(hook),
        wall_seconds=rec.get("wall_seconds"),
        pct_saved=reduction.get("pct_saved"),
        crf=settings.get("crf"),
        crf_retry_chain=crf_retry_chain or str(
            settings.get("crf") or ""),
        vmaf_mean=quality.get("vmaf_mean"),
    )
    if msg:
        print(msg, file=sys.stderr)

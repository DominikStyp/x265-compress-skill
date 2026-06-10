"""Per-file outputs that follow a successful encode: quality measurement
sidecar + the markdown report row.

Both are opt-out (queue runner passes `--no-report` and writes its own
aggregate; `--no-quality-check` skips the VMAF pass entirely). The work is
in `quality.py` and `report.py`; this module is the orchestration glue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .chunk_metrics_log import (
    aggregate_summary as _aggregate_chunk_metrics,
    compute_size_percent as _compute_size_percent,
    get_log as _get_chunk_metrics_log,
)
from .log_paths import per_encode_report_path, quality_sidecar_path
from .quality import (
    quality_check,
    quality_check_chunks,
    workdir_has_chunks,
)
from .quality_format import format_quality_summary


def measure_quality_and_write_sidecar(src: Path, dst: Path, workdir: Path, *,
                                     args: argparse.Namespace,
                                     n_chunks_total: int,
                                     total_duration_s: float | None = None
                                     ) -> dict | None:
    """Run the VMAF/PSNR/SSIM measurement and persist a sidecar JSON under
    `logs/<basename>.quality.json` so the queue runner can pull scores into
    its aggregate report. Pure measurement — does NOT trigger a re-encode
    on low scores (a low VMAF means CRF was too aggressive for the content,
    which is a subjective tuning decision, not a bug)."""
    if args.no_quality_check:
        return None

    # Resolve auto → chunks/full up front so the announcement matches what
    # we're actually about to run.
    mode_resolved = args.vmaf_mode
    if mode_resolved == "auto":
        mode_resolved = "chunks" if workdir_has_chunks(workdir) else "full"
    if mode_resolved == "chunks":
        print(f"\nMeasuring perceptual quality (per-chunk vs source "
              f"chunks, sampling {args.vmaf_chunks}/{n_chunks_total})...",
              flush=True)
    else:
        print("\nMeasuring perceptual quality vs source (libvmaf, "
              "full file)...", flush=True)

    q_start = time.monotonic()
    if mode_resolved == "chunks":
        quality_scores = quality_check_chunks(
            workdir,
            subsample=args.vmaf_subsample,
            n_chunks=args.vmaf_chunks,
            progress_prefix="  Quality check:",
        )
    else:
        quality_scores = quality_check(
            src, dst,
            subsample=args.vmaf_subsample,
            mode="full",
            progress_prefix="  Quality check:",
        )
    q_elapsed = time.monotonic() - q_start

    if not quality_scores:
        print(f"  (quality check failed after {q_elapsed:.0f}s — "
              f"libvmaf may be unavailable; encode still considered successful)")
        return None

    print(format_quality_summary(quality_scores))
    print(f"    (measured in {q_elapsed:.0f}s, subsample={args.vmaf_subsample})")
    # v1.18.0: fold the per-chunk encode metrics rollup into the same sidecar
    # so the queue runner reads both libvmaf scores AND time/size/bitrate
    # aggregates with one open. The chunk_metrics fold is best-effort: an
    # aggregation failure (corrupt JSONL, disk read error) must NEVER drop
    # the libvmaf scores that we DID measure.
    sidecar_payload = dict(quality_scores)
    _merge_chunk_metrics_into(sidecar_payload, src=src, dst=dst,
                              total_duration_s=total_duration_s)
    try:
        sidecar = quality_sidecar_path(dst)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: the queue runner parses this sidecar, so a kill
        # mid-write must never leave a truncated JSON at the final path
        # (atomic-writes invariant — same temp-then-replace as hook_config).
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        tmp.write_text(json.dumps(sidecar_payload, indent=2), encoding="utf-8")
        os.replace(tmp, sidecar)
    except Exception as e:
        print(f"  (warning: could not write quality sidecar: {e})")
    return sidecar_payload


def _merge_chunk_metrics_into(payload: dict, *, src: Path, dst: Path,
                              total_duration_s: float | None = None) -> None:
    """Fold the per-chunk metrics rollup into the in-progress sidecar payload
    under the ``encode`` key. Best-effort: an aggregation failure (corrupt
    JSONL, disk read error) is warned about and skipped — the libvmaf
    scores that were measured stay in the sidecar.

    Two no-op gates so this stays additive and never pollutes legacy paths:
      1. No log was initialized at all (single-pass compress.py, fixture-
         stubbed main() in tests).
      2. The log exists but no chunk rows were written (init-but-no-work
         e.g. a test that mocks the encode loop). Merging an empty
         summary would still inject the `encode` key, surprising consumers
         that exact-equality on the pre-v1.18.0 shape.
    """
    log = _get_chunk_metrics_log()
    if log is None:
        return
    try:
        try:
            src_bytes = src.stat().st_size if src.exists() else None
        except OSError:
            src_bytes = None
        try:
            out_bytes = dst.stat().st_size if dst.exists() else None
        except OSError:
            out_bytes = None
        # Duration source priority:
        # 1. Explicit total_duration_s threaded in from encode_resumable.main
        #    (the same `total_dur` the encoder used for chunking math).
        # 2. Fallback: payload's libvmaf-side fields (today neither
        #    quality_check nor quality_check_chunks populate these, but
        #    a future quality refactor might).
        # Without source #1 the `output_bitrate_kbps.overall` field in
        # the sidecar would always be null even on successful encodes —
        # caught by reviewer M2 in v1.18.0.
        duration_s: float | None = None
        if isinstance(total_duration_s, (int, float)) and total_duration_s > 0:
            duration_s = float(total_duration_s)
        else:
            for key in ("source_duration_s", "duration_s"):
                v = payload.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    duration_s = float(v)
                    break
        size_percent = _compute_size_percent(src_bytes, out_bytes)
        summary = _aggregate_chunk_metrics(
            source_codec=None,
            source_bytes=src_bytes,
            output_bytes=out_bytes,
            duration_s=duration_s,
            size_percent=size_percent,
            quality_aborted=False,
            quality_aborted_chunk=None,
        )
        # Skip merge when no chunks were actually logged — keeps the sidecar
        # byte-identical to pre-v1.18.0 in init-but-no-encode test paths.
        if summary.get("encode", {}).get("n_chunks", 0) > 0:
            payload.update(summary)
    except Exception as e:
        print(f"  (warning: chunk_metrics aggregation failed: {e})")


def write_single_file_report(src: Path, dst: Path, *,
                            args: argparse.Namespace,
                            source_bytes: int,
                            elapsed_s: float,
                            quality_scores: dict | None) -> None:
    """Write the per-file markdown report under `logs/<basename>.report.md`.
    Skipped via `--no-report` (the queue runner sets that because it writes
    its own aggregate). report.py is imported lazily so this module is
    usable standalone if report.py is absent."""
    try:
        import report  # noqa: WPS433
        md_path = per_encode_report_path(dst)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "input": str(src),
            "output": str(dst),
            # Use the caller's pre-cleanup byte count, never a fresh stat():
            # on an auto-patched run `src` is the original but the size the
            # encode gated against is `source_bytes` (the patched copy). Re-
            # statting would (a) disagree with `max_size_percent`'s denominator
            # below and (b) risk touching disk after workdir cleanup.
            "input_bytes": source_bytes,
            "output_bytes": dst.stat().st_size if dst.exists() else None,
            "crf": args.crf,
            "preset": args.preset,
            "parallel": args.parallel,
            "segments": None,  # we use --segment-seconds internally
            "max_size_percent": (
                round(args.max_output_bytes / source_bytes * 100, 1)
                if args.max_output_bytes and source_bytes else None
            ),
            "elapsed_seconds": elapsed_s,
            "status": "ok",
        }
        if quality_scores:
            row["vmaf_mean"] = quality_scores.get("vmaf_mean")
            row["vmaf_min"] = quality_scores.get("vmaf_min")
            row["psnr_y_mean"] = quality_scores.get("psnr_y_mean")
            row["ssim_mean"] = quality_scores.get("ssim_mean")
            row["quality_method"] = quality_scores.get("method")
        report.write_report(
            md_path, [row],
            title=f"Encoding Report: {src.name}",
        )
        print(f"Report: {md_path}")
    except Exception as e:
        print(f"WARNING: failed to write report: {e}", file=sys.stderr)


def print_summary(src: Path, dst: Path,
                  quality_scores: dict | None = None) -> None:
    """One-shot summary block at the end of a successful encode. Prints
    input MB, output MB, savings — nothing more (the markdown report and
    history JSONL carry the full detail).

    When a transparent VMAF (>=95) is available, also reassures the user the
    source is untouched and that deleting it by hand is safe. The tool NEVER
    deletes the source itself — only the user does."""
    in_mb = src.stat().st_size / (1024 * 1024)
    out_mb = dst.stat().st_size / (1024 * 1024)
    saved_pct = (in_mb - out_mb) / in_mb * 100 if in_mb else 0
    print()
    print("=== Done ===")
    print(f"    Input :  {in_mb:8.1f} MB")
    print(f"    Output:  {out_mb:8.1f} MB")
    print(f"    Saved :  {saved_pct:8.1f} %  ({in_mb - out_mb:.1f} MB)")
    vmaf = (quality_scores or {}).get("vmaf_mean")
    if vmaf is not None and vmaf >= 95:
        print(f"    Source untouched at: {src}")
        print(f"    VMAF {vmaf:.1f} (visually transparent) — safe to delete "
              f"the original yourself.")

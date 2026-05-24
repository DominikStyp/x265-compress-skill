"""Per-file outputs that follow a successful encode: quality measurement
sidecar + the markdown report row.

Both are opt-out (queue runner passes `--no-report` and writes its own
aggregate; `--no-quality-check` skips the VMAF pass entirely). The work is
in `quality.py` and `report.py`; this module is the orchestration glue.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .quality import (
    quality_check,
    quality_check_chunks,
    workdir_has_chunks,
)
from .quality_format import format_quality_summary


def measure_quality_and_write_sidecar(src: Path, dst: Path, workdir: Path, *,
                                     args: argparse.Namespace,
                                     n_chunks_total: int) -> dict | None:
    """Run the VMAF/PSNR/SSIM measurement and persist a sidecar JSON under
    `.tmp/<basename>.quality.json` so the queue runner can pull scores into
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
    try:
        sidecar_dir = dst.parent / ".tmp"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sidecar_dir / f"{dst.stem}.quality.json"
        sidecar.write_text(json.dumps(quality_scores, indent=2),
                          encoding="utf-8")
    except Exception as e:
        print(f"  (warning: could not write quality sidecar: {e})")
    return quality_scores


def write_single_file_report(src: Path, dst: Path, *,
                            args: argparse.Namespace,
                            source_bytes: int,
                            elapsed_s: float,
                            quality_scores: dict | None) -> None:
    """Write the per-file markdown report under `.tmp/<basename>.report.md`.
    Skipped via `--no-report` (the queue runner sets that because it writes
    its own aggregate). report.py is imported lazily so this module is
    usable standalone if report.py is absent."""
    try:
        import report  # noqa: WPS433
        tmp_dir = dst.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        md_path = tmp_dir / f"{dst.stem}.report.md"
        row = {
            "input": str(src),
            "output": str(dst),
            "input_bytes": src.stat().st_size,
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

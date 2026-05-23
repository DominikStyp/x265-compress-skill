"""Pre-flight scan + optional auto-patch decision tree.

Extracted from `encode_resumable.main()` so the body of main() stays as a
clean recipe. The function `handle_preflight` is the single entry point: it
takes a source path, runs the pre-flight scan, and returns one of:

  ("ok",      src,     scan, None)        scan passed, encode the source.
  ("patched", patched, scan, rescan)      auto-patch produced a clean file;
                                          encode that instead.
  ("failed",  src,     scan, rescan|None) scan failed (and patch declined or
                                          itself produced a non-clean file).
                                          Caller exits 6.

All four return values are the same shape so the caller can dispatch with a
single match.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from .pre_flight import format_pre_flight_summary, pre_flight_scan
from .source_guard import protect_source
from .source_patcher import auto_patch_source


def handle_preflight(src: Path, workdir: Path, args: argparse.Namespace
                    ) -> tuple[str, Path, dict, Optional[dict]]:
    """Run pre-flight + optional auto-patch. Returns (status, src, scan, rescan).

    Side effects: prints scan summary and patch progress to stdout;
    re-protects the new source path via `protect_source` if a patched
    file is adopted."""
    if args.no_pre_flight_scan:
        # Synthesize a "passed" scan dict so downstream history records
        # see a consistent shape even when the scan was skipped.
        return "ok", src, {"passed": True, "skipped": True}, None

    print("[0/4] Pre-flight scan: walking source for decode errors...")
    scan = pre_flight_scan(src, seg_sec=args.segment_seconds)
    print(format_pre_flight_summary(scan))

    if scan["passed"]:
        return "ok", src, scan, None

    if not args.auto_patch_source:
        return "failed", src, scan, None

    return _try_auto_patch(src, workdir, scan, args)


def _try_auto_patch(src: Path, workdir: Path, scan: dict,
                   args: argparse.Namespace
                   ) -> tuple[str, Path, dict, Optional[dict]]:
    """Attempt the surgical patch recipe + re-run pre-flight on the result.
    Encapsulated so handle_preflight stays a clean dispatch."""
    print(f"  > --auto-patch-source: attempting surgical patch "
          f"(loss budget: {args.max_patch_seconds:.0f}s)")
    workdir.mkdir(parents=True, exist_ok=True)
    patched = auto_patch_source(
        src, scan, workdir,
        max_patch_seconds=args.max_patch_seconds,
    )
    if patched is None:
        print("  ! auto-patch declined (codec not h264, loss budget "
              "exceeded, or build step failed); falling back to "
              "pre-flight-failed")
        return "failed", src, scan, None

    # Re-pre-flight the patched file. Patched file has its own
    # .preflight.json sidecar so this doesn't pollute the original's cache.
    print("  > re-running pre-flight on patched source...")
    rescan = pre_flight_scan(patched, seg_sec=args.segment_seconds,
                             use_cache=False)
    print(format_pre_flight_summary(rescan))

    if not rescan["passed"]:
        print("  ! auto-patch produced a file that still fails pre-flight; "
              "falling back to pre-flight-failed")
        return "failed", src, scan, rescan

    print(f"  + auto-patch SUCCEEDED — encoding will proceed against "
          f"{patched.name}")
    protect_source(patched)
    return "patched", patched, scan, rescan

"""
Resumable x265 encode: split → encode-per-chunk → concat.

The single-pass `.bat` cannot survive a reboot — x265 keeps lookahead,
reference frames, and rate-control state in RAM with no checkpoint to disk.
This wrapper sidesteps that by working in chunks:

  Phase 0  Pre-flight scan: walk source for bitstream errors. Optional
           --auto-patch-source path applies a surgical h264 GOP rebuild on
           broken sources and re-runs the scan on the result.
  Phase 1  Lossless segment (-c copy) of source into chunks, keyframe-aligned.
  Phase 2  Encode each chunk to x265 individually. Existing enc_*.mkv files
           are skipped — re-runs resume from the first missing chunk.
  Phase 3  Lossless concat of encoded chunks into final output, then verify.
  Phase 4  Quality measurement (VMAF/PSNR/SSIM) and workdir cleanup.

Killed during Phase 2? Re-run this script (or the calling .bat) — it picks
up at the next unencoded chunk. Survives reboots because all state is on disk.

The orchestration is intentionally thin — every meaningful step lives in
its own module under `encode_modules/`. main() reads as a recipe.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from encode_modules.chunking import cleanup, reorder_middle_first, split_source
from encode_modules.cli_args import parse_args
from encode_modules.finish_signal import FINISH_FILENAME
from encode_modules.history_state import (
    finalize_history_state,
    init_history_state,
    mark_status,
)
from encode_modules.preflight_decision import handle_preflight
from encode_modules.probes import probe_duration
from platform_compat import enable_ansi, enable_utf8_io
from encode_modules.reporting import (
    measure_quality_and_write_sidecar,
    print_summary,
    write_single_file_report,
)
from encode_modules.source_guard import ensure_not_source, protect_source
from encode_modules.verify_loop import (
    handle_verify_failure_block,
    run_encode_verify_loop,
)


# Force UTF-8 stdout/stderr FIRST (before any output) so the display's
# → / — / box-drawing glyphs survive a non-UTF-8 locale + redirected output
# (headless / queue logs). Then enable ANSI VT processing (Win32) for the
# in-place render; POSIX no-op.
enable_utf8_io()
enable_ansi()

MAX_VERIFY_ATTEMPTS = 3


def _validate_paths(src: Path, dst: Path, workdir: Path) -> None:
    """Catch the obviously-destructive misconfigurations before any work
    starts. Refusing to encode in-place or to a workdir that equals the
    source's parent dir is defense-in-depth against the source_guard
    catching it later (better to fail at setup than mid-encode)."""
    if not src.is_file():
        sys.exit(f"ERROR: input not found: {src}")
    if dst == src:
        sys.exit(f"ERROR: output path equals source path: {src}. Refusing "
                 "to encode in-place — would destroy the source.")
    if workdir == src.parent:
        sys.exit(f"ERROR: workdir equals source's parent directory: {src.parent}. "
                 "Workdir cleanup would risk the source. Use a different workdir.")


def _exit_pre_flight_failed(src: Path, dst: Path, args,
                            scan: dict, rescan: dict | None,
                            patched_attempted: bool) -> None:
    """Record the pre-flight failure in history and exit code 6. Separate
    branches for "patch was tried but failed" vs "patch was declined" so
    downstream tooling can distinguish the two."""
    init_history_state(src, dst, args, source_bytes=src.stat().st_size)
    extras: dict = {"pre_flight_scan": scan}
    if patched_attempted:
        extras["auto_patch_attempted"] = True
        if rescan is not None:
            extras["auto_patch_post_scan"] = rescan
        else:
            extras["auto_patch_declined"] = True
    mark_status("pre-flight-failed", **extras)
    sys.exit(6)


def main() -> int:
    """Top-level pipeline. Each phase is its own helper so this body reads
    as a recipe, not 250 lines of argparse + control flow.

    Side effect: appends one record to encoding_history.jsonl with input
    metadata, settings, per-chunk timings, output, and quality scores.
    Threshold-aborts and crashes flush partial records via atexit."""
    args = parse_args()

    src = Path(args.input).resolve()
    dst = Path(args.output).resolve()
    workdir = Path(args.workdir).resolve()

    _validate_paths(src, dst, workdir)

    # Register the source as off-limits to any rename/unlink routed
    # through ensure_not_source. Defense-in-depth — no current code path
    # targets it, but a future refactor accidentally aimed at it would
    # raise immediately rather than silently destroy the user's original.
    protect_source(src)

    if dst.exists():
        # Re-run after final success — exit cleanly. The caller can
        # delete the output if they want a fresh encode.
        print(f"Output already exists: {dst}")
        print("(Delete it manually if you want to re-encode from scratch.)")
        return 0

    run_start = time.monotonic()

    # Pre-flight + optional auto-patch. Bail before any chunking work if
    # the source is unsafe to encode.
    status, src, scan, rescan = handle_preflight(src, workdir, args)
    if status == "failed":
        _exit_pre_flight_failed(src, dst, args, scan, rescan,
                                patched_attempted=args.auto_patch_source)

    chunks = split_source(src, workdir, args.segment_seconds)
    print(f"      To stop after the current chunk (resumable): press 'f' in "
          f"the live display, or create {workdir / FINISH_FILENAME}")
    total_dur = (args.total_duration_seconds
                 if args.total_duration_seconds is not None
                 else sum(probe_duration(c) for c in chunks))
    source_bytes = (args.source_bytes if args.source_bytes is not None
                    else src.stat().st_size)

    init_history_state(src, dst, args, source_bytes)

    problems = run_encode_verify_loop(
        src, chunks, workdir, dst,
        args=args, total_dur=total_dur, source_bytes=source_bytes,
        max_attempts=MAX_VERIFY_ATTEMPTS,
    )
    if problems:
        mark_status("verify-failed", verify_problems=problems)
        handle_verify_failure_block(problems, workdir, dst, MAX_VERIFY_ATTEMPTS)
        # handle_verify_failure_block sys.exits(4); next line unreachable.

    quality_scores = measure_quality_and_write_sidecar(
        src, dst, workdir, args=args, n_chunks_total=len(chunks),
    )

    elapsed = time.monotonic() - run_start
    # Flush history BEFORE cleanup wipes the workdir — finalize_history_state
    # probes each chunk's duration and reads file sizes off disk.
    finalize_history_state(
        src, dst, workdir, chunks, elapsed, quality_scores,
        encode_order=reorder_middle_first(chunks),
    )

    ensure_not_source(workdir)
    cleanup(workdir)
    print_summary(src, dst, quality_scores)

    if not args.no_report:
        write_single_file_report(
            src, dst,
            args=args, source_bytes=source_bytes,
            elapsed_s=elapsed, quality_scores=quality_scores,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

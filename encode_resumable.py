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

from encode_modules.chunk_hook import ChunkHook
from encode_modules.chunking import cleanup, reorder_middle_first, split_source
from encode_modules.cli_args import parse_args
from encode_modules.done_dir import (
    DoneDirRefusedError,
    move_to_done_dir,
    resolve_done_dir,
)
from encode_modules.file_complete_hook import FileCompleteHook
from encode_modules.finish_signal import FINISH_FILENAME
from encode_modules.hook_config import load_hooks_sidecar
from encode_modules.history_state import (
    attach_file_complete_hook,
    attach_job_end_hook,
    finalize_history_state,
    init_history_state,
    mark_status,
)
from encode_modules.job_end_hook import JobEndHook
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

    # Read & attach the on_job_end / on_file_complete hooks EARLY — before
    # pre-flight runs — so a pre-flight failure also fires on_job_end (the
    # file_complete hook is success-only and won't fire on pre-flight fail,
    # which is the right behaviour). The chunk hook can't be built yet (it
    # needs the chunks list + total duration), so we defer that until after
    # chunking; the job-level hooks only need src/workdir.
    job_end_command = None
    file_complete_command = None
    chunk_hook_command = None
    if args.hooks_config:
        hooks = load_hooks_sidecar(Path(args.hooks_config))
        if hooks is None:
            print(f"      WARNING: hook config unreadable "
                  f"({args.hooks_config}); continuing without hooks.",
                  file=sys.stderr)
        else:
            chunk_hook_command = hooks.get("on_chunk_done")
            job_end_command = hooks.get("on_job_end")
            file_complete_command = hooks.get("on_file_complete")
    job_end_hook = JobEndHook(job_end_command, source=src, workdir=workdir)
    attach_job_end_hook(job_end_hook)
    file_complete_hook = FileCompleteHook(file_complete_command,
                                          source=src, workdir=workdir)
    attach_file_complete_hook(file_complete_hook)
    if job_end_hook.enabled:
        print(f"      on_job_end hook: {job_end_command}")
    if file_complete_hook.enabled:
        print(f"      on_file_complete hook: {file_complete_command}")

    # Pre-flight + optional auto-patch. Bail before any chunking work if
    # the source is unsafe to encode. `encode_src` is what the pipeline
    # actually consumes — on auto-patch it's a rebuilt copy that lives INSIDE
    # the workdir. `src` stays the user's original (outside the workdir, never
    # deleted): the end-of-run reporters must read IT, because cleanup() wipes
    # the workdir — statting the patched copy there would crash a successful
    # encode into a false exit-1 (see tests/test_patched_source_cleanup.py).
    status, encode_src, scan, rescan = handle_preflight(src, workdir, args)
    if status == "failed":
        _exit_pre_flight_failed(src, dst, args, scan, rescan,
                                patched_attempted=args.auto_patch_source)

    chunks = split_source(encode_src, workdir, args.segment_seconds)
    print(f"      To stop after the current chunk (resumable): press 'f' in "
          f"the live display, or create {workdir / FINISH_FILENAME}")

    # Hook commands were already loaded above so the job-end hook could
    # attach before pre-flight. Re-bind the chunk hook value here under its
    # local name so the chunk-hook construction below stays unchanged.
    hook_command = chunk_hook_command
    total_dur = (args.total_duration_seconds
                 if args.total_duration_seconds is not None
                 else sum(probe_duration(c) for c in chunks))
    source_bytes = (args.source_bytes if args.source_bytes is not None
                    else encode_src.stat().st_size)

    # Bind the hook to the ORIGINAL src, not encode_src: on_chunk_done
    # notifications (e.g. Pushbullet) surface X265_SOURCE to the user, who
    # expects the source they queued — not the auto-patch's working copy
    # (`source-patched.mp4`). The workdir/chunk paths it also reports stay
    # correct because they're keyed off the original-stem workdir.
    # `chunks` + `total_duration_sec` + `duration_probe` power the new
    # X265_CHUNKS_DONE / X265_PROGRESS_PERCENT contract — honest progress in
    # parallel mode where chunks finish out of order (see chunk_hook.py).
    chunk_hook = ChunkHook(hook_command, source=src, workdir=workdir,
                           total=len(chunks), chunks=chunks,
                           total_duration_sec=total_dur,
                           duration_probe=probe_duration)
    if chunk_hook.enabled:
        print(f"      on_chunk_done hook: {hook_command}")

    init_history_state(encode_src, dst, args, source_bytes)

    problems = run_encode_verify_loop(
        encode_src, chunks, workdir, dst,
        args=args, total_dur=total_dur, source_bytes=source_bytes,
        max_attempts=MAX_VERIFY_ATTEMPTS,
        chunk_hook=chunk_hook,
    )
    if problems:
        mark_status("verify-failed", verify_problems=problems)
        handle_verify_failure_block(problems, workdir, dst, MAX_VERIFY_ATTEMPTS)
        # handle_verify_failure_block sys.exits(4); next line unreachable.

    quality_scores = measure_quality_and_write_sidecar(
        encode_src, dst, workdir, args=args, n_chunks_total=len(chunks),
    )

    elapsed = time.monotonic() - run_start
    # Flush history BEFORE cleanup wipes the workdir — finalize_history_state
    # probes each chunk's duration and reads file sizes off disk.
    finalize_history_state(
        encode_src, dst, workdir, chunks, elapsed, quality_scores,
        encode_order=reorder_middle_first(chunks),
    )

    ensure_not_source(workdir)
    cleanup(workdir)

    # --done-dir post-processing: after cleanup, when the user asked for an
    # archive, move source + output. Always after cleanup so a failure to
    # move can't leave the workdir lingering. resolve_done_dir mkdir's the
    # destination at startup so any "directory doesn't exist" error fires
    # before the encode, not after.
    final_src = src
    final_dst = dst
    if getattr(args, "done_dir", None):
        try:
            resolved_done = resolve_done_dir(args.done_dir,
                                              base_dir=src.parent)
            if resolved_done is not None:
                result = move_to_done_dir(
                    source=src, output=dst,
                    done_dir=resolved_done, workdir=workdir,
                    sidecar_dir=workdir.parent,
                )
                if result.moved:
                    print(f"      Moved source+output to: {resolved_done}")
                    final_src = result.source_final
                    final_dst = result.output_final
        except DoneDirRefusedError as e:
            print(f"WARNING: --done-dir move refused: {e}", file=sys.stderr)
        except OSError as e:
            # A cross-volume move can fail (e.g. ENOSPC mid-copy). Source is
            # guaranteed intact by the output-first ordering.
            print(f"WARNING: --done-dir move failed: {e}", file=sys.stderr)
    # Report against the FINAL paths so the user sees where the files actually
    # ended up. Falls back to the original src/dst when no move occurred.
    print_summary(final_src, final_dst, quality_scores)

    if not args.no_report:
        write_single_file_report(
            final_src, final_dst,
            args=args, source_bytes=source_bytes,
            elapsed_s=elapsed, quality_scores=quality_scores,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

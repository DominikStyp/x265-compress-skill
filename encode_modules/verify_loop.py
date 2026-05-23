"""Encode → concat → verify retry loop, plus the verify-failed teardown.

Lifted out of encode_resumable.main() so each step is a self-contained unit
of policy. The loop handles three classes of recoverable failures:

  * Missing enc_*.mkv chunks   -> re-run the chunk encoder next attempt.
  * Bad-duration enc_*.mkv     -> quarantine (rename, NEVER unlink), re-run.
  * DTS-collision verify fail  -> MPEG-TS roundtrip remux of the merged
                                  output; no re-encode (chunks are correct).

Anything else (e.g. genuine bitstream/audio passthrough corruption) stops
the loop and returns the problems list to the caller, which then renders
the OUTPUT VERIFICATION FAILED block and exits 4.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .chunking import concat_chunks
from .dts_recovery import attempt_dts_fix_remux, is_dts_only_verify_failure
from .encoder import encode_chunks
from .history_state import mark_status
from .messages import print_chunks_skipped_block
from .source_guard import ensure_not_source
from .verify import find_missing_enc_chunks, identify_bad_chunks, verify_output


def quarantine_chunk(chunk: Path) -> Path:
    """Rename an unusable chunk aside with a timestamped suffix instead of
    deleting it. Encoded chunks represent hours of CPU and are non-
    reproducible if the input shifted, so even chunks the script thinks
    are bad get preserved for the user to inspect / restore. Returns the
    new path. The resumable loop treats the renamed file as "missing" and
    re-encodes a replacement on the next attempt."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    quarantined = chunk.with_suffix(f".broken-{stamp}{chunk.suffix}")
    try:
        chunk.rename(quarantined)
    except OSError:
        # Suffix collision (very rare — second-grained stamp). Add pid.
        quarantined = chunk.with_suffix(
            f".broken-{stamp}-{os.getpid()}{chunk.suffix}")
        chunk.rename(quarantined)
    return quarantined


def run_encode_verify_loop(src: Path, chunks: list[Path], workdir: Path,
                          dst: Path, *, args: argparse.Namespace,
                          total_dur: float, source_bytes: int,
                          max_attempts: int) -> list[str]:
    """encode → concat → verify, with bounded retry on missing/bad chunks.

    Returns the list of remaining verify problems (empty = success). When
    chunk-choke skips happen, this function exits 7 directly — successful
    chunks stay on disk, sidecars next to skipped chunks document what's
    missing, and the queue runner moves on.

    Encoded chunks (enc_src_*.mkv) are NEVER deleted by this loop — hard
    rule. The only sanctioned deletion point is `cleanup(workdir)` after
    a fully successful encode+verify, owned by the top-level caller."""
    problems: list[str] = []
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(f"\n[Verify attempt {attempt}/{max_attempts}] "
                  f"Re-running encode for the chunks that need it.")

        skipped = encode_chunks(
            chunks, workdir,
            parallel=max(1, args.parallel),
            crf=args.crf, preset=args.preset,
            pix_fmt=args.pix_fmt, x265_params=args.x265_params,
            total_duration_sec=total_dur,
            source_bytes=source_bytes,
            max_output_bytes=args.max_output_bytes,
            choke_threshold_speed=args.choke_threshold_speed,
            choke_grace_seconds=args.choke_grace_seconds,
            auto_fix_choke=args.auto_fix_choke,
            segment_seconds=args.segment_seconds,
        )

        # Per-chunk choke skips: do NOT merge. Successful chunks stay on
        # disk; needs_fix.json sidecars next to skipped chunks tell a
        # fixer (Claude or the user) what to produce. Drop the replacement
        # chunk(s) into the workdir and re-run; resumable logic picks up.
        if skipped:
            print_chunks_skipped_block(workdir, skipped)
            mark_status("awaiting-chunk-fix",
                       skipped_chunks=skipped, skipped_count=len(skipped))
            sys.exit(7)

        missing = find_missing_enc_chunks(workdir)
        bad = identify_bad_chunks(workdir)
        quarantined_paths = _quarantine_bad_chunks(bad)
        if missing or bad:
            if attempt < max_attempts:
                _log_missing_and_bad(missing, bad)
                continue
            return _final_attempt_problems(missing, quarantined_paths)

        if dst.exists():
            ensure_not_source(dst)
            dst.unlink()
        concat_chunks(workdir, dst)
        problems = verify_output(src, dst)
        if not problems:
            return []

        # DTS-collision-only failure: a known x265-chunked-output artifact
        # where per-chunk decode walks all pass but the concat'd mkv has
        # duplicate DTS that ffmpeg's strict -xerror decode flags. Plays
        # fine in real players; the fix is purely metadata. MPEG-TS
        # roundtrip rebuilds clean monotonic DTS without re-encoding video.
        # Try it ONCE before declaring upstream corruption.
        if is_dts_only_verify_failure(problems):
            if _attempt_dts_recovery(src, dst):
                return []

        return _final_verify_failure(problems, workdir)
    return problems


def _quarantine_bad_chunks(bad: list[Path]) -> list[Path]:
    """Best-effort rename of every bad chunk. Failed renames are warned but
    don't abort — the chunk stays in place and the next attempt's verify
    will see it as bad again."""
    quarantined: list[Path] = []
    for c in bad:
        try:
            quarantined.append(quarantine_chunk(c))
        except OSError as e:
            print(f"  WARNING: could not quarantine {c.name}: {e}")
    return quarantined


def _log_missing_and_bad(missing: list[Path], bad: list[Path]) -> None:
    if missing:
        print(f"  {len(missing)} chunk(s) missing after encode: "
              f"{[m.name for m in missing]}")
    if bad:
        print(f"  {len(bad)} chunk(s) had bad duration (quarantined to "
              f".broken-*.mkv, will re-encode): {[b.name for b in bad]}")


def _final_attempt_problems(missing: list[Path],
                           quarantined_paths: list[Path]) -> list[str]:
    """Build the problems list returned on attempt exhaustion."""
    return (
        [f"chunk missing after final attempt: {m.name}" for m in missing]
        + [f"chunk corrupt after final attempt (quarantined as {q.name}): "
           f"re-run to retry" for q in quarantined_paths]
    )


def _attempt_dts_recovery(src: Path, dst: Path) -> bool:
    """Try MPEG-TS roundtrip. Returns True iff the recovery produced a
    file that now passes verify_output."""
    print("[Verify] DTS-collision-only failure — attempting "
          "MPEG-TS roundtrip remux (no re-encode)...")
    if not attempt_dts_fix_remux(dst):
        print("[Verify] DTS-fix remux step itself failed; falling through "
              "to upstream-issue diagnostic.")
        return False
    if verify_output(src, dst):
        print("[Verify] DTS-fix remux did not clear all problems; falling "
              "through to upstream-issue diagnostic.")
        return False
    print("[Verify] DTS-fix remux passed verify. Output is clean.")
    return True


def _final_verify_failure(problems: list[str], workdir: Path) -> list[str]:
    """Print the diagnostic for an unrecoverable verify failure + preserve
    chunks. Returned `problems` is what main() exits 4 with."""
    print(f"\n[Verify] Merged output failed verification:")
    for p in problems:
        print(f"    - {p}")
    print("  All chunk files look OK individually, so the cause is in the "
          "source bitstream / passthrough audio / container mux, not in "
          "the video encode. Re-encoding would burn CPU on the same "
          "failure. Encoded chunks preserved in:")
    print(f"    {workdir}")
    return problems


def handle_verify_failure_block(problems: list[str], workdir: Path,
                               dst: Path, max_attempts: int) -> None:
    """Print the red OUTPUT VERIFICATION FAILED box and exit 4 after the
    encode→verify loop's recovery strategies have been exhausted. Renames
    the broken output IN PLACE to `damaged_<name>.<ext>` (same dir, NOT
    under .tmp/) so the user sees the failed file alongside where the
    successful one would have been."""
    damaged = _pick_damaged_name(dst)
    if dst.exists():
        ensure_not_source(dst)
        try:
            dst.rename(damaged)
        except OSError:
            damaged = dst
    R, Z = "\033[31;1m", "\033[0m"
    print()
    print(f"{R}=================================================={Z}")
    print(f"{R}  OUTPUT VERIFICATION FAILED AFTER {max_attempts} ATTEMPTS{Z}")
    for p in problems:
        print(f"{R}    - {p}{Z}")
    print(f"{R}  Damaged output: {damaged.name}{Z}")
    print(f"{R}  Workdir:        {workdir}  (chunks preserved){Z}")
    print(f"{R}  Encoded chunks are intact — DO NOT delete the workdir{Z}")
    print(f"{R}  unless you're ready to lose hours of CPU. Inspect first.{Z}")
    print(f"{R}=================================================={Z}")
    sys.exit(4)


def _pick_damaged_name(dst: Path) -> Path:
    """Pick the rename target for a failed output. Uniquify with timestamp
    if a previous damaged copy already sits at the obvious name."""
    damaged = dst.parent / f"damaged_{dst.name}"
    if damaged.exists():
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        damaged = dst.parent / f"damaged_{stamp}_{dst.name}"
    return damaged

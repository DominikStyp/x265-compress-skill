"""User-facing print blocks for the encoder pipeline. Extracted from
`encoder.py` so the encoder module stays focused on encoding logic; this
file is pure stdout formatting.

Every function here prints to stdout/stderr and returns nothing. Color
escapes are inline ANSI SGR (yellow-bold for warnings, red-bold for hard
failures, plain for normal info). The encoder entry point enables ANSI
via `platform_compat.enable_ansi()` before main() runs (no-op on POSIX).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from platform_compat import IS_POSIX, IS_WINDOWS


def print_encode_plan(todo: list, total: int, already: int, *,
                     parallel: int, cores_per_chunk: int,
                     first_pos: Optional[int],
                     max_output_bytes: Optional[int],
                     source_bytes: int) -> None:
    """[2/4] section header: how many chunks remain, what the encode order's
    next chunk is, and (if set) the size-guard threshold."""
    print(f"[2/4] Encoding {len(todo)} of {total} chunks. "
          f"parallel={parallel}, ~{cores_per_chunk} cores each. "
          f"{already} already done.")
    if todo and total > 1 and first_pos is not None:
        print(f"      Encoding order: middle-first "
              f"(next chunk: {first_pos}/{total})")
    if max_output_bytes:
        thr_mb = max_output_bytes / (1024 * 1024)
        thr_pct = max_output_bytes / source_bytes * 100 if source_bytes else 0
        print(f"      Max-size guard: stop if projected output > "
              f"{thr_mb:.1f} MB ({thr_pct:.1f}% of source).")


def print_runtime_protections(has_job_protection: bool) -> None:
    """Announce the two runtime safeguards both parallel and serial paths
    rely on: Win32 Job Object (auto-kills ffmpeg children if Python is
    hard-killed) and IDLE CPU priority (foreground apps always preempt)."""
    if has_job_protection:
        print("      Process protection: ffmpeg children auto-killed if this "
              "script dies (Job Object).")
    else:
        print("      WARNING: Job Object unavailable — hard-killing this "
              "script (taskkill /F) will leave orphan ffmpegs.",
              file=sys.stderr)
    if IS_WINDOWS or IS_POSIX:
        # Same effective behaviour on both OSes — foreground apps preempt
        # the encode. Win32 IDLE_PRIORITY_CLASS / POSIX nice 19.
        print("      CPU priority: ffmpeg runs at low priority — "
              "foreground apps (browser, editor) always preempt encode.")


def print_choke_guard_announcement(threshold_speed: float,
                                   grace_seconds: float) -> None:
    """One-line note that the choke guard is active and with what limits."""
    if threshold_speed > 0 and grace_seconds > 0:
        print(f"      Choke guard: skip chunk + write needs_fix sidecar if "
              f"speed stays below {threshold_speed:g}x after "
              f"{grace_seconds:.0f}s wall.")


def print_chunks_skipped_block(workdir: Path, skipped: list[dict]) -> None:
    """Yellow-bold "ENCODE INCOMPLETE — chunks skipped" block. Encode
    continues across other chunks; we stop at the merge step. The user
    (or a follow-up Claude run reading the needs_fix.json sidecars) is
    expected to provide the missing chunks before re-running."""
    Y, R = "\033[33;1m", "\033[0m"
    print()
    print(f"{Y}=================================================={R}")
    print(f"{Y}  ENCODE INCOMPLETE — {len(skipped)} chunk(s) skipped, "
          f"output NOT merged.{R}")
    for s in skipped:
        tr = s.get("time_range_seconds", [0, 0])
        print(f"{Y}  {s['chunk_name']:>18}  sec {tr[0]:.0f}-{tr[1]:.0f}  "
              f"speed {s.get('choke_speed', 0):.4f}x  "
              f"after {s.get('choke_wall_seconds', 0):.0f}s  "
              f"({s.get('error_count', 0)} decode errors){R}")
    print(f"{Y}  Workdir: {workdir}{R}")
    print(f"{Y}  Per-chunk needs_fix.json sidecars written next to each.{R}")
    print(f"{Y}  Drop fixed enc_src_NNNN.mkv into workdir and re-run the .bat;{R}")
    print(f"{Y}  resumable logic detects the chunks present and proceeds to{R}")
    print(f"{Y}  concat. To enable one-shot auto-retry with relaxed params:{R}")
    print(f"{Y}    add --auto-fix-choke (off by default).{R}")
    print(f"{Y}=================================================={R}")


def print_threshold_abort_block(workdir: Path, abort_reason: str) -> None:
    """Yellow-bold ENCODING STOPPED box following a max-size threshold abort.
    Tells the user how to recover (re-run without the guard, with higher
    CRF, or wipe the workdir to start fresh)."""
    Y, R = "\033[33;1m", "\033[0m"
    print()
    print(f"{Y}=================================================={R}")
    print(f"{Y}  ENCODING STOPPED!{R}")
    print(f"{Y}  {abort_reason}{R}")
    print(f"{Y}  Done chunks left on disk in {workdir} — re-run this .bat{R}")
    print(f"{Y}  WITHOUT --max-size-percent to keep going, or with a higher{R}")
    print(f"{Y}  CRF (--crf <N>) to reduce size, or delete the workdir to{R}")
    print(f"{Y}  start fresh.{R}")
    print(f"{Y}=================================================={R}")

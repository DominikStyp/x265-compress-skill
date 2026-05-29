"""
x265-compress-skill: analyse a video with ffprobe, decide x265 settings,
and write a .bat next to it that does the actual encode.

Run:
    python compress.py "<full path to source video>" [--crf N] [--preset name]
                                                     [--anime] [--grain]
                                                     [--eight-bit]

Output:
    - <source_dir>/.tmp/compress_<basename>.bat   (the script the user runs)
    - JSON summary printed to stdout              (consumed by run_queue.py)

The script never runs ffmpeg itself — pure analysis + .bat generation. All
the heavy lifting lives under `compress_modules/`:
    probe        ffprobe wrapper + SourceInfo dataclass.
    plan         CRF/preset/parallel decision logic + EncodePlan composition.
    bat_writer   Templates + write_bat().
    x265_params  Sharpness/motion-tuned x265 knob constants.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

from compress_modules.plan import pick_parallel, plan_encode
from compress_modules.probe import analyse
from compress_modules.script_writer import write_script
from encode_modules.hook_config import parse_hook_spec
from platform_compat import enable_utf8_io


def _build_arg_parser() -> argparse.ArgumentParser:
    """All argparse setup. Kept separate so `main` reads as a recipe rather
    than 70 lines of flag definitions."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="Path to the source video file")
    ap.add_argument("--crf", type=int, default=None,
                    help="Override the auto-chosen CRF (lower = higher quality)")
    ap.add_argument("--preset", default=None,
                    help="Override preset (e.g. medium / slow / slower / veryslow)")
    ap.add_argument("--anime", action="store_true",
                    help="Use x265 :tune=animation (flat-color / line-art content)")
    ap.add_argument("--grain", action="store_true",
                    help="Use x265 :tune=grain (preserve film grain)")
    ap.add_argument("--eight-bit", action="store_true",
                    help="Force 8-bit output (yuv420p) for max compatibility")
    ap.add_argument("--resumable", action="store_true",
                    help="Generate a resumable .bat: splits source into chunks, "
                         "encodes each, then concats. Re-run the .bat to resume "
                         "after a kill/reboot. Costs ~2-5%% size and a bit of time.")
    ap.add_argument("--segments", type=int, default=10,
                    help="With --resumable, target N total chunks (default 10 -> "
                         "each chunk is ~10%% of source length). Overridden by "
                         "--segment-seconds.")
    ap.add_argument("--segment-seconds", type=int, default=None,
                    help="Override --segments by giving an absolute chunk length "
                         "in seconds.")
    ap.add_argument("--parallel", default="auto",
                    help="With --resumable, encode N chunks concurrently. "
                         "Integer or 'auto' (default — picks from probed source "
                         "height: >=2160p -> 1, >=1080p -> 4, >=720p -> 6, lower -> 8).")
    ap.add_argument("--max-size-percent", type=float, default=None,
                    help="With --resumable, abort the encode if projected output "
                         "exceeds this percentage of source size. Projection "
                         "starts at >=5%% overall progress.")
    ap.add_argument("--auto-fix-choke", action="store_true",
                    help="With --resumable, if a chunk chokes mid-encode, retry "
                         "it once with relaxed x265 params (me=umh, subme=3, "
                         "merange=32) + decode-walk verification.")
    ap.add_argument("--no-pre-flight-scan", action="store_true",
                    help="Skip the pre-flight source-corruption scan.")
    ap.add_argument("--auto-patch-source", action="store_true",
                    help="On pre-flight failure for an h264 source, auto-apply "
                         "the surgical-patch recipe (re-encode JUST the broken "
                         "GOPs via ffmpeg error concealment). Requires --resumable.")
    ap.add_argument("--max-patch-seconds", type=float, default=10.0,
                    help="Loss budget for --auto-patch-source (default 10s).")
    ap.add_argument("--on-chunk-done", default=None, metavar="CMD",
                    help="With --resumable, run CMD after each chunk finishes "
                         "(success or failure). CMD is a JSON array argv list "
                         "(e.g. '[\"bash\",\"/path/notify.sh\"]') or a bare "
                         "command token. Context is passed via X265_* env vars "
                         "(X265_CHUNK_INDEX, X265_CHUNK_TOTAL, X265_CHUNK_STATUS, "
                         "X265_CHUNK_OUTPUT, X265_SOURCE, ...). Best-effort: a "
                         "slow/failing hook never aborts the encode.")
    ap.add_argument("--on-job-end", default=None, metavar="CMD",
                    help="With --resumable, run CMD exactly once when the job "
                         "ends — for ANY terminal status (ok, stopped-threshold, "
                         "chunk-choked, pre-flight-failed, verify-failed, "
                         "stopped-by-user, ...). Same shape as --on-chunk-done. "
                         "Context via X265_JOB_STATUS, X265_JOB_STOP_REASON, "
                         "X265_JOB_STOP_DETAIL, X265_CRF, X265_CRF_RETRY_CHAIN, "
                         "X265_OUTPUT_BYTES_PROJECTED, X265_PCT_SAVED, etc. "
                         "Best-effort, never aborts the encode.")
    ap.add_argument("--on-file-complete", default=None, metavar="CMD",
                    help="With --resumable, run CMD exactly once when the "
                         "encoded .mkv is final and on disk (success only — "
                         "skips for any stop / failure). Carries queue-level "
                         "X265_QUEUE_* counters when invoked via run_queue.py "
                         "(degrades to 1/1 defaults in single-file mode). "
                         "Same shape as --on-chunk-done; best-effort.")
    ap.add_argument("--no-report", action="store_true",
                    help="Don't write a per-file markdown report. Set by "
                         "run_queue.py because it writes an aggregate report.")
    ap.add_argument("--no-pause", action="store_true",
                    help="Skip the trailing `pause` in the generated .bat. "
                         "Set by run_queue.py so per-job bats don't block.")
    ap.add_argument("--done-dir", default=None, metavar="PATH",
                    help="With --resumable, after a successful encode move "
                         "BOTH source and output into PATH. Supports `~` "
                         "expansion; relative paths resolve against the "
                         "source's directory (single-file) or the queue's "
                         "directory (queue mode). The directory is created if "
                         "missing. Refuses to move into a workdir subtree or "
                         "overwrite an existing destination. Only fires on "
                         "status == ok; every other status leaves files in "
                         "place.")
    return ap


def _resolve_parallel(value: str | int, info, resumable: bool) -> tuple[int, bool]:
    """Translate the --parallel CLI value into a concrete int.

    `auto` derives from probed source height. Non-resumable invocations always
    collapse to 1 because the parallel-chunk encoder requires --resumable.
    Returns (parallel, was_auto) so callers can suppress the "ignored" warning
    in the auto case."""
    was_auto = isinstance(value, str) and value.lower() == "auto"
    if was_auto:
        parallel = pick_parallel(info)
    else:
        try:
            parallel = int(value)
        except (TypeError, ValueError):
            sys.exit(f"ERROR: --parallel must be an integer or 'auto', got {value!r}")
        if parallel < 1:
            sys.exit(f"ERROR: --parallel must be >= 1, got {parallel}")

    if parallel > 1 and not resumable:
        if was_auto:
            return 1, was_auto  # silent in auto case (default for non-resumable)
        print("WARNING: --parallel ignored without --resumable (parallel mode "
              "requires the chunked encoder). Add --resumable to use it.",
              file=sys.stderr)
        return 1, was_auto
    return parallel, was_auto


def _resolve_segment_seconds(args: argparse.Namespace, duration_sec: float) -> int:
    """--segment-seconds (explicit) wins; otherwise derive from --segments
    and source duration so the user gets the requested chunk *count*
    regardless of file length."""
    if args.segment_seconds is not None:
        return max(1, args.segment_seconds)
    n = max(1, args.segments)
    if duration_sec:
        return max(5, math.ceil(duration_sec / n))
    return 60  # source has no duration metadata — fall back to 1 min chunks


def _archival_size_guard_warning(crf: int,
                                max_size_percent: float | None) -> str | None:
    """Warn when a near-lossless/archival CRF is paired with a size guard:
    such encodes barely shrink and frequently exceed --max-size-percent,
    stopping early (exit 3) before finishing. Returns the message, or None."""
    if max_size_percent is not None and crf <= 18:
        return (f"WARNING: CRF {crf} targets archival / near-lossless quality, "
                f"which often exceeds --max-size-percent {max_size_percent:g}% "
                f"and stops the encode early (exit 3) before it finishes. For "
                f"archival, drop --max-size-percent; otherwise raise the CRF.")
    return None


def main() -> int:
    enable_utf8_io()  # ensure_ascii=False JSON + warnings -> utf-8 if redirected
    args = _build_arg_parser().parse_args()

    source_path = Path(args.input).resolve()
    if not source_path.is_file():
        hint = ("That's a folder — point me at a video file inside it"
                if source_path.is_dir() else
                "Check the path is spelled correctly, and wrap it in quotes "
                "if it contains spaces")
        sys.exit(f"ERROR: not a file: {source_path}\n       {hint}.")

    info = analyse(source_path)
    plan = plan_encode(
        info, source_path,
        override_crf=args.crf, override_preset=args.preset,
        anime=args.anime, grain=args.grain, eight_bit=args.eight_bit,
    )

    parallel, parallel_was_auto = _resolve_parallel(args.parallel, info, args.resumable)
    segment_seconds = _resolve_segment_seconds(args, info.duration_sec)

    if args.max_size_percent is not None and not args.resumable:
        print("WARNING: --max-size-percent ignored without --resumable.",
              file=sys.stderr)

    max_output_bytes: int | None = None
    if args.max_size_percent is not None and source_path.is_file():
        max_output_bytes = int(source_path.stat().st_size * args.max_size_percent / 100)

    if args.resumable:
        archival_warning = _archival_size_guard_warning(
            plan.crf, args.max_size_percent)
        if archival_warning:
            print(archival_warning, file=sys.stderr)

    # Parse the hooks up front so a malformed command fails loud HERE, before
    # any script is written or any encode starts. Both hooks require the
    # chunked encoder (the job-end hook attaches to the recorder which runs
    # inside encode_resumable.py), so warn + drop them for a non-resumable run.
    hook_command = None
    if args.on_chunk_done:
        try:
            hook_command = parse_hook_spec(args.on_chunk_done,
                                           key="on_chunk_done")
        except ValueError as e:
            sys.exit(f"ERROR: --on-chunk-done: {e}")
        if not args.resumable:
            print("WARNING: --on-chunk-done ignored without --resumable "
                  "(chunk hooks require the chunked encoder).", file=sys.stderr)
            hook_command = None
    job_end_command = None
    if args.on_job_end:
        try:
            job_end_command = parse_hook_spec(args.on_job_end,
                                              key="on_job_end")
        except ValueError as e:
            sys.exit(f"ERROR: --on-job-end: {e}")
        if not args.resumable:
            print("WARNING: --on-job-end ignored without --resumable "
                  "(job-end hook fires from the chunked encoder's history "
                  "flush).", file=sys.stderr)
            job_end_command = None
    file_complete_command = None
    if args.on_file_complete:
        try:
            file_complete_command = parse_hook_spec(args.on_file_complete,
                                                    key="on_file_complete")
        except ValueError as e:
            sys.exit(f"ERROR: --on-file-complete: {e}")
        if not args.resumable:
            print("WARNING: --on-file-complete ignored without --resumable "
                  "(file-complete hook fires from the chunked encoder's "
                  "history flush).", file=sys.stderr)
            file_complete_command = None

    # Resolve --done-dir against the source's directory in single-file mode.
    # Queue mode overrides this resolution by setting an ABSOLUTE path in the
    # done_dir argv (queue_modules.job_schema), so the queue's resolution
    # (relative to queue.json's dir) is what wins there.
    done_dir: str | None = None
    if args.done_dir:
        if not args.resumable:
            print("WARNING: --done-dir ignored without --resumable.",
                  file=sys.stderr)
        else:
            done_dir = args.done_dir

    write_script(
        info, plan, source_path,
        resumable=args.resumable, segment_seconds=segment_seconds,
        parallel=parallel,
        max_output_bytes=max_output_bytes,
        max_size_percent=args.max_size_percent,
        auto_fix_choke=args.auto_fix_choke,
        no_pre_flight_scan=args.no_pre_flight_scan,
        auto_patch_source=args.auto_patch_source,
        max_patch_seconds=args.max_patch_seconds,
        no_report=args.no_report,
        no_pause=args.no_pause,
        on_chunk_done=hook_command,
        on_job_end=job_end_command,
        on_file_complete=file_complete_command,
        done_dir=done_dir,
    )

    print(json.dumps({
        "script_path": plan.script_path,
        # Back-compat alias — queue runners written before the macOS port
        # still read summary["bat_path"]; same value, friendlier name first.
        "bat_path": plan.script_path,
        "input_path": str(source_path),
        "output_path": plan.output_path,
        "source": asdict(info),
        "plan": {
            "crf": plan.crf, "preset": plan.preset,
            "pix_fmt_out": plan.pix_fmt_out,
            "x265_params": plan.x265_params,
            "estimated_reduction": plan.estimated_reduction,
        },
        "warnings": plan.warnings, "notes": plan.notes,
        "resumable": args.resumable,
        "parallel": parallel, "parallel_auto": parallel_was_auto,
        "segments_requested": args.segments,
        "segment_seconds_effective": segment_seconds,
        "max_size_percent": args.max_size_percent,
        "max_output_bytes": max_output_bytes,
        "on_chunk_done": hook_command,
        "on_job_end": job_end_command,
        "on_file_complete": file_complete_command,
        "done_dir": done_dir,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

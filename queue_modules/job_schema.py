"""Job schema: which keys a queue.json job may contain, how to merge it with
defaults, how to translate it into compress.py argv, and how to expand
input globs into one job per matching file.

Adding a new compress.py flag to the queue surface = add a line to
VALID_KEYS and one to build_compress_argv. That's the whole change.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from compress_modules.plan import compress_workdir


# Snake-case keys a queue job dict may contain. Maps 1:1 to compress.py CLI
# flags (kebab-cased). Unknown keys are warned about + dropped — typo
# safety net.
VALID_KEYS: set[str] = {
    "input",
    "crf",
    "preset",
    "segments",
    "segment_seconds",
    "parallel",
    "max_size_percent",
    "visual_quality_threshold",
    "anime",
    "grain",
    "eight_bit",
    "resumable",
    "auto_fix_choke",
    "no_pre_flight_scan",
    "auto_patch_source",
    "max_patch_seconds",
    "on_chunk_done",
    "on_job_end",
    "on_file_complete",
    "done_dir",
    # Queue-only keys (consumed by the queue runner, NOT forwarded to
    # compress.py argv): auto-escalate CRF when the size guard stops a job;
    # fire the queue-side `on_queue_item_end` notification with the full
    # `[OK]` / `[FAILED]` / `[..]` snapshot after each finished job;
    # adaptive-CRF-jump escalation (since 1.15.0; opt-in, defaults
    # preserve today's `+crf_step` behaviour byte-identically).
    "retry_with_bigger_crf",
    "crf_step",
    "crf_max",
    "on_queue_item_end",
    "crf_jump",
    "crf_jump_k",
    "crf_jump_margin",
    "crf_floor_min_gain",
}


def merge_job(defaults: dict, job: dict) -> dict:
    """defaults <- overridden by job. Unknown keys are dropped with a
    warning so a typo in queue.json doesn't silently get ignored."""
    merged: dict = {}
    merged.update(defaults)
    merged.update(job)
    unknown = set(merged) - VALID_KEYS
    for k in sorted(unknown):
        print(f"WARNING: unknown queue key ignored: {k!r}", file=sys.stderr)
    return {k: v for k, v in merged.items() if k in VALID_KEYS}


def build_compress_argv(job: dict) -> list[str]:
    """Translate a merged job dict to compress.py argv. Each flag is only
    emitted when the corresponding key is present in `job` — a missing key
    means "use compress.py's default", not "explicit absent value"."""
    argv: list[str] = [str(Path(job["input"]).resolve())]

    if "crf" in job:
        argv += ["--crf", str(int(job["crf"]))]
    if "preset" in job:
        argv += ["--preset", str(job["preset"])]
    if "segments" in job:
        argv += ["--segments", str(int(job["segments"]))]
    if "segment_seconds" in job:
        argv += ["--segment-seconds", str(int(job["segment_seconds"]))]
    if "parallel" in job:
        # Accept either an integer or the string "auto" — compress.py
        # derives the int from probed source height for 'auto'.
        v = job["parallel"]
        if isinstance(v, str) and v.lower() == "auto":
            argv += ["--parallel", "auto"]
        else:
            argv += ["--parallel", str(int(v))]
    if "max_size_percent" in job:
        argv += ["--max-size-percent", str(float(job["max_size_percent"]))]
    if "visual_quality_threshold" in job:
        argv += ["--visual-quality-threshold",
                 str(float(job["visual_quality_threshold"]))]
    if job.get("anime"):
        argv += ["--anime"]
    if job.get("grain"):
        argv += ["--grain"]
    if job.get("eight_bit"):
        argv += ["--eight-bit"]
    if job.get("auto_fix_choke"):
        argv += ["--auto-fix-choke"]
    if job.get("no_pre_flight_scan"):
        argv += ["--no-pre-flight-scan"]
    if job.get("auto_patch_source"):
        argv += ["--auto-patch-source"]
    if job.get("max_patch_seconds") is not None:
        argv += ["--max-patch-seconds", str(job["max_patch_seconds"])]
    cmd = job.get("on_chunk_done")
    if cmd:
        # Travels as one JSON-array argv element over the (shell-free)
        # queue->compress.py subprocess boundary; compress.py's parse_hook_spec
        # reads it back. A bare string is wrapped so both queue spellings land
        # identically. A FALSY value (null / [] / "") is the supported way to
        # disable an inherited `defaults` hook for one job — emit no flag.
        argv += ["--on-chunk-done",
                 json.dumps(cmd if isinstance(cmd, list) else [cmd])]
    cmd_job_end = job.get("on_job_end")
    if cmd_job_end:
        # Same shape and disable-via-falsy semantics as on_chunk_done.
        argv += ["--on-job-end",
                 json.dumps(cmd_job_end if isinstance(cmd_job_end, list)
                            else [cmd_job_end])]
    cmd_fc = job.get("on_file_complete")
    if cmd_fc:
        argv += ["--on-file-complete",
                 json.dumps(cmd_fc if isinstance(cmd_fc, list) else [cmd_fc])]
    # done_dir is pre-resolved to an absolute path by run_queue.py (relative
    # to the queue.json's dir, per the spec — NOT to the source's dir). The
    # encoder then takes it as-is via --done-dir.
    if job.get("done_dir"):
        argv += ["--done-dir", str(job["done_dir"])]
    # In queue mode the default is resumable=true (kills survive, partial
    # work is preserved). Set "resumable": false in JSON to opt out.
    if job.get("resumable", True):
        argv += ["--resumable"]

    return argv


def derive_output_path(input_path: Path) -> Path:
    """Same rule as compress.py: same dir, same basename, .mkv ext. If
    source is already .mkv, suffix with .x265 to avoid overwrite."""
    base = input_path.stem
    if input_path.suffix.lower() == ".mkv":
        return input_path.parent / f"{base}.x265.mkv"
    return input_path.parent / f"{base}.mkv"


def derive_workdir(input_path: Path) -> Path:
    """The encoder's per-source working directory: `<output_dir>/.tmp/.compress_
    <source_stem>`. Used by the CRF-retry logic to locate already-encoded chunks
    that must be set aside between attempts. Routes through the shared
    `compress_workdir` so it can never drift from what the generator creates."""
    tmp_dir = derive_output_path(input_path).parent / ".tmp"
    return compress_workdir(tmp_dir, input_path)


def expand_jobs(jobs: list[dict], queue_dir: Path) -> list[dict]:
    """Resolve relative `input` paths against queue_dir; expand */?/[ globs
    into one job per matching file (other keys inherited verbatim).

    Literal-first rule: a filename containing `[ext.to]` would otherwise
    be interpreted as a glob character class and silently match nothing.
    We resolve as a literal path first and only fall back to glob expansion
    if the literal doesn't exist on disk."""
    out: list[dict] = []
    for raw in jobs:
        if "input" not in raw:
            print("WARNING: job has no 'input' key, skipped:", raw,
                  file=sys.stderr)
            continue
        pattern = str(raw["input"])
        literal = Path(pattern) if Path(pattern).is_absolute() else queue_dir / pattern
        if literal.exists():
            out.append({**raw, "input": str(literal.resolve())})
            continue
        if any(ch in pattern for ch in "*?["):
            p = Path(pattern)
            matches = (sorted(p.parent.glob(p.name)) if p.is_absolute()
                       else sorted(queue_dir.glob(pattern)))
            if not matches:
                print(f"WARNING: glob matched nothing: {pattern}",
                      file=sys.stderr)
                continue
            for m in matches:
                out.append({**raw, "input": str(m.resolve())})
        else:
            # No glob chars and literal doesn't exist — pass through anyway
            # so the downstream "skipped-not-found" row appears in the
            # report. Without this, the queue silently swallows typos.
            out.append({**raw, "input": str(literal.resolve())})
    return out

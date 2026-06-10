"""
Queue runner: process a JSON list of encoding jobs sequentially.

Usage:
    python run_queue.py <queue.json>
    python run_queue.py <queue.json> --stop-on-failure
    python run_queue.py <queue.json> --no-skip-existing
    python run_queue.py <queue.json> --json-status status.ndjson

Exit code: 0 = all jobs clean, 1 = at least one real failure, 2 = no hard
failure but a job needs attention (size-guard abort, chunks awaiting a
manual fix, missing input, corrupt source).

Live-reload: re-reads queue.json before every job, so edits to pending jobs
or `defaults` apply at the next job boundary — no restart needed.

JSON shapes accepted, per-job key schema, and the threshold/exit-code
behaviour are all documented in the skill's SKILL.md. Implementation is
split into queue_modules:
    job_schema -- VALID_KEYS + merge_job + build_compress_argv + globs.
    queue_io   -- load_queue + reload_queue_with_retry.
    job_runner -- generate_bat + run_bat + build_job_row + run_one_job.

This file owns ONLY the orchestration loop: live-reload, skip rules,
summary stats, aggregate-report writer dispatch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import time

from encode_modules.done_dir import resolve_done_dir
from encode_modules.log_paths import migrate_for_queue_run
from platform_compat import enable_utf8_io
from queue_modules.crf_retry import run_job_with_crf_retry
from queue_modules.job_schema import derive_output_path, merge_job
from queue_modules.queue_counters import compute_queue_counters, overlay_env
from queue_modules.queue_io import emit_status_record, reload_queue_with_retry
from queue_modules.queue_reporting import (
    print_summary_table as _print_summary_table,
    write_aggregate_reports as _write_aggregate_reports,
)
from queue_modules.queue_item_hook import dispatch_on_queue_item_end
from queue_modules.queue_state import (
    delete_queue_state,
    load_queue_state,
)
from queue_modules.status import run_inspector as _run_status_inspector
import history as _history_module

# Re-export so `from run_queue import _emit_json_status` (the test in
# test_queue_exit_codes uses this name) keeps working after the helper
# moved into queue_modules.queue_io. New code should use the public name.
_emit_json_status = emit_status_record


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("queue_file", help="Path to JSON queue file")
    ap.add_argument("--stop-on-failure", action="store_true",
                    help="Stop the queue on the first real failure. "
                         "Threshold-aborts (exit 3) never stop the queue "
                         "regardless of this flag.")
    ap.add_argument("--no-skip-existing", action="store_true",
                    help="Don't skip jobs whose final .mkv output already exists.")
    ap.add_argument("--json-status", metavar="PATH", default=None,
                    help="Append one NDJSON record per finished job to PATH "
                         "(machine-readable; stdout stays human-readable). "
                         "Tail it to watch a queue run live. Pass an empty "
                         "string to enable with the v1.19.0 default path "
                         "(<queue_folder>/logs/<queue_stem>.json-status.ndjson).")
    ap.add_argument("--reset-state", action="store_true",
                    help="Delete the queue's persistent state sidecar "
                         "(<queue_stem>.state.json) before starting. Use to "
                         "re-attempt jobs that were marked done in a "
                         "previous run.")
    ap.add_argument("--status", action="store_true",
                    help="Read-only inspector: print a single consolidated "
                         "table classifying every job in the queue as "
                         "DONE / PROCESSING / QUEUED with per-file sizes / "
                         "CRF / wall / savings. No encoding, no side "
                         "effects — combine with --status-json for "
                         "machine-readable output.")
    ap.add_argument("--status-json", action="store_true",
                    help="With --status, emit JSON instead of the table.")
    return ap.parse_args()


def _skip_if_missing_or_existing(merged: dict, *, i: int, n: int,
                                no_skip_existing: bool,
                                state=None) -> dict | None:
    """Pre-encode skip checks. Returns a placeholder report row if the job
    should be skipped (and prints the SKIP line); None if encoding should
    proceed.

    Order of checks:
      1. STATE: this job's `input_original` is in the state sidecar → clean
         skip (status `skipped-done`). Takes precedence over the input-missing
         check so a moved-via-done_dir source doesn't look like a missing
         input.
      2. INPUT MISSING: degrades to skipped-not-found (attention status —
         user typo / mount problem).
      3. OUTPUT EXISTS: same-directory clean skip (status `skipped-exists`).
    """
    input_path = Path(merged["input"])
    if state is not None and state.is_completed(input_path):
        rec = state.get(input_path) or {}
        moved_to = rec.get("moved_to_dir")
        location = (f" (moved to {moved_to})" if moved_to
                    else " (completed in place)")
        print(f"[{i}/{n}] SKIP — already done{location}")
        return {
            "input": str(input_path),
            "output": rec.get("output_final") or rec.get("output_original"),
            "input_bytes": rec.get("bytes_in", 0) or 0,
            "output_bytes": rec.get("bytes_out"),
            "crf": rec.get("crf_final") or merged.get("crf"),
            "preset": merged.get("preset"),
            "parallel": merged.get("parallel"),
            "max_size_percent": merged.get("max_size_percent"),
            "elapsed_seconds": rec.get("wall_seconds"),
            "status": "skipped-done",
        }
    if not input_path.is_file():
        print(f"[{i}/{n}] SKIP — input not found: {input_path}")
        return {
            "input": str(input_path), "output": None,
            "input_bytes": 0, "output_bytes": None,
            "crf": merged.get("crf"), "preset": merged.get("preset"),
            "parallel": merged.get("parallel"),
            "max_size_percent": merged.get("max_size_percent"),
            "elapsed_seconds": None, "status": "skipped-not-found",
        }
    out_path = derive_output_path(input_path)
    if out_path.exists() and not no_skip_existing:
        print(f"[{i}/{n}] SKIP — output exists: {out_path.name}")
        return {
            "input": str(input_path), "output": str(out_path),
            "input_bytes": input_path.stat().st_size,
            "output_bytes": out_path.stat().st_size,
            "crf": merged.get("crf"), "preset": merged.get("preset"),
            "parallel": merged.get("parallel"),
            "max_size_percent": merged.get("max_size_percent"),
            "elapsed_seconds": None, "status": "skipped-exists",
        }
    return None


def _pick_next_job(jobs: list[dict], seen_inputs: set[str],
                  attempted_inputs: set[str]) -> dict | None:
    """Walk the current snapshot in full, growing seen_inputs (the
    report-table denominator — never shrinks) over the WHOLE queue, then
    return the first unattempted job. Returns None if every job has been
    attempted.

    The full-snapshot walk matters: the denominator drives the `[i/n]`
    banner and `compute_queue_counters(total_jobs=...)` for the
    on_file_complete hook. An early-return after picking job 1 of a
    5-job queue used to leave `len(seen_inputs) == 1`, so the banner
    showed `[1/1]` and `X265_QUEUE_TOTAL` reported `1` until the second
    job was picked."""
    picked: dict | None = None
    for raw in jobs:
        input_str = str(Path(raw["input"]).resolve())
        seen_inputs.add(input_str)
        if picked is None and input_str not in attempted_inputs:
            picked = raw
    return picked


# Per-job status -> aggregate category for the process exit code. "clean":
# nothing to do. "attention": the encode ran but left a state needing a human
# decision. Anything else is treated as a real failure (fail-safe).
_CLEAN_STATUSES = {"ok", "skipped-exists", "skipped-done"}
_ATTENTION_STATUSES = {
    "stopped-threshold", "stopped-threshold-crf-exhausted",
    "stopped-quality-threshold",  # v1.17.0 — per-chunk VMAF guard fired
    "awaiting-chunk-fix", "skipped-not-found",
    "pre-flight-failed", "chunk-choked", "stopped-by-user",
}


def _aggregate_exit_code(job_reports: list[dict]) -> int:
    """Map the run's per-job statuses to a process exit code so a fleet
    runner can branch on $?:

        0  every job clean (ok, or output already existed)
        1  at least one real failure (compress.py crashed, bad output, ...)
        2  no hard failure, but a job needs attention (size-guard abort,
           chunks awaiting a manual fix, missing input, corrupt source)

    A hard failure (1) outranks a needs-attention (2). Unknown statuses are
    treated as failures so a new state surfaces loudly, not as 'clean'."""
    has_failure = False
    has_attention = False
    for j in job_reports:
        status = j.get("status", "")
        if status in _CLEAN_STATUSES:
            continue
        if status in _ATTENTION_STATUSES:
            has_attention = True
        else:
            has_failure = True
    if has_failure:
        return 1
    if has_attention:
        return 2
    return 0


def _record_completion(queue_state, queue_path: Path, row: dict,
                       merged: dict) -> None:
    """Append the just-finished ok job to the state sidecar + flush. Failure
    here is a logged warning — losing the state record is bad UX but never
    worth aborting the queue.

    Move-outcome verification: when the job had a done_dir, we VERIFY the
    files actually arrived before recording `moved_to_dir`. The encoder's
    move can fail (DoneDirRefusedError, OSError mid-copy) and the encoder
    still exits 0 because the encode itself succeeded — without this check
    we'd persist `moved_to_dir = <configured>` and the next run's skip
    logic would silently swallow a job whose files are still at the
    original path. Truth comes from disk."""
    try:
        input_path = Path(row["input"]).resolve()
        output_original = derive_output_path(input_path)
        done_dir_cfg = merged.get("done_dir")
        moved_to, input_final, output_final = _verify_move_outcome(
            done_dir_cfg, input_path, output_original)
        from datetime import datetime, timezone
        queue_state.add_completed(
            input_original=input_path,
            output_original=output_original,
            moved_to_dir=moved_to,
            input_final=input_final,
            output_final=output_final,
            crf_final=row.get("crf"),
            bytes_in=row.get("input_bytes"),
            bytes_out=row.get("output_bytes"),
            wall_seconds=row.get("elapsed_seconds"),
            completed_utc=datetime.now(timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        queue_state.save_atomically(queue_path)
    except (OSError, ValueError, KeyError, TypeError) as e:
        # Narrow catch per AGENTS.md: broad `except Exception` is reserved
        # for daemon-thread guard seams. State-sidecar failure must never
        # abort the queue, but the specific exceptions we can plausibly
        # see here are all I/O- or shape-related — list them explicitly.
        print(f"WARNING: state sidecar update failed: {e}", file=sys.stderr)


def _verify_move_outcome(done_dir_cfg, input_path: Path,
                         output_original: Path):
    """Look at disk truth: are source+output at done_dir, or still at the
    original location? Returns (moved_to_dir, input_final, output_final)
    with all three set when the move succeeded, all three None when no
    done_dir was configured, the move was refused/failed, OR done_dir
    resolves to the same directory as the source (a no-op configuration
    where the stat-checks would falsely register as "moved" because the
    files ARE in that directory — they just never went anywhere).

    A partial state (output moved but source not) is recorded conservatively
    as "no move" — the state sidecar's invariant is "if moved_to_dir is set,
    BOTH paths are at that location"; the next run will re-encode (which
    triggers the encoder's refuse-to-overwrite guard, which the user can
    resolve)."""
    if not done_dir_cfg:
        return None, None, None
    done_dir = Path(done_dir_cfg)
    if _same_dir(done_dir, input_path.parent):
        # done_dir == source's own directory → move_to_done_dir was a
        # no-op. Record no move (files never went anywhere) so the state
        # sidecar's "moved_to_dir set ⇒ files are at that location"
        # invariant holds.
        return None, None, None
    input_at_done = done_dir / input_path.name
    output_at_done = done_dir / output_original.name
    if input_at_done.exists() and output_at_done.exists():
        return done_dir, input_at_done, output_at_done
    return None, None, None


def _same_dir(a: Path, b: Path) -> bool:
    """True iff a and b refer to the same on-disk directory. Path.samefile
    is the canonical comparison (case-insensitive on Windows NTFS); falls
    back to a resolved-string compare when one side doesn't exist yet."""
    try:
        return a.samefile(b)
    except OSError:
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return str(a) == str(b)


def _should_halt_after(status: str, stop_on_failure: bool) -> bool:
    """Whether to stop launching further queue jobs after a job ended with
    `status`. A user-requested finish (`stopped-by-user`) always halts the
    queue — that's the point of the feature ('stop the queue too'). A real
    failure halts only under --stop-on-failure."""
    if status == "stopped-by-user":
        return True
    return stop_on_failure and status.startswith("failed")


def main() -> int:
    enable_utf8_io()  # status/report output -> utf-8 even when redirected
    args = _parse_args()
    queue_path = Path(args.queue_file).resolve()
    if not queue_path.is_file():
        sys.exit(f"ERROR: queue file not found: {queue_path}")

    skill_dir = Path(__file__).resolve().parent
    compress_py = skill_dir / "compress.py"
    if not compress_py.is_file():
        sys.exit(f"ERROR: compress.py missing at {compress_py}")

    # --status: read-only inspector. Early exit, no encoding — and crucially
    # BEFORE the log migration below, which physically moves files; "no side
    # effects" is part of the flag's documented contract.
    if args.status:
        return _run_status_inspector(queue_path, as_json=args.status_json)

    # v1.19.0 one-shot migration: queue-level state + report sidecars +
    # default history JSONL into logs/. Idempotent across runs; best-effort.
    _migrated = migrate_for_queue_run(
        queue_path, _history_module.default_history_root())
    if _migrated:
        print(f"Queue: v1.19.0 layout — migrated {len(_migrated)} legacy "
              f"log file(s) into logs/")

    if args.reset_state:
        delete_queue_state(queue_path)
        print(f"Queue: state sidecar cleared "
              f"({queue_path.stem}.state.json).")

    print(f"Queue: live-reloading {queue_path} before each job.")
    queue_state = load_queue_state(queue_path)
    if queue_state.completed:
        print(f"Queue: state sidecar records "
              f"{len(queue_state.completed)} previously-completed job(s).")
    if args.json_status is not None:
        # v1.19.0: empty string = "use the default under logs/" (lets queue
        # runners enable status without hard-coding a path). Explicit
        # path always wins.
        if args.json_status == "":
            from encode_modules.log_paths import queue_json_status_default_path
            args.json_status = str(queue_json_status_default_path(queue_path))
        # Truncate up front so a re-run starts a clean per-run status stream.
        try:
            Path(args.json_status).parent.mkdir(parents=True, exist_ok=True)
            open(args.json_status, "w", encoding="utf-8").close()
        except OSError as e:
            sys.exit(f"ERROR: cannot write --json-status file "
                     f"{args.json_status}: {e}")

    # `attempted_inputs` = jobs we've already started (by absolute resolved
    # path), so a job that already ran — ok, failed, threshold-aborted, or
    # skipped — never loops back even if it still appears in queue.json.
    # `seen_inputs` = union of every input observed across reloads; its
    # size is the [i/n] denominator so the count stays stable when the
    # user removes rows mid-run.
    job_reports: list[dict] = []
    attempted_inputs: set[str] = set()
    seen_inputs: set[str] = set()
    last_mtime: float | None = None
    job_counter = 0
    queue_start_monotonic = time.monotonic()

    while True:
        # JSON parse errors mid-edit are tolerated only after we've
        # successfully read it at least once; at startup a bad file is
        # fatal so a user typo doesn't silently kill the queue.
        try:
            cur_mtime, defaults, jobs = reload_queue_with_retry(queue_path)
        except Exception as e:
            if last_mtime is None:
                sys.exit(f"ERROR: failed to read queue: {e}")
            print(f"WARNING: queue.json reload failed ({e}); ending queue.",
                  file=sys.stderr)
            break

        if not jobs:
            if last_mtime is None:
                sys.exit("ERROR: no jobs in queue.")
            print("Queue empty after reload; ending.")
            break

        if last_mtime is not None and cur_mtime != last_mtime:
            print(f"  Queue updated: reloaded {queue_path.name} "
                  f"(now {len(jobs)} job(s)).")
        last_mtime = cur_mtime

        next_job = _pick_next_job(jobs, seen_inputs, attempted_inputs)
        if next_job is None:
            break

        # Mark attempted up-front so a transient failure can't put us in
        # an infinite loop on the same job.
        input_str = str(Path(next_job["input"]).resolve())
        attempted_inputs.add(input_str)
        job_counter += 1
        merged = merge_job(defaults, next_job)

        # Resolve per-job done_dir against the queue file's directory (the
        # spec calls for queue-relative resolution so a queue copied to a
        # different machine doesn't depend on shell cwd). Mutates the merged
        # dict so the resolved absolute path travels into build_compress_argv.
        if merged.get("done_dir"):
            try:
                resolved = resolve_done_dir(str(merged["done_dir"]),
                                            base_dir=queue_path.parent)
                if resolved is not None:
                    merged["done_dir"] = str(resolved)
            except OSError as e:
                print(f"WARNING: done_dir for job {job_counter} could not "
                      f"be created ({e}); dropping for this job.",
                      file=sys.stderr)
                merged.pop("done_dir", None)

        skip_row = _skip_if_missing_or_existing(
            merged, i=job_counter, n=len(seen_inputs),
            no_skip_existing=args.no_skip_existing,
            state=queue_state,
        )
        if skip_row is not None:
            job_reports.append(skip_row)
            if args.json_status:
                _emit_json_status(args.json_status, skip_row)
            continue

        # Overlay X265_QUEUE_* env onto the child encoder process so the
        # on_file_complete hook (which the encoder spawns) inherits the live
        # counters. Per-job scope: restored on the way out of the block, so a
        # subsequent skipped/missing job sees no stale overlay.
        counter_env = compute_queue_counters(
            job_reports,
            total_jobs=len(seen_inputs),
            queue_wall_seconds=time.monotonic() - queue_start_monotonic,
            upcoming_index=job_counter,
        )
        with overlay_env(counter_env):
            # queue_state / queue_path / history_path wire the v1.15.0
            # adaptive CRF-jump escalation (see queue_modules/crf_retry).
            status, row = run_job_with_crf_retry(
                compress_py=compress_py, merged=merged,
                i=job_counter, n=len(seen_inputs),
                queue_state=queue_state, queue_path=queue_path,
                history_path=_history_module.default_history_path(),
            )
        job_reports.append(row)
        if args.json_status:
            _emit_json_status(args.json_status, row)
        # Persist the completion to the state sidecar AFTER the run finishes,
        # so a kill mid-run never records a half-done job as completed.
        # done_dir-moved jobs are recorded with their final paths so a re-run
        # finds them via state lookup (the input path in queue.json no longer
        # exists on disk).
        if status == "ok":
            _record_completion(queue_state, queue_path, row, merged)

        # Queue-side notification: render the full `[OK]`/`[FAILED]`/`[..]`
        # snapshot and fire on_queue_item_end if configured. Best-effort —
        # a notification problem must never abort a queue that may have
        # hours of work left. Fires AFTER the state sidecar is on disk
        # (audit trail before side-effects) and BEFORE the halt check so
        # the user gets a notification for the final job too.
        log_line = dispatch_on_queue_item_end(
            merged=merged, jobs_snapshot=jobs, job_reports=job_reports,
            status=status, row=row,
        )
        if log_line:
            print(log_line, file=sys.stderr)

        if _should_halt_after(status, args.stop_on_failure):
            if status == "stopped-by-user":
                print("Queue halted: stopped by user — re-run to resume.")
            break

    _print_summary_table(job_reports)
    _write_aggregate_reports(skill_dir, queue_path, job_reports)
    return _aggregate_exit_code(job_reports)


if __name__ == "__main__":
    sys.exit(main())

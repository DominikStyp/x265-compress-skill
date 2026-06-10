"""Persist a finished ok job into the queue's state sidecar.

Split out of ``run_queue.py`` (which owns only the orchestration loop) so
the completion-recording cluster — append-to-state plus the move-outcome
verification that keeps the sidecar honest — lives in one cohesive unit.

Three pieces, tightly coupled:
  * ``_record_completion``   — build the state row + flush atomically.
  * ``_verify_move_outcome`` — look at disk truth to decide whether a
    done_dir move actually happened (the encoder exits 0 even when its
    move was refused, so disk is the only honest source).
  * ``_same_dir``            — same-on-disk-directory predicate the
    move-outcome check uses to recognize a no-op done_dir.

These are the queue-side companions to the encoder's done_dir move logic;
they never re-encode, only record what already happened. The underscore
names are preserved verbatim from run_queue.py: tests reference
``run_queue._verify_move_outcome`` / ``run_queue._skip_if_missing_or_existing``
via run_queue's re-imports, so the public surface is unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

from queue_modules.job_schema import derive_output_path


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

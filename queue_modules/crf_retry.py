"""CRF-escalation loop — the `retry_with_bigger_crf` machinery, extracted
from `job_runner.py` so neither module exceeds the project's 500-line cap.

Owns the escalation policy: when a job stops on the size guard
(`stopped-threshold`) and the user has opted in via `retry_with_bigger_crf`,
re-encode the SAME source at a higher CRF until it fits under
`max_size_percent` or `crf_max` is reached.

Two escalation modes:

  crf_jump: false (default)
      Classic `+crf_step` walk. Byte-identical to the v1.13.x behaviour
      when none of the v1.15.0 keys are set.

  crf_jump: true
      Adaptive — reads the encoder's projection from the just-written
      `encoding_history.jsonl` row and jumps `ceil(K · log2(P/T))` CRF
      points in one shot. Collapses 3–4 probe encodes to ~1.

Cross-restart resume: when `queue_state` is provided, every
threshold-stop persists `(last_crf_tried, last_projected_pct,
last_threshold_pct, attempts)` to `<queue_stem>.state.json`'s
`in_progress_escalations` field. A restart resumes at
`last_crf_tried + step`, not at the configured CRF — the
`23->24->[restart]->23->24->25` replay the spec calls out is eliminated.
On `ok` (or any non-threshold terminal status) the entry is cleared so
a fresh future re-attempt starts from the user's configured CRF.

Floor detector: two consecutive over-threshold probes with diminishing
returns (Δ < `crf_floor_min_gain` % points) emit
`stopped-threshold-crf-exhausted` immediately, instead of walking the
whole `+crf_step` ladder to `crf_max` on a source that physically
won't compress further (the spec's Alyssa 21→22 85.3→85.2 case).

References to `run_one_job` and `supersede_encoded_chunks` go through
`from . import job_runner` (NOT direct symbol imports) so the existing
`tests/test_crf_retry.py` monkey-patches of those names on the
job_runner module keep working unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import job_runner
from .crf_jump import (
    DEFAULT_K, DEFAULT_MARGIN, calibrate_k, compute_next_crf,
    is_floor_bound, read_last_projection,
)
from .job_schema import derive_workdir


# Re-export the only constant tests import from this module. The
# DEFAULT_CRF_STEP / DEFAULT_CRF_MAX defaults stay in job_runner — the
# retry loop reads them through the module attribute so a test that
# patches `job_runner.DEFAULT_*` (none today, but a defensible future
# pattern) still works.
CRF_EXHAUSTED_STATUS = job_runner.CRF_EXHAUSTED_STATUS

# Default min-gain for the floor detector. Tunable per-job via
# `crf_floor_min_gain` in queue.json; 0 disables. 2.0 % points matches
# the spec's recommendation and is conservative enough to never fire
# on a normal escalation (Emily 19→20 moves 13.2 pt — well clear).
DEFAULT_FLOOR_MIN_GAIN = 2.0


def _coerce_int(value, *, default: int, name: str) -> Optional[int]:
    """Coerce a queue.json value to int, or warn-and-return-None.
    Centralises the loudly-but-safely pattern the retry loop needs at
    every config touchpoint (crf_step / crf_max / crf_jump_k)."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        print(f"  retry_with_bigger_crf: invalid {name} in queue.json "
              f"({value!r}); not escalating.", file=sys.stderr)
        return None


def _resolve_k(merged: dict, history: list[tuple[int, float]]) -> float:
    """Resolve `crf_jump_k` from the merged job dict.

    Values:
      `"auto"`     → calibrate from the LAST two probes in `history`
                    (list of `(crf, projected_pct)` in chronological
                    order). Using the last two — not the first two —
                    lets K adapt as the source approaches its
                    compression floor (large K_obs near the floor
                    naturally produces smaller subsequent jumps). With
                    fewer than two probes the calibration falls back
                    to DEFAULT_K.
      numeric      → used verbatim (cast to float).
      anything else → loud warn + fall back to DEFAULT_K. Matches the
                    `_coerce_int` loud-fail discipline so a typo like
                    `"siz"` surfaces rather than silently disabling
                    calibration.

    The K_CLAMP_MIN/MAX bounds in `calibrate_k` keep auto-K from going
    wild on floor cases (very-large K_obs clamped to K_CLAMP_MAX).
    """
    raw = merged.get("crf_jump_k", DEFAULT_K)
    if isinstance(raw, str) and raw.lower() == "auto":
        if len(history) >= 2:
            crf1, pct1 = history[-2]
            crf2, pct2 = history[-1]
            return calibrate_k(crf1, pct1, crf2, pct2)
        return DEFAULT_K
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"  retry_with_bigger_crf: invalid crf_jump_k in "
              f"queue.json ({raw!r}); using default K={DEFAULT_K}.",
              file=sys.stderr)
        return DEFAULT_K


def _seed_crf_from_state(merged: dict, source: Path,
                         queue_state, crf_step: int
                         ) -> tuple[dict, int]:
    """Resume CRF-escalation across restarts.

    If `queue_state` carries an in-progress escalation for `source`,
    seed the starting CRF as `max(configured_crf, last_crf_tried +
    step)`. This eliminates the `23->24->[restart]->23->24->25` replay
    visible in the spec's history table — a restart resumes at 25, not 23.

    The `max(...)` honours a user who manually raised the configured
    CRF between runs (their new floor wins over our stale state).

    Returns `(possibly_modified_merged, attempts_so_far)`. attempts_so_far
    is the running counter we'll keep updating in the retry loop.
    """
    if queue_state is None:
        return merged, 0
    rec = queue_state.get_escalation(source)
    if not rec:
        return merged, 0
    last_crf = rec.get("last_crf_tried")
    attempts = int(rec.get("attempts", 0) or 0)
    if not isinstance(last_crf, int):
        return merged, attempts
    configured = merged.get("crf")
    resume_crf = int(last_crf) + crf_step
    if isinstance(configured, int) and configured > resume_crf:
        return merged, attempts
    # UX guard: if the user LOWERED `crf` in queue.json between runs
    # (wanting a higher-quality re-attempt), stale state forces the
    # higher resume CRF. The print discloses this so the user knows
    # to `--reset-state` if they really meant the lower value.
    lower_note = ""
    if isinstance(configured, int) and configured < resume_crf:
        lower_note = (f" (NOTE: configured CRF is {configured}; resume "
                      f"ignores it - pass --reset-state to honour the "
                      f"lowered config)")
    print(f"  retry_with_bigger_crf: resuming escalation at CRF "
          f"{resume_crf} (last tried {last_crf}, {attempts} attempt(s) "
          f"persisted){lower_note}.")
    return {**merged, "crf": resume_crf}, attempts


def run_job_with_crf_retry(*, compress_py: Path, merged: dict,
                          i: int, n: int,
                          queue_state=None,
                          queue_path: Optional[Path] = None,
                          history_path: Optional[Path] = None
                          ) -> tuple[str, dict]:
    """run_one_job, plus the opt-in `retry_with_bigger_crf` escalation.

    See module docstring for the full design. Signature additions vs
    v1.13.x:
      * `queue_state` / `queue_path` — when both are provided, the
        retry loop persists / clears the in-progress escalation record
        in the state sidecar so a restart resumes mid-escalation.
        Passing them is what enables Fix 2 of the spec.
      * `history_path` — where the encoder's audit JSONL lives; the
        adaptive-jump mode reads each just-finished probe's projection
        from there. Pass `None` to force the classic `+crf_step` walk
        even when `crf_jump: true` is set (defensive — never crash
        because of a missing history path).
    """
    # Coerce loudly-but-safely: a typo'd crf_step/crf_max in queue.json
    # must not crash the queue mid-run after attempt 1 already encoded.
    # Bail to no-escalation (single probe + return) the same way a
    # missing CRF does.
    crf_step = _coerce_int(
        merged.get("crf_step", job_runner.DEFAULT_CRF_STEP),
        default=job_runner.DEFAULT_CRF_STEP, name="crf_step")
    crf_max_v = _coerce_int(
        merged.get("crf_max", job_runner.DEFAULT_CRF_MAX),
        default=job_runner.DEFAULT_CRF_MAX, name="crf_max")
    if crf_step is None or crf_max_v is None:
        return job_runner.run_one_job(
            compress_py=compress_py, merged=merged, i=i, n=n)
    crf_step = max(1, crf_step)
    crf_max = crf_max_v

    source = Path(merged["input"])
    use_jump = bool(merged.get("crf_jump", False))
    margin = float(merged.get("crf_jump_margin", DEFAULT_MARGIN))
    min_gain = float(merged.get("crf_floor_min_gain",
                                DEFAULT_FLOOR_MIN_GAIN))

    # Resume across restarts BEFORE the first probe — but only when
    # retry_with_bigger_crf is on (no point resuming an escalation the
    # user has disabled).
    if merged.get("retry_with_bigger_crf"):
        merged, attempts = _seed_crf_from_state(
            merged, source, queue_state, crf_step)
    else:
        attempts = 0

    status, row = job_runner.run_one_job(
        compress_py=compress_py, merged=merged, i=i, n=n)
    if not merged.get("retry_with_bigger_crf"):
        return status, row

    workdir = derive_workdir(source)
    # This job's (crf, projected_pct) probes for K calibration + the
    # floor detector. First attempt populates [0]; each retry appends.
    probes: list[tuple[int, float]] = []

    while status == "stopped-threshold":
        used_crf = row.get("crf")
        if used_crf is None:
            print("  retry_with_bigger_crf: could not determine the CRF used; "
                  "not escalating.")
            _clear_state_if_present(queue_state, queue_path, source)
            return status, row
        used_crf = int(used_crf)
        attempts += 1

        # Projection from the encoder's just-flushed JSONL row. Falls
        # back to None if the encoder didn't write the new fields
        # (pre-v1.15 records or a malformed write) — in that case the
        # classic `+crf_step` path takes over for this probe.
        projection = (read_last_projection(history_path, source)
                      if history_path is not None else None)
        if projection is not None:
            probes.append((projection.crf, projection.projected_pct))

        # Floor detector — fires WHENEVER we have two consecutive
        # over-threshold probes with projection data, regardless of
        # whether crf_jump is on. The spec lists Fix 3 as independent
        # of Fix 1: a user on the blind `+crf_step` walk who sets
        # `crf_floor_min_gain` still benefits from early-stopping a
        # floor-bound source (saves walking the whole ladder to
        # crf_max). `min_gain=0` is the explicit opt-out.
        #
        # `projection is not None` is guaranteed by `len(probes) >= 2`
        # (probes is only appended when a projection was read), but the
        # threshold_pct comes off the LATEST projection — the encoder's
        # own threshold doesn't change between probes, so this is the
        # honest cap to feed the detector.
        if (min_gain > 0 and len(probes) >= 2
                and projection is not None):
            prev_pct = probes[-2][1]
            cur_pct = probes[-1][1]
            if is_floor_bound(prev_pct=prev_pct, current_pct=cur_pct,
                              max_size_pct=projection.threshold_pct,
                              min_gain=min_gain):
                # ASCII-only print — the queue's enable_utf8_io is only
                # active in the main() entry point; a unittest invocation
                # of this loop runs on the platform default codec, which
                # on Windows is cp1250 and refuses Greek letters / em
                # dashes. Stay ASCII so the message lands intact across
                # every CI environment.
                print(f"  retry_with_bigger_crf: source at compression "
                      f"floor (projected {prev_pct:.1f}% -> "
                      f"{cur_pct:.1f}% over {len(probes)} probes; gain "
                      f"{prev_pct - cur_pct:.1f} pt < {min_gain:.1f} pt). "
                      f"Stopping as {CRF_EXHAUSTED_STATUS}.")
                row["status"] = CRF_EXHAUSTED_STATUS
                _clear_state_if_present(queue_state, queue_path, source)
                return CRF_EXHAUSTED_STATUS, row

        next_crf = _decide_next_crf(
            used_crf=used_crf, projection=projection, probes=probes,
            merged=merged, use_jump=use_jump, margin=margin,
            crf_step=crf_step, crf_max=crf_max)

        # Exhausted check: either the next CRF would exceed crf_max
        # (blind-walk-style boundary), OR it didn't advance past
        # used_crf (jump-style boundary — `compute_next_crf` clamps to
        # `crf_max`, so once we've tried crf_max itself, the clamp
        # produces `next_crf == used_crf` and the loop would spin).
        # The `<= used_crf` check catches both cases for the jump path.
        if next_crf > crf_max or next_crf <= used_crf:
            print(f"  size guard still hit at CRF {used_crf}; CRF cap "
                  f"{crf_max} reached - giving up ({CRF_EXHAUSTED_STATUS}).")
            row["status"] = CRF_EXHAUSTED_STATUS
            _clear_state_if_present(queue_state, queue_path, source)
            return CRF_EXHAUSTED_STATUS, row

        # Persist the escalation state BEFORE the next probe so a kill
        # mid-encode resumes from the right place. Best-effort: the
        # save failure is logged but doesn't abort the queue.
        if projection is not None:
            _persist_state_if_present(
                queue_state, queue_path, source,
                last_crf=used_crf,
                last_pct=projection.projected_pct,
                last_threshold=projection.threshold_pct,
                attempts=attempts)

        moved = job_runner.supersede_encoded_chunks(workdir, used_crf)
        mode = "jump" if use_jump and projection is not None else "step"
        print(f"  size guard hit at CRF {used_crf}; {mode} -> CRF "
              f"{next_crf} (cap {crf_max}; set aside {moved} encoded "
              f"chunk(s)).")
        status, row = job_runner.run_one_job(
            compress_py=compress_py,
            merged={**merged, "crf": next_crf}, i=i, n=n)

    # Non-threshold terminal status — clear any in-progress escalation
    # state (the source either succeeded, was stopped by user, or
    # failed in a non-retry-recoverable way).
    _clear_state_if_present(queue_state, queue_path, source)
    return status, row


def _decide_next_crf(*, used_crf: int, projection, probes: list,
                     merged: dict, use_jump: bool, margin: float,
                     crf_step: int, crf_max: int) -> int:
    """Pick the next CRF to try. `crf_jump` + a valid projection -> the
    computed jump. Anything else (jump disabled, or missing projection)
    -> the classic `+crf_step` walk."""
    if use_jump and projection is not None:
        k = _resolve_k(merged, probes)
        proposal = compute_next_crf(
            current_crf=used_crf,
            projected_pct=projection.projected_pct,
            max_size_pct=projection.threshold_pct,
            margin=margin, k=k, crf_step=crf_step, crf_max=crf_max)
        if proposal is not None:
            return proposal
    return used_crf + crf_step


def _persist_state_if_present(queue_state, queue_path, source: Path, *,
                              last_crf: int, last_pct: float,
                              last_threshold: float, attempts: int) -> None:
    """Best-effort: record the just-failed probe to the state sidecar.
    A save failure is logged but never aborts the queue — the escalation
    will still proceed in-memory; only the cross-restart resume is at
    risk if state is lost."""
    if queue_state is None or queue_path is None:
        return
    try:
        queue_state.set_escalation(
            input_path=source, last_crf_tried=last_crf,
            last_projected_pct=last_pct,
            last_threshold_pct=last_threshold, attempts=attempts)
        queue_state.save_atomically(queue_path)
    except (OSError, ValueError, TypeError) as e:
        print(f"  WARNING: failed to persist escalation state: {e}",
              file=sys.stderr)


def _clear_state_if_present(queue_state, queue_path, source: Path) -> None:
    """Drop the in-progress entry for `source`. Best-effort — a clear
    failure is logged but never aborts the queue."""
    if queue_state is None or queue_path is None:
        return
    try:
        if queue_state.get_escalation(source) is None:
            return
        queue_state.clear_escalation(source)
        queue_state.save_atomically(queue_path)
    except (OSError, ValueError, TypeError) as e:
        print(f"  WARNING: failed to clear escalation state: {e}",
              file=sys.stderr)

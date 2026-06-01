"""Adaptive CRF-jump estimation — pure math + projection reader + floor
detector for the `retry_with_bigger_crf` escalation loop.

The classic loop walks `+crf_step` per probe: when the configured CRF
is far from the size-feasible CRF, 3–4 probe encodes are wasted just
to re-project. This module computes the next CRF in one shot using
x265's known approximately-exponential rate-control behaviour:

    size_ratio ≈ 2^(-ΔCRF / K)
    ⇒ next_crf = c + ceil(K · log2(P / T))

where c is the just-tried CRF, P is the projected % of source at c,
and T = max_size_percent − margin (a small target buffer so the next
attempt actually lands under the hard cap, not on it).

Median K in real history ≈ 6 (matches the textbook value); outliers
> 15 are the "compression floor" case where the source is already
near-optimal and extra CRF barely shrinks it — those should be stopped
as `stopped-threshold-crf-exhausted`, not stepped to crf_max one at a
time.

Spec: ../TO_ENCODE_AI/feature-request_adaptive_crf_jump_estimation.md
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Default rate constant (x265 CRF points per output halving). Used when
# the user passes a numeric `crf_jump_k`; matches the textbook value
# AND the median observed across the user's own historical encodes.
DEFAULT_K = 6.0

# Default safety margin under max_size_percent (in % points). Wider
# margin = larger jumps = lands feasible in fewer probes at the cost
# of mildly more compression than strictly necessary.
DEFAULT_MARGIN = 5.0

# When `crf_jump_k: "auto"`, K is calibrated per-job from the first two
# projections (`K_obs = -ΔCRF / log2(P2/P1)`). The clamp guards against
# two pathological inputs:
#   * very small K_obs → next jump overshoots wildly (skip past feasible)
#   * very large K_obs → next jump is ~zero (we'd re-walk one-step)
# Both extremes get capped to the bulk-range observed in real data.
K_CLAMP_MIN = 4.0
K_CLAMP_MAX = 9.0


def compute_next_crf(*, current_crf: int, projected_pct: float,
                     max_size_pct: float, margin: float,
                     k: float, crf_step: int, crf_max: int
                     ) -> Optional[int]:
    """Return the next CRF to try, or None when no escalation is needed.

    Args:
        current_crf:    the CRF the just-finished probe ran at
        projected_pct:  the projected output as % of source at that CRF
        max_size_pct:   the user's hard cap (--max-size-percent)
        margin:         aim this many % points UNDER the hard cap
        k:              x265 CRF points per output halving (≈6 default)
        crf_step:       minimum step size (the floor — keeps existing
                        `crf_step` semantics intact)
        crf_max:        upper bound on escalation (the ceiling)

    Returns:
        next CRF to try, or None if the projection is already below
        the target (caller should not be escalating in that case).

    The target T = max(1, max_size_pct − margin) is floored at 1% so a
    pathological `margin > max_size_pct` configuration doesn't make
    `log2(P/T)` blow up.
    """
    target = max(1.0, max_size_pct - margin)
    # No-op signal — the projection already meets the target. Caller
    # detects "no escalation needed" via the None return rather than
    # us emitting a zero-step.
    if projected_pct <= target:
        return None
    # Exact jump as per the rate-control model, then ceil to land at an
    # integer CRF >= the mathematical answer.
    jump_exact = k * math.log2(projected_pct / target)
    jump = max(crf_step, math.ceil(jump_exact))
    return min(crf_max, current_crf + jump)


def calibrate_k(crf1: int, pct1: float, crf2: int, pct2: float) -> float:
    """K_obs from two consecutive projections in the SAME job.

    K_obs = -ΔCRF / log2(P2 / P1)

    Sign convention: a higher CRF (crf2 > crf1) must yield a smaller
    projection (pct2 < pct1) for the formula to be physically
    meaningful. Inverted or zero-ratio inputs fall back to DEFAULT_K
    rather than producing nonsense; large outliers (floor cases) and
    tiny outliers (over-fit) get clamped to [K_CLAMP_MIN, K_CLAMP_MAX].
    """
    if crf1 == crf2 or pct1 <= 0 or pct2 <= 0:
        return DEFAULT_K
    if pct2 >= pct1:
        # Negative or zero K_obs — invalid signal (probably noise near
        # the compression floor or a regressed projection). Fall back.
        return DEFAULT_K
    try:
        k_obs = -(crf2 - crf1) / math.log2(pct2 / pct1)
    except (ValueError, ZeroDivisionError):
        return DEFAULT_K
    return max(K_CLAMP_MIN, min(K_CLAMP_MAX, k_obs))


def is_floor_bound(*, prev_pct: float, current_pct: float,
                   max_size_pct: float, min_gain: float) -> bool:
    """Diminishing-returns detector. True iff the source is at its
    compression floor (won't shrink meaningfully no matter what CRF
    we throw at it).

    Trigger: two consecutive probes both STILL over the cap, AND the
    second probe improved by strictly less than `min_gain` % points
    (so a 2-point gain at min_gain=2.0 is NOT floor — borderline).

    `min_gain=0` disables the detector entirely (opt-out).

    Negative gain (current_pct >= prev_pct) is counted as floor —
    the higher CRF didn't even help, so further escalation is wasted.
    """
    if min_gain <= 0:
        return False
    # If we LANDED under the cap, the job succeeded — never floor.
    if current_pct <= max_size_pct:
        return False
    gain = prev_pct - current_pct
    return gain < min_gain


@dataclass(frozen=True)
class HistoryProjection:
    """The handful of fields the queue runner needs from the last
    threshold-abort history record for a given source.

    `projected_pct` / `threshold_pct` are surfaced as floats (the % of
    source) because that's what the jump formula consumes. The encoder
    writes both the raw byte counts AND the pre-computed pct values to
    keep this layer free of size-arithmetic concerns."""

    crf: int
    projected_pct: float
    threshold_pct: float


def read_last_projection(history_path: Path, source_path: Path
                         ) -> Optional[HistoryProjection]:
    """Find the most recent threshold-abort record in
    `encoding_history.jsonl` for `source_path` and extract the CRF +
    projected_pct + threshold_pct.

    Returns None when:
      * the history file is missing or unreadable
      * no record matches `source_path`
      * matching records don't carry the projection fields (an `ok`
        record, or a pre-v1.15 record before the encoder wrote them)

    Path matching is by exact string equality of the resolved absolute
    path (the encoder writes the source's `Path.resolve()` into the
    record, and we resolve `source_path` the same way for comparison).

    Degrades silently per the project's never-crash-the-queue
    discipline. Corrupt lines are skipped, never raised.
    """
    if not history_path.is_file():
        return None
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return None
    target_key = _resolve_str(source_path)
    last_match: Optional[HistoryProjection] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        input_block = rec.get("input") or {}
        rec_path = input_block.get("path")
        if not isinstance(rec_path, str):
            continue
        try:
            rec_key = str(Path(rec_path).resolve())
        except OSError:
            rec_key = rec_path
        if rec_key != target_key:
            continue
        projection = _projection_from_record(rec)
        if projection is not None:
            last_match = projection
    return last_match


def _projection_from_record(rec: dict) -> Optional[HistoryProjection]:
    """Pull (crf, projected_pct, threshold_pct) out of one record, or
    None when the record doesn't carry the projection fields (success
    paths, pre-v1.15 records, partial writes)."""
    output = rec.get("output") or {}
    settings = rec.get("settings") or {}
    crf = settings.get("crf")
    p_pct = output.get("projected_pct")
    t_pct = output.get("threshold_pct")
    if not isinstance(crf, (int, float)):
        return None
    if not isinstance(p_pct, (int, float)) or p_pct <= 0:
        return None
    if not isinstance(t_pct, (int, float)) or t_pct <= 0:
        return None
    return HistoryProjection(crf=int(crf), projected_pct=float(p_pct),
                             threshold_pct=float(t_pct))


def _resolve_str(p: Path) -> str:
    """Resolve to absolute string. Degrade to str(p) if resolve raises
    (e.g. the source was unlinked between queue iteration and
    projection lookup)."""
    try:
        return str(p.resolve())
    except OSError:
        return str(p)

"""Shared trailing-window endpoint scan for the live-rate display and the
choke detector.

Both compute a delta over a trailing time window from the same
`(t, out_time_s, frame)` samples deque, and both did so with a byte-identical
"find the oldest sample still inside the window, else fall back to the very
oldest" loop — the exact copy-paste AGENTS.md says to extract. They differ only
in the window ANCHOR (the live display anchors on the newest sample's
timestamp; the choke detector anchors on the current monotonic clock), so that
is passed in as `window_start` rather than computed here.

No project imports → safe for both `display` and `choke_detection` to import at
module top without the circular dependency those two otherwise have.
"""
from __future__ import annotations

from typing import Sequence, Tuple

Sample = Tuple[float, float, float]


def select_window_endpoints(samples_list: Sequence[Sample],
                            window_start: float) -> Tuple[Sample, Sample]:
    """Return ``(older, newer)`` endpoints for a trailing window.

    `newer` is the most recent sample. `older` is the first sample whose
    timestamp is at or after `window_start`; if none qualify (the window is
    narrower than the gap to the newest sample) it falls back to the oldest
    sample so the caller always has two points to difference.

    The caller guarantees ``len(samples_list) >= 2`` (both sites bail to a
    "?"/no-verdict path on fewer)."""
    older: Sample | None = None
    for sample in samples_list:
        if sample[0] >= window_start:
            older = sample
            break
    if older is None:
        older = samples_list[0]
    return older, samples_list[-1]

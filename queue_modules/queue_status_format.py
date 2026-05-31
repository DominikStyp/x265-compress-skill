"""Render the queue-status summary the on_queue_item_end hook ships in
`X265_QUEUE_STATUS_SUMMARY`.

Pure renderer + status-to-marker classifier. Consumed by run_queue.py when
firing the queue-side notification hook, and exposed for tests/notifiers
that want to recreate the same marker mapping without re-implementing it.

Markers are deliberately just three (OK / FAILED / pending), per the
feature spec — a notification body wants a quick-glance overview, not the
full --status table. Anyone needing the rich classification (PROCESSING,
DONE, MISSING INPUT, QUEUED with notes) should use `run_queue.py --status`
instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence


OK_MARKER = "[OK]"
FAILED_MARKER = "[FAILED]"
PENDING_MARKER = "[..]"

# Statuses that ARE the user's goal (output is on disk). Anything else
# terminal is a failure for queue-summary purposes — including the
# "soft" stops (stopped-threshold, awaiting-chunk-fix) that, while not
# a crash, did NOT produce a usable file.
_OK_STATUSES = frozenset({"ok", "skipped-done", "skipped-exists"})


def classify_marker(status: Optional[str]) -> str:
    """status string -> `[OK]` / `[FAILED]` / `[..]`.

    Pending is `None` (the job hasn't been attempted yet). Any unknown
    status maps to FAILED — fail-safe: a new status added upstream should
    surface in notifications as a failure rather than silently looking
    healthy. The user can refine the mapping later if a new "soft" status
    deserves a different marker.
    """
    if status is None:
        return PENDING_MARKER
    if status in _OK_STATUSES:
        return OK_MARKER
    return FAILED_MARKER


def render_queue_summary(snapshot: Sequence[str],
                         reports_by_input: Mapping[str, dict]) -> str:
    """Render the snapshot as one line per job, in snapshot order.

    `snapshot` is an ordered sequence of resolved-absolute input paths
    (the queue runner already resolves these via `Path.resolve()`).
    `reports_by_input` is a `{resolved_input -> row}` lookup of jobs
    already attempted in the current run; rows must carry a `status`
    string. Lookup is by exact string match — keys must match the
    snapshot's resolution exactly (the queue runner uses the same
    `Path(...).resolve()` call on both sides, so this holds).

    Format:

        [ 1] [OK]    short_name.mp4
        [ 2] [OK]    another.mp4
        [ 3] [FAILED] broken.mp4
        [ 4] [..]    pending.mp4

    Index width auto-scales to the total count (12 jobs -> 2-digit
    padding) so a long queue still aligns. Marker column is padded to the
    longest marker so filenames line up regardless of which marker each
    row uses.

    Returns empty string for an empty snapshot — callers fire the hook
    with an empty summary rather than skipping the event, so a notifier
    still gets the per-job context (status, source, output) even on the
    first job of a one-job queue.
    """
    if not snapshot:
        return ""
    n = len(snapshot)
    idx_width = len(str(n))
    marker_width = max(len(OK_MARKER), len(FAILED_MARKER), len(PENDING_MARKER))
    lines: list[str] = []
    for i, input_path in enumerate(snapshot, 1):
        row = reports_by_input.get(input_path)
        status = row.get("status") if row else None
        marker = classify_marker(status)
        name = Path(input_path).name
        lines.append(
            f"[{i:>{idx_width}}] {marker:<{marker_width}} {name}")
    return "\n".join(lines)

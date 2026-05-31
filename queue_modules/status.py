"""Read-only `run_queue.py --status` inspector — single-table view of every
job in a queue.json.

Reconciles four data sources so the user doesn't have to:

  * queue.json          → the list of jobs and configured settings
  * encoding_history.jsonl → "what was the last run for this input?"
  * <workdir>/.tmp/.compress_<stem>/ → "is anything mid-encode?"
  * the output .mkv (and queue_state sidecar) → "is it done?"

Classification (in order; first match wins):
  1. DONE          — output `.mkv` exists OR state sidecar has a completion
                     record (handles done_dir-moved sources)
  2. MISSING INPUT — input not at the queue-listed path AND no completion
                     in state AND no output on disk
  3. PROCESSING    — workdir contains ≥1 enc_src_*.mkv AND the most recent
                     history record has status `in_progress` AND the
                     workdir was touched within the liveness window
  4. QUEUED        — everything else, including:
                       * a stale `in_progress` record (history says
                         in_progress but workdir hasn't been touched in
                         >LIVENESS_WINDOW)
                       * stopped-threshold-crf-exhausted (carries the last
                         tried CRF in Notes)
                       * any other historical non-ok terminal status

The PID-liveness check the original feature request asked for is approximated
via filesystem mtime — checking PIDs portably requires either psutil (which
the skill avoids as a runtime dep) or recording an encoder.pid sidecar (new
state to maintain). mtime is stdlib, cross-OS, and catches the common case
(crashed run → no recent chunk file writes → stale).

Side effects: zero. Reading workdir size is `stat()`, not `du`. Reading
history is one `read_text` pass over the JSONL.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# How long can a workdir go untouched before we call its in_progress flag
# stale? 15 minutes covers: a parallel encoder finishing a slow 4K chunk
# (10 min worst case), plus headroom. Tunable via env if a slow-storage user
# needs more.
LIVENESS_WINDOW_SEC = 15 * 60

from .job_schema import derive_output_path, derive_workdir, expand_jobs, merge_job
from .queue_io import reload_queue_with_retry
from .queue_state import QueueState, load_queue_state


@dataclass
class StatusRow:
    """One row in the status table — fully populated for the display layer
    to format. Optional fields are None when not applicable (e.g. wall_seconds
    on a QUEUED job)."""
    index: int
    name: str
    status: str
    crf_chain: str
    source_bytes: Optional[int]
    output_bytes: Optional[int]
    saved_pct: Optional[float]
    wall_seconds: Optional[float]
    notes: str


def history_by_input(history_path: Path) -> dict[str, dict]:
    """Return {resolved input path → most-recent history record} for fast
    classify-time lookup. encoding_history.jsonl is append-only, so the LAST
    record for a given input is the current truth.

    Degrades silently (empty dict) on any read error — the status inspector
    must never crash because of a malformed history line."""
    if not history_path.exists():
        return {}
    by: dict[str, dict] = {}
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        input_block = rec.get("input") or {}
        path = input_block.get("path")
        if not isinstance(path, str):
            continue
        try:
            key = str(Path(path).resolve())
        except OSError:
            key = path
        by[key] = rec
    return by


def classify(job: dict, *, queue_dir: Path,
             history: dict[str, dict],
             state: QueueState) -> StatusRow:
    """Classify one job → StatusRow. `queue_dir` is the directory containing
    queue.json; relative job inputs are resolved against it. `history` is the
    pre-built {input → record} dict; `state` is the queue's persistent sidecar.
    """
    input_str = str(job.get("input", ""))
    input_path = Path(input_str)
    if not input_path.is_absolute():
        input_path = queue_dir / input_path
    name = input_path.name
    history_rec = history.get(_key(input_path))

    # DONE via state sidecar (handles done_dir-moved sources): the input
    # may no longer exist at the queue-listed path, but the state's record
    # carries the bytes/wall/crf from the moved location.
    rec = state.get(input_path)
    if rec is not None:
        return _row_from_state(input_path, name, rec)

    # DONE via output on disk — checked BEFORE the input-missing branch so
    # a user who deleted the source but kept the output still sees DONE.
    out_path = derive_output_path(input_path)
    if out_path.exists():
        src_bytes = (input_path.stat().st_size
                     if input_path.is_file() else None)
        return _row_from_output_on_disk(name, input_path, out_path,
                                        history_rec, src_bytes)

    # MISSING INPUT — input not on disk, no state record, no output. Surface
    # separately from QUEUED so the user notices.
    if not input_path.is_file():
        return StatusRow(index=0, name=name, status="MISSING INPUT",
                         crf_chain="", source_bytes=None, output_bytes=None,
                         saved_pct=None, wall_seconds=None,
                         notes="input file not found at the listed path")

    src_bytes = input_path.stat().st_size

    # PROCESSING vs QUEUED depends on workdir state + history's last verdict
    # + filesystem mtime (liveness approximation).
    workdir = derive_workdir(input_path)
    chunks_done = _count_encoded_chunks(workdir)
    last_status = (history_rec or {}).get("status")

    if (chunks_done and history_rec
            and last_status == "in_progress"
            and _is_workdir_live(workdir)):
        return _row_processing(name, input_path, src_bytes, chunks_done,
                               history_rec)

    # CRF-exhausted is a distinct terminal state — surface it explicitly so
    # the user knows the job isn't waiting on fresh work, the CRF chain just
    # ran out.
    if last_status == "stopped-threshold-crf-exhausted":
        last_crf = (history_rec.get("settings") or {}).get("crf")
        crf_part = f" (last tried CRF {last_crf})" if last_crf else ""
        return StatusRow(
            index=0, name=name, status="QUEUED",
            crf_chain=_starting_crf(job),
            source_bytes=src_bytes, output_bytes=None,
            saved_pct=None, wall_seconds=None,
            notes=f"crf-exhausted{crf_part}")

    if chunks_done:
        # Workdir has chunks but the encode isn't alive (no in_progress, or
        # in_progress but mtime stale). Don't claim PROCESSING.
        status_note = last_status or "unknown"
        stale_marker = (" — stale in_progress (workdir mtime older than "
                        f"{LIVENESS_WINDOW_SEC // 60} min)"
                        if last_status == "in_progress" else "")
        return StatusRow(
            index=0, name=name, status="QUEUED",
            crf_chain=_starting_crf(job),
            source_bytes=src_bytes, output_bytes=None,
            saved_pct=None, wall_seconds=None,
            notes=(f"stale workdir from prior run "
                   f"(last status: {status_note}, "
                   f"{chunks_done} chunks present){stale_marker}"))
    return StatusRow(index=0, name=name, status="QUEUED",
                     crf_chain=_starting_crf(job),
                     source_bytes=src_bytes, output_bytes=None,
                     saved_pct=None, wall_seconds=None,
                     notes="")


def render_table(rows: list[StatusRow], *, totals: bool = True) -> str:
    """Render rows as a fixed-column markdown table. Wide-name column auto-
    sizes to the longest filename (capped at 60 chars to keep the table
    one-screen-wide on a typical terminal)."""
    if not rows:
        return "(queue is empty)\n"
    name_w = min(60, max(len("File"), max(len(r.name) for r in rows)))
    lines = []
    lines.append(
        f"| # | {'File':<{name_w}} | Status         | CRF        | "
        f"Source     | Output     | Saved  | Wall      | Notes")
    lines.append("|---|" + "-" * (name_w + 2) + "|----------------|"
                 "------------|------------|------------|--------|"
                 "-----------|" + "-" * 6)
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i:>1} | {r.name[:name_w]:<{name_w}} | "
            f"{r.status:<14} | {r.crf_chain:<10} | "
            f"{_fmt_bytes(r.source_bytes):>10} | "
            f"{_fmt_bytes(r.output_bytes):>10} | "
            f"{_fmt_pct(r.saved_pct):>6} | "
            f"{_fmt_wall(r.wall_seconds):>9} | {r.notes}")
    if totals:
        lines.append("")
        lines.append(_totals_line(rows))
    return "\n".join(lines) + "\n"


def render_json(rows: list[StatusRow]) -> str:
    """Machine-readable equivalent — `--status --json` consumer."""
    return json.dumps([asdict(r) for r in rows], indent=2)


# --- Helpers ----------------------------------------------------------------

def _key(p: Path) -> str:
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _count_encoded_chunks(workdir: Path) -> int:
    if not workdir.is_dir():
        return 0
    # Same glob the encoder's resume logic uses — counts only finalized
    # chunks, not the in-flight .part.mkv.
    return sum(1 for p in workdir.glob("enc_src_*.mkv")
               if ".part" not in p.suffixes)


def _starting_crf(job: dict) -> str:
    crf = job.get("crf")
    return f"{crf} (start)" if crf is not None else "(auto)"


def _row_from_output_on_disk(name: str, input_path: Path, out_path: Path,
                             history_rec: Optional[dict],
                             src_bytes: Optional[int]) -> StatusRow:
    out_bytes = out_path.stat().st_size
    saved = ((src_bytes - out_bytes) / src_bytes * 100.0
             if src_bytes else None)
    crf_chain = ""
    wall = None
    notes = "output present" if input_path.exists() else "output present (source missing)"
    if history_rec:
        crf_chain = str((history_rec.get("settings") or {}).get("crf", ""))
        wall = history_rec.get("wall_seconds")
    return StatusRow(index=0, name=name, status="DONE",
                     crf_chain=crf_chain,
                     source_bytes=src_bytes, output_bytes=out_bytes,
                     saved_pct=saved, wall_seconds=wall, notes=notes)


def _row_from_state(input_path: Path, name: str, rec: dict) -> StatusRow:
    bytes_in = rec.get("bytes_in")
    bytes_out = rec.get("bytes_out")
    saved = ((bytes_in - bytes_out) / bytes_in * 100.0
             if bytes_in and bytes_out is not None else None)
    moved_to = rec.get("moved_to_dir")
    notes = (f"moved to {moved_to}" if moved_to
             else "completed in place (per state sidecar)")
    return StatusRow(index=0, name=name, status="DONE",
                     crf_chain=str(rec.get("crf_final", "")),
                     source_bytes=bytes_in, output_bytes=bytes_out,
                     saved_pct=saved,
                     wall_seconds=rec.get("wall_seconds"),
                     notes=notes)


def _row_processing(name: str, input_path: Path, src_bytes: int,
                    chunks_done: int, history_rec: dict) -> StatusRow:
    """Live-encode row. wall_seconds is `now - timestamp_start_utc` so the
    user sees how long the current run has been going.

    Total-chunks: prefer the `chunks` array length (set on completion);
    otherwise estimate from `chunk_elapsed` (incremental per chunk during
    encode). Neither is a perfect mid-encode value; the display falls back
    to `N chunks done` when no total is known."""
    crf = (history_rec.get("settings") or {}).get("crf")
    crf_chain = str(crf) if crf is not None else ""
    chunks_block = history_rec.get("chunks")
    total_chunks = (len(chunks_block) if isinstance(chunks_block, list)
                    else 0)
    if not total_chunks:
        total_chunks = len(history_rec.get("chunk_elapsed") or {})
    chunk_progress = (f"{chunks_done}/{total_chunks} chunks done"
                      if total_chunks else f"{chunks_done} chunks done")
    wall = _wall_since_start(history_rec)
    return StatusRow(index=0, name=name, status="PROCESSING",
                     crf_chain=crf_chain,
                     source_bytes=src_bytes, output_bytes=None,
                     saved_pct=None, wall_seconds=wall,
                     notes=chunk_progress)


def _wall_since_start(history_rec: dict) -> Optional[float]:
    """Seconds since the encode's `timestamp_start_utc` (history's start
    field). Returns None if the field is missing or unparseable — the
    display column then renders `—`."""
    iso = history_rec.get("timestamp_start_utc")
    if not isinstance(iso, str):
        return None
    try:
        from datetime import datetime, timezone
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        started = datetime.fromisoformat(iso)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except (ValueError, TypeError):
        return None


def _is_workdir_live(workdir: Path) -> bool:
    """Approximation of "an encoder process is alive". True iff the workdir
    OR any encoded chunk inside it was touched within LIVENESS_WINDOW_SEC.
    A crashed run that left `in_progress` in history but stopped writing
    files registers as not-live (→ stale-workdir branch in classify).

    Stdlib-only, cross-OS — no psutil dep, no encoder.pid sidecar. False
    positives are bounded by LIVENESS_WINDOW_SEC: if the encoder is between
    chunks at exactly the wrong second, we under-report — but the user can
    re-run --status moments later for the live view."""
    if not workdir.is_dir():
        return False
    now = time.time()
    cutoff = now - LIVENESS_WINDOW_SEC
    candidates = [workdir]
    candidates.extend(workdir.glob("enc_src_*.mkv"))
    candidates.extend(workdir.glob("*.part.mkv"))
    for p in candidates:
        try:
            if p.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def _fmt_bytes(b: Optional[int]) -> str:
    if b is None:
        return "—"
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b/1024:.1f} KB"
    if b < 1024**3:
        return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def _fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "—"
    return f"{p:.1f}%"


def _fmt_wall(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def run_inspector(queue_path: Path, *, as_json: bool) -> int:
    """`--status` entry point. Reads queue.json + encoding_history.jsonl +
    workdir state + on-disk outputs + state sidecar, classifies every job,
    prints the result, exits 0. Strictly read-only — touches nothing on
    disk except for stat()-style queries.

    Lives here (not in run_queue.py) so the orchestration loop stays
    focused on what to do between jobs; this is a one-shot read-only
    command that fits the status module's classify/render concerns.
    """
    # Local import to avoid a top-level cycle (run_queue.py imports this
    # module via queue_modules.status, and history is a top-level package).
    import history as _history_module

    _, defaults, jobs_raw = reload_queue_with_retry(queue_path)
    if not jobs_raw:
        print("(queue is empty)")
        return 0
    jobs = expand_jobs(jobs_raw, queue_path.parent)
    history = history_by_input(_history_module.default_history_path())
    state = load_queue_state(queue_path)
    rows: list[StatusRow] = []
    for raw in jobs:
        merged = merge_job(defaults, raw)
        row = classify(merged, queue_dir=queue_path.parent,
                       history=history, state=state)
        rows.append(row)
    # Number the rows in the order queue.json lists them.
    for i, r in enumerate(rows, 1):
        r.index = i
    if as_json:
        print(render_json(rows))
    else:
        print(f"Queue: {queue_path}")
        print()
        print(render_table(rows, totals=True))
    return 0


def _totals_line(rows: list[StatusRow]) -> str:
    """Sum-of-DONE bytes + savings, ignores other statuses for cleanliness."""
    done = [r for r in rows if r.status == "DONE" and r.source_bytes
            and r.output_bytes is not None]
    if not done:
        return ""
    total_in = sum(r.source_bytes for r in done)
    total_out = sum(r.output_bytes for r in done)
    saved = total_in - total_out
    pct = saved / total_in * 100.0 if total_in else 0.0
    return (f"Totals (finished): {_fmt_bytes(total_in)} in → "
            f"{_fmt_bytes(total_out)} out → "
            f"{_fmt_bytes(saved)} / {pct:.1f}% saved "
            f"({len(done)} jobs)")

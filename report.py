"""
Markdown report generator for ffmpeg-compress-video runs.

Two ways to use it:

1. As a Python module — `from report import write_report`. Used by
   encode_resumable.py and run_queue.py after they've collected job results.

2. As a CLI — `python report.py single ...` for the simple non-resumable
   single-file bat, which has no Python orchestrator of its own.

Job dict schema (used by all entry points):

    {
        "input":            "<source path>",
        "output":           "<output path>",                      # None on failure
        "input_bytes":      int,
        "output_bytes":     int | None,                           # None on failure / skip
        "crf":              int | None,
        "preset":           str | None,
        "parallel":         int | None,
        "segments":         int | None,
        "max_size_percent": int | float | None,
        "elapsed_seconds":  float | None,
        "status":           str,   # ok / skipped-exists / stopped-threshold / failed-...
    }
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_mb(b: int | None) -> str:
    if not b:
        return "—"
    return f"{b / (1024 * 1024):.1f}"


def _fmt_gb(b: int | None) -> str:
    if not b:
        return "—"
    return f"{b / (1024 ** 3):.2f}"


def _fmt_pct(num: float, den: float) -> str:
    if not den:
        return "—"
    return f"{num / den * 100:.1f}%"


def _fmt_dur(sec: float | int | None) -> str:
    if not sec or sec < 0:
        return "—"
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


def _escape_md_pipe(s: str) -> str:
    # Pipes inside table cells need escaping. Backslash works in CommonMark.
    return s.replace("|", "\\|")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _grade(vmaf: float | None) -> str:
    if vmaf is None: return "—"
    if vmaf >= 95:   return "transparent"
    if vmaf >= 90:   return "excellent"
    if vmaf >= 80:   return "good"
    if vmaf >= 70:   return "acceptable"
    if vmaf >= 50:   return "degraded"
    return "poor"


def render(jobs: list[dict], title: str = "Encoding Report") -> str:
    """Build a markdown report from a list of job dicts. The first column is
    the job ordinal; the table includes status + per-file gain; a Summary block
    above aggregates over successful jobs only. Quality columns (VMAF / vmaf_lo
    / PSNR / SSIM / grade) appear when any job has quality scores attached."""
    successful = [
        j for j in jobs
        if j.get("status") == "ok"
        and j.get("input_bytes")
        and j.get("output_bytes")
    ]
    total_in = sum(j["input_bytes"] for j in successful)
    total_out = sum(j["output_bytes"] for j in successful)
    saved = total_in - total_out
    # Wall time also aggregates only successful jobs. Counting time spent
    # on threshold-aborted / failed jobs in the headline "Total wall time"
    # is misleading -- it implies that effort produced output, which it didn't.
    total_time = sum(j.get("elapsed_seconds") or 0 for j in successful)

    quality_jobs = [j for j in jobs if j.get("vmaf_mean") is not None]
    has_quality = bool(quality_jobs)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    other = len(jobs) - len(successful)

    lines: list[str] = [
        f"# {title}",
        "",
        f"_Generated: {now}_",
        "",
        "## Summary",
        "",
        f"- **Jobs**: {len(jobs)} total · {len(successful)} successful · "
        f"{other} skipped / aborted / failed",
    ]
    if successful:
        lines += [
            f"- **Total input**: {_fmt_mb(total_in)} MB  ({_fmt_gb(total_in)} GB)",
            f"- **Total output**: {_fmt_mb(total_out)} MB  ({_fmt_gb(total_out)} GB)",
            f"- **Saved**: {_fmt_mb(saved)} MB  ({_fmt_pct(saved, total_in)})",
        ]
    if total_time:
        lines.append(f"- **Total wall time**: {_fmt_dur(total_time)}")
    if has_quality:
        vmafs = [j["vmaf_mean"] for j in quality_jobs]
        avg_vmaf = sum(vmafs) / len(vmafs)
        worst_vmaf = min(vmafs)
        worst_job = min(quality_jobs, key=lambda j: j["vmaf_mean"])
        worst_name = Path(worst_job["input"]).name
        lines.append(
            f"- **Mean VMAF**: {avg_vmaf:.2f} (across {len(quality_jobs)} files; "
            f"worst {worst_vmaf:.2f} on `{worst_name}`)"
        )
        methods = sorted({j.get("quality_method") or "?" for j in quality_jobs})
        method_counts = {
            m: sum(1 for j in quality_jobs if (j.get("quality_method") or "?") == m)
            for m in methods
        }
        method_str = ", ".join(f"{m}: {n}" for m, n in method_counts.items())
        lines.append(f"- **Quality methods used**: {method_str}")

    lines += ["", "## Files", ""]

    if has_quality:
        lines += [
            "| # | File | Status | Input (MB) | Output (MB) | Saved (MB) | Saved % | "
            "CRF | Preset | Time | VMAF | vmaf_lo | PSNR (dB) | SSIM | Grade | Method |",
            "|---|------|--------|-----------:|------------:|-----------:|--------:|"
            "----:|--------|------|-----:|--------:|----------:|-----:|-------|--------|",
        ]
    else:
        lines += [
            "| # | File | Status | Input (MB) | Output (MB) | Saved (MB) | Saved % | CRF | Preset | Time |",
            "|---|------|--------|-----------:|------------:|-----------:|--------:|----:|--------|------|",
        ]

    for i, j in enumerate(jobs, 1):
        name = _escape_md_pipe(Path(j["input"]).name)
        status = j.get("status", "?")
        in_b = j.get("input_bytes")
        out_b = j.get("output_bytes")
        in_mb = _fmt_mb(in_b)
        out_mb = _fmt_mb(out_b)
        if in_b and out_b:
            saved_b = in_b - out_b
            saved_mb = _fmt_mb(saved_b)
            saved_pct = _fmt_pct(saved_b, in_b)
        else:
            saved_mb = "—"
            saved_pct = "—"
        crf = j.get("crf")
        crf_s = str(crf) if crf is not None else "—"
        preset = j.get("preset") or "—"
        time_s = _fmt_dur(j.get("elapsed_seconds"))

        row = (
            f"| {i} | {name} | {status} | {in_mb} | {out_mb} | "
            f"{saved_mb} | {saved_pct} | {crf_s} | {preset} | {time_s} |"
        )
        if has_quality:
            vmaf = j.get("vmaf_mean")
            vmaf_min = j.get("vmaf_min")
            psnr = j.get("psnr_y_mean")
            ssim = j.get("ssim_mean")
            method = j.get("quality_method") or "—"
            vmaf_s    = f"{vmaf:.2f}"    if vmaf     is not None else "—"
            vmaf_lo_s = f"{vmaf_min:.1f}" if vmaf_min is not None else "—"
            psnr_s    = f"{psnr:.2f}"    if psnr     is not None else "—"
            ssim_s    = f"{ssim:.4f}"    if ssim     is not None else "—"
            row += (f" {vmaf_s} | {vmaf_lo_s} | {psnr_s} | {ssim_s} | "
                    f"{_grade(vmaf)} | {method} |")
        lines.append(row)

    return "\n".join(lines) + "\n"


def write_report(md_path: Path | str, jobs: list[dict], title: str = "Encoding Report") -> Path:
    p = Path(md_path)
    p.write_text(render(jobs, title), encoding="utf-8")
    return p


def write_run_pair(
    tmp_dir: Path | str,
    queue_stem: str,
    queue_name: str,
    jobs: list[dict],
) -> tuple[Path, Path]:
    """Write the two reports a queue run produces:

    1. **Per-run timestamped report** at
       ``<tmp_dir>/report_<YYYY-MM-DD_HH_MM_SS>.md`` — contains only this
       run's jobs. Never overwritten; one file per invocation.

    2. **Incremental report** at ``<tmp_dir>/<queue_stem>_report.md`` —
       accumulates jobs across every run. The user controls reset by
       deleting this markdown file: if it's missing on entry, all prior
       history is discarded (the sidecar JSON is treated as stale and
       removed). Persistence is via the sidecar
       ``<tmp_dir>/<queue_stem>_report.history.json`` (raw job dicts so
       the next run can re-aggregate without re-parsing markdown).

    Returns (per_run_path, incremental_path).
    """
    tmp = Path(tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
    per_run_path = tmp / f"report_{ts}.md"
    per_run_path.write_text(
        render(jobs, title=f"Queue Run: {queue_name} ({ts})"),
        encoding="utf-8",
    )

    incremental_path = tmp / f"{queue_stem}_report.md"
    history_path = tmp / f"{queue_stem}_report.history.json"

    prior_jobs: list[dict] = []
    if incremental_path.exists():
        if history_path.exists():
            try:
                loaded = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    prior_jobs = loaded
            except Exception:
                prior_jobs = []
    else:
        # User deleted the incremental .md → treat as reset. Any sidecar
        # left behind is stale; remove it so it can't poison the next run.
        if history_path.exists():
            try:
                history_path.unlink()
            except OSError:
                pass

    combined = prior_jobs + jobs
    incremental_path.write_text(
        render(combined, title=f"Incremental Report: {queue_name}"),
        encoding="utf-8",
    )
    history_path.write_text(
        json.dumps(combined, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return per_run_path, incremental_path


# ---------------------------------------------------------------------------
# CLI: used by the non-resumable single-file bat
# ---------------------------------------------------------------------------

def _cli_single(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="report.py single")
    ap.add_argument("md_path", help="Where to write the report .md")
    ap.add_argument("input_path")
    ap.add_argument("output_path")
    ap.add_argument("--crf", type=int)
    ap.add_argument("--preset")
    ap.add_argument("--parallel", type=int)
    ap.add_argument("--segments", type=int)
    ap.add_argument("--max-size-percent", type=float)
    ap.add_argument("--elapsed-sec", type=float)
    ap.add_argument("--status", default="ok")
    ap.add_argument("--title")
    args = ap.parse_args(argv)

    in_p = Path(args.input_path)
    out_p = Path(args.output_path)
    job = {
        "input": str(in_p),
        "output": str(out_p) if args.status == "ok" else None,
        "input_bytes": in_p.stat().st_size if in_p.exists() else 0,
        "output_bytes": out_p.stat().st_size if out_p.exists() else None,
        "crf": args.crf,
        "preset": args.preset,
        "parallel": args.parallel,
        "segments": args.segments,
        "max_size_percent": args.max_size_percent,
        "elapsed_seconds": args.elapsed_sec,
        "status": args.status,
    }
    title = args.title or f"Encoding Report: {in_p.name}"
    p = write_report(args.md_path, [job], title)
    print(f"Report: {p}")
    return 0


def _cli_from_json(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="report.py from-json")
    ap.add_argument("md_path")
    ap.add_argument("jobs_json")
    ap.add_argument("--title", default="Encoding Report")
    args = ap.parse_args(argv)
    jobs = json.loads(Path(args.jobs_json).read_text(encoding="utf-8"))
    p = write_report(args.md_path, jobs, args.title)
    print(f"Report: {p}")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("single", "from-json"):
        print("Usage: python report.py {single | from-json} ...", file=sys.stderr)
        print("  single <md> <input> <output> [--crf N] [--preset NAME] ...", file=sys.stderr)
        print("  from-json <md> <jobs.json> [--title TITLE]", file=sys.stderr)
        return 2
    sub = sys.argv[1]
    rest = sys.argv[2:]
    if sub == "single":
        return _cli_single(rest)
    return _cli_from_json(rest)


if __name__ == "__main__":
    sys.exit(main())

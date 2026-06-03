"""argparse setup for encode_resumable.py.

Pulled into its own module so the main pipeline body reads as a recipe
rather than 80 lines of flag definitions. The single public function
`parse_args` builds the parser, parses sys.argv, and returns the namespace.
"""
from __future__ import annotations

import argparse


def _vmaf_threshold(raw: str) -> float:
    """argparse `type=` for `--visual-quality-threshold`. VMAF is bounded
    [0, 100] in the spec; we accept [1, 100] (a threshold of 0 would never
    trigger and ``None`` is the correct way to opt out). Values outside the
    range fail at parse time with a clear message — much better than the
    silent disable (negative) or false-positive abort on every chunk (>100)
    that an unconstrained type=float would produce."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"visual-quality-threshold must be a number, got {raw!r}")
    if not 1.0 <= value <= 100.0:
        raise argparse.ArgumentTypeError(
            f"visual-quality-threshold must be in [1, 100] (VMAF scale); "
            f"got {value}. Use no flag at all to disable the guard.")
    return value


def parse_args() -> argparse.Namespace:
    """Build the parser and consume sys.argv. All flag wiring lives here so
    the rest of the encoder doesn't import argparse."""
    ap = argparse.ArgumentParser(
        description="Resumable x265 encode: split → encode-per-chunk → "
                    "concat. Killed mid-encode? Re-run to resume.",
    )
    _add_pipeline_args(ap)
    _add_quality_args(ap)
    _add_preflight_args(ap)
    _add_choke_args(ap)
    return ap.parse_args()


def _add_pipeline_args(ap: argparse.ArgumentParser) -> None:
    """Mandatory I/O + encode params + threshold guard + report toggle."""
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--crf", type=int, required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--pix-fmt", required=True)
    ap.add_argument("--x265-params", required=True)
    ap.add_argument("--segment-seconds", type=int, default=60)
    ap.add_argument("--parallel", type=int, default=1,
                    help="Encode N chunks concurrently (default 1 = serial).")
    ap.add_argument("--max-output-bytes", type=int, default=None,
                    help="Abort encode if projected output > this many bytes "
                         "(checked after >=5%% overall progress).")
    ap.add_argument("--source-bytes", type=int, default=None,
                    help="Source file size in bytes; used in the abort "
                         "warning to display percentages.")
    ap.add_argument("--total-duration-seconds", type=float, default=None,
                    help="Total source duration in seconds; used by the "
                         "abort projection to know how much encoding is left.")
    ap.add_argument("--no-report", action="store_true",
                    help="Skip writing the per-file markdown report. The "
                         "queue runner sets this because it writes an "
                         "aggregate report of its own.")
    ap.add_argument("--hooks-config", default=None,
                    help="Path to the JSON sidecar holding the on_chunk_done "
                         "hook command (written by compress.py). Internal "
                         "plumbing — users set --on-chunk-done on compress.py.")
    ap.add_argument("--visual-quality-threshold", type=_vmaf_threshold,
                    default=None,
                    help="Stop encoding the file (exit 9 = "
                         "stopped-quality-threshold) if any chunk's measured "
                         "VMAF mean falls below this value. The first chunk "
                         "in temporal order is graced (single-chunk VMAF is "
                         "noisier than aggregate). The quality check runs in "
                         "parallel with the next chunk's encode at NORMAL CPU "
                         "priority so the abort decision lands quickly. Set "
                         "via queue.json `visual_quality_threshold` for "
                         "per-file control.")
    ap.add_argument("--done-dir", default=None,
                    help="If set, after a successful encode move BOTH source "
                         "and output into this directory (must be already "
                         "resolved to an absolute path by compress.py). Only "
                         "moves on status == ok; refuses to overwrite an "
                         "existing destination or move into the workdir.")


def _add_quality_args(ap: argparse.ArgumentParser) -> None:
    """VMAF/PSNR/SSIM measurement opts. Default is to measure after every
    successful encode; --no-quality-check skips entirely."""
    ap.add_argument("--no-quality-check", action="store_true",
                    help="Skip the VMAF/PSNR/SSIM quality measurement.")
    ap.add_argument("--vmaf-subsample", type=int, default=10,
                    help="Measure quality on every Nth frame. 1 = every frame "
                         "(slowest, most precise). 10 (default) is ~10x faster "
                         "and gives stable aggregate scores.")
    ap.add_argument("--vmaf-mode", choices=("auto", "chunks", "full"),
                    default="auto",
                    help="'auto' (default): per-chunk VMAF when workdir still "
                         "has paired chunks; otherwise full-file fallback.")
    ap.add_argument("--vmaf-chunks", type=int, default=3,
                    help="Number of chunks to sample in 'chunks' mode "
                         "(default 3).")


def _add_preflight_args(ap: argparse.ArgumentParser) -> None:
    """Pre-flight source-scan + auto-patch. Off-the-shelf safe defaults
    catch broken sources before any encode work happens."""
    ap.add_argument("--no-pre-flight-scan", action="store_true",
                    help="Skip the pre-flight decode scan. Default is to "
                         "scan first (cached in <source>.preflight.json) "
                         "and exit pre-flight-failed (code 6) on corruption.")
    ap.add_argument("--auto-patch-source", action="store_true",
                    help="When pre-flight finds bad windows in an h264 "
                         "source, attempt to surgically patch the broken "
                         "GOPs (re-encode JUST those few seconds via "
                         "ffmpeg's error concealment) and continue against "
                         "the patched intermediate. Original source untouched.")
    ap.add_argument("--max-patch-seconds", type=float, default=10.0,
                    help="Loss budget for --auto-patch-source. Bail if total "
                         "re-encoded GOP duration would exceed this (default 10s).")


def _add_choke_args(ap: argparse.ArgumentParser) -> None:
    """Per-chunk choke detection + optional auto-fix retry. Disable by
    setting either threshold or grace to 0."""
    ap.add_argument("--auto-fix-choke", action="store_true",
                    help="When a chunk chokes, try ONE re-encode with "
                         "relaxed motion-search params (me=umh, merange=32, "
                         "subme=3) + decode-walk verification before giving "
                         "up. Default off: choke → needs_fix.json + exit 7.")
    ap.add_argument("--choke-threshold-speed", type=float, default=0.05,
                    help="Encode-speed ratio below which a chunk is "
                         "considered choked. Set to 0 to disable.")
    ap.add_argument("--choke-grace-seconds", type=float, default=300.0,
                    help="How long a chunk must encode before being "
                         "eligible for the choke verdict. Set to 0 to disable.")

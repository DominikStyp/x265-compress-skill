"""Append-only JSONL history of every encode the skill performs.

One record per encode (success or threshold-abort). LLM-friendly format —
each line is a complete self-contained JSON object describing input metadata,
encoder settings, per-chunk timings, output metrics, and quality scores.

Typical use from an analysis session:

    import json
    from pathlib import Path
    p = Path(r'C:\\_MOJE\\other\\CUTTED\\encoding_history.jsonl')
    records = [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
    # → list[dict], analyse with pandas / hand-rolled stats / fed to an LLM

The file lives outside any specific queue directory so it accumulates across
batches and survives reorganization of the encoding library. Override the
location by setting the CLAUDE_ENCODING_HISTORY_PATH env var.

Append failures NEVER raise — encoding is the user-visible work and must not
fail because the side-channel history log misbehaved (disk full, permission,
concurrent writer). On error we print a one-line warning to stderr and move on.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Schema version. Bump when fields are added/removed/semantically changed so
# downstream analysis can branch on it.
SCHEMA_VERSION = 1

# Default history file location. Sits at the CUTTED root so it accumulates
# across queue/batch boundaries.
_DEFAULT_HISTORY_PATH = Path(r"C:\_MOJE\other\CUTTED\encoding_history.jsonl")


def default_history_path() -> Path:
    """Resolve the canonical history-file path. CLAUDE_ENCODING_HISTORY_PATH
    overrides the default; useful for testing or for redirecting to a
    portable location."""
    env = os.environ.get("CLAUDE_ENCODING_HISTORY_PATH")
    if env:
        return Path(env)
    return _DEFAULT_HISTORY_PATH


def append_record(record: dict, *, history_path: Path | None = None) -> None:
    """Append one JSON-serialized record as a single line to the history log.

    Never raises on failure — prints a one-line warning to stderr instead.
    Encoding is the load-bearing work; a broken side-channel must not abort it."""
    path = history_path or default_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False keeps non-ASCII filenames readable in the log;
        # one record per line so the file remains JSONL (NOT pretty-printed
        # JSON — that would break the one-line-per-record contract).
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"WARNING: failed to append encoding history: {e}",
              file=sys.stderr)


def _ffmpeg_version_line() -> str | None:
    """First line of `ffmpeg -version` (e.g. 'ffmpeg version N-119... ...').
    Cached on the function for the lifetime of the process so we don't spawn
    ffmpeg every record."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-version"],
                           capture_output=True, text=True, timeout=5)
        first = (r.stdout or "").splitlines()[:1]
        return first[0] if first else None
    except Exception:
        return None


# Cache the env block — it's invariant within one process and the ffmpeg
# spawn is ~50 ms we'd rather pay once.
_env_cache: dict | None = None


def collect_environment() -> dict:
    """Hardware + software fingerprint of the machine that produced the encode.
    Lets analysts compare timings across hosts (e.g. Scar 100 W vs M4 Pro)."""
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    _env_cache = {
        "platform": sys.platform,
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "machine": platform.node(),
        "processor": platform.processor(),
        "ffmpeg_version_line": _ffmpeg_version_line(),
    }
    return _env_cache


def now_iso_utc() -> str:
    """ISO-8601 UTC timestamp (e.g. '2026-05-21T12:30:45Z'). Used for both
    record start/end timestamps so analyses can bucket by wall-clock day
    even when individual encodes span hours."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_input_block(src_path: Path, probe_json: dict | None) -> dict:
    """Distill an ffprobe result into the flat metrics analysts actually
    use: codec, resolution, fps (as both fraction and decimal), bitrate,
    bits-per-pixel, duration, pix_fmt, container. Missing probes get None
    rather than raising — the rest of the record is still useful."""
    block: dict = {
        "path": str(src_path),
        "name": src_path.name,
        "size_bytes": (src_path.stat().st_size if src_path.exists() else None),
    }
    if not probe_json:
        return block

    fmt = probe_json.get("format", {}) or {}
    block["container"] = fmt.get("format_name")
    duration = fmt.get("duration")
    try:
        block["duration_s"] = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        block["duration_s"] = None

    # First video stream is what we encode (audio is passthrough; we don't
    # capture audio metadata here because it survives identically).
    video = next((s for s in (probe_json.get("streams") or [])
                  if s.get("codec_type") == "video"), None)
    if video is None:
        return block

    width = video.get("width")
    height = video.get("height")
    block["codec"] = video.get("codec_name")
    block["width"] = width
    block["height"] = height
    block["resolution"] = (f"{width}x{height}"
                           if width is not None and height is not None else None)
    block["pix_fmt"] = video.get("pix_fmt")
    block["fps"] = video.get("r_frame_rate")

    # Decimal fps for analyses that want a number rather than a fraction string.
    fps_decimal = None
    rfr = video.get("r_frame_rate") or ""
    if "/" in rfr:
        num, den = rfr.split("/", 1)
        try:
            n, d = float(num), float(den)
            fps_decimal = n / d if d else None
        except ValueError:
            pass
    block["fps_decimal"] = fps_decimal

    bitrate = video.get("bit_rate")
    try:
        block["bitrate_bps"] = int(bitrate) if bitrate is not None else None
    except (TypeError, ValueError):
        block["bitrate_bps"] = None

    # Bits per pixel — the single most predictive metric for the CRF/VMAF
    # tradeoff. Derived here so every record carries it ready-to-correlate.
    if (block.get("bitrate_bps") and width and height and fps_decimal
            and fps_decimal > 0):
        block["bpp"] = round(
            block["bitrate_bps"] / (width * height * fps_decimal), 6)
    else:
        block["bpp"] = None

    return block


def build_chunk_records(workdir: Path,
                        chunks: list[Path],
                        elapsed_by_chunk: dict[str, float],
                        encode_order: list[Path] | None = None,
                        ) -> list[dict]:
    """One dict per chunk: original index, position in encode order, input
    chunk size + duration, output (encoded) chunk size, wall time spent on
    encoding it. Encoder throughput can be derived as
    chunk_duration / elapsed_s (the speed factor that x265 reports live)."""
    order_position: dict[str, int] = {}
    if encode_order:
        order_position = {c.name: i for i, c in enumerate(encode_order)}

    out: list[dict] = []
    for idx, chunk in enumerate(chunks):
        enc = workdir / f"enc_{chunk.stem}.mkv"
        src_dur: float | None = None
        # Cheap probe: ffprobe each chunk's duration. For 10-chunk encodes
        # this is ~3 s total — negligible against multi-hour encode times.
        # A deliberate local probe (not probes.probe_duration): this records
        # None — not 0.0 — on failure so speed_factor is omitted rather than
        # divided by a bogus zero, and uses a short per-chunk timeout since
        # it runs in a loop.
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", str(chunk)],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
            )
            if r.returncode == 0:
                data = json.loads(r.stdout)
                d = data.get("format", {}).get("duration")
                src_dur = float(d) if d is not None else None
        except Exception:
            pass

        elapsed = elapsed_by_chunk.get(chunk.name)
        rec: dict = {
            "index": idx,
            "src_name": chunk.name,
            "src_size_bytes": (chunk.stat().st_size if chunk.exists() else None),
            "src_duration_s": src_dur,
            "enc_name": enc.name,
            "enc_size_bytes": (enc.stat().st_size if enc.exists() else None),
            "elapsed_s": elapsed,
        }
        if order_position:
            rec["encode_order_position"] = order_position.get(chunk.name)
        # Derived: encoder speed factor (source-seconds per wall-second).
        # >1 means faster than realtime; <1 means slower (typical for slow preset).
        if elapsed and src_dur and elapsed > 0:
            rec["speed_factor"] = round(src_dur / elapsed, 4)
        out.append(rec)
    return out

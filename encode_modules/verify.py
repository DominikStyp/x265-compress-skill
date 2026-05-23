"""Post-encode output verification.

Two layers of checks, cheap → expensive, short-circuiting on the first
failure:

  1. **Structural** (ffprobe-only, ~1 s): file exists, non-zero size, probe
     succeeds on both src + dst, container duration matches within tolerance,
     dst has exactly one HEVC video stream at the original resolution, audio
     stream count + per-stream codec/channels/sample-rate matches passthrough.

  2. **Decode pass** (every video frame + every audio sample → null, ~5-10×
     faster than realtime): catches the corruption classes structural probes
     miss — mid-bitstream truncation, bad packet offsets at chunk boundaries,
     NAL units the decoder can't reconstruct.

Also exports the two helpers the encode-retry loop uses to identify which
chunks need re-encoding: missing enc_NNNN.mkv files, and enc files whose
duration drifted from their src counterpart.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .probes import fmt_dur, probe_duration, probe_full
from .process_control import IDLE_PRIORITY_FLAGS


def decode_walk_chunk(chunk_path: Path, *,
                     timeout_s: int = 120) -> dict:
    """Decode-walk a single encoded chunk to confirm it's mergeable with
    other chunks via concat. Used after an --auto-fix-choke retry to make
    sure the fix didn't just produce a different flavor of broken output.

    Returns {ok: bool, decode_exit_code: int, error_count: int, error_samples: [...],
             duration_seconds: float, elapsed_seconds: float, timed_out: bool}.

    "Mergeable" means: ffmpeg can decode every frame + audio sample with
    -xerror set (no error concealment fallback hiding silent corruption).
    Identical check to what verify_output's decode pass would do on the
    final merged file — just narrowed to one chunk."""
    t0 = time.monotonic()
    timed_out = False
    duration_seconds = probe_duration(chunk_path)
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-hide_banner", "-xerror",
             "-i", str(chunk_path),
             "-map", "0:v?", "-map", "0:a?",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
            creationflags=IDLE_PRIORITY_FLAGS,
        )
        exit_code = r.returncode
        err_text = r.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = -1
        err_text = (e.stderr or b"").decode("utf-8", errors="replace") \
            if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
    lines = [l.strip() for l in err_text.splitlines() if l.strip()]
    return {
        "ok": exit_code == 0 and len(lines) == 0 and not timed_out,
        "decode_exit_code": exit_code,
        "error_count": len(lines),
        "error_samples": [l[:240] for l in lines[:8]],
        "duration_seconds": duration_seconds,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
        "timed_out": timed_out,
    }


def analyze_chunk_errors(chunk_path: Path, *,
                        timeout_s: int = 180,
                        max_samples: int = 12) -> dict:
    """Walk a chunk's bitstream with ffmpeg's decoder and report any
    decode/NAL errors found. Used after a `chunk-choked` event so the
    history log + queue report can record WHY the encode fell over (almost
    always upstream bitstream corruption that the decoder concealed and
    x265 then spun on).

    Returns a dict with stable keys for downstream consumers — never raises:
        decode_exit_code  ffmpeg's exit code (0 if no hard error; non-zero
                          when -xerror triggered)
        error_count       number of stderr lines printed by the decoder
        error_samples     first `max_samples` lines verbatim (truncated
                          per-line to 240 chars to keep records compact)
        elapsed_seconds   how long the analysis took
        timed_out         True if the probe was killed by `timeout_s`

    `timeout_s` caps the probe — for a 100-sec 4K chunk a decode walk
    usually completes in 5-15 s, but we don't want a doubly-broken file to
    block the next queue job indefinitely on this diagnostic step alone."""
    start = time.monotonic()
    timed_out = False
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-hide_banner", "-xerror",
             "-i", str(chunk_path),
             "-map", "0:v?", "-map", "0:a?",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
            creationflags=IDLE_PRIORITY_FLAGS,
        )
        exit_code = r.returncode
        err_text = r.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = None
        err_text = (e.stderr or b"").decode("utf-8", errors="replace") \
            if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
    except Exception as e:
        return {
            "decode_exit_code": None,
            "error_count": 0,
            "error_samples": [f"(could not run decode walk: {e})"],
            "elapsed_seconds": round(time.monotonic() - start, 1),
            "timed_out": False,
        }
    lines = [l.strip() for l in err_text.splitlines() if l.strip()]
    return {
        "decode_exit_code": exit_code,
        "error_count": len(lines),
        "error_samples": [l[:240] for l in lines[:max_samples]],
        "elapsed_seconds": round(time.monotonic() - start, 1),
        "timed_out": timed_out,
    }


def _decode_check(path: Path) -> str | None:
    """Decode every video frame and audio sample to /dev/null. Return None
    on a clean pass, or a short error description if anything failed.

    Catches the corruption classes structural ffprobe misses: mid-bitstream
    truncation, bad packet offsets at chunk boundaries, NAL units the decoder
    can't reconstruct, audio frames with invalid headers. The cost is a full
    decode of the file (typically 5-10x faster than real-time for HEVC at
    low bitrate). `-xerror` makes ffmpeg fast-fail on the first hard error
    rather than scanning the rest of the file, so a bad output is detected
    quickly while a healthy one still pays the full decode cost."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-hide_banner", "-xerror",
         "-i", str(path),
         "-map", "0:v?", "-map", "0:a?",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=IDLE_PRIORITY_FLAGS,
    )
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return f"ffmpeg exit {r.returncode}" + (f": {err[:400]}" if err else "")
    if err:
        return f"decode produced error output: {err[:400]}"
    return None


def find_missing_enc_chunks(workdir: Path) -> list[Path]:
    """Return the list of enc_src_NNNN.mkv paths that *should* exist (one per
    src_NNNN.mkv in workdir) but don't. Used by the retry loop to detect
    chunks whose worker crashed before producing output."""
    src_chunks = sorted(workdir.glob("src_*.mkv"))
    expected = [workdir / f"enc_{c.name}" for c in src_chunks]
    return [p for p in expected if not p.exists()]


def identify_bad_chunks(workdir: Path, *, tol: float = 2.0) -> list[Path]:
    """Return enc_src_NNNN.mkv chunks whose duration doesn't match their
    src_NNNN.mkv counterpart within ±tol seconds. Catches the rare case where
    a chunk file exists but is internally truncated (mid-rename crash,
    disk-full at end-of-write, etc.)."""
    bad: list[Path] = []
    for enc in sorted(workdir.glob("enc_src_*.mkv")):
        if ".part" in enc.suffixes:
            continue
        src = workdir / enc.name.removeprefix("enc_")  # enc_src_0004.mkv → src_0004.mkv
        if not src.exists():
            continue
        sd, ed = probe_duration(src), probe_duration(enc)
        if abs(sd - ed) > tol:
            bad.append(enc)
    return bad


def _streams_by_type(info: dict) -> dict[str, list[dict]]:
    """Group an ffprobe result's streams by codec_type. Returns a dict with
    'video' and 'audio' keys (other types — data, subtitles — are dropped).
    Used by the per-stream verify helpers."""
    out: dict[str, list[dict]] = {"video": [], "audio": []}
    for s in info.get("streams", []) or []:
        t = s.get("codec_type")
        if t in out:
            out[t].append(s)
    return out


def _verify_duration(src_info: dict, dst_info: dict, tol_s: float) -> list[str]:
    """Container-level duration check: input vs output within ±tol_s.
    Catches the common bug of a dropped chunk shortening the output."""
    src_dur = float(src_info.get("format", {}).get("duration", 0) or 0)
    dst_dur = float(dst_info.get("format", {}).get("duration", 0) or 0)
    diff = src_dur - dst_dur
    if abs(diff) <= tol_s:
        return []
    return [
        f"duration mismatch: input {fmt_dur(src_dur)} ({src_dur:.1f}s), "
        f"output {fmt_dur(dst_dur)} ({dst_dur:.1f}s), "
        f"diff {diff:+.1f}s (tolerance ±{tol_s}s)"
    ]


def _verify_video_streams(src_v: list[dict], dst_v: list[dict]) -> list[str]:
    """Verify the output has exactly one HEVC video stream and that its
    resolution matches the source. Resolution mismatches usually mean a
    rescale snuck in via a misconfigured filter graph."""
    problems: list[str] = []
    if len(dst_v) != 1:
        problems.append(
            f"expected exactly 1 video stream in output, found {len(dst_v)}")
    elif dst_v[0].get("codec_name") != "hevc":
        problems.append(
            f"video codec is {dst_v[0].get('codec_name')!r}, expected 'hevc'")
    if src_v and dst_v:
        sv, dv = src_v[0], dst_v[0]
        if sv.get("width") != dv.get("width") or sv.get("height") != dv.get("height"):
            problems.append(
                f"resolution mismatch: input {sv.get('width')}x{sv.get('height')}, "
                f"output {dv.get('width')}x{dv.get('height')}")
    return problems


def _verify_audio_streams(src_a: list[dict], dst_a: list[dict]) -> list[str]:
    """Verify audio passthrough was bit-perfect: same count of streams,
    each with matching codec / channel count / sample rate at the same
    index. We can't checksum the audio samples (would need a full decode),
    but matching these three fields catches every realistic re-encode bug."""
    problems: list[str] = []
    if len(dst_a) != len(src_a):
        problems.append(
            f"audio stream count mismatch: input has {len(src_a)}, "
            f"output has {len(dst_a)}")
        return problems
    for i, (sa, da) in enumerate(zip(src_a, dst_a)):
        if sa.get("codec_name") != da.get("codec_name"):
            problems.append(
                f"audio[{i}] codec mismatch (passthrough expected): "
                f"input {sa.get('codec_name')!r} → output {da.get('codec_name')!r}")
        if sa.get("channels") != da.get("channels"):
            problems.append(
                f"audio[{i}] channel count mismatch: "
                f"input {sa.get('channels')} → output {da.get('channels')}")
        if sa.get("sample_rate") != da.get("sample_rate"):
            problems.append(
                f"audio[{i}] sample rate mismatch: "
                f"input {sa.get('sample_rate')} Hz → output {da.get('sample_rate')} Hz")
    return problems


def verify_output(src: Path, dst: Path, *, duration_tol_sec: float = 2.0) -> list[str]:
    """Compare a freshly-merged output against its source. Returns a list of
    problems — empty list means the output is verified clean.

    The structural checks (existence, ffprobe, duration, streams) run first
    and cheaply. The decode pass (every frame + audio sample → /dev/null)
    only runs if everything else passed — it's the most expensive check and
    is pointless on an output already known to be broken.

    Intentionally NOT checked: bitrate (the whole point of compression),
    file size, per-frame visual equivalence (covered by the VMAF sidecar)."""
    if not dst.exists():
        return [f"output file does not exist: {dst}"]
    if dst.stat().st_size == 0:
        return ["output file is 0 bytes"]

    src_info = probe_full(src)
    if src_info is None:
        return [f"could not probe input {src.name} — cannot verify"]
    dst_info = probe_full(dst)
    if dst_info is None:
        return ["could not probe output — mux is corrupt or unreadable"]

    src_s, dst_s = _streams_by_type(src_info), _streams_by_type(dst_info)

    problems: list[str] = []
    problems += _verify_duration(src_info, dst_info, duration_tol_sec)
    problems += _verify_video_streams(src_s["video"], dst_s["video"])
    problems += _verify_audio_streams(src_s["audio"], dst_s["audio"])

    # Final guard: expensive full-decode pass. Skipped if structural checks
    # already found problems — the file is known broken, no need to spend
    # minutes confirming it.
    if not problems:
        decode_err = _decode_check(dst)
        if decode_err:
            problems.append(f"decode pass failed: {decode_err}")

    return problems

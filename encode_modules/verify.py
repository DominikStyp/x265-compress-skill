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

from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority

from .probes import fmt_dur, probe_duration, probe_full


def _run_decode_walk(path: Path, *,
                    timeout_s: int,
                    start_s: float | None = None,
                    dur_s: float | None = None,
                    max_samples: int = 8) -> dict:
    """Run ffmpeg in decode-walk mode and return a structured result.

    Single shared implementation behind `decode_walk_chunk`, `analyze_chunk_errors`,
    `_decode_check`, and `pre_flight._walk_one_window` — they previously each
    had their own near-identical copy of this code, with subtle drift risk
    every time one of them got a fix the others didn't.

    `-xerror` makes ffmpeg fast-fail on the first hard error rather than
    scanning the rest of the file, so a bad input is detected quickly while
    a healthy one still pays the full decode cost (~5-10x faster than
    real-time for HEVC at low bitrate). The `-map 0:v? -map 0:a?` selectors
    cover the common case where a file has either no video or no audio.

    `start_s` + `dur_s` (both None or both set) crop the walk to a window —
    used by pre-flight to find which chunk-sized window of a source has
    bitstream errors. Both None walks the whole file.

    Result keys (always present):
        ok               True iff exit_code == 0 AND no stderr AND not timed_out
        decode_exit_code ffmpeg's exit code (None on Popen-level failure)
        error_count      number of non-empty stderr lines (decoder errors)
        error_samples    first `max_samples` lines verbatim (truncated to 240 chars each)
        elapsed_seconds  wall time of the walk
        timed_out        True if killed by timeout_s
    """
    args = ["ffmpeg", "-v", "error", "-hide_banner", "-xerror"]
    if start_s is not None:
        args += ["-ss", f"{start_s}"]
    args += ["-i", str(path)]
    if dur_s is not None:
        args += ["-t", f"{dur_s}"]
    args += ["-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]

    t0 = time.monotonic()
    timed_out = False
    try:
        r = subprocess.run(
            wrap_cmd_for_low_priority(args),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s,
            **low_priority_popen_kwargs(),
        )
        exit_code = r.returncode
        err_text = r.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = -1  # sentinel for "timed out before producing exit"
        if isinstance(e.stderr, (bytes, bytearray)):
            err_text = e.stderr.decode("utf-8", errors="replace")
        else:
            err_text = e.stderr or ""
    except Exception as e:
        # subprocess itself failed (extremely rare — bad path, missing
        # ffmpeg binary, OS-level resource exhaustion). Return a shape-
        # compatible dict so callers don't need a special path.
        return {
            "ok": False,
            "decode_exit_code": None,
            "error_count": 0,
            "error_samples": [f"(could not run decode walk: {e})"],
            "elapsed_seconds": round(time.monotonic() - t0, 1),
            "timed_out": False,
        }

    lines = [l.strip() for l in err_text.splitlines() if l.strip()]
    return {
        "ok": exit_code == 0 and len(lines) == 0 and not timed_out,
        "decode_exit_code": exit_code,
        "error_count": len(lines),
        "error_samples": [l[:240] for l in lines[:max_samples]],
        "elapsed_seconds": round(time.monotonic() - t0, 1),
        "timed_out": timed_out,
    }


def decode_walk_chunk(chunk_path: Path, *,
                     timeout_s: int = 120) -> dict:
    """Decode-walk a single encoded chunk to confirm it's mergeable with
    other chunks via concat. Used after an --auto-fix-choke retry to make
    sure the fix didn't just produce a different flavor of broken output.

    Returns the standard `_run_decode_walk` dict plus `duration_seconds`.
    "Mergeable" means: ffmpeg can decode every frame + audio sample with
    -xerror set (no error concealment fallback hiding silent corruption)."""
    result = _run_decode_walk(chunk_path, timeout_s=timeout_s, max_samples=8)
    result["duration_seconds"] = probe_duration(chunk_path)
    return result


def analyze_chunk_errors(chunk_path: Path, *,
                        timeout_s: int = 180,
                        max_samples: int = 12) -> dict:
    """Walk a chunk's bitstream with ffmpeg's decoder and report any
    decode/NAL errors found. Used after a `chunk-choked` event so the
    history log + queue report can record WHY the encode fell over (almost
    always upstream bitstream corruption that the decoder concealed and
    x265 then spun on).

    Returns the standard `_run_decode_walk` dict (without the `ok` field —
    callers of analyze_chunk_errors only care about counts/samples, not
    a boolean verdict). Bigger default `max_samples` (12 vs 8) because
    diagnostic callers want richer context for the JSONL log.

    `timeout_s` caps the probe — for a 100-sec 4K chunk a decode walk
    usually completes in 5-15 s, but we don't want a doubly-broken file to
    block the next queue job indefinitely on this diagnostic step alone."""
    result = _run_decode_walk(chunk_path, timeout_s=timeout_s,
                              max_samples=max_samples)
    # Backward-compat: callers expect the shape without `ok`.
    result.pop("ok", None)
    return result


def _decode_check(path: Path) -> str | None:
    """Decode every video frame and audio sample to /dev/null. Return None
    on a clean pass, or a short error description if anything failed.

    Catches the corruption classes structural ffprobe misses: mid-bitstream
    truncation, bad packet offsets at chunk boundaries, NAL units the decoder
    can't reconstruct, audio frames with invalid headers."""
    # Generous timeout (10 min) — full-file decode of a 30-min 4K mkv can
    # take a few minutes on lower-end hardware.
    r = _run_decode_walk(path, timeout_s=600, max_samples=4)
    if r["timed_out"]:
        return f"decode walk timed out after {r['elapsed_seconds']}s"
    if r["decode_exit_code"] not in (0, None):
        tail = "; ".join(r["error_samples"])[:400]
        suffix = f": {tail}" if tail else ""
        return f"ffmpeg exit {r['decode_exit_code']}{suffix}"
    if r["error_count"] > 0:
        tail = "; ".join(r["error_samples"])[:400]
        return f"decode produced error output: {tail}"
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

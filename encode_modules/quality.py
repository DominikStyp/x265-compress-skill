"""VMAF / PSNR / SSIM measurement against the source.

Single `libvmaf` filter pass extracts all three perceptual metrics together
(VMAF model + PSNR feature + float-SSIM feature) — one ffmpeg invocation,
shared decode work, ~1 / 1.6× source duration on typical hardware.

Three measurement modes:

  full     — walk the entire merged output end-to-end. Slow but reliable.
             REQUIRES the `-r src_fps` fps-match fix on BOTH inputs (see
             _build_libvmaf_cmd's docstring) — without it, framesync silently
             drops/duplicates frames on concat'd outputs and craters scores.

  chunks   — measure each `enc_src_NNNN.mkv` against its paired source
             `src_NNNN.mkv` directly inside the workdir. Each pair is
             naturally aligned (both start at PTS 0), so no fps-match needed.
             ~30 s typical; the right answer for outputs whose workdir is
             still present.

  segments — sample N segments via `-ss` input seek. Fragile on chunked
             outputs (keyframe-layout mismatch at seek targets). The auto
             dispatcher never picks it; kept for the rare case the user
             forces it.

`quality_check_auto` is the dispatcher most callers want — it picks chunks
if the workdir still has paired chunks, full otherwise.
"""
from __future__ import annotations

import json
import os as _os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from .probes import probe_duration, probe_fps
from .progress_bar import ProgressBar, read_ffmpeg_progress
from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority


def _libvmaf_node_for(log_path: Path, subsample: int) -> str:
    """Build the libvmaf filtergraph node — VMAF + PSNR + SSIM features, JSON
    log to `log_path`, sampling every Nth frame. The colon in a Windows
    `C:\\` path must be escaped to `\\:` because ffmpeg's filter syntax uses
    `:` as the option separator; backslashes are converted to forward slashes
    everywhere else to sidestep further escaping headaches."""
    escaped = str(log_path).replace("\\", "/").replace(":", r"\:")
    return (f"libvmaf=log_path='{escaped}':log_fmt=json"
            f":feature='name=psnr|name=float_ssim':n_subsample={subsample}")


def _build_libvmaf_cmd(src: Path, dst: Path, *,
                      src_fps: str | None,
                      seek_start: float | None,
                      duration: float | None,
                      libvmaf_node: str) -> list[str]:
    """Assemble the ffmpeg command line for one libvmaf invocation.

    CRITICAL fps normalization: both inputs are prefixed with `-r src_fps`.
    The .mkv built from `ffmpeg -f concat -c copy` reports a slightly
    drifted container fps (e.g. src=50/1 → dst=18649/373≈49.997) even when
    frame counts match exactly. ffmpeg's framesync pairs frames between
    libvmaf's two inputs by PTS — with mismatched fps it silently drops or
    duplicates one stream to keep pace, pairing misaligned frames at certain
    instants. We've seen this drop VMAF from a true ~97.5 to a bogus 17.36
    on a 43-min file. Passing `-r src_fps` as an INPUT option tells ffmpeg
    "ignore stored PTSs, generate new ones assuming constant fps"; with the
    same value on both sides, frame *i* of source pairs with frame *i* of
    target deterministically.

    Applying `-r` to BOTH (not just dst) matters: even nominally-clean source
    PTSs have sub-frame jitter that accumulates over thousands of frames.
    Dst-only normalization gives 97 at 30s but craters to 32 at 5 min.

    Segment mode (seek_start + duration both set) crops both inputs to a
    [seek_start, seek_start+duration) window via trim+setpts, with a 2s
    pre-roll so the decoder is past the nearest keyframe by the time the
    trim window opens. Segment mode is fragile on chunked-concat outputs
    (keyframe-layout mismatch at seek targets); auto-mode never uses it."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostats",
           "-progress", "pipe:1"]

    def add_input(path: Path, seek_offset: float | None) -> None:
        if src_fps:
            cmd.extend(["-r", src_fps])
        if seek_offset is not None:
            cmd.extend(["-ss", f"{seek_offset:.3f}"])
        cmd.extend(["-i", str(path)])

    if seek_start is not None and duration is not None:
        pre_roll = 2.0
        seek_offset = max(0.0, seek_start - pre_roll)
        trim_start = seek_start - seek_offset
        add_input(src, seek_offset)
        add_input(dst, seek_offset)
        filter_complex = (
            f"[0:v]trim=start={trim_start:.3f}:duration={duration:.3f},"
            f"setpts=PTS-STARTPTS[ref];"
            f"[1:v]trim=start={trim_start:.3f}:duration={duration:.3f},"
            f"setpts=PTS-STARTPTS[dist];"
            f"[ref][dist]{libvmaf_node}"
        )
    else:
        # Full-file mode: no seek, libvmaf consumes the entire streams.
        add_input(src, None)
        add_input(dst, None)
        filter_complex = f"[0:v][1:v]{libvmaf_node}"

    cmd += ["-lavfi", filter_complex, "-f", "null", "-"]
    return cmd


def _parse_vmaf_log(log_path: Path) -> dict | None:
    """Read libvmaf's JSON log and extract the VMAF / PSNR / SSIM aggregates
    that downstream report-writers consume. Returns None when the file is
    missing or malformed (libvmaf crashed, disk full, etc.)."""
    if not log_path.exists():
        return None
    try:
        with open(log_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    pm = data.get("pooled_metrics", {})

    def metric(key: str, agg: str = "mean") -> float | None:
        v = pm.get(key, {}).get(agg)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "vmaf_mean": metric("vmaf"),
        "vmaf_min": metric("vmaf", "min"),
        "vmaf_harmonic_mean": metric("vmaf", "harmonic_mean"),
        "psnr_y_mean": metric("psnr_y") if metric("psnr_y") is not None else metric("psnr"),
        "ssim_mean": metric("float_ssim") if metric("float_ssim") is not None else metric("ssim"),
        "frames_evaluated": len(data.get("frames", [])),
    }


def _quality_check_run(src: Path, dst: Path, *,
                      subsample: int = 10,
                      seek_start: float | None = None,
                      duration: float | None = None,
                      on_progress: Optional[Callable[[float, str, str], None]]
                      = None) -> dict | None:
    """One ffmpeg+libvmaf invocation comparing `src` vs `dst`. Shared by
    full-file mode (no seek_start/duration) and segment-sampling mode (both
    set). The fps normalization that makes scores honest on concat'd outputs
    lives in `_build_libvmaf_cmd`; the JSON parser in `_parse_vmaf_log`.

    `on_progress(out_time_s, fps, speed)` is fired once per ffmpeg `-progress`
    tick; the CALLER owns rendering (so quality_check_chunks can draw one
    overall bar across many runs). Returns a dict of metrics, or None on any
    failure."""
    log_path = Path(tempfile.gettempdir()) / f"vmaf_{_os.getpid()}_{int(time.time()*1000000)}.json"
    libvmaf_node = _libvmaf_node_for(log_path, subsample)
    cmd = _build_libvmaf_cmd(
        src, dst,
        src_fps=probe_fps(src),
        seek_start=seek_start, duration=duration,
        libvmaf_node=libvmaf_node,
    )

    proc = None
    try:
        proc = subprocess.Popen(
            wrap_cmd_for_low_priority(cmd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **low_priority_popen_kwargs(),
        )

        def _tick(state: "dict[str, str]") -> None:
            if on_progress is None:
                return
            try:
                out_s = float(state.get("out_time_us", "0")) / 1_000_000
            except ValueError:
                return
            on_progress(out_s, state.get("fps", "?").strip(),
                        state.get("speed", "?").strip())

        assert proc.stdout is not None
        read_ffmpeg_progress(proc.stdout, _tick)
        rc = proc.wait()
        if proc.stderr:
            proc.stderr.read()
        if rc != 0:
            return None
        return _parse_vmaf_log(log_path)
    except Exception:
        return None
    finally:
        # Never leak the ffmpeg+libvmaf child. Unlike subprocess.run, a Popen is
        # not reaped when the read loop raises (decode error mid-stream) or the
        # user Ctrl-Cs during a multi-minute pass — terminate it before leaving.
        if proc is not None and proc.poll() is None:
            # Guard the whole teardown: a terminate()/wait() that itself raises
            # (e.g. a Windows handle race) must not skip the log unlink below.
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass


def _pick_chunks_to_sample(total: int, n: int) -> list[int]:
    """Pick `n` evenly-spaced chunk indices from a workdir with `total`
    chunks. Spreads between 10% and 90% of the chunk range so the first
    and last chunks (where boundary effects cluster — lookahead ramp-up,
    trailing content) are skipped when there's something better to pick.

    Example: total=10, n=3 → [1, 5, 9]  (matches the user's intuition)
             total=10, n=5 → [1, 3, 5, 7, 9]
             total=10, n=1 → [5]"""
    if total <= 0:
        return []
    n = max(1, min(n, total))
    if n >= total:
        return list(range(total))
    if n == 1:
        return [total // 2]
    raw = [total * (0.1 + 0.8 * i / (n - 1)) for i in range(n)]
    clamped = [max(0, min(total - 1, int(round(p)))) for p in raw]
    # Dedup while preserving order (rounding can collide on small totals).
    return list(dict.fromkeys(clamped))


def quality_check_chunks(workdir: Path, *,
                        subsample: int = 10,
                        n_chunks: int = 3,
                        progress_prefix: str | None = None) -> dict | None:
    """Per-chunk VMAF: compare each `enc_src_NNNN.mkv` against its source
    counterpart `src_NNNN.mkv` directly inside the workdir. Returns pooled
    scores aggregated across the sampled chunks.

    Why this is the right approach for chunked-concat output: each chunk
    pair is small, naturally aligned (both files start at PTS 0), and
    requires no input seek. None of the seek/concat-PTS-drift problems
    that crater full-file segment sampling apply here.

    Must be called BEFORE `cleanup()` wipes the workdir."""
    src_chunks = sorted(workdir.glob("src_*.mkv"))
    if not src_chunks:
        return None

    indices = _pick_chunks_to_sample(len(src_chunks), n_chunks)

    # ONE overall bar across the sampled chunks: total = Σ their durations,
    # `done_s` accumulates as each finishes, so the bar fills 0→100% smoothly
    # over the whole pass instead of resetting per chunk.
    durations = [probe_duration(src_chunks[idx]) for idx in indices]
    total_s = sum(durations)
    bar = ProgressBar(progress_prefix) if progress_prefix is not None else None
    done_s = 0.0

    per_chunk: list[dict] = []
    for i, idx in enumerate(indices):
        src_chunk = src_chunks[idx]
        enc_chunk = workdir / f"enc_{src_chunk.name}"
        if not enc_chunk.exists():
            # Missing chunk — pre-concat sanity should have caught this, but be
            # defensive: skip it, still advancing the bar so totals stay honest.
            done_s += durations[i]
            continue

        on_progress = None
        if bar is not None:
            # Capture per-chunk base/label by value (default args) so the
            # closure isn't bitten by late-binding of the loop variables.
            def on_progress(out_s, fps, speed, _base=done_s,
                            _label=f"chunk {i+1}/{len(indices)}"):
                bar.update(done_s=_base + out_s, total_s=total_s,
                           fps=fps, speed=speed, suffix=_label)
        scores = _quality_check_run(
            src_chunk, enc_chunk,
            subsample=subsample,
            on_progress=on_progress,
        )
        done_s += durations[i]
        if scores is not None:
            scores["_chunk_index"] = idx
            per_chunk.append(scores)

    if bar is not None:
        bar.finish()
    if not per_chunk:
        return None

    def aggregate(key: str, op: str = "mean") -> float | None:
        vals = [s.get(key) for s in per_chunk if s.get(key) is not None]
        if not vals:
            return None
        return min(vals) if op == "min" else sum(vals) / len(vals)

    chunk_indices = [s.get("_chunk_index") for s in per_chunk]
    return {
        "method": "chunks",
        "vmaf_mean": aggregate("vmaf_mean", "mean"),
        "vmaf_min": aggregate("vmaf_min", "min"),
        "vmaf_harmonic_mean": aggregate("vmaf_harmonic_mean", "mean"),
        "psnr_y_mean": aggregate("psnr_y_mean", "mean"),
        "ssim_mean": aggregate("ssim_mean", "mean"),
        "frames_evaluated": sum(s.get("frames_evaluated", 0) or 0 for s in per_chunk),
        "sampling_mode": (f"{len(per_chunk)} of {len(src_chunks)} chunks "
                         f"(1-indexed: {[i+1 for i in chunk_indices]})"),
        "chunk_indices": chunk_indices,
        "per_chunk_vmaf_mean": [
            round(s.get("vmaf_mean"), 2) if s.get("vmaf_mean") is not None else None
            for s in per_chunk
        ],
        "per_chunk_vmaf_min": [
            round(s.get("vmaf_min"), 2) if s.get("vmaf_min") is not None else None
            for s in per_chunk
        ],
    }


def workdir_has_chunks(workdir: Path | None) -> bool:
    """True iff `workdir` exists and contains at least one usable
    `src_NNNN.mkv` / `enc_src_NNNN.mkv` pair (final, not `.part`)."""
    if workdir is None or not workdir.is_dir():
        return False
    for src_chunk in workdir.glob("src_*.mkv"):
        enc = workdir / f"enc_{src_chunk.name}"
        if enc.exists() and ".part" not in enc.suffixes:
            return True
    return False


def quality_check_auto(src: Path, dst: Path, *,
                      workdir: Path | None = None,
                      subsample: int = 10,
                      n_chunks: int = 3,
                      progress_prefix: str | None = None) -> dict | None:
    """Quality-check dispatcher.

    If `workdir` exists and contains paired chunks, runs per-chunk VMAF
    (fast, no seek/PTS-drift issues). Otherwise walks the full merged
    output end-to-end (slower but works on any pair). The returned dict's
    `method` field records which path actually ran ("chunks" or "full")."""
    if workdir_has_chunks(workdir):
        return quality_check_chunks(
            workdir, subsample=subsample, n_chunks=n_chunks,
            progress_prefix=progress_prefix,
        )
    return quality_check(
        src, dst, subsample=subsample, mode="full",
        progress_prefix=progress_prefix,
    )


def quality_check(src: Path, dst: Path, *,
                 subsample: int = 10,
                 mode: str = "full",
                 num_segments: int = 5,
                 segment_sec: int = 30,
                 progress_prefix: str | None = None) -> dict | None:
    """Compare distorted target vs reference source using libvmaf (which also
    emits PSNR and SSIM in the same pass). Returns pooled scores or None.

    `mode`:
      "full" (default, reliable):
          Walks the entire file, decoding every frame of both inputs and
          computing VMAF on every `subsample`-th. Slow but accurate.

      "segments" (FRAGILE — broken for chunked/concat'd outputs):
          Samples `num_segments` evenly-spaced sections of `segment_sec`
          seconds each, scattered between 10% and 90% of source duration.
          Faster but assumes both inputs have stable matching timestamps
          after `-ss` input seek. That assumption holds for single-pass
          re-encoded outputs, but FAILS for outputs produced by ffmpeg
          `concat -c copy` over independently-encoded chunks (the
          per-chunk PTS drift causes the two inputs to land on different
          content after seek, and VMAF craters). The resumable encoder
          here produces concat'd output, so this mode is wrong by
          default — `mode="full"` is the only safe choice for it.

    Short files (where `num_segments × segment_sec × 2` exceeds source
    duration) automatically fall back to "full" mode."""
    if mode not in ("segments", "full"):
        mode = "segments"

    src_dur = probe_duration(src)
    if src_dur <= 0:
        return None

    # Segment-sampling mode (the fast default)
    if mode == "segments" and src_dur >= num_segments * segment_sec * 2:
        # Evenly spread N positions between 10% and 90% of duration so we
        # cover the encoder's behavior across the file without ever landing
        # right at the start or end (where chunk-boundary artifacts cluster).
        if num_segments == 1:
            positions = [src_dur * 0.5]
        else:
            positions = [
                src_dur * (0.1 + 0.8 * i / (num_segments - 1))
                for i in range(num_segments)
            ]

        # One overall bar across the sampled segments (same scheme as chunks).
        seg_total_s = len(positions) * segment_sec
        seg_bar = (ProgressBar(progress_prefix)
                   if progress_prefix is not None else None)
        seg_done_s = 0.0

        per_seg: list[dict] = []
        for i, start in enumerate(positions):
            on_progress = None
            if seg_bar is not None:
                def on_progress(out_s, fps, speed, _base=seg_done_s,
                                _label=f"seg {i+1}/{len(positions)}"):
                    seg_bar.update(done_s=_base + out_s, total_s=seg_total_s,
                                   fps=fps, speed=speed, suffix=_label)
            scores = _quality_check_run(
                src, dst,
                subsample=subsample,
                seek_start=start,
                duration=segment_sec,
                on_progress=on_progress,
            )
            seg_done_s += segment_sec
            if scores is not None:
                scores["_segment_start_sec"] = round(start, 1)
                per_seg.append(scores)

        if seg_bar is not None:
            seg_bar.finish()

        if not per_seg:
            return None

        def aggregate(key: str, op: str = "mean") -> float | None:
            vals = [s.get(key) for s in per_seg if s.get(key) is not None]
            if not vals:
                return None
            return min(vals) if op == "min" else sum(vals) / len(vals)

        return {
            "method": "segments",
            "vmaf_mean": aggregate("vmaf_mean", "mean"),
            "vmaf_min": aggregate("vmaf_min", "min"),
            "vmaf_harmonic_mean": aggregate("vmaf_harmonic_mean", "mean"),
            "psnr_y_mean": aggregate("psnr_y_mean", "mean"),
            "ssim_mean": aggregate("ssim_mean", "mean"),
            "frames_evaluated": sum(s.get("frames_evaluated", 0) or 0 for s in per_seg),
            "sampling_mode": f"{len(per_seg)} segments x {segment_sec}s",
            "segment_starts_sec": [s.get("_segment_start_sec") for s in per_seg],
            "per_segment_vmaf_mean": [
                round(s.get("vmaf_mean"), 2) if s.get("vmaf_mean") is not None else None
                for s in per_seg
            ],
        }

    # Full-file mode (slow exhaustive) — the single run IS the whole pass.
    bar = ProgressBar(progress_prefix) if progress_prefix is not None else None
    on_progress = None
    if bar is not None:
        def on_progress(out_s, fps, speed):
            bar.update(done_s=out_s, total_s=src_dur, fps=fps, speed=speed)
    scores = _quality_check_run(
        src, dst, subsample=subsample, on_progress=on_progress,
    )
    if bar is not None:
        bar.finish()
    if scores is not None:
        scores["method"] = "full"
        scores["sampling_mode"] = "full file"
    return scores


# format_quality_summary lives in quality_format.py — kept separate so the
# measurement core (this file) doesn't depend on / churn with presentation
# tweaks. Re-exported here so existing `from quality import format_quality_summary`
# call sites keep working without an import change.
from .quality_format import format_quality_summary  # noqa: F401, E402

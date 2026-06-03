"""Low-level libvmaf primitives: single ffmpeg+libvmaf invocation comparing
two video files, with the fps-normalization workaround the concat-mode path
depends on.

Split out of ``quality.py`` (which was nudging the 500-line cap when the
per-chunk QualityGuard added ``vmaf_pair``). The split is by altitude:

  * **This module (`quality_libvmaf.py`)** — pure single-pair primitives.
    Build one filtergraph node, build one ffmpeg argv, parse one JSON log,
    run one ffmpeg child end-to-end. No knowledge of chunks, workdirs, modes,
    or which file to compare against.

  * **`quality.py`** — chooses which pair(s) to compare and aggregates results
    across chunks. The chunk-sampling / full-file / auto-dispatcher logic.

Why fps normalization matters: the concat'd output reports a slightly drifted
container fps even when frame counts match exactly (e.g. src=50/1 →
dst=18649/373≈49.997). ffmpeg's framesync pairs frames by PTS — under
mismatched fps it silently drops or duplicates one stream and pairs misaligned
frames at certain instants. We've seen this drop VMAF from a true ~97.5 to a
bogus 17.36 on a 43-min file. The cure is `-r src_fps` as an INPUT option on
BOTH inputs — see [[reference_vmaf_concat_fps_gotcha]]."""
from __future__ import annotations

import json
import os as _os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority

from .probes import probe_fps
from .progress_bar import read_ffmpeg_progress


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
    The .mkv built from `ffmpeg -f concat -c copy` reports a slightly drifted
    container fps (e.g. src=50/1 → dst=18649/373≈49.997) even when frame
    counts match exactly. ffmpeg's framesync pairs frames between libvmaf's
    two inputs by PTS — with mismatched fps it silently drops or duplicates
    one stream to keep pace, pairing misaligned frames at certain instants.
    Passing `-r src_fps` as an INPUT option tells ffmpeg "ignore stored PTSs,
    generate new ones assuming constant fps"; with the same value on both
    sides, frame *i* of source pairs with frame *i* of target deterministically.

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
                      = None,
                      low_priority: bool = True) -> dict | None:
    """One ffmpeg+libvmaf invocation comparing `src` vs `dst`. Shared by
    full-file mode (no seek_start/duration) and segment-sampling mode (both
    set). The fps normalization that makes scores honest on concat'd outputs
    lives in `_build_libvmaf_cmd`; the JSON parser in `_parse_vmaf_log`.

    `on_progress(out_time_s, fps, speed)` is fired once per ffmpeg `-progress`
    tick; the CALLER owns rendering (so quality_check_chunks can draw one
    overall bar across many runs). Returns a dict of metrics, or None on any
    failure.

    `low_priority` (default True) wraps the ffmpeg child in `nice -n 19` /
    IDLE_PRIORITY_CLASS to match the rest of the encoder. The per-chunk
    QualityGuard (encode_modules.quality_guard) passes False because the
    quality check should preempt the encoder — until a chunk is judged, the
    encoder doesn't know whether to keep spending CPU on its file."""
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
        popen_cmd = wrap_cmd_for_low_priority(cmd) if low_priority else list(cmd)
        # POSIX `low_priority_popen_kwargs` returns `start_new_session=True`
        # which is a LIFECYCLE knob (killpg); we keep it on regardless of
        # the priority bypass so the guard's ffmpeg still cleans up if the
        # encoder dies hard. Windows' creationflags ARE priority-only — only
        # honour them under low_priority=True.
        if low_priority:
            popen_kwargs = low_priority_popen_kwargs()
        else:
            from platform_compat import IS_POSIX
            popen_kwargs = {"start_new_session": True} if IS_POSIX else {}
        proc = subprocess.Popen(
            popen_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **popen_kwargs,
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
        # Never leak the ffmpeg+libvmaf child. Unlike subprocess.run, a Popen
        # is not reaped when the read loop raises (decode error mid-stream) or
        # the user Ctrl-Cs during a multi-minute pass — terminate it before
        # leaving.
        if proc is not None and proc.poll() is None:
            # Guard the whole teardown: a terminate()/wait() that itself
            # raises (e.g. a Windows handle race) must not skip the log
            # unlink below.
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


def vmaf_pair(src: Path, dst: Path, *, subsample: int = 10) -> dict | None:
    """Public single-pair libvmaf comparator: runs `src` vs `dst` end-to-end
    at NORMAL CPU priority (no `nice -n 19` wrap) and returns a dict with
    vmaf_mean / vmaf_min / vmaf_harmonic_mean / psnr_mean / ssim_mean — or
    None on any failure (libvmaf crashed, log unparseable).

    This is the entry point that ``encode_modules.quality_guard.QualityGuard``
    wires up for per-chunk threshold checks: the guard runs in parallel with
    the next chunk's encode, and the quality check must preempt the encoder
    so abort decisions land before the encoder wastes more CPU."""
    return _quality_check_run(src, dst, subsample=subsample, low_priority=False)

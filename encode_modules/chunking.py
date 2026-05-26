"""Chunk-level operations: split source, build ffmpeg encode cmd, reorder
for projection accuracy, concat, cleanup.

These are the non-encode-loop pieces of the resumable pipeline — the steps
that bracket the actual `ffmpeg -i src_NNNN.mkv -c:v libx265 ...` invocations
in the encoder module. Kept separate so the encoder can focus on the
scheduling/display loop without the lossless-mux details and chunk-ordering
heuristic in the same file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority

from .probes import probe_duration
from .progress_bar import ProgressBar, read_ffmpeg_progress


def split_source(src: Path, workdir: Path, seg_sec: int) -> list[Path]:
    """Lossless segment (-c copy) of `src` into ~seg_sec-second chunks,
    keyframe-aligned. Idempotent — re-runs see the .split_done marker and
    return the existing chunk list without re-splitting."""
    flag = workdir / ".split_done"
    if flag.exists():
        chunks = sorted(workdir.glob("src_*.mkv"))
        print(f"[1/4] Split already done ({len(chunks)} chunks).")
        return chunks

    workdir.mkdir(parents=True, exist_ok=True)
    print(f"[1/4] Splitting source losslessly into ~{seg_sec}-sec chunks...")
    r = subprocess.run(wrap_cmd_for_low_priority([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0", "-map", "-0:d",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(seg_sec),
        "-reset_timestamps", "1",
        # ffmpeg's segment muxer parses the WHOLE output path for printf-style
        # conversion tokens, not just the basename. Escape any literal % in the
        # workdir (e.g. a source named "70% Hell") to %% so only the intended
        # src_%04d.mkv pattern is treated as a format string — otherwise ffmpeg
        # reads "% H" as an invalid spec and rejects the template (exit 234).
        # Escaping only the argument (not the on-disk dir) keeps the
        # .compress_<stem> / .split_done resume convention intact.
        os.path.join(str(workdir).replace("%", "%%"), "src_%04d.mkv"),
    ]), **low_priority_popen_kwargs())
    if r.returncode != 0:
        sys.exit(f"ERROR: segmenter failed (exit {r.returncode})")
    flag.touch()
    chunks = sorted(workdir.glob("src_*.mkv"))
    print(f"      -> {len(chunks)} chunks created")
    return chunks


def reorder_middle_first(chunks: list[Path]) -> list[Path]:
    """Reorder a sorted chunk list so the median chunk encodes first, with
    remaining chunks alternating outward from there.

    The src_*.mkv filenames preserve original chunk order, and concat reads
    them by name, so reordering only changes the **encoding** sequence — the
    final stitched output is identical.

    Why: byte-rate during the first ~5% of encode dominates the projection
    used by `--max-size-percent`. With front-first ordering, intro chunks
    tend to be lower-bitrate than the source average, so the projection
    under-estimates and the threshold check fires LATE (or not at all,
    letting a doomed encode finish anyway — the symptom that prompted this
    change). For the kind of clips the user typically encodes, motion-heavy
    material clusters near the middle, so kicking the median chunk off
    first puts a representative high-bitrate sample in the projection cache
    early. Then alternating outward (median+1, median-1, median+2, ...)
    spreads the next data points across the file, so by the time the gate
    opens the projection is honest rather than biased toward one segment.

    For N=10: 0-indexed median = (N-1)//2 = 4 → 1-indexed chunk 5.
        Output 1-indexed: [5, 6, 4, 7, 3, 8, 2, 9, 1, 10]
    For N=2:  0-indexed median = 0 → 1-indexed chunk 1.
        Output 1-indexed: [1, 2]    (with only two chunks each covering 50%
        of the source, there's no clean "middle" to pick — chunk 1 is the
        deterministic lower-half choice).
    For N=1: returned unchanged.
    """
    n = len(chunks)
    if n <= 1:
        return list(chunks)
    mid = (n - 1) // 2
    order: list[int] = [mid]
    step = 1
    while len(order) < n:
        forward = mid + step
        backward = mid - step
        if forward < n:
            order.append(forward)
        if backward >= 0:
            order.append(backward)
        step += 1
    return [chunks[i] for i in order]


def ffmpeg_chunk_cmd(chunk: Path, part: Path, *, crf: int, preset: str,
                    pix_fmt: str, x265_params: str,
                    extra_progress: list[str] | None = None) -> list[str]:
    """Build the ffmpeg command that encodes a single chunk to libx265.
    Always writes to a .part path; caller renames to the final .mkv on success.
    `extra_progress` lets the parallel encoder request `-progress -` so it can
    parse fps/speed/out_time off ffmpeg's stdout."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
    ]
    if extra_progress:
        cmd.extend(extra_progress)
    cmd.extend([
        "-i", str(chunk),
        "-map", "0", "-map", "-0:d",
        "-c:v", "libx265", "-preset", preset, "-crf", str(crf),
        "-x265-params", x265_params,
        "-pix_fmt", pix_fmt,
        "-c:a", "copy", "-c:s", "copy",
        str(part),
    ])
    return cmd


def unlink_with_retry(path: Path, *, attempts: int = 8, base_delay: float = 0.25) -> None:
    """Best-effort unlink that survives Windows transient file locks.

    Windows holds file handles briefly after the owning process exits, and AV
    scanners (Defender) can grab a read handle the moment a file appears on
    disk. Either causes WinError 32 on unlink. Retry with exponential backoff;
    re-raise only if it never clears."""
    last_exc: OSError | None = None
    for i in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as e:
            last_exc = e
            time.sleep(base_delay * (2 ** i))
    if last_exc is not None:
        raise last_exc


def x265_params_with_pools(x265_params: str, parallel: int) -> str:
    """If running >1 chunks in parallel, cap each x265 instance's thread pool
    so they don't all fight for the same cores. Single-pool only — most
    consumer CPUs are single-NUMA-node."""
    if parallel <= 1:
        return x265_params
    cpu_count = os.cpu_count() or 8
    cores_per_chunk = max(2, cpu_count // parallel)
    return f"{x265_params}:pools={cores_per_chunk}"


def concat_chunks(workdir: Path, dst: Path, *,
                  total_dur: float | None = None) -> None:
    """Lossless concat of all enc_src_*.mkv chunks in workdir into `dst`.
    Refuses to concat if any src_*.mkv lacks a paired enc_src_*.mkv — that
    silent-truncation guard sits AHEAD of the concat so we never splice an
    incomplete set together and then wipe the workdir in cleanup.

    `total_dur` (seconds) scales the progress bar; the caller passes the value
    it already computed. When omitted it's probed from the encoded chunks, so
    standalone callers still get a bar (just at the cost of a few probes)."""
    src_chunks = sorted(workdir.glob("src_*.mkv"))
    expected = {workdir / f"enc_{c.name}" for c in src_chunks}
    missing = sorted(p for p in expected if not p.exists())
    if missing:
        names = ", ".join(p.name for p in missing)
        sys.exit(
            f"ERROR: refusing to concat — {len(missing)} of {len(src_chunks)} "
            f"chunks were never produced: {names}. Re-run to retry the missing "
            f"chunks; workdir preserved."
        )

    # Only finalized chunks. `enc_*.mkv` would also match `enc_*.part.mkv`
    # (in-flight or abandoned), which would splice corrupt segments into the
    # final video.
    encs = sorted(c for c in workdir.glob("enc_*.mkv") if ".part" not in c.suffixes)
    if not encs:
        sys.exit("ERROR: no encoded chunks to concatenate")
    concat_list = workdir / "concat.txt"
    # ffmpeg concat demuxer wants forward-slash paths inside single quotes.
    concat_list.write_text(
        "\n".join(f"file '{c.resolve().as_posix()}'" for c in encs) + "\n",
        encoding="utf-8",
    )
    if total_dur is None:
        total_dur = sum(probe_duration(c) for c in encs)
    # -fflags +genpts regenerates clean monotonic PTS/DTS during the
    # concat copy. Without it, per-chunk DTS quirks at chunk seams trip
    # verify_output's `-xerror` decode walk; dts_recovery then has to run
    # a costly MPEG-TS roundtrip (~5 min on a 2.5 GB output) to clean
    # them up. Setting +genpts preemptively prevents that on most files,
    # and is no-op when timestamps are already clean.
    # -progress pipe:1 + -nostats stream machine-readable progress to stdout so
    # we can draw a bar; ffmpeg's own logs/errors still go to inherited stderr.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]
    bar = ProgressBar(f"[3/4] Concatenating {len(encs)} chunks")
    proc = None
    rc = 1
    try:
        proc = subprocess.Popen(
            wrap_cmd_for_low_priority(cmd),
            stdout=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", **low_priority_popen_kwargs(),
        )

        def _tick(state: "dict[str, str]") -> None:
            try:
                out_s = float(state.get("out_time_us", "0")) / 1_000_000
            except ValueError:
                return
            bar.update(done_s=out_s, total_s=total_dur,
                       speed=state.get("speed", "?").strip())

        read_ffmpeg_progress(proc.stdout, _tick)
        rc = proc.wait()
    except BaseException:
        # Never leak the concat ffmpeg on error / Ctrl-C (subprocess discipline).
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise
    finally:
        bar.finish()
    if rc != 0:
        sys.exit(f"ERROR: concat failed (exit {rc})")


def cleanup(workdir: Path) -> None:
    """Remove the chunk workdir after a successful encode + verify."""
    print(f"[4/4] Cleaning up {workdir}")
    shutil.rmtree(workdir, ignore_errors=True)

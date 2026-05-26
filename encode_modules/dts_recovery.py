"""DTS-fix remux: MPEG-TS roundtrip to scrub non-monotonic DTS from a
chunked-concat output.

Why this exists
---------------
Some x265-encoded chunks produce internal DTS patterns that, when stitched
via ffmpeg's concat demuxer, end up with scattered duplicate DTS values in
the merged mkv. ffmpeg's `_decode_check` runs the walk with `-xerror` which
treats those duplicates as errors — so `verify_output` returns a failure
even though:

  - Every chunk decodes clean in isolation (per-chunk decode-walk passes).
  - The merged file decodes end-to-end with rc=0 (the warnings are stderr
    noise, not fatal errors).
  - Every media player on the planet (VLC, mpv, MX Player, Windows Media
    Player, Galaxy player) plays the file without artifacts.

The fix is purely metadata: pump the merged file through an MPEG-TS
container, then back to MKV with `+genpts` + `avoid_negative_ts make_zero`.
The roundtrip drops the bad DTS values and ffmpeg regenerates clean
monotonic timestamps. The encoded video bytes are untouched.

Observed and validated on Emily PATCHED (2026-05-22):
  - 19 DTS collisions in the merged output (all inside chunk 0000).
  - After roundtrip: 0 errors on full decode-walk. File visually identical.

When to call it
---------------
`_run_encode_verify_loop` calls `is_dts_only_verify_failure(problems)` on
the result of `verify_output`. If true, it calls `attempt_dts_fix_remux`
ONCE before falling through to the upstream-issue diagnostic. The retry is
free relative to a full re-encode (~5 min on 2.5 GB output vs hours).

Safety
------
- Old `dst` is renamed to `<stem>.pre-dts-fix-<ts>{suffix}` before the new
  file is moved in. NEVER unlinked — per the never-delete rule, the
  pre-fix bytes are forensic.
- The MPEG-TS intermediate lives in `dst.parent`; cleaned on success,
  preserved on failure.
- Codec-agnostic: probes video codec to pick `h264_mp4toannexb` /
  `hevc_mp4toannexb`. `aac_adtstoasc` is unconditional on the
  return-to-mkv leg — ffmpeg ignores it for non-ADTS audio.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from platform_compat import low_priority_popen_kwargs, wrap_cmd_for_low_priority


# stderr marker emitted by ffmpeg's muxer when it sees `cur_dts <= prev_dts`.
# Picked specifically over a generic decode-error marker so we don't
# accidentally trigger on a genuine data-corruption failure that an
# MPEG-TS roundtrip won't fix.
DTS_MARKER = "non monotonically increasing dts"


def is_dts_only_verify_failure(problems: list[str]) -> bool:
    """True if every entry in `problems` mentions the DTS marker — the class
    of verify failure that `attempt_dts_fix_remux` can clear without
    re-encoding. False for empty input (no failure means nothing to fix)
    and False for any failure that lacks the marker (could be a real
    structural issue, retry would mask it)."""
    if not problems:
        return False
    return all(DTS_MARKER in p for p in problems)


def _probe_codec(path: Path, stream_type: str) -> Optional[str]:
    """Return the codec_name of the first stream of `stream_type` ("v" or
    "a") in `path`, or None on any probe failure. Used to pick the
    annex-b bitstream filter; falls back to "hevc" for video / "aac" for
    audio if probing fails (matches what the encoder produces by default)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-select_streams", stream_type, str(path)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            return None
        streams = json.loads(r.stdout).get("streams") or []
        if not streams:
            return None
        return streams[0].get("codec_name")
    except Exception:
        return None


def attempt_dts_fix_remux(dst: Path) -> bool:
    """Run an MPEG-TS roundtrip on `dst` to rebuild monotonic DTS. Returns
    True iff a fixed `dst` is in place; False if any step failed and `dst`
    is unchanged.

    On success, the old `dst` is renamed to
    `<stem>.pre-dts-fix-<ts><suffix>` in the same directory — kept as
    forensic per the never-delete rule. The MPEG-TS intermediate is
    cleaned up on success and preserved on failure (lives next to `dst`).

    Safe to call when `dst` doesn't exist — returns False without raising."""
    if not dst.exists():
        return False

    video_codec = _probe_codec(dst, "v") or "hevc"
    if video_codec == "h264":
        v_bsf = "h264_mp4toannexb"
    elif video_codec in ("hevc", "h265"):
        v_bsf = "hevc_mp4toannexb"
    else:
        # Unknown video codec — bail; better not to risk a bad remux on
        # something the BSF list doesn't cover.
        return False

    ts_path = dst.with_suffix(dst.suffix + ".dtsfix.ts")
    new_path = dst.with_suffix(dst.suffix + ".dtsfix.tmp")

    # Leg 1: mkv -> mpegts. Strips container-level DTS quirks.
    r1 = subprocess.run(
        wrap_cmd_for_low_priority(
            ["ffmpeg", "-v", "error", "-hide_banner", "-y",
             "-i", str(dst),
             "-c", "copy",
             "-bsf:v", v_bsf,
             "-f", "mpegts", str(ts_path)]
        ),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **low_priority_popen_kwargs(),
    )
    if r1.returncode != 0:
        # Don't leave a partial ts file silently — keep it for inspection,
        # but log so the user knows it's there.
        print(f"[DTS-fix] MPEG-TS pack failed (rc={r1.returncode}); "
              f"intermediate left at {ts_path}",
              file=sys.stderr)
        return False

    # Leg 2: mpegts -> mkv with regenerated PTS/DTS.
    r2 = subprocess.run(
        wrap_cmd_for_low_priority(
            ["ffmpeg", "-v", "error", "-hide_banner", "-y",
             "-fflags", "+genpts",
             "-i", str(ts_path),
             "-c", "copy",
             "-bsf:a", "aac_adtstoasc",
             "-avoid_negative_ts", "make_zero",
             str(new_path)]
        ),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **low_priority_popen_kwargs(),
    )
    if r2.returncode != 0:
        print(f"[DTS-fix] Remux back to mkv failed (rc={r2.returncode}); "
              f"intermediate ts at {ts_path}, partial mkv at {new_path}",
              file=sys.stderr)
        return False

    # Swap files: old dst gets renamed aside, new file moves into place.
    stamp = int(time.time())
    aside = dst.with_name(f"{dst.stem}.pre-dts-fix-{stamp}{dst.suffix}")
    try:
        dst.rename(aside)
    except OSError as e:
        print(f"[DTS-fix] Could not rename old dst aside: {e}", file=sys.stderr)
        return False
    try:
        new_path.rename(dst)
    except OSError as e:
        # Try to roll back the aside rename so the user isn't left with
        # neither file in `dst`.
        print(f"[DTS-fix] Could not move new file into dst: {e}; "
              "rolling back rename", file=sys.stderr)
        try:
            aside.rename(dst)
        except OSError:
            pass
        return False

    # Clean up the ts intermediate (success path).
    try:
        ts_path.unlink()
    except OSError:
        pass
    return True

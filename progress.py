"""
Render ffmpeg's -progress output as a percentage bar with ETA.

Pipe ffmpeg into this script. Total source duration is passed as --duration
(in seconds) so it can compute percent done and remaining wall time:

    ffmpeg ... -progress - ... | python -u progress.py --duration 1846.37

What you see (updates in place every ~500 ms):

    [#############-----------] 53.4 %  16:21 / 30:46  fps=  15  speed=0.27x  ETA  1:12:30

Exits non-zero if ffmpeg never emits `progress=end` — that catches the
"ffmpeg crashed mid-encode" case so the calling .bat can report a real error.
"""

from __future__ import annotations

import argparse
import sys
import time


def fmt_time(seconds: float | int | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:  # None or NaN or negative
        return "?:??:??"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"


def render(pct: float, out_time_s: float, total_s: float,
           fps: str, speed: str, eta_s: float) -> str:
    bar_w = 24
    filled = max(0, min(bar_w, int(round(pct / 100 * bar_w))))
    bar = "#" * filled + "-" * (bar_w - filled)
    # Trailing spaces overwrite any leftover chars from a previous longer line.
    return (
        f"\r[{bar}] {pct:5.1f} %  "
        f"{fmt_time(out_time_s)} / {fmt_time(total_s)}  "
        f"fps={fps:>5}  speed={speed:>6}  "
        f"ETA {fmt_time(eta_s)}      "
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, required=True,
                    help="Source duration in seconds (from ffprobe).")
    args = ap.parse_args()
    total = max(0.001, args.duration)
    # In-place \r updates are for a terminal; piped to a log/CI they become
    # carriage-return spam. Non-tty falls back to throttled newline lines.
    is_tty = sys.stdout.isatty()
    last_emit = 0.0
    last_pct = -100.0

    state: dict[str, str] = {}
    saw_end = False

    try:
        for raw in sys.stdin:
            line = raw.rstrip()
            if not line or "=" not in line:
                continue
            key, val = line.split("=", 1)
            state[key] = val

            if key != "progress":
                continue  # block isn't complete yet

            try:
                out_time_us = float(state.get("out_time_us", "0") or 0)
            except ValueError:
                out_time_us = 0.0
            out_time_s = out_time_us / 1_000_000
            pct = max(0.0, min(100.0, out_time_s / total * 100))

            fps = state.get("fps", "?")
            speed_raw = state.get("speed", "?")
            try:
                speed = float(speed_raw.rstrip("x"))
            except ValueError:
                speed = 0.0

            eta_s = (total - out_time_s) / speed if speed > 0 else 0.0
            line = render(pct, out_time_s, total, fps, speed_raw, eta_s)
            if is_tty:
                sys.stdout.write(line)
                sys.stdout.flush()
            elif (val == "end" or pct - last_pct >= 5.0
                    or time.monotonic() - last_emit >= 30.0):
                # Plain, newline-terminated, throttled — readable in a log.
                last_pct, last_emit = pct, time.monotonic()
                sys.stdout.write(line.replace("\r", "").rstrip() + "\n")
                sys.stdout.flush()

            if val == "end":
                saw_end = True
                break
    except KeyboardInterrupt:
        pass

    if is_tty:
        # Terminate the in-place bar line. Non-tty already emits full lines.
        sys.stdout.write("\n")
        sys.stdout.flush()
    return 0 if saw_end else 1


if __name__ == "__main__":
    sys.exit(main())

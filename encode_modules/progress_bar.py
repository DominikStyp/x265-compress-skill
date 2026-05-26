"""A single-line progress bar for the phases that run AFTER the live
`ParallelDisplay` tears down — concat (phase 3) and quality (phase 4).

It draws the same `[#####-----] NN.N%  H:MM:SS / H:MM:SS` look the encode phase
uses (reusing `display_render.bar_fill` and `formatting.format_hms`, so the
glyphs and time format never drift), and adapts to its output:

  - **TTY:** rewrite in place with `\\r … \\033[K` on every tick.
  - **pipe / headless:** print a fresh line only when progress advanced by at
    least `_PIPE_STEP_PCT`, so a redirected log isn't drowned in updates.

The output stream and `is_tty` are injectable so tests never touch a real
terminal. This is intentionally NOT the multi-slot live display (that stays in
`display.py`); it's the lightweight single-line counterpart for serial phases.
"""
from __future__ import annotations

import sys
from typing import Callable, Iterable, Mapping, Optional, TextIO

from formatting import format_hms

from .display_render import C_GREEN, C_RESET, bar_fill

# Headless/pipe throttle: only reprint once progress has advanced this many
# percentage-points since the last printed line.
_PIPE_STEP_PCT = 10.0


def read_ffmpeg_progress(stdout: Iterable[str],
                         on_tick: Callable[[Mapping[str, str]], None]) -> None:
    """Drive `on_tick` from ffmpeg's `-progress` key=value stream.

    ffmpeg emits one stats block per interval as `key=value` lines terminated
    by a `progress=continue` (or `progress=end`) line. We accumulate the latest
    values into a dict and call `on_tick(state)` once per completed block, so
    the callback always sees a consistent snapshot (out_time_us, fps, speed, …).
    Shared by concat and the VMAF runner so the parse loop isn't re-inlined."""
    state: dict[str, str] = {}
    for raw in stdout:
        line = raw.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        state[key] = value
        if key == "progress":  # end of one stats block
            on_tick(state)


class ProgressBar:
    """Render progress for one serial phase. `prefix` labels the line (e.g.
    "[3/4] Concatenating" or "Quality check"). Call `update()` per tick and
    `finish()` once at the end to clear the in-place line on a TTY."""

    def __init__(self, prefix: str, *, is_tty: Optional[bool] = None,
                 stream: Optional[TextIO] = None) -> None:
        self._prefix = prefix
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self._is_tty = (self._stream.isatty() if is_tty is None else is_tty)
        self._last_pipe_pct = -1000.0  # force the first pipe line to print
        self._drew_inplace = False

    def update(self, *, done_s: float, total_s: float,
               fps: str = "?", speed: str = "?", suffix: str = "") -> None:
        """Render one tick. `done_s`/`total_s` drive the bar; `fps`/`speed`
        come straight from ffmpeg's `-progress` stream ("?" = omit); `suffix`
        is an optional trailing note (e.g. "chunk 2/3")."""
        pct = 0.0 if total_s <= 0 else min(100.0, done_s / total_s * 100.0)
        line = self._format(pct, done_s, total_s, fps, speed, suffix)
        if self._is_tty:
            self._stream.write(f"\r{line}\033[K")
            self._stream.flush()
            self._drew_inplace = True
        elif pct - self._last_pipe_pct >= _PIPE_STEP_PCT:
            print(line, file=self._stream, flush=True)
            self._last_pipe_pct = pct

    def finish(self) -> None:
        """Clear the in-place TTY line so the next print starts clean. No-op on
        a pipe (nothing was left dangling there)."""
        if self._is_tty and self._drew_inplace:
            self._stream.write("\r\033[K")
            self._stream.flush()
            self._drew_inplace = False

    def _format(self, pct: float, done_s: float, total_s: float,
                fps: str, speed: str, suffix: str) -> str:
        fill = bar_fill(pct)
        bar = f"{C_GREEN}{fill}{C_RESET}" if self._is_tty else fill
        body = (f"{self._prefix}  [{bar}]  {pct:5.1f}%  "
                f"{format_hms(done_s)} / {format_hms(total_s)}")
        if suffix:
            body += f"  {suffix}"
        if fps and fps != "?":
            body += f"  {fps} fps"
        if speed and speed != "?":
            body += f"  {speed}"
        return body

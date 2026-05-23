"""Non-blocking keyboard listener for the parallel encode display.

Runs in its own thread. Single-byte reads are delegated to
`platform_compat.read_key_byte` so the keystroke-decoding logic here is
OS-agnostic: we receive raw bytes and dispatch from there.

Handled keys (slot numbers shown in the display are 1-based — the digit
you press matches the on-screen label):

    ↑ / ↓     move the focus cursor between slots
    Space     pause/resume the focused slot
    1..9      pause/resume slot N directly (so `1` = first slot)
    0         pause/resume slot 10 (only matters for --parallel 10)
    r / R     resume every paused slot
    ? / h     print the key list as an event in the live log

Arrow keys come in three forms; we handle all of them:
  • Legacy Windows conhost: 0xE0 (or 0x00) followed by `H` / `P` / `K` / `M`.
  • ConPTY / Windows Terminal: VT escape sequence ESC `[` `A`/`B`/`C`/`D`.
  • POSIX terminals (Terminal.app, iTerm2, xterm, etc.): same VT sequence
    as ConPTY — ESC `[` `A`/`B`/`C`/`D`. No special case needed.

Every handled key sets display.input_event so the render thread redraws
immediately instead of waiting for its next 500 ms tick.

No-op if platform_compat reports no keyboard input available (non-TTY
stdin — the queue runner's old DEVNULL flow would land here).
"""
from __future__ import annotations

import threading

from platform_compat import HAS_KEY_INPUT, read_key_byte


def _read_byte_within(timeout_s: float) -> bytes | None:
    """Pull one byte from the platform's keyboard input with a timeout.
    Used to assemble multi-byte sequences (legacy 0xE0+X, VT ESC[X) where
    the trailing bytes may arrive a few ms after the prefix."""
    return read_key_byte(timeout_s)


def keyboard_listener(display, stop_event: threading.Event) -> None:
    """Run forever in its own thread; mutate `display` in response to keys.
    Exits when `stop_event` is set. `display` is a `ParallelDisplay` (from
    the display module); not imported here to keep this module dependency-
    free so it can be tested standalone with a mock."""
    if not HAS_KEY_INPUT:
        return

    def signal() -> None:
        display.input_event.set()

    while not stop_event.is_set():
        # 50 ms poll — short enough that key presses feel responsive,
        # long enough that the loop doesn't burn CPU.
        ch = read_key_byte(0.05)
        if ch is None:
            continue

        # Legacy Windows conhost: arrow keys as 0xE0 (or 0x00) + key code.
        if ch in (b"\x00", b"\xe0"):
            ch2 = _read_byte_within(0.05) or b""
            if ch2 == b"H":      # up arrow
                display.move_focus(-1); signal()
            elif ch2 == b"P":    # down arrow
                display.move_focus(1); signal()
            continue

        # ConPTY / POSIX terminals: arrow keys as ESC [ A/B/C/D.
        if ch == b"\x1b":
            ch2 = _read_byte_within(0.02)
            if ch2 != b"[":
                # Plain Esc or an unrecognised sequence — eat one more byte
                # if present to keep the queue clean, then ignore.
                if ch2 is None:
                    continue
                _read_byte_within(0.01)
                continue
            ch3 = _read_byte_within(0.02)
            if ch3 == b"A":      # up
                display.move_focus(-1); signal()
            elif ch3 == b"B":    # down
                display.move_focus(1); signal()
            continue

        # Single-byte keys.
        if ch == b" ":
            display.events.put(display.toggle_pause(display.focused_slot))
            signal()
        elif ch in b"123456789":
            display.events.put(display.toggle_pause(int(ch) - 1))
            signal()
        elif ch == b"0":
            # '0' = slot 10 in 1-based labelling (internal index 9). Only
            # meaningful when --parallel >= 10; harmless otherwise.
            display.events.put(display.toggle_pause(9))
            signal()
        elif ch in (b"r", b"R"):
            for m in display.resume_all():
                display.events.put(m)
            signal()
        elif ch in (b"?", b"h"):
            # NOTE: lowercase only — capital `H` is the legacy arrow-up code
            # and would otherwise fire help on every up-arrow press.
            display.events.put(
                "  Keys: ↑↓=focus  Space=toggle  1-9=slot N  r=resume all  ?=help"
            )
            signal()

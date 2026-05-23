"""Non-blocking keyboard listener for the parallel encode display.

Runs in its own thread; uses msvcrt on Windows. Handled keys (slot numbers
shown in the display are 1-based — the digit you press matches the on-screen
label):

    ↑ / ↓     move the focus cursor between slots
    Space     pause/resume the focused slot
    1..9      pause/resume slot N directly (so `1` = first slot)
    0         pause/resume slot 10 (only matters for --parallel 10)
    r / R     resume every paused slot
    ? / h     print the key list as an event in the live log

Handles both arrow-key encodings:
  • Legacy conhost: 0xE0 (or 0x00) followed by `H` / `P` / `K` / `M`.
  • ConPTY / Windows Terminal (default on Win11): VT escape sequence
    ESC `[` `A` / `B` / `C` / `D`.

Every handled key sets display.input_event so the render thread redraws
immediately instead of waiting for its next 500 ms tick.

No-op if msvcrt isn't available (non-Windows) or stdin isn't a console
(the queue runner's old DEVNULL flow would land here).
"""
from __future__ import annotations

import threading
import time

try:
    import msvcrt  # type: ignore
    HAS_KEY_INPUT = True
except ImportError:
    HAS_KEY_INPUT = False


def keyboard_listener(display, stop_event: threading.Event) -> None:
    """Run forever in its own thread; mutate `display` in response to keys.
    Exits when `stop_event` is set. `display` is a `ParallelDisplay` (from
    the display module); not imported here to keep this module dependency-free
    so it can be tested standalone with a mock."""
    if not HAS_KEY_INPUT:
        return
    try:
        msvcrt.kbhit()
    except Exception:
        return

    def read_byte_within(timeout_s: float) -> bytes | None:
        """Wait up to timeout_s for one byte. Used to assemble multi-byte
        sequences (legacy 0xE0+X, VT ESC[X) where the trailing bytes may
        arrive a few ms after the prefix."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return msvcrt.getch()
            time.sleep(0.002)
        return None

    def signal() -> None:
        display.input_event.set()

    while not stop_event.is_set():
        try:
            if not msvcrt.kbhit():
                # Tight poll so keys feel responsive. The render thread is
                # the expensive consumer, not this loop.
                stop_event.wait(0.02)
                continue
            ch = msvcrt.getch()
        except Exception:
            return

        # Legacy console: arrow keys as 0xE0 (or 0x00) + key code.
        if ch in (b"\x00", b"\xe0"):
            ch2 = read_byte_within(0.05) or b""
            if ch2 == b"H":      # up arrow
                display.move_focus(-1); signal()
            elif ch2 == b"P":    # down arrow
                display.move_focus(1); signal()
            continue

        # ConPTY / Windows Terminal: arrow keys as ESC [ A/B/C/D.
        if ch == b"\x1b":
            ch2 = read_byte_within(0.02)
            if ch2 != b"[":
                # Plain Esc or an unrecognised sequence — eat one more byte
                # if present to keep the queue clean, then ignore.
                if ch2 is None:
                    continue
                read_byte_within(0.01)
                continue
            ch3 = read_byte_within(0.02)
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

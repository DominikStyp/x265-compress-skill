"""'Finish after the current chunk' request — the single source of truth both
encode loops consult between chunks.

A request can arrive two ways:
  * interactively, via the parallel display's `f` key  -> the in-memory flag
  * headless / serial, via a `<workdir>/FINISH` stop-file the operator creates
    (e.g. over SSH, where there's no keyboard)

Either one makes `requested` True. Neither interrupts an in-flight chunk — the
encoders only consult this at chunk boundaries, so the chunk currently encoding
always finishes. The stop-file is consumed once honored so a resumed run does
not immediately stop again.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional


# Sentinel filename inside the encode workdir. A user (or a script, over SSH)
# creates this file to ask for a graceful stop when no keyboard is available.
FINISH_FILENAME = "FINISH"


class FinishSignal:
    """Tracks whether the user has asked to finish after the current chunk.

    `requested` is True if the keyboard flag is set OR the stop-file exists.
    Thread-safe: the flag is a threading.Event (the keyboard listener runs in
    its own thread, the workers poll from theirs); the file check is a stat
    with no shared mutable state.
    """

    def __init__(self, stop_file: Optional[Path]) -> None:
        self._event = threading.Event()
        self._stop_file = stop_file

    @property
    def requested(self) -> bool:
        if self._event.is_set():
            return True
        if self._stop_file is not None:
            try:
                return self._stop_file.exists()
            except OSError:
                return False
        return False

    def request(self) -> None:
        """Keyboard `f` ON — ask to finish after the current chunk."""
        self._event.set()

    def cancel(self) -> None:
        """Keyboard `f` OFF — withdraw the in-memory request. A stop-file, if
        present, still counts; it is cleared by consume_stop_file() once the
        stop is actually honored."""
        self._event.clear()

    def toggle(self) -> bool:
        """Flip the in-memory flag (keyboard `f`) and return the new state
        (True = now requesting finish). Independent of the stop-file."""
        if self._event.is_set():
            self._event.clear()
            return False
        self._event.set()
        return True

    def consume_stop_file(self) -> None:
        """Delete the stop-file if present so a resumed run does not stop again
        immediately. Best-effort — never raises (a permission failure just
        means the next run re-stops, which is rare and self-explanatory)."""
        if self._stop_file is None:
            return
        try:
            self._stop_file.unlink()
        except OSError:
            pass

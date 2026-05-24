"""finish-after-current-chunk: FinishSignal is the single source of truth both
encode loops consult between chunks. `requested` is True if the keyboard flag
is set OR the <workdir>/FINISH stop-file exists; consume_stop_file() clears the
file so a resumed run doesn't immediately re-stop.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.finish_signal import FINISH_FILENAME, FinishSignal  # noqa: E402


class FinishSignalTest(unittest.TestCase):
    def test_default_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(FinishSignal(Path(td) / FINISH_FILENAME).requested)

    def test_keyboard_request_and_cancel(self) -> None:
        sig = FinishSignal(None)
        self.assertFalse(sig.requested)
        sig.request()
        self.assertTrue(sig.requested)
        sig.cancel()
        self.assertFalse(sig.requested)

    def test_stop_file_presence_requests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            stop = Path(td) / FINISH_FILENAME
            sig = FinishSignal(stop)
            self.assertFalse(sig.requested)
            stop.write_text("")  # a headless operator / script touches it
            self.assertTrue(sig.requested)

    def test_consume_stop_file_clears_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            stop = Path(td) / FINISH_FILENAME
            stop.write_text("")
            sig = FinishSignal(stop)
            self.assertTrue(sig.requested)
            sig.consume_stop_file()
            self.assertFalse(stop.exists())
            self.assertFalse(sig.requested)

    def test_consume_missing_file_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sig = FinishSignal(Path(td) / FINISH_FILENAME)
            sig.consume_stop_file()  # no file present — must not raise
            self.assertFalse(sig.requested)

    def test_none_stop_file_is_event_only(self) -> None:
        sig = FinishSignal(None)
        sig.consume_stop_file()  # no-op, must not raise
        self.assertFalse(sig.requested)
        sig.request()
        self.assertTrue(sig.requested)

    def test_toggle_flips_in_memory_flag(self) -> None:
        sig = FinishSignal(None)
        self.assertTrue(sig.toggle())   # off -> on, returns the new state
        self.assertTrue(sig.requested)
        self.assertFalse(sig.toggle())  # on -> off
        self.assertFalse(sig.requested)


if __name__ == "__main__":
    unittest.main()

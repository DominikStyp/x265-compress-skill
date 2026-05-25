"""Regression: the live display must advertise pause/resume based on the REAL
platform capability, not a Windows-only `import msvcrt` probe.

The bug: `display.py` computed its own `HAS_KEY_INPUT` from `import msvcrt`
(Windows-only), so on macOS/Linux it was always False and the help footer said
"keyboard pause/resume unavailable on this platform" — even though the keyboard
listener (which gates on `platform_compat.HAS_KEY_INPUT`, the termios+isatty
value) was running and the pause keys actually worked. The display flag must be
the SAME object the listener uses, so the footer never contradicts reality.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import platform_compat  # noqa: E402
from encode_modules import display  # noqa: E402
from encode_modules import keyboard_input  # noqa: E402
from encode_modules.display_render import render_help  # noqa: E402


class HasKeyInputIsPlatformTruth(unittest.TestCase):
    def test_display_flag_matches_platform_compat(self) -> None:
        # Single source of truth — not a separate (Windows-only) probe.
        self.assertEqual(display.HAS_KEY_INPUT, platform_compat.HAS_KEY_INPUT)

    def test_listener_and_display_agree(self) -> None:
        # The footer (display.HAS_KEY_INPUT) must never claim a different
        # capability than the listener actually gates on.
        self.assertEqual(display.HAS_KEY_INPUT, keyboard_input.HAS_KEY_INPUT)


class RenderHelpReflectsCapability(unittest.TestCase):
    def test_available_shows_keys(self) -> None:
        footer = render_help(True, finish_requested=False)
        self.assertIn("Space", footer)
        self.assertNotIn("unavailable", footer)

    def test_no_tty_footer_points_to_file_fallbacks(self) -> None:
        # When keys are off, the footer must tell the operator about the
        # file-based PAUSE/FINISH fallbacks rather than just "unavailable".
        footer = render_help(False, finish_requested=False)
        self.assertIn("PAUSE", footer)
        self.assertIn("FINISH", footer)


if __name__ == "__main__":
    unittest.main()

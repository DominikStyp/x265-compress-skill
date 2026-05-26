"""The shared trailing-window endpoint scan used by both the live-rate display
and the choke detector. Extracted so the two can't drift (they had byte-for-byte
copies of this loop). The window ANCHOR differs between callers, so it is passed
in as `window_start` — the helper itself only finds the endpoints.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules._sample_window import select_window_endpoints  # noqa: E402

SAMPLES = [(0.0, 0.0, 0), (1.0, 10.0, 30), (2.0, 20.0, 60), (3.0, 30.0, 90)]


class SelectWindowEndpointsTest(unittest.TestCase):
    def test_newer_is_always_the_last_sample(self) -> None:
        older, newer = select_window_endpoints(SAMPLES, window_start=1.5)
        self.assertEqual(newer, SAMPLES[-1])

    def test_older_is_first_sample_at_or_after_window_start(self) -> None:
        older, _ = select_window_endpoints(SAMPLES, window_start=1.5)
        self.assertEqual(older, (2.0, 20.0, 60))

    def test_window_start_is_inclusive(self) -> None:
        older, _ = select_window_endpoints(SAMPLES, window_start=2.0)
        self.assertEqual(older, (2.0, 20.0, 60))

    def test_falls_back_to_oldest_when_window_covers_all(self) -> None:
        # window_start older than every sample → the oldest sample anchors it.
        older, _ = select_window_endpoints(SAMPLES, window_start=-100.0)
        self.assertEqual(older, SAMPLES[0])

    def test_falls_back_to_oldest_when_no_sample_in_window(self) -> None:
        # window_start newer than every sample → no match, fall back to [0].
        older, newer = select_window_endpoints(SAMPLES, window_start=99.0)
        self.assertEqual(older, SAMPLES[0])
        self.assertEqual(newer, SAMPLES[-1])


if __name__ == "__main__":
    unittest.main()

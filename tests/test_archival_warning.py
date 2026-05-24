"""Tier 2.3: warn when a near-lossless/archival CRF (<=18) is paired with
--max-size-percent. That combo barely shrinks and frequently trips the size
guard, stopping the encode early (exit 3) before it finishes — a surprising
outcome for someone asking for archival quality.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compress import _archival_size_guard_warning  # noqa: E402


class ArchivalSizeGuardWarningTest(unittest.TestCase):
    def test_low_crf_with_size_guard_warns(self) -> None:
        msg = _archival_size_guard_warning(17, 80.0)
        self.assertIsNotNone(msg)
        self.assertIn("17", msg)
        self.assertIn("max-size-percent", msg)

    def test_crf_18_boundary_warns(self) -> None:
        self.assertIsNotNone(_archival_size_guard_warning(18, 80.0))

    def test_normal_crf_no_warning(self) -> None:
        self.assertIsNone(_archival_size_guard_warning(22, 80.0))

    def test_no_size_guard_no_warning(self) -> None:
        self.assertIsNone(_archival_size_guard_warning(17, None))


if __name__ == "__main__":
    unittest.main()

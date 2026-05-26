"""The canonical H:MM:SS formatter, and proof the always-hours copies delegate.

`format_hms` is the single source of truth for "always show hours" duration
strings. `probes.fmt_dur` and `script_writer.fmt_duration` were byte-identical
copies of it; this pins that they now produce exactly what `format_hms` does so
the three can never drift. (`report._fmt_dur` and `progress.fmt_time` keep their
own variants on purpose — different sentinels / hour-dropping — see their docs.)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from formatting import format_hms  # noqa: E402


class FormatHmsTest(unittest.TestCase):
    def test_basic_cases(self) -> None:
        self.assertEqual(format_hms(0), "0:00:00")
        self.assertEqual(format_hms(59), "0:00:59")
        self.assertEqual(format_hms(60), "0:01:00")
        self.assertEqual(format_hms(3600), "1:00:00")
        self.assertEqual(format_hms(3661), "1:01:01")
        self.assertEqual(format_hms(36000), "10:00:00")

    def test_truncates_fractional_seconds(self) -> None:
        self.assertEqual(format_hms(90.9), "0:01:30")

    def test_delegators_match_canonical(self) -> None:
        from compress_modules.script_writer import fmt_duration
        from encode_modules.probes import fmt_dur
        for s in (0, 7, 65, 3725, 90061):
            self.assertEqual(fmt_dur(s), format_hms(s))
            self.assertEqual(fmt_duration(s), format_hms(s))


if __name__ == "__main__":
    unittest.main()

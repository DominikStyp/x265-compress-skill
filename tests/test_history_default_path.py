"""CR-4 (v1.20.0): the default encoding-history root must be OS-appropriate.

Before v1.20.0 the default root was the hardcoded literal
``C:\\_MOJE\\other\\CUTTED`` on every platform. On POSIX that string is a
valid *filename* — so the history JSONL would land as a single file literally
named ``C:\\_MOJE\\other\\CUTTED`` in the CWD instead of a directory tree.
``CLAUDE_ENCODING_HISTORY_PATH`` still overrides verbatim on both platforms.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history  # noqa: E402


class DefaultHistoryRootTest(unittest.TestCase):
    def test_windows_keeps_cutted_literal(self) -> None:
        with mock.patch.object(history, "IS_WINDOWS", True):
            self.assertEqual(history.default_history_root(),
                             Path(r"C:\_MOJE\other\CUTTED"))

    def test_posix_default_is_not_the_windows_literal(self) -> None:
        # The regression guard: on POSIX the default must NOT be the hardcoded
        # Windows path (which would become a backslash-named file). It's
        # derived from the user's home instead.
        with mock.patch.object(history, "IS_WINDOWS", False):
            root = history.default_history_root()
            self.assertNotEqual(root, Path(r"C:\_MOJE\other\CUTTED"))
            self.assertNotIn("_MOJE", str(root))
            self.assertEqual(root, Path.home() / "x265-encoding")


class EnvOverrideWinsTest(unittest.TestCase):
    def test_env_override_is_verbatim_on_either_os(self) -> None:
        with mock.patch.dict(
                "os.environ",
                {"CLAUDE_ENCODING_HISTORY_PATH": "/custom/h.jsonl"}):
            self.assertEqual(history.default_history_path(),
                             Path("/custom/h.jsonl"))


if __name__ == "__main__":
    unittest.main()

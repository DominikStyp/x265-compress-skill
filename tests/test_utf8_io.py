"""enable_utf8_io() forces stdout/stderr to UTF-8 so non-ASCII output
(the → / — / box-drawing glyphs, accented filenames) doesn't crash with
UnicodeEncodeError when the OS locale codepage isn't UTF-8 (e.g. Windows
cp1250) and output is redirected to a file/pipe — i.e. headless / queue-log
runs, where Python picks the locale encoding instead of the console's.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import platform_compat  # noqa: E402


def _enc(stream) -> str:
    return stream.encoding.lower().replace("-", "")


class EnableUtf8IoTest(unittest.TestCase):
    def test_reconfigures_non_utf8_streams_to_utf8(self) -> None:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")
        try:
            platform_compat.enable_utf8_io()
            self.assertEqual(_enc(sys.stdout), "utf8")
            self.assertEqual(_enc(sys.stderr), "utf8")
            # The whole point: a → no longer raises UnicodeEncodeError.
            sys.stdout.write("→ — ─\n")
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_skips_streams_without_reconfigure(self) -> None:
        old = sys.stdout
        sys.stdout = io.StringIO()  # StringIO has no .reconfigure
        try:
            platform_compat.enable_utf8_io()  # must not raise
        finally:
            sys.stdout = old


if __name__ == "__main__":
    unittest.main()

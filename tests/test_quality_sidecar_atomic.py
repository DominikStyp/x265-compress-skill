"""Regression: the .quality.json sidecar must be written atomically.

The queue runner parses `<output>.quality.json` into its aggregate report. A
kill mid-write would leave a truncated JSON the runner then chokes on. AGENTS.md
atomic-writes invariant: "never write a final file in place, including
sidecar/cache JSON" — write to a temp name and os.replace() it in (the same
pattern hook_config / pre_flight already use). This pins that the sidecar goes
through os.replace and lands as valid JSON.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import reporting  # noqa: E402


class QualitySidecarAtomicTest(unittest.TestCase):
    def test_sidecar_is_written_via_os_replace(self) -> None:
        scores = {"vmaf_mean": 97.0, "vmaf_min": 88.0}
        args = SimpleNamespace(no_quality_check=False, vmaf_mode="full",
                               vmaf_subsample=10, vmaf_chunks=3)
        real_replace = reporting.os.replace
        seen: list[tuple[str, str]] = []

        def spy_replace(a, b):
            seen.append((str(a), str(b)))
            return real_replace(a, b)

        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "out.mkv"
            with mock.patch.object(reporting, "quality_check",
                                   return_value=scores), \
                 mock.patch.object(reporting.os, "replace",
                                   side_effect=spy_replace), \
                 mock.patch("sys.stdout"):
                got = reporting.measure_quality_and_write_sidecar(
                    Path("src.mkv"), dst, Path(td) / "wd",
                    args=args, n_chunks_total=1,
                )
            sidecar = dst.parent / ".tmp" / f"{dst.stem}.quality.json"

            self.assertEqual(got, scores)
            # The write must have gone through os.replace (atomic), landing at
            # the sidecar path from a temp file in the SAME directory (so the
            # rename is atomic, not cross-filesystem).
            matching = [src for src, dst_path in seen if dst_path == str(sidecar)]
            self.assertTrue(matching, "sidecar was not written via os.replace")
            tmp_src = Path(matching[0])
            self.assertTrue(tmp_src.name.endswith(".tmp"))
            self.assertEqual(tmp_src.parent, sidecar.parent)
            # ...and the final file must be valid, complete JSON, with no
            # leftover .tmp beside it.
            self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8")),
                             scores)
            self.assertEqual(list(sidecar.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

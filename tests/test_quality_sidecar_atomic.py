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
            # v1.19.0: sidecar lives under logs/, not .tmp/.
            sidecar = dst.parent / "logs" / f"{dst.stem}.quality.json"

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


class QueueRunnerReaderWriterParityTest(unittest.TestCase):
    """v1.19.0 reader/writer parity: ``encode_modules/reporting.py`` writes
    the sidecar via ``quality_sidecar_path(dst)`` (under ``logs/``). The
    queue runner's ``read_quality_sidecar`` must read from the same
    location, otherwise every aggregate-report row silently loses VMAF
    scores — the bug Reviewer A caught pre-ship."""

    def test_reader_finds_sidecar_at_logs_path(self) -> None:
        from queue_modules.job_runner import read_quality_sidecar
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst = td / "movie.mkv"
            sidecar = td / "logs" / "movie.quality.json"
            sidecar.parent.mkdir()
            payload = {"vmaf_mean": 97.3, "vmaf_min": 94.1}
            sidecar.write_text(json.dumps(payload), encoding="utf-8")

            got = read_quality_sidecar(dst)
            self.assertEqual(got, payload)

    def test_reader_still_finds_legacy_tmp_path(self) -> None:
        # Back-compat: a sidecar written by a pre-v1.19.0 encoder (or by a
        # partially-migrated workspace) under .tmp/ is still picked up.
        from queue_modules.job_runner import read_quality_sidecar
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst = td / "movie.mkv"
            sidecar = td / ".tmp" / "movie.quality.json"
            sidecar.parent.mkdir()
            payload = {"vmaf_mean": 96.5}
            sidecar.write_text(json.dumps(payload), encoding="utf-8")

            got = read_quality_sidecar(dst)
            self.assertEqual(got, payload)

    def test_reader_prefers_logs_when_both_exist(self) -> None:
        from queue_modules.job_runner import read_quality_sidecar
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst = td / "movie.mkv"
            new_sc = td / "logs" / "movie.quality.json"
            old_sc = td / ".tmp" / "movie.quality.json"
            new_sc.parent.mkdir()
            old_sc.parent.mkdir()
            new_sc.write_text('{"vmaf_mean": 97.5}', encoding="utf-8")
            old_sc.write_text('{"vmaf_mean": 80.0}', encoding="utf-8")

            got = read_quality_sidecar(dst)
            self.assertEqual(got, {"vmaf_mean": 97.5})


if __name__ == "__main__":
    unittest.main()

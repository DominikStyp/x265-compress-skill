"""handle_preflight: the auto-patch dispatch must protect BOTH sources.

`handle_preflight` is the single entry point that decides whether to encode
the original source, a surgically-patched copy, or bail. The load-bearing
invariant is the last branch: when a patch is ADOPTED, the patched copy is
handed to `protect_source` — and because that ADDS rather than replaces, the
user's original (registered at startup) must stay protected alongside it.
A regression here re-opens the exact hole `source_guard` exists to close:
the original source losing its guard the instant a patch succeeds.

We also pin the other four branches (scan-skipped, scan-passed,
scan-failed-no-autopatch, autopatch-declined / still-bad) so the four-tuple
shape stays uniform and the caller can keep dispatching on `status` alone.
All collaborators are patched with `mock.patch.object` so no real ffmpeg /
ffprobe ever runs.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import preflight_decision, source_guard  # noqa: E402
from encode_modules.preflight_decision import handle_preflight  # noqa: E402
from encode_modules.source_guard import ensure_not_source  # noqa: E402


def _args(*, no_scan: bool, auto_patch: bool):
    return types.SimpleNamespace(
        no_pre_flight_scan=no_scan,
        auto_patch_source=auto_patch,
        segment_seconds=60,
        max_patch_seconds=10.0,
    )


class HandlePreflightDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.workdir = Path(self._td.name) / "wd"
        self.src = Path(self._td.name) / "movie.mp4"
        self.src.write_bytes(b"src")
        # Keep summary output empty so redirected stdout stays uncluttered.
        self._fmt = mock.patch.object(
            preflight_decision, "format_pre_flight_summary",
            return_value="")
        self._fmt.start()
        self.addCleanup(self._fmt.stop)

    def tearDown(self) -> None:
        source_guard._protected_sources.clear()
        self._td.cleanup()

    def _run(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return handle_preflight(self.src, self.workdir, args)

    def test_scan_skipped_returns_ok_synthetic(self) -> None:
        with mock.patch.object(preflight_decision, "pre_flight_scan") as scan:
            status, src, scan_dict, rescan = self._run(
                _args(no_scan=True, auto_patch=False))
        self.assertEqual(status, "ok")
        self.assertIs(src, self.src)
        self.assertEqual(scan_dict, {"passed": True, "skipped": True})
        self.assertIsNone(rescan)
        scan.assert_not_called()

    def test_scan_passes_returns_ok(self) -> None:
        with mock.patch.object(preflight_decision, "pre_flight_scan",
                               return_value={"passed": True}) as scan:
            status, src, scan_dict, rescan = self._run(
                _args(no_scan=False, auto_patch=True))
        self.assertEqual(status, "ok")
        self.assertIs(src, self.src)
        self.assertEqual(scan_dict, {"passed": True})
        self.assertIsNone(rescan)
        scan.assert_called_once()

    def test_scan_fails_no_autopatch_returns_failed(self) -> None:
        with mock.patch.object(preflight_decision, "pre_flight_scan",
                               return_value={"passed": False}):
            with mock.patch.object(preflight_decision,
                                   "auto_patch_source") as patcher:
                status, src, scan_dict, rescan = self._run(
                    _args(no_scan=False, auto_patch=False))
        self.assertEqual(status, "failed")
        self.assertIs(src, self.src)
        self.assertEqual(scan_dict, {"passed": False})
        self.assertIsNone(rescan)
        patcher.assert_not_called()

    def test_autopatch_declined_returns_failed(self) -> None:
        with mock.patch.object(preflight_decision, "pre_flight_scan",
                               return_value={"passed": False}):
            with mock.patch.object(preflight_decision, "auto_patch_source",
                                   return_value=None) as patcher:
                with mock.patch.object(preflight_decision,
                                       "protect_source") as guard:
                    status, src, scan_dict, rescan = self._run(
                        _args(no_scan=False, auto_patch=True))
        self.assertEqual(status, "failed")
        self.assertIs(src, self.src)
        self.assertIsNone(rescan)
        patcher.assert_called_once()
        guard.assert_not_called()

    def test_autopatch_success_adds_patched_to_guard_and_returns_patched(self):
        patched = Path(self._td.name) / "movie.patched.mp4"
        with mock.patch.object(preflight_decision, "pre_flight_scan",
                               side_effect=[{"passed": False},
                                            {"passed": True}]):
            with mock.patch.object(preflight_decision, "auto_patch_source",
                                   return_value=patched):
                with mock.patch.object(preflight_decision,
                                       "protect_source") as guard:
                    status, src, scan_dict, rescan = self._run(
                        _args(no_scan=False, auto_patch=True))
        self.assertEqual(status, "patched")
        self.assertEqual(src, patched)
        self.assertEqual(scan_dict, {"passed": False})
        self.assertEqual(rescan, {"passed": True})
        guard.assert_called_once_with(patched)

    def test_autopatch_still_bad_returns_failed_with_rescan(self) -> None:
        patched = Path(self._td.name) / "movie.patched.mp4"
        with mock.patch.object(preflight_decision, "pre_flight_scan",
                               side_effect=[{"passed": False},
                                            {"passed": False}]):
            with mock.patch.object(preflight_decision, "auto_patch_source",
                                   return_value=patched):
                with mock.patch.object(preflight_decision,
                                       "protect_source") as guard:
                    status, src, scan_dict, rescan = self._run(
                        _args(no_scan=False, auto_patch=True))
        self.assertEqual(status, "failed")
        self.assertIs(src, self.src)
        self.assertEqual(rescan, {"passed": False})
        # A non-clean patch must NOT be adopted into the guard set.
        guard.assert_not_called()

    def test_autopatch_success_keeps_original_protected_end_to_end(self) -> None:
        # Use the REAL source_guard here — this is the regression that matters:
        # after a patch is adopted, BOTH the original and the patched copy must
        # raise from ensure_not_source.
        patched = Path(self._td.name) / "movie.patched.mp4"
        source_guard.protect_source(self.src)  # as startup would do
        with mock.patch.object(preflight_decision, "pre_flight_scan",
                               side_effect=[{"passed": False},
                                            {"passed": True}]):
            with mock.patch.object(preflight_decision, "auto_patch_source",
                                   return_value=patched):
                status, src, scan_dict, rescan = self._run(
                    _args(no_scan=False, auto_patch=True))
        self.assertEqual(status, "patched")
        self.assertEqual(src, patched)
        with self.assertRaises(RuntimeError):
            ensure_not_source(self.src)
        with self.assertRaises(RuntimeError):
            ensure_not_source(patched)


if __name__ == "__main__":
    unittest.main()

"""REGRESSION (v1.18.0 architect review H1 + H3): the per-chunk metrics
rollup must land in the history JSONL on EVERY terminal status — not just
the success path.

v1.18.0 initial wiring called ``_mirror_chunk_metrics`` from ``finalize()``,
which only runs on the success path. Every abort path (threshold, quality,
chunk-failed, verify-failed, pre-flight-failed, awaiting-chunk-fix,
stopped-by-user) reaches ``flush()`` via ``mark_status`` + ``sys.exit`` +
the atexit hook — bypassing finalize entirely. The post-mortem use case
the spec was specifically written for (
``FEATURE-REQUEST_persist-per-chunk-and-per-file-encode-metrics_2026-06-03.md``
lines 79-80: "Abort post-mortems without terminal scrollback") therefore
had no persisted rollup on the runs that needed it most.

The fix moves the mirror call into ``flush()``, gated on the
``n_chunks > 0`` check already in place so init-but-no-work paths stay
byte-identical.

H3 (``quality_aborted`` / ``quality_aborted_chunk`` always False/None):
when ``status == "stopped-quality-threshold"`` the mirror reads
chunk_name from the structured extras that ``mark_status`` stashed.
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history as _hist  # noqa: E402

from encode_modules import chunk_metrics_log as _cml  # noqa: E402
from encode_modules import history_state as _hs  # noqa: E402


def _make_args(crf: int = 22, preset: str = "slow") -> types.SimpleNamespace:
    """Minimal args namespace covering everything init_history_state reads."""
    return types.SimpleNamespace(
        crf=crf, preset=preset, pix_fmt="yuv420p10le", x265_params="",
        parallel=2, segment_seconds=60,
        max_output_bytes=None,
    )


class _AbortMirrorFixtureMixin:
    """Shared setup: a fresh history recorder, a fresh chunk_metrics_log
    pointing at a tempdir JSONL, three chunks already logged, and a
    monkey-patched ``_hist.append_record`` that captures whatever the
    flush would have written to disk."""

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.dst = self.tmp_path / "out.mkv"
        self.dst.write_bytes(b"x" * 1000)
        self.src = self.tmp_path / "in.mp4"
        self.src.write_bytes(b"x" * 2000)
        self.workdir = self.tmp_path / "workdir"
        self.workdir.mkdir()
        self.jsonl = self.tmp_path / ".tmp" / "out.chunk_metrics.jsonl"

        # Fresh chunk metrics log + three chunks logged so the rollup is
        # non-empty.
        log = _cml.init_chunk_metrics_log(
            self.jsonl, enabled=True,
            position_of={"src_0001.mkv": 1, "src_0002.mkv": 2,
                         "src_0003.mkv": 3},
            width=1920, height=1080, fps=23.976,
            crf=22, preset="slow", quality_threshold=90.0,
            workdir=self.workdir,
        )
        log.record_chunk(chunk_name="src_0001.mkv",
                        encode_elapsed_s=40.0, chunk_duration_s=100.0,
                        output_bytes=5_000_000)
        log.record_chunk(chunk_name="src_0002.mkv",
                        encode_elapsed_s=50.0, chunk_duration_s=100.0,
                        output_bytes=6_000_000)
        log.record_chunk(chunk_name="src_0003.mkv",
                        encode_elapsed_s=30.0, chunk_duration_s=100.0,
                        output_bytes=4_000_000)

        # Replace history recorder + capture writes.
        _hs._reset_for_tests()
        self.captured: list[dict] = []
        self._orig_append = _hist.append_record
        _hist.append_record = lambda rec: self.captured.append(dict(rec))

    def tearDown(self) -> None:  # type: ignore[override]
        _hist.append_record = self._orig_append
        _cml._reset_for_tests()
        _hs._reset_for_tests()
        self._tmp.cleanup()
        super().tearDown()  # type: ignore[misc]


class FlushMirrorsOnSuccessTest(_AbortMirrorFixtureMixin, unittest.TestCase):
    """Sanity: the success path still mirrors (regression-proofing the move
    from finalize into flush)."""

    def test_success_path_carries_chunk_metrics_summary(self) -> None:
        args = _make_args()
        _hs.init_history_state(self.src, self.dst, args, source_bytes=2000)
        _hs.mark_status("ok")
        _hs._recorder.flush()  # explicit flush mirrors flow on success path
        self.assertEqual(len(self.captured), 1)
        rec = self.captured[0]
        self.assertIn("chunk_metrics_summary", rec,
                      "successful flush must still mirror the rollup")
        self.assertEqual(rec["chunk_metrics_summary"]["n_chunks"], 3)


class FlushMirrorsOnThresholdAbortTest(_AbortMirrorFixtureMixin,
                                       unittest.TestCase):
    """H1 reproducer #1: size-threshold abort path. mark_status writes
    stopped-threshold; the atexit flush is what produces the JSONL row.
    We invoke flush directly (same code path, deterministic test)."""

    def test_threshold_abort_carries_rollup(self) -> None:
        args = _make_args()
        _hs.init_history_state(self.src, self.dst, args, source_bytes=2000)
        _hs.mark_status("stopped-threshold",
                       abort_reason="projected 90% > 75%")
        _hs._recorder.flush()
        self.assertEqual(len(self.captured), 1)
        rec = self.captured[0]
        self.assertIn("chunk_metrics_summary", rec,
                      "threshold abort must still mirror the rollup — "
                      "this is the post-mortem use case the spec exists for")
        self.assertEqual(rec["chunk_metrics_summary"]["n_chunks"], 3)


class FlushMirrorsOnQualityAbortTest(_AbortMirrorFixtureMixin,
                                     unittest.TestCase):
    """H1 + H3 reproducer: quality-threshold abort path.
    The rollup must (a) be present, (b) carry quality_aborted=True,
    (c) carry quality_aborted_chunk = the failing chunk name."""

    def test_quality_abort_carries_rollup_with_flags(self) -> None:
        args = _make_args()
        _hs.init_history_state(self.src, self.dst, args, source_bytes=2000)
        # encode_parallel.py:401-405 sets exactly these extras.
        _hs.mark_status("stopped-quality-threshold",
                       chunk_idx=1, chunk_name="src_0002.mkv",
                       vmaf_mean=85.0, threshold=90.0)
        _hs._recorder.flush()
        self.assertEqual(len(self.captured), 1)
        rec = self.captured[0]
        summary = rec.get("chunk_metrics_summary")
        self.assertIsNotNone(summary,
                            "quality abort must mirror the rollup")
        self.assertTrue(summary["quality_aborted"],
                       "quality_aborted must be True for "
                       "stopped-quality-threshold status")
        self.assertEqual(summary["quality_aborted_chunk"], "src_0002.mkv",
                        "quality_aborted_chunk must report the failing chunk")


class FlushMirrorsOnChunkFailedTest(_AbortMirrorFixtureMixin,
                                    unittest.TestCase):
    """H1 reproducer #3: chunk-failed abort path (a real ffmpeg failure).
    The rollup carries whatever chunks DID complete — the partial signal
    is the whole point of the post-mortem story."""

    def test_chunk_failed_carries_rollup(self) -> None:
        args = _make_args()
        _hs.init_history_state(self.src, self.dst, args, source_bytes=2000)
        _hs.mark_status("chunk-failed",
                       failed_chunks=["src_0004.mkv"])
        _hs._recorder.flush()
        rec = self.captured[0]
        self.assertIn("chunk_metrics_summary", rec)
        # The 3 chunks that DID complete are visible in the rollup.
        self.assertEqual(rec["chunk_metrics_summary"]["n_chunks"], 3)
        # quality_aborted stays False for non-quality stops.
        self.assertFalse(rec["chunk_metrics_summary"]["quality_aborted"])


if __name__ == "__main__":
    unittest.main()

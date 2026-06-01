"""Adaptive CRF-jump estimation — pure math + floor detector + per-job K
calibration. Replacement for the blind `+crf_step` walk when the user
opts in via `crf_jump: true`.

Model: x265's rate control is approximately exponential in CRF —

    size_ratio ≈ 2^(-ΔCRF / K)

so the CRF that lands at a target size ratio T is

    next_crf = c + ceil(K · log2(P / T))

where c is the just-tried CRF, P is the projected % of source at c, and
T is the target % (a margin under max_size_percent). Median K observed in
real history ≈ 6 (textbook value); large outliers (K > 15) signal a
source already at its compression floor and should be stopped, not stepped.

Spec: ../TO_ENCODE_AI/feature-request_adaptive_crf_jump_estimation.md
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.crf_jump import (  # noqa: E402
    DEFAULT_K, DEFAULT_MARGIN, K_CLAMP_MAX, K_CLAMP_MIN,
    calibrate_k, compute_next_crf, is_floor_bound,
)


class ComputeNextCrfTest(unittest.TestCase):
    """The jump math: next_crf = c + ceil(K · log2(P/T)). Numbers in the
    expected column come from running the spec's worked examples through
    the same formula (the spec table at lines 86-89)."""

    def test_emily_19_at_122pct_jumps_4_to_23(self) -> None:
        # Spec example: Emily start crf 19 @ 122.0% with K=6, margin=5,
        # max_size_percent=85 (target T=80). jump = ceil(6 * log2(122/80))
        # = ceil(6 * 0.6088) = ceil(3.6529) = 4 -> 19 + 4 = 23.
        self.assertEqual(compute_next_crf(
            current_crf=19, projected_pct=122.0,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28), 23)

    def test_alyssa_19_at_107pct_jumps_3_to_22(self) -> None:
        # ceil(6 * log2(107.1/80)) = ceil(6 * 0.4205) = ceil(2.5232) = 3.
        self.assertEqual(compute_next_crf(
            current_crf=19, projected_pct=107.1,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28), 22)

    def test_april_23_at_95pct_jumps_2_to_25(self) -> None:
        # ceil(6 * log2(95.7/80)) = ceil(6 * 0.2585) = ceil(1.5511) = 2.
        self.assertEqual(compute_next_crf(
            current_crf=23, projected_pct=95.7,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28), 25)

    def test_pauline_19_at_90pct_jumps_2_to_21(self) -> None:
        # ceil(6 * log2(90.6/80)) = ceil(6 * 0.1796) = ceil(1.0775) = 2.
        # Spec's intent: land on the feasible CRF in ONE jump (vs 2 probes).
        self.assertEqual(compute_next_crf(
            current_crf=19, projected_pct=90.6,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28), 21)

    def test_jump_clamped_to_crf_max(self) -> None:
        # P way over T -> mathematical jump would shoot past crf_max.
        # Must clamp.
        self.assertEqual(compute_next_crf(
            current_crf=23, projected_pct=300.0,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28), 28)

    def test_jump_floored_at_crf_step(self) -> None:
        # When the computed jump is < crf_step (e.g. P barely over T),
        # the floor wins so we still make minimum progress. This is the
        # whole point of keeping crf_step as the minimum.
        self.assertEqual(compute_next_crf(
            current_crf=21, projected_pct=86.0,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=2, crf_max=28), 23)  # 21 + max(2, 1) = 23

    def test_p_at_or_below_target_returns_none(self) -> None:
        # Defensive: if the caller mis-routes here when the job is
        # already feasible, return None so the caller can detect "no
        # escalation needed" rather than emitting a no-op step.
        self.assertIsNone(compute_next_crf(
            current_crf=22, projected_pct=80.0,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28))
        self.assertIsNone(compute_next_crf(
            current_crf=22, projected_pct=70.0,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28))

    def test_already_at_crf_max_returns_max(self) -> None:
        # No further escalation possible — caller should treat as
        # crf-exhausted.
        self.assertEqual(compute_next_crf(
            current_crf=28, projected_pct=120.0,
            max_size_pct=85.0, margin=5.0,
            k=6.0, crf_step=1, crf_max=28), 28)

    def test_target_floor_at_one_pct(self) -> None:
        # Defensive: margin > max_size_pct would yield T ≤ 0 and break
        # log2. Implementation must floor T at a safe minimum (1.0%).
        # With T=1, jump = ceil(6 * log2(120/1)) = ceil(41.5) = 42 ->
        # clamped to crf_max.
        self.assertEqual(compute_next_crf(
            current_crf=20, projected_pct=120.0,
            max_size_pct=2.0, margin=10.0,
            k=6.0, crf_step=1, crf_max=28), 28)


class CalibrateKTest(unittest.TestCase):
    """`crf_jump_k: "auto"` calibrates K from this job's first two
    projections: K_obs = -ΔCRF / log2(P2/P1). Clamped to [4, 9] so a
    wild outlier (floor case) doesn't produce a runaway jump."""

    def test_alyssa_19_to_20_yields_about_578(self) -> None:
        # Spec table: 19->20, 107.1->95.0 yields k=5.78. Tolerance ±0.01
        # because of float arithmetic.
        k = calibrate_k(crf1=19, pct1=107.1, crf2=20, pct2=95.0)
        self.assertAlmostEqual(k, 5.78, places=2)

    def test_emily_19_to_20_yields_about_605(self) -> None:
        k = calibrate_k(crf1=19, pct1=122.0, crf2=20, pct2=108.8)
        self.assertAlmostEqual(k, 6.05, places=2)

    def test_clamped_to_min_when_observed_very_small(self) -> None:
        # If K_obs comes back tiny (e.g. a single step almost halved the
        # output), clamp up to the floor so jumps don't overshoot wildly.
        k = calibrate_k(crf1=20, pct1=120.0, crf2=21, pct2=30.0)
        self.assertEqual(k, K_CLAMP_MIN)

    def test_clamped_to_max_when_observed_huge(self) -> None:
        # Floor case (Alyssa 21->22 went 85.3->85.2, k=590 in the spec):
        # clamp down so the next jump is conservative, not zero.
        k = calibrate_k(crf1=21, pct1=85.3, crf2=22, pct2=85.2)
        self.assertEqual(k, K_CLAMP_MAX)

    def test_increasing_pct_means_invalid_input_returns_default(
            self) -> None:
        # Defensive: if P2 > P1 the formula gives a NEGATIVE k, which
        # would invert the jump direction. Return the default rather
        # than calibrating on broken data.
        k = calibrate_k(crf1=20, pct1=80.0, crf2=21, pct2=85.0)
        self.assertEqual(k, DEFAULT_K)

    def test_zero_or_equal_pct_returns_default(self) -> None:
        # log2(P2/P1) -> 0 makes K_obs explode; treat as "no signal".
        self.assertEqual(calibrate_k(20, 80.0, 21, 80.0), DEFAULT_K)
        self.assertEqual(calibrate_k(20, 80.0, 20, 80.0), DEFAULT_K)

    def test_same_crf_returns_default(self) -> None:
        # Two probes at the same CRF tell us nothing about k.
        self.assertEqual(calibrate_k(20, 100.0, 20, 95.0), DEFAULT_K)


class FloorDetectorTest(unittest.TestCase):
    """Diminishing-returns early stop. The spec's two examples:

      Alyssa 21->22: 85.3 -> 85.2  (Δ 0.1 pt) -> floor; stop
      Baby Kxtten 23->...->23: stuck ~85.0-85.5 across CRFs  -> floor
    """

    def test_alyssa_floor_case(self) -> None:
        # Δ between consecutive projections is 0.1 pt, well below the
        # default min_gain of 2.0. Both probes are STILL over threshold,
        # so the floor verdict is "won't compress, stop".
        self.assertTrue(is_floor_bound(
            prev_pct=85.3, current_pct=85.2,
            max_size_pct=85.0, min_gain=2.0))

    def test_baby_kxtten_flat_case(self) -> None:
        # Δ 0.4 pt, both probes STILL over the 85% cap -> floor.
        # (Spec's Baby Kxtten line: "stuck ~85.0-85.5 across CRFs" —
        # both probes triggered a stopped-threshold, both above the
        # 85.0% cap.)
        self.assertTrue(is_floor_bound(
            prev_pct=85.5, current_pct=85.1,
            max_size_pct=85.0, min_gain=2.0))

    def test_real_progress_is_not_floor(self) -> None:
        # Emily 19->20: 122.0 -> 108.8 (Δ 13.2 pt) -> NOT floor.
        self.assertFalse(is_floor_bound(
            prev_pct=122.0, current_pct=108.8,
            max_size_pct=85.0, min_gain=2.0))

    def test_below_threshold_is_not_floor(self) -> None:
        # If the current probe LANDED under the threshold, the job
        # succeeded — never call this "floor-bound".
        self.assertFalse(is_floor_bound(
            prev_pct=90.0, current_pct=80.0,
            max_size_pct=85.0, min_gain=2.0))

    def test_min_gain_zero_disables_detector(self) -> None:
        # Explicit opt-out: min_gain=0 means "never declare floor".
        self.assertFalse(is_floor_bound(
            prev_pct=85.3, current_pct=85.2,
            max_size_pct=85.0, min_gain=0.0))

    def test_needs_strictly_less_gain_than_threshold(self) -> None:
        # A Δ exactly equal to min_gain is borderline; spec wording
        # ("yields < MIN_GAIN points of improvement") implies STRICTLY
        # less. A 2-point gain at min_gain=2.0 is NOT floor.
        self.assertFalse(is_floor_bound(
            prev_pct=87.0, current_pct=85.0,
            max_size_pct=84.0, min_gain=2.0))

    def test_negative_gain_is_floor(self) -> None:
        # If the higher CRF projected LARGER (regression noise / very
        # near-floor source), it's still "won't compress" — call it
        # floor.
        self.assertTrue(is_floor_bound(
            prev_pct=85.2, current_pct=85.4,
            max_size_pct=85.0, min_gain=2.0))


class HistoryReaderTest(unittest.TestCase):
    """The queue runner reads the LAST encoding_history.jsonl record for
    the just-finished source to extract (projected_pct, threshold_pct,
    crf_used). The encoder writes those fields under `output` on
    threshold-abort runs (the contract added in this same release).
    """

    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.history = Path(self._td.name) / "encoding_history.jsonl"

    def _write(self, lines: list[dict]) -> None:
        import json
        with self.history.open("w", encoding="utf-8") as f:
            for rec in lines:
                f.write(json.dumps(rec) + "\n")

    def test_reads_last_record_for_matching_source(self) -> None:
        # Encoder schema (added in this same release): the threshold
        # abort record carries `projected_pct` AND `threshold_pct` on
        # `output` — both pre-computed as % of source so the queue
        # never has to redo the bytes-arithmetic.
        from queue_modules.crf_jump import read_last_projection
        target = "/v/a.mp4"
        self._write([
            {"input": {"path": "/v/other.mp4"},
             "status": "ok", "output": {}},
            {"input": {"path": target}, "status": "stopped-threshold",
             "output": {"projected_pct": 95.7, "threshold_pct": 85.0,
                        "bytes_projected": 1000, "bytes_threshold": 850},
             "settings": {"crf": 23}},
        ])
        result = read_last_projection(self.history, Path(target))
        self.assertIsNotNone(result)
        self.assertEqual(result.crf, 23)
        self.assertAlmostEqual(result.projected_pct, 95.7, places=2)
        self.assertAlmostEqual(result.threshold_pct, 85.0, places=2)

    def test_returns_none_when_no_record_for_source(self) -> None:
        from queue_modules.crf_jump import read_last_projection
        self._write([
            {"input": {"path": "/v/other.mp4"},
             "status": "stopped-threshold",
             "output": {"bytes_projected": 1, "bytes_threshold": 1},
             "settings": {"crf": 22}},
        ])
        self.assertIsNone(
            read_last_projection(self.history, Path("/v/missing.mp4")))

    def test_returns_none_when_history_missing(self) -> None:
        from queue_modules.crf_jump import read_last_projection
        self.assertIsNone(
            read_last_projection(self.history.parent / "missing.jsonl",
                                  Path("/v/x.mp4")))

    def test_returns_none_when_record_lacks_projection_fields(
            self) -> None:
        from queue_modules.crf_jump import read_last_projection
        # An `ok` record has no projection — caller needs None so it
        # can fall back to today's blind step.
        self._write([
            {"input": {"path": "/v/a.mp4"}, "status": "ok",
             "output": {"size_bytes": 100}},
        ])
        self.assertIsNone(
            read_last_projection(self.history, Path("/v/a.mp4")))

    def test_last_record_wins_over_earlier_match(self) -> None:
        from queue_modules.crf_jump import read_last_projection
        self._write([
            {"input": {"path": "/v/a.mp4"},
             "status": "stopped-threshold",
             "output": {"bytes_projected": 100, "bytes_threshold": 80,
                        "projected_pct": 95.0, "threshold_pct": 80.0},
             "settings": {"crf": 22}},
            {"input": {"path": "/v/a.mp4"},
             "status": "stopped-threshold",
             "output": {"bytes_projected": 90, "bytes_threshold": 80,
                        "projected_pct": 88.0, "threshold_pct": 80.0},
             "settings": {"crf": 23}},
        ])
        result = read_last_projection(self.history, Path("/v/a.mp4"))
        self.assertIsNotNone(result)
        self.assertEqual(result.crf, 23)
        self.assertAlmostEqual(result.projected_pct, 88.0, places=2)

    def test_corrupt_lines_are_skipped(self) -> None:
        # Single garbage line mid-file shouldn't kill the read.
        from queue_modules.crf_jump import read_last_projection
        text = ('{"input":{"path":"/v/a.mp4"},"status":"stopped-threshold",'
                '"output":{"projected_pct":95.0,"threshold_pct":80.0},'
                '"settings":{"crf":22}}\n'
                'this is not json at all\n')
        self.history.write_text(text, encoding="utf-8")
        result = read_last_projection(self.history, Path("/v/a.mp4"))
        self.assertIsNotNone(result)
        self.assertEqual(result.crf, 22)


if __name__ == "__main__":
    unittest.main()

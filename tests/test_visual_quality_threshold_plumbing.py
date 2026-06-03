"""End-to-end plumbing for the v1.17.0 `visual_quality_threshold` feature:

  CLI         compress.py  --visual-quality-threshold <N>
  CLI         encode_resumable.py --visual-quality-threshold <N>
  Queue JSON  defaults / per-job  "visual_quality_threshold": <N>
              → built into compress.py argv by job_schema.build_compress_argv

  Encoder    encode_chunks dispatch routes to parallel path when threshold
              is set (the QualityGuard lives there)

  Exit code 9 → status string "stopped-quality-threshold"

These tests pin every link in the chain — none of them spawn ffmpeg. We
exercise: argparse parses the flag; job_schema's VALID_KEYS accepts the
key + build_compress_argv emits the flag; the encoder dispatcher picks the
parallel path when only `visual_quality_threshold` is set; and the
exit-code-to-status table maps 9 correctly.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import cli_args, encoder  # noqa: E402
from queue_modules import job_runner  # noqa: E402
from queue_modules.job_schema import (  # noqa: E402
    VALID_KEYS,
    build_compress_argv,
    merge_job,
)


class EncodeResumableCliTest(unittest.TestCase):
    """encode_resumable.py CLI parser accepts --visual-quality-threshold and
    surfaces it on the parsed namespace."""

    def _parse(self, extra: list[str]) -> object:
        argv = ["prog", "--input", "x.mp4", "--output", "x.mkv",
                "--workdir", "wd", "--crf", "22", "--preset", "slow",
                "--pix-fmt", "yuv420p10le", "--x265-params", "tune=animation",
                *extra]
        with mock.patch.object(sys, "argv", argv):
            return cli_args.parse_args()

    def test_flag_parsed_as_float(self) -> None:
        ns = self._parse(["--visual-quality-threshold", "90"])
        self.assertEqual(ns.visual_quality_threshold, 90.0)
        self.assertIsInstance(ns.visual_quality_threshold, float)

    def test_flag_default_is_none(self) -> None:
        ns = self._parse([])
        self.assertIsNone(ns.visual_quality_threshold)

    def test_flag_accepts_decimal(self) -> None:
        ns = self._parse(["--visual-quality-threshold", "92.5"])
        self.assertEqual(ns.visual_quality_threshold, 92.5)


class QueueJobSchemaTest(unittest.TestCase):
    """queue.json plumbing: VALID_KEYS recognises the key + the merger keeps
    it + build_compress_argv emits --visual-quality-threshold."""

    def test_key_is_in_valid_keys(self) -> None:
        self.assertIn("visual_quality_threshold", VALID_KEYS)

    def test_merge_per_job_overrides_defaults(self) -> None:
        defaults = {"visual_quality_threshold": 88}
        job = {"input": "x.mp4", "visual_quality_threshold": 92}
        merged = merge_job(defaults, job)
        self.assertEqual(merged["visual_quality_threshold"], 92)

    def test_merge_per_job_inherits_default(self) -> None:
        defaults = {"visual_quality_threshold": 88}
        job = {"input": "x.mp4"}
        merged = merge_job(defaults, job)
        self.assertEqual(merged["visual_quality_threshold"], 88)

    def test_build_compress_argv_emits_flag(self) -> None:
        job = {"input": "/tmp/x.mp4", "visual_quality_threshold": 90}
        argv = build_compress_argv(job)
        # Flag and value should appear together
        self.assertIn("--visual-quality-threshold", argv)
        idx = argv.index("--visual-quality-threshold")
        self.assertEqual(argv[idx + 1], "90.0")

    def test_build_compress_argv_omits_flag_when_unset(self) -> None:
        job = {"input": "/tmp/x.mp4", "crf": 22}
        argv = build_compress_argv(job)
        self.assertNotIn("--visual-quality-threshold", argv)


class EncoderDispatchTest(unittest.TestCase):
    """encode_chunks routes to the parallel path when visual_quality_threshold
    is set, even with parallel=1 and no other guards. The serial path can't
    host the QualityGuard."""

    def test_visual_quality_threshold_forces_parallel_path(self) -> None:
        called_parallel = []
        called_serial = []

        def fake_parallel(*args, **kwargs):
            called_parallel.append(kwargs)
            return []

        def fake_serial(*args, **kwargs):
            called_serial.append(kwargs)

        with mock.patch.object(encoder, "encode_chunks_parallel",
                              side_effect=fake_parallel), \
             mock.patch.object(encoder, "encode_chunks_serial",
                              side_effect=fake_serial):
            encoder.encode_chunks(
                chunks=[Path("a.mkv")], workdir=Path("wd"),
                parallel=1, crf=22, preset="slow",
                pix_fmt="yuv420p10le", x265_params="tune=animation",
                max_output_bytes=None,
                choke_threshold_speed=0,  # disable choke -> would normally pick serial
                choke_grace_seconds=0,
                visual_quality_threshold=90,
            )
        self.assertEqual(len(called_parallel), 1,
                        "VQT must force the parallel path")
        self.assertEqual(len(called_serial), 0)
        self.assertEqual(called_parallel[0]["visual_quality_threshold"], 90)

    def test_no_guards_still_takes_serial_when_parallel_1(self) -> None:
        # Regression: without any guard, parallel=1 still uses serial.
        called_parallel = []
        called_serial = []
        with mock.patch.object(encoder, "encode_chunks_parallel",
                              side_effect=lambda *a, **k: called_parallel.append(k) or []), \
             mock.patch.object(encoder, "encode_chunks_serial",
                              side_effect=lambda *a, **k: called_serial.append(k)):
            encoder.encode_chunks(
                chunks=[Path("a.mkv")], workdir=Path("wd"),
                parallel=1, crf=22, preset="slow",
                pix_fmt="yuv420p10le", x265_params="tune=animation",
                max_output_bytes=None,
                choke_threshold_speed=0, choke_grace_seconds=0,
                visual_quality_threshold=None,
            )
        self.assertEqual(len(called_serial), 1, "no guards -> serial")
        self.assertEqual(len(called_parallel), 0)


class ThresholdRangeValidationTest(unittest.TestCase):
    """CLI rejects out-of-range thresholds at parse time. Without this, the
    value silently disables the guard (<1) or false-aborts every chunk
    (>100) — both confusing failure modes."""

    def _parse_threshold(self, value: str) -> None:
        argv = ["prog", "--input", "x.mp4", "--output", "x.mkv",
                "--workdir", "wd", "--crf", "22", "--preset", "slow",
                "--pix-fmt", "yuv420p10le", "--x265-params", "tune=animation",
                "--visual-quality-threshold", value]
        with mock.patch.object(sys, "argv", argv):
            cli_args.parse_args()

    def test_rejects_negative(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_threshold("-5")

    def test_rejects_zero(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_threshold("0")

    def test_rejects_above_100(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_threshold("150")

    def test_accepts_boundary_1(self) -> None:
        # 1.0 is the lowest accepted value (smallest meaningful threshold).
        self._parse_threshold("1")  # no exception = pass

    def test_accepts_boundary_100(self) -> None:
        # 100.0 is the top of the VMAF scale.
        self._parse_threshold("100")

    def test_rejects_non_numeric(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_threshold("ninety")


class QueueStatusClassificationTest(unittest.TestCase):
    """v1.17.0: `stopped-quality-threshold` must be classified as ATTENTION
    (not FAILURE) in the queue aggregator so a quality-skipped file doesn't
    flag the whole queue as broken to a CI/wrapper exit-code consumer."""

    def test_stopped_quality_threshold_in_attention_set(self) -> None:
        import run_queue  # noqa: PLC0415 — module import gated to test
        self.assertIn("stopped-quality-threshold",
                     run_queue._ATTENTION_STATUSES)
        self.assertNotIn("stopped-quality-threshold",
                        run_queue._CLEAN_STATUSES)

    def test_stopped_quality_threshold_in_queue_counters(self) -> None:
        # Without this entry the counter hook would lose track of
        # quality-aborted jobs (none of finished/failed/stopped/skipped sets
        # would claim them, breaking the X265_QUEUE_ITEMS_STOPPED env var).
        from queue_modules import queue_counters  # noqa: PLC0415
        self.assertIn("stopped-quality-threshold",
                     queue_counters._STOPPED_STATUSES)


class ExitCodeMappingTest(unittest.TestCase):
    """Exit code 9 from encode_resumable.py is mapped to the human-readable
    status string consumed by the queue report + on_queue_item_end hook."""

    def test_exit_9_maps_to_stopped_quality_threshold(self) -> None:
        self.assertEqual(
            job_runner.status_for_exit(9),
            "stopped-quality-threshold",
        )

    def test_other_exit_codes_unchanged(self) -> None:
        # Sanity: the new entry doesn't shadow the existing ones.
        self.assertEqual(job_runner.status_for_exit(0), "ok")
        self.assertEqual(job_runner.status_for_exit(3), "stopped-threshold")
        self.assertEqual(job_runner.status_for_exit(6), "pre-flight-failed")
        self.assertEqual(job_runner.status_for_exit(8), "stopped-by-user")

    def test_unknown_exit_code_falls_through(self) -> None:
        self.assertEqual(job_runner.status_for_exit(42), "failed-exit-42")


if __name__ == "__main__":
    unittest.main()

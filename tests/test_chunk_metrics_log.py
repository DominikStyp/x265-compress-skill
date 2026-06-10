"""Per-chunk + per-file encode metrics log (v1.18.0).

The encoder already COMPUTES per-chunk encode time, chunk duration, output
size, and (when the quality guard is on) VMAF + decision. v1.17.0 persisted
NONE of this — it only made it to the live terminal and was lost the moment
a queue ran unattended. ``encode_modules.chunk_metrics_log`` writes:

  * ``<workdir>/logs/<stem>.chunk_metrics.jsonl`` — one self-contained line
    per chunk event. Append-only + kill-safe per line (each line is its own
    valid JSON); reader is tolerant of a torn final line.
  * A per-file ``encode`` summary block folded into the existing
    ``<stem>.quality.json`` sidecar (and mirrored into the history JSONL),
    aggregating elapsed/bitrate/vmaf min/mean/max from the per-chunk rows.

Tests inject the worker-side base row and the guard-side merge separately
(matching the production seam) so each path can be exercised in isolation
without spawning ffmpeg / libvmaf.

This file covers the record/emission side: worker base rows, guard merges,
the disabled no-op, thread-safe appends, and the CLI + queue schema flags.
The per-file ``aggregate_summary`` rollup side — plus the init-time JSONL
lifecycle (truncate vs preserve) the aggregator depends on — lives in the
sibling ``tests/test_chunk_metrics_aggregate.py``.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.chunk_metrics_log import (  # noqa: E402
    ChunkMetricsLog,
)


class _FixtureMixin:
    """Per-test scratch dir + a populated ChunkMetricsLog. Subclasses set
    ``enabled`` / ``threshold`` to vary the construction."""

    enabled: bool = True
    quality_threshold: float | None = 90.0

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.jsonl = self.tmp_path / "video.x265.chunk_metrics.jsonl"
        self.position_of = {
            "src_0001.mkv": 1,
            "src_0002.mkv": 2,
            "src_0003.mkv": 3,
            "src_0004.mkv": 4,
        }
        self.log = ChunkMetricsLog(
            self.jsonl,
            enabled=self.enabled,
            position_of=self.position_of,
            width=1920, height=1080, fps=23.976,
            crf=22, preset="slow",
            quality_threshold=self.quality_threshold,
        )

    def tearDown(self) -> None:  # type: ignore[override]
        self._tmp.cleanup()
        super().tearDown()  # type: ignore[misc]

    def _read_jsonl(self) -> list[dict]:
        if not self.jsonl.exists():
            return []
        out: list[dict] = []
        for line in self.jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out


class WorkerBaseRowTest(_FixtureMixin, unittest.TestCase):
    """Worker writes one base row per finalized chunk: chunk_name, idx,
    encode time, chunk duration, output bytes, derived bitrate, plus the
    static file context (width/height/fps/crf/preset). vmaf/decision are
    null until the guard merges them."""

    def test_record_chunk_writes_full_base_row(self) -> None:
        self.log.record_chunk(
            chunk_name="src_0002.mkv",
            encode_elapsed_s=41.8,
            chunk_duration_s=97.0,
            output_bytes=5_123_344,
        )
        rows = self._read_jsonl()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Spec field set
        self.assertEqual(row["chunk_name"], "src_0002.mkv")
        self.assertEqual(row["chunk_idx"], 1)  # 0-based; position_of is 1-based
        self.assertAlmostEqual(row["encode_elapsed_s"], 41.8)
        self.assertAlmostEqual(row["chunk_duration_s"], 97.0)
        self.assertEqual(row["output_bytes"], 5_123_344)
        # Bitrate math: 5_123_344 * 8 / 97.0 / 1000 == 422.55…
        expected_kbps = 5_123_344 * 8 / 97.0 / 1000
        self.assertAlmostEqual(row["output_bitrate_kbps"],
                              expected_kbps, places=1)
        # Static file context
        self.assertEqual(row["width"], 1920)
        self.assertEqual(row["height"], 1080)
        self.assertAlmostEqual(row["fps"], 23.976, places=3)
        self.assertEqual(row["crf"], 22)
        self.assertEqual(row["preset"], "slow")
        # Guard fields default to null until merged
        self.assertIsNone(row["vmaf_mean"])
        self.assertIsNone(row["decision"])
        # Threshold echoes the guard's configured value so the row is
        # self-describing even without the guard merging.
        self.assertEqual(row["threshold"], 90.0)
        # Timestamp is present and looks like a unix epoch float.
        self.assertIsInstance(row["ts"], (int, float))
        self.assertGreater(row["ts"], 1_700_000_000)  # plausible 2026+ epoch

    def test_zero_duration_yields_null_bitrate_no_zero_division(self) -> None:
        # Defensive: chunk_duration_s of 0 (degenerate ffprobe / 0-byte chunk)
        # must not crash the recorder with ZeroDivisionError. Spec says null.
        self.log.record_chunk(
            chunk_name="src_0003.mkv",
            encode_elapsed_s=1.0,
            chunk_duration_s=0.0,
            output_bytes=1024,
        )
        rows = self._read_jsonl()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["output_bitrate_kbps"])


class GuardMergeTest(_FixtureMixin, unittest.TestCase):
    """Quality guard appends one update row per decision. The aggregator
    treats LAST-line-per-chunk_name as canonical so out-of-order writes
    from worker + guard both land cleanly."""

    def _base(self, name: str, idx_zero_based: int) -> None:
        # Worker writes its base row first (production order: rename + record).
        self.log.record_chunk(
            chunk_name=name,
            encode_elapsed_s=42.0,
            chunk_duration_s=97.0,
            output_bytes=4_000_000,
        )

    def test_decision_ok(self) -> None:
        self._base("src_0002.mkv", 1)
        self.log.update_chunk_quality(
            chunk_name="src_0002.mkv",
            vmaf_mean=93.71, decision="ok",
        )
        rows = self._read_jsonl()
        # Two lines (base + update). Aggregator uses the LATER one.
        self.assertEqual(len(rows), 2)
        last = rows[-1]
        self.assertEqual(last["chunk_name"], "src_0002.mkv")
        self.assertAlmostEqual(last["vmaf_mean"], 93.71)
        self.assertEqual(last["decision"], "ok")

    def test_decision_warmup_grace(self) -> None:
        # Warmup grace: vmaf was measured but threshold check was skipped.
        # Decision string distinguishes from a real ok so an analyst can spot
        # "chunk 0 vmaf is ALWAYS below threshold" by chunk_idx==0 +
        # decision=='warmup-grace'.
        self._base("src_0001.mkv", 0)
        self.log.update_chunk_quality(
            chunk_name="src_0001.mkv",
            vmaf_mean=88.20, decision="warmup-grace",
        )
        last = self._read_jsonl()[-1]
        self.assertEqual(last["decision"], "warmup-grace")
        self.assertAlmostEqual(last["vmaf_mean"], 88.20)

    def test_decision_abort(self) -> None:
        # Threshold failure -> decision='abort'. Aggregate then knows which
        # chunk killed the file without scanning for "vmaf < threshold".
        self._base("src_0003.mkv", 2)
        self.log.update_chunk_quality(
            chunk_name="src_0003.mkv",
            vmaf_mean=85.10, decision="abort",
        )
        last = self._read_jsonl()[-1]
        self.assertEqual(last["decision"], "abort")
        self.assertAlmostEqual(last["vmaf_mean"], 85.10)

    def test_decision_infra_fail_vmaf_none(self) -> None:
        # libvmaf crashed -> guard emits decision='infra-fail' with
        # vmaf_mean=None. Critical that the chunk is still in the log so the
        # analyst can see "the guard tried; it failed" instead of a silent gap.
        self._base("src_0004.mkv", 3)
        self.log.update_chunk_quality(
            chunk_name="src_0004.mkv",
            vmaf_mean=None, decision="infra-fail",
        )
        last = self._read_jsonl()[-1]
        self.assertEqual(last["decision"], "infra-fail")
        self.assertIsNone(last["vmaf_mean"])


class DisabledFlagTest(unittest.TestCase):
    """When enabled=False the log is a complete no-op: no file is created,
    no rows accumulate, the aggregate returns the empty shape."""

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.jsonl = self.tmp_path / "video.chunk_metrics.jsonl"

    def tearDown(self) -> None:  # type: ignore[override]
        self._tmp.cleanup()
        super().tearDown()

    def test_disabled_writes_nothing(self) -> None:
        log = ChunkMetricsLog(
            self.jsonl, enabled=False,
            position_of={"src_0001.mkv": 1},
            width=1920, height=1080, fps=23.976,
            crf=22, preset="slow", quality_threshold=None,
        )
        log.record_chunk(chunk_name="src_0001.mkv",
                        encode_elapsed_s=40.0,
                        chunk_duration_s=100.0,
                        output_bytes=5_000_000)
        log.update_chunk_quality(chunk_name="src_0001.mkv",
                                vmaf_mean=95.0, decision="ok")
        self.assertFalse(self.jsonl.exists(),
                        "disabled log must not create the JSONL file")
        # Aggregate still returns a usable shape (n_chunks=0).
        summary = log.aggregate_summary(
            source_codec=None, source_bytes=None,
            output_bytes=None, duration_s=None, size_percent=None,
            quality_aborted=False, quality_aborted_chunk=None,
        )
        self.assertEqual(summary["encode"]["n_chunks"], 0)


class GuardOffMetricsStillLoggedTest(_FixtureMixin, unittest.TestCase):
    """quality_threshold=None means the guard is off, but the per-chunk
    metrics (time/size/bitrate) MUST still log — spec is explicit on this."""

    quality_threshold: float | None = None

    def test_base_row_written_with_null_threshold(self) -> None:
        self.log.record_chunk(chunk_name="src_0002.mkv",
                              encode_elapsed_s=41.8,
                              chunk_duration_s=97.0,
                              output_bytes=5_123_344)
        rows = self._read_jsonl()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["chunk_name"], "src_0002.mkv")
        self.assertEqual(row["output_bytes"], 5_123_344)
        self.assertIsNone(row["threshold"])  # guard disabled -> null
        self.assertIsNone(row["vmaf_mean"])
        self.assertIsNone(row["decision"])


class ThreadSafeAppendTest(_FixtureMixin, unittest.TestCase):
    """Multiple parallel workers call record_chunk concurrently. Each line
    in the JSONL must be a complete valid JSON object — the file lock must
    prevent two writers from interleaving partial writes on the same line."""

    def test_concurrent_writers_produce_valid_lines(self) -> None:
        names = [f"src_{i:04d}.mkv" for i in range(1, 21)]
        # Extend position_of so all names resolve.
        for i, n in enumerate(names, 1):
            self.position_of[n] = i
        self.log = ChunkMetricsLog(
            self.jsonl, enabled=True, position_of=self.position_of,
            width=1920, height=1080, fps=23.976,
            crf=22, preset="slow", quality_threshold=90.0,
        )

        def worker(name: str) -> None:
            self.log.record_chunk(
                chunk_name=name,
                encode_elapsed_s=40.0,
                chunk_duration_s=100.0,
                output_bytes=5_000_000,
            )

        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = self._read_jsonl()
        self.assertEqual(len(rows), 20)
        # Every row a valid dict with required fields.
        for r in rows:
            self.assertIn("chunk_name", r)
            self.assertIn("encode_elapsed_s", r)


class AutoFixChokeEmitsRealMetricsTest(_FixtureMixin, unittest.TestCase):
    """REGRESSION (v1.18.0 reviewer M1): the auto-fix-choke recovery path
    renames `.part` -> `enc_*.mkv` after a successful re-encode. v1.18.0
    initial wiring forgot to call ``record_chunk_metrics`` from that path,
    so the QualityGuard's later ``update_chunk_quality`` would fall into
    the stub branch (encode_elapsed_s=0, output_bytes=0, bitrate=null).
    Last-wins aggregation then made the zeroed row canonical, silently
    poisoning ``output_bitrate_kbps.chunk_min`` and ``elapsed_s.min`` for
    any file that hit a choke + auto-fix.

    Reproducer: call update_chunk_quality WITHOUT a prior record_chunk and
    assert the resulting row's encode_elapsed_s is the stub zero. Then call
    record_chunk first (the FIX) and assert the merged row carries real
    numbers."""

    def test_guard_update_without_base_yields_stub_zero(self) -> None:
        # Document the failure mode the fix prevents. The stub row carries
        # the guard's vmaf but zero elapsed/bytes — exactly the foot-gun.
        self.log.update_chunk_quality(
            chunk_name="src_0002.mkv",
            vmaf_mean=95.0, decision="ok",
        )
        rows = self._read_jsonl()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["encode_elapsed_s"], 0.0)
        self.assertEqual(rows[0]["output_bytes"], 0)
        self.assertIsNone(rows[0]["output_bitrate_kbps"])

    def test_auto_fix_path_emits_real_metrics(self) -> None:
        # The fix: chunk_recovery.try_auto_fix_chunk calls record_chunk_metrics
        # before the guard's update. Simulate the production sequence:
        # 1. record_chunk (the rename + record callsite),
        # 2. update_chunk_quality (the guard's verdict),
        # and assert the LAST row has real numbers, not the stub zero.
        self.log.record_chunk(
            chunk_name="src_0002.mkv",
            encode_elapsed_s=42.0,
            chunk_duration_s=97.0,
            output_bytes=5_000_000,
        )
        self.log.update_chunk_quality(
            chunk_name="src_0002.mkv",
            vmaf_mean=95.0, decision="ok",
        )
        rows = self._read_jsonl()
        last = rows[-1]
        # Real numbers carried forward, not stub zeros.
        self.assertAlmostEqual(last["encode_elapsed_s"], 42.0)
        self.assertEqual(last["output_bytes"], 5_000_000)
        self.assertIsNotNone(last["output_bitrate_kbps"])
        self.assertAlmostEqual(last["vmaf_mean"], 95.0)


class UnknownChunkNameChunkIdxNullTest(_FixtureMixin, unittest.TestCase):
    """REGRESSION (v1.18.0 reviewer N6): chunk_idx must be ``null`` (not
    -1) when position_of doesn't know the chunk. -1 is indistinguishable
    from a real index in downstream analytics; null is unambiguous."""

    def test_unknown_name_yields_null_chunk_idx(self) -> None:
        self.log.record_chunk(
            chunk_name="src_9999.mkv",  # not in position_of
            encode_elapsed_s=40.0,
            chunk_duration_s=100.0,
            output_bytes=5_000_000,
        )
        rows = self._read_jsonl()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["chunk_idx"])


class CliFlagTest(unittest.TestCase):
    """The encoder's CLI exposes --no-log-chunk-metrics (opt-out, default
    ON). Matches the existing --no-report / --no-quality-check pattern."""

    def test_default_is_enabled(self) -> None:
        from encode_modules import cli_args
        from unittest import mock
        # Minimum required arg set + the chunk-metrics flag absent.
        argv = ["--input", "i", "--output", "o", "--workdir", "w",
                "--crf", "22", "--preset", "slow",
                "--pix-fmt", "yuv420p10le", "--x265-params", ""]
        with mock.patch("sys.argv", ["encode_resumable.py", *argv]):
            ns = cli_args.parse_args()
        self.assertFalse(ns.no_log_chunk_metrics,
                        "default must be ENABLED (no_log_chunk_metrics=False)")

    def test_no_log_chunk_metrics_flips_to_true(self) -> None:
        from encode_modules import cli_args
        from unittest import mock
        argv = ["--input", "i", "--output", "o", "--workdir", "w",
                "--crf", "22", "--preset", "slow",
                "--pix-fmt", "yuv420p10le", "--x265-params", "",
                "--no-log-chunk-metrics"]
        with mock.patch("sys.argv", ["encode_resumable.py", *argv]):
            ns = cli_args.parse_args()
        self.assertTrue(ns.no_log_chunk_metrics)


class QueueSchemaTest(unittest.TestCase):
    """The queue layer surfaces ``log_chunk_metrics`` as a per-job /
    per-defaults key. Default true => no flag emitted (compress.py default);
    explicit false => --no-log-chunk-metrics in the compress.py argv."""

    def test_log_chunk_metrics_is_recognized_key(self) -> None:
        from queue_modules.job_schema import VALID_KEYS
        self.assertIn("log_chunk_metrics", VALID_KEYS)

    def test_default_emits_no_flag(self) -> None:
        from queue_modules.job_schema import build_compress_argv
        argv = build_compress_argv({"input": "x.mp4"})
        self.assertNotIn("--no-log-chunk-metrics", argv,
                        "no log_chunk_metrics key => no flag (compress default)")

    def test_explicit_true_emits_no_flag(self) -> None:
        from queue_modules.job_schema import build_compress_argv
        argv = build_compress_argv({"input": "x.mp4",
                                    "log_chunk_metrics": True})
        self.assertNotIn("--no-log-chunk-metrics", argv,
                        "log_chunk_metrics:true matches default; no flag")

    def test_false_emits_opt_out_flag(self) -> None:
        from queue_modules.job_schema import build_compress_argv
        argv = build_compress_argv({"input": "x.mp4",
                                    "log_chunk_metrics": False})
        self.assertIn("--no-log-chunk-metrics", argv)


if __name__ == "__main__":
    unittest.main()

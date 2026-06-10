"""Per-file rollup / aggregation side of the chunk metrics log (v1.18.0).

Covers ``ChunkMetricsLog.aggregate_summary``: folding the per-chunk JSONL
rows into the per-file ``encode`` summary block (total/mean/min/max of
elapsed + bitrate + vmaf, last-wins-per-chunk_name dedup), the
torn-last-line tolerance that keeps a kill mid-write from crashing the
rollup, and the init-time JSONL lifecycle (truncate-on-fresh-encode vs
preserve-on-resume) that guarantees the rows the aggregator reads are
canonical for the current run.

The record/emission side (worker base rows, guard merges, disabled no-op,
thread-safe appends, CLI + queue schema flags) lives in the sibling
``tests/test_chunk_metrics_log.py``.
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


class AggregateSummaryTest(_FixtureMixin, unittest.TestCase):
    """The per-file rollup folds per-chunk metrics into total/mean/min/max.
    Same chunk_name written twice (base + guard update) deduplicates to the
    latest row."""

    def test_summary_aggregates_per_chunk(self) -> None:
        # Three chunks, one with vmaf merged.
        self.log.record_chunk(chunk_name="src_0001.mkv",
                              encode_elapsed_s=40.0,
                              chunk_duration_s=100.0,
                              output_bytes=5_000_000)
        self.log.update_chunk_quality(chunk_name="src_0001.mkv",
                                     vmaf_mean=92.0, decision="warmup-grace")
        self.log.record_chunk(chunk_name="src_0002.mkv",
                              encode_elapsed_s=50.0,
                              chunk_duration_s=100.0,
                              output_bytes=6_000_000)
        self.log.update_chunk_quality(chunk_name="src_0002.mkv",
                                     vmaf_mean=94.0, decision="ok")
        self.log.record_chunk(chunk_name="src_0003.mkv",
                              encode_elapsed_s=30.0,
                              chunk_duration_s=100.0,
                              output_bytes=4_000_000)
        # 3rd chunk has no guard merge — vmaf stays None in its row.

        summary = self.log.aggregate_summary(
            source_codec="h264", source_bytes=20_000_000,
            output_bytes=15_000_000, duration_s=300.0, size_percent=75.0,
            quality_aborted=False, quality_aborted_chunk=None,
        )

        enc = summary["encode"]
        # File-level constants echoed
        self.assertEqual(enc["width"], 1920)
        self.assertEqual(enc["height"], 1080)
        self.assertAlmostEqual(enc["fps"], 23.976, places=3)
        self.assertEqual(enc["source_codec"], "h264")
        self.assertEqual(enc["output_codec"], "hevc")
        self.assertEqual(enc["crf"], 22)
        self.assertEqual(enc["preset"], "slow")
        self.assertEqual(enc["n_chunks"], 3)
        self.assertEqual(enc["source_bytes"], 20_000_000)
        self.assertEqual(enc["output_bytes"], 15_000_000)
        self.assertAlmostEqual(enc["size_percent"], 75.0)
        self.assertAlmostEqual(enc["duration_s"], 300.0)

        # elapsed_s aggregates
        self.assertAlmostEqual(enc["elapsed_s"]["total"], 120.0)
        self.assertAlmostEqual(enc["elapsed_s"]["mean"], 40.0)
        self.assertAlmostEqual(enc["elapsed_s"]["min"], 30.0)
        self.assertAlmostEqual(enc["elapsed_s"]["max"], 50.0)

        # bitrate_kbps aggregates — overall from output/duration; per-chunk
        # mean/min/max from individual rows.
        # Overall: 15e6 * 8 / 300 / 1000 = 400
        self.assertAlmostEqual(enc["output_bitrate_kbps"]["overall"], 400.0)
        # Per-chunk: chunk1 = 5e6*8/100/1000 = 400; chunk2 = 480; chunk3 = 320
        self.assertAlmostEqual(enc["output_bitrate_kbps"]["chunk_mean"],
                              (400 + 480 + 320) / 3.0, places=1)
        self.assertAlmostEqual(enc["output_bitrate_kbps"]["chunk_min"], 320.0)
        self.assertAlmostEqual(enc["output_bitrate_kbps"]["chunk_max"], 480.0)

        # vmaf_chunk aggregates — only count rows that have vmaf set.
        self.assertEqual(enc["vmaf_chunk"]["count"], 2)
        self.assertAlmostEqual(enc["vmaf_chunk"]["mean"], 93.0)
        self.assertAlmostEqual(enc["vmaf_chunk"]["min"], 92.0)
        self.assertAlmostEqual(enc["vmaf_chunk"]["max"], 94.0)

        self.assertEqual(enc["quality_threshold"], 90.0)
        self.assertFalse(enc["quality_aborted"])

    def test_summary_empty_when_no_rows(self) -> None:
        # No record_chunk calls -> summary still returns a stable shape so
        # the queue runner can read it without branching. n_chunks=0 is the
        # sentinel.
        summary = self.log.aggregate_summary(
            source_codec=None, source_bytes=None,
            output_bytes=None, duration_s=None, size_percent=None,
            quality_aborted=False, quality_aborted_chunk=None,
        )
        self.assertIn("encode", summary)
        self.assertEqual(summary["encode"]["n_chunks"], 0)
        self.assertEqual(summary["encode"]["vmaf_chunk"]["count"], 0)


class TornJsonlLineTest(_FixtureMixin, unittest.TestCase):
    """A kill mid-write can truncate the last JSONL line. The aggregator
    must skip unparseable lines, not crash."""

    def test_aggregate_skips_torn_last_line(self) -> None:
        self.log.record_chunk(chunk_name="src_0001.mkv",
                              encode_elapsed_s=40.0,
                              chunk_duration_s=100.0,
                              output_bytes=5_000_000)
        # Simulate a torn write: append a bogus partial JSON line at the end.
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write('{"chunk_name":"src_0002.mkv","outpu')
            # Note: no closing brace, no newline.

        summary = self.log.aggregate_summary(
            source_codec=None, source_bytes=None,
            output_bytes=None, duration_s=None, size_percent=None,
            quality_aborted=False, quality_aborted_chunk=None,
        )
        # The valid first row counted; the torn fragment skipped, no crash.
        self.assertEqual(summary["encode"]["n_chunks"], 1)


class FreshEncodeTruncatesJsonlTest(unittest.TestCase):
    """REGRESSION (v1.18.0 reviewer M2): the JSONL is opened in append
    mode and never truncates. A user running multiple encode attempts of
    the same file (CRF sweep, retry after manual deletion) ends up with
    a JSONL that contains the UNION of all runs. Last-wins aggregation
    still yields a correct *current* rollup, but external analytics see
    multiple "first runs" interleaved. Fix: truncate at init when the
    workdir contains no `enc_*.mkv` chunks (= phase 1 about to run)."""

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.jsonl = self.tmp_path / "video.chunk_metrics.jsonl"
        self.workdir = self.tmp_path / "workdir"
        self.workdir.mkdir()

    def tearDown(self) -> None:  # type: ignore[override]
        self._tmp.cleanup()
        super().tearDown()

    def test_init_truncates_when_workdir_is_empty(self) -> None:
        # Pre-populate the JSONL as if a prior run wrote it.
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl.write_text('{"chunk_name":"stale_0001.mkv"}\n',
                              encoding="utf-8")
        # Fresh init with an EMPTY workdir = new encode = stale rows
        # must be cleared so the resulting JSONL is canonical for this run.
        from encode_modules.chunk_metrics_log import init_chunk_metrics_log
        init_chunk_metrics_log(
            self.jsonl, enabled=True,
            position_of={"src_0001.mkv": 1},
            width=1920, height=1080, fps=23.976,
            crf=22, preset="slow", quality_threshold=None,
            workdir=self.workdir,
        )
        self.assertFalse(self.jsonl.exists() and self.jsonl.read_text(),
                        "stale JSONL must be cleared when workdir is empty")

    def test_init_preserves_jsonl_when_workdir_has_enc_chunks(self) -> None:
        # Pre-populate the JSONL and put an enc_*.mkv in the workdir to
        # simulate a resumed encode. The JSONL must NOT be truncated —
        # the prior rows are still authoritative for the chunks we won't
        # re-encode.
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl.write_text('{"chunk_name":"src_0001.mkv"}\n',
                              encoding="utf-8")
        (self.workdir / "enc_src_0001.mkv").write_text("fake")
        from encode_modules.chunk_metrics_log import init_chunk_metrics_log
        init_chunk_metrics_log(
            self.jsonl, enabled=True,
            position_of={"src_0001.mkv": 1, "src_0002.mkv": 2},
            width=1920, height=1080, fps=23.976,
            crf=22, preset="slow", quality_threshold=None,
            workdir=self.workdir,
        )
        self.assertTrue(self.jsonl.exists())
        self.assertIn("src_0001.mkv", self.jsonl.read_text())


if __name__ == "__main__":
    unittest.main()

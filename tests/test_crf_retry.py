"""Opt-in retry_with_bigger_crf: when the size guard stops a job
(`stopped-threshold`), the queue re-encodes the same source at a higher CRF
until it fits under max_size_percent or crf_max is reached — escalating from
the CRF each attempt actually ran at, reusing the lossless split, and setting
the superseded encoded chunks aside (never deleting them).

run_one_job is monkeypatched so no encoder is spawned; the only filesystem test
is supersede_encoded_chunks itself.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules import job_runner  # noqa: E402
from queue_modules.crf_retry import (  # noqa: E402
    CRF_EXHAUSTED_STATUS,
    run_job_with_crf_retry,
)
# supersede_encoded_chunks stayed in job_runner — the retry loop calls it
# via `job_runner.supersede_encoded_chunks` so the monkey-patches below
# (which set the attribute on `job_runner`) keep working unchanged.
from queue_modules.job_runner import supersede_encoded_chunks  # noqa: E402
from queue_modules.job_schema import derive_workdir  # noqa: E402
from encode_modules.verify import find_missing_enc_chunks  # noqa: E402


class _ScriptedRunOneJob:
    """Replacement for run_one_job: records the CRF of each call and returns a
    scripted (status, row) per attempt. The row echoes the CRF actually used,
    matching run_one_job's real behavior (row['crf'] = plan crf)."""

    def __init__(self, statuses: list[str]):
        self._statuses = list(statuses)
        self.crfs_seen: list = []

    def __call__(self, *, compress_py, merged, i, n):
        crf = merged.get("crf")
        self.crfs_seen.append(crf)
        status = self._statuses.pop(0)
        return status, {"status": status, "crf": crf}


class CrfRetryDisabled(unittest.TestCase):
    def test_no_flag_runs_once_even_on_threshold(self) -> None:
        fake = _ScriptedRunOneJob(["stopped-threshold"])
        orig = job_runner.run_one_job
        job_runner.run_one_job = fake
        try:
            status, row = run_job_with_crf_retry(
                compress_py=Path("c.py"),
                merged={"input": "/x/foo.mp4", "crf": 21}, i=1, n=1)
        finally:
            job_runner.run_one_job = orig
        self.assertEqual(status, "stopped-threshold")
        self.assertEqual(fake.crfs_seen, [21])


class CrfRetryEscalation(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_run = job_runner.run_one_job
        self._orig_supersede = job_runner.supersede_encoded_chunks
        self.superseded_at: list = []
        job_runner.supersede_encoded_chunks = (
            lambda workdir, old_crf: self.superseded_at.append(old_crf) or 1)

    def tearDown(self) -> None:
        job_runner.run_one_job = self._orig_run
        job_runner.supersede_encoded_chunks = self._orig_supersede

    def test_escalates_until_under_threshold(self) -> None:
        fake = _ScriptedRunOneJob(
            ["stopped-threshold", "stopped-threshold", "ok"])
        job_runner.run_one_job = fake
        status, row = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": "/x/foo.mp4", "crf": 18,
                    "retry_with_bigger_crf": True, "crf_step": 1,
                    "crf_max": 28}, i=1, n=1)
        self.assertEqual(status, "ok")
        self.assertEqual(fake.crfs_seen, [18, 19, 20])   # escalated from used crf
        self.assertEqual(row["crf"], 20)                 # row reflects final crf
        self.assertEqual(self.superseded_at, [18, 19])   # set aside between tries

    def test_honours_crf_step(self) -> None:
        fake = _ScriptedRunOneJob(["stopped-threshold", "ok"])
        job_runner.run_one_job = fake
        status, _ = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": "/x/foo.mp4", "crf": 20,
                    "retry_with_bigger_crf": True, "crf_step": 3}, i=1, n=1)
        self.assertEqual(status, "ok")
        self.assertEqual(fake.crfs_seen, [20, 23])

    def test_bad_crf_config_bails_without_crashing(self) -> None:
        # A typo'd crf_max must not crash the queue mid-run: bail to the
        # already-obtained first result, escalate nothing.
        fake = _ScriptedRunOneJob(["stopped-threshold"])
        job_runner.run_one_job = fake
        status, _ = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": "/x/foo.mp4", "crf": 18,
                    "retry_with_bigger_crf": True, "crf_max": "twenty"},
            i=1, n=1)
        self.assertEqual(status, "stopped-threshold")
        self.assertEqual(fake.crfs_seen, [18])
        self.assertEqual(self.superseded_at, [])

    def test_exhaustion_at_crf_max(self) -> None:
        fake = _ScriptedRunOneJob(["stopped-threshold"] * 5)
        job_runner.run_one_job = fake
        status, row = run_job_with_crf_retry(
            compress_py=Path("c.py"),
            merged={"input": "/x/foo.mp4", "crf": 18,
                    "retry_with_bigger_crf": True, "crf_step": 1,
                    "crf_max": 20}, i=1, n=1)
        # Attempts at 18, 19, 20; next (21) exceeds cap -> exhausted.
        self.assertEqual(status, CRF_EXHAUSTED_STATUS)
        self.assertEqual(row["status"], CRF_EXHAUSTED_STATUS)
        self.assertEqual(fake.crfs_seen, [18, 19, 20])
        self.assertEqual(self.superseded_at, [18, 19])


class SupersedeEncodedChunks(unittest.TestCase):
    def test_moves_enc_chunks_keeps_split(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / ".split_done").write_text("done", encoding="utf-8")
            for i in (1, 2):
                (wd / f"src_000{i}.mkv").write_bytes(b"split")        # keep
                (wd / f"enc_src_000{i}.mkv").write_bytes(b"encoded")  # move
            (wd / "enc_src_0003.part.mkv").write_bytes(b"partial")    # move (.part)

            moved = supersede_encoded_chunks(wd, old_crf=18)

            self.assertEqual(moved, 3)
            # split + marker untouched
            self.assertTrue((wd / ".split_done").exists())
            self.assertTrue((wd / "src_0001.mkv").exists())
            self.assertTrue((wd / "src_0002.mkv").exists())
            # encoded chunks gone from the workdir top level
            self.assertEqual(sorted(wd.glob("enc_src_*.mkv")), [])
            # ...and preserved (not deleted) under a superseded subdir
            aside = list(wd.glob(".crf18_superseded_*"))
            self.assertEqual(len(aside), 1)
            moved_names = {p.name for p in aside[0].iterdir()}
            self.assertEqual(
                moved_names,
                {"enc_src_0001.mkv", "enc_src_0002.mkv", "enc_src_0003.part.mkv"})
            # Closing the loop: with the enc chunks set aside, the encoder's
            # own resume gate now sees every split chunk as needing a (new-CRF)
            # re-encode — i.e. no old-CRF chunk can leak into the next attempt.
            missing = {p.name for p in find_missing_enc_chunks(wd)}
            self.assertEqual(missing, {"enc_src_0001.mkv", "enc_src_0002.mkv"})

    def test_noop_when_nothing_encoded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "src_0001.mkv").write_bytes(b"split")
            self.assertEqual(supersede_encoded_chunks(wd, old_crf=20), 0)
            self.assertEqual(list(wd.glob(".crf*_superseded_*")), [])

    def test_missing_workdir_is_safe(self) -> None:
        self.assertEqual(
            supersede_encoded_chunks(Path("/no/such/dir/x"), old_crf=20), 0)


class DeriveWorkdir(unittest.TestCase):
    def test_matches_encoder_layout(self) -> None:
        wd = derive_workdir(Path("/movies/foo.mp4"))
        self.assertEqual(wd.name, ".compress_foo")
        self.assertEqual(wd.parent.name, ".tmp")


if __name__ == "__main__":
    unittest.main()

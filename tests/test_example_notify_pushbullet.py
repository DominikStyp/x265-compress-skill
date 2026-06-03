"""The shipped on_chunk_done example (examples/notify_pushbullet.py) builds
its Pushbullet payload purely from X265_* context and must NOT carry any
hardcoded secret — the token/device come from the environment.
`build_payload` is the pure, network-free seam, so we can assert its output
without touching the API.

Since v1.14.0 the example dispatches on `X265_HOOK_EVENT` and ships one
payload shape per known hook event:

  chunk-done       — per-chunk progress notification (original behaviour)
  job-end          — per-source terminal status, incl. ⚠️ SIZE LIMIT
                     for stopped-threshold[+crf-exhausted]
  file-complete    — per-source success with queue-counter context
  queue-item-end   — queue snapshot after each finished job

Back-compat rule: missing `X265_HOOK_EVENT` (or any unknown event string)
must reproduce the legacy chunk push byte-identical to v1.13.x and
earlier, so anyone still wiring only `on_chunk_done` sees no behaviour
change after upgrading.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

EXAMPLE = (Path(__file__).resolve().parent.parent
           / "examples" / "notify_pushbullet.py")


def _load():
    spec = importlib.util.spec_from_file_location("notify_pushbullet", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ChunkDoneBuilderTest(unittest.TestCase):
    """The original v1.13.x chunk-done payload. These assertions are the
    back-compat contract: any change to `build_payload` must keep them
    green so existing on_chunk_done wirings are unaffected."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_ok_chunk_title_body_and_no_device_by_default(self) -> None:
        # Index is the just-finished chunk; done/percent come from the new
        # ground-truth env vars (out-of-order parallel completions stay honest).
        p = self.mod.build_payload({
            "X265_CHUNK_INDEX": "7", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "4", "X265_PROGRESS_PERCENT": "38.2",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        })
        self.assertEqual(p["type"], "note")
        self.assertEqual(p["title"], "Chunk-07-Done, 4/10 done (38.2%)")
        self.assertEqual(p["body"], "movie.mp4")
        # PUSHBULLET_DEVICE unset -> omit the key entirely (push to all devices)
        self.assertNotIn("device_iden", p)

    def test_failed_chunk_says_failed(self) -> None:
        p = self.mod.build_payload({
            "X265_CHUNK_INDEX": "3", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "0", "X265_PROGRESS_PERCENT": "0.0",
            "X265_CHUNK_STATUS": "failed", "X265_SOURCE": "/v/a.mkv",
        })
        self.assertIn("-FAILED,", p["title"])

    def test_device_included_when_set(self) -> None:
        p = self.mod.build_payload({
            "X265_CHUNK_INDEX": "1", "X265_CHUNK_TOTAL": "2",
            "X265_CHUNKS_DONE": "1", "X265_PROGRESS_PERCENT": "50.0",
            "X265_SOURCE": "/v/a.mkv", "PUSHBULLET_DEVICE": "DEV123",
        })
        self.assertEqual(p["device_iden"], "DEV123")

    def test_empty_env_no_div_by_zero(self) -> None:
        p = self.mod.build_payload({})
        # Missing progress vars -> "0/0 done (0.0%)" — no crash.
        self.assertIn("0/0 done (0.0%)", p["title"])
        self.assertEqual(p["body"], "(unknown source)")

    def test_progress_reflects_real_done_not_index_in_parallel(self) -> None:
        # Regression: parallel mode finishes chunk 10 first, but only 1 chunk
        # is actually done. The title MUST say 1/10 (10.0%), not 10/10 (100%).
        p = self.mod.build_payload({
            "X265_CHUNK_INDEX": "10", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "1", "X265_PROGRESS_PERCENT": "10.0",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/a.mkv",
        })
        self.assertEqual(p["title"], "Chunk-10-Done, 1/10 done (10.0%)")

    def test_explicit_chunk_done_event_matches_legacy(self) -> None:
        # X265_HOOK_EVENT=chunk-done — explicit form — produces the exact
        # same payload as the legacy no-event form.
        env_implicit = {
            "X265_CHUNK_INDEX": "7", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "4", "X265_PROGRESS_PERCENT": "38.2",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        }
        env_explicit = dict(env_implicit, **{"X265_HOOK_EVENT": "chunk-done"})
        self.assertEqual(self.mod.build_payload(env_implicit),
                         self.mod.build_payload(env_explicit))


class JobEndBuilderTest(unittest.TestCase):
    """on_job_end — the key new wiring. Size-stops, CRF-exhausted, and real
    failures each get their own visual class so notification consumers can
    distinguish "the size guard caught it" from "the encode crashed"."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_stopped_threshold_emits_size_limit_warning(self) -> None:
        # The motivating case: a size stop is NOT "Done" and NOT a "FAILED"
        # — give it its own ⚠️ tag. The JOB_STOP_DETAIL banner travels to
        # the body so the user sees the projection that tripped it.
        # Frozen title (assertEqual, not assertIn) — locks the spec'd
        # contract so a regression to "SIZE LIMIT EXCEEDED MAYBE" or
        # similar doesn't slip through unnoticed.
        detail = ("Estimated output 2659.6 MB (85.0% of source) exceeds "
                  "threshold 2659.3 MB (85.0%). Stopped at 89.2% overall "
                  "progress.")
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-threshold",
            "X265_JOB_STOP_REASON": "stopped-threshold",
            "X265_JOB_STOP_DETAIL": detail,
            "X265_SOURCE": "/v/big.mp4",
            "X265_CRF": "21", "X265_CRF_RETRY_CHAIN": "21",
        })
        self.assertEqual(p["type"], "note")
        self.assertEqual(p["title"], "⚠️ SIZE LIMIT · CRF 21")
        self.assertEqual(p["body"], f"big.mp4\n{detail}")

    def test_stopped_threshold_crf_exhausted_distinguishable(self) -> None:
        # The "crf-exhausted" variant means raising crf_max won't help.
        # The title must say so explicitly so the user doesn't waste time
        # re-running with a higher crf_max.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-threshold-crf-exhausted",
            "X265_JOB_STOP_REASON": "stopped-threshold-crf-exhausted",
            "X265_JOB_STOP_DETAIL": "ran out at crf 28",
            "X265_SOURCE": "/v/hard.mp4",
            "X265_CRF": "28", "X265_CRF_RETRY_CHAIN": "23,24,25,26,27,28",
        })
        # Frozen title: "(CRF maxed)" tag + the actual escalation chain.
        self.assertEqual(p["title"],
                         "⚠️ SIZE LIMIT (CRF maxed) · CRF 23,24,25,26,27,28")

    def test_stopped_threshold_with_empty_crf_chain_degrades_gracefully(
            self) -> None:
        # Edge case: encoder set X265_CRF but left X265_CRF_RETRY_CHAIN
        # empty (a very-early stop before any escalation, or a future
        # encoder that drops the chain). Title must NOT show "CRF " with
        # nothing after it; it falls back to the single CRF value.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-threshold",
            "X265_SOURCE": "/v/x.mp4",
            "X265_CRF": "21", "X265_CRF_RETRY_CHAIN": "",
        })
        self.assertEqual(p["title"], "⚠️ SIZE LIMIT · CRF 21")

    def test_stopped_threshold_with_no_crf_at_all_uses_placeholder(
            self) -> None:
        # Deepest degradation: neither CRF nor chain published. Title
        # must still parse — `· CRF ?` is the documented placeholder.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-threshold",
            "X265_SOURCE": "/v/x.mp4",
        })
        self.assertEqual(p["title"], "⚠️ SIZE LIMIT · CRF ?")

    def test_ok_status_emits_done_with_savings(self) -> None:
        # Frozen title — the encoder formats pct_saved as `{:.2f}`, so
        # the example MUST pass it through verbatim (not re-parse to int).
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "ok",
            "X265_SOURCE": "/v/good.mp4",
            "X265_CRF": "21", "X265_CRF_RETRY_CHAIN": "21",
            "X265_PCT_SAVED": "35.05",
        })
        self.assertEqual(p["title"], "✅ DONE · CRF 21 · saved 35.05%")
        self.assertEqual(p["body"], "good.mp4")

    def test_ok_status_without_pct_saved_still_works(self) -> None:
        # Don't crash and don't include "saved" if the encoder didn't
        # publish PCT_SAVED (e.g. very fast probe). The title must still
        # convey DONE clearly.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "ok",
            "X265_SOURCE": "/v/quick.mp4",
            "X265_CRF": "22", "X265_CRF_RETRY_CHAIN": "22",
        })
        self.assertIn("DONE", p["title"])
        self.assertNotIn("saved", p["title"])

    def test_pre_flight_failed_dedicated_class(self) -> None:
        # Per the spec — pre-flight is a distinct status the user often
        # acts on (corrupt source needs re-download/re-rip), so it gets
        # its own visual class.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "pre-flight-failed",
            "X265_JOB_STOP_REASON": "pre-flight-failed",
            "X265_JOB_STOP_DETAIL": "12 decoding errors in pre-scan",
            "X265_SOURCE": "/v/broken.mp4",
        })
        self.assertIn("PRE-FLIGHT", p["title"])
        self.assertIn("broken.mp4", p["body"])
        self.assertIn("12 decoding errors", p["body"])

    def test_generic_failure_emits_stop_marker(self) -> None:
        # Frozen title — generic failure uses upper-cased status + CRF.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "chunk-choked",
            "X265_JOB_STOP_REASON": "chunk-choked",
            "X265_JOB_STOP_DETAIL": "src_0003.mkv, src_0007.mkv",
            "X265_SOURCE": "/v/x.mp4",
            "X265_CRF": "23",
        })
        self.assertEqual(p["title"], "⛔ CHUNK-CHOKED · CRF 23")

    def test_job_end_device_iden_when_pushbullet_device_set(self) -> None:
        # The transport key (device_iden) is set by `build_payload` from
        # PUSHBULLET_DEVICE — independent of the event. Regression guard:
        # someone refactoring builders mustn't accidentally move the
        # device logic into the chunk-done builder.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "ok",
            "X265_SOURCE": "/v/x.mp4", "X265_CRF": "22",
            "PUSHBULLET_DEVICE": "DEV456",
        })
        self.assertEqual(p["device_iden"], "DEV456")

    def test_job_end_omits_device_iden_when_unset(self) -> None:
        # Symmetric to the chunk-done case — every event must omit the
        # key entirely when PUSHBULLET_DEVICE is unset.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "ok",
            "X265_SOURCE": "/v/x.mp4", "X265_CRF": "22",
        })
        self.assertNotIn("device_iden", p)

    def test_unknown_status_does_not_crash(self) -> None:
        # Forward-compat: a new status added upstream surfaces as a
        # generic stop, not a crash.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "brand-new-mystery",
            "X265_SOURCE": "/v/x.mp4",
            "X265_CRF": "22",
        })
        self.assertIn("BRAND-NEW-MYSTERY", p["title"].upper())


class FileCompleteBuilderTest(unittest.TestCase):
    """on_file_complete — success-only, the queue-counter env vars
    populated by run_queue.py travel through, so the push can say
    "3/8 done" without the script knowing about the queue."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_file_complete_with_queue_counters(self) -> None:
        # Frozen title — encoder formats pct_saved as "{:.2f}", so the
        # example must surface it verbatim ("30.5%" here, "30.50%" with
        # the encoder's real two-decimal formatter — both round-trip).
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "file-complete",
            "X265_SOURCE": "/v/clip.mp4",
            "X265_OUTPUT": "/v/clip.mkv",
            "X265_CRF": "22", "X265_CRF_RETRY_CHAIN": "21,22",
            "X265_PCT_SAVED": "30.50",
            "X265_QUEUE_INDEX": "3", "X265_QUEUE_TOTAL": "8",
            "X265_QUEUE_ITEMS_FINISHED": "3",
        })
        self.assertEqual(
            p["title"],
            "📦 FILE READY · 3/8 · CRF 21,22 · saved 30.50%")
        self.assertEqual(p["body"], "clip.mp4")

    def test_file_complete_single_file_mode_no_counters(self) -> None:
        # Single-file compress.py run -> counters fall through with the
        # 1/1 defaults the encoder seeds. Don't crash; show 1/1.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "file-complete",
            "X265_SOURCE": "/v/solo.mp4",
            "X265_CRF": "22",
            "X265_QUEUE_INDEX": "1", "X265_QUEUE_TOTAL": "1",
            "X265_QUEUE_ITEMS_FINISHED": "1",
        })
        self.assertIn("FILE READY", p["title"])
        self.assertIn("1/1", p["title"])

    def test_file_complete_device_iden_passthrough(self) -> None:
        # Per-event device_iden symmetry (regression guard — see
        # JobEndBuilderTest comment for the rationale).
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "file-complete",
            "X265_SOURCE": "/v/x.mp4", "X265_CRF": "22",
            "X265_QUEUE_INDEX": "1", "X265_QUEUE_TOTAL": "1",
            "PUSHBULLET_DEVICE": "DEV789",
        })
        self.assertEqual(p["device_iden"], "DEV789")


class QueueItemEndBuilderTest(unittest.TestCase):
    """on_queue_item_end — queue-side fire after each job (success or
    failure). The body is the full multi-line snapshot the runner ships
    in X265_QUEUE_STATUS_SUMMARY; the title shows the just-finished
    job's marker + name for the lock-screen glance."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_queue_item_end_ok_marker_with_summary_body(self) -> None:
        # Frozen title — marker leads (so it's always visible on a
        # truncated lock-screen preview even with a long filename).
        summary = ("[ 1] [OK]     a.mp4\n"
                   "[ 2] [OK]     b.mp4\n"
                   "[ 3] [..]     c.mp4")
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "queue-item-end",
            "X265_JOB_STATUS": "ok",
            "X265_JOB_MARKER": "[OK]",
            "X265_SOURCE": "/v/b.mp4",
            "X265_OUTPUT": "/v/b.mkv",
            "X265_QUEUE_STATUS_SUMMARY": summary,
        })
        self.assertEqual(p["type"], "note")
        self.assertEqual(p["title"], "Queue [OK] · b.mp4")
        self.assertEqual(p["body"], summary)

    def test_queue_item_end_device_iden_passthrough(self) -> None:
        # Per-event device_iden symmetry.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "queue-item-end",
            "X265_JOB_MARKER": "[OK]", "X265_SOURCE": "/v/x.mp4",
            "X265_QUEUE_STATUS_SUMMARY": "[1] [OK] x.mp4",
            "PUSHBULLET_DEVICE": "DEV321",
        })
        self.assertEqual(p["device_iden"], "DEV321")

    def test_queue_item_end_failed_marker(self) -> None:
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "queue-item-end",
            "X265_JOB_STATUS": "failed-gen",
            "X265_JOB_MARKER": "[FAILED]",
            "X265_SOURCE": "/v/broken.mp4",
            "X265_QUEUE_STATUS_SUMMARY": "[ 1] [FAILED] broken.mp4",
        })
        self.assertIn("[FAILED]", p["title"])
        self.assertIn("broken.mp4", p["title"])

    def test_queue_item_end_missing_summary_falls_back_cleanly(self) -> None:
        # Robust against a partial env: still produce SOMETHING readable.
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "queue-item-end",
            "X265_JOB_MARKER": "[OK]",
            "X265_SOURCE": "/v/x.mp4",
        })
        self.assertIn("[OK]", p["title"])
        self.assertTrue(p["body"], "body must not be empty")


class BackCompatTest(unittest.TestCase):
    """Acceptance-checklist guarantee: missing X265_HOOK_EVENT still
    produces the exact legacy chunk push. Anyone still wiring only
    `on_chunk_done` sees zero behaviour change after this upgrade."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_missing_event_is_chunk_done_byte_identical(self) -> None:
        env = {
            "X265_CHUNK_INDEX": "5", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "3", "X265_PROGRESS_PERCENT": "29.7",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        }
        p = self.mod.build_payload(env)
        # Frozen contract — exact title string.
        self.assertEqual(p["title"], "Chunk-05-Done, 3/10 done (29.7%)")
        self.assertEqual(p["body"], "movie.mp4")
        self.assertEqual(p["type"], "note")
        self.assertNotIn("device_iden", p)

    def test_unknown_event_falls_back_to_chunk_done(self) -> None:
        # Forward-compat: a future event added upstream that this old
        # example doesn't know about must not crash and must not emit a
        # confusing wrong-shape push. Falling back to chunk-done is the
        # least surprising choice — the existing chunk env vars are the
        # most likely to be present.
        env = {
            "X265_HOOK_EVENT": "some-future-event",
            "X265_CHUNK_INDEX": "5", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "3", "X265_PROGRESS_PERCENT": "29.7",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        }
        p = self.mod.build_payload(env)
        self.assertEqual(p["title"], "Chunk-05-Done, 3/10 done (29.7%)")


class NoHardcodedSecretTest(unittest.TestCase):
    def test_token_and_device_come_from_env_not_a_literal(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        # The specific leaked values must never be committed.
        self.assertNotIn("o.ohD", text)
        self.assertNotIn("ujxU7PM8", text)
        # Secrets must be read from the environment.
        self.assertIn("PUSHBULLET_TOKEN", text)
        self.assertIn("PUSHBULLET_DEVICE", text)


class QualityThresholdJobEndTest(unittest.TestCase):
    """v1.17.0: a `stopped-quality-threshold` job-end gets its own QUALITY
    FAIL branch with the encoder's stop-detail (chunk# + VMAF score) in
    the body — distinct from the SIZE LIMIT branch (different remedy)."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_quality_abort_title_and_body(self) -> None:
        detail = "chunk 4 (src_0004.mkv) VMAF=84.50 < 90"
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-quality-threshold",
            "X265_JOB_STOP_DETAIL": detail,
            "X265_SOURCE": "/Users/dominik/Movies/anime.mp4",
            "X265_CRF": "22", "X265_CRF_RETRY_CHAIN": "22",
        })
        self.assertEqual(p["title"], "📉 QUALITY FAIL · CRF 22")
        self.assertEqual(p["body"], f"anime.mp4\n{detail}")

    def test_quality_abort_distinct_from_size_limit(self) -> None:
        # Defensive: status string must not accidentally hit the SIZE LIMIT
        # branch — they're different failure modes with different remedies
        # (size = raise CRF; quality = lower CRF or skip).
        p = self.mod.build_payload({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-quality-threshold",
            "X265_SOURCE": "/x.mp4",
            "X265_CRF": "22",
        })
        self.assertNotIn("SIZE LIMIT", p["title"])
        self.assertIn("QUALITY", p["title"])


if __name__ == "__main__":
    unittest.main()

"""examples/notify_ntfy.py — a stdlib-only ntfy.sh notifier mirroring the
Pushbullet example's multi-event contract.

`build_notification(env)` is the pure, network-free seam: it returns the
four ntfy wire fields (title, body, tags, priority) so we can assert the
payload per event without touching the network. Parallels
test_example_notify_pushbullet.py.

Key ntfy-specific invariants under test:
  * The `Title` header is ASCII-sanitized — HTTP headers are latin-1, so
    emoji/smart punctuation must be stripped (never raise); the emoji ride
    in `Tags` instead.
  * chunk-done (ok) is LOW priority (2) so progress pings don't buzz the
    phone; failures + size-limit + real failures are high (4).
  * Missing/unknown X265_HOOK_EVENT falls through to chunk-done, matching
    the Pushbullet example.
  * A missing NTFY_TOPIC makes main() exit non-zero with a one-line stderr
    (no traceback) so the encoder logs a warning and keeps encoding.
"""
from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

EXAMPLE = (Path(__file__).resolve().parent.parent
           / "examples" / "notify_ntfy.py")


def _load():
    spec = importlib.util.spec_from_file_location("notify_ntfy", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AsciiTitleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_ascii_strips_emoji_and_never_raises(self) -> None:
        # HTTP headers are latin-1. An emoji-bearing string must survive
        # encode("latin-1") after sanitization, never raising.
        out = self.mod._ascii("DONE ✅ — saved 35%")
        out.encode("latin-1")  # would raise if emoji survived
        self.assertNotIn("✅", out)
        self.assertIn("DONE", out)

    def test_ascii_all_stripped_falls_back_to_placeholder(self) -> None:
        # An all-emoji title must not collapse to "" (ntfy shows nothing) —
        # falls back to a stable placeholder.
        self.assertTrue(self.mod._ascii("\U0001f600\U0001f389").strip())

    def test_ascii_strips_control_chars(self) -> None:
        # A POSIX filename can contain a newline; queue-item-end embeds the
        # basename in the Title header. A raw \n / \r / \t would make
        # http.client.putheader raise ValueError — they MUST be stripped.
        out = self.mod._ascii("Queue [OK] - a\nb\r\tc.mp4")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\t", out)
        out.encode("latin-1")  # header-encodable
        self.assertIn("Queue", out)


class ChunkDoneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_ok_chunk_is_low_priority_progress(self) -> None:
        title, body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "chunk-done",
            "X265_CHUNK_INDEX": "7", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "4", "X265_PROGRESS_PERCENT": "38.2",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        })
        self.assertIn("07", title)
        self.assertIn("4/10", title)
        self.assertIn("38.2", title)
        self.assertEqual(body, "movie.mp4")
        self.assertEqual(priority, "2")  # low: glanceable, no buzz

    def test_failed_chunk_is_high_priority(self) -> None:
        title, _body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "chunk-done",
            "X265_CHUNK_INDEX": "3", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "0", "X265_PROGRESS_PERCENT": "0.0",
            "X265_CHUNK_STATUS": "failed", "X265_SOURCE": "/v/a.mkv",
        })
        self.assertIn("FAILED", title)
        self.assertEqual(priority, "4")
        self.assertEqual(tags, "warning")

    def test_empty_env_no_div_by_zero(self) -> None:
        title, body, _t, _p = self.mod.build_notification({})
        self.assertIn("0/0", title)
        self.assertEqual(body, "(unknown source)")


class JobEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_ok_done_with_savings(self) -> None:
        title, body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "job-end", "X265_JOB_STATUS": "ok",
            "X265_SOURCE": "/v/good.mp4",
            "X265_CRF": "21", "X265_CRF_RETRY_CHAIN": "21",
            "X265_PCT_SAVED": "35.05",
        })
        self.assertIn("DONE", title)
        self.assertIn("CRF 21", title)
        self.assertIn("35.05", title)
        self.assertEqual(body, "good.mp4")
        self.assertEqual(tags, "white_check_mark")
        self.assertEqual(priority, "3")

    def test_size_limit_high_priority_with_detail_in_body(self) -> None:
        detail = ("Estimated output 622.4 MB (85.2% of source) exceeds "
                  "threshold 621.0 MB (85.0%).")
        title, body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-threshold",
            "X265_JOB_STOP_DETAIL": detail,
            "X265_SOURCE": "/v/big.mp4",
            "X265_CRF": "21", "X265_CRF_RETRY_CHAIN": "21",
        })
        self.assertIn("SIZE LIMIT", title)
        self.assertNotIn("CRF maxed", title)
        self.assertEqual(priority, "4")
        self.assertEqual(tags, "warning")
        self.assertIn("big.mp4", body)
        self.assertIn(detail, body)

    def test_size_limit_crf_exhausted_distinguished(self) -> None:
        title, _b, _t, _p = self.mod.build_notification({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "stopped-threshold-crf-exhausted",
            "X265_SOURCE": "/v/hard.mp4",
            "X265_CRF": "28", "X265_CRF_RETRY_CHAIN": "23,24,25,26,27,28",
        })
        self.assertIn("CRF maxed", title)
        self.assertIn("23,24,25,26,27,28", title)

    def test_pre_flight_failed_dedicated_class(self) -> None:
        title, body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "pre-flight-failed",
            "X265_JOB_STOP_DETAIL": "12 decoding errors in pre-scan",
            "X265_SOURCE": "/v/broken.mp4",
        })
        self.assertIn("PRE-FLIGHT", title.upper())
        self.assertEqual(tags, "no_entry")
        self.assertEqual(priority, "4")
        self.assertIn("broken.mp4", body)
        self.assertIn("12 decoding errors", body)

    def test_generic_failure_uppercases_status(self) -> None:
        title, _b, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "job-end",
            "X265_JOB_STATUS": "chunk-choked",
            "X265_SOURCE": "/v/x.mp4", "X265_CRF": "23",
        })
        self.assertIn("CHUNK-CHOKED", title.upper())
        self.assertEqual(tags, "no_entry")
        self.assertEqual(priority, "4")

    def test_ok_without_pct_saved_omits_saved(self) -> None:
        title, _b, _t, _p = self.mod.build_notification({
            "X265_HOOK_EVENT": "job-end", "X265_JOB_STATUS": "ok",
            "X265_SOURCE": "/v/quick.mp4", "X265_CRF": "22",
        })
        self.assertIn("DONE", title)
        self.assertNotIn("saved", title)


class FileCompleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_file_complete_with_queue_counters(self) -> None:
        title, body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "file-complete",
            "X265_SOURCE": "/v/clip.mp4", "X265_OUTPUT": "/v/clip.mkv",
            "X265_CRF": "22", "X265_CRF_RETRY_CHAIN": "21,22",
            "X265_PCT_SAVED": "30.50",
            "X265_QUEUE_INDEX": "3", "X265_QUEUE_TOTAL": "8",
        })
        self.assertIn("FILE READY", title)
        self.assertIn("3/8", title)
        self.assertIn("21,22", title)
        self.assertIn("30.50", title)
        self.assertEqual(body, "clip.mp4")
        self.assertEqual(priority, "3")


class QueueItemEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_ok_marker_with_summary_body(self) -> None:
        summary = "[ 1] [OK]     a.mp4\n[ 2] [..]     b.mp4"
        title, body, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "queue-item-end",
            "X265_JOB_STATUS": "ok", "X265_JOB_MARKER": "[OK]",
            "X265_SOURCE": "/v/a.mp4",
            "X265_QUEUE_STATUS_SUMMARY": summary,
        })
        self.assertIn("[OK]", title)
        self.assertIn("a.mp4", title)
        self.assertEqual(body, summary)
        self.assertEqual(tags, "white_check_mark")

    def test_failed_marker_high_priority(self) -> None:
        title, _b, tags, priority = self.mod.build_notification({
            "X265_HOOK_EVENT": "queue-item-end",
            "X265_JOB_STATUS": "failed-gen", "X265_JOB_MARKER": "[FAILED]",
            "X265_SOURCE": "/v/broken.mp4",
        })
        self.assertIn("[FAILED]", title)
        self.assertEqual(priority, "4")
        self.assertEqual(tags, "warning")


class BackCompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_missing_event_is_chunk_done(self) -> None:
        title, body, _t, _p = self.mod.build_notification({
            "X265_CHUNK_INDEX": "5", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "3", "X265_PROGRESS_PERCENT": "29.7",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        })
        self.assertIn("05", title)
        self.assertIn("3/10", title)
        self.assertEqual(body, "movie.mp4")

    def test_unknown_event_falls_back_to_chunk_done(self) -> None:
        title, _b, _t, _p = self.mod.build_notification({
            "X265_HOOK_EVENT": "some-future-event",
            "X265_CHUNK_INDEX": "5", "X265_CHUNK_TOTAL": "10",
            "X265_CHUNKS_DONE": "3", "X265_PROGRESS_PERCENT": "29.7",
            "X265_CHUNK_STATUS": "ok", "X265_SOURCE": "/v/movie.mp4",
        })
        self.assertIn("05", title)
        self.assertIn("3/10", title)


class MissingTopicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_main_clean_nonzero_exit_on_missing_topic(self) -> None:
        # No NTFY_TOPIC -> non-zero exit, one-line stderr, NO traceback,
        # and crucially no network attempt.
        import os
        saved = os.environ.pop("NTFY_TOPIC", None)
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = self.mod.main()
            self.assertNotEqual(rc, 0)
            self.assertIn("NTFY_TOPIC", buf.getvalue())
        finally:
            if saved is not None:
                os.environ["NTFY_TOPIC"] = saved


class PushTransportTest(unittest.TestCase):
    """`_push` is the network seam. Drive it with a fake urlopen so the
    success / HTTP-error / transport-error branches are exercised without a
    real request, and confirm failures honour the no-raise + notify-log
    contract."""

    def setUp(self) -> None:
        self.mod = _load()

    class _FakeResp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _with_topic(self, **extra):
        import os
        env = {"NTFY_TOPIC": "t"}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def test_success_returns_zero(self) -> None:
        with self._with_topic(), mock.patch.object(
                self.mod.urllib.request, "urlopen",
                return_value=self._FakeResp(200)):
            self.assertEqual(self.mod._push("T", "b", "tag", "3"), 0)

    def test_http_error_status_returns_one_and_logs(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            logp = Path(td) / "n.log"
            with self._with_topic(X265_NOTIFY_LOG=str(logp),
                                  X265_HOOK_EVENT="job-end"), \
                 mock.patch.object(self.mod.urllib.request, "urlopen",
                                   return_value=self._FakeResp(500)):
                self.assertEqual(self.mod._push("T", "b", "tag", "3"), 1)
            self.assertIn("HTTP 500", logp.read_text(encoding="utf-8"))

    def test_transport_error_returns_one(self) -> None:
        import urllib.error
        with self._with_topic(), mock.patch.object(
                self.mod.urllib.request, "urlopen",
                side_effect=urllib.error.URLError("boom")):
            self.assertEqual(self.mod._push("T", "b", "tag", "3"), 1)

    def test_value_error_from_header_is_caught(self) -> None:
        # Defense-in-depth: even if a control char reached putheader, the
        # ValueError must be swallowed into a clean non-zero, not a traceback.
        with self._with_topic(), mock.patch.object(
                self.mod.urllib.request, "urlopen",
                side_effect=ValueError("Invalid header value")):
            self.assertEqual(self.mod._push("T", "b", "tag", "3"), 1)

    def test_main_with_newline_in_source_does_not_raise(self) -> None:
        # End-to-end: a queue-item-end whose source basename has a newline
        # must NOT produce an uncaught traceback (the title is sanitized).
        with self._with_topic(X265_HOOK_EVENT="queue-item-end",
                              X265_JOB_MARKER="[OK]",
                              X265_SOURCE="/v/a\nb.mp4",
                              X265_QUEUE_STATUS_SUMMARY="s"), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               return_value=self._FakeResp(200)):
            self.assertEqual(self.mod.main(), 0)  # no exception


class NoHardcodedSecretTest(unittest.TestCase):
    def test_topic_and_token_from_env(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("NTFY_TOPIC", text)
        self.assertIn("NTFY_SERVER", text)
        self.assertIn("NTFY_TOKEN", text)
        # The local reference impl hardcoded a topic — must NOT be committed.
        self.assertNotIn("mac-x265-", text.replace("mac-x265-xxxxxxxxxxxx", ""))


class NotifyLogTest(unittest.TestCase):
    """CR-5 item 4: when X265_NOTIFY_LOG is set, a transport failure appends
    one secret-free line to that path; unset -> no file created."""

    def setUp(self) -> None:
        self.mod = _load()

    def test_failure_appends_when_log_path_set(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            logp = Path(td) / "notify.log"
            self.mod._append_notify_log(
                str(logp), event="job-end", outcome="URLError: boom")
            self.assertTrue(logp.exists())
            text = logp.read_text(encoding="utf-8")
            self.assertIn("job-end", text)
            self.assertIn("boom", text)

    def test_no_file_when_log_path_empty(self) -> None:
        # Empty/None path -> no-op, never raises, no file.
        self.mod._append_notify_log("", event="job-end", outcome="x")
        self.mod._append_notify_log(None, event="job-end", outcome="x")


if __name__ == "__main__":
    unittest.main()

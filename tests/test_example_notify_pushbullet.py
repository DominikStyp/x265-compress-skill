"""The shipped on_chunk_done example (examples/notify_pushbullet.py) builds its
Pushbullet payload purely from X265_* context and must NOT carry any hardcoded
secret — the token/device come from the environment. `build_payload` is the
pure, network-free seam, so we can assert its output without touching the API.
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


class BuildPayloadTest(unittest.TestCase):
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


class NoHardcodedSecretTest(unittest.TestCase):
    def test_token_and_device_come_from_env_not_a_literal(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        # The specific leaked values must never be committed.
        self.assertNotIn("o.ohD", text)
        self.assertNotIn("ujxU7PM8", text)
        # Secrets must be read from the environment.
        self.assertIn("PUSHBULLET_TOKEN", text)
        self.assertIn("PUSHBULLET_DEVICE", text)


if __name__ == "__main__":
    unittest.main()

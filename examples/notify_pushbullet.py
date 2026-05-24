"""on_chunk_done hook: push a Pushbullet note when an x265 chunk finishes.

The x265 encoder runs this with shell=False and passes per-chunk context via
X265_* environment variables (see encode_modules/chunk_hook.py). Stdlib only —
no pip install — matching the skill's no-third-party-deps promise.

Wire it into a queue (or a single run) by pointing on_chunk_done at this file:

    # queue.json
    "on_chunk_done": ["python3", "/path/to/notify_pushbullet.py"]   # POSIX
    "on_chunk_done": ["python",  "C:/path/to/notify_pushbullet.py"] # Windows

    # or: compress.py video.mp4 --resumable \
    #         --on-chunk-done '["python3","/path/to/notify_pushbullet.py"]'

Secrets come from the ENVIRONMENT, never this file — keep tokens out of the repo
and out of queue.json (anything committed or pasted leaks easily; revoke a
leaked token at pushbullet.com -> Settings -> Access Tokens):

    export PUSHBULLET_TOKEN=o.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    export PUSHBULLET_DEVICE=XXXXXXXXXXXXXXXXXXXXXX   # optional; unset = all devices

Title : "Chunk-07-Done, 7/10 (70.0%)"  (or "...-FAILED" if the chunk produced
        no output). Body: the source file name.

Exit 0 on success; non-zero on a missing token or a transport/API error, so the
encoder logs a one-line warning (it never aborts the encode over a hook).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Mapping

API_URL = "https://api.pushbullet.com/v2/pushes"
TIMEOUT_SEC = 20


def build_payload(env: Mapping[str, str]) -> dict:
    """Build the Pushbullet note from the X265_* context in `env`. Pure and
    network-free, so it's unit-testable. device_iden is omitted entirely when
    PUSHBULLET_DEVICE is unset (-> Pushbullet pushes to all devices), rather
    than sending an empty/placeholder value."""
    index = int(env.get("X265_CHUNK_INDEX", "0") or "0")
    total = int(env.get("X265_CHUNK_TOTAL", "0") or "0")
    status = env.get("X265_CHUNK_STATUS", "ok")
    source = env.get("X265_SOURCE", "")

    pct = (index / total * 100.0) if total else 0.0
    word = "Done" if status == "ok" else "FAILED"
    payload = {
        "type": "note",
        "title": f"Chunk-{index:02d}-{word}, {index}/{total} ({pct:.1f}%)",
        "body": os.path.basename(source) if source else "(unknown source)",
    }
    device = env.get("PUSHBULLET_DEVICE")
    if device:
        payload["device_iden"] = device
    return payload


def main() -> int:
    token = os.environ.get("PUSHBULLET_TOKEN")
    if not token:
        # Fail loud (but cheaply) — the encoder logs this one line and keeps
        # encoding. The token value is never printed.
        print("pushbullet: PUSHBULLET_TOKEN is not set in the environment",
              file=sys.stderr)
        return 2

    data = json.dumps(build_payload(os.environ)).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=data, method="POST",
        headers={"Access-Token": token,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status >= 300:
                print(f"pushbullet: HTTP {resp.status}", file=sys.stderr)
                return 1
    except (urllib.error.URLError, OSError) as e:
        # Never print the token; only the error type/message.
        print(f"pushbullet: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

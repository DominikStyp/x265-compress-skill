"""Pushbullet notifier — multi-event dispatcher for the four x265 hooks.

The encoder runs this with `shell=False` and passes per-event context via
`X265_*` environment variables. Stdlib only — no `pip install` — matching
the skill's no-third-party-deps promise.

Since v1.14.0 the script dispatches on `X265_HOOK_EVENT` so a single
notifier file can be wired at every hook the skill ships:

    # queue.json — wire the SAME script at every hook you care about
    "on_chunk_done":     ["python3", "/path/to/notify_pushbullet.py"],
    "on_job_end":        ["python3", "/path/to/notify_pushbullet.py"],
    "on_file_complete":  ["python3", "/path/to/notify_pushbullet.py"],
    "on_queue_item_end": ["python3", "/path/to/notify_pushbullet.py"]

    # or for a single run:
    # compress.py video.mp4 --resumable \
    #     --on-chunk-done '["python3","/path/to/notify_pushbullet.py"]' \
    #     --on-job-end    '["python3","/path/to/notify_pushbullet.py"]'

On Windows: `["python", "C:/path/to/notify_pushbullet.py"]`.

Why the `job-end` wiring is the important new one: an `on_chunk_done`-only
setup misses the **size-stop** notification, which is the single most
useful "this source needs my attention" message. A `stopped-threshold`
job pushes a ⚠️ SIZE LIMIT note carrying the encoder's projection banner
(via `X265_JOB_STOP_DETAIL`); the `stopped-threshold-crf-exhausted`
variant pushes ⚠️ SIZE LIMIT (CRF maxed) so you know raising `crf_max`
won't help.

Per-event payloads (the encoder formats `X265_PCT_SAVED` as `{:.2f}`, so
real pushes show `35.05%` not `35%`):

    chunk-done       "Chunk-07-Done, 4/10 done (38.2%)"      body: filename
    job-end (ok)     "✅ DONE · CRF 21,22 · saved 35.05%"    body: filename
    job-end (size)   "⚠️ SIZE LIMIT · CRF 21"                body: filename + projection
    job-end (fail)   "⛔ <STATUS> · CRF 21"                  body: filename + detail
    file-complete    "📦 FILE READY · 3/8 · CRF 22 · saved 30.50%"  body: filename
    queue-item-end   "Queue [OK] · filename"                 body: full queue snapshot

Title length: the four single-source / chunk titles stay short (≤ ~50
chars even with the longest realistic CRF chain). The `queue-item-end`
title is `Queue {marker} · {basename}` and does NOT truncate the
basename — a long filename pushes the title past the lock-screen
preview length; the marker is always visible because it leads.

Back-compat: a missing `X265_HOOK_EVENT` (or any unknown event string)
falls through to `chunk-done`, byte-identical to the v1.13.x notifier —
so anyone still wiring only `on_chunk_done` sees zero behaviour change.

Secrets come from the ENVIRONMENT, never this file — keep tokens out of
the repo and out of queue.json (anything committed or pasted leaks
easily; revoke a leaked token at pushbullet.com → Settings → Access
Tokens):

    export PUSHBULLET_TOKEN=o.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    export PUSHBULLET_DEVICE=XXXXXXXXXXXXXXXXXXXXXX   # optional; unset = all devices

Exit 0 on success; non-zero on a missing token or a transport/API error,
so the encoder logs a one-line warning (it never aborts the encode over
a hook).
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
    """Build the Pushbullet note from the X265_* context in `env`. Pure
    and network-free, so it's unit-testable. Dispatches on
    `X265_HOOK_EVENT`; missing or unknown event falls through to
    `chunk-done` for back-compat with the v1.13.x notifier. `device_iden`
    is omitted entirely when `PUSHBULLET_DEVICE` is unset (so Pushbullet
    pushes to all devices) rather than sending an empty value."""
    event = env.get("X265_HOOK_EVENT", "chunk-done")
    if event == "job-end":
        title, body = _build_job_end(env)
    elif event == "file-complete":
        title, body = _build_file_complete(env)
    elif event == "queue-item-end":
        title, body = _build_queue_item_end(env)
    else:
        # "chunk-done" + forward-compat unknown events. The chunk env
        # vars are the most likely to be present in an unknown future
        # event (the encoder usually carries them), so this is the
        # least-surprising fallback.
        title, body = _build_chunk_done(env)
    payload = {"type": "note", "title": title, "body": body}
    device = env.get("PUSHBULLET_DEVICE")
    if device:
        payload["device_iden"] = device
    return payload


def _build_chunk_done(env: Mapping[str, str]) -> tuple[str, str]:
    """Title: 'Chunk-07-Done, 4/10 done (38.2%)' — the just-finished
    chunk's positional index, then the REAL progress: how many chunks
    are actually done (parallel-safe) and what % of source duration
    that is. (Or '...-FAILED' for a chunk that produced no output.)
    Body: the source file name.

    X265_PROGRESS_PERCENT is ground-truth (counts enc_*.mkv on disk
    against probed source-chunk durations); INDEX is only the positional
    id of THIS chunk. In parallel mode the two diverge — chunk 10 may
    finish before chunk 2, so reporting index/total here would lie.
    """
    index = int(env.get("X265_CHUNK_INDEX", "0") or "0")
    total = int(env.get("X265_CHUNK_TOTAL", "0") or "0")
    done = int(env.get("X265_CHUNKS_DONE", "0") or "0")
    pct = float(env.get("X265_PROGRESS_PERCENT", "0") or "0")
    status = env.get("X265_CHUNK_STATUS", "ok")
    word = "Done" if status == "ok" else "FAILED"
    title = f"Chunk-{index:02d}-{word}, {done}/{total} done ({pct:.1f}%)"
    body = _source_basename(env)
    return title, body


def _build_job_end(env: Mapping[str, str]) -> tuple[str, str]:
    """Per-source terminal status. The interesting four classes:

      stopped-threshold[+crf-exhausted]  →  ⚠️ SIZE LIMIT (not a failure,
                                            queue keeps going, but the user
                                            wants to know)
      ok                                  →  ✅ DONE (+ pct saved)
      pre-flight-failed                   →  ⛔ PRE-FLIGHT FAILED (own class,
                                            user typically re-rips/redownloads)
      anything else terminal              →  ⛔ <STATUS>

    Body always includes the basename + the encoder's STOP_DETAIL banner
    (the same line shown on screen), so a push delivers the actionable
    detail without the user having to SSH in.
    """
    status = env.get("X265_JOB_STATUS", "")
    name = _source_basename(env)
    crf = env.get("X265_CRF") or "?"
    chain = env.get("X265_CRF_RETRY_CHAIN") or crf
    detail = env.get("X265_JOB_STOP_DETAIL", "")
    if status in ("stopped-threshold", "stopped-threshold-crf-exhausted"):
        # Visually distinct from both ok and fail — neither "Done" nor a
        # crash. The "CRF maxed" suffix on the crf-exhausted variant lets
        # the user immediately know raising crf_max won't help this source.
        tag = ("SIZE LIMIT (CRF maxed)"
               if status.endswith("crf-exhausted") else "SIZE LIMIT")
        title = f"⚠️ {tag} · CRF {chain}"
    elif status == "ok":
        saved = env.get("X265_PCT_SAVED", "")
        suffix = f" · saved {saved}%" if saved else ""
        title = f"✅ DONE · CRF {chain}{suffix}"
    elif status == "pre-flight-failed":
        title = "⛔ PRE-FLIGHT FAILED"
    else:
        # Generic failure / unknown future status. Upper-case the status
        # so a glance distinguishes it from the ok/size-limit classes.
        upper = (status or "FAILED").upper()
        title = f"⛔ {upper} · CRF {crf}"
    # Body: filename always; detail appended only when present so
    # success-bodies stay clean.
    body = f"{name}\n{detail}" if detail else name
    return title, body


def _build_file_complete(env: Mapping[str, str]) -> tuple[str, str]:
    """Per-source SUCCESS with queue-counter context. Title shows
    'N/M' progress against the whole queue (1/1 in single-file mode)
    plus the CRF chain and pct saved; body is the basename. Use this
    for 'celebrate one file done, here's where we are in the batch'.
    """
    name = _source_basename(env)
    idx = env.get("X265_QUEUE_INDEX") or "1"
    total = env.get("X265_QUEUE_TOTAL") or "1"
    chain = (env.get("X265_CRF_RETRY_CHAIN") or env.get("X265_CRF")
             or "?")
    saved = env.get("X265_PCT_SAVED", "")
    suffix = f" · saved {saved}%" if saved else ""
    title = f"📦 FILE READY · {idx}/{total} · CRF {chain}{suffix}"
    return title, name


def _build_queue_item_end(env: Mapping[str, str]) -> tuple[str, str]:
    """Queue-side fire after each finished job. Title: marker + name for
    a lock-screen glance; body: the full multi-line queue snapshot the
    runner ships in X265_QUEUE_STATUS_SUMMARY — one line per job marked
    [OK] / [FAILED] / [..]. A single push delivers a complete 'where
    are we now' picture without parsing job_reports."""
    marker = env.get("X265_JOB_MARKER", "[?]")
    name = _source_basename(env)
    summary = env.get("X265_QUEUE_STATUS_SUMMARY", "")
    title = f"Queue {marker} · {name}"
    # Don't ship an empty body — partial env (missing summary) should
    # still produce something readable. Fall back to the basename so the
    # user at least sees what just finished.
    body = summary if summary else name
    return title, body


def _source_basename(env: Mapping[str, str]) -> str:
    source = env.get("X265_SOURCE", "")
    return os.path.basename(source) if source else "(unknown source)"


def main() -> int:
    token = os.environ.get("PUSHBULLET_TOKEN")
    if not token:
        # Fail loud (but cheaply) — the encoder logs this one line and
        # keeps encoding. The token value is never printed.
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

"""ntfy.sh notifier — multi-event dispatcher for the four x265 hooks.

Stdlib only (no `pip install`), mirroring `notify_pushbullet.py`'s structure
and contract. The encoder runs this with `shell=False` and passes per-event
context via `X265_*` environment variables; the script dispatches on
`X265_HOOK_EVENT` so ONE file can be wired at every hook you care about:

    # queue.json — wire the SAME script at each hook
    "on_chunk_done":     ["python3", "/path/to/notify_ntfy.py"],
    "on_job_end":        ["python3", "/path/to/notify_ntfy.py"],
    "on_file_complete":  ["python3", "/path/to/notify_ntfy.py"],
    "on_queue_item_end": ["python3", "/path/to/notify_ntfy.py"]

    # or for a single run:
    # compress.py video.mp4 --resumable \
    #     --on-job-end    '["python3","/path/to/notify_ntfy.py"]' \
    #     --on-chunk-done '["python3","/path/to/notify_ntfy.py"]'

On Windows: `["python", "C:/path/to/notify_ntfy.py"]`.

Why ntfy: the Pushbullet free tier is capped at 500 pushes/MONTH (a rolling
counter that only clears on month rollover or by buying Pro) and fails opaquely
with HTTP 400 `pushbullet_pro_required` once exhausted. ntfy.sh's free tier is
~250 notifications/DAY with no monthly lockout, is open-source + self-hostable,
and needs no account or token for a public topic. See
`docs/AGENT_QUEUE_RECIPES.md` for when to wire `on_chunk_done` (fine on ntfy,
leave OFF on Pushbullet — it multiplies push volume by the chunk count).

Config comes from the ENVIRONMENT, never this file (the topic is effectively
the only secret on public ntfy.sh — anyone who knows it can read/publish):

    export NTFY_TOPIC=your-unguessable-topic     # required
    export NTFY_SERVER=https://ntfy.sh           # optional (self-host = your URL)
    export NTFY_TOKEN=tk_xxxxxxxxxxxx             # optional (protected topics)

ntfy delivery: POST the message BODY to <server>/<topic>; metadata in headers:
  Title    one ASCII line (HTTP headers are latin-1 -> emoji must NOT go here;
           `_ascii` strips them so urllib never raises)
  Tags     emoji shortcodes (white_check_mark / warning / no_entry /
           chart_decreasing / package / hourglass_flowing_sand)
  Priority 1..5 (3 default, 4 high for failures + size-limit, 2 low for
           per-chunk progress so it doesn't buzz the phone like the
           call-to-action job-end/failure alerts)

Per-event payloads (the encoder formats X265_PCT_SAVED as `{:.2f}`):

    chunk-done (ok)     "Chunk 07 done - 4/10 (38.2%)"   hourglass / 2 (low)
    chunk-done (fail)   "Chunk 07 FAILED - 4/10 (38.2%)" warning / 4
    job-end (ok)        "DONE - CRF 21 - saved 35.05%"   white_check_mark / 3
    job-end (size)      "SIZE LIMIT - CRF 21"            warning / 4 (+ detail)
    job-end (size-max)  "SIZE LIMIT (CRF maxed) - CRF 28" warning / 4
    job-end (quality)   "QUALITY FAIL - CRF 22"          chart_decreasing / 4
    job-end (pre-flight)"PRE-FLIGHT FAILED"              no_entry / 4 (+ detail)
    job-end (fail)      "<STATUS> - CRF 21"              no_entry / 4 (+ detail)
    file-complete       "FILE READY - 3/8 - CRF 22 - saved 30.50%"  package / 3
    queue-item-end      "Queue [OK] - file.mp4"          marker-tag / 3 or 4

Back-compat: a missing `X265_HOOK_EVENT` (or any unknown event string) falls
through to `chunk-done`, matching the Pushbullet example — so anyone wiring
only `on_chunk_done` sees the expected progress push.

Exit 0 on success; non-zero (with a one-line stderr) on a missing topic or a
transport/API error, so the encoder logs a warning (it never aborts an encode
over a hook). When `X265_NOTIFY_LOG` (optional path) is set, a transport
FAILURE is also appended as one secret-free line to that file, so a webhook
failure is recorded even when this script runs standalone (outside the queue)
or the encoder's stderr capture is bypassed.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from typing import Mapping

TIMEOUT_SEC = 20


def _ascii(s: str) -> str:
    """Sanitize a string for use as an HTTP header value (the ntfy `Title`).
    HTTP headers are latin-1, so emoji / smart punctuation are dropped — but
    we must ALSO strip control characters (newlines/CR/tabs). A POSIX source
    filename can legally contain a newline, and the queue-item-end title
    embeds the basename; a raw newline would make ``http.client.putheader``
    raise ``ValueError`` (header injection guard). Falls back to a stable
    placeholder when sanitization leaves nothing (e.g. an all-emoji title) so
    ntfy still shows a heading rather than an empty line."""
    cleaned = s.encode("ascii", "ignore").decode("ascii")
    # Keep only printable ASCII (space..~); drops \n \r \t and other controls.
    cleaned = "".join(c for c in cleaned if " " <= c <= "~")
    return cleaned.strip() or "x265"


def _source_basename(env: Mapping[str, str]) -> str:
    source = env.get("X265_SOURCE", "")
    return os.path.basename(source) if source else "(unknown source)"


def build_notification(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Build the ntfy (title, body, tags, priority) tuple from the X265_*
    context in `env`. Pure and network-free, so it's unit-testable.
    Dispatches on `X265_HOOK_EVENT`; missing or unknown event falls through
    to `chunk-done` for parity with the Pushbullet example."""
    event = env.get("X265_HOOK_EVENT", "chunk-done")
    if event == "job-end":
        return _build_job_end(env)
    if event == "file-complete":
        return _build_file_complete(env)
    if event == "queue-item-end":
        return _build_queue_item_end(env)
    # "chunk-done" + forward-compat unknown events.
    return _build_chunk_done(env)


def _build_chunk_done(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Per-chunk progress. X265_PROGRESS_PERCENT / X265_CHUNKS_DONE are
    ground-truth (counted from enc_*.mkv on disk) so parallel out-of-order
    completions stay honest; INDEX is only this chunk's positional id.

    OK progress is Priority 2 (low): a glanceable ping that does NOT buzz the
    phone like the call-to-action job-end/failure alerts. A chunk that
    produced no output is a real problem -> warning / 4."""
    index = int(env.get("X265_CHUNK_INDEX", "0") or "0")
    total = int(env.get("X265_CHUNK_TOTAL", "0") or "0")
    done = int(env.get("X265_CHUNKS_DONE", "0") or "0")
    pct = float(env.get("X265_PROGRESS_PERCENT", "0") or "0")
    status = env.get("X265_CHUNK_STATUS", "ok")
    name = _source_basename(env)
    if status == "ok":
        return (f"Chunk {index:02d} done - {done}/{total} ({pct:.1f}%)",
                name, "hourglass_flowing_sand", "2")
    return (f"Chunk {index:02d} FAILED - {done}/{total} ({pct:.1f}%)",
            name, "warning", "4")


def _crf_chain(env: Mapping[str, str]) -> str:
    """The CRF escalation chain if the encoder published one, else the single
    CRF, else the `?` placeholder — so a title never shows a dangling
    'CRF ' with nothing after it."""
    return env.get("X265_CRF_RETRY_CHAIN") or env.get("X265_CRF") or "?"


def _build_job_end(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Per-source terminal status. Distinct visual classes so a notification
    consumer can tell "the size guard caught it" (raise CRF) from "quality
    guard caught it" (lower CRF) from "real crash". The encoder's STOP_DETAIL
    banner rides in the body so the push carries the actionable detail."""
    status = env.get("X265_JOB_STATUS", "")
    name = _source_basename(env)
    chain = _crf_chain(env)
    detail = env.get("X265_JOB_STOP_DETAIL", "")
    if status in ("stopped-threshold", "stopped-threshold-crf-exhausted"):
        tag = ("SIZE LIMIT (CRF maxed)"
               if status.endswith("crf-exhausted") else "SIZE LIMIT")
        title = f"{tag} - CRF {chain}"
        return title, _body(name, detail), "warning", "4"
    if status == "stopped-quality-threshold":
        # Per-chunk VMAF guard fired. Different remedy from SIZE LIMIT (lower
        # CRF / skip, not raise) -> its own tag + emoji.
        return f"QUALITY FAIL - CRF {chain}", _body(name, detail), \
            "chart_decreasing", "4"
    if status == "ok":
        saved = env.get("X265_PCT_SAVED", "")
        suffix = f" - saved {saved}%" if saved else ""
        return f"DONE - CRF {chain}{suffix}", name, "white_check_mark", "3"
    if status == "pre-flight-failed":
        return "PRE-FLIGHT FAILED", _body(name, detail), "no_entry", "4"
    upper = (status or "FAILED").upper()
    crf = env.get("X265_CRF") or "?"
    return f"{upper} - CRF {crf}", _body(name, detail), "no_entry", "4"


def _build_file_complete(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Per-source SUCCESS with queue-counter context (N/M against the whole
    queue, 1/1 in single-file mode). Body is the basename."""
    name = _source_basename(env)
    idx = env.get("X265_QUEUE_INDEX") or "1"
    total = env.get("X265_QUEUE_TOTAL") or "1"
    chain = _crf_chain(env)
    saved = env.get("X265_PCT_SAVED", "")
    suffix = f" - saved {saved}%" if saved else ""
    title = f"FILE READY - {idx}/{total} - CRF {chain}{suffix}"
    return title, name, "package", "3"


def _build_queue_item_end(env: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Queue-side fire after each finished job. Title: marker + name for a
    lock-screen glance; body: the full multi-line queue snapshot the runner
    ships in X265_QUEUE_STATUS_SUMMARY. The marker drives tag/priority so a
    failed item in the batch buzzes (4) while an OK one is informational (3)."""
    marker = env.get("X265_JOB_MARKER", "[?]")
    name = _source_basename(env)
    summary = env.get("X265_QUEUE_STATUS_SUMMARY", "")
    title = f"Queue {marker} - {name}"
    body = summary if summary else name
    if marker == "[OK]":
        return title, body, "white_check_mark", "3"
    return title, body, "warning", "4"


def _body(name: str, detail: str) -> str:
    """Filename, plus the encoder's stop-detail banner on a second line when
    present — so success/clean bodies stay just the filename."""
    return f"{name}\n{detail}" if detail else name


def _append_notify_log(path: str | None, *, event: str, outcome: str) -> None:
    """Best-effort append of ONE secret-free line recording a transport
    failure, when X265_NOTIFY_LOG points somewhere. Never raises (a logging
    failure must not change the script's exit code) and never writes the
    topic/token (only the event + the error class/message). No-op on an
    empty/unset path."""
    if not path:
        return
    try:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t" \
               f"ntfy\t{event}\t{outcome}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _push(title: str, body: str, tags: str, priority: str) -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    log_path = os.environ.get("X265_NOTIFY_LOG", "").strip()
    event = os.environ.get("X265_HOOK_EVENT", "chunk-done")
    if not topic:
        print("ntfy: NTFY_TOPIC is not set in the environment",
              file=sys.stderr)
        _append_notify_log(log_path, event=event, outcome="NTFY_TOPIC unset")
        return 2
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": _ascii(title), "Tags": tags, "Priority": priority}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8"),
        method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status >= 300:
                outcome = f"HTTP {resp.status}"
                print(f"ntfy: {outcome}", file=sys.stderr)
                _append_notify_log(log_path, event=event, outcome=outcome)
                return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Never print the topic/token; only the error type/message. ValueError
        # covers http.client's header-injection guard (a stray control char in
        # a header) and an embedded NUL — defense-in-depth so a pathological
        # filename can't turn a notification into an uncaught traceback.
        outcome = f"{type(e).__name__}: {e}"
        print(f"ntfy: {outcome}", file=sys.stderr)
        _append_notify_log(log_path, event=event, outcome=outcome)
        return 1
    return 0


def main() -> int:
    return _push(*build_notification(os.environ))


if __name__ == "__main__":
    sys.exit(main())

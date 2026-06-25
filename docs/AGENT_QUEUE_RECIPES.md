# Queue recipes for AI agents

Paste-ready `queue.json` snippets for the most common single-prompt
scenarios. Each `queue.json` lives **next to the source videos**, not
inside this repo. Run with `python3 run_queue.py queue.json` (or
`python` on Windows).

---

## Recipe 1 — "Compress all videos in this folder"

Sensible defaults for a mixed bag of unknown sources. `--parallel auto`
picks 1 for 4K, 4 for 1080p, etc. `auto_patch_source` rescues broken
h264 sources automatically. `max_size_percent: 80` skips files that
won't save at least 20 %.

```json
{
  "defaults": {
    "crf": 22,
    "parallel": "auto",
    "max_size_percent": 80,
    "auto_patch_source": true,
    "auto_fix_choke": true,
    "resumable": true
  },
  "jobs": [
    {"input": "*.mp4"},
    {"input": "*.mkv"}
  ]
}
```

Globs are expanded against the queue file's directory. To pick up
sub-folders too, list them explicitly: `{"input": "season-01/*.mkv"}`.

---

## Recipe 2 — "Just compress these three files"

Explicit job list. Per-job overrides win over defaults.

```json
{
  "defaults": {
    "crf": 22,
    "parallel": "auto",
    "max_size_percent": 80,
    "auto_patch_source": true,
    "resumable": true
  },
  "jobs": [
    {"input": "movie1.mp4"},
    {"input": "movie2.mp4", "crf": 20, "preset": "slower"},
    {"input": "movie3.mp4", "crf": 24}
  ]
}
```

---

## Recipe 3 — "Smaller files, quality matters less"

CRF +2 above the auto value; threshold tightened so files that won't
shrink meaningfully are skipped fast.

```json
{
  "defaults": {
    "crf": 24,
    "parallel": "auto",
    "max_size_percent": 70,
    "resumable": true
  },
  "jobs": [
    {"input": "*.mp4"}
  ]
}
```

---

## Recipe 4 — "Visually lossless archival"

CRF 17 + slower preset. No size guard — keep the output regardless of
size. `auto_patch_source` still on in case the source has localized
h264 corruption.

```json
{
  "defaults": {
    "crf": 17,
    "preset": "slower",
    "parallel": "auto",
    "auto_patch_source": true,
    "resumable": true
  },
  "jobs": [
    {"input": "master.mp4"}
  ]
}
```

---

## Recipe 5 — "Anime / cartoon content"

x265 `:tune=animation` replaces the default sharpness/motion tuning
with the line-art-friendly profile.

```json
{
  "defaults": {
    "crf": 22,
    "anime": true,
    "parallel": "auto",
    "resumable": true
  },
  "jobs": [
    {"input": "*.mkv"}
  ]
}
```

---

## Recipe 6 — "Film with visible grain"

`:tune=grain` preserves the grain instead of smearing it. Slight CRF
drop because grain costs bits.

```json
{
  "defaults": {
    "crf": 20,
    "grain": true,
    "preset": "slower",
    "parallel": "auto",
    "resumable": true
  },
  "jobs": [
    {"input": "*.mp4"}
  ]
}
```

---

## Recipe 7 — "Mobile playback (old TV / phone that chokes on 10-bit HEVC)"

`eight_bit: true` forces 8-bit output (yuv420p) instead of the default
10-bit. Slightly less efficient but maximum decoder compatibility.

```json
{
  "defaults": {
    "crf": 22,
    "eight_bit": true,
    "parallel": "auto",
    "resumable": true
  },
  "jobs": [
    {"input": "*.mp4"}
  ]
}
```

---

## Recipe 8 — "Notify me as each chunk finishes (Pushbullet)"

`on_chunk_done` runs a command after every chunk (success *and* failure). It's an
argv list; chunk context arrives via `X265_*` env vars, so the script takes no
arguments. Best-effort — a slow or failing hook never stalls or aborts the encode.

> **Keep your access token OUT of `queue.json` and out of the script** — read it
> from an environment variable. A token written into a queue file or a committed
> script leaks easily; if one ever lands in a shell history, a log, or a paste,
> revoke it at **pushbullet.com → Settings → Access Tokens** and issue a new one.

**Ready-made:** a stdlib-only, cross-platform version ships in
[`examples/notify_pushbullet.py`](../examples/notify_pushbullet.py). Point the
hook straight at it — no `curl`/`jq`, identical on Windows and POSIX:

```json
{
  "defaults": {
    "crf": 22,
    "parallel": "auto",
    "resumable": true,
    "on_chunk_done": ["python3", "/path/to/examples/notify_pushbullet.py"]
  },
  "jobs": [
    {"input": "*.mkv"}
  ]
}
```

On Windows use `["python", "C:/path/to/examples/notify_pushbullet.py"]`. It reads
`PUSHBULLET_TOKEN` (required) and `PUSHBULLET_DEVICE` (optional) from the
environment, and pushes a note like `Chunk-07-Done, 4/10 done (38.2%)` with the
source filename as the body (`...-FAILED` for a chunk that produced no output).
The index is the just-finished chunk's position; `done`/`%` come from the
ground-truth env vars (`X265_CHUNKS_DONE`, `X265_PROGRESS_PERCENT`) so the
report stays honest in parallel mode where chunks finish out of order.

Prefer an inline shell hook instead of a separate file? Point `on_chunk_done` at
one of these (`["bash","/home/me/notify-pushbullet.sh"]` on POSIX,
`["pwsh","-File","C:/tools/notify-pushbullet.ps1"]` on Windows):

`/home/me/notify-pushbullet.sh`:

```bash
#!/usr/bin/env bash
# Token (and target device) come from the environment — NOT this file:
#   export PUSHBULLET_TOKEN=o.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   export PUSHBULLET_DEVICE=XXXXXXXXXXXXXXXXXXXXXX   # or delete the device_iden line to notify all devices
set -euo pipefail

# X265_PROGRESS_PERCENT is the encoder's ground-truth progress (counts
# enc_*.mkv on disk vs probed durations). X265_CHUNK_INDEX is only the
# positional id of THIS chunk — useless as progress in parallel mode.
name=$(basename "$X265_SOURCE")

curl -fsS -X POST https://api.pushbullet.com/v2/pushes \
  --header "Access-Token: ${PUSHBULLET_TOKEN:?set PUSHBULLET_TOKEN in your env}" \
  --header 'Content-Type: application/json' \
  --data @- <<JSON
{"type":"note",
 "title":"Chunk ${X265_CHUNK_INDEX} ${X265_CHUNK_STATUS} — ${X265_CHUNKS_DONE}/${X265_CHUNK_TOTAL} done (${X265_PROGRESS_PERCENT}%)",
 "body":"${name}",
 "device_iden":"${PUSHBULLET_DEVICE}"}
JSON
```

Windows — `"on_chunk_done": ["pwsh", "-File", "C:/tools/notify-pushbullet.ps1"]`, with:

```powershell
# notify-pushbullet.ps1 — token + device from the environment, never the file:
#   $env:PUSHBULLET_TOKEN, $env:PUSHBULLET_DEVICE  (drop device_iden to notify all devices)
$ErrorActionPreference = 'Stop'
# Ground truth (X265_PROGRESS_PERCENT / X265_CHUNKS_DONE) — NOT INDEX —
# is what to report as progress. INDEX is only the positional id of the
# just-finished chunk, which is misleading in parallel mode.
$payload = @{
    type        = 'note'
    title       = "Chunk $($env:X265_CHUNK_INDEX) $($env:X265_CHUNK_STATUS) — $($env:X265_CHUNKS_DONE)/$($env:X265_CHUNK_TOTAL) done ($($env:X265_PROGRESS_PERCENT)%)"
    body        = [IO.Path]::GetFileName($env:X265_SOURCE)
    device_iden = $env:PUSHBULLET_DEVICE
} | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri 'https://api.pushbullet.com/v2/pushes' `
    -Headers @{ 'Access-Token' = $env:PUSHBULLET_TOKEN } `
    -ContentType 'application/json' -Body $payload
```

Each finished chunk pushes a note like **"Chunk 7 ok — 4/10 done (38.2%)"** with
the source filename as the body. `X265_CHUNK_STATUS` is `failed` for a chunk
that produced no output, so you're alerted to problems too — not just progress.
The hook is best-effort: if Pushbullet is down, the `curl` failure is logged and
the encode keeps going.

> **Why `X265_CHUNKS_DONE` and `X265_PROGRESS_PERCENT` and not `X265_CHUNK_INDEX`
> / `X265_CHUNK_TOTAL`?** In parallel mode (`--parallel >1`) chunks finish out
> of order: chunk 10 of 10 may complete before chunk 2. `INDEX/TOTAL` would
> then read "100%" with 9 chunks of work left. `CHUNKS_DONE` and
> `PROGRESS_PERCENT` are derived from disk ground truth (which `enc_*.mkv`
> exist) summed against actual chunk durations, so they stay honest.

---

## Recipe 9 — "Notify me when a source is stopped by the size guard (Pushbullet)"

`on_chunk_done` fires per chunk and only knows chunk-level facts — so it
**cannot** distinguish "this job hit `max_size_percent` and was stopped"
from any other terminal status. That information lives on the **`on_job_end`**
event (added in 1.9.0), which fires once per source with the terminal
status, the projection banner, and the CRF chain.

Since 1.14.0 the shipped recipe at [`examples/notify_pushbullet.py`](../examples/notify_pushbullet.py)
dispatches on `X265_HOOK_EVENT` and produces a distinct payload per event,
so the same script can be wired at every hook you care about:

```json
{
  "defaults": {
    "crf": 22,
    "parallel": "auto",
    "resumable": true,
    "max_size_percent": 85,
    "retry_with_bigger_crf": true,
    "crf_max": 26,
    "on_chunk_done":     ["python3", "/path/to/examples/notify_pushbullet.py"],
    "on_job_end":        ["python3", "/path/to/examples/notify_pushbullet.py"],
    "on_file_complete":  ["python3", "/path/to/examples/notify_pushbullet.py"],
    "on_queue_item_end": ["python3", "/path/to/examples/notify_pushbullet.py"]
  },
  "jobs": [{"input": "*.mp4"}]
}
```

Push titles by event:

| Event | Status | Example title | Why it stands out |
|---|---|---|---|
| `chunk-done` | (per-chunk) | `Chunk-07-Done, 4/10 done (38.2%)` | Real ground-truth progress, parallel-safe |
| `job-end` | `ok` | `✅ DONE · CRF 21,22 · saved 35%` | Includes the CRF chain so you see escalation |
| `job-end` | `stopped-threshold` | `⚠️ SIZE LIMIT · CRF 21` | **Not a failure** — own visual class |
| `job-end` | `stopped-threshold-crf-exhausted` | `⚠️ SIZE LIMIT (CRF maxed) · CRF 28` | Means raising `crf_max` won't help this source |
| `job-end` | `pre-flight-failed` | `⛔ PRE-FLIGHT FAILED` | Re-rip / re-download the source |
| `job-end` | other failure | `⛔ <STATUS> · CRF 21` | Real crash; check logs |
| `file-complete` | (ok only) | `📦 FILE READY · 3/8 · CRF 22 · saved 30%` | Queue-counter context built in |
| `queue-item-end` | (per finished job) | `Queue [OK] · clip.mp4` | Body is the full queue snapshot |

The size-stop body carries the encoder's `X265_JOB_STOP_DETAIL` line (the
same `"Estimated output 2659.6 MB (85.0% of source) exceeds threshold
2659.3 MB (85.0%). Stopped at 89.2% overall progress."` you'd see on
stdout), so the push tells you the projection without an SSH-in.

**Token handling unchanged**: `PUSHBULLET_TOKEN` (required) and
`PUSHBULLET_DEVICE` (optional) come from environment variables only —
never the script, never `queue.json`. A leaked token can be revoked at
**pushbullet.com → Settings → Access Tokens**.

**`stopped-threshold-crf-exhausted` ↔ [`crf_max`](#per-job-keys-full-schema)**:
when the `(CRF maxed)` variant fires, the encoder walked up to `crf_max`
(default 28) without ever fitting under `max_size_percent`. The source
is already efficiently compressed — raising `crf_max` will keep degrading
quality without meaningful shrinkage. See `retry_with_bigger_crf` /
`crf_max` / `crf_step` in the schema table for the escalation knobs.

> **Back-compat**: a missing `X265_HOOK_EVENT` (or any unknown event)
> falls through to the chunk-done branch — byte-identical to the v1.13.x
> notifier — so wiring the v1.14.0 script at *only* `on_chunk_done` works
> exactly like before.

---

## Recipe 10 — "Notify me via ntfy.sh (no account, no monthly cap)"

Pushbullet's free tier is capped at **500 pushes per month** — a rolling
counter that only clears on month rollover (or by buying Pro). Once exhausted,
every push fails opaquely with `HTTP 400 {"error":{"code":
"pushbullet_pro_required", ...}}`, and there's no header telling you how many
pushes remain. [`examples/notify_ntfy.py`](../examples/notify_ntfy.py) uses
[ntfy.sh](https://ntfy.sh) instead: ~250 notifications/**day**, no monthly
lockout, open-source + self-hostable, and no account or token needed for a
public topic. Same `X265_HOOK_EVENT` dispatch and the same four events as the
Pushbullet example.

```json
{
  "defaults": {
    "crf": 22,
    "parallel": "auto",
    "resumable": true,
    "max_size_percent": 85,
    "on_job_end":     ["python3", "/path/to/examples/notify_ntfy.py"],
    "on_chunk_done":  ["python3", "/path/to/examples/notify_ntfy.py"]
  },
  "jobs": [{"input": "*.mkv"}]
}
```

On Windows: `["python", "C:/path/to/examples/notify_ntfy.py"]`. Config comes
from the environment (the topic is the only "secret" on public ntfy.sh — anyone
who knows it can read/publish, so make it unguessable):

```sh
export NTFY_TOPIC=your-unguessable-topic     # required — subscribe to it in the ntfy app
export NTFY_SERVER=https://ntfy.sh           # optional (self-host = your URL)
export NTFY_TOKEN=tk_xxxxxxxxxxxx            # optional (protected topics)
```

ntfy puts metadata in HTTP headers (`Title` / `Tags` / `Priority`) and the
message in the body. Because HTTP headers are latin-1, the notifier
ASCII-sanitizes the `Title` and carries the emoji as `Tags` shortcodes instead.

### Which event to wire on which transport

Wire **`on_job_end`** as the always-on baseline on *any* transport: one push
per finished file, and the only event carrying the size-stop banner. Treat
`on_chunk_done` as **transport-dependent**:

| Transport | Cap | `on_chunk_done`? |
|---|---|---|
| **Pushbullet** | 500/**month** (rolling) | **Leave OFF.** With `segments=10` it's ≈11 pushes/job — the cap is gone in ~22 jobs |
| **ntfy.sh** | ~250/**day** | Fine to enable. ≈11 pushes/job → ≈22 jobs/day before the daily cap |

`notify_ntfy.py` sends `on_chunk_done` (ok) at **Priority 2 (low)** so progress
pings don't buzz the phone, while job-end/failure alerts ride at Priority 3–4.

---

## Recipe 11 — "Robust notifications: ntfy with a Pushbullet fallback"

A single transport is a single point of failure (ntfy unreachable, or
Pushbullet quota exhausted). [`examples/notify_dispatch.py`](../examples/notify_dispatch.py)
runs an ordered list of notifiers and stops at the first success — exit 0 if
**any** delivered, non-zero only if **all** failed.

```json
{
  "defaults": {
    "resumable": true,
    "on_job_end": ["python3", "/path/to/examples/notify_dispatch.py"]
  },
  "jobs": [{"input": "*.mp4"}]
}
```

Set the transport secrets in the environment (`NTFY_TOPIC` for the primary,
`PUSHBULLET_TOKEN` for the fallback). The chain defaults to
`notify_ntfy.py` → `notify_pushbullet.py` resolved next to the dispatcher;
override it with `X265_NOTIFY_CHAIN` (an `os.pathsep`-separated list of notifier
script paths — `:` on POSIX, `;` on Windows). Every `X265_*` context var and
the secrets pass straight through to each child.

---

## Where hook errors are logged

Every hook fire — for **all** four events — records one JSONL line to
`<video_folder>/logs/<source>.hooks.log` (since v1.20.0):

```json
{"ts":"2026-06-25T18:03:11Z","event":"on_job_end","command":["python3","/p/notify_pushbullet.py"],"outcome":"exited 1","stderr_tail":"pushbullet: HTTP 400 ..."}
```

`outcome` is one of `ok` / `exited <rc>` / `timeout` / `spawn-error`; the
stderr tail (up to ~500 chars) is captured on failure so an opaque webhook
error like Pushbullet's `pushbullet_pro_required` 400 is recorded instead of
scrolling off the terminal. The line is **secret-free** — it logs the argv and
outcome, never the environment, so `PUSHBULLET_TOKEN` / `NTFY_TOKEN` never land
in the log.

Independently, the shipped notifiers append their own failure line to a file
named by the optional **`X265_NOTIFY_LOG`** env var (when set) — so a webhook
failure is recorded even when a notifier runs standalone, outside the queue:

```sh
export X265_NOTIFY_LOG=/path/to/notify-failures.log
```

---

## Environment variables

Switches read from the environment (not `queue.json`), useful on dedicated
encoder hardware and for redirecting logs:

| Env var | Effect | When to use | Default |
|---|---|---|---|
| `CLAUDE_ENCODING_NO_NICE` | Any non-empty value skips the idle-priority wrap (`nice -n 19` / `IDLE_PRIORITY_CLASS`) on every ffmpeg child. Lifecycle plumbing (killpg / Job Object) is unaffected | Dedicated encoder machine with no foreground workload competing for CPU (e.g. a Mac running batch encodes) | unset = wrap ON |
| `CLAUDE_ENCODING_HISTORY_PATH` | Sets the exact path of the append-only `encoding_history.jsonl`, verbatim | Redirect history to a portable location, or share one history file across machines | `C:\_MOJE\other\CUTTED\logs\encoding_history.jsonl` (Windows) / `~/x265-encoding/logs/encoding_history.jsonl` (macOS/Linux) |
| `NTFY_TOPIC` / `NTFY_SERVER` / `NTFY_TOKEN` | ntfy notifier config (topic required; server/token optional) | With `examples/notify_ntfy.py` | — / `https://ntfy.sh` / none |
| `PUSHBULLET_TOKEN` / `PUSHBULLET_DEVICE` | Pushbullet notifier config (token required; device optional) | With `examples/notify_pushbullet.py` | — / all devices |
| `X265_NOTIFY_CHAIN` | `os.pathsep`-separated ordered list of notifier scripts for `notify_dispatch.py` | Customize the primary→fallback order | `notify_ntfy.py` then `notify_pushbullet.py` |
| `X265_NOTIFY_LOG` | Path the notifier examples append a secret-free failure line to | Capture webhook failures from standalone notifier runs | unset = no notifier-side log |

---

## Size guard ↔ CRF interaction (tuning for a size budget)

The auto-picked starting CRF (`compress_modules/plan.py:pick_crf`) sets a
quality *floor*; the size gate (`max_size_percent`) drives the effective CRF
*up* from there. For 4K with a strict size budget (e.g. `max_size_percent ≤ 85`),
the shipped starting CRF frequently lands above the gate, so
`retry_with_bigger_crf` escalates `crf` by `crf_step` per pass until it fits —
meaning the job converges to a CRF higher than the recipe's default after one or
more rejected (cheap, ~5%-progress) passes. If you routinely encode
size-constrained 4K, **raise the starting `crf`** (≈23 is a common convergence
point) rather than only raising `crf_max` — that reaches the target in fewer
retry passes. See [`references/x265-tuning.md`](../references/x265-tuning.md)
for the full rationale.

---

## Per-job keys (full schema)

Any key in this table can appear in `defaults` (applies to all jobs)
or per-job (overrides the default for that one):

| Key | Type | Meaning |
|---|---|---|
| `input` | str / glob | **Required.** Absolute or relative path; globs supported |
| `crf` | int | Quality (lower = better, 17 = transparent, 22 = balanced, 28 = aggressive) |
| `preset` | str | `medium` / `slow` / `slower` / `veryslow` (speed ↔ compression) |
| `segments` | int | Target chunk count (default 10) |
| `segment_seconds` | int | Absolute chunk length in seconds (overrides `segments`) |
| `parallel` | int or `"auto"` | Concurrent ffmpegs. `"auto"` picks by source height |
| `max_size_percent` | float | Abort if projected output > N % of source |
| `anime` | bool | Use x265 `tune=animation` (line-art content) |
| `grain` | bool | Use x265 `tune=grain` (preserve film grain) |
| `eight_bit` | bool | Force 8-bit output (compatibility mode) |
| `resumable` | bool | Default **true** in queue mode; opt out with `false` |
| `auto_fix_choke` | bool | One auto-retry with relaxed motion params on choke |
| `no_pre_flight_scan` | bool | Skip the source-corruption pre-scan |
| `auto_patch_source` | bool | Surgically patch broken h264 GOPs and continue |
| `max_patch_seconds` | float | Loss budget for auto-patch (default 10) |
| `on_chunk_done` | argv list / str | Command run after each chunk (success+failure); context via `X265_*` env vars. Best-effort, 30 s timeout — never derails the encode |
| `on_job_end` | argv list / str | (1.9.0) Command run once per source at the terminal-status chokepoint — fires for **every** status (`ok`, `stopped-threshold[+crf-exhausted]`, `chunk-choked`, `pre-flight-failed`, `verify-failed`, `stopped-by-user`, `failed-*`). Env vars include `X265_JOB_STATUS`, `X265_JOB_STOP_REASON`, `X265_JOB_STOP_DETAIL` (the projection banner), `X265_CRF`, `X265_CRF_RETRY_CHAIN`, `X265_OUTPUT_BYTES_PROJECTED` / `_THRESHOLD`. The right hook for **size-stop notifications** — see Recipe 9 |
| `on_file_complete` | argv list / str | (1.10.0) Command run once per source on `ok` only (after the final `.mkv` is on disk). Carries per-file env vars **plus** queue-counter env vars (`X265_QUEUE_INDEX` / `_TOTAL` / `_ITEMS_FINISHED` / `_ITEMS_REMAINING` / `_BYTES_*_SO_FAR` / `_PCT_SAVED_SO_FAR`) so notifications can say "3 of 8 done · saved 28% so far". Falls back to `1/1/0/0` defaults in single-file `compress.py` mode so the same script works in both contexts |
| `on_queue_item_end` | argv list / str | (1.13.0, queue-only) Command run by `run_queue.py` after each finished job (success **or** failure — but not skips). Ships `X265_QUEUE_STATUS_SUMMARY`: a multi-line snapshot of the whole queue marked `[OK]` / `[FAILED]` / `[..]` per job, plus `X265_JOB_MARKER` for the just-finished one. One push delivers a complete "where are we now" picture |
| `retry_with_bigger_crf` | bool | (1.6.0, queue-only) Auto-escalate CRF on a `stopped-threshold` abort. Re-encodes the same source at `crf + crf_step` until under `max_size_percent` or `crf_max` is hit. Cheap by design — the size guard aborts at ~5% progress, so each rejected CRF costs a fraction of an encode |
| `crf_step` | int | (queue-only) How much to raise CRF per retry. Default **1**. Increase for faster convergence on stubborn sources (at the cost of overshooting the minimum feasible CRF by a point or two) |
| `crf_max` | int | (queue-only) Cap on `retry_with_bigger_crf` escalation. Default **28**. When hit, the job ends as `stopped-threshold-crf-exhausted` (a needs-attention status, doesn't stop the queue). The "CRF maxed" tag in Recipe 9's size-limit push means raising this won't help — the source is already efficiently compressed |
| `done_dir` | path | (1.11.0) Move BOTH source and output into this directory after `status == ok`. `~` expands; relative paths resolve against the queue.json's directory. Cross-volume safe; refuses to overwrite or move into a workdir. State recorded in `<queue_stem>.state.json` so a re-run silently skips with status `skipped-done` |

---

## Exit-code → status mapping (run_queue.py / encode_resumable.py)

| Exit | Status string | Meaning |
|---|---|---|
| 0 | `ok` | Encode + verify both clean |
| 3 | `stopped-threshold` | Aborted by `--max-size-percent` — output not worth keeping. **Not a failure** — queue continues |
| 4 | `failed-exit-4` | Verify failed after retries. Output renamed to `damaged_<name>.<ext>`. Chunks preserved |
| 6 | `pre-flight-failed` | Source corruption pre-scan caught it. No encoding work done. With `auto_patch_source: true`, an h264 source would have been patched first |
| 7 | `awaiting-chunk-fix` | At least one chunk choked. `enc_<chunk>.needs_fix.json` sidecar dropped in workdir. Re-run after fixing |
| 8 | `stopped-by-user` | You pressed `f` / created a `FINISH` file — finished the current chunk(s), then stopped. Resumable; re-run to continue. **Halts the queue.** |
| other | `failed-exit-N` | Genuine error. See logs |

The queue runner keeps going past `stopped-threshold` and most failures by default. Pass `--stop-on-failure` to bail on the first real failure.

**`run_queue.py` aggregate exit code** (distinct from the per-job codes above):

| Exit | Meaning |
|---|---|
| 0 | Every job clean (`ok` / `skipped-exists`) |
| 1 | At least one real failure (`failed-gen` / `failed-parse` / `failed-exit-N`) |
| 2 | No hard failure, but a job needs attention (`stopped-threshold`, `awaiting-chunk-fix`, `skipped-not-found`, `pre-flight-failed`, `chunk-choked`) |

A hard failure (1) outranks needs-attention (2). Add `--json-status <path>` to also append one NDJSON record per job (`{input, status, output, input_bytes, output_bytes, elapsed_seconds, vmaf_mean}`) — machine-readable for fleet monitoring, while stdout stays human-readable. `tail -f` it to watch a run live.

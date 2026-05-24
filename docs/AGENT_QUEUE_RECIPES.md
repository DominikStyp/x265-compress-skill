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

## Recipe 8 — "Notify me as each chunk finishes (Pusher / webhook)"

`on_chunk_done` runs a command after every chunk (success *and* failure). It's an
argv list; chunk context arrives via `X265_*` env vars, so the script takes no
arguments. Best-effort — a slow or failing hook never stalls or aborts the encode.

```json
{
  "defaults": {
    "crf": 22,
    "parallel": "auto",
    "resumable": true,
    "on_chunk_done": ["bash", "/home/me/notify.sh"]
  },
  "jobs": [
    {"input": "*.mkv"}
  ]
}
```

`/home/me/notify.sh`:

```bash
#!/usr/bin/env bash
curl -fsS -X POST https://api.example/notify \
  -d "text=${X265_CHUNK_INDEX}/${X265_CHUNK_TOTAL} ${X265_CHUNK_STATUS}: $(basename "$X265_SOURCE")"
```

Windows: use `["pwsh","-File","C:/tools/notify.ps1"]` and read `$env:X265_CHUNK_INDEX`, etc.

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

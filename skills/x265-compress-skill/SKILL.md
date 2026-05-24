---
name: x265-compress-skill
description: Use whenever the user wants to compress, shrink, re-encode, or transcode a video file with ffmpeg / x265 / HEVC. Analyses the source with ffprobe, picks CRF and motion/sharpness-tuned x265 parameters based on the source codec and bits-per-pixel, then writes a `.bat` next to the source that does the encode (no audio re-encoding, output is always `.mkv`). Trigger on phrases like "compress this video", "make this mp4 smaller", "x265 encode", "shrink this file", "convert to h265", "transcode to mkv", even when the user does not say "ffmpeg" explicitly.
---

# x265-compress-skill

Generate a one-shot script (`.bat` on Windows, `.sh` on macOS/Linux) that compresses a video with x265 (CPU), tuned for sharpness and motion, with no audio re-encoding. **All decision logic lives in the bundled `compress.py`** — this skill exists to invoke it, interpret its output, and let Claude tweak the script afterwards for unusual sources.

OS-specific behaviour (priority class, suspend syscall, script extension, shell to run) is auto-detected at import time by `platform_compat/`. The agent doesn't need to know which OS it's on.

## Agent playbook — single prompt to queue running

Use this decision tree when the user invokes the skill. Detailed schemas and edge cases are linked.

### 0. First-use dependency check (~2 s)

Before invoking `compress.py`, run `ffmpeg -version` to confirm ffmpeg is available. The plugin's `SessionStart` hook (`hooks/check_ffmpeg.py`) auto-installs ffmpeg via `brew` (macOS) or `winget --scope user` (Windows) on session start, but the hook runs async and Linux installs need sudo — so always verify before encoding.

If `ffmpeg -version` fails:
- **macOS**: offer to run `brew install ffmpeg`
- **Windows**: offer to run `winget install --id Gyan.FFmpeg --scope user`
- **Linux**: print `sudo apt install ffmpeg` (or `dnf install ffmpeg`); don't auto-elevate

After install (especially on Windows with `--scope user`) the user may need to restart their shell/Claude Code session for ffmpeg to be on PATH.

### 1. The user has ONE video → single-file mode

```
python compress.py "<full path to source>" [--crf N] [--preset name] [--anime] [--grain]
```

Read the JSON the script prints. Report to the user, in short prose:
- the chosen CRF + preset and why (cite source codec + bits-per-pixel),
- the expected size reduction range,
- the path to the generated `.bat` / `.sh`,
- any `warnings` from the JSON, verbatim.

Then stop. **Do not run the script yourself** — the encode can take hours; the user runs it manually.

### 2. The user has MULTIPLE videos (a folder, a list, a glob) → queue mode

a. Decide the queue file's location: **the directory the videos live in**, not this repo. Name it `queue.json`.

b. Write `queue.json` with reasonable defaults. The minimal "compress everything in this folder" template:

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

c. Run it:
```
python3 run_queue.py /path/to/queue.json
```
(Use `python` on Windows.)

d. Read the exit code + the markdown report it writes to `<videos>/.tmp/report_<timestamp>.md`. Report results to the user.

See [`docs/AGENT_QUEUE_RECIPES.md`](../../docs/AGENT_QUEUE_RECIPES.md) for paste-ready recipes (anime, grain, mobile-compat, archival, etc.) and the full per-job key schema + exit-code mapping.

### 3. Interpreting the user's wording

| User says... | Tweak |
|---|---|
| "smaller, quality matters less" | `crf` +2 above auto |
| "visually lossless" / "archival quality" | `crf: 17`, `preset: "slower"` |
| "anime" / "cartoon" / "line-art" | `anime: true` |
| "preserve grain" / "film look" | `grain: true` |
| "play on my old TV / phone" | `eight_bit: true` |
| "save power, file is for playback" | mention NVENC (`-c:v hevc_nvenc -preset p7 -rc vbr -cq 24`) as an alternative; this skill still defaults to libx265 |
| "resume" / "continue" | just re-run the same `.bat`/`.sh` or `queue.json` — resumable picks up automatically |

### 4. After the encode runs

The user (or `run_queue.py`) writes a markdown report under `<videos>/.tmp/`:
- `report_<YYYY-MM-DD_HH-MM-SS>.md` — only this run (one file per invocation)
- `<queue_stem>_report.md` — incremental, accumulates jobs across every run

VMAF/PSNR/SSIM scores land in those reports. If a row shows `vmaf_mean < 90`, something went wrong — see the "fps-mismatch gotcha" section below.

If any job exits `pre-flight-failed` (code 6), `awaiting-chunk-fix` (code 7), or `failed-exit-4` (verify), follow [Manual recovery recipes](#manual-recovery-recipes).

## Action

Run the bundled script with the source video as the argument:

When Claude Code invokes the skill, the plugin root is exposed as
`${CLAUDE_PLUGIN_ROOT}`. Prefer that env var so the call is location-
agnostic; fall back to the canonical install path otherwise.

```powershell
# Windows — env var primary, plugins-dir fallback
python "${env:CLAUDE_PLUGIN_ROOT}\compress.py" "<full path to source video>"
# or:
python "$env:USERPROFILE\.claude\skills\x265-compress-skill\compress.py" "<source>"
```

```bash
# macOS / Linux
python3 "${CLAUDE_PLUGIN_ROOT}/compress.py" "<full path to source video>"
# or:
python3 "$HOME/.claude/skills/x265-compress-skill/compress.py" "<source>"
```

The script:

1. Runs `ffprobe` on the input and reads codec / resolution / fps / bitrate / pix-fmt / color metadata.
2. Picks a CRF from the source bits-per-pixel (see [`references/x265-tuning.md`](../../references/x265-tuning.md) for the bands).
3. Picks a preset from encode work (duration × resolution × fps): `slower` < 5e9 work units, `slow` default, `medium` > 5e11.
4. Adds HDR metadata to `-x265-params` if the source is HDR (bt2020 / PQ / HLG).
5. Writes `compress_<basename>.bat` **next to the source file**.
6. Prints a JSON summary to stdout.

The generated `.bat` pipes ffmpeg's `-progress -` output through the bundled `progress.py`, so when the user runs it they see a live percentage bar with ETA instead of ffmpeg's default raw status line:

```
[#############-----------] 53.4 %  16:21 / 30:46  fps=  15  speed=0.27x  ETA  1:12:30
```

Read the JSON. Report to the user, in short prose:

- the chosen CRF + preset and why (cite the source codec + bits-per-pixel from the summary),
- the expected size reduction range,
- the path to the `.bat`,
- any `warnings` from the JSON, verbatim.

Then stop. **Do not run the `.bat` yourself** — the encode can take hours; the user runs it manually.

## When to override the defaults

The script picks reasonable defaults but they are not always right. Re-run it with flags, or hand-edit the generated `.bat`, when the user's request implies one of these cases:

| User signal | Action |
|---|---|
| "smaller, quality matters less" / "I don't care if it loses a bit" | `--crf` +2 or +3 above the auto value |
| "visually lossless" / "absolutely no quality loss" | `--crf 17` and `--preset slower` |
| anime / cartoon / line-art content | `--anime` (uses x265 `:tune=animation`) |
| obvious film grain to preserve | `--grain` (uses x265 `:tune=grain`) |
| target device that chokes on 10-bit HEVC (older TVs, some phones) | `--eight-bit` |
| user wants to pause/resume across reboots, or laptop encoding overnight | `--resumable` (see below) |
| dark / shadow-heavy content | leave defaults — `aq-mode=3` already handles this |
| very fast-motion (sports, action) | leave defaults — `bframes=8`, `me=star`, `merange=57` already cover it |

## Resumable mode

`--resumable` generates a `.bat` that splits the source losslessly into chunks, encodes each chunk independently, and concatenates them losslessly at the end. If the encode is killed (or the laptop reboots), re-running the same `.bat` picks up at the first unencoded chunk — all state lives on disk in `<source-dir>/.compress_<basename>/`.

**Chunk count**: by default `--segments 10` (each chunk is ~10 % of source length). For a 30-min source that's ~3-min chunks. The chunk count is the main trade-off knob:

| `--segments N` | Per-chunk length (for 30-min source) | Output size penalty vs single-pass | Max progress lost on kill |
|---|---|---|---|
| 5 | ~6 min | ~0.5-1 % | 20 % |
| **10** (default) | **~3 min** | **~1-2 %** | **10 %** |
| 30 (≈ old 60-sec default) | ~1 min | ~3-5 % | ~3 % |
| 60 | ~30 sec | ~5-8 % | ~1.5 % |

The penalty scales roughly linearly with chunk count, because each chunk pays a near-constant overhead (lookahead reset + RC restart + extra I-frame). Pick the smallest chunk count whose max-loss-on-kill you can tolerate.

If you'd rather think in absolute seconds (every file chunks the same way regardless of length), pass `--segment-seconds N` instead — it overrides `--segments`.

**For short pauses without reboot** (lunch break, need the CPU for something else), `--resumable` is overkill. Instead, suspend the single-pass ffmpeg process with Sysinternals `pssuspend ffmpeg` and resume with `pssuspend -r ffmpeg`. The process keeps its full RAM state. Doesn't survive reboot.

### Encoding order: middle-first (default)

Chunks are **not** encoded front-to-back. The median chunk runs first, then the rest expand outward symmetrically. For N=10 the encode order is `[5, 6, 4, 7, 3, 8, 2, 9, 1, 10]`; for N=11 it's `[6, 7, 5, 8, 4, 9, 3, 10, 2, 11, 1]`. The pattern is `[mid, mid+1, mid-1, mid+2, mid-2, ...]` where `mid = (N-1)//2` (0-indexed) — so N=2 picks chunk 1 first (the only consistent rule when there's no true middle).

Why: the size projection used by `--max-size-percent` is dominated by the byte-rate of the first ~5% of work. Intro chunks tend to be quieter than the source average, so front-first ordering under-estimates the projection and the threshold guard fires LATE — sometimes after the encode has already finished above-budget. For the kind of clips this tool targets, motion-heavy material clusters near the middle, so kicking the median chunk off first puts a representative (often higher-bitrate) sample in the projection cache early. Alternating outward then spreads the next data points across the file, so by the time the 5% gate opens the projection is honest rather than biased toward one segment.

Effects on the rest of the pipeline:
- **Concat is unaffected**: chunks are stored on disk with their original-position filenames (`enc_src_0005.mkv` etc.) and concat reads them in filename order. Encoding order only changes what runs when.
- **Resume is unaffected**: chunks already on disk are filtered out before the order is applied to the work queue. If chunks 5/6/4 are done and you re-run, the remaining encode order is `[7, 3, 8, 2, 9, 1, 10]` — the rest of the middle-out walk.
- **Per-chunk labels stay 1-indexed by original position**: serial-mode prints `Chunk 5/10`, `Chunk 6/10`, `Chunk 4/10`, ...; the parallel display events log shows `+ src_0005.mkv: done in ...`. Counts in the live block (`Chunks: K / N done`) are physical-count, not position.
- **The 5% projection gate stays at 5%**. With middle-first the bias flips direction (projection is now slightly *over*-estimated early, instead of under-estimated), which is the safer side: a borderline encode aborts a touch sooner instead of finishing above budget. If you see false-positive aborts on consistent-bitrate sources, raise the gate inside `encode_resumable.py`'s `_compute_projection()`.

## Parallel chunks (requires --resumable)

`--parallel N` encodes N chunks concurrently inside a `--resumable` run. Each x265 instance is given a `pools=<cpu_count / N>` limit so they don't fight for the same cores. On a 32-core CPU, `--parallel 4` gives each ffmpeg ~8 cores — that's enough for x265 at low-to-mid resolutions, where a single instance can't keep all 32 cores busy anyway (WPP is bounded by CTU rows).

**Default is `--parallel auto`**: compress.py derives the chunk-count from probed source height, since the x265 WPP thread ceiling scales with `ceil(height/64)` CTU rows and there's no point stacking more workers than the single-instance ceiling leaves room for. Built-in mapping:

| Source height | `auto` value | Why |
|---|---|---|
| ≥2160p (4K+) | **1** | RAM is the bottleneck at 4K, not cores: each 10-bit x265 instance holds several GB of lookahead + reference frames, so stacking even 2 tips a 32 GB box into paging |
| ≥1080p / 1440p | **4** | ~17 rows per instance; 4 stacked fits a 32-core box cleanly |
| ≥720p | **6** | ~12 rows; one instance leaves significant cores idle |
| <720p (480p / SD) | **8** | ~8 rows; needs heavy stacking to saturate CPU |

> **Apple Silicon:** the 4K → `1` cap matters even more on unified memory (shared with the GPU and the rest of the system). An M-series base config is often 16–24 GB, so a single 10-bit 4K x265 instance already claims a real share — don't raise `--parallel` for 4K on a Mac unless you have 32 GB+ and have watched the memory headroom.

Override per-file with `--parallel N` (or `"parallel": N` in a queue JSON). In queue JSON you may also pass `"parallel": "auto"` explicitly, or omit the key entirely (same effect).

Trade-offs:
- **Speedup** is close to linear up to the "single-instance ceiling" — i.e. until you're properly using all cores.
- **Disk I/O** scales linearly too. On HDDs, watch for I/O contention.
- **Per-chunk progress bars are rendered live** via ANSI cursor manipulation — N stacked bars (one per slot), a `─` horizontal rule, then a 3-line overall-file summary block, refreshed ~2 Hz. Completion events (`+ src_NNNN.mkv: done in 60.1s`) scroll above the live block. Requires Windows 10+ cmd.exe (which supports virtual-terminal sequences — enabled automatically via `SetConsoleMode` in `encode_resumable.py`). Layout:

  - **Slot rows (per-chunk)**: `[slot N] src_NNNN.mkv [bar] XX.X%  fps=...  speed=...  elapsed H:MM:SS  ETA H:MM:SS` — one row per parallel slot. The trailing `elapsed`/`ETA` are **per-chunk** (wall time since *this* chunk's encode started, with any paused windows excluded; ETA = elapsed × (100 − pct) / pct, shown as `paused` when the slot is suspended and `—` before progress is measurable).
  - **Box rule** (`──────...`) — a U+2500 horizontal line below the slot block, dividing per-chunk metrics from overall-file metrics. Helps the eye instantly distinguish "this chunk is at 30 %" from "the whole file is at 30 %".
  - **Overall block (file-level)** — three `---` framed lines, each speaking about the entire encode:
    - `Chunks:   K / N done (XX.X%)   elapsed H:MM:SS   ETA H:MM:SS` — discrete, only ticks when a chunk physically lands on disk. ETA is derived from chunk-completion rate (lumpy until several chunks finish).
    - `Progress: [bar] XX.X%   (K.KK of N chunks)   ETA H:MM:SS` — **smooth** full-file progress that factors in the partial progress of in-flight chunks. With `--parallel 1` this bar moves continuously instead of jumping every chunk; the "K.KK of N chunks" hint translates the smooth fraction back into chunk-equivalents (e.g. `2.50 of 10 chunks` = chunks 1+2 done, chunk 3 at 50%). ETA from this bar is steadier than the chunks-line ETA because it updates at every render tick rather than waiting for the next chunk to finish.
    - `Size: [bar with `|` marker] XX.X%   proj XXX.X / src YYY.Y MB   thr ZZ.Z%` — live projection of final output size as a percentage of source. The `|` inside the bar marks the `--max-size-percent` threshold; the bar fill is the *current projected output size*. Bar is green while under threshold, **red** the moment projection crosses it (you'll see one red frame before `check_threshold()` aborts the encode on the same tick). For the first ~5% of overall progress the bar shows `(estimating...)` because byte-rate is dominated by initial-keyframe overhead and a projection that early would be misleading.
- **Interactive pause/resume per slot** (htop-style). While the encode is running, keypresses control individual slots without killing the run:

  | Key | Effect |
  |---|---|
  | ↑ / ↓ | Move the `>` focus cursor between slot rows |
  | Space | Toggle pause on the focused slot |
  | 1–9 | Toggle slot N directly (the digit matches the on-screen `[slot N]` label) |
  | `0` | Toggle slot 10 (only relevant if `--parallel 10`) |
  | `r` | Resume every paused slot at once |
  | `f` | Toggle **finish after current chunk**: finish in-flight chunks, start no new ones, then stop the encode AND the queue (resumable — re-run to continue) |
  | `?` / `h` | Print the key list as an event in the live log |

  Paused slots show `[PAUSED]` next to the chunk row and burn zero CPU. Suspension uses Win32 `NtSuspendProcess` on the ffmpeg PID directly, so x265's RAM state (lookahead, reference frames, RC) is preserved bit-for-bit — when you resume, ffmpeg picks up at the exact same frame with no quality loss. Works in single-file mode and inside `run_queue.py` (the queue runner inherits stdin so keypresses flow through cmd → bat → python).
  **Finish after the current chunk (`f`)** — a graceful stop: in-flight chunks complete, no new chunks start, and the encode exits resumably (exit code 8 = `stopped-by-user`), which also halts the queue. Re-run to pick up from the next chunk. Headless or serial runs (no keyboard) trigger the same stop by creating a `FINISH` file in the workdir — `encode_resumable.py` prints the exact path at startup; it's consumed when honored.
- **Hard-kill safety**: every ffmpeg child is assigned to a Windows **Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. If you `taskkill /F` the script (or hit a BSOD, or anything else that bypasses normal shutdown), the kernel automatically TerminateProcess's every assigned ffmpeg the moment the parent's handle to the job closes. No more "I killed Python and now there are 4 suspended ffmpeg orphans I have to hunt down". On a clean exit (Ctrl+C / normal completion / threshold abort), the same protection applies — the `finally` block also calls `resume_all()` first so paused chunks aren't left half-flushed, but the Job Object is the canonical safety net. You'll see `Process protection: ffmpeg children auto-killed if this script dies (Job Object).` near the start of each parallel encode.
- **Quality is identical** to serial resumable mode — the encoder params per chunk are the same; only the scheduling changes.

## CPU priority: ffmpeg always runs IDLE

Every ffmpeg child spawned by the skill (chunk encodes, lossless split, concat, full decode-check, libvmaf measurement) runs at low CPU priority. The exact mechanism depends on OS, dispatched via `platform_compat.low_priority_popen_kwargs()`:

- **Windows**: `subprocess.IDLE_PRIORITY_CLASS` creationflag — scheduler priority 4, lower than `start /low` (BELOW_NORMAL = 6).
- **macOS / Linux**: `nice -n 19` prepended to the ffmpeg cmd. Same effect — the scheduler only runs the encode when no other process wants CPU. (Wraps the cmd rather than using `preexec_fn`, which Python's docs flag as unsafe in multi-threaded contexts.)

In practice this means:

- The browser, editor, video player, terminal, etc. always preempt ffmpeg the moment they need CPU. You can keep working through a long encode with no perceptible slowdown to interactive apps, even when every core is at 100 %.
- Wall-clock encode time is essentially unchanged on a quiet system (low priority still gets all available cycles when nothing else wants them). It only stretches when you're actively using the machine — exactly when you want it to back off.
- ffprobe and `progress.py` stay at NORMAL priority because they're sub-second / I/O-bound; touching their priority adds noise to the diff but no behavior change.
- You'll see `CPU priority: ffmpeg runs at low priority — foreground apps (browser, editor) always preempt encode.` near the start of each encode (both serial and parallel paths).

If for some reason you need ffmpeg back at NORMAL (e.g. dedicated encoding box, nothing else running), edit `wrap_cmd_for_low_priority()` in `platform_compat/_posix.py` to return the cmd unchanged (or `low_priority_popen_kwargs()` on Windows to return `{}`) — those two helpers are the only knobs, and they cover every spawn site.

## Size-budget guard (`--max-size-percent N`)

For the case where you only want to keep encodes that actually save meaningful disk space, add `--max-size-percent N` (requires `--resumable`). The chunked encoder watches the running bytes-per-source-second ratio; once ≥5 % of overall progress is reached, it projects the final output size by multiplying that ratio by the total source duration. If the projection exceeds `N %` of the source size, it:

1. Terminates all in-flight ffmpeg processes.
2. Prints a yellow bold `ENCODING STOPPED!` block with the projected size and the percentage of source it represents.
3. Exits with code 3 (distinct from 1 = real failure).
4. Leaves the already-encoded chunks on disk so you can inspect or resume.

Recovery options after a threshold-stop:
- Re-run the same `.bat` *without* the flag → keeps the existing chunks, encodes the rest, accepts the over-budget result.
- Re-run with a higher CRF (e.g. `--crf 22`) → smaller per-frame output, but you'll need to **delete the workdir first** (chunk outputs from CRF 18 can't be mixed with chunks from CRF 22 — the concat would still work, but the user's intent of "consistent quality" would be violated).
- Delete the workdir and accept that this source isn't a good compression candidate (already efficient).

Typical thresholds: `80` means "only useful if I save ≥20 %". `60` is aggressive — many high-quality x264 sources won't make it under that with `--crf 18`.

## Three-layer source-corruption defense

Some upstream `.mp4` / `.mkv` files have bitstream corruption in a specific time range (broken NAL units, dropped frames, mis-muxed packets, AAC channel-element mismatches). The decoder silently conceals these errors and passes garbage to x265. With `me=star + merange=57 + subme=4 + bframes=8 + b-adapt=2`, x265 hits a worst-case WPP dependency chain on the concealed frames — symptom is one ffmpeg burning ~1 core for hours while producing only a few MB of bitstream. The skill defends in three layers:

### Layer 1 — pre-flight scan (default on, opt-out with `--no-pre-flight-scan`) + optional `--auto-patch-source`

Before any encoding, `pre_flight_scan` walks the source in `segment_seconds`-sized windows with `ffmpeg -ss N -t seg_sec -xerror -f null -`. Each window that returns non-zero is recorded with its time range + first error lines. If ANY window is bad, encode_resumable.py exits **code 6** (`pre-flight-failed`); the queue runner moves to the next file. **No chunking, no encode CPU wasted.**

Result is cached in `<source>.preflight.json` keyed on `(file_size, file_mtime)`. Re-runs / queue restarts skip the scan unless the source bytes changed. The cache also accelerates the case where Claude rebuilds the queue after a reboot.

#### `--auto-patch-source` (opt-in): automate the surgical patch

When the source is **h264** and pre-flight fails, `--auto-patch-source` triggers the surgical-patch logic in `encode_modules/source_patcher.py` instead of exiting code 6 straight away:

1. Refine each coarse bad window (10s → 1s decode-walks) to second-resolution bad zones.
2. Probe I-frames around each bad zone; snap to GOP boundaries.
3. Loss-budget gate: bail if cumulative re-encoded duration > `--max-patch-seconds` (default `10`).
4. Build a multi-part concat: stream-copy clean parts as MPEG-TS, re-encode bad GOPs at `libx264 -preset veryfast -crf 14` (ffmpeg's decoder fills broken refs via error concealment).
5. Concat via concat *demuxer* (NOT protocol — protocol stitches blindly and produces backwards DTS at the seam) into `<workdir>/source-patched.mp4`.
6. Re-run pre-flight on the patched file (uncached). If it passes, the rest of the pipeline continues with the patched file as its source (output keeps the original name with `.mkv` suffix). The patched intermediate lives in the workdir and is cleaned up with everything else on success.

Fall-back to `pre-flight-failed` (code 6) if: source isn't h264, loss budget exceeded, GOP boundaries can't be found, or any ffmpeg step fails. The history record carries `auto_patch_attempted=true` + `auto_patch_declined`/`auto_patch_post_scan` so the JSONL log shows what happened.

**Original source is never modified** — patched copy is a new file inside the workdir; source-guard protects the original from any rename/unlink elsewhere in the pipeline.

Validated against the Emily PATCHED 2026-05-22 case: same source, same broken-ref window (35.82-35.98 s), same GOP-aligned patch (34.04-36.04 s), same final pre-flight-passes outcome — but now produced automatically inside the encode run.

In a queue.json defaults block:
```json
{
  "defaults": { "auto_patch_source": true, "max_patch_seconds": 10 },
  ...
}
```

### Layer 2 — per-chunk choke detection + skip (default on)

When the encoder is mid-encode and a slot's encode-speed ratio (`out_time / wall_seconds`) stays below `--choke-threshold-speed` (default `0.05x`) past `--choke-grace-seconds` (default `300s`), `check_choke` fires:

1. **ONLY that slot's ffmpeg is terminated.** Other slots keep encoding undisturbed.
2. The worker thread sees rc != 0, looks the chunk up in `display.choked_chunks`, records the skip, and pulls the next chunk from the work queue.
3. After all workers finish, for each skipped chunk: `analyze_chunk_errors()` walks the source to capture the bitstream errors; `write_needs_fix_sidecar()` drops `enc_<chunk>.needs_fix.json` next to the source chunk with the chunk index, time range, choke speed/wall, original params, decode-error samples, and the expected output path.
4. If any chunks were skipped, **concat is suppressed** — the partially-encoded file is NOT merged. Resumable encoded chunks stay on disk untouched.
5. encode_resumable.py exits **code 7** (`awaiting-chunk-fix`); the queue runner moves to the next file.

To produce the missing chunk(s) later: read the needs_fix.json sidecar, encode a replacement (manually with different params, via a follow-up Claude conversation, or by replacing the source segment with a clean copy), drop the resulting `enc_<chunk>.mkv` into the workdir, re-run the .bat. The resumable loop detects all chunks present and proceeds to concat → verify → quality measurement → done.

### Layer 2b — `--auto-fix-choke` (opt-in)

When set, the worker tries ONE relaxed-params re-encode after a choke: `me=umh + subme=3 + merange=32` (the three knobs that triggered the WPP pathology on the original chunk-0008 incident). The result is then decode-walked end-to-end before being accepted — a relaxed-encode chunk that itself fails verification is quarantined as `enc_<chunk>.autofix-broken-<ts>.mkv` (never deleted) and the chunk is marked needs-fix as if the auto-fix never happened. Successful auto-fix removes the chunk from the skipped list; if every choke gets auto-fixed, the encode completes normally with no exit 7.

### Layer 3 — output verification (always on)

After concat, `verify_output` walks the merged file end-to-end with `-xerror`. If it fails, the broken merged file is renamed **in place** to `damaged_<original-name>.<ext>` (NOT hidden under `.tmp/` — the user sees it sitting next to where the clean output would have been). Workdir is preserved untouched — encoded chunks are sacred. Encode_resumable.py exits code 4; queue continues to next file.

#### Auto DTS-fix remux (verify failure recovery, no re-encode)

Some chunked-concat outputs trip verify on **non-monotonic DTS** warnings only — every chunk decodes clean in isolation, the merged file plays in every real player, but ffmpeg's strict `-xerror` flags scattered duplicate DTS timestamps from the concat process. Before declaring the file damaged, `_run_encode_verify_loop` checks `is_dts_only_verify_failure(problems)` and — if true — runs an **MPEG-TS roundtrip remux** on the merged output: `mkv → mpegts (annex-b BSF) → mkv (+genpts +avoid_negative_ts make_zero)`. The roundtrip drops the bad container metadata and ffmpeg regenerates clean monotonic timestamps. **Encoded video bytes are not touched.** Re-verify runs once; if it now passes, the encode is accepted as clean.

Cost: ~5 min remux on a 2.5 GB output (vs hours of re-encode that wouldn't help — the chunks are already correct, the failure is in the merged container metadata). The pre-fix output is preserved as `<stem>.pre-dts-fix-<ts><suffix>` (never unlinked, per the never-delete rule). Validated end-to-end on the Emily-PATCHED-2026-05-22 case: 19 DTS collisions across chunk 0000 of the merged output → 0 errors after roundtrip.

See `encode_modules/dts_recovery.py` for the implementation. The hook only fires when **every** verify problem mentions the DTS marker — genuine structural failures (truncated chunks, codec issues, audio sync) skip the remux and go straight to the damaged-file diagnostic.

### Hard rule: encoded chunks are sacred

`enc_src_NNNN.mkv` files are NEVER deleted by any code path. Duration-bad chunks get **quarantined** (renamed to `enc_src_NNNN.broken-<timestamp>.mkv`) rather than unlinked, so the resumable loop sees them as missing and re-encodes — but the originals remain for inspection. Only `cleanup(workdir)` after a fully-verified successful encode wipes the workdir. See `feedback_never_delete_encoded_chunks.md` for the incident behind this rule.

## Manual recovery recipes

Cases the automated layers above can't (or shouldn't) handle. These are operator-driven workflows — the user runs them by hand when the encoder bails out on a specific kind of failure.

### Surgical h264 patch (localized broken refs)

**Symptom**: `pre-flight-failed` with bad-window samples like `reference picture missing during reorder` / `Missing reference picture`. The source's h264 has localized corruption (typically a few seconds of broken inter-frame references); the rest of the file is fine.

**Recipe** (used to recover Emily Belle 4k, 2026-05-22):

1. **Localize the bad zone**. The pre-flight reports a bad window in chunk-sized seconds (default 173 s). Decode-walk that window in 10 s sub-windows, then in 1 s sub-windows around the hit, to find the exact second(s) where the errors live. Use `ffmpeg -v error -hide_banner -xerror -ss N -i src -t W -f null -` for each window; lines on stderr count the errors.

2. **Find GOP boundaries around the bad zone**. Probe with `ffprobe -read_intervals N%M -select_streams v:0 -show_frames -show_entries frame=pts_time,key_frame,pict_type`. I-frames mark clean cut points — pick `keyframe_before_bad` and `keyframe_after_bad`.

3. **Build a 3-part concat** to MPEG-TS intermediates:
   - Part A: `ffmpeg -i src -t <keyframe_before_bad> -c copy -bsf:v h264_mp4toannexb -f mpegts part_a.ts`
   - Part B (re-encode the broken GOP using ffmpeg's error concealment): `ffmpeg -ss <keyframe_before_bad> -i src -t <bad_zone_duration> -c:v libx264 -preset veryfast -crf 14 -pix_fmt yuv420p -profile:v high -level 5.1 -c:a copy -bsf:v h264_mp4toannexb -f mpegts part_b.ts`
   - Part C: `ffmpeg -ss <keyframe_after_bad> -i src -c copy -bsf:v h264_mp4toannexb -f mpegts part_c.ts`

4. **Concat via concat *demuxer*, NOT concat protocol**. The protocol blindly stitches and produces DTS that walks backwards at the seam (we burned an iteration on this). The demuxer rebases timestamps monotonically:
   ```
   ffmpeg -f concat -safe 0 -i list.txt -c copy -bsf:a aac_adtstoasc -movflags +faststart <name> PATCHED.mp4
   ```
   where `list.txt` is `file 'part_a.ts'\nfile 'part_b.ts'\nfile 'part_c.ts'\n`.

5. **Re-pre-flight the patched file** to confirm the bad window is gone. Source is preserved — the patch produces a new file `<name> PATCHED.mp4` (never modifies the original).

6. **Queue the patched file** under its new name. The source-guard module (`encode_modules/source_guard.py`) prevents anything from accidentally deleting either file later in the pipeline.

Loss budget: x264 CRF 14 with `preset veryfast` on a ~2 s GOP is visually transparent. Total content drift from the original is the duration of one GOP (typically 2 s) re-encoded through ffmpeg's concealment + x264 — well within a 10-second tolerance.

### Cross-run chunk salvage (skip work the encoder already did)

**Symptom**: The encoder is about to encode a file from scratch, but you have a prior workdir (e.g., from before a pre-flight gate was added) with most chunks already encoded. The split chunks differ by a few KB of container metadata, but the decoded video frames are identical.

**Recipe** (used to skip ~10 hours of CPU on Emily PATCHED, 2026-05-22):

1. **Verify frame equivalence** between the prior workdir's `src_*.mkv` and the new workdir's `src_*.mkv` for ONE representative non-patched chunk:
   ```
   ffmpeg -t 1 -i prior/src_0005.mkv -an -vf "select='lte(n,49)'" -f framehash a.txt
   ffmpeg -t 1 -i new/src_0005.mkv  -an -vf "select='lte(n,49)'" -f framehash b.txt
   ```
   Compare the per-frame hashes (skip the `#tb` metadata lines). Identical hashes = the encoder will produce identical output bytes from either src — so prior `enc_*.mkv` can substitute. **DO NOT skip this check**: file sizes that look "close" (~50 KB diff per chunk is normal) are not proof of equivalence.

2. **Stop the running queue** by killing python PIDs (NEVER taskkill by image name — see `feedback_taskkill_by_pid_only.md`). The Windows Job Object attached to `encode_resumable.py` kills any ffmpeg children automatically.

3. **Copy the salvageable `enc_src_NNNN.mkv` files** from the prior workdir into the new one. Leave `enc_src_*.part.mkv` files alone in both places (forensic).

4. **Restart the queue**. The resumable loop sees `enc_*.mkv` for each salvaged chunk and skips it; only the genuinely-missing chunks (typically the ones that needed the patch fix + any never-reached ones) get encoded.

5. **Watch for DTS-collision-only verify failure** at concat time. Mixed-timebase chunks (different runs, different builds of ffmpeg) sometimes produce that artifact. The auto DTS-fix remux (above) usually clears it without re-encoding.

This salvage pattern is intentionally a **manual operator step**, not an automatic feature: detecting "the user's other workdir has chunks that match the current source" requires cross-workdir heuristics that would be brittle and surprising. Better that an operator runs the framehash equivalence check and decides than to have the encoder silently substitute chunks from somewhere else.

## Queue mode (`run_queue.py`)

For batch encoding of multiple files, write a JSON queue and feed it to the bundled `run_queue.py`. Each job inherits an optional `defaults` block and may override any compress.py flag per job.

```powershell
python "$env:USERPROFILE\.claude\skills\x265-compress-skill\run_queue.py" "C:\path\to\queue.json"
```

**Resuming + headless use.** A queue run resumes at two levels: re-running the same `queue.json` skips jobs whose output already exists, and within a job the chunk encoder picks up at the first missing chunk. So an interrupted run (SSH drop, reboot, `Ctrl+C`) just needs the same command again. For fire-and-forget over SSH, detach it — `nohup python3 run_queue.py queue.json > queue.log 2>&1 &`, or `tmux` / `systemd-run --user`. Under systemd use `KillMode=control-group` so the unit reaps ffmpeg children cleanly.

### Queue JSON schema

Two shapes are accepted. Flat list (no shared defaults):

```json
[
  {"input": "C:/videos/clip1.mp4", "crf": 18, "parallel": 4},
  {"input": "C:/videos/clip2.mp4", "max_size_percent": 80}
]
```

Object with `defaults` (recommended — set things once, override per file):

```json
{
  "defaults": {
    "parallel": 4,
    "segments": 10,
    "max_size_percent": 80,
    "resumable": true
  },
  "jobs": [
    {"input": "clip1.mp4"},
    {"input": "clip2.mp4", "crf": 20, "preset": "slower"},
    {"input": "season-01/*.mkv"}
  ]
}
```

### Per-job keys

| JSON key | Maps to | Notes |
|---|---|---|
| `input` (required) | positional arg | Absolute or relative to queue.json; supports `*`/`?` globs (expanded to one job per match) |
| `crf` | `--crf` | int |
| `preset` | `--preset` | string |
| `segments` | `--segments` | int |
| `segment_seconds` | `--segment-seconds` | int |
| `parallel` | `--parallel` | int or `"auto"` — omit to let compress.py auto-pick from source height (4K → 1, 1080p → 4, 720p → 6, lower → 8) |
| `max_size_percent` | `--max-size-percent` | float |
| `anime` | `--anime` | bool |
| `grain` | `--grain` | bool |
| `eight_bit` | `--eight-bit` | bool |
| `resumable` | `--resumable` | bool, **default `true` in queue mode** |

Unknown keys produce a warning and are dropped — gives you a typo safety net.

### Launcher (`run_queue.bat`)

**Whenever you write a `queue.json`, also write a `run_queue.bat` next to it** so the user can start the encode by double-clicking instead of typing the python invocation. Use this exact template, saved UTF-8 without BOM and with **CRLF (Windows) line endings** — cmd.exe mis-parses LF-only .bat files, dropping leading characters per line (`chcp`→`cp`, `title`→`tle`):

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "%USERPROFILE%\.claude\skills\x265-compress-skill\run_queue.py" "%~dp0queue.json"
set QUEUE_RC=%errorlevel%
pause
exit /b %QUEUE_RC%
```

Notes:
- `cd /d "%~dp0"` makes the bat work regardless of where it's launched from (cmd cwd, Explorer double-click, scheduler, etc.).
- `chcp 65001` is required if any input filename in the queue contains non-ASCII characters.
- Exit code is captured **before** `pause` and re-raised with `exit /b` **after** it, so outer runners (CI, schedulers, parent .bats) see the real result. `pause` itself always returns 0 and would mask failure.
- If the queue file is named something other than `queue.json`, mirror that name in both the launcher's filename and the path it passes to `run_queue.py`.
- The `\.claude\skills\x265-compress-skill\` segment must match where the skill is actually installed. It may instead live under `\.claude\plugins\x265-compress-skill\` (the installer default) — point the launcher at whichever directory contains `run_queue.py`. `%CLAUDE_PLUGIN_ROOT%` can't be used here because it isn't set when the user double-clicks the .bat.

### Behaviour

- **Sequential** — one job at a time. Each job already uses `--parallel` chunks, so running queue jobs concurrently would just thrash the CPU.
- **Live-reload between jobs** — the runner re-reads `queue.json` before picking each next job. Edit the file while encoding to add new jobs, remove pending ones, reorder them, or change a pending job's settings (CRF, threshold, etc.) and the change takes effect at the next job boundary — no Ctrl+C / restart needed. Jobs already attempted in this session (ok / failed / aborted / skipped) are never re-attempted even if they still appear in the file. Edits to a job currently *being encoded* are too late; the encoder is already committed. When a reload notices a changed mtime, you'll see `Queue updated: reloaded queue.json (now N job(s)).` between jobs. JSON parse errors during a mid-edit save are tolerated with one automatic retry; if both attempts fail, the runner exits gracefully instead of crashing.
- **Skip existing outputs** by default (override with `--no-skip-existing`).
- **Continue on per-job failure** by default (override with `--stop-on-failure`).
- **Threshold-aborts never stop the queue** regardless of `--stop-on-failure` — the whole point of the size guard is "this one isn't worth keeping; move on".
- **"Finish after current chunk" DOES stop the queue** — pressing `f` (or creating a `FINISH` file) is an explicit "stop everything" request: the current file stops after its in-flight chunks finish, no further jobs start, and the queue exits (`stopped-by-user`, exit 8). Resumable — re-run the queue to continue.
- **Final summary table** lists each job and its status: `ok`, `skipped-exists`, `skipped-not-found`, `stopped-threshold`, `pre-flight-failed`, `awaiting-chunk-fix`, `chunk-choked`, `stopped-by-user`, `failed-gen`, `failed-parse`, `failed-exit-<N>`. Queue exit code: `0` = all clean, `1` = a real failure (`failed-*`), `2` = a job needs attention (`stopped-threshold`, `awaiting-chunk-fix`, `skipped-not-found`, `pre-flight-failed`, `chunk-choked`) — a hard failure outranks needs-attention. Pass `--json-status <path>` for an NDJSON per-job stream (machine-readable; stdout stays human-readable).
- **Resumable inside a job** still works — if the queue is killed mid-job, re-run it and the in-progress job resumes from its last completed chunk; preceding jobs are skipped because their output already exists.

## Markdown reports

Every successful encode generates a markdown report listing inputs, sizes, and gains:

| Mode | Report path(s) | Contents |
|---|---|---|
| Single-file (`compress.py`) | `<output_dir>/<basename>.report.md` | One row, this file only |
| Queue (`run_queue.py`) — **per-run** | `<queue_dir>/.tmp/report_<YYYY-MM-DD_HH_MM_SS>.md` | Only this run's jobs. Never overwritten — one file per `run_queue.py` invocation. |
| Queue (`run_queue.py`) — **incremental** | `<queue_dir>/.tmp/<queue_basename>_report.md` | Accumulates every job from every run. Persistence is via a sidecar JSON (`<queue_basename>_report.history.json`) alongside the markdown. |

The queue runner **passes `--no-report` to each per-job `compress.py` invocation**, so a queue run produces *only* the queue-level reports — no per-file noise in the output directory.

### Resetting the incremental report

The incremental markdown is the user-visible source of truth for "should I keep history?". To start fresh: **delete the incremental `.md` file**. On the next run, the helper sees the markdown is gone, discards the sidecar JSON as stale (deletes it too), and the run's jobs become the new starting point. The per-run timestamped reports are independent and stay on disk regardless.

Side-effects to be aware of:

- A job retried at a different CRF / preset / threshold appears in the incremental report as **two rows** (chronological): the first attempt with its original status, then the retry with `ok`. Summary aggregates only `status == "ok"` rows, so threshold-aborts and failures never pollute the totals.
- The sidecar JSON stores raw job dicts, not parsed markdown — so manual edits to the `.md` won't break the next run's aggregation. If you want to surgically remove a row, edit the sidecar JSON, then delete the `.md` so it gets re-rendered from the trimmed history.
- If you delete only the sidecar JSON (and not the markdown), the next run's helper will see no prior history and rewrite the markdown with just the new run — effectively the same outcome as deleting the markdown.

Report shape (both modes):

```markdown
# Encoding Report: ...

_Generated: 2026-05-16 21:40:55_

## Summary
- **Jobs**: N total · K successful · M skipped / aborted / failed
- **Total input**: ... MB
- **Total output**: ... MB
- **Saved**: ... MB (XX.X%)
- **Total wall time**: H:MM:SS
- **Mean VMAF**: 97.50 (across N files; worst 94.20 on `<name>`)   ← if quality scores present
- **Quality methods used**: chunks: K, full: M                      ← if quality scores present

## Files
| # | File | Status | Input | Output | Saved | Saved % | CRF | Preset | Time | VMAF | vmaf_lo | PSNR | SSIM | Grade | Method |
```

The Status column uses the same vocabulary as the queue runner: `ok`, `skipped-exists`, `skipped-not-found`, `stopped-threshold`, `pre-flight-failed`, `awaiting-chunk-fix`, `chunk-choked`, `stopped-by-user`, `failed-gen`, `failed-parse`, `failed-exit-<N>`. Skipped/aborted/failed rows still appear in the table (with `—` for Output/Saved) so you can see *every* attempt. Quality columns (VMAF, vmaf_lo, PSNR, SSIM, Grade, Method) appear only when at least one job has a quality sidecar.

To opt out of the per-file report on a direct `compress.py` run, pass `--no-report`.

## Encoding history log (`encoding_history.jsonl`)

Every encode appends one JSONL record to `C:\_MOJE\other\CUTTED\encoding_history.jsonl` (override via the `CLAUDE_ENCODING_HISTORY_PATH` env var). Records survive across queue runs, batch boundaries, and queue file changes — the file is purely an append-only audit trail you can feed to an LLM or analyze with pandas/jq.

Each record is one JSON object on one line. Schema (v1):

```json
{
  "schema_version": 1,
  "timestamp_start_utc": "2026-05-21T12:30:45Z",
  "timestamp_end_utc":   "2026-05-21T15:18:22Z",
  "wall_seconds": 10057.3,
  "status": "ok" | "stopped-threshold" | "pre-flight-failed" | "awaiting-chunk-fix" | "chunk-choked" | "chunk-failed" | "verify-failed" | "in_progress",
  "abort_reason": null,
  "input": {
    "path": ..., "name": ..., "size_bytes": ...,
    "codec": "h264", "width": 3840, "height": 2160,
    "resolution": "3840x2160",
    "fps": "50/1", "fps_decimal": 50.0,
    "bitrate_bps": 14378000, "bpp": 0.034669,
    "duration_s": 1032.288, "pix_fmt": "yuv420p", "container": "..."
  },
  "output": {"path": ..., "size_bytes": ..., "container": "matroska"},
  "reduction": {"bytes_saved": ..., "pct_saved": 20.7},
  "settings": {"crf": 25, "preset": "slow", "pix_fmt": "yuv420p10le",
               "x265_params": "psy-rd=2.0:...", "parallel": 1,
               "segment_seconds": 104, "max_size_percent": 85.0, "max_output_bytes": ...},
  "chunks": [
    {"index": 0, "encode_order_position": 8,
     "src_name": "src_0000.mkv", "src_size_bytes": ..., "src_duration_s": 103.2,
     "enc_name": "enc_src_0000.mkv", "enc_size_bytes": ...,
     "elapsed_s": 845.3, "speed_factor": 0.122},
    ... (one per chunk)
  ],
  "quality": {"vmaf_mean": 95.42, "vmaf_min": 92.18, "vmaf_harmonic_mean": ...,
              "psnr_y_mean": 44.5, "ssim_mean": 0.987,
              "frames_evaluated": ..., "method": "chunks" | "full"},
  "environment": {"platform": "win32", "python_version": "3.12.x",
                  "cpu_count": 32, "machine": "host-name", "processor": "...",
                  "ffmpeg_version_line": "..."}
}
```

Key field: `input.bpp` — bits per pixel, the single most predictive metric for the CRF/VMAF tradeoff. Records on the same BPP class are directly comparable for tuning the compression decision matrix.

Reading the log for analysis:

```python
import json
from pathlib import Path
records = [json.loads(l) for l in
           Path(r"C:\_MOJE\other\CUTTED\encoding_history.jsonl")
           .read_text(encoding="utf-8").splitlines() if l.strip()]
# → list[dict], analyse however you like
```

Behavior:
- **Success path**: record is finalized synchronously at end of `main()` (output size, reduction, per-chunk records, quality scores) and appended in one write.
- **Threshold abort** (exit 3): `status="stopped-threshold"`, the abort_reason string is captured, partial chunk_elapsed map is included. Flushed by an `atexit` hook before sys.exit.
- **Chunk failures** (exit 1): `status="chunk-failed"`, `failed_chunks` lists names.
- **Verify failures** (exit 4): `status="verify-failed"`, `verify_problems` lists messages.
- **Hard kill / crash**: `atexit` hook flushes whatever was filled in (input + settings + chunks done so far). Status remains `"in_progress"` so analyses can filter on it.
- **History write failures never fail the encode** — they print one warning line and continue. Disk full, file locked, etc.

## Quality measurement (VMAF / PSNR / SSIM)

After every successful resumable encode, `encode_resumable.py` runs libvmaf comparing target to source and writes the result to a JSON sidecar at `<output_dir>/.tmp/<basename>.quality.json`. The aggregate queue report reads these sidecars and surfaces the scores in the Method column.

### Auto-mode (default)

`--vmaf-mode auto` (the default) picks between two strategies based on what's on disk:

| State | Method used | Speed | Why |
|---|---|---|---|
| Chunk workdir still present (paired `src_NNNN.mkv` + `enc_src_NNNN.mkv` files) | **chunks** — sample N chunk pairs (default 3, picks 1-indexed `[2,6,10]` for 10 chunks) | ~30s | Each chunk pair is naturally aligned. Bypasses the fps-mismatch trap (below). |
| Workdir cleaned up | **full** — walk the merged output end-to-end | Source duration / 1.6x | The only option once chunks are gone. Requires the fps fix to be correct. |

`--vmaf-mode chunks` and `--vmaf-mode full` force the respective path. `--no-quality-check` skips the measurement entirely.

### The fps-mismatch gotcha (critical)

`ffmpeg -f concat -c copy` builds outputs whose container fps reports as e.g. `18649/373` (≈49.997) even when the source is exactly `50/1` and the frame counts match. ffmpeg's filter graph framesync pairs frames between libvmaf's two inputs **by PTS** — with mismatched fps it silently drops/duplicates one stream to stay in sync, pairing misaligned frames at certain instants. The result is a catastrophic VMAF score (we observed 17.36 on a 43-min file whose actual quality is VMAF 97.5) that has **nothing to do with encode quality**.

The fix lives inside `_quality_check_run`: probe the source's `r_frame_rate` and pass `-r <src_fps>` as an INPUT option before **both** `-i` flags. As an input option, `-r` tells ffmpeg "ignore stored PTSs, generate new ones assuming constant fps" — with the same value on both sides, frame *i* of source pairs with frame *i* of target deterministically. Validated end-to-end: a 29-min file scored 97.59 full-file vs 97.53 per-chunk (Δ -0.085, well within sampling variance).

Applying `-r` to **both** inputs matters: even nominally-clean source PTSs have sub-frame jitter that accumulates over thousands of frames. Dst-only normalization gives 97 at 30s but craters to 32 at 5 min.

### Sidecar caching — re-runs are opt-in

Each successful measurement writes a sidecar JSON containing all scores plus `method`. Both `encode_resumable.py` (after fresh encodes) and the standalone quality-sweep script honor an existing sidecar: if it's there with a `vmaf_mean`, the file is skipped. To force a re-measure for specific files, **delete their sidecars** in `.tmp/<basename>.quality.json` — the sweep / encoder will refill them next run.

Sidecar fields:
```json
{
  "method": "chunks" | "full",
  "vmaf_mean":  97.45,
  "vmaf_min":   94.17,
  "vmaf_harmonic_mean": ...,
  "psnr_y_mean": 45.12,
  "ssim_mean":   0.99485,
  "frames_evaluated": 13996,
  "sampling_mode": "3 of 10 chunks (1-indexed: [2, 6, 10])"   // or "full file"
}
```

### Interpretation grades

| VMAF | Grade | Meaning |
|------|-------|---------|
| ≥95  | TRANSPARENT | Indistinguishable from source |
| ≥90  | EXCELLENT   | Very close to source, sub-perceptual artifacts |
| ≥80  | GOOD        | Minor compression artifacts on close inspection |
| ≥70  | ACCEPTABLE  | Visible artifacts but watchable |
| ≥50  | DEGRADED    | Noticeable quality loss |
| <50  | POOR        | Significant degradation — encode likely over-compressed (or, if you see this on a full-file VMAF of a concat'd target, suspect the fps-mismatch bug FIRST) |

### CLI flags (encode_resumable.py / via compress.py)

| Flag | Default | Notes |
|---|---|---|
| `--vmaf-mode {auto,chunks,full}` | `auto` | Auto picks chunks→full based on workdir state |
| `--vmaf-subsample N` | `10` | Score every Nth frame. 1 = every frame (slow + precise), 10 = ~10× faster, stable aggregate |
| `--vmaf-chunks N` | `3` | Number of chunk pairs to sample in `chunks` mode. Spread between 10–90 % of file |
| `--no-quality-check` | off | Skip measurement entirely |

**Segment mode** (`_quality_check(mode="segments")`) exists but is fragile on chunked-concat outputs because of keyframe-layout mismatch at the seek target — a separate failure mode from the fps issue, not fixed by the `-r` normalization. Auto-mode never picks segments; chunks→full is the only path that's been validated to give honest scores.

## Quality at chunk boundaries

When `--resumable` chunks are stitched together, the concat phase uses `-c copy` (bit-perfect remux, no re-encoding). The result is:

- **No visible artifacts** at chunk boundaries — every chunk starts at a keyframe (segment muxer guarantees this), so the decoder has a clean reference. No flash, no pop, no stutter.
- **No audio/video sync drift** — `-reset_timestamps 1` during split + concat demuxer timestamp rebuild keep PTS/DTS monotonic.
- **Audio is byte-identical** to source (always `-c copy`).
- **Measurable differences vs single-pass** are limited to: (1) the ~2-5 % size penalty already documented above, from rate-control reset per chunk; (2) a tiny VMAF dip in the first ~25-40 frames of each chunk because x265's lookahead can't see across boundaries. Both are invisible to the eye and well within industry-standard tolerances (Netflix, YouTube, AWS MediaConvert all chunk-encode for the same reasons we do).

For anything unusual that the flags don't cover, **edit the `.bat` directly after generation**. The structure is intentionally simple: one `ffmpeg` invocation with line-continuations. [`references/x265-tuning.md`](../../references/x265-tuning.md) explains what every parameter does so you can change them with intent rather than guessing.

## Preconditions

- `ffmpeg` and `ffprobe` must be on PATH. The script checks for `ffprobe` and exits with a clear error if missing.
- Python 3.9+ (uses `from __future__ import annotations` + the standard library only — no extra installs). The generated `.bat` also calls `python -u progress.py`, so Python must be on PATH at encode time too.

## Notes

- Output extension is always `.mkv`. If the source is already `.mkv`, output is `<name>.x265.mkv` to avoid overwriting.
- `-map 0 -map -0:d` copies every stream (video, all audio tracks, subtitles, chapters) but drops MP4 data streams, which commonly fail when muxed into Matroska.
- Audio is `-c:a copy` always. The user's requirement is explicit: no audio re-encoding ever.
- Subtitles are `-c:s copy`. If that fails on a specific file (rare — e.g. `mov_text` from MP4 into MKV), edit the `.bat` to add `-c:s srt`.
- The `.bat` is written as UTF-8 **without** BOM, and starts with `chcp 65001 >nul` so non-ASCII paths work in `cmd.exe`.

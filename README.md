# x265-compress-skill

Resumable x265 chunked video encoder. Splits a source video into
keyframe-aligned chunks, encodes each independently with libx265, and
concatenates losslessly. Survives kills, reboots, and laptop sleep —
re-running picks up at the first unencoded chunk.

Built as a [Claude Code](https://claude.com/claude-code) skill but the
scripts run standalone from any shell.

## Features

- **Resumable** — kill mid-encode, re-run, picks up at the first missing chunk
- **Parallel chunks** — N concurrent ffmpegs with shared x265 thread pool,
  htop-style live display, per-slot pause/resume (`Space`, `1`-`9`, `r`)
- **Finish after current chunk** — press `f` (or drop a `FINISH` file, for
  headless) to stop gracefully once in-flight chunks complete; halts the queue,
  fully resumable
- **Chunk-finished hook** — run a command after each chunk (e.g. a Pusher /
  webhook progress ping); set per file or in the queue `defaults`, context via
  `X265_*` env vars, best-effort so it never derails the encode
- **Source corruption guard** — pre-flight bitstream scan plus an opt-in
  surgical patch that re-encodes JUST the broken h264 GOPs
- **Choke detection** — per-chunk progress watchdog frees a stuck slot
  without aborting the rest of the file
- **Quality scoring** — VMAF + PSNR + SSIM after every successful encode
- **Threshold guard** — abort early if projected output won't save enough
  disk space (`--max-size-percent N`)
- **DTS-collision auto-recovery** — MPEG-TS roundtrip remux clears the
  one verify failure that doesn't actually mean the video is broken
- **Queue runner** — JSON queue of jobs, sequential, live-reloadable
  mid-flight (edit `queue.json`, changes apply at the next job boundary)

## Requirements

- **Windows 10+** *or* **macOS 11+** (Linux works too but isn't a primary
  target — the POSIX backend covers both). OS-specific behaviour is
  auto-detected; see [`platform_compat/`](platform_compat/__init__.py).
- Python 3.9+ (standard library only — no `pip install` step)
- `ffmpeg` and `ffprobe` on PATH (and `python3` on PATH on POSIX)

## Install

Two install paths, depending on what you've already got set up.

### Option 1 — `/plugin install` (Claude Code native)

Inside Claude Code, run:

```
/plugin install github:DominikStyp/x265-compress-skill
```

Claude Code clones the repo, reads `.claude-plugin/plugin.json`, and
loads the bundled skill. After the install, **restart Claude Code**
once — the bundled `SessionStart` hook (`hooks/check_ffmpeg.py`) fires
on next session start and:

- **macOS**: auto-installs ffmpeg via `brew install ffmpeg` if missing
- **Windows**: auto-installs ffmpeg via `winget install --scope user Gyan.FFmpeg` if missing
- **Linux**: prints `sudo apt install ffmpeg` (or dnf/pacman) — sudo
  isn't auto-elevated from plugin hooks for safety

If ffmpeg's already installed: the hook is a fast no-op (silent).

To uninstall: `/plugin uninstall x265-compress-skill`.

### Option 2 — `curl | bash` (full setup, including ffmpeg)

Use this on a fresh machine where ffmpeg isn't installed yet. The
installer auto-clones the repo into your Claude Code plugins directory,
checks Python, offers to install ffmpeg via the system package manager
(`brew` / `apt` / `dnf` / `pacman` / `winget`) if it's missing, then
runs an import smoke test.

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.ps1 | iex
```

Non-interactive (CI / automation):

```bash
INSTALL_YES=1 curl -fsSL https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.sh | bash
```

```powershell
$env:INSTALL_YES=1; irm https://raw.githubusercontent.com/DominikStyp/x265-compress-skill/master/install.ps1 | iex
```

Override the install location with `SKILL_DIR=/some/where` (POSIX) or
`$env:SKILL_DIR="..."` (PowerShell). Default is
`~/.claude/plugins/x265-compress-skill/`.

> **Install location.** Two layouts are supported:
> - `~/.claude/plugins/x265-compress-skill/` — the installer default (plugin install).
> - `~/.claude/skills/x265-compress-skill/` — when dropped in as a user skill.
>
> Both work identically. The path examples below use `plugins/`; if your
> copy lives under `skills/`, substitute that segment everywhere — including
> in any generated `run_queue.bat` / invocation, which must point at the
> directory the skill actually lives in.

After install, restart Claude Code (or run `/skills` to refresh). The
skill activates automatically via the `name:` / `description:`
frontmatter in `SKILL.md` on prompts like *"compress this video"*,
*"shrink this mp4"*, *"compress everything in this folder"*.

Re-running the installer inside an existing clone is supported — it
detects the clone via a sibling `.claude-plugin/plugin.json`, skips the
clone step, and just verifies deps. Useful after `git pull` to confirm
nothing broke.

Standalone use without Claude Code is also fine — clone anywhere, run
the installer, then invoke `compress.py` / `run_queue.py` directly.

## Upgrading

For users who installed via `/plugin install`, Claude Code's normal
plugin-update mechanism handles the pull.

For users who installed via the curl/irm one-liner:

```bash
# macOS / Linux  (use skills/ instead of plugins/ if installed there)
cd ~/.claude/plugins/x265-compress-skill && git pull && bash install.sh
```

```powershell
# Windows  (use skills\ instead of plugins\ if installed there)
Set-Location "$env:USERPROFILE\.claude\plugins\x265-compress-skill"
git pull
& .\install.ps1 -Yes
```

`install.sh` / `install.ps1` detect the existing clone and just verify
deps (no overwrite). See [`CHANGELOG.md`](CHANGELOG.md) for what
changed between versions; `version.txt` is written at install time with
the commit SHA so a bug report can identify which build you're running.

## Data locations

The plugin directory holds **only the code** — your encoding data lives
next to your video files:

| Data | Location |
|---|---|
| Encoder script (`.bat` / `.sh`) | `<video_folder>/.tmp/compress_<name>.bat` |
| Chunked workdir | `<video_folder>/.tmp/.compress_<name>/` |
| Pre-flight scan cache | `<source_video>.preflight.json` (next to source) |
| Quality scores sidecar | `<video_folder>/.tmp/<output>.quality.json` |
| Per-encode markdown report | `<video_folder>/.tmp/<output>.report.md` |
| Queue aggregate report | `<queue_folder>/.tmp/<queue_stem>_report.md` |
| Encoding history JSONL | `<video_folder>/encoding_history.jsonl` |

`rm -rf ~/.claude/plugins/x265-compress-skill/` (or `.../skills/...` if
installed there) removes ONLY the code — your sidecars, reports, history,
and any in-progress chunked workdirs stay with the videos. A fresh install
picks up where you left off.

## Quick start

**Simplest form** — auto-picks CRF, preset, parallelism, and pixel format;
every flag is optional. Just point it at a file:

```bash
python compress.py "input.mp4"
```

That writes a one-shot encoder script next to the source; run it to encode.
Add `--resumable` for long encodes you might interrupt (chunked + re-runnable).
The examples below show the common overrides.

Single file (Windows):

```powershell
python compress.py "C:\videos\input.mp4" --resumable --crf 22 --preset slow
```

Single file (macOS / Linux):

```bash
python3 compress.py /Users/you/videos/input.mp4 --resumable --crf 22 --preset slow
```

Writes `.tmp/compress_input.bat` (Windows) or `.tmp/compress_input.sh`
(POSIX) next to the source. Run that script to start the encode.

Queue mode (same JSON, same flags, both OSes):

```bash
python3 run_queue.py queue.json
```

Minimal `queue.json`:

```json
{
  "defaults": {
    "crf": 22,
    "parallel": 4,
    "max_size_percent": 80,
    "auto_patch_source": true
  },
  "jobs": [
    {"input": "file1.mp4"},
    {"input": "file2.mp4", "crf": 20}
  ]
}
```

## Documentation

- [**SKILL.md**](SKILL.md) — comprehensive reference: every flag, every
  recovery path, queue schema, quality-measurement modes, operational
  notes. Starts with an **Agent playbook** for AI agents picking up the
  skill.
- [**docs/AGENT_QUEUE_RECIPES.md**](docs/AGENT_QUEUE_RECIPES.md) —
  paste-ready `queue.json` templates for the common single-prompt
  scenarios (anime, grain, mobile-compat, archival, etc.) + the full
  per-job key schema + exit-code mapping.
- [**references/x265-tuning.md**](references/x265-tuning.md) — the
  sharpness/motion-tuned x265 parameter set the skill applies by default.

## Layout

| Path | Purpose |
|---|---|
| `compress.py` | CLI entry: ffprobe → decide CRF/preset/x265-params → write the encoder script |
| `encode_resumable.py` | Pipeline: pre-flight → split → encode → verify → quality → cleanup |
| `run_queue.py` | Sequential queue runner with mid-flight live-reload |
| `platform_compat/` | OS abstraction (Win32 ↔ POSIX). Single point where the codebase decides what shell, what priority class, what suspend syscall to use |
| `compress_modules/` | Source probe, plan composition, `.bat`/`.sh` script writer |
| `encode_modules/` | Chunk worker, serial + parallel loops, live display, choke detection, VMAF, history, source guard |
| `queue_modules/` | Job schema, queue I/O, per-job runner |
| `references/` | x265 parameter rationale |

## Platform support

The OS-specific surface is intentionally contained to one package
(`platform_compat/`). Adding a new platform = drop in a new
`platform_compat/_<name>.py` providing the same set of names; nothing
else in the codebase changes.

| Concern | Windows backend | POSIX backend (macOS / Linux) |
|---|---|---|
| Subprocess priority | `IDLE_PRIORITY_CLASS` creationflag | `nice -n 19` cmd wrapper (thread-safe alt to preexec_fn) |
| Suspend / resume | `NtSuspendProcess` / `NtResumeProcess` | `SIGSTOP` / `SIGCONT` |
| ANSI escape support | `SetConsoleMode` VT processing | Native (no-op) |
| Kill children with parent | Win32 Job Object (`KILL_ON_JOB_CLOSE`) | Process group + `atexit`/`SIGTERM` handler |
| Single-char keyboard | `msvcrt.getch` | `termios` cbreak + `select.select` |
| Encoder script | `.bat` (cmd.exe + `chcp 65001`) | `.sh` (bash + UTF-8 native) |

Known gap on POSIX: a `kill -9` of the parent Python skips signal handlers
and will orphan in-flight ffmpeg children. Win32 Job Objects survive that;
POSIX has no exact equivalent. Doesn't affect Ctrl+C, normal exits, or
SIGTERM — only the hard-kill case. Running under systemd closes even that
gap: put the encode in a unit with `KillMode=control-group` and the cgroup
reaps any orphaned ffmpeg when the service stops.

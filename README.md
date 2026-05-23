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

- Windows 10+ (uses Win32 Job Objects, NtSuspendProcess, cmd.exe — not
  portable to Linux/Mac without rework)
- Python 3.9+ (standard library only — no `pip install` step)
- `ffmpeg` and `ffprobe` on PATH

## Quick start

Single file:

```powershell
python compress.py "C:\videos\input.mp4" --resumable --crf 22 --preset slow
```

Writes `.tmp\compress_input.bat` next to the source. Run that `.bat` to
start the encode.

Queue mode:

```powershell
python run_queue.py queue.json
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

[**SKILL.md**](SKILL.md) is the comprehensive reference — every flag,
every recovery path, the x265 parameter rationale, queue schema details,
quality-measurement modes, and operational notes.

[**references/x265-tuning.md**](references/x265-tuning.md) explains the
sharpness/motion-tuned x265 parameter set the skill applies by default.

## Layout

| Path | Purpose |
|---|---|
| `compress.py` | CLI entry: ffprobe → decide CRF/preset/x265-params → write the encoder .bat |
| `encode_resumable.py` | Pipeline: pre-flight → split → encode → verify → quality → cleanup |
| `run_queue.py` | Sequential queue runner with mid-flight live-reload |
| `compress_modules/` | Source probe, plan composition, .bat templates |
| `encode_modules/` | Chunk worker, serial + parallel loops, live display, choke detection, VMAF, history, source guard |
| `queue_modules/` | Job schema, queue I/O, per-job runner |
| `references/` | x265 parameter rationale |

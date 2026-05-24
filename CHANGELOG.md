# Changelog

All notable changes to this skill are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.2.0] — 2026-05-24

Robustness pass from a multi-perspective audit: correctness fixes in the
parallel encoder, headless/CI-friendly output, richer queue observability,
and broader installer coverage. The simple default path (`python
compress.py video.mp4`) is unchanged.

### Added
- **Headless / non-tty output.** The parallel live display and the serial
  progress bar now detect when stdout isn't a terminal (piped to a log,
  `nohup`, `systemd`, CI) and emit plain, log-friendly lines instead of
  ANSI cursor-control / carriage-return spam. The interactive terminal
  experience is unchanged.
- **`run_queue.py --json-status <path>`** — append one NDJSON record per
  finished job (`{input, status, output, input_bytes, output_bytes,
  elapsed_seconds, vmaf_mean}`) for fleet monitoring; `tail -f`-able, and
  kept off stdout so the human summary stays clean.
- **Generated-script preflight guard.** The `.bat` / `.sh` now verify
  ffmpeg and Python are on PATH and fail with a readable message instead
  of a vanishing window or `'python' is not recognized`.
- **Archival-confidence reporting.** The success summary states the source
  is left untouched and, when VMAF ≥ 95, that it's safe to delete the
  original by hand; the VMAF worst-frame now carries its own quality grade
  (the floor matters most when deciding to delete an original).
- **First unit-test suite** under `tests/` (stdlib `unittest`, no new
  dependencies) covering the fixes and logic changes below.
- **Docs**: the zero-flag `python compress.py video.mp4` simplest form
  (README); CRF-17 archival rationale and an "avoid VideoToolbox for
  archival" note (`references/x265-tuning.md`); `systemd
  KillMode=control-group` for orphan-free kills; an Apple-Silicon
  unified-memory note on the 4K parallel cap; queue resume + `nohup` /
  `tmux` / `systemd-run` guidance.

### Changed
- **`run_queue.py` aggregate exit code** now distinguishes three outcomes:
  `0` = all jobs clean, `1` = at least one real failure, `2` = no hard
  failure but a job needs attention (size-guard abort, awaiting chunk fix,
  missing input, corrupt source). Previously a threshold-abort reported `0`
  (looked clean) and everything else collapsed to `1`. **Scripts that
  branch on the queue exit code should review the new mapping** (documented
  in `docs/AGENT_QUEUE_RECIPES.md`).
- **`compress.py`** now warns (stderr) when an archival CRF (≤ 18) is paired
  with `--max-size-percent` — a combination that often stops the encode
  early (exit 3) before finishing.
- **Clearer error messages** for missing ffprobe (per-OS install command +
  "restart your shell"), a bad/missing input path (folder-vs-typo hint),
  and the size-guard abort (plain-English lead-in).
- **Installer distro coverage** (`install.sh`): Fedora/RHEL enables RPM
  Fusion (or falls back to `ffmpeg-free`) instead of failing `dnf install
  ffmpeg`; Alpine also installs `bash` (the generated `.sh` needs it) and
  surfaces the `community`-repo requirement; the dnf Python hint points at
  packages that actually exist. The SessionStart hook prints the RPM Fusion
  note too.
- **Generated `.sh`** resolves `python3`-or-`python` instead of hard-coding
  `python3`, so it runs on systems that ship only `python`.
- Size-projection / threshold logic extracted from `display.py` into
  `encode_modules/size_projection.py` (keeps `display.py` under the
  500-line module cap; behavior identical).

### Fixed
- **Choked-chunk partial encodes are no longer deleted.** `skipped_collector`
  now quarantines a choked chunk's `enc_*.part.mkv` (renames it aside)
  instead of unlinking it, matching the never-delete-encoded-bytes rule the
  rest of the pipeline already follows. (The old "delete so resume doesn't
  reuse it" rationale didn't hold — resume keys off the final `.mkv`, and
  the re-encode path already quarantines stale parts.)
- **Abort race in the parallel encoder.** An ffmpeg launched in the brief
  window after a threshold/choke abort fired could escape the terminate
  sweep and run to completion unsupervised. `register_proc` now refuses and
  terminates a process started once the abort flag is set.
- **A render-thread crash no longer disables the safety guards.** An
  unexpected exception in the live-render tick silently killed the
  size-guard (`check_threshold`) and choke-detector (`check_choke`) along
  with the display; the tick is now guarded and the error surfaced to the
  event log.
- **`HistoryRecorder.record_chunk_elapsed`** holds a lock while mutating
  shared state (defensive; consistent with the codebase's
  lock-all-shared-state discipline).

## [1.1.0] — 2026-05-23

### Added
- **macOS support** via new `platform_compat/` package — OS-specific
  behaviour auto-detected at import time. Win32 backend (Job Objects,
  NtSuspendProcess, msvcrt, IDLE_PRIORITY_CLASS) and POSIX backend
  (process groups + atexit/SIGTERM, SIGSTOP/SIGCONT, termios cbreak +
  select, `nice -n 19` cmd wrapper) share a unified API.
- **Linux support** for openSUSE (zypper) and Alpine (apk) in install.sh
  and the SessionStart hook. Conditional `sudo` prefix — works inside
  minimal containers as root.
- **Apple Silicon brew detection** — probes `/opt/homebrew/bin/brew` and
  `/usr/local/bin/brew` as fallbacks when `command -v brew` fails on a
  fresh M1+ install whose `~/.zprofile` hasn't been updated yet.
- **tmux/screen arrow-key support** — handles `ESC O A/B/C/D`
  (application-keypad mode) in addition to `ESC [ A/B/C/D` (cursor mode).
- **Self-bootstrapping installers** — `install.sh` / `install.ps1` detect
  whether they're inside a clone or piped from web (`curl | bash` /
  `irm | iex`) and auto-clone if missing. `INSTALL_YES=1` env var for
  non-interactive runs.
- **Claude Code plugin manifest** at `.claude-plugin/plugin.json`. Skill
  is now installable via `/plugin install github:DominikStyp/x265-compress-skill`.
- **SessionStart hook** (`hooks/check_ffmpeg.py`) auto-installs ffmpeg
  on first session start via `brew` (macOS) or `winget --scope user`
  (Windows). Linux prints the apt/dnf/pacman/zypper/apk command without
  auto-elevation.
- **Auto-patch broken h264 sources** (`--auto-patch-source` flag, +
  `auto_patch_source: true` in queue defaults). Localizes broken GOPs
  via decode-walk, re-encodes only those GOPs through ffmpeg's error
  concealment, concat-demuxes back into a `source-patched.mp4`.
- **DTS-collision auto-recovery** — MPEG-TS roundtrip when concat output
  has non-monotonic DTS but per-chunk decode walks all pass.
- **VMAF fps-mismatch fix** — `-r src_fps` on BOTH inputs to libvmaf
  prevents framesync from silently pairing misaligned frames on
  chunked-concat outputs.
- **Live encode rates** computed from sample deque rather than ffmpeg's
  cumulative averages — survives hibernation cleanly.
- **`docs/AGENT_QUEUE_RECIPES.md`** — 7 paste-ready `queue.json`
  templates for common scenarios (anime, grain, mobile, archival, …).
- **`_smoke_test.py`** — installer-invoked import graph check.
- **`version.txt`** — git commit SHA written at install time for
  bug-report traceability.

### Changed
- **`subprocess` spawn pattern**: every ffmpeg child now uses
  `wrap_cmd_for_low_priority(cmd)` + `**low_priority_popen_kwargs()`.
  POSIX: prepends `nice -n 19` + `start_new_session=True` (replaces the
  prior `preexec_fn` approach — Python flags preexec_fn as unsafe in
  multi-threaded contexts, which the encoder is).
- **`encoder.py`** decomposed: 479 LOC → 51 LOC dispatcher + 4 cohesive
  modules (`chunk_worker`, `encode_serial`, `encode_parallel`,
  `skipped_collector`).
- **`compress.py`** decomposed: 752 LOC → 168 LOC CLI orchestrator + 4
  modules under `compress_modules/`.
- **`encode_resumable.py`** decomposed: 468 LOC → 142 LOC main pipeline
  + `cli_args.py` + `preflight_decision.py` + `verify_loop.py`.
- **`run_queue.py`** decomposed: 447 LOC → 174 LOC + 3 modules under
  `queue_modules/`.
- **Concat now uses `-fflags +genpts`** — skips the DTS-recovery roundtrip
  on most files (~5 min saved per encode).
- **Grain tune** also overrides `me=umh:merange=32` — `tune=grain` works
  best with less-aggressive motion search.
- **`HistoryRecorder` class** replaces the module-level globals in
  `history_state.py`. Same public API (call sites unchanged); cleaner
  internal seam for future tests.
- **Decode-walk consolidation** — `_run_decode_walk` is now the single
  implementation behind `decode_walk_chunk`, `analyze_chunk_errors`,
  `_decode_check`, and `pre_flight._walk_one_window`. No more 4-way drift.
- **Em-dashes removed from `install.ps1`** — Windows PowerShell 5.1 reads
  UTF-8-no-BOM scripts as Windows-1252 by default, and the em-dash's
  multi-byte sequence includes a byte (0x94) that the string parser
  interprets as a closing curly quote.

### Fixed
- **`[ext.to]` glob bug** in `run_queue.py` — filenames containing `[`
  no longer trigger glob char-class expansion that silently drops the
  job. Literal-first resolution; falls back to glob only on misses.
- **Encoder worker survival** — exceptions in `_encode_one_chunk_with_display`
  or `try_auto_fix_chunk` no longer kill the daemon thread silently.
  Wrapped per-chunk try/except logs the error, marks the chunk as choked,
  and the worker continues on the next chunk.
- **Stale `.part` quarantine** — `chunk_worker.py` no longer unlinks
  pre-existing `.part.mkv` files; quarantines them as
  `.stale-pre-encode-<ts>.mkv` per the never-delete-encoded-chunks rule.

## [1.0.0] — 2026-05-22

### Added
- Initial commit: resumable x265 chunked encoder with VMAF scoring,
  choke detection, parallel chunks with htop-style pause/resume, queue
  runner with mid-flight live-reload, markdown reports, encoding history
  JSONL log. Windows-first.

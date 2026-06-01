# Changelog

All notable changes to this skill are recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.14.0] — 2026-06-01

### Changed
- **`examples/notify_pushbullet.py` is now a multi-event dispatcher.**
  The shipped recipe used to know only `chunk-done`, which silently
  mis-rendered every other event as `"Chunk-00-Done, 0/0 done (0.0%)"`
  when wired at `on_job_end` / `on_file_complete` / `on_queue_item_end`
  — so the headline notification ("⚠️ this source was stopped by the
  size guard at CRF N, projected X% of source") was impossible to get
  from the bundled script. `build_payload` now branches on
  `X265_HOOK_EVENT` and produces a distinct payload per event:

  | Event | Title shape |
  |---|---|
  | `chunk-done` | `Chunk-07-Done, 4/10 done (38.2%)` (unchanged) |
  | `job-end` + `ok` | `✅ DONE · CRF 21,22 · saved 35.05%` |
  | `job-end` + `stopped-threshold` | `⚠️ SIZE LIMIT · CRF 21` (body carries the projection banner) |
  | `job-end` + `stopped-threshold-crf-exhausted` | `⚠️ SIZE LIMIT (CRF maxed) · CRF 23,24,25,26,27,28` |
  | `job-end` + `pre-flight-failed` | `⛔ PRE-FLIGHT FAILED` |
  | `job-end` + any other failure | `⛔ <STATUS> · CRF 21` |
  | `file-complete` | `📦 FILE READY · 3/8 · CRF 22 · saved 30.50%` |
  | `queue-item-end` | `Queue [OK] · filename` (body is the full queue snapshot) |

  Back-compat: a missing or unknown `X265_HOOK_EVENT` falls through to
  the chunk-done branch, byte-identical to v1.13.x — so anyone wiring
  the script at only `on_chunk_done` sees zero change. Token / device
  still come from `PUSHBULLET_TOKEN` / `PUSHBULLET_DEVICE` env only and
  are never printed. Stdlib only.

### Added
- **`docs/AGENT_QUEUE_RECIPES.md` Recipe 9 — "Notify me when a source
  is stopped by the size guard (Pushbullet)".** Documents the
  size-stop notification (the motivating use case) plus the full
  one-file-per-every-hook wiring. Cross-linked to `retry_with_bigger_crf`
  / `crf_max` so readers connect the "CRF maxed" tag back to the
  escalation ceiling.
- **`docs/AGENT_QUEUE_RECIPES.md` per-job-keys schema table grew the
  rows it referenced but didn't define:** `on_job_end` (1.9.0),
  `on_file_complete` (1.10.0), `on_queue_item_end` (1.13.0),
  `retry_with_bigger_crf` / `crf_step` / `crf_max` (1.6.0+), `done_dir`
  (1.11.0). Recipe 9's cross-links now resolve.

### Tests
- 21 new cases on top of the existing 5 in `tests/test_example_notify_pushbullet.py`,
  covering every event branch (frozen title strings via `assertEqual`,
  not loose `assertIn`), the empty-CRF-chain degraded path on
  `job-end`, the missing-event back-compat path, and the
  `device_iden`-omission contract per event. Two reviewer subagents
  (PASS + PASS WITH NITS) flagged the missing schema-table rows and
  the loose title assertions; both addressed in this release.

## [1.13.0] — 2026-05-31

### Added
- **`on_queue_item_end` — queue-side status notification hook.** Fires
  from `run_queue.py` after every finished job (success *or* failure),
  with `X265_QUEUE_STATUS_SUMMARY` carrying a `\n`-joined snapshot of
  the whole queue marked `[OK]` / `[FAILED]` / `[..]` per job — so a
  single push gives the user a complete "where we are" picture without
  parsing `job_reports`. Configure under `defaults` (or per-job) in
  queue.json:
  ```json
  {"defaults": {"on_queue_item_end": ["python3", "/x/notify.py"]}}
  ```
  Skip rows (output exists, input missing, prior-run completion) do NOT
  fire — the spec is "fully processed OR failed", which a skip is
  neither. Same best-effort discipline as the in-encoder hooks (30 s
  timeout, NUL-argv defended, failures logged, queue never aborted).
  State sidecar is on disk BEFORE the hook fires (audit-trail-before-
  side-effects). Env vars passed: `X265_HOOK_EVENT=queue-item-end`,
  `X265_JOB_STATUS`, `X265_JOB_MARKER` (`[OK]` / `[FAILED]`),
  `X265_SOURCE`, `X265_OUTPUT`, `X265_QUEUE_STATUS_SUMMARY`. The marker
  classifier is fail-safe — any unknown status (e.g. one added by a
  future upstream change) maps to `[FAILED]` rather than silently
  looking healthy.

### Changed
- `run_queue.py` moved its `_run_status_inspector` helper into
  `queue_modules/status.py` as `run_inspector`, and its
  `_emit_json_status` helper into `queue_modules/queue_io.py` as
  `emit_status_record`. Behaviour-preserving; the old names are kept as
  re-exports on `run_queue` so `from run_queue import _emit_json_status`
  (and the existing test that uses it) keep working. The refactor was
  needed to keep `run_queue.py` under the project's 500-line cap after
  wiring in the new hook.

## [1.12.1] — 2026-05-29

### Fixed
Patch round from a multi-agent code audit. All findings actioned with
matching tests; full suite 347 green.

- **Queue `[i/n]` banner + `X265_QUEUE_TOTAL` / `_REMAINING` were always
  wrong on early jobs.** `_pick_next_job` walked the snapshot and returned
  at the FIRST unattempted job — `seen_inputs` only grew up to that point.
  On job 1 of a 5-job queue the banner read `[1/1]` and the file_complete
  hook reported `X265_QUEUE_TOTAL=1`, `X265_QUEUE_ITEMS_REMAINING=0`. The
  picker now walks the full snapshot before returning, so the denominator
  reflects the true queue size from the first pick.
- **Same-directory `done_dir` falsely recorded `moved_to_dir`.** When
  `done_dir` resolved to the source's own directory, `move_to_done_dir`
  returned `moved=False` (correct no-op) — but `_verify_move_outcome`
  stat'd both files at that dir, saw them, and recorded "moved" in the
  state sidecar. Next run printed misleading `SKIP — already done (moved
  to …)`. `_verify_move_outcome` now detects the same-dir case and
  returns no-move.
- **Hooks sidecar leaked into archive on `.mkv` sources.** `move_to_done_dir`
  passed `output.stem` to `_cleanup_sidecars`, but `script_writer` writes
  the sidecar as `<source.stem>.hooks.json`. For `movie.mkv` →
  `movie.x265.mkv`, `output.stem == "movie.x265"`, so the cleanup looked
  for `movie.x265.hooks.json` (which doesn't exist) and silently no-op'd.
  Fixed by passing `source.stem`; the hooks sidecar is now deleted as
  documented.
- **`X265_QUEUE_BYTES_*_SO_FAR` reset to 0 every queue relaunch.**
  `compute_queue_counters` excluded `skipped-done` from byte aggregation,
  even though the state sidecar carries faithful prior-run input/output
  byte measurements. The file_complete hook's "cumulative savings"
  contract was broken across sessions. `skipped-done` now counts in both
  `ITEMS_FINISHED` and the byte aggregates (deliberately distinct from
  `skipped-exists`, which has no provenance for its bytes).

### Changed
- `run_script` now accepts an optional `timeout` parameter (default
  unbounded, preserving long-encode behaviour). The docstring documents
  the deliberate exception to AGENTS.md's "every subprocess has a
  timeout" rule (which is scoped to probe-style calls). A `TimeoutExpired`
  is mapped to exit-code 124 (GNU coreutils convention) rather than
  raised, so the queue runner's normal status-mapping logic handles it.
- `delete_queue_state` docstring no longer claims "silently drop" —
  every OSError except `FileNotFoundError` propagates loudly. The
  `--reset-state` user explicitly asked for the action.
- Narrowed a broad `except Exception` in `_record_completion` to
  `(OSError, ValueError, KeyError, TypeError)` per AGENTS.md's "broad
  except only at daemon-thread guard seams" rule.

## [1.12.0] — 2026-05-29

### Added
- **`run_queue.py --status` — read-only queue inspector.** Prints a single
  consolidated table covering every job in `queue.json`, classified as
  DONE / PROCESSING / QUEUED / MISSING INPUT, with per-file sizes / CRF /
  wall time / savings + an aggregate "Totals (finished)" line. Reconciles
  four data sources (queue.json + encoding_history.jsonl + workdir state +
  on-disk outputs + queue_state sidecar) so the user doesn't have to
  `tail`/`jq`/`ls` four places. Strictly read-only — no encoding, no side
  effects. `--status-json` adds machine-readable output. Stale workdir
  detection: a workdir with `enc_src_*.mkv` but no `in_progress` history
  record is classified QUEUED with a `stale workdir from prior run` note,
  not falsely PROCESSING.

## [1.11.0] — 2026-05-29

### Added
- **`done_dir` — archive source+output after a successful encode.**
  Optional opt-in via `--done-dir PATH` on `compress.py` or `done_dir` in
  queue.json `defaults` / per job. Moves BOTH source `.mp4` AND output
  `.mkv` into the directory after `status == "ok"`. `~` expansion;
  relative paths resolve against the queue.json's directory (queue mode)
  or the source's directory (single-file). Cross-volume safe via
  `shutil.move`. Refuses to overwrite an existing destination, refuses to
  move into a workdir subtree (cleanup would eat the result), treats a
  same-directory done_dir as a no-op. Output is moved FIRST, source
  SECOND — a step-2 failure leaves source intact + output in done_dir
  (recoverable, no data loss).

- **Persistent queue state — `<queue_stem>.state.json` sidecar.**
  Records every `ok` completion (input_original / moved_to_dir /
  input_final / sizes / wall) so a re-run can silently skip with status
  `skipped-done` (new clean status, exit code stays 0). Without this, a
  `done_dir`-moved source would show up as `skipped-not-found` on the next
  run and bump the queue's exit code to 2. Schema versioned (v1), atomic
  write, degrades to empty on corruption / unknown schema. New
  `run_queue.py --reset-state` flag deletes the sidecar for a clean
  re-attempt. The state row's `moved_to_dir` is set ONLY when disk truth
  confirms both source and output arrived at the configured done_dir —
  protects against a refused/failed move silently poisoning the next
  run's skip logic. The queue runner's aggregate report also picks up
  the moved output's byte count via a stat-fallback to the done_dir
  location, so a successful move doesn't leave "0 bytes saved" in the
  summary.

## [1.10.0] — 2026-05-29

### Added
- **`on_file_complete` hook — fires success-only with queue-level counter
  context.** Companion to `on_job_end` (which fires for ANY terminal status):
  this one fires ONLY on `status == "ok"` AND ONLY when the final `.mkv` is
  on disk, so notification scripts can assume "if it fired, the file is
  ready". Fires from `HistoryRecorder.flush()` AFTER `on_job_end` (which
  itself runs after the JSONL audit row lands on disk) — so a slow
  file-complete celebration push can't delay the job-end alert.

  Per-file env: `X265_SOURCE`, `X265_OUTPUT`, `X265_SOURCE_BYTES`,
  `X265_OUTPUT_BYTES`, `X265_PCT_SAVED`, `X265_WALL_SECONDS`, `X265_CRF`,
  `X265_CRF_RETRY_CHAIN`, `X265_VMAF_MEAN`.

  Queue-level env (set by `run_queue.py` per spawn, inherits straight
  through cmd → bat → python → hook subprocess via standard env
  inheritance): `X265_QUEUE_INDEX`, `X265_QUEUE_TOTAL`,
  `X265_QUEUE_ITEMS_FINISHED` (inclusive of just-finished job),
  `X265_QUEUE_ITEMS_REMAINING/FAILED/STOPPED/SKIPPED`,
  `X265_QUEUE_BYTES_IN_SO_FAR`, `X265_QUEUE_BYTES_OUT_SO_FAR`,
  `X265_QUEUE_PCT_SAVED_SO_FAR`, `X265_QUEUE_WALL_SECONDS`. Per-run scope
  (resets every `run_queue.py` invocation). Single-file `compress.py` mode
  populates degraded `1/1/0/0` defaults so the same hook script works in
  both modes without a queue-presence branch.

  Configured via CLI `--on-file-complete`, queue.json `on_file_complete`
  key, or the `<stem>.hooks.json` sidecar (now carrying all three hook
  keys). Existing single-key v1.8.x sidecars still load on resume.

## [1.9.0] — 2026-05-29

### Added
- **`on_job_end` hook — fires once per job at the terminal-status chokepoint.**
  Companion to `on_chunk_done`, with rich detail aimed at notifications that
  need to distinguish "size guard tripped at CRF 21, projected 87% of source"
  from "actual crash". Configured the same way (queue.json key,
  `--on-job-end` CLI flag, sidecar JSON), requires `--resumable`, same
  best-effort no-raise discipline (30 s timeout; failures logged, never
  abort the encode). Fires from `HistoryRecorder.flush()` AFTER the JSONL
  audit record lands, so the audit trail is always on disk first.

  New env vars (always present — empty when not applicable):
  - `X265_JOB_STATUS` — the terminal status (`ok`, `stopped-threshold`,
    `stopped-threshold-crf-exhausted`, `chunk-failed`, `chunk-choked`,
    `awaiting-chunk-fix`, `verify-failed`, `pre-flight-failed`,
    `stopped-by-user`, generic failure)
  - `X265_JOB_STOP_REASON` — machine-readable reason; empty on `ok`
  - `X265_JOB_STOP_DETAIL` — human-readable banner text (e.g. the exact
    threshold projection line) or derived from the status's structured
    failure fields (`failed_chunks`, `verify_problems`, …)
  - `X265_CRF`, `X265_CRF_RETRY_CHAIN`
  - `X265_OUTPUT`, `X265_OUTPUT_BYTES_FINAL` (empty when status != `ok`)
  - `X265_OUTPUT_BYTES_PROJECTED`, `X265_OUTPUT_BYTES_THRESHOLD` (set on
    threshold abort)
  - `X265_SOURCE_BYTES` — the **user's original** source size, never the
    auto-patched copy (the project invariant: every user-facing surface
    reports the original `src`)
  - `X265_WALL_SECONDS`, `X265_PCT_SAVED`

  Hook attaches to the recorder BEFORE pre-flight, so pre-flight failures
  also notify. Existing v1.8.x `<stem>.hooks.json` sidecars (single-key)
  still load on resume.

## [1.8.2] — 2026-05-29

### Fixed
- **`concat.txt` fails with exit 254 on any path containing an apostrophe.**
  ffmpeg's concat demuxer parses `file '<path>'` as single-quoted, and inside
  the quotes an apostrophe must be written as `'\''` — the previous writers
  interpolated paths directly, so any workdir whose name carried `'`
  (possessives: `O'Brien`, `DP'd in Gym`, …) wrote a broken concat.txt and
  ffmpeg failed at finalize. The escaping logic now lives in a single shared
  helper (`encode_modules/concat_list.py`) that both `chunking.concat_chunks`
  and `source_patcher` route through; defensive regression tests prevent
  either writer from drifting back to a hand-rolled `f"file '{path}'"`
  template. Half-finished workdirs from v1.8.0/1.8.1 are picked up
  transparently — `concat.txt` is rewritten on every run, so the upgraded
  encoder just produces a correct file in place.

## [1.8.1] — 2026-05-28

### Fixed
- **`on_chunk_done` hook now reports honest overall progress.** Previously the
  only progress signal was `X265_CHUNK_INDEX` (the just-finished chunk's
  1-based positional id) and every shipped example computed
  `pct = index / total`. In parallel mode chunks finish out of order — chunk
  10 of 10 can complete before chunk 2 — so notifications reported "100%"
  with 9 chunks of actual work left. The contract now also emits four
  ground-truth fields derived from disk state and lazily-probed source-chunk
  durations (cached per-process):
  - `X265_CHUNKS_DONE` — count of `enc_*.mkv` files actually present
  - `X265_DURATION_DONE_SEC` — encoded source seconds so far
  - `X265_DURATION_TOTAL_SEC` — total source duration
  - `X265_PROGRESS_PERCENT` — overall progress 0–100, clamped

  `X265_CHUNK_INDEX` is preserved for back-compat but documented as
  not-progress. Existing notification scripts keep working; updating them
  to read `X265_PROGRESS_PERCENT` (or `X265_CHUNKS_DONE / X265_CHUNK_TOTAL`)
  makes parallel-mode reports honest. The shipped `examples/notify_pushbullet.py`
  and the recipes in `docs/AGENT_QUEUE_RECIPES.md` were updated to use the
  new fields. SKILL.md's contract table now also documents them.

## [1.8.0] — 2026-05-27

### Added
- **Progress bars for the concat and quality-check phases.** These two phases
  run after the live chunk-encode display tears down, and previously showed no
  live progress — concat was silent end to end, and the quality pass printed
  only a plain text percentage. Both now render the same
  `[#####-----] NN.N%  H:MM:SS / H:MM:SS` bar the encode phase uses:
  - **Concat (phase 3):** a bar driven by ffmpeg's `-progress` stream, scaled to
    the total duration.
  - **Quality (phase 4):** one *overall* bar across the sampled chunks (weighted
    by chunk duration, annotated `chunk i/N`) instead of a per-chunk flicker.

  Both adapt to their output: an in-place rewrite on a TTY, or a throttled line
  roughly every 10% on a pipe / headless log. No new dependencies — the renderer
  reuses the existing bar glyphs and time formatter.

### Changed
- Internal: a shared `encode_modules/progress_bar.py` (single-line bar renderer +
  ffmpeg `-progress` reader) now backs both phases, and `display_render.bar_fill`
  was extracted so the bar glyphs can't drift between the encode display and the
  new bars. The concat ffmpeg is now spawned via `Popen` and terminated on any
  error/abort (subprocess-discipline invariant). Behaviour-preserving for the
  live encode display.

## [1.7.3] — 2026-05-26

### Fixed
- **`on_chunk_done` notifications (e.g. Pushbullet) now show the original
  source filename even under `--auto-patch-source`.** When a broken source is
  rebuilt into `source-patched.mp4` (in the workdir), the hook's `X265_SOURCE`
  was bound to that patched working copy, so notifications read
  "source-patched.mp4" instead of the file you queued. The hook is now bound to
  the original input — completing the same original-vs-patched correction the
  v1.7.1 release made for the end-of-run summary and report. The encode still
  runs against the patched copy internally; only the user-facing source name
  changed. (The `encoding_history.jsonl` `input` block still records the
  encoded/patched file by design, because its size feeds the live size guard —
  unchanged here.)

## [1.7.2] — 2026-05-26

A code-quality pass (readability + SOLID/DRY) plus three robustness fixes that
tighten the project's own subprocess-discipline invariant. No user-facing
behaviour or CLI changes; all changes are internal and covered by 34 new tests
(full suite: 196).

### Fixed
- **The VMAF quality pass no longer leaks its ffmpeg+libvmaf child** if the
  read loop raises mid-stream or you Ctrl-C during a multi-minute measurement.
  The `Popen` is now reaped (terminate → wait → kill) in a `finally`, satisfying
  "every spawned ffmpeg must be terminated on abort/error."
- **Probe/build subprocess calls are now bounded by a timeout.** A wedged
  `ffprobe` (metadata probes in `probes.py`, `pre_flight`, `source_patcher`, and
  the upfront `compress.py` probe) or a stuck segment build/concat during
  `--auto-patch-source` previously hung the run forever; each now degrades to its
  documented safe default (or a clear error) instead. Timeouts are generous
  (120 s probe / 600 s build) so they can't false-trip on slow-but-working storage.
- **The `.quality.json` sidecar is written atomically** (temp + `os.replace`,
  matching `hook_config`/`pre_flight`), so a kill mid-write can't hand the queue
  runner a truncated file.

### Changed (internal — behaviour-preserving refactors)
- **One canonical `H:MM:SS` formatter** (`formatting.format_hms`); the two
  byte-identical copies now delegate to it (`progress`/`report` keep their
  deliberately different sentinels, now documented).
- **De-duplicated the ffprobe duration probe** — `source_patcher` uses the
  canonical `probes.probe_duration`; `history` dropped a redundant loop-local
  import.
- **Extracted the trailing-window sample scan** shared by the live-rate display
  and the choke detector into `encode_modules/_sample_window.py` (the window
  anchor stays caller-specific, so choke timing is unchanged).
- **Centralized the `.compress_<stem>` workdir name** in `plan.compress_workdir`,
  used by both the script generator and the queue's CRF-retry chunk locator —
  removing a silent drift risk that could make CRF-retry re-encode from scratch.
- **`SCRIPT_EXTENSION` now has a single source** (`plan.py`); promoted the
  cross-module `DTS_MARKER` to public; clarified the video-bitrate estimator;
  removed a dead import and a redundant one.

## [1.7.1] — 2026-05-26

### Fixed
- **A successful encode of an auto-patched source no longer reports a false
  `exit-1`.** With `--auto-patch-source`, the pre-flight step rebuilds a broken
  h264 source into a patched copy that lives **inside** the workdir, and
  `main()` was treating that copy as "the source" for the whole run. After the
  encode finished and `cleanup()` wiped the workdir (patched copy included), the
  end-of-run `print_summary()` called `src.stat()` on the now-deleted file and
  crashed with `FileNotFoundError` — turning a fully verified, successful encode
  into a reported failure. `main()` now keeps `src` pointing at the user's
  original input (which lives outside the workdir and is never deleted) for the
  post-cleanup reporters, while a separate `encode_src` feeds the pipeline,
  history, and quality measurement. The "Source untouched at: …" hint now also
  names the real original instead of the deleted temp copy.
- **`write_single_file_report` no longer re-`stat()`s the source for
  `input_bytes`.** It records the caller's pre-cleanup `source_bytes` instead,
  so the report can never touch a deleted workdir path and its `input_bytes`
  agrees with the `max_size_percent` denominator.

## [1.7.0] — 2026-05-25

### Fixed
- **Pause/resume is no longer wrongly reported as unavailable on macOS/Linux.**
  The live display computed its own `HAS_KEY_INPUT` via `import msvcrt` (a
  Windows-only module), so on macOS/Linux it was always False and the help
  footer printed "(keyboard pause/resume unavailable on this platform)" — even
  though the key listener was running and the Space/1-9/r keys actually worked
  on a TTY. The display now reads the same `platform_compat.HAS_KEY_INPUT` the
  listener gates on (termios + isatty), so the footer tells the truth. The
  underlying SIGSTOP/SIGCONT pause path was already correct on POSIX.

### Added
- **File-based PAUSE for headless / over-SSH runs.** The keyboard pause keys
  need an interactive TTY; a detached run (`nohup`, `&`, redirected output,
  cron) has none, so the keys are off. Now creating a `PAUSE` file in the encode
  workdir suspends **every** active slot, and deleting it resumes them — the
  no-keyboard counterpart to Space/1-9 (mirrors the existing `FINISH` stop-file).
  - Polled ~2×/second by the render thread and **level-triggered**, so it also
    re-pauses freshly-started chunks while the file persists (a chunk boundary
    starts a new ffmpeg). While the file exists the encode stays suspended,
    including at the point it would otherwise finish — remove it to let the run
    complete, or use `FINISH` for a graceful stop-after-current-chunk.
  - The no-TTY footer now points operators at the `PAUSE`/`FINISH` file
    fallbacks instead of just saying "unavailable".

### Changed
- **Refactor:** the display's pause/resume controls moved out of `display.py`
  (which was at the 500-line cap) into a focused `encode_modules/pause_control.py`
  (same delegation pattern as `size_projection`/`choke_detection`); `display.py`
  is back under the cap. Behaviour-preserving, covered by tests.

## [1.6.0] — 2026-05-25

### Added
- **`retry_with_bigger_crf` — auto-escalate CRF when the size guard stops a
  job.** Previously a `stopped-threshold` abort (projected output over
  `max_size_percent`) produced no output and you had to manually re-run at a
  higher `--crf`, guessing a value. Opt in per job or in `defaults` and the
  queue now re-encodes the same source at a progressively higher CRF until the
  projection fits — or `crf_max` is reached (new terminal status
  `stopped-threshold-crf-exhausted`, a needs-attention state that doesn't stop
  the queue). New queue-only keys: `retry_with_bigger_crf` (default `false`),
  `crf_step` (default `1`), `crf_max` (default `28`).
  - Cheap by design: the size guard aborts at ~5% progress, so each *rejected*
    CRF costs a fraction of an encode; only the accepted CRF runs to completion.
  - Each attempt escalates from the CRF the previous attempt **actually used**
    (so an auto-picked CRF is handled correctly), and the report row records the
    final CRF.
  - Correctness: the CRF-independent lossless split is reused across attempts;
    the superseded encoded chunks (`enc_src_*.mkv`, incl. `.part`) are **moved
    aside** into a `.crf<N>_superseded_<ts>/` subdir of the workdir (never
    deleted, per the never-delete rule) so no old-CRF video can leak into the
    retry's concat. A typo'd `crf_step`/`crf_max` bails to no-escalation with a
    warning rather than crashing the queue.

### Fixed
- **Pre-flight no longer fails an otherwise-fine source on benign dup-DTS.**
  Sources cut/joined with tools like Machete carry duplicate DTS at the join
  points ("non monotonically increasing dts" muxer warnings). The decoder
  finishes cleanly (`exit 0`) and the file plays fine, but the pre-flight scan
  counted those stderr lines as decode errors and failed the whole job
  (`pre-flight-failed`, exit 6) — forcing a `no_pre_flight_scan: true`
  workaround that also disabled *real* corruption screening.
  - The codebase already treats dup-DTS as non-fatal **post-encode**
    (`verify_loop` → `is_dts_only_verify_failure`); pre-flight now applies the
    same carve-out. A window is benign only when `decode_exit_code == 0` **and**
    every stderr line is a dup-DTS warning — classified via a new
    `non_dts_error_count` on the decode walk, computed over **all** stderr lines
    (not the truncated samples), so a real error can't hide behind dup-DTS noise.
    A window mixing dup-DTS with any genuine decode error still fails.
  - Surfaced in the summary (`… N dup-DTS window(s) — benign, will be
    re-stamped`). The `.preflight.json` cache gains a `scan_version` so stale
    pre-fix "failed" verdicts are re-scanned, and is now written atomically.

## [1.5.2] — 2026-05-25

### Fixed
- **Long 4K outputs are no longer falsely quarantined as `damaged_*`.** The
  final post-encode verification step (`_decode_check`) decodes every frame +
  audio sample of the merged output, but used a flat **600 s** timeout. A
  bit-perfect 32.7-min 4K HEVC encode whose honest low-priority decode legitimately
  ran longer than 10 min hit that cap, was reported as `OUTPUT VERIFICATION
  FAILED`, and got renamed `damaged_*` — despite being completely clean.
  - The decode-walk timeout now **scales with the output's duration**
    (`decode_walk_timeout_s` in `encode_modules/verify.py`): `6×` the file's
    duration, with a `900 s` floor for short clips and a `4 h` ceiling. A healthy
    decode runs several times faster than realtime, so the budget is only ever
    reached by a genuine decoder *hang* (no progress at all) — not by slow
    hardware or a long-but-honest file. The ceiling bounds the wasted wall-clock
    if the decoder really has wedged.
  - `verify_output` reuses the duration it already probed (no second `ffprobe`),
    and the timeout message now names the cap and the file's duration so a real
    hang is distinguishable from a too-tight budget.
  - Genuine corruption is still caught immediately: `-xerror` fast-fails on hard
    decode errors regardless of the timeout, so the data-safety invariant (never
    silently accept a broken encode) is unchanged.

## [1.5.1] — 2026-05-25

### Fixed
- **`%` in a filename no longer breaks the generated encoder scripts** — a full
  audit + fix of every `%`-sensitive sink in script generation, extending the
  1.4.3 ffmpeg-segment fix:
  - **POSIX terminal-title `printf`** (the reported bug): the title was
    interpolated into the `printf` *format* string, so `printf` parsed a `%` in
    the filename (e.g. `70% Hell` → `% H`) as a conversion specifier and printed
    `invalid format character`. The title is now passed as a `%s` *data*
    argument, immune to `%` and any other printf-special character.
  - **Windows `.bat` path stashing** (a separate, more serious miss): cmd
    expands `%VAR%` and strips a lone `%` inside `set "VAR=..."`, so a source
    like `C:\…50%PATH%….mkv` had its **actual input/output/workdir path
    corrupted** before the encoder ran. Every value embedded in a `set "..."`
    (input, output, workdir, worker/report script paths, the hook sidecar path)
    now doubles its `%`. The legacy `.bat` terminal title also now uses the
    escaped form (it previously used the raw stem, mishandling `%` and `&`).

  Verified there are no remaining `%`-unsafe sinks: ffmpeg `concat`/`mpegts`/
  `null` outputs are plain (not printf templates), and no Python `%`-format
  string is ever fed a filename.

## [1.5.0] — 2026-05-25

### Fixed
- **No more false chunk-chokes after the laptop sleeps (macOS/Linux).** The
  choke detector's system-sleep guard inferred sleep from a jump in
  `time.monotonic()`, which keeps counting suspended time on Windows but
  *freezes* across system sleep on macOS/Linux — so the guard only ever fired on
  Windows, and every in-flight chunk got falsely flagged as choked and restarted
  on each wake. It now detects suspend clock-agnostically by tracking both the
  monotonic and wall clocks and tripping on the larger gap, so it works on all
  three platforms. Observed on a real macOS run.

### Added
- **Orphaned-ffmpeg watchdog on POSIX (parity with the Windows Job Object).**
  When the orchestrator dies, the chunk ffmpeg encoders no longer keep running.
  Two layers: the lifetime cleanup now also fires on **SIGHUP** (terminal/window
  close) and **SIGQUIT**, and a new sidecar watchdog process reaps the tracked
  ffmpeg process-groups even on a hard `kill -9` of the orchestrator — detected
  via `os.getppid()` reparenting, which no in-process signal handler can cover.
  Best-effort and fully degradable: if the watchdog can't spawn, behaviour falls
  back to the previous atexit/signal cleanup. Windows is unaffected (its Job
  Object already reaps in-kernel).

## [1.4.3] — 2026-05-24

### Fixed
- **Source filenames containing `%` no longer break the split phase.** ffmpeg's
  `segment` muxer scans the *entire* output path for printf-style tokens, so a
  source named e.g. `70% Hell` produced a chunk workdir like `.compress_70% Hell`
  whose `% H` was read as an invalid conversion spec — ffmpeg rejected the
  template ("Invalid segment filename template") and the encode failed before any
  chunk was written (exit 234). The workdir portion of the segment template is now
  escaped (`%` → `%%`) while the intended `src_%04d.mkv` pattern is preserved.
  Only the ffmpeg argument is escaped — the on-disk workdir name (and thus the
  `.split_done` / resume convention) is unchanged. Found on a real MacBook Pro run.

## [1.4.2] — 2026-05-24

### Added
- **`examples/notify_pushbullet.py`** — a ready-to-use, stdlib-only `on_chunk_done`
  hook that pushes a Pushbullet note per finished chunk (cross-platform; no
  `curl` / `jq` needed). The token and target device are read from the
  `PUSHBULLET_TOKEN` / `PUSHBULLET_DEVICE` environment variables, never the file.
  Queue recipe 8 and the README layout point to it; its payload builder is
  unit-tested, including a guard that no secret literal is ever committed.

## [1.4.1] — 2026-05-24

### Documentation
- **Concrete Pushbullet example for the `on_chunk_done` hook** (queue recipe 8),
  with both a POSIX `curl` script and a Windows PowerShell script. The access
  token (and target device) are read from environment variables rather than
  stored in `queue.json` or the script — with a note to revoke any token that
  leaks. No code change.

## [1.4.0] — 2026-05-24

### Added
- **`on_chunk_done` command hook** — run a command after each chunk finishes
  (success *and* failure), e.g. to push a progress notification. Configure it in
  a queue's `defaults` (every job), per job (one file), or on a single run via
  `compress.py --on-chunk-done`. The command is an argv list (`shell=False`, no
  injection) and receives context through `X265_*` environment variables
  (`X265_CHUNK_INDEX`, `X265_CHUNK_TOTAL`, `X265_CHUNK_STATUS`,
  `X265_CHUNK_OUTPUT`, `X265_SOURCE`, …). Best-effort: it runs with a 30 s
  timeout and a missing/slow/failing hook is logged and ignored — it can never
  derail or stall the encode. The hook command travels to the chunk encoder via
  a JSON sidecar, so its quotes never get embedded into the generated `.bat`/`.sh`
  (only the sidecar's path is, riding the existing safe path-stash pattern).

## [1.3.0] — 2026-05-24

### Added
- **"Finish after current chunk"** — a graceful, resumable stop you can toggle
  mid-encode. Press `f` in the live parallel display (or, for headless / serial
  runs, create a `FINISH` file in the workdir — the exact path is printed at
  startup) to let the chunks already encoding finish, start no new ones, and
  stop the encode **and** the queue. The encoder exits `stopped-by-user`
  (code 8) and `run_queue.py` halts; re-run to resume from the next chunk.
  In-flight chunks are never killed, and the stop-file is consumed once honored
  so a re-run doesn't immediately re-stop.

### Fixed
- **UnicodeEncodeError on redirected output under a non-UTF-8 locale.** On
  Windows code pages such as cp1250, redirecting stdout (headless / `nohup` /
  queue log) made Python encode output in the locale codepage and crash on the
  `→` / box-drawing glyphs in the quality summary and live display. The entry
  points now force UTF-8 stdout/stderr via `platform_compat.enable_utf8_io()`.
- **LF-only `.bat` generation.** cmd.exe requires CRLF in `.bat` files; with
  bare LF it dropped a leading character per line (`chcp`→`cp`, `title`→`tle`).
  Generated `.bat` files are normalized to CRLF on Windows (`.sh` stays LF).

### Changed
- **Install-path docs.** Documented both the `~/.claude/plugins/...` (installer
  default) and `~/.claude/skills/...` (user-skill) layouts, and reconciled the
  invocation paths throughout README / SKILL.md.
- The `run_queue.py` aggregate exit code `2` (needs-attention) now also covers
  the new `stopped-by-user` outcome; the queue halts on it.

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

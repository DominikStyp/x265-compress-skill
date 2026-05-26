# Progress bars for concat + quality-check phases

**Date:** 2026-05-27
**Status:** approved (design)

## Problem

The chunk-encode phase shows a rich live bar (`[#####-----] 42.3% …`) via
`ParallelDisplay`. The two phases that run *after* it tear down show nothing
comparable:

- **Phase 3 — concat** (`chunking.concat_chunks`) runs `ffmpeg -c copy` as a
  blocking `subprocess.run` with no `-progress` and no rendering: the user sees
  one `[3/4] Concatenating N chunks…` line, then silence until done.
- **Phase 4 — quality** (`quality.py`) does emit a progress line, but it's plain
  text (no visual bar) and in chunks mode it ticks per sampled chunk, so it
  flickers rather than reading as a single pass.

Goal: visual parity — give both phases the same `[bar] pct  out/total  fps  speed`
line the encode phase uses, **reusing** existing bar code (no duplication).

## Approach

One shared renderer, reused by concat + quality. (Rejected: inlining a bar at
each site — duplicates the bar string, violates DRY; unifying the live
`ParallelDisplay` + standalone `progress.py` into it too — broad, risky refactor
of working display code, out of scope.)

### `encode_modules/progress_bar.py` (new)

A small renderer that turns `(prefix, done_s, total_s, fps, speed)` into the
canonical line and prints it adaptively:

- **TTY:** in-place `\r{line}\033[K` (every tick).
- **pipe / headless:** print a fresh line only when progress advanced ≥ a
  threshold (~10 percentage points) so logs aren't drowned.

Reuses `display_render`'s bar glyphs / `BAR_WIDTH` and `formatting.format_hms`
for the `H:MM:SS` fields — no new bar-drawing or time-formatting code. `is_tty`
and the output stream are injectable so tests never touch a real terminal. A
tiny mutable `state` (last-printed pct) carries the pipe-throttle between ticks.
A `finish()` clears the TTY line (`\r\033[K`) at the end.

### Concat (`chunking.concat_chunks`)

- New signature `concat_chunks(workdir, dst, *, total_dur=None)`. `verify_loop`
  passes the `total_dur` it already computed; when omitted it falls back to
  probing the encoded chunks (keeps other/standalone callers working) —
  backward-compatible.
- Switch the blocking `subprocess.run` to a `Popen` with
  `-progress pipe:1 -nostats`, parse `out_time_us`, drive the renderer against
  `total_dur`.
- The ffmpeg child is terminated in a `finally` on error/abort (no leaked
  encoder — subprocess-discipline invariant). The data-loss / incomplete-set
  guard ahead of the concat is unchanged.

### Quality (`quality.py`)

- `_quality_check_run` stops drawing its own line; it gains an
  `on_progress(out_s, fps, speed)` callback (default `None` = silent). Cleaner
  seam, and it lets the caller own the *overall* picture.
- `quality_check_chunks` (the user's mode): `total = Σ probe_duration(sampled
  src chunk)`; maintains a running `done_s`; the callback renders **one overall
  bar** `(done_s + current_out_s) / total`, annotated `chunk i/N`. After each
  chunk, `done_s += that chunk's duration`. Smooth 0→100% across the pass.
- `quality_check` (full mode): single run → bar `out_s / duration` directly.

`_render_vmaf_progress`'s rendering moves into the shared module, so `quality.py`
should stay roughly flat. It is ~493/500 lines; if this change tips it over, the
chunk-sampling logic is split into its own module **as part of this change**
(500-line rule).

## Testing (TDD)

- **progress_bar:** bar fill + pct + field formatting; TTY renders every tick;
  pipe throttles to ≥ threshold; `finish()` clears. Inject fake stream + `is_tty`.
- **concat:** fake `Popen` emitting `out_time_us=` lines → the renderer is driven
  with the right `total`; ffmpeg is terminated on an error/exception path.
- **quality overall:** inject a fake `_quality_check_run` that fires
  `on_progress` → the overall pct accumulates correctly across chunks
  (`done + current` over `total`) and the prefix carries `chunk i/N`.

## Cross-platform

No new OS-specific code. The `\r` / `\033[K` path is already gated by
`platform_compat.enable_ansi` (Win32 VT enabled at startup; POSIX no-op), and
ffmpeg `-progress` is cross-OS. Verified by reasoning + the injected-stream tests.

## Out of scope

The live `ParallelDisplay` and the standalone `progress.py` keep their own
renderers; no behaviour change to the encode-phase bar.

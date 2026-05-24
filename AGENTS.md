# Agent instructions for x265-compress-skill

These rules are **mandatory** for any coding agent (Claude Code, Copilot, Cursor,
Codex, Gemini, etc.) working in this repository. Follow them on **every** change,
without being reminded.

This is a resumable, cross-platform x265 chunked video encoder written in pure
Python 3.9+ stdlib (no third-party runtime deps). It runs as a Claude Code skill
*and* as standalone scripts. Correctness and cross-OS behaviour matter more than
cleverness — a bad encode can silently corrupt or delete a user's source video.

## 1. Code quality: SOLID, DRY, readability

- Apply **SOLID** and **DRY**. No copy-paste logic — extract a shared helper.
- Each function does one thing. Prefer small, pure, testable functions with a
  clear seam (the existing tests inject `render_fn`, fake procs, etc. — preserve
  that style so behaviour stays unit-testable).
- Match the surrounding code: naming, type hints (`from __future__ import
  annotations`), comment density, and idioms already in the file.
- Readability is a feature. If a reviewer needs the diff explained, simplify it.
- **Stdlib only.** No third-party runtime *or* test dependencies — preserve the
  "no `pip install`" promise. If you reach for a package, solve it with the
  standard library instead.
- **Comment the *why*, not the *what*,** for any safety/guard code. Match the
  existing test docstrings (e.g. `tests/test_render_tick.py`) that explain *why*
  a guard exists and what breaks without it.

### Project invariants — do not regress these

These encode hard-won correctness properties of a kill-survivable encoder that
can otherwise corrupt or delete a user's source video. Treat them as
non-negotiable; the reviewer subagents in rule 4 must check them.

- **Data safety (the data-loss guard).** Never delete or overwrite a user's
  source until the encoded output has passed verification. Destructive cleanup is
  **quarantine-first** — move aside, don't `unlink`, the way `skipped_collector`
  quarantines choked parts. When in doubt, keep the file.
- **Subprocess discipline.** Build commands as argument **lists** — never
  `shell=True`, never string-concatenated command lines (avoids cross-OS quoting
  and injection bugs). Put a timeout on probe-style `subprocess.run` calls. Every
  spawned ffmpeg must be terminated on abort/error so encoders are never leaked.
- **Error handling.** Catch specific exceptions and fail loud and early. A broad
  `except Exception` is allowed **only** at the daemon-thread guard seam, and it
  must surface the error to the events queue — never silently swallow (see the
  `_render_tick` pattern). No bare `except:`.
- **Atomic writes & resumability.** Write every final artifact to a temp name and
  atomically `os.replace()`/`rename()` it into place (the chunk workers' `*.part`
  → `rename` pattern) — never write a final file in place, including sidecar/cache
  JSON. Every operation must be safe to re-run from any interruption point.
  Changes to on-disk state (history JSONL, `queue.json`) must stay
  backward-compatible or ship a migration (see the `backward_compatible` test).

## 2. Keep modules under 500 lines

- **No source file may exceed 500 lines.** Modules must stay atomic and
  maintainable.
- If a change pushes a file over 500 lines, **refactor it** (split into focused
  modules under `encode_modules/`, `compress_modules/`, or `platform_compat/`) as
  part of the same change — do not defer it.
- `encode_modules/display.py` is currently ~540 lines (a pre-existing
  violation). If you touch it, split it instead of growing it further.
- Splitting must be behaviour-preserving and covered by the existing/extended
  tests.

## 3. Always write, update, and verify tests

- Any source change requires matching test work: add tests for new behaviour,
  update tests for changed behaviour, and keep regression tests green.
- Tests are stdlib `unittest`, live in `tests/`, and add the repo root to
  `sys.path`. Run the full suite and confirm it passes **before** claiming done:

  ```sh
  python -m unittest discover -s tests -v
  ```

- Never assert success without showing the run. If a test is skipped or fails,
  say so explicitly with the output.

## 4. Dispatch at least 2 reviewer subagents after every change

After completing a change (and getting tests green), dispatch **at least two
review subagents** using the most capable model available at the highest
reasoning effort. Ask each to independently review the diff for:

- bugs and logic errors introduced or exposed by the change,
- failure modes / edge cases (kill-resume, partial chunks, corrupt sources),
- anything that should be refactored (including the 500-line rule),
- cross-platform regressions (see rule 5).

Relay their findings, reconcile conflicts, and address real issues before
finishing. Two independent perspectives are required, not optional.

## 5. Verify every change against Windows, Linux, and macOS

This project explicitly targets **Windows 10+, macOS 11+, and Linux**. OS
behaviour is auto-detected via `platform_compat/` (`_windows.py`, `_posix.py`).
For every change, check that it works — or at least does not break — on all three:

- No hardcoded path separators, shells, or platform assumptions outside
  `platform_compat/`. Route OS-specific behaviour through that layer.
- Watch for: path handling (`pathlib`, drive letters vs POSIX), process
  spawning/termination signals, `python` vs `python3` on PATH, line endings,
  ANSI/terminal handling, and `.bat` vs `.sh` script generation
  (`compress_modules/_bat_templates.py` vs `_sh_templates.py`).
- When a change can behave differently per OS, state how you verified each
  platform (test, reasoning, or doc reference) and make the reviewer subagents in
  rule 4 check cross-OS impact.

## Quick reference

- Run tests: `python -m unittest discover -s tests -v`
- Entry points: `compress.py`, `encode_resumable.py`, `run_queue.py`
- OS abstraction: `platform_compat/`
- Encode pipeline: `encode_modules/`
- Script generation: `compress_modules/`
- Tuning notes: `references/x265-tuning.md`

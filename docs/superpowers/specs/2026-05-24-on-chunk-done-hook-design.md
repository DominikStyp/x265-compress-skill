# Design: `on_chunk_done` command hook

**Date:** 2026-05-24
**Status:** Approved
**Target version:** 1.4.0 (MINOR — new feature)

## Goal

Let the user run an external command **after each chunk finishes**, configurable
both **per file** and **in the queue JSON**. Motivating example: a Pusher /
webhook notification on encode progress.

## Decisions (from brainstorming)

1. **Command format:** an **argv list**, run with `shell=False` + a timeout.
   Context is delivered via `X265_*` **environment variables** — no shell
   quoting, no string concatenation, no injection. Cross-OS because the user
   names the interpreter in the list (`["bash","x.sh"]`, `["pwsh","-File","x.ps1"]`).
2. **Fire scope:** once per finished chunk, on **success *and* failure**
   (`X265_CHUNK_STATUS=ok|failed`). No separate whole-file-done event.

## The linchpin: crossing the generated-script boundary

`compress.py` does not encode — it writes a `.bat`/`.sh` that later invokes
`encode_resumable.py`. Config must survive
`run_queue → compress.py → generated script → encode_resumable → encoder`.

The generated scripts carry paths safely by stashing them in a shell variable
(`set "_SKILL_IN={path}"` on Windows; `_SKILL_IN='...'` on POSIX). That trick is
safe **only because a Windows filename can never contain `"`**. A hook argv like
`["bash","x.sh"]` *does* contain `"`, so embedding it inline or via that stash
breaks on `.bat` — the cross-OS quoting hazard the project invariants forbid.

**Chosen approach — JSON sidecar, pass only its path.** Write the argv list to
`<tmp>/<stem>.hooks.json`; bake only the sidecar's *path* into the script (a path
has no `"`, so it rides the existing stash pattern unchanged). The quote-bearing
content lives in a file written/read with the `json` module.

*Rejected:* inline JSON arg (fragile `.bat` quoting); base64-encode the JSON
(avoids a file but is opaque/un-debuggable in the generated script).

## Config surface

One mechanism, both requirements — everything funnels through `compress.py`:

- **queue.json:** `on_chunk_done` key, valid in `defaults` (all jobs) **and**
  per-job (overrides → per-file granularity, via the existing merge mechanism).
  Value is a JSON array (a bare string is accepted and wrapped to a 1-element list).
- **compress.py CLI:** `--on-chunk-done '["bash","/path/notify.sh"]'` (or a bare
  single token).

Flow:
```
queue on_chunk_done (list)
  -> merge_job (VALID_KEYS gate)
  -> build_compress_argv:  argv += ["--on-chunk-done", json.dumps(list)]   # safe subprocess arg-list hop
compress.py --on-chunk-done <json|bare>
  -> parse_hook_spec(value) -> list[str]            # validate; fail loud on bad config
  -> write_script(..., on_chunk_done=list)
       -> write_hook_sidecar(<tmp>/<stem>.hooks.json)   # atomic temp + os.replace
       -> script stashes the sidecar PATH, adds: --hooks-config "<path>"   # only a path crosses
encode_resumable.py --hooks-config <path>
  -> load_hook_sidecar(path) -> list[str] | None
  -> ChunkHook(command, source, workdir, total)
  -> run_encode_verify_loop(..., chunk_hook) -> encode_chunks(..., chunk_hook)
       -> encode_chunks_parallel / encode_chunks_serial
```

## The hook contract (`X265_*` env vars)

| Var | Example | Notes |
|---|---|---|
| `X265_HOOK_EVENT` | `chunk-done` | constant; future-proofs other events |
| `X265_CHUNK_STATUS` | `ok` \| `failed` | |
| `X265_SOURCE` | `/abs/a.mp4` | source video |
| `X265_WORKDIR` | `/abs/.tmp/.compress_a` | |
| `X265_CHUNK_NAME` | `src_0003.mkv` | chunk file name |
| `X265_CHUNK_INDEX` | `3` | 1-based original position of THIS chunk (NOT progress) |
| `X265_CHUNK_TOTAL` | `12` | |
| `X265_CHUNK_OUTPUT` | `…/enc_src_0003.mkv` | empty string if `failed` |
| `X265_CHUNK_ELAPSED_SEC` | `84.21` | wall seconds, 2dp |
| `X265_CHUNKS_DONE` | `4` | chunks completed so far (disk ground truth) |
| `X265_DURATION_DONE_SEC` | `240.00` | source seconds encoded so far |
| `X265_DURATION_TOTAL_SEC` | `720.00` | source duration |
| `X265_PROGRESS_PERCENT` | `33.3` | overall progress 0–100, clamped |

All values are strings. The hook process inherits `os.environ` plus these.

> The overall-progress fields were added because parallel mode can finish
> chunks out of order (chunk 10 may complete before chunk 2), making
> `INDEX/TOTAL` a meaningless "percentage". `X265_CHUNKS_DONE` /
> `X265_DURATION_DONE_SEC` / `X265_PROGRESS_PERCENT` are derived from disk
> ground truth + cached `probe_duration` calls, so they're honest in both
> parallel and serial mode.

## Fire semantics (safety-first)

- **Best-effort, never derails the encode.** `ChunkHook.fire()` catches
  `TimeoutExpired`, `OSError`, `SubprocessError`, and non-zero exit; it **returns
  an optional log line and never raises** (the command is pre-validated). This is
  essential in the parallel worker: a raising hook would otherwise trip the
  choke/needs-fix path or kill a worker slot.
- **Synchronous**, 30 s fixed timeout (`HOOK_TIMEOUT_SEC`). Async/fire-and-forget
  rejected — adds a thread + abort-cleanup burden for a fast notification.
- **Resume-safe.** Already-encoded chunks are filtered out of `todo` before
  encoding, so a re-run does not re-fire `ok` for completed chunks. A previously
  failed chunk that is retried fires again (correct).
- **Logging.** `fire()` returns a message only on hook failure/timeout/non-zero;
  success is silent (the user's command does the actual notifying). Parallel logs
  via `display.events.put`; serial via `print`.

## Fire sites

- **Parallel** (`encode_parallel._attempt_chunk`): a `finally` fires exactly once
  per attempt, deriving status from ground truth (`enc_<stem>.mkv` exists → `ok`,
  else `failed`). Uniformly covers success / autofix-success / choke / exception.
  Only `elapsed` is threaded out of the try.
- **Serial** (`encode_serial`): after `part.rename(out)` (`ok`) and before the
  failure `sys.exit` (`failed`).

## New modules

- `encode_modules/chunk_hook.py` — `ChunkHook` (command, source, workdir, total,
  injectable `runner`/`timeout`), `.enabled`, `.fire(...)`, `_build_env`.
  Runtime only; imported by the encoders + `encode_resumable`.
- `encode_modules/hook_config.py` — `parse_hook_spec(str) -> list[str]`,
  `write_hook_sidecar(tmp_dir, stem, command) -> Path` (atomic),
  `load_hook_sidecar(path) -> list[str] | None`. Config IO; imported by
  `compress.py` / `script_writer.py` / `encode_resumable.py`.

A corrupt/missing sidecar at encode time warns and proceeds **without** the hook
(an auxiliary notification typo must not abort a multi-hour encode); bad config is
caught loudly at `compress.py` time by `parse_hook_spec` before any encode.

## Touched files

`queue_modules/job_schema.py` (VALID_KEYS + argv), `compress.py` (`--on-chunk-done`),
`compress_modules/script_writer.py` + `_bat_templates.py` + `_sh_templates.py`
(resumable templates only: `{hooks_setup}`/`{hooks_flag}`), `encode_modules/cli_args.py`
(`--hooks-config`), `encode_resumable.py`, `encode_modules/verify_loop.py` +
`encode_modules/encoder.py` (thread `chunk_hook`), `encode_modules/encode_parallel.py`,
`encode_modules/encode_serial.py`.

Docs/version: `SKILL.md`, `README.md`, `docs/AGENT_QUEUE_RECIPES.md`, `CHANGELOG.md`,
`.claude-plugin/plugin.json` → `1.4.0`.

## Tests (TDD, stdlib unittest)

- `parse_hook_spec`: JSON array; bare token → 1-element; invalid JSON / non-list /
  empty / non-string items → loud failure.
- sidecar `write` → `load` roundtrip; `load` of missing/None → `None`; atomic
  (no partial file on the final path).
- `ChunkHook.fire`: disabled → no-op, runner not called; success → returns `None`,
  runner called with exact argv, env contains every `X265_*` with correct string
  values (output `""` on failure); non-zero exit / `OSError` / `TimeoutExpired` →
  returns a message and **never raises**.
- `build_compress_argv`: emits `--on-chunk-done <json>` for both list and bare
  string forms; absent when key missing.
- Integration: parallel `_attempt_chunk` fires `ok` (output set) and `failed`
  (output empty); serial fires both. Injected fake `runner`.

Full suite stays green; then ≥2 reviewer subagents (rule 4) + Windows/Linux/macOS
check (rule 5). Ship v1.4.0 (rule: always release after a change).

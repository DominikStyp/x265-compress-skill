# Finish-after-current-chunk — design

- **Date:** 2026-05-24
- **Status:** approved (design)
- **Summary:** a user-toggleable "finish the current chunk, then stop" control
  for the resumable encoder and the queue runner.

## Goal

During an encode, let the user request a *graceful* stop: let the chunk(s)
currently encoding finish, do **not** start new ones, stop the current file
**and** the queue, and exit. Re-running resumes from the next unencoded chunk
(and the next queue job). The source is never touched; the output is left
intentionally incomplete and resumable.

## Non-goals

- Killing in-flight chunks — that is the existing Ctrl-C / abort path.
  "Finish current" lets the running chunk(s) complete.
- A new resume mechanism — reuse the existing skip-existing resume (a chunk is
  done when `enc_<stem>.mkv` exists; a queue job is done when its output
  exists).
- Interactive keyboard in the serial path (it has no listener) — the stop-file
  covers serial + headless.

## Trigger — `FinishSignal`

New module `encode_modules/finish_signal.py`, the single source of truth both
encode loops consult:

- `FINISH_FILENAME = "FINISH"` — the sentinel filename inside the workdir.
- `class FinishSignal(stop_file: Path | None)`
  - `requested -> bool`: `True` if the in-memory flag is set **or**
    `stop_file` exists on disk.
  - `request()` / `cancel()`: set / clear the in-memory flag (keyboard).
  - `consume_stop_file()`: best-effort delete of `stop_file` if present, so a
    re-run does not immediately re-stop. Never raises.

Two trigger mechanisms, both feeding `FinishSignal`:

1. **Keyboard `f`** (parallel path, where the listener + live display already
   run): toggles the in-memory flag ON/OFF. The live footer shows
   `FINISH AFTER CURRENT CHUNK: ON`. Reversible while the current chunk is
   still encoding.
2. **Stop-file `<workdir>/FINISH`** (serial + headless / `nohup` / CI): both
   loops check it. `encode_resumable` prints the exact path at startup; because
   `run_queue` inherits stdout, that hint also appears in the queue log.
   Consumed (deleted) when honored.

Each encoder builds its own `FinishSignal(workdir / FINISH_FILENAME)` from the
workdir it already has — the parallel encoder hands it to `ParallelDisplay` so
the `f` key can toggle it; the serial encoder uses the file-only form. No new
parameter is threaded through `encoder.encode_chunks`.

## Stop semantics

Each loop checks `finish_signal.requested` at the **top of each chunk
iteration**, next to the existing `abort_event` check:

- **Parallel** (`_worker`, `encode_parallel.py`): if requested, the worker
  stops pulling new chunks and returns. The chunk it is *currently* encoding
  finishes normally — never killed. All in-flight slots complete; no new chunk
  starts.
- **Serial** (`encode_chunks_serial`): if requested, `break` before starting
  the next chunk. The in-flight chunk has already finished (we are at the top
  of the next iteration).

When the encode stops with unencoded chunks remaining, the encoder:
`finish_signal.consume_stop_file()` → `mark_status("stopped-by-user")` → print
a clear "STOPPED — re-run to resume" block → `sys.exit(8)` (skip concat /
verify; the output is intentionally incomplete).

If the request is cancelled (keyboard `f` off) before a loop acted on it, the
encode continues normally. In parallel, if some workers already exited, the
survivors drain the remaining queue and the encode still completes correctly
(just with fewer slots).

## Exit code + queue

- **Exit code 8** = stopped-by-user (raised by the encoder; propagates through
  `encode_resumable`).
- `queue_modules/job_runner._EXIT_STATUS`: add `8: "stopped-by-user"`.
- `run_queue.py`: on status `stopped-by-user`, **halt the whole queue** (break
  the loop; launch no further jobs) and exit. Add `stopped-by-user` to the
  needs-attention status set → aggregate exit code **2** (intentional and
  resumable — not a failure).

## Resume

No new code. Re-running the `.bat` resumes at the first missing
`enc_<stem>.mkv`; re-running the queue skips finished outputs and resumes the
interrupted file. The consumed stop-file ensures the re-run does not re-stop.

## Files touched

- **NEW** `encode_modules/finish_signal.py` (+ `tests/test_finish_signal.py`)
- `encode_modules/keyboard_input.py` — handle `f`
- `encode_modules/display.py` — own the `FinishSignal`; `f` toggles; footer state
- `encode_modules/display_render.py` — `f` in the help line + the ON banner
- `encode_modules/encode_parallel.py` — build signal, worker checks `requested`, stop → exit 8
- `encode_modules/encode_serial.py` — build signal, loop checks `requested`, stop → exit 8
- `encode_resumable.py` — print the FINISH-file hint at startup
- `run_queue.py` + `queue_modules/job_runner.py` — status 8 → halt + aggregate 2
- docs: `SKILL.md` (key list, queue behavior, exit-code table), `README.md`
  (Features bullet), `CHANGELOG.md`
- All modules stay under 500 lines.

## Tests (stdlib `unittest`)

- `FinishSignal`: default `requested` False; `request()`→True; `cancel()`→False;
  stop-file present→True; `consume_stop_file()` deletes it; missing file safe.
- Parallel worker honors the signal: `_worker` with `requested` True pulls
  nothing and returns; with it False, the existing path runs.
- Keyboard `f` toggles the signal (feed `b"f"` to the listener with a fake
  display; assert request/cancel).
- `job_runner.status_for_exit(8) == "stopped-by-user"`.
- `run_queue._aggregate_exit_code(["stopped-by-user"]) == 2`, and the halt
  decision.

## Edge cases

- Keyboard `cancel` does not remove a present stop-file (request = event OR
  file); mixing both in one run is unusual — documented.
- Stop-file delete failure (permissions): best-effort + warn; a re-run would
  re-stop (rare).
- Headless parallel (no tty): no keyboard — the stop-file is the trigger.

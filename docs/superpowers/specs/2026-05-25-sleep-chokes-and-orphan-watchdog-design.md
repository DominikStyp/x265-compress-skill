# Design: macOS sleep false-chokes + orphaned-ffmpeg watchdog

**Date:** 2026-05-25
**Status:** Implemented (v1.5.0)
**Origin:** Two related process-lifetime/time-across-suspend defects observed on
a real macOS 15 / Apple-Silicon run (bug report `x265-BUG-sleep-chokes-and-orphaned-ffmpeg.md`).

Both bugs are POSIX-specific gaps where Windows already behaved correctly, so
the fixes bring macOS/Linux up to parity without touching the Windows paths.

---

## BUG 1 — sleep guard never fired on macOS/Linux → false chokes

### Problem
The choke detector skips its verdict for one cycle when it detects the machine
slept (the suspended ffmpeg produced no progress, so every slot would otherwise
look choked on wake). It inferred sleep from a jump in `time.monotonic()`. That
clock **keeps counting** suspended wall time on Windows but **freezes** across
system sleep on macOS/Linux (CPython uses `CLOCK_MONOTONIC` / `mach_absolute_time`
/ `CLOCK_UPTIME_RAW`, none suspend-aware). So the guard only ever tripped on
Windows; on macOS each `pmset` wake produced false chokes and needless restarts.

### Fix
Track **both** clocks at each check and trip on `max(monotonic_gap, wall_gap)`.
The wall clock (`time.time()`) always advances across suspend on every platform,
so the wall gap catches macOS/Linux; the monotonic gap still catches Windows.

- **Trade-off:** wall time is non-monotonic. A forward NTP/manual step >
  `sleep_detect_seconds` could trip the guard without a real suspend — but the
  only cost is masking a real choke for one cycle (it reasserts next check), and
  NTP slews rather than steps in steady state. A backward step makes `wall_gap`
  negative, so `max()` falls back to the monotonic gap (no spurious trip). The
  rare self-healing false-positive is far cheaper than the false-negative it
  replaces.
- Touch points: `encode_modules/choke_detection.py` (`check_choke`),
  `encode_modules/display.py` (new `_last_choke_check_wall` field + comment).
- Tests: `tests/test_sleep_detection.py` — macOS case (small mono gap / large
  wall gap) trips; Windows case still trips; normal cadence, first call, and a
  backward wall jump do **not** trip.

---

## BUG 2 — ffmpeg orphaned when the orchestrator dies on POSIX

### Problem
Chunk ffmpegs spawn with `start_new_session=True` (own session/pgid), which is
why they survive — and are immune to — a terminal SIGHUP. The lifetime cleanup
(`_LifetimeGroup`) was `atexit` + SIGTERM only, so closing the launching terminal
(SIGHUP) or `kill -9` (SIGKILL) of the orchestrator left the ffmpegs running with
`ppid==1`, burning CPU with no orchestrator to concat/verify/advance. Windows has
no such gap — its Job Object reaps assigned children in-kernel on parent death.

### Fix (two layers, for parity with the Job Object)
1. **Signals:** `_LifetimeGroup._install_signal_handlers` now also chains
   **SIGHUP** and **SIGQUIT** (getattr-guarded so the module still imports on
   Windows). This directly covers the observed terminal-close case. SIGINT is
   still left to Python's KeyboardInterrupt → encoder `resume_all()` path.
2. **Watchdog:** a sidecar process (`platform_compat/_posix_watchdog.py`),
   spawned by `_LifetimeGroup.__init__` as a fresh interpreter (subprocess, **not**
   `os.fork` — the orchestrator is multi-threaded and fork+threads is unsafe on
   macOS). The orchestrator streams ffmpeg pgids to its stdin as it spawns them.
   The watchdog polls `os.getppid()`; when the orchestrator dies for **any**
   reason (incl. SIGKILL) it reparents (getppid changes) and SIGTERMs+SIGKILLs the
   tracked groups. A `QUIT` sentinel from the orchestrator's own cleanup stands it
   down on graceful exit so it never double-reaps.

- A shared `kill_groups()` helper (SIGTERM → grace → SIGKILL) is used by **both**
  the watchdog and `_LifetimeGroup._cleanup` (DRY; identical reap semantics).
- **Accepted risks (unchanged from prior `_cleanup`):** a tracked pgid recycled
  by the OS in the sub-second window between death and reap could be wrongly
  killed — negligible vs the pid space. Detection latency is bounded by the 1 s
  poll + reap grace. pgids accumulate across a queue run and are never pruned
  (pruning would need liveness tracking the design avoids; reaping a dead pgid is
  a harmless no-op).
- Everything is best-effort: if the watchdog can't spawn, behaviour degrades to
  the atexit/signal path (covers all but parent-SIGKILL) and the encode is never
  broken.
- Windows-import safety: `signal.SIGKILL`/`SIGHUP`/`SIGQUIT` and `os.killpg`
  don't exist on Windows; all are getattr-guarded or lazily bound so the POSIX
  module imports cleanly for cross-platform unit tests.
- Tests: `tests/test_orphan_watchdog.py` — pure-function (`kill_groups`) and
  injected-fake tests (`run`, `_LifetimeGroup` add/cleanup/SIGHUP) run on every
  OS; a POSIX-gated end-to-end test spawns a real victim + real watchdog and
  asserts reaping on simulated parent death.

"""Primary + fallback notifier dispatcher (stdlib only).

A single transport is a single point of failure: ntfy.sh can be unreachable,
or a Pushbullet free account can hit its 500-pushes/month cap. This wrapper
runs an ORDERED list of notifier scripts in subprocesses (forwarding the
current environment) and stops at the first success — so a job-end alert still
lands as long as ONE transport works.

Wire it at a hook instead of an individual notifier:

    # queue.json
    "on_job_end": ["python3", "/path/to/examples/notify_dispatch.py"]
    # env: NTFY_TOPIC=...        (primary, tried first)
    #      PUSHBULLET_TOKEN=...   (fallback, tried only if ntfy fails)

On Windows: `["python", "C:/path/to/examples/notify_dispatch.py"]`.

The chain is configurable via the `X265_NOTIFY_CHAIN` env var — an
OS-path-separated (`os.pathsep`: `:` POSIX, `;` Windows) list of notifier
script paths, tried in order. With it unset the default is
`[notify_ntfy.py, notify_pushbullet.py]` resolved RELATIVE to this
dispatcher's own directory, so a stock `examples/` checkout works with no
configuration beyond the transport secrets.

Exit semantics: 0 if ANY notifier exited 0; non-zero if ALL failed (or the
chain was empty). Each child inherits this process's environment, so
`X265_HOOK_EVENT` and every `X265_*` context var — plus the transport secrets
— pass straight through. A notifier that can't even be spawned (OSError /
ValueError / SubprocessError) counts as a failure and the chain continues to
the next entry, never aborting the dispatcher.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping

# The encoder wraps the WHOLE dispatcher in a 30 s hook timeout, so the sum of
# the per-child timeouts must stay under that even with every transport slow /
# unreachable. `_per_notifier_timeout` divides a 28 s budget across the chain
# (capped at 12 s each) so a 2-chain gets 12 s each (24 s total) and a longer
# user-configured chain shrinks per-child rather than blowing the 30 s wrapper
# and silently cutting off a working later transport.
PER_NOTIFIER_TIMEOUT_SEC = 12.0
_TOTAL_BUDGET_SEC = 28.0

_DEFAULT_NOTIFIERS = ("notify_ntfy.py", "notify_pushbullet.py")


def _per_notifier_timeout(chain_len: int) -> float:
    """Per-child subprocess timeout: the 28 s budget split across the chain,
    capped at 12 s, so chain_len × timeout < the encoder's 30 s hook wrapper."""
    if chain_len <= 0:
        return PER_NOTIFIER_TIMEOUT_SEC
    return min(PER_NOTIFIER_TIMEOUT_SEC, _TOTAL_BUDGET_SEC / chain_len)


def _append_notify_log(path: str, *, outcome: str) -> None:
    """Best-effort, secret-free append of ONE line when X265_NOTIFY_LOG is set
    and the whole chain failed. Never raises. No-op on empty path."""
    if not path:
        return
    try:
        line = (f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t"
                f"dispatch\t{os.environ.get('X265_HOOK_EVENT', 'chunk-done')}"
                f"\t{outcome}\n")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def parse_chain(env: Mapping[str, str], *, script_dir: Path) -> list[str]:
    """The ordered list of notifier script paths to try. `X265_NOTIFY_CHAIN`
    (OS-path-separated) overrides; empty segments are dropped so a trailing
    separator is harmless. Unset -> the default ntfy-then-pushbullet pair
    resolved relative to `script_dir` (this dispatcher's own directory)."""
    raw = env.get("X265_NOTIFY_CHAIN", "")
    if raw.strip():
        return [seg for seg in raw.split(os.pathsep) if seg.strip()]
    return [str(script_dir / name) for name in _DEFAULT_NOTIFIERS]


def run_chain(chain: list[str], *, env: Mapping[str, str], python: str,
              runner: Callable[..., object] = subprocess.run,
              timeout: float = PER_NOTIFIER_TIMEOUT_SEC) -> int:
    """Run each notifier in order; return 0 at the first success, else 1.

    `runner` is injectable so tests never spawn a real process. A spawn
    failure (OSError: interpreter/script missing; ValueError: embedded NUL;
    SubprocessError: timeout etc.) is treated as a failure for that entry and
    the chain continues — one broken notifier must not sink the others."""
    for script in chain:
        try:
            proc = runner(
                [python, script], env=dict(env),
                timeout=timeout,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if getattr(proc, "returncode", 1) == 0:
            return 0
    return 1


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    chain = parse_chain(os.environ, script_dir=script_dir)
    rc = run_chain(chain, env=os.environ, python=sys.executable,
                   timeout=_per_notifier_timeout(len(chain)))
    if rc != 0:
        print("notify_dispatch: all notifiers failed", file=sys.stderr)
        _append_notify_log(os.environ.get("X265_NOTIFY_LOG", "").strip(),
                           outcome="all notifiers failed")
    return rc


if __name__ == "__main__":
    sys.exit(main())

"""Shared opt-out switch for the low-CPU-priority wrap.

Default encoder behaviour is to run every ffmpeg child at idle priority so
foreground apps (browser, editor, video playback) always preempt the encode.
That's right for daily-driver laptops but pessimizes throughput on dedicated
encoder hardware where no foreground workload competes for CPU.

Set ``CLAUDE_ENCODING_NO_NICE=1`` (any non-empty value) in the encoder's
environment to disable the wrap on both backends:

  * POSIX (`_posix.wrap_cmd_for_low_priority`) — skips the ``nice -n 19`` prefix.
  * Windows (`_windows.low_priority_popen_kwargs`) — drops the
    ``IDLE_PRIORITY_CLASS`` creationflag.

Lifecycle kwargs (POSIX ``start_new_session=True`` for killpg, Windows Job
Object tracking) are deliberately unaffected — this knob is priority-only.
Empty string and unset are treated as "use default wrap" so a stale
``export CLAUDE_ENCODING_NO_NICE=`` in a shell rc file does not silently
disable the wrap."""
from __future__ import annotations

import os


def low_priority_disabled() -> bool:
    """True iff the env-var opt-out is set to a non-empty (truthy) value."""
    return bool(os.environ.get("CLAUDE_ENCODING_NO_NICE"))

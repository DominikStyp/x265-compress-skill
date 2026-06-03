"""CLAUDE_ENCODING_NO_NICE env var: opt out of the low-priority wrap.

Default behaviour wraps every spawned ffmpeg in `nice -n 19` (POSIX) or starts
it with IDLE_PRIORITY_CLASS (Windows) so foreground apps always preempt the
encode. On a dedicated encoder machine — Dominik's Mac during a batch run, or
a headless Linux box — there is no foreground workload competing for CPU, and
idle priority simply leaves throughput on the table. The env-var opt-out is the
single, OS-portable switch for that case.

Tests assert: (a) default behaviour is byte-identical to v1.15.0 on both
backends; (b) setting the env to any non-empty string bypasses the wrap; (c)
unrelated kwargs (POSIX `start_new_session` for killpg lifecycle) are NOT
disabled when the env is set — the bypass is priority-only."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from platform_compat import _posix  # noqa: E402 — always importable
from platform_compat._priority_env import low_priority_disabled  # noqa: E402


class _EnvSandboxMixin:
    """Snapshot and restore CLAUDE_ENCODING_NO_NICE around each test so a test
    that sets the env does not bleed into the next one. Not a TestCase itself —
    mixed into each TestCase below."""

    def setUp(self) -> None:  # type: ignore[override]
        super().setUp()  # type: ignore[misc]
        self._saved_env = os.environ.pop("CLAUDE_ENCODING_NO_NICE", None)

    def tearDown(self) -> None:  # type: ignore[override]
        os.environ.pop("CLAUDE_ENCODING_NO_NICE", None)
        if self._saved_env is not None:
            os.environ["CLAUDE_ENCODING_NO_NICE"] = self._saved_env
        super().tearDown()  # type: ignore[misc]


class LowPriorityDisabledHelperTest(_EnvSandboxMixin, unittest.TestCase):
    """Pin the truth-table of the shared helper directly — both wrappers
    delegate to it, so changing its contract changes both backends in lockstep.
    Especially documents the deliberately-surprising "0" / "false" / "no"
    behaviour (all are truthy strings in Python) so any future refactor toward
    stricter parsing flips a red test instead of silently changing semantics."""

    def test_unset_returns_false(self) -> None:
        self.assertFalse(low_priority_disabled())

    def test_empty_string_returns_false(self) -> None:
        # `export CLAUDE_ENCODING_NO_NICE=` in a stale rc file must NOT bypass.
        os.environ["CLAUDE_ENCODING_NO_NICE"] = ""
        self.assertFalse(low_priority_disabled())

    def test_any_nonempty_string_returns_true(self) -> None:
        # Lock the "any truthy string opts out" contract. "0", "false", "no",
        # "off" are all opt-OUT despite reading like "off" — documented in
        # SKILL.md so users know to `unset` rather than set to "0".
        for value in ("1", "true", "yes", "on", "0", "false", "no", "off", " "):
            with self.subTest(value=value):
                os.environ["CLAUDE_ENCODING_NO_NICE"] = value
                self.assertTrue(
                    low_priority_disabled(),
                    f"value {value!r} should be truthy → opt out",
                )


class PosixWrapBypassTest(_EnvSandboxMixin, unittest.TestCase):
    """`_posix.wrap_cmd_for_low_priority` must prepend nice by default and skip
    it when the env var is truthy. Module-level imports are pure-Python; the
    functions don't touch any POSIX-only syscall, so this runs on Windows too."""

    def test_default_wraps_with_nice_19(self) -> None:
        # Regression guard: the existing wrap semantics must survive the change.
        result = _posix.wrap_cmd_for_low_priority(["ffmpeg", "-i", "x"])
        self.assertEqual(result, ["nice", "-n", "19", "ffmpeg", "-i", "x"])

    def test_env_truthy_bypasses_wrap(self) -> None:
        os.environ["CLAUDE_ENCODING_NO_NICE"] = "1"
        result = _posix.wrap_cmd_for_low_priority(["ffmpeg", "-i", "x"])
        self.assertEqual(result, ["ffmpeg", "-i", "x"])

    def test_env_empty_string_keeps_default(self) -> None:
        # Empty string is falsy by os.environ.get() truth test; users who
        # `unset` or `export CLAUDE_ENCODING_NO_NICE=` must get default wrap.
        os.environ["CLAUDE_ENCODING_NO_NICE"] = ""
        result = _posix.wrap_cmd_for_low_priority(["ffmpeg", "-i", "x"])
        self.assertEqual(result, ["nice", "-n", "19", "ffmpeg", "-i", "x"])

    def test_bypass_returns_fresh_list(self) -> None:
        # The non-bypass branch already returns a new list (the *cmd splat).
        # The bypass branch must do the same — caller may mutate without
        # affecting our copy.
        cmd = ["ffmpeg", "-i", "x"]
        os.environ["CLAUDE_ENCODING_NO_NICE"] = "1"
        result = _posix.wrap_cmd_for_low_priority(cmd)
        self.assertEqual(result, cmd)
        self.assertIsNot(result, cmd, "bypass branch must not alias the input")

    def test_lifecycle_kwargs_unaffected_by_bypass(self) -> None:
        # `start_new_session=True` is what makes killpg work — it's a
        # lifecycle requirement on POSIX, NOT a priority knob. The env-var
        # opt-out is priority-only; it must not weaken cleanup guarantees.
        os.environ["CLAUDE_ENCODING_NO_NICE"] = "1"
        self.assertEqual(
            _posix.low_priority_popen_kwargs(),
            {"start_new_session": True},
        )


@unittest.skipUnless(
    sys.platform == "win32",
    "_windows.py imports subprocess.IDLE_PRIORITY_CLASS at module-load — "
    "Windows-only constant; the wider behavioural test runs on Scar/CI-Windows.",
)
class WindowsKwargsBypassTest(_EnvSandboxMixin, unittest.TestCase):
    """`_windows.low_priority_popen_kwargs` returns the IDLE_PRIORITY_CLASS
    creationflag by default and an empty dict when the env var is truthy.
    `wrap_cmd_for_low_priority` was already a no-op and stays one on both
    branches."""

    def setUp(self) -> None:
        super().setUp()
        from platform_compat import _windows  # local import: Win-only module
        self._windows = _windows

    def test_default_returns_idle_priority_creationflag(self) -> None:
        import subprocess
        kwargs = self._windows.low_priority_popen_kwargs()
        self.assertEqual(
            kwargs,
            {"creationflags": subprocess.IDLE_PRIORITY_CLASS},
        )

    def test_env_truthy_bypasses_kwargs(self) -> None:
        os.environ["CLAUDE_ENCODING_NO_NICE"] = "1"
        self.assertEqual(self._windows.low_priority_popen_kwargs(), {})

    def test_env_empty_string_keeps_default(self) -> None:
        import subprocess
        os.environ["CLAUDE_ENCODING_NO_NICE"] = ""
        self.assertEqual(
            self._windows.low_priority_popen_kwargs(),
            {"creationflags": subprocess.IDLE_PRIORITY_CLASS},
        )

    def test_wrap_cmd_remains_noop_both_branches(self) -> None:
        cmd = ["ffmpeg", "-i", "x"]
        self.assertEqual(self._windows.wrap_cmd_for_low_priority(cmd), cmd)
        os.environ["CLAUDE_ENCODING_NO_NICE"] = "1"
        self.assertEqual(self._windows.wrap_cmd_for_low_priority(cmd), cmd)


if __name__ == "__main__":
    unittest.main()

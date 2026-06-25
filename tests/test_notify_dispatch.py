"""examples/notify_dispatch.py — a stdlib-only "primary + fallback" notifier
wrapper. Runs an ordered list of notifier scripts in subprocesses (forwarding
the current env) and stops at the first success; exit 0 if ANY transport
delivered, non-zero only if ALL failed.

`parse_chain` and `run_chain` are the pure/injectable seams: `run_chain` takes
a fake `runner` so tests never spawn a real process or hit the network.
"""
from __future__ import annotations

import importlib.util
import os
import types
import unittest
from pathlib import Path
from unittest import mock

EXAMPLE = (Path(__file__).resolve().parent.parent
           / "examples" / "notify_dispatch.py")


def _load():
    spec = importlib.util.spec_from_file_location("notify_dispatch", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Runner:
    """Records argv per call and returns a scripted returncode (or raises)
    per call index, so we can model 'first fails, second succeeds' etc."""

    def __init__(self, results):
        # results: list of int returncodes or Exception instances to raise.
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        idx = len(self.calls) - 1
        outcome = self._results[idx]
        if isinstance(outcome, BaseException):
            raise outcome
        return types.SimpleNamespace(returncode=outcome)


class ParseChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_default_chain_is_ntfy_then_pushbullet(self) -> None:
        chain = self.mod.parse_chain({}, script_dir=Path("/ex"))
        self.assertEqual(len(chain), 2)
        self.assertTrue(chain[0].endswith("notify_ntfy.py"))
        self.assertTrue(chain[1].endswith("notify_pushbullet.py"))

    def test_env_chain_parsed_by_os_pathsep(self) -> None:
        raw = os.pathsep.join(["/a/one.py", "/b/two.py"])
        chain = self.mod.parse_chain(
            {"X265_NOTIFY_CHAIN": raw}, script_dir=Path("/ex"))
        self.assertEqual(chain, ["/a/one.py", "/b/two.py"])

    def test_env_chain_ignores_empty_segments(self) -> None:
        raw = os.pathsep.join(["/a/one.py", "", "  "])
        chain = self.mod.parse_chain(
            {"X265_NOTIFY_CHAIN": raw}, script_dir=Path("/ex"))
        self.assertEqual(chain, ["/a/one.py"])


class RunChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_first_success_wins_second_not_run(self) -> None:
        runner = _Runner([0, 0])
        rc = self.mod.run_chain(["/a.py", "/b.py"], env={},
                                python="py", runner=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.calls), 1)  # second notifier NOT spawned
        self.assertEqual(runner.calls[0], ["py", "/a.py"])

    def test_falls_through_to_second_on_first_failure(self) -> None:
        runner = _Runner([1, 0])
        rc = self.mod.run_chain(["/a.py", "/b.py"], env={},
                                python="py", runner=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.calls), 2)

    def test_all_fail_returns_nonzero(self) -> None:
        runner = _Runner([1, 2])
        rc = self.mod.run_chain(["/a.py", "/b.py"], env={},
                                python="py", runner=runner)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(runner.calls), 2)

    def test_spawn_error_treated_as_failure_chain_continues(self) -> None:
        # A notifier that can't even be spawned (OSError) must not abort the
        # dispatcher — it counts as a failure and the chain tries the next.
        runner = _Runner([FileNotFoundError("no py"), 0])
        rc = self.mod.run_chain(["/missing.py", "/b.py"], env={},
                                python="py", runner=runner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.calls), 2)

    def test_empty_chain_is_nonzero(self) -> None:
        runner = _Runner([])
        rc = self.mod.run_chain([], env={}, python="py", runner=runner)
        self.assertNotEqual(rc, 0)
        self.assertEqual(runner.calls, [])

    def test_value_error_spawn_also_continues(self) -> None:
        # Embedded-NUL argv raises ValueError from subprocess — same band.
        runner = _Runner([ValueError("nul"), 0])
        rc = self.mod.run_chain(["/x.py", "/b.py"], env={},
                                python="py", runner=runner)
        self.assertEqual(rc, 0)


class PerNotifierTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_two_chain_keeps_full_per_child_timeout(self) -> None:
        # 28/2 = 14, capped at 12 -> 12 each, 24s total < the 30s hook wrapper.
        self.assertEqual(self.mod._per_notifier_timeout(2), 12.0)

    def test_long_chain_shrinks_to_fit_budget(self) -> None:
        # 4 notifiers must not blow the 30s wrapper: 28/4 = 7 each (28s total).
        self.assertAlmostEqual(self.mod._per_notifier_timeout(4), 7.0)
        self.assertLess(self.mod._per_notifier_timeout(4) * 4, 30.0)

    def test_zero_chain_is_safe(self) -> None:
        self.assertEqual(self.mod._per_notifier_timeout(0), 12.0)


class DispatchAllFailLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_all_fail_appends_to_notify_log(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            logp = Path(td) / "n.log"
            with mock.patch.dict(os.environ,
                                 {"X265_NOTIFY_CHAIN": "/a.py",
                                  "X265_NOTIFY_LOG": str(logp),
                                  "X265_HOOK_EVENT": "job-end"}), \
                 mock.patch.object(self.mod, "run_chain", return_value=1):
                rc = self.mod.main()
            self.assertEqual(rc, 1)
            self.assertIn("all notifiers failed",
                          logp.read_text(encoding="utf-8"))


class StdlibOnlyTest(unittest.TestCase):
    def test_no_third_party_imports(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("import requests", text)
        self.assertIn("subprocess", text)


if __name__ == "__main__":
    unittest.main()

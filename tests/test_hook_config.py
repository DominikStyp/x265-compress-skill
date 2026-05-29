"""on_chunk_done hook config (de)serialization.

`parse_hook_spec` turns the CLI/queue value into an argv list. It fails LOUD on
bad config (a typo'd hook must surface at compress.py time, before any encode
starts) — but the sidecar reader `load_hook_sidecar` is the opposite: a missing
or corrupt sidecar at encode time returns None (no hook) rather than aborting a
multi-hour encode, because the hook is auxiliary.

The sidecar exists so a JSON array (which contains `"`) never has to be embedded
into a generated .bat/.sh — only the sidecar's *path* crosses that boundary.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules.hook_config import (  # noqa: E402
    load_hook_sidecar,
    load_hooks_sidecar,
    parse_hook_spec,
    write_hook_sidecar,
    write_hooks_sidecar,
)


class ParseHookSpecTest(unittest.TestCase):
    def test_json_array_parses_to_list(self) -> None:
        self.assertEqual(parse_hook_spec('["bash","/x/notify.sh"]'),
                         ["bash", "/x/notify.sh"])

    def test_json_array_with_interpreter_flags(self) -> None:
        # The exact Windows case the argv-list design exists for: -File starts
        # with '-' and would break nargs parsing, but is fine inside JSON.
        self.assertEqual(parse_hook_spec('["pwsh","-File","notify.ps1"]'),
                         ["pwsh", "-File", "notify.ps1"])

    def test_bare_token_wraps_to_single_element(self) -> None:
        self.assertEqual(parse_hook_spec("/usr/local/bin/notify"),
                         ["/usr/local/bin/notify"])

    def test_leading_trailing_whitespace_ignored(self) -> None:
        self.assertEqual(parse_hook_spec('  ["notify.exe"]  '), ["notify.exe"])

    def test_empty_or_whitespace_is_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_hook_spec("   ")

    def test_empty_json_array_is_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_hook_spec("[]")

    def test_non_string_item_is_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_hook_spec('["bash", 3]')

    def test_malformed_json_array_is_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_hook_spec('["bash"')

    def test_json_object_is_error_not_silently_a_bare_token(self) -> None:
        # A '{...}' value is a user mistake (object, not array); surface it
        # rather than treating the whole blob as one program name.
        with self.assertRaises(ValueError):
            parse_hook_spec('{"cmd": "notify"}')

    def test_nul_in_json_item_is_error(self) -> None:
        # A NUL makes subprocess.run raise ValueError at fire time (could kill a
        # worker slot). Reject it loud HERE so bad config fails before encoding.
        with self.assertRaises(ValueError):
            parse_hook_spec('["prog\\u0000x"]')

    def test_nul_in_bare_token_is_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_hook_spec("prog\x00x")


class SidecarRoundtripTest(unittest.TestCase):
    def test_write_then_load_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cmd = ["bash", "/x/pusher.sh"]
            path = write_hook_sidecar(Path(td), "myvid", cmd)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "myvid.hooks.json")
            self.assertEqual(load_hook_sidecar(path), cmd)

    def test_write_leaves_no_temp_file_behind(self) -> None:
        # Atomic write = temp + os.replace; a stray *.tmp would mean the
        # replace step was skipped (non-atomic) — guard against that.
        with tempfile.TemporaryDirectory() as td:
            write_hook_sidecar(Path(td), "myvid", ["notify"])
            leftovers = [p.name for p in Path(td).iterdir()
                         if p.name != "myvid.hooks.json"]
            self.assertEqual(leftovers, [])

    def test_load_missing_path_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(load_hook_sidecar(Path(td) / "absent.hooks.json"))

    def test_load_none_is_none(self) -> None:
        self.assertIsNone(load_hook_sidecar(None))

    def test_load_corrupt_sidecar_is_none_not_raise(self) -> None:
        # A hand-edited/corrupt sidecar must not abort an encode — degrade to
        # "no hook" rather than crash.
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "v.hooks.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_hook_sidecar(bad))

    def test_load_sidecar_without_command_key_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v.hooks.json"
            p.write_text('{"something_else": 1}', encoding="utf-8")
            self.assertIsNone(load_hook_sidecar(p))

    def test_load_sidecar_with_nul_item_is_none(self) -> None:
        # A hand-edited sidecar with a NUL must degrade to None, not arm the
        # worker-killing ValueError at fire time.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v.hooks.json"
            p.write_text('{"on_chunk_done": ["a\\u0000b"]}', encoding="utf-8")
            self.assertIsNone(load_hook_sidecar(p))


class MultiHookSidecarTest(unittest.TestCase):
    """The sidecar carries multiple independent hook commands keyed by name
    (`on_chunk_done`, `on_job_end`, …). Backward-compat: a sidecar produced
    by an earlier version that only stored `on_chunk_done` must still load,
    and a single-key writer must produce a sidecar an old reader can still
    parse."""

    def test_write_both_keys_then_load_both(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = write_hooks_sidecar(
                Path(td), "v",
                on_chunk_done=["bash", "/chunk.sh"],
                on_job_end=["python3", "/job.py"],
            )
            self.assertEqual(path.name, "v.hooks.json")
            commands = load_hooks_sidecar(path)
            self.assertEqual(commands,
                             {"on_chunk_done": ["bash", "/chunk.sh"],
                              "on_job_end": ["python3", "/job.py"]})

    def test_write_subset_loads_only_present_keys(self) -> None:
        # Each hook is independent; a user may configure one and not the other.
        with tempfile.TemporaryDirectory() as td:
            path = write_hooks_sidecar(Path(td), "v",
                                       on_job_end=["py", "/x.py"])
            self.assertEqual(load_hooks_sidecar(path),
                             {"on_job_end": ["py", "/x.py"]})

    def test_write_no_hooks_returns_none_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = write_hooks_sidecar(Path(td), "v")
            self.assertIsNone(path)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_load_v181_sidecar_still_works(self) -> None:
        # A sidecar written by v1.8.1's `write_hook_sidecar` (single key) must
        # still load — both via the old single-key reader (back-compat for
        # encode_resumable's existing call site) and via the new multi-key
        # reader (which sees `on_chunk_done` and no `on_job_end`).
        import json
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v.hooks.json"
            path.write_text(json.dumps({"on_chunk_done": ["bash", "/x.sh"]}),
                            encoding="utf-8")
            self.assertEqual(load_hook_sidecar(path), ["bash", "/x.sh"])
            self.assertEqual(load_hooks_sidecar(path),
                             {"on_chunk_done": ["bash", "/x.sh"]})

    def test_load_corrupt_sidecar_returns_none(self) -> None:
        # Same degrade-to-none contract as load_hook_sidecar.
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "v.hooks.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_hooks_sidecar(bad))

    def test_load_drops_invalid_hook_keys_keeps_valid_ones(self) -> None:
        # A sidecar with one valid and one invalid entry: the valid hook
        # still loads, the invalid one is silently dropped (degrade — never
        # abort the encode). NUL embedded in the invalid one would arm the
        # worker-killing ValueError if it slipped through.
        import json
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v.hooks.json"
            path.write_text(json.dumps({
                "on_chunk_done": ["bash", "/ok.sh"],
                "on_job_end": ["a\x00b"],
            }), encoding="utf-8")
            commands = load_hooks_sidecar(path)
            self.assertEqual(commands, {"on_chunk_done": ["bash", "/ok.sh"]})

    def test_write_hooks_sidecar_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_hooks_sidecar(Path(td), "v",
                                on_chunk_done=["a"], on_job_end=["b"])
            leftovers = [p.name for p in Path(td).iterdir()
                         if p.name != "v.hooks.json"]
            self.assertEqual(leftovers, [])


class BackCompatSingleKeyWriterTest(unittest.TestCase):
    """The pre-existing `write_hook_sidecar(tmp_dir, stem, command)` API
    must keep behaving identically — external callers may still use it."""

    def test_write_hook_sidecar_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cmd = ["bash", "/x/pusher.sh"]
            path = write_hook_sidecar(Path(td), "myvid", cmd)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "myvid.hooks.json")
            # Old reader still returns the argv.
            self.assertEqual(load_hook_sidecar(path), cmd)
            # New reader sees it as a one-key dict.
            self.assertEqual(load_hooks_sidecar(path),
                             {"on_chunk_done": cmd})


if __name__ == "__main__":
    unittest.main()

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
    parse_hook_spec,
    write_hook_sidecar,
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


if __name__ == "__main__":
    unittest.main()

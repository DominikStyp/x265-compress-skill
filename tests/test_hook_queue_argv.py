"""queue.json `on_chunk_done` -> compress.py argv.

The queue->compress.py hop is a real subprocess arg LIST (no shell), so the hook
command travels safely as a single JSON-array argument that compress.py's
parse_hook_spec reads back. A list is emitted verbatim; a bare string is wrapped
to a 1-element array so both queue spellings reach compress.py identically.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from queue_modules.job_schema import (  # noqa: E402
    VALID_KEYS,
    build_compress_argv,
    merge_job,
)


class OnChunkDoneIsValidKeyTest(unittest.TestCase):
    def test_key_is_recognised_not_dropped_as_typo(self) -> None:
        self.assertIn("on_chunk_done", VALID_KEYS)
        merged = merge_job({}, {"input": "a.mp4", "on_chunk_done": ["notify"]})
        self.assertEqual(merged["on_chunk_done"], ["notify"])

    def test_defaults_supply_hook_when_job_omits_it(self) -> None:
        # defaults block -> applies to every job (the "all files" case).
        merged = merge_job({"on_chunk_done": ["bash", "/x/n.sh"]},
                           {"input": "a.mp4"})
        self.assertEqual(merged["on_chunk_done"], ["bash", "/x/n.sh"])

    def test_per_job_overrides_defaults(self) -> None:
        # per-job override -> the "per file" case.
        merged = merge_job({"on_chunk_done": ["a"]},
                           {"input": "a.mp4", "on_chunk_done": ["b"]})
        self.assertEqual(merged["on_chunk_done"], ["b"])


class BuildArgvHookTest(unittest.TestCase):
    def test_list_emitted_as_json_array(self) -> None:
        argv = build_compress_argv(
            {"input": "a.mp4", "on_chunk_done": ["bash", "/x/n.sh"]})
        self.assertIn("--on-chunk-done", argv)
        val = argv[argv.index("--on-chunk-done") + 1]
        self.assertEqual(json.loads(val), ["bash", "/x/n.sh"])

    def test_string_wrapped_to_single_element_array(self) -> None:
        argv = build_compress_argv(
            {"input": "a.mp4", "on_chunk_done": "/x/n.sh"})
        val = argv[argv.index("--on-chunk-done") + 1]
        self.assertEqual(json.loads(val), ["/x/n.sh"])

    def test_absent_when_key_missing(self) -> None:
        argv = build_compress_argv({"input": "a.mp4"})
        self.assertNotIn("--on-chunk-done", argv)

    def test_falsy_value_disables_inherited_default(self) -> None:
        # The supported way to opt one job out of a `defaults` hook: a falsy
        # override emits NO flag (rather than crashing the job at compress.py).
        for falsy in (None, [], ""):
            argv = build_compress_argv(
                {"input": "a.mp4", "on_chunk_done": falsy})
            self.assertNotIn("--on-chunk-done", argv,
                             f"falsy {falsy!r} should disable the hook")


if __name__ == "__main__":
    unittest.main()

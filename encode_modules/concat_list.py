"""Shared `concat.txt` line builder for ffmpeg's concat demuxer.

ffmpeg's concat demuxer parses each `file '<path>'` line as a single-quoted
path. Inside the quotes, the ONLY special character is the apostrophe itself:
to embed one you close the quote, emit `\\'`, then reopen — the documented
escape sequence is `'\\''`.

We had two list writers — `chunking.concat_chunks` and the source-patcher's
segment-list builder — and both interpolated paths directly into the line
template. Any source whose workdir contained `'` (any possessive: `O'Brien`,
`DP'd`, …) wrote a broken concat.txt and ffmpeg failed at finalize with exit
254. Centralizing the formatter here means neither writer can drift from the
correct escape, and a future TS/segment list inherits the fix for free.

Other shell metacharacters (space, `&`, `;`, `$`, `!`, parens, brackets) are
LITERAL inside single quotes for the demuxer, so we deliberately don't touch
them — adding extra escapes would corrupt the path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


def escape_concat_path(path: str) -> str:
    """Escape apostrophes for safe interpolation inside `file '…'`.

    `O'Brien/foo.mkv` → `O'\\''Brien/foo.mkv`. Idempotent for paths that
    contain no apostrophe at all (most of them)."""
    return path.replace("'", "'\\''")


def concat_list_lines(chunks: Iterable[Path]) -> str:
    """Build the full concat.txt body for `chunks`.

    Each line is `file '<absolute-posix-path>'` with apostrophes escaped per
    the ffmpeg demuxer rules. The trailing newline matches the prior
    hand-rolled writers byte-for-byte (so an existing concat.txt left on
    disk from a resumed run keeps the same content)."""
    return "\n".join(
        f"file '{escape_concat_path(c.resolve().as_posix())}'"
        for c in chunks
    ) + "\n"

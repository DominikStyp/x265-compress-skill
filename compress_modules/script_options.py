"""`ScriptOptions` — the encode-behaviour flags the script generator threads
verbatim from `write_script()` down through the per-OS renderers and
`_build_extra_args`.

Before this dataclass existed, ~17 keyword arguments were passed by name
through `write_script` -> `_render_windows_script` / `_render_posix_script`
-> `_build_extra_args`. Adding one encoder flag meant editing ~6 signatures
in lockstep. Packing the flags into one frozen object collapses that
threading to a single parameter while keeping `write_script`'s public kwargs
API (and the `write_bat` alias) byte-for-byte unchanged.

Only flags that ride through *unchanged* live here. The identity/context
arguments (`info`, `plan`, `source_path`) and the shell-specific derived
values (`line_cont`, `quote_value`) are NOT options — they're computed or
selected per-OS inside the renderers, so they stay explicit parameters.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScriptOptions:
    """The encode-behaviour flags forwarded unchanged through the renderers.

    Field order and defaults mirror `write_script`'s keyword parameters so the
    `ScriptOptions(**...)` pack in `write_script` reads as a 1:1 transcription
    and a future flag is a one-line addition here plus one in `write_script`'s
    signature — no renderer signature churn."""

    resumable: bool = False
    segment_seconds: int = 60
    parallel: int = 1
    max_output_bytes: int | None = None
    max_size_percent: float | None = None
    auto_fix_choke: bool = False
    no_pre_flight_scan: bool = False
    auto_patch_source: bool = False
    max_patch_seconds: float = 10.0
    no_report: bool = False
    no_pause: bool = False
    on_chunk_done: list[str] | None = None
    on_job_end: list[str] | None = None
    on_file_complete: list[str] | None = None
    done_dir: str | None = None
    visual_quality_threshold: float | None = None
    no_log_chunk_metrics: bool = False

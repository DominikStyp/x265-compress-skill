"""Generate the encoder shell script. OS-aware — produces `.bat` on
Windows and `.sh` on POSIX (macOS / Linux).

This module replaces the previous `bat_writer.py`. The Win32-specific and
POSIX-specific text lives in `_bat_templates.py` and `_sh_templates.py`
respectively; this file:

  1. Picks the template + extension based on `platform_compat.IS_WINDOWS`.
  2. Calls the per-OS renderer, which pulls its substitution dict + escaping
     from `_substitutions.py` (cmd.exe takes raw paths inside `set "..."`;
     bash takes single-quoted literals via `_sh_quote`).
  3. Writes the script + chmod +x on POSIX.

The per-shell value-escaping and the variable trailing-flags block live in
`_substitutions.py`; this file owns the orchestration, the hook-sidecar
fragments, and the public `write_script()` API (platform-agnostic).
"""
from __future__ import annotations

import os
from pathlib import Path

from encode_modules.hook_config import write_hooks_sidecar
from encode_modules.log_paths import logs_dir, per_encode_report_path
from platform_compat import IS_WINDOWS

from . import _bat_templates as bat_t
from . import _sh_templates as sh_t
from ._legacy_report_call import (
    build_legacy_report_call_posix as _build_legacy_report_call_posix,
    build_legacy_report_call_win as _build_legacy_report_call_win,
)
from ._substitutions import (
    _build_extra_args,
    _cmd_set_escape,
    _posix_substitutions,
    _sh_quote,
    _win_substitutions,
    fmt_duration,  # re-exported below — tests/test_formatting.py imports it here
)
from .plan import compress_workdir, EncodePlan, SCRIPT_EXTENSION
from .probe import SourceInfo
from .script_options import ScriptOptions

# SCRIPT_EXTENSION is re-exported from plan.py (its single source of truth) for
# the handful of callers that read it off this module (e.g. _smoke_test.py).
# fmt_duration is re-exported from _substitutions.py for tests/test_formatting.
__all__ = ["write_script", "SCRIPT_EXTENSION", "fmt_duration"]


# --- on_chunk_done hook fragments ------------------------------------------
#
# The hook argv (which contains `"`) lives in a JSON sidecar; only the sidecar
# PATH is baked into the script. A path can't contain `"`, so it rides the same
# proven stash pattern as the source/output paths — no new cross-OS quoting.
#
# The sidecar is keyed on the SOURCE stem (like the workdir `.compress_<stem>`),
# so a resumed run re-reads the same file. Two different sources that share a
# stem in one output dir already collide at the workdir level — pre-existing,
# not introduced here.

def _hook_fragments_win(tmp_dir: Path, stem: str,
                       on_chunk_done: list[str] | None,
                       on_job_end: list[str] | None,
                       on_file_complete: list[str] | None
                       ) -> tuple[str, str]:
    """(setup, flag) for the .bat: a `set` stashing the sidecar path + the
    --hooks-config flag referencing it. ("", "") when no hook is configured.

    Sidecar carries all hooks side-by-side (when configured) so a single
    --hooks-config flag suffices — `encode_resumable.py` reads each key."""
    sidecar = write_hooks_sidecar(tmp_dir, stem,
                                  on_chunk_done=on_chunk_done,
                                  on_job_end=on_job_end,
                                  on_file_complete=on_file_complete)
    if sidecar is None:
        return "", ""
    # Sidecar path goes into `set "VAR=..."`, so double any `%` (the stem can
    # contain one, e.g. ".compress_70% Hell.hooks.json").
    return (f'\nset "_SKILL_HOOKS={_cmd_set_escape(str(sidecar))}"',
            ' ^\n  --hooks-config "%_SKILL_HOOKS%"')


def _hook_fragments_posix(tmp_dir: Path, stem: str,
                         on_chunk_done: list[str] | None,
                         on_job_end: list[str] | None,
                         on_file_complete: list[str] | None
                         ) -> tuple[str, str]:
    """POSIX equivalent — sidecar path single-quoted via _sh_quote."""
    sidecar = write_hooks_sidecar(tmp_dir, stem,
                                  on_chunk_done=on_chunk_done,
                                  on_job_end=on_job_end,
                                  on_file_complete=on_file_complete)
    if sidecar is None:
        return "", ""
    return (f"\n_SKILL_HOOKS={_sh_quote(str(sidecar))}",
            ' \\\n  --hooks-config "${_SKILL_HOOKS}"')


# The substitution-dict builders, shell-quoting helpers, and the variable
# trailing-flags block (`_build_extra_args`) live in ``_substitutions.py`` so
# this module stays a slim orchestrator under the 500-line cap. They're
# imported at the top under the same names the renderers use, so the call
# sites below are unchanged.


# Legacy single-pass report-call helpers were extracted to
# ``_legacy_report_call.py`` so this module stays under the 500-line cap. The
# imports above expose them under the same names the renderers used so the
# call sites are unchanged.


# --- Public API -------------------------------------------------------------

def write_script(info: SourceInfo, plan: EncodePlan, source_path: Path,
                *, resumable: bool = False, segment_seconds: int = 60,
                parallel: int = 1,
                max_output_bytes: int | None = None,
                max_size_percent: float | None = None,
                auto_fix_choke: bool = False,
                no_pre_flight_scan: bool = False,
                auto_patch_source: bool = False,
                max_patch_seconds: float = 10.0,
                no_report: bool = False,
                no_pause: bool = False,
                on_chunk_done: list[str] | None = None,
                on_job_end: list[str] | None = None,
                on_file_complete: list[str] | None = None,
                done_dir: str | None = None,
                visual_quality_threshold: float | None = None,
                no_log_chunk_metrics: bool = False) -> None:
    """Render the encoder script for the current OS and write it to
    `plan.script_path`. On Windows that's a `.bat`; on POSIX it's a `.sh`.

    `resumable=True` selects the chunked/resumable template (queue mode
    default); False uses the single-pass template. The two flavors share
    common substitutions but diverge on what they put into the command body.
    """
    skill_dir = Path(__file__).resolve().parent.parent
    output_path_obj = Path(plan.output_path)
    tmp_dir = output_path_obj.parent / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # v1.19.0: report + hooks sidecar live under logs/ (not .tmp/). The
    # chunked workdir stays in .tmp/ — that's mid-flight scratch, wiped
    # on cleanup; logs are persistent artefacts that survive.
    sidecar_dir = logs_dir(output_path_obj.parent)
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    report_md_path = per_encode_report_path(output_path_obj)

    # Pack the encode-behaviour flags into one object so the renderers and
    # `_build_extra_args` take a single parameter instead of re-threading ~17
    # of them by name. The public kwargs above stay the API; this is a 1:1
    # transcription (see ScriptOptions for why each field belongs here).
    opts = ScriptOptions(
        resumable=resumable, segment_seconds=segment_seconds,
        parallel=parallel,
        max_output_bytes=max_output_bytes,
        max_size_percent=max_size_percent,
        auto_fix_choke=auto_fix_choke,
        no_pre_flight_scan=no_pre_flight_scan,
        auto_patch_source=auto_patch_source,
        max_patch_seconds=max_patch_seconds,
        no_report=no_report,
        no_pause=no_pause,
        on_chunk_done=on_chunk_done,
        on_job_end=on_job_end,
        on_file_complete=on_file_complete,
        done_dir=done_dir,
        visual_quality_threshold=visual_quality_threshold,
        no_log_chunk_metrics=no_log_chunk_metrics,
    )

    render = _render_windows_script if IS_WINDOWS else _render_posix_script
    content = render(
        info, plan, source_path, skill_dir, tmp_dir,
        sidecar_dir, report_md_path, opts,
    )

    out_path = Path(plan.script_path)
    # Write without BOM. cmd.exe + chcp 65001 handles UTF-8; bash reads
    # UTF-8 natively. A BOM at the top of a .bat misparses on some cmd
    # versions; same for some POSIX shebang lines.
    #
    # Line endings: cmd.exe REQUIRES CRLF in .bat files. With bare LF it
    # reads lines at the wrong byte offsets and drops a variable number of
    # leading characters per line (e.g. `chcp`->`cp`, `title`->`tle`,
    # `REM`->`EM`), producing "'cp' is not recognized" errors. POSIX shells
    # want LF. Normalize per-OS regardless of what the templates contain.
    if IS_WINDOWS:
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    out_path.write_bytes(content.encode("utf-8"))
    if not IS_WINDOWS:
        # +x so users can run the script directly. ~/sources may be on a
        # filesystem that doesn't honour mode bits (FAT/SMB) — chmod
        # failures are non-fatal; users can fall back to `bash script.sh`.
        try:
            mode = os.stat(out_path).st_mode
            os.chmod(out_path, mode | 0o111)
        except OSError:
            pass


def _render_windows_script(info, plan, source_path, skill_dir, tmp_dir,
                          sidecar_dir, report_md_path,
                          opts: ScriptOptions) -> str:
    common = _win_substitutions(info, plan, source_path, no_pause=opts.no_pause)
    if opts.resumable:
        workdir = compress_workdir(tmp_dir, source_path)
        parallel_label = (f"{opts.parallel} chunks in parallel"
                          if opts.parallel > 1 else "one at a time")
        extra_args = _build_extra_args(
            opts, line_cont="^",
            source_path=source_path, info=info,
            # cmd.exe parses `--done-dir "C:\path with spaces"`; wrap in dq
            # and double `%` inside. cmd doesn't strip backslashes — Path
            # separators survive.
            quote_value=lambda v: f'"{_cmd_set_escape(v)}"',
        )
        # `>` must be escaped as `^>` and `%` as `%%` because this string
        # is baked into a literal `echo` line. Otherwise cmd.exe parses
        # the `>` as redirection and creates a file named after the
        # threshold value (e.g. `80.0`).
        threshold_label = (
            f"abort if projected output ^> {opts.max_size_percent:.1f}%% of source"
            if opts.max_size_percent is not None else "off"
        )
        no_report_flag = " ^\n  --no-report" if opts.no_report else ""
        hooks_setup, hooks_flag = _hook_fragments_win(
            sidecar_dir, source_path.stem, opts.on_chunk_done, opts.on_job_end,
            opts.on_file_complete)
        return bat_t.RESUMABLE_BAT_TEMPLATE.format(
            resumable_script=_cmd_set_escape(str(skill_dir / "encode_resumable.py")),
            workdir=_cmd_set_escape(str(workdir)),
            segment_seconds=opts.segment_seconds, parallel=opts.parallel,
            parallel_label=parallel_label,
            extra_args=extra_args,
            threshold_label=threshold_label,
            no_report_flag=no_report_flag,
            hooks_setup=hooks_setup, hooks_flag=hooks_flag,
            **common,
        )
    return bat_t.BAT_TEMPLATE.format(
        progress_script=_cmd_set_escape(str(skill_dir / "progress.py")),
        report_script=_cmd_set_escape(str(skill_dir / "report.py")),
        report_md_path=_cmd_set_escape(str(report_md_path)),
        report_call=_build_legacy_report_call_win(plan, opts.max_size_percent,
                                                  no_report=opts.no_report),
        **common,
    )


def _render_posix_script(info, plan, source_path, skill_dir, tmp_dir,
                        sidecar_dir, report_md_path,
                        opts: ScriptOptions) -> str:
    common = _posix_substitutions(info, plan, source_path, no_pause=opts.no_pause)
    # Skill-script paths are bash variable values — quote them too.
    resumable_script = _sh_quote(str(skill_dir / "encode_resumable.py"))
    progress_script = _sh_quote(str(skill_dir / "progress.py"))
    report_script = _sh_quote(str(skill_dir / "report.py"))

    if opts.resumable:
        workdir = compress_workdir(tmp_dir, source_path)
        parallel_label = (f"{opts.parallel} chunks in parallel"
                          if opts.parallel > 1 else "one at a time")
        extra_args = _build_extra_args(
            opts, line_cont="\\",
            source_path=source_path, info=info,
            # bash single-quote escape for paths with spaces / `&` / `(` etc.
            quote_value=_sh_quote,
        )
        threshold_label = (
            f"abort if projected output > {opts.max_size_percent:.1f}% of source"
            if opts.max_size_percent is not None else "off"
        )
        no_report_flag = " \\\n  --no-report" if opts.no_report else ""
        hooks_setup, hooks_flag = _hook_fragments_posix(
            sidecar_dir, source_path.stem, opts.on_chunk_done, opts.on_job_end,
            opts.on_file_complete)
        return sh_t.RESUMABLE_SH_TEMPLATE.format(
            resumable_script=resumable_script,
            workdir=_sh_quote(str(workdir)),
            segment_seconds=opts.segment_seconds, parallel=opts.parallel,
            parallel_label=parallel_label,
            extra_args=extra_args,
            threshold_label=threshold_label,
            no_report_flag=no_report_flag,
            hooks_setup=hooks_setup, hooks_flag=hooks_flag,
            **common,
        )
    return sh_t.SH_TEMPLATE.format(
        progress_script=progress_script,
        report_script=report_script,
        report_md_path=_sh_quote(str(report_md_path)),
        report_call=_build_legacy_report_call_posix(plan, opts.max_size_percent,
                                                    no_report=opts.no_report),
        **common,
    )


# Back-compat: keep the old name `write_bat` as an alias so other callers
# that still reference it (e.g. tests or external scripts) keep working.
write_bat = write_script

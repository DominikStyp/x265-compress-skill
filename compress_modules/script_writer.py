"""Generate the encoder shell script. OS-aware — produces `.bat` on
Windows and `.sh` on POSIX (macOS / Linux).

This module replaces the previous `bat_writer.py`. The Win32-specific and
POSIX-specific text lives in `_bat_templates.py` and `_sh_templates.py`
respectively; this file:

  1. Picks the template + extension based on `platform_compat.IS_WINDOWS`.
  2. Builds the substitution dict the way each shell wants its values
     (cmd.exe takes raw paths inside `set "..."`; bash takes single-quoted
     literals via `_sh_quote`).
  3. Builds the variable trailing-flags block with the right line-continuation
     character (`^` for cmd, `\\` for bash).
  4. Writes the script + chmod +x on POSIX.

The OS-specific stuff is contained to the three helper functions
`_win_substitutions`, `_posix_substitutions`, and `_build_extra_args`.
The public `write_script()` API is platform-agnostic.
"""
from __future__ import annotations

import os
from pathlib import Path

from platform_compat import IS_WINDOWS

from . import _bat_templates as bat_t
from . import _sh_templates as sh_t
from .plan import EncodePlan
from .probe import SourceInfo


# Extension to write. compress.py resolves this for the .bat_path in plan.py.
SCRIPT_EXTENSION = ".bat" if IS_WINDOWS else ".sh"


# --- Quoting helpers --------------------------------------------------------

def _sh_quote(s: str) -> str:
    """Single-quote a string for safe inclusion in a bash variable
    assignment. POSIX shells treat anything inside single quotes as literal
    EXCEPT the single quote itself, which must be escaped with the
    close-escape-reopen idiom (`'\\''`)."""
    return "'" + s.replace("'", "'\\''") + "'"


def _cmd_title_escape(s: str) -> str:
    """Escape cmd meta-chars for the title-context. cmd parses unquoted
    `title` args, so `&` in a filename becomes a command separator. `^`
    must be escaped first so we don't double-escape our own escape character."""
    return s.replace("^", "^^").replace("&", "^&").replace("%", "%%")


def _bash_title_escape(s: str) -> str:
    """Escape for embedding in a bash printf single-quoted format string.
    Title goes into `printf '\\033]0;TITLE\\007'`; we just need to handle
    embedded single quotes (close-escape-reopen) — every other character
    is literal inside single quotes."""
    return s.replace("'", "'\\''")


# --- Time formatting -------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """Format seconds as `H:MM:SS` for the script's pre-encode summary."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"


def _safe_audio_codecs(values) -> str:
    """Strip quotes/newlines so values are safe to embed in the banner echo
    lines. Defensive — ffprobe output is already well-formed."""
    safe = [str(v).replace('"', "'").replace("\n", " ") for v in values]
    return ", ".join(safe) or "none"


# --- Substitution-dict builders -------------------------------------------

def _common_fields(info: SourceInfo, plan: EncodePlan) -> dict:
    """Fields used identically in both Windows and POSIX templates."""
    return dict(
        codec=info.codec,
        width=info.width, height=info.height,
        fps=f"{info.fps:.3f}".rstrip("0").rstrip("."),
        src_kbps=info.video_bitrate_kbps,
        bit_depth=info.bit_depth,
        hdr_tag=" HDR" if info.is_hdr else "",
        preset=plan.preset, crf=plan.crf, pix_fmt_out=plan.pix_fmt_out,
        x265_params=":".join(plan.x265_params),
        audio_codecs=_safe_audio_codecs(info.audio_codecs),
        duration=f"{info.duration_sec:.3f}",
        duration_str=fmt_duration(info.duration_sec),
    )


def _win_substitutions(info: SourceInfo, plan: EncodePlan, source_path: Path,
                      *, no_pause: bool) -> dict:
    """Build the Windows substitution dict. Paths embedded raw inside cmd
    `set "..."` syntax — cmd handles `&`, `(`, `)`, `!`, `^` etc. correctly
    in that context."""
    base = source_path.stem
    common = _common_fields(info, plan)
    return dict(
        common,
        base=base,
        base_title=_cmd_title_escape(base),
        input_path=str(source_path),
        output_path=plan.output_path,
        # `%` escape for the cmd `echo` line that prints estimated_reduction.
        estimated_reduction=plan.estimated_reduction.replace("%", "%%"),
        pause_line="" if no_pause else "pause",
    )


def _posix_substitutions(info: SourceInfo, plan: EncodePlan, source_path: Path,
                        *, no_pause: bool) -> dict:
    """Build the POSIX substitution dict. Paths are pre-quoted via
    `_sh_quote` so the variable assignments embed safely regardless of
    spaces, `&`, `[`, `]`, `$`, `!` in filenames."""
    base = source_path.stem
    common = _common_fields(info, plan)
    pause = ('read -n1 -s -r -p "Press any key to continue..."; echo'
             if not no_pause else "")
    return dict(
        common,
        base=base,
        base_title=_bash_title_escape(base),
        input_path=_sh_quote(str(source_path)),
        # plan.output_path is used both as a value (need quoting) and as
        # a label in echo (just embed as bash literal, no quoting needed
        # since it's inside the printf format which is single-quoted).
        output_path=_sh_quote(plan.output_path),
        estimated_reduction=plan.estimated_reduction,
        pause_line=pause,
    )


# --- extra_args block (line-continuation differs by shell) ------------------

def _build_extra_args(*, line_cont: str,
                     max_output_bytes: int | None,
                     auto_fix_choke: bool,
                     no_pre_flight_scan: bool,
                     auto_patch_source: bool,
                     max_patch_seconds: float,
                     source_path: Path,
                     info: SourceInfo) -> str:
    """Build the variable trailing-flags block appended to the encoder
    command line. `line_cont` is the shell's line-continuation character
    (`^` for cmd.exe, `\\` for bash)."""
    extra = ""
    if max_output_bytes is not None:
        extra += f" {line_cont}\n  --max-output-bytes {max_output_bytes}"
        extra += f" {line_cont}\n  --source-bytes {source_path.stat().st_size}"
        extra += f" {line_cont}\n  --total-duration-seconds {info.duration_sec:.3f}"
    if auto_fix_choke:
        extra += f" {line_cont}\n  --auto-fix-choke"
    if no_pre_flight_scan:
        extra += f" {line_cont}\n  --no-pre-flight-scan"
    if auto_patch_source:
        extra += f" {line_cont}\n  --auto-patch-source"
        extra += f" {line_cont}\n  --max-patch-seconds {max_patch_seconds}"
    return extra


def _build_legacy_report_call_win(plan: EncodePlan,
                                  max_size_percent: float | None,
                                  *, no_report: bool) -> str:
    """The non-resumable .bat has no Python orchestrator inside it, so we
    inject a report.py CLI call at the success branch."""
    if no_report:
        return ""
    mx_part = (f" --max-size-percent {max_size_percent:.1f}"
               if max_size_percent is not None else "")
    return (
        'for /f %%S in (\'powershell -NoProfile -Command '
        '"[int]((Get-Date) - [datetime]\'%_START_ISO%\').TotalSeconds"\') '
        'do set _ELAPSED=%%S\n'
        f'python -u "%_SKILL_REPORT%" single "%_SKILL_REPORT_MD%" '
        f'"%_SKILL_IN%" "%_SKILL_OUT%" --crf {plan.crf} '
        f'--preset {plan.preset}{mx_part} '
        f'--elapsed-sec %_ELAPSED% --status ok\n'
    )


def _build_legacy_report_call_posix(plan: EncodePlan,
                                    max_size_percent: float | None,
                                    *, no_report: bool) -> str:
    """POSIX equivalent of the legacy report-call. `date +%s` gives epoch
    seconds; we diff against the start timestamp captured at the top of
    the script."""
    if no_report:
        return ""
    mx_part = (f" --max-size-percent {max_size_percent:.1f}"
               if max_size_percent is not None else "")
    return (
        '\n    _ELAPSED=$(( $(date +%s) - _START_TS ))\n'
        f'    python3 -u "${{_SKILL_REPORT}}" single "${{_SKILL_REPORT_MD}}" '
        f'"${{_SKILL_IN}}" "${{_SKILL_OUT}}" --crf {plan.crf} '
        f'--preset {plan.preset}{mx_part} '
        f'--elapsed-sec ${{_ELAPSED}} --status ok'
    )


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
                no_pause: bool = False) -> None:
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
    report_md_path = tmp_dir / f"{output_path_obj.stem}.report.md"

    if IS_WINDOWS:
        content = _render_windows_script(
            info, plan, source_path, skill_dir, tmp_dir, report_md_path,
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
        )
    else:
        content = _render_posix_script(
            info, plan, source_path, skill_dir, tmp_dir, report_md_path,
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
        )

    out_path = Path(plan.script_path)
    # Write without BOM. cmd.exe + chcp 65001 handles UTF-8; bash reads
    # UTF-8 natively. A BOM at the top of a .bat misparses on some cmd
    # versions; same for some POSIX shebang lines.
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
                          report_md_path, *, resumable, segment_seconds,
                          parallel, max_output_bytes, max_size_percent,
                          auto_fix_choke, no_pre_flight_scan,
                          auto_patch_source, max_patch_seconds,
                          no_report, no_pause) -> str:
    common = _win_substitutions(info, plan, source_path, no_pause=no_pause)
    if resumable:
        workdir = tmp_dir / f".compress_{source_path.stem}"
        parallel_label = (f"{parallel} chunks in parallel"
                          if parallel > 1 else "one at a time")
        extra_args = _build_extra_args(
            line_cont="^",
            max_output_bytes=max_output_bytes,
            auto_fix_choke=auto_fix_choke,
            no_pre_flight_scan=no_pre_flight_scan,
            auto_patch_source=auto_patch_source,
            max_patch_seconds=max_patch_seconds,
            source_path=source_path, info=info,
        )
        # `>` must be escaped as `^>` and `%` as `%%` because this string
        # is baked into a literal `echo` line. Otherwise cmd.exe parses
        # the `>` as redirection and creates a file named after the
        # threshold value (e.g. `80.0`).
        threshold_label = (
            f"abort if projected output ^> {max_size_percent:.1f}%% of source"
            if max_size_percent is not None else "off"
        )
        no_report_flag = " ^\n  --no-report" if no_report else ""
        return bat_t.RESUMABLE_BAT_TEMPLATE.format(
            resumable_script=str(skill_dir / "encode_resumable.py"),
            workdir=str(workdir),
            segment_seconds=segment_seconds, parallel=parallel,
            parallel_label=parallel_label,
            extra_args=extra_args,
            threshold_label=threshold_label,
            no_report_flag=no_report_flag,
            **common,
        )
    return bat_t.BAT_TEMPLATE.format(
        progress_script=str(skill_dir / "progress.py"),
        report_script=str(skill_dir / "report.py"),
        report_md_path=str(report_md_path),
        report_call=_build_legacy_report_call_win(plan, max_size_percent,
                                                  no_report=no_report),
        **common,
    )


def _render_posix_script(info, plan, source_path, skill_dir, tmp_dir,
                        report_md_path, *, resumable, segment_seconds,
                        parallel, max_output_bytes, max_size_percent,
                        auto_fix_choke, no_pre_flight_scan,
                        auto_patch_source, max_patch_seconds,
                        no_report, no_pause) -> str:
    common = _posix_substitutions(info, plan, source_path, no_pause=no_pause)
    # Skill-script paths are bash variable values — quote them too.
    resumable_script = _sh_quote(str(skill_dir / "encode_resumable.py"))
    progress_script = _sh_quote(str(skill_dir / "progress.py"))
    report_script = _sh_quote(str(skill_dir / "report.py"))

    if resumable:
        workdir = tmp_dir / f".compress_{source_path.stem}"
        parallel_label = (f"{parallel} chunks in parallel"
                          if parallel > 1 else "one at a time")
        extra_args = _build_extra_args(
            line_cont="\\",
            max_output_bytes=max_output_bytes,
            auto_fix_choke=auto_fix_choke,
            no_pre_flight_scan=no_pre_flight_scan,
            auto_patch_source=auto_patch_source,
            max_patch_seconds=max_patch_seconds,
            source_path=source_path, info=info,
        )
        threshold_label = (
            f"abort if projected output > {max_size_percent:.1f}% of source"
            if max_size_percent is not None else "off"
        )
        no_report_flag = " \\\n  --no-report" if no_report else ""
        return sh_t.RESUMABLE_SH_TEMPLATE.format(
            resumable_script=resumable_script,
            workdir=_sh_quote(str(workdir)),
            segment_seconds=segment_seconds, parallel=parallel,
            parallel_label=parallel_label,
            extra_args=extra_args,
            threshold_label=threshold_label,
            no_report_flag=no_report_flag,
            **common,
        )
    return sh_t.SH_TEMPLATE.format(
        progress_script=progress_script,
        report_script=report_script,
        report_md_path=_sh_quote(str(report_md_path)),
        report_call=_build_legacy_report_call_posix(plan, max_size_percent,
                                                    no_report=no_report),
        **common,
    )


# Back-compat: keep the old name `write_bat` as an alias so other callers
# that still reference it (e.g. tests or external scripts) keep working.
write_bat = write_script

"""Generate the encoder .bat that the user actually runs.

Two templates live here: a chunked/resumable variant (the queue mode default)
and a single-pass variant (legacy direct use). Splitting these out of
compress.py keeps the size of the planner/CLI manageable; this module is
mostly text — the cmd.exe escaping rules and the per-flag wiring are the
only real logic."""
from __future__ import annotations

from pathlib import Path

from .plan import EncodePlan
from .probe import SourceInfo


RESUMABLE_BAT_TEMPLATE = """@echo off
chcp 65001 >nul
title x265 compress (resumable): {base_title}

set "_SKILL_IN={input_path}"
set "_SKILL_OUT={output_path}"
set "_SKILL_WORKER={resumable_script}"
set "_SKILL_WORKDIR={workdir}"

echo === ffmpeg x265 compression (resumable mode) ===
echo Input:    "%_SKILL_IN%"
echo Output:   "%_SKILL_OUT%"
echo Workdir:  "%_SKILL_WORKDIR%"
echo Source:   {codec}, {width}x{height}, {fps} fps, ~{src_kbps} kbps, {bit_depth}-bit{hdr_tag}
echo Duration: {duration_str}
echo Target:   x265 preset {preset}, CRF {crf}, {pix_fmt_out}, sharpness/motion tuned
echo Audio:    passthrough ({audio_codecs})
echo Mode:     split into {segment_seconds}s chunks, encode {parallel_label}, concat.
echo Size guard: {threshold_label}
echo           If killed (or laptop rebooted), re-run this .bat to resume.
echo.

python -u "%_SKILL_WORKER%" ^
  --input "%_SKILL_IN%" ^
  --output "%_SKILL_OUT%" ^
  --workdir "%_SKILL_WORKDIR%" ^
  --crf {crf} ^
  --preset {preset} ^
  --pix-fmt {pix_fmt_out} ^
  --x265-params "{x265_params}" ^
  --segment-seconds {segment_seconds} ^
  --parallel {parallel}{extra_args}{no_report_flag}

set ENCODE_RC=%errorlevel%
echo.
if %ENCODE_RC% neq 0 goto FAILED
goto END

:FAILED
echo === Failed or interrupted (exit %ENCODE_RC%). Re-run this .bat to resume. ===

:END
{pause_line}
exit /b %ENCODE_RC%
"""


BAT_TEMPLATE = """@echo off
chcp 65001 >nul
title x265 compress: {base}

REM Paths are stashed in env vars so '(', ')', '!', '&', '^' in filenames
REM survive cmd / PowerShell parsing without per-char escaping.
set "_SKILL_IN={input_path}"
set "_SKILL_OUT={output_path}"
set "_SKILL_PROGRESS={progress_script}"
set "_SKILL_REPORT={report_script}"
set "_SKILL_REPORT_MD={report_md_path}"

REM Capture start time so we can record encode wall-clock duration in the report.
for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-Date).ToString('o')"') do set _START_ISO=%%T

echo === ffmpeg x265 compression ===
echo Input:   "%_SKILL_IN%"
echo Output:  "%_SKILL_OUT%"
echo Source:  {codec}, {width}x{height}, {fps} fps, ~{src_kbps} kbps, {bit_depth}-bit{hdr_tag}
echo Duration:{duration_str}
echo Target:  x265 preset {preset}, CRF {crf}, {pix_fmt_out}, sharpness/motion tuned
echo Audio:   passthrough ({audio_codecs})
echo Est.:    ~{estimated_reduction} smaller
echo.
echo Progress (percent / encoded / fps / speed / ETA) updates in place:
echo.

ffmpeg -hide_banner -loglevel error -nostats -progress - -y ^
  -i "%_SKILL_IN%" ^
  -map 0 -map -0:d ^
  -c:v libx265 ^
  -preset {preset} ^
  -crf {crf} ^
  -x265-params "{x265_params}" ^
  -pix_fmt {pix_fmt_out} ^
  -c:a copy ^
  -c:s copy ^
  "%_SKILL_OUT%" | python -u "%_SKILL_PROGRESS%" --duration {duration}

set ENCODE_RC=%errorlevel%
echo.
if %ENCODE_RC% neq 0 goto FAILED

echo === Done. Output: "{output_path}" ===
powershell -NoProfile -Command "$in=(Get-Item -LiteralPath $env:_SKILL_IN).Length; $out=(Get-Item -LiteralPath $env:_SKILL_OUT).Length; $pct=($in-$out)/$in*100; '    Input :  {{0,8:N1}} MB' -f ($in/1MB); '    Output:  {{0,8:N1}} MB' -f ($out/1MB); '    Saved :  {{0,8:N1}} %% ({{1:N1}} MB)' -f $pct, (($in-$out)/1MB)"
{report_call}goto END

:FAILED
echo === Failed with exit code %ENCODE_RC% ===

:END
{pause_line}
exit /b %ENCODE_RC%
"""


def fmt_duration(seconds: float) -> str:
    """Format seconds as `H:MM:SS` for the .bat's pre-encode summary."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"


def _safe_args(values) -> list[str]:
    """Strip quotes and newlines so values are safe to inject into the .bat
    banner's `echo` lines. Defensive — input has already been parsed by
    ffprobe so weird content is unlikely."""
    return [str(v).replace('"', "'").replace("\n", " ") for v in values]


def _common_substitutions(info: SourceInfo, plan: EncodePlan, source_path: Path,
                         *, no_pause: bool) -> dict:
    """The substitutions both templates need. Extracted so the two template
    branches don't duplicate twenty lines of dict construction."""
    base = source_path.stem
    audio_codecs = ", ".join(_safe_args(info.audio_codecs)) or "none"
    hdr_tag = " HDR" if info.is_hdr else ""
    # `base` is used in Python Path joins (safe) and in cmd `title` lines.
    # cmd parses unquoted `title` args, so `&` in a filename becomes a command
    # separator (`& DRINKS` runs DRINKS as a command). Escape cmd meta-chars
    # for the title-context substitution only. `^` must be escaped first so
    # we don't double-escape our own escape characters.
    base_title = base.replace("^", "^^").replace("&", "^&").replace("%", "%%")
    return dict(
        base=base, base_title=base_title,
        input_path=str(source_path), output_path=plan.output_path,
        codec=info.codec, width=info.width, height=info.height,
        fps=f"{info.fps:.3f}".rstrip("0").rstrip("."),
        src_kbps=info.video_bitrate_kbps,
        bit_depth=info.bit_depth, hdr_tag=hdr_tag,
        preset=plan.preset, crf=plan.crf, pix_fmt_out=plan.pix_fmt_out,
        x265_params=":".join(plan.x265_params),
        # Escape `%` so cmd.exe doesn't try to expand the value as a variable
        # reference when the .bat runs `echo Est.: ~{estimated_reduction} smaller`.
        estimated_reduction=plan.estimated_reduction.replace("%", "%%"),
        audio_codecs=audio_codecs,
        duration=f"{info.duration_sec:.3f}",
        duration_str=fmt_duration(info.duration_sec),
        # The queue runner passes --no-pause so per-job bats exit straight away
        # (otherwise each job would block on a keypress between files). Direct
        # invocations keep `pause` so the window doesn't slam shut on the user.
        pause_line="" if no_pause else "pause",
    )


def _build_resumable_extra_args(*, max_output_bytes: int | None,
                               max_size_percent: float | None,
                               auto_fix_choke: bool,
                               no_pre_flight_scan: bool,
                               auto_patch_source: bool,
                               max_patch_seconds: float,
                               source_path: Path,
                               info: SourceInfo) -> str:
    """Build the variable trailing-flags block appended to the resumable
    encoder's command line. Order matches the precedence in the docs."""
    extra = ""
    if max_output_bytes is not None:
        extra += f" ^\n  --max-output-bytes {max_output_bytes}"
        extra += f" ^\n  --source-bytes {source_path.stat().st_size}"
        extra += f" ^\n  --total-duration-seconds {info.duration_sec:.3f}"
    if auto_fix_choke:
        extra += " ^\n  --auto-fix-choke"
    if no_pre_flight_scan:
        extra += " ^\n  --no-pre-flight-scan"
    if auto_patch_source:
        extra += " ^\n  --auto-patch-source"
        extra += f" ^\n  --max-patch-seconds {max_patch_seconds}"
    return extra


def _build_legacy_report_call(plan: EncodePlan,
                             max_size_percent: float | None,
                             *, no_report: bool) -> str:
    """The non-resumable .bat has no Python orchestrator inside it, so we
    inject a report.py CLI call at the success branch. Returns the cmd
    snippet (empty string if no_report)."""
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


def write_bat(info: SourceInfo, plan: EncodePlan, source_path: Path,
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
    """Render the .bat for `plan` and write it to plan.bat_path.

    `resumable=True` selects the chunked/resumable template (queue mode
    default); False uses the single-pass legacy template. The two flavors
    share `common` substitutions but diverge on what they put into the
    command body."""
    skill_dir = Path(__file__).resolve().parent.parent
    common = _common_substitutions(info, plan, source_path, no_pause=no_pause)

    output_path_obj = Path(plan.output_path)
    tmp_dir = output_path_obj.parent / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    report_md_path = tmp_dir / f"{output_path_obj.stem}.report.md"

    if resumable:
        workdir = tmp_dir / f".compress_{source_path.stem}"
        parallel_label = (f"{parallel} chunks in parallel"
                          if parallel > 1 else "one at a time")
        extra_args = _build_resumable_extra_args(
            max_output_bytes=max_output_bytes,
            max_size_percent=max_size_percent,
            auto_fix_choke=auto_fix_choke,
            no_pre_flight_scan=no_pre_flight_scan,
            auto_patch_source=auto_patch_source,
            max_patch_seconds=max_patch_seconds,
            source_path=source_path, info=info,
        )
        # The `>` MUST be escaped as `^>` and `%` as `%%` because this string
        # is baked into a literal `echo` line. cmd.exe would otherwise parse
        # `>` as stdout redirection and silently create a file named after
        # the threshold value (e.g. `80.0`) on every run.
        threshold_label = (
            f"abort if projected output ^> {max_size_percent:.1f}%% of source"
            if max_size_percent is not None else "off"
        )
        no_report_flag = " ^\n  --no-report" if no_report else ""
        content = RESUMABLE_BAT_TEMPLATE.format(
            resumable_script=str(skill_dir / "encode_resumable.py"),
            workdir=str(workdir),
            segment_seconds=segment_seconds, parallel=parallel,
            parallel_label=parallel_label,
            extra_args=extra_args,
            threshold_label=threshold_label,
            no_report_flag=no_report_flag,
            **common,
        )
    else:
        content = BAT_TEMPLATE.format(
            progress_script=str(skill_dir / "progress.py"),
            report_script=str(skill_dir / "report.py"),
            report_md_path=str(report_md_path),
            report_call=_build_legacy_report_call(plan, max_size_percent,
                                                  no_report=no_report),
            **common,
        )
    # Write without BOM. cmd.exe + chcp 65001 handles UTF-8 fine; a BOM at
    # the top of a .bat causes the first line to be misparsed by some
    # versions of cmd.
    Path(plan.bat_path).write_bytes(content.encode("utf-8"))

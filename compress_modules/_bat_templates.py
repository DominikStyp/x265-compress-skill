"""Windows cmd.exe `.bat` templates for the encoder.

Two variants:
  RESUMABLE_BAT_TEMPLATE   The chunked/resumable encoder (the default queue
                           uses this — survives kills, reboots, sleep).
  BAT_TEMPLATE             Single-pass encoder (legacy direct use; the queue
                           never picks this path).

Templates contain str.format placeholders only. All cmd.exe escaping rules
(`&` -> `^&`, `%` -> `%%`, `>` -> `^>`) are applied by `script_writer` BEFORE
the substitutions run — never inside the templates themselves.
"""
from __future__ import annotations


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

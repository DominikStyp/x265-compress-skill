"""POSIX bash `.sh` templates for the encoder. Mirror of `_bat_templates.py`.

Two variants:
  RESUMABLE_SH_TEMPLATE   Chunked/resumable encoder.
  SH_TEMPLATE             Single-pass legacy encoder.

POSIX escaping is much simpler than cmd.exe — single-quoted strings are
literal, so the only thing that needs escaping inside a single-quoted
value is the single quote itself (handled by `script_writer._sh_quote`).
Everything else (`&`, `(`, `)`, `!`, `[`, `]`, `$`) is safe inside single
quotes.

Variable references use `${VAR}` (not `$VAR`) so values containing letters
adjacent to the var name expand correctly. `$?` captures the last exit
code (cmd.exe's `%errorlevel%`).

The `set +e` line is intentional: we want to capture the encoder's exit
code manually rather than aborting the script on any non-zero. The .bat
equivalent has implicit fall-through, which Bash gives us with `set +e`.
"""
from __future__ import annotations


RESUMABLE_SH_TEMPLATE = """#!/usr/bin/env bash
# x265 compress (resumable): {base}
set +e

# Preflight: the encoder needs ffmpeg; the resumable worker needs Python.
# Resolve python3-or-python (some systems ship only `python`) and fail with a
# clear message instead of a cryptic "command not found" if this terminal was
# opened before the tools were installed.
PY="$(command -v python3 || command -v python)"
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: ffmpeg not found on PATH. Install ffmpeg, then reopen this terminal." >&2
    {pause_line}
    exit 127
fi
if [ -z "${{PY}}" ]; then
    echo "ERROR: python3/python not found on PATH. Install Python 3.9+, then reopen this terminal." >&2
    {pause_line}
    exit 127
fi

_SKILL_IN={input_path}
_SKILL_OUT={output_path}
_SKILL_WORKER={resumable_script}
_SKILL_WORKDIR={workdir}{hooks_setup}

# Terminal title (works in Terminal.app, iTerm2, gnome-terminal, etc.).
printf '\\033]0;x265 compress (resumable): {base_title}\\007'

echo "=== ffmpeg x265 compression (resumable mode) ==="
echo "Input:    \\"${{_SKILL_IN}}\\""
echo "Output:   \\"${{_SKILL_OUT}}\\""
echo "Workdir:  \\"${{_SKILL_WORKDIR}}\\""
echo "Source:   {codec}, {width}x{height}, {fps} fps, ~{src_kbps} kbps, {bit_depth}-bit{hdr_tag}"
echo "Duration: {duration_str}"
echo "Target:   x265 preset {preset}, CRF {crf}, {pix_fmt_out}, sharpness/motion tuned"
echo "Audio:    passthrough ({audio_codecs})"
echo "Mode:     split into {segment_seconds}s chunks, encode {parallel_label}, concat."
echo "Size guard: {threshold_label}"
echo "          If killed (or laptop rebooted), re-run this .sh to resume."
echo ""

"${{PY}}" -u "${{_SKILL_WORKER}}" \\
  --input "${{_SKILL_IN}}" \\
  --output "${{_SKILL_OUT}}" \\
  --workdir "${{_SKILL_WORKDIR}}" \\
  --crf {crf} \\
  --preset {preset} \\
  --pix-fmt {pix_fmt_out} \\
  --x265-params "{x265_params}" \\
  --segment-seconds {segment_seconds} \\
  --parallel {parallel}{extra_args}{no_report_flag}{hooks_flag}

ENCODE_RC=$?
echo ""
if [ ${{ENCODE_RC}} -ne 0 ]; then
    echo "=== Failed or interrupted (exit ${{ENCODE_RC}}). Re-run this .sh to resume. ==="
fi
{pause_line}
exit ${{ENCODE_RC}}
"""


SH_TEMPLATE = """#!/usr/bin/env bash
# x265 compress: {base}
set +e

# Preflight: this script needs ffmpeg and Python (the progress reader).
# Resolve python3-or-python (some systems ship only `python`) and fail with a
# clear message instead of a cryptic "command not found" if this terminal was
# opened before the tools were installed.
PY="$(command -v python3 || command -v python)"
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: ffmpeg not found on PATH. Install ffmpeg, then reopen this terminal." >&2
    {pause_line}
    exit 127
fi
if [ -z "${{PY}}" ]; then
    echo "ERROR: python3/python not found on PATH. Install Python 3.9+, then reopen this terminal." >&2
    {pause_line}
    exit 127
fi

_SKILL_IN={input_path}
_SKILL_OUT={output_path}
_SKILL_PROGRESS={progress_script}
_SKILL_REPORT={report_script}
_SKILL_REPORT_MD={report_md_path}

# Terminal title (Terminal.app, iTerm2, gnome-terminal, etc.).
printf '\\033]0;x265 compress: {base_title}\\007'

# Capture start time for the wall-clock duration in the report.
_START_TS=$(date +%s)

echo "=== ffmpeg x265 compression ==="
echo "Input:   \\"${{_SKILL_IN}}\\""
echo "Output:  \\"${{_SKILL_OUT}}\\""
echo "Source:  {codec}, {width}x{height}, {fps} fps, ~{src_kbps} kbps, {bit_depth}-bit{hdr_tag}"
echo "Duration:{duration_str}"
echo "Target:  x265 preset {preset}, CRF {crf}, {pix_fmt_out}, sharpness/motion tuned"
echo "Audio:   passthrough ({audio_codecs})"
echo "Est.:    ~{estimated_reduction} smaller"
echo ""
echo "Progress (percent / encoded / fps / speed / ETA) updates in place:"
echo ""

ffmpeg -hide_banner -loglevel error -nostats -progress - -y \\
  -i "${{_SKILL_IN}}" \\
  -map 0 -map -0:d \\
  -c:v libx265 \\
  -preset {preset} \\
  -crf {crf} \\
  -x265-params "{x265_params}" \\
  -pix_fmt {pix_fmt_out} \\
  -c:a copy \\
  -c:s copy \\
  "${{_SKILL_OUT}}" | "${{PY}}" -u "${{_SKILL_PROGRESS}}" --duration {duration}

# In a pipeline, $? is the LAST command's exit. We want ffmpeg's, which is
# the first element of PIPESTATUS.
ENCODE_RC=${{PIPESTATUS[0]}}
echo ""
if [ ${{ENCODE_RC}} -eq 0 ]; then
    echo "=== Done. Output: \\"{output_path}\\" ==="
    _IN_BYTES=$(stat -f%z "${{_SKILL_IN}}" 2>/dev/null || stat -c%s "${{_SKILL_IN}}" 2>/dev/null)
    _OUT_BYTES=$(stat -f%z "${{_SKILL_OUT}}" 2>/dev/null || stat -c%s "${{_SKILL_OUT}}" 2>/dev/null)
    awk -v i="${{_IN_BYTES}}" -v o="${{_OUT_BYTES}}" 'BEGIN {{
        printf "    Input :  %8.1f MB\\n", i/1048576
        printf "    Output:  %8.1f MB\\n", o/1048576
        if (i > 0) {{
            saved = i - o
            printf "    Saved :  %8.1f %% (%.1f MB)\\n", (saved/i)*100, saved/1048576
        }}
    }}'
{report_call}
else
    echo "=== Failed with exit code ${{ENCODE_RC}} ==="
fi
{pause_line}
exit ${{ENCODE_RC}}
"""

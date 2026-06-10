"""Substitution-dict builders, shell-quoting helpers, and the variable
trailing-flags block for the encoder-script generator.

Extracted from ``script_writer.py`` so that module stays a slim orchestrator
(pick template/extension → call a renderer → write + chmod) instead of also
owning every per-shell value-escaping rule. This file is the "how each shell
wants its values" layer:

  * quoting helpers (``_sh_quote`` / ``_cmd_set_escape`` / ``_cmd_title_escape``)
    — the per-shell escaping a value needs before it's spliced into a template;
  * ``_common_fields`` / ``_win_substitutions`` / ``_posix_substitutions`` —
    the str.format substitution dicts each template is rendered with;
  * ``_build_extra_args`` — the variable trailing-flags block appended to the
    encoder command line (the bit that differs by ``ScriptOptions``).

Pure text generation; no I/O. ``script_writer`` imports these and keeps the
orchestration + hook-sidecar plumbing.
"""
from __future__ import annotations

from pathlib import Path

from formatting import format_hms

from .plan import EncodePlan
from .probe import SourceInfo
from .script_options import ScriptOptions


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


def _cmd_set_escape(s: str) -> str:
    """Escape a value embedded inside cmd `set "VAR=<value>"`. Inside the
    double quotes, `&` `(` `)` `^` `!` are already literal — but `%` is NOT:
    cmd expands `%VAR%` and STRIPS a lone `%` on the line before `set` runs, so
    a path like `C:\\50%PATH%.mkv` would expand PATH (corrupting the path the
    encoder actually runs on) and `70% Hell` would lose its `%`. Double every
    `%` so cmd stores the literal. (`"` needs no handling — it can't occur in a
    Windows path or filename.)"""
    return s.replace("%", "%%")


# --- Time formatting -------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """Format seconds as `H:MM:SS` for the script's pre-encode summary
    (delegates to the canonical formatting.format_hms)."""
    return format_hms(seconds)


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
        # Paths land in `set "VAR=..."` (and the done-echo), so `%` must be
        # doubled — see _cmd_set_escape. Other meta-chars are literal there.
        input_path=_cmd_set_escape(str(source_path)),
        output_path=_cmd_set_escape(plan.output_path),
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
        # The title is passed to printf as a %s DATA argument (see the .sh
        # template), so it just needs full single-quoting like any other value.
        base_title=_sh_quote(base),
        input_path=_sh_quote(str(source_path)),
        # plan.output_path is used both as a quoted variable value and as a
        # label in a double-quoted echo; _sh_quote covers the value, and echo
        # (unlike printf) treats `%` literally, so no extra escaping is needed.
        output_path=_sh_quote(plan.output_path),
        estimated_reduction=plan.estimated_reduction,
        pause_line=pause,
    )


# --- extra_args block (line-continuation differs by shell) ------------------

def _build_extra_args(opts: ScriptOptions, *, line_cont: str,
                     source_path: Path, info: SourceInfo,
                     quote_value=None) -> str:
    """Build the variable trailing-flags block appended to the encoder
    command line. The encode-behaviour flags ride in on `opts`; `line_cont`
    is the shell's line-continuation character (`^` for cmd.exe, `\\` for
    bash) and `quote_value` is the shell's path-quoting helper — both are
    selected per-OS by the caller, so they stay explicit, not in opts."""
    extra = ""
    if opts.max_output_bytes is not None:
        extra += f" {line_cont}\n  --max-output-bytes {opts.max_output_bytes}"
        extra += f" {line_cont}\n  --source-bytes {source_path.stat().st_size}"
        extra += f" {line_cont}\n  --total-duration-seconds {info.duration_sec:.3f}"
    if opts.auto_fix_choke:
        extra += f" {line_cont}\n  --auto-fix-choke"
    if opts.no_pre_flight_scan:
        extra += f" {line_cont}\n  --no-pre-flight-scan"
    if opts.auto_patch_source:
        extra += f" {line_cont}\n  --auto-patch-source"
        extra += f" {line_cont}\n  --max-patch-seconds {opts.max_patch_seconds}"
    if opts.visual_quality_threshold is not None:
        extra += (f" {line_cont}\n  --visual-quality-threshold "
                  f"{opts.visual_quality_threshold:g}")
    if opts.done_dir:
        # done_dir is a path possibly containing spaces, `&`, etc. The
        # caller injects a quote-helper (`_cmd_set_escape`-via-double-quote
        # on Windows, `_sh_quote` on POSIX) so the shell sees a single
        # token. Falls back to raw interpolation when none provided —
        # tests use that to inspect the unquoted value.
        quoted = quote_value(opts.done_dir) if quote_value is not None else (
            f'"{opts.done_dir}"')
        extra += f" {line_cont}\n  --done-dir {quoted}"
    if opts.no_log_chunk_metrics:
        extra += f" {line_cont}\n  --no-log-chunk-metrics"
    return extra

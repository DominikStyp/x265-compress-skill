"""Legacy single-pass (.bat / .sh) report-call generators.

The non-resumable script has no Python orchestrator inside it, so it injects
a ``report.py`` CLI call at its success branch. The resumable script paths
do their own reporting via ``encode_modules.reporting`` and don't need this.

Lifted out of ``script_writer.py`` to keep that module under the 500-line
cap mandated by AGENTS.md when ``chunk_metrics_log`` plumbing pushed it
over (v1.18.0). Pure text generation — no side effects."""
from __future__ import annotations

from .plan import EncodePlan


def build_legacy_report_call_win(plan: EncodePlan,
                                 max_size_percent: float | None,
                                 *, no_report: bool) -> str:
    """Windows .bat fragment: capture elapsed seconds via PowerShell and run
    report.py for a one-row markdown report. Empty string when ``no_report``."""
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


def build_legacy_report_call_posix(plan: EncodePlan,
                                   max_size_percent: float | None,
                                   *, no_report: bool) -> str:
    """POSIX .sh equivalent of the Windows legacy report call.
    ``date +%s`` gives epoch seconds; we diff against the start timestamp
    captured at the top of the script."""
    if no_report:
        return ""
    mx_part = (f" --max-size-percent {max_size_percent:.1f}"
               if max_size_percent is not None else "")
    return (
        '\n    _ELAPSED=$(( $(date +%s) - _START_TS ))\n'
        f'    "${{PY}}" -u "${{_SKILL_REPORT}}" single "${{_SKILL_REPORT_MD}}" '
        f'"${{_SKILL_IN}}" "${{_SKILL_OUT}}" --crf {plan.crf} '
        f'--preset {plan.preset}{mx_part} '
        f'--elapsed-sec ${{_ELAPSED}} --status ok'
    )

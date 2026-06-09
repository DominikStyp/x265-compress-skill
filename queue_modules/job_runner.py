"""Per-job execution: invoke compress.py to produce a .bat, then run the .bat,
then build the report row.

Extracted from run_queue.main()'s while-loop so the loop itself stays focused
on what to do *between* jobs (live-reload, skip rules, summary stats).

The `retry_with_bigger_crf` escalation loop (incl. v1.15.0 adaptive jump +
state persistence + floor detector) lives in the sibling `crf_retry` module
to keep this file focused on single-attempt execution and stay under the
project's 500-line cap. References to `run_one_job` and
`supersede_encoded_chunks` from the retry loop go through the module (not
direct symbol imports) so tests that monkey-patch those names on
`job_runner` continue to work unchanged.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from platform_compat import IS_WINDOWS

from .job_schema import build_compress_argv, derive_output_path, derive_workdir


# Exit-code -> status mapping. Matches encode_resumable.py's sys.exit values.
# Keep in sync if new exit codes appear there.
_EXIT_STATUS: dict[int, str] = {
    0: "ok",
    3: "stopped-threshold",
    5: "chunk-choked",        # legacy whole-file abort, kept for back-compat
    6: "pre-flight-failed",
    7: "awaiting-chunk-fix",
    8: "stopped-by-user",      # graceful 'finish after current chunk' stop
    9: "stopped-quality-threshold",  # v1.17.0 — per-chunk VMAF guard fired
}

# retry_with_bigger_crf defaults (see job_schema.VALID_KEYS). A higher CRF
# means a smaller file at lower quality; crf_max caps how far we degrade
# before giving up. Status emitted when the cap is reached still over budget.
DEFAULT_CRF_STEP = 1
DEFAULT_CRF_MAX = 28
CRF_EXHAUSTED_STATUS = "stopped-threshold-crf-exhausted"


def status_for_exit(rc: int) -> str:
    """Translate an encode_resumable.py exit code to the status string used
    in the queue report. Unknown codes fall through as `failed-exit-<N>`."""
    return _EXIT_STATUS.get(rc, f"failed-exit-{rc}")


def generate_bat(compress_py: Path, merged_job: dict
                ) -> tuple[int, dict | None, str]:
    """Run compress.py to produce the encoder .bat. Returns (rc, summary_dict
    or None, stderr_tail). On success the summary dict carries `bat_path`
    plus the JSON-serialized SourceInfo/EncodePlan."""
    argv = build_compress_argv(merged_job) + ["--no-report", "--no-pause"]
    gen = subprocess.run(
        [sys.executable, str(compress_py), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if gen.returncode != 0:
        return gen.returncode, None, gen.stderr.strip()
    try:
        return 0, json.loads(gen.stdout), ""
    except Exception as e:
        return 0, None, f"parse error: {e}"


def run_script(script_path: str, *,
               timeout: float | None = None) -> tuple[int, float]:
    """Run the encoder script that compress.py just wrote. Dispatches to
    cmd.exe on Windows or bash on POSIX. Returns (exit_code, elapsed_seconds).

    `stdin` is INHERITED (not redirected to DEVNULL) so the encoder's
    interactive keyboard control (↑↓/Space/1-9/r) can read keypresses
    through the launcher → script → python.

    Windows: the `call` prefix matters when the bat path contains `&` (or any
    of `<>()@^|`). Without it, `cmd /c "path with & in it.bat"` triggers
    cmd's quote-stripping rule (see `cmd /?` rule 2): leading quote stripped,
    trailing quote stripped, then cmd parses `path with & in it.bat` as TWO
    commands separated by `&`. With `cmd /c call "path"`, the first token
    after /c is `call`, not `"`, so rule 2 never fires.

    POSIX: `bash <path>` works regardless of executable bit (chmod failures
    on FAT/SMB filesystems would otherwise leave the script unrunnable).
    Bash handles single-quoted paths with `&`, `[`, `]`, `(`, `)` natively
    when assigned to variables via `_SKILL_IN='...'`.

    **Deliberate exception to AGENTS.md's "every subprocess has a timeout"
    rule.** That rule is scoped to PROBE-style calls (ffprobe, version
    checks) where unbounded hangs are pure cost. The encoder script can
    legitimately run for hours — a 4K x265 source at preset slow takes
    well past 24h. A wrapping timeout here would have to be larger than
    any realistic encode, at which point it's not catching anything that
    a process-monitor wouldn't catch sooner. Callers who want a hard
    backstop can pass `timeout=N` explicitly; default is unbounded."""
    t0 = time.monotonic()
    try:
        if IS_WINDOWS:
            rc = subprocess.call(["cmd.exe", "/c", "call", script_path],
                                 timeout=timeout)
        else:
            rc = subprocess.call(["bash", script_path], timeout=timeout)
    except subprocess.TimeoutExpired:
        # Surface as a non-zero rc so the caller sees "failed" rather
        # than swallowing the exception. Re-raise if the caller wants
        # to special-case it would need a wrapping try/except — kept
        # simple here because the default code path is timeout=None.
        return 124, time.monotonic() - t0  # 124 = GNU coreutils' "timeout"
    return rc, time.monotonic() - t0


# Back-compat alias for any caller still using the old name.
run_bat = run_script


def read_quality_sidecar(out_path: Path) -> dict | None:
    """Read the VMAF/PSNR/SSIM sidecar ``encode_resumable.py`` writes for a
    successful output. v1.19.0: prefers the new ``logs/`` location;
    falls back to the pre-v1.19.0 ``.tmp/`` path so a queue runner picking
    up sidecars from a partially-migrated workspace (or a sidecar written
    by an old encoder still on the same machine) keeps reading them.
    Returns None when the sidecar is missing or unreadable — caller fills
    the report row with placeholders."""
    from encode_modules.log_paths import quality_sidecar_path
    candidates = [
        quality_sidecar_path(out_path),
        # Legacy .tmp/ path — harmless fallback after v1.19.0 migration.
        out_path.parent / ".tmp" / f"{out_path.stem}.quality.json",
    ]
    for sidecar in candidates:
        if sidecar.exists():
            try:
                return json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _placeholder_row(input_path: Path, in_bytes: int, merged: dict,
                    status: str) -> dict:
    """Common skeleton row for non-ok statuses (skipped, failed, etc).
    output_bytes / elapsed_seconds are None — there's nothing to report."""
    return {
        "input": str(input_path), "output": None,
        "input_bytes": in_bytes, "output_bytes": None,
        "crf": merged.get("crf"), "preset": merged.get("preset"),
        "parallel": merged.get("parallel"),
        "max_size_percent": merged.get("max_size_percent"),
        "elapsed_seconds": None, "status": status,
    }


def build_job_row(*, input_path: Path, out_path: Path,
                 in_bytes: int, merged: dict,
                 status: str, elapsed: float,
                 summary: dict | None) -> dict:
    """Build the per-job dict for the aggregate report. Includes quality
    scores from the sidecar when status == 'ok' and the sidecar exists.

    `done_dir` post-move: when the encoder moved source+output away from
    their derived paths, `out_path.stat()` would return None and the row
    would lose its output size + savings. We detect the move by stat-ing
    the configured done_dir's destination and use that path instead. The
    quality sidecar lives in `<out_path.parent>/.tmp/` and is NOT moved
    by `done_dir`, so it's still found at the original location."""
    output_bytes = _resolve_output_bytes(out_path, merged)
    plan = (summary or {}).get("plan") or {}
    row: dict = {
        "input": str(input_path),
        "output": str(out_path) if status == "ok" else None,
        "input_bytes": in_bytes,
        "output_bytes": output_bytes,
        "crf": plan.get("crf") or merged.get("crf"),
        "preset": plan.get("preset") or merged.get("preset"),
        "parallel": merged.get("parallel"),
        "max_size_percent": merged.get("max_size_percent"),
        "elapsed_seconds": elapsed,
        "status": status,
    }
    if status == "ok":
        q = read_quality_sidecar(out_path)
        if q is not None:
            row["vmaf_mean"] = q.get("vmaf_mean")
            row["vmaf_min"] = q.get("vmaf_min")
            row["psnr_y_mean"] = q.get("psnr_y_mean")
            row["ssim_mean"] = q.get("ssim_mean")
            row["quality_method"] = q.get("method")
    return row


def _resolve_output_bytes(out_path: Path, merged: dict) -> int | None:
    """Return the actual output file size, handling the case where
    `done_dir` moved the output away from `out_path`. Falls back to the
    original path stat (failed-move case) or None (genuinely missing)."""
    if out_path.exists():
        return out_path.stat().st_size
    done_dir = merged.get("done_dir")
    if done_dir:
        moved = Path(done_dir) / out_path.name
        if moved.exists():
            return moved.stat().st_size
    return None


def run_one_job(*, compress_py: Path, merged: dict,
               i: int, n: int) -> tuple[str, dict]:
    """Execute a single queue job end-to-end. Returns (status, report_row).

    Steps: print header → compress.py to generate .bat → cmd /c call .bat
    → translate exit code to status → read quality sidecar → build row.
    Bails to `failed-gen` / `failed-parse` rows if compress.py itself
    failed; bails to `skipped-not-found` / `skipped-exists` upstream of
    this function (run_queue.main owns those guards)."""
    input_path = Path(merged["input"])
    in_bytes = input_path.stat().st_size if input_path.exists() else 0

    print()
    print("=" * 70)
    print(f"[{i}/{n}] {input_path.name}")
    print("=" * 70)

    rc, summary, err = generate_bat(compress_py, merged)
    if rc != 0:
        print(f"[{i}/{n}] FAILED to generate bat:\n{err}")
        return "failed-gen", _placeholder_row(input_path, in_bytes,
                                              merged, "failed-gen")
    if summary is None:
        print(f"[{i}/{n}] FAILED to parse compress.py output: {err}")
        return "failed-parse", _placeholder_row(input_path, in_bytes,
                                                 merged, "failed-parse")

    # compress.py emits "script_path" plus a "bat_path" back-compat alias.
    # Prefer the canonical name; fall back to the alias.
    rc, elapsed = run_script(summary.get("script_path") or summary["bat_path"])
    status = status_for_exit(rc)
    print(f"[{i}/{n}] -> {status}  ({elapsed:.0f}s)")

    out_path = derive_output_path(input_path)
    row = build_job_row(
        input_path=input_path, out_path=out_path,
        in_bytes=in_bytes, merged=merged,
        status=status, elapsed=elapsed, summary=summary,
    )
    return status, row


def supersede_encoded_chunks(workdir: Path, old_crf: int) -> int:
    """Set aside chunks already encoded at `old_crf` so the next CRF attempt
    re-encodes them. Returns how many files were moved.

    Moves every `enc_src_*.mkv` (the glob also covers the `enc_src_*.part.mkv`
    partials) into a `.crf<old>_superseded_<ts>/` subdir of the workdir. We
    MOVE, never delete (encoded bytes are user data — the never-delete rule),
    and we leave the CRF-independent split (`src_*.mkv` + `.split_done`)
    untouched so re-splitting is skipped. The encoder's resume/verify globs are
    non-recursive (`workdir.glob(...)`), so chunks under the subdir become
    invisible — a chunk with no `enc_*.mkv` is re-encoded at the new CRF."""
    if not workdir.is_dir():
        return 0
    encoded = sorted(workdir.glob("enc_src_*.mkv"))
    if not encoded:
        return 0
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    aside = workdir / f".crf{old_crf}_superseded_{stamp}"
    aside.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in encoded:
        try:
            f.rename(aside / f.name)
            moved += 1
        except OSError as e:
            print(f"  WARNING: could not set aside {f.name}: {e}",
                  file=sys.stderr)
    return moved

"""Encoding-decision logic: CRF, preset, parallel-chunk count, x265-param
composition, output path. Pure functions that consume a SourceInfo and
produce an EncodePlan — no I/O, no ffprobe, no .bat writing."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platform_compat import IS_WINDOWS

from .probe import SourceInfo
from .x265_params import BASE_X265_PARAMS


# Generated script extension by OS. cmd.exe needs `.bat`; bash uses `.sh`.
# Plan.py owns this because it builds the script's output path during
# plan_encode(); the writer reads it from here too.
SCRIPT_EXTENSION = ".bat" if IS_WINDOWS else ".sh"


def script_filename_for(source_path: Path, ext: str) -> str:
    """The generated encoder script's filename: ``compress_<stem><ext>``,
    with any ``%`` in the stem replaced by ``_pct_``.

    Single source of truth for the script *filename* (the script *path* is
    built from this in `_resolve_output_paths`; the queue side reads the path
    back out of compress.py's JSON, so both ends use one formula).

    Why sanitize ``%``: the queue runner launches the script via
    ``subprocess.call(["cmd.exe", "/c", "call", script_path], ...)``. cmd.exe
    re-parses that command line and EXPANDS ``%VAR%`` inside ``script_path`` —
    so a source named ``50%PATH%off.mkv`` would make cmd look for a different,
    non-existent file (and `%PATH%` is gone). The script CONTENTS are already
    ``%``-escaped (`_cmd_set_escape`); the FILENAME is the uncovered sink.

    The replacement is deterministic and applied identically for ``.bat`` and
    ``.sh`` so a workdir resumed on a different OS still finds the same name.
    Collisions are tolerable (two stems differing only in ``%`` vs ``_pct_``
    is astronomically rare and no worse than the pre-existing same-stem
    workdir collision). Sources without ``%`` are byte-identical to the old
    ``f"compress_{stem}{ext}"`` formula — zero churn for the normal case."""
    safe_stem = source_path.stem.replace("%", "_pct_")
    return f"compress_{safe_stem}{ext}"


def compress_workdir(tmp_dir: Path, source_path: Path) -> Path:
    """The encoder's per-source working directory: ``<tmp_dir>/.compress_<stem>``.

    Single source of truth for this name. The script generator
    (`script_writer`) creates this directory and the queue's CRF-retry logic
    (`job_schema.derive_workdir`) locates already-encoded chunks inside it — if
    the two ever computed it differently, CRF-retry would silently re-encode
    from scratch. Keep them both going through here."""
    return tmp_dir / f".compress_{source_path.stem}"


@dataclass
class EncodePlan:
    crf: int
    preset: str
    pix_fmt_out: str
    x265_params: list[str]
    output_path: str
    script_path: str    # `.bat` on Windows, `.sh` on POSIX — see plan.py
    warnings: list[str]
    estimated_reduction: str
    notes: list[str]

    @property
    def bat_path(self) -> str:
        """Back-compat alias for callers that still reference bat_path."""
        return self.script_path


def pick_crf(info: SourceInfo) -> int:
    """CRF chosen from bits-per-pixel of the source. Bands tuned for H.264
    (the common case); H.265/AV1 sources get an additional bump in
    plan_encode() because they're already efficient and need more headroom."""
    bpp = info.bits_per_pixel
    if bpp <= 0:
        return 19  # No reliable bitrate info — be conservative.
    if bpp > 0.12:
        crf = 18      # very high quality master / Blu-ray
    elif bpp > 0.06:
        crf = 19      # high quality (typical Blu-ray rip, good web HD)
    elif bpp > 0.025:
        crf = 20      # medium (decent streaming quality)
    else:
        crf = 21      # already heavily compressed
    if info.width >= 3840:
        crf += 1      # 4K+: detail is denser per pixel, +1 still transparent.
    return crf


def pick_parallel(info: SourceInfo) -> int:
    """Concurrent-chunk count, derived from probed source height.

    Cap is driven by x265's WPP thread ceiling (~ceil(height/64) CTU rows) —
    past that a single instance can't usefully use more cores, so stacking
    N instances trades nothing for chunk overhead. Bands:

        4K / 2160p   -> 1  (RAM pressure dominates: each 10-bit 4K x265
                            holds GBs of lookahead+refs; 2 stacked tips
                            a 32 GB box into paging)
        1440p/1080p  -> 4  (~17 rows; 4 stacked fit a 32-core box)
        720p         -> 6  (~12 rows; lots of idle cores otherwise)
        below 720p   -> 8  (~8 rows; needs heavy stacking to saturate)

    Override per-file with --parallel N to bypass."""
    h = info.height or 0
    if h >= 2160:
        return 1
    if h >= 1080:
        return 4
    if h >= 720:
        return 6
    return 8


def pick_preset(info: SourceInfo) -> str:
    """Preset chosen from encode WORK (duration × resolution × fps), not file
    size. Size is a poor proxy — a 30-min 480p file and a 5-min 1080p file
    can match in size but the 480p has ~6× more frames to encode."""
    if not (info.duration_sec and info.width and info.height and info.fps):
        return "slow"
    work = info.duration_sec * info.width * info.height * info.fps
    # Reference points (work units):
    #   1 min 720p30  ~ 1.7e9    -> slower  (~5 min encode)
    #   5 min 720p30  ~ 8.3e9    -> slow    (~25 min)
    #   30 min 480p50 ~ 3.8e10   -> slow    (~1-2 hr)
    #   1 hr 1080p30  ~ 2.2e11   -> slow    (~3-6 hr)
    #   2 hr 4K30     ~ 1.8e12   -> medium  (otherwise days)
    if work < 5e9:
        return "slower"
    if work > 5e11:
        return "medium"
    return "slow"


def _codec_warning(codec: str) -> str | None:
    """Return the warning string for codecs where x265 yields diminishing
    returns (or is the wrong tool)."""
    if codec in {"hevc", "h265"}:
        return ("Source is already HEVC/x265. Re-encoding typically yields "
                "only 5-15% reduction at similar quality. Consider whether "
                "this is worth doing.")
    if codec == "av1":
        return ("Source is AV1. x265 is unlikely to beat AV1 on compression "
                "at the same quality.")
    if codec == "vp9":
        return ("Source is VP9. Compression gains from x265 are typically "
                "marginal.")
    return None


def _apply_tune_overrides(params: list[str], anime: bool, grain: bool,
                         notes: list[str]) -> list[str]:
    """Layer in x265 tune profiles. `anime` replaces our params with
    tune=animation entirely; `grain` is additive but removes our psy-rdoq
    and aq-strength so x265's grain tune wins on those knobs."""
    if anime:
        notes.append("Anime mode: using x265 :tune=animation profile.")
        return ["tune=animation"]
    if grain:
        notes.append("Grain mode: using x265 :tune=grain (preserves film grain).")
        # tune=grain itself bumps aq-strength and disables psy-rdoq —
        # remove ours so x265's tune wins on those knobs. Also drop the
        # expensive motion-search defaults (me=star + merange=57): tune=grain
        # benefits from a less aggressive ME because exhaustive subpixel
        # refinement can fight the tune's flat-grain preservation. me=umh +
        # merange=32 matches x265 docs' recommendation when grain matters.
        filtered = [
            p for p in params
            if not p.startswith(("psy-rdoq", "aq-strength", "me=", "merange="))
        ]
        return filtered + ["me=umh", "merange=32", "tune=grain"]
    return params


def _apply_hdr(params: list[str], info: SourceInfo,
              notes: list[str]) -> list[str]:
    """Add HDR signalling to x265 params if the source has HDR primaries or
    transfer. Idempotent — safe to call on non-HDR sources (it just returns
    params unchanged)."""
    if not info.is_hdr:
        return params
    transfer = (info.color_transfer
                if info.color_transfer in {"smpte2084", "arib-std-b67"}
                else "smpte2084")
    notes.append(
        f"HDR detected (transfer={info.color_transfer or 'unknown'}). "
        "Color metadata preserved."
    )
    return params + [
        "hdr-opt=1", "repeat-headers=1",
        "colorprim=bt2020", f"transfer={transfer}", "colormatrix=bt2020nc",
    ]


def _resolve_output_paths(source_path: Path) -> tuple[Path, Path, str]:
    """Decide (output_path, script_path, basename). All generated artifacts
    live under <source_dir>/.tmp/ so the encoding directory stays clean —
    only sources, finished targets, and queue.json at root."""
    source_dir = source_path.parent
    tmp_dir = source_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    base = source_path.stem
    # If source is already .mkv, suffix with .x265 to avoid overwriting.
    out_name = f"{base}.x265.mkv" if source_path.suffix.lower() == ".mkv" else f"{base}.mkv"
    output_path = source_dir / out_name
    # Route through the shared helper so the script filename's `%`-sanitization
    # (cmd.exe re-expands `%VAR%` in the path it's `call`ed with) lives in one
    # place the queue side can also reference.
    script_path = tmp_dir / script_filename_for(source_path, SCRIPT_EXTENSION)
    return output_path, script_path, base


_REDUCTION_TABLE: dict[str, str] = {
    "h264": "30-45%", "avc": "30-45%",
    "mpeg2video": "50-70%", "mpeg4": "40-60%",
    "hevc": "5-15%", "h265": "5-15%",
    "av1": "uncertain (may be larger)",
    "vp9": "0-20%",
}


def plan_encode(info: SourceInfo, source_path: Path, *,
               override_crf: int | None, override_preset: str | None,
               anime: bool, grain: bool, eight_bit: bool) -> EncodePlan:
    """Compose an EncodePlan from a SourceInfo and CLI overrides. The work
    splits into 5 cohesive steps: CRF/preset, codec-specific warnings,
    x265 param layering (base + tunes + HDR), pix_fmt, output paths."""
    warnings: list[str] = []
    notes: list[str] = []

    crf = override_crf if override_crf is not None else pick_crf(info)
    preset = override_preset or pick_preset(info)

    codec_warning = _codec_warning(info.codec)
    if codec_warning:
        warnings.append(codec_warning)
    if info.codec in {"hevc", "h265"} and override_crf is None:
        crf = max(crf, 22)

    if info.bits_per_pixel and info.bits_per_pixel < 0.025:
        warnings.append(
            f"Source bits-per-pixel is very low ({info.bits_per_pixel:.4f}). "
            "Hitting the 20% size-reduction target without visible quality "
            "loss may not be possible."
        )

    params = _apply_tune_overrides(list(BASE_X265_PARAMS), anime, grain, notes)
    params = _apply_hdr(params, info, notes)

    if eight_bit:
        pix_fmt_out = "yuv420p"
        notes.append("8-bit output forced (compatibility mode).")
    else:
        pix_fmt_out = "yuv420p10le"
        if info.bit_depth < 10:
            notes.append(
                "Encoding to 10-bit even though source is 8-bit. "
                "10-bit x265 is ~5-10% more efficient at the same quality "
                "and produces smoother gradients."
            )

    output_path, script_path, _ = _resolve_output_paths(source_path)
    estimated_reduction = _REDUCTION_TABLE.get(info.codec, "30-50% (typical)")

    return EncodePlan(
        crf=crf, preset=preset, pix_fmt_out=pix_fmt_out,
        x265_params=params,
        output_path=str(output_path), script_path=str(script_path),
        warnings=warnings, estimated_reduction=estimated_reduction,
        notes=notes,
    )

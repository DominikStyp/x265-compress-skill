"""Shared video-stream metric extraction — single source of truth for the
fps / bits-per-pixel derivations the codebase used to duplicate three times.

Lives at the repo root (like ``formatting.py``) so both the encode pipeline
(``encode_modules`` / ``history``) and the script generator
(``compress_modules``) import it without forging a cross-package dependency.
Stdlib-only.

Why this module exists
----------------------
``r_frame_rate`` → decimal fps → bits-per-pixel was parsed independently in
``history.build_input_block``, ``compress_modules.probe.analyse`` and the
per-chunk-metrics block of ``encode_resumable``. BPP is the single most
predictive metric the encoder logs, and three derivations that *should* agree
but live apart inevitably drift. These helpers are the canonical derivation;
each former call site becomes a thin adapter so the numbers can't diverge.

Adapter-compat notes (the old call sites differed — preserved deliberately):
  * ``parse_fps_fraction`` returns ``Optional[float]`` (None on any failure)
    and additionally understands a bare numeric string with no ``/`` (e.g.
    ``"30"`` or ``"23.976"``) — ffprobe can emit ``avg_frame_rate`` that way.
    The old ``compress_modules.probe.parse_fps`` returned ``0.0`` on failure
    AND treated a slash-less string as a failure; its public name is kept as a
    backward-compatible wrapper there that maps None→0.0.
  * fps precedence differs by caller. history + encode_resumable use ONLY
    ``r_frame_rate`` (no ``avg_frame_rate`` fallback); ``video_stream_metrics``
    matches that by default and only consults ``avg_frame_rate`` when
    ``avg_fps_fallback=True`` is passed.
  * BPP rounding differs by caller (history rounds to 6 dp, compress does not),
    and the two feed different bitrate estimates — so ``bits_per_pixel`` takes
    the bitrate as an explicit argument and an optional ``ndigits`` rather than
    baking either choice in.
"""
from __future__ import annotations

from typing import Optional


def parse_fps_fraction(rfr: Optional[str]) -> Optional[float]:
    """Parse an ffprobe frame-rate string to a decimal fps, or None on failure.

    Handles:
      * rational fractions: ``"30000/1001"`` → 29.97…, ``"50/1"`` → 50.0;
      * a bare numeric string with no slash: ``"30"`` → 30.0, ``"23.976"`` →
        23.976 (ffprobe emits ``avg_frame_rate`` this way on some containers);
      * zero / zero-denominator (``"30/0"``, ``"0/0"``) → None;
      * garbage, too many slashes (``"a/b/c"``), empty / whitespace / None →
        None.
    """
    if not rfr:
        return None
    text = rfr.strip()
    if not text:
        return None
    if "/" in text:
        parts = text.split("/")
        if len(parts) != 2:
            return None  # "a/b/c" — not a rational we understand
        num_s, den_s = parts
        try:
            num, den = float(num_s), float(den_s)
        except ValueError:
            return None
        if den == 0:
            return None
        fps = num / den
        return fps if fps != 0 else None
    # No slash — treat as a plain float string.
    try:
        fps = float(text)
    except ValueError:
        return None
    return fps if fps != 0 else None


def bits_per_pixel(bitrate_bps: Optional[float],
                   width: Optional[int],
                   height: Optional[int],
                   fps_decimal: Optional[float],
                   *,
                   ndigits: Optional[int] = None) -> Optional[float]:
    """Bits per pixel = ``bitrate_bps / (width * height * fps)``.

    Returns None unless every input is present and positive — the same guard
    every former call site applied before dividing. ``ndigits`` rounds the
    result (history wants 6 dp); leave it None to return the raw float (the
    compress planner stores it unrounded).
    """
    if not bitrate_bps or not width or not height or not fps_decimal:
        return None
    if fps_decimal <= 0:
        return None
    bpp = bitrate_bps / (width * height * fps_decimal)
    return round(bpp, ndigits) if ndigits is not None else bpp


def video_stream_metrics(probe_json: dict,
                         *,
                         avg_fps_fallback: bool = False) -> dict:
    """Extract the first video stream's metrics from a parsed ffprobe dict.

    Returns a flat dict with keys ``width``, ``height``, ``codec_name``,
    ``pix_fmt``, ``r_frame_rate`` (the original fraction string) and
    ``fps_decimal`` (via :func:`parse_fps_fraction`). Every value is None when
    there is no video stream / the field is absent, so callers never KeyError.

    fps precedence:
      * default — ``r_frame_rate`` only (history / encode_resumable behaviour);
      * ``avg_fps_fallback=True`` — if ``r_frame_rate`` doesn't parse, retry
        with ``avg_frame_rate``.
    """
    streams = probe_json.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return {
            "width": None,
            "height": None,
            "codec_name": None,
            "pix_fmt": None,
            "r_frame_rate": None,
            "fps_decimal": None,
        }

    rfr = video.get("r_frame_rate")
    fps_decimal = parse_fps_fraction(rfr)
    if fps_decimal is None and avg_fps_fallback:
        fps_decimal = parse_fps_fraction(video.get("avg_frame_rate"))

    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "codec_name": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "r_frame_rate": rfr,
        "fps_decimal": fps_decimal,
    }

"""Human-readable formatting of the VMAF / PSNR / SSIM scores produced by
`quality.py`. Separated so the measurement core stays focused on running
libvmaf, and so adding new grading bands or alternate summary layouts
doesn't churn the measurement code.
"""
from __future__ import annotations


def _grade(vmaf: float | None) -> str:
    """VMAF score → one-line interpretation. The bands match the SKILL.md
    table verbatim; keep them in sync if either side changes."""
    if vmaf is None:
        return "?"
    if vmaf >= 95:
        return "VISUALLY TRANSPARENT (indistinguishable from source)"
    if vmaf >= 90:
        return "EXCELLENT (very close to source, sub-perceptual artifacts)"
    if vmaf >= 80:
        return "GOOD (minor compression artifacts visible on close inspection)"
    if vmaf >= 70:
        return "ACCEPTABLE (visible artifacts but still watchable)"
    if vmaf >= 50:
        return "DEGRADED (noticeable quality loss)"
    return "POOR (significant degradation — encode likely over-compressed)"


def format_quality_summary(scores: dict) -> str:
    """Pretty-print VMAF/PSNR/SSIM scores with an interpretation tag."""
    vmaf = scores.get("vmaf_mean")
    vmaf_min = scores.get("vmaf_min")
    psnr = scores.get("psnr_y_mean")
    ssim = scores.get("ssim_mean")
    n = scores.get("frames_evaluated", 0)

    mode = scores.get("sampling_mode", "?")
    lines = [f"  Quality vs source ({mode}, sampled {n} frames):"]
    if vmaf is not None:
        worst = f"worst frame: {vmaf_min:.1f}" if vmaf_min is not None else "worst: ?"
        lines.append(f"    VMAF:  {vmaf:6.2f}  |  {worst:>18s}  →  {_grade(vmaf)}")
    if psnr is not None:
        lines.append(f"    PSNR:  {psnr:6.2f} dB                  (>40 excellent, 30-40 good, <30 poor)")
    if ssim is not None:
        lines.append(f"    SSIM:  {ssim:6.4f}                     (>0.95 excellent, >0.90 good)")
    return "\n".join(lines)

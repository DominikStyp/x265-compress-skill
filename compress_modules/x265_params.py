"""x265 parameter constants. Pure data — no logic. See
`references/x265-tuning.md` for the rationale behind every knob."""
from __future__ import annotations


# Sharpness + motion oriented. Applied to every encode unless overridden by
# tune=animation / tune=grain in plan_encode().
BASE_X265_PARAMS: list[str] = [
    "psy-rd=2.0",            # psycho-visual RD: preserves texture, fights blur
    "psy-rdoq=2.0",          # psycho-visual at quantization stage: extra detail
    "aq-mode=3",             # autovariance + dark/bright bias: better motion in shadows
    "aq-strength=0.8",       # moderate AQ strength; too high softens edges
    "bframes=8",             # max B-frames; big win for motion-heavy content
    "b-adapt=2",             # full B-frame analysis (slower, smarter placement)
    "ref=5",                 # more reference frames -> better motion matching
    "me=star",               # most accurate motion search (slow but precise)
    "subme=4",               # deep subpixel refinement
    "merange=57",            # wider motion search radius for fast pans
    "rect=1",                # rectangular CU partitions
    "amp=1",                 # asymmetric motion partitions (fits motion better)
    "rd=4",                  # max useful rate-distortion analysis
    "rdoq-level=2",          # full RDO quantization
    "deblock=-1,-1",         # weaker deblocking -> sharper edges
    "sao=0",                 # SAO smooths small details; off for sharpness
    "strong-intra-smoothing=0",  # off; keeps I-frames sharp
    "weightp=2",             # weighted P-prediction (fades, lighting)
    "weightb=1",             # weighted B-prediction
]


# Picture formats considered "already 10-bit/12-bit". Output is still 10-bit
# by default, but the planner mentions this in `notes` if the source is 8-bit.
HIGH_BIT_DEPTH_FMTS: set[str] = {
    "yuv420p10le", "yuv420p12le", "yuv422p10le", "yuv444p10le",
    "p010le", "p016le",
}

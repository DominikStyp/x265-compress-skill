# x265 parameter rationale

This document explains every choice in `compress.py` so you can override defaults with intent. Read this when the user asks "why these params?" or when you need to edit the generated `.bat` for an unusual source.

## Goal hierarchy

The script optimises for, in order:

1. **No visible quality loss** vs. the source (the user's hard constraint).
2. **Sharpness** — output must never look softer than the source.
3. **Motion fidelity** — fast pans, action, sports must not smear or block.
4. **Size reduction** — at least 20%, ideally 30-50% on H.264 sources.

The defaults err on the side of (1)-(3). If the user wants more aggressive compression at the cost of (2)/(3), bump CRF rather than weakening the sharpness/motion params.

## CRF selection (bits-per-pixel bands)

| BPP (video bitrate / (W × H × fps)) | Source quality class | Default CRF |
|---|---|---|
| > 0.12 | Very high (master, untouched Blu-ray) | 18 |
| 0.06 - 0.12 | High (Blu-ray rip, high-bitrate web HD) | 19 |
| 0.025 - 0.06 | Medium (typical streaming) | 20 |
| < 0.025 | Low (already heavily compressed) | 21 + warning |

4K and above: +1 to CRF — denser pixel grid means visual artefacts are harder to see at the same CRF.

x265 source: clamp to CRF 22 minimum. Re-encoding HEVC → HEVC at the same CRF just bleeds quality with little size benefit.

**Archival / "visually lossless" override (CRF 17).** The auto-picker floors at CRF 18 — already visually lossless for normal viewing, so it never spends bits going lower on its own. For an archive you intend to keep (and maybe delete the source against later), drop to `--crf 17 --preset slower` by hand: CRF 17 is effectively transparent (VMAF typically ≥ 96) for a modest size cost over 18. This is the value the SKILL.md override table and `docs/AGENT_QUEUE_RECIPES.md` Recipe 4 ("visually lossless archival") use.

## The base `-x265-params` set

```
psy-rd=2.0:psy-rdoq=2.0:aq-mode=3:aq-strength=0.8:bframes=8:b-adapt=2:
ref=5:me=star:subme=4:merange=57:rect=1:amp=1:rd=4:rdoq-level=2:
deblock=-1,-1:sao=0:strong-intra-smoothing=0:weightp=2:weightb=1
```

### Sharpness group

| Param | Default in x265 | Our value | Why |
|---|---|---|---|
| `psy-rd` | 2.0 | 2.0 | Psycho-visual rate-distortion. At 0 the encoder optimises for PSNR and looks blurry. 2.0 is the sweet spot for live action. |
| `psy-rdoq` | 0.0 | 2.0 | Same idea, applied at quantization. Default is off; turning it on is the single biggest sharpness gain. Higher = sharper + bigger. |
| `sao` | 1 (on) | 0 | Sample-Adaptive Offset is a smoothing post-filter. It hides ringing but blurs fine detail. Disabling it is the standard "anti-blur" move. |
| `deblock` | 0,0 | -1,-1 | Negative values weaken the in-loop deblocker. -1,-1 is a gentle bias toward sharpness; -2,-2 risks visible block edges. |
| `strong-intra-smoothing` | 1 (on) | 0 | Only affects I-frames in large CUs. Smooths gradients but softens textured frames. Off keeps key-frames crisp. |

### Motion group

| Param | Default | Our value | Why |
|---|---|---|---|
| `bframes` | 4 | 8 | Maximum. More B-frames = better compression of smooth motion (panning, scrolling backgrounds). |
| `b-adapt` | 2 | 2 | Already max in most presets; explicit for clarity. Full lookahead chooses B-frame placement intelligently. |
| `ref` | 3-4 (preset-dep) | 5 | More reference frames = better motion matching across cuts and repeating patterns. Hits diminishing returns past 5. |
| `me` | hex (medium) / star (slow) | star | Most accurate motion search. Already default at `slow`; explicit keeps it on if user drops preset. |
| `subme` | 3 (slow) | 4 | Deeper sub-pixel refinement. 5 is also valid; the cost climbs fast. |
| `merange` | 57 (slow) | 57 | Search radius in pixels. 57 is enough for typical 1080p; bump to 64+ for fast 4K action only if you see motion artefacts. |
| `rect` | 1 (slow) | 1 | Allow rectangular CU partitions — fits non-square motion regions. |
| `amp` | 1 (slow) | 1 | Asymmetric motion partitions. Small win on top of `rect`. |
| `weightp` | 2 (slow) | 2 | Weighted P-prediction — handles fades and lighting changes. |
| `weightb` | 0 | 1 | Same for B-frames. Tiny cost, helps fades. |

### Adaptive quantization

| Param | Default | Our value | Why |
|---|---|---|---|
| `aq-mode` | 2 | 3 | Mode 3 (autovariance + biases dark/bright) preserves shadow detail and reduces banding in dark scenes — common failure mode for motion-heavy content shot at night. |
| `aq-strength` | 1.0 | 0.8 | High AQ strength softens edges. 0.8 keeps shadow detail benefits without much sharpness cost. |

### Rate-distortion

| Param | Default | Our value | Why |
|---|---|---|---|
| `rd` | 4 (slow) | 4 | Already max useful at `slow` preset. Above 4 the analysis cost explodes for negligible gain. |
| `rdoq-level` | 2 (slow) | 2 | Full RDO quantization. Required for `psy-rdoq` to do anything. |

## 10-bit output (`-pix_fmt yuv420p10le`)

Encoding to 10-bit even from 8-bit source:

- **~5-10% smaller** at the same perceived quality. The encoder's internal precision is higher, which mostly helps motion residuals.
- **No banding** in gradients (skies, fades to black).
- Plays back on essentially everything modern (any HEVC decoder from ~2017 onward, including iOS, Android, smart TVs).
- Older hardware (some smart TVs from before ~2016, some game consoles) may refuse it. That's what `--eight-bit` is for.

## HDR handling

When ffprobe reports `color_primaries=bt2020` or `color_transfer in {smpte2084, arib-std-b67}`, the script appends:

```
hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=<source_transfer>:colormatrix=bt2020nc
```

- `hdr-opt=1` — turns on HDR-specific rate-control tweaks.
- `repeat-headers=1` — writes VPS/SPS/PPS at every key-frame. Required for some players to recover after seeks.
- `colorprim` / `transfer` / `colormatrix` — explicit color metadata so decoders don't guess.

We do **not** currently pass `--master-display` or `--max-cll`. ffprobe sometimes has these in `side_data_list`; harvesting them would be a future improvement. Without them, HDR10 metadata won't survive the encode — the picture will still display correctly on HDR displays in most players, but precision tone-mapping is lost. Flag this to the user if the source is HDR10 mastering-display content.

HLG sources work without extra metadata (HLG is self-describing).

## When to override (cheat sheet for editing the `.bat`)

- **Source is animation / cartoon**: replace the whole `-x265-params` value with just `tune=animation`. `:tune=animation` re-tunes psy-rd, AQ, and deblock for flat-color content; our defaults are wrong for it.
- **Heavy film grain you want to keep**: use `:tune=grain` (replace base params). It bumps `aq-strength`, disables psy-rdoq, and bumps QP offsets for grainy regions so the grain survives instead of being quantized away into a blocky mess.
- **User has a target file size**: switch from CRF to two-pass ABR. Replace `-crf 19` with `-b:v <target_kbps>k` and run with `-pass 1` then `-pass 2`. The `.bat` would need restructuring; consider whether the user actually wants this or just wants "smaller".
- **Speed matters more than size**: drop preset to `medium`; the sharpness/motion params still help. Below `medium`, our params start getting ignored because the preset turns off the analysis they depend on.
- **Subtitles fail to mux**: change `-c:s copy` to `-c:s srt` (for text-based subs) or drop with `-sn`.

## What we are NOT doing and why

- **No two-pass.** CRF is rate-control by quality, which is what "no quality loss" actually means. Two-pass targets a bitrate, which is the wrong abstraction for this user's stated goal.
- **No GPU/ASIC encoding (NVENC, QSV, AMF, Apple VideoToolbox).** Hardware HEVC encoders are 3-5× faster and far lower-power, but produce noticeably worse quality at the same bitrate and aren't CRF-true. They're the right call for *playback* transcodes — and the wrong call for an archive you may delete originals against. On Apple Silicon, `hevc_videotoolbox` is especially tempting for its speed/efficiency; resist it for archival and keep the software libx265 default. The user explicitly asked for CPU x265.
- **No denoising, no scaling, no deinterlacing.** Out of scope — the user wants compression, not a filter chain. If a specific source needs `-vf yadif` (deinterlace) or `-vf hqdn3d` (denoise), add it manually after generation.
- **No `--tune psnr` or `--tune ssim`.** Both *lower* visual quality at the same CRF; they exist for benchmark cheating, not real viewing.

## CRF auto-pick vs. the size gate and retry chain (tuning for a budget)

`pick_crf()` (`compress_modules/plan.py`) chooses a starting CRF from the
source's bits-per-pixel, then `plan_encode()` adds **+1 for 4K** (`width ≥
3840`) because detail is denser per pixel there, and floors HEVC/AV1 sources at
`max(crf, 22)` (already-efficient codecs need more headroom). That value is a
quality **floor** — the *lowest* CRF the encoder will start at.

In queue mode the **size gate drives CRF up from that floor.** When
`max_size_percent` is set and `retry_with_bigger_crf` is on, a job whose
projected output exceeds the gate is aborted cheaply (the size-projection guard
stops it at ~5 % progress) and re-encoded at `crf + crf_step`, repeating until
it fits or `crf_max` is reached (then it ends as
`stopped-threshold-crf-exhausted`). So for 4K under a tight budget (e.g.
`max_size_percent ≤ 85`) the *effective* starting CRF users converge to is
often higher than the recipe's auto value — the shipped 4K CRFs frequently
produce output larger than an 85 % gate, forcing a retry chain.

**Practical guidance:** if you routinely encode size-constrained 4K, set a
higher starting `crf` (≈23 is a common convergence point on a live-action 4K
library at `max_size_percent: 85`) rather than only raising `crf_max`. Raising
the starting CRF reaches the target in fewer retry passes; raising only
`crf_max` just lets a too-low start climb further, wasting rejected passes. The
auto floor is deliberately conservative (quality-first) and is **not** changed
by default — tuning for size is a per-job / `defaults` `crf` override, not a
silent default shift.

> The reverse case — a source that's *already* small enough — never triggers
> the retry chain, so the auto floor stands and you get the highest quality the
> budget allows. The gate only ever raises CRF, never lowers it.

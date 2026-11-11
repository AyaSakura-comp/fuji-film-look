# Recipe and darktable internals

## Pipeline (one history stack, darktable 5.6.2 module versions)

| Module | Version | Size (bytes) | Role |
|---|---|---|---|
| colorbalancergb | 5 | 132 | global chroma down, cool lift in shadows, slight warm gain in highlights, small contrast |
| rgbcurve | 1 | 516 | film tone curve, per channel (`preserve_colors=none`): lifted toe (faded blacks), gentle S, top node <1 (roll-off, no clipping) |
| colorzones | 5 | 520 | per-hue lightness, chroma and hue offsets (select by hue, smooth mode, Catmull-Rom) |
| grain | 2 | 16 | fine lightness grain |
| bilat (local contrast) | 3 | 20 | street look only: local laplacian, detail +35% |
| vignette | 4 | 44 | edge darkening (street: strong, wide) |

Pipeline order: written explicitly as a custom order (`iop_order_version=0`). It is darktable's v5.0 order (`v50_order` for RAW, `v50_jpg_order` otherwise) with `rgbcurve` moved right after `vignette`. Without it darktable falls back to the legacy 2.6 order.

Blend params: blendop v14 with masks off (`gz11eNpj...`, taken from a built-in preset).

## Struct layouts (little-endian, packed as C structs)

- colorbalancergb: 32 × float, then int `saturation_formula` (1 = darktable UCS). The floats are, in order: shadows Y C H, midtones Y C H, highlights Y C H, global Y C H; shadows_weight, white_fulcrum, highlights_weight; chroma shadows/highlights/global/midtones; saturation global/highlights/midtones/shadows; hue_angle; brilliance global/highlights/midtones/shadows; mask_grey_fulcrum; vibrance; grey_fulcrum; contrast. Hue is in degrees. Shadows = lift, midtones = power, highlights = gain.
- rgbcurve: nodes[3][20]{x,y}, int num_nodes[3], int type[3] (2 = monotone hermite), int autoscale (0 = linked), int compensate_middle_grey, int preserve_colors (0 none, 1 luminance). Nodes are in **linear** RGB. The script designs them in gamma 2.2 and converts with v^2.2.
- colorzones: int channel (2 = hue), nodes[3][20]{x,y} (curves L, C, h), int num_nodes[3], int type[3] (1 = Catmull-Rom), float strength, int mode (0 smooth), int splines_version (1). The effect is L × 2^(4(y−0.5)), C × 2y, h + (y−0.5) turns. x is CIELab hue / 360.
- bilat: int mode (1 = local laplacian), float sigma_r (highlights), float sigma_s (shadows), float detail, float midtone.
- grain: int channel (2 = lightness), float scale (= coarseness / 213.2), float strength, float midtones_bias.
- vignette: float scale, falloff_scale, brightness, saturation; float center x, y; int autoratio; float whratio, shape; int dithering; int unbound.

Source of truth: `src/iop/<module>.c` at tag `release-5.6.2` (`DT_MODULE_INTROSPECTION(version, ...)` and `typedef struct dt_iop_<module>_params_t`). If darktable bumps a module version, update its packer and the version number in `fuji_look.py`.

## Why these choices

- Classic Chrome character: muted chroma, darker and denser reds and blues, olive greens, hard shadows with lifted black, neutral-to-slightly-warm highlights.
- Classic Neg character: higher contrast, cyan/teal shadows, warm highlights, greens pushed toward teal, magentas muted.
- The per-channel curve was chosen over a luminance-preserving one because the luminance norm let saturated highlights clip (verify caught +0.4% clipping).
- Orange/skin gets a positive chroma offset because the fade and roll-off alone desaturate skin by about 20%.

- 200-photo Sony test (2026-11-08): 200 distinct ARWs from pixls.us Play Raw (CC BY-SA; 42 models from NEX-5N/A230 to A1/A7RV/A7CII; landscapes, night, moon, people, pets, flowers, interiors), picked by model round-robin from 323 found. `portrait`, default settings: 108 s (0.54 s/photo; analyse 6.6 s, render+post overlap 101 s), 199/200 (the one: vivid pink flower field whose source clips 2.9%, output 1.4% — passes with `--verify full`). `--verify full`: 177 s, 200/200. Output 616 MB (3.2 MB/file, q100 4:4:4, 2048 px).
- Portrait character, measured from the user's two doll-photography references (forest doll in a red dress, two dolls with a guitar): L p1 13–20 (soft matte), mid-tone L std 16–17 (gentle), mean chroma 5–11.5, neutrals slightly warm (a +1–2, b +3–6), red dress C 22–33 at hue 16–26° (crimson), skin hue 50–57° C 15–16 with pink cheeks, cream apron C 6 hue 62°, greens C 10–15 (muted). The look keeps colour in the shadows (no `chroma_shadows`), shifts reds 6° toward crimson and orange/skin 4° toward pink, cuts green ×0.56, cyan ×0.64, yellow ×0.84, and tints highlights warm-pink (hue 30°). 63/63 PASS with `--verify full` (58 lighting/RAW set + DSC04537 + 4 iPhone doll/cat JPEGs).
- Street character, measured from the user's three reference shots (Japanese stations by day and an izakaya at night): L p1 7–14 (matte), mid-tone L std 22–26 by day, mean chroma 4–8, shadow chroma about 1 (colourless), sky hue 241–252° with L 50–57 and C 15–18, warm lights at C about 31, corners 30–60% darker than the centre, and grain clearly visible in flat sky. Colour zones blend out low-chroma pixels, so a hazy sky in the source can't be turned deep blue. The look darkens and deepens skies that already have some colour.

## Tested on

- Harbor, daylight (Nikon Z fc out-of-camera JPEG, Wikimedia Commons): both looks PASS.
- Night street market (Sony A7 IV out-of-camera JPEG, Wikimedia Commons): both looks PASS.
- Street look on the harbor, night market, and two Shikoku station phone shots (Wikimedia Commons), checked against the user's references: every hard check PASS. The remaining ref-match WARNs are content-driven (low-chroma night source, overcast sky, blue railings).
- Sony A7 III ARW (raw.pixls.us), compared against darktable's default render: PASS.
- 2026-11 calibration batch: 20 Sony ARWs from raw.pixls.us (A1-class bodies, A7 C/CII/III/IV/SII/SIII, A9, A6000/6100/6300/6400/6500/6600, RX100 VA/VII, RX10 IV, ZV-1, ZV-E1, ZV-E10, FX3) plus 4 JPEGs, each against darktable's default render. classic-chrome, classic-neg and street all pass 24/24.

## Fast pipeline (run.py) benchmark, 2026-11, Mac mini M4 (10 cores, 16 GB)

| Setup (100 × 24 MP Sony ARW, street) | Wall time |
|---|---|
| old: one darktable-cli per photo, 1600 px hq, darktable grain | ~7 s/photo, about 12 min serial (~2.5 min at 4 jobs for 20) |
| 1 process, --import ×100, 2048 hq=false, darktable grain | 110 s |
| same, grain off (numpy grain instead) | 49 s (1 proc) / 41 s (2) / 39 s (3) |
| run.py end to end (render + grain + verify + report), 3 looks | 44.7–48.9 s, 100/100 PASS each |
| run.py with best-quality output (16-bit TIFF hand-off, JPEG q100 4:4:4), back-to-back with the old q92 path | street 51.5 s vs 50.0 s old; chrome 47.4 s; neg 47.2 s; 100/100 PASS each; 3.5 MB/file (old 1.0) |
| auto lighting cost, same machine state, back to back (machine under memory pressure: the pre-auto code took 63.1 s here vs 51.5 s idle) | pre-auto code 63.1 s; `--no-auto` 65.0/65.9 s; auto 68.0/68.3 s → auto ≈ +5 s (+8%), ≈55–56 s on the idle machine. Analysis alone 3.5–3.9 s / 100 RAWs |
| run.py `--size 0` (full res, q100 4:4:4), 20 RAWs 20–42 MP | 54.7 s (~2.7 s/photo), 20/20 PASS, 22 MB avg / 38 MB max per JPEG, peak RSS ~5 GB |

darktable start-up is 3.9–4.3 s per process, even with a warm config dir. OpenCL on the M4 GPU was slower than CPU (9.5 s vs 8.3 s for one full-res file).
Per-module time at 2048 px: grain 0.71 s, demosaic 0.12, colorbalancergb 0.11, bilat 0.05, the rest <0.03 each.
Numpy grain vs darktable grain (street, 8-bit std of the grain field per tone band 20-60/60-120/120-180/180-230): 3.2/5.9/7.1/6.5 vs 2.4/5.9/6.8/6.2. Neighbour autocorrelation 0.15 vs 0.20.

### JPEG output fidelity (2026-11-08)
Ground truth = darktable 16-bit render + float grain, never quantised. 6 files (4 Sony ARW, 2 JPEG), street look, 2048 px:

| Path | PSNR | mean ΔE76 | ΔE76 p99.9 | size |
|---|---|---|---|---|
| old: darktable JPEG q96 (4:2:0) → PIL q92 (4:2:0) | 33.4–39.1 dB | 1.3–3.7 | 8–18 | ~1.1 MB |
| new: darktable 16-bit TIFF → PIL q100 4:4:4 optimised | 49.7–51.3 dB | 0.45–0.65 | 2.6–2.9 | ~3.7 MB |

The 4:2:0 subsampling and the double encode visibly smeared the grain and fine colour (4x crops: mean error 5.6–6.2 levels old vs 0.63 new). PIL decodes darktable's TIFF (RowsPerStrip=1) in ~0.8 s and only to 8 bit, so run.py maps the strips into numpy directly (~5 ms for 16-bit 2048 px).

## Auto lighting (scene.py) — module params and calibration (2026-11-08)

Per-photo modules written by `fuji_look.correction_entries()` (darktable 5.6.2, little-endian):

| Module | Version | Size | Layout / values |
|---|---|---|---|
| exposure (instance 0, replaces darktable's auto-applied one) | 7 | 28 | `<iffffii`: mode 0 manual, black (RAW default −0.000244140625, + haze offset), exposure (RAW 0.7 + auto EV; JPEG = auto EV), deflicker percentile 50, target −4, compensate_exposure_bias (RAW 1), compensate_hilite_pres (RAW 1) |
| channelmixerrgb (instance 1, "auto wb") | 3 | 160 | `<24f6i4i4fii`: red/green/blue rows identity, saturation/lightness/grey 0, normalize ×6 = 0, illuminant 7 CUSTOM, fluo 2, led 4, adaptation 1 CAT16, x, y, temperature (unused), gamut 1.0, clip 1, version 2 (= v3 algorithm). iop order: `channelmixerrgb,1` right after `channelmixerrgb,0` |
| shadhi (backlit) | 5 | 48 | `<i8fIfi`: order 0, radius 100, shadows 20–45, whitepoint 0, highlights −25, reserved 0, compress 50, shadows_ccorrect 100, highlights_ccorrect 50, flags 127 (UNBOUND_DEFAULT), low_approximation 1e-6, algo 1 bilateral |
| colorzones (instance 1, "reds first", street) | 5 | 520 | same layout as instance 0, only `pre_zone_L` (magenta-red −0.04, red −0.08, magenta −0.02); iop order: `colorzones,1` right before `colorbalancergb,0` |

darktable's own defaults read back from an export (to replicate exactly): exposure +0.7 EV, black −0.000244, compensate bias on; channelmixerrgb illuminant D (2) with the as-shot xy/CCT (e.g. 0.3685, 0.3771, 4339 K), CAT16; sigmoid contrast 1.5, skew 0, target black 0.0152 %, white 100 %.

The WB instance adapts from `xy = D50 + frac · (white_D50 − D50)`, where `white_D50` is the white estimate (linear sRGB) → XYZ (D65) → Bradford D65→D50 → xy. frac = 1 would fully neutralise; the plan uses 0.5–0.9.

Tone prediction (`TONE_X` → `TONE_Y`): binned medians of (log2 Y percentile from rawpy, L* percentile of darktable's default render) over 54 RAWs × 10 percentiles, made monotone. Residual std: p2–p10 0.4–0.5, p25 0.8, p50 1.0, p75 1.4, p90–p95 1.6, p98+ ~3 (clipped highlights).

Camera embedded previews vs darktable default render: median L corr 0.81, resid std 8 L* (bias compensation, DRO, picture styles incl. monochrome) → not used. rawpy half-size: median L resid 1.1 L*, ~0.1 s per 24 MP ARW (0.05 s unpack + 0.05 s half-size).

### Lighting test set (34 Play Raw files from discuss.pixls.us, CC BY-SA, + 20 Sony RAWs + 4 JPEGs)
Night neon (sign, palace, pizzeria CR3), sodium street (cat NEF, Prague DNG), snow at night (sodium), tungsten bar (NEF), blue hour city, dusk station, backlit sun / drinks, golden-hour portrait and church, fog, haze with blue cast, harsh midday alley/mountain, snow high-key, overcast, shade portrait, under-exposed landscape / egret, blown sky, museum, library interior (X-Trans DNG), Tokyo daytime ("night" in the title but EV100 13.7).

| Run (street unless noted, `--verify full`) | Result |
|---|---|
| before auto, first 24 files | 16/24 PASS: near-black night shots, orange sodium snow, neon washed grey (chroma ratio 0.20), dark backlit portraits |
| auto, 34 lighting files | 32/34 → after backlit/haze/check fixes 34/34 |
| auto, all 58 files, street / classic-chrome / classic-neg | 58/58, 58/58, 58/58 |

### Spotlight / pool of light (2026-11-11)
Halloween doll shoot, 67 Sony A7CR ARWs (EV100 6–9 → dim, dark set, doll under a spot): the dim rule's median target (p50 → 25) asked for +1.4–1.5 EV on 62 frames. Measured against the camera JPEGs (aligned, same crop): camera L p50 ≈ 10, p95 ≈ 60; old output p50 29, p95 82. Fixed exposures on 6 frames (+0, +0.4, +0.7 EV) showed the subject at p95 60–67 with the surround dark was right. `spot_weight` = clip((dark − 0.30)/0.15) × clip((L p95 − 30)/10) (dark = share of L < 8 in darktable's default render); the subject target is p95 → 60, max +0.8 EV, p99 ≤ 88, blended with the old dim rule by the weight. Result: 62 frames get +0.3–1.0 EV instead of +1.2–1.5. `--verify full`: 65/67 before the rule; 63/67 with the rule and the old reds bound (DSC04533/04534 at 0.80, DSC04539 at 0.79, DSC04519); **66/67** with the 0.78 bound (only DSC04519, a near-black tissue-box test shot). Portrait `reds kept` lower bound 0.80 → 0.78: dark crimson under dim light measures 0.79–0.80 and looks identical to 0.82 at 100%.
Spatial metrics (top-20% blocks vs median block in stops, lit/dark block shares on a 10×10 grid) were computed but not needed: the dim class plus the dark share and p95 already isolate these frames on every test set.

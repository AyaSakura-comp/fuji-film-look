#!/usr/bin/env python3
"""Apply a Fujifilm-style film look to a photo using darktable-cli.

Builds a darktable XMP sidecar (color balance rgb + rgb curve + color zones +
grain + vignette) whose binary module params are packed to match darktable
5.6.x, then renders it with darktable-cli.

stdlib only. Usage:
  fuji_look.py INPUT [-o OUTPUT] [--look classic-chrome|classic-neg|street|portrait]
               [--strength 1.0] [--brightness 0] [--grain [AMOUNT]] [--vignette 1.0]
               [--red-lightness -0.5 ...] [--xmp-only FILE] [--width N]
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import math
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

# Hard dependency: darktable 5.6.x. The module params below are packed for darktable 5.6
# module versions (tested on 5.6.2); older darktable cannot read them.
DARKTABLE_REQUIRED = (5, 6)
DARKTABLE_TESTED = "5.6.2"
DARKTABLE_CLI_CANDIDATES = [
    os.environ.get("DARKTABLE_CLI", ""),
    "/Applications/darktable.app/Contents/MacOS/darktable-cli",
    str(Path.home() / "Applications/darktable.app/Contents/MacOS/darktable-cli"),
    shutil.which("darktable-cli") or "",
]
DARKTABLE_HINT = (
    f"This skill depends on darktable {DARKTABLE_REQUIRED[0]}.{DARKTABLE_REQUIRED[1]}.x (darktable-cli). "
    "Run the skill's install.sh (macOS: installs the official darktable "
    f"{DARKTABLE_TESTED} DMG after checking its SHA-256), install it from "
    "https://www.darktable.org/install/, or set DARKTABLE_CLI to the darktable-cli binary."
)

# blendop v14 blobs (420 bytes) taken from darktable 5.6.2 built-in presets.
# mask_mode = 0 (blending off) so the colorspace field is irrelevant.
BLENDOP_VERSION = 14
BLENDOP_HEX_PREFIX = None  # computed lazily from the gz string below
BLENDOP_GZ = "gz11eNpjYGBgYAZiCQYYOOHEgAZYGTABIwM20GAPwSOVT13wHwhANABvUh0I"


# darktable 5.6 default pipeline orders (src/common/iop_order.c: v50_order /
# v50_jpg_order). Without an explicit order darktable falls back to the
# *legacy* (2.6) order, so we always write a custom order.
IOP_V50 = "rawprepare invert temperature rasterfile highlights cacorrect hotpixels rawdenoise demosaic denoiseprofile bilateral rotatepixels scalepixels lens cacorrectrgb hazeremoval ashift flip enlargecanvas overlay clipping liquify spots retouch exposure tonemap toneequal crop graduatednd equalizer colorin channelmixerrgb diffuse censorize negadoctor blurs primaries nlmeans colorchecker neutral defringe atrous lowpass highpass sharpen colortransfer colormapping channelmixer basicadj colorharmonizer colorbalance colorequal colorbalancergb rgbcurve rgblevels basecurve filmic sigmoid agx filmicrgb lut3d colisa tonecurve levels shadhi zonesystem globaltonemap relight bilat colorcorrection colorcontrast velvia vibrance colorzones bloom colorize lowlight monochrome grain soften splittoning vignette colorreconstruct finalscale colorout clahe overexposed rawoverexposed dither borders watermark gamma".split()
IOP_V50_JPG = "rawprepare invert temperature rasterfile highlights cacorrect hotpixels rawdenoise demosaic colorin denoiseprofile bilateral rotatepixels scalepixels lens cacorrectrgb hazeremoval ashift flip enlargecanvas overlay clipping liquify spots retouch exposure tonemap toneequal crop graduatednd equalizer channelmixerrgb diffuse censorize negadoctor blurs primaries nlmeans colorchecker neutral defringe atrous lowpass highpass sharpen colortransfer colormapping channelmixer basicadj colorharmonizer colorbalance colorequal colorbalancergb rgbcurve rgblevels basecurve filmic sigmoid agx filmicrgb lut3d colisa tonecurve levels shadhi zonesystem globaltonemap relight bilat colorcorrection colorcontrast velvia vibrance colorzones bloom colorize lowlight monochrome grain soften splittoning vignette colorreconstruct finalscale colorout clahe overexposed rawoverexposed dither borders watermark gamma".split()
RAW_EXT = {".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".orf", ".rw2", ".pef", ".srw", ".nrw", ".3fr", ".iiq"}


def iop_order_list(is_raw: bool, wb_instance: bool = False, pre_zones: bool = False,
                   hi_zones: bool = False) -> str:
    """Default v5 order with rgbcurve moved after vignette, so the film curve
    (faded blacks, highlight roll-off) acts on the final display-referred
    image: after sigmoid/filmic tone mapping for RAW, and after the vignette
    so the matte black floor survives in the corners."""
    order = list(IOP_V50 if is_raw else IOP_V50_JPG)
    order.remove("rgbcurve")
    order.insert(order.index("vignette") + 1, "rgbcurve")
    items = [f"{op},0" for op in order]
    if wb_instance:  # 2nd colour-calibration instance (auto WB fix) right after the default one
        items.insert(items.index("channelmixerrgb,0") + 1, "channelmixerrgb,1")
    if pre_zones:  # 2nd color-zones instance BEFORE color balance rgb: acts on full-chroma colours
        items.insert(items.index("colorbalancergb,0"), "colorzones,1")
    if hi_zones:  # 3rd color-zones instance (chroma by lightness) right after the main one
        items.insert(items.index("colorzones,0") + 1, "colorzones,2")
    return ",".join(items)

# ---------------------------------------------------------------- looks ---
# Every number below is a *neutral offset* so --strength can scale it.
# Hue positions for color zones are CIELab hue / 360 (darktable Lab, D50):
#   red ~0.10  orange/skin ~0.17  yellow ~0.27  foliage ~0.33  green ~0.40
#   cyan ~0.54  sky ~0.72  blue ~0.84  magenta ~0.92
HUE_X = [0.02, 0.10, 0.17, 0.27, 0.36, 0.54, 0.72, 0.84, 0.93]

# Highlight shoulder (all looks): chroma scale by lightness (x = L/100, scale), applied by
# a 3rd color-zones instance after the main one. Film desaturates as it nears white; it
# keeps saturated sunlit colours (pink petals, red flowers, the sun) inside sRGB now that
# the curve's white point is ~L 98 instead of 94-96. Skin (L 55-75) loses <=4% chroma.
HI_CHROMA = [(0.0, 1.0), (0.55, 1.0), (0.66, 0.98), (0.76, 0.92), (0.86, 0.83), (1.0, 0.70)]

LOOKS = {
    # muted, documentary, deep cyan-blue skies, darker restrained reds,
    # olive greens, hard-ish shadows, faded blacks
    "classic-chrome": dict(
        # tone curve in *perceptual* (gamma 2.2) coordinates, (x, y) pairs.
        # Top end (all looks): white -> 0.965-0.975 (L* ~97-98) with a shoulder from ~L 85.
        # Was 0.935-0.95 (white capped at L* 94-96): whites, frost, white walls and
        # overcast skies read grey on the 200-RAW test (2026-11-11, references/recipe.md)
        curve=[(0.0, 0.095), (0.10, 0.138), (0.25, 0.242), (0.50, 0.500),
               (0.75, 0.772), (0.90, 0.900), (1.0, 0.975)],
        # color balance rgb
        chroma_global=-0.11, saturation_global=0.0, vibrance=0.0,
        contrast=0.05,
        shadows=(205.0, 0.030), highlights=(65.0, 0.012),  # (hue deg, chroma)
        saturation_shadows=0.0, saturation_highlights=-0.05,
        # color zones offsets per HUE_X node: lightness (stops-ish/4), chroma (x), hue (turns)
        zone_L=[-0.015, -0.030, -0.005, 0.000, -0.012, -0.010, -0.040, -0.030, -0.010],
        zone_C=[-0.04, -0.05, 0.060, -0.05, -0.14, -0.05, -0.030, -0.050, -0.06],
        zone_h=[0.000, 0.000, 0.000, -0.005, -0.015, -0.010, -0.012, 0.000, 0.000],
        grain=(900.0, 9.0), vignette=-0.12,
    ),
    # Classic Neg / Superia-like: punchier contrast, teal shadows, warm
    # highlights, deep reds, greens pushed toward teal
    "classic-neg": dict(
        curve=[(0.0, 0.090), (0.10, 0.125), (0.25, 0.228), (0.50, 0.500),
               (0.75, 0.787), (0.90, 0.908), (1.0, 0.975)],
        chroma_global=-0.10, saturation_global=0.0, vibrance=0.0,
        contrast=0.05,
        shadows=(195.0, 0.045), highlights=(55.0, 0.020),
        saturation_shadows=0.04, saturation_highlights=-0.06,
        zone_L=[-0.020, -0.035, 0.000, 0.005, -0.015, -0.010, -0.030, -0.020, -0.015],
        zone_C=[-0.02, -0.03, 0.060, -0.05, -0.16, -0.03, 0.020, 0.000, -0.08],
        zone_h=[0.000, -0.005, 0.000, -0.008, 0.020, -0.008, -0.010, 0.000, 0.000],
        grain=(1100.0, 11.0), vignette=-0.15,
    ),
    # Japanese street / "Fuji street" look (matched to user reference shots):
    # matte lifted blacks, strong mid contrast, very low overall chroma with
    # colourless shadows, deep steel-blue skies that keep their colour, warm
    # practical lights kept, crunchy local contrast, heavy grain + vignette
    "street": dict(
        curve=[(0.0, 0.135), (0.08, 0.150), (0.20, 0.205), (0.50, 0.510),
               (0.75, 0.792), (0.90, 0.910), (1.0, 0.965)],
        chroma_global=-0.10, saturation_global=-0.03, vibrance=0.0,
        contrast=0.07,
        shadows=(220.0, 0.006), highlights=(225.0, 0.020),
        saturation_shadows=-0.20, saturation_highlights=-0.08,
        chroma_shadows=-0.40, chroma_highlights=-0.10,
        zone_L=[-0.030, -0.060, 0.000, 0.000, -0.020, -0.020, -0.085, -0.050, -0.020],
        pre_zone_L=[-0.040, -0.080, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, -0.020],
        zone_C=[-0.06, -0.04, 0.180, 0.070, -0.150, 0.030, 0.100, 0.000, -0.100],
        zone_h=[0.000, 0.000, 0.000, -0.005, 0.030, 0.010, -0.022, -0.028, 0.000],
        grain=(1700.0, 34.0, 55.0), vignette=-0.26, vignette_shape=(55.0, 85.0),
        local_contrast=0.35,
    ),
    # people & dolls (Pro Neg / Nostalgic Neg feel, matched to the user's doll
    # photography references): soft matte blacks, gentle contrast, warm
    # pink-cream whites, rosy skin, crimson reds kept saturated, muted
    # olive/teal greens and blues, light vignette. No shadow desaturation:
    # dark red dresses and roses must stay red, not turn brown.
    "portrait": dict(
        curve=[(0.0, 0.120), (0.10, 0.160), (0.25, 0.262), (0.50, 0.505),
               (0.75, 0.762), (0.90, 0.893), (1.0, 0.972)],
        chroma_global=-0.04, saturation_global=0.0, vibrance=0.0,
        contrast=0.0,
        shadows=(40.0, 0.008), highlights=(30.0, 0.015),
        saturation_shadows=0.0, saturation_highlights=-0.06,
        chroma_shadows=0.0, chroma_highlights=-0.10,
        zone_L=[0.010, 0.015, 0.010, 0.000, -0.020, -0.020, -0.030, -0.020, 0.000],
        zone_C=[0.03, 0.06, 0.030, -0.08, -0.22, -0.18, -0.12, -0.10, -0.04],
        zone_h=[0.000, -0.018, -0.012, -0.012, 0.010, 0.000, 0.000, 0.000, 0.000],
        grain=(900.0, 8.0), vignette=-0.10,
        wb_amber_min_c=20.0,  # doll exhibit under warm light measured 23-29; orange cat 18-28
    ),
}

ZONE_NAMES = ["magenta-red", "red", "orange", "yellow", "green", "cyan", "sky", "blue", "magenta"]


# ------------------------------------------------------------- encoders ---
def hexs(b: bytes) -> str:
    return b.hex()


def gz(b: bytes) -> str:
    z = zlib.compress(b, 9)
    factor = min(len(b) // len(z) + 1, 99)
    return "gz%02d" % factor + base64.b64encode(z).decode()


def p_colorbalancergb(look, s, brightness, shadows_lift, highlights_gain):
    sh_h, sh_c = look["shadows"]
    hi_h, hi_c = look["highlights"]
    v = [
        shadows_lift, sh_c * s, sh_h,          # shadows lift  Y C H
        brightness, 0.0, 0.0,                  # power (midtones) Y C H
        highlights_gain, hi_c * s, hi_h,       # highlights gain Y C H
        0.0, 0.0, 0.0,                         # global offset Y C H
        1.0, 0.0, 1.0,                         # shadows_weight, white_fulcrum, highlights_weight
        look.get("chroma_shadows", 0.0) * s, look.get("chroma_highlights", 0.0) * s,
        look["chroma_global"] * s, 0.0,        # chroma shadows/highlights/global/midtones
        look["saturation_global"] * s, look["saturation_highlights"] * s, 0.0,
        look["saturation_shadows"] * s,        # saturation global/highlights/midtones/shadows
        0.0,                                   # hue_angle
        0.0, 0.0, 0.0, 0.0,                    # brilliance global/hi/mid/shadows
        0.1845,                                # mask_grey_fulcrum
        look["vibrance"] * s, 0.1845, look["contrast"] * s,
    ]
    assert len(v) == 32
    return struct.pack("<32fi", *v, 1)  # saturation_formula = darktable UCS


def p_rgbcurve(look, s):
    # perceptual -> linear (darktable rgb curve works on linear RGB, middle grey
    # compensation off). Scale deviation from identity by strength.
    nodes = []
    for x, y in look["curve"]:
        y = x + (y - x) * s
        y = min(max(y, 0.0), 1.0)
        nodes.append((x ** 2.2, y ** 2.2))
    curves = []
    for ch in range(3):
        pts = nodes if ch == 0 else [(0.0, 0.0), (1.0, 1.0)]
        flat = []
        for i in range(20):
            flat += list(pts[i]) if i < len(pts) else [0.0, 0.0]
        curves.append(flat)
    data = [f for c in curves for f in c]
    num = [len(nodes), 2, 2]
    types = [2, 2, 2]  # MONOTONE_HERMITE
    # autoscale linked RGB, no middle grey compensation, preserve colors = none
    # (per-channel like film: the top node < 1 guarantees no channel clips)
    return struct.pack("<120f3i3iiii", *data, *num, *types, 0, 0, 0)


def p_colorzones(look, s, overrides):
    L = list(look["zone_L"])
    C = list(look["zone_C"])
    H = list(look["zone_h"])
    for name, (kind, val) in overrides.items():
        i = ZONE_NAMES.index(name)
        {"L": L, "C": C, "h": H}[kind][i] = val
    curves = []
    for offs in (L, C, H):
        flat = []
        for i in range(20):
            if i < len(HUE_X):
                flat += [HUE_X[i], 0.5 + offs[i] * s]
            else:
                flat += [0.0, 0.0]
        curves.append(flat)
    data = [f for c in curves for f in c]
    n = len(HUE_X)
    # channel = hue (2); CATMULL_ROM; strength 0; mode smooth; splines v2
    return struct.pack("<i120f3i3ifii", 2, *data, n, n, n, 1, 1, 1, 0.0, 0, 1)


def p_colorzones_hi(nodes, s):
    """color zones selected by lightness: chroma scale per L (0..1). Film-like
    shoulder desaturation: saturated colours near white stay inside sRGB while
    neutral whites keep the full white point."""
    flat = lambda pts: [f for i in range(20) for f in (pts[i] if i < len(pts) else (0.0, 0.0))]
    keep = [(0.0, 0.5), (1.0, 0.5)]
    cpts = [(x, 0.5 + (k / 2 - 0.5) * s) for x, k in nodes]
    data = flat(keep) + flat(cpts) + flat(keep)
    # channel = lightness (0); CATMULL_ROM; strength 0; mode smooth; splines v2
    return struct.pack("<i120f3i3ifii", 0, *data, 2, len(cpts), 2, 1, 1, 1, 0.0, 0, 1)


def p_grain(coarseness, strength, midtones_bias=100.0):
    return struct.pack("<ifff", 2, coarseness / 213.2, strength, midtones_bias)


def p_bilat(detail):
    # local contrast, local laplacian mode, default shadows/highlights 50%
    return struct.pack("<iffff", 1, 0.5, 0.5, detail, 0.5)


def p_vignette(brightness, shape=(80.0, 60.0)):
    return struct.pack("<4f2fiffii", shape[0], shape[1], brightness, -0.10, 0.0, 0.0,
                       1, 1.0, 1.0, 0, 1)


# ------------------------------------------------------------------ xmp ---
def build_xmp(entries, is_raw=False) -> str:
    """entries: (op, modversion, params[, multi_priority, multi_name])"""
    lis = []
    wb_instance = pre_zones = hi_zones = False
    for i, e in enumerate(entries):
        op, ver, params = e[:3]
        prio, mname = (e[3], e[4]) if len(e) > 3 else (0, "")
        wb_instance |= (op == "channelmixerrgb" and prio == 1)
        pre_zones |= (op == "colorzones" and prio == 1)
        hi_zones |= (op == "colorzones" and prio == 2)
        lis.append(
            f'''     <rdf:li
      darktable:num="{i}"
      darktable:operation="{op}"
      darktable:enabled="1"
      darktable:modversion="{ver}"
      darktable:params="{hexs(params)}"
      darktable:multi_name="{mname}"
      darktable:multi_name_hand_edited="{1 if mname else 0}"
      darktable:multi_priority="{prio}"
      darktable:blendop_version="{BLENDOP_VERSION}"
      darktable:blendop_params="{BLENDOP_GZ}"/>''')
    body = "\n".join(lis)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:darktable="http://darktable.sf.net/"
   darktable:xmp_version="5"
   darktable:raw_params="0"
   darktable:auto_presets_applied="0"
   darktable:iop_order_version="0"
   darktable:iop_order_list="{iop_order_list(is_raw, wb_instance, pre_zones, hi_zones)}"
   darktable:history_end="{len(entries)}">
   <darktable:history>
    <rdf:Seq>
{body}
    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
'''


# ------------------------------------------------- per-photo corrections ---
# (computed by scene.py in auto mode; see SKILL.md "Different lighting")
def p_exposure(ev, is_raw, black=0.0):
    """exposure v7. RAW: darktable's own default (+0.7 EV, compensate camera
    exposure bias, highlight-preservation compensation) plus `ev`.
    Other inputs: plain `ev`. black > 0 deepens the blacks (haze/flat light)."""
    if is_raw:
        return struct.pack("<iffffii", 0, -0.000244140625 + black, 0.7 + ev, 50.0, -4.0, 1, 1)
    return struct.pack("<iffffii", 0, black, ev, 50.0, -4.0, 0, 0)


def p_shadhi(shadows, highlights):
    """shadows and highlights v5 (local tone, bilateral): lifts a dark
    foreground against a bright background without touching the sky."""
    unbound = 1 | 2 | 4 | 8 | 16 | 32 | 64  # UNBOUND_DEFAULT
    return struct.pack("<i8fIfi", 0, 100.0, shadows, 0.0, highlights, 0.0, 50.0, 100.0, 50.0,
                       unbound, 0.000001, 1)


# Sign recovery (night/dim RAWs with blown light boxes, scene.sign_weight): tone equalizer
# bands in EV for its mask at -8..0 EV. The mask is shifted by SIGN_MASK_EV, so the bands
# act on scene-linear -6..+2 EV after darktable's exposure. In darktable's default render
# -1 EV is ~L 78 (-0.1 EV), 0 EV ~L 90 (-0.5), the clip point +0.7 ~L 97 (about -1.2),
# +2 EV -2.8: shadows, mid-tones and skin do not move; a light box comes back below white
# with its lettering (the RAW keeps it under the clip point). Guided filter, wide
# smoothing: the whole sign gets one correction, so its local contrast is kept.
# Kumamoto night streets (2026-11-14): lettering of white light boxes and arcade signs
# readable again, orange signs orange instead of salmon; overall brightness unchanged.
SIGN_BANDS = (0.0, 0.0, 0.0, 0.0, 0.0, -0.1, -0.5, -1.5, -2.8)
SIGN_MASK_EV = -2.0


def p_toneequal(weight):
    """tone equalizer v2: noise..speculars (9 bands, EV), smoothing diameter, curve
    smoothing, edge feathering, mask quantization, mask contrast/exposure compensation,
    details (2 = guided filter), luminance estimator (4 = RGB euclidean norm), iterations."""
    bands = [b * weight for b in SIGN_BANDS]
    return struct.pack("<15f3i", *bands, 10.0, math.sqrt(2.0), 5.0, 0.0, 0.0, SIGN_MASK_EV, 2, 4, 1)


def p_channelmixer_cat(x, y):
    """colour calibration v3, used as a 2nd instance: CAT16 from a custom
    illuminant (x, y) to the pipeline's D50, identity channel mixing."""
    red, green, blue = [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]
    zeros = [0.0] * 4
    return struct.pack("<24f6i4i4fii", *red, *green, *blue, *zeros, *zeros, *zeros,
                       0, 0, 0, 0, 0, 0,          # normalize R G B sat light grey
                       7, 2, 4, 1,                # illuminant CUSTOM, fluo F3, led B5, CAT16
                       x, y, 5003.0, 1.0,         # x, y, temperature (unused), gamut compression
                       1, 2)                      # clip, version 3


# lights live in the highlights: relax global + highlight desaturation at night,
# keep the shadows as colourless as the look wants (no purple/orange murk)
CHROMA_KEYS = ("chroma_global", "saturation_global", "chroma_highlights", "saturation_highlights")


def adjusted_look(look, plan):
    """Scene-dependent look changes: at night keep part of the global/highlight
    desaturation off so neon/practical lights and lit colours survive."""
    if not plan or plan.get("chroma_keep", 1.0) >= 0.999:
        return look
    k = plan["chroma_keep"]
    lk = dict(look)
    for key in CHROMA_KEYS:
        if lk.get(key, 0.0) < 0:
            lk[key] = lk[key] * k
    lk["zone_C"] = [c * k if c < 0 else c for c in look["zone_C"]]
    lk["hi_chroma"] = [(x, 1.0 - (1.0 - c) * k) for x, c in look.get("hi_chroma", HI_CHROMA)]
    return lk


def correction_entries(plan, is_raw):
    ents = []
    if not plan:
        return ents
    if abs(plan.get("ev", 0.0)) >= 0.05 or plan.get("black", 0.0) > 0:
        ents.append(("exposure", 7, p_exposure(plan.get("ev", 0.0), is_raw, plan.get("black", 0.0))))
    if plan.get("signs", 0.0) > 0 and is_raw:
        ents.append(("toneequal", 2, p_toneequal(plan["signs"])))
    if plan.get("shadhi"):
        ents.append(("shadhi", 5, p_shadhi(*plan["shadhi"])))
    if plan.get("wb_xy"):
        ents.append(("channelmixerrgb", 3, p_channelmixer_cat(*plan["wb_xy"]), 1, "auto wb"))
    return ents


def make_correction_xmp(plan, is_raw):
    """XMP with only the auto corrections (the fair 'before' for verification);
    None when the photo needs none."""
    ents = correction_entries(plan, is_raw)
    return build_xmp(ents, is_raw) if ents else None


def darktable_version(cli: str):
    """(major, minor, patch) from `darktable-cli --version`, or None."""
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"darktable\s+(\d+)\.(\d+)(?:\.(\d+))?", out)
    return tuple(int(x or 0) for x in m.groups()) if m else None


_CLI = None


def find_cli() -> str:
    """darktable-cli path; exits with install instructions when darktable is missing or too old.
    FUJI_LOOK_SKIP_VERSION_CHECK=1 skips the version check (~0.4 s)."""
    global _CLI
    if _CLI:
        return _CLI
    cli = next((c for c in DARKTABLE_CLI_CANDIDATES if c and os.access(c, os.X_OK)), None)
    if not cli:
        sys.exit("darktable-cli not found. " + DARKTABLE_HINT)
    if not os.environ.get("FUJI_LOOK_SKIP_VERSION_CHECK"):
        v = darktable_version(cli)
        vs = ".".join(map(str, v)) if v else "?"
        if v is None:
            print(f"warning: could not read the darktable version from `{cli} --version`", file=sys.stderr)
        elif v[:2] < DARKTABLE_REQUIRED:
            sys.exit(f"darktable {vs} at {cli} is too old. " + DARKTABLE_HINT)
        elif v[:2] > DARKTABLE_REQUIRED:
            print(f"warning: darktable {vs} is newer than the tested {DARKTABLE_TESTED}; module params are "
                  "packed for 5.6, so check the verify report", file=sys.stderr)
    _CLI = cli
    return cli


def add_look_args(ap):
    """Look/tuning flags shared by fuji_look.py and run.py."""
    ap.add_argument("--look", default="classic-chrome", choices=LOOKS)
    ap.add_argument("--strength", type=float, default=1.0, help="0=off, 1=default, up to ~1.6")
    ap.add_argument("--brightness", type=float, default=0.0, help="midtone power, -0.3..0.3")
    ap.add_argument("--shadows", type=float, default=0.0, help="shadow lift, -0.3..0.3")
    ap.add_argument("--highlights", type=float, default=0.0, help="highlight gain, -0.3..0.3")
    ap.add_argument("--grain", type=float, nargs="?", const=1.0, default=0.0, metavar="AMOUNT",
                    help="film grain, OFF by default: --grain = the look's grain, --grain 0.5 = half, 1.5 = more")
    ap.add_argument("--vignette", type=float, default=1.0, help="vignette multiplier (0=off)")
    ap.add_argument("--local-contrast", type=float, default=1.0, help="local contrast multiplier (street look; 0=off)")
    ap.add_argument("--zone", action="append", default=[], metavar="NAME:KIND=VAL",
                    help=f"override a color-zone offset, e.g. red:L=-0.05 (KIND L|C|h; names {','.join(ZONE_NAMES)})")


def parse_zones(zones):
    overrides = {}
    for z in zones:
        name, rest = z.split(":", 1)
        kind, val = rest.split("=", 1)
        if name not in ZONE_NAMES or kind not in ("L", "C", "h"):
            sys.exit(f"bad --zone {z}")
        overrides[name] = (kind, float(val))
    return overrides


def make_xmp(a, is_raw: bool, darktable_grain: bool = True, plan=None) -> str:
    """Build the XMP for parsed look args `a`. darktable_grain=False leaves the
    (slow) darktable grain module out so the caller can add grain itself.
    plan: per-photo auto corrections from scene.plan() (exposure, WB, night chroma)."""
    look = adjusted_look(LOOKS[a.look], plan)
    s = a.strength
    shadows = a.shadows + (plan.get("shadows_add", 0.0) if plan else 0.0)
    entries = correction_entries(plan, is_raw)
    if look.get("pre_zone_L"):
        # darken reds while they still have full chroma: color balance rgb's
        # brightness-preserving desaturation (darktable UCS, Helmholtz-Kohlrausch)
        # otherwise lifts low-chroma reds/browns (bricks, hair, fur) above neutral
        pz = dict(zone_L=look["pre_zone_L"], zone_C=[0.0] * 9, zone_h=[0.0] * 9)
        entries.append(("colorzones", 5, p_colorzones(pz, s, {}), 1, "reds first"))
    entries += [
        ("colorbalancergb", 5, p_colorbalancergb(look, s, a.brightness, shadows, a.highlights)),
        ("rgbcurve", 1, p_rgbcurve(look, s)),
        ("colorzones", 5, p_colorzones(look, s, parse_zones(a.zone))),
    ]
    hi = look.get("hi_chroma", HI_CHROMA)
    if hi:
        entries.append(("colorzones", 5, p_colorzones_hi(hi, s), 2, "highlight shoulder"))
    if look.get("local_contrast") and a.local_contrast > 0:
        entries.append(("bilat", 3, p_bilat(look["local_contrast"] * a.local_contrast * s)))
    gc, gs, *gb = look["grain"]
    if a.grain > 0 and darktable_grain:
        entries.append(("grain", 2, p_grain(gc, gs * a.grain, *gb)))
    if a.vignette > 0:
        entries.append(("vignette", 4, p_vignette(look["vignette"] * a.vignette,
                                                  look.get("vignette_shape", (80.0, 60.0)))))
    return build_xmp(entries, is_raw)


def grain_spec(a):
    """(amplitude in 8-bit units at mid-grey, floor weight) for the numpy grain
    in run.py, calibrated against darktable's grain module at 2048 px output
    (std ~0.2 x strength at mid-grey; bias 100 -> flat, bias 55 -> midtone-peaked)."""
    gc, gs, *gb = LOOKS[a.look]["grain"]
    bias = gb[0] if gb else 100.0
    floor = 0.30 + (bias - 55.0) / 45.0 * 0.32
    return 0.2 * gs * a.grain, max(0.0, min(1.0, floor))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="output file (default: <input>_fuji-<look>.jpg)")
    add_look_args(ap)
    ap.add_argument("--width", type=int, default=0, help="max output width (0=full)")
    ap.add_argument("--quality", type=int, default=100, help="JPEG quality (default 100 = best, always 4:4:4)")
    ap.add_argument("--xmp-only", metavar="FILE", help="only write the XMP, don't render")
    ap.add_argument("--keep-xmp", action="store_true", help="save the XMP next to the output")
    a = ap.parse_args()

    is_raw = Path(a.input).suffix.lower() in RAW_EXT
    xmp = make_xmp(a, is_raw)

    if a.xmp_only:
        Path(a.xmp_only).write_text(xmp)
        print(a.xmp_only)
        return

    src = Path(a.input).expanduser().resolve()
    if not src.exists():
        sys.exit(f"input not found: {src}")
    out = Path(a.output).expanduser().resolve() if a.output else src.with_name(f"{src.stem}_fuji-{a.look}.jpg")
    cli = find_cli()

    with tempfile.TemporaryDirectory(prefix="fujilook-") as td:
        td = Path(td)
        xmp_path = td / "look.xmp"
        xmp_path.write_text(xmp)
        tmp_out = td / ("out" + out.suffix.lower())
        cmd = [cli, str(src), str(xmp_path), str(tmp_out),
               "--apply-custom-presets", "false", "--hq", "true"]
        if a.width:
            cmd += ["--width", str(a.width)]
        # private config dir: avoids clashing with a running darktable GUI
        cmd += ["--core", "--configdir", str(td / "cfg"), "--library", ":memory:",
                "--conf", f"plugins/imageio/format/jpeg/quality={a.quality}",
                "--conf", "plugins/imageio/format/jpeg/subsample=1",  # 4:4:4, no chroma subsampling
                "--conf", "plugins/imageio/format/tiff/bpp=16",        # if -o is .tif
                "--conf", "plugins/lighttable/export/metadata_flags=21"]  # EXIF + darktable history
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not tmp_out.exists():
            sys.stderr.write(r.stdout + r.stderr)
            sys.exit(f"darktable-cli failed (exit {r.returncode})")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_out), out)
        if a.keep_xmp:
            shutil.copy(xmp_path, out.with_suffix(".xmp"))
    print(out)


if __name__ == "__main__":
    main()

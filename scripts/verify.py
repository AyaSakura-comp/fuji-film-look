#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "pillow"]
# ///
"""Verify that an edited photo really has the Fuji film look vs. the original.

Usage:
  uv run verify.py ORIGINAL EDITED [--look street] [--compare compare.jpg] [--json report.json]
                   [--reference REF ...]

Measures both images in CIELab and checks the look's signature:
  tone   : faded (lifted) blacks, highlight roll-off, no new clipping,
           mid-tone contrast kept
  color  : global chroma muted, reds darker + less saturated,
           greens muted, blues/sky not brighter
  tint   : shadows cooler (b* down), highlights not cooler
  sanity : same size, structure preserved (L correlation)
Exit code 0 = all hard checks pass, 1 = a hard check failed.

Also importable: analyze() (relative checks vs a baseline), output_checks()
(absolute checks on the edited image alone), reference_stats().
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageOps


def load(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def shrink(im, max_side):
    k = max_side / max(im.size)
    if k >= 1:
        return im
    return im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))), Image.BOX)


def to_lab(im):
    a = np.asarray(im, dtype=np.float32) / 255.0
    lin = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
    # explicit sums instead of `lin @ M.T`: Accelerate BLAS on macOS emits
    # spurious overflow warnings for float32 matmul
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    xyz = np.stack([(M[i, 0] * r + M[i, 1] * g + M[i, 2] * b) / wp
                    for i, wp in enumerate((0.95047, 1.0, 1.08883))], axis=-1)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    A = 500 * (f[..., 0] - f[..., 1])
    B = 200 * (f[..., 1] - f[..., 2])
    return L, A, B, a


def band_mask(A, B, L, lo, hi, cmin=18):
    h = (np.degrees(np.arctan2(B, A)) + 360) % 360
    C = np.hypot(A, B)
    return (h >= lo) & (h < hi) & (C >= cmin) & (L > 8) & (L < 95)


# per-look expectations for the relative (before/after) checks
PROFILES = {
    # calibrated on 20 Sony RAWs + 4 JPEGs and visual review (2026-11):
    # chrome lands at 0.6-0.9 chroma ratio, neg up to ~1.07 on blue-sky scenes.
    # Upper limits keep ~0.03 headroom: baselines rendered at different sizes
    # (800 px fast vs 1600 px hq) move the ratio by up to +-0.02.
    "classic-chrome": dict(chroma=(0.55, 0.92), skin_min=0.80, warm_highlights=True, contrast=(0.82, 1.25), corr=0.95),
    "classic-neg": dict(chroma=(0.55, 1.10), skin_min=0.75, warm_highlights=True, contrast=(0.82, 1.30), corr=0.95),
    # street adds a lot of contrast to flat sources and strips colour hard
    "street": dict(chroma=(0.30, 0.88), skin_min=0.45, warm_highlights=False, contrast=(0.88, 1.40), corr=0.93),
    # people & dolls: colour is kept (reds and skin are the point), only greens/blues muted
    # (green/blue-heavy landscapes and fog drop to 0.38-0.5: the muted greens are part of the look)
    "portrait": dict(chroma=(0.35, 1.12), skin_min=0.80, warm_highlights=True, contrast=(0.75, 1.20), corr=0.95,
                     reds="keep"),
}

# absolute expectations on the edited image alone (cheap, run on every file).
# Observed on the 72-render calibration set; limits = observed range + margin.
OUTPUT_PROFILES = {
    "classic-chrome": dict(p1=(4.5, 24.0), max_chroma=21.0, max_shadow_c=None),
    "classic-neg": dict(p1=(4.0, 22.0), max_chroma=23.0, max_shadow_c=None),
    "street": dict(p1=(9.5, 20.0), max_chroma=16.0, max_shadow_c=5.0),
    "portrait": dict(p1=(6.0, 26.0), max_chroma=26.0, max_shadow_c=None),
}

LOOK_MODULES = (b"colorbalancergb", b"rgbcurve", b"colorzones")


def history_check(edited_bytes):
    """darktable embeds its history in the export: were all look modules applied?"""
    if not edited_bytes or b"darktable" not in edited_bytes:
        return None
    ops = set(re.findall(rb'darktable:operation="([a-z0-9]+)"', edited_bytes))
    missing = sorted(x.decode() for x in set(LOOK_MODULES) - ops)
    return dict(name="darktable applied look modules", ok=not missing, hard=True,
                detail="missing: " + ", ".join(missing) if missing else "colorbalancergb, rgbcurve, colorzones present")


def style_stats(L, A, B):
    """Content-light style fingerprint used for --reference matching."""
    C = np.hypot(A, B)
    h = (np.degrees(np.arctan2(B, A)) + 360) % 360
    mid = (L > 10) & (L < 90)
    st = {
        "black L p1": float(np.percentile(L, 1)),
        "mid-tone L std": float(L[mid].std()) if mid.mean() > 0.05 else float(L.std()),
        "mean chroma": float(C.mean()),
        "shadow chroma (L<25)": float(C[L < 25].mean()) if (L < 25).mean() > 0.01 else None,
    }
    sky = (h >= 220) & (h < 280) & (C > 8) & (L > 30)
    if sky.mean() > 0.01:
        st["blue/sky L"] = float(L[sky].mean())
        st["blue/sky chroma"] = float(C[sky].mean())
    return st


def reference_stats(paths, work=1200):
    return [style_stats(*to_lab(shrink(load(p), work))[:3]) for p in paths]


REF_TOL = {"black L p1": 2.0, "mid-tone L std": 3.0, "mean chroma": 1.5,
           "shadow chroma (L<25)": 1.0, "blue/sky L": 5.0, "blue/sky chroma": 3.0}


def ref_checks(mine, refs):
    out = []
    for key, t in REF_TOL.items():
        vals = [r[key] for r in refs if r.get(key) is not None]
        if not vals or mine.get(key) is None:
            continue
        lo_r, hi_r = min(vals) - t, max(vals) + t
        v = mine[key]
        out.append(dict(name=f"ref match: {key}", ok=lo_r <= v <= hi_r, hard=False,
                        detail=f"{v:.1f} vs reference {min(vals):.1f}-{max(vals):.1f} (±{t})"))
    return out


def output_checks(e_im, look, edited_bytes=None, ref_stats=None, work=600):
    """Absolute checks on the edited image only (no baseline needed)."""
    prof = OUTPUT_PROFILES.get(look, OUTPUT_PROFILES["classic-chrome"])
    checks, m = [], {}
    hc = history_check(edited_bytes)
    if hc:
        checks.append(hc)
    L, A, B, rgb = to_lab(shrink(e_im, work))
    C = np.hypot(A, B)
    h = (np.degrees(np.arctan2(B, A)) + 360) % 360
    p1 = float(np.percentile(L, 1))
    clip = float((rgb.max(-1) >= 0.995).mean() * 100)
    crush = float((rgb.max(-1) <= 0.01).mean() * 100)
    m.update(out_L_p1=p1, out_L_p50=float(np.median(L)), out_mean_chroma=float(C.mean()),
             out_clipped_pct=clip, out_crushed_pct=crush)
    lo, hi = prof["p1"]
    checks.append(dict(name=f"matte black floor (L p1 {lo}-{hi})", ok=lo <= p1 <= hi, hard=True, detail=f"L p1 {p1:.1f}"))
    checks.append(dict(name="no clipping (<=0.3% at 255)", ok=clip <= 0.3, hard=True, detail=f"{clip:.2f}%"))
    checks.append(dict(name="no crushed blacks (<=0.1% at 0)", ok=crush <= 0.1, hard=True, detail=f"{crush:.2f}%"))
    checks.append(dict(name=f"muted overall (mean C <= {prof['max_chroma']})", ok=C.mean() <= prof["max_chroma"],
                       hard=True, detail=f"mean C {C.mean():.1f}"))
    if prof["max_shadow_c"] is not None:
        sh = (L < 25) & ~((h >= 220) & (h < 290) & (C > 2))
        if sh.mean() > 0.01:
            sc = float(C[sh].mean())
            m["out_shadow_chroma"] = sc
            checks.append(dict(name=f"colourless shadows (C <= {prof['max_shadow_c']})", ok=sc <= prof["max_shadow_c"],
                               hard=True, detail=f"shadow C {sc:.1f}"))
    checks.append(dict(name="not blank", ok=float(L.std()) > 2, hard=True, detail=f"L std {L.std():.1f}"))
    if ref_stats:
        mine = style_stats(L, A, B)
        m["reference_style"] = dict(edited=mine)
        checks += ref_checks(mine, ref_stats)
    return checks, m


def analyze(o_im, e_im, look, edited_bytes=None, ref_stats=None, work=1200, require_same_size=True):
    """Relative checks: edited vs baseline ("before") image. Returns (checks, metrics)."""
    prof = PROFILES.get(look, PROFILES["classic-chrome"])
    checks = []

    def check(name, ok, detail, hard=True):
        checks.append(dict(name=name, ok=bool(ok), hard=hard, detail=detail))

    if require_same_size:
        check("same size", o_im.size == e_im.size, f"{o_im.size} vs {e_im.size}")
    hc = history_check(edited_bytes)
    if hc:
        checks.append(hc)

    # work on downscaled copies of identical size (also averages out grain)
    o_s = shrink(o_im, work)
    e_s = e_im.resize(o_s.size, Image.BOX) if e_im.size != o_s.size else e_im
    Lo, Ao, Bo, rgb_o = to_lab(o_s)
    Le, Ae, Be, rgb_e = to_lab(e_s)
    Co, Ce = np.hypot(Ao, Bo), np.hypot(Ae, Be)

    m = {}
    p = lambda x, q: float(np.percentile(x, q))
    m["L_p1"] = (p(Lo, 1), p(Le, 1))
    m["L_p50"] = (p(Lo, 50), p(Le, 50))
    m["L_p99"] = (p(Lo, 99), p(Le, 99))
    m["L_iqr"] = (p(Lo, 75) - p(Lo, 25), p(Le, 75) - p(Le, 25))
    clip = lambda rgb: float((rgb.max(axis=-1) >= 0.995).mean() * 100)
    crush = lambda rgb: float((rgb.max(axis=-1) <= 0.01).mean() * 100)
    m["clipped_%"] = (clip(rgb_o), clip(rgb_e))
    m["crushed_%"] = (crush(rgb_o), crush(rgb_e))
    m["chroma_mean"] = (float(Co.mean()), float(Ce.mean()))

    # tone checks
    black_lift = m["L_p1"][1] - m["L_p1"][0]
    check("faded blacks (L p1 lifted)", black_lift >= 0.8 or m["L_p1"][1] >= 6,
          f"L p1 {m['L_p1'][0]:.1f} -> {m['L_p1'][1]:.1f}")
    check("no crushed blacks", m["crushed_%"][1] <= m["crushed_%"][0] + 0.1,
          f"pixels <=1%: {m['crushed_%'][0]:.2f}% -> {m['crushed_%'][1]:.2f}%")
    check("highlight roll-off (no new clipping)", m["clipped_%"][1] <= m["clipped_%"][0] + 0.1,
          f"clipped {m['clipped_%'][0]:.2f}% -> {m['clipped_%'][1]:.2f}%")
    check("highlights softened (L p99 not higher)", m["L_p99"][1] <= m["L_p99"][0] + 0.5,
          f"L p99 {m['L_p99'][0]:.1f} -> {m['L_p99'][1]:.1f}", hard=False)
    # contrast among pixels that were mid-tones in the original (ignores big
    # black areas in night shots whose lift is intentional fade)
    mid = (Lo > 10) & (Lo < 90)
    if mid.mean() > 0.05:
        sd_o, sd_e = float(Lo[mid].std()), float(Le[mid].std())
    else:
        sd_o, sd_e = float(Lo.std()), float(Le.std())
    m["L_mid_std"] = (sd_o, sd_e)
    ratio = sd_e / max(sd_o, 1e-6)
    c_lo, c_hi = prof["contrast"]
    # dark-dominated frame (silhouette, night with black sky): the median sits in
    # the black floor the look lifts on purpose and the "mid-tones" are a sliver,
    # so these global relative stats become warnings
    dark_frame = m["L_p50"][0] < 15
    m["dark_frame"] = dark_frame
    check(f"mid-tone contrast kept (std ratio {c_lo}-{c_hi})", c_lo <= ratio <= c_hi,
          f"mid L std {sd_o:.1f} -> {sd_e:.1f} (x{ratio:.2f}); full IQR {m['L_iqr'][0]:.1f} -> {m['L_iqr'][1]:.1f}",
          hard=not dark_frame)
    mid_shift = m["L_p50"][1] - m["L_p50"][0]
    check("overall brightness preserved (|dL median| <= 6)", abs(mid_shift) <= 6,
          f"median L {m['L_p50'][0]:.1f} -> {m['L_p50'][1]:.1f}" + (" (dark frame)" if dark_frame else ""),
          hard=not dark_frame)

    # color checks
    cr = m["chroma_mean"][1] / max(m["chroma_mean"][0], 1e-6)
    lo_c, hi_c = prof["chroma"]
    # near-grey originals (mean C < 8): a ratio of tiny numbers is noise, and the
    # look's split toning adds ~1-3 C to neutral pixels -> judge the absolute change
    grey_orig = m["chroma_mean"][0] < 8
    ok_c = lo_c <= cr <= hi_c or (grey_orig and cr > lo_c and m["chroma_mean"][1] - m["chroma_mean"][0] <= 2.5)
    check(f"global chroma muted (ratio {lo_c:.2f}-{hi_c:.2f}" + ("; near-grey: +<=2.5 C" if grey_orig else "") + ")",
          ok_c, f"mean C {m['chroma_mean'][0]:.1f} -> {m['chroma_mean'][1]:.1f} (x{cr:.2f})", hard=not dark_frame)

    # expected lightness change from the tone curve alone, as a function of the
    # original L, measured on near-neutral pixels. Colour-band dL is reported
    # relative to it so a global fade/lift doesn't mask hue-specific darkening.
    neutral = Co < 6
    edges = np.linspace(0, 100, 21)
    centers, dls = [], []
    for lo_e, hi_e in zip(edges[:-1], edges[1:]):
        sel = neutral & (Lo >= lo_e) & (Lo < hi_e)
        if sel.sum() > 50:
            centers.append((lo_e + hi_e) / 2); dls.append(float((Le[sel] - Lo[sel]).mean()))

    def tone_dL(Lvals):
        if len(centers) < 2:
            return float(np.mean(Le - Lo))
        return float(np.interp(Lvals, centers, dls).mean())

    bands = {"red": (15, 50), "orange/skin": (50, 75), "yellow": (75, 105),
             "green": (105, 170), "cyan": (170, 235), "blue/sky": (235, 300)}
    for name, (lo, hi) in bands.items():
        # reds: clearly red objects only (C >= 28); low-chroma browns (brick, fur,
        # hair) are desaturated by the look, not darkened
        msk = band_mask(Ao, Bo, Lo, lo, hi, cmin=28 if name == "red" else 18)
        frac = int(msk.sum()) / msk.size
        if frac < 0.002:
            m[f"band_{name}"] = None
            continue
        dL_abs = float(Le[msk].mean() - Lo[msk].mean())
        dL = dL_abs - tone_dL(Lo[msk])  # hue-specific part only
        rC = float(Ce[msk].mean() / max(Co[msk].mean(), 1e-6))
        m[f"band_{name}"] = dict(pixels_pct=round(frac * 100, 2), dL_rel=round(dL, 2), dL_abs=round(dL_abs, 2), C_ratio=round(rC, 3))
        small = frac < 0.005  # <0.5% of pixels: too noisy for a hard verdict
        if name == "red" and prof.get("reds") == "keep":
            # portrait: red dresses, lips, roses stay red - not darker, not browner
            # 0.78: dark crimson dresses under dim light measure 0.79-0.80 (the matte floor
            # dilutes chroma near black) and look the same as 0.82 at 100%; browned reds are <0.7
            check("reds kept (C ratio 0.78-1.35)", 0.78 <= rC <= 1.35, f"red C x{rC:.2f} ({frac*100:.1f}% px)", hard=not small)
            # darker relative to the tone curve AND in absolute terms (scenes with few
            # neutral pixels make the relative figure alone unreliable)
            check("reds not darker (dL rel >= -1.5)", dL >= -1.5 or dL_abs >= 0,
                  f"red dL {dL:+.2f} relative ({dL_abs:+.2f} abs)", hard=not small)
        elif name == "red":
            check("reds darker (vs neutral tones)", dL < -0.3,
                  f"red dL {dL:+.2f} relative ({dL_abs:+.2f} abs, {frac*100:.1f}% px)", hard=not small)
            check("reds less saturated", rC < 0.97, f"red C x{rC:.2f}", hard=not small)
        elif name == "green":
            check("greens muted", rC < 0.95, f"green C x{rC:.2f} ({frac*100:.1f}% px)", hard=not small)
        elif name == "blue/sky":
            check("blues not brighter (vs neutral tones)", dL <= 0.5, f"blue dL {dL:+.2f} rel, C x{rC:.2f} ({frac*100:.1f}% px)", hard=False)
        elif name == "orange/skin":
            check(f"skin/warm tones kept (C ratio >= {prof['skin_min']})", rC >= prof["skin_min"],
                  f"orange C x{rC:.2f}, dL {dL:+.2f}", hard=False)

    # split-tone checks (soft: depends on scene)
    sh = Lo < 30
    hl = Lo > 70
    if sh.mean() > 0.01:
        db = float(Be[sh].mean() - Bo[sh].mean())
        da = float(Ae[sh].mean() - Ao[sh].mean())
        m["shadow_tint_shift_ab"] = (round(da, 2), round(db, 2))
        check("shadows cooler (b* down / teal)", db < 0.3, f"shadow da {da:+.2f} db {db:+.2f}", hard=False)
    if hl.mean() > 0.01:
        db = float(Be[hl].mean() - Bo[hl].mean())
        m["highlight_tint_shift_b"] = round(db, 2)
        if prof["warm_highlights"]:
            check("highlights not cooler (warm/neutral)", db > -1.0, f"highlight db {db:+.2f}", hard=False)
        else:
            check("highlights neutral/cool (not warmer)", db < 1.0, f"highlight db {db:+.2f}", hard=False)
    if look == "street" and (Le < 25).mean() > 0.01:
        # the look deliberately keeps deep blue skies: leave blue hues out
        hue_o = (np.degrees(np.arctan2(Bo, Ao)) + 360) % 360
        hue_e = (np.degrees(np.arctan2(Be, Ae)) + 360) % 360
        not_blue = ~(((hue_o >= 220) & (hue_o < 290) & (Co > 6)) | ((hue_e >= 220) & (hue_e < 290) & (Ce > 2)))
        se, so = (Le < 25) & not_blue, (Lo < 25) & not_blue
        shC = float(Ce[se].mean()) if se.any() else 0.0
        shC0 = float(Co[so].mean()) if so.mean() > 0.01 else shC
        # deep blue sky / water can sit in the shadows: accept a >=60% cut too
        check("shadows nearly colourless, excl. blue (C < 3.5 or cut >= 60%)", shC < 3.5 or shC <= 0.4 * shC0,
              f"shadow C {shC0:.1f} -> {shC:.1f}")

    # structure sanity
    # measured on ~256 px block averages: robust to the sub-pixel offsets
    # between differently-scaled renders, still catches broken/garbled output
    k = max(1, int(np.ceil(max(Lo.shape) / 256)))
    hh, ww = (Lo.shape[0] // k) * k, (Lo.shape[1] // k) * k
    blk = lambda X: X[:hh, :ww].reshape(hh // k, k, ww // k, k).mean(axis=(1, 3))
    corr = float(np.corrcoef(blk(Lo).ravel(), blk(Le).ravel())[0, 1])
    m["L_correlation"] = corr
    check(f"structure preserved (L corr >= {prof['corr']})", corr >= prof["corr"], f"corr {corr:.3f}")

    # style match against reference images (soft: content differs)
    if ref_stats:
        mine = style_stats(Le, Ae, Be)
        m["reference_style"] = dict(edited=mine, references=ref_stats)
        checks += ref_checks(mine, ref_stats)
    return checks, m


# night tops out like dim: EV100 < 6 also covers lamp-lit rooms, shot bright on purpose
LIGHT_BANDS = {"day": (22, 70), "dim": (16, 66), "night": (5, 66)}


def lighting_checks(e_im, plan, base_im=None, work=600):
    """Absolute checks of the auto (different lighting) corrections, on every file."""
    import scene as SC
    checks, m = [], {}
    if not plan or plan.get("cls") in (None, "?"):
        return checks, m
    L, A, B, rgb = to_lab(shrink(e_im, work))
    C = np.hypot(A, B)
    med, p95 = float(np.median(L)), float(np.percentile(L, 95))
    m.update(scene=plan["cls"], auto_ev=plan.get("ev", 0.0), out_L_p95=p95)
    # capped: the plan wanted more lift than its limits allow (max EV, highlight or night-median
    # guard) -> a dark result is the scene's own contrast, reported as a warning
    capped = bool(plan.get("capped"))
    cap_note = f" (lift capped at {plan.get('ev', 0):+.1f} EV)" if capped else ""
    if not plan.get("lowkey") and not plan.get("highkey"):
        lo, hi = LIGHT_BANDS[plan["cls"]]
        if plan.get("shadhi"):  # backlit composition: dark foreground against a bright sky is the scene
            lo = min(lo, 15)
        if plan.get("spot", 0) >= 0.5:  # spotlit subject in a dark surround: the median sits in the
            lo = min(lo, 5)             # surround (the look's matte floor); "lit subject readable" guards it
        checks.append(dict(name=f"exposure fits a {plan['cls']} scene (median L {lo}-{hi})", ok=lo <= med <= hi,
                           hard=not (capped and med < lo), detail=f"median L {med:.1f}{cap_note}"))
    if not plan.get("lowkey"):
        checks.append(dict(name="lit subject readable (L p95 >= 40)", ok=p95 >= 40, hard=not capped,
                           detail=f"L p95 {p95:.1f}{cap_note}"))
    if plan.get("wb"):
        est = SC.white_estimates(SC.srgb_to_lin(rgb))
        c_out = sorted(float(np.hypot(*SC.white_ab(w))) for w in est.values())[1]
        c_in = plan["wb"]["cast_C"]
        lim = c_in * (1 - 0.5 * plan["wb"]["frac"])
        m["cast_C"] = [c_in, c_out]
        checks.append(dict(name=f"artificial-light cast tamed (C <= {lim:.0f})", ok=c_out <= lim, hard=True,
                           detail=f"cast C {c_in:.0f} -> {c_out:.0f}"))
    if plan["cls"] == "night" and base_im is not None:
        bC = np.hypot(*to_lab(shrink(base_im, work))[1:3])
        r = float(np.percentile(C, 99) / max(np.percentile(bC, 99), 1e-6))
        m["light_C99_ratio"] = r
        checks.append(dict(name="light colours kept (C p99 ratio >= 0.55)", ok=r >= 0.55, hard=False, detail=f"{r:.2f}"))
    return checks, m


def passed(checks):
    return not any((not c["ok"]) and c["hard"] for c in checks)


def print_report(checks):
    width = max(len(c["name"]) for c in checks)
    for c in checks:
        tag = "PASS" if c["ok"] else ("FAIL" if c["hard"] else "WARN")
        print(f"[{tag}] {c['name'].ljust(width)}  {c['detail']}")
    print("RESULT:", "PASS" if passed(checks) else "FAIL")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("original")
    ap.add_argument("edited")
    ap.add_argument("--compare", help="write side-by-side comparison JPEG here")
    ap.add_argument("--json", help="write metrics JSON here")
    ap.add_argument("--look", default="classic-chrome",
                    help="look that was applied (classic-chrome, classic-neg, street, portrait) to pick thresholds")
    ap.add_argument("--reference", nargs="+", metavar="IMG",
                    help="style reference image(s): also report how close the edit's style fingerprint is")
    a = ap.parse_args()

    o_im, e_im = load(a.original), load(a.edited)
    refs = reference_stats(a.reference) if a.reference else None
    checks, m = analyze(o_im, e_im, a.look, open(a.edited, "rb").read(), refs)
    print_report(checks)
    ok = passed(checks)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(dict(metrics=m, checks=checks, passed=ok), f, indent=2)
    if a.compare:
        h = 900

        def fit(im):
            return im.resize((round(im.size[0] * h / im.size[1]), h), Image.LANCZOS)
        L_, R_ = fit(o_im), fit(e_im)
        canvas = Image.new("RGB", (L_.size[0] + R_.size[0] + 30, h + 60), (24, 24, 24))
        canvas.paste(L_, (10, 50)); canvas.paste(R_, (20 + L_.size[0], 50))
        d = ImageDraw.Draw(canvas)
        d.text((14, 16), "ORIGINAL", fill=(230, 230, 230))
        d.text((24 + L_.size[0], 16), f"FUJI LOOK  ({'PASS' if ok else 'FAIL'})", fill=(230, 230, 230))
        canvas.save(a.compare, quality=90)
        print("compare:", a.compare)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Per-photo scene analysis and auto corrections for the Fuji look.

Why: one fixed recipe cannot suit a sunny street, a tungsten bar, a sodium-lit
night street and a backlit portrait. Before rendering, every photo is measured
and gets its own small plan (exposure offset, partial white-balance fix for
artificial light, night colour protection), written into its own XMP sidecar.

What is measured (no darktable involved, ~0.1 s per 24 MP RAW):
  * EXIF shutter / aperture / ISO (or the metered BrightnessValue) -> EV100,
    i.e. how bright the scene really was: night < 6 <= dim < 9.5 <= day.
  * RAW: a half-size LibRaw decode (rawpy) with the camera white balance, in
    linear sRGB. TONE_X/TONE_Y map its scene-linear luminance (after darktable's
    default +0.7 EV and exposure-bias compensation) to the L* darktable's default
    render will have: within ~1 L* at the median, ~1.6 at p95 (fitted on 54 RAWs
    from 30 camera models). So the effect of any exposure change is predicted
    without rendering.
  * JPEG/PNG/TIFF: the file itself, where an exposure change is exact maths.
  * White-point estimates (grey world, shades of grey p=4, iterated near-neutral
    pixels). Colour constancy is unreliable on real content (skies read as
    "blue cast", sunsets as "warm cast"), so a WB fix is only made when all
    three agree on an artificial-light cast (amber sodium/tungsten or green
    fluorescent) in a night/dim scene, and only partially (keeps the mood).

`python3 scene.py FILES...` prints the analysis and plan for each file.
"""
from __future__ import annotations

import io
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

RAW_EXT = {".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".orf", ".rw2", ".pef", ".srw", ".nrw", ".3fr", ".iiq"}
_TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1), 7: ("B", 1),
          8: ("h", 2), 9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8), 13: ("I", 4)}

# scene-linear log2(Y) (camera WB, after darktable's default exposure) -> L* of
# darktable 5.6's default scene-referred (sigmoid) render. Fitted 2026-11-08.
TONE_X = [-14.0, -9.67, -9.06, -8.73, -8.32, -7.81, -7.25, -6.77, -6.31, -5.7, -5.26, -4.74, -4.21, -3.7,
          -3.26, -2.81, -2.23, -1.69, -1.23, -0.8, -0.28, 0.2, 0.7, 1.99, 4.0]
TONE_Y = [0.0, 0.5, 0.6, 0.7, 1.0, 1.4, 2.4, 3.4, 5.4, 9.0, 12.9, 18.1, 24.5, 31.4, 38.3, 46.3, 56.9, 66.2,
          75.1, 81.7, 87.3, 91.5, 96.7, 98.8, 99.5]
Q = (2, 5, 10, 25, 50, 75, 90, 95, 98, 99, 99.5)

M_SRGB = np.array([[0.4124564, 0.3575761, 0.1804375],
                   [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]])
BRADFORD_D65_D50 = np.array([[1.0478112, 0.0228866, -0.0501270],
                             [0.0295424, 0.9904844, -0.0170491],
                             [-0.0092345, 0.0150436, 0.7521316]])
D50_XY = (0.3457, 0.3585)


# ------------------------------------------------------------ TIFF walk ---
def _ifd(buf, off, e, depth=0, out=None, seen=None):
    """Collect every IFD (incl. SubIFDs/Exif) as {tag: value(s)} dicts."""
    out = [] if out is None else out
    seen = set() if seen is None else seen
    while off and off + 2 <= len(buf) and off not in seen and depth < 8 and len(out) < 64:
        seen.add(off)
        n = struct.unpack_from(e + "H", buf, off)[0]
        if n > 1000 or off + 2 + n * 12 > len(buf):
            break
        tags = {}
        for i in range(n):
            tag, typ, cnt = struct.unpack_from(e + "HHI", buf, off + 2 + i * 12)
            if typ not in _TYPES or cnt > 1_000_000:
                continue
            fmt, size = _TYPES[typ]
            total = size * cnt
            voff = off + 2 + i * 12 + 8
            if total > 4:
                voff = struct.unpack_from(e + "I", buf, voff)[0]
                if voff + total > len(buf):
                    continue
            if typ == 2:
                val = buf[voff:voff + cnt].split(b"\0", 1)[0].decode("latin1", "replace").strip()
            elif typ in (5, 10):
                raw = struct.unpack_from(e + fmt[0] * (2 * cnt), buf, voff)
                val = [raw[j] / raw[j + 1] if raw[j + 1] else 0.0 for j in range(0, len(raw), 2)]
            elif typ in (1, 6, 7) and cnt > 8:
                val = None  # opaque blobs (MakerNote, ...) are not needed
            else:
                val = list(struct.unpack_from(e + fmt * cnt, buf, voff))
            tags[tag] = val
        out.append(tags)
        for sub in (0x14A, 0x8769):  # SubIFDs, Exif IFD
            for so in tags.get(sub) or []:
                _ifd(buf, so, e, depth + 1, out, seen)
        nxt = off + 2 + n * 12
        off = struct.unpack_from(e + "I", buf, nxt)[0] if nxt + 4 <= len(buf) else 0
    return out


def _first(v):
    return v[0] if isinstance(v, list) and v else v


_EXIF_TAGS = ((0x829A, "t"), (0x829D, "N"), (0x8827, "iso"), (0x8832, "iso"), (0x9204, "bias"),
              (0x9203, "bv"), (0x0112, "orientation"), (0x010F, "make"), (0x0110, "model"))


def read_meta(path, data=None, head=None):
    """-> (exif dict, list of (offset, length) candidate embedded JPEGs, bytes).
    head: only read the first `head` bytes (EXIF lives there; RAWs are 25-60 MB)."""
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read(head) if head else fh.read()
    exif, jpegs = {}, []
    blocks = []
    if data[:4] in (b"II*\0", b"MM\0*", b"IIRO", b"IIU\0"):
        blocks.append(0)
    else:  # CR3 & co. carry standalone TIFF blocks (CMT1/CMT2 ...) near the start
        for magic in (b"II*\0\x08\0\0\0", b"MM\0*\0\0\0\x08"):
            pos = data.find(magic, 0, 4 << 20)
            while pos >= 0 and len(blocks) < 8:
                blocks.append(pos)
                pos = data.find(magic, pos + 8, 4 << 20)
    for b0 in blocks:
        buf = data if b0 == 0 else data[b0:b0 + (1 << 20)]
        e = "<" if buf[:2] == b"II" else ">"
        try:
            ifds = _ifd(buf, struct.unpack_from(e + "I", buf, 4)[0], e)
        except struct.error:
            ifds = []
        for t in ifds:
            for k, name in _EXIF_TAGS:
                if k in t and t[k] not in (None, [], "") and name not in exif:
                    exif[name] = _first(t[k])
            if b0 == 0:
                if 0x201 in t and 0x202 in t:
                    jpegs.append((_first(t[0x201]), _first(t[0x202])))
                if _first(t.get(0x103)) in (6, 7) and t.get(0x111) and t.get(0x117) and len(t[0x111]) == 1:
                    jpegs.append((t[0x111][0], t[0x117][0]))
    if data[:16] == b"FUJIFILMCCD-RAW ":
        off, ln = struct.unpack_from(">II", data, 84)
        jpegs.append((off, ln))
    if not jpegs:  # generic: every JPEG SOI in the container
        pos = data.find(b"\xff\xd8\xff")
        while pos >= 0 and len(jpegs) < 12:
            jpegs.append((pos, len(data) - pos))
            pos = data.find(b"\xff\xd8\xff", pos + 4)
    return exif, jpegs, data


def embedded_preview(path, target=400):
    """Largest embedded JPEG (camera rendering) -> (exif, small RGB image)."""
    exif, jpegs, data = read_meta(path)
    best = None
    for off, ln in jpegs:
        try:
            im = Image.open(io.BytesIO(data[off:off + ln]))
            if im.format not in ("JPEG", "MPO"):
                continue
            if best is None or im.size[0] * im.size[1] > best.size[0] * best.size[1]:
                best = im
        except Exception:
            continue
    if best is None:
        return exif, None
    best.draft("RGB", (target, target))
    im = best.convert("RGB")
    im.thumbnail((target, target))
    return exif, im


def _pil_exif(im):
    try:
        ex = im.getexif(); e2 = ex.get_ifd(0x8769)
        return {k: v for k, v in dict(t=e2.get(0x829A), N=e2.get(0x829D), iso=e2.get(0x8827), bias=e2.get(0x9204),
                                      bv=e2.get(0x9203), orientation=ex.get(0x0112)).items() if v is not None}
    except Exception:
        return {}


def ev100(exif):
    """Scene brightness in EV at ISO 100: from shutter/aperture/ISO when the lens
    reports an aperture, else from the camera's metered BrightnessValue (Bv + 5)."""
    ev = None
    try:
        t, N, iso = float(exif["t"]), float(exif["N"]), float(exif["iso"])
        if 1 / 32000 <= t <= 3600 and 0.7 <= N <= 64 and 25 <= iso <= 409600:  # sane values only
            ev = math.log2(N * N / t) - math.log2(iso / 100.0)
    except (KeyError, TypeError, ValueError):
        pass
    if ev is None:
        try:
            bv = float(exif["bv"])
            ev = bv + 5.0 if -10 < bv < 20 else None
        except (KeyError, TypeError, ValueError):
            ev = None
    return ev if ev is not None and -8 <= ev <= 20 else None


# ------------------------------------------------------------- colour ---
def srgb_to_lin(s):
    return np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def lstar(Y):
    Y = np.asarray(Y, dtype=np.float64)
    return np.where(Y > 216 / 24389, 116 * np.cbrt(Y) - 16, Y * 24389 / 27)


def _lum(lin):
    return lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722


def white_ab(rgb):
    """a*, b* of a linear-sRGB white estimate (lightness normalised)."""
    rgb = np.asarray(rgb, dtype=np.float64)
    X, Y, Z = M_SRGB @ (rgb / max(float(_lum(rgb)), 1e-9))
    f = np.cbrt
    fx, fy, fz = f(X / 0.95047), f(Y), f(Z / 1.08883)
    return float(500 * (fx - fy)), float(200 * (fy - fz))


def white_xy_d50(rgb):
    """chromaticity, in darktable's D50 pipeline frame, of a linear-sRGB white."""
    XYZ = BRADFORD_D65_D50 @ (M_SRGB @ np.asarray(rgb, dtype=np.float64))
    s = XYZ.sum()
    return float(XYZ[0] / s), float(XYZ[1] / s)


def white_estimates(lin):
    """{name: rgb} white-point estimates on a linear RGB image."""
    Y = _lum(lin)
    mx = lin.max(-1)
    ok = (mx < 0.95 * max(float(np.percentile(mx, 99.9)), 1e-6)) & (Y > np.percentile(Y, 5))
    px = lin[ok].astype(np.float64)
    if len(px) < 100:
        px = lin.reshape(-1, 3).astype(np.float64)
    est = {"grey": px.mean(0), "sog4": (px ** 4).mean(0) ** 0.25}
    w = est["sog4"]
    for _ in range(3):  # iterate towards the most neutral 20 % under the current white
        n = px / np.maximum(w, 1e-9)
        n = n / np.maximum(n.mean(-1, keepdims=True), 1e-9)
        dev = np.abs(n - 1).sum(-1)
        w = px[dev <= np.percentile(dev, 20)].mean(0)
    est["neutral20"] = w
    return {k: np.maximum(v, 1e-9) for k, v in est.items()}


# ----------------------------------------------------------- analysis ---
def _small_linear_raw(path, target=400):
    import rawpy  # only needed for RAW analysis
    with rawpy.imread(str(path)) as r:
        a = r.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=True, gamma=(1, 1),
                          output_bps=16, user_flip=0)
    if max(a.shape[:2]) >= 4 * target:  # statistics only: every 2nd pixel is plenty (4x less work)
        a = a[::2, ::2]
    h, w = a.shape[:2]
    k = max(1, max(h, w) // target)
    a = a[:h // k * k, :w // k * k].reshape(h // k, k, w // k, k, 3).mean((1, 3), dtype=np.float32)
    return a * np.float32(1 / 65535.0)


def analyze(path):
    """Measure one photo. Returns a plain (picklable) dict."""
    p = Path(path)
    out = dict(path=str(p), kind="none", ev100=None, bias=0.0, note="")
    try:
        if p.suffix.lower() in RAW_EXT:
            exif, _, _ = read_meta(p, head=4 << 20)
            bias = float(exif.get("bias") or 0.0)
            try:
                lin = _small_linear_raw(p)
            except Exception as e:  # unsupported by LibRaw: no auto plan
                out.update(ev100=ev100(exif), note=f"raw decode failed ({type(e).__name__})")
                return out
            Ys = _lum(lin) * 2 ** (0.7 - bias)  # what darktable's default exposure does
            x = np.log2(np.maximum(Ys, 1e-7))
            out.update(kind="raw", bias=bias, xq=[float(np.percentile(x, q)) for q in Q])
            Lpix = np.interp(x, TONE_X, TONE_Y)
        else:
            im = Image.open(p)
            exif = _pil_exif(im)
            im.draft("RGB", (800, 800))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((400, 400))
            lin = srgb_to_lin(np.asarray(im, dtype=np.float32) / 255.0)
            Y = _lum(lin)
            out.update(kind="rgb", yq=[float(np.percentile(Y, q)) for q in Q])
            Lpix = lstar(Y)
        out["ev100"] = ev100(exif)
        out["dark"] = float((Lpix < 8).mean())
        est = white_estimates(lin)
        out["white"] = {k: [float(c) for c in v] for k, v in est.items()}
        out["cast"] = {k: white_ab(v) for k, v in est.items()}
    except Exception as e:
        out.update(kind="none", note=f"analysis failed ({type(e).__name__}: {e})")
    return out


def L_at(an, q, dev=0.0):
    """Predicted L* percentile q of darktable's default render after +dev EV."""
    i = Q.index(q)
    if an["kind"] == "raw":
        return float(np.interp(an["xq"][i] + dev, TONE_X, TONE_Y))
    return float(lstar(min(an["yq"][i] * 2 ** dev, 1.0)))


def _solve(an, q, target, lo=-2.0, hi=3.0):
    """dev such that L_at(q, dev) == target (monotone bisection)."""
    if L_at(an, q, lo) >= target:
        return lo
    if L_at(an, q, hi) <= target:
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if L_at(an, q, mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _lin_at(an, q, dev):
    """scene-linear luminance (after darktable's default exposure) at percentile q."""
    i = Q.index(q)
    if an["kind"] == "raw":
        return 2 ** (an["xq"][i] + dev)
    return an["yq"][i] * 2 ** dev


def _black_k(an, dev, q, target_L):
    """k such that (Y_q - k) / (1 - k) lands on target_L (darktable exposure black)."""
    Yq = _lin_at(an, q, dev)
    if an["kind"] == "raw":
        T = 2 ** float(np.interp(target_L, TONE_Y, TONE_X))
    else:
        T = ((target_L + 16) / 116) ** 3
    if Yq <= T:
        return 0.0
    return float(min((Yq - T) / (1 - T), 0.4 * Yq))  # gentle: big offsets push hazy blue shadows off-hue


def spot_weight(an):
    """0..1: how much the frame is a pool of light (a spotlit subject in a dark
    surround: spotlit dolls and tables, stage light, candle-lit corners). The
    dark surround is the picture: a median-based exposure target would lift it
    to grey and flatten the light falloff. Ramped, not a switch, so frames near
    the limits do not jump: >=30..45% of the frame near-black (L < 8 in
    darktable's default render) and a lit subject (L p95 30..40)."""
    d = (an.get("dark", 0.0) - 0.30) / 0.15
    s = (L_at(an, 95) - 30.0) / 10.0
    return float(min(1.0, max(0.0, d)) * min(1.0, max(0.0, s)))


SPOT_P95, SPOT_MAX_EV, SPOT_P99_MAX = 60.0, 0.8, 88.0


def _spot_blend(an, dev, want, why):
    """Pool-of-light frames: expose for the lit subject (p95 -> SPOT_P95, at most
    SPOT_MAX_EV, p99 kept <= SPOT_P99_MAX), never for the median. Blended with the
    normal underexposure fix by spot_weight()."""
    w = spot_weight(an)
    if w <= 0:
        return dev, want, why, 0.0
    ws = _solve(an, 95, SPOT_P95)
    ds = max(0.0, min(ws, SPOT_MAX_EV, _solve(an, 99, SPOT_P99_MAX)))
    dev = (1 - w) * dev + w * ds
    want = (1 - w) * want + w * max(ws, 0.0)
    if w >= 0.5:
        why = (f"{why.split(' (')[0]}, spotlit subject in a dark surround ({an['dark']:.0%} near-black, "
               f"L p95 {L_at(an, 95):.0f}): exposed for the subject, surround kept dark")
    else:
        why += f", {w:.0%} toward subject exposure (dark surround)"
    return dev, want, why, round(w, 2)


def scene_class(an):
    ev = an.get("ev100")
    if ev is not None:
        return "night" if ev < 6 else "dim" if ev < 9.5 else "day"
    if L_at(an, 50) < 15 and L_at(an, 99.5) > 70:  # dark frame with light sources
        return "night"
    return "day"


# --------------------------------------------------------------- plan ---
def plan(an, enable=True, amber_min_c=25.0):
    """Auto corrections for one photo (see SKILL.md 'Different lighting')."""
    pl = dict(cls="?", ev=0.0, black=0.0, shadhi=None, wb_xy=None, wb=None, chroma_keep=1.0, shadows_add=0.0,
              lowkey=False, highkey=False, capped=False, spot=0.0, reasons=[], pred={})
    if not enable or an.get("kind") == "none":
        if an.get("note"):
            pl["reasons"].append(an["note"])
        return pl
    P = lambda q, d=0.0: L_at(an, q, d)
    cls = scene_class(an)
    lowkey = an["dark"] >= 0.4 and P(99.5) >= 75 and P(95) >= 55
    blackbg = P(95) < 2  # >=95% of the frame black: moon, stars, fireworks, a spotlit object
    pl.update(cls=cls, lowkey=lowkey or blackbg, blackbg=blackbg)
    dev, want, why, spot = 0.0, 0.0, "", 0.0
    if blackbg:
        why = "black frame with a small lit subject (moon, stars, spotlight): kept"
    elif lowkey:
        why = "low-key frame with well exposed highlights: kept"
    elif cls == "night":
        # keep night dark; only make sure the lit subject reads (p95) and the
        # mid-shadows are not black, never push the median above 30
        if P(95) < 55:
            want = max(_solve(an, 95, 55), _solve(an, 75, 12) if P(75) < 12 else 0.0)
            dev = min(want, 2.0, _solve(an, 50, 30))
            why = f"night, lit areas too dark (L p95 {P(95):.0f})"
        elif P(50) > 62:
            # EV100 < 6 also covers lamp-lit rooms (a home is EV 3-6), and a bright
            # phone night-mode render is a choice: only darken clearly overexposed frames
            dev = max(_solve(an, 50, 56), -1.0)
            why = f"overexposed (median L {P(50):.0f})"
    elif cls == "dim":
        if P(95) < 60 or P(50) < 22:
            want = max(_solve(an, 95, 60) if P(95) < 60 else 0.0, _solve(an, 50, 25) if P(50) < 22 else 0.0)
            dev = min(want, 1.5, _solve(an, 50, 45))
            why = f"dim interior underexposed (median L {P(50):.0f}, p95 {P(95):.0f})"
            dev, want, why, spot = _spot_blend(an, dev, want, why)
        elif P(50) > 62:
            dev = max(_solve(an, 50, 56), -1.0)
            why = f"overexposed (median L {P(50):.0f})"
    else:  # day
        if P(50) < 30 and P(95) < 70:
            want = _solve(an, 50, 32)
            dev = min(want, _solve(an, 90, 78) if P(90) < 78 else 0.0, 1.5)
            why = f"underexposed (median L {P(50):.0f}, p95 {P(95):.0f})"
            # no spotlight blend in daylight: dark forests/shade there are scenery, not a lit pool
        elif P(50) < 28:
            # bright areas are fine (backlight / sky): small lift only, highlights capped
            want = _solve(an, 50, 30)
            dev = min(want, _solve(an, 90, 80) if P(90) < 80 else 0.0, 0.4)
            why = f"dark subject against bright areas (median L {P(50):.0f}, p90 {P(90):.0f})"
        elif P(50) > 62 and P(2) > 20:
            dev = max(_solve(an, 50, 57), -1.0)
            why = f"overexposed / washed out (median L {P(50):.0f})"
        elif P(50) > 62:
            pl["highkey"] = True
            why = "high-key frame with real blacks (snow, surf, fog, white walls): kept"
    dev = round(max(dev, 0.0) if why.startswith(("night, lit", "dim", "under", "dark")) else dev, 2)
    pl["capped"] = want > 0 and dev < want - 0.05  # more lift was needed than the limits allow
    pl["spot"] = spot if dev > 0 or spot >= 0.5 else 0.0
    if abs(dev) >= 0.1:
        pl["ev"] = dev
        pl["reasons"].append(f"exposure {dev:+.2f} EV: {why}")
    elif why:
        pl["reasons"].append(why if why.endswith("kept") else "exposure ok (needed < 0.1 EV)")
    pl["pred"] = {f"p{q}": [round(P(q), 1), round(P(q, pl['ev']), 1)] for q in (50, 95)}

    if cls == "day" and not lowkey and P(50) < 28 and P(90) >= 65:
        # backlight: dark foreground, bright sky -> local shadow lift does the work,
        # the sky stays put (exposure lift capped so highlights do not clip)
        if pl["ev"] > 0.25:
            pl["reasons"] = [r for r in pl["reasons"] if not r.startswith("exposure")]
            pl["ev"] = 0.25
            pl["capped"] = True
            pl["reasons"].append("exposure +0.25 EV (capped: backlit)")
            pl["pred"] = {f"p{q}": [round(P(q), 1), round(P(q, pl['ev']), 1)] for q in (50, 95)}
        sh = round(min(45.0, max(20.0, 20 + (28 - P(50, pl["ev"])) * 1.5)), 1)
        pl["shadhi"] = (sh, -25.0)
        pl["reasons"].append(f"backlit (median L {P(50):.0f}, p90 {P(90):.0f}): local shadow lift {sh:.0f}")
    if cls in ("day", "dim") and not lowkey and P(2, pl["ev"]) > 26:
        # haze / flat light: no real blacks -> black-level correction in scene-linear
        k = _black_k(an, pl["ev"], 2, 22.0)
        if k > 0:
            E = (0.7 - an.get("bias", 0.0) + pl["ev"]) if an["kind"] == "raw" else pl["ev"]
            pl["black"] = round(k / 2 ** E, 6)
            pl["reasons"].append(f"hazy/flat, no blacks (L p2 {P(2, pl['ev']):.0f}): black level deepened")

    # white balance: artificial light only, all estimators must agree, partial
    if cls in ("night", "dim") and an.get("cast"):
        casts = [an["cast"][k] for k in ("grey", "sog4", "neutral20")]
        hues = [math.degrees(math.atan2(b, a)) % 360 for a, b in casts]
        chs = [math.hypot(a, b) for a, b in casts]
        kind = None
        if all(15 <= h <= 95 for h in hues) and min(chs) > amber_min_c:
            # real uncorrected sodium/tungsten measured C 30-90; weaker "casts" are
            # warm content (gold, wood, orange fur, red) or an AWB-handled room.
            # The portrait look lowers the bar (look["wb_amber_min_c"]): yellow
            # skin and yellow whites are worse on people than a lost ambience.
            kind = "amber"
        elif all(110 <= h <= 200 for h in hues) and min(chs) > 8:
            kind = "green"
        elif cls == "night" and all(270 <= h <= 330 for h in hues) and 15 < min(chs) and max(chs) < 70:
            kind = "purple"  # camera AWB over-correcting sodium/LED mix; strong neon (C > 70) is left alone
        if kind:
            i_med = int(np.argsort(chs)[1])
            C = chs[i_med]
            frac = {"amber": min(0.85, max(0.55, 1 - 22 / C)), "green": min(0.9, max(0.7, 1 - 10 / C)),
                    "purple": 0.5}[kind]
            wx, wy = white_xy_d50(an["white"][("grey", "sog4", "neutral20")[i_med]])
            x = D50_XY[0] + (wx - D50_XY[0]) * frac
            y = D50_XY[1] + (wy - D50_XY[1]) * frac
            pl["wb_xy"] = (round(x, 5), round(y, 5))
            pl["wb"] = dict(kind=kind, cast_ab=[round(v, 1) for v in casts[i_med]], cast_C=round(C, 1), frac=round(frac, 2),
                            white=an["white"][("grey", "sog4", "neutral20")[i_med]])
            label = {"amber": "sodium/tungsten", "green": "fluorescent", "purple": "purple night (AWB/mixed light)"}[kind]
            pl["reasons"].append(f"{label} cast C {C:.0f} "
                                 f"(a {casts[i_med][0]:+.0f}, b {casts[i_med][1]:+.0f}): {frac:.0%} removed")
    if cls == "night":
        pl["chroma_keep"] = 0.5
        pl["reasons"].append("night: half of the look's global/highlight desaturation kept off (lights keep colour)")
    return pl


def apply_plan_rgb(img, pl):
    """Apply a plan's exposure and WB to an sRGB PIL image (approximation used to
    build the verification 'before' image for JPEG/PNG/TIFF inputs)."""
    if not pl or (abs(pl.get("ev", 0)) < 0.05 and not pl.get("wb") and not pl.get("black")):
        return img
    lin = srgb_to_lin(np.asarray(img, dtype=np.float32) / 255.0)
    lin = lin * 2 ** pl.get("ev", 0.0)
    if pl.get("black"):
        k = pl["black"] * 2 ** pl.get("ev", 0.0)
        lin = np.clip((lin - k) / (1 - k), 0, None)
    if pl.get("wb"):
        w = np.asarray(pl["wb"]["white"], dtype=np.float64)
        g = float(_lum(w)) / w
        g = 1 + (g - 1) * pl["wb"]["frac"]
        lin = lin * (g / float(_lum(g)))[None, None, :]
    return Image.fromarray((lin_to_srgb(lin) * 255 + 0.5).astype(np.uint8))


def describe(an, pl):
    ev = an.get("ev100")
    return (f"{Path(an['path']).name}: {pl['cls']}" + (f" (EV100 {ev:.1f})" if ev is not None else " (EV100 ?)")
            + (f", median L {pl['pred']['p50'][0]:.0f}->{pl['pred']['p50'][1]:.0f}" if pl.get("pred") else "")
            + "".join(f"\n    - {r}" for r in pl["reasons"]))


if __name__ == "__main__":
    for f in sys.argv[1:]:
        a = analyze(f)
        print(describe(a, plan(a)))

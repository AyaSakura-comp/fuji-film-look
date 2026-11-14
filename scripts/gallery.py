#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "pillow", "rawpy"]
# ///
"""Before/after gallery (a static web page) for a run.py output folder.

Usage:
  uv run gallery.py OUT_DIR [-o GALLERY_DIR] [--before auto|camera|default] [--title TITLE]
                    [--lang zh|en] [--start NAME] [--long 2048] [--standalone]

For every photo in OUT_DIR/_report/summary.json it writes
  GALLERY_DIR/img/a/<name>.jpg   the look (from run.py's output)
  GALLERY_DIR/img/b/<name>.jpg   the "before", on the same pixel grid so a slider lines up
  GALLERY_DIR/img/t/<name>.jpg   a thumbnail for the contact sheet
  GALLERY_DIR/index.html         slider / before / after viewer, contact sheet, the auto
                                 corrections per photo (why), and the frames verify flagged

"Before" (--before auto, default): the camera JPEG of the RAW (RAW+JPG shooting: same folder,
--camera-dir, the RAW folder's parent, or ../JPG) when one exists and has the same framing; else the input itself for JPEG/PNG/TIFF inputs; else
darktable's default render of the RAW (no look, no corrections), rendered here. Camera JPEGs are
lens-corrected and 0.5-3.5% larger in scale than the RAW render, so each one is registered to
the render (scale + radial lens term + shift, fitted by phase correlation on a 5x5 patch grid;
frames without texture borrow the median model of frames at the same focal length) and both
are cropped by the same 1-3% margin.
--before default always uses darktable's default render; --before camera never renders.

Web copies are re-encoded (quality 85) with no EXIF, so no GPS or serial numbers are published.
index.html is an Artifact page fragment (no <!doctype>): publish it with the Artifact tool, with
`root` = GALLERY_DIR and the img/ files in `files` (3 files per photo; one publish takes 255).
--standalone writes a full HTML document instead, for opening locally or any static host.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fuji_look as FL  # noqa: E402

JPG_EXT = (".JPG", ".jpg", ".JPEG", ".jpeg")
THUMB, Q = 420, 85

STR = {
    "zh": dict(
        lang="zh-Hant", frames="張", look_label="{look}", before_camera="相機直出 JPG", before_source="原圖",
        before_default="darktable 預設", compare="對比", aria_viewer="前後對比", aria_divider="對比分隔線",
        aria_modes="顯示方式", prev="上一張", next="下一張", sheet="整卷 {n} 張",
        sheet_hint="點縮圖放上燈箱 · <span class=\"mono\">●</span> 自動檢查有提醒",
        hint="拖曳照片移動分隔線；只看單張時左右滑動換張。<span class=\"keys\">鍵盤：← → 換張，1 2 3 切換顯示方式。</span>",
        cls_day="白天", cls_dim="室內弱光", cls_night="夜景", spot="聚光", capped="提亮有上限", signs="招牌高光壓回",
        alt_a="{n}，{look} 成品", alt_b="{n}，{before}",
        f_camera="Before：相機直出的 JPG。相機會做鏡頭校正，比例和 RAW 成品差約 1%，已逐張對齊並同樣裁掉邊緣 1–2%。",
        f_source="Before：原圖（JPEG/PNG/TIFF 輸入）。",
        f_default="Before：darktable 預設顯影（沒有風格、沒有自動修正），代表 RAW 原本的樣子。",
        f_after="After：用 darktable 套 {look} 風格{grain}，每張依光線自動修正（原因列在每張下面）。"
                "網頁版長邊約 {long} px，重新壓縮過，已移除 EXIF（含 GPS）；完整成品在 {out}。",
        grain_off="（不加顆粒）", grain_on="（加顆粒）",
    ),
    "en": dict(
        lang="en", frames="frames", look_label="{look}", before_camera="Camera JPEG", before_source="Original",
        before_default="darktable default", compare="Compare", aria_viewer="Before and after",
        aria_divider="Comparison divider", aria_modes="View", prev="Previous", next="Next",
        sheet="All {n} frames",
        sheet_hint="Click a thumbnail to view it · <span class=\"mono\">●</span> flagged by the checks",
        hint="Drag across the photo to move the divider; swipe to change photo in single view."
             "<span class=\"keys\"> Keys: ← → change photo, 1 2 3 switch view.</span>",
        cls_day="day", cls_dim="dim interior", cls_night="night", spot="spotlight", capped="lift capped", signs="signs recovered",
        alt_a="{n}, {look}", alt_b="{n}, {before}",
        f_camera="Before: the camera JPEG. The camera corrects lens distortion, so its scale differs from the RAW "
                 "render by ~1%; every pair is registered and both are cropped by the same 1–2% margin.",
        f_source="Before: the original file (JPEG/PNG/TIFF input).",
        f_default="Before: darktable's default render of the RAW (no look, no corrections).",
        f_after="After: the {look} look rendered with darktable{grain}, with per-photo lighting corrections "
                "(listed under each photo). Web copies are ~{long} px, re-encoded, EXIF (and GPS) removed; "
                "full-quality files are in {out}.",
        grain_off=" (no grain)", grain_on=" (with grain)",
    ),
}


# ------------------------------------------------------------ registration ---
def _shift(pa, pb):
    """(dy, dx) such that b[p + d] ~ a[p], and the correlation peak."""
    win = np.outer(np.hanning(pa.shape[0]), np.hanning(pa.shape[1]))
    fa = np.fft.fft2((pa - pa.mean()) * win)
    fb = np.fft.fft2((pb - pb.mean()) * win)
    r = fb * np.conj(fa)
    r /= np.abs(r) + 1e-9
    c = np.real(np.fft.ifft2(r))
    i = np.unravel_index(np.argmax(c), c.shape)
    return [v if v <= s // 2 else v - s for v, s in zip(i, c.shape)], c.max()


GRID = (0.08, 0.29, 0.5, 0.71, 0.92)


def _model(R, wts, nr):
    """Least squares for d = X*(k + k2*r^2) + t (both axes, shared k, k2)."""
    X, Y, r2 = R[:, 1], R[:, 0], (R[:, 0] ** 2 + R[:, 1] ** 2) / nr ** 2
    M = np.zeros((2 * len(R), 4))
    v = np.zeros(2 * len(R))
    M[0::2, 0], M[0::2, 1], M[0::2, 2], v[0::2] = X, X * r2, 1, R[:, 3]
    M[1::2, 0], M[1::2, 1], M[1::2, 3], v[1::2] = Y, Y * r2, 1, R[:, 2]
    w2 = np.repeat(wts, 2)
    sol, *_ = np.linalg.lstsq(M * w2[:, None], v * w2, rcond=None)
    res = np.abs(M @ sol - v).reshape(-1, 2).max(1)
    return sol, res


def register(a, b, patch=256):
    """Map the camera JPEG b onto the render a: scale + radial (lens-distortion) term + shift,
    d(p) = (p - c) * (k + k2 * r^2) + t, so that b[p + d] ~ a[p]. Fitted on a 5x5 grid of patches
    by phase correlation (night frames: dark patches still correlate, so no brightness gate beyond
    'not flat'), with outlier rejection. Camera JPEGs differ from darktable's render by 0.5-3.5% in
    scale and by the lens-distortion correction, which a uniform scale cannot follow. None if
    unreliable."""
    A = np.sqrt(np.asarray(a.convert("L"), float))  # sqrt: lift dark night patches
    B = np.sqrt(np.asarray(b.convert("L"), float))
    h, w = A.shape
    cy, cx, nr = h / 2, w / 2, math.hypot(w, h) / 2
    patch = min(patch, h // 4, w // 4)
    rows = []
    for fy in GRID:
        for fx in GRID:
            y0 = min(max(0, int(fy * h - patch / 2)), h - patch)
            x0 = min(max(0, int(fx * w - patch / 2)), w - patch)
            pa, pb = A[y0:y0 + patch, x0:x0 + patch], B[y0:y0 + patch, x0:x0 + patch]
            if pa.std() < 0.15 or pb.std() < 0.15:  # flat (black sky, blown sign): nothing to match
                continue
            (dy, dx), pk = _shift(pa, pb)
            if pk < 0.06 or abs(dy) > 90 or abs(dx) > 90:
                continue
            rows.append((y0 + patch / 2 - cy, x0 + patch / 2 - cx, dy, dx, pk))
    R = np.array(rows)
    for _ in range(3):  # fit, drop the worst outliers, refit
        if len(R) < 6:
            return None
        sol, res = _model(R, R[:, 4], nr)
        keep = res <= max(2.5, 2.5 * np.median(res))
        if keep.all():
            break
        R = R[keep]
    k, k2, tx, ty = sol
    if len(R) < 6 or res.max() > 3 or abs(k) > 0.06 or abs(k2) > 0.06:
        return None
    return dict(k=float(k), k2=float(k2), tx=float(tx), ty=float(ty), n=len(R), s=1 + float(k + k2 / 2))


def _disp(p, x, y, w, h):
    nr = math.hypot(w, h) / 2
    X, Y = x - w / 2, y - h / 2
    f = p["k"] + p["k2"] * (X * X + Y * Y) / nr ** 2
    return X * f + p["tx"], Y * f + p["ty"]


def warp(b, p, cells=24):
    """Resample b at p + d(p) for every output pixel p (PIL mesh of small quads)."""
    w, h = b.size
    xs = np.linspace(0, w, cells + 1)
    ys = np.linspace(0, h, cells + 1)
    mesh = []
    for i in range(cells):
        for j in range(cells):
            x0, x1, y0, y1 = xs[j], xs[j + 1], ys[i], ys[i + 1]
            quad = []
            for x, y in ((x0, y0), (x0, y1), (x1, y1), (x1, y0)):  # UL, LL, LR, UR
                dx, dy = _disp(p, x, y, w, h)
                quad += [x + dx, y + dy]
            mesh.append(((round(x0), round(y0), round(x1), round(y1)), quad))
    return b.transform(b.size, Image.MESH, mesh, resample=Image.BICUBIC)


def margins(p, w, h):
    """Crop (x, y) that keeps only pixels the warped image really covers, same on both sides."""
    t = np.linspace(0, 1, 33)
    mx = max(abs(_disp(p, x, y * h, w, h)[0]) for x in (0, w) for y in t)
    my = max(abs(_disp(p, x * w, y, w, h)[1]) for y in (0, h) for x in t)
    return math.ceil(mx) + 2, math.ceil(my) + 2


# ------------------------------------------------------------------ frames ---
def fit(im, long):
    w, h = im.size
    s = long / max(w, h)
    return im.resize((round(w * s), round(h * s)), Image.LANCZOS) if s < 1 else im


def oriented_size(path):
    with Image.open(path) as im:
        w, h = im.size
        if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
            w, h = h, w
    return w, h


def camera_jpeg(src, extra=()):
    """The camera JPEG of a RAW: same folder, --camera-dir, the parent folder (RAWs sorted into a
    RAW/ subfolder) or a sibling JPG/JPEG folder."""
    p = Path(src)
    up = p.parent.parent
    for d in (p.parent, *extra, up, up / "JPG", up / "JPEG", up / "jpg"):
        for ext in JPG_EXT:
            c = Path(d) / (p.stem + ext)
            if c.exists() and c.resolve() != p.resolve():
                return c
    return None


def one(job):
    a = fit(Image.open(job["final"]).convert("RGB"), job["long"])
    b = Image.open(job["before"])
    b.draft("RGB", (a.width * 2, a.height * 2))
    try:
        focal = float(b.getexif().get_ifd(0x8769).get(0x920A) or 0)  # FocalLength: same lens setting
    except Exception:
        focal = 0.0
    b = ImageOps.exif_transpose(b).convert("RGB").resize(a.size, Image.LANCZOS)  # same grid as the after
    p = job.get("p") or (register(a, b) if job["kind"] == "camera" else None)
    if p:
        b = warp(b, p)
        w, h = a.size
        mx, my = margins(p, w, h)
        a, b = a.crop((mx, my, w - mx, h - my)), b.crop((mx, my, w - mx, h - my))  # no empty edges
    g = Path(job["gdir"]) / "img"
    a.save(g / "a" / f"{job['n']}.jpg", quality=Q, optimize=True, progressive=True)
    b.save(g / "b" / f"{job['n']}.jpg", quality=Q, optimize=True, progressive=True)
    fit(a, THUMB).save(g / "t" / f"{job['n']}.jpg", quality=78, optimize=True, progressive=True)
    return dict(n=job["n"], size=a.size, al=round(p["s"], 4) if p else None, p=p, focal=focal,
                borrowed=bool(job.get("p")))


def process(jobs, workers):
    """Render every frame; camera frames that could not be registered (black night sky, motion,
    no texture) get the median lens model of registered frames at the same focal length."""
    with ProcessPoolExecutor(workers) as ex:
        done = {r["n"]: r for r in ex.map(one, jobs)}
        fixed = []
        for j in jobs:
            r = done[j["n"]]
            if j["kind"] != "camera" or r["p"] or not r["focal"]:
                continue
            same = [o["p"] for o in done.values() if o["p"] and not o["borrowed"]
                    and abs(o["focal"] - r["focal"]) <= 0.03 * r["focal"]]
            if len(same) >= 3:
                fixed.append(dict(j, p={k: float(np.median([q[k] for q in same])) for k in ("k", "k2", "tx", "ty", "s")}))
        done.update({r["n"]: r for r in ex.map(one, fixed)})
    return done


def render_defaults(items, size, work):
    """darktable's default render of each RAW (no look, no corrections), one JPEG per item."""
    import run as R  # darktable process helpers (lazy: pulls run.py's imports)
    cli = FL.find_cli()
    template = R.template_config(cli)
    links, out = work / "links", work / "out"
    links.mkdir()
    out.mkdir()
    for it in items:  # symlinks: a sidecar XMP next to the original (darktable GUI edits) is not picked up
        os.symlink(it["src"], links / (it["name"] + Path(it["src"]).suffix))
    feeds = sorted(links.iterdir())
    n = max(1, min(3, math.ceil(len(feeds) / 12)))
    threads = max(2, (os.cpu_count() or 8) // n + 1)
    procs = [R.launch(cli, feeds[i::n], None, out, size, False, threads, template, 95, work / f"dt{i}.log")
             for i in range(n)]
    for p in procs:
        p.wait()
        shutil.rmtree(p._cfg, ignore_errors=True)
    for it in items:
        f = out / f"{it['name']}.jpg"
        it["before"], it["kind"] = (str(f), "default") if f.exists() else (None, None)


def roll_line(finals, n, look, lang):
    """'2026.11.05 · Sony ILCE-7CR · 67 frames · portrait' from the outputs' EXIF."""
    dates, cams = set(), {}
    for f in finals[:: max(1, len(finals) // 40)]:
        try:
            ex = Image.open(f).getexif()
            d = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
            if d:
                dates.add(str(d)[:10].replace(":", "."))
            cam = " ".join(str(ex.get(t, "")).strip() for t in (0x010F, 0x0110)).strip()
            if cam:
                cams[cam] = cams.get(cam, 0) + 1
        except Exception:
            pass
    parts = []
    if dates:
        ds = sorted(dates)
        parts.append(ds[0] if len(ds) == 1 else f"{ds[0]}–{ds[-1]}")
    if cams:
        top = sorted(cams, key=cams.get, reverse=True)
        cam = top[0]
        if cam.lower().startswith("sony ") and " ilce-" in cam.lower():
            cam = "Sony " + cam.split(" ", 1)[1]
        parts.append(cam + (f" +{len(top) - 1}" if len(top) > 1 else ""))
    parts.append(f"{n} {STR[lang]['frames']}")
    parts.append(f"{look} look")
    return " · ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", help="a run.py output folder (with _report/summary.json)")
    ap.add_argument("-o", "--gallery", help="gallery folder (default: OUT_DIR/_gallery)")
    ap.add_argument("--before", choices=["auto", "camera", "default"], default="auto")
    ap.add_argument("--title", help="page title (default: the input folder name)")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh", help="page language (zh = Traditional Chinese)")
    ap.add_argument("--camera-dir", action="append", default=[],
                    help="folder with the camera JPEGs (default: next to the RAW, its parent, ../JPG)")
    ap.add_argument("--start", help="photo name shown first (default: the first)")
    ap.add_argument("--long", type=int, default=2048, help="long edge of the web copies (px)")
    ap.add_argument("--standalone", action="store_true", help="full HTML document (not an Artifact fragment)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    T0 = time.time()
    out = Path(a.outdir).expanduser().resolve()
    summ = out / "_report" / "summary.json"
    if not summ.exists():
        sys.exit(f"{summ} not found: run run.py first")
    summary = json.loads(summ.read_text())
    res = [r for r in summary["results"] if r.get("final") and Path(r["final"]).exists()]
    if not res:
        sys.exit("no finished outputs in summary.json")
    res.sort(key=lambda r: r["name"])
    gdir = Path(a.gallery).expanduser().resolve() if a.gallery else out / "_gallery"
    for k in "abt":
        (gdir / "img" / k).mkdir(parents=True, exist_ok=True)
    run = summary.get("run") or {}  # older summaries: look from the file name, grain from summary.md
    look = run.get("look") or Path(res[0]["final"]).stem[len(res[0]["name"]) + 1:] or "fuji"
    if "grain" in run:
        grain = run["grain"] > 0
    else:
        md = out / "_report" / "summary.md"
        grain = None if not md.exists() else ("; grain off;" not in md.read_text()[:2000])
    S = STR[a.lang]

    # pick the "before" for every photo
    items, need = [], []
    for r in res:
        it = dict(name=r["name"], src=r["src"], final=r["final"], r=r, before=None, kind=None)
        raw = Path(r["src"]).suffix.lower() in FL.RAW_EXT
        if not raw:
            it["before"], it["kind"] = r["src"], "source"
        elif a.before != "default":
            cj = camera_jpeg(r["src"], [Path(d).expanduser() for d in a.camera_dir])
            if cj:
                (w1, h1), (w2, h2) = oriented_size(cj), oriented_size(r["final"])
                if abs(w1 / h1 - w2 / h2) < 0.015 * (w2 / h2):  # same framing (not a 16:9 / crop-mode JPEG)
                    it["before"], it["kind"] = str(cj), "camera"
        if it["before"] is None and raw and a.before != "camera":
            need.append(it)
        items.append(it)
    if need:
        print(f"rendering darktable's default for {len(need)} RAW(s) without a matching camera JPEG ...")
        with tempfile.TemporaryDirectory(prefix="fujigal-") as td:
            render_defaults(need, a.long, Path(td))
            # read the renders before the temp folder goes away
            jobs = [dict(n=it["name"], final=it["final"], before=it["before"], kind=it["kind"], long=a.long,
                         gdir=str(gdir)) for it in items if it["before"]]
            done = process(jobs, a.workers)
    else:
        jobs = [dict(n=it["name"], final=it["final"], before=it["before"], kind=it["kind"], long=a.long,
                     gdir=str(gdir)) for it in items if it["before"]]
        done = process(jobs, a.workers)
    skipped = [it["name"] for it in items if it["name"] not in done]

    meta = []
    for it in items:
        if it["name"] not in done:
            continue
        r, d = it["r"], done[it["name"]]
        sz, al = d["size"], d["al"]
        p = r.get("plan") or {}
        fails = [c["name"] for c in r.get("checks", []) if c.get("hard") and not c.get("ok", True)]
        if r.get("error"):
            fails.append(r["error"])
        meta.append(dict(n=it["name"], w=sz[0], h=sz[1], al=al, bk=it["kind"], ok=bool(r.get("passed")),
                         cls=p.get("cls"), ev=p.get("ev", 0), spot=p.get("spot", 0), capped=bool(p.get("capped")),
                         signs=p.get("signs", 0),
                         why=p.get("reasons", []), fails=fails))
    kinds = sorted({m["bk"] for m in meta}, key=["camera", "source", "default"].index)
    if not meta:
        sys.exit("no frames: every photo lacked a usable 'before' (try --before default)")

    # page
    folder = Path(res[0]["src"]).parent
    if folder.name.lower() in ("raw", "raws", "arw") and folder.parent.name:
        folder = folder.parent  # .../0929熊本商店街/RAW -> 0929熊本商店街
    title = a.title or folder.name or out.name
    home = str(Path.home())
    out_txt = str(out).replace(home, "~", 1) if str(out).startswith(home) else str(out)
    T = {**S, "title": title, "look": look, "n": len(meta),
         "roll": roll_line([it["final"] for it in items], len(meta), look, a.lang),
         "before_main": S["before_" + kinds[0]], "after": look,
         "sheet": S["sheet"].format(n=len(meta)),
         "foot_before": "</p><p>".join(html.escape(S["f_" + k]) for k in kinds),
         "foot_after": html.escape(S["f_after"].format(look=look, grain="" if grain is None else S["grain_on" if grain else "grain_off"],
                                                       long=a.long, out=out_txt))}
    start = a.start if a.start and any(m["n"] == a.start for m in meta) else meta[0]["n"]
    js = dict(lang=a.lang, look=look, start=start, before={k: S["before_" + k] for k in kinds},
              cls={"day": S["cls_day"], "dim": S["cls_dim"], "night": S["cls_night"]}, spot=S["spot"],
              capped=S["capped"], signs=S["signs"], alt_a=S["alt_a"], alt_b=S["alt_b"])
    page = (HERE / "gallery.html").read_text()
    raw_keys = {"sheet_hint", "hint", "foot_before", "foot_after"}  # already HTML
    page = re.sub(r"\{\{(\w+)\}\}", lambda m: str(T[m.group(1)]) if m.group(1) in raw_keys
                  else html.escape(str(T[m.group(1)])), page)
    blob = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("/*META*/", blob(meta)).replace("/*STR*/", blob(js))
    if a.standalone:
        page = (f'<!doctype html>\n<html lang="{S["lang"]}">\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n' + page)
    (gdir / "index.html").write_text(page)

    mb = sum(f.stat().st_size for f in (gdir / "img").rglob("*.jpg")) / 1e6
    print(f"{len(meta)} frames -> {gdir}/index.html  ({mb:.0f} MB of images, {time.time() - T0:.0f} s)")
    print("before: " + ", ".join(f"{k} {sum(m['bk'] == k for m in meta)}" for k in kinds)
          + (f"; skipped (no before): {', '.join(skipped)}" if skipped else ""))
    aligned = [m["al"] for m in meta if m["al"]]
    if aligned:
        nb = sum(d["borrowed"] for d in done.values())
        print(f"camera JPEGs registered: {len(aligned)}/{sum(m['bk'] == 'camera' for m in meta)} "
              f"(scale {min(aligned):.4f}-{max(aligned):.4f}"
              + (f"; {nb} with the lens model of other frames at the same focal length" if nb else "") + ")")
    if 3 * len(meta) > 255:
        print(f"note: {3 * len(meta)} image files; an Artifact publish takes 255, so publish in parts to the same url")


if __name__ == "__main__":
    main()

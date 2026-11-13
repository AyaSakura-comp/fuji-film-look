#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pillow", "numpy"]
# ///
"""iPhone-Photos style album (a static web page) for a gallery.py folder.

Usage:
  uv run album.py GALLERY_DIR [--summary OUT_DIR/_report/summary.json] [--title TITLE]
                  [--lang zh|en] [-o index.html]

GALLERY_DIR is what gallery.py built: img/a (the look), img/b (the before), img/t (thumbs).
This writes an album page that behaves like the Photos app on an iPhone:

  * a square thumbnail grid (3 columns on a phone, more on a desktop)
  * tap a thumbnail -> it grows into a full-screen photo (no scrolling, no page jump)
  * pinch / double-tap / wheel to zoom, drag to pan, swipe left-right between photos
  * swipe down, the back chevron, or Esc -> it shrinks back into the grid
  * a 對比 button (or key b) swaps to the before image, which is pixel-registered

The slider gallery stays reachable: an existing index.html is kept as compare.html and the album
links to it. Re-running gallery.py rewrites index.html, so run album.py after gallery.py.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STR = {
    "zh": dict(lang="zh-Hant", hint="點一張放大 · 雙指或雙擊縮放 · 左右滑換張 · 下滑收回",
               cmp_link="前後對比模式 →", aria="相簿", close="返回相簿", compare="對比",
               alt_a="{n}，成品", before="相機直出", spot="聚光", capped="提亮到上限",
               cls={"day": "白天", "dim": "昏暗", "night": "夜拍"},
               foot="Fuji look 由 darktable 從 RAW 輸出；相簿內的圖都是 2048 px、無 EXIF。"),
    "en": dict(lang="en", hint="tap to open · pinch or double-tap to zoom · swipe to change · swipe down to close",
               cmp_link="before / after mode →", aria="album", close="back to album", compare="Before",
               alt_a="{n}", before="before", spot="spotlight", capped="lift capped",
               cls={"day": "day", "dim": "dim", "night": "night"},
               foot="Fuji look rendered from RAW by darktable; web copies are 2048 px with no EXIF."),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gallery", help="a folder built by gallery.py (img/a, img/b, img/t)")
    ap.add_argument("--summary", help="run.py _report/summary.json (default: GALLERY/../_report/summary.json)")
    ap.add_argument("--title", help="album title (default: the gallery's parent folder name)")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh")
    ap.add_argument("-o", "--out", help="output html (default: GALLERY/index.html)")
    a = ap.parse_args()

    gdir = Path(a.gallery).expanduser().resolve()
    for k in "abt":
        if not (gdir / "img" / k).is_dir():
            sys.exit(f"{gdir}/img/{k} missing: run gallery.py first")
    summ = Path(a.summary).expanduser().resolve() if a.summary else gdir.parent / "_report" / "summary.json"
    out = Path(a.out).expanduser().resolve() if a.out else gdir / "index.html"

    names = sorted(p.stem for p in (gdir / "img" / "t").glob("*.jpg") if not p.name.startswith("._"))
    if not names:
        sys.exit(f"no thumbnails in {gdir}/img/t")

    # per-photo size + what the run auto-corrected (summary.json is optional: the album works without it)
    info, finals = {}, []
    if summ.exists():
        summary = json.loads(summ.read_text())
        run = summary.get("run") or {}
        for r in summary.get("results", []):
            if r.get("final") and Path(r["final"]).exists():
                finals.append(r["final"])
            p = r.get("plan") or {}
            info[r["name"]] = dict(ok=bool(r.get("passed")), cls=p.get("cls"), ev=p.get("ev", 0) or 0,
                                   spot=p.get("spot", 0), capped=bool(p.get("capped")))
        look = run.get("look") or "fuji"
    else:
        print(f"note: {summ} not found: no per-photo correction notes")
        look = "fuji"

    meta = []
    for n in names:
        f = gdir / "img" / "a" / f"{n}.jpg"
        if not f.exists():
            print(f"skip {n}: no img/a/{n}.jpg")
            continue
        with Image.open(f) as im:
            w, h = im.size
        d = info.get(n, {})
        meta.append(dict(n=n, w=w, h=h, ok=d.get("ok", True), cls=d.get("cls"), ev=d.get("ev", 0),
                         spot=d.get("spot", 0), capped=d.get("capped", False)))
    if not meta:
        sys.exit("no frames")

    # keep the slider gallery reachable
    cur = gdir / "index.html"
    cmp_page = gdir / "compare.html"
    if out == cur and cur.exists() and not cmp_page.exists():
        shutil.copyfile(cur, cmp_page)

    title = a.title or gdir.parent.name or "album"
    S = STR[a.lang]
    try:  # the run outputs keep EXIF; the web copies do not
        from gallery import roll_line
        roll = roll_line(finals or [str(gdir / "img" / "a" / f"{m['n']}.jpg") for m in meta],
                         len(meta), look, a.lang)
    except Exception:
        roll = f"{len(meta)} {'張' if a.lang == 'zh' else 'frames'} · {look} look"

    T = dict(title=title, roll=roll, hint=S["hint"], cmp_link=S["cmp_link"], aria=S["aria"],
             close=S["close"], compare=S["compare"], foot=S["foot"])
    page = (HERE / "album.html").read_text()
    page = re.sub(r"\{\{(\w+)\}\}", lambda m: html.escape(str(T[m.group(1)])), page)
    blob = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("/*META*/", blob(meta)).replace("/*STR*/", blob({k: S[k] for k in
               ("alt_a", "before", "spot", "capped", "cls")}))
    out.write_text('<!doctype html>\n<html lang="%s">\n<meta charset="utf-8">\n'
                   '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                   '<meta name="theme-color" content="#0d0d0e">\n' % S["lang"] + page)
    print(f"{len(meta)} frames -> {out}")
    if cmp_page.exists():
        print(f"slider gallery kept at {cmp_page} (linked from the album header)")


if __name__ == "__main__":
    main()

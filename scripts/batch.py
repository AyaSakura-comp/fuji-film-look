#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "pillow"]
# ///
"""Batch-apply a Fuji look to many photos, verify each, and summarise.

Usage:
  uv run batch.py INPUT_DIR_OR_FILES... -o OUT_DIR [--look street] [--width 1600]
                  [--jobs 3] [--reference REF...] [-- extra fuji_look.py args]

For every input:
  1. baseline  = darktable's *default* render (no look). For RAW files this is
                 the fair "before" image; for JPEG/PNG/TIFF the file itself is used.
  2. edited    = fuji_look.py render
  3. verify.py baseline vs edited (+ --reference if given) -> JSON
Writes OUT_DIR/summary.md (table of PASS/FAIL + key metrics + failed checks),
OUT_DIR/summary.json, and OUT_DIR/contact_sheet.jpg (before/after pairs).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fuji_look import find_cli  # noqa: E402

RAW_EXT = {".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".orf", ".rw2", ".pef", ".srw", ".nrw", ".3fr", ".iiq"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"} | RAW_EXT


def baseline(src: Path, out: Path, width: int):
    """darktable default render (scene-referred defaults, no look)."""
    if src.suffix.lower() not in RAW_EXT:
        im = Image.open(src).convert("RGB")
        if width and im.size[0] > width:
            im = im.resize((width, round(im.size[1] * width / im.size[0])), Image.LANCZOS)
        im.save(out, quality=95)
        return
    with tempfile.TemporaryDirectory(prefix="fujibase-") as td:
        cmd = [find_cli(), str(src), str(out), "--apply-custom-presets", "false", "--hq", "true"]
        if width:
            cmd += ["--width", str(width)]
        cmd += ["--core", "--configdir", td, "--library", ":memory:",
                "--conf", "plugins/imageio/format/jpeg/quality=95"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode or not out.exists():
            raise RuntimeError("baseline failed: " + (r.stderr or r.stdout)[-400:])


def process(src: Path, outdir: Path, a, extra):
    stem = src.stem
    base = outdir / f"{stem}_base.jpg"
    edit = outdir / f"{stem}_{a.look}.jpg"
    rep = outdir / f"{stem}_report.json"
    try:
        if not base.exists() or a.force:
            baseline(src, base, a.width)
        if not edit.exists() or a.force:
            if edit.exists():
                edit.unlink()
            cmd = [sys.executable, str(HERE / "fuji_look.py"), str(src), "-o", str(edit), "--look", a.look]
            if a.width:
                cmd += ["--width", str(a.width)]
            r = subprocess.run(cmd + extra, capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError("look failed: " + (r.stderr or r.stdout)[-400:])
        vcmd = [sys.executable, str(HERE / "verify.py"), str(base), str(edit), "--look", a.look, "--json", str(rep)]
        if a.reference:
            vcmd += ["--reference", *a.reference]
        subprocess.run(vcmd, capture_output=True, text=True)
        data = json.loads(rep.read_text())
        return dict(name=stem, base=str(base), edit=str(edit), passed=data["passed"],
                    metrics=data["metrics"], checks=data["checks"])
    except Exception as e:  # keep the batch going
        return dict(name=stem, error=str(e), passed=False, checks=[], metrics={})


def contact_sheet(results, path, cell_h=260, cols=2):
    pairs = []
    for r in results:
        if "error" in r:
            continue
        b, e = Image.open(r["base"]).convert("RGB"), Image.open(r["edit"]).convert("RGB")
        w = round(b.size[0] * cell_h / b.size[1])
        pairs.append((r, b.resize((w, cell_h), Image.LANCZOS), e.resize((w, cell_h), Image.LANCZOS)))
    if not pairs:
        return
    pw = max(p[1].size[0] for p in pairs) * 2 + 30
    rows = (len(pairs) + cols - 1) // cols
    sheet = Image.new("RGB", (pw * cols, rows * (cell_h + 34)), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for i, (r, b, e) in enumerate(pairs):
        x0, y0 = (i % cols) * pw, (i // cols) * (cell_h + 34)
        sheet.paste(b, (x0 + 10, y0 + 28)); sheet.paste(e, (x0 + 20 + b.size[0], y0 + 28))
        tag = "PASS" if r["passed"] else "FAIL"
        d.text((x0 + 12, y0 + 8), f"{r['name']}  [{tag}]  default render  ->  look",
               fill=(120, 220, 120) if r["passed"] else (240, 110, 110))
    sheet.save(path, quality=88)


def main():
    argv = sys.argv[1:]
    extra = []
    if "--" in argv:
        i = argv.index("--"); extra = argv[i + 1:]; argv = argv[:i]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--look", default="classic-chrome")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--reference", nargs="+")
    ap.add_argument("--force", action="store_true", help="re-render even if outputs exist")
    a = ap.parse_args(argv)

    files = []
    for p in map(Path, a.inputs):
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in IMG_EXT)
        elif p.suffix.lower() in IMG_EXT:
            files.append(p)
    outdir = Path(a.outdir); outdir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(lambda f: process(f, outdir, a, extra), files))

    # summary
    lines = [f"# Batch: look={a.look}  files={len(results)}  "
             f"pass={sum(r['passed'] for r in results)}  fail={sum(not r['passed'] for r in results)}", "",
             "| file | result | L p1 | median L | chroma ratio | mid-std ratio | clipped % | failed (hard) | warnings |",
             "|---|---|---|---|---|---|---|---|---|"]
    fail_count, warn_count = {}, {}
    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | ERROR | | | | | | {r['error'][:80]} | |"); continue
        m = r["metrics"]
        fails = [c["name"] for c in r["checks"] if not c["ok"] and c["hard"]]
        warns = [c["name"] for c in r["checks"] if not c["ok"] and not c["hard"]]
        for f in fails: fail_count[f] = fail_count.get(f, 0) + 1
        for w in warns: warn_count[w] = warn_count.get(w, 0) + 1
        cr = m["chroma_mean"][1] / max(m["chroma_mean"][0], 1e-6)
        sr = m["L_mid_std"][1] / max(m["L_mid_std"][0], 1e-6)
        lines.append(f"| {r['name']} | {'PASS' if r['passed'] else 'FAIL'} | {m['L_p1'][0]:.1f}→{m['L_p1'][1]:.1f} | "
                     f"{m['L_p50'][0]:.1f}→{m['L_p50'][1]:.1f} | {cr:.2f} | {sr:.2f} | "
                     f"{m['clipped_%'][0]:.2f}→{m['clipped_%'][1]:.2f} | {', '.join(fails)} | {', '.join(warns)} |")
    lines += ["", "## Failure counts (hard)"] + [f"- {k}: {v}" for k, v in sorted(fail_count.items(), key=lambda x: -x[1])]
    lines += ["", "## Warning counts"] + [f"- {k}: {v}" for k, v in sorted(warn_count.items(), key=lambda x: -x[1])]
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")
    (outdir / "summary.json").write_text(json.dumps(results, indent=1, default=str))
    contact_sheet(results, outdir / "contact_sheet.jpg")
    print("\n".join(lines))
    print(f"\nsheet: {outdir / 'contact_sheet.jpg'}")


if __name__ == "__main__":
    main()

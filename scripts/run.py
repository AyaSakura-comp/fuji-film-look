#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "pillow", "rawpy"]
# ///
"""Fast Fuji-look pipeline for 1..N photos (target: 100 RAWs < 60 s on an M4).

Usage:
  uv run run.py INPUTS... -o OUT_DIR [--look street] [--size 2048] [--verify sample|full|none]
                [--reference REF ...] [look knobs: --strength --brightness --zone ...] [--grain [AMOUNT]]

Film grain is OFF by default; --grain adds the look's grain (only when the user asks for it).

What makes it fast (measured on M4 / darktable 5.6, 24 MP ARW):
  * darktable-cli start-up costs ~4 s, so photos are rendered by a few long-lived
    darktable-cli processes (--import file1 --import file2 ...) instead of one
    process per photo.
  * darktable's grain module costs ~0.7 s of the ~1.1 s pipeline at 2048 px.
    darktable renders everything *except* grain; grain is added in numpy from a
    pre-computed noise tile (~20 ms), calibrated to match darktable's grain.
  * --size N renders with --hq false (pipeline runs at output size): ~3x faster
    than full-res. --size 0 = full resolution (slow path, for prints).
  * Verification is tiered: cheap absolute checks on every output (history,
    black floor, clipping, chroma, shadow colour) + full before/after checks on
    every JPEG input and on a sample of RAWs (darktable default render as the
    baseline). --verify full gives every RAW a baseline (~+20 s / 100 RAWs).
  * Post-processing (grain, JPEG encode, verification, thumbnails) runs in a
    process pool and overlaps with rendering.

Different lighting (auto, default on): every photo is measured first (EXIF scene
brightness + a 0.1 s half-size LibRaw decode, see scene.py) and gets its own
XMP sidecar: exposure offset (night kept dark, low-key kept, underexposed
lifted), a partial WB fix for sodium/tungsten/fluorescent casts in night/dim
scenes, and night colour protection. --no-auto renders every photo the same.

Output quality (default = best): darktable hands over a lossless 16-bit TIFF
(no JPEG-in-the-middle), grain is added in float, and the image is quantised
to 8 bit exactly once, in the final JPEG: quality 100, 4:4:4 (no chroma
subsampling), optimised Huffman tables, darktable's sRGB ICC profile, EXIF and
the darktable history (XMP) kept. The written file is re-opened and checked
(quantisation tables, sampling, ICC, size). --quality N trades size for quality.
Outputs: OUT/<stem>_<look>.jpg, OUT/_report/{summary.md,summary.json,sheet.jpg,verify_sheet.jpg}
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fuji_look as FL  # noqa: E402
import scene as SC  # noqa: E402
import verify as V  # noqa: E402

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fuji-film-look"
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"} | FL.RAW_EXT
TILE = 2048


# ------------------------------------------------------------ darktable ---
def template_config(cli: str) -> Path:
    """A darktable config dir initialised once (presets db etc.), copied per process."""
    d = CACHE / "dtconfig"
    if (d / "data.db").exists():
        return d
    d.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "t.png"
        Image.new("RGB", (16, 16), (128, 128, 128)).save(png)
        subprocess.run([cli, str(png), str(Path(td) / "o.jpg"), "--core", "--configdir", str(d),
                        "--library", ":memory:"], capture_output=True)
    return d


def launch(cli, files, xmp, outdir, size, hq, threads, template, quality, log, fmt="jpg"):
    cfg = tempfile.mkdtemp(prefix="fujicfg-")
    shutil.copytree(template, cfg, dirs_exist_ok=True)
    args = [cli]
    for f in files:
        args += ["--import", str(f)]
    if xmp:
        args.append(str(xmp))
    args += [str(outdir), "--apply-custom-presets", "false", "--hq", "true" if hq else "false"]
    if fmt == "tif16":
        args += ["--out-ext", "tif"]  # a CLI option: must come before --core
    if size:
        args += ["--width", str(size), "--height", str(size)]
    args += ["--core", "--configdir", cfg, "--library", ":memory:",
             "--conf", "plugins/lighttable/export/metadata_flags=21"]  # EXIF + darktable history
    if fmt == "tif16":  # lossless 16-bit intermediate: the only 8-bit rounding is the final JPEG
        args += ["--conf", "plugins/imageio/format/tiff/bpp=16",
                 "--conf", "plugins/imageio/format/tiff/compress=0",
                 "--conf", "plugins/imageio/format/tiff/pixelformat=false",
                 "--conf", "plugins/imageio/format/tiff/shortfile=false"]
    else:
        args += ["--conf", f"plugins/imageio/format/jpeg/quality={quality}",
                 "--conf", "plugins/imageio/format/jpeg/subsample=1"]  # 4:4:4
    env = {**os.environ, "OMP_NUM_THREADS": str(threads)}
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=open(log, "w"), env=env)
    p._cfg = cfg  # cleaned up later
    return p


# ---------------------------------------------------------------- grain ---
_TILE = None


def _noise_tile():
    """Unit-variance luminance noise with darktable-like 1 px grain
    (neighbour autocorrelation ~0.2 at 2048 px, measured)."""
    global _TILE
    if _TILE is None:
        rng = np.random.default_rng(1234)
        n = rng.standard_normal((TILE, TILE), dtype=np.float32)
        a, b = 0.085, 0.83
        n = b * n + a * (np.roll(n, 1, 0) + np.roll(n, -1, 0))
        n = b * n + a * (np.roll(n, 1, 1) + np.roll(n, -1, 1))
        _TILE = (n / n.std()).astype(np.float32)
    return _TILE


def add_grain(arr, amp, floor, seed, scale=1.0):
    h, w = arr.shape[:2]
    tile = _noise_tile()
    rng = np.random.default_rng(seed)
    if scale > 1.25:  # bigger output -> proportionally bigger grain
        sh, sw = math.ceil(h / scale), math.ceil(w / scale)
        reps = (math.ceil(sh / TILE), math.ceil(sw / TILE))
        small = np.tile(tile, reps)[:sh, :sw]
        n = np.asarray(Image.fromarray(small).resize((w, h), Image.BILINEAR), dtype=np.float32) * 1.15
    else:
        if h > TILE or w > TILE:
            n = np.tile(tile, (math.ceil(h / TILE), math.ceil(w / TILE)))[:h, :w]
        else:
            y, x = rng.integers(0, TILE - h + 1), rng.integers(0, TILE - w + 1)
            n = tile[y:y + h, x:x + w]
            if rng.random() < 0.5:
                n = n[::-1]
    Y = 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]
    wgt = np.maximum(floor, 1.0 - ((Y - 145.0) / 125.0) ** 2)
    arr += (amp * wgt * n)[..., None]
    return arr


# ----------------------------------------------------------- post worker ---
def _tiff_pixels(data, im):
    """Uncompressed contiguous RGB TIFF -> uint8/uint16 array straight from the
    buffer (PIL decodes darktable's one-row-per-strip TIFFs ~100x slower and
    drops 16-bit RGB to 8 bit)."""
    t = im.tag_v2
    w, h = im.size
    bps, spp = t[258][0], t.get(277, 1)
    offs, cnts = t[273], t[279]
    if t.get(259, 1) != 1 or spp != 3 or t.get(284, 1) != 1 or bps not in (8, 16) or t.get(339, (1,))[0] != 1:
        raise ValueError("unsupported TIFF layout")
    if any(offs[i] + cnts[i] != offs[i + 1] for i in range(len(offs) - 1)):
        raise ValueError("non-contiguous strips")
    order = "<" if data[:2] == b"II" else ">"
    dt = np.dtype(order + ("u2" if bps == 16 else "u1"))
    if offs[0] + w * h * 3 * dt.itemsize > len(data):
        raise OSError("truncated TIFF")
    return np.frombuffer(data, dt, count=w * h * 3, offset=offs[0]).reshape(h, w, 3)


def _read_render(path, tries=20):
    """-> (bytes, PIL image (metadata only for TIFF), float32 RGB in 0..255).
    darktable writes the file, then rewrites it to add metadata: retry until it
    decodes completely."""
    for i in range(tries):
        try:
            data = open(path, "rb").read()
            im = Image.open(io.BytesIO(data))
            if im.format == "TIFF":
                px = _tiff_pixels(data, im)
                arr = px.astype(np.float32)
                if px.dtype.itemsize == 2:
                    arr *= np.float32(255.0 / 65535.0)
            else:
                im.load()
                arr = np.asarray(im.convert("RGB"), dtype=np.float32)
            return data, im, arr
        except (OSError, SyntaxError, ValueError, KeyError):
            if i == tries - 1:
                raise
            time.sleep(0.15)


NIGHT_SOFT = ("reds darker", "reds less saturated", "blues", "skin/warm", "greens", "sky",
              "colourless shadows", "muted overall", "global chroma muted",  # neon-lit shadows / kept light colours
              "no clipping", "highlight roll-off",  # emissive lights (neon, lamps) may clip
              "reds kept", "reds not darker")  # portrait: red neon is a light source, not a dress
IFD0_KEEP = (0x010E, 0x010F, 0x0110, 0x0112, 0x011A, 0x011B, 0x0128, 0x0131, 0x0132, 0x013B, 0x8298)


def jpeg_metadata(im):
    """Metadata to carry from darktable's render into the final JPEG:
    ICC profile, a clean EXIF (no TIFF strip/layout tags), XMP history, dpi."""
    kw = {}
    icc = im.info.get("icc_profile")
    if icc:
        kw["icc_profile"] = icc
    try:
        src = im.getexif()
        for drop_makernote in (False, True):
            ex = Image.Exif()
            for t in IFD0_KEEP:
                v = src.get(t)
                if v not in (None, "", b""):
                    ex[t] = v
            for ifd in (0x8769, 0x8825):  # Exif, GPS
                d = {k: v for k, v in src.get_ifd(ifd).items()
                     if v not in (None, "", b"") and not (drop_makernote and k == 0x927C)}
                if d:
                    ex[ifd] = d
            b = ex.tobytes()
            if len(b) < 65000:
                kw["exif"] = b
                break
    except Exception:
        pass
    xmp = im.info.get("xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode()
    if xmp and len(xmp) < 65000:
        kw["xmp"] = xmp
    dpi = im.info.get("dpi")
    if dpi:
        kw["dpi"] = tuple(round(float(x)) for x in dpi)
    return kw


def jpeg_checks(path, quality, size, want_icc):
    """Re-open the written JPEG and check it really is what was asked for."""
    from PIL import JpegImagePlugin
    im = Image.open(path)
    scale = 5000 / quality if quality < 50 else 200 - 2 * quality
    expect = max(1, min(255, int((121 * scale + 50) // 100)))  # largest IJG table entry at this quality
    qmax = max(max(t) for t in im.quantization.values())
    samp = JpegImagePlugin.get_sampling(im)
    c = [dict(name=f"JPEG quality {quality} (max quant step <= {expect})", ok=qmax <= expect, hard=True,
              detail=f"max quant step {qmax}"),
         dict(name="no chroma subsampling (4:4:4)", ok=samp == 0, hard=True,
              detail={0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}.get(samp, str(samp))),
         dict(name="full render size kept", ok=im.size == size, hard=True, detail=f"{im.size[0]}x{im.size[1]}"),
         dict(name="EXIF kept", ok=bool(im.info.get("exif")), hard=False, detail="")]
    if want_icc:
        c.append(dict(name="ICC profile embedded", ok=bool(im.info.get("icc_profile")), hard=True, detail=""))
    return c, dict(jpeg_bytes=os.path.getsize(path), jpeg_qmax=qmax)


def post(job):
    t0 = time.time()
    try:
        data, im, arr = _read_render(job["render"])
        if job["amp"] > 0:
            seed = int(hashlib.md5(job["name"].encode()).hexdigest()[:8], 16)
            arr = add_grain(arr, job["amp"], job["floor"], seed, max(arr.shape[:2]) / 2048.0)
        out = Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8))
        meta = jpeg_metadata(im)
        try:  # optimised Huffman needs the whole scan in one buffer: noisy q100 files reach ~3 bytes/px
            out.save(job["final"], quality=job["quality"], subsampling=0, optimize=True,
                     bufsize=out.size[0] * out.size[1] * 4 + (4 << 20), **meta)
        except OSError:  # never lose a file over a smaller-file optimisation (pixels are identical)
            out.save(job["final"], quality=job["quality"], subsampling=0, **meta)

        checks, metrics, base = [], {}, None
        if job["verify"] != "none":
            c, m = jpeg_checks(job["final"], job["quality"], out.size, "icc_profile" in meta)
            checks += c; metrics.update(m)
            c, m = V.output_checks(out, job["look"], data, job["refs"])
            checks += c; metrics.update(m)
            if job["baseline"] == "self":  # JPEG/PNG/TIFF input: the file (+ auto corrections) is the "before"
                b = Image.open(job["src"])
                b.draft("RGB", (1000, 1000))
                base = SC.apply_plan_rgb(ImageOps.exif_transpose(b).convert("RGB"), job["plan"])
            elif job["baseline"] and Path(job["baseline"]).exists():
                base = V.load(job["baseline"])
            if base is not None:
                c, m = V.analyze(base, out, job["look"], None, None, work=800, require_same_size=False)
                checks += c; metrics.update(m)
                for ch in checks:  # sun, speculars, vivid flowers clip in the source: "no new clipping" decides
                    if ch["name"].startswith("no clipping"):
                        ch["hard"] = False
                bt = base.copy(); bt.thumbnail((360, 360)); bt.save(job["thumb_base"], quality=85)
            if (job["plan"] or {}).get("blackbg"):  # the source itself is >=95% black (moon on black sky)
                for ch in checks:
                    if ch["name"] == "not blank":
                        ch["hard"] = False
            if (job["plan"] or {}).get("cls") == "night":
                # night colours are mostly emissive (neon, tail lights), protected on purpose:
                # the film colour-band rules become warnings there
                for ch in checks:
                    if ch["name"].startswith(NIGHT_SOFT):
                        ch["hard"] = False
            c, m = V.lighting_checks(out, job["plan"], base)
            checks += c; metrics.update(m)
        th = out.copy(); th.thumbnail((360, 360)); th.save(job["thumb"], quality=85)
        if not job["keep"]:
            os.unlink(job["render"])  # 16-bit TIFFs are big (17 MB at 2048 px, ~200 MB full-res)
        return dict(name=job["name"], src=job["src"], final=job["final"], passed=V.passed(checks), plan=job["plan"],
                    full_verify=any(c["name"].startswith("faded blacks") for c in checks),
                    checks=checks, metrics=metrics, post_s=round(time.time() - t0, 3),
                    thumb=job["thumb"], thumb_base=job["thumb_base"] if base is not None else None)
    except Exception as e:  # keep the batch going
        return dict(name=job["name"], src=job["src"], error=repr(e), passed=False, checks=[], metrics={})


# ----------------------------------------------------------------- main ---
def collect(inputs):
    files = []
    for p in map(lambda x: Path(x).expanduser(), inputs):
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in IMG_EXT and not f.name.startswith("."))
        elif p.suffix.lower() in IMG_EXT and p.exists():
            files.append(p)
    return [f.resolve() for f in files]


def write_sheets(results, rep, cols=6):
    ok = [r for r in results if r.get("thumb")]
    if ok:
        cw, ch = 370, 280
        rows = math.ceil(len(ok) / cols)
        sheet = Image.new("RGB", (cols * cw + 10, rows * (ch + 24) + 10), (24, 24, 24))
        d = ImageDraw.Draw(sheet)
        for i, r in enumerate(ok):
            x, y = 10 + (i % cols) * cw, 10 + (i // cols) * (ch + 24)
            t = Image.open(r["thumb"]); t.thumbnail((cw - 10, ch))  # portrait thumbs are 360 tall: fit the cell
            sheet.paste(t, (x + (cw - 10 - t.size[0]) // 2, y + 18))
            d.text((x, y + 2), f"{r['name'][:38]} [{'PASS' if r['passed'] else 'FAIL'}]",
                   fill=(120, 220, 120) if r["passed"] else (240, 110, 110))
        sheet.save(rep / "sheet.jpg", quality=85)
    pairs = [r for r in ok if r.get("thumb_base")]
    if pairs:
        cols2, cw, ch = 3, 740, 280
        rows = math.ceil(len(pairs) / cols2)
        sheet = Image.new("RGB", (cols2 * cw + 10, rows * (ch + 24) + 10), (24, 24, 24))
        d = ImageDraw.Draw(sheet)
        for i, r in enumerate(pairs):
            x, y = 10 + (i % cols2) * cw, 10 + (i // cols2) * (ch + 24)
            b, e = Image.open(r["thumb_base"]), Image.open(r["thumb"])
            half = (cw - 16) // 2
            b.thumbnail((half, ch)); e.thumbnail((half, ch))
            sheet.paste(b, (x, y + 18)); sheet.paste(e, (x + b.size[0] + 6, y + 18))
            d.text((x, y + 2), f"{r['name'][:40]} [{'PASS' if r['passed'] else 'FAIL'}]  before -> after",
                   fill=(120, 220, 120) if r["passed"] else (240, 110, 110))
        sheet.save(rep / "verify_sheet.jpg", quality=85)


def write_summary(results, rep, a, timing):
    n = len(results)
    npass = sum(r["passed"] for r in results)
    nfull = sum(r.get("full_verify", False) for r in results)
    lines = [f"# Fuji look run: look={a.look} files={n} pass={npass} fail={n - npass}  "
             f"(full before/after verify on {nfull}, absolute checks on all)",
             "", "Timing: " + ", ".join(f"{k} {v:.1f}s" for k, v in timing.items()), "",
             f"Output: JPEG quality {a.quality}, 4:4:4, optimised Huffman, ICC/EXIF/XMP kept; "
             f"long edge {a.size or 'full'} px{' (hq)' if a.hq else ''}; grain {f'x{a.grain:g}' if a.grain > 0 else 'off'}; "
             f"avg {sum(r['metrics'].get('jpeg_bytes', 0) for r in results) / max(1, sum('jpeg_bytes' in r['metrics'] for r in results)) / 1e6:.1f} MB/file", "",
             "| file | result | verify | scene | auto | L p1 | mean C | chroma ratio | mid-std ratio | failed (hard) | warnings |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    fails, warns = {}, {}
    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | ERROR | | | | | | | | {r['error'][:90]} | |"); continue
        m = r["metrics"]
        f = [c["name"] for c in r["checks"] if not c["ok"] and c["hard"]]
        w = [c["name"] for c in r["checks"] if not c["ok"] and not c["hard"]]
        for x in f: fails[x] = fails.get(x, 0) + 1
        for x in w: warns[x] = warns.get(x, 0) + 1
        cr = f"{m['chroma_mean'][1] / max(m['chroma_mean'][0], 1e-6):.2f}" if "chroma_mean" in m else ""
        sr = f"{m['L_mid_std'][1] / max(m['L_mid_std'][0], 1e-6):.2f}" if "L_mid_std" in m else ""
        pl = r.get("plan") or {}
        auto = ", ".join(x for x in (f"{pl['ev']:+.1f} EV" if pl.get("ev") else "",
                                     f"WB {pl['wb']['kind']} -{pl['wb']['frac']:.0%}" if pl.get("wb") else "",
                                     "low-key" if pl.get("lowkey") else "", "high-key" if pl.get("highkey") else "",
                                     "spotlight" if pl.get("spot", 0) >= 0.5 else "", "lift capped" if pl.get("capped") else "",
                                     f"signs {pl['signs']:.0%}" if pl.get("signs") else "") if x)
        lines.append(f"| {r['name']} | {'PASS' if r['passed'] else 'FAIL'} | {'full' if r.get('full_verify') else 'abs'} | "
                     f"{pl.get('cls', '')} | {auto} | "
                     f"{m.get('out_L_p1', 0):.1f} | {m.get('out_mean_chroma', 0):.1f} | {cr} | {sr} | {', '.join(f)} | {', '.join(w)} |")
    lines += ["", "## Auto corrections (why)"] + [
        f"- {r['name']}: " + "; ".join((r.get("plan") or {}).get("reasons") or ["none"])
        for r in results if (r.get("plan") or {}).get("reasons")]
    lines += ["", "## Failure counts (hard)"] + [f"- {k}: {v}" for k, v in sorted(fails.items(), key=lambda x: -x[1])]
    lines += ["", "## Warning counts"] + [f"- {k}: {v}" for k, v in sorted(warns.items(), key=lambda x: -x[1])]
    (rep / "summary.md").write_text("\n".join(lines) + "\n")
    run = dict(look=a.look, grain=a.grain, size=a.size, quality=a.quality, verify=a.verify, auto=a.auto)  # for gallery.py
    (rep / "summary.json").write_text(json.dumps(dict(run=run, timing=timing, results=results), indent=1, default=str))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="files and/or folders")
    ap.add_argument("-o", "--outdir", required=True)
    FL.add_look_args(ap)
    ap.add_argument("--size", type=int, default=2048, help="long edge in px (0 = full resolution, slow)")
    ap.add_argument("--hq", action="store_true", help="full-res pipeline before downscaling (slower, marginally sharper)")
    ap.add_argument("--quality", type=int, default=100,
                    help="final JPEG quality (default 100 = best; always 4:4:4, ICC/EXIF/XMP kept)")
    ap.add_argument("--verify", choices=["sample", "full", "none"], default="sample")
    ap.add_argument("--sample", type=float, default=0.10, help="fraction of RAWs with a full before/after check")
    ap.add_argument("--reference", nargs="+", help="style reference images for ref-match checks")
    ap.add_argument("--procs", type=int, default=3, help="parallel darktable-cli renderers")
    ap.add_argument("--workers", type=int, default=4, help="post-processing workers")
    ap.add_argument("--no-auto", dest="auto", action="store_false",
                    help="no per-photo scene analysis (same recipe for every photo)")
    ap.add_argument("--exposure", type=float, default=0.0, help="extra exposure in EV, added to the auto value")
    ap.add_argument("--keep-work", action="store_true")
    a = ap.parse_args()

    T0 = time.time()
    timing = {}
    files = collect(a.inputs)
    if not files:
        sys.exit("no input images found")
    out = Path(a.outdir).expanduser().resolve()
    work, rep = out / ".work", out / "_report"
    for d in (out, rep):
        d.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    cli = FL.find_cli()
    template = template_config(cli)

    # unique stems (darktable names outputs by file stem)
    seen, items = {}, []
    for f in files:
        stem = f.stem
        k = seen.get(stem, 0); seen[stem] = k + 1
        if k:
            stem = f"{stem}__{k}"
        items.append(dict(name=stem, src=str(f), raw=f.suffix.lower() in FL.RAW_EXT))

    # per-photo scene analysis -> plan (all cores: darktable has not started yet)
    if a.auto:
        os.environ.setdefault("OMP_NUM_THREADS", "1")  # LibRaw/OpenMP: one thread per worker, no oversubscription
        with ProcessPoolExecutor(max(2, os.cpu_count() or 4)) as ex:
            ans = list(ex.map(SC.analyze, [it["src"] for it in items], chunksize=2))
    else:
        ans = [dict(path=it["src"], kind="none") for it in items]
    for it, an in zip(items, ans):
        pl = SC.plan(an, enable=a.auto, amber_min_c=FL.LOOKS[a.look].get("wb_amber_min_c", 25.0))
        if a.exposure:
            pl["ev"] = round(pl["ev"] + a.exposure, 2)
            pl["reasons"].append(f"user exposure {a.exposure:+.2f} EV")
        it["plan"], it["ev100"] = pl, an.get("ev100")
    timing["analyze"] = time.time() - T0

    # every photo gets a symlink + its own XMP sidecar (<link>.xmp), which
    # darktable-cli applies on --import: one process renders many photos, each
    # with its own corrections. No darktable grain: added in numpy.
    (work / "links").mkdir()
    for it in items:
        link = work / "links" / (it["name"] + Path(it["src"]).suffix)
        os.symlink(it["src"], link)
        Path(str(link) + ".xmp").write_text(FL.make_xmp(a, it["raw"], darktable_grain=False, plan=it["plan"]))
        it["feed"] = str(link)
    amp, floor = FL.grain_spec(a)

    # baselines for full verification (RAW only; other inputs are their own baseline)
    raws = [it for it in items if it["raw"]]
    if a.verify == "full":
        sample = raws
    elif a.verify == "sample" and raws:
        k = min(len(raws), max(3, min(15, round(len(raws) * a.sample))))
        step = len(raws) / k
        sample = [raws[int(i * step)] for i in range(k)]
    else:
        sample = []
    sample_names = {it["name"] for it in sample}

    # render chunks: split procs between RAW and non-RAW groups
    groups = [(True, [it for it in items if it["raw"]]), (False, [it for it in items if not it["raw"]])]
    groups = [g for g in groups if g[1]]
    nprocs = max(1, min(a.procs, len(items)))
    procs, chunks = [], []
    threads = max(2, (os.cpu_count() or 8) // nprocs + 1)
    for gi, (is_raw, its) in enumerate(groups):
        share = max(1, round(nprocs * len(its) / len(items))) if len(groups) > 1 else nprocs
        share = min(share, len(its))
        for ci in range(share):
            sub = its[ci::share]
            d = work / f"r{gi}_{ci}"; d.mkdir()
            p = launch(cli, [it["feed"] for it in sub], None, d, a.size, a.hq, threads, template,
                       100, d / "darktable.log", fmt="tif16")
            procs.append(p); chunks.append(dict(proc=p, items=sub, dir=d, next=0))
    bproc = None
    if sample:
        bd = work / "baseline"; bd.mkdir()
        (work / "blinks").mkdir()
        for it in sample:  # "before" = darktable default + the same auto corrections, no look
            bl = work / "blinks" / Path(it["feed"]).name
            os.symlink(it["src"], bl)
            cx = FL.make_correction_xmp(it["plan"], True)
            if cx:
                Path(str(bl) + ".xmp").write_text(cx)
            it["bfeed"] = str(bl)
        nb = 1 if len(sample) <= 15 else 2
        bprocs = []
        for bi in range(nb):
            bprocs.append(launch(cli, [it["bfeed"] for it in sample[bi::nb]], None, bd, 800, False, 2,
                                 template, 90, work / f"baseline{bi}.log"))
        bproc = bprocs
    timing["launch"] = time.time() - T0

    refs = V.reference_stats(a.reference) if a.reference else None

    def job_for(it, baseline):
        return dict(name=it["name"], src=it["src"], render=str(it["_render"]),
                    final=str(out / f"{it['name']}_{a.look}.jpg"), look=a.look, amp=amp, floor=floor,
                    quality=a.quality, verify=a.verify, refs=refs, baseline=baseline, keep=a.keep_work,
                    plan=it["plan"],
                    thumb=str(work / f"{it['name']}_t.jpg"), thumb_base=str(work / f"{it['name']}_b.jpg"))

    sizes = {}

    def complete(path):
        """size unchanged for >=0.3 s and structurally complete
        (JPEG: ends with EOI; TIFF: all strips inside the file)"""
        try:
            st = path.stat().st_size
        except FileNotFoundError:
            return False
        now = time.time()
        prev = sizes.get(path)
        if prev is None or prev[0] != st:
            sizes[path] = (st, now)
            return False
        if now - prev[1] < 0.3:
            return False
        if path.suffix == ".tif":
            try:
                with Image.open(path) as im:
                    t = im.tag_v2
                    return max(o + c for o, c in zip(t[273], t[279])) <= st
            except Exception:
                return False
        with open(path, "rb") as fh:
            fh.seek(-2, 2)
            return fh.read(2) == b"\xff\xd9"

    results, futures, deferred = [], [], []
    with ProcessPoolExecutor(a.workers) as ex:
        pending = True
        while pending:
            pending = False
            for c in chunks:
                done_proc = c["proc"].poll() is not None
                while c["next"] < len(c["items"]):
                    it = c["items"][c["next"]]
                    path = c["dir"] / (Path(it["feed"]).stem + ".tif")
                    nxt_exists = c["next"] + 1 < len(c["items"]) and \
                        (c["dir"] / (Path(c["items"][c["next"] + 1]["feed"]).stem + ".tif")).exists()
                    if path.exists() and (nxt_exists or done_proc) and complete(path):
                        it["_render"] = path
                        if it["name"] in sample_names:
                            deferred.append(it)  # wait for its baseline
                        else:
                            futures.append(ex.submit(post, job_for(it, None if it["raw"] else "self")))
                        c["next"] += 1
                    elif done_proc and not path.exists():  # render failed for this file
                        results.append(dict(name=it["name"], src=it["src"], error="darktable render failed (see .work log)",
                                            passed=False, checks=[], metrics={}))
                        c["next"] += 1
                    else:
                        break
                if c["next"] < len(c["items"]) or not done_proc:
                    pending = True
            if pending:
                time.sleep(0.05)
        timing["render"] = time.time() - T0
        if bproc:
            for p in bproc:
                p.wait()
        timing["baselines"] = time.time() - T0
        for it in deferred:
            futures.append(ex.submit(post, job_for(it, str(work / "baseline" / (Path(it["feed"]).stem + ".jpg")))))
        results += [f.result() for f in futures]
    timing["post"] = time.time() - T0

    order = {it["name"]: i for i, it in enumerate(items)}
    results.sort(key=lambda r: order.get(r["name"], 0))
    write_sheets(results, rep)
    timing["total"] = time.time() - T0
    lines = write_summary(results, rep, a, timing)
    for p in procs + (bproc or []):
        shutil.rmtree(p._cfg, ignore_errors=True)
    if not a.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print("\n".join(lines[:3]))
    bad = [l for l in lines if "| FAIL |" in l or "| ERROR |" in l]
    if bad:
        print("\n".join(bad))
    print(f"\noutputs: {out}\nreport:  {rep / 'summary.md'}\nsheets:  {rep / 'sheet.jpg'}  {rep / 'verify_sheet.jpg'}")
    sys.exit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()

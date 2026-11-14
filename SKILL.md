---
name: fuji-film-look
description: Edit a photo (JPEG or camera RAW such as Sony ARW) into a Fujifilm film-simulation look (Classic Chrome, Classic Neg, a moody Japanese "street" Fuji look, or a warm "portrait" look with rosy skin for people and dolls) using darktable-cli, then verify the result numerically, against optional style reference images, and visually. Adapts each photo to its lighting automatically (night, tungsten/sodium/neon, backlight, haze, under/over-exposure). Also batch-tests a look over many photos and builds a before/after gallery web page of the results. Use when the user gives a photo and asks for a Fuji look, film look, film simulation, 富士風, 富士底片風, 底片風, 膠片風, 日系街拍, 人像/娃娃/BJD 調色, 膚色紅潤, Classic Chrome, Classic Neg, or to make a photo look like film or like a reference photo; also to show edited photos before/after (前後對比, gallery).
---

# Fuji film look (darktable-cli)

Takes a photo (JPEG/PNG/TIFF or RAW: ARW/NEF/CR3/RAF/DNG…) and renders a Fuji-style version with
darktable-cli. Then it checks the result against the look's signature and builds a
side-by-side comparison for you to look at.

## Requirements (darktable is a hard dependency)

- **darktable 5.6.x** (tested on 5.6.2). Every photo is rendered by `darktable-cli`, and the module params are packed for darktable 5.6 module versions (`references/recipe.md`). `find_cli()` runs `darktable-cli --version`: older than 5.6 → exit with install instructions; newer → warning (the verify step catches a mismatch). It looks for `$DARKTABLE_CLI`, then `/Applications/darktable.app/Contents/MacOS/darktable-cli`, `~/Applications/darktable.app/…`, then `darktable-cli` on PATH.
- **Install it with `./install.sh`** (repo root). On macOS (Apple silicon, macOS 14+) it downloads the official darktable 5.6.2 DMG from github.com/darktable-org, checks its SHA-256, and copies darktable.app to /Applications (else ~/Applications). It never replaces an existing darktable.app without `--force`, and with `--force` it moves the old one aside. It also installs uv (Homebrew) and links the skill into `~/.claude/skills` (and `~/.pi/agent/skills`). `./install.sh --check` only reports. Do **not** use `brew install --cask darktable`: Homebrew disabled that cask on 2026-09-01 because it fails the Gatekeeper check. Linux: the script prints Flatpak/AppImage instructions (untested).
- `uv` to run `run.py`, `verify.py` and `batch.py` (they pull numpy and pillow; run.py also pulls `rawpy` (LibRaw) for the per-photo scene analysis). The first run also caches a darktable config in `~/.cache/fuji-film-look/` (~4 s, once). `fuji_look.py` needs only the standard library.

Set `S=~/.claude/skills/fuji-film-look/scripts` (pi sees the same folder at `~/.pi/agent/skills/fuji-film-look/scripts`).

## Default workflow: `run.py` (1 photo or 1000)

One command analyses each photo's lighting, renders, verifies, and writes a report (film grain only on request, see Options). Measured 2026-11-08 on an M4 (10 cores, 16 GB): **200 distinct Sony RAWs (42 camera models) in 108 s** = 0.54 s/photo, ~54 s per 100 (auto lighting + best-quality JPEG included; `--no-auto` saves ~5 s per 100); `--verify full` on the same 200: 177 s. A single photo takes ~5 s (darktable start-up).
```bash
uv run $S/run.py INPUTS... -o OUT_DIR --look street [--size 2048] [--verify sample|full|none] \
     [--reference REF1 REF2 ...] [look knobs: --strength --brightness --shadows --highlights --zone ... --vignette --local-contrast] [--grain [AMOUNT]]
```
1. **Pick the look**
   - `classic-chrome` (default): muted and documentary. Reds are darker and less saturated, greens go olive, blues are deep but restrained, blacks fade, shadows go slightly cool.
   - `classic-neg`: punchier, with teal shadows, warm highlights, deep reds, greens pushed toward teal, and saturated deep skies (Superia-like).
   - `portrait`: **people and dolls (BJD)**. Pick it whenever the photo is about a person or a doll. Tested on 200 distinct Sony RAWs (pixls.us Play Raw, 42 models: landscapes, night, people, pets, flowers) — 200/200 PASS with `--verify full` — plus the 63-photo set (63/63). It keeps reds saturated, so bright red/pink flowers in sun can leave the sRGB gamut and clip a channel: highlight chroma −0.10 / saturation −0.06 plus the shared highlight shoulder (`HI_CHROMA`) keep that at or below the source while whites stay at L ≈ 97. Soft matte blacks, gentle contrast, warm pink-cream whites, rosy skin, crimson reds kept saturated (dresses, lips, roses), muted olive/teal greens and blues, light vignette. Matched to the user's doll-photography references (2026-11-08). On night/dim interiors it removes a yellow/amber light cast from C > 20 (other looks: C > 25), because yellow skin and yellow whites hurt people more than a lost ambience.
   - `street`: moody Japanese street / Fuji-street look. **Not for people or dolls**: its colourless shadows turn dark red clothes brown and its cool highlights grey the skin. It has matte blacks (L≈10–12), strong mid contrast with crunchy local contrast, very low chroma, colourless shadows, cool-neutral whites, steel-blue skies, warm practical lights kept, and a heavy vignette (its heavy grain only with `--grain`). Pick it when the user shows dark, desaturated, grainy Japanese station or street references.
2. **Run** `run.py` on the files or folders. Outputs go to `OUT_DIR/<stem>_<look>.jpg`; the report goes to `OUT_DIR/_report/`:
   - `summary.md`: PASS/FAIL per file, scene type and auto corrections, key metrics, failed checks, **failure counts per check**, an "Auto corrections (why)" list, and timing.
   - `sheet.jpg`: thumbnails of every output, tagged PASS/FAIL.
   - `verify_sheet.jpg`: before/after pairs for the fully verified files.
   - `summary.json`: the same, machine-readable (run settings + per-file plan, checks, metrics); `gallery.py` reads it.
3. **Read** `summary.md`, then **look at** `verify_sheet.jpg` and `sheet.jpg` with Read. Numbers can pass on an ugly image. Check skin, sky, murkiness, greyness, and whether the vignette (or grain, if on) is overwhelming.
4. **Iterate** on a failing or ugly file with the tuning guide (re-run just that file with knobs), then report the output folder, the sheets, and a one- or two-line PASS/WARN summary.
5. **Before/after page** when the user wants to see before/after, a gallery or a web page: `gallery.py` (see "Before/after gallery" below), then publish it. If the user complains about scrolling and wants an iPhone-Photos feel, run `album.py` on the gallery folder (see the same section).

Options:
- `--size 2048` (default) is the long edge, rendered with `--hq false`: the pipeline runs at output size, so it's fast and fine for screen and sharing. Use `--size 0` for full resolution (prints): ~2.7 s per photo (100 photos ≈ 4.5 min), ~22 MB per JPEG at q100. `--hq` runs the full-res pipeline before downscaling (slower, marginally sharper).
- `--verify sample` (default): absolute checks on **every** output, plus full before/after checks on every JPEG input and on 10% of RAWs (min 3, max 15; darktable default render as the baseline). `--verify full` gives every RAW a baseline (+~20 s per 100 RAWs). Use it when tuning a look. `--verify none` skips verification.
- **Film grain is OFF by default** (the user's choice, 2026-11-08): add it only when the user asks for grain, noise, 顆粒 or 噪點. `--grain` adds the look's own grain (street heavy, chrome/neg fine), `--grain 0.5` half, `--grain 1.5` more. Put `--grain` after the input files, or give it a number, so it doesn't swallow a file name.
- `--quality 100` (default) gives the **best possible JPEG**, see "Output quality" below. Only lower it (e.g. `--quality 95`, about 2.5x smaller files) when the user asks for small files.
- Auto lighting is on by default (next section). `--no-auto` renders every photo with the same recipe; `--exposure 0.3` adds EV on top of the auto value (use per photo).
- Exit code 0 means every file passed the hard checks.

### Different lighting (auto, default on). Keep this when changing code
A fixed recipe cannot suit a sunny street, a tungsten bar, a sodium-lit night street, a neon sign and a backlit portrait. `scene.py` measures every photo **before** rendering (~2–4 s per 100 RAWs, all cores) and writes **its own XMP sidecar** (`.work/links/<name>.<ext>.xmp` next to a symlink; darktable-cli applies a sidecar per `--import`, so one process still renders many photos). `python3 $S/scene.py FILES...` prints the analysis and plan without rendering.

What it measures:
- **Scene brightness EV100** from EXIF shutter/aperture/ISO (or the metered BrightnessValue, e.g. manual lenses): night < 6 ≤ dim < 9.5 ≤ day. Unknown EXIF → from the image (dark frame with light sources = night). Sanity ranges reject garbage EXIF (one CR3 reported f/41486).
- **Predicted darktable render**: a half-size LibRaw decode (rawpy, camera WB, ~0.1 s) gives scene-linear luminance; `TONE_X/TONE_Y` map it (after darktable's default +0.7 EV and exposure-bias compensation) to the L* of darktable's default render: ±1 L* at the median, ±1.6 at p95 (fitted on 54 RAWs, 30 camera models). So any exposure change is predicted **without rendering**. JPEG/PNG/TIFF inputs are measured directly (exact).
- **White point** by grey world, shades-of-grey (p=4) and iterated near-neutral pixels.

What it changes (each one listed with the reason in `summary.md`):

| Situation (rule) | Correction |
|---|---|
| Night (EV100 < 6) with dark lit areas (p95 < 55) | exposure up until p95 ≈ 55 (and p75 ≥ 12), max +2 EV, median kept ≤ 30: night stays night |
| Night/indoor rendered bright (median ≤ 62) | **left alone**: EV100 < 6 also covers lamp-lit rooms (a home is EV 3–6), and phone night mode is a choice. Only median > 62 is darkened (to ≈56, max −1 EV) |
| Dim interior, underexposed (p95 < 60 or median < 22) | exposure up, max +1.5 EV, median ≤ 45 |
| Dim interior, **spotlit subject in a dark surround** (≥30–45% of the frame near-black and lit subject L p95 ≥ 30–40, ramped: `scene.spot_weight()`) | exposure set by the **subject**: p95 → 60, max +0.8 EV, p99 ≤ 88; the median is not a target, so the surround stays dark and the light falloff survives. Blended with the rule above by the weight; tagged `spotlight` in the summary |
| Day, underexposed (median < 30, p95 < 70) | exposure up to median ≈ 32, max +1.5, p90 kept ≤ 78 |
| Backlit day (median < 28, p90 ≥ 65) | `shadhi` local shadow lift 20–45 (highlights −25), exposure capped at +0.25 EV: foreground opens, sky untouched |
| Low-key frame (≥40% near-black, bright highlights well exposed) | left alone (silhouettes, dramatic portraits) |
| Black frame (L p95 < 2: moon, stars, fireworks, a spotlit object on black) | left alone (lifting only shows noise); `not blank` becomes a warning |
| High-key day frame with real blacks (median > 62, L p2 ≤ 20: surf, snow, fog, white walls) | left alone (only washed-out frames without blacks are darkened) |
| Over-exposed (median > 62, no blacks) | exposure down to ≈57, max −1 EV |
| Haze / flat light (day/dim, L p2 > 26) | exposure black level so p2 → ≈22 (gentle; big offsets push hazy blue shadows off-hue) |
| Amber cast in night/dim (sodium, tungsten): all 3 estimators agree, hue 15–95°, every estimator C > 25 (`portrait`: C > 20) | 2nd colour-calibration instance (CAT16, custom illuminant): 55–85% of the cast removed, some warmth kept |
| Green cast in night/dim (fluorescent), hue 110–200° | 70–90% removed |
| Purple night sky (AWB over-correcting sodium/LED), hue 270–330°, C 15–70 | 50% removed; strong neon (C > 70) left alone |
| Any other cast, or any daylight scene | **no WB change**: colour constancy is unreliable on real content (blue skies read as "blue cast", sunsets as "warm cast"), and golden hour / blue hour are the mood |
| Night | half of the look's global/highlight desaturation kept off so neon and lamps keep colour; shadows stay as colourless as the look wants |
| **Lit signs / light boxes blown** (night/dim RAW; `scene.sign_weight()`: ≥0.05–0.15% of the RAW clipped **and** the clipped pixels ≥10–20% of those brighter than ~L 78, both ramped) | `toneequal` (tone equalizer, guided filter, mask shifted −2 EV): areas brighter than white come down up to −2.8 EV (−0.5 at L≈90, −0.1 at L≈78, nothing below), so the lettering the RAW keeps under the clip point reads again and orange signs stay orange; tagged `signs N%`. Dim frames (a lit street at EV 6–9.5) with ≥0.15–0.35% clipped also keep up to half of the look's desaturation off, like night. A spotlit doll in a white dress is a big bright area with almost nothing clipped (ratio 0.02–0.07): untouched |

When a lift is **capped** (max EV, highlight guard or night-median guard: the scene needs more than the rules allow), the median/p95 lighting checks become warnings: the dark result is the scene's own contrast (dark forest with bright snow, a village at night). The summary marks such files `lift capped`. With a baseline, the absolute `no clipping` check is a warning and the relative `no new clipping` check decides: the sun, speculars and vivid flowers clip in the source itself. Without a baseline (`--verify sample`), a source that clips >0.3% fails the absolute check: re-run that file with `--verify full` to see whether the look added any clipping.

Verification follows the plan: the "before" image gets the **same corrections** (darktable default + exposure/WB/shadhi/sign recovery, no look; for JPEG inputs the same maths in numpy), so the relative checks still measure the look only. On top, `lighting_checks()` runs on every file: median L fits the scene type (day 22–70, dim 16–66, night 5–66; backlit ≥15; spotlight ≥5; low-key exempt), lit subject readable (L p95 ≥ 40), artificial-light cast tamed (output cast C ≤ input × (1 − frac/2)), and night light colours kept (C p99 ratio ≥ 0.55, warn).

Tested 2026-11-08 on 58 photos (34 pixls.us Play Raw files picked for lighting: night neon, sodium streets, tungsten bar, blue hour, snow at night, backlit sun, golden-hour portraits, fog, haze, harsh midday, under/over-exposed; plus the 20 Sony RAWs and 4 JPEGs): **58/58 PASS for all three looks** with `--verify full`. Before auto: street 16/24 on the first 24, with near-black night shots, orange sodium snow, grey-washed neon.

Spotlight rule (2026-11-11): a doll shoot under a spotlight (67 Sony A7CR RAWs, dark set, lit doll) got +1.4–1.5 EV from the median target; the surround went from L 10 to 29 and the doll to L 82, so the light pool was flattened. With the rule they get +0.3–1.0 EV (render: doll L p95 ≈ 60–67, surround L ≈ 15–20, close to the camera JPEG). Regression: of the 200 Sony RAWs, 58 lighting files and 63 user files, only 2 dim frames move (by −0.1 EV); daylight is excluded on purpose (dark forest next to snow is scenery, not a light pool).

### Output quality (default = best). Keep this when changing code
- darktable hands run.py a **lossless 16-bit TIFF** (`--out-ext tif`, `tiff/bpp=16`, uncompressed), not a JPEG. Grain is added in float, and the image is rounded to 8 bit **once**, in the final JPEG.
- Final JPEG: **quality 100, 4:4:4** (no chroma subsampling), optimised Huffman tables. darktable's sRGB ICC profile, a clean EXIF (camera, exposure, dates; no TIFF layout tags), the darktable history XMP, and 300 dpi are copied over.
- Every output is **re-opened and checked**: max quantisation step for the requested quality (q100 means all steps are 1), sampling 4:4:4, ICC present, size equal to the render, EXIF present (soft). These are hard checks in `summary.md`.
- Measured on 6 files against a never-quantised ground truth (16-bit render plus float grain): PSNR **~50 dB** versus 33–39 dB for the old path (darktable JPEG q96 4:2:0, then PIL q92 4:2:0). Mean ΔE76 0.6 versus 1.3–3.7; worst 0.1% of pixels ΔE 2.8 versus 8–18. Cost: ~3–5 MB per 2048 px file (was ~1 MB), ~25–45 MB per full-res 24–33 MP file.
- The 16-bit TIFFs are 17 MB at 2048 px and ~200 MB at full res. run.py deletes each one right after its JPEG is written (`--keep-work` keeps them).
- For the largest *and* best files use `--size 0` (full resolution). It is slower; see the benchmark in references/recipe.md.

### Why it is fast (measured on M4, darktable 5.6, 24 MP ARW). Keep these when changing code
| Cost | Fix in run.py |
|---|---|
| darktable-cli start-up ~4 s per process | a few long-lived processes (`--procs 3`), each fed many files with `--import f1 --import f2 …` |
| darktable `grain` module = 0.7 s of the 1.1 s pipeline | (only when `--grain` is on) darktable renders without grain; numpy adds grain from a pre-computed noise tile (~20 ms), calibrated to darktable's grain (std ≈0.2×strength at mid-grey, ~1 px grain at 2048 px, tone-weighted) |
| full-res pipeline ~3 s/photo | `--hq false` at `--size 2048`: ~0.4 s/photo |
| a baseline render for every RAW would add +50% | tiered verification: absolute checks on all, before/after on a sample |
| post work (grain, JPEG encode, verify, thumbnails) | `ProcessPoolExecutor` overlapping with rendering; a file is picked up once it is complete (stable size + every TIFF strip inside the file; darktable rewrites the file to add metadata) |
| reading darktable's TIFF | PIL decodes its one-row-per-strip TIFF in ~0.8 s and truncates 16-bit RGB to 8 bit, so `_tiff_pixels()` maps the contiguous strips straight into numpy (~5 ms) |
| darktable-cli option order | `--out-ext` is a CLI option and must come **before** `--core`; after `--core` it is silently ignored and you get JPEGs |
| per-photo scene analysis (auto lighting) | before darktable starts, all cores, LibRaw single-threaded per worker (`OMP_NUM_THREADS=1`), EXIF read from the first 4 MB only, statistics on every 2nd pixel: ~3.7 s per 100 RAWs, +5 s (~8%) end to end |
| OpenCL (GPU) | tested: slower than CPU on Apple M4, so left off |
| more darktable processes | no gain beyond 3 (darktable already uses all cores); 5 processes were slower |

Quality equivalence (fast vs the original slow path: 1600 px hq, darktable grain) on 20 Sony RAWs: mean ΔE76 1.3 (max 2.7), chroma ratio 0.998, black p1 −0.1, median L −0.2. Visually identical. All 3 looks pass 24/24 with `--verify full`, and 100/100 with `--verify sample`.

### Before/after gallery (`gallery.py`)
```bash
uv run $S/gallery.py OUT_DIR [-o GALLERY_DIR] [--title "萬聖節娃拍"] [--start DSC04537] [--lang zh|en] \
     [--before auto|camera|default] [--long 2048] [--standalone]
```
- **Before** per photo (`--before auto`): the camera JPEG of the RAW (RAW+JPG shooting, same framing; looked up next to the RAW, in `--camera-dir`, in the RAW folder's parent (a `RAW/` subfolder) or `../JPG`); else the input itself (JPEG/PNG/TIFF); else darktable's default render of the RAW (no look, no corrections; rendered on the spot, ~0.4 s/photo). `--before default` always uses darktable's default render, `--before camera` never renders. Camera JPEGs are lens-corrected and 0.5–3.5% larger in scale than the RAW render, so each one is **registered**: d(p) = (p − c)·(k + k2·r²) + t (scale, radial lens term, shift), fitted by phase correlation on a 5×5 patch grid (on √L, so night frames correlate) with outlier rejection, warped with a PIL mesh; frames that can't be fitted (black sky, no texture) borrow the median model of frames at the same focal length. Both images are cropped by the same 1–3% margin, so the slider lines up. Kumamoto night set (149 A7CR frames, zoom lens): 149/149 aligned, 3 by borrowing.
- **Writes** `GALLERY_DIR/index.html` (default `OUT_DIR/_gallery`) and `img/{a,b,t}/<name>.jpg` (2048 px q85 plus 420 px thumbnails, ~0.4 MB per photo). The web copies carry **no EXIF**, so no GPS or serial numbers go online. 67 RAW+JPG pairs take ~15 s.
- **The page**: slider / before only / after only (keys 1 2 3 and ← →, swipe on a phone), a contact sheet, each photo's auto corrections in plain words (`--lang zh` = Traditional Chinese, the default, for this user), chips for scene type, spotlight and capped lifts, and a dot on frames that failed a check. Single dark warm-grey theme on purpose: photos are judged on a dim neutral ground. Checked at 390 px and 1280 px. The template is `scripts/gallery.html`.
- **Show it**: `index.html` is an Artifact page fragment
- **iPhone-album mode (`album.py`)** when the user does not want to scroll (不要上下捲/像 iPhone 相簿/點開放大): `uv run $S/album.py GALLERY_DIR [--title T] [--lang zh]` rewrites `GALLERY_DIR/index.html` as a Photos-app album — square thumbnail grid, tap → the photo grows full-screen, pinch / double-tap / wheel zoom + drag pan, swipe left-right between frames, swipe down (or Esc / the chevron) shrinks it back to the grid, and a 對比 button (key `b`) swaps to the registered before. It reuses the gallery's `img/a|b|t`, reads `_report/summary.json` for the per-photo notes, and keeps the slider page as `compare.html` (linked from the header). Run it **after** `gallery.py`, which owns `index.html`. Serve the folder with `python3 -m http.server PORT --bind 0.0.0.0 --directory GALLERY_DIR` and give the LAN IP. (starts with `<title>`, no doctype). Publish it with the Artifact tool: `file_path` = `GALLERY_DIR/index.html`, `root` = `GALLERY_DIR`, `files` = every `img/a|b|t/*.jpg` path. One publish takes 255 files (3 per photo, so ~85 photos): publish larger sets in parts to the same url. A version holds 511 files (~170 photos): above that use `--long 1600` and split the set. To refresh a published gallery after a re-render, rebuild into the same folder and republish the same `file_path` (same URL). Without the Artifact tool (pi), build with `--standalone` (a full HTML document) and `open GALLERY_DIR/index.html`.

### Single file, full control (`fuji_look.py`)
`python3 $S/fuji_look.py IN -o OUT.jpg --look street [--width N] [--keep-xmp]` renders one file the slow, classic way (hq, darktable's own grain module when `--grain` is given, darktable writes the JPEG directly at quality 100 and 4:4:4; use `-o OUT.tif` for a 16-bit TIFF). `--keep-xmp` saves the sidecar for the darktable GUI, and `--xmp-only FILE` writes just the XMP. It applies the plain recipe (no auto lighting); for a single photo prefer `run.py` (~5 s, with auto lighting). Verify with `uv run $S/verify.py BEFORE.jpg OUT.jpg --look street --compare CMP.jpg`. For a RAW, BEFORE is darktable's default render.

`batch.py` is the older slow batch tool (one darktable process per photo, ~7 s per photo). Prefer run.py.

## How to tune (what to change, where)

Command-line knobs (per photo):

| Symptom | Fix |
|---|---|
| Effect too strong / weak overall | `--strength 0.6` … `1.4` (scales every deviation from neutral) |
| Image too dark / murky | `--brightness 0.08` (mid-tone power); for street also `--local-contrast 0.6` |
| Image too bright / flat | `--brightness -0.08` |
| Shadows too heavy / too lifted | `--shadows 0.05` / `-0.05` |
| Highlights clipping | `--highlights -0.05` |
| Whites look grey / highlights dull | `--highlights 0.05` (highlight gain). The white point is the curve's top node (0.965–0.975 ≈ L 97–98 since 2026-11-11); never lower it below ~0.97 to stop clipping, use the highlight shoulder instead |
| Auto exposure wrong for one photo | `--exposure 0.3` / `-0.3` (adds to the auto EV); `--no-auto` to compare with the plain recipe |
| Auto WB took too much / too little warmth out | change `frac` in `scene.plan()` (amber 0.55–0.85); casts in daylight are never touched |
| Reds still too bright / too saturated | `--zone red:L=-0.06` / `--zone red:C=-0.10` |
| Skin or warm lights look grey | `--zone orange:C=0.10` (street: `0.25`) |
| Sky not deep enough | `--zone sky:L=-0.08 --zone sky:C=0.08` (only works if the source sky has *some* colour) |
| Greens too neon / too yellow | `--zone green:C=-0.15` / `--zone green:h=0.02` (toward teal) |
| User wants film grain | `--grain` (the look's grain) / `--grain 0.5` (off by default) |
| Vignette too much | `--vignette 0.5` (0 = off) |
| Street look too crunchy | `--local-contrast 0.5` |

Zone names: magenta-red, red, orange, yellow, green, cyan, sky, blue, magenta.
An override replaces that node's offset: L applies ×2^(4·L), C applies ×(1+2C), and h shifts hue by h turns.

Editing a look permanently (`LOOKS` dict in `fuji_look.py`):

| Batch failure / visual problem | Parameter to move |
|---|---|
| `global chroma muted` ratio too high (too colourful) | more negative `chroma_global` (non-linear: −0.07 gave ≈1.0, −0.11 ≈0.75, −0.17 ≈0.65 on the RAW set). Also check zone_C on sky/blue, since blue-sky scenes dominate the ratio |
| ratio too low (too grey) | less negative `chroma_global`; for street, less negative `chroma_shadows` (night shots are mostly shadow) |
| `faded blacks` fails | raise the curve's first node y (perceptual; 0.095 ≈ L 8, 0.135 ≈ L 11) |
| `no crushed blacks` / murky | raise the second and third curve nodes; lower `contrast` |
| `mid-tone contrast` too high | lower `contrast` (RAW already gets contrast from sigmoid) or `local_contrast` |
| `highlight roll-off` (new clipping) | saturated colours near white leave sRGB: strengthen the highlight shoulder `HI_CHROMA` (chroma scale by lightness, 3rd color-zones instance) or lower highlight tint chroma. Keep the last curve node 0.965–0.98: it *is* the white point, and a lower one greys every white (`whites kept bright` fails) |
| `reds darker` fails | more negative `pre_zone_L` (red/magenta-red; colour zones placed *before* color balance rgb, where reds still have full chroma). `zone_L` after the desaturation barely acts on reds (colour zones weight by chroma) |
| `greens muted` fails | more negative `zone_C[4]` |
| street `shadows nearly colourless` fails | more negative `chroma_shadows` / `saturation_shadows` |
| `ref match:` WARNs | move the matching fingerprint (black p1 → curve toe, mean chroma → chroma_global, shadow chroma → chroma_shadows, sky L/C → zone sky) |

Workflow for building a **new look from reference photos**:
1. Measure the references with `verify.py any.jpg any.jpg --reference REF...`, or use the `style_stats()` fingerprint.
2. Copy the nearest look, set the curve toe from the black p1, chroma from the mean chroma, and zones from the sky and warm-light values.
3. Render 3–4 similar-content test photos and compare side by side with the references.
4. Run the batch on the RAW set, then add a `PROFILES` entry in `verify.py` with thresholds calibrated on that batch plus visual review.

## What verify.py checks

Two tiers (`verify.py` functions, also used by run.py):
- **`output_checks()`** looks at the edited image alone (`OUTPUT_PROFILES[look]`) and runs on every file: history contains the look modules, matte black floor (L p1: chrome 4.5–24, neg 4–22, street 9.5–20), ≤0.3% clipped, ≤0.1% crushed, mean chroma under a cap (21/23/16), street shadows (non-blue) C ≤5, and not blank.
- **`analyze()`** is the before/after check against a baseline (`PROFILES[look]`), in CIELab after downscaling to 800–1200 px (this also averages out grain):

| Check | Hard? | Signature |
|---|---|---|
| darktable applied look modules | yes | colorbalancergb, rgbcurve and colorzones appear in the embedded history |
| faded blacks | yes | L p1 lifted ≥0.8, or ≥6 |
| no crushed blacks / no new clipping | yes | ≤0.1% more pixels at 0 or 255 |
| mid-tone contrast | yes | std of L over the original's mid-tones: chrome 0.82–1.25, neg 0.82–1.30, street 0.88–1.40 |
| brightness preserved | yes† | median L moves ≤6 |
| global chroma muted | yes† | mean-C ratio: chrome 0.55–0.92, neg 0.55–1.10, street 0.30–0.88; near-grey originals (mean C < 8) may instead rise by ≤2.5 C (the look's split toning) |
| shadows nearly colourless (street) | yes | non-blue shadow C <3.5 or cut ≥60% (blue skies and water are excluded on purpose) |
| reds kept (portrait only) | yes* | red C ratio 0.78–1.35, and not darker (fails only if dL rel < −1.5 *and* absolute dL < 0: scenes with few neutral pixels make the relative figure unreliable); replaces the two red checks below |
| reds darker (vs neutral tones) | yes* | clearly red pixels (C ≥ 28): red-band dL minus the tone curve's own dL at the same L <−0.3. Low-chroma browns (brick, fur, hair) are desaturated, not darkened |
| reds less saturated / greens muted | yes* | C ratio <0.97 / <0.95 |
| whites kept bright | yes | pixels at L ≥ 95 in the original (≥0.3% of the frame: white walls, frost, lace, overcast sky) keep a mean L ≥ 92.5 (`WHITES_MIN`; street ≥ 87.5 for its heavy vignette), measured rank-based: the brightest same share of the output, because the baseline and the output are separate renders and a position mask picks edge pixels that read ~4 L too dark |
| blues, skin/warm tones, shadow/highlight tint, highlights softened | warn | scene-dependent |
| structure preserved | yes | L correlation on ~256 px block means ≥0.95 (street ≥0.93); catches broken renders, ignores sub-pixel offsets between differently scaled renders |
| ref match: … (with `--reference`) | warn | fingerprint inside the reference range ± tolerance |

\* A band covering <0.5% of pixels is downgraded to WARN (too noisy), and one under 0.2% is skipped. In **night** scenes the colour-band checks, colourless/muted shadows, global chroma ratio and clipping are WARN: those colours are mostly emissive (neon, lamps) and protected on purpose.
† WARN on dark-dominated frames (original median L < 15: silhouettes, black night skies), where the median sits in the black floor the look lifts on purpose.

Plus `lighting_checks()` on every file (see "Different lighting") and the JPEG output-file checks (see "Output quality").

## Lessons learned (don't re-learn these)

- **Don't buy "no clipping" with a low white point.** Curve tops of 0.935–0.95 made clipping impossible but capped white at L 94–96: on 200 Sony RAWs whites (L ≥ 95 in the source) came out at L 91–92.5 in `portrait` (white walls, frost, lace, overcast sky looked paper-grey; the user saw over-suppressed highlights). Fix (2026-11-11): tops 0.965–0.975 plus a highlight shoulder (`HI_CHROMA`, chroma eased off above L ≈ 60 by lightness) that keeps sunlit saturated colours in sRGB. `whites kept bright` guards it.
- **Always write an explicit pipeline order.** Without `iop_order_version`, darktable silently uses the **legacy 2.6 order**. `fuji_look.py` writes a custom v5.0 order (RAW and JPEG variants) with `rgbcurve` moved after `vignette`. The film curve then acts on the final display-referred image: after sigmoid tone mapping for RAW (before this change, 14/20 RAWs got crushed blacks), and after the vignette, so the matte floor survives in the corners.
- Speed: darktable-cli start-up (~4 s) and the grain module (0.7 s/photo) dominate. Never spawn one darktable process per photo in a batch, and never re-enable darktable grain in run.py (see "Why it is fast").
- Thresholds sitting exactly at the edge of the calibration range flicker with baseline render size (±0.02 chroma ratio). Keep headroom.
- RAW is the real test. Looks tuned on JPEGs only failed 17/20 RAWs. JPEG-only testing is not enough.
- Compare RAW results against darktable's *default* render, never against the camera's embedded JPEG.
- Colour checks must be relative to the tone curve. A global fade lifts dark reds too, which hides hue-specific darkening, so verify subtracts the neutral-tone dL.
- Mean-chroma ratios depend on the scene: low-saturation scenes drop more and blue-sky scenes drop less. Calibrate thresholds on a diverse batch plus visual review, not on 3 photos.
- Sony "Lossless Compressed **Small/Medium**" ARW (YCbCr sRAW) is not supported by darktable's rawspeed ("Unsupported photometric interpretation: 6"). Use full-size RAWs.
- Colour zones blend out low-chroma pixels, so a white or hazy sky can't be turned blue. Say so instead of over-pushing.
- The same weighting makes colour zones *after* color balance rgb weak on reds (their chroma is already cut to 25–50%), and darktable UCS desaturation keeps perceived brightness (Helmholtz–Kohlrausch), which *lifts* desaturated reds. Darken reds in a colour-zones instance *before* color balance rgb (`pre_zone_L`).
- Don't analyse RAWs from the camera's embedded preview: it ignores darktable's exposure-bias compensation (a −2 EV shot previews 2 stops darker than darktable renders it) and carries the camera's picture style (several test previews were black & white). Use a LibRaw decode with the camera WB.
- Automatic white balance from image content is unreliable (estimators disagree by 10–40 C on skies, sunsets, neon). Only fix casts that are physically likely (artificial light at night/indoors), only when all estimators agree, and only partially.
- A 200-photo real-world batch finds what a curated set misses: on 200 Play Raw ARWs the first run failed 13 (black moon frames lifted +1.5 EV for nothing, bright red flowers clipping because the portrait look keeps reds, and false alarms on capped night/forest lifts, high-key surf, near-neutral fog). Test new looks on a large random batch, then fix causes, not thresholds, wherever the image itself was wrong.
- Check colour on 100% crops of the subject next to the user's reference, never on a thumbnail sheet. The street look turned a doll's crimson dress and red roses brown/black (red C 27 → 5: `chroma_shadows` −0.40 hits every dark colour, not only grey shadows) and its cool highlights greyed the skin; at thumbnail size it read as "deep wine red". Measure patches (dress, skin, cheek, white fabric) in Lab against the reference: the `portrait` look was tuned that way (reference dress C 22–33 h 16–26, apron C 6 h 62; DSC04537 now dress C 19–20 h 24–27, apron C 9 h 59, rosy cheeks h 36).
- EV100 tells how much light there was, not where: a lamp-lit room (EV 3–6) is "night" by EV100. iPhone shots of a doll exhibit and a cat at home (2026-11-08) were darkened −1 EV by an old "night rendered like day" rule and lost their colour to an amber-cast fix that read gold, wood and orange fur as tungsten (C 12–23; real sodium/tungsten casts measured 30–90). Night/indoor frames are now darkened only when clearly overexposed, and amber casts need C > 25 on every estimator.
- File names lie: three "night" Play Raws were daytime shots. Classify by EXIF scene brightness, not by how dark the frame is.
- Before/after with a camera JPEG: it is the "before" the user remembers, but it is lens-corrected (0.5–3.5% larger scale, slightly shifted), so laid straight over the RAW render the slider swims at the edges. Register it and crop both by the same margin (`gallery.py`). A uniform scale is not enough for a zoom: barrel correction at the wide end and pincushion at the tele end (k2 of opposite sign) left 85 of 149 night frames unregistered until the radial term went in. Show darktable's default render only when there is no camera JPEG.
- Night street signs (Kumamoto, 2026-11-14): light boxes and arcade signs came out as white slabs with the lettering gone and orange signs salmon, while the camera JPEG still showed them. The RAW keeps the lettering under the clip point (only the sign background clips); darktable's +0.7 EV and sigmoid push it into the white shoulder, and EXIF calls a lit street "dim", so the night colour protection did not apply. A highlight compression *before* the tone mapper (`toneequal`) brings it back; a lower white point or `shadhi` after sigmoid cannot. Gate it on what is clipped, not on how bright: a white dress and a light box both sit at L 90–97, only the box is clipped. The clipped area alone is the wrong measure (a small arcade sign is 0.3% of the frame); use the clipped share of the bright pixels. Measured: Kumamoto 137/149 frames recovered, overall brightness unchanged; Halloween doll set 7/67 touched at weight ≤0.43, ≤0.02% of pixels changed by >3 levels.
- darktable-cli reads `<file>.xmp` sidecars by the **absolute** path of the `--import` argument: relative paths silently render without the sidecar (an experiment script that passed relative paths rendered every variant identically).
- Per-photo settings in one darktable-cli process: symlink each input into a work folder and write `<link>.xmp` next to it; call darktable-cli with `--import` per file and **no** XMP argument.

## Troubleshooting

- `darktable-cli failed`: run the printed command with `--verbose`. The script uses a throwaway `--configdir` and an in-memory library, so a running darktable GUI doesn't conflict.
- "darktable applied look modules" FAIL: a param blob was rejected, which usually means a module version bump. Compare the struct in the darktable source with `references/recipe.md`.
- `--keep-xmp` saves the sidecar so the edit can be opened and refined in the darktable GUI. `--xmp-only FILE` writes just the XMP.
- If macOS blocks darktable (Gatekeeper, ad-hoc signature), run `xattr -dr com.apple.quarantine /Applications/darktable.app`.
- Every file reports "darktable render failed" and `.work/r*/darktable.log` says `assuming '…/$(FILE_NAME).jpg'`: `--out-ext tif` ended up after `--core`. It must come before it.
- "JPEG quality N" or "no chroma subsampling" FAIL: the encoder ignored `quality`/`subsampling` (very old Pillow). run.py's uv header pulls a current Pillow; check with `uv run --with pillow python -c 'import PIL; print(PIL.__version__)'` (tested with 12.3).

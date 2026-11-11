# fuji-film-look

An agent skill (Claude Code, pi) that turns photos into Fujifilm-style film looks with **darktable**. It takes JPEG/PNG/TIFF or camera RAW (Sony ARW, NEF, CR3, RAF, DNG, …). For each photo it:

1. reads the lighting (night, tungsten/sodium/neon, backlight, spotlit subjects on a dark set, haze, under- or over-exposure) and picks per-photo corrections;
2. renders the look with `darktable-cli`;
3. checks the result numerically against the look's signature, and against your reference photos if you give some;
4. writes a report with contact sheets.

把照片（JPEG 或 Sony ARW 等 RAW）用 darktable 調成富士底片風。內建 Classic Chrome、Classic Neg、日系街拍和人像（膚色紅潤，適合人和娃娃）四種風格，會依每張照片的光線自動修正，最後再用數值和縮圖驗證結果。

## Looks

| `--look` | Character |
|---|---|
| `classic-chrome` (default) | Muted, documentary: darker, less saturated reds, olive greens, restrained blues, faded blacks |
| `classic-neg` | Punchier: teal shadows, warm highlights, deep reds, saturated skies |
| `street` | Moody Japanese street: matte blacks, low chroma, colourless shadows, crunchy local contrast |
| `portrait` | Warm and soft for people and dolls: rosy skin, deep reds and oranges kept, muted greens and blues, faded blacks |

Film grain is off by default; add it with `--grain` (or `--grain 0.5` for half).

## Requirements

- **darktable 5.6.x**. This is a hard dependency: every photo is rendered by `darktable-cli`, and the module parameters are packed for darktable 5.6 module versions (tested on 5.6.2).
  - darktable older than 5.6 is refused with install instructions.
  - A newer darktable runs, but prints a warning.
  - Homebrew's `darktable` cask has been disabled since 2026-09-01 because it fails the Gatekeeper check. `install.sh` downloads the official release instead.
- **[uv](https://docs.astral.sh/uv/)**. It runs the Python scripts and installs numpy, pillow and rawpy from each script's inline metadata (PEP 723).
- macOS on Apple silicon, version 14 or later. darktable 5.6.2 ships a macOS build only for arm64. The scripts are plain Python and should run on Linux with darktable 5.6, but that is untested.

## Install

```bash
git clone https://github.com/AyaSakura-comp/fuji-film-look.git
cd fuji-film-look
./install.sh            # darktable 5.6.2 + uv + link into ~/.claude/skills (and ~/.pi/agent/skills)
./install.sh --check    # report only
```

What `install.sh` does:

- **darktable**: If darktable 5.6.x is missing, it downloads `darktable-5.6.2-arm64.dmg` from [darktable's GitHub release](https://github.com/darktable-org/darktable/releases/tag/release-5.6.2) and verifies its SHA-256. It then copies `darktable.app` to `/Applications`, or to `~/Applications` if `/Applications` is not writable.
- **Existing darktable**: It never replaces an existing `darktable.app` unless you pass `--force`. With `--force`, the old app is moved aside, never deleted.
- **uv**: If uv is missing, it installs it with Homebrew.
- **Skill link**: It symlinks this checkout into your agent's skills folder. Use `--skills-dir DIR` for other agents.
- **Help**: `./install.sh --help` lists all options, such as `--app-dir`, `--no-link` and `--install-darktable`.

darktable-cli is found in this order:

1. `$DARKTABLE_CLI`
2. `/Applications/darktable.app/Contents/MacOS/darktable-cli`
3. `~/Applications/darktable.app/…`
4. `darktable-cli` on `PATH`

## Usage

Agents read [`SKILL.md`](SKILL.md). By hand:

```bash
S=~/.claude/skills/fuji-film-look/scripts
uv run $S/run.py photo.ARW -o out --look portrait                 # one photo, ~5 s
uv run $S/run.py ~/Pictures/trip -o out --look classic-chrome     # a folder
uv run $S/run.py in/ -o out --look street --verify full --reference ref1.jpg ref2.jpg
python3 $S/scene.py photo.ARW                                     # lighting analysis only, no render
```

Outputs are written as `out/<name>_<look>.jpg`: 2048 px long edge by default (`--size 0` gives full resolution), JPEG quality 100 with 4:4:4 chroma, ICC profile and EXIF kept. The report is in `out/_report/`:

- `summary.md`: PASS/FAIL for each check, the auto corrections and why each was made, and timing.
- `sheet.jpg`: contact sheet of the outputs.
- `verify_sheet.jpg`: before/after pairs.

## Performance

Measured on a Mac mini M4 (10 cores, 16 GB) with darktable 5.6.2, using 200 distinct Sony RAWs from 42 camera models:

| Mode | 200 RAWs | Per photo |
|---|---|---|
| default (`--verify sample`) | 108 s | 0.54 s |
| `--verify full` | 177 s | 0.89 s |

How it gets there:

- **Rendering**: three long-lived `darktable-cli` processes, each fed many files. darktable uses its own threads on top.
- **Lighting analysis**: one process per core.
- **Post-processing**: a pool of 4 workers saves the JPEGs and runs the checks while rendering continues.
- **Film grain**: added in numpy, because darktable's grain module costs about 0.7 s per photo.

See SKILL.md ("Why it is fast") for the measurements.

## Layout

```text
fuji-film-look/
├── SKILL.md              agent instructions, tuning guide, lessons learned
├── install.sh            dependencies (darktable 5.6.x, uv) + skill link
├── scripts/
│   ├── run.py            fast batch pipeline (1..N photos): analyse → render → grain → JPEG → verify → report
│   ├── fuji_look.py      looks (LOOKS dict), darktable XMP builder, single-file renderer, darktable-cli lookup + version check
│   ├── scene.py          per-photo lighting analysis and auto corrections
│   ├── verify.py         numeric checks (CIELab), reference matching, comparison sheets
│   └── batch.py          older one-process-per-photo batch tool
└── references/recipe.md  module parameters, measurements, test results
```

## Notes

- darktable is GPL-3.0 software by the darktable project. It is not bundled here; `install.sh` downloads the official, unmodified release.
- The test photos (pixls.us Play Raw, CC BY-SA; raw.pixls.us, CC0) are not included. CI downloads one CC0 Sony RAW from raw.pixls.us for its smoke test.

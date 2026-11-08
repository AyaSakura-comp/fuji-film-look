#!/usr/bin/env bash
# Install the fuji-film-look agent skill and its dependencies:
#   * darktable 5.6.x: hard dependency, every photo is rendered by darktable-cli and the
#     module params are packed for darktable 5.6 module versions (tested on 5.6.2)
#   * uv: runs the Python scripts (numpy, pillow and rawpy come from their inline metadata)
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
NAME=fuji-film-look

DT_VERSION=5.6.2
DT_MAJOR=5 DT_MINOR=6
DT_DMG="darktable-${DT_VERSION}-arm64.dmg"
DT_SHA256=6ff88e58a2a59cb07b0a1502fea7205e68cd783380a33a3ce7bda68ec29def0c  # GitHub release asset digest
DT_URL="https://github.com/darktable-org/darktable/releases/download/release-${DT_VERSION}/${DT_DMG}"

APP_DIR=""
SKILL_DIRS=()
DO_LINK=1 DO_DARKTABLE=1 DO_UV=1 DO_WARM=1 CHECK=0 FORCE=0 INSTALL_DT=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Installs the dependencies and links this checkout into your agent's skills folder.

  darktable ${DT_MAJOR}.${DT_MINOR}.x  required. If it is missing on macOS (Apple silicon, macOS 14+), the official
                  darktable ${DT_VERSION} DMG is downloaded from github.com/darktable-org, checked
                  against its SHA-256 and copied to /Applications (or ~/Applications).
                  On Linux, the script prints instructions instead.
  uv              required. Installed with Homebrew when missing.

Options:
  --skills-dir DIR     link the skill into DIR (repeatable). Default: ~/.claude/skills,
                       plus ~/.pi/agent/skills if it exists
  --app-dir DIR        where to put darktable.app (default: /Applications, else ~/Applications)
  --install-darktable  install darktable ${DT_VERSION} even if another darktable is found
  --force              replace an existing skill folder or darktable.app (the old one is
                       moved aside, never deleted)
  --no-darktable       skip the darktable step
  --no-uv              skip the uv step
  --no-link            do not link the skill
  --no-warm            do not pre-fetch the Python packages
  --check              only report what is installed; exit 1 if something is missing
  -h, --help           show this help
EOF
}

while (($#)); do
  case "$1" in
    --skills-dir) [[ $# -ge 2 ]] || { echo "Missing value for --skills-dir" >&2; exit 2; }; SKILL_DIRS+=("$2"); shift 2 ;;
    --app-dir) [[ $# -ge 2 ]] || { echo "Missing value for --app-dir" >&2; exit 2; }; APP_DIR="$2"; shift 2 ;;
    --install-darktable) INSTALL_DT=1; shift ;;
    --force) FORCE=1; shift ;;
    --no-darktable) DO_DARKTABLE=0; shift ;;
    --no-uv) DO_UV=0; shift ;;
    --no-link) DO_LINK=0; shift ;;
    --no-warm) DO_WARM=0; shift ;;
    --check) CHECK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
stamp() { date +%Y%m%d-%H%M%S; }

# ---------------------------------------------------------------- darktable
find_darktable() {
  local c
  for c in "${DARKTABLE_CLI:-}" \
           ${APP_DIR:+"$APP_DIR/darktable.app/Contents/MacOS/darktable-cli"} \
           /Applications/darktable.app/Contents/MacOS/darktable-cli \
           "$HOME/Applications/darktable.app/Contents/MacOS/darktable-cli" \
           "$(command -v darktable-cli 2>/dev/null || true)"; do
    if [[ -n "$c" && -x "$c" ]]; then printf '%s\n' "$c"; return 0; fi
  done
  return 1
}

darktable_version() {  # prints e.g. 5.6.2
  "$1" --version 2>/dev/null | sed -n 's/^darktable \([0-9][0-9.]*\).*/\1/p' | head -n 1
}

# 0 = supported (5.6.x), 1 = older, 2 = newer than tested, 3 = unreadable
classify_version() {
  local v="$1" maj min
  [[ "$v" =~ ^([0-9]+)\.([0-9]+) ]] || return 3
  maj="${BASH_REMATCH[1]}" min="${BASH_REMATCH[2]}"
  if ((maj == DT_MAJOR && min == DT_MINOR)); then return 0; fi
  if ((maj < DT_MAJOR || (maj == DT_MAJOR && min < DT_MINOR))); then return 1; fi
  return 2
}

linux_instructions() {
  cat >&2 <<EOF
darktable ${DT_MAJOR}.${DT_MINOR}.x is required. On Linux, install it yourself, then rerun ./install.sh:
  * Flatpak: flatpak install flathub org.darktable.Darktable
    then put a wrapper named darktable-cli on PATH:
      #!/bin/sh
      exec flatpak run --command=darktable-cli org.darktable.Darktable "\$@"
  * AppImage: https://github.com/darktable-org/darktable/releases/tag/release-${DT_VERSION}
  * or your distribution's package, if it ships ${DT_MAJOR}.${DT_MINOR}
Set DARKTABLE_CLI=/path/to/darktable-cli if it is not on PATH. (The skill is tested on macOS only.)
EOF
}

TMP=""
MNT=""
cleanup() {
  if [[ -n "$MNT" && -d "$MNT" ]]; then hdiutil detach "$MNT" -quiet 2>/dev/null || hdiutil detach "$MNT" -force -quiet 2>/dev/null || true; fi
  if [[ -n "$TMP" && -d "$TMP" ]]; then
    rm -f -- "$TMP/$DT_DMG"
    rmdir -- "$TMP/mnt" "$TMP" 2>/dev/null || true
  fi
}
trap cleanup EXIT

install_darktable_macos() {
  [[ "$(uname -m)" == arm64 ]] || die "darktable ${DT_VERSION} is published for Apple silicon Macs only (no Intel DMG). Install darktable ${DT_MAJOR}.${DT_MINOR} another way and set DARKTABLE_CLI."
  local osmajor; osmajor="$(sw_vers -productVersion | cut -d. -f1)"
  ((osmajor >= 14)) || die "darktable ${DT_VERSION} needs macOS 14 or newer (this is $(sw_vers -productVersion))."

  local dest="$APP_DIR"
  if [[ -z "$dest" ]]; then
    if [[ -w /Applications ]]; then dest=/Applications; else dest="$HOME/Applications"; fi
  fi
  mkdir -p -- "$dest"
  if [[ -e "$dest/darktable.app" ]] && ((!FORCE)); then
    die "$dest/darktable.app already exists ($(darktable_version "$dest/darktable.app/Contents/MacOS/darktable-cli" || true)). Rerun with --force to move it aside and install ${DT_VERSION}."
  fi

  TMP="$(mktemp -d "${TMPDIR:-/tmp}/fuji-film-look.XXXXXX")"
  say "Downloading darktable ${DT_VERSION} (official release, ~89 MB)..."
  curl -fL --retry 3 --progress-bar -o "$TMP/$DT_DMG" "$DT_URL"
  local got; got="$(shasum -a 256 "$TMP/$DT_DMG" | awk '{print $1}')"
  [[ "$got" == "$DT_SHA256" ]] || die "SHA-256 mismatch for $DT_DMG: got $got, expected $DT_SHA256"
  say "SHA-256 OK."

  MNT="$TMP/mnt"; mkdir -p -- "$MNT"
  if diskutil image >/dev/null 2>&1; then  # macOS 26+: hdiutil attach is deprecated
    diskutil image attach --readOnly --nobrowse --mountPoint "$MNT" "$TMP/$DT_DMG" >/dev/null
  else
    hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$TMP/$DT_DMG" >/dev/null
  fi
  [[ -d "$MNT/darktable.app" ]] || die "darktable.app not found inside $DT_DMG"
  if [[ -e "$dest/darktable.app" ]]; then
    local old; old="$dest/darktable.app.old-$(stamp)"
    mv -- "$dest/darktable.app" "$old"
    say "Moved the previous darktable.app to $old"
  fi
  ditto "$MNT/darktable.app" "$dest/darktable.app"
  hdiutil detach "$MNT" -quiet && MNT=""
  # curl does not set com.apple.quarantine, so Gatekeeper does not block darktable-cli.
  codesign --verify --deep --strict "$dest/darktable.app" 2>/dev/null || warn "codesign could not verify $dest/darktable.app"
  say "Installed $dest/darktable.app"
  if [[ "$dest" != /Applications && "$dest" != "$HOME/Applications" ]]; then
    say "darktable is in a non-standard folder; set: export DARKTABLE_CLI=\"$dest/darktable.app/Contents/MacOS/darktable-cli\""
  fi
  APP_DIR="$dest"
}

ensure_darktable() {
  local cli v rc=0
  if ((INSTALL_DT)) && ((!CHECK)); then
    [[ "$(uname -s)" == Darwin ]] || { linux_instructions; exit 1; }
    install_darktable_macos
  fi
  if cli="$(find_darktable)"; then
    v="$(darktable_version "$cli")"
    classify_version "$v" || rc=$?
    case $rc in
      0) say "darktable $v: $cli"; return 0 ;;
      2) warn "darktable $v at $cli is newer than the tested ${DT_VERSION}; it should read the 5.6 params, check the verify report."; return 0 ;;
      3) warn "could not read the darktable version from $cli --version"; return 0 ;;
      1) if ((CHECK)) || [[ "$(uname -s)" != Darwin ]] || ((!FORCE)); then
           say "darktable $v at $cli is too old: ${DT_MAJOR}.${DT_MINOR}.x is required." >&2
           [[ "$(uname -s)" == Darwin ]] && ((!CHECK)) && say "Rerun with --force to install ${DT_VERSION} (the old app is moved aside)." >&2
           return 1
         fi ;;
    esac
  else
    say "darktable: not found" >&2
    ((CHECK)) && return 1
    [[ "$(uname -s)" == Darwin ]] || { linux_instructions; return 1; }
  fi
  install_darktable_macos
  cli="$(find_darktable)" || die "darktable-cli still not found after installing"
  v="$(darktable_version "$cli")"
  classify_version "$v" || die "installed darktable reports version '$v', expected ${DT_MAJOR}.${DT_MINOR}.x"
  say "darktable $v: $cli"
}

# ---------------------------------------------------------------- uv
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then say "uv: $(uv --version)"; return 0; fi
  say "uv: not found" >&2
  ((CHECK)) && return 1
  if command -v brew >/dev/null 2>&1; then
    HOMEBREW_NO_AUTO_UPDATE=1 brew install uv
  else
    die "install uv (https://docs.astral.sh/uv/getting-started/installation/), then rerun ./install.sh"
  fi
  say "uv: $(uv --version)"
}

# ---------------------------------------------------------------- skill link
link_skill() {
  local dir target real seen=" "
  if ((${#SKILL_DIRS[@]} == 0)); then
    SKILL_DIRS=("$HOME/.claude/skills")
    [[ -d "$HOME/.pi/agent/skills" ]] && SKILL_DIRS+=("$HOME/.pi/agent/skills")
  fi
  local missing=0
  for dir in "${SKILL_DIRS[@]}"; do
    if ((CHECK)); then
      if [[ -e "$dir/$NAME/SKILL.md" ]]; then say "skill: $dir/$NAME"; else say "skill: not linked in $dir" >&2; missing=1; fi
      continue
    fi
    mkdir -p -- "$dir"
    real="$(cd -- "$dir" && pwd -P)"
    [[ "$seen" == *" $real "* ]] && continue  # e.g. ~/.pi/agent/skills -> ~/.claude/skills
    seen+="$real "
    target="$real/$NAME"
    if [[ -e "$target" && "$(cd -- "$target" && pwd -P)" == "$ROOT" ]]; then
      say "skill: $dir/$NAME (this checkout)"; continue
    fi
    if [[ -L "$target" ]]; then
      rm -f -- "$target"
    elif [[ -e "$target" ]]; then
      ((FORCE)) || die "$target exists and is not a link to this checkout. Rerun with --force to move it aside."
      mv -- "$target" "$target.bak-$(stamp)"
      say "Moved the previous $target to $target.bak-*"
    fi
    ln -s -- "$ROOT" "$target"
    say "skill: $dir/$NAME -> $ROOT"
  done
  return $missing
}

# ---------------------------------------------------------------- main
status=0
((DO_DARKTABLE)) && { ensure_darktable || status=1; }
((DO_UV)) && { ensure_uv || status=1; }
((DO_LINK)) && { link_skill || status=1; }

if ((CHECK)); then
  ((status == 0)) && say "All dependencies present." || say "Missing dependencies: run ./install.sh" >&2
  exit $status
fi
((status == 0)) || exit $status

if ((DO_WARM)) && command -v uv >/dev/null 2>&1; then
  say "Fetching Python packages (numpy, pillow, rawpy) into uv's cache..."
  uv run --quiet "$ROOT/scripts/run.py" --help >/dev/null
  uv run --quiet "$ROOT/scripts/verify.py" --help >/dev/null
fi

cat <<EOF

Done. Try:
  uv run "$ROOT/scripts/run.py" PHOTO.ARW -o OUT_DIR --look portrait
EOF

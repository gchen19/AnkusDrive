#!/usr/bin/env bash
#
# install-renderers.sh — provision the external photoreal renderers AnkusDrive's
# render_photoreal tool shells out to, and make them discoverable by the agent.
#
# WHAT IT DOES
#   Downloads the prebuilt, standalone renderer builds, verifies them against
#   pinned SHA-256 checksums, unpacks them under $PREFIX/<renderer>/, and writes a
#   tiny wrapper to $BINDIR named exactly like the binary AnkusDrive's _RENDERERS
#   registry looks for (ankusdrive/worker.py). The wrapper puts the build's bundled
#   shared libraries on LD_LIBRARY_PATH and execs the real binary — so a *found*
#   binary actually launches (the standalone tarballs carry their own lib/ dir).
#
# WHY A WRAPPER ON PATH
#   render_photoreal resolves a renderer via, in order: ANKUSDRIVE_<R>_PATH env ->
#   FreeCAD prefs -> PATH (shutil.which) -> per-OS dirs. A wrapper on PATH whose
#   name matches the registry's `binaries` entry satisfies discovery (#3) AND the
#   bundled-library problem at once, with zero code or MCP-config changes. See
#   docs/RENDER_RENDERER_INSTALL.md §3.
#
#   NB: discovery must happen in the environment that launches the MCP server
#   (`python -m ankusdrive mcp`) — the renderer subprocess inherits the worker's env,
#   which inherits the server's. A wrapper in a system PATH dir covers that;
#   setting PATH only in an interactive shell does not.
#
# USAGE
#   sudo scripts/install-renderers.sh                 # install all prebuilt renderers
#   sudo scripts/install-renderers.sh luxcore         # just one (luxcore|appleseed)
#   scripts/install-renderers.sh blender              # full Blender (Cycles) for the studio backend
#                                                     # (Linux x86_64 + macOS; never in the no-arg run)
#   scripts/install-renderers.sh --list               # show what's pinned + status
#   PREFIX=~/r BINDIR=~/bin scripts/install-renderers.sh   # rootless (PATH must include BINDIR)
#
# ENV OVERRIDES
#   PREFIX     install root           (default: /opt)
#   BINDIR     wrapper dir on PATH    (default: /usr/local/bin)
#   CACHE_DIR  archive download cache (default: ${TMPDIR:-/tmp}/ankusdrive-renderer-cache)
#   FORCE=1    reinstall even if already present
#
# Idempotent: re-running skips a renderer whose binary + wrapper are already in
# place (unless FORCE=1). Verify afterwards with the render_capabilities MCP tool
# or `.venv/bin/python3 tests/test_render_photoreal.py` (gated tests flip SKIP->PASS).
#
# Linux x86_64 only for the automated path; macOS/Windows print guidance (the
# prebuilt layouts differ — a documented follow-on). OSPRay Studio, pbrt-v4 and
# Cycles have no usable prebuilt CLI and must be built from source (see below).
#
# BLENDER (issue #335) is its own target with its own layout, because it is not an
# add-on renderer: render_photoreal's studio backend runs full Blender headless and
# discovers it through ankusdrive/solvers.py (`blender` entry), NOT a wrapper on PATH.
#   Linux x86_64  pinned official tarball (SHA-256 checked) -> /opt/blender-<ver> as
#                 root, else ~/.local/opt/blender-<ver>; both are globbed by discovery,
#                 so it resolves with no PATH or env change. A `blender` symlink goes
#                 in BINDIR as well when that dir is writable (for your own shell).
#   macOS         `brew install --cask blender` when Homebrew is present, else the
#                 pinned arm64 DMG copied to /Applications (or ~/Applications).
#                 Apple Silicon only (5.x has no Intel build). An existing Blender.app
#                 is kept unless FORCE=1 (which also lets brew replace a non-brew app).
#   Windows       scripts/install-solvers.ps1 blender (pinned portable zip; winget is 403-blocked upstream).
# It is ~1 GB on disk and GPL-3.0 (run only as a subprocess), so the no-arg run skips it.
set -euo pipefail

PREFIX="${PREFIX:-/opt}"
BINDIR="${BINDIR:-/usr/local/bin}"
CACHE_DIR="${CACHE_DIR:-${TMPDIR:-/tmp}/ankusdrive-renderer-cache}"
FORCE="${FORCE:-0}"

# --- pinned artifacts (real SHA-256s, computed from the GitHub release assets) --
#
# Records are "key|url|sha256|archive_top|binary_relpath|wrapper_name|libdirs".
#   archive_top    the single top-level dir inside the archive (stripped on install)
#   binary_relpath path to the real executable, relative to $PREFIX/<key>/
#   wrapper_name   MUST equal an entry in _RENDERERS[<Renderer>]["binaries"]
#   libdirs        colon-separated lib dirs (relative to $PREFIX/<key>/) for the wrapper
#
# LuxCore: the plain standalone tarball ships luxcoreui + pyluxcore only — the
# `luxcoreconsole` CLI the addon's batch mode needs lives in the *-sdk* tarball.
# v2.6 is the last release with a standalone build at all (newer = pip wheels).
PINNED_luxcore="luxcore|https://github.com/LuxCoreRender/LuxCore/releases/download/luxcorerender_v2.6/luxcorerender-v2.6-linux64-sdk.tar.bz2|c4a387ee65765b235d47c81c83e293ff584bd4da4b5e24fc89d651c8c1393393|LuxCore-sdk|bin/luxcoreconsole|luxcoreconsole|lib:."
# Appleseed: 2.1.0-beta (2019) is the final published build; ships appleseed.cli.
PINNED_appleseed="appleseed|https://github.com/appleseedhq/appleseed/releases/download/2.1.0-beta/appleseed-2.1.0-beta-0-g015adb503-linux64-gcc74.zip|e96fc907fa95b38c7be542b796fd783870da67612eb233bd4965c2ebec7335d2|appleseed|bin/appleseed.cli|appleseed.cli|lib"

PREBUILT_KEYS="luxcore appleseed"

# Blender: 5.2 LTS (>= 5.1 also satisfies Blender's own MCP server for the .blend
# hand-off). Hashes are the official blender-<ver>.sha256 manifest. download.blender.org
# sits behind a bot challenge that rejects scripted downloads, so the official
# mirrors are tried first and the canonical host last.
BLENDER_VERSION="5.2.1"
BLENDER_SERIES="5.2"
BLENDER_SHA_linux_x64="a31f524fa99a527d3d52b7f5aaa68c34e1a19d5a1c9473f79c5cc610fd5b10e9"
BLENDER_SHA_macos_arm64="6409e21de80994db5f4c4a34486b6fd43cea21085b912f7491c53e923acb65a3"
BLENDER_MIRRORS="${BLENDER_MIRRORS:-https://mirrors.ocf.berkeley.edu/blender/release https://ftp.nluug.nl/pub/graphics/blender/release https://mirror.clarkson.edu/blender/release https://download.blender.org/release}"

# Staging dirs to clean on exit (populated by install_prebuilt), and the Blender DMG
# mount point (install_blender_macos) so a die between attach and detach unmounts it.
STAGE_DIRS=()
DMG_MOUNT=""
cleanup() {
  local d
  if [ -n "$DMG_MOUNT" ]; then hdiutil detach -quiet "$DMG_MOUNT" 2>/dev/null || true; rmdir "$DMG_MOUNT" 2>/dev/null || true; fi
  for d in "${STAGE_DIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done
  return 0   # an EXIT trap's last status becomes the script's: a false `[ -n "" ]` turned success into exit 1
}
trap cleanup EXIT

# --- logging ------------------------------------------------------------------
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s==>%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%swarn%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; exit 1; }

# --- prerequisites ------------------------------------------------------------
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

sha256_of() {  # portable sha256: coreutils sha256sum or BSD/macOS shasum
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum  >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die "no sha256sum/shasum available"; fi
}

# --- platform guard / stubs ---------------------------------------------------
platform_guard() {
  local os; os="$(uname -s)"
  case "$os" in
    Linux) [ "$(uname -m)" = "x86_64" ] || die "automated path is Linux x86_64 only (got $(uname -m))" ;;
    Darwin)
      cat >&2 <<'EOF'
macOS is not yet automated (the prebuilt layouts differ; follow-on work).
Get the binaries manually, then drop a wrapper on PATH named like the registry
entry (luxcoreconsole / appleseed.cli), e.g.:
  - LuxCore:   build/get luxcoreconsole; brew can supply deps
  - Appleseed: appleseedhq/appleseed releases (mac64-clang build)
A wrapper that exports DYLD_LIBRARY_PATH to the build's lib dir is the macOS
equivalent of the Linux LD_LIBRARY_PATH wrapper this script writes.
EOF
      exit 2 ;;
    *)
      cat >&2 <<'EOF'
Windows / other is not automated. Install the renderer (installer or zip), then
ensure its .exe is on PATH under the registry's binary name, or set the
ANKUSDRIVE_<RENDERER>_PATH env var in the environment that launches the MCP server.
EOF
      exit 2 ;;
  esac
}

# --- core install -------------------------------------------------------------
fetch() {  # url dest — cached; resumes are avoided (we re-verify by checksum)
  local url="$1" dest="$2"
  if [ -f "$dest" ]; then log "using cached $(basename "$dest")"; return; fi
  log "downloading $(basename "$dest")"
  curl -fL --retry 3 --connect-timeout 30 -o "$dest.part" "$url" || die "download failed: $url"
  mv "$dest.part" "$dest"
}

extract() {  # archive stage_dir
  local a="$1" stage="$2"
  case "$a" in
    *.tar.bz2|*.tbz2) tar xjf "$a" -C "$stage" ;;
    *.tar.gz|*.tgz)   tar xzf "$a" -C "$stage" ;;
    *.tar.xz)         tar xJf "$a" -C "$stage" ;;
    *.zip)            need_cmd unzip; unzip -q "$a" -d "$stage" ;;
    *) die "unknown archive type: $a" ;;
  esac
}

write_wrapper() {  # wrapper_path real_binary libdirs_abs_csv
  local wrapper="$1" real="$2" libs="$3"
  cat > "$wrapper" <<EOF
#!/bin/sh
# AnkusDrive renderer wrapper — generated by scripts/install-renderers.sh.
# Puts the build's bundled libraries on the loader path, then execs the binary.
LD_LIBRARY_PATH="${libs}\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "${real}" "\$@"
EOF
  chmod +x "$wrapper"
}

install_prebuilt() {  # record string
  local rec="$1"
  local key url sha top binrel wrap libcsv
  IFS='|' read -r key url sha top binrel wrap libcsv <<EOF
$rec
EOF
  local dest="$PREFIX/$key"
  local real="$dest/$binrel"
  local wrapper="$BINDIR/$wrap"

  if [ "$FORCE" != "1" ] && [ -x "$real" ] && [ -f "$wrapper" ]; then
    ok "$key already installed ($real); skipping (FORCE=1 to reinstall)"
    return
  fi

  local archive="$CACHE_DIR/$(basename "$url")"
  fetch "$url" "$archive"

  log "verifying checksum"
  local got; got="$(sha256_of "$archive")"
  [ "$got" = "$sha" ] || die "checksum mismatch for $(basename "$archive")
  expected $sha
  got      $got
(delete $archive and retry, or the pinned hash is stale — see the script header)"
  ok "sha256 $got"

  # Extract to a staging dir, then move the single top-level dir's contents into
  # $PREFIX/<key> (uniform across tar/zip; no reliance on tar --strip-components).
  # An EXIT trap cleans staging even on the die paths below (RETURN traps leak to
  # the caller under set -u, so we avoid them).
  local stage; stage="$(mktemp -d "${TMPDIR:-/tmp}/ankusdrive-stage.XXXXXX")"
  STAGE_DIRS+=("$stage")
  log "extracting into $dest"
  extract "$archive" "$stage"
  [ -d "$stage/$top" ] || die "archive top dir '$top' not found in $(basename "$archive")"
  rm -rf "$dest"; mkdir -p "$dest"
  cp -a "$stage/$top/." "$dest/"
  rm -rf "$stage"
  [ -f "$real" ] || die "expected binary not found after extract: $real"
  # Normalize permissions: some archives (e.g. the Appleseed 2019 zip) ship libs
  # at mode 600, which cp -a preserves — then a non-root user can't load them when
  # the install ran as root (loader: "cannot open shared object file"). a+rX makes
  # every file readable and keeps execute on dirs + already-executable files.
  chmod -R a+rX "$dest"
  chmod +x "$real" 2>/dev/null || true

  # Build an absolute LD_LIBRARY_PATH from the colon-separated relative lib dirs.
  local libs="" d
  IFS=':' read -ra _libdirs <<< "$libcsv"
  for d in "${_libdirs[@]}"; do
    case "$d" in .) d="$dest" ;; *) d="$dest/$d" ;; esac
    libs="${libs:+$libs:}$d"
  done

  log "writing wrapper $wrapper -> $real"
  write_wrapper "$wrapper" "$real" "$libs"
  ok "$key installed; '$wrap' on PATH at $wrapper"
}

# --- Blender (studio backend, issue #335) ---------------------------------------
fetch_blender() {  # file dest — try each mirror, keep the first complete download
  local file="$1" dest="$2" m
  if [ -f "$dest" ]; then log "using cached $file"; return; fi
  for m in $BLENDER_MIRRORS; do
    log "downloading $file from $m"
    if curl -fL --retry 2 --connect-timeout 30 -A "ankusdrive-install-renderers" \
         -o "$dest.part" "$m/Blender$BLENDER_SERIES/$file"; then
      mv "$dest.part" "$dest"; return
    fi
    warn "mirror failed: $m"
  done
  rm -f "$dest.part"
  die "could not download $file from any mirror (set BLENDER_MIRRORS=<base url> to add one)"
}

verify_sha() {  # file expected
  local got; got="$(sha256_of "$1")"
  [ "$got" = "$2" ] || die "checksum mismatch for $(basename "$1")
  expected $2
  got      $got
(delete $1 and retry)"
  ok "sha256 $got"
}

install_blender_linux() {
  [ "$(uname -m)" = "x86_64" ] || die "Blender publishes Linux builds for x86_64 only (got $(uname -m)); build from source or use a distro package, then set ANKUSDRIVE_BLENDER_PATH"
  need_cmd curl; need_cmd tar
  local root dest file archive
  if [ "$(id -u)" = "0" ]; then root="${BLENDER_PREFIX:-/opt}"; else root="${BLENDER_PREFIX:-$HOME/.local/opt}"; fi
  dest="$root/blender-$BLENDER_VERSION"
  if [ "$FORCE" != "1" ] && [ -x "$dest/blender" ]; then
    ok "Blender $BLENDER_VERSION already installed ($dest/blender); skipping (FORCE=1 to reinstall)"
  else
    file="blender-$BLENDER_VERSION-linux-x64.tar.xz"
    mkdir -p "$CACHE_DIR" "$root" || die "cannot create $root (set BLENDER_PREFIX=<writable dir>)"
    archive="$CACHE_DIR/$file"
    fetch_blender "$file" "$archive"
    verify_sha "$archive" "$BLENDER_SHA_linux_x64"
    local stage; stage="$(mktemp -d "${TMPDIR:-/tmp}/ankusdrive-stage.XXXXXX")"
    STAGE_DIRS+=("$stage")
    log "extracting into $dest"
    tar xJf "$archive" -C "$stage"
    [ -x "$stage/blender-$BLENDER_VERSION-linux-x64/blender" ] || die "unexpected tarball layout"
    rm -rf "$dest"
    mv "$stage/blender-$BLENDER_VERSION-linux-x64" "$dest"
    chmod -R a+rX "$dest"
  fi
  # Discovery globs $root/blender-*; the symlink only helps an interactive shell.
  local bindir="${BINDIR_BLENDER:-}"
  [ -z "$bindir" ] && { if [ "$(id -u)" = "0" ]; then bindir="$BINDIR"; else bindir="$HOME/.local/bin"; fi; }
  if mkdir -p "$bindir" 2>/dev/null && [ -w "$bindir" ]; then
    ln -sfn "$dest/blender" "$bindir/blender"
    ok "symlinked $bindir/blender"
  fi
  blender_postcheck "$dest/blender"
}

# The macOS app bundles discovery (solvers.py `blender`) looks in, in its order.
MAC_BLENDER_APPS="/Applications/Blender.app $HOME/Applications/Blender.app"

mac_blender_found() {  # print the first discoverable Blender binary, if any
  local app
  for app in $MAC_BLENDER_APPS; do
    [ -x "$app/Contents/MacOS/Blender" ] && { printf '%s\n' "$app/Contents/MacOS/Blender"; return 0; }
  done
  return 1
}

install_blender_macos() {
  # Blender 5.x ships macOS builds for Apple Silicon only, and the brew cask carries
  # no Intel variant (it would install an arm64 app that cannot launch), so gate the
  # arch before either path.
  [ "$(uname -m)" = "arm64" ] || die "Blender $BLENDER_SERIES has no Intel macOS build (the cask and the pinned DMG are arm64 only); install Blender 4.5 LTS (the last Intel release) from blender.org, then set ANKUSDRIVE_BLENDER_PATH if it is not in /Applications"
  local existing
  if [ "$FORCE" != "1" ] && existing="$(mac_blender_found)"; then
    ok "Blender already installed ($existing); skipping (FORCE=1 to reinstall)"
    blender_postcheck "$existing"
    return
  fi
  if command -v brew >/dev/null 2>&1; then
    # --force replaces an app brew did not install (e.g. a blender.org drag-install),
    # which a plain `brew install --cask` refuses to overwrite.
    local force_flag=""; [ "$FORCE" = "1" ] && force_flag="--force"
    log "brew install --cask $force_flag blender"
    brew install --cask $force_flag blender || die "brew install --cask blender failed (an existing Blender.app not installed by brew? re-run with FORCE=1)"
    # The cask honours --appdir / HOMEBREW_CASK_OPTS, so don't assume /Applications.
    if existing="$(mac_blender_found)"; then
      blender_postcheck "$existing"
    elif existing="$(command -v blender)"; then
      blender_postcheck "$existing"
      warn "Blender.app is outside /Applications and ~/Applications, so discovery will not find it; set ANKUSDRIVE_BLENDER_PATH to the Blender executable inside the app"
    else
      die "brew installed Blender but no Blender.app was found; set ANKUSDRIVE_BLENDER_PATH"
    fi
    return
  fi
  need_cmd curl; need_cmd hdiutil
  local file="blender-$BLENDER_VERSION-macos-arm64.dmg"
  mkdir -p "$CACHE_DIR"
  fetch_blender "$file" "$CACHE_DIR/$file"
  verify_sha "$CACHE_DIR/$file" "$BLENDER_SHA_macos_arm64"
  DMG_MOUNT="$(mktemp -d "${TMPDIR:-/tmp}/ankusdrive-dmg.XXXXXX")"   # detached by cleanup()
  hdiutil attach -nobrowse -readonly -mountpoint "$DMG_MOUNT" "$CACHE_DIR/$file" >/dev/null </dev/null || die "hdiutil attach failed"
  local apps="/Applications"; [ -w "$apps" ] || { apps="$HOME/Applications"; mkdir -p "$apps"; }
  log "copying Blender.app to $apps"
  rm -rf "$apps/Blender.app"
  cp -R "$DMG_MOUNT/Blender.app" "$apps/" || die "copy to $apps failed"
  hdiutil detach -quiet "$DMG_MOUNT" && { rmdir "$DMG_MOUNT" 2>/dev/null || true; DMG_MOUNT=""; }
  blender_postcheck "$apps/Blender.app/Contents/MacOS/Blender"
}

blender_postcheck() {  # binary — confirm it launches headless and prints its version
  local v hint
  v="$("$1" --background --factory-startup --version 2>/dev/null | head -1)" || true
  case "$(uname -s)" in
    Darwin) hint="run it directly to see the error, or check the arch with: file $1" ;;
    *)      hint="missing system libs? try: ldd $1" ;;
  esac
  [ -n "$v" ] || die "installed, but '$1 --background --version' did not run ($hint)"
  ok "$v at $1"
  [ "$(uname -s)" = "Darwin" ] && log "the first Metal render compiles GPU kernels once (~1-2 min); later renders start in seconds"
  ok "verify discovery: render_capabilities (renderers.Blender) or 'ankusdrive doctor' (studio_render)"
}

install_blender() {
  case "$(uname -s)" in
    Linux)  install_blender_linux ;;
    Darwin) install_blender_macos ;;
    *) die "on Windows run: scripts/install-solvers.ps1 blender  (the winget route is 403-blocked upstream: winget fetches the MSI from download.blender.org)" ;;
  esac
}

# --- list / status ------------------------------------------------------------
record_for() { eval "printf '%s' \"\${PINNED_$1:-}\""; }

do_list() {
  printf 'Prebuilt renderers this script can install (Linux x86_64):\n\n'
  local key rec wrap binrel
  for key in $PREBUILT_KEYS; do
    rec="$(record_for "$key")"
    IFS='|' read -r _ _ _ _ binrel wrap _ <<EOF
$rec
EOF
    if command -v "$wrap" >/dev/null 2>&1; then
      printf '  %-10s wrapper "%s"  [installed: %s]\n' "$key" "$wrap" "$(command -v "$wrap")"
    else
      printf '  %-10s wrapper "%s"  [not installed]\n' "$key" "$wrap"
    fi
  done
  printf '\nStudio backend (own target, not in the no-arg run):\n\n'
  local found=""
  case "$(uname -s)" in
    Darwin) found="$(mac_blender_found)" || found="" ;;
    Linux)  for found in /opt/blender-* "$HOME"/.local/opt/blender-*; do
              [ -x "$found/blender" ] && { found="$found/blender"; break; }
              found=""
            done ;;
  esac
  if [ -n "$found" ]; then
    printf '  %-10s Blender %s  [installed: %s]\n' blender "$BLENDER_VERSION" "$found"
  elif command -v blender >/dev/null 2>&1; then
    printf '  %-10s Blender %s  [on PATH: %s]\n' blender "$BLENDER_VERSION" "$(command -v blender)"
  else
    printf '  %-10s Blender %s  (Linux x86_64 tarball / macOS cask; discovery also globs /opt/blender-* and ~/.local/opt/blender-*)\n' blender "$BLENDER_VERSION"
  fi
  cat <<'EOF'

Build-from-source only (no usable prebuilt CLI — a separate phase):
  ospray     OSPRay Studio publishes no `ospStudio` release binary; the OSPRay
             SDK tarball ships the library + examples, not the Studio app.
             Build RenderKit/ospray_studio with CMake against the OSPRay SDK.
  pbrt       Build pbrt-v4 (CMake) from https://github.com/mmp/pbrt-v4 (experimental upstream).
  cycles     Build the standalone `cycles` CLI (Blender's engine); no standalone build ships.
             For Cycles quality without a source build, use the `blender` target instead.
Drop the resulting binary (or a wrapper) on PATH under the registry name
(ospStudio / pbrt / cycles), then re-run render_capabilities to confirm.
EOF
}

# --- main ---------------------------------------------------------------------
main() {
  local targets=() want_blender=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --list|-l) do_list; exit 0 ;;
      -h|--help) sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d'; exit 0 ;;
      luxcore|appleseed) targets+=("$1") ;;
      blender) want_blender=1 ;;
      ospray|pbrt|cycles) die "$1 has no prebuilt CLI; build from source (scripts/install-renderers.sh --list)" ;;
      *) die "unknown argument: $1 (try --list or --help)" ;;
    esac
    shift
  done
  if [ "$want_blender" = "1" ]; then
    install_blender
    [ ${#targets[@]} -eq 0 ] && exit 0
  fi
  [ ${#targets[@]} -eq 0 ] && targets=($PREBUILT_KEYS)

  platform_guard
  need_cmd curl; need_cmd tar
  mkdir -p "$CACHE_DIR"
  mkdir -p "$PREFIX" 2>/dev/null || die "cannot create $PREFIX (run with sudo, or set PREFIX=<writable dir>)"
  mkdir -p "$BINDIR" 2>/dev/null || die "cannot create $BINDIR (run with sudo, or set BINDIR=<dir on PATH>)"
  [ -w "$PREFIX" ] || die "$PREFIX not writable (run with sudo, or set PREFIX=<writable dir>)"
  [ -w "$BINDIR" ] || die "$BINDIR not writable (run with sudo, or set BINDIR=<dir on PATH>)"

  local key
  for key in "${targets[@]}"; do
    log "renderer: $key"
    install_prebuilt "$(record_for "$key")"
  done

  echo
  ok "done. Verify: render_capabilities (MCP) or .venv/bin/python3 tests/test_render_photoreal.py"
  case ":$PATH:" in
    *":$BINDIR:"*) : ;;
    *) warn "$BINDIR is not on PATH — add it in the env that launches 'python -m ankusdrive mcp', else the agent won't find the wrappers" ;;
  esac
}

main "$@"

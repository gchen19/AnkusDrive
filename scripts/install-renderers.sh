#!/usr/bin/env bash
#
# install-renderers.sh — provision the external photoreal renderers DriftPin's
# render_photoreal tool shells out to, and make them discoverable by the agent.
#
# WHAT IT DOES
#   Downloads the prebuilt, standalone renderer builds, verifies them against
#   pinned SHA-256 checksums, unpacks them under $PREFIX/<renderer>/, and writes a
#   tiny wrapper to $BINDIR named exactly like the binary DriftPin's _RENDERERS
#   registry looks for (driftpin/worker.py). The wrapper puts the build's bundled
#   shared libraries on LD_LIBRARY_PATH and execs the real binary — so a *found*
#   binary actually launches (the standalone tarballs carry their own lib/ dir).
#
# WHY A WRAPPER ON PATH
#   render_photoreal resolves a renderer via, in order: DRIFTPIN_<R>_PATH env ->
#   FreeCAD prefs -> PATH (shutil.which) -> per-OS dirs. A wrapper on PATH whose
#   name matches the registry's `binaries` entry satisfies discovery (#3) AND the
#   bundled-library problem at once, with zero code or MCP-config changes. See
#   docs/RENDER_RENDERER_INSTALL.md §3.
#
#   NB: discovery must happen in the environment that launches the MCP server
#   (`python -m driftpin mcp`) — the renderer subprocess inherits the worker's env,
#   which inherits the server's. A wrapper in a system PATH dir covers that;
#   setting PATH only in an interactive shell does not.
#
# USAGE
#   sudo scripts/install-renderers.sh                 # install all prebuilt renderers
#   sudo scripts/install-renderers.sh luxcore         # just one (luxcore|appleseed)
#   scripts/install-renderers.sh --list               # show what's pinned + status
#   PREFIX=~/r BINDIR=~/bin scripts/install-renderers.sh   # rootless (PATH must include BINDIR)
#
# ENV OVERRIDES
#   PREFIX     install root           (default: /opt)
#   BINDIR     wrapper dir on PATH    (default: /usr/local/bin)
#   CACHE_DIR  archive download cache (default: ${TMPDIR:-/tmp}/driftpin-renderer-cache)
#   FORCE=1    reinstall even if already present
#
# Idempotent: re-running skips a renderer whose binary + wrapper are already in
# place (unless FORCE=1). Verify afterwards with the render_capabilities MCP tool
# or `.venv/bin/python3 tests/test_render_photoreal.py` (gated tests flip SKIP->PASS).
#
# Linux x86_64 only for the automated path; macOS/Windows print guidance (the
# prebuilt layouts differ — a documented follow-on). OSPRay Studio, pbrt-v4 and
# Cycles have no usable prebuilt CLI and must be built from source (see below).
set -euo pipefail

PREFIX="${PREFIX:-/opt}"
BINDIR="${BINDIR:-/usr/local/bin}"
CACHE_DIR="${CACHE_DIR:-${TMPDIR:-/tmp}/driftpin-renderer-cache}"
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

# Staging dirs to clean on exit (populated by install_prebuilt).
STAGE_DIRS=()
cleanup() { local d; for d in "${STAGE_DIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
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
DRIFTPIN_<RENDERER>_PATH env var in the environment that launches the MCP server.
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
# DriftPin renderer wrapper — generated by scripts/install-renderers.sh.
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
  local stage; stage="$(mktemp -d "${TMPDIR:-/tmp}/driftpin-stage.XXXXXX")"
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
  cat <<'EOF'

Build-from-source only (no usable prebuilt CLI — a separate phase):
  ospray     OSPRay Studio publishes no `ospStudio` release binary; the OSPRay
             SDK tarball ships the library + examples, not the Studio app.
             Build RenderKit/ospray_studio with CMake against the OSPRay SDK.
  pbrt       Build pbrt-v4 (CMake) from https://github.com/mmp/pbrt-v4 (experimental upstream).
  cycles     Build the standalone `cycles` CLI (Blender's engine); no standalone build ships.
Drop the resulting binary (or a wrapper) on PATH under the registry name
(ospStudio / pbrt / cycles), then re-run render_capabilities to confirm.
EOF
}

# --- main ---------------------------------------------------------------------
main() {
  local targets=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --list|-l) do_list; exit 0 ;;
      -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
      luxcore|appleseed) targets+=("$1") ;;
      ospray|pbrt|cycles) die "$1 has no prebuilt CLI; build from source (scripts/install-renderers.sh --list)" ;;
      *) die "unknown argument: $1 (try --list or --help)" ;;
    esac
    shift
  done
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
    *) warn "$BINDIR is not on PATH — add it in the env that launches 'python -m driftpin mcp', else the agent won't find the wrappers" ;;
  esac
}

main "$@"

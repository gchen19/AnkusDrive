#!/usr/bin/env bash
#
# build-renderers.sh — build the three source-only photoreal renderers AnkusDrive's
# render_photoreal tool can shell out to, and make them discoverable by the agent.
#
# WHAT IT DOES
#   The companion to scripts/install-renderers.sh. That script provisions the two
#   renderers that ship a usable prebuilt CLI (Appleseed, LuxCore). This one builds
#   the three that publish NO usable prebuilt headless binary and must be compiled:
#
#     pbrt    pbrt-v4         — self-contained CMake build (vendors its deps).
#     cycles  Cycles (Blender)— CPU standalone, built against SYSTEM libraries
#                               (no Blender precompiled-lib bundle needed).
#     ospray  OSPRay Studio   — built against Intel's prebuilt OSPRay SDK (which
#                               bundles Embree/OpenVKL/rkcommon/TBB/OIDN).
#
#   For each it builds under $BUILD_ROOT, installs under $PREFIX/<renderer>/, and
#   writes a wrapper to $BINDIR named exactly like the binary AnkusDrive's _RENDERERS
#   registry looks for (ankusdrive/worker.py): `pbrt`, `cycles`, `ospStudio`. The
#   OSPRay wrapper also puts the SDK's bundled libraries on LD_LIBRARY_PATH (pbrt
#   and cycles link only system libs, so their wrappers just exec). Same discovery
#   contract as install-renderers.sh — see docs/RENDER_RENDERER_INSTALL.md §3.
#
#   NB: discovery must happen in the environment that launches the MCP server
#   (`python -m ankusdrive mcp`); a wrapper in a system PATH dir covers that.
#
# USAGE
#   sudo scripts/build-renderers.sh                  # build all three
#   sudo scripts/build-renderers.sh pbrt             # just one (pbrt|cycles|ospray)
#   scripts/build-renderers.sh --list                # show what's pinned + status
#   sudo scripts/build-renderers.sh --deps-only      # only apt-install build deps
#
# ENV OVERRIDES
#   PREFIX      install root           (default: /opt)
#   BINDIR      wrapper dir on PATH    (default: /usr/local/bin)
#   BUILD_ROOT  build/scratch dir      (default: ${TMPDIR:-/tmp}/ankusdrive-renderer-build)
#   JOBS        parallel build jobs    (default: nproc)
#   FORCE=1     rebuild even if already installed
#   SKIP_APT=1  don't apt-install build deps (assume they're present)
#
# Idempotent: re-running skips a renderer whose binary + wrapper are already in
# place (unless FORCE=1). Verify with the render_capabilities MCP tool or
# `.venv/bin/python3 tests/test_render_photoreal.py` (gated tests flip SKIP->PASS).
#
# Linux x86_64 only. Needs: git, cmake (>=3.15), ninja, curl, a C++17 compiler
# (gcc>=9.3), and apt for the system libraries Cycles links against. Building all
# three needs a few GB of scratch in $BUILD_ROOT and installs ~0.5 GB under $PREFIX.
set -euo pipefail

PREFIX="${PREFIX:-/opt}"
BINDIR="${BINDIR:-/usr/local/bin}"
BUILD_ROOT="${BUILD_ROOT:-${TMPDIR:-/tmp}/ankusdrive-renderer-build}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
FORCE="${FORCE:-0}"
SKIP_APT="${SKIP_APT:-0}"

# --- pinned sources (commit/tag pins for reproducibility) ---------------------
PBRT_REPO="https://github.com/mmp/pbrt-v4.git"
PBRT_COMMIT="7154d8268ba1f512b20f25e6826999e346d02a15"   # master, Jun 2026

# Cycles standalone. release/v4.2 (tag v4.2.0) aligns with Ubuntu 24.04's system
# libs (OpenImageIO 2.4, OpenColorIO 2.1, Embree 4.3, OpenEXR 3.1, OpenVDB 10).
CYCLES_REPO="https://github.com/blender/cycles.git"
CYCLES_TAG="v4.2.0"

# OSPRay Studio is archived upstream (RenderKit/ospray-studio, master == v1.1.0).
# Its CMake pins OSPRAY_VERSION 3.2.0 — we build it against the matching prebuilt SDK.
OSPRAY_STUDIO_REPO="https://github.com/RenderKit/ospray-studio.git"
OSPRAY_STUDIO_COMMIT="686ceff5f1f9ee5441d5551bc22a42bcc1ef0567"   # archived master (v1.1.0)
OSPRAY_SDK_VERSION="3.2.0"
OSPRAY_SDK_URL="https://github.com/RenderKit/ospray/releases/download/v${OSPRAY_SDK_VERSION}/ospray-${OSPRAY_SDK_VERSION}.x86_64.linux.tar.gz"
OSPRAY_SDK_SHA256="d8670e69b4762e24f2aa83629af897e8f33ea1c724e17e613942c0ccc1c723be"

ALL_KEYS="pbrt cycles ospray"

# --- logging ------------------------------------------------------------------
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s==>%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%swarn%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum  >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die "no sha256sum/shasum available"; fi
}

# --- platform / prerequisites -------------------------------------------------
platform_guard() {
  [ "$(uname -s)" = "Linux" ] || die "build path is Linux x86_64 only (got $(uname -s))"
  [ "$(uname -m)" = "x86_64" ] || die "build path is Linux x86_64 only (got $(uname -m))"
}

# Build deps. The Cycles list is the set of -dev packages its CMake finds as system
# libraries; OpenSubdiv/Alembic/OSL/OIDN are NOT in Ubuntu's archive, so the Cycles
# build below disables them. libglfw3-dev is for OSPRay Studio's (headless-capable) UI.
APT_DEPS_COMMON="git cmake ninja-build curl build-essential"
APT_DEPS_OSPRAY="libglfw3-dev"
APT_DEPS_CYCLES="libopenimageio-dev libopencolorio-dev libembree-dev libopenexr-dev \
libtbb-dev libboost-dev libboost-filesystem-dev libboost-system-dev libboost-thread-dev \
libboost-regex-dev libpugixml-dev libjpeg-dev libpng-dev libtiff-dev libopenvdb-dev \
libblosc-dev zlib1g-dev"

apt_install() {  # space-separated package list
  [ "$SKIP_APT" = "1" ] && { warn "SKIP_APT=1 — assuming build deps present"; return; }
  command -v apt-get >/dev/null 2>&1 || { warn "no apt-get; install build deps manually: $*"; return; }
  log "apt-get install build deps"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q $* || die "apt-get install failed"
}

write_exec_wrapper() {  # wrapper_path real_binary [libdirs_abs]
  local wrapper="$1" real="$2" libs="${3:-}"
  if [ -n "$libs" ]; then
    cat > "$wrapper" <<EOF
#!/bin/sh
# AnkusDrive renderer wrapper — generated by scripts/build-renderers.sh.
LD_LIBRARY_PATH="${libs}\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "${real}" "\$@"
EOF
  else
    cat > "$wrapper" <<EOF
#!/bin/sh
# AnkusDrive renderer wrapper — generated by scripts/build-renderers.sh.
# Binary links only system libraries, so no LD_LIBRARY_PATH is needed.
exec "${real}" "\$@"
EOF
  fi
  chmod +x "$wrapper"
}

already_installed() {  # real_binary wrapper_path
  [ "$FORCE" != "1" ] && [ -x "$1" ] && [ -f "$2" ]
}

# --- pbrt-v4 ------------------------------------------------------------------
build_pbrt() {
  local real="$PREFIX/pbrt/bin/pbrt" wrapper="$BINDIR/pbrt"
  if already_installed "$real" "$wrapper"; then
    ok "pbrt already installed ($real); skipping (FORCE=1 to rebuild)"; return; fi
  apt_install $APT_DEPS_COMMON
  local src="$BUILD_ROOT/pbrt-v4"
  if [ ! -d "$src/.git" ]; then
    log "cloning pbrt-v4 @ ${PBRT_COMMIT:0:12}"
    rm -rf "$src"; git clone --recursive "$PBRT_REPO" "$src"
    git -C "$src" checkout -q "$PBRT_COMMIT"
    git -C "$src" submodule update --init --recursive
  fi
  log "configuring pbrt (Release, CPU)"
  cmake -S "$src" -B "$src/build" -G Ninja -DCMAKE_BUILD_TYPE=Release
  log "building pbrt ($JOBS jobs)"
  ninja -C "$src/build" -j "$JOBS" pbrt imgtool
  log "installing -> $PREFIX/pbrt"
  install -d "$PREFIX/pbrt/bin"
  install -m755 "$src/build/pbrt"    "$PREFIX/pbrt/bin/pbrt"
  install -m755 "$src/build/imgtool" "$PREFIX/pbrt/bin/imgtool"
  write_exec_wrapper "$wrapper" "$real"
  ok "pbrt installed; 'pbrt' on PATH at $wrapper"
}

# --- Cycles (standalone, CPU, system libs) ------------------------------------
build_cycles() {
  local real="$PREFIX/cycles/bin/cycles" wrapper="$BINDIR/cycles"
  if already_installed "$real" "$wrapper"; then
    ok "cycles already installed ($real); skipping (FORCE=1 to rebuild)"; return; fi
  apt_install $APT_DEPS_COMMON $APT_DEPS_CYCLES
  local src="$BUILD_ROOT/cycles"
  if [ ! -d "$src/.git" ]; then
    log "cloning Cycles $CYCLES_TAG"
    rm -rf "$src"; git clone --depth 1 --branch "$CYCLES_TAG" "$CYCLES_REPO" "$src"
  fi
  # The repo carries EMPTY precompiled-lib submodule placeholders (lib/linux_x64,
  # update=none). Their mere existence flips Cycles' CMake into "precompiled libs"
  # mode and makes it ignore system paths -> "Could NOT find ZLIB". Removing the
  # empty placeholders forces the system-library detection branch.
  rm -rf "$src/lib"
  log "configuring Cycles (CPU standalone, system libs; OSL/USD/OIDN/OpenSubdiv/Alembic off)"
  cmake -S "$src" -B "$src/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DWITH_CYCLES_STANDALONE=ON -DWITH_CYCLES_STANDALONE_GUI=OFF \
    -DWITH_CYCLES_OSL=OFF -DWITH_CYCLES_USD=OFF -DWITH_CYCLES_HYDRA_RENDER_DELEGATE=OFF \
    -DWITH_CYCLES_DEVICE_CUDA=OFF -DWITH_CYCLES_DEVICE_OPTIX=OFF -DWITH_CYCLES_DEVICE_HIP=OFF \
    -DWITH_CYCLES_EMBREE=ON -DWITH_CYCLES_OPENVDB=ON -DWITH_CYCLES_OPENSUBDIV=OFF \
    -DWITH_CYCLES_OPENIMAGEDENOISE=OFF -DWITH_CYCLES_ALEMBIC=OFF \
    -DWITH_CYCLES_PATH_GUIDING=OFF -DWITH_CYCLES_NANOVDB=OFF
  log "building cycles ($JOBS jobs)"
  ninja -C "$src/build" -j "$JOBS" cycles
  log "installing -> $PREFIX/cycles"
  install -d "$PREFIX/cycles/bin"
  install -m755 "$src/build/bin/cycles" "$PREFIX/cycles/bin/cycles"
  write_exec_wrapper "$wrapper" "$real"   # CPU build links only system libs
  ok "cycles installed; 'cycles' on PATH at $wrapper"
}

# --- OSPRay Studio (against the prebuilt OSPRay SDK) --------------------------
build_ospray() {
  local real="$PREFIX/ospray_studio/bin/ospStudio" wrapper="$BINDIR/ospStudio"
  if already_installed "$real" "$wrapper"; then
    ok "ospray_studio already installed ($real); skipping (FORCE=1 to rebuild)"; return; fi
  apt_install $APT_DEPS_COMMON $APT_DEPS_OSPRAY
  local sdk_tar="$BUILD_ROOT/ospray-${OSPRAY_SDK_VERSION}.tar.gz"
  local sdk_dir="$BUILD_ROOT/ospray-${OSPRAY_SDK_VERSION}.x86_64.linux"
  if [ ! -d "$sdk_dir" ]; then
    if [ ! -f "$sdk_tar" ]; then
      log "downloading OSPRay $OSPRAY_SDK_VERSION SDK"
      curl -fL --retry 3 -o "$sdk_tar.part" "$OSPRAY_SDK_URL" || die "download failed: $OSPRAY_SDK_URL"
      mv -f "$sdk_tar.part" "$sdk_tar"
    fi
    log "verifying SDK checksum"
    local got; got="$(sha256_of "$sdk_tar")"
    [ "$got" = "$OSPRAY_SDK_SHA256" ] || die "OSPRay SDK checksum mismatch
  expected $OSPRAY_SDK_SHA256
  got      $got"
    ok "sha256 $got"
    tar xzf "$sdk_tar" -C "$BUILD_ROOT"
  fi
  local src="$BUILD_ROOT/ospray-studio"
  if [ ! -d "$src/.git" ]; then
    log "cloning OSPRay Studio @ ${OSPRAY_STUDIO_COMMIT:0:12} (archived master)"
    rm -rf "$src"; git clone --recursive "$OSPRAY_STUDIO_REPO" "$src"
    git -C "$src" checkout -q "$OSPRAY_STUDIO_COMMIT"
    git -C "$src" submodule update --init --recursive
  fi
  log "configuring OSPRay Studio against the SDK"
  # find_package(ospray) resolves from the SDK on CMAKE_PREFIX_PATH; Studio
  # FetchContent-builds its own static rkcommon + TBB (it anticipates this).
  cmake -S "$src" -B "$src/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$sdk_dir" \
    -DCMAKE_INSTALL_PREFIX="$PREFIX/ospray_studio" \
    -DBUILD_TESTING=OFF
  log "building ospStudio ($JOBS jobs)"
  ninja -C "$src/build" -j "$JOBS" ospStudio
  log "installing -> $PREFIX/ospray_studio (binary + Studio sg lib + SDK runtime libs)"
  install -d "$PREFIX/ospray_studio/bin" "$PREFIX/ospray_studio/lib"
  install -m755 "$src/build/ospStudio"        "$PREFIX/ospray_studio/bin/ospStudio"
  install -m755 "$src/build/libospray_sg.so"  "$PREFIX/ospray_studio/lib/"
  cp -a "$sdk_dir"/lib/*.so* "$PREFIX/ospray_studio/lib/"
  chmod -R a+rX "$PREFIX/ospray_studio"
  write_exec_wrapper "$wrapper" "$real" "$PREFIX/ospray_studio/lib"
  ok "ospray_studio installed; 'ospStudio' on PATH at $wrapper"
  warn "OSPRay renders the addon's stock 'ospray_standard.sg' with a dim ambient light;"
  warn "its image is correct but low-contrast (see docs/RENDERING.md known limitations)."
}

# --- list ---------------------------------------------------------------------
do_list() {
  printf 'Source-built renderers this script provides (Linux x86_64):\n\n'
  local rows="pbrt:pbrt:pbrt-v4 (CMake, vendored deps)\ncycles:cycles:Cycles %s standalone (CPU, system libs)\nospray:ospStudio:OSPRay Studio (vs OSPRay %s SDK)"
  printf "$rows\n" "$CYCLES_TAG" "$OSPRAY_SDK_VERSION" | while IFS=: read -r key wrap desc; do
    if command -v "$wrap" >/dev/null 2>&1; then
      printf '  %-8s wrapper "%-10s" %-40s [installed: %s]\n' "$key" "$wrap" "$desc" "$(command -v "$wrap")"
    else
      printf '  %-8s wrapper "%-10s" %-40s [not installed]\n' "$key" "$wrap" "$desc"
    fi
  done
  printf '\nPrebuilt renderers (Appleseed, LuxCore) are handled by install-renderers.sh.\n'
}

# --- main ---------------------------------------------------------------------
main() {
  local targets=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --list|-l)   do_list; exit 0 ;;
      --deps-only) platform_guard; apt_install $APT_DEPS_COMMON $APT_DEPS_OSPRAY $APT_DEPS_CYCLES; exit 0 ;;
      -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
      pbrt|cycles|ospray) targets+=("$1") ;;
      appleseed|luxcore) die "$1 has a prebuilt CLI — use scripts/install-renderers.sh" ;;
      *) die "unknown argument: $1 (try --list or --help)" ;;
    esac
    shift
  done
  [ ${#targets[@]} -eq 0 ] && targets=($ALL_KEYS)

  platform_guard
  need_cmd git; need_cmd curl
  mkdir -p "$BUILD_ROOT"
  mkdir -p "$PREFIX" 2>/dev/null || die "cannot create $PREFIX (run with sudo, or set PREFIX=<writable dir>)"
  mkdir -p "$BINDIR" 2>/dev/null || die "cannot create $BINDIR (run with sudo, or set BINDIR=<dir on PATH>)"
  [ -w "$PREFIX" ] || die "$PREFIX not writable (run with sudo, or set PREFIX=<writable dir>)"
  [ -w "$BINDIR" ] || die "$BINDIR not writable (run with sudo, or set BINDIR=<dir on PATH>)"

  local key
  for key in "${targets[@]}"; do
    log "renderer: $key"
    case "$key" in
      pbrt)   build_pbrt ;;
      cycles) build_cycles ;;
      ospray) build_ospray ;;
    esac
  done

  echo
  ok "done. Verify: render_capabilities (MCP) or .venv/bin/python3 tests/test_render_photoreal.py"
  case ":$PATH:" in
    *":$BINDIR:"*) : ;;
    *) warn "$BINDIR is not on PATH — add it in the env that launches 'python -m ankusdrive mcp'" ;;
  esac
}

main "$@"

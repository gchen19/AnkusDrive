#!/usr/bin/env bash
#
# install-freecad-appimage.sh — provision FreeCAD ITSELF on Linux from the official
# upstream AppImage (issue #280). The solver twin is scripts/install-solvers.sh; this
# script handles the one dependency AnkusDrive does not pip-install: FreeCAD.
#
# WHY THIS EXISTS
#   FreeCAD was dropped from Ubuntu 24.04's universe repo, so `apt install freecad`
#   simply has no candidate there. Upstream points at snap and flatpak, and NEITHER
#   works in a container or a sandboxed agent environment (no snapd session bus, no
#   FUSE). That dead-ends the single most common Linux target for AnkusDrive.
#   The path that does work everywhere — verified end to end in the external install
#   report on Ubuntu 24.04 x86_64 (issue #280) — is the release AppImage with
#   `--appimage-extract`: extraction is a pure userspace unpack of the embedded
#   squashfs, so it needs no FUSE, no root, and no snapd. The extracted tree carries
#   FreeCAD's bundled Python, `ccx` (CalculiX) and `gmsh`, so core CAD *and* the
#   structural-FEM families come up from this one download.
#
# WHAT IT DOES
#   1. downloads the PINNED release AppImage for this arch (x86_64 / aarch64)
#   2. verifies its SHA-256 against the checksum upstream publishes beside it
#   3. `--appimage-extract`s it into $PREFIX/squashfs-root   (no FUSE, no root needed)
#   4. symlinks freecadcmd, freecad, ccx and gmsh into $BINDIR
#   5. LIVE-VERIFIES the result: `ankusdrive ping` (boots the worker through the new
#      binary) plus a real CalculiX solve (`ankusdrive fem cantilever`) — the same
#      "prove it on the box, don't just install it" pattern as the macOS molding
#      gates (issue #275, scripts/ci-macos-preflight.sh).
#   Step 4 is a convenience, not a requirement: ankusdrive/client.py also probes
#   $PREFIX/squashfs-root/usr/bin directly for the default prefixes below (#280), so
#   `--no-symlink` still yields a discoverable install.
#
# USAGE
#   scripts/install-freecad-appimage.sh                    # /opt/freecad + /usr/local/bin
#   scripts/install-freecad-appimage.sh --prefix ~/fc      # unpack somewhere else
#   scripts/install-freecad-appimage.sh --no-symlink       # rely on discovery alone
#   scripts/install-freecad-appimage.sh --appimage ./FreeCAD_1.1.3-...AppImage
#                                                          # use a local download
#   scripts/install-freecad-appimage.sh --verify-only      # just re-run the live checks
#   scripts/install-solvers.sh freecad                     # same thing, solver-script entry
#
# ENV OVERRIDES (all also settable as flags where a flag exists)
#   PREFIX     where squashfs-root lands   (default /opt/freecad, else ~/.local/opt/freecad
#              when /opt is not writable and there is no sudo — both are auto-discovered)
#   BINDIR     where the four symlinks go  (default /usr/local/bin, else ~/.local/bin)
#   APPIMAGE   path to an already-downloaded AppImage (skips download; skips the
#              pinned-checksum check unless FREECAD_SHA256 is set)
#   FREECAD_VERSION / FREECAD_URL / FREECAD_SHA256   override the pin below
#   WORKDIR    scratch dir for download+extract (default: beside $PREFIX, same
#              filesystem, so the final move is cheap)
#   KEEP_APPIMAGE=1   don't delete the ~800 MB download after extracting
#   PY         interpreter that runs the live verify (default: ./.venv/bin/python3 if
#              present, else python3 — must be the env AnkusDrive is installed into)
#
# Idempotent: re-running replaces $PREFIX/squashfs-root and re-points the symlinks.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# --- the pin ------------------------------------------------------------------
# Pinned version + per-arch SHA-256, taken from the checksum files upstream publishes
# next to each asset (…-SHA256.txt in the release). A pin (not "latest") is what makes
# this reproducible AND makes the checksum meaningful — a floating download can only
# ever be checked against itself. Bump both together when moving to a new release.
FREECAD_VERSION="${FREECAD_VERSION:-1.1.3}"
SHA256_X86_64="3a853eb69ee595f779f2255dbf80a765926981d8ff68903cefee4dfb03a8f5ef"
SHA256_AARCH64="9a8f9f7f2802bb856f2bb70f53d536e2ae06569f4e6d718407803076104ff55e"

# The four binaries the extracted tree must hand us: freecadcmd is what AnkusDrive
# actually spawns; freecad is the GUI (harmless, and what a human expects to exist);
# ccx and gmsh are FreeCAD's bundled FEM solver and mesher, which is why the FEM
# families work off this single download with nothing else installed.
LINK_BINS="freecadcmd freecad ccx gmsh"

# --- logging (matches install-solvers.sh) -------------------------------------
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s==>%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%swarn%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; exit 1; }

# --- args ---------------------------------------------------------------------
DO_SYMLINK=1
VERIFY_ONLY=0
DO_VERIFY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)      PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --bindir)      BINDIR="${2:?--bindir needs a directory}"; shift 2 ;;
    --appimage)    APPIMAGE="${2:?--appimage needs a file}"; shift 2 ;;
    --version)     FREECAD_VERSION="${2:?--version needs a version}"; shift 2 ;;
    --no-symlink)  DO_SYMLINK=0; shift ;;
    --no-verify)   DO_VERIFY=0; shift ;;     # for offline/CI runs with a stub tree
    --verify-only) VERIFY_ONLY=1; shift ;;
    -h|--help)     sed -n '2,60p' "$0"; exit 0 ;;
    *)             die "unknown argument: $1 (try --help)" ;;
  esac
done

[ "$(uname -s)" = "Linux" ] || die "the AppImage path is Linux-only (macOS: the .app bundle from freecad.org; Windows: the installer / scripts/install-solvers.ps1)"

# --- where things go ----------------------------------------------------------
# Derive the prefix rather than assuming root: /opt is the conventional system spot
# (and what a container runs as root will get), but a plain user without sudo gets
# ~/.local/opt instead. ankusdrive/client.py probes BOTH (#280), so either is
# auto-discovered — the difference is only whether other tools see the symlinks.
_writable_dir() { [ -w "$1" ] || { [ ! -e "$1" ] && [ -w "$(dirname "$1")" ]; }; }

if [ -z "${PREFIX:-}" ]; then
  if _writable_dir /opt; then PREFIX="/opt/freecad"
  else
    PREFIX="$HOME/.local/opt/freecad"
    warn "/opt is not writable — installing to $PREFIX instead"
    warn "(still auto-discovered; re-run with sudo, or --prefix /opt/freecad, for a system-wide install)"
  fi
fi
if [ -z "${BINDIR:-}" ]; then
  if _writable_dir /usr/local/bin; then BINDIR="/usr/local/bin"
  else BINDIR="$HOME/.local/bin"; warn "/usr/local/bin is not writable — symlinking into $BINDIR"; fi
fi
BINDIR="${BINDIR%/}"
PREFIX="${PREFIX%/}"
FC_BIN="$PREFIX/squashfs-root/usr/bin"          # what the extracted AppImage exposes
FREECADCMD="$FC_BIN/freecadcmd"

if [ -z "${PY:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then PY="$REPO_ROOT/.venv/bin/python3"; else PY="python3"; fi
fi

# --- arch -> asset ------------------------------------------------------------
arch_asset() {
  case "$(uname -m)" in
    x86_64|amd64)   ASSET="FreeCAD_${FREECAD_VERSION}-Linux-x86_64-py311.AppImage";  SHA_PIN="$SHA256_X86_64" ;;
    aarch64|arm64)  ASSET="FreeCAD_${FREECAD_VERSION}-Linux-aarch64-py311.AppImage"; SHA_PIN="$SHA256_AARCH64" ;;
    *) die "no official FreeCAD AppImage for $(uname -m) — build from source or use a distro package" ;;
  esac
  URL="${FREECAD_URL:-https://github.com/FreeCAD/FreeCAD/releases/download/${FREECAD_VERSION}/${ASSET}}"
  # A non-default version invalidates the pinned digest, so require the caller to
  # supply the matching one rather than silently checking against the wrong release.
  if [ -n "${FREECAD_SHA256:-}" ]; then SHA_PIN="$FREECAD_SHA256"; fi
  if [ "$FREECAD_VERSION" != "1.1.3" ] && [ -z "${FREECAD_SHA256:-}" ]; then
    SHA_PIN=""
    warn "version $FREECAD_VERSION is not the pinned 1.1.3 — no checksum to verify against"
    warn "(set FREECAD_SHA256=... from the release's ${ASSET}-SHA256.txt)"
  fi
}

sha256_of() {  # file -> digest on stdout (coreutils, or the BSD-ish shasum fallback)
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die "neither sha256sum nor shasum found — cannot verify the download"; fi
}

# --- download + extract -------------------------------------------------------
fetch_and_extract() {
  arch_asset
  # Scratch beside the prefix so the finished squashfs-root is a rename, not a 2 GB
  # cross-filesystem copy (/tmp is frequently a small tmpfs, and 24.10+ makes that
  # the default — an 800 MB download plus its unpacked tree will not fit there).
  local work="${WORKDIR:-$(dirname "$PREFIX")/.ankusdrive-freecad.$$}"
  mkdir -p "$work" || die "cannot create work dir $work (set WORKDIR=...)"
  local img="${APPIMAGE:-}"
  local downloaded=0

  if [ -n "$img" ]; then
    [ -f "$img" ] || die "no such AppImage: $img"
    img="$(cd "$(dirname "$img")" && pwd)/$(basename "$img")"
    log "using local AppImage $img (skipping download)"
  else
    img="$work/$ASSET"
    downloaded=1
    log "download FreeCAD $FREECAD_VERSION ($(uname -m)) -> $img"
    log "  $URL   (~800 MB, this is the slow step)"
    command -v curl >/dev/null 2>&1 \
      || die "curl not found (apt-get install -y curl), or pass --appimage <already-downloaded file>"
    curl -fL --retry 3 --connect-timeout 30 --progress-bar -o "$img" "$URL" \
      || die "download failed: $URL"
  fi

  if [ -n "$SHA_PIN" ]; then
    log "verify SHA-256 against the pin"
    local got; got="$(sha256_of "$img")"
    if [ "$got" != "$SHA_PIN" ]; then
      # Leave the bad file in place only when the user brought it; never keep a
      # download that failed its pin.
      if [ "$downloaded" = 1 ]; then rm -f "$img"; fi
      rm -rf "$work"
      die "SHA-256 mismatch for $ASSET
       expected $SHA_PIN
       got      $got
       (a corrupted download, or not the pinned release — re-run, or set FREECAD_SHA256)"
    fi
    ok "sha256 $got"
  else
    warn "no pinned checksum for this download — skipping integrity check"
  fi

  # --appimage-extract unpacks the embedded squashfs in userspace: no FUSE, no root,
  # no snapd — which is the whole reason this path works in a container (#280).
  chmod +x "$img" 2>/dev/null || true
  log "extract (--appimage-extract: userspace unpack, no FUSE)"
  ( cd "$work" && "$img" --appimage-extract >/dev/null ) \
    || die "--appimage-extract failed (is $img really an AppImage?)"
  [ -x "$work/squashfs-root/usr/bin/freecadcmd" ] \
    || die "extracted tree has no usr/bin/freecadcmd — unexpected AppImage layout"

  log "install -> $PREFIX/squashfs-root"
  mkdir -p "$PREFIX" || die "cannot create $PREFIX (re-run with sudo, or --prefix ~/.local/opt/freecad)"
  rm -rf "$PREFIX/squashfs-root"
  mv "$work/squashfs-root" "$PREFIX/squashfs-root" || die "could not move the extracted tree into $PREFIX"

  if [ "$downloaded" = 1 ] && [ "${KEEP_APPIMAGE:-0}" != "1" ]; then rm -f "$img"; fi
  rm -rf "$work"
  ok "FreeCAD $FREECAD_VERSION extracted to $PREFIX/squashfs-root"
}

# --- symlinks -----------------------------------------------------------------
make_symlinks() {
  mkdir -p "$BINDIR" || die "cannot create $BINDIR (re-run with sudo, or --bindir ~/.local/bin)"
  local missing=""
  for b in $LINK_BINS; do
    if [ -x "$FC_BIN/$b" ]; then
      ln -sfn "$FC_BIN/$b" "$BINDIR/$b" || die "could not symlink $b into $BINDIR"
      ok "$BINDIR/$b -> $FC_BIN/$b"
    else
      missing="$missing $b"
    fi
  done
  # ccx/gmsh missing is survivable (the FEM families degrade cleanly); freecadcmd is not.
  if [ -n "$missing" ]; then warn "not in the extracted tree, skipped:$missing"; fi
  [ -x "$FREECADCMD" ] || die "freecadcmd missing at $FREECADCMD"
  case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) warn "$BINDIR is not on PATH — add it, or rely on AnkusDrive's own discovery of $FC_BIN" ;;
  esac
}

# --- live verify (the #275 pattern: prove it on the box) ----------------------
# An install that merely put files on disk is not evidence. These two checks are the
# ones the install report actually ran: a worker boot through the new binary, and a
# real CalculiX solve — which is what proves the bundled ccx/gmsh came along.
live_verify() {
  local failed=0
  [ -x "$FREECADCMD" ] || die "nothing installed at $FREECADCMD — run without --verify-only first"

  # PATH: so FreeCAD's FEM workbench finds the bundled ccx/gmsh even when BINDIR is
  # not on the caller's PATH. ANKUSDRIVE_FREECADCMD: verify THIS install, never some
  # other FreeCAD that happens to be on PATH.
  export PATH="$BINDIR:$FC_BIN:$PATH"
  export ANKUSDRIVE_FREECADCMD="$FREECADCMD"

  if ! "$PY" -c "import ankusdrive" >/dev/null 2>&1; then
    warn "ankusdrive is not importable in $PY — skipping the live verify"
    warn "install it first:  $PY -m pip install -e $REPO_ROOT   (then re-run with --verify-only)"
    return 0
  fi

  log "verify 1/2: ankusdrive ping  (boots a worker through the new freecadcmd)"
  if ( cd "$REPO_ROOT" && "$PY" -m ankusdrive ping ); then ok "worker reached FreeCAD"
  else fail_hint="ping"; failed=1; fi

  log "verify 2/2: ankusdrive fem cantilever  (a real CalculiX solve via the bundled ccx)"
  if ( cd "$REPO_ROOT" && "$PY" -m ankusdrive fem cantilever --mesh-size 800 ); then
    ok "CalculiX solved through the extracted tree"
  else
    warn "the CalculiX smoke failed — core CAD may still be fine; check 'ccx -v' and 'ankusdrive doctor'"
    failed=1
  fi

  if [ "$failed" != 0 ]; then
    die "live verify FAILED (${fail_hint:-fem}) — the install is on disk but not usable; run '$PY -m ankusdrive doctor' for the per-item report"
  fi
  ok "live verify passed"
}

# --- main ---------------------------------------------------------------------
if [ "$VERIFY_ONLY" != 1 ]; then
  log "FreeCAD $FREECAD_VERSION AppImage -> prefix=$PREFIX bindir=$BINDIR"
  fetch_and_extract
  if [ "$DO_SYMLINK" = 1 ]; then make_symlinks
  else ok "--no-symlink: relying on AnkusDrive's discovery of $FC_BIN (#280)"; fi
fi
if [ "$DO_VERIFY" = 1 ]; then live_verify; fi

echo
ok "FreeCAD ready: $FREECADCMD"
ok "next:  $PY -m ankusdrive doctor       # per-item FreeCAD + solver checklist"
ok "       scripts/install-solvers.sh   # the optional simulation solvers"

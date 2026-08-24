#!/usr/bin/env bash
# Set up THIS machine to run the AnkusDrive test suite locally — the local
# replacement for the GitHub Actions test runner that used to live in
# .github/workflows/test.yml.
#
# It REUSES an already-installed FreeCAD 1.1 AppImage instead of downloading
# anything, mirroring what CI did with a cached conda env:
#
#   - freecadcmd : symlinked onto PATH (~/.local/bin) so ankusdrive's worker
#                  resolves FreeCAD via shutil.which() — no env var needed.
#   - .venv      : .venv/bin/python3 -> FreeCAD's *bundled* python, which already
#                  ships numpy + Pillow. That feeds run_all.sh's two-interpreter
#                  split (system python3 for the worker tests, .venv python3 for
#                  the numpy/Pillow tests) without a second download.
#
# Idempotent — safe to re-run. Overrides:
#   FREECAD_HOME       an extracted .../squashfs-root/usr to use as-is
#   ANKUSDRIVE_APPIMAGE  a specific FreeCAD*.AppImage to extract
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
LOCALBIN="$HOME/.local/bin"

# --- 1. locate FreeCAD: prefer an already-extracted tree, else extract an AppImage ---
find_extracted() {
  for d in "$HOME"/Applications/FreeCAD*-extracted/squashfs-root/usr \
           "$HOME"/Applications/*/squashfs-root/usr; do
    [ -x "$d/bin/freecadcmd" ] && { echo "$d"; return 0; }
  done
  return 1
}
find_appimage() {
  for f in "${ANKUSDRIVE_APPIMAGE:-}" \
           "$HOME"/Applications/FreeCAD*.AppImage \
           "$HOME"/Downloads/FreeCAD*.AppImage; do
    [ -n "$f" ] && [ -f "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

USR="${FREECAD_HOME:-}"
if [ -n "$USR" ]; then
  echo "Using FREECAD_HOME: $USR"
elif USR=$(find_extracted); then
  echo "Using extracted FreeCAD: $USR"
elif APP=$(find_appimage); then
  DEST="${APP%.AppImage}-extracted"
  echo "Extracting FreeCAD AppImage (one-time, no download): $APP -> $DEST"
  mkdir -p "$DEST"
  ( cd "$DEST" && "$APP" --appimage-extract >/dev/null )
  USR="$DEST/squashfs-root/usr"
else
  echo "ERROR: no FreeCAD found. Install FreeCAD 1.1 (drop the AppImage in" >&2
  echo "       ~/Applications or ~/Downloads), or set FREECAD_HOME to an" >&2
  echo "       extracted .../squashfs-root/usr directory." >&2
  exit 1
fi

FREECADCMD="$USR/bin/freecadcmd"
PYBUNDLE="$USR/bin/python"
[ -x "$FREECADCMD" ] || { echo "ERROR: $FREECADCMD missing/not executable" >&2; exit 1; }
[ -x "$PYBUNDLE" ]   || { echo "ERROR: $PYBUNDLE missing/not executable"   >&2; exit 1; }

# --- 2. put freecadcmd on PATH (ankusdrive resolves it via shutil.which) ---
mkdir -p "$LOCALBIN"
ln -sfn "$FREECADCMD" "$LOCALBIN/freecadcmd"
echo "Linked $LOCALBIN/freecadcmd -> $FREECADCMD"

# --- 3. .venv/bin/python3 -> FreeCAD's bundled python (already has numpy + Pillow) ---
mkdir -p "$REPO/.venv/bin"
ln -sfn "$PYBUNDLE" "$REPO/.venv/bin/python3"
echo "Linked $REPO/.venv/bin/python3 -> $PYBUNDLE"

# --- 4. verify ---
echo
echo "== verify =="
if command -v freecadcmd >/dev/null; then
  echo "  freecadcmd on PATH: $(command -v freecadcmd)"
else
  echo "  WARN: freecadcmd not on PATH — add $LOCALBIN to PATH, or export"
  echo "        ANKUSDRIVE_FREECADCMD=$FREECADCMD before running the suite."
fi
"$REPO/.venv/bin/python3" - <<'PY'
import sys, numpy, PIL
print(f"  .venv python {sys.version.split()[0]}  numpy {numpy.__version__}  Pillow {PIL.__version__}")
PY
python3 - <<'PY'
import shutil
print("  ankusdrive resolves freecadcmd ->", shutil.which("freecadcmd") or "NOT FOUND (check PATH)")
PY
echo
echo "Setup complete. Run the suite with:  bash tests/run_all.sh"

#!/usr/bin/env bash
#
# write_solver_manifest.sh — record what THIS image contains, at /etc/ankusdrive/solvers.json (#422 part C).
#
# WHY THIS EXISTS
#   Under ANKUSDRIVE_SUBSTRATE=container the host cannot see inside the container, so
#   an in-container path is trusted rather than stat'd. That is safe while every image
#   carries every solver. Once an image can legitimately omit one (#422 part B), the
#   same trust turns into the worst failure shape there is: `doctor` says
#   "ready via yade (in container)" and the solve dies minutes later.
#
#   So the image states its own contents, and AnkusDrive reads that first. A solver
#   listed here is present at the recorded path; one listed under "excluded" is
#   absent BY DESIGN, with the reason and the fix. An image with no manifest at all
#   (anything built before this landed) is handled by probing, exactly as before.
#
# The file is data, not instructions: AnkusDrive reads paths and reasons from it and
# never executes anything it names.
#
# The file also records HOW the image was built (BUILT_SOURCE / BUILT_COMMIT): a local
# build stamps the checkout it came from, so `ankusdrive doctor` can say "the image you
# built here, unsigned" rather than warning about an anonymous one (#423). This is the
# image describing ITSELF — anything can write it, so it is only ever used to phrase a
# message. Authenticity comes from the signature over the published digest, never here.
#
# USAGE
#   write_solver_manifest.sh <arch> <selected solver>...
#   SELECTED="openfoam fsi" write_solver_manifest.sh amd64 $SELECTED
#
# Every solver the image COULD carry is listed either way, so "absent from the file"
# never has to mean anything — an unknown solver is simply not this image's business.
set -eu

out="${MANIFEST_PATH:-/etc/ankusdrive/solvers.json}"
arch="${1:?usage: $0 <arch> <solver>...}"
shift
selected=" $* "

# solver | in-container path it resolves to | what it is
# The paths must match what the Dockerfile's ENV publishes; test_slim_image.py pins that.
rows() {
  cat <<'EOF'
openfoam|/usr/lib/openfoam/openfoam2512/platforms/current/bin/simpleFoam|ESI OpenFOAM 2512 (CFD, and the FSI fluid participant)
elmer|/opt/elmer/bin/ElmerSolver|Elmer FEM (transient/radiation thermal, CHT, low-frequency EM, acoustic + harmonic FEM)
yade|/opt/yade/bin/yade|YADE (granular DEM)
openems|/opt/venv-openems/bin/python|openEMS FDTD (full-wave EM), in its own venv
fsi|/opt/fsi/calculix-adapter/bin/ccx_preCICE|the preCICE FSI stack (ccx_preCICE + both adapters)
oims|/opt/of7/OpenFOAM/site/7/platforms/current/bin/openInjMoldSim|openInjMoldSim on OpenFOAM-7 (injection-molding fill/pack)
bempp|/opt/venv-bempp/bin/python|Bempp-cl (acoustic BEM), in its own venv
EOF
}

mkdir -p "$(dirname "$out")"
{
  echo '{'
  echo '  "schema": 1,'
  echo "  \"architecture\": \"$arch\","
  echo '  "solvers": {'
  first=1
  while IFS='|' read -r name path desc; do
    case "$selected" in *" $name "*) ;; *) continue ;; esac
    [ "$first" = 1 ] || echo ','
    first=0
    printf '    "%s": {"path": "%s", "description": "%s"}' "$name" "$path" "$desc"
  done <<EOF
$(rows)
EOF
  echo
  echo '  },'
  echo '  "excluded": {'
  first=1
  while IFS='|' read -r name path desc; do
    case "$selected" in *" $name "*) continue ;; *) ;; esac
    [ "$first" = 1 ] || echo ','
    first=0
    printf '    "%s": {"reason": "not selected when this image was built"}' "$name"
  done <<EOF
$(rows)
EOF
  echo
  echo '  },'
  printf '  "built": {"source": "%s"' "${BUILT_SOURCE:-local}"
  [ -n "${BUILT_COMMIT:-}" ] && printf ', "commit": "%s"' "$BUILT_COMMIT"
  [ -n "${BUILT_WORKFLOW:-}" ] && printf ', "workflow": "%s"' "$BUILT_WORKFLOW"
  printf '}\n'
  echo '}'
} > "$out"

# A malformed manifest would be worse than none: AnkusDrive falls back to probing when
# it cannot parse one, so a silent typo here would quietly undo the whole mechanism.
if command -v python3 >/dev/null 2>&1; then
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['solvers'] or d['excluded']" "$out"
fi
echo "wrote $out"

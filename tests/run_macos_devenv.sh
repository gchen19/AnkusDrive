#!/usr/bin/env bash
# The discovery suites on a Mac set up the way docs/MACOS.md tells a developer to set
# one up (#314). Three leaks in a row -- #313 (config.toml layer), #349 (the Multipass
# branch of find_solver), #314 (the report of #349 from the degradation suite) -- all
# had the same shape: a test's "no solver present" premise quietly read the real
# machine, so the suite was green on CI and red on exactly the Macs that followed our
# own setup guide. The Linux lanes fake that machine by monkeypatching; this lane
# BUILDS it, with nothing patched:
#
#   * `multipass` on PATH is tests/ci/multipass, a real executable that answers
#     `multipass info openfoam --format json` with a Running VM -- so
#     multipass_available(), shutil.which and the `info` subprocess all run for real;
#   * the OpenFOAM override is declared in BOTH layers the guide uses: the exports
#     (env) and ~/.config/ankusdrive/config.toml (file, via ANKUSDRIVE_CONFIG).
#
# The premise is checked first: unforced, OpenFOAM must resolve via the VM and the cfd
# family must read available. If it does not, the lane is not testing anything and
# fails rather than going vacuously green.
#
# No FreeCAD needed. Run it on a hosted macos-latest runner (test.yml) or locally:
#   bash tests/run_macos_devenv.sh [test files...]
set -uo pipefail
cd "$(dirname "$0")/.."

if [ "$(uname -s)" != "Darwin" ]; then
  echo "run_macos_devenv.sh: Darwin only (the Multipass branch is macOS-gated)" >&2
  exit 2
fi

PY="${PYTHON:-python3}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/ankusdrive-devenv.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT

# In-VM paths from docs/MACOS.md. macOS never stats them; they only have to be
# declared for the Multipass branch to trust them.
FOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
FOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam

cat > "$SCRATCH/config.toml" <<EOF
[solvers]
openfoam_bashrc = "$FOAM_BASHRC"
openfoam_path = "$FOAM_PATH"
EOF

export ANKUSDRIVE_CONFIG="$SCRATCH/config.toml"
export ANKUSDRIVE_OPENFOAM_BASHRC="$FOAM_BASHRC"
export ANKUSDRIVE_OPENFOAM_PATH="$FOAM_PATH"
export PATH="$PWD/tests/ci:$PATH"
export MULTIPASS_SHIM_STATE=Running
export MULTIPASS_SHIM_MOUNTS="$SCRATCH"
unset RUN_HEAVY_SOLVES ANKUSDRIVE_SUBSTRATE

echo "== premise: this machine looks like a docs/MACOS.md developer Mac =="
"$PY" - <<'EOF' || { echo "premise FAILED: the lane would test nothing" >&2; exit 1; }
import shutil
from ankusdrive import solvers
mp = shutil.which("multipass")
assert mp and mp.endswith("tests/ci/multipass"), ("shim not first on PATH", mp)
assert solvers.multipass_available(), "multipass substrate not selected"
assert solvers.multipass_vm_state() == "running", solvers.multipass_vm_state()
foam = solvers.find_solver("openfoam")
assert foam.get("available") and foam.get("via") == "multipass", foam
cfd = solvers.capabilities()["families"]["cfd"]
assert "openfoam" in cfd["available"] and cfd["any_available"], cfd
print("  multipass  ->", mp)
print("  openfoam   ->", foam.get("path"), "(via multipass)")
print("  cfd family ->", cfd["available"])
EOF

if [ "$#" -gt 0 ]; then
  FILES=("$@")
else
  # Every suite that asserts a solver or family is absent/unwired and needs no
  # FreeCAD. A new such suite belongs here too.
  FILES=(tests/test_solve_degradation.py tests/test_wsl_routing.py
         tests/test_macos_relay_shim.py tests/test_toolsets.py)
fi

FAILED=""
for f in "${FILES[@]}"; do
  echo
  echo "== $f =="
  if "$PY" "$f"; then
    echo "-- $f PASSED"
  else
    echo "-- $f FAILED"
    FAILED="$FAILED $f"
  fi
done

echo
if [ -z "$FAILED" ]; then
  echo "macOS dev-env discovery: ALL PASSED (${#FILES[@]} file(s))"
  exit 0
fi
echo "macOS dev-env discovery FAILED:$FAILED" >&2
exit 1

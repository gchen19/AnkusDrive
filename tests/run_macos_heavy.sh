#!/usr/bin/env bash
# Run the macOS-reachable HEAVY solver regressions (issue #220) — the Apple-Silicon
# lane of .github/workflows/heavy-solves.yml.
#
# WHY A SUBSET AND NOT run_all.sh
#   The Linux heavy lane runs the whole suite with RUN_HEAVY_SOLVES=1 because every
#   heavy solver is installed on that box. On macOS most of them are not reachable at
#   all (Elmer has no macOS binaries, YADE/openEMS/bempp have no macOS build path here
#   — see the per-solver table in docs/MACOS.md), so a full run would spend 90 minutes
#   re-confirming skips. This lane runs exactly the paths that are macOS-SPECIFIC and
#   therefore untested by the Linux lane:
#
#     tests/test_wsl_routing.py   the Darwin substrate routing contracts (bash_argv ->
#                                 `multipass exec`, runs_in_substrate, _fsi_override's
#                                 in-VM path trust, the FSI participant routing) — pure
#                                 Python, seconds, and the first thing a regression breaks
#     tests/test_su2_native.py    the LIVE SU2 channel solve: the official x86_64
#                                 binary under Rosetta 2 through solvers.run_argvs
#     tests/test_fsi.py           the LIVE preCICE OpenFOAM<->CalculiX coupled solve,
#                                 executed inside the Multipass VM
#     tests/test_openfoam.py      the LIVE built-in CFD cases (Hagen-Poiseuille, D^4,
#                                 Blasius, Colebrook) on the in-VM OpenFOAM (#223)
#     tests/test_meshbridge.py    the LIVE snappyHexMesh bridge + the virtual wind
#                                 tunnel gated on the sphere drag curve (#223/#224)
#     tests/test_wind_tunnel.py   the same tunnel end to end, through the real
#                                 cfd_external_flow_submit handler and job registry —
#                                 including the TURBULENT oracle (#262): a cube face-on
#                                 at Re=1e4/1e5 vs the bluff-body Cd table, which is
#                                 what makes kOmegaSST report gated:true
#
#     tests/test_su2_case.py      the NATIVE SU2 plane-Poiseuille gate (#237 item 3) —
#                                 no VM at all, so it is the one CFD path that still
#                                 works if Multipass is down
#     tests/test_molding_fill.py  the LIVE interFoam cavity-fill gates in the VM
#                                 (#193's last checkbox): a fillable cavity reaches the
#                                 far end, a short shot stalls. The openInjMoldSim
#                                 (OF7-org) tests inside it run too where the VM has the
#                                 arm64 source build (#276, tools/build_openinjmoldsim.sh
#                                 + the ANKUSDRIVE_OPENINJMOLDSIM exports) and SKIP where
#                                 it doesn't
#
#   Add more files as arguments once their in-VM provisioning is validated:
#
#     bash tests/run_macos_heavy.sh tests/test_some_new_gate.py
#
# Every file runs even if an earlier one fails (unlike run_all.sh's `set -e`), so one
# flaky coupled solve does not hide an SU2 regression; the exit code is non-zero if any
# failed and the summary names them.
#
# USAGE
#   bash tests/run_macos_heavy.sh                 # preflight + the default set
#   bash tests/run_macos_heavy.sh <file>...       # preflight + just these files
#   ANKUSDRIVE_SKIP_PREFLIGHT=1 bash tests/run_macos_heavy.sh
#       skip the substrate health check — the CI job runs it as its own step so a
#       substrate failure is attributed there instead of to the solves.
set -uo pipefail
cd "$(dirname "$0")/.."

# The gate tests/heavy_solve.py reads; without it every live solve SKIPs and this
# lane would be a no-op that passes.
export RUN_HEAVY_SOLVES=1

if [ "${ANKUSDRIVE_SKIP_PREFLIGHT:-}" != "1" ]; then
  bash scripts/ci-macos-preflight.sh || {
    echo
    echo "Aborting: the macOS heavy-solve substrate is not healthy (see above)." >&2
    echo "The solves would fail deep inside OpenFOAM/preCICE with an unrelated-looking error." >&2
    exit 1
  }
  echo
fi

if [ "$#" -gt 0 ]; then
  FILES=("$@")
else
  FILES=(tests/test_wsl_routing.py tests/test_su2_native.py tests/test_su2_case.py
         tests/test_fsi.py tests/test_openfoam.py tests/test_meshbridge.py
         tests/test_wind_tunnel.py tests/test_molding_fill.py)
fi

FAILED=""
for f in "${FILES[@]}"; do
  echo
  echo "== $f =="
  if python3 "$f"; then
    echo "-- $f PASSED"
  else
    echo "-- $f FAILED"
    FAILED="$FAILED $f"
  fi
done

echo
if [ -z "$FAILED" ]; then
  echo "macOS heavy solves: ALL PASSED (${#FILES[@]} file(s))"
  exit 0
fi
echo "macOS heavy solves FAILED:$FAILED" >&2
exit 1

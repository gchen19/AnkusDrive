#!/usr/bin/env bash
#
# build_openinjmoldsim.sh — DOCUMENTED, NOT-RUN-BY-DEFAULT build recipe for the
# openInjMoldSim injection-molding solver (GitHub issue #105).
#
# WHAT THIS IS
#   openInjMoldSim (https://github.com/krebeljk/openInjMoldSim, GPL-3.0) is the only
#   purpose-built, peer-reviewed open-source injection-molding fill/pack/cool solver
#   (Krebelj et al., MDPI Fluids 5(2):84, 2020). It is a modified compressibleInterFoam
#   (VOF melt+air) with Cross-WLF viscosity + Tait EOS — the HIGH-FIDELITY twin behind
#   driftpin's molding_fill_submit. Until this build lands, molding_fill_submit runs the
#   runnable FALLBACK: a 2-D interFoam VOF cavity fill on the existing OpenFOAM
#   (.com/ESI) — see driftpin/analysis/molding_fill.py. That fallback answers the
#   strongest gate (short-shot/fill) but loses the packing stage + the published IM
#   validation; THIS path restores them.
#
# THE HARD CONSTRAINT — OpenFOAM VERSION
#   openInjMoldSim targets OpenFOAM 7 (.ORG / openfoam.org) via its ./Allwmake. This
#   host builds OpenFOAM v19xx/v25xx (.COM / ESI). The two forks are NOT drop-in
#   compatible, so this needs a PARALLEL OpenFOAM-7 (.org) build alongside the ESI one
#   — a multi-hour, host-heavy compile. It is therefore NOT part of the default
#   install-solvers.sh run, NOT executed in CI by default, and this script REFUSES to
#   build unless you pass --build (a dry run otherwise just prints the plan).
#
# GUARDRAIL (the solver-campaign memo): OTHER AGENTS SHARE THIS HOST. Every compile is
#   capped at -j${JOBS:-8} (never -j$(nproc)) + ccache. Do not raise JOBS on the shared
#   runner.
#
# AFTER A SUCCESSFUL BUILD — how the worker switches to it
#   driftpin/solvers.py resolves the binary side-effect-free:
#     solvers.openinjmoldsim_bin()    -> DRIFTPIN_OPENINJMOLDSIM[_PATH] env
#                                        -> PATH (which openInjMoldSim)
#                                        -> ~/opt/openInjMoldSim/*/bin/openInjMoldSim
#                                        -> ~/OpenFOAM/*/platforms/*/bin/openInjMoldSim
#     solvers.openinjmoldsim_bashrc() -> DRIFTPIN_OPENINJMOLDSIM_BASHRC env
#                                        -> ~/OpenFOAM/OpenFOAM-7/etc/bashrc (et al.)
#   When openinjmoldsim_bin() resolves AND molding_fill_submit is called with a prepared
#   OF7-org `case_dir`, the worker runs openInjMoldSim (GPL, subprocess boundary —
#   never imported) instead of the interFoam fallback. Export, then verify:
#     export DRIFTPIN_OPENINJMOLDSIM="$PREFIX/openInjMoldSim/.../bin/openInjMoldSim"
#     export DRIFTPIN_OPENINJMOLDSIM_BASHRC="$OF7_DIR/etc/bashrc"
#     python3 -c "from driftpin import solvers; print(solvers.openinjmoldsim_bin())"
#
# USAGE
#   tools/build_openinjmoldsim.sh              # DRY RUN: print the plan + preconditions
#   tools/build_openinjmoldsim.sh --build      # actually build (long; capped -j8)
#   JOBS=4 tools/build_openinjmoldsim.sh --build
#
set -euo pipefail

JOBS="${JOBS:-8}"
PREFIX="${PREFIX:-$HOME/opt}"
OF7_VERSION="${OF7_VERSION:-7}"
OF7_DIR="$HOME/OpenFOAM/OpenFOAM-${OF7_VERSION}"
OIMS_TAG="${OIMS_TAG:-v7.2}"          # last released tag; the repo is dormant (2021)
OIMS_REPO="https://github.com/krebeljk/openInjMoldSim.git"
DRY_RUN=1
[[ "${1:-}" == "--build" ]] && DRY_RUN=0

say()  { printf '\033[1;36m[oims]\033[0m %s\n' "$*"; }
plan() { printf '   $ %s\n' "$*"; }

cat <<EOF
============================================================================
 openInjMoldSim (GPL-3.0) build — OpenFOAM-${OF7_VERSION} (.org) parallel stack
 JOBS=${JOBS}  PREFIX=${PREFIX}  OIMS_TAG=${OIMS_TAG}
 mode: $([[ $DRY_RUN == 1 ]] && echo 'DRY RUN (no compile)' || echo 'BUILD')
============================================================================
EOF

# --- preconditions -----------------------------------------------------------
say "preconditions"
MISSING=0
need() { command -v "$1" >/dev/null 2>&1 || { echo "   MISSING: $1 ($2)"; MISSING=1; }; }
need git    "clone openInjMoldSim + OpenFOAM-7"
need cmake  "OpenFOAM-7 / ThirdParty build"
need g++    "C++ compiler"
need gfortran "scotch / metis"
need make   "build driver"
command -v ccache >/dev/null 2>&1 || echo "   RECOMMENDED: ccache (faster rebuilds)"
[[ $MISSING == 0 ]] && echo "   ok — core toolchain present" || \
  echo "   install the MISSING tools before --build (apt: build-essential cmake git gfortran)"

# --- the plan ----------------------------------------------------------------
say "build plan (OpenFOAM-7 .org is the linchpin; capped -j${JOBS})"
cat <<'PLANEOF'
   1. Build (or locate) OpenFOAM 7 (.org) — the version openInjMoldSim targets.
      Do NOT reuse the ESI v19xx/v25xx tree; it is a different fork.
PLANEOF
plan "git clone -b version-${OF7_VERSION} https://github.com/OpenFOAM/OpenFOAM-${OF7_VERSION}.git ${OF7_DIR}"
plan "git clone -b version-${OF7_VERSION} https://github.com/OpenFOAM/ThirdParty-${OF7_VERSION}.git $HOME/OpenFOAM/ThirdParty-${OF7_VERSION}"
plan "source ${OF7_DIR}/etc/bashrc"
plan "( cd \$WM_THIRD_PARTY_DIR && ./Allwmake -j${JOBS} > log.tp 2>&1 )"
plan "( cd ${OF7_DIR} && export WM_NCOMPPROCS=${JOBS} && ./Allwmake -j${JOBS} > log.of 2>&1 )"
echo
cat <<'PLANEOF'
   2. Clone openInjMoldSim and build its solver libs/apps against OF-7 (Allwmake).
      openInjMoldSim (filling+pack+cool) and openInjMoldSimF (fiber orientation).
PLANEOF
plan "git clone ${OIMS_REPO} ${PREFIX}/openInjMoldSim && cd ${PREFIX}/openInjMoldSim && git checkout ${OIMS_TAG}"
plan "source ${OF7_DIR}/etc/bashrc"
plan "export WM_NCOMPPROCS=${JOBS}"
plan "( cd ${PREFIX}/openInjMoldSim && ./Allwmake -j${JOBS} > log.oims 2>&1 )"
echo
cat <<PLANEOF
   3. Expose to driftpin (see solvers.openinjmoldsim_bin / _bashrc):
PLANEOF
plan "export DRIFTPIN_OPENINJMOLDSIM=\$FOAM_USER_APPBIN/openInjMoldSim"
plan "export DRIFTPIN_OPENINJMOLDSIM_BASHRC=${OF7_DIR}/etc/bashrc"
plan "python3 -c 'from driftpin import solvers; print(solvers.openinjmoldsim_bin())'"
echo
cat <<'PLANEOF'
   4. Validate (oracle-gated, per the solver-campaign discipline): reproduce the MDPI
      rectangular-cavity-with-insert / dogbone tutorial shipped in the repo and assert
      flow-front + cavity-pressure agreement within the published tolerance. Then point
      molding_fill_submit at that prepared OF7-org case_dir.
PLANEOF

if [[ $DRY_RUN == 1 ]]; then
  echo
  say "DRY RUN — nothing built. Re-run with --build to execute the plan above."
  say "remember: this is a multi-hour, host-heavy compile; OTHER AGENTS SHARE THE HOST."
  exit 0
fi

# --- actual build (opt-in) ---------------------------------------------------
say "BUILD requested (--build). Capped at -j${JOBS}."
[[ $MISSING == 0 ]] || { echo "refusing to build: install the MISSING tools first"; exit 1; }
export MAKEFLAGS="-j${JOBS}"
export WM_NCOMPPROCS="${JOBS}"
command -v ccache >/dev/null 2>&1 && export WM_COMPILER_TYPE=system

mkdir -p "$HOME/OpenFOAM" "${PREFIX}"

if [[ ! -f "${OF7_DIR}/etc/bashrc" ]]; then
  say "cloning + building OpenFOAM-${OF7_VERSION} (.org) — this is the long part"
  git clone -b "version-${OF7_VERSION}" \
    "https://github.com/OpenFOAM/OpenFOAM-${OF7_VERSION}.git" "${OF7_DIR}"
  git clone -b "version-${OF7_VERSION}" \
    "https://github.com/OpenFOAM/ThirdParty-${OF7_VERSION}.git" \
    "$HOME/OpenFOAM/ThirdParty-${OF7_VERSION}"
  # OF-7's etc/bashrc reads unguarded vars ($ZSH_NAME) — dies under our set -u
  set +u
  # shellcheck disable=SC1091
  source "${OF7_DIR}/etc/bashrc"
  set -u
  ( cd "$WM_THIRD_PARTY_DIR" && ./Allwmake -j"${JOBS}" )
  ( cd "${OF7_DIR}" && ./Allwmake -j"${JOBS}" )
else
  say "OpenFOAM-${OF7_VERSION} already present at ${OF7_DIR} — reusing"
  set +u
  # shellcheck disable=SC1091
  source "${OF7_DIR}/etc/bashrc"
  set -u
fi

if [[ ! -d "${PREFIX}/openInjMoldSim" ]]; then
  git clone "${OIMS_REPO}" "${PREFIX}/openInjMoldSim"
fi
( cd "${PREFIX}/openInjMoldSim" && git checkout "${OIMS_TAG}" && ./Allwmake -j"${JOBS}" )

say "build done. Export the env vars (see step 3) and verify with:"
plan "python3 -c 'from driftpin import solvers; print(solvers.openinjmoldsim_bin())'"

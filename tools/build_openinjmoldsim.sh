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
#   ankusdrive's molding_fill_submit. Until this build lands, molding_fill_submit runs the
#   runnable FALLBACK: a 2-D interFoam VOF cavity fill on the existing OpenFOAM
#   (.com/ESI) — see ankusdrive/analysis/molding_fill.py. That fallback answers the
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
#   ankusdrive/solvers.py resolves the binary side-effect-free:
#     solvers.openinjmoldsim_bin()    -> ANKUSDRIVE_OPENINJMOLDSIM[_PATH] env
#                                        -> PATH (which openInjMoldSim)
#                                        -> ~/opt/openInjMoldSim/*/bin/openInjMoldSim
#                                        -> ~/OpenFOAM/*/platforms/*/bin/openInjMoldSim
#     solvers.openinjmoldsim_bashrc() -> ANKUSDRIVE_OPENINJMOLDSIM_BASHRC env
#                                        -> ~/OpenFOAM/OpenFOAM-7/etc/bashrc (et al.)
#   When openinjmoldsim_bin() resolves AND molding_fill_submit is called with a prepared
#   OF7-org `case_dir`, the worker runs openInjMoldSim (GPL, subprocess boundary —
#   never imported) instead of the interFoam fallback. Export, then verify:
#     export ANKUSDRIVE_OPENINJMOLDSIM="$PREFIX/openInjMoldSim/.../bin/openInjMoldSim"
#     export ANKUSDRIVE_OPENINJMOLDSIM_BASHRC="$OF7_DIR/etc/bashrc"
#     python3 -c "from ankusdrive import solvers; print(solvers.openinjmoldsim_bin())"
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
is_arm64() { case "$(uname -m)" in aarch64|arm64) return 0 ;; *) return 1 ;; esac; }

# --- arm64 (issue #276) ------------------------------------------------------
# OpenFOAM-7 (2019) predates AArch64: no linuxArm64 wmake rules, and no aarch64
# case in etc/config.sh/settings — `uname -m` falls through to "Unknown
# processor" and WM_OPTIONS never forms. OpenFOAM-8 added both. Its
# linuxArm64Gcc rules are OF-7's own linux64Gcc with exactly two changes
# (full-diff-verified live, #276): -m64 dropped, -mcpu=native added to c++Opt —
# so we synthesize them from the local tree instead of fetching OF-8. The
# compiler is pinned to gcc-11: the 2019 sources are unproven against gcc-13
# (the noble default), and gcc-11 is what built this tree on x86_64/WSL (#193)
# and on the Multipass VM (#276).
arm64_enable_of7() {
  is_arm64 || return 0
  local rules="${OF7_DIR}/wmake/rules"
  if [[ ! -d "${rules}/linuxArm64Gcc" ]]; then
    say "arm64: synthesizing wmake/rules/linuxArm64Gcc from linux64Gcc (gcc-11 pin)"
    cp -r "${rules}/linux64Gcc" "${rules}/linuxArm64Gcc"
    sed -i 's/ -m64//g' "${rules}/linuxArm64Gcc/c" "${rules}/linuxArm64Gcc/c++"
    sed -i 's/^cc          = gcc$/cc          = gcc-11/' "${rules}/linuxArm64Gcc/c"
    sed -i 's/^CC          = g++ /CC          = g++-11 /' "${rules}/linuxArm64Gcc/c++"
    sed -i 's/^c++OPT      = -O3$/c++OPT      = -O3 -mcpu=native/' \
      "${rules}/linuxArm64Gcc/c++Opt"
  fi
  local settings="${OF7_DIR}/etc/config.sh/settings"
  if ! grep -q 'aarch64' "${settings}"; then
    say "arm64: adding the aarch64 case to etc/config.sh/settings"
    # insert before the unknown-processor catch-all (the first 4-space `*)`)
    awk '
      /^    \*\)$/ && !done {
        print "    aarch64)"
        print "        WM_ARCH=linuxArm64"
        print "        export WM_COMPILER_LIB_ARCH=64"
        print "        export WM_CC=gcc-11"
        print "        export WM_CXX=g++-11"
        print "        export WM_CFLAGS=-fPIC"
        print "        export WM_CXXFLAGS='\''-fPIC -std=c++0x'\''"
        print "        export WM_LDFLAGS="
        print "        ;;"
        print ""
        done = 1
      }
      { print }
    ' "${settings}" > "${settings}.new" && mv "${settings}.new" "${settings}"
  fi
}

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
if is_arm64; then
  # the arm64 path pins the compiler — see arm64_enable_of7 above
  need gcc-11 "OF-7 arm64 compiler pin (apt: gcc-11 g++-11)"
  need g++-11 "OF-7 arm64 compiler pin (apt: gcc-11 g++-11)"
fi
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
if is_arm64; then
  plan "arm64_enable_of7   # synthesize linuxArm64Gcc wmake rules + aarch64 settings case, gcc-11 pin (#276)"
fi
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
plan "( cd ${PREFIX}/openInjMoldSim/applications/solvers/multiphase/openInjMoldSim && ./Allwmake > log.oims 2>&1 )"
echo
cat <<PLANEOF
   3. Expose to ankusdrive (see solvers.openinjmoldsim_bin / _bashrc):
PLANEOF
plan "export ANKUSDRIVE_OPENINJMOLDSIM=\$FOAM_USER_APPBIN/openInjMoldSim"
plan "export ANKUSDRIVE_OPENINJMOLDSIM_BASHRC=${OF7_DIR}/etc/bashrc"
plan "python3 -c 'from ankusdrive import solvers; print(solvers.openinjmoldsim_bin())'"
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
  arm64_enable_of7
  # OF-7's etc/bashrc reads unguarded vars ($ZSH_NAME) — dies under our set -u —
  # and exits non-zero, which set -e would turn fatal (verified live, #193)
  set +u
  # shellcheck disable=SC1091
  source "${OF7_DIR}/etc/bashrc" || true
  set -u
  ( cd "$WM_THIRD_PARTY_DIR" && ./Allwmake -j"${JOBS}" )
  ( cd "${OF7_DIR}" && ./Allwmake -j"${JOBS}" )
else
  say "OpenFOAM-${OF7_VERSION} already present at ${OF7_DIR} — reusing"
  arm64_enable_of7
  set +u
  # shellcheck disable=SC1091
  source "${OF7_DIR}/etc/bashrc" || true
  set -u
fi

if [[ ! -d "${PREFIX}/openInjMoldSim" ]]; then
  git clone "${OIMS_REPO}" "${PREFIX}/openInjMoldSim"
fi
# the Allwmake lives in the solver subdir, not the repo root (v7.2 layout,
# verified live #193); it takes no -j — WM_NCOMPPROCS caps the parallelism
( cd "${PREFIX}/openInjMoldSim" && git checkout "${OIMS_TAG}" )
( cd "${PREFIX}/openInjMoldSim/applications/solvers/multiphase/openInjMoldSim" \
  && ./Allwmake )

say "build done. Export the env vars (see step 3) and verify with:"
plan "python3 -c 'from ankusdrive import solvers; print(solvers.openinjmoldsim_bin())'"

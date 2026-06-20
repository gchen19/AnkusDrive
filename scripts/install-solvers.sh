#!/usr/bin/env bash
#
# install-solvers.sh — provision the external P2 solvers DriftPin's heavy
# simulation families shell out to, and make them discoverable by the agent. The
# solver twin of scripts/install-renderers.sh (see docs/SIMULATION_P2_KICKOFF.md).
#
# WHAT IT DOES
#   Two solver shapes, two install paths:
#     * pip-wheel solvers (MBD: PyBullet/MuJoCo; topology: solidspy/topopt; optics:
#       rayoptics + optiland for sequential lens design/optimize) — a thin
#       `pip install '.[<extra>]'` into the worker's Python env. Clean, no system
#       package; this script automates it.
#     * GPL opt-in wheel (optics_gpl: KrakenOS, for non-sequential tracing through STL
#       solids) — GPL-3.0, so it is NOT installed by the default (no-arg) run; request
#       it explicitly (`install-solvers.sh optics_gpl`). DriftPin only ever runs it
#       out-of-process via driftpin/optics_gpl_runner.py, so its copyleft does not reach
#       DriftPin's own code (same arm's-length boundary as the GPL Elmer/OpenFOAM bins).
#     * GPL opt-in SOURCE BUILD (em_gpl: openEMS FDTD full-wave EM) — GPL-3.0 AND not
#       on PyPI/conda, so it is source-built from the openEMS-Project meta-repo into a
#       DEDICATED venv (.venv-openems) and run ONLY out-of-process via
#       driftpin/em_fullwave_gpl_runner.py. Request it explicitly
#       (`install-solvers.sh em_gpl`); never installed in the no-arg run. Same
#       arm's-length copyleft boundary as KrakenOS / the GPL Elmer/OpenFOAM bins.
#     * system-package solvers (CFD: OpenFOAM/SU2; transient/radiation thermal:
#       Elmer) — large apt/conda installs that vary by distro and need root, so this
#       script PRINTS the documented commands rather than running them. Install them,
#       then ensure their binary is on PATH (or set DRIFTPIN_<SOLVER>_PATH).
#
# DISCOVERY (mirrors the renderers)
#   A family resolves its solver via driftpin/solvers.py: for a wheel, the module
#   must import in the worker's env; for a binary, DRIFTPIN_<SOLVER>_PATH env ->
#   PATH (shutil.which) -> common per-OS install dirs. So the wheel installs MUST
#   target the SAME interpreter that launches `python -m driftpin mcp`, and a
#   system binary must be on that env's PATH. Verify with the solve_capabilities
#   MCP tool (or `do_list` below) — absent -> a family degrades to a clean
#   {ok:false, reason, install} dict; present -> the family can solve.
#
# USAGE
#   scripts/install-solvers.sh                  # pip-install permissive wheel extras + print system guidance
#   scripts/install-solvers.sh mbd              # just the MBD wheels (PyBullet)
#   scripts/install-solvers.sh topology optics  # several extras
#   scripts/install-solvers.sh optics_gpl       # opt-in GPL-3.0 non-sequential engine (KrakenOS)
#   scripts/install-solvers.sh em_gpl           # opt-in GPL-3.0 full-wave FDTD (openEMS, source-built)
#   scripts/install-solvers.sh --optics-gallery # bootstrap: install BOTH optics lanes + render every gallery figure
#   scripts/install-solvers.sh cfd              # print OpenFOAM/SU2 install guidance (no auto-install)
#   scripts/install-solvers.sh --list           # show what resolves right now (per solve_capabilities)
#
# ENV OVERRIDES
#   PY       interpreter whose env gets the wheels (default: ./.venv/bin/python3 if
#            present, else python3 — MUST match the MCP server's interpreter)
#   FORCE=1  pass --force-reinstall to pip
#
# Idempotent: pip skips an already-satisfied wheel; re-running is safe. Heavy
# *solves* belong on the provisioned/self-hosted FreeCAD runner — this script only
# provisions + verifies discovery, which is CI-checkable on any box.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# Pick the worker's interpreter: the repo venv if it exists (what tests use), else
# system python3. Override with PY=... to target the exact env the MCP server runs.
if [ -z "${PY:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then PY="$REPO_ROOT/.venv/bin/python3"; else PY="python3"; fi
fi
FORCE="${FORCE:-0}"

# Wheel-installable extras (pyproject [project.optional-dependencies]) -> the pip
# install this script automates. Keep in lockstep with pyproject.toml. WHEEL_EXTRAS
# are permissive-licensed and run by default; GPL_EXTRAS are GPL-3.0 and install ONLY
# when named explicitly (never in the no-arg run).
WHEEL_EXTRAS="mbd topology optics"
GPL_EXTRAS="optics_gpl"

# --- logging (matches install-renderers.sh) -----------------------------------
_c() { [ -t 1 ] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s==>%s %s\n' "$(_c '1;34')" "$(_c 0)" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%swarn%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; exit 1; }

# --- pip-wheel extras ----------------------------------------------------------
pip_install_extra() {  # extra
  local extra="$1"
  case " $WHEEL_EXTRAS $GPL_EXTRAS " in *" $extra "*) ;; *) die "unknown wheel extra: $extra (have: $WHEEL_EXTRAS $GPL_EXTRAS)";; esac
  command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || die "interpreter not found: $PY (set PY=...)"
  case " $GPL_EXTRAS " in *" $extra "*)
    warn "'$extra' pulls GPL-3.0 software (e.g. KrakenOS). DriftPin runs it only"
    warn "out-of-process (driftpin/optics_gpl_runner.py), keeping its own license clean."
  ;; esac
  local force_flag=""; [ "$FORCE" = "1" ] && force_flag="--force-reinstall"
  log "pip install '.[$extra]'  (into $PY)"
  "$PY" -m pip install $force_flag ".[$extra]" || die "pip install of '.[$extra]' failed"
  ok "$extra wheels installed into $($PY -c 'import sys; print(sys.prefix)')"
}

# --- system-package solvers (documented, not auto-installed) -------------------
system_guidance() {  # which: cfd|thermal|all
  local which="${1:-all}"
  if [ "$which" = "cfd" ] || [ "$which" = "all" ]; then
    cat <<'EOF'

CFD (OpenFOAM / SU2) — system package, install manually then put on PATH:
  Linux (OpenFOAM, openfoam.org):
      sudo sh -c "wget -O - https://dl.openfoam.org/gpg.key | apt-key add -"
      sudo add-apt-repository http://dl.openfoam.org/ubuntu
      sudo apt-get update && sudo apt-get install -y openfoam11
      # then: source /opt/openfoam11/etc/bashrc   (puts foamRun/simpleFoam on PATH)
  conda (any OS):  conda install -c conda-forge openfoam
  FreeCAD CfdOF workbench bundles a usable OpenFOAM on some platforms.
  SU2 alternative:  download from https://su2code.github.io/download.html
  Verify:  foamRun -help  (or set DRIFTPIN_OPENFOAM_PATH / DRIFTPIN_SU2_PATH)
EOF
  fi
  if [ "$which" = "thermal" ] || [ "$which" = "all" ]; then
    cat <<'EOF'

Transient/radiation thermal (Elmer) — system package:
  Linux:   sudo apt-get install -y elmerfem-csc
  conda:   conda install -c conda-forge elmer
  source:  https://www.elmerfem.org/
  Verify:  ElmerSolver -v  (or set DRIFTPIN_ELMER_PATH)
EOF
  fi
}

# --- em_gpl: openEMS FDTD full-wave EM (GPL-3.0, source build) ------------------
# openEMS is NOT a pip wheel — it is built from the openEMS-Project meta-repo with
# its update_openEMS.sh into a prefix + a DEDICATED venv (.venv-openems) carrying
# the openEMS/CSXCAD python bindings. DriftPin runs it only out-of-process
# (driftpin/em_fullwave_gpl_runner.py); set DRIFTPIN_OPENEMS_PYTHON to that venv's
# python so the worker resolves it. Override the prefix/clone/venv with EM_PREFIX /
# EM_SRC / EM_VENV.
build_openems() {
  warn "openEMS is GPL-3.0. DriftPin runs it ONLY out-of-process"
  warn "(driftpin/em_fullwave_gpl_runner.py), keeping its own license clean."
  local prefix="${EM_PREFIX:-$HOME/opt/openEMS}"
  local src="${EM_SRC:-$HOME/openEMS-Project}"
  local venv="${EM_VENV:-$REPO_ROOT/.venv-openems}"

  log "openEMS build deps (apt — needs sudo; adjust package names per distro)"
  cat <<'EOF'
  sudo apt-get install -y build-essential cmake git libhdf5-dev libvtk9-dev \
       libboost-all-dev libcgal-dev libtinyxml-dev libqt5xml5 qtbase5-dev
EOF
  if [ ! -d "$src" ]; then
    log "clone openEMS-Project -> $src"
    git clone --recursive https://github.com/thliebig/openEMS-Project.git "$src" \
      || die "git clone failed"
  fi
  log "build openEMS (+ python bindings) into $prefix  (this takes a while)"
  ( cd "$src" && ./update_openEMS.sh "$prefix" --python ) || die "update_openEMS.sh failed"

  log "dedicated venv -> $venv  (openEMS/CSXCAD python bindings)"
  python3 -m venv "$venv" || die "venv create failed"
  "$venv/bin/pip" install --quiet --upgrade pip numpy h5py cython || die "venv deps failed"
  CSXCAD_INSTALL_PATH="$prefix" "$venv/bin/pip" install --quiet "$src/CSXCAD/python" \
    || die "CSXCAD python install failed"
  CSXCAD_INSTALL_PATH="$prefix" OPENEMS_INSTALL_PATH="$prefix" \
    "$venv/bin/pip" install --quiet "$src/openEMS/python" || die "openEMS python install failed"

  log "smoke-test the engine"
  "$venv/bin/python" -c "import openEMS, CSXCAD; print('openEMS', openEMS.__version__)" \
    || die "openEMS import failed — check $prefix/lib is on the runtime path"
  ok "openEMS built. Point the worker at it:"
  ok "    export DRIFTPIN_OPENEMS_PYTHON=$venv/bin/python"
  ok "(or it is auto-discovered if .venv-openems sits beside the repo)"
}

# --- optics gallery bootstrap (install both lanes + render every figure) -------
build_optics_gallery() {
  log "bootstrapping the optics gallery (install both lanes, then render every figure)"
  pip_install_extra optics            # sequential: optiland + rayoptics (permissive)
  pip_install_extra optics_gpl        # non-sequential: KrakenOS (GPL-3.0, prints its notice)
  local gens="examples/optics_gallery.py examples/optics_gallery_3d.py examples/optics_ball_lens.py"
  for g in $gens; do
    [ -f "$REPO_ROOT/$g" ] || die "generator not found: $g (run from a DriftPin checkout)"
    log "render $g"
    ( cd "$REPO_ROOT" && "$PY" "$g" >/dev/null ) || die "rendering $g failed"
  done
  ok "optics gallery written to $REPO_ROOT/examples/optics_gallery/  ($(ls "$REPO_ROOT"/examples/optics_gallery/*.png 2>/dev/null | wc -l | tr -d ' ') PNGs)"
}

# --- DEM source-build: YADE (GPL-3.0, not a pip wheel) -------------------------
# YADE ships no PyPI/conda-noble wheel, so the `dem_gpl` extra is a SOURCE BUILD,
# not a pip install. DriftPin drives it only out-of-process via the `yade`
# executable running driftpin/dem_gpl_runner.py, so its GPL-3.0 copyleft does not
# reach into DriftPin's own (permissive) code. Installs into ~/opt/yade by default.
# RESOURCE NOTE: caps the build at -j8 so it never starves a co-resident CI runner.
build_dem_gpl() {
  warn "'dem_gpl' source-builds YADE (GPL-3.0). DriftPin runs it only out-of-process"
  warn "(driftpin/dem_gpl_runner.py via the \`yade\` executable), keeping its own license clean."
  local prefix="${YADE_PREFIX:-$HOME/opt/yade}"
  local src="${YADE_SRC:-$HOME/yade-trunk}"
  local jobs="${YADE_JOBS:-8}"   # cap parallelism — do NOT use -j$(nproc) on a CI host
  log "build deps (apt) — boost, gmp/mpfr, cgal, eigen, vtk, gts, metis, ccache"
  sudo apt-get install -y cmake git build-essential ccache libboost-all-dev \
      libgmp-dev libmpfr-dev libcgal-dev libeigen3-dev python3-dev python3-numpy \
      python3-mpmath libvtk9-dev libgts-dev libmetis-dev libopenblas-dev \
      libsuitesparse-dev zlib1g-dev || die "apt build-deps failed"
  if [ ! -d "$src/.git" ]; then
    log "clone YADE (gitlab.com/yade-dev/trunk) -> $src"
    git clone --depth 1 https://gitlab.com/yade-dev/trunk.git "$src" || die "git clone failed"
  fi
  log "cmake configure (prefix=$prefix, GUI off, VTK on, ccache, PYTHON_VERSION=3)"
  cmake -B "$src/build" -S "$src" \
      -DCMAKE_INSTALL_PREFIX="$prefix" -DENABLE_GUI=OFF -DENABLE_VTK=ON \
      -DENABLE_GTS=ON -DENABLE_MPI=OFF -DENABLE_LBMFLOW=OFF \
      -DCMAKE_CXX_COMPILER_LAUNCHER=ccache -DCMAKE_BUILD_TYPE=Release \
      -DPYTHON_VERSION=3 || die "cmake configure failed"
  log "cmake build -j$jobs  (capped — keep headroom for any co-resident runner)"
  cmake --build "$src/build" -j"$jobs" || die "cmake build failed"
  cmake --build "$src/build" --target install || die "cmake install failed"
  ok "YADE installed to $prefix — verify: $prefix/bin/yade --version  (or set DRIFTPIN_YADE)"
}

# --- list / status (delegates to driftpin.solvers — same probe as the tool) ----
do_list() {
  printf 'P2 solver discovery (what resolves in %s right now):\n\n' "$PY"
  PY="$PY" "$PY" - <<'PYEOF' || warn "could not import driftpin.solvers (run from a checkout with driftpin importable)"
import os, sys
sys.path.insert(0, os.getcwd())
from driftpin import solvers
caps = solvers.capabilities()
print(f"  platform: {caps['platform']}")
for name in solvers.known_solvers():
    info = caps["solvers"][name]
    mark = "ok " if info["available"] else "-- "
    where = info.get("path") or info.get("module") or "(absent)"
    extra = f"  [pip install 'driftpin[{info['extra']}]']" if info["extra"] else "  [system package]"
    print(f"  {mark}{name:10s} {info['kind']:6s} family={info['family']:18s} {where}{'' if info['available'] else extra}")
print()
for fam, fi in sorted(caps["families"].items()):
    state = "READY" if fi["any_available"] else "no solver"
    print(f"  family {fam:18s} {state}  (solvers: {', '.join(fi['solvers'])})")
PYEOF
}

# --- main ----------------------------------------------------------------------
main() {
  local extras=() systems=() do_all=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --list|-l) do_list; exit 0 ;;
      --optics-gallery) build_optics_gallery; exit 0 ;;
      -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
      mbd|topology|optics|optics_gpl) extras+=("$1"); do_all=0 ;;
      dem_gpl|dem|yade)  build_dem_gpl; exit 0 ;;   # GPL-3.0 source build, never default
      em_gpl|openems)    build_openems; exit 0 ;;
      cfd)               systems+=("cfd"); do_all=0 ;;
      thermal|elmer)     systems+=("thermal"); do_all=0 ;;
      openfoam|su2)      systems+=("cfd"); do_all=0 ;;
      *) die "unknown argument: $1 (try --list or --help)" ;;
    esac
    shift
  done

  if [ "$do_all" = "1" ]; then
    for e in $WHEEL_EXTRAS; do pip_install_extra "$e"; done
    system_guidance all
    echo
    warn "GPL opt-in NOT installed by default: the non-sequential optics engine (KrakenOS)"
    warn "is GPL-3.0 — install it explicitly with: scripts/install-solvers.sh optics_gpl"
    warn "Granular DEM (YADE, GPL-3.0) is also opt-in and SOURCE-BUILT — install with:"
    warn "  scripts/install-solvers.sh dem_gpl   (cmake build into ~/opt/yade, -j8)"
    warn "Full-wave FDTD EM (openEMS) is GPL-3.0 AND source-built (not a pip wheel) —"
    warn "build it explicitly with: scripts/install-solvers.sh em_gpl"
  else
    for e in "${extras[@]:-}";  do [ -n "$e" ] && pip_install_extra "$e"; done
    for s in "${systems[@]:-}"; do [ -n "$s" ] && system_guidance "$s"; done
  fi

  echo
  ok "done. Verify with the solve_capabilities MCP tool, or: scripts/install-solvers.sh --list"
}

main "$@"

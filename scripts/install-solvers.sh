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
      -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
      mbd|topology|optics|optics_gpl) extras+=("$1"); do_all=0 ;;
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
  else
    for e in "${extras[@]:-}";  do [ -n "$e" ] && pip_install_extra "$e"; done
    for s in "${systems[@]:-}"; do [ -n "$s" ] && system_guidance "$s"; done
  fi

  echo
  ok "done. Verify with the solve_capabilities MCP tool, or: scripts/install-solvers.sh --list"
}

main "$@"

#!/usr/bin/env bash
#
# ci-macos-preflight.sh — health-check the macOS heavy-solve substrate BEFORE any
# solve runs (issue #220).
#
# WHY THIS EXISTS
#   The Apple-Silicon heavy lane's two solver paths both run off-host:
#     * FSI (preCICE OpenFOAM<->CalculiX) executes inside a Multipass VM —
#       `solvers.bash_argv` emits `multipass exec <inst> -- bash -c "cd <case> && ..."`
#       on Darwin (issue #193), so the case dir the host built must be visible in
#       the VM at the SAME absolute path, and the four in-VM stack overrides must
#       point at paths that really exist there.
#     * SU2 is the official x86_64 macOS binary running under Rosetta 2.
#   Every one of those is stateful runner setup, not something the repo carries.
#   When the VM is stopped, the mount has dropped (multipass-sshfs is unreliable
#   under load), or an override is stale, the solve fails deep inside OpenFOAM or
#   at a preCICE handshake deadline — minutes later, with an error that says
#   nothing about the real cause. This script turns all of those into a fast,
#   named failure with the exact fix.
#
# WHAT IT CHECKS  (host -> VM -> in-VM stack -> SU2/Rosetta)
#   1. running on macOS, `multipass` present
#   2. the OpenFOAM instance exists and is Running (starts it if it is not)
#   3. the VM answers `multipass exec` at all
#   4. TMPDIR is set and round-trips host->VM at a MATCHING absolute path
#      (writes a sentinel and reads it back inside the VM) — remounts once and
#      retries, since a dropped sshfs mount is the common flake
#   5. the four ANKUSDRIVE_* FSI overrides are set AND their paths exist in the VM
#   6. ankusdrive's own host-side resolution agrees (`fsi_stack_status().ok`)
#   7. Rosetta 2 present on arm64 and SU2_CFD resolves
#
#   Checks report ALL failures (dependent checks are skipped when a prerequisite
#   fails), then exit 1 with a summary — one CI run tells you everything that is
#   wrong, not just the first thing.
#
# USAGE
#   bash scripts/ci-macos-preflight.sh          # full check (CI + local)
#   bash scripts/ci-macos-preflight.sh --fsi    # FSI substrate only (skip SU2/Rosetta)
#   bash scripts/ci-macos-preflight.sh --su2    # SU2/Rosetta only (skip the VM)
#
# Runner setup lives in docs/MACOS.md ("Self-hosted CI runner"). The env vars are
# expected from the runner's own environment (<runner-dir>/.env), NOT from the
# workflow — they are box-specific absolute paths.
set -uo pipefail
cd "$(dirname "$0")/.."

WANT_FSI=1
WANT_SU2=1
case "${1:-}" in
  --fsi) WANT_SU2=0 ;;
  --su2) WANT_FSI=0 ;;
  "")    ;;
  *)     echo "usage: $0 [--fsi|--su2]" >&2; exit 2 ;;
esac

# A counter, not an array: macOS ships bash 3.2, where `${ARR[@]}` on an empty
# array trips `set -u`.
FAILED=0

# GitHub Actions renders ::error:: as an annotation on the job; locally it is
# just a prefix. Either way the failure text is identical.
_gha() { [ "${GITHUB_ACTIONS:-}" = "true" ] && printf '::%s::' "$1" || printf '%s: ' "$1"; }
ok()   { printf '  ok    %s\n' "$*"; }
skip() { printf '  skip  %s\n' "$*"; }
warn() { printf '%s%s\n' "$(_gha warning)" "$*"; }
fail() { printf '%s%s\n' "$(_gha error)" "$*"; FAILED=$((FAILED + 1)); }
step() { printf '\n== %s ==\n' "$*"; }

INSTANCE="${ANKUSDRIVE_OPENFOAM_INSTANCE:-openfoam}"

# --- 1. host ------------------------------------------------------------------
step "Host"
if [ "$(uname -s)" != "Darwin" ]; then
  fail "not macOS (uname -s = $(uname -s)) — this preflight is for the Apple-Silicon lane"
  printf '\nPreflight FAILED.\n' >&2; exit 1
fi
ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?') on $(uname -m)"

# --- 1b. legacy env var names (deprecated with the shim: delete in 0.6) -------
# The 0.5 rename (#295) moved every override to ANKUSDRIVE_*. ankusdrive's own
# import-time shim still promotes a DRIFTPIN_* var in-process, so Python-side
# resolution keeps working and `doctor` looks healthy -- but this script reads the
# job environment DIRECTLY, and so does everything else outside Python. A runner
# whose .env was never renamed therefore fails below as six separate "is unset"
# errors that each name the wrong fix. Say the real one, once, up front.
step "Environment naming"
legacy=$(env | sed -n 's/^\(DRIFTPIN_[A-Za-z0-9_]*\)=.*/\1/p' | sort | tr '\n' ' ')
if [ -n "$legacy" ]; then
  fail "legacy DRIFTPIN_* vars are set in the runner environment: ${legacy% }. They were renamed to ANKUSDRIVE_* in 0.5 (#295) and this script reads only the new names, so the overrides below will report as unset. Fix, on the runner box:  sed -i '' 's/^DRIFTPIN_/ANKUSDRIVE_/' <runner-dir>/.env  then restart it:  ./svc.sh stop && ./svc.sh start"
else
  ok "no legacy DRIFTPIN_* vars in the environment"
fi

# --- 2/3/4/5. the Multipass substrate ----------------------------------------
VM_UP=0
if [ "$WANT_FSI" = 1 ]; then
  step "Multipass substrate (instance '$INSTANCE')"
  if ! command -v multipass >/dev/null 2>&1; then
    fail "multipass not on PATH — install it:  brew install --cask multipass  (docs/MACOS.md)"
  else
    # `multipass version` prints both `multipass` and `multipassd` — take the client line only.
    ok "multipass $(multipass version 2>/dev/null | awk '/^multipass /{print $2; exit}' || echo '?')"

    state=$(multipass info "$INSTANCE" --format csv 2>/dev/null | awk -F, 'NR==2{print $2}')
    if [ -z "$state" ]; then
      fail "instance '$INSTANCE' does not exist — create it:  multipass launch -c 8 -m 8G -d 80G -n $INSTANCE 24.04  (then provision: scripts/install-solvers.sh fsi)"
    else
      if [ "$state" != "Running" ]; then
        warn "instance '$INSTANCE' is '$state' — starting it"
        multipass start "$INSTANCE" >/dev/null 2>&1
        state=$(multipass info "$INSTANCE" --format csv 2>/dev/null | awk -F, 'NR==2{print $2}')
      fi
      if [ "$state" != "Running" ]; then
        fail "instance '$INSTANCE' is '$state' after 'multipass start' — the VM is down; check 'multipass info $INSTANCE'"
      # A Running instance can still be unreachable (agent wedged after a host
      # sleep), which is exactly the state that hangs a solve. Exec is the honest probe.
      elif ! multipass exec "$INSTANCE" -- true >/dev/null 2>&1; then
        fail "'multipass exec $INSTANCE' does not respond though the instance reports Running — try:  multipass restart $INSTANCE"
      else
        VM_UP=1
        ok "instance '$INSTANCE' Running and answering exec"
      fi
    fi
  fi

  # --- 4. the host<->VM mount at a MATCHING absolute path ---------------------
  step "Case-dir mount (TMPDIR must resolve to the same path inside the VM)"
  if [ "$VM_UP" != 1 ]; then
    skip "VM is not up — cannot check the mount"
  elif [ -z "${TMPDIR:-}" ]; then
    fail "TMPDIR is unset — the FSI case dirs (mkdtemp) must land inside the mount; export TMPDIR=<mounted dir> in the runner env (docs/MACOS.md)"
  else
    # macOS's default TMPDIR is a per-user /var/folders/... path with a trailing
    # slash; normalize so the host and in-VM strings compare and mount cleanly.
    MOUNT="${TMPDIR%/}"
    if [ ! -d "$MOUNT" ]; then
      fail "TMPDIR=$MOUNT does not exist on the host — create it and mount it into the VM"
    else
      probe_mount() {
        local sentinel="$MOUNT/.ankusdrive-preflight-$$"
        : > "$sentinel" 2>/dev/null || return 1
        local rc=0
        multipass exec "$INSTANCE" -- test -f "$sentinel" >/dev/null 2>&1 || rc=1
        rm -f "$sentinel"
        return $rc
      }
      if probe_mount; then
        ok "$MOUNT round-trips host -> $INSTANCE at the same path"
      else
        # A dropped sshfs mount is THE recurring flake here; one remount is worth
        # more than a red run. If it still fails, the message names the fix.
        warn "$MOUNT is not visible in the VM at that path — remounting once"
        multipass mount "$MOUNT" "$INSTANCE:$MOUNT" >/dev/null 2>&1
        if probe_mount; then
          ok "$MOUNT round-trips after remount"
        else
          fail "TMPDIR=$MOUNT is not visible inside '$INSTANCE' at the same absolute path. Fix:  multipass mount $MOUNT $INSTANCE:$MOUNT  (needs the multipass-sshfs snap: multipass exec $INSTANCE -- sudo snap install multipass-sshfs). A 'cd <case>' inside the VM cannot resolve without it."
        fi
      fi
    fi
  fi

  # --- 5. the four in-VM stack overrides -------------------------------------
  # On Darwin solvers._fsi_override TRUSTS an absolute override when multipass is
  # present (the VM filesystem is opaque from the host), so a typo'd or stale path
  # resolves fine here and only explodes mid-solve. The VM can check it for real.
  step "In-VM FSI stack overrides"
  check_in_vm() {   # $1=var $2=test-flag(-x|-d) $3=what
    local var="$1" flag="$2" what="$3" val="${!1:-}"
    if [ -z "$val" ]; then
      fail "$var is unset — export it in the runner env (docs/MACOS.md): the in-VM $what"
      return
    fi
    case "$val" in
      /*) ;;
      *)  fail "$var=$val is not absolute — it must be an absolute path INSIDE the VM"; return ;;
    esac
    if [ "$VM_UP" != 1 ]; then
      skip "$var=$val (VM down — cannot verify in-VM)"
    elif multipass exec "$INSTANCE" -- test "$flag" "$val" >/dev/null 2>&1; then
      ok "$var -> $val"
    else
      fail "$var=$val does not exist in '$INSTANCE' ($what) — reprovision:  scripts/install-solvers.sh fsi  (run the printed recipe inside 'multipass shell $INSTANCE')"
    fi
  }
  check_in_vm ANKUSDRIVE_CCX_PRECICE          -x "ccx_preCICE solid solver"        # executed
  check_in_vm ANKUSDRIVE_PRECICE_LIB          -d "libprecice.so directory"
  check_in_vm ANKUSDRIVE_OPENFOAM_ADAPTER_LIB -d "OpenFOAM preCICE adapter lib directory"
  # -r, not -x: the bashrc is SOURCED, and the ESI deb ships it 644.
  check_in_vm ANKUSDRIVE_FSI_OPENFOAM_BASHRC  -r "OpenFOAM etc/bashrc the adapter was built against"

  # --- 5b. the plain-CFD overrides (issue #223) -------------------------------
  # The built-in CFD case builders (pipe, flat plate, snappy bridge, wind tunnel)
  # are OpenFOAM-only and resolve through ANKUSDRIVE_OPENFOAM_*, NOT the FSI pair
  # above. Without them test_openfoam / test_meshbridge / test_wind_tunnel SKIP
  # their live halves and the lane goes green having solved nothing.
  step "In-VM plain-CFD overrides"
  check_in_vm ANKUSDRIVE_OPENFOAM_BASHRC -r "OpenFOAM etc/bashrc for the built-in CFD cases"
  check_in_vm ANKUSDRIVE_OPENFOAM_PATH   -x "OpenFOAM solver binary (simpleFoam/interFoam)"

  # --- 6. ankusdrive's own host-side view --------------------------------------
  # The overrides above can all be right and the stack still report not-ok if the
  # config layer disagrees (e.g. config.toml shadowing, multipass off PATH). This
  # is the exact predicate tests/test_fsi.py gates the live solve on, so check it
  # rather than infer it.
  step "ankusdrive fsi_stack_status() (the predicate test_fsi.py gates on)"
  status_raw=$(python3 - <<'PY' 2>&1
import json, sys
sys.path.insert(0, ".")
try:
    from ankusdrive import solvers
    s = solvers.fsi_stack_status()
except Exception as e:                       # import/resolution blew up
    print("ERR " + str(e)); sys.exit(0)
print(("OK " if s["ok"] else "MISSING ") + json.dumps(s.get("missing") or []))
PY
)
  # The probe prints its verdict as the LAST line. Keep 2>&1 -- a traceback on
  # stderr is worth having in the log -- but read only that last line: any
  # chatter before it (deprecation warnings, FreeCAD/numpy noise) is NOT the
  # verdict. Folding it in turned a healthy substrate into "could not evaluate",
  # and in the SU2 case below it did worse: non-empty stderr read as a resolved
  # path, passing the lane green with SU2 unverified.
  status=$(printf '%s\n' "$status_raw" | tail -n 1)
  case "$status" in
    "OK "*)      ok "fsi_stack_status().ok — the live FSI test will run, not skip" ;;
    "MISSING "*) fail "fsi_stack_status() reports missing: ${status#MISSING } — test_fsi.py's live solve would SKIP silently, so the lane would pass without testing anything" ;;
    *)           fail "could not evaluate fsi_stack_status(): ${status#ERR }"; printf '%s\n' "$status_raw" >&2 ;;
  esac

  # The CFD files gate on this exact predicate, so check it rather than infer it
  # from the overrides (config.toml shadowing, multipass off PATH, ...).
  step "ankusdrive find_solver('openfoam') (the predicate the CFD files gate on)"
  foam_raw=$(python3 - <<'FOAMPY' 2>&1
import sys
sys.path.insert(0, ".")
try:
    from ankusdrive import solvers
    info = solvers.find_solver("openfoam")
except Exception as e:
    print("ERR " + str(e)); sys.exit(0)
print(("OK " + info.get("path", "")) if info["available"] else "MISSING " + info["status"])
FOAMPY
)
  foam=$(printf '%s\n' "$foam_raw" | tail -n 1)   # last line only -- see above
  case "$foam" in
    "OK "*)      ok "openfoam resolves -> ${foam#OK }" ;;
    "MISSING "*) fail "openfoam does not resolve (status: ${foam#MISSING }) — the live CFD / mesh-bridge / wind-tunnel gates would SKIP silently. See docs/MACOS.md, 'Plain CFD ... in the VM'" ;;
    *)           fail "could not evaluate find_solver('openfoam'): ${foam#ERR }"; printf '%s\n' "$foam_raw" >&2 ;;
  esac
fi

# --- 7. SU2 under Rosetta ------------------------------------------------------
if [ "$WANT_SU2" = 1 ]; then
  step "SU2 (official x86_64 binary under Rosetta 2)"
  if [ "$(uname -m)" = "arm64" ]; then
    if /usr/bin/pgrep -q oahd 2>/dev/null; then
      ok "Rosetta 2 present (oahd running)"
    else
      fail "Rosetta 2 absent on this arm64 Mac — SU2's official macOS build is x86_64. Install:  softwareupdate --install-rosetta --agree-to-license"
    fi
  else
    ok "x86_64 host — no Rosetta needed"
  fi
  # Prints an explicit "OK "/"MISSING " sentinel rather than the bare path. The
  # bare form could not be read safely: its not-found verdict was the EMPTY
  # string, which command substitution strips along with the trailing newline, so
  # a single line of stderr (the DRIFTPIN_* deprecation warning, a numpy notice)
  # became the whole captured value and matched the catch-all -- reporting
  # "SU2_CFD -> <warning text>" and passing the lane green with SU2 unverified.
  # With a sentinel, anything unrecognised is a failure instead of a pass.
  su2_raw=$(python3 - <<'PY' 2>&1
import sys
sys.path.insert(0, ".")
try:
    from ankusdrive import solvers
    path = solvers.find_solver("su2").get("path") or ""
except Exception as e:
    print("ERR " + str(e)); sys.exit(0)
print(("OK " + path) if path else "MISSING")
PY
)
  su2=$(printf '%s\n' "$su2_raw" | tail -n 1)     # last line only -- see above
  case "$su2" in
    "OK "*) ok "SU2_CFD -> ${su2#OK }" ;;
    *)      fail "SU2_CFD does not resolve — install it:  scripts/install-solvers.sh su2  (without it test_su2_native.py's live solve SKIPs and the lane tests nothing)"; printf '%s\n' "$su2_raw" >&2 ;;
  esac
fi

# --- summary -------------------------------------------------------------------
echo
if [ "$FAILED" -eq 0 ]; then
  echo "Preflight OK — the macOS heavy-solve substrate is up."
  exit 0
fi
echo "Preflight FAILED ($FAILED problem(s)) — see the errors above; docs/MACOS.md has the runner setup." >&2
exit 1

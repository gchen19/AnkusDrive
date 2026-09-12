#!/usr/bin/env bash
#
# ci-linux-preflight.sh — assert the Linux heavy-solve stack is COMPLETE before any
# solve runs (#339 / #340). The Linux counterpart of ci-macos-preflight.sh.
#
# WHY THIS EXISTS
#   Every live heavy test SKIPs when its solver does not resolve. That is the right
#   contract on a partly provisioned dev box, and the wrong one for the heavy lane:
#   an image that lost YADE, or a job whose env dropped an override, would report a
#   green run having silently stopped testing that solver (the #316 shape). This
#   script turns "absent" into a named failure, using the SAME predicates the tests
#   gate on (ankusdrive.solvers), never a separate re-implementation of discovery.
#
# WHAT IT CHECKS
#   1. no legacy DRIFTPIN_* vars in the environment (renamed in 0.5, #295)
#   2. freecadcmd resolves to an executable file
#   3. every solver in REQUIRED resolves with status ok (not absent, not unwired)
#   4. the four-part FSI stack resolves (fsi_stack_status().ok)
#   5. openInjMoldSim and its OpenFOAM-7 bashrc resolve
#
#   All checks run; the exit is non-zero if any failed, and each failure names itself.
#
# NOT REQUIRED here, deliberately: su2 (covered natively by the macOS and Windows
# lanes) and mujoco (the Darwin alternative to pybullet for the mbd family).
#
# USAGE
#   bash scripts/ci-linux-preflight.sh     # from a checkout, ankusdrive importable
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"
"$PY" - <<'PYEOF'
import os
import sys

sys.path.insert(0, os.getcwd())

GHA = os.environ.get("GITHUB_ACTIONS") == "true"
failed = 0


def ok(msg):
    print(f"  ok    {msg}")


def fail(msg):
    global failed
    failed += 1
    print(f"{'::error::' if GHA else 'error: '}{msg}")


print("== Environment naming ==")
legacy = sorted(k for k in os.environ if k.startswith("DRIFTPIN_"))
if legacy:
    fail(f"legacy DRIFTPIN_* vars set: {', '.join(legacy)} — renamed to ANKUSDRIVE_* in 0.5 (#295)")
else:
    ok("no legacy DRIFTPIN_* vars")

from ankusdrive import client, solvers  # noqa: E402  (after the path insert)

print("\n== FreeCAD ==")
fc = client.FREECADCMD
if os.path.isfile(fc) and os.access(fc, os.X_OK):
    ok(f"freecadcmd -> {fc}")
else:
    fail(f"freecadcmd does not resolve to an executable (got {fc!r}) — set ANKUSDRIVE_FREECADCMD")

# Every solver the Linux heavy lane runs live. Adding a heavy family means adding
# its solver here, or its live tests can go absent without anything turning red.
# bempp and openems are NOT here: they live in dedicated venvs, and find_solver only
# probes THIS interpreter — see the dedicated-venv section below.
REQUIRED = (
    "calculix", "elmer", "kraken", "openfoam", "optiland",
    "precice", "prusaslicer", "pybullet", "rayoptics", "topopt", "yade",
)
print("\n== Solvers ==")
caps = solvers.capabilities()["solvers"]
for name in REQUIRED:
    info = caps.get(name)
    if info is None:
        fail(f"{name}: not a known solver — REQUIRED is stale against ankusdrive.solvers")
        continue
    status = info.get("status", "ok" if info["available"] else "absent")
    where = info.get("path") or info.get("module") or info.get("found_at") or ""
    if status == "ok" and info["available"]:
        ok(f"{name:11s} {where}")
    elif status == "unwired":
        fail(f"{name}: installed but unwired at {info.get('found_at')} — {info.get('wire_hint')}")
    else:
        fail(f"{name}: absent — {info.get('install') or 'see scripts/install-solvers.sh'}")

# The live Bempp / openEMS tests gate on their own interpreter probes
# (tests/test_acoustics_bem.py::_bempp_python, tests/test_em_fullwave.py::
# _openems_python): the env override, then .venv-<x> beside the repo or one level
# up, then python3 on PATH — each checked with find_spec in THAT interpreter.
# Same order and same probe here, so this passes exactly when those tests would run.
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(os.getcwd())


def venv_python(env_var, venv, modules):
    cands = [os.environ.get(env_var)]
    for base in (REPO, REPO.parent):
        cands += [str(base / venv / "bin" / "python3"), str(base / venv / "bin" / "python")]
    cands.append(shutil.which("python3"))
    probe = ("import importlib.util,sys;sys.exit(0 if all(importlib.util.find_spec(m) "
             f"for m in {list(modules)!r}) else 1)")
    for c in dict.fromkeys(c for c in cands if c):
        if os.path.isfile(c):
            try:
                if subprocess.run([c, "-c", probe], capture_output=True, timeout=30).returncode == 0:
                    return c
            except Exception:
                pass
    return None


print("\n== Dedicated-venv solvers ==")
for label, env_var, venv, modules in (
    ("bempp", "ANKUSDRIVE_BEMPP_PYTHON", ".venv-bempp", ("bempp_cl",)),
    ("openems", "ANKUSDRIVE_OPENEMS_PYTHON", ".venv-openems", ("openEMS", "CSXCAD")),
):
    if py := venv_python(env_var, venv, modules):
        ok(f"{label:11s} {py}")
    else:
        fail(f"{label}: no interpreter imports {', '.join(modules)} — set {env_var} "
             f"(scripts/install-solvers.sh {'acoustics_bem' if label == 'bempp' else 'em_gpl'})")

print("\n== FSI stack ==")
fsi = solvers.fsi_stack_status()
if fsi["ok"]:
    for k in ("ccx_precice", "precice_lib", "openfoam_adapter_lib", "openfoam_bashrc"):
        ok(f"{k:21s} {fsi[k]}")
else:
    fail(f"FSI stack incomplete, missing: {', '.join(fsi['missing'])}")

print("\n== openInjMoldSim ==")
oims, oims_rc = solvers.openinjmoldsim_bin(), solvers.openinjmoldsim_bashrc()
if oims and oims_rc:
    ok(f"openInjMoldSim -> {oims}")
    ok(f"OpenFOAM-7 bashrc -> {oims_rc}")
else:
    fail(f"openInjMoldSim does not resolve (bin={oims!r}, bashrc={oims_rc!r}) — "
         "set ANKUSDRIVE_OPENINJMOLDSIM / ANKUSDRIVE_OPENINJMOLDSIM_BASHRC")

if failed:
    print(f"\nPreflight FAILED: {failed} check(s). The heavy suite would have SKIPped these, not failed.")
    sys.exit(1)
print("\nPreflight passed.")
PYEOF

#!/usr/bin/env bash
#
# ci-container-preflight.sh — assert the container-substrate lane (#362) really
# crosses the relay before any solve runs. The container twin of ci-macos-preflight.sh.
#
# WHY THIS EXISTS
#   Lane B runs the macOS VM lane's files on a hosted Linux runner with the solvers in
#   a sidecar container (ANKUSDRIVE_SUBSTRATE=container, #361). Every way that set-up
#   can be subtly wrong ends in a green run that proved nothing:
#     * the substrate is not actually `container` -> solves go native (or skip);
#     * the container is down                     -> live tests SKIP, lane passes;
#     * the scratch is not mounted at the SAME path -> OpenFOAM aborts in a dir that
#       does not exist, deep inside a solve;
#     * files the container writes are root-owned -> host-side test cleanup fails;
#     * the host can run OpenFOAM itself          -> a solve that silently bypassed
#       the relay would still pass, so the lane would not be testing the relay.
#   This turns each into a named failure, using the same predicates the tests gate on
#   (ankusdrive.solvers), never a re-implementation.
#
# WHAT IT CHECKS
#   1. substrate() == "container" and the engine is on PATH
#   2. container_state() == "running"
#   3. the host scratch ($TMPDIR) round-trips host->container AND container->host at
#      the same absolute path, and a container-written file is deletable by the host
#   4. the host has NO OpenFOAM of its own (no foam binary on PATH, no apt layout)
#   5. through the container: openfoam resolves via:container, the FSI stack is
#      complete, openInjMoldSim + its OF-7 bashrc resolve
#   6. freecadcmd resolves on the host (test_wind_tunnel drives the real worker)
#
# USAGE
#   ANKUSDRIVE_SUBSTRATE=container bash scripts/ci-container-preflight.sh
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"
"$PY" - <<'PYEOF'
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.getcwd())
GHA = os.environ.get("GITHUB_ACTIONS") == "true"
failed = 0


def ok(msg):
    print(f"  ok    {msg}")


def fail(msg):
    global failed
    failed += 1
    print(f"{'::error::' if GHA else 'error: '}{msg}")


from ankusdrive import client, solvers  # noqa: E402

print("== Substrate ==")
try:
    sub = solvers.substrate()
except ValueError as e:
    fail(str(e))
    sub = None
if sub != "container":
    fail(f"substrate is {sub!r}, not 'container' — export ANKUSDRIVE_SUBSTRATE=container")
    print(f"\nPreflight FAILED: {failed} check(s).")
    sys.exit(1)
engine, name = solvers.container_engine(), solvers.container_name()
if shutil.which(engine):
    ok(f"substrate container, engine {engine}, container {name!r}")
else:
    fail(f"`{engine}` is not on PATH")

print("\n== Container ==")
state = solvers.container_state()
if state == "running":
    ok(f"{name!r} running")
else:
    fail(f"container {name!r} is {state} — create it: {solvers.container_run_command()}")


def cexec(script):
    return subprocess.run(solvers.bash_argv(script), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


print("\n== Same-path scratch ==")
scratch = os.environ.get("TMPDIR", "")
if state != "running":
    print("  skip  container not running")
elif not scratch or not os.path.isdir(scratch):
    fail(f"TMPDIR={scratch!r} is not a host directory — case dirs must live under the mount")
else:
    probe = tempfile.mkdtemp(prefix="preflight-", dir=scratch)
    token = uuid.uuid4().hex
    with open(os.path.join(probe, "from-host"), "w") as f:
        f.write(token)
    r = cexec(f"cat '{probe}/from-host' && echo {token}-back > '{probe}/from-container'")
    if r.returncode != 0 or r.stdout.strip() != token:
        fail(f"host file not visible in the container at {probe} — mount {scratch} at the "
             f"same path (rc={r.returncode}, stderr={r.stderr.strip()[-200:]!r})")
    else:
        ok(f"host -> container at the same path ({probe})")
        back = os.path.join(probe, "from-container")
        if os.path.isfile(back) and open(back).read().strip() == f"{token}-back":
            ok("container -> host at the same path")
        else:
            fail("a file the container wrote did not appear on the host")
    try:
        shutil.rmtree(probe)
        ok("container-written files are deletable by the host (no root-owned outputs)")
    except OSError as e:
        fail(f"cannot delete container-written files ({e}) — run the container with "
             "--user \"$(id -u):$(id -g)\"")

print("\n== Host has no OpenFOAM of its own ==")
native = [b for b in ("simpleFoam", "blockMesh", "foamRun", "pimpleFoam", "interFoam")
          if shutil.which(b)]
native += glob.glob("/usr/lib/openfoam/openfoam*/etc/bashrc") + glob.glob("/opt/openfoam*")
if native:
    fail(f"the host can run OpenFOAM itself ({native}) — a solve that bypassed the relay "
         "would still pass, so this lane would not be testing the relay")
else:
    ok("no native OpenFOAM on the host")

print("\n== Stack through the container ==")
of = solvers.find_solver("openfoam")
if of.get("available") and of.get("via") == "container":
    ok(f"openfoam via container -> {of['path']}")
else:
    fail(f"openfoam does not resolve via the container: status={of.get('status')} "
         f"hint={of.get('wire_hint') or of.get('install_hint')!r}")
fsi = solvers.fsi_stack_status()
if fsi["ok"]:
    ok("FSI stack complete (ccx_preCICE, libprecice, adapter, bashrc)")
else:
    fail(f"FSI stack incomplete, missing: {', '.join(fsi['missing'])}")
oims, oims_rc = solvers.openinjmoldsim_bin(), solvers.openinjmoldsim_bashrc()
if oims and oims_rc:
    ok(f"openInjMoldSim -> {oims}")
else:
    fail(f"openInjMoldSim does not resolve (bin={oims!r}, bashrc={oims_rc!r})")

print("\n== Host FreeCAD ==")
fc = client.FREECADCMD
if os.path.isfile(fc) and os.access(fc, os.X_OK):
    ok(f"freecadcmd -> {fc}")
else:
    fail(f"freecadcmd does not resolve ({fc!r}) — test_wind_tunnel drives the real worker")

if failed:
    print(f"\nPreflight FAILED: {failed} check(s). The live tests would have SKIPped or "
          "passed without crossing the relay.")
    sys.exit(1)
print("\nPreflight passed.")
PYEOF

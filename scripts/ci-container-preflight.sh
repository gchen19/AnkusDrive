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
#   4. the host has NO OpenFOAM, YADE, Elmer, openEMS or Bempp of its own
#   5. through the container: openfoam resolves via:container, the FSI stack is
#      complete, openInjMoldSim + its OF-7 bashrc resolve, and YADE / Elmer (with its
#      ElmerGrid + ViewFactors siblings) / openEMS / Bempp resolve via:container by
#      probing — with NO export for them, the path a macOS user takes (#419)
#   6. freecadcmd resolves on the host (test_wind_tunnel drives the real worker)
#
# A PARTIAL IMAGE (#422)
#   EXPECT_SOLVERS names the solvers the image was built with, as the image names them
#   (`openfoam fsi oims yade elmer openems bempp`; default: all). Each expected solver
#   gets the checks above; each one NOT expected must be reported excluded — listed
#   under `excluded` in /etc/ankusdrive/solvers.json and surfaced by discovery as
#   "this image does not include …", never as ready. That is the path a user of
#   `ankusdrive-solvers:openfoam` takes, and the only proof the manifest is honoured
#   end to end. With EXPECT_SOLVERS set the image MUST carry a manifest.
#   PREFLIGHT_HOST_FREECAD=0 skips check 6 where no suite runs after the preflight.
#
# USAGE
#   ANKUSDRIVE_SUBSTRATE=container bash scripts/ci-container-preflight.sh
#   EXPECT_SOLVERS=openfoam PREFLIGHT_HOST_FREECAD=0 ANKUSDRIVE_SUBSTRATE=container \
#     bash scripts/ci-container-preflight.sh
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

# the image's own names for what it can carry (tools/build_solver_image.sh)
ALL = ("openfoam", "fsi", "oims", "yade", "elmer", "openems", "bempp")
SUBSET = bool(os.environ.get("EXPECT_SOLVERS", "").strip())
expect = set(os.environ["EXPECT_SOLVERS"].split()) if SUBSET else set(ALL)
if expect - set(ALL):
    fail(f"EXPECT_SOLVERS names unknown solvers {sorted(expect - set(ALL))}; "
         f"known: {' '.join(ALL)}")
    sys.exit(1)

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

print("\n== Host has no YADE / Elmer / openEMS / Bempp of its own (#419) ==")
import importlib.util  # noqa: E402
native = [b for b in ("yade", "ElmerSolver", "ElmerGrid", "ViewFactors") if shutil.which(b)]
native += [m for m in ("openEMS", "CSXCAD", "bempp_cl") if importlib.util.find_spec(m)]
native += [v for v in (".venv-openems", ".venv-bempp") if os.path.isdir(v)]
if native:
    fail(f"the host has its own {native} — a solve that bypassed the relay would still "
         "pass, so this lane would not be testing the relay")
else:
    ok("no native YADE / Elmer / openEMS / Bempp on the host")

print("\n== What the image says it carries (#422) ==")
manifest = solvers.container_manifest() if state == "running" else None
if manifest is None:
    if SUBSET:
        fail("EXPECT_SOLVERS is set but the image has no /etc/ankusdrive/solvers.json — "
             "a partial image without a manifest reports every omitted solver as ready")
    else:
        ok("no manifest (an image from before #422) — discovery probes instead")
else:
    listed = set(manifest["solvers"])
    excluded = set(manifest.get("excluded") or {})
    if listed == expect and excluded == set(ALL) - expect:
        ok(f"manifest lists {' '.join(sorted(listed))}"
           + (f"; excludes {' '.join(sorted(excluded))}" if excluded else ""))
    else:
        fail(f"manifest lists {sorted(listed)} / excludes {sorted(excluded)}; "
             f"expected {sorted(expect)} / {sorted(set(ALL) - expect)}")


def excluded_ok(key, info):
    """An omitted solver must be a named miss that says the IMAGE left it out."""
    hint = info.get("wire_hint") or info.get("install_hint") or ""
    if info.get("available"):
        fail(f"{key} is excluded from this image but resolves ({info.get('path')}) — "
             "the manifest was not honoured")
    elif "does not include" not in hint:
        fail(f"{key} is excluded but its hint does not say so: {hint!r}")
    else:
        ok(f"{key} reported excluded: {hint.split('(')[0].strip()}")


print("\n== Stack through the container ==")
of = solvers.find_solver("openfoam")
if "openfoam" not in expect:
    excluded_ok("openfoam", of)
elif of.get("available") and of.get("via") == "container":
    ok(f"openfoam via container -> {of['path']}")
else:
    fail(f"openfoam does not resolve via the container: status={of.get('status')} "
         f"hint={of.get('wire_hint') or of.get('install_hint')!r}")
fsi = solvers.fsi_stack_status()
if "fsi" not in expect:
    if fsi["ok"]:
        fail("the FSI stack resolves in an image that excluded it")
    else:
        excluded_ok("fsi", solvers.find_solver("precice"))
elif fsi["ok"]:
    ok("FSI stack complete (ccx_preCICE, libprecice, adapter, bashrc)")
else:
    fail(f"FSI stack incomplete, missing: {', '.join(fsi['missing'])}")
oims, oims_rc = solvers.openinjmoldsim_bin(), solvers.openinjmoldsim_bashrc()
if "oims" not in expect:
    if oims:
        fail(f"openInjMoldSim resolves ({oims}) in an image that excluded it")
    elif not solvers.container_excludes("oims"):
        fail("openInjMoldSim does not resolve, but the manifest does not exclude it")
    else:
        ok("oims reported excluded by the manifest")
elif oims and oims_rc:
    ok(f"openInjMoldSim -> {oims}")
else:
    fail(f"openInjMoldSim does not resolve (bin={oims!r}, bashrc={oims_rc!r})")

for solver in ("yade", "elmer", "openems", "bempp"):
    info = solvers.find_solver(solver)
    if solver not in expect:
        excluded_ok(solver, info)
    elif info.get("available") and info.get("via") == "container":
        ok(f"{solver} via container -> {info['path']}")
    else:
        fail(f"{solver} does not resolve via the container: status={info.get('status')} "
             f"hint={info.get('wire_hint') or info.get('install_hint')!r}")
if "elmer" in expect:
    elmer = solvers.find_solver("elmer").get("path")
    for sib in ("ElmerGrid", "ViewFactors"):
        path = solvers.sibling_bin(elmer, sib, "elmer") if elmer else None
        if path and solvers.container_file_exists(path):
            ok(f"{sib} in container -> {path}")
        else:
            fail(f"{sib} is not beside ElmerSolver in the container ({path!r})")

print("\n== Host FreeCAD ==")
fc = client.FREECADCMD
if os.environ.get("PREFLIGHT_HOST_FREECAD") == "0":
    print("  skip  PREFLIGHT_HOST_FREECAD=0 (no suite runs after this preflight)")
elif os.path.isfile(fc) and os.access(fc, os.X_OK):
    ok(f"freecadcmd -> {fc}")
else:
    fail(f"freecadcmd does not resolve ({fc!r}) — test_wind_tunnel drives the real worker")

if failed:
    print(f"\nPreflight FAILED: {failed} check(s). The live tests would have SKIPped or "
          "passed without crossing the relay.")
    sys.exit(1)
print("\nPreflight passed.")
PYEOF

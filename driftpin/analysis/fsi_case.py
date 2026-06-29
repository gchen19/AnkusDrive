"""Partitioned FSI case generation + coupled-run orchestration for the preCICE
OpenFOAM ↔ CalculiX stack (the executable twin of ``driftpin/analysis/fsi.py``).

Pure-Python, FreeCAD-free. This module materialises a *runnable* preCICE FSI
case from the vendored ``fsi_template/`` (the perpendicular-flap tutorial,
LGPL-3.0, parameterised) and drives the two coupled subprocesses — OpenFOAM
``pimpleFoam`` (fluid, writes Force) and the preCICE-enabled ``ccx_preCICE``
(solid, writes Displacement) — never touching FreeCAD. The worker exports the
plate/channel geometry on the main thread; the job here runs the solve on the
exported case.

The stack is resolved through ``driftpin.solvers`` (the ``fsi`` family gates on
``precice`` + both adapters); on a miss every entry point degrades to a
structured ``{ok: False, ...}`` dict rather than raising. The heavy solvers run
as their own subprocesses (the arm's-length boundary OpenFOAM/Elmer already use);
preCICE is LGPL and only ever invoked out-of-process here.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from .. import solvers

_TEMPLATE = os.path.join(os.path.dirname(__file__), "fsi_template")


# --- case materialisation -----------------------------------------------------

def write_fsi_case(
    case_dir: str,
    *,
    inlet_velocity_m_s: float = 10.0,
    nu_m2_s: float = 1.0,
    rho_kg_m3: float = 1.0,
    youngs_pa: float = 4.0e6,
    poisson: float = 0.3,
    density_kg_m3: float = 3000.0,
    end_time_s: float = 5.0,
    time_window_s: float = 0.01,
    max_iterations: int = 50,
) -> dict:
    """Materialise a runnable preCICE FSI case (fluid-openfoam + solid-calculix +
    precice-config.xml) into ``case_dir`` from the vendored flap template,
    substituting the fluid inlet velocity / viscosity / density, the solid
    Young's modulus / Poisson / density, and the coupling time window + end time.

    Geometry (the flexible flap clamped at the channel floor, the channel mesh)
    comes from the validated template; the *physics* are parameterised so the
    coupled solve can be steered to the small-deflection operating point the
    ``fsi.plate_deflection`` oracle anchors. Returns
    ``{case_dir, fluid_dir, solid_dir, params}``."""
    if os.path.exists(case_dir):
        shutil.rmtree(case_dir)
    shutil.copytree(_TEMPLATE, case_dir)
    fluid = os.path.join(case_dir, "fluid-openfoam")
    solid = os.path.join(case_dir, "solid-calculix")

    def _sub(path, pairs):
        with open(path) as fh:
            txt = fh.read()
        for pat, repl in pairs:
            txt = re.sub(pat, repl, txt)
        with open(path, "w") as fh:
            fh.write(txt)

    # fluid: inlet velocity (0/U internalField), nu (transportProperties),
    # controlDict end time + write interval
    _sub(os.path.join(fluid, "0", "U"),
         [(r"internalField\s+uniform \([^)]*\);",
           f"internalField   uniform ({inlet_velocity_m_s} 0 0);")])
    _sub(os.path.join(fluid, "constant", "transportProperties"),
         [(r"nu\s+nu \[[^\]]*\]\s+[0-9eE.+-]+;",
           f"nu              nu [ 0 2 -1 0 0 0 0 ] {nu_m2_s};")])
    _sub(os.path.join(fluid, "system", "controlDict"),
         [(r"endTime\s+[0-9eE.+-]+;", f"endTime         {end_time_s};"),
          (r"deltaT\s+[0-9eE.+-]+;", f"deltaT          {time_window_s};")])
    # fluid preciceDict rho
    pd = os.path.join(fluid, "system", "preciceDict")
    if os.path.exists(pd):
        _sub(pd, [(r"rho rho \[[^\]]*\]\s+[0-9eE.+-]+;",
                   f"rho rho [1 -3 0 0 0 0 0] {rho_kg_m3};")])

    # solid: *ELASTIC E,nu and *DENSITY and *DYNAMIC step time
    inp = os.path.join(solid, "flap.inp")
    _sub(inp,
         [(r"\*ELASTIC\s*\n\s*[0-9eE.+-]+,\s*[0-9eE.+-]+",
           f"*ELASTIC\n {youngs_pa}, {poisson}"),
          (r"\*DENSITY\s*\n\s*[0-9eE.+-]+",
           f"*DENSITY\n {density_kg_m3}"),
          (r"\*DYNAMIC, ALPHA=0\.0, DIRECT\s*\n\s*[0-9eE.+-]+,\s*[0-9eE.+-]+",
           f"*DYNAMIC, ALPHA=0.0, DIRECT\n{time_window_s}, {end_time_s}")])

    # precice-config.xml: time window, end time, max iterations
    cfg = os.path.join(case_dir, "precice-config.xml")
    _sub(cfg,
         [(r'<time-window-size value="[^"]*" />',
           f'<time-window-size value="{time_window_s}" />'),
          (r'<max-time value="[^"]*" />',
           f'<max-time value="{end_time_s}" />'),
          (r'<max-iterations value="[^"]*" />',
           f'<max-iterations value="{max_iterations}" />')])

    return {
        "case_dir": case_dir,
        "fluid_dir": fluid,
        "solid_dir": solid,
        "params": {
            "inlet_velocity_m_s": inlet_velocity_m_s,
            "nu_m2_s": nu_m2_s,
            "rho_kg_m3": rho_kg_m3,
            "youngs_pa": youngs_pa,
            "poisson": poisson,
            "density_kg_m3": density_kg_m3,
            "end_time_s": end_time_s,
            "time_window_s": time_window_s,
            "max_iterations": max_iterations,
        },
    }


# --- coupled run --------------------------------------------------------------

def _adapter_env() -> dict:
    """Build the environment that puts libprecice + ccx_preCICE + the OpenFOAM
    function-object adapter on the runtime paths. Resolved through
    ``solvers`` (env overrides honoured)."""
    env = dict(os.environ)
    lib = solvers.precice_lib_dir()
    if lib:
        env["LD_LIBRARY_PATH"] = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    ofa = solvers.openfoam_adapter_lib_dir()
    if ofa:
        env["FOAM_USER_LIBBIN"] = ofa
    ccxbin = solvers.ccx_precice_bin()
    if ccxbin:
        env["PATH"] = os.path.dirname(ccxbin) + os.pathsep + env.get("PATH", "")
    env.setdefault("OMP_NUM_THREADS", "4")
    return env


def run_coupled_fsi(case_dir: str, *, timeout_s: int = 600) -> dict:
    """Run the partitioned preCICE OpenFOAM↔CalculiX solve in ``case_dir``:
    blockMesh, then ``pimpleFoam`` (Fluid) and ``ccx_preCICE`` (Solid)
    concurrently coupled over preCICE sockets. Parses the flap-tip watch-point
    log for the final tip displacement.

    Returns ``{ok, returncode_fluid, returncode_solid, time_windows, tip_disp_m,
    tip_history, coupling_converged, log_tail}``. ``ok`` is true when both
    participants exit 0 and preCICE reached the final time window."""
    fluid = os.path.join(case_dir, "fluid-openfoam")
    solid = os.path.join(case_dir, "solid-calculix")
    # The fluid must source the OpenFOAM version the preCICE adapter was built
    # against, NOT just any OpenFOAM — a version mismatch makes pimpleFoam abort
    # at startup (can't load the function-object adapter) and hangs the Solid at
    # the handshake. See solvers.fsi_openfoam_bashrc().
    of_bashrc = solvers.fsi_openfoam_bashrc()
    ccxbin = solvers.ccx_precice_bin()
    lib = solvers.precice_lib_dir()
    if not (of_bashrc and ccxbin and lib):
        return {"ok": False, "reason": "fsi stack not fully resolved",
                "openfoam_bashrc": of_bashrc, "ccx_precice": ccxbin,
                "precice_lib": lib}

    env = _adapter_env()
    # clean stale preCICE coupling dir
    for stale in ("precice-run", "precice-profiling"):
        p = os.path.join(case_dir, stale)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    # 1) blockMesh (fluid mesh) — needs the OpenFOAM env
    bm = subprocess.run(
        ["bash", "-c", f"source '{of_bashrc}' >/dev/null 2>&1 && blockMesh"],
        cwd=fluid, env=env, capture_output=True, text=True, timeout=timeout_s)
    if bm.returncode != 0:
        return {"ok": False, "reason": "blockMesh failed",
                "log_tail": (bm.stdout + bm.stderr)[-1500:]}

    # 2) launch Solid (ccx_preCICE) then Fluid (pimpleFoam), wait on both.
    # The solid only needs libprecice on the path; the fluid needs OpenFOAM's full
    # env (sourced bashrc) with libprecice PREPENDED — replacing LD_LIBRARY_PATH
    # after the source would drop OF's own libs (libregionFaModels.so etc.) and
    # pimpleFoam fails to load. So we prepend, never clobber.
    solid_log = os.path.join(case_dir, "solid.log")
    fluid_log = os.path.join(case_dir, "fluid.log")
    ld = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    with open(solid_log, "w") as sl:
        solid_proc = subprocess.Popen(
            [ccxbin, "-i", "flap", "-precice-participant", "Solid"],
            cwd=solid, env={**env, "LD_LIBRARY_PATH": ld}, stdout=sl,
            stderr=subprocess.STDOUT)
    time.sleep(2.0)  # let the solid participant bind the preCICE socket first
    with open(fluid_log, "w") as fl:
        fluid_proc = subprocess.Popen(
            ["bash", "-c",
             f"source '{of_bashrc}' >/dev/null 2>&1; "
             f"export LD_LIBRARY_PATH='{lib}':\"$LD_LIBRARY_PATH\"; pimpleFoam"],
            cwd=fluid, env=env, stdout=fl, stderr=subprocess.STDOUT)

    deadline = time.time() + timeout_s
    rc_fluid = rc_solid = None
    try:
        while time.time() < deadline:
            if fluid_proc.poll() is not None and solid_proc.poll() is not None:
                break
            time.sleep(1.0)
        rc_fluid = fluid_proc.poll()
        rc_solid = solid_proc.poll()
    finally:
        for proc in (fluid_proc, solid_proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    tip = parse_tip_watchpoint(solid)
    windows = _count_time_windows(solid_log)
    converged = _reached_end(solid_log)
    tail = ""
    if os.path.exists(solid_log):
        with open(solid_log) as fh:
            tail = fh.read()[-1500:]

    ok = (rc_fluid == 0 and rc_solid == 0 and converged)
    return {
        "ok": ok,
        "returncode_fluid": rc_fluid,
        "returncode_solid": rc_solid,
        "time_windows": windows,
        "tip_disp_m": tip.get("tip_disp_m") if tip else None,
        "tip_history": tip.get("history") if tip else None,
        "coupling_converged": converged,
        "log_tail": tail,
    }


def parse_tip_watchpoint(solid_dir: str) -> dict | None:
    """Parse the preCICE flap-tip watch-point log (Displacement0/1 columns) →
    ``{tip_disp_m (final, magnitude), history: [(t, dx, dy), ...]}``."""
    cand = None
    for fn in os.listdir(solid_dir):
        if "watchpoint" in fn.lower() and fn.endswith(".log"):
            cand = os.path.join(solid_dir, fn)
            break
    if not cand or not os.path.exists(cand):
        return None
    rows = []
    with open(cand) as fh:
        lines = fh.readlines()
    if not lines:
        return None
    header = lines[0].split()
    try:
        di = [header.index(c) for c in header if c.startswith("Displacement")]
        ti = 0  # Time is always first column
    except ValueError:
        return None
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) < max(di) + 1:
            continue
        try:
            t = float(parts[ti])
            dx = float(parts[di[0]])
            dy = float(parts[di[1]]) if len(di) > 1 else 0.0
        except (ValueError, IndexError):
            continue
        rows.append((t, dx, dy))
    if not rows:
        return None
    _, dx, dy = rows[-1]
    mag = (dx * dx + dy * dy) ** 0.5
    return {"tip_disp_m": mag, "history": rows}


def _count_time_windows(log_path: str) -> int:
    if not os.path.exists(log_path):
        return 0
    seen = set()
    with open(log_path) as fh:
        for ln in fh:
            m = re.search(r"time-window (\d+),", ln)
            if m:
                seen.add(int(m.group(1)))
    return len(seen)


def _reached_end(log_path: str) -> bool:
    if not os.path.exists(log_path):
        return False
    with open(log_path) as fh:
        txt = fh.read()
    return "Reached end at" in txt or "final time-window" in txt

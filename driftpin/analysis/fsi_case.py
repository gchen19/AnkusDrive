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
        with open(path, encoding="utf-8") as fh:
            txt = fh.read()
        for pat, repl in pairs:
            txt = re.sub(pat, repl, txt)
        with open(path, "w", encoding="utf-8") as fh:
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

def _participant_script(setup_lines: list, exec_line: str) -> str:
    """A participant launch script: record the pid, then ``exec`` the solver so
    the recorded pid IS the solver's (bash replaces itself). All environment
    (LD_LIBRARY_PATH, FOAM_USER_LIBBIN, ...) crosses as export lines in the
    script text — under a substrate relay (WSL on Windows, Multipass on macOS,
    ``solvers.bash_argv``, issue #193) an ``env=`` on the relay Popen would never
    reach the in-substrate solver; on Linux the exports are equivalent to the old
    ``env=`` dict. The pidfile is what ``_stop_participant`` kills behind a relay,
    where terminating the Popen only reaches the relay, not the solver inside it."""
    lines = ["echo $$ > .driftpin-participant.pid"] + [l for l in setup_lines if l]
    return "\n".join(lines) + f"\nexec {exec_line}"


def _stop_participant(proc, workdir: str) -> None:
    """Stop a participant: terminate/kill the Popen, then — when the solver ran
    behind a substrate relay (WSL on Windows, Multipass on macOS, issue #193) —
    sweep the in-substrate process by the pidfile its own launch script wrote
    (case-scoped, so concurrent FSI runs never kill each other). Killing the Popen
    only reaches the relay (wsl.exe / multipass), not the solver inside it, so the
    pidfile kill is what actually stops it. Best-effort — a participant that already
    exited leaves a stale pid that kill quietly misses."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if solvers.runs_in_substrate():
        try:
            # the kill runs in the SAME substrate (workdir → the VM-mounted case dir
            # on macOS) so it reads the pidfile and kills the solver by its in-VM pid.
            subprocess.run(solvers.bash_argv(
                "kill -TERM $(cat .driftpin-participant.pid 2>/dev/null) "
                "2>/dev/null; sleep 2; "
                "kill -KILL $(cat .driftpin-participant.pid 2>/dev/null) "
                "2>/dev/null; true", workdir),
                cwd=workdir, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=30)
        except Exception:
            pass


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

    # clean stale preCICE coupling dir
    for stale in ("precice-run", "precice-profiling"):
        p = os.path.join(case_dir, stale)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    # 1) blockMesh (fluid mesh) — needs the OpenFOAM env. The workdir is passed to
    # bash_argv so the macOS Multipass branch cd's into the (VM-mounted) case dir
    # (issue #193); Linux/Windows ignore it and use the subprocess cwd.
    #
    # Every launch below pins stdin=DEVNULL. These run on a worker JOB THREAD whose
    # stdin is the client's JSON-RPC pipe, and `multipass exec` forwards stdin into
    # the VM — an inherited stdin lets the relay eat the caller's next request, so
    # the solve succeeds while every subsequent poll times out. Verified live.
    bm = subprocess.run(
        solvers.bash_argv(f"source '{of_bashrc}' >/dev/null 2>&1 && blockMesh", fluid),
        cwd=fluid, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=timeout_s)
    if bm.returncode != 0:
        return {"ok": False, "reason": "blockMesh failed",
                "log_tail": solvers.clean_wsl_text(bm.stdout + bm.stderr)[-1500:]}

    # 2) launch Solid (ccx_preCICE) then Fluid (pimpleFoam), wait on both. All
    # runtime env lives in the script text (see _participant_script). The solid
    # only needs libprecice on the loader path; the fluid needs OpenFOAM's full
    # env (sourced bashrc) with libprecice PREPENDED — replacing LD_LIBRARY_PATH
    # after the source would drop OF's own libs (libregionFaModels.so etc.) and
    # pimpleFoam fails to load. So we prepend, never clobber. FOAM_USER_LIBBIN
    # exports AFTER the source for the same reason (the bashrc recomputes it).
    solid_log = os.path.join(case_dir, "solid.log")
    fluid_log = os.path.join(case_dir, "fluid.log")

    solid_script = _participant_script(
        [f"export LD_LIBRARY_PATH='{lib}':\"$LD_LIBRARY_PATH\"",
         'export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"'],
        f"'{ccxbin}' -i flap -precice-participant Solid")
    ofa = solvers.openfoam_adapter_lib_dir()
    fluid_script = _participant_script(
        [f"source '{of_bashrc}' >/dev/null 2>&1",
         f"export LD_LIBRARY_PATH='{lib}':\"$LD_LIBRARY_PATH\"",
         (f"export FOAM_USER_LIBBIN='{ofa}'" if ofa else ""),
         'export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"'],
        "pimpleFoam")

    with open(solid_log, "w", encoding="utf-8") as sl:
        solid_proc = subprocess.Popen(
            solvers.bash_argv(solid_script, solid),
            cwd=solid, stdin=subprocess.DEVNULL, stdout=sl,
            stderr=subprocess.STDOUT)
    time.sleep(2.0)  # let the solid participant bind the preCICE socket first
    with open(fluid_log, "w", encoding="utf-8") as fl:
        fluid_proc = subprocess.Popen(
            solvers.bash_argv(fluid_script, fluid),
            cwd=fluid, stdin=subprocess.DEVNULL, stdout=fl,
            stderr=subprocess.STDOUT)

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
        _stop_participant(fluid_proc, fluid)
        _stop_participant(solid_proc, solid)

    tip = parse_tip_watchpoint(solid)
    windows = _count_time_windows(solid_log)
    converged = _reached_end(solid_log)
    tail = ""
    if os.path.exists(solid_log):
        with open(solid_log, encoding="utf-8") as fh:
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
    with open(cand, encoding="utf-8") as fh:
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
    with open(log_path, encoding="utf-8") as fh:
        for ln in fh:
            m = re.search(r"time-window (\d+),", ln)
            if m:
                seen.add(int(m.group(1)))
    return len(seen)


def _reached_end(log_path: str) -> bool:
    if not os.path.exists(log_path):
        return False
    with open(log_path, encoding="utf-8") as fh:
        txt = fh.read()
    return "Reached end at" in txt or "final time-window" in txt

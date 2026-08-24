"""OpenFOAM case generation — the heavy-solver case-from-geometry half of P2 family 6.

Pure-Python, FreeCAD-free (the case text is testable on the no-FreeCAD lane; the
*solve* needs the OpenFOAM binaries). The CFD family (``docs/archive/SIMULATION_P2_KICKOFF.md``
M5) rides on OpenFOAM; the kickoff's "unambiguous gate" is the **straight circular
pipe**, whose laminar pressure drop is the exact Hagen–Poiseuille law

    Δp = 128·μ·L·Q / (π·D⁴)   (= 32·μ·U·L / D²),

with its sharp D⁴ scaling. ``ankusdrive.analysis.cfd.pipe_pressure_drop`` is that
analytic oracle; this module builds the matching CFD case so the solve can be gated
against it.

Geometry — an **axisymmetric wedge** of the pipe: a single blockMesh hex collapsed
onto the axis (radius along +y, the thin wedge spanning ±``half_angle_deg`` in z, the
axis edge at r=0), so the 3-D circular pipe is captured by an essentially 2-D mesh
(``wedge`` front/back patches). A long, low-Reynolds pipe with a uniform inlet keeps
the entrance length a small fraction of L, so the inlet-to-outlet drop is
Hagen–Poiseuille to within a few percent (and ``2·mean(p)`` over the developed column
recovers it almost exactly). ``simpleFoam`` (steady, laminar) solves it.

Pressure is read straight from the converged ``p`` field — not via a SaveScalars-style
function object (the ``surfaceFieldValue`` writer is broken in some OpenFOAM builds).
With the outlet pinned to p=0 the maximum of p is the inlet (= Δp); ``2·mean(p)`` is
the same drop inferred from the (linear) developed profile. OpenFOAM ``p`` is kinematic
(m²/s²), so Δp_Pa = ρ · p. Validated vs Hagen–Poiseuille to < 3 % (Re=50, L=50·D).

Units are SI (m, m/s, m²/s, kg/m³, Pa). See ``tests/test_openfoam.py`` for the
structure gate and the solver-backed Hagen–Poiseuille / D⁴ gate.
"""
from __future__ import annotations

import math
import os
import re


def _header(cls: str, obj: str, location: str) -> str:
    return (
        "FoamFile\n{\n"
        "    version     2.0;\n"
        "    format      ascii;\n"
        f"    class       {cls};\n"
        f'    location    "{location}";\n'
        f"    object      {obj};\n"
        "}\n"
    )


# SIMPLE's stopping criterion. `endTime` is only the CAP — a steady case is meant to
# stop the moment every controlled field's initial residual drops below its tolerance,
# and a case that runs to the cap instead is, by its own definition, not converged
# (issue #225 exists because that state used to be invisible in the payload).
_RESIDUAL_CONTROL = {"p": 1e-7, "U": 1e-7}


def _residual_control_text(control: dict | None) -> str:
    items = " ".join(f"{k} {v:.10g};" for k, v in (control or _RESIDUAL_CONTROL).items())
    return f"residualControl {{ {items} }}"


def steady_laminar_common_files(*, nu_m2_s: float, end_time: int,
                                residual_control: dict | None = None) -> dict:
    """The case files every central-scheme steady laminar ``simpleFoam`` builder
    shares verbatim — ``constant/transportProperties`` (Newtonian ``nu_m2_s``),
    ``constant/turbulenceProperties`` (laminar), ``system/controlDict`` (``end_time``
    iterations, final write only) and the central-scheme ``system/fvSchemes`` /
    ``system/fvSolution`` (SIMPLEC, Gauss linear) used by the wedge pipe and the M4
    snappy bridge. The flat plate keeps its own bounded-linearUpwind variants, NOT
    these.

    ``residual_control`` overrides SIMPLE's stopping criterion (default
    ``{p: 1e-7, U: 1e-7}``) — needed by geometries where one velocity component is a
    numerical artifact rather than a physical quantity; see ``pipe_case_files``.
    Returns ``{relpath: contents}`` for the caller to extend with its mesh and 0/
    fields."""
    if nu_m2_s <= 0:
        raise ValueError("nu_m2_s must be > 0")
    files = {}
    files["constant/transportProperties"] = (
        _header("dictionary", "transportProperties", "constant")
        + f"\ntransportModel  Newtonian;\nnu              {nu_m2_s:.10g};\n")
    files["constant/turbulenceProperties"] = (
        _header("dictionary", "turbulenceProperties", "constant")
        + "\nsimulationType  laminar;\n")
    files["system/controlDict"] = (
        _header("dictionary", "controlDict", "system")
        + "\napplication     simpleFoam;\nstartFrom       startTime;\nstartTime       0;\n"
        "stopAt          endTime;\n"
        f"endTime         {int(end_time)};\ndeltaT          1;\n"
        "writeControl    timeStep;\n"
        f"writeInterval   {int(end_time)};\npurgeWrite      1;\nwriteFormat     ascii;\n"
        "writePrecision  10;\nwriteCompression off;\ntimeFormat      general;\n"
        "runTimeModifiable false;\n")
    files["system/fvSchemes"] = (
        _header("dictionary", "fvSchemes", "system")
        + "\nddtSchemes { default steadyState; }\n"
        "gradSchemes { default Gauss linear; }\n"
        "divSchemes\n{\n    default none;\n    div(phi,U) bounded Gauss linear;\n"
        "    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n}\n"
        "laplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes { default corrected; }\n")
    files["system/fvSolution"] = (
        _header("dictionary", "fvSolution", "system")
        + "\nsolvers\n{\n"
        "    p { solver GAMG; smoother GaussSeidel; tolerance 1e-9; relTol 0.01; }\n"
        "    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }\n"
        "}\n"
        "SIMPLE\n{\n    nNonOrthogonalCorrectors 2;\n    consistent yes;\n"
        f"    {_residual_control_text(residual_control)}\n}}\n"
        "relaxationFactors { equations { U 0.9; } fields { p 0.9; } }\n")
    return files


def pipe_blockmeshdict(*, diameter_m: float, length_m: float,
                       half_angle_deg: float, n_axial: int, n_radial: int) -> str:
    """blockMeshDict for the axisymmetric wedge pipe (one collapsed-axis hex).

    Radius R=D/2 along +y, the wedge spanning ±``half_angle_deg`` about the x-y plane
    (z = ±r·tan(angle)); patches inlet (x=0), outlet (x=L), wall (r=R) and the two
    ``wedge`` faces. ``n_axial``×``n_radial`` cells."""
    if diameter_m <= 0 or length_m <= 0:
        raise ValueError("diameter_m and length_m must be > 0")
    if n_axial < 2 or n_radial < 2:
        raise ValueError("n_axial and n_radial must each be >= 2")
    r = diameter_m / 2.0
    z = r * math.tan(math.radians(half_angle_deg))
    L = length_m
    return _header("dictionary", "blockMeshDict", "system") + f"""
scale   1;

vertices
(
    (0    0     0)
    ({L:.10g} 0     0)
    (0    {r:.10g} {-z:.10g})
    ({L:.10g} {r:.10g} {-z:.10g})
    (0    {r:.10g} {z:.10g})
    ({L:.10g} {r:.10g} {z:.10g})
);

blocks
(
    hex (0 1 3 2 0 1 5 4) ({int(n_axial)} {int(n_radial)} 1) simpleGrading (1 1 1)
);

edges ();

boundary
(
    inlet  {{ type patch; faces ((0 2 4 0)); }}
    outlet {{ type patch; faces ((1 3 5 1)); }}
    wall   {{ type wall;  faces ((2 3 5 4)); }}
    wedge1 {{ type wedge; faces ((0 1 3 2)); }}
    wedge2 {{ type wedge; faces ((0 1 5 4)); }}
);

mergePatchPairs ();
"""


def pipe_case_files(*, diameter_m: float, length_m: float, velocity_m_s: float,
                    nu_m2_s: float, half_angle_deg: float = 2.5,
                    n_axial: int = 120, n_radial: int = 15,
                    end_time: int = 4000) -> dict:
    """Every text file of the steady laminar pipe case as ``{relpath: contents}``.

    A uniform inlet (``velocity_m_s`` in +x), zero-pressure outlet, no-slip wall and
    the two wedge patches; ``simpleFoam`` (SIMPLEC, central schemes) on the
    ``pipe_blockmeshdict`` mesh with kinematic viscosity ``nu_m2_s``. Returns the dict
    the caller writes into a case directory (keys: ``system/blockMeshDict``,
    ``constant/transportProperties``, ``constant/turbulenceProperties``, ``0/U``,
    ``0/p``, ``system/{controlDict,fvSchemes,fvSolution}``)."""
    U = velocity_m_s
    # U is deliberately ABSENT from this case's stopping criterion. The two wedge
    # patches pin an essentially 2-D solution into a 3-D solver, so the out-of-plane
    # momentum residual is normalized by a near-zero field and is round-off noise, not
    # a convergence measure: measured live, Uz floors at ~1.6e-5 REGARDLESS of mesh
    # density while Ux reaches 5.8e-16. Gating on U therefore never trips and the case
    # runs to endTime — 3000+ iterations for an answer that was complete at 74. With p
    # alone the pipe converges in 74 iterations (n_axial=80) / 102 (n_axial=180) to a
    # pressure drop identical to the 3000-iteration one to six decimals, with Ux at
    # 1.8e-8. Nothing is hidden by this: the trust block reports every field's measured
    # final residual, so a caller can see Uz for themselves.
    files = steady_laminar_common_files(
        nu_m2_s=nu_m2_s, end_time=end_time, residual_control={"p": 1e-7})
    files["system/blockMeshDict"] = pipe_blockmeshdict(
        diameter_m=diameter_m, length_m=length_m, half_angle_deg=half_angle_deg,
        n_axial=n_axial, n_radial=n_radial)
    files["0/U"] = (
        _header("volVectorField", "U", "0")
        + "\ndimensions      [0 1 -1 0 0 0 0];\n"
        "internalField   uniform (0 0 0);\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type fixedValue; value uniform ({U:.10g} 0 0); }}\n"
        "    outlet { type zeroGradient; }\n"
        "    wall   { type noSlip; }\n"
        "    wedge1 { type wedge; }\n"
        "    wedge2 { type wedge; }\n}\n")
    files["0/p"] = (
        _header("volScalarField", "p", "0")
        + "\ndimensions      [0 2 -2 0 0 0 0];\n"
        "internalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type zeroGradient; }\n"
        "    outlet { type fixedValue; value uniform 0; }\n"
        "    wall   { type zeroGradient; }\n"
        "    wedge1 { type wedge; }\n"
        "    wedge2 { type wedge; }\n}\n")
    return files


def write_pipe_case(case_dir: str, *, diameter_m: float, length_m: float,
                    velocity_m_s: float, nu_m2_s: float, half_angle_deg: float = 2.5,
                    n_axial: int = 120, n_radial: int = 15,
                    end_time: int = 4000) -> dict:
    """Write a complete, runnable OpenFOAM pipe case under ``case_dir``.

    Lays down ``0/``, ``constant/`` and ``system/`` with the files from
    ``pipe_case_files``. Returns metadata ``{case_dir, diameter_m, length_m,
    velocity_m_s, nu_m2_s, reynolds, n_axial, n_radial, end_time}`` (Re = U·D/ν)."""
    files = pipe_case_files(
        diameter_m=diameter_m, length_m=length_m, velocity_m_s=velocity_m_s,
        nu_m2_s=nu_m2_s, half_angle_deg=half_angle_deg, n_axial=n_axial,
        n_radial=n_radial, end_time=end_time)
    for rel, text in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return {
        "case_dir": case_dir,
        "diameter_m": diameter_m,
        "length_m": length_m,
        "velocity_m_s": velocity_m_s,
        "nu_m2_s": nu_m2_s,
        "reynolds": velocity_m_s * diameter_m / nu_m2_s if nu_m2_s else float("inf"),
        "n_axial": n_axial,
        "n_radial": n_radial,
        "end_time": end_time,
    }


def _latest_time_dir(case_dir: str) -> str | None:
    """The numeric time directory with the largest value (the converged result), or
    None if only the 0/ directory exists."""
    best, best_val = None, -1.0
    for name in os.listdir(case_dir):
        if not os.path.isdir(os.path.join(case_dir, name)):
            continue
        try:
            v = float(name)
        except ValueError:
            continue
        if v > best_val:
            best, best_val = name, v
    return best if best_val > 0 else None


def _read_internal_scalar_field(path: str):
    """Parse the internalField of an OpenFOAM scalar field file into a list of floats
    (handles both ``nonuniform List<scalar>`` and a single ``uniform`` value). Returns
    None on a shape it can't read."""
    txt = open(path, encoding="utf-8").read()
    m = re.search(r"internalField\s+nonuniform\s+List<scalar>\s*\n\s*(\d+)\s*\n\(\s*(.*?)\)\s*;",
                  txt, re.S)
    if m:
        return [float(x) for x in m.group(2).split()]
    m = re.search(r"internalField\s+uniform\s+([-\d.eE+]+)\s*;", txt)
    if m:
        return [float(m.group(1))]
    return None


def parse_pressure_drop(case_dir: str, *, rho_kg_m3: float,
                        time_dir: str | None = None) -> dict | None:
    """Read the converged ``p`` field and return the pipe pressure drop.

    ``{dp_inlet_pa, dp_developed_pa, p_max, p_mean, n_cells, time}`` where (outlet
    pinned to 0) ``dp_inlet_pa = ρ·max(p)`` is the true inlet-to-outlet drop and
    ``dp_developed_pa = ρ·2·mean(p)`` is the same drop inferred from the linear
    developed profile (the one that cancels entrance/exit effects). OpenFOAM ``p`` is
    kinematic, hence the ρ factor. Returns None when no converged field is present
    (the solve failed)."""
    time = time_dir or _latest_time_dir(case_dir)
    if time is None:
        return None
    pfile = os.path.join(case_dir, time, "p")
    if not os.path.isfile(pfile):
        return None
    vals = _read_internal_scalar_field(pfile)
    if not vals:
        return None
    p_max = max(vals)
    p_mean = sum(vals) / len(vals)
    return {
        "dp_inlet_pa": rho_kg_m3 * p_max,
        "dp_developed_pa": rho_kg_m3 * 2.0 * p_mean,
        "p_max": p_max,
        "p_mean": p_mean,
        "n_cells": len(vals),
        "time": time,
    }


# --- forces and moments on a patch (issue #224) --------------------------------
#
# The `forces` function object integrates the pressure and viscous traction over a
# set of patches every iteration and writes postProcessing/forces/<t>/{force,moment}.dat.
# Historical note: the P3 flat-plate work found forces/forceCoeffs aborting with a
# 'sha1' IOstream error and worked around it by hand-integrating drag from the raw U
# field (parse_flat_plate_drag). That was a property of THAT OpenFOAM build — verified
# working on OpenFOAM v2512 (ESI), where the FO writes both files cleanly and its pipe
# wall-shear force matches the Hagen-Poiseuille wall traction to ~1 %. The hand
# integration stays as the flat plate's primary number (it is gated and byte-stable);
# the FO is what makes drag on an ARBITRARY body possible at all, since there is no
# indexable cell layout to hand-integrate on a snapped mesh.
#
# rho: incompressible OpenFOAM p is kinematic (m^2/s^2), so the FO is told `rho rhoInf`
# + the density and returns forces in NEWTONS. writeInterval is 1 on purpose — the run
# stops the moment SIMPLE's residualControl is met, which is almost never a multiple of
# a coarser interval, and a force file that was never written is indistinguishable
# downstream from a solve that produced no force.


def forces_function_object(*, patches, rho_kg_m3: float,
                           centre_of_rotation=(0.0, 0.0, 0.0),
                           name: str = "forces") -> str:
    """The ``functions { … }`` block appending a ``forces`` function object to a
    controlDict: pressure + viscous force and moment over ``patches``, written every
    iteration to ``postProcessing/<name>/<startTime>/{force,moment}.dat`` in newtons
    (``rho rhoInf`` + ``rhoInf`` = ``rho_kg_m3``, since incompressible p is kinematic).
    Moments are about ``centre_of_rotation``. Returns the text to CONCATENATE onto the
    controlDict; parse the result with :func:`parse_forces`. Raises ValueError on an
    empty patch list or non-positive density."""
    names = [str(p) for p in (patches or [])]
    if not names or any((not p) or " " in p for p in names):
        raise ValueError("patches must be a non-empty list of patch names")
    if rho_kg_m3 <= 0:
        raise ValueError("rho_kg_m3 must be > 0")
    cofr = tuple(float(v) for v in centre_of_rotation)
    if len(cofr) != 3:
        raise ValueError("centre_of_rotation must be a 3-vector")
    return f"""
functions
{{
    {name}
    {{
        type            forces;
        libs            ("libforces.so");
        writeControl    timeStep;
        writeInterval   1;
        log             true;
        patches         ({" ".join(names)});
        rho             rhoInf;
        rhoInf          {rho_kg_m3:.10g};
        CofR            ({cofr[0]:.10g} {cofr[1]:.10g} {cofr[2]:.10g});
    }}
}}
"""


def _last_force_row(path: str):
    """Last non-comment row of a forces ``*.dat`` as a list of floats, or None."""
    try:
        rows = [r for r in open(path, encoding="utf-8").read().splitlines()
                if r.strip() and not r.lstrip().startswith("#")]
    except OSError:
        return None
    for row in reversed(rows):
        try:
            return [float(v) for v in row.replace("(", " ").replace(")", " ").split()]
        except ValueError:
            continue
    return None


def parse_forces(case_dir: str, *, name: str = "forces",
                 velocity_m_s: float | None = None,
                 rho_kg_m3: float | None = None,
                 reference_area_m2: float | None = None,
                 reference_length_m: float | None = None,
                 flow_direction=(1.0, 0.0, 0.0),
                 lift_direction=None) -> dict | None:
    """Read the converged force/moment integral written by :func:`forces_function_object`
    and resolve it into drag, lift and (optionally) the coefficients.

    Reads the LAST row of ``postProcessing/<name>/<t>/force.dat`` (highest start time)
    and its ``moment.dat`` twin: total / pressure / viscous vectors, in newtons and
    newton-metres. Drag is the total force projected on ``flow_direction``; lift is the
    projection on ``lift_direction`` (default: the component of +z orthogonal to the
    flow, falling back to +y when the flow is along z). The pressure/viscous split is
    reported separately — for a bluff body form drag dominates, for a streamlined one
    friction does, and a body whose "drag" is ~100 % pressure at low Re is a signal the
    mesh never resolved the boundary layer.

    Coefficients need all of ``velocity_m_s``, ``rho_kg_m3`` and ``reference_area_m2``
    (``cd``/``cl``); ``reference_length_m`` additionally gives ``cm``. Without them the
    force vectors come back and the coefficient keys are None.

    Also returns ``n_samples`` and ``force_drift_pct`` — |ΔD| over the last two written
    samples as a percentage of the final drag. A converged steady solve drifts ≲0.1 %
    per iteration; a large drift means the force has not settled and the number should
    not be quoted. Returns None when the function object wrote nothing (the solve
    failed, or the FO never ran)."""
    root = os.path.join(case_dir, "postProcessing", name)
    if not os.path.isdir(root):
        return None
    times = []
    for d in os.listdir(root):
        try:
            times.append((float(d), d))
        except ValueError:
            continue
    if not times:
        return None
    tdir = os.path.join(root, max(times)[1])
    fpath = os.path.join(tdir, "force.dat")
    row = _last_force_row(fpath)
    if not row or len(row) < 10:
        return None
    total = tuple(row[1:4])
    pressure = tuple(row[4:7])
    viscous = tuple(row[7:10])

    d = tuple(float(v) for v in flow_direction)
    dmag = math.sqrt(sum(v * v for v in d))
    if len(d) != 3 or dmag == 0:
        raise ValueError("flow_direction must be a non-zero 3-vector")
    d = tuple(v / dmag for v in d)
    if lift_direction is None:
        up = (0.0, 0.0, 1.0) if abs(d[2]) < 0.9 else (0.0, 1.0, 0.0)
        proj = sum(up[i] * d[i] for i in range(3))
        lift_direction = tuple(up[i] - proj * d[i] for i in range(3))
    lift_direction = tuple(float(v) for v in lift_direction)
    lmag = math.sqrt(sum(v * v for v in lift_direction))
    lhat = tuple(v / lmag for v in lift_direction) if lmag else (0.0, 0.0, 0.0)

    def dot(v, u):
        return sum(v[i] * u[i] for i in range(3))

    drag = dot(total, d)
    lift = dot(total, lhat)

    # convergence witness: how much the drag moved over the last written interval
    rows = [r for r in open(fpath, encoding="utf-8").read().splitlines()
            if r.strip() and not r.lstrip().startswith("#")]
    drift = None
    if len(rows) >= 2:
        prev = [float(v) for v in
                rows[-2].replace("(", " ").replace(")", " ").split()]
        if len(prev) >= 4 and drag != 0:
            drift = abs(dot(tuple(prev[1:4]), d) - drag) / abs(drag) * 100.0

    out = {
        "time": max(times)[1],
        "force_total_n": total,
        "force_pressure_n": pressure,
        "force_viscous_n": viscous,
        "drag_force_n": drag,
        "drag_pressure_n": dot(pressure, d),
        "drag_viscous_n": dot(viscous, d),
        "lift_force_n": lift,
        "flow_direction": d,
        "lift_direction": lhat,
        "n_samples": len(rows),
        "force_drift_pct": drift,
        "cd": None,
        "cl": None,
        "cm": None,
        "moment_total_nm": None,
    }
    mrow = _last_force_row(os.path.join(tdir, "moment.dat"))
    if mrow and len(mrow) >= 4:
        out["moment_total_nm"] = tuple(mrow[1:4])
    if velocity_m_s and rho_kg_m3 and reference_area_m2:
        q = 0.5 * float(rho_kg_m3) * float(velocity_m_s) ** 2
        denom = q * float(reference_area_m2)
        if denom > 0:
            out["cd"] = drag / denom
            out["cl"] = lift / denom
            if reference_length_m and out["moment_total_nm"]:
                # pitching moment about the axis normal to both flow and lift
                axis = (d[1] * lhat[2] - d[2] * lhat[1],
                        d[2] * lhat[0] - d[0] * lhat[2],
                        d[0] * lhat[1] - d[1] * lhat[0])
                out["cm"] = dot(out["moment_total_nm"], axis) / (
                    denom * float(reference_length_m))
    return out


# --- the trust layer: what the numerics actually did (issue #225) ---------------
#
# Every parser below reads an artifact the solver already writes, so nothing here
# changes a case or costs a solve. The point is that a steady CFD result which hit
# endTime unconverged, or converged beautifully on a mesh with 89-degree
# non-orthogonality, is INDISTINGUISHABLE in the payload from a good one — the
# numbers have the same shape and the same rc=0. These turn that into fields.
#
# `_run_foam` tees each app's output to <case_dir>/log.<app>, which is what
# parse_residuals and parse_checkmesh read. parse_yplus reads the postProcessing
# tree the `yPlus` function object writes (run as `simpleFoam -postProcess -func
# yPlus -latestTime`, which works for laminar cases too — nut is simply zero).


def parse_residuals(case_dir: str, *, app: str = "simpleFoam",
                    log_text: str | None = None) -> dict | None:
    """Convergence state and final residuals from a solver log.

    Reads ``<case_dir>/log.<app>`` (or ``log_text`` directly) and scrapes the SIMPLE
    iteration lines — ``smoothSolver:  Solving for Ux, Initial residual = 1.2e-08,
    …`` — keeping each field's LAST initial residual, which is the level the solution
    actually settled at. ``converged`` is True when the log carries "SIMPLE solution
    converged", False when the run reached its end marker without it (i.e. it ran out
    of iterations at ``endTime`` — a result that looks converged and is not), and None
    when the log says neither.

    Returns {converged, iterations, final_residuals: {field: value}, max_residual,
    fields}, or None when no log is readable. ``iterations`` is the last ``Time = N``
    reached, so an unconverged run reports the iteration cap it hit."""
    import glob as _glob
    text = log_text
    if text is None:
        path = os.path.join(case_dir, f"log.{app}")
        if not os.path.isfile(path):
            hits = sorted(_glob.glob(os.path.join(case_dir, "log.*Foam*")))
            if not hits:
                return None
            path = hits[-1]
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            return None
    if not text:
        return None

    residuals: dict[str, float] = {}
    for m in re.finditer(r"Solving for (\w+),\s*Initial residual = ([\d.eE+-]+)", text):
        try:
            residuals[m.group(1)] = float(m.group(2))
        except ValueError:
            continue
    iterations = None
    times = re.findall(r"^Time = (\d+)", text, re.M)
    if times:
        iterations = int(times[-1])
    m = re.search(r"SIMPLE solution converged in (\d+) iterations", text)
    if m:
        converged, iterations = True, int(m.group(1))
    elif re.search(r"^End\s*$", text, re.M):
        converged = False
    else:
        converged = None
    return {
        "converged": converged,
        "iterations": iterations,
        "final_residuals": {k: v for k, v in sorted(residuals.items())},
        "max_residual": (max(residuals.values()) if residuals else None),
        "fields": sorted(residuals),
    }


def parse_checkmesh(case_dir: str, log_text: str | None = None) -> dict | None:
    """Mesh quality from a ``checkMesh`` log (``<case_dir>/log.checkMesh``).

    checkMesh exits 0 whether the mesh passes or not — the verdict is in the TEXT
    ("Mesh OK." vs "Failed N mesh checks"), so this parses rather than trusting the
    return code. Surfaces the two numbers that actually decide whether a finite-volume
    solve can be believed: maximum non-orthogonality (the corrector-loop killer; > 70°
    is where OpenFOAM's own default gives up) and maximum skewness (> 4 internal / > 20
    boundary is checkMesh's own fail threshold).

    Returns {ok, failed_checks, max_non_orthogonality_deg, avg_non_orthogonality_deg,
    max_skewness, max_aspect_ratio, n_cells, n_faces, n_points, severe_warnings}, or
    None when no log is readable. ``ok`` False means checkMesh itself failed the mesh;
    the caller decides whether that is fatal, since a snapped mesh often carries a
    handful of bad cells that do not touch the region of interest."""
    text = log_text
    if text is None:
        path = os.path.join(case_dir, "log.checkMesh")
        if not os.path.isfile(path):
            return None
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            return None
    if not text:
        return None

    def _num(pattern, cast=float):
        m = re.search(pattern, text, re.M)           # the stats block is line-anchored
        try:
            return cast(m.group(1)) if m else None
        except ValueError:
            return None

    failed = _num(r"Failed (\d+) mesh checks", int)
    out = {
        "ok": failed in (None, 0) and "Mesh OK." in text,
        "failed_checks": failed or 0,
        "max_non_orthogonality_deg": _num(r"non-orthogonality Max:\s*([\d.eE+-]+)"),
        "avg_non_orthogonality_deg": _num(
            r"non-orthogonality Max:\s*[\d.eE+-]+\s*average:\s*([\d.eE+-]+)"),
        "max_skewness": _num(r"Max skewness = ([\d.eE+-]+)"),
        "max_aspect_ratio": _num(r"Max aspect ratio = ([\d.eE+-]+)"),
        "n_cells": _num(r"^\s*cells:\s*(\d+)", int),
        "n_faces": _num(r"^\s*faces:\s*(\d+)", int),
        "n_points": _num(r"^\s*points:\s*(\d+)", int),
    }
    out["severe_warnings"] = [
        line.strip() for line in text.splitlines()
        if line.lstrip().startswith("***")][:8]
    return out


def yplus_command(app: str = "simpleFoam") -> list:
    """The argv that computes y+ on every wall patch from the converged field:
    ``<app> -postProcess -func yPlus -latestTime``. Writes
    ``postProcessing/yPlus/<t>/yPlus.dat``; read it with :func:`parse_yplus`. Works on
    a laminar case too (ν_t is simply zero there), so it needs no turbulence branch."""
    return [app, "-postProcess", "-func", "yPlus", "-latestTime"]


# Wall-function validity band: below ~30 the first cell sits inside the buffer/viscous
# layer the log-law wall function assumes it is above, and above ~300 the log layer has
# been over-shot. A resolved (low-Re) mesh wants y+ ~ 1 instead — a different, equally
# valid target, which is why the verdict needs to know which one was intended.
_YPLUS_WALL_FUNCTION = (30.0, 300.0)
_YPLUS_RESOLVED_MAX = 5.0


def parse_yplus(case_dir: str, *, name: str = "yPlus",
                wall_treatment: str | None = None) -> dict | None:
    """Measured y+ per wall patch, from the ``yPlus`` function object's output.

    This is the MEASURED value — computed from the solved wall shear — as opposed to
    the a-priori ``y_plus_estimate`` the RANS case builders report from a correlation
    before any solving happens. The two disagreeing is itself information.

    ``wall_treatment`` turns the numbers into a verdict: ``'wall_function'`` expects
    30 ≤ y+ ≤ 300, ``'resolved'`` expects y+ ≲ 5, and None (the default) reports the
    values with ``in_band`` left None rather than inventing an intent.

    Returns {time, patches: {name: {min, max, average}}, y_plus_max, y_plus_min,
    wall_treatment, in_band, warnings}, or None when the function object wrote
    nothing."""
    root = os.path.join(case_dir, "postProcessing", name)
    if not os.path.isdir(root):
        return None
    times = []
    for d in os.listdir(root):
        try:
            times.append((float(d), d))
        except ValueError:
            continue
    if not times:
        return None
    tdir = max(times)[1]
    path = os.path.join(root, tdir, "yPlus.dat")
    if not os.path.isfile(path):
        return None
    patches: dict[str, dict] = {}
    stamp = None
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.lstrip().startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            stamp = parts[0]
            patches[parts[1]] = {"min": float(parts[2]), "max": float(parts[3]),
                                 "average": float(parts[4])}
        except ValueError:
            continue
    if not patches:
        return None
    y_max = max(p["max"] for p in patches.values())
    y_min = min(p["min"] for p in patches.values())
    warnings: list[str] = []
    in_band = None
    if wall_treatment == "wall_function":
        lo, hi = _YPLUS_WALL_FUNCTION
        in_band = lo <= y_max <= hi
        if not in_band:
            warnings.append(
                f"measured y+ max {y_max:.3g} is outside the {lo:g}-{hi:g} wall-function "
                "band: the log-law the wall functions assume does not apply at the "
                "first cell, so the wall shear (and any drag built on it) is off by "
                "more than the model's own uncertainty")
    elif wall_treatment == "resolved":
        in_band = y_max <= _YPLUS_RESOLVED_MAX
        if not in_band:
            warnings.append(
                f"measured y+ max {y_max:.3g} exceeds {_YPLUS_RESOLVED_MAX:g}: the "
                "boundary layer is NOT resolved to the wall, so a low-Re model is "
                "being applied on a mesh that cannot support it")
    return {
        "time": stamp,
        "patches": patches,
        "y_plus_max": y_max,
        "y_plus_min": y_min,
        "wall_treatment": wall_treatment,
        "in_band": in_band,
        "warnings": warnings,
    }


def solve_converged(log_tail: str) -> bool | None:
    """Did SIMPLE reach its ``residualControl`` targets, or did it just run out of
    iterations? True when the run's tail carries "SIMPLE solution converged", False when
    it carries the end-of-run marker without it, None when neither is present (the tail
    is too short to tell). A steady case that stops at ``endTime`` unconverged returns
    numbers that look exactly like converged ones — this is the cheap witness that says
    which happened. (The full trust layer — checkMesh, residual histories, mesh
    independence — is issue #225.)"""
    if not log_tail:
        return None
    if "SIMPLE solution converged" in log_tail:
        return True
    if "\nEnd\n" in log_tail or log_tail.rstrip().endswith("End"):
        return False
    return None


# --- external flow: laminar flat plate (Blasius) — P3 M3 ----------------------
# The external counterpart of the internal pipe: a 2-D laminar flat plate whose
# friction drag has the exact Blasius average skin friction Cf = 1.328/√Re_L
# (ankusdrive.analysis.cfd.flat_plate_drag is that oracle). Three blockMesh blocks make
# the bottom slip / no-slip-plate / slip, giving a CLEAN leading edge (the plate sees
# uniform flow, not the inlet corner); the top is a far-field symmetryPlane and the
# ±z faces are empty (2-D). simpleFoam (steady, laminar) solves it.
#
# Drag is read STRAIGHT FROM THE CONVERGED U FIELD, not from a function object. That
# started as a workaround (forces/wallShearStress aborted with a "sha1" IOstream error
# in the build this was written against — the same reason parse_pressure_drop reads p
# directly) and stays as the primary number now that the FO works on v2512, because it
# is the gated, byte-stable one; forces_function_object above is the cross-check and is
# what the arbitrary-body tunnel uses. The friction drag is the wall-shear
# integral over the plate, τ_w ≈ μ·u₁/y₁ from the first off-wall cell (μ·u₁/y₁·face
# area, summed); the trailing-edge momentum thickness θ gives an independent
# cross-check (D = ρ·U²·θ·b). Validated vs Blasius to ~10 % (wall shear; it converges
# toward 1.0 as Re_L rises and the boundary layer is better resolved).
#
# blockMesh cell ordering is x-fastest then y, blocks in declaration order, so the
# plate block's wall row and trailing-edge column are addressable by index.


def _flat_plate_vid(ix: int, top: int, zt: int) -> int:
    """Vertex id for x-station ix (0..3), top edge flag, z-layer flag — the 16-vertex
    numbering shared by the blockMeshDict and (implicitly) the cell layout."""
    return ix * 2 + (1 if top else 0) + (8 if zt else 0)


def flat_plate_blockmeshdict(
    *, plate_length_m: float, upstream_m: float, wake_m: float, height_m: float,
    thickness_m: float, nx_plate: int, nx_upstream: int, nx_wake: int,
    n_y: int, grading_y: float,
) -> str:
    """blockMeshDict for the 3-block flat-plate domain (slip / plate / slip bottom).

    x-stations 0, upstream, upstream+plate, upstream+plate+wake; height ``height_m``
    (far-field), depth ``thickness_m`` (2-D). Patches inlet (x=0), outlet (x=end),
    plate (no-slip, the middle block's bottom), slip (the up/downstream bottoms),
    top (far-field symmetryPlane), frontAndBack (empty). y graded by ``grading_y``
    (last/first cell) toward the wall."""
    if min(plate_length_m, upstream_m, wake_m, height_m, thickness_m) <= 0:
        raise ValueError("all flat-plate dimensions must be > 0")
    if min(nx_plate, nx_upstream, nx_wake, n_y) < 1:
        raise ValueError("all cell counts must be >= 1")
    X = [0.0, upstream_m, upstream_m + plate_length_m,
         upstream_m + plate_length_m + wake_m]
    vid = _flat_plate_vid
    vlist = [None] * 16
    for zt in (0, 1):
        zc = thickness_m if zt else 0.0
        for ix in range(4):
            vlist[vid(ix, 0, zt)] = f"({X[ix]:.8g} 0 {zc:.8g})"
            vlist[vid(ix, 1, zt)] = f"({X[ix]:.8g} {height_m:.8g} {zc:.8g})"
    vtxt = "\n".join(f"    {v}" for v in vlist)

    def block(ix, ncx):
        a, b = ix, ix + 1
        return (f"    hex ({vid(a,0,0)} {vid(b,0,0)} {vid(b,1,0)} {vid(a,1,0)} "
                f"{vid(a,0,1)} {vid(b,0,1)} {vid(b,1,1)} {vid(a,1,1)}) "
                f"({int(ncx)} {int(n_y)} 1) simpleGrading (1 {grading_y:.10g} 1)")
    blocks = "\n".join([block(0, nx_upstream), block(1, nx_plate), block(2, nx_wake)])

    def xface(ix):
        return f"({vid(ix,0,0)} {vid(ix,1,0)} {vid(ix,1,1)} {vid(ix,0,1)})"

    def botface(ix):
        a, b = ix, ix + 1
        return f"({vid(a,0,0)} {vid(b,0,0)} {vid(b,0,1)} {vid(a,0,1)})"

    def topface(ix):
        a, b = ix, ix + 1
        return f"({vid(a,1,0)} {vid(b,1,0)} {vid(b,1,1)} {vid(a,1,1)})"

    def zface(ix, zt):
        a, b = ix, ix + 1
        return f"({vid(a,0,zt)} {vid(b,0,zt)} {vid(b,1,zt)} {vid(a,1,zt)})"

    return _header("dictionary", "blockMeshDict", "system") + f"""
scale 1;
vertices
(
{vtxt}
);
blocks
(
{blocks}
);
edges ();
boundary
(
    inlet  {{ type patch; faces ({xface(0)}); }}
    outlet {{ type patch; faces ({xface(3)}); }}
    plate  {{ type wall;  faces ({botface(1)}); }}
    slip   {{ type symmetryPlane; faces ({botface(0)} {botface(2)}); }}
    top    {{ type symmetryPlane; faces ({topface(0)} {topface(1)} {topface(2)}); }}
    frontAndBack {{ type empty; faces ({zface(0,0)} {zface(1,0)} {zface(2,0)} {zface(0,1)} {zface(1,1)} {zface(2,1)}); }}
);
mergePatchPairs ();
"""


def flat_plate_case_files(
    *, velocity_m_s: float, nu_m2_s: float, plate_length_m: float = 0.1,
    upstream_m: float = 0.03, wake_m: float = 0.06, height_m: float = 1.5,
    thickness_m: float = 0.002, nx_plate: int = 160, nx_upstream: int = 40,
    nx_wake: int = 60, n_y: int = 140, grading_y: float = 3000.0,
    end_time: int = 3000,
) -> dict:
    """Every text file of the steady laminar flat-plate case as ``{relpath: contents}``.

    Uniform inlet ``velocity_m_s`` in +x, slip up/downstream bottom + no-slip plate,
    far-field symmetryPlane top, zero-pressure outlet; ``simpleFoam`` (SIMPLEC,
    bounded linearUpwind) with kinematic viscosity ``nu_m2_s`` on the
    ``flat_plate_blockmeshdict`` mesh."""
    U = velocity_m_s
    files = {}
    files["system/blockMeshDict"] = flat_plate_blockmeshdict(
        plate_length_m=plate_length_m, upstream_m=upstream_m, wake_m=wake_m,
        height_m=height_m, thickness_m=thickness_m, nx_plate=nx_plate,
        nx_upstream=nx_upstream, nx_wake=nx_wake, n_y=n_y, grading_y=grading_y)
    files["constant/transportProperties"] = (
        _header("dictionary", "transportProperties", "constant")
        + f"\ntransportModel  Newtonian;\nnu              {nu_m2_s:.10g};\n")
    files["constant/turbulenceProperties"] = (
        _header("dictionary", "turbulenceProperties", "constant")
        + "\nsimulationType  laminar;\n")
    files["0/U"] = (
        _header("volVectorField", "U", "0")
        + f"\ndimensions      [0 1 -1 0 0 0 0];\ninternalField   uniform ({U:.10g} 0 0);\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type fixedValue; value uniform ({U:.10g} 0 0); }}\n"
        "    outlet { type zeroGradient; }\n"
        "    plate  { type noSlip; }\n"
        "    slip   { type symmetryPlane; }\n"
        "    top    { type symmetryPlane; }\n"
        "    frontAndBack { type empty; }\n}\n")
    files["0/p"] = (
        _header("volScalarField", "p", "0")
        + "\ndimensions      [0 2 -2 0 0 0 0];\ninternalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type zeroGradient; }\n"
        "    outlet { type fixedValue; value uniform 0; }\n"
        "    plate  { type zeroGradient; }\n"
        "    slip   { type symmetryPlane; }\n"
        "    top    { type symmetryPlane; }\n"
        "    frontAndBack { type empty; }\n}\n")
    files["system/controlDict"] = (
        _header("dictionary", "controlDict", "system")
        + "\napplication     simpleFoam;\nstartFrom       startTime;\nstartTime       0;\n"
        f"stopAt          endTime;\nendTime         {int(end_time)};\ndeltaT          1;\n"
        f"writeControl    timeStep;\nwriteInterval   {int(end_time)};\npurgeWrite      1;\n"
        "writeFormat     ascii;\nwritePrecision  8;\nwriteCompression off;\n"
        "timeFormat      general;\nrunTimeModifiable false;\n")
    files["system/fvSchemes"] = (
        _header("dictionary", "fvSchemes", "system")
        + "\nddtSchemes { default steadyState; }\ngradSchemes { default Gauss linear; }\n"
        "divSchemes\n{\n    default none;\n    div(phi,U) bounded Gauss linearUpwind grad(U);\n"
        "    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n}\n"
        "laplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n")
    files["system/fvSolution"] = (
        _header("dictionary", "fvSolution", "system")
        + "\nsolvers\n{\n    p { solver GAMG; smoother GaussSeidel; tolerance 1e-9; relTol 0.01; }\n"
        "    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }\n}\n"
        "SIMPLE\n{\n    nNonOrthogonalCorrectors 1;\n    consistent yes;\n"
        "    residualControl { p 1e-6; U 1e-6; }\n}\n"
        "relaxationFactors { equations { U 0.9; } fields { p 0.9; } }\n")
    return files


def write_flat_plate_case(
    case_dir: str, *, velocity_m_s: float, nu_m2_s: float, plate_length_m: float = 0.1,
    upstream_m: float = 0.03, wake_m: float = 0.06, height_m: float = 1.5,
    thickness_m: float = 0.002, nx_plate: int = 160, nx_upstream: int = 40,
    nx_wake: int = 60, n_y: int = 140, grading_y: float = 3000.0,
    end_time: int = 3000,
) -> dict:
    """Write a complete, runnable OpenFOAM flat-plate case under ``case_dir``.

    Lays down ``0/``, ``constant/``, ``system/`` from ``flat_plate_case_files``.
    Returns metadata (echoed geometry/mesh layout so ``parse_flat_plate_drag`` can
    index the plate cells): ``{case_dir, velocity_m_s, nu_m2_s, plate_length_m,
    thickness_m, reynolds_l, nx_plate, nx_upstream, n_y, grading_y, height_m,
    end_time}`` (Re_L = U·L/ν)."""
    files = flat_plate_case_files(
        velocity_m_s=velocity_m_s, nu_m2_s=nu_m2_s, plate_length_m=plate_length_m,
        upstream_m=upstream_m, wake_m=wake_m, height_m=height_m, thickness_m=thickness_m,
        nx_plate=nx_plate, nx_upstream=nx_upstream, nx_wake=nx_wake, n_y=n_y,
        grading_y=grading_y, end_time=end_time)
    for rel, text in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return {
        "case_dir": case_dir,
        "velocity_m_s": velocity_m_s,
        "nu_m2_s": nu_m2_s,
        "plate_length_m": plate_length_m,
        "thickness_m": thickness_m,
        "reynolds_l": velocity_m_s * plate_length_m / nu_m2_s if nu_m2_s else float("inf"),
        "nx_plate": nx_plate,
        "nx_upstream": nx_upstream,
        "n_y": n_y,
        "grading_y": grading_y,
        "height_m": height_m,
        "end_time": end_time,
    }


def _read_internal_vector_field(path: str):
    """Parse the internalField of an OpenFOAM vector field into a list of (x,y,z)
    tuples (nonuniform List<vector>), or None on a shape it can't read."""
    txt = open(path, encoding="utf-8").read()
    m = re.search(
        r"internalField\s+nonuniform\s+List<vector>\s*\n?\s*(\d+)\s*\n\(\s*(.*?)\)\s*;",
        txt, re.S)
    if not m:
        return None
    return [tuple(float(x) for x in v.split())
            for v in re.findall(r"\(([^)]*)\)", m.group(2))]


def parse_flat_plate_drag(
    case_dir: str, *, rho_kg_m3: float, nu_m2_s: float, velocity_m_s: float,
    plate_length_m: float, thickness_m: float, nx_plate: int, nx_upstream: int,
    n_y: int, grading_y: float, height_m: float, time_dir: str | None = None,
) -> dict | None:
    """Read the converged ``U`` field and return the flat-plate friction drag.

    Wall-shear integral over the plate (primary): τ_w ≈ μ·u₁/y₁ from the first off-wall
    cell of the plate block, summed as μ·u₁/y₁·(dx·b). Momentum thickness at the
    trailing edge (cross-check): D = ρ·U²·θ·b with θ = ∫(u/U)(1−u/U)dy (freestream
    overspeed clamped out). ``cd`` is the wall-shear drag over ½·ρ·U²·(L·b).

    Returns {drag_force_n (= wall shear), drag_wall_shear_n, drag_momentum_n, cd,
    cf_solved, reynolds_l, n_cells, time}, or None when no converged field is present."""
    time = time_dir or _latest_time_dir(case_dir)
    if time is None:
        return None
    ufile = os.path.join(case_dir, time, "U")
    if not os.path.isfile(ufile):
        return None
    vecs = _read_internal_vector_field(ufile)
    if not vecs:
        return None

    mu = nu_m2_s * rho_kg_m3
    U = velocity_m_s
    b = thickness_m                                   # 2-D span (unit-depth case)
    # y cell heights from simpleGrading (1, grading_y, 1): geometric series.
    k = grading_y ** (1.0 / (n_y - 1)) if n_y > 1 else 1.0
    h1 = height_m * (1 - k) / (1 - k ** n_y) if k != 1 else height_m / n_y
    heights = [h1 * k ** j for j in range(n_y)]
    y1 = heights[0] / 2.0
    dx = plate_length_m / nx_plate

    # block layout: block0 (slip) has nx_upstream*n_y cells, then the plate block.
    n0 = nx_upstream * n_y
    # wall row j=0 of the plate block: indices n0 + i
    drag_wall = sum(mu * abs(vecs[n0 + i][0]) / y1 for i in range(nx_plate)) * dx * b
    # trailing-edge column: plate block last x-col, all j
    u_te = [vecs[n0 + (nx_plate - 1) + j * nx_plate][0] for j in range(n_y)]
    theta = sum(max(0.0, (u / U) * (1.0 - u / U)) * h for u, h in zip(u_te, heights))
    drag_mom = rho_kg_m3 * U * U * theta * b

    q = 0.5 * rho_kg_m3 * U * U
    cd = drag_wall / (q * plate_length_m * b) if q > 0 else float("inf")
    return {
        "drag_force_n": drag_wall,
        "drag_wall_shear_n": drag_wall,
        "drag_momentum_n": drag_mom,
        "cd": cd,
        "cf_solved": cd,                              # for a flat plate Cd ≡ avg Cf
        "reynolds_l": U * plate_length_m / nu_m2_s if nu_m2_s else float("inf"),
        "n_cells": len(vecs),
        "time": time,
    }


# --- Tier B3: kOmegaSST RANS variants (SIMULATION_NEXT) -------------------------
#
# Turbulent upgrades of the two validation cases above, extending the CFD
# validity envelope past Re~2300. Wall-function discipline: meshes target a
# first-cell y+ of ~30-100 (reported as y_plus_estimate), k/omega/nut carry the
# standard wall functions, and the GATES ARE BANDED, never exact — the
# references themselves (Colebrook, the 1/7-power Cf family) are +-10-15 %
# correlations. Inlet turbulence: k = 1.5*(I*U)^2, omega = k^0.5/(Cmu^0.25*l).

_CMU = 0.09


def _rans_scalar_field(obj: str, dims: str, internal: float, bcs: dict) -> str:
    s = (f"FoamFile {{ version 2.0; format ascii; class volScalarField; "
         f"object {obj}; }}\n"
         f"dimensions {dims};\ninternalField uniform {internal:.10g};\n"
         "boundaryField\n{\n")
    for patch, body in bcs.items():
        s += f"    {patch} {{ {body} }}\n"
    return s + "}\n"


def _rans_turbulence_properties() -> str:
    return ("FoamFile { version 2.0; format ascii; class dictionary; "
            "location \"constant\"; object turbulenceProperties; }\n"
            "simulationType  RAS;\n"
            "RAS\n{\n    RASModel        kOmegaSST;\n"
            "    turbulence      on;\n    printCoeffs     off;\n}\n")


def _rans_inlet_k_omega(velocity_m_s: float, length_scale_m: float,
                        intensity: float) -> tuple:
    k = 1.5 * (intensity * velocity_m_s) ** 2
    omega = math.sqrt(k) / (_CMU ** 0.25 * length_scale_m)
    return k, omega


_RESIDUAL_CONTROL_RANS = {"p": 1e-6, "U": 1e-6, "k": 1e-6, "omega": 1e-6}


def _rans_overlay(files: dict, *, k_in: float, omega_in: float,
                  k_bcs: dict, omega_bcs: dict, nut_bcs: dict,
                  residual_control: dict | None = None) -> dict:
    """Turn a laminar simpleFoam case-file dict into the kOmegaSST one: RAS
    turbulence properties, 0/k + 0/omega + 0/nut, upwind k/omega divergence +
    wallDist in fvSchemes, k/omega solvers + 0.7 relaxation in fvSolution."""
    files = dict(files)
    files["constant/turbulenceProperties"] = _rans_turbulence_properties()
    files["0/k"] = _rans_scalar_field("k", "[0 2 -2 0 0 0 0]", k_in, k_bcs)
    files["0/omega"] = _rans_scalar_field("omega", "[0 0 -1 0 0 0 0]",
                                          omega_in, omega_bcs)
    files["0/nut"] = _rans_scalar_field("nut", "[0 2 -1 0 0 0 0]", 0.0, nut_bcs)
    s = files["system/fvSchemes"]
    s = s.replace("divSchemes\n{",
                  "divSchemes\n{\n"
                  "    div(phi,k)      bounded Gauss upwind;\n"
                  "    div(phi,omega)  bounded Gauss upwind;")
    files["system/fvSchemes"] = s + "\nwallDist { method meshWave; }\n"
    s = files["system/fvSolution"]
    s = s.replace(
        "U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }",
        "U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }\n"
        "    k { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }\n"
        "    omega { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }")
    s = re.sub(r"residualControl \{[^}]*\}",
               _residual_control_text(residual_control or _RESIDUAL_CONTROL_RANS), s)
    s = re.sub(r"relaxationFactors.*",
               "relaxationFactors { equations { U 0.7; k 0.7; omega 0.7; } "
               "fields { p 0.7; } }", s, flags=re.S)
    files["system/fvSolution"] = s
    return files


def pipe_rans_case_files(
    *,
    diameter_m: float,
    length_m: float,
    velocity_m_s: float,
    nu_m2_s: float,
    intensity: float = 0.05,
    half_angle_deg: float = 2.5,
    n_axial: int = 100,
    n_radial: int = 24,
    end_time: int = 2000,
) -> dict:
    """kOmegaSST axisymmetric pipe (the turbulent upgrade of pipe_case_files):
    same wedge mesh, RAS model + wall-function k/omega/nut, upwind convection.
    Keep the pipe long (>= 40 D) so the developed-gradient fit has room."""
    files = pipe_case_files(
        diameter_m=diameter_m, length_m=length_m, velocity_m_s=velocity_m_s,
        nu_m2_s=nu_m2_s, half_angle_deg=half_angle_deg, n_axial=n_axial,
        n_radial=n_radial, end_time=end_time)
    # sharper convection for the mean flow than the laminar central default
    files["system/fvSchemes"] = files["system/fvSchemes"].replace(
        "div(phi,U) bounded Gauss linear;",
        "div(phi,U) bounded Gauss linearUpwind grad(U);")
    k_in, omega_in = _rans_inlet_k_omega(velocity_m_s, 0.07 * diameter_m, intensity)
    wedges = {"wedge1": "type wedge;", "wedge2": "type wedge;"}
    return _rans_overlay(
        files, k_in=k_in, omega_in=omega_in,
        # same wedge-Uz artifact as the laminar pipe — see pipe_case_files
        residual_control={"p": 1e-6, "k": 1e-6, "omega": 1e-6},
        k_bcs={"inlet": f"type fixedValue; value uniform {k_in:.10g};",
               "outlet": "type zeroGradient;",
               "wall": f"type kqRWallFunction; value uniform {k_in:.10g};",
               **wedges},
        omega_bcs={"inlet": f"type fixedValue; value uniform {omega_in:.10g};",
                   "outlet": "type zeroGradient;",
                   "wall": f"type omegaWallFunction; value uniform {omega_in:.10g};",
                   **wedges},
        nut_bcs={"inlet": "type calculated; value uniform 0;",
                 "outlet": "type calculated; value uniform 0;",
                 "wall": "type nutkWallFunction; value uniform 0;",
                 **wedges})


def write_pipe_rans_case(
    case_dir: str,
    *,
    diameter_m: float,
    length_m: float,
    velocity_m_s: float,
    nu_m2_s: float,
    intensity: float = 0.05,
    n_axial: int = 100,
    n_radial: int = 24,
    end_time: int = 2000,
) -> dict:
    """Write the runnable kOmegaSST pipe case. Returns {case_dir, reynolds,
    n_axial, n_radial, end_time, y_plus_estimate, colebrook_dpdx_pa_m,
    blasius_dpdx_pa_m} — the references use water-like rho via the caller; the
    dp/dx fields here are per unit rho (kinematic·rho applied by the caller)."""
    re_d = velocity_m_s * diameter_m / nu_m2_s
    if re_d < 4000:
        raise ValueError(
            f"Re = {re_d:.0f} is not turbulent — use the laminar pipe case "
            "(or raise the velocity)")
    if length_m < 40 * diameter_m:
        raise ValueError("keep length_m >= 40*diameter_m so the second-half "
                         "gradient fit sits in developed flow")
    files = pipe_rans_case_files(
        diameter_m=diameter_m, length_m=length_m, velocity_m_s=velocity_m_s,
        nu_m2_s=nu_m2_s, intensity=intensity, n_axial=n_axial,
        n_radial=n_radial, end_time=end_time)
    for rel, content in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    # first-cell y+ from the Blasius shear estimate: tau = f/8 * rho U^2
    from . import cfd as _cfd
    f_est = _cfd.colebrook_friction_factor(re_d)
    u_star = velocity_m_s * math.sqrt(f_est / 8.0)
    y1 = (diameter_m / 2.0) / n_radial / 2.0
    return {
        "case_dir": case_dir,
        "reynolds": re_d,
        "n_axial": n_axial,
        "n_radial": n_radial,
        "end_time": end_time,
        "y_plus_estimate": round(y1 * u_star / nu_m2_s, 1),
        "friction_factor_colebrook": f_est,
    }


def parse_pipe_rans_dpdx(
    case_dir: str,
    *,
    n_axial: int,
    n_radial: int,
    length_m: float,
    rho_kg_m3: float,
    time_dir: str | None = None,
) -> dict | None:
    """Developed pressure gradient from the converged p field: column-mean p per
    axial station (cells are x-fastest in the single-block wedge), linear fit
    over the SECOND HALF of the pipe (past the turbulent entrance length).
    Returns {dpdx_pa_m, n_fit_points, n_cells, time} or None."""
    td = time_dir or _latest_time_dir(case_dir)
    if td is None or td == "0":
        return None
    vals = _read_internal_scalar_field(os.path.join(case_dir, td, "p"))
    if not vals or len(vals) != n_axial * n_radial:
        return None
    col = [sum(vals[i + j * n_axial] for j in range(n_radial)) / n_radial
           for i in range(n_axial)]
    dx = length_m / n_axial
    pts = [((i + 0.5) * dx, p) for i, p in enumerate(col)
           if (i + 0.5) * dx >= length_m / 2.0]
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(p for _, p in pts)
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * p for x, p in pts)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return {
        "dpdx_pa_m": -slope * rho_kg_m3,   # OpenFOAM p is kinematic (m^2/s^2)
        "n_fit_points": n,
        "n_cells": len(vals),
        "time": td,
    }


def flat_plate_rans_case_files(
    *,
    velocity_m_s: float,
    nu_m2_s: float,
    plate_length_m: float = 1.0,
    upstream_m: float = 0.15,
    wake_m: float = 0.3,
    height_m: float = 0.5,
    thickness_m: float = 0.002,
    nx_plate: int = 120,
    nx_upstream: int = 20,
    nx_wake: int = 30,
    n_y: int = 50,
    grading_y: float = 25.0,
    end_time: int = 2000,
    intensity: float = 0.05,
) -> dict:
    """kOmegaSST flat plate (the turbulent upgrade of flat_plate_case_files):
    same 3-block mesh with a mild wall grading sized for wall-function y+."""
    files = flat_plate_case_files(
        velocity_m_s=velocity_m_s, nu_m2_s=nu_m2_s,
        plate_length_m=plate_length_m, upstream_m=upstream_m, wake_m=wake_m,
        height_m=height_m, thickness_m=thickness_m, nx_plate=nx_plate,
        nx_upstream=nx_upstream, nx_wake=nx_wake, n_y=n_y,
        grading_y=grading_y, end_time=end_time)
    k_in, omega_in = _rans_inlet_k_omega(velocity_m_s, 0.01, intensity)
    sym = "type symmetryPlane;"
    empty = "type empty;"
    return _rans_overlay(
        files, k_in=k_in, omega_in=omega_in,
        k_bcs={"inlet": f"type fixedValue; value uniform {k_in:.10g};",
               "outlet": "type zeroGradient;",
               "plate": f"type kqRWallFunction; value uniform {k_in:.10g};",
               "slip": sym, "top": sym, "frontAndBack": empty},
        omega_bcs={"inlet": f"type fixedValue; value uniform {omega_in:.10g};",
                   "outlet": "type zeroGradient;",
                   "plate": f"type omegaWallFunction; value uniform {omega_in:.10g};",
                   "slip": sym, "top": sym, "frontAndBack": empty},
        nut_bcs={"inlet": "type calculated; value uniform 0;",
                 "outlet": "type calculated; value uniform 0;",
                 "plate": "type nutkWallFunction; value uniform 0;",
                 "slip": sym, "top": sym, "frontAndBack": empty})


def write_flat_plate_rans_case(case_dir: str, **kwargs) -> dict:
    """Write the runnable kOmegaSST flat-plate case. Same knobs as
    flat_plate_rans_case_files. Returns {case_dir, reynolds_l, ...mesh knobs...,
    y_plus_estimate}."""
    velocity_m_s = kwargs["velocity_m_s"]
    nu_m2_s = kwargs["nu_m2_s"]
    plate_length_m = kwargs.get("plate_length_m", 1.0)
    n_y = kwargs.get("n_y", 50)
    grading_y = kwargs.get("grading_y", 25.0)
    height_m = kwargs.get("height_m", 0.5)
    re_l = velocity_m_s * plate_length_m / nu_m2_s
    if re_l < 5e5:
        raise ValueError(
            f"Re_L = {re_l:.3g} is below transition — use the laminar "
            "flat-plate case")
    files = flat_plate_rans_case_files(**kwargs)
    for rel, content in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    # first cell height from the geometric grading; y+ from mid-plate local Cf
    k = grading_y ** (1.0 / (n_y - 1))
    h1 = height_m * (k - 1.0) / (k ** n_y - 1.0)
    cf_mid = 0.0592 * (re_l / 2.0) ** -0.2
    u_star = velocity_m_s * math.sqrt(cf_mid / 2.0)
    return {
        "case_dir": case_dir,
        "reynolds_l": re_l,
        "velocity_m_s": velocity_m_s,
        "nu_m2_s": nu_m2_s,
        "plate_length_m": plate_length_m,
        "thickness_m": kwargs.get("thickness_m", 0.002),
        "nx_plate": kwargs.get("nx_plate", 120),
        "nx_upstream": kwargs.get("nx_upstream", 20),
        "n_y": n_y,
        "grading_y": grading_y,
        "height_m": height_m,
        "end_time": kwargs.get("end_time", 2000),
        "y_plus_estimate": round((h1 / 2.0) * u_star / nu_m2_s, 1),
    }


def parse_flat_plate_rans_drag(
    case_dir: str,
    *,
    rho_kg_m3: float,
    nu_m2_s: float,
    velocity_m_s: float,
    plate_length_m: float,
    thickness_m: float,
    nx_plate: int,
    nx_upstream: int,
    n_y: int,
    grading_y: float,
    height_m: float,
    time_dir: str | None = None,
) -> dict | None:
    """Turbulent plate drag. The HEADLINE number is the trailing-edge
    momentum-thickness drag (pure momentum conservation — valid for any
    turbulence treatment); the laminar-style mu*u1/y1 wall sum is corrected with
    the wall-function eddy viscosity, tau_w = rho*(nu + nut_wall)*u1/y1, and
    reported as a cross-check. Returns {drag_momentum_n, cf_momentum,
    drag_wall_corrected_n, cf_wall_corrected, reynolds_l, n_cells, time} or
    None."""
    base = parse_flat_plate_drag(
        case_dir, rho_kg_m3=rho_kg_m3, nu_m2_s=nu_m2_s,
        velocity_m_s=velocity_m_s, plate_length_m=plate_length_m,
        thickness_m=thickness_m, nx_plate=nx_plate, nx_upstream=nx_upstream,
        n_y=n_y, grading_y=grading_y, height_m=height_m, time_dir=time_dir)
    if base is None:
        return None
    td = time_dir or _latest_time_dir(case_dir)
    q = 0.5 * rho_kg_m3 * velocity_m_s ** 2
    area = plate_length_m * thickness_m
    out = {
        "drag_momentum_n": base["drag_momentum_n"],
        "cf_momentum": base["drag_momentum_n"] / (q * area),
        "reynolds_l": base["reynolds_l"],
        "n_cells": base["n_cells"],
        "time": base["time"],
        "drag_wall_corrected_n": None,
        "cf_wall_corrected": None,
    }
    # wall-function correction: nut on the plate patch scales each station's
    # mu*u1/y1 contribution by (nu + nut_i)/nu
    try:
        txt = open(os.path.join(case_dir, td, "nut"), encoding="utf-8").read()
        m = re.search(r"plate\s*\{(.*?)\n\s*\}", txt, re.S)
        lst = re.search(r"List<scalar>\s*\n?\s*\d+\s*\(([^)]*)\)", m.group(1), re.S)
        nut_wall = [float(v) for v in lst.group(1).split()]
    except (OSError, AttributeError, ValueError):
        return out
    if len(nut_wall) != nx_plate:
        return out
    uvals = _read_internal_vector_field(os.path.join(case_dir, td, "U"))
    if not uvals:
        return out
    k = grading_y ** (1.0 / (n_y - 1))
    h1 = height_m * (k - 1.0) / (k ** n_y - 1.0)
    y1 = h1 / 2.0
    dx = plate_length_m / nx_plate
    n0 = nx_upstream * n_y
    drag = 0.0
    for i in range(nx_plate):
        u1 = uvals[n0 + i][0]
        drag += rho_kg_m3 * (nu_m2_s + nut_wall[i]) * (u1 / y1) * dx * thickness_m
    out["drag_wall_corrected_n"] = drag
    out["cf_wall_corrected"] = drag / (q * area)
    return out

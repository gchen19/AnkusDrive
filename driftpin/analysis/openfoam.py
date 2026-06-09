"""OpenFOAM case generation — the heavy-solver case-from-geometry half of P2 family 6.

Pure-Python, FreeCAD-free (the case text is testable on the no-FreeCAD lane; the
*solve* needs the OpenFOAM binaries). The CFD family (``docs/SIMULATION_P2_KICKOFF.md``
M5) rides on OpenFOAM; the kickoff's "unambiguous gate" is the **straight circular
pipe**, whose laminar pressure drop is the exact Hagen–Poiseuille law

    Δp = 128·μ·L·Q / (π·D⁴)   (= 32·μ·U·L / D²),

with its sharp D⁴ scaling. ``driftpin.analysis.cfd.pipe_pressure_drop`` is that
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
    files = {}
    files["system/blockMeshDict"] = pipe_blockmeshdict(
        diameter_m=diameter_m, length_m=length_m, half_angle_deg=half_angle_deg,
        n_axial=n_axial, n_radial=n_radial)
    files["constant/transportProperties"] = (
        _header("dictionary", "transportProperties", "constant")
        + f"\ntransportModel  Newtonian;\nnu              {nu_m2_s:.10g};\n")
    files["constant/turbulenceProperties"] = (
        _header("dictionary", "turbulenceProperties", "constant")
        + "\nsimulationType  laminar;\n")
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
        "    residualControl { p 1e-7; U 1e-7; }\n}\n"
        "relaxationFactors { equations { U 0.9; } fields { p 0.9; } }\n")
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
        with open(path, "w") as f:
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
    txt = open(path).read()
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

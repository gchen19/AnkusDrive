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


# --- external flow: laminar flat plate (Blasius) — P3 M3 ----------------------
# The external counterpart of the internal pipe: a 2-D laminar flat plate whose
# friction drag has the exact Blasius average skin friction Cf = 1.328/√Re_L
# (driftpin.analysis.cfd.flat_plate_drag is that oracle). Three blockMesh blocks make
# the bottom slip / no-slip-plate / slip, giving a CLEAN leading edge (the plate sees
# uniform flow, not the inlet corner); the top is a far-field symmetryPlane and the
# ±z faces are empty (2-D). simpleFoam (steady, laminar) solves it.
#
# Drag is read STRAIGHT FROM THE CONVERGED U FIELD — OpenFOAM's forces/wallShearStress
# function objects abort with a "sha1" IOstream error in this OpenFOAM build (the same
# reason parse_pressure_drop reads p directly). The friction drag is the wall-shear
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
        with open(path, "w") as f:
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
    txt = open(path).read()
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

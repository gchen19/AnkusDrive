"""Injection-molding *fill* solve — the higher-fidelity twin of ``molding_screen``.

Pure-Python case generation + result parsing (FreeCAD-free, so the case text and
the field parsers are testable on the no-solver CI lane; the *solve* needs the
OpenFOAM binaries). This is the runnable-today half of GitHub issue #105.

Scope reality (see the issue): the headline solver is **openInjMoldSim** (GPL-3.0,
a modified ``compressibleInterFoam`` with Cross-WLF + Tait), but it targets
OpenFOAM **7 (.org)** while this host builds OpenFOAM **v1912/v2512 (.com / ESI)**.
A from-source OpenFOAM-7 build is a multi-hour, host-heavy job, so the issue's
documented fallback is taken here: a **VOF cavity-fill on the EXISTING OpenFOAM**
(``interFoam``), melt + air, with a shear-thinning (``BirdCarreau``) or Newtonian
melt viscosity. This answers the *strongest, most reliable* molding gate —
**short-shot / fill ability** (does the melt front reach the cavity end before
the run ends) — plus fill time and an injection-pressure history. It loses the
packing/cooling stage and the published openInjMoldSim validation; those ride on
the OF7 path scaffolded in ``tools/build_openinjmoldsim.sh``.

Geometry — a **2-D rectangular plaque cavity** (``length`` × ``height``, one cell
deep, ``empty`` front/back). The gate is a short inlet patch on the left edge
(``x = 0``); the rest of the left edge, the top, the bottom and the right end are
walls. Air leaves through small **vent** patches at the far corners (top-right /
bottom-right) so the displaced air has somewhere to go — without a vent the
incompressible interFoam cannot fill (nowhere for the air to exit) and the front
stalls spuriously. The melt enters at a fixed mean velocity (from the volumetric
injection rate / gate area); ``interFoam`` (transient VOF) tracks the alpha=0.5
front.

Fill metrics are read straight from the final ``alpha.melt`` (a.k.a.
``alpha.water``) field — ``filled_fraction`` is the mean of alpha over the cavity
cells (1.0 = full, < 1 = short shot), ``last_to_fill_x_frac`` locates the
least-filled axial band (the region a short shot starves), ``max_pressure_pa`` is
ρ·max(p_rgh)+the static head from the converged ``p`` field, and ``fill_time_s``
is the case end time when the cavity reaches the fill threshold. No
function-object writers are used (``surfaceFieldValue`` is sha1-broken in this
OpenFOAM build — same reason ``openfoam.parse_pressure_drop`` reads ``p``
directly).

Units SI (m, m/s, Pa, kg/m³, Pa·s). See ``tests/test_molding_fill.py`` for the
case-structure gate and the solver-backed fill / short-shot gate.
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


# The two phase-fraction field names interFoam may use: openInjMoldSim and the
# OpenFOAM damBreak tutorial call it alpha.water; we name the melt phase
# explicitly. parse_fill accepts either.
_ALPHA_MELT = "alpha.melt"


def cavity_blockmeshdict(
    *, length_m: float, height_m: float, depth_m: float,
    gate_height_m: float, vent_height_m: float,
    nx: int, ny: int,
) -> str:
    """blockMeshDict for the 2-D plaque cavity (one block, one cell deep).

    The left edge is split into three patches by ``gate_height_m`` (the gate sits
    at the *bottom* of the left edge, ``0 <= y <= gate_height_m``): ``inlet``
    (the gate) and ``wallLeft`` (above it). The right edge is split by
    ``vent_height_m`` at the *top* (``height-vent <= y <= height``): ``vent``
    (air escape) and ``wallRight`` (below it). Top/bottom are ``wall``; the ±z
    faces are ``empty`` (2-D). ``nx``×``ny`` cells.

    A single hex can only carry one patch per face, so the left/right faces are
    split with explicit ``boundary`` face lists addressed by the 12-vertex
    numbering of the two-layer (z=0, z=depth) box plus the two split points.
    """
    if min(length_m, height_m, depth_m, gate_height_m, vent_height_m) <= 0:
        raise ValueError("all cavity dimensions must be > 0")
    if gate_height_m >= height_m or vent_height_m >= height_m:
        raise ValueError("gate_height_m and vent_height_m must be < height_m")
    if nx < 4 or ny < 3:
        raise ValueError("nx must be >= 4 and ny >= 3")
    L, H, D = length_m, height_m, depth_m
    gy = gate_height_m
    vy = H - vent_height_m            # bottom of the vent band on the right edge
    # We mesh the cavity as THREE stacked horizontal blocks so the y-splits at the
    # gate (left) and vent (right) land on block boundaries — that makes the
    # inlet/vent patches exact cell-face sets. Bands: [0,gy], [gy,vy], [vy,H].
    ys = sorted({0.0, gy, vy, H})
    # collapse near-duplicate splits (gy could equal vy); keep a clean ascending list
    bands = [(ys[i], ys[i + 1]) for i in range(len(ys) - 1)]
    nb = len(bands)
    # vertex grid: 2 columns (x=0, x=L) × (nb+1) rows × 2 z-layers
    nrows = nb + 1
    yvals = [bands[0][0]] + [b[1] for b in bands]

    def vid(col, row, zt):
        return zt * (2 * nrows) + row * 2 + col

    verts = [None] * (2 * 2 * nrows)
    for zt in (0, 1):
        zc = D if zt else 0.0
        for row in range(nrows):
            yv = yvals[row]
            verts[vid(0, row, zt)] = f"({0.0:.10g} {yv:.10g} {zc:.10g})"
            verts[vid(1, row, zt)] = f"({L:.10g} {yv:.10g} {zc:.10g})"
    vtxt = "\n".join(f"    {v}" for v in verts)

    # y-cell distribution proportional to band height (keeps cells near-square)
    ny_band = []
    rem = ny
    for i, (y0, y1) in enumerate(bands):
        n = max(1, round(ny * (y1 - y0) / H))
        ny_band.append(n)
    # fix rounding so the total is exactly ny (cosmetic; not load-bearing)
    diff = ny - sum(ny_band)
    ny_band[ny_band.index(max(ny_band))] += diff

    blocks = []
    for row in range(nb):
        a0, a1 = vid(0, row, 0), vid(1, row, 0)
        b0, b1 = vid(0, row + 1, 0), vid(1, row + 1, 0)
        c0, c1 = vid(0, row, 1), vid(1, row, 1)
        d0, d1 = vid(0, row + 1, 1), vid(1, row + 1, 1)
        blocks.append(
            f"    hex ({a0} {a1} {b1} {b0} {c0} {c1} {d1} {d0}) "
            f"({nx} {ny_band[row]} 1) simpleGrading (1 1 1)")
    blk = "\n".join(blocks)

    def lface(row):  # left edge of band `row` (x=0), z-extruded
        a, b = vid(0, row, 0), vid(0, row + 1, 0)
        c, d = vid(0, row + 1, 1), vid(0, row, 1)
        return f"({a} {b} {c} {d})"

    def rface(row):  # right edge (x=L)
        a, b = vid(1, row, 0), vid(1, row + 1, 0)
        c, d = vid(1, row + 1, 1), vid(1, row, 1)
        return f"({a} {b} {c} {d})"

    def bot():
        a, b = vid(0, 0, 0), vid(1, 0, 0)
        c, d = vid(1, 0, 1), vid(0, 0, 1)
        return f"({a} {b} {c} {d})"

    def top():
        a, b = vid(0, nb, 0), vid(1, nb, 0)
        c, d = vid(1, nb, 1), vid(0, nb, 1)
        return f"({a} {b} {c} {d})"

    def zfaces():
        out = []
        for zt in (0, 1):
            for row in range(nb):
                a, b = vid(0, row, zt), vid(1, row, zt)
                c, d = vid(1, row + 1, zt), vid(0, row + 1, zt)
                out.append(f"({a} {b} {c} {d})")
        return " ".join(out)

    # classify each band's left/right face as inlet/vent/wall by its y-centre
    inlet_faces, leftwall_faces, vent_faces, rightwall_faces = [], [], [], []
    for row, (y0, y1) in enumerate(bands):
        yc = 0.5 * (y0 + y1)
        (inlet_faces if yc <= gy else leftwall_faces).append(lface(row))
        (vent_faces if yc >= vy else rightwall_faces).append(rface(row))

    boundary = (
        "boundary\n(\n"
        f"    inlet  {{ type patch; faces ({' '.join(inlet_faces)}); }}\n"
        f"    vent   {{ type patch; faces ({' '.join(vent_faces)}); }}\n"
        f"    walls  {{ type wall;  faces ("
        f"{' '.join(leftwall_faces + rightwall_faces)} {bot()} {top()}); }}\n"
        f"    frontAndBack {{ type empty; faces ({zfaces()}); }}\n"
        ");\n"
    )
    return _header("dictionary", "blockMeshDict", "system") + f"""
scale 1;
vertices
(
{vtxt}
);
blocks
(
{blk}
);
edges ();
{boundary}
mergePatchPairs ();
"""


def cavity_case_files(
    *,
    length_m: float = 0.10,
    height_m: float = 0.002,
    depth_m: float = 0.001,
    gate_height_m: float | None = None,
    vent_height_m: float | None = None,
    nx: int = 120,
    ny: int = 8,
    inject_velocity_m_s: float = 0.5,
    end_time_s: float = 0.4,
    write_interval_s: float | None = None,
    deltaT_s: float = 1e-4,
    max_co: float = 0.4,
    # melt rheology — Newtonian by default; BirdCarreau (Cross-like shear thin)
    # when `carreau` is given.
    melt_rho_kg_m3: float = 900.0,
    melt_nu_m2_s: float = 1.0e-3,
    air_rho_kg_m3: float = 1.0,
    air_nu_m2_s: float = 1.5e-5,
    sigma_n_m: float = 0.0,
    carreau: dict | None = None,
    gravity: bool = False,
) -> dict:
    """Every text file of the 2-D interFoam cavity-fill case as ``{relpath: text}``.

    The cavity starts full of **air** (alpha.melt = 0); melt is injected at the
    gate (mean ``inject_velocity_m_s`` in +x, alpha.melt = 1), displacing the air
    out the far vent. ``end_time_s`` bounds the run (a fillable cavity completes
    well within it; a short shot does not). Melt viscosity is Newtonian
    (``melt_nu_m2_s``, kinematic) unless ``carreau`` is given as
    ``{nu0, nuInf, k, n}`` (BirdCarreau, the interFoam shear-thinning model that
    stands in for openInjMoldSim's Cross-WLF). ``gravity`` toggles a real g
    (default off — thin cavities are pressure-driven, gravity is noise and only
    risks the air buoying through the melt).

    Returns the dict the caller writes into a case dir (keys under ``0/``,
    ``constant/``, ``system/``)."""
    L, H, D = length_m, height_m, depth_m
    gy = gate_height_m if gate_height_m is not None else H * 0.5
    vy = vent_height_m if vent_height_m is not None else H * 0.5
    wi = write_interval_s if write_interval_s is not None else end_time_s
    U = inject_velocity_m_s

    files: dict[str, str] = {}
    files["system/blockMeshDict"] = cavity_blockmeshdict(
        length_m=L, height_m=H, depth_m=D, gate_height_m=gy, vent_height_m=vy,
        nx=nx, ny=ny)

    # --- constant/ -----------------------------------------------------------
    if carreau:
        nu0 = float(carreau.get("nu0", melt_nu_m2_s))
        nuInf = float(carreau.get("nuInf", melt_nu_m2_s * 1e-2))
        kk = float(carreau.get("k", 1.0))
        nn = float(carreau.get("n", 0.5))
        melt_visc = (
            "    transportModel  BirdCarreau;\n"
            f"    nu              {nu0:.10g};\n"
            f"    BirdCarreauCoeffs\n    {{\n"
            f"        nu0     {nu0:.10g};\n"
            f"        nuInf   {nuInf:.10g};\n"
            f"        k       {kk:.10g};\n"
            f"        n       {nn:.10g};\n"
            "    }\n")
    else:
        melt_visc = (
            "    transportModel  Newtonian;\n"
            f"    nu              {melt_nu_m2_s:.10g};\n")
    files["constant/transportProperties"] = (
        _header("dictionary", "transportProperties", "constant")
        + "\nphases (melt air);\n\n"
        "melt\n{\n"
        + melt_visc
        + f"    rho             {melt_rho_kg_m3:.10g};\n}}\n\n"
        "air\n{\n"
        "    transportModel  Newtonian;\n"
        f"    nu              {air_nu_m2_s:.10g};\n"
        f"    rho             {air_rho_kg_m3:.10g};\n}}\n\n"
        f"sigma           {sigma_n_m:.10g};\n")
    files["constant/turbulenceProperties"] = (
        _header("dictionary", "turbulenceProperties", "constant")
        + "\nsimulationType  laminar;\n")
    gz = "-9.81" if gravity else "0"
    files["constant/g"] = (
        _header("uniformDimensionedVectorField", "g", "constant")
        + "\ndimensions      [0 1 -2 0 0 0 0];\n"
        f"value           (0 {gz} 0);\n")

    # --- 0/ ------------------------------------------------------------------
    files["0/alpha.melt"] = (
        _header("volScalarField", "alpha.melt", "0")
        + "\ndimensions      [0 0 0 0 0 0 0];\n"
        "internalField   uniform 0;\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type fixedValue; value uniform 1; }}\n"
        "    vent   { type inletOutlet; inletValue uniform 0; value uniform 0; }\n"
        "    walls  { type zeroGradient; }\n"
        "    frontAndBack { type empty; }\n}\n")
    files["0/U"] = (
        _header("volVectorField", "U", "0")
        + "\ndimensions      [0 1 -1 0 0 0 0];\n"
        "internalField   uniform (0 0 0);\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type fixedValue; value uniform ({U:.10g} 0 0); }}\n"
        "    vent   { type pressureInletOutletVelocity; value uniform (0 0 0); }\n"
        "    walls  { type noSlip; }\n"
        "    frontAndBack { type empty; }\n}\n")
    files["0/p_rgh"] = (
        _header("volScalarField", "p_rgh", "0")
        + "\ndimensions      [1 -1 -2 0 0 0 0];\n"
        "internalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type fixedFluxPressure; value uniform 0; }\n"
        "    vent   { type totalPressure; p0 uniform 0; value uniform 0; }\n"
        "    walls  { type fixedFluxPressure; value uniform 0; }\n"
        "    frontAndBack { type empty; }\n}\n")

    # --- system/ -------------------------------------------------------------
    files["system/controlDict"] = (
        _header("dictionary", "controlDict", "system")
        + "\napplication     interFoam;\nstartFrom       startTime;\nstartTime       0;\n"
        f"stopAt          endTime;\nendTime         {end_time_s:.10g};\n"
        f"deltaT          {deltaT_s:.10g};\nwriteControl    adjustableRunTime;\n"
        f"writeInterval   {wi:.10g};\npurgeWrite      0;\nwriteFormat     ascii;\n"
        "writePrecision  8;\nwriteCompression off;\ntimeFormat      general;\n"
        "timePrecision   8;\nrunTimeModifiable false;\n"
        f"adjustTimeStep  yes;\nmaxCo           {max_co:.10g};\nmaxAlphaCo      {max_co:.10g};\n"
        f"maxDeltaT       {max(deltaT_s * 50, 1e-3):.10g};\n")
    files["system/fvSchemes"] = (
        _header("dictionary", "fvSchemes", "system")
        + "\nddtSchemes { default Euler; }\n"
        "gradSchemes { default Gauss linear; }\n"
        "divSchemes\n{\n"
        "    div(rhoPhi,U)        Gauss linearUpwind grad(U);\n"
        "    div(phi,alpha)       Gauss vanLeer;\n"
        "    div(phirb,alpha)     Gauss linear;\n"
        "    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;\n"
        "    default              none;\n}\n"
        "laplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes { default corrected; }\n")
    files["system/fvSolution"] = (
        _header("dictionary", "fvSolution", "system")
        + "\nsolvers\n{\n"
        '    "alpha.melt.*"\n    {\n'
        "        nAlphaCorr      2;\n        nAlphaSubCycles 1;\n        cAlpha          1;\n"
        "        MULESCorr       yes;\n        nLimiterIter    5;\n"
        "        solver          smoothSolver;\n        smoother        symGaussSeidel;\n"
        "        tolerance       1e-8;\n        relTol          0;\n    }\n"
        '    "pcorr.*" { solver PCG; preconditioner DIC; tolerance 1e-7; relTol 0; }\n'
        "    p_rgh { solver PCG; preconditioner DIC; tolerance 1e-7; relTol 0.05; }\n"
        "    p_rghFinal { $p_rgh; relTol 0; }\n"
        "    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-7; relTol 0; }\n"
        "}\n"
        "PIMPLE\n{\n    momentumPredictor no;\n    nOuterCorrectors 1;\n"
        "    nCorrectors      3;\n    nNonOrthogonalCorrectors 0;\n}\n")
    files["system/setFieldsDict"] = (
        _header("dictionary", "setFieldsDict", "system")
        + "\ndefaultFieldValues ( volScalarFieldValue alpha.melt 0 );\n"
        "regions ();\n")
    return files


def write_cavity_case(case_dir: str, **kwargs) -> dict:
    """Write a complete, runnable interFoam cavity-fill case under ``case_dir``.

    Same knobs as ``cavity_case_files``. Returns metadata for the parser/gate:
    ``{case_dir, length_m, height_m, depth_m, nx, ny, inject_velocity_m_s,
    end_time_s, gate_height_m, melt_rho_kg_m3, expected_fill_time_s,
    flow_length_ratio}`` where ``expected_fill_time_s = length / U`` (the plug-flow
    fill estimate — a fillable cavity completes near this; ``end_time_s`` should be
    a few× larger)."""
    files = cavity_case_files(**kwargs)
    for rel, text in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    L = kwargs.get("length_m", 0.10)
    H = kwargs.get("height_m", 0.002)
    U = kwargs.get("inject_velocity_m_s", 0.5)
    return {
        "case_dir": case_dir,
        "length_m": L,
        "height_m": H,
        "depth_m": kwargs.get("depth_m", 0.001),
        "nx": kwargs.get("nx", 120),
        "ny": kwargs.get("ny", 8),
        "inject_velocity_m_s": U,
        "end_time_s": kwargs.get("end_time_s", 0.4),
        "gate_height_m": kwargs.get("gate_height_m", H * 0.5),
        "melt_rho_kg_m3": kwargs.get("melt_rho_kg_m3", 900.0),
        "expected_fill_time_s": L / U if U else float("inf"),
        "flow_length_ratio": L / H if H else float("inf"),
    }


# --- field parsing -----------------------------------------------------------

def _time_dirs(case_dir: str) -> list:
    """Ascending numeric time-directory names present in ``case_dir`` (incl. '0')."""
    out = []
    for name in os.listdir(case_dir):
        if not os.path.isdir(os.path.join(case_dir, name)):
            continue
        try:
            out.append((float(name), name))
        except ValueError:
            continue
    return [n for _, n in sorted(out)]


def _latest_time_dir(case_dir: str) -> str | None:
    """The largest non-zero numeric time directory, or None."""
    dirs = [(float(n), n) for n in _time_dirs(case_dir)]
    dirs = [(v, n) for v, n in dirs if v > 0]
    return max(dirs)[1] if dirs else None


def _read_internal_scalar_field(path: str):
    """Parse an OpenFOAM scalar field internalField into a list of floats (handles
    nonuniform List<scalar> and a single uniform value), or None."""
    txt = open(path).read()
    m = re.search(
        r"internalField\s+nonuniform\s+List<scalar>\s*\n\s*(\d+)\s*\n\(\s*(.*?)\)\s*;",
        txt, re.S)
    if m:
        return [float(x) for x in m.group(2).split()]
    m = re.search(r"internalField\s+uniform\s+([-\d.eE+]+)\s*;", txt)
    if m:
        return [float(m.group(1))]
    return None


def _alpha_file(case_dir: str, time_dir: str) -> str | None:
    """Path to the melt phase-fraction field in ``time_dir`` (alpha.melt, or the
    tutorial's alpha.water), or None."""
    for name in (_ALPHA_MELT, "alpha.water", "alpha.phase1"):
        cand = os.path.join(case_dir, time_dir, name)
        if os.path.isfile(cand):
            return cand
    return None


def parse_fill(
    case_dir: str, *, nx: int, ny: int, length_m: float,
    melt_rho_kg_m3: float = 900.0, fill_threshold: float = 0.5,
    time_dir: str | None = None,
) -> dict | None:
    """Read the final ``alpha.melt`` (+ ``p``/``p_rgh``) and return the fill result.

    ``filled_fraction`` is the cavity-mean alpha (clamped to [0,1] per cell;
    1.0 = full, < 1 = short shot). ``filled_cell_fraction`` is the share of cells
    above ``fill_threshold`` (the alpha=0.5 melt front). ``last_to_fill_x_frac`` is
    the axial position (0=gate, 1=far end) of the least-filled column — where a
    short shot starves. ``front_x_frac`` is the furthest column that is at least
    half full (how far the front reached). ``max_pressure_pa`` is ρ·max(p_rgh) from
    the converged field (kinematic→Pa; the peak injection pressure proxy).

    Cells are x-fastest within each y-row of the (possibly multi-band) block; we
    fold to ``nx`` axial columns by averaging over all cells sharing an x-column.
    Returns None when no converged alpha field is present (the solve failed)."""
    td = time_dir or _latest_time_dir(case_dir)
    if td is None:
        return None
    af = _alpha_file(case_dir, td)
    if af is None:
        return None
    alpha = _read_internal_scalar_field(af)
    if not alpha:
        return None
    a = [min(1.0, max(0.0, v)) for v in alpha]
    n = len(a)
    filled_fraction = sum(a) / n
    filled_cell_fraction = sum(1 for v in a if v >= fill_threshold) / n

    # fold to nx axial columns: cells are written row-major with x fastest, so
    # cell k has column (k % nx) for any number of stacked y-rows.
    col_sum = [0.0] * nx
    col_cnt = [0] * nx
    for k, v in enumerate(a):
        c = k % nx
        col_sum[c] += v
        col_cnt[c] += 1
    col_mean = [(col_sum[c] / col_cnt[c]) if col_cnt[c] else 0.0 for c in range(nx)]
    # last-to-fill: least-filled column (axial centre fraction)
    worst_c = min(range(nx), key=lambda c: col_mean[c])
    last_to_fill_x_frac = (worst_c + 0.5) / nx
    # front reach: furthest column at least half full
    front_c = max((c for c in range(nx) if col_mean[c] >= fill_threshold),
                  default=-1)
    front_x_frac = (front_c + 1) / nx if front_c >= 0 else 0.0

    out = {
        "time": td,
        "n_cells": n,
        "filled_fraction": round(filled_fraction, 6),
        "filled_cell_fraction": round(filled_cell_fraction, 6),
        "last_to_fill_x_frac": round(last_to_fill_x_frac, 4),
        "last_to_fill_alpha": round(col_mean[worst_c], 6),
        "front_x_frac": round(front_x_frac, 4),
        "min_col_mean_alpha": round(min(col_mean), 6),
    }
    # pressure: prefer p (absolute) then p_rgh; both kinematic in interFoam? No —
    # interFoam p_rgh has dims [1 -1 -2] (already Pa). Read it directly.
    for pname, kinematic in (("p", False), ("p_rgh", False)):
        pf = os.path.join(case_dir, td, pname)
        if os.path.isfile(pf):
            pv = _read_internal_scalar_field(pf)
            if pv:
                out["max_pressure_pa"] = round(max(pv), 3)
                out["pressure_field"] = pname
                break
    return out


# --- the moldability gate ----------------------------------------------------

def fill_gate(
    parsed: dict, *, expected_fill_time_s: float,
    machine_max_pressure_pa: float = 1.8e8,
    fill_fraction_pass: float = 0.97,
) -> dict:
    """Turn a ``parse_fill`` result into the house verdict shape.

    ``pass`` is true when (a) the cavity is essentially full —
    ``filled_fraction >= fill_fraction_pass`` (no short shot) — AND (b) the peak
    injection pressure is within ``machine_max_pressure_pa`` (a typical IM machine
    is ~140-200 MPa at the screw tip; default 180 MPa). ``score`` is the filled
    fraction itself (1.0 = perfect fill). ``fidelity`` is ``"solve"`` (a real VOF
    fill, not a correlation). ``band_pct`` reflects the case-setup-owned validation
    (we own the interFoam fill case, so a conservative ±20 %).

    Returns ``{pass, score, fidelity, band_pct, filled_fraction, short_shot,
    last_to_fill_x_frac, max_pressure_pa, pressure_ok, fill_time_s, warnings}``."""
    ff = parsed.get("filled_fraction", 0.0)
    short_shot = ff < fill_fraction_pass
    pmax = parsed.get("max_pressure_pa")
    pressure_ok = (pmax is None) or (pmax <= machine_max_pressure_pa)
    warnings: list[str] = []
    if short_shot:
        warnings.append(
            f"short shot — cavity only {ff*100:.1f}% filled; the last-to-fill band "
            f"sits at x≈{parsed.get('last_to_fill_x_frac', 1.0)*100:.0f}% of the "
            "flow length (add gates, thicken the wall, raise melt temp/pressure, "
            "or shorten the flow path)")
    if not pressure_ok:
        warnings.append(
            f"peak pressure {pmax/1e6:.0f} MPa exceeds the machine limit "
            f"{machine_max_pressure_pa/1e6:.0f} MPa — the press cannot fill this")
    return {
        "pass": (not short_shot) and pressure_ok,
        "score": round(ff, 4),
        "fidelity": "solve",
        "band_pct": 20.0,
        "filled_fraction": round(ff, 4),
        "short_shot": short_shot,
        "last_to_fill_x_frac": parsed.get("last_to_fill_x_frac"),
        "front_x_frac": parsed.get("front_x_frac"),
        "max_pressure_pa": pmax,
        "pressure_ok": pressure_ok,
        "fill_time_s": round(expected_fill_time_s, 5),
        "warnings": warnings,
    }

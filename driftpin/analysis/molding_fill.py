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
        "    inlet  { type fixedValue; value uniform 1; }\n"
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
        with open(path, "w", encoding="utf-8") as f:
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


# --- openInjMoldSim (OF7-org) case generation --------------------------------
#
# The HEADLINE solver of issue #105: krebeljk/openInjMoldSim (GPL-3.0), a modified
# compressibleInterFoam with **Cross-WLF** shear-thinning viscosity + a **2-domain
# Tait** PVT equation of state and a real cooled-mold-wall heat-flux BC — i.e. the
# packing/cooling physics the interFoam fallback (above) cannot reach. It targets
# OpenFOAM **7 (.org)**, built from source on this host (see
# tools/build_openinjmoldsim.sh; binary auto-resolved by
# solvers.openinjmoldsim_bin()).
#
# Geometry — the same 2-D rectangular plaque as the fallback, but the case is
# **pressure-driven** (an injection-pressure ramp imposed at the gate, the way a
# real press is controlled) rather than fixed-velocity, and **non-isothermal**:
# melt enters hot, the y=0/y=H mold walls draw heat out through an external
# heat-transfer coefficient, and the Cross-WLF viscosity climbs as the melt cools —
# so a thin/long/cold cavity freezes off mid-fill (a real short shot), not just a
# kinematic one. The melt phase is ``alpha.poly`` (poly = the polymer); air is the
# second phase.
#
# Two case-prep gotchas are baked in (the OSHA1stream SHA1 path is broken in this
# toolchain — see docs/MOLDING_FILL_SOLVER.md): every ``#calc``/``#codeStream`` is
# pre-evaluated to a literal, and **no** ``functions{}`` functionObject block is
# emitted. The Cross-WLF + Tait coefficients come from the #106 materials corpus
# (``materials.get(resin)['cross_wlf'|'tait_pvt']``), so this is the path that
# actually consumes that corpus.

# Tutorial-proven PS coefficients (krebeljk demo/fill_pack) — the fallback used
# when the corpus lacks a resin's cross_wlf / tait_pvt cards.
_PS_CROSS_WLF_FALLBACK = {
    "n": 0.252, "Tau": 30800.0, "D1": 4.76e10, "D2": 373.15, "D3": 0.0,
    "A1": 25.7, "A2": 61.06,
}
_PS_TAIT_FALLBACK = {
    "b1m": 9.76e-4, "b2m": 5.8e-7, "b3m": 1.67e8, "b4m": 3.6e-3,
    "b1s": 9.76e-4, "b2s": 2.3e-7, "b3s": 2.6e8, "b4s": 3.0e-3,
    "b5": 373.0, "b6": 5.1e-7, "b7": 0.0, "b8": 0.0, "b9": 0.0,
}
# corpus key -> openInjMoldSim dict key
_CROSS_WLF_MAP = {
    "n": "n", "tau_star_pa": "Tau", "D1_pa_s": "D1", "D2_k": "D2",
    "D3_k_per_pa": "D3", "A1": "A1", "A2_k": "A2",
}
_TAIT_MAP = {
    "b1m_m3_kg": "b1m", "b2m_m3_kg_k": "b2m", "b3m_pa": "b3m", "b4m_per_k": "b4m",
    "b1s_m3_kg": "b1s", "b2s_m3_kg_k": "b2s", "b3s_pa": "b3s", "b4s_per_k": "b4s",
    "b5_k": "b5", "b6_k_per_pa": "b6", "b7_m3_kg": "b7", "b8_per_k": "b8",
    "b9_per_pa": "b9",
}


def _resin_cross_wlf_tait(resin: str | None):
    """Pull Cross-WLF + 2-domain Tait coefficients for ``resin`` from the #106
    corpus, mapped to openInjMoldSim's dict keys; fall back to the tutorial-proven
    PS coefficients when the corpus has no usable cards. Returns ``(cross_wlf,
    tait)`` as float dicts."""
    cw = dict(_PS_CROSS_WLF_FALLBACK)
    tt = dict(_PS_TAIT_FALLBACK)
    if not resin:
        return cw, tt
    try:
        from driftpin.analysis import materials
        card = materials.get(resin) or {}
    except Exception:
        return cw, tt
    raw_cw = card.get("cross_wlf")
    if isinstance(raw_cw, dict):
        try:
            cw = {dst: float(raw_cw[src]) for src, dst in _CROSS_WLF_MAP.items()
                  if src in raw_cw}
            for k, v in _PS_CROSS_WLF_FALLBACK.items():   # backfill any gaps
                cw.setdefault(k, v)
        except (TypeError, ValueError):
            cw = dict(_PS_CROSS_WLF_FALLBACK)
    raw_tt = card.get("tait_pvt")
    if isinstance(raw_tt, dict):
        try:
            tt = {dst: float(raw_tt[src]) for src, dst in _TAIT_MAP.items()
                  if src in raw_tt}
            for k, v in _PS_TAIT_FALLBACK.items():
                tt.setdefault(k, v)
        except (TypeError, ValueError):
            tt = dict(_PS_TAIT_FALLBACK)
    return cw, tt


def _plaque_blockmeshdict(*, length_m, height_m, depth_m, nx, ny, nz=1,
                          split_walls=False) -> str:
    """blockMeshDict for the 2-D plaque: flow along x (``inlet`` at x=0, ``outlet``
    at x=L), the cooled mold ``walls`` at y=0 and y=H, ``frontAndBack`` (the ±z
    faces) ``empty`` (one cell deep). One hex block, ``nx``×``ny``×``nz`` cells.

    ``split_walls`` emits the two thickness faces as **separate** patches —
    ``wallLow`` (y=0) and ``wallHigh`` (y=H) — instead of one fused ``walls`` patch,
    so the pack/cool stage can pull heat out of each face at a different ``h``
    (asymmetric cooling → a frozen-in through-thickness bending differential, #134).
    The default (fused ``walls``) is byte-identical to the pre-#134 case."""
    if min(length_m, height_m, depth_m) <= 0:
        raise ValueError("all plaque dimensions must be > 0")
    if nx < 4 or ny < 2:
        raise ValueError("nx must be >= 4 and ny >= 2")
    L, H, D = length_m, height_m, depth_m
    verts = [
        (0, 0, 0), (L, 0, 0), (L, H, 0), (0, H, 0),
        (0, 0, D), (L, 0, D), (L, H, D), (0, H, D),
    ]
    vtxt = "\n".join(f"    ({x:.10g} {y:.10g} {z:.10g})" for x, y, z in verts)
    # y=0 face is (0 1 5 4); y=H face is (3 2 6 7).
    walls_patches = (
        "    wallLow  { type wall;  faces ((0 1 5 4)); }\n"
        "    wallHigh { type wall;  faces ((3 2 6 7)); }\n"
        if split_walls else
        "    walls  { type wall;  faces ((0 1 5 4) (3 2 6 7)); }\n"
    )
    boundary = (
        "boundary\n(\n"
        "    inlet  { type patch; faces ((0 3 7 4)); }\n"
        "    outlet { type patch; faces ((1 5 6 2)); }\n"
        + walls_patches
        + "    frontAndBack { type empty; faces ((0 1 2 3) (4 5 6 7)); }\n"
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
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);
edges ();
{boundary}
mergePatchPairs ();
"""


def _pressure_ramp_table(peak_pa, ramp_s, end_s, base_pa=1.0e5, n=24) -> str:
    """An OpenFOAM ``table`` of (time, gate pressure) pairs: a linear ramp from
    ``base_pa`` to ``peak_pa`` over ``ramp_s``, held at ``peak_pa`` to ``end_s``.
    Emitted inline (no ``#include`` of an external data file)."""
    pts = []
    for i in range(n + 1):                          # ramp
        t = ramp_s * i / n
        p = base_pa + (peak_pa - base_pa) * i / n
        pts.append((t, p))
    pts.append((end_s, peak_pa))                    # hold to the end
    body = " ".join(f"({t:.8g} {p:.8g})" for t, p in pts)
    return f"table ({body})"


def openinjmoldsim_case_files(
    *,
    length_m: float = 0.02,
    height_m: float = 1.0e-3,
    depth_m: float = 1.0e-3,
    nx: int = 60,
    ny: int = 8,
    nz: int = 1,
    seed_len_m: float | None = None,
    resin: str | None = "PS",
    cross_wlf: dict | None = None,
    tait: dict | None = None,
    melt_temp_k: float = 493.15,
    mold_temp_k: float = 333.15,
    wall_h_w_m2k: float = 1.0,
    split_walls: bool = False,
    wall_h_low_w_m2k: float | None = None,
    wall_h_high_w_m2k: float | None = None,
    peak_pressure_pa: float = 2.0e6,
    ramp_time_s: float = 0.12,
    end_time_s: float = 0.6,
    write_interval_s: float | None = None,
    deltaT_s: float = 1.0e-7,
    max_deltaT_s: float = 3.0e-6,
    max_co: float = 0.05,
    max_solid_co: float = 0.005,
    n_outer: int = 50,
    p_rgh_relax: float = 0.3,
    u_relax: float = 0.5,
    cp_j_kgk: float = 1900.0,
    kappa_w_mk: float = 0.18,
    mol_weight: float = 104.15,
    shear_modulus_pa: float = 907.0e6,
    elastic: bool = False,
    eta_max_pa_s: float = 1.0e7,
    eta_min_pa_s: float = 5.0,
    t_noflow_k: float = 373.15,
    deltaT_interp_k: float = 5.0,
    p_min_pa: float = 1.0e4,
    sigma_n_m: float = 0.03,
    air_mu_pa_s: float = 0.1,
    air_cp_j_kgk: float = 1007.0,
    air_mol_weight: float = 28.9,
) -> dict:
    """Every text file of a runnable **openInjMoldSim** (OF7-org) plaque-fill case
    as ``{relpath: text}``.

    Pressure-driven, non-isothermal fill of the 2-D plaque (``length_m`` × thin gap
    ``height_m``, one cell deep): melt (``alpha.poly``) enters hot at the gate
    against an imposed injection-pressure ramp (to ``peak_pressure_pa`` over
    ``ramp_time_s``); the y=0/y=H mold walls pull heat out at ``wall_h_w_m2k`` toward
    ``mold_temp_k``; the **Cross-WLF** viscosity (coeffs ``cross_wlf`` or the #106
    corpus card for ``resin``) climbs as the melt cools, so a too-thin/too-long/
    too-cold cavity freezes off mid-fill. The **2-domain Tait** EOS (``tait`` or the
    corpus card) gives the compressible PVT behaviour the packing stage needs.

    Both SHA1 gotchas of this toolchain are pre-handled: the elastic
    ``viscLimEl``/``etaMax`` values are written as literals (no ``#calc``) and no
    ``functions{}`` block is emitted. Constant ``cp_j_kgk``/``kappa_w_mk`` are used
    (hPolynomial thermo + crossWLF transport) so no external cp/kappa tables are
    needed. Run order: ``blockMesh`` → ``setFields`` → ``openInjMoldSim -fillEnd``
    (with ``FOAM_SIGFPE`` **unset** — see the runner; this build's bashrc exports it
    so the FPE trap is on by default, which aborts on transient ``exp`` infinities).

    Stability (validated 2026-06-22, PS/20 mm × 1 mm plaque → 0.98 fill, no nan):
    the advancing melt front excites a low-pressure region that pins to ``pMin`` and
    makes the PIMPLE outer correctors oscillate. Three knobs keep it converged — a
    **small ``max_deltaT_s``** (3 µs; a large cap lets ``deltaT`` grow during the
    quiescent ramp, then the first fast-flow step is too big and diverges *within*
    the step before ``adjustTimeStep`` can react), a **low ``max_co``** (0.05), and
    **under-relaxed non-final PIMPLE iterations** (``p_rgh_relax``/``u_relax``). The
    default wall is near-adiabatic (``wall_h_w_m2k=1``) for a clean fill demo; raise
    it (with ``mold_temp_k`` kept **above** the Cross-WLF singularity ``D2-A2`` —
    ~321 K for corpus PS) to model freeze-off short shots.

    **Asymmetric cooling (#134):** pass ``split_walls=True`` (or a per-wall
    ``wall_h_low_w_m2k``/``wall_h_high_w_m2k``) to mesh the y=0/y=H faces as separate
    ``wallLow``/``wallHigh`` patches, so the pack stage can pull heat out of each at a
    different ``h`` (via :func:`set_wall_h_cmd`). The unequal cooling freezes a
    through-thickness temperature differential whose antisymmetric (bending) part
    :func:`cooling_field_dT_through_k` reduces to an effective ``dT_through_k`` for the
    warpage hand-off. The default (fused ``walls``, both faces equal) is unchanged.

    Returns the dict the caller writes under ``0/``, ``constant/``, ``system/``."""
    L, H, D = length_m, height_m, depth_m
    cw = cross_wlf or _resin_cross_wlf_tait(resin)[0]
    tt = tait or _resin_cross_wlf_tait(resin)[1]
    if cross_wlf is None and tait is None:
        cw, tt = _resin_cross_wlf_tait(resin)
    seed = seed_len_m if seed_len_m is not None else max(2.0 * L / nx, L * 0.04)
    wi = write_interval_s if write_interval_s is not None else max(ramp_time_s / 8.0,
                                                                   end_time_s / 40.0)
    files: dict[str, str] = {}

    # Asymmetric per-wall cooling (#134): when split_walls (or either per-wall h) is
    # requested, the y=0/y=H faces become separate wallLow/wallHigh patches; the fill
    # stage stays symmetric-near-adiabatic unless a per-wall h is given (the asymmetry
    # normally lands at the pack stage, set via set_wall_h_cmd). Default = fused walls.
    split = bool(split_walls or wall_h_low_w_m2k is not None
                 or wall_h_high_w_m2k is not None)
    h_low = wall_h_low_w_m2k if wall_h_low_w_m2k is not None else wall_h_w_m2k
    h_high = wall_h_high_w_m2k if wall_h_high_w_m2k is not None else wall_h_w_m2k

    def _walls(body: str, body_high: str | None = None) -> str:
        """A field's wall boundaryField entry/entries: one fused ``walls`` patch
        (default) or split ``wallLow``/``wallHigh`` patches (asymmetric cooling)."""
        if not split:
            return f"    walls  {{ {body} }}\n"
        return (f"    wallLow  {{ {body} }}\n"
                f"    wallHigh {{ {body_high if body_high is not None else body} }}\n")

    # --- system/ -------------------------------------------------------------
    files["system/blockMeshDict"] = _plaque_blockmeshdict(
        length_m=L, height_m=H, depth_m=D, nx=nx, ny=ny, nz=nz, split_walls=split)
    files["system/controlDict"] = (
        _header("dictionary", "controlDict", "system")
        # startFrom latestTime (not startTime): latestTime is 0 initially so the FILL
        # phase still starts at 0, but it lets the PACK phases resume from the filled
        # state (the tutorial's controlDict0 does the same — see pack continuation).
        + "\napplication     openInjMoldSim;\nstartFrom       latestTime;\n"
        "startTime       0;\nstopAt          endTime;\n"
        f"endTime         {end_time_s:.10g};\ndeltaT          {deltaT_s:.10g};\n"
        "writeControl    adjustableRunTime;\n"
        f"writeInterval   {wi:.10g};\npurgeWrite      0;\nwriteFormat     ascii;\n"
        "writePrecision  8;\nwriteCompression off;\ntimeFormat      general;\n"
        "timePrecision   10;\nrunTimeModifiable yes;\nadjustTimeStep  yes;\n"
        f"maxCo           {max_co:.10g};\nmaxAlphaCo      {max_co:.10g};\n"
        f"maxSolidCo      {max_solid_co:.10g};\nmaxDeltaT       {max_deltaT_s:.10g};\n"
        "pAuxRlx 0.2;\n")
    # NOTE: deliberately NO functions{} block (probes/libsampling.so) — its SHA1
    # write aborts this toolchain at "Starting time loop".
    files["system/fvSchemes"] = (
        _header("dictionary", "fvSchemes", "system")
        + "\nddtSchemes { default Euler; }\n"
        "gradSchemes { default Gauss linear; }\n"
        "divSchemes\n{\n"
        "    div(phi,alpha)  Gauss vanLeer;\n"
        "    div(phirb,alpha) Gauss linear;\n"
        "    div(rhoPhi,U)  Gauss upwind;\n"
        "    div(phi,thermo:rho.poly) Gauss upwind;\n"
        "    div(phi,thermo:rho.air) Gauss upwind;\n"
        "    div(rhoPhi,T)  Gauss upwind;\n"
        "    div(rhoPhi,K)  Gauss upwind;\n"
        "    div(phi,p)      Gauss upwind;\n"
        "    div(phi,k)      Gauss upwind;\n"
        "    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;\n"
        "    div(phi,elSigDev) Gauss upwind;\n"
        "    div(elSigDev) Gauss linear;\n}\n"
        "laplacianSchemes { default Gauss linear uncorrected; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes { default uncorrected; }\n")
    files["system/fvSolution"] = (
        _header("dictionary", "fvSolution", "system")
        + "\nsolvers\n{\n"
        "    alpha.poly { nAlphaCorr 1; nAlphaSubCycles 1; cAlpha 1; }\n"
        '    ".*(rho|rhoFinal)" { solver diagonal; }\n'
        '    "(elSigDev|elSigDevFinal)" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-11; relTol 0; }\n'
        "    pcorr { solver PCG; preconditioner DIC; tolerance 1e-10; relTol 0; maxIter 100; }\n"
        "    p_rgh { solver GAMG; tolerance 1e-9; relTol 0.01; smoother DIC; nPreSweeps 0; nPostSweeps 2; nFinestSweeps 2; cacheAgglomeration true; nCellsInCoarsestLevel 10; agglomerator faceAreaPair; mergeLevels 1; }\n"
        "    p_rghFinal { $p_rgh; relTol 0; }\n"
        "    U { solver smoothSolver; smoother GaussSeidel; tolerance 1e-10; relTol 0.01; nSweeps 1; }\n"
        '    "(T|k|B|nuTilda).*" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-10; relTol 0.01; }\n'
        "}\n"
        "PIMPLE\n{\n    momentumPredictor no;\n    transonic no;\n"
        f"    nOuterCorrectors {n_outer};\n    nCorrectors 3;\n"
        "    nNonOrthogonalCorrectors 0;\n"
        "    outerCorrectorResidualControl\n    {\n"
        "        p_rgh { tolerance 1e-4; relTol 0; }\n"
        "        T     { tolerance 1e-4; relTol 0; }\n    }\n}\n"
        "relaxationFactors\n{\n"
        # Under-relax the NON-final PIMPLE iterations to damp the outer-corrector
        # oscillation the advancing melt front excites against the pMin clamp; the
        # *Final iterations stay 1.0 so the converged step is time-accurate.
        f"    fields {{ p_rgh {p_rgh_relax:.4g}; p_rghFinal 1; T 1; TFinal 1; }}\n"
        f'    equations {{ "U|T|elSigDev" {u_relax:.4g}; "(U|T|elSigDev)Final" 1; }}\n}}\n')
    files["system/setFieldsDict"] = (
        _header("dictionary", "setFieldsDict", "system")
        + "\ndefaultFieldValues ( volScalarFieldValue alpha.poly 0 );\n"
        "regions\n(\n    boxToCell\n    {\n"
        f"        box (-1 -1 -1) ({seed:.10g} 1 1);\n"
        "        fieldValues ( volScalarFieldValue alpha.poly 1 );\n    }\n);\n")

    # --- constant/ -----------------------------------------------------------
    files["constant/g"] = (
        _header("uniformDimensionedVectorField", "g", "constant")
        + "\ndimensions      [0 1 -2 0 0 0 0];\nvalue           (0 0 0);\n")
    files["constant/turbulenceProperties"] = (
        _header("dictionary", "turbulenceProperties", "constant")
        + "\nsimulationType  laminar;\n")
    files["constant/thermophysicalProperties"] = (
        _header("dictionary", "thermophysicalProperties", "constant")
        + "\nphases (poly air);\n"
        f"pMin            [1 -1 -2 0 0 0 0] {p_min_pa:.10g};\n"
        f"sigma           [1 0 -2 0 0 0 0] {sigma_n_m:.10g};\n")
    eos = "\n".join(f"        {k}         {tt[k]:.10g};" for k in
                    ("b1m", "b2m", "b3m", "b4m", "b1s", "b2s", "b3s", "b4s",
                     "b5", "b6", "b7", "b8", "b9"))
    files["constant/thermophysicalProperties.poly"] = (
        _header("dictionary", "thermophysicalProperties", "constant")
        + "\nthermoType\n{\n"
        "    type            mojHeRhoThermo;\n    mixture         pureMixture;\n"
        "    transport       crossWLF;\n    thermo          hPolynomial;\n"
        "    equationOfState polymerPVT;\n    specie          specie;\n"
        "    energy          sensibleInternalEnergy;\n}\n\n"
        "mixture\n{\n"
        f"    specie {{ nMoles 1; molWeight {mol_weight:.10g}; }}\n"
        "    equationOfState\n    {\n" + eos + "\n    }\n"
        "    thermodynamics\n    {\n        Hf 0;\n        Sf 0;\n"
        f"        CpCoeffs<8> ({cp_j_kgk:.10g} 0 0 0 0 0 0 0);\n    }}\n"
        "    transport\n    {\n"
        f"        n          {cw['n']:.10g};\n        Tau        {cw['Tau']:.10g};\n"
        f"        D1         {cw['D1']:.10g};\n        D2         {cw['D2']:.10g};\n"
        f"        D3         {cw['D3']:.10g};\n        A1         {cw['A1']:.10g};\n"
        f"        A2         {cw['A2']:.10g};\n"
        f"        kappa      {kappa_w_mk:.10g};\n        etaMin     {eta_min_pa_s:.10g};\n"
        f"        etaMax     {eta_max_pa_s:.10g};\n        TnoFlow    {t_noflow_k:.10g};\n"
        f"        deltaTempInterp {deltaT_interp_k:.10g};\n    }}\n}}\n")
    files["constant/thermophysicalProperties.air"] = (
        _header("dictionary", "thermophysicalProperties", "constant")
        + "\nthermoType\n{\n"
        "    type            mojHeRhoThermo;\n    mixture         pureMixture;\n"
        "    transport       mojConst;\n    thermo          hConst;\n"
        "    equationOfState perfectGas;\n    specie          specie;\n"
        "    energy          sensibleInternalEnergy;\n}\n\n"
        "mixture\n{\n"
        f"    specie {{ nMoles 1; molWeight {air_mol_weight:.10g}; }}\n"
        f"    thermodynamics {{ Cp {air_cp_j_kgk:.10g}; Hf 0; }}\n"
        f"    transport {{ mu {air_mu_pa_s:.10g}; Pr 1e6; }}\n}}\n")
    # solidificationProperties — viscLimEl written as a LITERAL (the tutorial's
    # `#calc "$etaMax*0.5"` SHA1-aborts this toolchain).
    #
    # The elastic shear-stress (elSigDev) model activates once the cooling melt's
    # viscosity exceeds viscLimEl. On this coarse, constant-cp/kappa case that coupling
    # is violently unstable during pack solidification (max(U)→1e8, nan). For Part A
    # (shrinkage / sink / cooling time — none of which need elasticity) we DISABLE it
    # by setting viscLimEl ABOVE etaMax (per the model: "viscLimEl should be less than
    # etaMax to allow elastic behavior"). elastic=True restores the tutorial's
    # behaviour (for a future residual-stress / warpage path, #113 Part B).
    visc_lim_el = eta_max_pa_s * 0.5 if elastic else eta_max_pa_s * 2.0
    files["constant/solidificationProperties"] = (
        _header("dictionary", "solidificationProperties", "constant")
        + f"\nshearModulus {shear_modulus_pa:.10g};\n"
        f"etaMax {eta_max_pa_s:.10g};\n"
        f"viscLimEl {visc_lim_el:.10g};\n")

    # --- 0/ ------------------------------------------------------------------
    empty = "    frontAndBack { type empty; }\n"
    files["0/alpha.poly"] = (
        _header("volScalarField", "alpha.poly", "0")
        + "\ndimensions      [0 0 0 0 0 0 0];\ninternalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type fixedValue; value uniform 1; }\n"
        "    outlet { type zeroGradient; }\n"
        + _walls("type zeroGradient;") + empty + "}\n")
    files["0/U"] = (
        _header("volVectorField", "U", "0")
        + "\ndimensions      [0 1 -1 0 0 0 0];\ninternalField   uniform (0 0 0);\n"
        "boundaryField\n{\n"
        "    inlet  { type zeroGradient; }\n"
        "    outlet { type zeroGradient; }\n"
        + _walls("type fixedValue; value uniform (0 0 0);") + empty + "}\n")
    files["0/p"] = (
        _header("volScalarField", "p", "0")
        + "\ndimensions      [1 -1 -2 0 0 0 0];\ninternalField   uniform 1e5;\n"
        "boundaryField\n{\n"
        "    inlet  { type calculated; value uniform 1e5; }\n"
        "    outlet { type calculated; value uniform 1e5; }\n"
        + _walls("type calculated; value uniform 1e5;") + empty + "}\n")
    ramp = _pressure_ramp_table(peak_pressure_pa, ramp_time_s, end_time_s)
    files["0/p_rgh"] = (
        _header("volScalarField", "p_rgh", "0")
        + "\ndimensions      [1 -1 -2 0 0 0 0];\ninternalField   uniform 1e5;\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type uniformFixedValue; uniformValue {ramp}; }}\n"
        "    outlet { type fixedValue; value uniform 1e5; }\n"
        + _walls("type fixedFluxPressure; value uniform 1e5;") + empty + "}\n")

    def _t_wall(h):
        return ("type externalWallHeatFluxTemperature; kappaMethod lookup; "
                f"mode coefficient; Ta uniform {mold_temp_k:.10g}; h uniform {h:.10g}; "
                f"value uniform {melt_temp_k:.10g}; kappa mojKappaOut; Qr none; "
                "relaxation 1;")
    files["0/T"] = (
        _header("volScalarField", "T", "0")
        + "\ndimensions      [0 0 0 1 0 0 0];\n"
        f"internalField   uniform {melt_temp_k:.10g};\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type fixedValue; value uniform {melt_temp_k:.10g}; }}\n"
        "    outlet { type externalWallHeatFluxTemperature; kappaMethod lookup; "
        f"mode coefficient; Ta uniform {mold_temp_k:.10g}; h uniform 1; "
        f"value uniform {melt_temp_k:.10g}; kappa mojKappaOut; Qr none; relaxation 1; }}\n"
        + _walls(_t_wall(h_low if split else wall_h_w_m2k), _t_wall(h_high))
        + empty + "}\n")
    files["0/shrRate"] = (
        _header("volScalarField", "shrRate", "0")
        + "\ndimensions      [0 0 -1 0 0 0 0];\ninternalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type calculated; value uniform 0; }\n"
        "    outlet { type calculated; value uniform 0; }\n"
        + _walls("type calculated; value uniform 0;") + empty + "}\n")
    return files


# --- openInjMoldSim packing / cooling continuation (issue #113, Part A) -------
#
# After the cavity FILLS, a real cycle SEALS the gate/outlet and HOLDS while the part
# cools — the stage that gives volumetric shrinkage (real Tait PVT, not the #104 CTE
# estimate), residual pressure, sink risk, and cooling time. openInjMoldSim runs this
# as a *continuation* of the same case from the filled state (startFrom latestTime).
#
# The mechanics mirror the tutorial's AllRun (translated to SERIAL — driftpin runs no
# decomposePar/reconstructPar): per pack phase, reset the restart deltaT so the
# continuation eases in, switch the outlet BCs to "closed + cooling", rewrite
# controlDict's time controls, and re-run the solver (NO -fillEnd). The BC switches
# are emitted as `foamDictionary ... -set ...` argv lists (the env is sourced in
# `_run_foam`), exactly as the tutorial's close_outlet does.


def foamdict_set(target: str, entry: str, value: str) -> list:
    """One ``foamDictionary <target> -entry <entry> -set <value>`` argv list. Quote
    the value yourself if it contains spaces (e.g. a vector ``"(0 0 0)"``)."""
    return ["foamDictionary", target, "-entry", entry, "-set", value]


def close_outlet_cmds(time_dir: str, *, walls_h_entry: str = "boundaryField.walls.h"
                      ) -> list:
    """The argv lists that **seal the gate and let the part cool through the former
    outlet** on time directory ``time_dir`` — the serial equivalent of the tutorial's
    ``close_outlet``:

    - ``p_rgh`` outlet ``type`` → ``fixedFluxPressure`` (no more driving pressure),
    - ``U``     outlet ``type`` → ``fixedValue``, ``value`` → ``uniform (0 0 0)`` (sealed),
    - ``T``     outlet ``h``    → the walls' coefficient (cool through it like a wall).

    The walls' ``h`` is read at run time with ``$(foamDictionary ... -value)`` and
    substituted, so this returns a list where the ``T`` command embeds that shell
    substitution. Run inside the sourced-env bash script `_run_foam` builds."""
    p_rgh = f"{time_dir}/p_rgh"
    u = f"{time_dir}/U"
    t = f"{time_dir}/T"
    h_sub = f"$(foamDictionary {t} -entry {walls_h_entry} -value)"
    return [
        foamdict_set(p_rgh, "boundaryField.outlet.type", "fixedFluxPressure"),
        foamdict_set(u, "boundaryField.outlet.type", "fixedValue"),
        foamdict_set(u, "boundaryField.outlet.value", '"uniform (0 0 0)"'),
        foamdict_set(t, "boundaryField.outlet.h", f'"{h_sub}"'),
    ]


def set_walls_h_cmd(time_dir: str, h_w_m2k: float) -> list:
    """Set the mold-wall heat-transfer coefficient on ``<time_dir>/T`` — used at the
    pack transition to **switch on cooling** when the fill ran near-adiabatic
    (``wall_h_w_m2k≈1``). Real fill is fast enough to be ~isothermal, so we fill hot
    (clean, completes) then extract heat during the pack/hold — and `close_outlet`
    (run after this) copies this same ``h`` onto the sealed outlet."""
    return set_wall_h_cmd(time_dir, "walls", h_w_m2k)


def set_wall_h_cmd(time_dir: str, patch: str, h_w_m2k: float) -> list:
    """Set the heat-transfer coefficient ``h`` on one wall ``patch`` of
    ``<time_dir>/T``. For asymmetric cooling (#134) the case is meshed with split
    ``wallLow``/``wallHigh`` patches, and the pack stage calls this twice (one per
    face) with different ``h`` so the two thickness faces freeze at different rates —
    the through-thickness bending differential the warpage hand-off reads."""
    return foamdict_set(f"{time_dir}/T", f"boundaryField.{patch}.h", f"{h_w_m2k:.10g}")


def reset_restart_deltaT_cmd(time_dir: str, deltaT: float = 1e-10) -> list:
    """Reset the restart ``deltaT`` in ``<time_dir>/uniform/time`` so the pack
    continuation eases in (the serial equivalent of the tutorial's ``new_deltaT`` —
    without it the stiff restart diverges immediately, same class as the fill
    ``maxDeltaT`` lesson)."""
    return foamdict_set(f"{time_dir}/uniform/time", "deltaT", f"{deltaT:.10g}")


def time_extend_cmds(*, end_time_s: float, write_interval_s: float,
                     max_deltaT_s: float) -> list:
    """Rewrite ``system/controlDict``'s time controls for a pack phase (the
    tutorial's ``time_extend <endTime> <writeInterval> <maxDeltaT>``). Returns the
    argv lists."""
    cd = "system/controlDict"
    return [
        foamdict_set(cd, "endTime", f"{end_time_s:.10g}"),
        foamdict_set(cd, "writeInterval", f"{write_interval_s:.10g}"),
        foamdict_set(cd, "maxDeltaT", f"{max_deltaT_s:.10g}"),
    ]


def pack_phase_plan(fill_end_time_s: float, *, n_phases: int = 2,
                    cool_window_s: float | None = None) -> list:
    """A list of ``(end_time_s, write_interval_s, max_deltaT_s)`` for the pack phases,
    each extending the run further and coarsening the step as the dynamics slow (the
    tutorial uses 3 phases out to t=6 s for full cooling).

    For the driftpin TOY/gate we keep it short: ``cool_window_s`` (default a few× the
    fill time) bounds the total cooling so CI stays fast — cool until the gate freezes,
    not the full realistic cycle. Returns ``n_phases`` tuples spanning
    ``[fill_end, fill_end + cool_window]``."""
    cool = cool_window_s if cool_window_s is not None else max(3.0 * fill_end_time_s,
                                                               0.3)
    plan = []
    t0 = fill_end_time_s
    for i in range(n_phases):
        frac = (i + 1) / n_phases
        end = t0 + cool * frac
        wi = cool / (n_phases * 8)
        # coarsen maxDeltaT as we go (pack1 tightest, later phases looser)
        mdt = 1e-4 * (i + 1)
        plan.append((round(end, 8), round(wi, 8), mdt))
    return plan


def write_openinjmoldsim_case(case_dir: str, **kwargs) -> dict:
    """Write a complete, runnable openInjMoldSim (OF7-org) plaque-fill case under
    ``case_dir``. Same knobs as ``openinjmoldsim_case_files``. Returns metadata for
    the parser/gate: ``{case_dir, length_m, height_m, depth_m, nx, ny, resin,
    peak_pressure_pa, end_time_s, expected_fill_time_s, flow_length_ratio,
    backend}``. ``expected_fill_time_s`` is a coarse ramp-based estimate (the
    pressure-driven fill has no fixed plug velocity); the real fill time comes from
    the solved time directories."""
    files = openinjmoldsim_case_files(**kwargs)
    for rel, text in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    L = kwargs.get("length_m", 0.02)
    H = kwargs.get("height_m", 1.0e-3)
    return {
        "case_dir": case_dir,
        "length_m": L,
        "height_m": H,
        "depth_m": kwargs.get("depth_m", 1.0e-3),
        "nx": kwargs.get("nx", 60),
        "ny": kwargs.get("ny", 8),
        "resin": kwargs.get("resin", "PS"),
        "peak_pressure_pa": kwargs.get("peak_pressure_pa", 2.0e6),
        "ramp_time_s": kwargs.get("ramp_time_s", 0.12),
        "end_time_s": kwargs.get("end_time_s", 0.6),
        "expected_fill_time_s": kwargs.get("ramp_time_s", 0.12),
        "flow_length_ratio": L / H if H else float("inf"),
        "backend": "openInjMoldSim (GPL-3.0, OpenFOAM-7 .org)",
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
    txt = open(path, encoding="utf-8").read()
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
    for name in (_ALPHA_MELT, "alpha.poly", "alpha.water", "alpha.phase1"):
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


# --- packing / cooling parse + gate (issue #113, Part A) ---------------------

# Universal Tait constant (Moldflow/Autodesk 2-domain Tait-PVT form).
_TAIT_C = 0.0894


def tait_density(tait: dict, T_k: float, p_pa: float) -> float:
    """Specific density [kg/m^3] from the **2-domain Tait** PVT law (the same EOS
    openInjMoldSim integrates), given the corpus ``tait`` coefficient dict (keys
    ``b1m..b9`` — see ``_resin_cross_wlf_tait``), temperature ``T_k`` and pressure
    ``p_pa``.

    V(T,p) = V0(T)·[1 − C·ln(1 + p/B(T))] + Vt;  ρ = 1/V. The melt branch (T > the
    transition Tt = b5 + b6·p) uses the ``*m`` coefficients with Vt = 0; the solid
    branch uses ``*s`` plus the solid-state correction Vt. Used to predict the
    expected cooling densification so the gate can check the solve is faithful to its
    own EOS (the 'validate the artifact, not a model' discipline)."""
    b = tait
    b5, b6 = b["b5"], b["b6"]
    Tt = b5 + b6 * p_pa
    dT = T_k - b5
    if T_k > Tt:                                   # melt / liquid domain
        V0 = b["b1m"] + b["b2m"] * dT
        B = b["b3m"] * math.exp(-b["b4m"] * dT)
        Vt = 0.0
    else:                                          # solid domain
        V0 = b["b1s"] + b["b2s"] * dT
        B = b["b3s"] * math.exp(-b["b4s"] * dT)
        Vt = b["b7"] * math.exp(b["b8"] * dT - b["b9"] * p_pa)
    V = V0 * (1.0 - _TAIT_C * math.log(1.0 + p_pa / B)) + Vt
    return 1.0 / V if V > 0 else float("nan")


def tait_densification_pct(tait: dict, *, T_hot_k: float, T_cold_k: float,
                           p_pa: float) -> float:
    """Expected volumetric densification (%) from the Tait EOS as the melt cools from
    ``T_hot_k`` to ``T_cold_k`` at pressure ``p_pa``: ``100·(1 − ρ_hot/ρ_cold)``.
    The physical yardstick the solved cooling shrinkage is checked against."""
    rho_hot = tait_density(tait, T_hot_k, p_pa)
    rho_cold = tait_density(tait, T_cold_k, p_pa)
    if not (rho_hot > 0 and rho_cold > 0):
        return float("nan")
    return 100.0 * (1.0 - rho_hot / rho_cold)


# --- net mold shrinkage (the cavity-sizing number; issue #116) ---------------
#
# parse_pack / pack_gate report the RAW PVT densification on cooling
# (volumetric_shrinkage_pct = 1 - rho_fill/rho_final), checked for Tait-EOS
# faithfulness — deliberately NOT the net "mold shrinkage" molders quote on a resin
# card (PS 0.4-0.7%, HDPE 1.5-4.0% linear). The quoted number is POST-packing-feed:
# while the gate is open the melt is fed at hold pressure to make up the volume lost
# as it densifies, so only the densification AFTER the gate freezes is uncompensated
# and becomes net dimensional shrinkage. The gate seals when the melt at the gate
# reaches the no-flow / PVT transition temperature (Tt = b5 + b6*p — the same melt/
# solid switch the Tait EOS uses). After that the sealed, constant-mass part keeps
# cooling to room temperature at (decaying) atmospheric pressure: that residual
# densification IS the net mold shrinkage. This is the cavity-sizing verdict the #104
# CTE screen approximates and the number gated against the corpus band.


def tait_transition_temp_k(tait: dict, p_pa: float) -> float:
    """The 2-domain Tait melt/solid transition temperature ``Tt = b5 + b6*p`` — where
    the melt at the gate solidifies and the gate seals (feeding stops). The natural
    default gate-freeze temperature for the net-shrinkage model."""
    return tait["b5"] + tait["b6"] * p_pa


def net_mold_shrinkage(
    tait: dict, *, melt_temp_k: float, gate_freeze_temp_k: float | None = None,
    room_temp_k: float = 296.15, hold_pressure_pa: float = 1.0e7,
    ambient_pressure_pa: float = 1.0e5,
) -> dict:
    """Net (post-packing-feed) mold shrinkage from the 2-domain Tait EOS — the
    cavity-sizing number molders quote, distinct from the raw PVT densification.

    A real cycle FEEDS fresh melt through the gate at ``hold_pressure_pa`` while the
    melt densifies, so the densification from the melt temperature down to **gate
    freeze** is compensated (make-up melt) and contributes ~nothing to the part's net
    dimensional change. The gate seals at ``gate_freeze_temp_k`` (default the Tait
    transition ``b5 + b6*p_hold`` — the no-flow temperature). The sealed, constant-mass
    part then cools the rest of the way to ``room_temp_k`` at ``ambient_pressure_pa``;
    that **uncompensated** densification is the net shrinkage:

        S_vol = 1 - rho(T_gate_freeze, p_hold) / rho(T_room, p_atm)

    (cavity packed full and dense at the gate-freeze state vs. the free cold part).
    Linear shrinkage assumes isotropy: ``S_lin = 1 - (1 - S_vol)^(1/3)`` — the form
    resin cards quote (``mold_shrinkage_pct``). Much smaller than the raw melt->room
    densification because the feed makes up the early shrink.

    Returns ``{net_vol_pct, net_linear_pct, raw_vol_pct, compensated_vol_pct,
    gate_freeze_temp_k, hold_pressure_pa, room_temp_k, rho_gate_freeze, rho_room}``;
    ``raw_vol_pct`` is the un-fed melt->room densification (the upper bound) and
    ``compensated_vol_pct`` the make-up the feed contributes (melt->gate-freeze)."""
    p_hold = hold_pressure_pa
    default_tgf = gate_freeze_temp_k is None
    tgf = (tait_transition_temp_k(tait, p_hold) if default_tgf
           else gate_freeze_temp_k)
    rho_melt = tait_density(tait, melt_temp_k, p_hold)
    # The gate seals while the melt is still MOLTEN at the no-flow temperature, so the
    # crystallization volume jump (the melt/solid Tait branch step — big for HDPE, ~nil
    # for amorphous PS) happens AFTER feeding stops and is uncompensated. Evaluate the
    # gate-freeze state on the melt side of the transition (a negligible nudge) so that
    # jump is counted in the net shrinkage; an explicit gate_freeze_temp_k is honoured
    # on whichever branch it falls.
    rho_gf = tait_density(tait, tgf + (1e-3 if default_tgf else 0.0), p_hold)
    rho_room = tait_density(tait, room_temp_k, ambient_pressure_pa)
    if not (rho_gf > 0 and rho_room > 0 and rho_melt > 0):
        return {"net_vol_pct": float("nan"), "net_linear_pct": float("nan"),
                "raw_vol_pct": float("nan"), "compensated_vol_pct": float("nan"),
                "gate_freeze_temp_k": tgf, "hold_pressure_pa": p_hold,
                "room_temp_k": room_temp_k, "rho_gate_freeze": rho_gf,
                "rho_room": rho_room}
    s_vol = 1.0 - rho_gf / rho_room                       # uncompensated (net)
    raw_vol = 1.0 - rho_melt / rho_room                   # un-fed upper bound
    comp_vol = 1.0 - rho_melt / rho_gf                    # what the feed makes up
    s_lin = 1.0 - (1.0 - s_vol) ** (1.0 / 3.0) if s_vol < 1.0 else float("nan")
    return {
        "net_vol_pct": round(100.0 * s_vol, 4),
        "net_linear_pct": round(100.0 * s_lin, 4),
        "raw_vol_pct": round(100.0 * raw_vol, 4),
        "compensated_vol_pct": round(100.0 * comp_vol, 4),
        "gate_freeze_temp_k": round(tgf, 3),
        "hold_pressure_pa": round(p_hold, 1),
        "room_temp_k": room_temp_k,
        "rho_gate_freeze": round(rho_gf, 3),
        "rho_room": round(rho_room, 3),
    }


def mold_shrinkage_gate(
    net: dict, *, corpus_band_pct: tuple, band_pct: float = 30.0,
) -> dict:
    """House verdict for the **net mold shrinkage** vs the resin's published linear
    band (``materials`` card ``mold_shrinkage_pct``, e.g. PS ``(0.4, 0.7)``, HDPE
    ``(1.5, 4.0)``) — the cavity-sizing pass/fail (#116).

    ``pass`` is true when the modelled ``net_linear_pct`` lands inside
    ``corpus_band_pct = (lo, hi)``: the cavity allowance the part was (or should be)
    sized for is consistent with the resin's quoted shrinkage. Below the band → the
    part is over-packed / cavity oversized (parts run large); above → under-packed /
    undersized (parts run small, possible sink). ``score`` is 1.0 inside the band and
    decays with the fractional distance to the nearest edge. This is **distinct from**
    ``pack_gate`` (whose pass/fail is sink risk and whose shrinkage check is raw-PVT
    Tait faithfulness) — keep both: one sizes the cavity, the other flags sinks.

    Returns ``{pass, score, fidelity, band_pct, net_linear_pct, net_vol_pct,
    corpus_band_pct, in_band, raw_vol_pct, compensated_vol_pct, gate_freeze_temp_k,
    warnings}``."""
    lo, hi = float(corpus_band_pct[0]), float(corpus_band_pct[1])
    lin = net.get("net_linear_pct")
    warnings: list[str] = []
    in_band = lin is not None and lin == lin and lo <= lin <= hi
    if lin is None or lin != lin:
        score = 0.0
        warnings.append("net shrinkage unavailable (degenerate Tait/temperature inputs)")
    elif in_band:
        score = 1.0
    else:
        width = max(hi - lo, 1e-6)
        dist = (lo - lin) if lin < lo else (lin - hi)
        score = max(0.0, 1.0 - dist / width)
        if lin < lo:
            warnings.append(
                f"net mold shrinkage {lin:.2f}% is BELOW the resin band "
                f"{lo:.2f}-{hi:.2f}% — likely over-packed (high hold pressure / late "
                "gate freeze): the cavity allowance is too small, parts will run large")
        else:
            warnings.append(
                f"net mold shrinkage {lin:.2f}% is ABOVE the resin band "
                f"{lo:.2f}-{hi:.2f}% — under-packed (low hold pressure / early gate "
                "freeze): the cavity allowance is too large, parts run small (and risk "
                "sink); raise/extend the hold")
    return {
        "pass": bool(in_band),
        "score": round(float(score), 4),
        "fidelity": "solve",
        "band_pct": band_pct,
        "net_linear_pct": lin,
        "net_vol_pct": net.get("net_vol_pct"),
        "corpus_band_pct": [lo, hi],
        "in_band": bool(in_band),
        "raw_vol_pct": net.get("raw_vol_pct"),
        "compensated_vol_pct": net.get("compensated_vol_pct"),
        "gate_freeze_temp_k": net.get("gate_freeze_temp_k"),
        "warnings": warnings,
    }


# --- cooling field -> antisymmetric dT_through_k (warpage hand-off; issue #116)
#
# Part B's molding_warpage_submit takes the through-thickness differential dT_through_k
# as an INPUT. The fuller coupling auto-derives it from the Part-A cooling solve's
# cell-centre temperature field: average T into through-thickness (y) layers, then
# reduce that profile to its ANTISYMMETRIC (bending) component — the only part that
# drives warp (a symmetric profile just shrinks the part uniformly). The result is an
# effective linear dT_through_k the free-plate twin (warpage.free_plate_thermal_bow)
# and the ccx eigenstrain consume directly.


def through_thickness_layer_temps(
    field: list, *, nx: int, ny: int, nz: int = 1, mask: list | None = None,
) -> list:
    """Average a structured-mesh cell ``field`` into ``ny`` through-thickness layers.

    The openInjMoldSim plaque meshes **x fastest, then y (thickness — walls at y=0 and
    y=H), then z**, so cell ``k`` sits in y-layer ``iy = (k // nx) % ny``. Returns
    ``[layer_0 .. layer_{ny-1}]`` from the y=0 wall to the y=H wall, each the mean over
    that layer's cells. ``mask`` (a per-cell list; a cell counts when ``mask[k] >=
    0.5``) restricts the average to melt/polymer cells (pass the ``alpha.poly`` field).
    Layers with no contributing cell come back as ``None``."""
    sums = [0.0] * ny
    cnts = [0] * ny
    for k, v in enumerate(field):
        if mask is not None and (k >= len(mask) or mask[k] < 0.5):
            continue
        iy = (k // nx) % ny
        sums[iy] += v
        cnts[iy] += 1
    return [(sums[i] / cnts[i]) if cnts[i] else None for i in range(ny)]


def antisymmetric_dT_through(layer_temps: list) -> float:
    """Reduce a through-thickness temperature profile to its **antisymmetric (bending)
    component**, expressed as an effective linear differential ``dT_through_k = T(min-
    thickness face) − T(max-thickness face)``.

    Least-squares fit ``T(xi) ≈ a + b·xi`` over layer-centre coordinates
    ``xi_i = (i + 0.5)/n − 0.5`` (the y=0 / min-thickness face at ``xi = −0.5``). The
    even (symmetric) part of the profile contributes zero to the odd moment ``Σ xi·T``,
    so the slope isolates the bending eigenstrain; ``dT_eff = −b`` reproduces the
    convention of :func:`warpage.linear_through_thickness_temps` (positive ⇒ the min-
    thickness face is hotter, the part bows toward it). ``None`` layers are skipped.

    Raises ``ValueError`` with fewer than two usable layers."""
    pts = [(((i + 0.5) / len(layer_temps)) - 0.5, t)
           for i, t in enumerate(layer_temps) if t is not None]
    if len(pts) < 2:
        raise ValueError("need >= 2 usable through-thickness layers")
    n = len(pts)
    xbar = sum(x for x, _ in pts) / n
    tbar = sum(t for _, t in pts) / n
    sxx = sum((x - xbar) ** 2 for x, _ in pts)
    if sxx <= 0:
        return 0.0
    b = sum((x - xbar) * (t - tbar) for x, t in pts) / sxx
    return -b


def cooling_field_dT_through_k(
    case_dir: str, *, nx: int, ny: int, nz: int = 1, time_dir: str | None = None,
    alpha_melt_min: float = 0.5,
) -> dict | None:
    """Read the pack/cool case's ``T`` field and reduce it to an effective
    antisymmetric ``dT_through_k`` for the warpage hand-off (#116) — so a
    ``molding_fill_submit(stages="fill_pack")`` cooling solve flows straight into
    ``molding_warpage_submit`` without the user hand-passing the differential.

    Averages the cell-centre temperatures into ``ny`` through-thickness layers
    (melt-masked at ``alpha.poly >= alpha_melt_min``), then extracts the bending
    component via :func:`antisymmetric_dT_through`. Returns ``{dT_through_k,
    layer_temps, n_layers, mean_temp_k, time}`` (temperatures in K, matching the
    field), or None when the field is missing/degenerate."""
    td = time_dir or _latest_time_dir(case_dir)
    if td is None:
        return None
    T = _read_internal_scalar_field(os.path.join(case_dir, td, "T"))
    if not T or len(T) < ny:
        return None
    af = _alpha_file(case_dir, td)
    mask = _read_internal_scalar_field(af) if af else None
    layers = through_thickness_layer_temps(T, nx=nx, ny=ny, nz=nz, mask=mask)
    usable = [t for t in layers if t is not None]
    if len(usable) < 2:
        return None
    try:
        dT = antisymmetric_dT_through(layers)
    except ValueError:
        return None
    return {
        "dT_through_k": round(dT, 6),
        "layer_temps": [round(t, 4) if t is not None else None for t in layers],
        "n_layers": len(usable),
        "mean_temp_k": round(sum(usable) / len(usable), 4),
        "time": td,
    }


def _melt_stats(case_dir: str, time_dir: str, *, alpha_melt_min: float = 0.99):
    """Mean/min density and max/mean temperature over the **melt** cells
    (alpha.poly >= alpha_melt_min) at ``time_dir``, plus the melt cell count and the
    max residual pressure. Returns a dict or None if fields are missing.

    Masking to melt cells matters: the residual **air** pocket compresses and heats to
    unphysical values (T up to ~800 K, rho < 1) that would swamp an unmasked mean."""
    af = _alpha_file(case_dir, time_dir)
    if af is None:
        return None
    a = _read_internal_scalar_field(af)
    rho = _read_internal_scalar_field(os.path.join(case_dir, time_dir, "rho"))
    T = _read_internal_scalar_field(os.path.join(case_dir, time_dir, "T"))
    if not a or not rho or not T:
        return None
    melt = [i for i, v in enumerate(a) if v >= alpha_melt_min and i < len(rho)
            and i < len(T)]
    if not melt:
        return None
    rho_m = [rho[i] for i in melt]
    T_m = [T[i] for i in melt]
    out = {
        "n_melt_cells": len(melt),
        "rho_mean": sum(rho_m) / len(rho_m),
        "rho_min": min(rho_m),
        "T_max": max(T_m),
        "T_mean": sum(T_m) / len(T_m),
    }
    for pname in ("p", "p_rgh"):
        pv = _read_internal_scalar_field(os.path.join(case_dir, time_dir, pname))
        if pv:
            out["residual_pressure_pa"] = max(pv)
            break
    return out


def cooling_time_s(case_dir: str, *, fill_end_time_s: float, eject_temp_k: float,
                   alpha_melt_min: float = 0.99) -> float | None:
    """Time (from ``fill_end_time_s``) for the hottest **melt** cell to drop below
    ``eject_temp_k`` — the cooling/cycle-time driver. Scans the written time
    directories at/after the fill end. Returns None if the part never cools below the
    eject temp within the run (the cooling window was too short)."""
    for name in _time_dirs(case_dir):
        try:
            t = float(name)
        except ValueError:
            continue
        if t < fill_end_time_s:
            continue
        st = _melt_stats(case_dir, name, alpha_melt_min=alpha_melt_min)
        if st and st["T_max"] <= eject_temp_k:
            return round(t - fill_end_time_s, 6)
    return None


def parse_pack(
    case_dir: str, *, fill_rho_mean: float | None = None,
    fill_end_time_s: float | None = None, eject_temp_k: float = 353.15,
    t_noflow_k: float = 373.15, alpha_melt_min: float = 0.99,
    time_dir: str | None = None,
) -> dict | None:
    """Read the **final** (pack/cool) state and return the packing result.

    ``rho_mean_final`` is the cavity-mean melt density after cooling; with
    ``fill_rho_mean`` (the melt density at fill end) the **volumetric shrinkage** is
    ``1 - rho_fill/rho_final`` (densification on cooling — the real Tait-PVT twin of
    the #104 CTE estimate). ``rho_min`` flags **sink risk** (a local under-packed,
    low-density region). ``frozen_fraction`` is the share of melt cells already below
    ``t_noflow_k`` (solidified). ``residual_pressure_pa`` is the holding pressure left
    in the field. ``cooling_time_s`` (needs ``fill_end_time_s``) is the time for the
    hottest melt cell to fall below ``eject_temp_k``.

    Returns None if no melt fields are present (the pack solve failed)."""
    td = time_dir or _latest_time_dir(case_dir)
    if td is None:
        return None
    st = _melt_stats(case_dir, td, alpha_melt_min=alpha_melt_min)
    if st is None:
        return None
    # frozen fraction (recompute over the melt mask at this time)
    af = _alpha_file(case_dir, td)
    a = _read_internal_scalar_field(af) if af else None
    T = _read_internal_scalar_field(os.path.join(case_dir, td, "T"))
    frozen_fraction = None
    if a and T:
        melt = [i for i, v in enumerate(a) if v >= alpha_melt_min and i < len(T)]
        if melt:
            frozen_fraction = round(
                sum(1 for i in melt if T[i] < t_noflow_k) / len(melt), 4)
    out = {
        "time": td,
        "n_melt_cells": st["n_melt_cells"],
        "rho_mean_final": round(st["rho_mean"], 3),
        "rho_min": round(st["rho_min"], 3),
        "T_max_melt": round(st["T_max"], 3),
        "T_mean_melt": round(st["T_mean"], 3),
        "frozen_fraction": frozen_fraction,
    }
    if "residual_pressure_pa" in st:
        out["residual_pressure_pa"] = round(st["residual_pressure_pa"], 3)
    if fill_rho_mean:
        shr = 1.0 - (fill_rho_mean / st["rho_mean"]) if st["rho_mean"] else 0.0
        out["fill_rho_mean"] = round(fill_rho_mean, 3)
        out["volumetric_shrinkage_pct"] = round(100.0 * shr, 4)
    if fill_end_time_s is not None:
        out["cooling_time_s"] = cooling_time_s(
            case_dir, fill_end_time_s=fill_end_time_s, eject_temp_k=eject_temp_k,
            alpha_melt_min=alpha_melt_min)
    return out


def pack_gate(
    parsed: dict, *, tait: dict | None = None, fill_T_mean_k: float | None = None,
    sink_rel: float = 0.92, faithfulness_tol: float = 2.0,
) -> dict:
    """Turn a ``parse_pack`` result into the house verdict shape for the **packing**
    stage.

    Design note (issue #113): the solved ``volumetric_shrinkage_pct`` is the **raw
    PVT densification on cooling**, NOT the net "mold shrinkage" molders quote (which
    is post-packing-feed compensation — out of scope without modelling the feed). So
    the gate does **not** pass/fail on a net-shrinkage band. Instead:

    - **pass/fail = sink risk** (the actionable moldability signal): a local
      under-packed region, ``rho_min < sink_rel · rho_mean_final`` — measured against
      the part's OWN mean density, so no dubious external reference.
    - **shrinkage is checked for FAITHFULNESS** against the resin's own 2-domain Tait
      EOS (when ``tait`` + ``fill_T_mean_k`` are given): the solved densification
      should track ``tait_densification_pct`` between the fill and final mean melt
      temperatures at the hold pressure. A solved/expected ratio outside
      ``[1/faithfulness_tol, faithfulness_tol]`` raises a warning that the solve may be
      unfaithful (heterogeneous fill-end state, too-coarse mesh) — it does NOT fail the
      gate (it's a sanity check, not a moldability verdict).

    ``score`` is the packing uniformity ``rho_min/rho_mean_final`` (1.0 = perfectly
    uniform, no sink). ``fidelity="solve"``. Returns ``{pass, score, fidelity,
    band_pct, volumetric_shrinkage_pct, expected_densification_pct, pvt_faithful,
    rho_min, rho_mean_final, sink_risk, frozen_fraction, cooling_time_s,
    residual_pressure_pa, warnings}``."""
    warnings: list[str] = []
    shr = parsed.get("volumetric_shrinkage_pct")
    rho_min = parsed.get("rho_min")
    rho_mean = parsed.get("rho_mean_final")

    # --- sink risk (the pass/fail signal) ---
    sink_risk = False
    if rho_min is not None and rho_mean:
        sink_risk = rho_min < sink_rel * rho_mean
        if sink_risk:
            warnings.append(
                f"sink-mark risk — a melt region is only {rho_min:.0f} kg/m^3, "
                f"{(1 - rho_min/rho_mean)*100:.0f}% below the part mean "
                f"{rho_mean:.0f} kg/m^3: under-packed, expect a surface sink/void there")

    # --- Tait-EOS faithfulness of the solved densification (informational) ---
    expected = None
    pvt_faithful = None
    if tait and fill_T_mean_k and parsed.get("T_mean_melt") is not None:
        p_pa = parsed.get("residual_pressure_pa") or 1.0e5
        expected = tait_densification_pct(
            tait, T_hot_k=fill_T_mean_k, T_cold_k=parsed["T_mean_melt"], p_pa=p_pa)
        if (shr is not None and expected and expected == expected   # not nan
                and expected > 1e-6):
            ratio = shr / expected
            pvt_faithful = (1.0 / faithfulness_tol) <= ratio <= faithfulness_tol
            if not pvt_faithful:
                warnings.append(
                    f"solved cooling densification {shr:.2f}% is {ratio:.1f}x the "
                    f"Tait-EOS expectation {expected:.2f}% (T {fill_T_mean_k:.0f}->"
                    f"{parsed['T_mean_melt']:.0f} K @ {p_pa/1e6:.1f} MPa) — likely a "
                    "heterogeneous fill-end state or too-coarse mesh, treat the "
                    "shrinkage number with caution")

    frozen = parsed.get("frozen_fraction")
    if frozen is not None and frozen < 0.5:
        warnings.append(
            f"only {frozen*100:.0f}% of the melt has frozen in the cooling window — "
            "cooling_time_s is a lower bound; extend the pack window (or thin the "
            "wall) for a true cycle time")

    score = (rho_min / rho_mean) if (rho_min is not None and rho_mean) else 0.5
    return {
        "pass": not sink_risk,
        "score": round(float(min(1.0, score)), 4),
        "fidelity": "solve",
        "band_pct": 25.0,
        "volumetric_shrinkage_pct": shr,
        "expected_densification_pct": (round(expected, 4)
                                       if expected and expected == expected else None),
        "pvt_faithful": pvt_faithful,
        "rho_min": rho_min,
        "rho_mean_final": rho_mean,
        "sink_risk": sink_risk,
        "frozen_fraction": frozen,
        "cooling_time_s": parsed.get("cooling_time_s"),
        "residual_pressure_pa": parsed.get("residual_pressure_pa"),
        "warnings": warnings,
    }


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

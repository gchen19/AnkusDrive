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


def _plaque_blockmeshdict(*, length_m, height_m, depth_m, nx, ny, nz=1) -> str:
    """blockMeshDict for the 2-D plaque: flow along x (``inlet`` at x=0, ``outlet``
    at x=L), the cooled mold ``walls`` at y=0 and y=H, ``frontAndBack`` (the ±z
    faces) ``empty`` (one cell deep). One hex block, ``nx``×``ny``×``nz`` cells."""
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
    boundary = (
        "boundary\n(\n"
        "    inlet  { type patch; faces ((0 3 7 4)); }\n"
        "    outlet { type patch; faces ((1 5 6 2)); }\n"
        "    walls  { type wall;  faces ((0 1 5 4) (3 2 6 7)); }\n"
        "    frontAndBack { type empty; faces ((0 1 2 3) (4 5 6 7)); }\n"
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

    # --- system/ -------------------------------------------------------------
    files["system/blockMeshDict"] = _plaque_blockmeshdict(
        length_m=L, height_m=H, depth_m=D, nx=nx, ny=ny, nz=nz)
    files["system/controlDict"] = (
        _header("dictionary", "controlDict", "system")
        + "\napplication     openInjMoldSim;\nstartFrom       startTime;\n"
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
    files["constant/solidificationProperties"] = (
        _header("dictionary", "solidificationProperties", "constant")
        + f"\nshearModulus {shear_modulus_pa:.10g};\n"
        f"etaMax {eta_max_pa_s:.10g};\n"
        f"viscLimEl {eta_max_pa_s * 0.5:.10g};\n")

    # --- 0/ ------------------------------------------------------------------
    empty = "    frontAndBack { type empty; }\n"
    files["0/alpha.poly"] = (
        _header("volScalarField", "alpha.poly", "0")
        + "\ndimensions      [0 0 0 0 0 0 0];\ninternalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type fixedValue; value uniform 1; }\n"
        "    outlet { type zeroGradient; }\n"
        "    walls  { type zeroGradient; }\n" + empty + "}\n")
    files["0/U"] = (
        _header("volVectorField", "U", "0")
        + "\ndimensions      [0 1 -1 0 0 0 0];\ninternalField   uniform (0 0 0);\n"
        "boundaryField\n{\n"
        "    inlet  { type zeroGradient; }\n"
        "    outlet { type zeroGradient; }\n"
        "    walls  { type fixedValue; value uniform (0 0 0); }\n" + empty + "}\n")
    files["0/p"] = (
        _header("volScalarField", "p", "0")
        + "\ndimensions      [1 -1 -2 0 0 0 0];\ninternalField   uniform 1e5;\n"
        "boundaryField\n{\n"
        "    inlet  { type calculated; value uniform 1e5; }\n"
        "    outlet { type calculated; value uniform 1e5; }\n"
        "    walls  { type calculated; value uniform 1e5; }\n" + empty + "}\n")
    ramp = _pressure_ramp_table(peak_pressure_pa, ramp_time_s, end_time_s)
    files["0/p_rgh"] = (
        _header("volScalarField", "p_rgh", "0")
        + "\ndimensions      [1 -1 -2 0 0 0 0];\ninternalField   uniform 1e5;\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type uniformFixedValue; uniformValue {ramp}; }}\n"
        "    outlet { type fixedValue; value uniform 1e5; }\n"
        "    walls  { type fixedFluxPressure; value uniform 1e5; }\n" + empty + "}\n")
    files["0/T"] = (
        _header("volScalarField", "T", "0")
        + "\ndimensions      [0 0 0 1 0 0 0];\n"
        f"internalField   uniform {melt_temp_k:.10g};\n"
        "boundaryField\n{\n"
        f"    inlet  {{ type fixedValue; value uniform {melt_temp_k:.10g}; }}\n"
        "    outlet { type externalWallHeatFluxTemperature; kappaMethod lookup; "
        f"mode coefficient; Ta uniform {mold_temp_k:.10g}; h uniform 1; "
        f"value uniform {melt_temp_k:.10g}; kappa mojKappaOut; Qr none; relaxation 1; }}\n"
        "    walls  { type externalWallHeatFluxTemperature; kappaMethod lookup; "
        f"mode coefficient; Ta uniform {mold_temp_k:.10g}; h uniform {wall_h_w_m2k:.10g}; "
        f"value uniform {melt_temp_k:.10g}; kappa mojKappaOut; Qr none; relaxation 1; }}\n"
        + empty + "}\n")
    files["0/shrRate"] = (
        _header("volScalarField", "shrRate", "0")
        + "\ndimensions      [0 0 -1 0 0 0 0];\ninternalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type calculated; value uniform 0; }\n"
        "    outlet { type calculated; value uniform 0; }\n"
        "    walls  { type calculated; value uniform 0; }\n" + empty + "}\n")
    return files


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
        with open(path, "w") as f:
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

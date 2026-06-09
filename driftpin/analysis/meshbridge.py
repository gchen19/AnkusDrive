"""Geometry-driven meshing bridge (P3 M4) — heavy solves on an ARBITRARY FreeCAD solid.

Every P2/P3 heavy solve so far meshes *parametrically* (a native 1-D Elmer slab, an
axisymmetric wedge pipe, a 3-block flat plate). This module is the bridge the P3
kickoff names the "real unlock": mesh a real FreeCAD solid, solve on it, and gate the
result against the parametric/analytic case of the same shape. Two paths:

* **Elmer:** FreeCAD solid → Gmsh (the worker's ``femmesh.gmshtools``, main thread) →
  UNV → **ElmerGrid** (``8 2`` = UNV in, ElmerSolver mesh out) → HeatSolver. The
  load-bearing fact (verified live): FreeCAD's UNV export preserves **per-face
  groups**, and ElmerGrid turns them into boundary tags with
  ``tag i == shape.Faces[i-1]`` — so boundary conditions bind to FreeCAD face indices
  with no geometric retagging. FreeCAD meshes in mm; the ``.sif`` carries
  ``Coordinate Scaling = 0.001`` so the solve stays SI.
* **OpenFOAM:** FreeCAD solid → per-face-group multi-``solid`` ASCII STL (regions
  ``inlet`` / ``outlet`` / ``walls``) → ``blockMesh`` background box around it →
  ``snappyHexMesh`` (castellated + snap, one patch per STL region, implicit feature
  snap — no surfaceFeatureExtract step) → ``simpleFoam``. The pressure drop is read
  from the converged ``p`` field by the shipped ``openfoam.parse_pressure_drop``;
  for a bridged pipe the **developed-profile estimator** (2·mean(p)) is the gateable
  number — the inlet max sees the snapped-corner entrance loss.

Verification is **relative** (an arbitrary mesh has no closed form): a FreeCAD *box*
solved as a plane wall must match ``thermal_transient_1d`` (measured 0.2–0.9 % at
Bi=0.5, Fo≈0.5 on the default Gmsh mesh), and a FreeCAD *cylinder* run through
snappyHexMesh must match Hagen–Poiseuille / the parametric wedge-pipe builder
(measured ``dp_developed`` within 0.6 % at Re=50).

Pure-Python, FreeCAD-free (the FreeCAD-side meshing/tessellation lives in the worker;
everything here is case text + commands + parsers, testable on the no-FreeCAD lane).
SI units unless a name says otherwise. See ``tests/test_meshbridge.py``.
"""
from __future__ import annotations

import math
import os

from .openfoam import _header

# SaveScalars column order fixed by body_transient_sif: operator 1 = max, 2 = min.
_COL_MAX = 0
_COL_MIN = 1


# --- Elmer path: UNV -> ElmerGrid -> HeatSolver --------------------------------

def elmergrid_cmd(unv_filename: str, mesh_name: str) -> list:
    """The ElmerGrid argv converting a UNV volume mesh into an ElmerSolver mesh
    directory: ``ElmerGrid 8 2 <unv> -out <mesh_name> -autoclean`` (8 = UNV in,
    2 = ElmerSolver out; ``-autoclean`` renumbers and drops unused nodes while
    preserving the UNV face-group → boundary-tag order)."""
    if not unv_filename or not mesh_name:
        raise ValueError("unv_filename and mesh_name are required")
    return ["ElmerGrid", "8", "2", unv_filename, "-out", mesh_name, "-autoclean"]


def body_transient_sif(
    *,
    k: float,
    rho: float,
    cp: float,
    h_conv: float,
    t_initial_c: float,
    t_ambient_c: float,
    dt: float,
    n_steps: int,
    convection_tags: list,
    mesh_name: str = "bodymesh",
    scalars: str = "scalars.dat",
    coordinate_scaling: float = 0.001,
) -> str:
    """The ``.sif`` deck for a transient conduction solve on a bridged 3-D body mesh.

    Cartesian 3D with ``Coordinate Scaling`` (FreeCAD meshes in mm → 0.001), uniform
    ``t_initial_c`` initial field, material (``k``, ``rho``, ``cp``), and a single
    convective boundary (``h_conv``, ``t_ambient_c``) over ``convection_tags`` — the
    FreeCAD **face indices** (1-based), since the UNV face groups become exactly those
    boundary tags. Every untagged face is natural (adiabatic). BDF(2), ``n_steps``
    steps of ``dt``; a SaveScalars solver writes max then min of Temperature to
    ``scalars`` (parse with :func:`parse_minmax_scalars`). Returns the deck text."""
    if min(k, rho, cp, h_conv) <= 0:
        raise ValueError("k, rho, cp, h_conv must be > 0")
    if dt <= 0 or n_steps < 1:
        raise ValueError("dt must be > 0 and n_steps >= 1")
    tags = [int(t) for t in (convection_tags or [])]
    if not tags or any(t < 1 for t in tags):
        raise ValueError("convection_tags must be 1-based face indices (non-empty)")
    tag_txt = " ".join(str(t) for t in tags)
    return f"""Header
  Mesh DB "." "{mesh_name}"
End
Simulation
  Coordinate System = Cartesian 3D
  Coordinate Scaling = {coordinate_scaling:.10g}
  Simulation Type = Transient
  Timestepping Method = BDF
  BDF Order = 2
  Timestep Sizes = {dt:.10g}
  Timestep Intervals = {int(n_steps)}
  Steady State Max Iterations = 1
  Output Intervals = 0
End
Body 1
  Equation = 1
  Material = 1
  Initial Condition = 1
End
Initial Condition 1
  Temperature = {t_initial_c:.10g}
End
Material 1
  Density = {rho:.10g}
  Heat Conductivity = {k:.10g}
  Heat Capacity = {cp:.10g}
End
Solver 1
  Equation = Heat Equation
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Linear System Solver = Iterative
  Linear System Iterative Method = BiCGStab
  Linear System Preconditioning = ILU0
  Linear System Max Iterations = 500
  Linear System Convergence Tolerance = 1.0e-10
  Nonlinear System Max Iterations = 1
  Steady State Convergence Tolerance = 1.0e-8
End
Solver 2
  Equation = SaveScalars
  Procedure = "SaveData" "SaveScalars"
  Filename = "{scalars}"
  Variable 1 = Temperature
  Operator 1 = max
  Variable 2 = Temperature
  Operator 2 = min
End
Equation 1
  Active Solvers(2) = 1 2
End
Boundary Condition 1
  Target Boundaries({len(tags)}) = {tag_txt}
  Heat Transfer Coefficient = {h_conv:.10g}
  External Temperature = {t_ambient_c:.10g}
End
"""


def write_body_transient_case(
    case_dir: str,
    *,
    k: float,
    rho: float,
    cp: float,
    h_conv: float,
    duration_s: float,
    convection_tags: list,
    t_initial_c: float = 100.0,
    t_ambient_c: float = 25.0,
    n_steps: int = 120,
    unv_filename: str = "body.unv",
    mesh_name: str = "bodymesh",
    scalars: str = "scalars.dat",
    coordinate_scaling: float = 0.001,
) -> dict:
    """Write the bridged-body transient case under ``case_dir`` (sif + STARTINFO).

    The UNV itself is written by the caller (the worker's FemMesh export, main
    thread); the background job then runs ``elmergrid_argv`` (UNV → ``mesh_name``)
    followed by ElmerSolver — both pure subprocesses. Returns ``{case_dir, sif,
    mesh_name, unv, elmergrid_argv, dt, n_steps, scalars}``."""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    dt = float(duration_s) / int(n_steps)
    sif_text = body_transient_sif(
        k=k, rho=rho, cp=cp, h_conv=h_conv, t_initial_c=t_initial_c,
        t_ambient_c=t_ambient_c, dt=dt, n_steps=n_steps,
        convection_tags=convection_tags, mesh_name=mesh_name, scalars=scalars,
        coordinate_scaling=coordinate_scaling)
    os.makedirs(case_dir, exist_ok=True)
    with open(os.path.join(case_dir, "case.sif"), "w") as f:
        f.write(sif_text)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w") as f:
        f.write("case.sif\n")
    return {
        "case_dir": case_dir,
        "sif": "case.sif",
        "mesh_name": mesh_name,
        "unv": unv_filename,
        "elmergrid_argv": elmergrid_cmd(unv_filename, mesh_name),
        "dt": dt,
        "n_steps": int(n_steps),
        "scalars": scalars,
    }


def parse_minmax_scalars(case_dir: str, scalars: str = "scalars.dat") -> dict | None:
    """Read the SaveScalars history of a bridged solve: last-row max/min Temperature.

    Returns ``{t_max_c, t_min_c, n_steps_written}`` or None when the file is absent/
    unreadable (the solve failed). For a body cooling through its tagged faces the
    max is the interior (slab: the mid-plane) and the min the convective surface."""
    import glob
    path = os.path.join(case_dir, scalars)
    matches = sorted(glob.glob(path)) or sorted(glob.glob(path + "*"))
    data = next((m for m in [path, *matches]
                 if os.path.isfile(m) and not m.endswith(".names")), None)
    if data is None:
        return None
    rows = [r for r in open(data).read().splitlines() if r.strip()]
    if not rows:
        return None
    try:
        last = rows[-1].split()
        return {
            "t_max_c": float(last[_COL_MAX]),
            "t_min_c": float(last[_COL_MIN]),
            "n_steps_written": len(rows),
        }
    except (ValueError, IndexError):
        return None


# --- OpenFOAM path: multi-region STL -> snappyHexMesh -> simpleFoam ------------

def ascii_stl_regions(regions: dict) -> str:
    """One ASCII STL with a named ``solid`` per region — snappyHexMesh reads each as
    a surface region it can turn into its own patch.

    ``regions`` maps a region name to a list of triangles, each a 3-tuple of (x, y, z)
    points **in metres**, wound so the right-hand normal points OUT of the fluid
    volume (facet normals are recomputed from the vertices). Raises ValueError on an
    empty region set / region / degenerate name."""
    if not regions:
        raise ValueError("at least one STL region is required")
    out = []
    for name, tris in regions.items():
        if not name or " " in str(name):
            raise ValueError(f"bad STL region name {name!r}")
        if not tris:
            raise ValueError(f"STL region {name!r} has no triangles")
        out.append(f"solid {name}\n")
        for a, b, c in tris:
            ux, uy, uz = (b[i] - a[i] for i in range(3))
            vx, vy, vz = (c[i] - a[i] for i in range(3))
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            mag = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
            out.append(
                f"  facet normal {n[0] / mag:.9g} {n[1] / mag:.9g} {n[2] / mag:.9g}\n"
                "    outer loop\n"
                + "".join(f"      vertex {p[0]:.9g} {p[1]:.9g} {p[2]:.9g}\n"
                          for p in (a, b, c))
                + "    endloop\n  endfacet\n")
        out.append(f"endsolid {name}\n")
    return "".join(out)


def cylinder_stl_regions(diameter_m: float, length_m: float,
                         n_segments: int = 48) -> dict:
    """The closed validation cylinder (axis +z, base at the origin) as STL regions
    ``{walls, inlet, outlet}`` — ``inlet`` the z=0 disc, ``outlet`` the z=L disc,
    ``walls`` the lateral surface; all normals outward. The pure-Python twin of the
    worker's per-face tessellation, so the snappy pipeline is testable without
    FreeCAD. Raises ValueError on degenerate sizes."""
    if diameter_m <= 0 or length_m <= 0:
        raise ValueError("diameter_m and length_m must be > 0")
    if n_segments < 8:
        raise ValueError("n_segments must be >= 8")
    r = diameter_m / 2.0
    ring0 = [(r * math.cos(2 * math.pi * i / n_segments),
              r * math.sin(2 * math.pi * i / n_segments), 0.0)
             for i in range(n_segments)]
    ring1 = [(x, y, length_m) for (x, y, _) in ring0]
    c0, c1 = (0.0, 0.0, 0.0), (0.0, 0.0, length_m)
    walls, inlet, outlet = [], [], []
    for i in range(n_segments):
        a, b = ring0[i], ring0[(i + 1) % n_segments]
        a1, b1 = ring1[i], ring1[(i + 1) % n_segments]
        walls.append((a, b, b1))
        walls.append((a, b1, a1))
        inlet.append((c0, b, a))                       # outward = -z
        outlet.append((c1, a1, b1))                    # outward = +z
    return {"walls": walls, "inlet": inlet, "outlet": outlet}


def snappy_internal_case_files(
    *,
    bbox_min_m,
    bbox_max_m,
    inlet_velocity_m_s,
    nu_m2_s: float,
    stl_filename: str = "body.stl",
    location_in_mesh_m=None,
    base_cell_m: float | None = None,
    margin_frac: float = 0.25,
    wall_refine: int = 2,
    end_time: int = 3000,
) -> dict:
    """Every text file of a snappyHexMesh internal-flow case as ``{relpath: contents}``.

    The background ``blockMesh`` box is the solid's bbox grown by ``margin_frac`` per
    side (the closed STL must sit strictly inside it); ``snappyHexMesh`` keeps the
    cells around ``location_in_mesh_m`` (default the bbox centre — override for
    non-convex solids), castellating + snapping to the STL with one patch per region:
    ``walls`` (no-slip wall, refined to level ``wall_refine``), ``inlet`` / ``outlet``
    (patches). ``inlet_velocity_m_s`` is the (vx, vy, vz) fixed inlet velocity;
    ``base_cell_m`` the background cell size (default: bbox-min-dimension / 8).
    ``simpleFoam`` steady laminar, same schemes as the parametric pipe. The caller
    writes ``constant/triSurface/<stl_filename>`` (e.g. :func:`ascii_stl_regions`).

    Gate the parsed drop with ``openfoam.parse_pressure_drop`` — use
    ``dp_developed_pa`` (2·mean(p)); the inlet max includes the snapped-corner
    entrance loss. Raises ValueError on degenerate bounds/velocity/viscosity."""
    lo = tuple(float(v) for v in bbox_min_m)
    hi = tuple(float(v) for v in bbox_max_m)
    if len(lo) != 3 or len(hi) != 3 or any(h <= low for low, h in zip(lo, hi)):
        raise ValueError("bbox_min_m/bbox_max_m must be valid 3-D bounds")
    vel = tuple(float(v) for v in inlet_velocity_m_s)
    if len(vel) != 3 or not any(vel):
        raise ValueError("inlet_velocity_m_s must be a non-zero (vx, vy, vz)")
    if nu_m2_s <= 0:
        raise ValueError("nu_m2_s must be > 0")
    if not 0.0 < margin_frac < 2.0:
        raise ValueError("margin_frac must be in (0, 2)")
    dims = tuple(h - low for low, h in zip(lo, hi))
    cell = float(base_cell_m) if base_cell_m else min(dims) / 8.0
    if cell <= 0:
        raise ValueError("base_cell_m must be > 0")
    pad = tuple(d * margin_frac for d in dims)
    blo = tuple(low - q for low, q in zip(lo, pad))
    bhi = tuple(h + q for h, q in zip(hi, pad))
    ncells = tuple(max(4, int(math.ceil((h - low) / cell))) for low, h in zip(blo, bhi))
    loc = (tuple(float(v) for v in location_in_mesh_m) if location_in_mesh_m
           else tuple((low + h) / 2.0 for low, h in zip(lo, hi)))

    v = [(blo[0], blo[1], blo[2]), (bhi[0], blo[1], blo[2]),
         (bhi[0], bhi[1], blo[2]), (blo[0], bhi[1], blo[2]),
         (blo[0], blo[1], bhi[2]), (bhi[0], blo[1], bhi[2]),
         (bhi[0], bhi[1], bhi[2]), (blo[0], bhi[1], bhi[2])]
    vtxt = "\n".join(f"    ({p[0]:.9g} {p[1]:.9g} {p[2]:.9g})" for p in v)

    files = {}
    files["system/blockMeshDict"] = (
        _header("dictionary", "blockMeshDict", "system") + f"""
scale 1;
vertices
(
{vtxt}
);
blocks ( hex (0 1 2 3 4 5 6 7) ({ncells[0]} {ncells[1]} {ncells[2]}) simpleGrading (1 1 1) );
edges ();
boundary
(
    background
    {{
        type wall;
        faces ( (0 3 2 1) (4 5 6 7) (0 1 5 4) (2 3 7 6) (0 4 7 3) (1 2 6 5) );
    }}
);
mergePatchPairs ();
""")
    files["system/snappyHexMeshDict"] = (
        _header("dictionary", "snappyHexMeshDict", "system") + f"""
castellatedMesh true;
snap true;
addLayers false;
geometry
{{
    {stl_filename}
    {{
        type triSurfaceMesh;
        name body;
        regions
        {{
            walls  {{ name walls; }}
            inlet  {{ name inlet; }}
            outlet {{ name outlet; }}
        }}
    }}
}}
castellatedMeshControls
{{
    maxLocalCells 1000000;
    maxGlobalCells 4000000;
    minRefinementCells 0;
    nCellsBetweenLevels 2;
    features ();
    refinementSurfaces
    {{
        body
        {{
            level (1 1);
            regions
            {{
                walls  {{ level (1 {int(wall_refine)}); patchInfo {{ type wall; }} }}
                inlet  {{ level (1 1); patchInfo {{ type patch; }} }}
                outlet {{ level (1 1); patchInfo {{ type patch; }} }}
            }}
        }}
    }}
    resolveFeatureAngle 30;
    refinementRegions {{}}
    locationInMesh ({loc[0]:.9g} {loc[1]:.9g} {loc[2]:.9g});
    allowFreeStandingZoneFaces true;
}}
snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 50;
    nRelaxIter 5;
    nFeatureSnapIter 10;
    implicitFeatureSnap true;
    explicitFeatureSnap false;
    multiRegionFeatureSnap false;
}}
addLayersControls
{{
    relativeSizes true;
    layers {{}}
    expansionRatio 1.0;
    finalLayerThickness 0.3;
    minThickness 0.1;
    nGrow 0;
    featureAngle 60;
    slipFeatureAngle 30;
    nRelaxIter 3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals 3;
    nSmoothThickness 10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle 90;
    nBufferCellsNoExtrude 0;
    nLayerIter 50;
}}
meshQualityControls
{{
    maxNonOrtho 65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave 80;
    minVol 1e-13;
    minTetQuality 1e-15;
    minArea -1;
    minTwist 0.02;
    minDeterminant 0.001;
    minFaceWeight 0.02;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
}}
mergeTolerance 1e-6;
""")
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
        f"    inlet  {{ type fixedValue; value uniform ({vel[0]:.10g} {vel[1]:.10g} {vel[2]:.10g}); }}\n"
        "    outlet { type zeroGradient; }\n"
        "    walls  { type noSlip; }\n"
        "    background { type noSlip; }\n}\n")
    files["0/p"] = (
        _header("volScalarField", "p", "0")
        + "\ndimensions      [0 2 -2 0 0 0 0];\n"
        "internalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    inlet  { type zeroGradient; }\n"
        "    outlet { type fixedValue; value uniform 0; }\n"
        "    walls  { type zeroGradient; }\n"
        "    background { type zeroGradient; }\n}\n")
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


def write_snappy_internal_case(case_dir: str, *, stl_text: str,
                               stl_filename: str = "body.stl", **kwargs) -> dict:
    """Write a complete, runnable snappy internal-flow case under ``case_dir``:
    the dicts from :func:`snappy_internal_case_files` (forwarding ``**kwargs``) plus
    ``constant/triSurface/<stl_filename>`` = ``stl_text``. Run it as
    :func:`snappy_mesh_cmds` then ``simpleFoam`` (with the OpenFOAM env sourced).
    Returns ``{case_dir, stl, cmds}``."""
    files = snappy_internal_case_files(stl_filename=stl_filename, **kwargs)
    files[os.path.join("constant", "triSurface", stl_filename)] = stl_text
    for rel, text in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    return {"case_dir": case_dir, "stl": stl_filename, "cmds": snappy_mesh_cmds()}


def snappy_mesh_cmds() -> list:
    """The app sequence that meshes and solves a written snappy internal case:
    blockMesh → snappyHexMesh -overwrite → simpleFoam (each an argv list for the
    worker's ``_run_foam``)."""
    return [["blockMesh"], ["snappyHexMesh", "-overwrite"], ["simpleFoam"]]

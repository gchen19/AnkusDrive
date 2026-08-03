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

* **OpenFOAM, external (the virtual wind tunnel, issue #223):** the same STL, but the
  fluid is meshed OUTSIDE the solid — an auto-sized farfield box, the body carved out
  by snappyHexMesh, and the ``forces`` function object integrating pressure + viscous
  traction over it into drag/lift/moment. This is the half that lets an agent design to
  an aero spec: hand it a shape, get Cd back.

Verification is **relative** (an arbitrary mesh has no closed form) except in the
tunnel, where the sphere has a published drag curve: a FreeCAD *box* solved as a plane
wall must match ``thermal_transient_1d`` (measured 0.2–0.9 % at Bi=0.5, Fo≈0.5 on the
default Gmsh mesh), a FreeCAD *cylinder* run through snappyHexMesh must match
Hagen–Poiseuille / the parametric wedge-pipe builder (measured ``dp_developed`` within
0.6 % at Re=50), and a *sphere* in the tunnel must land on Clift–Gauvin across two
Reynolds decades (measured 0.8 % at Re=1, 2.0 % at Re=100; gate 10 %, banded because
the drag curve is a correlation).

Pure-Python, FreeCAD-free (the FreeCAD-side meshing/tessellation lives in the worker;
everything here is case text + commands + parsers, testable on the no-FreeCAD lane).
SI units unless a name says otherwise. See ``tests/test_meshbridge.py``.
"""
from __future__ import annotations

import math
import os

from .openfoam import _header, steady_laminar_common_files

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
    with open(os.path.join(case_dir, "case.sif"), "w", encoding="utf-8") as f:
        f.write(sif_text)
    with open(os.path.join(case_dir, "ELMERSOLVER_STARTINFO"), "w", encoding="utf-8") as f:
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


def mesh_boundary_count(case_dir: str, mesh_name: str) -> int:
    """Number of boundary elements in the ElmerGrid-written mesh DB — the third field
    of ``<case_dir>/<mesh_name>/mesh.header`` line 1 (``n_nodes n_bulk n_boundary``).

    Used to catch the silent failure where ElmerGrid succeeds (rc=0) but the UNV face
    groups don't survive the conversion, leaving 0 boundary elements: the convective
    BC would then bind to nothing and the body stays adiabatic — physically wrong but
    rc=0. Returns 0 when the header is absent or unparseable (treated as a failure)."""
    path = os.path.join(case_dir, mesh_name, "mesh.header")
    try:
        with open(path, encoding="utf-8") as f:
            fields = f.readline().split()
        return int(fields[2]) if len(fields) >= 3 else 0
    except (OSError, ValueError, IndexError):
        return 0


def boundary_name_map(case_dir: str, mesh_name: str) -> dict:
    """The boundary numbers ElmerGrid actually assigned to the UNV face groups,
    parsed from ``<mesh_name>/mesh.names`` (``$ Face1 = 6`` lines under the
    "names for boundaries" section).

    ElmerGrid v26 RENUMBERS the groups during UNV import (Face4 → boundary 1,
    …), so a SIF that targets ``Face i`` as ``boundary i`` binds the convective
    BC to arbitrary faces — the corner-cooled-cube failure found live on the
    #205 Windows verification. Older ElmerGrids (the apt v9 the Linux lane uses)
    preserve group order and write no mesh.names — then this returns {} and the
    legacy tag-order contract stands. Keys are group names, values boundary
    numbers."""
    import re
    path = os.path.join(case_dir, mesh_name, "mesh.names")
    if not os.path.isfile(path):
        return {}
    out = {}
    in_boundaries = False
    for line in open(path, encoding="utf-8", errors="replace"):
        if "names for boundaries" in line:
            in_boundaries = True
            continue
        if line.startswith("!"):
            in_boundaries = False if "names for" in line else in_boundaries
            continue
        m = re.match(r"\$\s*(\w+)\s*=\s*(\d+)", line)
        if m and in_boundaries:
            out[m.group(1)] = int(m.group(2))
    return out


def retarget_convection_boundaries(case_dir: str, sif: str, tags: list) -> None:
    """Rewrite the (single) convection BC's ``Target Boundaries`` list in the
    already-written SIF with the boundary numbers ElmerGrid actually assigned
    (see ``boundary_name_map``)."""
    import re
    path = os.path.join(case_dir, sif)
    text = open(path, encoding="utf-8").read()
    tag_txt = " ".join(str(t) for t in sorted(tags))
    new = f"Target Boundaries({len(tags)}) = {tag_txt}"
    text, n = re.subn(r"Target Boundaries\(\d+\)\s*=\s*[\d ]+", new, text)
    if n != 1:
        raise RuntimeError(f"expected exactly one Target Boundaries line, found {n}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


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
    rows = [r for r in open(data, encoding="utf-8").read().splitlines() if r.strip()]
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
    # The default seed is the solid's bbox centre — but nudged off the background grid
    # by an irrational-ish fraction of a cell. A centred solid on a symmetric box puts
    # the exact centre on a cell VERTEX, and snappyHexMesh v2512 rejects that outright
    # ("Point (…) is not inside the mesh or on a face or edge") — a fatal error for
    # every symmetric body, which is most of them. The nudge is min_dim/58, far inside
    # any solid this bridge can mesh. A caller-supplied point is used verbatim.
    loc = (tuple(float(v) for v in location_in_mesh_m) if location_in_mesh_m
           else tuple((low + h) / 2.0 + cell * f
                      for low, h, f in zip(lo, hi, (0.137, 0.113, 0.101))))

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
    # transport/turbulence/control + the central-scheme fvSchemes/fvSolution are
    # shared verbatim with the parametric wedge pipe
    files.update(steady_laminar_common_files(nu_m2_s=nu_m2_s, end_time=end_time))
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
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return {"case_dir": case_dir, "stl": stl_filename, "cmds": snappy_mesh_cmds()}


def snappy_mesh_cmds() -> list:
    """The app sequence that meshes and solves a written snappy internal case:
    blockMesh → snappyHexMesh -overwrite → simpleFoam (each an argv list for the
    worker's ``_run_foam``). The external case (:func:`snappy_external_case_files`)
    runs the same three apps."""
    return [["blockMesh"], ["snappyHexMesh", "-overwrite"], ["simpleFoam"]]


# --- OpenFOAM path: the virtual wind tunnel (external flow, issue #223) --------
#
# The external twin of snappy_internal_case_files: instead of meshing the fluid
# INSIDE a closed solid, mesh the fluid OUTSIDE it, in an auto-sized farfield box.
# Three things differ and each one is load-bearing:
#
#   * locationInMesh sits in a domain CORNER (outside the body), so snappyHexMesh
#     keeps the exterior and carves the body out — the internal case seeds the
#     interior instead.
#   * one patch, `farfield`, carries the whole outer box with the `freestream` /
#     `freestreamPressure` pair. That pair is direction-agnostic (it switches
#     itself between inlet and outlet per face from the local flux), so an
#     arbitrary flow vector needs no inlet/outlet split of the box.
#   * the body patch is `walls` (no-slip) and the `forces` function object
#     integrates pressure + viscous traction over it — the drag/lift/moment the
#     whole thing exists to produce (see openfoam.forces_function_object).
#
# SIZING IS A CORRECTNESS ISSUE, NOT A PERFORMANCE ONE. snappyHexMesh finds a
# surface by testing background-cell EDGES against it: when the background cell is
# bigger than the body, no edge crosses it, refinement marks 0 cells, and the run
# completes rc=0 having meshed an EMPTY tunnel — reporting ~1e-14 N of drag as a
# converged answer. Verified live at cell = L (fails) and L/2 (works), L being the
# body's LARGEST bbox dimension. external_domain_box therefore defaults the cell to
# L/2 and REJECTS anything coarser, rather than trusting the caller.


def projected_area(triangles, direction) -> float:
    """Frontal (projected) area of a closed triangulated surface along ``direction``.

    ½·Σ|n̂·d̂|·A over every triangle — exact for a CONVEX body, since each projection
    ray crosses the surface exactly twice (front and back), so the summed |cos| areas
    double-count the silhouette exactly once. A non-convex body (a duct, a body with a
    re-entrant pocket) counts every crossing, so the result OVERSTATES the silhouette;
    pass a measured frontal area instead when that matters. ``direction`` need not be
    normalized, and the result carries the square of whatever unit the triangles are in
    (m² on the case-building path, mm² when the worker measures a FreeCAD silhouette).

    Raises ValueError on an empty triangle list or a zero direction."""
    if not triangles:
        raise ValueError("triangles must be non-empty")
    d = tuple(float(v) for v in direction)
    dmag = math.sqrt(sum(v * v for v in d))
    if len(d) != 3 or dmag == 0:
        raise ValueError("direction must be a non-zero 3-vector")
    d = tuple(v / dmag for v in d)
    total = 0.0
    for a, b, c in triangles:
        ux, uy, uz = (b[i] - a[i] for i in range(3))
        vx, vy, vz = (c[i] - a[i] for i in range(3))
        n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        total += abs(n[0] * d[0] + n[1] * d[1] + n[2] * d[2])   # |n|=2A, so this is 2A|n̂·d̂|
    return total / 4.0                                          # ½·Σ A·|n̂·d̂|


def external_domain_box(
    bbox_min_m,
    bbox_max_m,
    *,
    flow_direction=(1.0, 0.0, 0.0),
    upstream_factor: float = 5.0,
    downstream_factor: float = 10.0,
    lateral_factor: float = 5.0,
    frontal_area_m2: float | None = None,
    base_cell_m: float | None = None,
) -> dict:
    """Auto-size the farfield box around a body's bounding box (standard external-CFD
    practice) and report whether it is big enough to trust.

    The characteristic length L is the body's largest bbox dimension. Each side is
    padded by L times a factor resolved against the unit flow direction d̂, so the
    padding follows the flow for any direction, axis-aligned or not::

        pad_lo[i] = L·(upstream·max(0, d̂ᵢ) + downstream·max(0, −d̂ᵢ) + lateral·(1−|d̂ᵢ|))
        pad_hi[i] = L·(downstream·max(0, d̂ᵢ) + upstream·max(0, −d̂ᵢ) + lateral·(1−|d̂ᵢ|))

    For the default d̂ = +x this is the textbook 5L upstream / 10L downstream / 5L all
    round. ``blockage_ratio`` is the body's frontal area over the domain's cross-section
    (domain volume / streamwise extent — exact for axis-aligned flow); above 5 % the
    walls squeeze the flow and inflate drag, so it lands in ``warnings``.

    ``base_cell_m`` defaults to L/2 and a caller value coarser than L/2 is REJECTED:
    snappyHexMesh detects a surface by background-cell EDGE intersections, so once the
    cell reaches the body's own size no edge crosses it, refinement marks zero cells,
    and the run completes rc=0 having meshed an EMPTY tunnel — reporting ~1e-14 N of
    drag as a converged answer. Verified live at cell = L (fails) and L/2 (works).

    Returns {lo, hi, n_cells (background counts), base_cell_m, char_length_m,
    streamwise_extent_m, cross_section_m2, blockage_ratio, frontal_area_m2, warnings}.
    Raises ValueError on degenerate bounds/direction or a too-coarse ``base_cell_m``."""
    lo_b = tuple(float(v) for v in bbox_min_m)
    hi_b = tuple(float(v) for v in bbox_max_m)
    if len(lo_b) != 3 or len(hi_b) != 3 or any(h <= low for low, h in zip(lo_b, hi_b)):
        raise ValueError("bbox_min_m/bbox_max_m must be valid 3-D bounds")
    d = tuple(float(v) for v in flow_direction)
    dmag = math.sqrt(sum(v * v for v in d))
    if len(d) != 3 or dmag == 0:
        raise ValueError("flow_direction must be a non-zero 3-vector")
    d = tuple(v / dmag for v in d)
    if min(upstream_factor, downstream_factor, lateral_factor) <= 0:
        raise ValueError("domain factors must be > 0")

    dims = tuple(h - low for low, h in zip(lo_b, hi_b))
    char = max(dims)
    smallest = min(dims)
    cell = float(base_cell_m) if base_cell_m else char / 2.0
    if cell <= 0:
        raise ValueError("base_cell_m must be > 0")
    if cell > char / 2.0:
        raise ValueError(
            f"base_cell_m {cell:.6g} m is coarser than half the body's largest "
            f"dimension ({char:.6g} m): snappyHexMesh would find no background cell "
            "edge crossing the surface, mesh an EMPTY tunnel and report ~0 drag with "
            f"returncode 0 — use <= {char / 2.0:.6g} m")

    pad_lo = tuple(char * (upstream_factor * max(0.0, d[i])
                           + downstream_factor * max(0.0, -d[i])
                           + lateral_factor * (1.0 - abs(d[i]))) for i in range(3))
    pad_hi = tuple(char * (downstream_factor * max(0.0, d[i])
                           + upstream_factor * max(0.0, -d[i])
                           + lateral_factor * (1.0 - abs(d[i]))) for i in range(3))
    lo = tuple(low - p for low, p in zip(lo_b, pad_lo))
    hi = tuple(h + p for h, p in zip(hi_b, pad_hi))
    ext = tuple(h - low for low, h in zip(lo, hi))
    n_cells = tuple(max(4, int(round(e / cell))) for e in ext)

    streamwise = sum(abs(d[i]) * ext[i] for i in range(3))
    volume = ext[0] * ext[1] * ext[2]
    cross = volume / streamwise if streamwise > 0 else float("inf")
    # Frontal area: the caller's measured silhouette (projected_area over the STL)
    # when given, else the bbox silhouette along d̂ — an upper bound, which makes the
    # reported blockage conservative rather than flattering.
    frontal = float(frontal_area_m2) if frontal_area_m2 else sum(
        abs(d[i]) * dims[(i + 1) % 3] * dims[(i + 2) % 3] for i in range(3))
    blockage = frontal / cross if cross > 0 else float("inf")

    warnings: list[str] = []
    if blockage > 0.05:
        warnings.append(
            f"blockage ratio {blockage * 100:.1f}% exceeds the 5% practice limit — "
            "the farfield squeezes the flow and inflates drag; raise lateral_factor")
    return {
        "lo": lo,
        "hi": hi,
        "n_cells": n_cells,
        "base_cell_m": cell,
        "char_length_m": char,
        "smallest_dim_m": smallest,
        "streamwise_extent_m": streamwise,
        "cross_section_m2": cross,
        "blockage_ratio": blockage,
        "frontal_area_m2": frontal,
        "warnings": warnings,
    }


def sphere_stl_triangles(diameter_m: float, n_lat: int = 24, n_lon: int = 48,
                         centre=(0.0, 0.0, 0.0)) -> list:
    """A closed UV-sphere triangulation (outward normals) — the pure-Python twin of
    the worker's FreeCAD face tessellation, so the wind tunnel is testable, and gateable
    against the sphere drag curve, with no CAD kernel. Poles are single fans (no
    degenerate triangles). Raises ValueError on degenerate size/resolution."""
    if diameter_m <= 0:
        raise ValueError("diameter_m must be > 0")
    if n_lat < 4 or n_lon < 6:
        raise ValueError("n_lat must be >= 4 and n_lon >= 6")
    r = diameter_m / 2.0
    cx, cy, cz = (float(v) for v in centre)

    def P(i, j):
        th = math.pi * i / n_lat
        ph = 2.0 * math.pi * j / n_lon
        return (cx + r * math.sin(th) * math.cos(ph),
                cy + r * math.sin(th) * math.sin(ph),
                cz + r * math.cos(th))

    tris = []
    for i in range(n_lat):
        for j in range(n_lon):
            a, b, c, e = P(i, j), P(i, j + 1), P(i + 1, j + 1), P(i + 1, j)
            if i == 0:                       # north pole fan
                tris.append((a, c, e))
            elif i == n_lat - 1:             # south pole fan
                tris.append((a, b, c))
            else:
                tris.append((a, b, c))
                tris.append((a, c, e))
    return tris


def snappy_external_case_files(
    *,
    bbox_min_m,
    bbox_max_m,
    freestream_velocity_m_s,
    nu_m2_s: float,
    rho_kg_m3: float,
    stl_filename: str = "body.stl",
    base_cell_m: float | None = None,
    upstream_factor: float = 5.0,
    downstream_factor: float = 10.0,
    lateral_factor: float = 5.0,
    frontal_area_m2: float | None = None,
    surface_refine=(2, 3),
    wake_refine: int = 1,
    end_time: int = 1500,
    turbulence: str = "laminar",
    intensity: float = 0.05,
    centre_of_rotation=None,
) -> dict:
    """Every text file of the external-flow (wind tunnel) snappyHexMesh case, plus the
    sizing metadata, as ``{"files": {relpath: contents}, "domain": {...}}``.

    The body is carved OUT of an auto-sized farfield box (:func:`external_domain_box`
    — see it for the sizing rules and the empty-tunnel guard). ``freestream_velocity_m_s``
    is the (vx, vy, vz) freestream vector; its direction sets the domain padding, the
    single ``farfield`` patch carries ``freestream``/``freestreamPressure`` (which need
    no inlet/outlet split), and the carved body becomes the no-slip ``walls`` patch.
    A refinement box hugging the body and its wake runs at ``wake_refine``; the surface
    itself at ``surface_refine`` = (min, max) levels. ``rho_kg_m3`` reaches the case only
    through the ``forces`` function object, which reports forces in NEWTONS (OpenFOAM's
    incompressible p is kinematic, so the FO needs ρ to dimensionalize). Moments are
    taken about ``centre_of_rotation``, defaulting to the body's BBOX CENTRE rather than
    the global origin — a solid modelled far from the origin would otherwise report a
    moment dominated by drag × lever arm, which says nothing about the body.

    ``turbulence='kOmegaSST'`` overlays the RANS fields (the same
    ``openfoam._rans_overlay`` the pipe/plate cases use). **That path is UNGATED**: the
    laminar body case is verified live against the sphere drag curve at Re = 1 and
    Re = 100, the RANS one has no verified oracle yet — treat its numbers as indicative
    and say so downstream.

    The caller writes ``constant/triSurface/<stl_filename>`` (one ``walls`` region, e.g.
    :func:`ascii_stl_regions`). Run with :func:`snappy_mesh_cmds`; read the result with
    ``openfoam.parse_forces``."""
    from .openfoam import (_rans_inlet_k_omega, _rans_overlay,
                           steady_laminar_common_files)

    vel = tuple(float(v) for v in freestream_velocity_m_s)
    if len(vel) != 3 or not any(vel):
        raise ValueError("freestream_velocity_m_s must be a non-zero (vx, vy, vz)")
    if nu_m2_s <= 0 or rho_kg_m3 <= 0:
        raise ValueError("nu_m2_s and rho_kg_m3 must be > 0")
    lvl = tuple(int(v) for v in surface_refine)
    if len(lvl) != 2 or lvl[0] < 0 or lvl[1] < lvl[0]:
        raise ValueError("surface_refine must be (min_level, max_level), min <= max")

    dom = external_domain_box(
        bbox_min_m, bbox_max_m, flow_direction=vel, base_cell_m=base_cell_m,
        upstream_factor=upstream_factor, downstream_factor=downstream_factor,
        lateral_factor=lateral_factor, frontal_area_m2=frontal_area_m2)
    lo, hi, n = dom["lo"], dom["hi"], dom["n_cells"]
    cell = dom["base_cell_m"]
    char = dom["char_length_m"]
    finest = cell / (2 ** lvl[1])
    if dom["smallest_dim_m"] < 2.0 * finest:
        dom["warnings"].append(
            f"the body's thinnest dimension ({dom['smallest_dim_m']:.4g} m) is under "
            f"2 cells wide even at the finest surface level ({finest:.4g} m) — raise "
            "surface_refine or lower base_cell_m, or the thin feature is meshed away")
    dom["finest_cell_m"] = finest
    umag = math.sqrt(sum(v * v for v in vel))
    dhat = tuple(v / umag for v in vel)

    v8 = [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]),
          (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
          (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]),
          (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2])]
    vtxt = "\n".join(f"    ({p[0]:.9g} {p[1]:.9g} {p[2]:.9g})" for p in v8)

    # refinement box: 1.5 L around the body, stretched 4 L downstream along the flow
    bl = tuple(float(v) for v in bbox_min_m)
    bh = tuple(float(v) for v in bbox_max_m)
    rlo = tuple(bl[i] - char * (1.5 + 2.5 * max(0.0, -dhat[i])) for i in range(3))
    rhi = tuple(bh[i] + char * (1.5 + 2.5 * max(0.0, dhat[i])) for i in range(3))

    files = {}
    files["system/blockMeshDict"] = (
        _header("dictionary", "blockMeshDict", "system") + f"""
scale 1;
vertices
(
{vtxt}
);
blocks ( hex (0 1 2 3 4 5 6 7) ({n[0]} {n[1]} {n[2]}) simpleGrading (1 1 1) );
edges ();
boundary
(
    farfield
    {{
        type patch;
        faces ( (0 3 2 1) (4 5 6 7) (0 1 5 4) (2 3 7 6) (0 4 7 3) (1 2 6 5) );
    }}
);
mergePatchPairs ();
""")
    # locationInMesh: a domain corner, deliberately off the cell grid by an
    # irrational-ish fraction so it can never land on a face/edge — and always
    # OUTSIDE the body, which is what makes this the external case.
    seed = tuple(lo[i] + cell * (0.37, 0.41, 0.43)[i] for i in range(3))
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
        regions {{ walls {{ name walls; }} }}
    }}
    wake
    {{
        type searchableBox;
        min ({rlo[0]:.9g} {rlo[1]:.9g} {rlo[2]:.9g});
        max ({rhi[0]:.9g} {rhi[1]:.9g} {rhi[2]:.9g});
    }}
}}
castellatedMeshControls
{{
    maxLocalCells 2000000;
    maxGlobalCells 8000000;
    minRefinementCells 0;
    nCellsBetweenLevels 3;
    features ();
    refinementSurfaces
    {{
        body
        {{
            level ({lvl[0]} {lvl[1]});
            regions {{ walls {{ level ({lvl[0]} {lvl[1]}); patchInfo {{ type wall; }} }} }}
        }}
    }}
    resolveFeatureAngle 30;
    refinementRegions {{ wake {{ mode inside; levels ((1e15 {int(wake_refine)})); }} }}
    locationInMesh ({seed[0]:.9g} {seed[1]:.9g} {seed[2]:.9g});
    allowFreeStandingZoneFaces true;
}}
snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 100;
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
    expansionRatio 1.2;
    finalLayerThickness 0.4;
    minThickness 0.1;
    nGrow 0;
    featureAngle 120;
    slipFeatureAngle 30;
    nRelaxIter 5;
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
    minVol 1e-16;
    minTetQuality 1e-18;
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
    files.update(steady_laminar_common_files(nu_m2_s=nu_m2_s, end_time=end_time))
    files["0/U"] = (
        _header("volVectorField", "U", "0")
        + "\ndimensions      [0 1 -1 0 0 0 0];\n"
        f"internalField   uniform ({vel[0]:.10g} {vel[1]:.10g} {vel[2]:.10g});\n"
        "boundaryField\n{\n"
        f"    farfield {{ type freestream; freestreamValue uniform "
        f"({vel[0]:.10g} {vel[1]:.10g} {vel[2]:.10g}); }}\n"
        "    walls    { type noSlip; }\n}\n")
    files["0/p"] = (
        _header("volScalarField", "p", "0")
        + "\ndimensions      [0 2 -2 0 0 0 0];\n"
        "internalField   uniform 0;\n"
        "boundaryField\n{\n"
        "    farfield { type freestreamPressure; freestreamValue uniform 0; }\n"
        "    walls    { type zeroGradient; }\n}\n")
    if str(turbulence).lower() in ("komegasst", "k-omega-sst", "rans", "turbulent"):
        k_in, omega_in = _rans_inlet_k_omega(umag, 0.07 * char, intensity)
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(
            "div(phi,U) bounded Gauss linear;",
            "div(phi,U) bounded Gauss linearUpwind grad(U);")
        files = _rans_overlay(
            files, k_in=k_in, omega_in=omega_in,
            k_bcs={"farfield": f"type inletOutlet; inletValue uniform {k_in:.10g}; "
                               f"value uniform {k_in:.10g};",
                   "walls": f"type kqRWallFunction; value uniform {k_in:.10g};"},
            omega_bcs={"farfield": f"type inletOutlet; inletValue uniform "
                                   f"{omega_in:.10g}; value uniform {omega_in:.10g};",
                       "walls": f"type omegaWallFunction; value uniform {omega_in:.10g};"},
            nut_bcs={"farfield": "type calculated; value uniform 0;",
                     "walls": "type nutkWallFunction; value uniform 0;"})
    from .openfoam import forces_function_object
    cofr = (tuple(float(v) for v in centre_of_rotation) if centre_of_rotation
            else tuple((bl[i] + bh[i]) / 2.0 for i in range(3)))
    files["system/controlDict"] += forces_function_object(
        patches=("walls",), rho_kg_m3=rho_kg_m3, centre_of_rotation=cofr)
    return {"files": files, "domain": dom, "velocity_magnitude_m_s": umag,
            "flow_direction": dhat, "centre_of_rotation_m": cofr}


def write_snappy_external_case(case_dir: str, *, stl_text: str,
                               stl_filename: str = "body.stl", **kwargs) -> dict:
    """Write a complete, runnable external-flow (wind tunnel) case under ``case_dir``:
    the files from :func:`snappy_external_case_files` (forwarding ``**kwargs``) plus
    ``constant/triSurface/<stl_filename>`` = ``stl_text``. Run it with
    :func:`snappy_mesh_cmds`. Returns ``{case_dir, stl, cmds, domain,
    velocity_magnitude_m_s, flow_direction, centre_of_rotation_m}``."""
    built = snappy_external_case_files(stl_filename=stl_filename, **kwargs)
    files = dict(built["files"])
    files[os.path.join("constant", "triSurface", stl_filename)] = stl_text
    for rel, text in files.items():
        path = os.path.join(case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return {"case_dir": case_dir, "stl": stl_filename, "cmds": snappy_mesh_cmds(),
            "domain": built["domain"],
            "velocity_magnitude_m_s": built["velocity_magnitude_m_s"],
            "flow_direction": built["flow_direction"],
            "centre_of_rotation_m": built["centre_of_rotation_m"]}

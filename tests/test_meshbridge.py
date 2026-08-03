"""Geometry-bridge toys (P3 M4) — case generation + the solver-backed relative gates.

Two tiers, both runnable on the no-FreeCAD lane:
  * **structure** (always): the bridged-body .sif binds the convective BC to the
    given FreeCAD face tags with mm→m Coordinate Scaling; the snappy case's
    background box strictly contains the solid; the multi-region STL is closed and
    its signed volume is the cylinder's; command builders and parsers behave. Pure
    string/geometry checks, no solver.
  * **solver-backed** (when the binaries resolve, else SKIP):
      - Elmer path: the committed Gmsh box UNV (tests/fixtures/box20_coarse.unv —
        a 20 mm cube meshed by FreeCAD's GmshTools, face groups intact) through
        ElmerGrid + ElmerSolver as a plane wall (convection on faces 1+2 = x=0 and
        x=20 mm) must match `thermal_transient_1d` (Heisler) — the kickoff's
        relative gate "mesh-from-FreeCAD == parametric/analytic".
      - OpenFOAM path: the pure-Python closed cylinder STL through blockMesh +
        snappyHexMesh + simpleFoam must reproduce Hagen–Poiseuille within 10 %
        on the developed-profile estimator (measured 0.6 % at Re=50).

Run:  python3 tests/test_meshbridge.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import cfd  # noqa: E402
from driftpin.analysis import meshbridge as mb  # noqa: E402
from driftpin.analysis import openfoam  # noqa: E402
from driftpin.analysis import thermal  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402

_FIXTURE_UNV = Path(__file__).resolve().parent / "fixtures" / "box20_coarse.unv"


# --- structure: Elmer half ------------------------------------------------------

def test_body_sif_binds_face_tags_and_scales_mm():
    sif = mb.body_transient_sif(
        k=200.0, rho=2700.0, cp=900.0, h_conv=10000.0,
        t_initial_c=100.0, t_ambient_c=25.0, dt=0.005, n_steps=120,
        convection_tags=[1, 2])
    for token in ("Coordinate System = Cartesian 3D",
                  "Coordinate Scaling = 0.001",
                  "Target Boundaries(2) = 1 2",
                  "Heat Transfer Coefficient = 10000",
                  "External Temperature = 25",
                  "Heat Conductivity = 200",
                  "Operator 1 = max", "Operator 2 = min",
                  "Timestep Intervals = 120"):
        assert token in sif, f"missing {token!r}"


def test_body_case_writes_sif_and_elmergrid_argv():
    with tempfile.TemporaryDirectory() as d:
        built = mb.write_body_transient_case(
            d, k=200, rho=2700, cp=900, h_conv=10000, duration_s=0.6,
            convection_tags=[1, 2])
        assert os.path.isfile(os.path.join(d, "case.sif"))
        assert open(os.path.join(d, "ELMERSOLVER_STARTINFO"), encoding="utf-8").read().strip() == "case.sif"
        assert built["elmergrid_argv"] == [
            "ElmerGrid", "8", "2", "body.unv", "-out", "bodymesh", "-autoclean"]
        assert abs(built["dt"] - 0.005) < 1e-12 and built["n_steps"] == 120


def test_parse_minmax_scalars_reads_last_row():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "scalars.dat"), "w", encoding="utf-8").write(
            "9.9e1 9.0e1\n9.058932393949E+001 7.653637074115E+001\n")
        r = mb.parse_minmax_scalars(d)
        assert abs(r["t_max_c"] - 90.58932393949) < 1e-9
        assert abs(r["t_min_c"] - 76.53637074115) < 1e-9
        assert r["n_steps_written"] == 2
        assert mb.parse_minmax_scalars(d, "missing.dat") is None


def test_mesh_boundary_count_guards_zero():
    with tempfile.TemporaryDirectory() as d:
        mdir = os.path.join(d, "case")
        os.makedirs(mdir)
        # mesh.header line 1 = "n_nodes n_bulk n_boundary"
        open(os.path.join(mdir, "mesh.header"), "w", encoding="utf-8").write("729 384 104\n3\n")
        assert mb.mesh_boundary_count(d, "case") == 104        # healthy mesh
        open(os.path.join(mdir, "mesh.header"), "w", encoding="utf-8").write("729 384 0\n3\n")
        assert mb.mesh_boundary_count(d, "case") == 0          # the silent-zero failure
        assert mb.mesh_boundary_count(d, "absent") == 0        # missing header -> 0


def test_sif_validation():
    ok = dict(k=1, rho=1, cp=1, h_conv=1, t_initial_c=1, t_ambient_c=0,
              dt=0.1, n_steps=2, convection_tags=[1])
    for bad in (dict(ok, k=0), dict(ok, dt=0), dict(ok, n_steps=0),
                dict(ok, convection_tags=[]), dict(ok, convection_tags=[0])):
        try:
            mb.body_transient_sif(**bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


# --- structure: OpenFOAM half ----------------------------------------------------

def test_cylinder_stl_is_closed_and_has_the_right_volume():
    import math
    D, L = 0.01, 0.1
    regions = mb.cylinder_stl_regions(D, L, n_segments=64)
    assert set(regions) == {"walls", "inlet", "outlet"}
    tris = [t for tris in regions.values() for t in tris]
    # closed 2-manifold: every directed edge appears exactly once (its reverse on
    # the neighbouring facet)
    edges = {}
    for a, b, c in tris:
        for p, q in ((a, b), (b, c), (c, a)):
            edges[(p, q)] = edges.get((p, q), 0) + 1
    assert all(n == 1 for n in edges.values()), "directed edge repeated"
    assert all((q, p) in edges for (p, q) in edges), "open (boundary) edge"
    # divergence theorem: signed volume == cylinder volume (outward normals)
    vol = 0.0
    for a, b, c in tris:
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1])
                - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    exact = math.pi * (D / 2) ** 2 * L
    # the inscribed polygon underestimates the circle by sin(x)/x, ~0.16% at 64 seg
    assert 0.99 * exact < vol < 1.001 * exact, (vol, exact)
    stl_lines = mb.ascii_stl_regions(regions).splitlines()
    assert stl_lines.count("solid walls") == 1, "one named solid per region"
    assert stl_lines.count("endsolid outlet") == 1
    assert sum(1 for ln in stl_lines if ln.startswith("  facet normal")) == len(tris)


def test_snappy_case_box_contains_solid_and_names_regions():
    files = mb.snappy_internal_case_files(
        bbox_min_m=(-0.005, -0.005, 0.0), bbox_max_m=(0.005, 0.005, 0.1),
        inlet_velocity_m_s=(0, 0, 0.005), nu_m2_s=1e-6)
    assert set(files) >= {"system/blockMeshDict", "system/snappyHexMeshDict",
                          "0/U", "0/p", "system/controlDict", "system/fvSchemes",
                          "system/fvSolution", "constant/transportProperties",
                          "constant/turbulenceProperties"}
    bm = files["system/blockMeshDict"]
    # the background box is grown by the margin on every side
    assert "(-0.0075 -0.0075 -0.025)" in bm, bm
    assert "(0.0075 0.0075 0.125)" in bm, bm
    sn = files["system/snappyHexMeshDict"]
    for token in ("name walls;", "name inlet;", "name outlet;",
                  "patchInfo { type wall; }", "patchInfo { type patch; }",
                  "implicitFeatureSnap true"):
        assert token in sn, f"missing {token!r}"
    # the seed is the solid's centre NUDGED off the background grid: the exact centre
    # of a symmetric solid lands on a cell vertex, which snappyHexMesh v2512 rejects
    # outright ("Point (...) is not inside the mesh or on a face or edge")
    seed = [float(v) for v in sn.split("locationInMesh (")[1].split(")")[0].split()]
    cell = 0.01 / 8.0                                   # min bbox dim / 8
    assert all(0 < abs(s - c) < cell for s, c in zip(seed, (0.0, 0.0, 0.05))), seed
    assert "fixedValue; value uniform (0 0 0.005)" in files["0/U"]
    assert mb.snappy_mesh_cmds() == [["blockMesh"], ["snappyHexMesh", "-overwrite"],
                                     ["simpleFoam"]]


def test_snappy_case_validation():
    ok = dict(bbox_min_m=(0, 0, 0), bbox_max_m=(1, 1, 1),
              inlet_velocity_m_s=(1, 0, 0), nu_m2_s=1e-6)
    for bad in (dict(ok, bbox_max_m=(0, 1, 1)),
                dict(ok, inlet_velocity_m_s=(0, 0, 0)),
                dict(ok, nu_m2_s=0),
                dict(ok, margin_frac=0)):
        try:
            mb.snappy_internal_case_files(**bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")
    for bad_regions in ({}, {"walls": []}, {"bad name": [((0,) * 3,) * 3]}):
        try:
            mb.ascii_stl_regions(bad_regions)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad_regions}")


# --- structure: the external half (the virtual wind tunnel, issue #223) ----------

def test_projected_area_is_exact_for_convex_bodies():
    sph = mb.sphere_stl_triangles(0.01, n_lat=64, n_lon=128)
    exact = 3.141592653589793 * 0.005 ** 2
    got = mb.projected_area(sph, (1, 0, 0))
    assert abs(got / exact - 1.0) < 2e-3, (got, exact)   # faceting undershoots
    # direction-independence for a sphere, and no dependence on |direction|
    for d in ((0, 1, 0), (0, 0, 1), (1, 1, 1), (7, 0, 0)):
        assert abs(mb.projected_area(sph, d) / got - 1.0) < 5e-3, d
    # a box's silhouette is exactly the face it presents
    box = _box_triangles((0, 0, 0), (0.02, 0.01, 0.03))
    assert abs(mb.projected_area(box, (1, 0, 0)) - 0.01 * 0.03) < 1e-12
    assert abs(mb.projected_area(box, (0, 0, 1)) - 0.02 * 0.01) < 1e-12
    for bad in (([], (1, 0, 0)), (box, (0, 0, 0))):
        try:
            mb.projected_area(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError("projected_area should have raised")


def _box_triangles(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (2, 3, 7, 6), (0, 4, 7, 3), (1, 2, 6, 5)]
    out = []
    for a, b, c, d in quads:
        out.append((v[a], v[b], v[c]))
        out.append((v[a], v[c], v[d]))
    return out


def test_external_domain_follows_the_flow_direction():
    lo, hi = (-0.005,) * 3, (0.005,) * 3               # 10 mm cube of body
    x = mb.external_domain_box(lo, hi)                  # default +x
    assert abs(x["lo"][0] - (-0.055)) < 1e-12           # 5 L upstream
    assert abs(x["hi"][0] - 0.105) < 1e-12              # 10 L downstream
    assert abs(x["lo"][1] - (-0.055)) < 1e-12           # 5 L lateral
    back = mb.external_domain_box(lo, hi, flow_direction=(-1, 0, 0))
    assert abs(back["hi"][0] - 0.055) < 1e-12           # upstream is now +x
    assert abs(back["lo"][0] - (-0.105)) < 1e-12
    up = mb.external_domain_box(lo, hi, flow_direction=(0, 0, 3))
    assert abs(up["hi"][2] - 0.105) < 1e-12 and abs(up["lo"][2] - (-0.055)) < 1e-12
    # blockage: tight lateral padding trips the 5 % practice limit, roomy does not
    tight = mb.external_domain_box(lo, hi, lateral_factor=1.0)
    assert tight["blockage_ratio"] > 0.05 and tight["warnings"], tight
    assert not x["warnings"] and x["blockage_ratio"] < 0.05, x


def test_external_case_rejects_a_cell_that_would_mesh_an_empty_tunnel():
    """The load-bearing guard: snappyHexMesh finds a surface by background-cell EDGE
    intersection, so a cell as big as the body marks zero cells for refinement and the
    run completes rc=0 around an EMPTY tunnel, reporting ~1e-14 N as a converged drag.
    Verified live at cell = L. The builder must refuse, not produce that case."""
    lo, hi = (-0.005,) * 3, (0.005,) * 3               # L = 0.01 m
    assert mb.external_domain_box(lo, hi)["base_cell_m"] == 0.005      # default L/2
    assert mb.external_domain_box(lo, hi, base_cell_m=0.005)["n_cells"][0] > 4
    for bad_cell in (0.01, 0.02, -1.0):
        try:
            mb.external_domain_box(lo, hi, base_cell_m=bad_cell)
        except ValueError as e:
            if bad_cell > 0:
                assert "EMPTY tunnel" in str(e), e
        else:
            raise AssertionError(f"base_cell_m={bad_cell} should have raised")
    # a body thinner than the finest surface cell is warned about, not meshed away quietly
    thin = mb.snappy_external_case_files(
        bbox_min_m=(0, 0, 0), bbox_max_m=(0.1, 0.1, 0.0005),
        freestream_velocity_m_s=(10, 0, 0), nu_m2_s=1.5e-5, rho_kg_m3=1.2)
    assert any("thinnest dimension" in w for w in thin["domain"]["warnings"]), thin


def test_external_case_carves_the_body_out_and_measures_it():
    built = mb.snappy_external_case_files(
        bbox_min_m=(-0.005,) * 3, bbox_max_m=(0.005,) * 3,
        freestream_velocity_m_s=(12.0, 0.0, 0.0), nu_m2_s=1.5e-5, rho_kg_m3=1.204)
    files, dom = built["files"], built["domain"]
    for rel in ("system/blockMeshDict", "system/snappyHexMeshDict", "system/controlDict",
                "system/fvSchemes", "system/fvSolution", "0/U", "0/p",
                "constant/transportProperties", "constant/turbulenceProperties"):
        assert rel in files, rel
    snappy = files["system/snappyHexMeshDict"]
    # the seed point must sit OUTSIDE the body — that is what makes this external
    seed = [float(v) for v in
            snappy.split("locationInMesh (")[1].split(")")[0].split()]
    assert all(s < -0.005 for s in seed), seed
    assert all(abs(s - lo) < dom["base_cell_m"] for s, lo in zip(seed, dom["lo"]))
    assert "patchInfo { type wall; }" in snappy and "searchableBox" in snappy
    # one farfield patch, freestream pair, no-slip body
    assert "freestream;" in files["0/U"] and "freestreamValue" in files["0/U"]
    assert "walls    { type noSlip; }" in files["0/U"]
    assert "freestreamPressure" in files["0/p"]
    assert files["system/blockMeshDict"].count("farfield") == 1
    # the forces function object is what makes a force readable at all
    cd_text = files["system/controlDict"]
    assert "type            forces;" in cd_text and "rhoInf          1.204" in cd_text
    assert "patches         (walls);" in cd_text
    assert "writeInterval   1;" in cd_text      # never miss the converged sample
    assert "simulationType  laminar" in files["constant/turbulenceProperties"]
    # RANS overlay swaps the model in and adds the turbulence fields
    rans = mb.snappy_external_case_files(
        bbox_min_m=(-0.005,) * 3, bbox_max_m=(0.005,) * 3,
        freestream_velocity_m_s=(12.0, 0.0, 0.0), nu_m2_s=1.5e-5, rho_kg_m3=1.204,
        turbulence="kOmegaSST")["files"]
    assert "kOmegaSST" in rans["constant/turbulenceProperties"]
    for f in ("0/k", "0/omega", "0/nut"):
        assert f in rans and "walls" in rans[f], f
    assert "nutkWallFunction" in rans["0/nut"]


def test_external_case_validation_and_writer():
    good = dict(bbox_min_m=(-0.005,) * 3, bbox_max_m=(0.005,) * 3,
                freestream_velocity_m_s=(12.0, 0.0, 0.0), nu_m2_s=1.5e-5,
                rho_kg_m3=1.204)
    for bad in ({"freestream_velocity_m_s": (0, 0, 0)}, {"nu_m2_s": 0},
                {"rho_kg_m3": -1}, {"surface_refine": (3, 1)},
                {"bbox_max_m": (-0.01,) * 3}):
        try:
            mb.snappy_external_case_files(**{**good, **bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"snappy_external_case_files({bad}) should have raised")
    with tempfile.TemporaryDirectory() as d:
        tris = mb.sphere_stl_triangles(0.01)
        out = mb.write_snappy_external_case(
            d, stl_text=mb.ascii_stl_regions({"walls": tris}), **good)
        assert os.path.isfile(os.path.join(d, "constant", "triSurface", "body.stl"))
        assert os.path.isfile(os.path.join(d, "system", "snappyHexMeshDict"))
        assert out["cmds"] == [["blockMesh"], ["snappyHexMesh", "-overwrite"],
                               ["simpleFoam"]]
        assert abs(out["flow_direction"][0] - 1.0) < 1e-12
        assert abs(out["velocity_magnitude_m_s"] - 12.0) < 1e-12
    # the sphere twin itself must be a closed, correctly-sized surface
    for bad in ((0.0, 24, 48), (0.01, 2, 48), (0.01, 24, 3)):
        try:
            mb.sphere_stl_triangles(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"sphere_stl_triangles{bad} should have raised")


def test_forces_object_and_parser_resolve_drag_and_lift():
    text = openfoam.forces_function_object(patches=("walls", "tail"), rho_kg_m3=998.0,
                                           centre_of_rotation=(0.1, 0, 0))
    assert "patches         (walls tail);" in text and "rhoInf          998" in text
    assert "CofR            (0.1 0 0);" in text
    for bad in (dict(patches=(), rho_kg_m3=1.0), dict(patches=("a b",), rho_kg_m3=1.0),
                dict(patches=("a",), rho_kg_m3=0.0)):
        try:
            openfoam.forces_function_object(**bad)
        except ValueError:
            pass
        else:
            raise AssertionError("forces_function_object should have raised")

    with tempfile.TemporaryDirectory() as d:
        fdir = os.path.join(d, "postProcessing", "forces", "0")
        os.makedirs(fdir)
        # total = pressure + viscous; drag along +x = 3.0 N, lift along +z = 4.0 N
        with open(os.path.join(fdir, "force.dat"), "w", encoding="utf-8") as f:
            f.write("# Force\n# Time total_x total_y total_z ...\n")
            f.write("1\t2.5 0 4.0\t2.0 0 4.0\t0.5 0 0\n")
            f.write("2\t3.0 0 4.0\t2.4 0 4.0\t0.6 0 0\n")
        with open(os.path.join(fdir, "moment.dat"), "w", encoding="utf-8") as f:
            f.write("# Moment\n2\t0 1.5 0\t0 1.2 0\t0 0.3 0\n")
        r = openfoam.parse_forces(d, velocity_m_s=10.0, rho_kg_m3=2.0,
                                  reference_area_m2=0.1, reference_length_m=0.5)
        assert abs(r["drag_force_n"] - 3.0) < 1e-12, r
        assert abs(r["lift_force_n"] - 4.0) < 1e-12, r
        assert abs(r["drag_pressure_n"] - 2.4) < 1e-12
        assert abs(r["drag_viscous_n"] - 0.6) < 1e-12
        # q = 0.5*2*100 = 100, A = 0.1 -> denom = 10
        assert abs(r["cd"] - 0.3) < 1e-12 and abs(r["cl"] - 0.4) < 1e-12, r
        # drift: |2.5 - 3.0| / 3.0 = 16.7 %
        assert abs(r["force_drift_pct"] - 100.0 / 6.0) < 1e-9, r
        assert r["n_samples"] == 2
        # flow along -x flips the sign of drag
        back = openfoam.parse_forces(d, flow_direction=(-1, 0, 0))
        assert abs(back["drag_force_n"] + 3.0) < 1e-12
        assert back["cd"] is None                       # no reference state given
    assert openfoam.parse_forces(tempfile.mkdtemp()) is None


def test_solve_converged_distinguishes_converged_from_out_of_iterations():
    assert openfoam.solve_converged(
        "Time = 219\nSIMPLE solution converged in 219 iterations\nEnd\n") is True
    assert openfoam.solve_converged("Time = 3000\nExecutionTime = 9 s\nEnd\n") is False
    assert openfoam.solve_converged("Time = 1200\nsmoothSolver: ...") is None
    assert openfoam.solve_converged("") is None


# --- solver-backed: the relative gates -------------------------------------------

def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def _run_foam_case(case_dir, cmds):
    """blockMesh/snappy/simpleFoam in ``case_dir`` through the platform substrate
    (``bash`` on Linux, ``wsl`` on Windows, ``multipass exec`` on macOS — which needs
    the case dir passed so it can cd into the VM-mounted path). Returns the proc."""
    bashrc = solvers.openfoam_bashrc()
    chain = " && ".join(" ".join(a) for a in cmds)
    script = (f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else "") + chain
    return subprocess.run(solvers.bash_argv(script, case_dir), cwd=case_dir,
                          capture_output=True, text=True)


def test_bridged_box_matches_heisler_oracle():
    """The kickoff's Elmer-path relative gate: a FreeCAD-meshed 20 mm cube (the
    committed UNV fixture, face groups intact) through ElmerGrid + ElmerSolver as a
    plane wall — convection on faces 1+2 (x=0, x=20 mm), the four lateral faces
    natural — must match the Heisler one-term oracle at Bi=0.5, Fo≈0.49 (measured
    0.2–0.9 % live; gate 3 %)."""
    if skip_heavy("Elmer/OpenFOAM mesh bridge"):
        return
    if not solvers.is_available("elmer") or not os.path.isfile(
            solvers.sibling_bin(solvers.find_solver("elmer")["path"], "ElmerGrid")):
        print("    SKIP — ElmerSolver/ElmerGrid not installed")
        return
    assert _FIXTURE_UNV.is_file(), f"missing fixture {_FIXTURE_UNV}"
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(_FIXTURE_UNV, os.path.join(d, "body.unv"))
        built = mb.write_body_transient_case(
            d, k=200.0, rho=2700.0, cp=900.0, h_conv=10000.0, duration_s=0.6,
            convection_tags=[1, 2], t_initial_c=100.0, t_ambient_c=25.0)
        grid_argv = [solvers.sibling_bin(          # bare "ElmerGrid" isn't on PATH
            solvers.find_solver("elmer")["path"], "ElmerGrid")] + built["elmergrid_argv"][1:]
        grid = _run(grid_argv, d)
        assert grid.returncode == 0, grid.stdout[-800:] + grid.stderr[-800:]
        solve = _run([solvers.find_solver("elmer")["path"], built["sif"]], d)
        assert solve.returncode == 0, solve.stdout[-800:] + solve.stderr[-800:]
        parsed = mb.parse_minmax_scalars(d, built["scalars"])
        assert parsed, "no scalars written"

        oracle = thermal.thermal_transient_1d(
            half_thickness_mm=10.0, h_conv=10000.0, duration_s=0.6,
            k=200.0, rho=2700.0, cp=900.0, t_initial_c=100.0, t_ambient_c=25.0)
        assert oracle["one_term_valid"], oracle
        # compare excursions above ambient (offset-free)
        for solved, key in ((parsed["t_max_c"], "t_center_c"),
                            (parsed["t_min_c"], "t_surface_c")):
            ratio = (solved - 25.0) / (oracle[key] - 25.0)
            assert 0.97 < ratio < 1.03, (key, solved, oracle[key], ratio)
        print(f"    bridged box vs Heisler: center {parsed['t_max_c']:.2f} vs "
              f"{oracle['t_center_c']:.2f} C, surface {parsed['t_min_c']:.2f} vs "
              f"{oracle['t_surface_c']:.2f} C")


def test_bridged_cylinder_matches_hagen_poiseuille():
    """The kickoff's OpenFOAM-path relative gate: the closed cylinder STL (D=10 mm,
    L=100 mm) through blockMesh + snappyHexMesh + simpleFoam at Re=50 must land the
    developed-profile pressure drop on Hagen–Poiseuille (measured 0.6 %; gate 10 %)."""
    if skip_heavy("Elmer/OpenFOAM mesh bridge"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    D, L, U, nu, rho = 0.01, 0.1, 0.005, 1e-6, 1000.0
    with tempfile.TemporaryDirectory() as d:
        mb.write_snappy_internal_case(
            d, stl_text=mb.ascii_stl_regions(mb.cylinder_stl_regions(D, L)),
            bbox_min_m=(-D / 2, -D / 2, 0.0), bbox_max_m=(D / 2, D / 2, L),
            inlet_velocity_m_s=(0.0, 0.0, U), nu_m2_s=nu)
        proc = _run_foam_case(d, mb.snappy_mesh_cmds())
        assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1200:]

        parsed = openfoam.parse_pressure_drop(d, rho_kg_m3=rho)
        assert parsed, "no converged p field"
        hp = cfd.pipe_pressure_drop(diameter_mm=D * 1000, length_mm=L * 1000,
                                    velocity_m_s=U, mu_pa_s=nu * rho, rho_kg_m3=rho)
        ratio = parsed["dp_developed_pa"] / hp["hagen_poiseuille_pa"]
        assert 0.9 < ratio < 1.1, (parsed, hp["hagen_poiseuille_pa"], ratio)
        print(f"    bridged cylinder vs Hagen-Poiseuille: "
              f"{parsed['dp_developed_pa']:.4g} vs {hp['hagen_poiseuille_pa']:.4g} Pa "
              f"(ratio {ratio:.3f}, {parsed['n_cells']} cells)")


def _tunnel_solve(case_dir, *, tris, bbox_min_m, bbox_max_m, velocity, direction,
                  nu, rho, **kwargs):
    """Write + run one wind-tunnel case and return the resolved force result."""
    area = mb.projected_area(tris, direction)
    mb.write_snappy_external_case(
        case_dir, stl_text=mb.ascii_stl_regions({"walls": tris}),
        bbox_min_m=bbox_min_m, bbox_max_m=bbox_max_m,
        freestream_velocity_m_s=tuple(v * velocity for v in direction),
        nu_m2_s=nu, rho_kg_m3=rho, frontal_area_m2=area, **kwargs)
    proc = _run_foam_case(case_dir, mb.snappy_mesh_cmds())
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
    out = openfoam.parse_forces(case_dir, velocity_m_s=velocity, rho_kg_m3=rho,
                                reference_area_m2=area, flow_direction=direction)
    assert out, "the forces function object wrote nothing"
    assert openfoam.solve_converged(proc.stdout + proc.stderr) is not False, \
        "the solve ran out of iterations instead of converging"
    out["frontal_area_m2"] = area
    return out


def test_wind_tunnel_sphere_matches_the_drag_curve():
    """The wind tunnel's headline gate (#223/#224): a sphere STL through blockMesh +
    snappyHexMesh + simpleFoam, with the force integrated by the `forces` function
    object, must land on the standard sphere drag curve across two Reynolds decades.
    BANDED (10 %) — Clift-Gauvin is a correlation, not an exact law. Measured live on
    OpenFOAM v2512: 0.8 % at Re=1, 1.8 % at Re=100."""
    if skip_heavy("OpenFOAM wind tunnel"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    D, rho = 0.01, 998.2
    mu, _ = cfd._fluid_props("water-20c", None, None)
    nu = mu / rho
    tris = mb.sphere_stl_triangles(D)
    for re in (1.0, 100.0):
        U = re * nu / D
        with tempfile.TemporaryDirectory() as d:
            got = _tunnel_solve(d, tris=tris, bbox_min_m=(-D / 2,) * 3,
                                bbox_max_m=(D / 2,) * 3, velocity=U,
                                direction=(1.0, 0.0, 0.0), nu=nu, rho=rho)
        oracle = cfd.sphere_drag(D * 1000, U, mu_pa_s=mu, rho_kg_m3=rho)
        ratio = got["cd"] / oracle["cd"]
        assert abs(ratio - 1.0) < oracle["band_pct"] / 100.0, \
            (re, got["cd"], oracle["cd"], ratio)
        # a symmetric body in axial flow makes no lift, and the force must have BOTH
        # a pressure and a viscous part (all-pressure means the BL was never resolved)
        assert abs(got["lift_force_n"]) < 1e-3 * abs(got["drag_force_n"]), got
        assert got["drag_pressure_n"] > 0 and got["drag_viscous_n"] > 0, got
        assert got["force_drift_pct"] < 0.1, got
        print(f"    sphere Re={re:g}: Cd {got['cd']:.4g} vs curve {oracle['cd']:.4g} "
              f"(ratio {ratio:.3f}, drift {got['force_drift_pct']:.1e} %)")


def test_wind_tunnel_orders_broadside_above_edge_on():
    """The two-sided physical gate: the SAME plate, turned into the flow, must drag
    far more broadside than edge-on, and its drag must become form-dominated when it
    does. Also exercises `flow_direction` (the body never moves; only the freestream
    vector does), which the sphere's symmetry cannot test.

    The comparison is on FORCE, not Cd: each orientation normalizes by its own frontal
    area, and at Re=100 the edge-on plate's small silhouette carries a viscous drag that
    makes its Cd the LARGER of the two (measured 2.97 vs 1.27) — true and unsurprising,
    but not the quantity "streamlining reduces drag" is about."""
    if skip_heavy("OpenFOAM wind tunnel"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    rho = 998.2
    mu, _ = cfd._fluid_props("water-20c", None, None)
    nu = mu / rho
    lo, hi = (-0.01, -0.01, -0.002), (0.01, 0.01, 0.002)     # 20 x 20 x 4 mm plate
    tris = _box_triangles(lo, hi)
    U = 100.0 * nu / 0.02                                     # Re = 100 on the 20 mm
    got = {}
    for label, direction in (("broadside", (0.0, 0.0, 1.0)),
                             ("edge_on", (1.0, 0.0, 0.0))):
        with tempfile.TemporaryDirectory() as d:
            got[label] = _tunnel_solve(
                d, tris=tris, bbox_min_m=lo, bbox_max_m=hi, velocity=U,
                direction=direction, nu=nu, rho=rho, surface_refine=(3, 4))
    b, e = got["broadside"], got["edge_on"]
    assert b["frontal_area_m2"] > 4 * e["frontal_area_m2"], (b, e)   # 400 vs 80 mm^2
    assert b["drag_force_n"] > 2.0 * e["drag_force_n"], (b, e)
    # broadside is form-drag dominated; edge-on is not
    assert b["drag_pressure_n"] / b["drag_force_n"] > 0.5, b
    assert (b["drag_pressure_n"] / b["drag_force_n"]
            > e["drag_pressure_n"] / e["drag_force_n"]), (b, e)
    print(f"    plate broadside Cd {b['cd']:.3g} ({b['drag_force_n']:.3g} N) vs "
          f"edge-on Cd {e['cd']:.3g} ({e['drag_force_n']:.3g} N)")


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not solvers.is_available("elmer") or not solvers.is_available("openfoam"):
        print("  (a solver is absent — structure tests run, its live gate SKIPs)")
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:54s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:54s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

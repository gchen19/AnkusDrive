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
                  "locationInMesh (0 0 0.05)", "implicitFeatureSnap true"):
        assert token in sn, f"missing {token!r}"
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


# --- solver-backed: the relative gates -------------------------------------------

def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


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
    bashrc = solvers.openfoam_bashrc()
    with tempfile.TemporaryDirectory() as d:
        mb.write_snappy_internal_case(
            d, stl_text=mb.ascii_stl_regions(mb.cylinder_stl_regions(D, L)),
            bbox_min_m=(-D / 2, -D / 2, 0.0), bbox_max_m=(D / 2, D / 2, L),
            inlet_velocity_m_s=(0.0, 0.0, U), nu_m2_s=nu)
        chain = " && ".join(" ".join(a) for a in mb.snappy_mesh_cmds())
        script = (f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else "") + chain
        proc = subprocess.run(solvers.bash_argv(script), cwd=d,
                              capture_output=True, text=True)
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

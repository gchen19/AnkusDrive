"""Elmer transient-thermal toys — case generation + the solver-backed oracle gate.

Two tiers, both runnable on the no-FreeCAD lane:
  * **structure** (always): the generated native mesh + .sif are well-formed — the
    half-slab has the right node/element counts, two boundary tags (symmetry +
    convection), and the .sif carries the material, the convective BC, and the
    SaveScalars max/min probes. Pure string/file checks, no solver.
  * **solver-backed** (when ElmerSolver resolves, else SKIP): build the 1-D plane-wall
    transient, run ElmerSolver, and assert the centre/surface temperatures match
    `thermal_transient_1d` (the Heisler one-term oracle) — and, in the small-Biot
    limit, the lumped `thermal_lumped` exponential. This is the kickoff's relative
    gate for the heavy thermal solve, now that the solver is provisioned.

Run:  python3 tests/test_elmer.py
"""
import math
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
from driftpin.analysis import elmer  # noqa: E402
from driftpin.analysis import thermal  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


# --- structure (no solver) ----------------------------------------------------

def test_slab_mesh_files_are_well_formed():
    files = elmer.slab_mesh_files(n_elements=10, length_m=0.02)
    assert set(files) == {"mesh.header", "mesh.nodes", "mesh.elements", "mesh.boundary"}
    # 11 nodes, 10 line bulk elements, 2 point boundary elements
    assert files["mesh.header"].split("\n")[0].split() == ["11", "10", "2"]
    assert len(files["mesh.nodes"].strip().splitlines()) == 11
    assert len(files["mesh.elements"].strip().splitlines()) == 10
    bnd = files["mesh.boundary"].strip().splitlines()
    assert len(bnd) == 2
    # tag 1 at node 1 (x=0 symmetry); tag 2 at the last node (x=L convection)
    assert bnd[0].split()[1] == "1" and bnd[0].split()[-1] == "1"
    assert bnd[1].split()[1] == "2" and bnd[1].split()[-1] == "11"
    # nodes span [0, length] and the last node sits exactly at L
    xs = [float(line.split()[2]) for line in files["mesh.nodes"].strip().splitlines()]
    assert xs[0] == 0.0 and abs(xs[-1] - 0.02) < 1e-12


def test_slab_sif_carries_material_bc_and_probes():
    sif = elmer.slab_transient_sif(
        k=15.0, rho=8000.0, cp=500.0, h_conv=375.0,
        t_initial_c=100.0, t_ambient_c=25.0, dt=0.5, n_steps=120)
    for token in ("Simulation Type = Transient", "Heat Conductivity = 15",
                  "Density = 8000", "Heat Capacity = 500",
                  "Heat Transfer Coefficient = 375", "External Temperature = 25",
                  "Initial Condition 1", "Temperature = 100",
                  "Operator 1 = max", "Operator 2 = min",
                  "Timestep Intervals = 120"):
        assert token in sif, f"missing {token!r} in .sif"


def test_write_case_lays_down_files_and_splits_time():
    with tempfile.TemporaryDirectory() as d:
        meta = elmer.write_slab_transient_case(
            d, half_thickness_m=0.02, k=15.0, rho=8000.0, cp=500.0, h_conv=375.0,
            t_initial_c=100.0, t_ambient_c=25.0, duration_s=60.0,
            n_elements=20, n_steps=120)
        assert os.path.isfile(os.path.join(d, "case.sif"))
        assert os.path.isfile(os.path.join(d, "ELMERSOLVER_STARTINFO"))
        assert os.path.isfile(os.path.join(d, "slab", "mesh.header"))
        assert abs(meta["dt"] - 0.5) < 1e-12  # 60 s / 120 steps
        assert meta["mesh_db"] == "slab"


def test_parse_scalars_reads_last_row():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "scalars.dat"), "w") as f:
            f.write("  99.0  80.0\n  90.0  77.0\n  89.81  76.52\n")
        got = elmer.parse_slab_scalars(d)
        assert abs(got["t_center_c"] - 89.81) < 1e-9    # col 1 = max = centre
        assert abs(got["t_surface_c"] - 76.52) < 1e-9   # col 2 = min = surface
        assert got["n_steps_written"] == 3
    # absent file -> None, never raises
    with tempfile.TemporaryDirectory() as d:
        assert elmer.parse_slab_scalars(d) is None


def test_input_validation():
    for bad in (
        lambda: elmer.slab_mesh_files(1, 0.02),                 # too few elems
        lambda: elmer.slab_mesh_files(10, 0.0),                 # zero length
        lambda: elmer.write_slab_transient_case(
            "/tmp/nope_x", half_thickness_m=-1, k=1, rho=1, cp=1, h_conv=1,
            t_initial_c=100, t_ambient_c=25, duration_s=1),     # bad geometry
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- radiation structure (no solver) ------------------------------------------

def test_radiation_mesh_two_plates_well_formed():
    files = elmer.radiation_plates_mesh_files(n_x=8, width_m=1.0,
                                              plate_thickness_m=0.01, gap_m=0.01, n_y=2)
    assert set(files) == {"mesh.header", "mesh.nodes", "mesh.elements", "mesh.boundary"}
    # two 8x2 quad grids = 32 bulk elems; 4 boundary tags x 8 edges = 32 boundary elems
    n_nodes, n_bulk, n_bnd = files["mesh.header"].split("\n")[0].split()
    assert (n_bulk, n_bnd) == ("32", "32"), files["mesh.header"]
    assert len(files["mesh.elements"].strip().splitlines()) == 32
    tags = sorted({line.split()[1] for line in files["mesh.boundary"].strip().splitlines()})
    assert tags == ["1", "2", "3", "4"], tags          # 4 plate faces
    # all bulk elements are 404 quads
    assert all(line.split()[2] == "404" for line in files["mesh.elements"].strip().splitlines())


def test_radiation_sif_is_diffuse_gray_in_kelvin():
    sif = elmer.radiation_plates_sif(t1_c=500.0, t2_c=100.0,
                                     emissivity_1=0.8, emissivity_2=0.6)
    for token in ("Simulation Type = Steady State", "Stefan Boltzmann",
                  "Radiation = Diffuse Gray", "Emissivity = 0.8", "Emissivity = 0.6",
                  "View Factors", "Gebhart Factors", 'Operator 1 = "diffusive flux"'):
        assert token in sif, f"missing {token!r} in radiation .sif"
    # temperatures are imposed in Kelvin (radiation is T^4): 500C -> 773.15, 100C -> 373.15
    assert "773.15" in sif and "373.15" in sif, sif


def test_radiation_write_case_and_parse_flux():
    with tempfile.TemporaryDirectory() as d:
        meta = elmer.write_radiation_plates_case(
            d, t1_c=500.0, t2_c=100.0, emissivity_1=0.8, emissivity_2=0.8,
            width_m=2.0, gap_m=0.01, n_x=8)
        assert os.path.isfile(os.path.join(d, "case.sif"))
        assert os.path.isfile(os.path.join(d, "rad", "mesh.header"))
        assert abs(meta["area_1_m2"] - 2.0) < 1e-12       # width 2 x depth 1
        # parse: col 1 of the last row is the net flux (W); /area -> W/m^2
        with open(os.path.join(d, meta["scalars"]), "w") as f:
            f.write("  100.0 1 1\n  25510.0 1 1\n")        # net 25510 W over 2 m^2
        got = elmer.parse_radiation_flux(d, meta["scalars"], meta["area_1_m2"])
        assert abs(got["q_net_w"] - 25510.0) < 1e-9
        assert abs(got["flux_w_m2"] - 12755.0) < 1e-9
    with tempfile.TemporaryDirectory() as d:
        assert elmer.parse_radiation_flux(d) is None       # absent -> None, no raise


def test_radiation_input_validation():
    try:
        elmer.write_radiation_plates_case("/tmp/nope_rad", t1_c=500, t2_c=100,
                                          emissivity_1=1.5, emissivity_2=0.8)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on emissivity > 1")


# --- solver-backed (skips when ElmerSolver is absent) -------------------------

def _run_slab(half_mm, h, dur, k, rho, cp, ti=100.0, ta=25.0, ne=40, ns=120):
    """Build + run the slab case, return parse_slab_scalars dict (or None)."""
    d = tempfile.mkdtemp(prefix="elmer_test_")
    built = elmer.write_slab_transient_case(
        d, half_thickness_m=half_mm / 1000.0, k=k, rho=rho, cp=cp, h_conv=h,
        t_initial_c=ti, t_ambient_c=ta, duration_s=dur, n_elements=ne, n_steps=ns)
    binpath = solvers.find_solver("elmer")["path"]
    subprocess.run([binpath, built["sif"]], cwd=d, capture_output=True, text=True)
    return elmer.parse_slab_scalars(d, built["scalars"])


def test_solve_matches_heisler_oracle():
    """Bi=0.5, Fo=0.5 plane wall: the Elmer centre/surface temps match the one-term
    analytic oracle to a small fraction of a degree."""
    if skip_heavy("Elmer thermal/radiation"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    L_mm, h, k, rho, cp = 20.0, 375.0, 15.0, 8000.0, 500.0
    alpha = k / (rho * cp)
    dur = 0.5 * (L_mm / 1000.0) ** 2 / alpha          # Fo = 0.5
    got = _run_slab(L_mm, h, dur, k, rho, cp)
    assert got is not None, "no SaveScalars output (solve failed)"
    orc = thermal.thermal_transient_1d(
        half_thickness_mm=L_mm, h_conv=h, duration_s=dur, k=k, rho=rho, cp=cp)
    assert abs(got["t_center_c"] - orc["t_center_c"]) < 0.3, (got, orc)
    assert abs(got["t_surface_c"] - orc["t_surface_c"]) < 0.3, (got, orc)


def test_solve_agrees_with_lumped_at_small_biot():
    """As Bi→0 the slab is near-isothermal: the Elmer centre temp tracks the lumped
    exponential exp(−Bi·Fo), so centre≈surface and both match thermal_transient_1d's
    lumped cross-check."""
    if skip_heavy("Elmer thermal/radiation"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    L_mm, h, k, rho, cp = 10.0, 20.0, 200.0, 2700.0, 900.0   # tiny Bi (high-k metal)
    alpha = k / (rho * cp)
    dur = 0.5 * (L_mm / 1000.0) ** 2 / alpha
    got = _run_slab(L_mm, h, dur, k, rho, cp)
    assert got is not None
    orc = thermal.thermal_transient_1d(
        half_thickness_mm=L_mm, h_conv=h, duration_s=dur, k=k, rho=rho, cp=cp)
    assert orc["biot"] < 0.01, f"expected small Biot, got {orc['biot']}"
    # near-isothermal: centre and surface within a few hundredths of a degree
    assert abs(got["t_center_c"] - got["t_surface_c"]) < 0.1, got
    assert abs(got["t_center_c"] - orc["t_center_lumped_c"]) < 0.2, (got, orc)


def _run_radiation(t1_c, t2_c, e1, e2, width_m=1.0, gap_m=0.01, n_x=80):
    """Build + run the two-plate radiation case (ViewFactors then ElmerSolver), return
    (parse_radiation_flux dict, area). Returns (None, area) when the solve produced no
    scalars."""
    d = tempfile.mkdtemp(prefix="elmer_rad_test_")
    built = elmer.write_radiation_plates_case(
        d, t1_c=t1_c, t2_c=t2_c, emissivity_1=e1, emissivity_2=e2,
        width_m=width_m, gap_m=gap_m, n_x=n_x)
    elmer_bin = solvers.find_solver("elmer")["path"]
    vf_bin = solvers.sibling_bin(elmer_bin, "ViewFactors")
    subprocess.run([vf_bin, built["sif"]], cwd=d, capture_output=True, text=True)
    subprocess.run([elmer_bin, built["sif"]], cwd=d, capture_output=True, text=True)
    return elmer.parse_radiation_flux(d, built["scalars"], built["area_1_m2"]), built["area_1_m2"]


def test_radiation_matches_two_plate_oracle():
    """Two parallel plates exchanging diffuse-gray radiation: the Elmer net flux matches
    the exact two-plate closed form q = σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1) within 2% (the residual
    is finite-plate edge leakage), across symmetric and asymmetric emissivities."""
    if skip_heavy("Elmer thermal/radiation"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    if not os.path.isfile(solvers.sibling_bin(
            solvers.find_solver("elmer")["path"], "ViewFactors")):
        print("    SKIP — Elmer ViewFactors binary not found")
        return
    for t1, t2, e1, e2 in ((500, 100, 0.8, 0.8), (450, 50, 0.5, 0.9), (300, 20, 0.9, 1.0)):
        got, _area = _run_radiation(t1, t2, e1, e2)
        assert got is not None, f"no radiation flux output for {(t1, t2, e1, e2)}"
        orc = thermal.radiation_exchange(t1, t2, e1, e2)
        ratio = got["flux_w_m2"] / orc["two_plate_flux_w_m2"]
        assert 0.98 <= ratio <= 1.02, (t1, t2, e1, e2, got["flux_w_m2"],
                                       orc["two_plate_flux_w_m2"], ratio)


def test_radiation_emissivity_lowers_flux():
    """Halving both emissivities cuts the exchanged flux (the 1/ε₁+1/ε₂−1 denominator
    grows), and Elmer tracks the oracle's drop."""
    if skip_heavy("Elmer thermal/radiation"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    if not os.path.isfile(solvers.sibling_bin(
            solvers.find_solver("elmer")["path"], "ViewFactors")):
        print("    SKIP — Elmer ViewFactors binary not found")
        return
    hi, _a = _run_radiation(500, 100, 0.9, 0.9)
    lo, _b = _run_radiation(500, 100, 0.45, 0.45)
    assert hi is not None and lo is not None
    assert lo["flux_w_m2"] < hi["flux_w_m2"], (lo, hi)
    # the ratio of the two solves tracks the ratio of the two oracles within 2%
    o_hi = thermal.radiation_exchange(500, 100, 0.9, 0.9)["two_plate_flux_w_m2"]
    o_lo = thermal.radiation_exchange(500, 100, 0.45, 0.45)["two_plate_flux_w_m2"]
    assert abs((lo["flux_w_m2"] / hi["flux_w_m2"]) - (o_lo / o_hi)) < 0.02, (lo, hi)


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not solvers.is_available("elmer"):
        print("  (ElmerSolver absent — structure tests run, solver tests SKIP)")
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

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

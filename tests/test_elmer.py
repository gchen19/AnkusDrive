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

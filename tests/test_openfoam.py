"""OpenFOAM CFD toys — pipe case generation + the Hagen–Poiseuille solver gate.

Two tiers, both runnable on the no-FreeCAD lane:
  * **structure** (always): the generated case is well-formed — the wedge
    blockMeshDict has the six collapsed-axis vertices and the inlet/outlet/wall/wedge
    patches, the field/dictionary files carry the inlet velocity, viscosity and the
    laminar model. Pure string/file checks, no solver.
  * **solver-backed** (when OpenFOAM resolves, else SKIP): build the axisymmetric
    laminar pipe, run blockMesh+simpleFoam, and assert the solved pressure drop matches
    Hagen–Poiseuille (Δp = 128μLQ/πD⁴) within 10% — plus the D⁴ scaling law (halving D
    at fixed flow → ~16× Δp). This is the kickoff's "unambiguous" CFD gate, now that
    OpenFOAM is provisioned.

Run:  python3 tests/test_openfoam.py
"""
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import cfd  # noqa: E402
from driftpin.analysis import openfoam  # noqa: E402


# --- structure (no solver) ----------------------------------------------------

def test_blockmeshdict_is_a_collapsed_axis_wedge():
    bm = openfoam.pipe_blockmeshdict(
        diameter_m=0.01, length_m=0.5, half_angle_deg=2.5, n_axial=100, n_radial=10)
    # six vertices: axis pair (r=0) + four at r=R on the ±z wedge faces
    verts = re.search(r"vertices\s*\((.*?)\);", bm, re.S).group(1)
    assert verts.count("(") == 6, verts
    for patch in ("inlet", "outlet", "wall", "wedge1", "wedge2"):
        assert patch in bm, f"missing patch {patch}"
    assert "type wedge;" in bm
    assert "(100 10 1)" in bm  # n_axial n_radial 1


def test_case_files_carry_flow_and_model():
    files = openfoam.pipe_case_files(
        diameter_m=0.01, length_m=0.5, velocity_m_s=0.005, nu_m2_s=1e-6,
        n_axial=80, n_radial=8, end_time=2000)
    assert {"system/blockMeshDict", "constant/transportProperties",
            "constant/turbulenceProperties", "0/U", "0/p",
            "system/controlDict", "system/fvSchemes",
            "system/fvSolution"} <= set(files)
    assert "uniform (0.005 0 0)" in files["0/U"]      # inlet velocity
    assert "nu              1e-06" in files["constant/transportProperties"]
    assert "simulationType  laminar;" in files["constant/turbulenceProperties"]
    assert "application     simpleFoam;" in files["system/controlDict"]
    assert "fixedValue; value uniform 0;" in files["0/p"]  # outlet pinned


def test_write_case_and_reynolds():
    with tempfile.TemporaryDirectory() as d:
        meta = openfoam.write_pipe_case(
            d, diameter_m=0.01, length_m=0.5, velocity_m_s=0.005, nu_m2_s=1e-6)
        assert os.path.isfile(os.path.join(d, "system", "blockMeshDict"))
        assert os.path.isfile(os.path.join(d, "0", "U"))
        assert os.path.isfile(os.path.join(d, "constant", "transportProperties"))
        assert abs(meta["reynolds"] - 50.0) < 1e-6     # 0.005*0.01/1e-6


def test_parse_pressure_drop_reads_field():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "1000"))
        # a linear pressure profile: mean = max/2, so 2*mean == max == inlet drop
        with open(os.path.join(d, "1000", "p"), "w") as f:
            f.write("internalField   nonuniform List<scalar>\n4\n(\n0.8\n0.6\n0.4\n0.2\n)\n;\n")
        got = openfoam.parse_pressure_drop(d, rho_kg_m3=1000.0)
        assert abs(got["dp_inlet_pa"] - 800.0) < 1e-6        # 1000 * max(0.8)
        assert abs(got["dp_developed_pa"] - 1000.0) < 1e-6   # 1000 * 2 * mean(0.5)
        assert got["n_cells"] == 4 and got["time"] == "1000"
    with tempfile.TemporaryDirectory() as d:
        assert openfoam.parse_pressure_drop(d, rho_kg_m3=1000.0) is None


def test_input_validation():
    for bad in (
        lambda: openfoam.pipe_blockmeshdict(
            diameter_m=0, length_m=1, half_angle_deg=2.5, n_axial=10, n_radial=10),
        lambda: openfoam.pipe_blockmeshdict(
            diameter_m=0.01, length_m=0.5, half_angle_deg=2.5, n_axial=1, n_radial=10),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- solver-backed (skips when OpenFOAM is absent) ----------------------------

def _solve_pipe(D_mm, L_mm, U, nu, rho, na=120, nr=15, et=4000):
    """Build + run a pipe case, return parse_pressure_drop dict (or None)."""
    d = tempfile.mkdtemp(prefix="foam_test_")
    openfoam.write_pipe_case(
        d, diameter_m=D_mm / 1000.0, length_m=L_mm / 1000.0, velocity_m_s=U,
        nu_m2_s=nu, n_axial=na, n_radial=nr, end_time=et)
    bashrc = solvers.openfoam_bashrc()
    src = f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else ""
    subprocess.run(["bash", "-c", src + "blockMesh > log.bm 2>&1 && simpleFoam > log.sf 2>&1"],
                   cwd=d, capture_output=True, text=True)
    return openfoam.parse_pressure_drop(d, rho_kg_m3=rho)


def test_pipe_matches_hagen_poiseuille():
    """Re≈50 laminar pipe: the solved developed Δp is within 10% of Hagen–Poiseuille
    (it lands within ~1% in practice)."""
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    D_mm, L_mm, nu, rho = 10.0, 500.0, 1.0038e-6, 998.2     # water-20c
    U = 50.0 * nu / (D_mm / 1000.0)                          # Re = 50
    got = _solve_pipe(D_mm, L_mm, U, nu, rho)
    assert got is not None, "no converged p field (solve failed)"
    hp = cfd.pipe_pressure_drop(diameter_mm=D_mm, length_mm=L_mm, velocity_m_s=U,
                                mu_pa_s=nu * rho, rho_kg_m3=rho)
    ratio = got["dp_developed_pa"] / hp["hagen_poiseuille_pa"]
    assert 0.9 <= ratio <= 1.1, (ratio, got, hp)


def test_pipe_d4_scaling_law():
    """At fixed volumetric flow, halving the bore raises Δp ~16× (the D⁴ law a
    mis-scaled solver fails). Compare two solved pipes (D and D/2, same Q)."""
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    L_mm, nu, rho = 400.0, 1.0038e-6, 998.2
    # fixed flow rate Q; U = Q / area, so U_small = 4*U_big (area ¼). Keep Re laminar.
    D_big = 10.0
    Q = 50.0 * nu * math.pi * (D_big / 1000.0) / 4.0        # gives Re≈50 in the big pipe
    def U_for(D_mm):
        return Q / (math.pi * (D_mm / 1000.0) ** 2 / 4.0)
    big = _solve_pipe(D_big, L_mm, U_for(D_big), nu, rho, na=100, nr=12)
    small = _solve_pipe(D_big / 2, L_mm, U_for(D_big / 2), nu, rho, na=100, nr=12)
    assert big and small, (big, small)
    ratio = small["dp_developed_pa"] / big["dp_developed_pa"]
    assert 13.5 <= ratio <= 18.5, f"D^4 scaling ratio {ratio:.2f} not ~16"


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not solvers.is_available("openfoam"):
        print("  (OpenFOAM absent — structure tests run, solver tests SKIP)")
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

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

from ankusdrive import solvers  # noqa: E402
from ankusdrive.analysis import cfd  # noqa: E402
from ankusdrive.analysis import openfoam  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


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
        with open(os.path.join(d, "1000", "p"), "w", encoding="utf-8") as f:
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


# --- flat-plate structure (no solver) -----------------------------------------

def test_flat_plate_blockmesh_is_three_blocks():
    bm = openfoam.flat_plate_blockmeshdict(
        plate_length_m=0.1, upstream_m=0.03, wake_m=0.06, height_m=1.0,
        thickness_m=0.002, nx_plate=20, nx_upstream=6, nx_wake=8, n_y=10, grading_y=100.0)
    # 16 vertices (4 x-stations x 2 heights x 2 z-layers), 3 hex blocks
    verts = re.search(r"vertices\s*\((.*?)\);", bm, re.S).group(1)
    assert verts.count("(") == 16, verts
    assert bm.count("hex (") == 3, bm
    for patch in ("inlet", "outlet", "plate", "slip", "top", "frontAndBack"):
        assert patch in bm, f"missing patch {patch}"
    assert "type wall" in bm and "type symmetryPlane" in bm and "type empty" in bm


def test_flat_plate_case_files_complete():
    files = openfoam.flat_plate_case_files(velocity_m_s=1.5, nu_m2_s=1.5e-5)
    for rel in ("system/blockMeshDict", "constant/transportProperties",
                "constant/turbulenceProperties", "0/U", "0/p", "system/controlDict",
                "system/fvSchemes", "system/fvSolution"):
        assert rel in files, f"missing {rel}"
    assert "nu              1.5e-05" in files["constant/transportProperties"]
    assert "simulationType  laminar" in files["constant/turbulenceProperties"]
    assert "noSlip" in files["0/U"] and "(1.5 0 0)" in files["0/U"]


def test_flat_plate_write_case_and_meta():
    with tempfile.TemporaryDirectory() as d:
        meta = openfoam.write_flat_plate_case(d, velocity_m_s=1.5, nu_m2_s=1.5e-5,
                                              plate_length_m=0.1)
        assert os.path.isfile(os.path.join(d, "system", "blockMeshDict"))
        assert os.path.isfile(os.path.join(d, "0", "U"))
        assert abs(meta["reynolds_l"] - 1.5 * 0.1 / 1.5e-5) < 1e-6
        assert meta["nx_plate"] == 160 and meta["nx_upstream"] == 40


def test_flat_plate_parse_absent_is_none():
    with tempfile.TemporaryDirectory() as d:
        got = openfoam.parse_flat_plate_drag(
            d, rho_kg_m3=1.2, nu_m2_s=1.5e-5, velocity_m_s=1.5, plate_length_m=0.1,
            thickness_m=0.002, nx_plate=160, nx_upstream=40, n_y=140, grading_y=3000.0,
            height_m=1.5)
        assert got is None       # no converged field -> None, never raises


# --- solver-backed (skips when OpenFOAM is absent) ----------------------------

def _solve_pipe(D_mm, L_mm, U, nu, rho, na=120, nr=15, et=4000):
    """Build + run a pipe case, return parse_pressure_drop dict (or None)."""
    d = tempfile.mkdtemp(prefix="foam_test_")
    openfoam.write_pipe_case(
        d, diameter_m=D_mm / 1000.0, length_m=L_mm / 1000.0, velocity_m_s=U,
        nu_m2_s=nu, n_axial=na, n_radial=nr, end_time=et)
    bashrc = solvers.openfoam_bashrc()
    src = f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else ""
    # bash_argv routes through the WSL distro on Windows (issue #193)
    subprocess.run(solvers.bash_argv(src + "blockMesh > log.bm 2>&1 && simpleFoam > log.sf 2>&1", d),
                   cwd=d, capture_output=True, text=True)
    return openfoam.parse_pressure_drop(d, rho_kg_m3=rho)


def test_pipe_matches_hagen_poiseuille():
    """Re≈50 laminar pipe: the solved developed Δp is within 10% of Hagen–Poiseuille
    (it lands within ~1% in practice)."""
    if skip_heavy("OpenFOAM CFD"):
        return
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
    if skip_heavy("OpenFOAM CFD"):
        return
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


def _solve_flat_plate(U, nu, rho, L=0.1):
    """Build + run a flat-plate case, return (parse_flat_plate_drag dict, Blasius oracle)."""
    d = tempfile.mkdtemp(prefix="foam_plate_test_")
    built = openfoam.write_flat_plate_case(d, velocity_m_s=U, nu_m2_s=nu, plate_length_m=L)
    bashrc = solvers.openfoam_bashrc()
    src = f"source '{bashrc}' >/dev/null 2>&1\n" if bashrc else ""
    # bash_argv routes through the WSL distro on Windows (issue #193)
    subprocess.run(solvers.bash_argv(src + "blockMesh > log.bm 2>&1 && simpleFoam > log.sf 2>&1", d),
                   cwd=d, capture_output=True, text=True)
    got = openfoam.parse_flat_plate_drag(
        d, rho_kg_m3=rho, nu_m2_s=nu, velocity_m_s=U, plate_length_m=L,
        thickness_m=built["thickness_m"], nx_plate=built["nx_plate"],
        nx_upstream=built["nx_upstream"], n_y=built["n_y"], grading_y=built["grading_y"],
        height_m=built["height_m"])
    orc = cfd.flat_plate_drag(length_mm=L * 1000, velocity_m_s=U,
                              width_mm=built["thickness_m"] * 1000, mu_pa_s=nu * rho,
                              rho_kg_m3=rho)
    return got, orc


def test_flat_plate_matches_blasius():
    """Laminar flat plate (Re_L≈1e4): the solved wall-shear Cd is within 15% of the
    Blasius average Cf=1.328/√Re_L (it lands ~9% high and converges down with Re)."""
    if skip_heavy("OpenFOAM CFD"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    got, orc = _solve_flat_plate(U=1.5, nu=1.5e-5, rho=1.2)
    assert got is not None, "no converged U field (solve failed)"
    ratio = got["cf_solved"] / orc["cf_avg"]
    assert 0.85 <= ratio <= 1.15, (ratio, got["cf_solved"], orc["cf_avg"])


def test_flat_plate_blasius_u_scaling():
    """Blasius friction drag ∝ U^1.5 — two solved plates (U and 2U) reproduce the
    exponent the analytic oracle predicts, within the solver's tolerance."""
    if skip_heavy("OpenFOAM CFD"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    lo, _o1 = _solve_flat_plate(U=1.0, nu=1.5e-5, rho=1.2)
    hi, _o2 = _solve_flat_plate(U=2.0, nu=1.5e-5, rho=1.2)
    assert lo and hi, (lo, hi)
    ratio = hi["drag_force_n"] / lo["drag_force_n"]
    assert abs(ratio - 2.0 ** 1.5) / (2.0 ** 1.5) < 0.1, f"U^1.5 scaling: got {ratio:.3f}"



# --- B3: kOmegaSST RANS (case gen always; live banded gates when present) -------

def test_rans_pipe_case_files_carry_turbulence_model():
    files = openfoam.pipe_rans_case_files(
        diameter_m=0.05, length_m=2.4, velocity_m_s=2.0, nu_m2_s=1e-6)
    tp = files["constant/turbulenceProperties"]
    assert "RAS" in tp and "kOmegaSST" in tp, tp
    for f in ("0/k", "0/omega", "0/nut"):
        assert f in files, sorted(files)
    assert "kqRWallFunction" in files["0/k"]
    assert "omegaWallFunction" in files["0/omega"]
    assert "nutkWallFunction" in files["0/nut"]
    assert "wallDist" in files["system/fvSchemes"]
    assert "div(phi,k)" in files["system/fvSchemes"]
    assert "omega { solver" in files["system/fvSolution"]


def test_rans_pipe_writer_polices_regime_and_length():
    # laminar Re refused; short pipe (no developed region to fit) refused
    for bad in (
        lambda: openfoam.write_pipe_rans_case(
            tempfile.mkdtemp(), diameter_m=0.05, length_m=2.4,
            velocity_m_s=0.01, nu_m2_s=1e-6),
        lambda: openfoam.write_pipe_rans_case(
            tempfile.mkdtemp(), diameter_m=0.05, length_m=0.5,
            velocity_m_s=2.0, nu_m2_s=1e-6),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    d = tempfile.mkdtemp(prefix="ranspipe_gen_")
    built = openfoam.write_pipe_rans_case(
        d, diameter_m=0.05, length_m=2.4, velocity_m_s=2.0, nu_m2_s=1e-6)
    # wall-function discipline: first-cell y+ estimate inside the 30-300 window
    assert 20 < built["y_plus_estimate"] < 300, built["y_plus_estimate"]
    assert (Path(d) / "0" / "nut").exists()


def test_rans_plate_case_files_and_writer():
    files = openfoam.flat_plate_rans_case_files(velocity_m_s=30, nu_m2_s=1.5e-5)
    assert "kOmegaSST" in files["constant/turbulenceProperties"]
    assert "symmetryPlane" in files["0/k"]          # slip/top carried through
    d = tempfile.mkdtemp(prefix="ransplate_gen_")
    built = openfoam.write_flat_plate_rans_case(d, velocity_m_s=30, nu_m2_s=1.5e-5)
    assert built["reynolds_l"] == 2e6, built
    assert 20 < built["y_plus_estimate"] < 300, built["y_plus_estimate"]
    # sub-transition plate refused (that is the laminar case's job)
    try:
        openfoam.write_flat_plate_rans_case(
            tempfile.mkdtemp(), velocity_m_s=1.0, nu_m2_s=1.5e-5)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError below transition")
    # parsers are None-safe on an unsolved case
    assert openfoam.parse_pipe_rans_dpdx(
        d, n_axial=10, n_radial=10, length_m=1.0, rho_kg_m3=1000) is None


def test_rans_pipe_matches_colebrook_banded():
    if skip_heavy("OpenFOAM CFD"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    D, L, U, nu, rho = 0.05, 2.4, 2.0, 1e-6, 998.0
    d = tempfile.mkdtemp(prefix="ranspipe_live_")
    built = openfoam.write_pipe_rans_case(
        d, diameter_m=D, length_m=L, velocity_m_s=U, nu_m2_s=nu)
    rc = _run_case(d)
    assert rc == 0, "simpleFoam failed"
    parsed = openfoam.parse_pipe_rans_dpdx(
        d, n_axial=built["n_axial"], n_radial=built["n_radial"],
        length_m=L, rho_kg_m3=rho)
    assert parsed is not None, "no converged p field"
    ref = built["friction_factor_colebrook"] / D * 0.5 * rho * U * U
    ratio = parsed["dpdx_pa_m"] / ref
    # BANDED gate: the Moody chart itself is ±10 %; wall-function kOmegaSST
    # lands ~5-8 % low on a smooth pipe (observed 0.93 on this mesh)
    assert 0.85 <= ratio <= 1.15, (parsed["dpdx_pa_m"], ref, ratio)


def test_rans_plate_matches_mixed_cf_banded():
    if skip_heavy("OpenFOAM CFD"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    U, nu, rho = 30.0, 1.5e-5, 1.205
    d = tempfile.mkdtemp(prefix="ransplate_live_")
    built = openfoam.write_flat_plate_rans_case(d, velocity_m_s=U, nu_m2_s=nu)
    rc = _run_case(d)
    assert rc == 0, "simpleFoam failed"
    parsed = openfoam.parse_flat_plate_rans_drag(
        d, rho_kg_m3=rho, nu_m2_s=nu, velocity_m_s=U,
        plate_length_m=built["plate_length_m"], thickness_m=built["thickness_m"],
        nx_plate=built["nx_plate"], nx_upstream=built["nx_upstream"],
        n_y=built["n_y"], grading_y=built["grading_y"], height_m=built["height_m"])
    assert parsed is not None, "no converged U field"
    orc = cfd.flat_plate_drag_turbulent(
        1000 * built["plate_length_m"], U, mu_pa_s=nu * rho, rho_kg_m3=rho)
    ratio = parsed["cf_momentum"] / orc["cf_mixed"]
    # BANDED gate vs the mixed-transition 1/7-power Cf (observed 1.02 here);
    # the answer must also be unmistakably turbulent, not a laminar relapse
    assert 0.85 <= ratio <= 1.15, (parsed["cf_momentum"], orc["cf_mixed"], ratio)
    assert parsed["cf_momentum"] > 2.5 * orc["cf_laminar_blasius"], parsed
    # the wall-function-corrected shear agrees with the momentum integral
    if parsed["cf_wall_corrected"]:
        assert 0.7 <= parsed["cf_wall_corrected"] / parsed["cf_momentum"] <= 1.4


def _run_case(case_dir):
    bashrc = solvers.openfoam_bashrc()
    # cwd= (not an in-script `cd`) so a Windows case_dir auto-maps to /mnt/<drive>
    # when bash_argv routes through the WSL distro (issue #193)
    return subprocess.run(
        solvers.bash_argv(f"source '{bashrc}' >/dev/null 2>&1; blockMesh && simpleFoam",
                          case_dir),
        cwd=case_dir, capture_output=True, text=True).returncode


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

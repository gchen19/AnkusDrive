"""Acoustic FEM (Elmer HelmholtzSolve) — case generation always, solver gate
when ElmerSolver is present (SIMULATION_NEXT Tier B1).

The gates are the exact acoustic_screen closed forms: the driven closed duct's
rigid-end pressure 1/cos(kL), and the rigid-cavity eigenfrequencies localized
by the in-phase sign flip of a corner probe through resonance.

Run:  python3 tests/test_acoustic_fem.py
"""
import math
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers                       # noqa: E402
from driftpin.analysis import acoustics as ac      # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


def _run_elmer(case_dir, sif):
    binary = solvers.find_solver("elmer")["path"]
    return subprocess.run([binary, sif], cwd=case_dir,
                          capture_output=True, text=True)


# --- case generation (no solver needed) -----------------------------------------

def test_duct_case_structure():
    d = tempfile.mkdtemp(prefix="acfem_gen_")
    built = ac.write_helmholtz_duct_case(d, n_elements=50)
    mesh = Path(d) / built["mesh_db"]
    header = (mesh / "mesh.header").read_text().split()
    assert header[0] == "51" and header[1] == "50", header   # nodes, elements
    sif = (Path(d) / built["sif"]).read_text()
    assert "HelmholtzSolve" in sif and "Sound Speed" in sif
    assert f"Frequency = {built['frequency_hz']:.10g}" in sif
    # exact oracle values ride along with the case
    assert abs(built["p_end_exact"] - 1 / math.cos(2.0)) < 1e-12
    # a drive ON a duct resonance must be refused (the oracle diverges)
    try:
        ac.write_helmholtz_duct_case(tempfile.mkdtemp(), kl=math.pi / 2)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for kl at resonance")


def test_cavity_case_structure_and_sweep():
    d = tempfile.mkdtemp(prefix="acfem_gen_")
    built = ac.write_helmholtz_cavity_case(d, nx=10, ny=8, mode_nx=1, mode_ny=1)
    bnd = (Path(d) / built["mesh_db"] / "mesh.boundary").read_text()
    tags = {ln.split()[1] for ln in bnd.strip().splitlines()}
    assert tags == {"1", "2", "3"}, tags          # drive, rigid, corner probe
    # the sweep straddles the exact eigenfrequency without landing on it
    freqs = built["freqs"]
    f_ex = built["f_exact_hz"]
    assert freqs[0] < f_ex < freqs[-1]
    assert all(abs(f - f_ex) > 1e-9 for f in freqs)
    sif = (Path(d) / built["sif"]).read_text()
    assert "Wave Flux 1" in sif and "Scanning" in sif
    for bad in (
        lambda: ac.write_helmholtz_cavity_case(tempfile.mkdtemp(), mode_nx=0,
                                               mode_ny=0),
        lambda: ac.write_helmholtz_cavity_case(tempfile.mkdtemp(), nx=2, ny=2),
        lambda: ac.write_helmholtz_cavity_case(tempfile.mkdtemp(), n_steps=3),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_resonance_locator_on_synthetic_pole():
    # A(f) = C/(fn^2 - f^2) sampled off-resonance: the 1/A zero crossing in f^2
    # must recover fn essentially exactly (it is linear in that variable)
    fn = 412.3
    rows = [(1.0 / (fn ** 2 - f ** 2), f) for f in (380, 395, 405, 418, 430, 445)]
    f_est = ac.locate_resonance(rows)
    assert abs(f_est - fn) < 1e-9, f_est
    # no sign flip -> None, never a fabricated number
    assert ac.locate_resonance([(1.0, 100.0), (2.0, 110.0)]) is None
    # parsers return None on absent files, never raise
    assert ac.parse_helmholtz_duct(tempfile.mkdtemp()) is None
    assert ac.parse_helmholtz_cavity(tempfile.mkdtemp()) is None


# --- live solver gates (skip when ElmerSolver absent) ----------------------------

def test_duct_standing_wave_matches_exact():
    if skip_heavy("Elmer acoustic FEM"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    d = tempfile.mkdtemp(prefix="acfem_duct_")
    built = ac.write_helmholtz_duct_case(d)
    proc = _run_elmer(d, built["sif"])
    assert proc.returncode == 0, proc.stdout[-500:]
    parsed = ac.parse_helmholtz_duct(d, built["scalars"])
    assert parsed is not None, "no SaveScalars output"
    end_ratio = parsed["p_end_re"] / built["p_end_exact"]
    mean_ratio = parsed["p_mean_re"] / built["p_mean_exact"]
    assert abs(end_ratio - 1) < 0.005, end_ratio     # machine-tight in practice
    assert abs(mean_ratio - 1) < 0.01, mean_ratio


def test_cavity_oblique_mode_matches_exact():
    if skip_heavy("Elmer acoustic FEM"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    d = tempfile.mkdtemp(prefix="acfem_cav_")
    built = ac.write_helmholtz_cavity_case(d, mode_nx=1, mode_ny=1)
    proc = _run_elmer(d, built["sif"])
    assert proc.returncode == 0, proc.stdout[-500:]
    rows = ac.parse_helmholtz_cavity(d, built["scalars"])
    assert rows and len(rows) >= 5, rows
    f_est = ac.locate_resonance(rows)
    assert f_est is not None, "no resonance sign-flip captured"
    ratio = f_est / built["f_exact_hz"]
    assert abs(ratio - 1) < 0.005, (f_est, built["f_exact_hz"])


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
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

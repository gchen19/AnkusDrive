"""Harmonic forced response (Elmer StressSolve, Harmonic Analysis) — case
generation always, solver gate when ElmerSolver is present (SIMULATION_NEXT
Tier B2).

Three gates from the in-phase response of a tip-driven plane-stress cantilever:
Re(H) = 0 exactly at resonance (-> f1 vs the Euler-Bernoulli closed form), the
quasi-static point vs F*L^3/(3EI), and max|Re|/static vs Q/2 = 1/(4*zeta).

Run:  python3 tests/test_harmonic_fem.py
"""
import math
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import solvers                       # noqa: E402
from ankusdrive.analysis import vibration as vib     # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402


# --- case generation (no solver needed) -----------------------------------------

def test_beam_case_structure():
    d = tempfile.mkdtemp(prefix="frf_gen_")
    built = vib.write_harmonic_beam_case(d, nx=20, ny=2)
    sif = (Path(d) / built["sif"]).read_text(encoding="utf-8")
    assert "StressSolve" in sif and "Harmonic Analysis = True" in sif
    assert "Plane Stress = True" in sif and "Rayleigh Damping Beta" in sif
    # Rayleigh beta tuned to give zeta at f1: beta = 2*zeta/omega_1
    beta = 2 * built["damping_ratio"] / (2 * math.pi * built["f1_eb_hz"])
    assert f"{beta:.10g}" in sif, beta
    # first sweep point is quasi-static, the rest straddle f1
    freqs = built["freqs"]
    assert freqs[0] < built["f1_eb_hz"] / 20
    assert freqs[1] < built["f1_eb_hz"] < freqs[-1]
    # the exact static compliance rides along: F*L^3/(3EI) per unit depth
    i_area = 0.01 ** 3 / 12
    expected = (1000.0 * 0.01) * 0.2 ** 3 / (3 * 200e9 * i_area)
    assert abs(built["static_exact_m"] / expected - 1) < 1e-12
    for bad in (
        lambda: vib.write_harmonic_beam_case(tempfile.mkdtemp(), damping_ratio=0.5),
        lambda: vib.write_harmonic_beam_case(tempfile.mkdtemp(), nx=4),
        lambda: vib.write_harmonic_beam_case(tempfile.mkdtemp(), n_sweep=3),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_frf_locator_on_synthetic_sdof():
    # signed Re(H) of an SDOF: (1-r^2)/((1-r^2)^2 + (2 z r)^2) — zero exactly at
    # fn for ANY damping; the locator must recover it from a coarse sweep
    fn, z = 187.4, 0.03
    def re_h(f):
        r = f / fn
        return (1 - r * r) / ((1 - r * r) ** 2 + (2 * z * r) ** 2)
    freqs = [fn * (0.9 + 0.02 * i) for i in range(11)]
    f_est = vib.locate_frf_resonance(freqs, [re_h(f) for f in freqs])
    assert abs(f_est / fn - 1) < 1e-4, f_est
    # no flip captured -> None
    assert vib.locate_frf_resonance([100, 110], [1.0, 2.0]) is None
    # vtu parser: absent files -> None, never raises
    assert vib.parse_harmonic_beam(tempfile.mkdtemp(), n_steps=3, tip_node=0) is None


# --- live solver gate (skip when ElmerSolver absent) -----------------------------

def test_frf_sweep_matches_sdof_oracle():
    if skip_heavy("Elmer harmonic FEM"):
        return
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    d = tempfile.mkdtemp(prefix="frf_live_")
    built = vib.write_harmonic_beam_case(d)
    binary = solvers.find_solver("elmer")["path"]
    proc = subprocess.run([binary, built["sif"]], cwd=d,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-500:]
    tips = vib.parse_harmonic_beam(d, n_steps=built["n_steps"],
                                   tip_node=built["tip_node"])
    assert tips is not None and len(tips) == built["n_steps"], tips
    # gate 1: quasi-static point vs the exact tip compliance F*L^3/(3EI)
    static = abs(tips[0])
    assert abs(static / built["static_exact_m"] - 1) < 0.08, static
    # gate 2: resonance location vs Euler-Bernoulli beam_modal closed form
    f_est = vib.locate_frf_resonance(built["freqs"][1:], tips[1:])
    assert f_est is not None, "no resonance sign-flip captured"
    assert abs(f_est / built["f1_eb_hz"] - 1) < 0.03, (f_est, built["f1_eb_hz"])
    # gate 3: max in-phase amplification vs the SDOF identity Q/2 = 1/(4*zeta)
    peak = max(abs(t) for t in tips[1:])
    q_ratio = (peak / static) / (built["q_factor"] / 2.0)
    assert abs(q_ratio - 1) < 0.2, q_ratio


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

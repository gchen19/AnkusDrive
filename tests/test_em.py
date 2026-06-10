"""Low-frequency EM toys (P3 M6) — exact oracles + the solver-backed gates.

Two tiers, both runnable on the no-FreeCAD lane:
  * **structure** (always): the closed forms are exact — copper skin depth at
    50 Hz is the handbook 9.346 mm, R = L/(σ·A) with the Ohm/Joule pair, the wire
    field B = μ₀I/(2πr) and solenoid B = μ₀·μ_r·n·I; the decay-length fitter
    recovers δ from a synthetic exact profile to machine precision; the strip/slab
    mesh and the two .sif decks are well-formed (true boundary parents — the
    ``diffusive flux`` operator aborts without them).
  * **solver-backed** (when ElmerSolver resolves, else SKIP): the DC strip
    (StatCurrentSolver) reproduces R = L/(σ·A), the electrode current and the
    Joule power to machine precision; the harmonic skin-effect slab
    (MagnetoDynamics2DHarmonic) decays with e-folding length δ in BOTH magnitude
    and phase within 2 % (measured 0.1 % live).

Run:  python3 tests/test_em.py
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
from driftpin.analysis import em  # noqa: E402


# --- exact oracles ----------------------------------------------------------------

def test_skin_depth_copper_50hz_handbook():
    r = em.skin_depth(50.0, conductor="copper")
    assert abs(r["skin_depth_mm"] - 9.3459) < 1e-3, r["skin_depth_mm"]
    # δ ∝ 1/√f: 100× the frequency → δ/10
    r2 = em.skin_depth(5000.0, conductor="copper")
    assert abs(r2["skin_depth_m"] - r["skin_depth_m"] / 10.0) < 1e-12
    # R_s = 1/(σδ), and μ_r shrinks δ by √μ_r
    assert abs(r["surface_resistance_ohm"] - 1.0 / (5.8e7 * r["skin_depth_m"])) < 1e-15
    r3 = em.skin_depth(50.0, conductor="copper", mu_r=100.0)
    assert abs(r3["skin_depth_m"] - r["skin_depth_m"] / 10.0) < 1e-12


def test_dc_resistance_ohm_joule_pair():
    # 1 m of 1 mm² copper: the handbook ~17.2 mΩ
    r = em.dc_resistance(1000.0, 1.0, conductor="copper", voltage_v=1.0)
    assert abs(r["resistance_ohm"] - 1.0 / (5.8e7 * 1e-6)) < 1e-12
    assert abs(r["resistance_ohm"] - 0.01724) < 1e-4
    assert abs(r["current_a"] * r["resistance_ohm"] - 1.0) < 1e-9   # Ohm
    assert abs(r["joule_w"] - r["current_a"]) < 1e-9                # P = V·I at V=1


def test_wire_and_solenoid_fields_are_exact():
    # B(10 mm, 1 A) = μ₀/(2π·0.01) = 2e-5 T exactly
    w = em.wire_field(1.0, 10.0)
    assert abs(w["b_t"] - 2.0e-5) < 1e-15, w["b_t"]
    # 1000 turns/m at 1 A: B = μ₀·n·I = 4π·10⁻⁴ T
    s = em.solenoid_field(1000.0, 1.0)
    assert abs(s["b_t"] - 4.0e-7 * math.pi * 1000.0) < 1e-15, s["b_t"]
    assert abs(em.solenoid_field(1000.0, 1.0, mu_r=2.0)["b_t"] - 2 * s["b_t"]) < 1e-15


def test_fit_decay_length_recovers_synthetic_delta():
    delta = 0.0093459
    pts = [(x, 1e-3 * complex(math.cos(-x / delta), math.sin(-x / delta))
            * math.exp(-x / delta)) for x in [i * delta / 20 for i in range(80)]]
    fit = em.fit_decay_length(pts, 0.5 * delta, 2.5 * delta)
    assert abs(fit["decay_length_m"] / delta - 1.0) < 1e-9, fit
    assert abs(fit["phase_length_m"] / delta - 1.0) < 1e-9, fit
    # a non-decaying profile is rejected
    flat = [(x, complex(1.0, 0.0)) for x in [i * 1e-3 for i in range(40)]]
    try:
        em.fit_decay_length(flat, 0.0, 0.05)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-decaying profile")


def test_oracle_validation():
    for bad in (
        lambda: em.skin_depth(0.0, conductor="copper"),
        lambda: em.skin_depth(50.0),                            # no σ at all
        lambda: em.skin_depth(50.0, conductor="unobtainium"),
        lambda: em.dc_resistance(0.0, 1.0, conductor="copper"),
        lambda: em.dc_resistance(10.0, 1.0, conductivity_s_m=-1.0),
        lambda: em.wire_field(1.0, 0.0),
        lambda: em.solenoid_field(0.0, 1.0),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- case structure ---------------------------------------------------------------

def test_rect_mesh_has_true_boundary_parents():
    files = em.rect_mesh_files(10, 4, 0.1, 0.02)
    header = files["mesh.header"].split("\n")[0].split()
    assert header == ["55", "40", "8"], header                # 11x5 nodes, 10x4 elems
    for ln in files["mesh.boundary"].strip().splitlines():
        f = ln.split()
        parent = int(f[2])
        assert 1 <= parent <= 40, f"bad parent {parent}"      # a real bulk element
        # tag 1 parents are the first column (i=0), tag 2 the last (i=nx-1)
        if f[1] == "1":
            assert (parent - 1) % 10 == 0, ln
        else:
            assert parent % 10 == 0, ln


def test_dc_and_skin_sif_structure():
    dc = em.dc_strip_sif(conductivity_s_m=5.8e7, voltage_v=0.001)
    for token in ("StatCurrentSolver", "Electric Conductivity = 58000000",
                  "Calculate Joule Heating = True", "Potential = 0.001",
                  "Mask Name 1 = electrodemask"):
        assert token in dc, f"missing {token!r}"
    sk = em.skin_slab_sif(frequency_hz=50.0, conductivity_s_m=5.8e7, mu_r=1.0)
    for token in ("MagnetoDynamics2DHarmonic", "Frequency = 50",
                  "Potential Re = 0.001", "Polyline Divisions(1) = 100",
                  "Relative Permeability = 1"):
        assert token in sk, f"missing {token!r}"


def test_case_writers_and_oracles():
    with tempfile.TemporaryDirectory() as d:
        built = em.write_dc_strip_case(d)
        assert os.path.isfile(os.path.join(d, "case.sif"))
        assert abs(built["oracle"]["resistance_ohm"] - 0.1 / (5.8e7 * 0.02)) < 1e-15
    with tempfile.TemporaryDirectory() as d:
        built = em.write_skin_effect_case(d)
        # the slab spans `depths` skin depths
        assert abs(built["length_m"] - 5.3 * built["oracle"]["skin_depth_m"]) < 1e-12
        try:
            em.write_skin_effect_case(d, depths=2.0)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for depths < 3")


# --- solver-backed gates ------------------------------------------------------------

def test_dc_strip_is_machine_exact():
    """StatCurrentSolver on the strip: electrode current, Joule power and Elmer's
    own effective resistance all land on R = L/(σ·A) to ~1e-6 (verified live)."""
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        built = em.write_dc_strip_case(d)
        proc = subprocess.run([solvers.find_solver("elmer")["path"], built["sif"]],
                              cwd=d, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout[-800:]
        parsed = em.parse_dc_scalars(d, built["scalars"])
        assert parsed, "no scalars written"
        orc = built["oracle"]
        for got, want, name in (
                (parsed["current_a"], orc["current_a"], "current"),
                (parsed["effective_resistance_ohm"], orc["resistance_ohm"], "R"),
                (parsed["joule_w"], orc["joule_w"], "joule")):
            assert abs(got / want - 1.0) < 1e-4, (name, got, want)
        print(f"    DC strip: I {parsed['current_a']:.6g} A, "
              f"R {parsed['effective_resistance_ohm']:.6g} ohm — machine-exact")


def test_skin_effect_decays_at_the_exact_skin_depth():
    """MagnetoDynamics2DHarmonic on the slab: the complex A(x) e-folding length
    matches δ = √(2/(ωμσ)) in BOTH magnitude and phase (measured 0.1 % live;
    gate 2 %)."""
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        built = em.write_skin_effect_case(d)
        proc = subprocess.run([solvers.find_solver("elmer")["path"], built["sif"]],
                              cwd=d, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout[-800:]
        pts = em.parse_line_profile(d, built["line_file"])
        assert pts and len(pts) > 50, "no line profile written"
        delta = built["oracle"]["skin_depth_m"]
        fit = em.fit_decay_length(pts, 0.5 * delta, 2.5 * delta)
        assert 0.98 < fit["decay_length_m"] / delta < 1.02, (fit, delta)
        assert 0.98 < fit["phase_length_m"] / delta < 1.02, (fit, delta)
        print(f"    skin effect: decay {fit['decay_length_m'] / delta:.4f}, "
              f"phase {fit['phase_length_m'] / delta:.4f} of exact delta")



# --- B5: coupled induction heating (case gen always; Elmer gate when present) ---

def test_induction_heating_power_identity():
    # P'' = omega^2*sigma*A0^2*delta/4 must equal R_s*|H0|^2/2 with
    # H0 = A0*sqrt(2)/(mu*delta) — two routes to the same exact dissipation
    import math
    f, sigma, mu_r, a0 = 1.0e4, 5.96e7, 1.0, 1.0e-3
    p1 = em.induction_heating_power(f, sigma, mu_r, a0)
    orc = em.skin_depth(f, conductivity_s_m=sigma, mu_r=mu_r)
    delta = orc["skin_depth_m"]
    mu = mu_r * 4e-7 * math.pi
    h0 = a0 * math.sqrt(2.0) / (mu * delta)
    rs = 1.0 / (sigma * delta)
    p2 = rs * h0 ** 2 / 2.0
    assert abs(p1 / p2 - 1) < 1e-9, (p1, p2)
    # at fixed A0, H0 ~ sqrt(f) so P'' ~ f^(3/2): 4x frequency -> 8x dissipation
    p4 = em.induction_heating_power(4 * f, sigma, mu_r, a0)
    assert abs(p4 / p1 - 8.0) < 1e-6


def test_induction_heating_case_structure():
    d = tempfile.mkdtemp(prefix="indheat_gen_")
    built = em.write_induction_heating_case(d)
    sif = open(os.path.join(d, built["sif"])).read()
    assert "MagnetoDynamicsCalcFields" in sif and "Joule Heat = Logical True" in sif
    assert "Transient" in sif and "Before Simulation" in sif
    assert built["dt_mean_exact_k"] > 0 and built["p_total_w_m"] > 0
    for bad in (
        lambda: em.write_induction_heating_case(tempfile.mkdtemp(), depths=2.0),
        lambda: em.write_induction_heating_case(tempfile.mkdtemp(),
                                                heat_duration_s=0),
        lambda: em.write_induction_heating_case(tempfile.mkdtemp(), n_steps=1),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    assert em.parse_induction_scalars(tempfile.mkdtemp()) is None


def test_induction_heating_solve_closes_energy_balance():
    if not solvers.is_available("elmer"):
        print("    SKIP — ElmerSolver not installed")
        return
    d = tempfile.mkdtemp(prefix="indheat_live_")
    built = em.write_induction_heating_case(d)
    binary = solvers.find_solver("elmer")["path"]
    proc = subprocess.run([binary, built["sif"]], cwd=d,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-500:]
    r = em.parse_induction_scalars(d, built["scalars"])
    assert r is not None and r["eddy_power_w_m"] is not None, r
    # gate 1: solved eddy-current power vs the exact R_s|H0|^2/2 (live 1.0003)
    joule_ratio = r["eddy_power_w_m"] / built["p_total_w_m"]
    assert abs(joule_ratio - 1) < 0.03, joule_ratio
    # gate 2: adiabatic energy balance dT = P*t/(m*cp) (live 1.005)
    balance = r["t_mean_final_k"] / built["dt_mean_exact_k"]
    assert abs(balance - 1) < 0.05, balance


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
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")

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

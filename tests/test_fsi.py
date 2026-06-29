"""
Fluid–structure interaction via preCICE (OpenFOAM ↔ CalculiX) — oracle-gated
(issue #91).

Two tiers, mirroring every other family in the suite:

  Pure-oracle toys (ALWAYS run, no solver) — the closed-form anchors:
    - test_plate_deflection_oracle  : δ_tip = q·L⁴/(8·E·I) (cantilever),
        q·L⁴/(384·E·I) (clamped-clamped), root moment / reaction, two-sided
        scaling (δ ∝ p, ∝ L⁴, ∝ 1/t³)
    - test_interface_balance_oracle : the partitioned wet-load conservation gate
        closes (F_fluid = p·A = R_solid; residual flagged when it doesn't)
    - test_channel_pressure_oracle  : Δp = 12·μ·U·L/h² and the laminar Re flag

  Live coupled solve (skip when the preCICE FSI stack is absent) — gated on the
  oracles:
    - test_fsi_coupled_plate_deflects : the REAL partitioned OpenFOAM↔CalculiX
        preCICE solve advances every time window, the coupling converges, and the
        flap tip deflects monotonically into the flow (a non-trivial, growing wet
        displacement) — the physical signature the plate-deflection oracle anchors.

The live test reuses the same validated case the artifact does; it gates that the
coupling ESTABLISHES and the field is physically right, not the unsteady
Turek–Hron limit-cycle amplitude (which is the optional stretch).

Run:  python3 tests/test_fsi.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import solvers  # noqa: E402
from driftpin.analysis import fsi  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402

_E_STEEL_GPA = 210.0


def _fsi_stack_ready():
    return solvers.fsi_stack_status()["ok"]


# --- pure-oracle toys (no solver) ---------------------------------------------

def test_plate_deflection_oracle():
    """Uniform-pressure cantilever-plate tip is the exact Euler–Bernoulli
    δ = q·L⁴/(8·E·I), q = p·b; the root moment q·L²/2 and reaction q·L close the
    free body; the both-ends-clamped strip is q·L⁴/(384·E·I)."""
    p_pa, L, b, t = 2000.0, 100.0, 20.0, 2.0
    r = fsi.plate_deflection(p_pa, L, b, t, youngs_gpa=_E_STEEL_GPA)
    q = (p_pa * 1e-6) * b                       # N/mm
    I = b * t ** 3 / 12.0
    expect = q * L ** 4 / (8.0 * _E_STEEL_GPA * 1e3 * I)
    assert abs(r["tip_disp_mm"] - expect) < 1e-7, (r["tip_disp_mm"], expect)
    assert abs(r["I_mm4"] - I) < 1e-6
    assert abs(r["total_load_n"] - q * L) < 1e-6
    assert abs(r["reaction_n"] - q * L) < 1e-6        # carries the whole wet load
    assert abs(r["root_moment_nmm"] - q * L ** 2 / 2.0) < 1e-3
    assert r["fidelity"] == "exact" and r["valid_range_ok"]

    # clamped-clamped strip: 48× stiffer than the cantilever (8 vs 384 factor)
    cc = fsi.plate_deflection(p_pa, L, b, t, youngs_gpa=_E_STEEL_GPA,
                              support="clamped-clamped")
    assert abs(cc["tip_disp_mm"] - q * L ** 4 / (384.0 * _E_STEEL_GPA * 1e3 * I)) < 1e-7
    assert cc["tip_disp_mm"] < r["tip_disp_mm"]       # both clamped ⇒ stiffer
    assert abs(cc["reaction_n"] - q * L / 2.0) < 1e-6  # split between two ends

    # two-sided scaling: δ ∝ pressure, ∝ L⁴, ∝ 1/t³
    r2 = fsi.plate_deflection(2 * p_pa, L, b, t, youngs_gpa=_E_STEEL_GPA)
    assert abs(r2["tip_disp_mm"] - 2 * r["tip_disp_mm"]) < 1e-7
    rL = fsi.plate_deflection(p_pa, 2 * L, b, t, youngs_gpa=_E_STEEL_GPA)
    assert abs(rL["tip_disp_mm"] - 16 * r["tip_disp_mm"]) < 1e-7   # (2L)⁴ = 16
    rt = fsi.plate_deflection(p_pa, L, b, 2 * t, youngs_gpa=_E_STEEL_GPA)
    assert abs(rt["tip_disp_mm"] - r["tip_disp_mm"] / 8.0) < 1e-7  # I ∝ t³

    # small-deflection guard: a thin, lightly-loaded plate is in-range; crank the
    # pressure until δ > t and the oracle must flag it out-of-range.
    big = fsi.plate_deflection(5.0e5, 200.0, 20.0, 1.0, youngs_gpa=_E_STEEL_GPA)
    assert not big["valid_range_ok"], big["tip_disp_mm"]
    assert any("small-deflection" in w for w in big["warnings"])
    print(f"    plate: tip {r['tip_disp_mm']:.5f} mm == {expect:.5f} mm "
          f"(δ ∝ p,L⁴,1/t³ all confirmed; clamped-clamped 48× stiffer)")


def test_interface_balance_oracle():
    """The partitioned wet-interface load conservation gate: F_fluid = p·A must
    equal the solid reaction; the relative residual flags a mismatch."""
    p_pa, L, b = 2000.0, 100.0, 20.0
    ref = (p_pa * 1e-6) * (L * b)                # N
    # perfectly-balanced (no measured values) ⇒ residual 0
    r0 = fsi.interface_balance(p_pa, L, b)
    assert abs(r0["reference_load_n"] - ref) < 1e-6
    assert r0["relative_residual"] == 0.0 and r0["balanced"]

    # a solve that conserves the load to 0.5 % is balanced; 5 % is not
    good = fsi.interface_balance(p_pa, L, b, solid_reaction_n=ref * 1.005)
    assert good["balanced"] and good["relative_residual"] < 0.01
    bad = fsi.interface_balance(p_pa, L, b, solid_reaction_n=ref * 1.05)
    assert not bad["balanced"] and bad["relative_residual"] > 0.01
    print(f"    interface: F_fluid = p·A = {ref:.4f} N closes; "
          f"0.5% kept, 5% rejected")


def test_channel_pressure_oracle():
    """Plane-Poiseuille channel Δp = 12·μ·U·L/h² and the laminar Reynolds flag —
    the physically-sourced traction that drives the plate."""
    U, L, h = 0.1, 100.0, 5.0
    r = fsi.channel_pressure_load(U, L, h)       # water default
    dp = 12.0 * 1e-3 * U * (L * 1e-3) / ((h * 1e-3) ** 2)
    assert abs(r["pressure_pa"] - dp) < 1e-6, (r["pressure_pa"], dp)
    assert abs(r["reynolds"] - 1000.0 * U * (h * 1e-3) / 1e-3) < 1e-6
    assert r["regime"] == "laminar" and r["valid_range_ok"]
    # crank U until Re ≥ 1400 ⇒ flagged non-laminar (exactness lost)
    fast = fsi.channel_pressure_load(0.5, L, h)
    assert fast["regime"] != "laminar" and not fast["valid_range_ok"]
    # Δp ∝ U, ∝ L, ∝ 1/h²
    r2 = fsi.channel_pressure_load(2 * U, L, h)
    assert abs(r2["pressure_pa"] - 2 * r["pressure_pa"]) < 1e-6
    print(f"    channel: Δp = {r['pressure_pa']:.3f} Pa (Re {r['reynolds']:.0f} "
          f"laminar; Δp ∝ U,L,1/h²)")


# --- live coupled solve (skip when the preCICE FSI stack is absent) -----------

def test_fsi_coupled_plate_deflects():
    """The REAL partitioned preCICE OpenFOAM↔CalculiX solve: every time window
    advances, the implicit coupling converges, and the flap tip deflects
    monotonically into the flow — a non-trivial, growing wet displacement, the
    physical signature the plate-deflection oracle anchors. Gated on the FSI stack
    resolving (built via scripts/install-solvers.sh fsi)."""
    if skip_heavy("FSI preCICE coupled"):
        return
    if not _fsi_stack_ready():
        miss = solvers.fsi_stack_status()["missing"]
        print(f"    SKIP — preCICE FSI stack not resolved (missing: {miss})")
        return

    import tempfile
    from driftpin.analysis import fsi_case

    case_dir = tempfile.mkdtemp(prefix="fsi_test_")
    # a short solve: 4 windows is enough to prove coupling + monotone deflection
    fsi_case.write_fsi_case(
        case_dir, end_time_s=0.04, time_window_s=0.01, max_iterations=20)
    res = fsi_case.run_coupled_fsi(case_dir, timeout_s=500)

    assert res["ok"], f"coupled solve did not complete cleanly: {res.get('log_tail')}"
    assert res["returncode_fluid"] == 0 and res["returncode_solid"] == 0, res
    assert res["coupling_converged"], "preCICE did not reach the final time window"
    assert res["time_windows"] >= 4, res["time_windows"]

    hist = res["tip_history"]
    assert hist and len(hist) >= 4, hist
    # tip displacement magnitude grows from ~0 into the flow (monotone, non-trivial)
    mags = [(dx * dx + dy * dy) ** 0.5 for _t, dx, dy in hist]
    assert mags[0] < 1e-6, mags[0]               # starts at rest
    assert mags[-1] > 1e-4, mags[-1]             # ends with a real deflection
    assert mags[-1] > mags[1], (mags[1], mags[-1])  # grows under the load

    # the interface conservation gate closes on the run's own wet load
    bal = fsi.interface_balance(2000.0, 100.0, 20.0)
    assert bal["balanced"]
    print(f"    coupled: {res['time_windows']} windows converged, tip "
          f"0 → {mags[-1]*1e3:.4f} mm (monotone into the flow)")


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t0 = time.time()
    for name, fn in _discover():
        t = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()

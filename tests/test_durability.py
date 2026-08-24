"""Wear / fatigue / fracture toys — two-sided oracles for
ankusdrive.analysis.durability.

Pure-Python, no FreeCAD. Each check pins a closed-form result against a hand
calculation AND verifies a deliberately-bad input is caught, mirroring
tests/TOYS.md and docs/SIMULATION_EXAMPLES.md (family 3).

Run:  python3 tests/test_durability.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import durability as dur  # noqa: E402


def test_fatigue_goodman_fails():
    # AL6061-T6 (σ_e=96, σ_uts=310); range 180 -> σ_a=90, σ_m=40
    # Goodman SF = 1/(90/96 + 40/310) = 1/1.0665 = 0.938 -> fail
    r = dur.fatigue_check(stress_range_mpa=180, mean_stress_mpa=40,
                          cycles=1_000_000, material="AL6061-T6")
    assert abs(r["safety_factor"] - 0.938) < 0.01, r["safety_factor"]
    assert r["pass"] is False
    assert r["governing_mode"] == "goodman_mean_stress", r["governing_mode"]
    # finite life in the expected band (S-N to ~6e5 cycles)
    assert 4e5 < r["life_cycles"] < 9e5, r["life_cycles"]


def test_fatigue_crosses_to_pass_when_load_drops():
    # drop the load 20% -> σ_ar falls below the endurance limit -> infinite life
    r = dur.fatigue_check(stress_range_mpa=144, mean_stress_mpa=32,
                          cycles=1_000_000, material="AL6061-T6")
    assert r["safety_factor"] > 1.0, r["safety_factor"]
    assert r["pass"] is True
    assert r["governing_mode"] == "infinite_life", r["governing_mode"]
    assert r["life_cycles"] is None  # infinite life reports no finite N


def test_fatigue_static_overload_is_caught():
    # tensile mean above UTS must fail regardless of cycle count
    r = dur.fatigue_check(stress_range_mpa=10, mean_stress_mpa=320,
                          cycles=1, material="AL6061-T6")
    assert r["pass"] is False
    assert r["governing_mode"] == "static_overload", r["governing_mode"]
    # missing strength data with no override raises (no silent default)
    try:
        dur.fatigue_check(stress_range_mpa=100, material="NoSuchAlloy")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without a usable UTS")


def test_fatigue_endurance_estimated_when_absent():
    # AL1100-O has no fatigue_endurance -> 0.4*UTS fallback, flagged in basis
    r = dur.fatigue_check(stress_range_mpa=20, mean_stress_mpa=0, material="AL1100-O")
    assert r["endurance_basis"] == "estimated_0.4_uts", r["endurance_basis"]
    assert abs(r["endurance_mpa"] - 0.4 * 90) < 0.5, r["endurance_mpa"]


def test_fracture_lefm_pass():
    # σ=150 MPa, a=2 mm, Y=1.12 -> K = 1.12*150*sqrt(pi*0.002) = 13.32 MPa*sqrt(m)
    # AL6061 K_IC=29 -> SF=2.18; a_c = (29/(1.12*150))^2/pi = 9.48 mm
    r = dur.fracture_check(stress_mpa=150, crack_len_mm=2.0, material="AL6061-T6")
    assert abs(r["k_applied_mpa_sqrt_m"] - 13.32) < 0.1, r["k_applied_mpa_sqrt_m"]
    assert abs(r["safety_factor"] - 2.18) < 0.03, r["safety_factor"]
    assert abs(r["critical_crack_mm"] - 9.48) < 0.1, r["critical_crack_mm"]
    assert r["pass"] is True and r["margin"] > 0


def test_fracture_past_critical_crack_is_caught():
    # a crack longer than a_c (9.48 mm) must report SF<1 and a negative margin
    r = dur.fracture_check(stress_mpa=150, crack_len_mm=12.0, material="AL6061-T6")
    assert r["pass"] is False
    assert r["safety_factor"] < 1.0, r["safety_factor"]
    assert r["margin"] < 0, r["margin"]
    # crack past a_c is consistent: K_applied > K_IC
    assert r["k_applied_mpa_sqrt_m"] > r["k_ic_mpa_sqrt_m"]
    # no toughness data and no override -> raise
    try:
        dur.fracture_check(stress_mpa=50, crack_len_mm=1.0, material="ABS")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without K_IC")


def test_wear_archard_closed_form():
    # V = k*F*s/H = 1e-4 * 200 * 5000 / 2.2e9 m^3 = 45.45 mm^3
    r = dur.wear_estimate(load_n=200, sliding_dist_m=5000,
                          wear_coef=1e-4, hardness_mpa=2200)
    assert abs(r["volume_loss_mm3"] - 45.45) < 0.5, r["volume_loss_mm3"]
    assert r["coef_basis"] == "explicit"
    # depth from apparent area, gated against a limit
    r2 = dur.wear_estimate(200, 5000, wear_coef=1e-4, hardness_mpa=2200,
                           apparent_area_mm2=500, max_depth_mm=0.05)
    assert abs(r2["depth_loss_mm"] - 0.0909) < 1e-3, r2["depth_loss_mm"]
    assert r2["pass"] is False  # 0.091 mm > 0.05 mm limit


def test_wear_is_linear_and_pair_resolves():
    # Archard is linear in load: double F -> double V (loads sized so 4-dp
    # rounding is negligible against ~40 mm^3 volumes)
    a = dur.wear_estimate(1000, 1000, material_pair=["Steel-1045", "Nylon-6/6"])
    b = dur.wear_estimate(2000, 1000, material_pair=["Steel-1045", "Nylon-6/6"])
    assert abs(b["volume_loss_mm3"] - 2 * a["volume_loss_mm3"]) < 0.01
    # hardness from Tabor 3*min(yield) = 3*82 = 246 MPa; k from steel+polymer table
    assert abs(a["hardness_mpa"] - 246.0) < 0.5, a["hardness_mpa"]
    assert a["hardness_basis"] == "tabor_3x_yield"
    assert a["coef_basis"].startswith("category_pair")


def test_creep_screen_two_sided():
    # AL6061-T6 service limit 170 C: below passes, above flags
    ok = dur.creep_flag(stress_mpa=50, temp_c=150, material="AL6061-T6")
    assert ok["pass"] is True and ok["creep_risk"] is False
    assert abs(ok["margin_c"] - 20.0) < 1e-6, ok["margin_c"]
    hot = dur.creep_flag(stress_mpa=50, temp_c=200, material="AL6061-T6")
    assert hot["pass"] is False and hot["creep_risk"] is True
    # no service-temp data, no override -> raise
    try:
        dur.creep_flag(stress_mpa=10, temp_c=100, material="NoSuchAlloy")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without a service temperature")


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

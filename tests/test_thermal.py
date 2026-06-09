"""Lumped transient thermal toys — two-sided oracles for
driftpin.analysis.thermal.

Pure-Python, no FreeCAD. Pins the first-order RC response against the exact
exponential AND checks the degenerate/limit cases, mirroring tests/TOYS.md and
docs/SIMULATION_EXAMPLES.md (family 4).

Run:  python3 tests/test_thermal.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import thermal as th  # noqa: E402


def test_rc_warmup_matches_exact_exponential():
    # §4 toy: m=120 g, c_p=900, P=15 W, h=12, A=20000 mm^2, T_amb=25, t=300 s
    # dT_ss=62.5 K -> t_steady=87.5; tau=450 s; T(300)=55.4; reached 48.7%
    r = th.thermal_lumped(mass_g=120, power_w=15, h_conv=12, area_mm2=20000,
                          c_p="900 J/kg/K", t_ambient_c=25, duration_s=300)
    assert abs(r["delta_t_steady_k"] - 62.5) < 0.1, r["delta_t_steady_k"]
    assert abs(r["t_steady_c"] - 87.5) < 0.1, r["t_steady_c"]
    assert abs(r["time_constant_s"] - 450.0) < 0.01, r["time_constant_s"]
    assert abs(r["t_final_c"] - 55.4) < 0.1, r["t_final_c"]
    assert abs(r["reached_steady_pct"] - 48.7) < 0.2, r["reached_steady_pct"]


def test_one_time_constant_is_63_percent():
    # at t = tau the body must reach T_amb + 0.632*dT_ss (the RC signature)
    r = th.thermal_lumped(mass_g=120, power_w=15, h_conv=12, area_mm2=20000,
                          c_p=900, t_ambient_c=25, duration_s=450)
    expected = 25 + 0.63212 * 62.5
    assert abs(r["t_final_c"] - expected) < 0.05, (r["t_final_c"], expected)
    assert abs(r["reached_steady_pct"] - 63.2) < 0.2, r["reached_steady_pct"]


def test_limits_zero_power_and_long_time():
    # zero power -> stays at ambient, no spurious rise
    z = th.thermal_lumped(mass_g=120, power_w=0, h_conv=12, area_mm2=20000,
                          c_p=900, t_ambient_c=25, duration_s=1000)
    assert abs(z["t_steady_c"] - 25.0) < 1e-9, z["t_steady_c"]
    assert abs(z["t_final_c"] - 25.0) < 1e-9, z["t_final_c"]
    # duration >> 5*tau -> converges to steady (no numerical overshoot)
    long = th.thermal_lumped(mass_g=120, power_w=15, h_conv=12, area_mm2=20000,
                             c_p=900, t_ambient_c=25, duration_s=10 * 450)
    assert abs(long["t_final_c"] - long["t_steady_c"]) < 0.05, long


def test_radiation_screen_two_sided():
    # the §4 case (87.5 C, h=12) must NOT flag radiation as significant...
    cool = th.thermal_lumped(mass_g=120, power_w=15, h_conv=12, area_mm2=20000,
                             c_p=900, t_ambient_c=25)
    assert cool["radiation_significant"] is False, cool["h_rad_w_m2k"]
    # ...but a hot body with weak convection (T_steady>150 C, low h) must flip it
    hot = th.thermal_lumped(mass_g=120, power_w=80, h_conv=5, area_mm2=20000,
                            c_p=900, t_ambient_c=25)
    assert hot["t_steady_c"] > 150, hot["t_steady_c"]
    assert hot["radiation_significant"] is True, hot["h_rad_w_m2k"]


def test_specific_heat_from_material_and_errors():
    # c_p can come from the Materials DB (AL6061-T6 specific_heat = 896 J/kg/K)
    r = th.thermal_lumped(mass_g=100, power_w=10, h_conv=10, area_mm2=10000,
                          material="AL6061-T6", duration_s=100)
    # tau = m*cp/(h*A) = 0.1*896/(10*0.01) = 896 s
    assert abs(r["time_constant_s"] - 896.0) < 1.0, r["time_constant_s"]
    # neither c_p nor a usable material -> raise
    try:
        th.thermal_lumped(mass_g=100, power_w=10, h_conv=10, area_mm2=10000)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without c_p or material")
    # zero area -> raise (no divide-by-zero)
    try:
        th.thermal_lumped(mass_g=100, power_w=10, h_conv=10, area_mm2=0, c_p=900)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for zero area")


def test_transient_1d_matches_heisler_one_term():
    # Bi=1, Fo=1 (L=0.1 m, k=10, h=100, alpha=1e-4, t=100 s): center 65.0 C
    r = th.thermal_transient_1d(half_thickness_mm=100, h_conv=100, duration_s=100,
                                k=10, alpha_m2_s=1e-4, t_initial_c=100, t_ambient_c=25)
    assert abs(r["biot"] - 1.0) < 1e-6 and abs(r["fourier"] - 1.0) < 1e-6, r
    assert abs(r["eigenvalue_1"] - 0.8603) < 1e-3, r["eigenvalue_1"]
    assert abs(r["t_center_c"] - 65.0) < 0.2, r["t_center_c"]
    assert r["t_surface_c"] < r["t_center_c"], r       # surface leads the center
    assert r["one_term_valid"] is True, r


def test_transient_1d_agrees_with_lumped_at_small_biot():
    # thin, high-conductivity slab -> isothermal -> the lumped exponential
    s = th.thermal_transient_1d(half_thickness_mm=5, h_conv=20, duration_s=200,
                                material="AL6061-T6", t_initial_c=100, t_ambient_c=25)
    assert s["biot"] < 0.01, s["biot"]
    assert abs(s["t_center_c"] - s["t_center_lumped_c"]) < 0.5, s
    assert s["lumped_agrees"] is True, s


def test_transient_1d_errors():
    # no properties resolvable
    try:
        th.thermal_transient_1d(half_thickness_mm=10, h_conv=10, duration_s=10)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without k/rho/cp or alpha")
    # non-positive geometry
    try:
        th.thermal_transient_1d(half_thickness_mm=0, h_conv=10, duration_s=10,
                                k=10, alpha_m2_s=1e-4)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for zero thickness")


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

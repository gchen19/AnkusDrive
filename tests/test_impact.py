"""Drop/impact screening toys — two-sided oracles for driftpin.analysis.impact.

Pure-Python, no FreeCAD. Pins the energy-balance identities: G = h/d, the
exact pulse-shape factors, v = sqrt(2gh), mass independence of G, and the
round-trip between the stroke and fragility forms.

Run:  python3 tests/test_impact.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import impact as im  # noqa: E402


def test_energy_balance_identity():
    # G_avg = h/d exactly (energy method): 1 m onto 10 mm of crush = 100 g
    r = im.drop_impact(1000, crush_distance_mm=10)
    assert abs(r["g_avg"] - 100.0) < 1e-9, r["g_avg"]
    assert r["fidelity"] == "exact" and r["band_pct"] is None, r


def test_impact_velocity_closed_form():
    # v = sqrt(2*g*h): 1 m -> 4.429 m/s, and v ~ sqrt(h) (4x height -> 2x v)
    one = im.drop_impact(1000, crush_distance_mm=10)
    four = im.drop_impact(4000, crush_distance_mm=10)
    assert abs(one["impact_velocity_m_s"] - math.sqrt(2 * 9.80665)) < 1e-3
    assert abs(four["impact_velocity_m_s"] / one["impact_velocity_m_s"] - 2.0) < 1e-3


def test_pulse_factors_exact():
    # same stroke, three pulse idealizations: peak/avg = 1, 2, pi/2 exactly
    kw = dict(drop_height_mm=1000, crush_distance_mm=10)
    const = im.drop_impact(pulse="constant", **kw)
    spring = im.drop_impact(pulse="linear_spring", **kw)
    sine = im.drop_impact(pulse="half_sine", **kw)
    assert const["g_peak"] == const["g_avg"], const
    assert abs(spring["g_peak"] / spring["g_avg"] - 2.0) < 1e-9, spring
    assert abs(sine["g_peak"] / sine["g_avg"] - math.pi / 2) < 1e-3, sine


def test_fragility_round_trip():
    # invert for the required crush at a 50 g limit, then run forward with that
    # stroke: the peak must land exactly back on the limit
    inv = im.drop_impact(1000, deceleration_limit_g=50, pulse="linear_spring")
    assert abs(inv["required_crush_mm"] - 40.0) < 1e-9, inv  # 2*1000/50
    fwd = im.drop_impact(1000, crush_distance_mm=inv["required_crush_mm"],
                         pulse="linear_spring")
    assert abs(fwd["g_peak"] - 50.0) < 1e-6, fwd["g_peak"]
    # gentler pulse (constant) needs HALF the stroke of the spring for the
    # same limit — the design lever, exactly 2x
    crumple = im.drop_impact(1000, deceleration_limit_g=50, pulse="constant")
    assert abs(inv["required_crush_mm"] / crumple["required_crush_mm"] - 2.0) < 1e-9


def test_mass_cancels_from_g_but_not_force():
    # G is mass-independent (energy AND force scale together); force is linear in m
    light = im.drop_impact(1000, crush_distance_mm=10, mass_g=100)
    heavy = im.drop_impact(1000, crush_distance_mm=10, mass_g=1000)
    assert light["g_peak"] == heavy["g_peak"], (light, heavy)
    assert abs(heavy["peak_force_n"] / light["peak_force_n"] - 10.0) < 1e-3
    assert abs(heavy["energy_j"] - 1.0 * 9.80665 * 1.0) < 1e-3, heavy["energy_j"]
    # without a mass the force/energy fields stay None (not zero)
    nomass = im.drop_impact(1000, crush_distance_mm=10)
    assert nomass["peak_force_n"] is None and nomass["energy_j"] is None


def test_pulse_duration_and_soft_landing_flag():
    # constant-force stroke time t = 2d/v: 10 mm at 4.429 m/s = 4.52 ms
    r = im.drop_impact(1000, crush_distance_mm=10)
    assert abs(r["pulse_duration_ms"] - 2 * 0.010 / r["impact_velocity_m_s"] * 1e3) < 1e-3
    # a crush stroke longer than the drop is suspicious — flagged, not silent
    soft = im.drop_impact(100, crush_distance_mm=200)
    assert soft["g_avg"] < 1.0 and soft["valid_range_ok"] is False, soft


def test_input_validation():
    for bad in (
        lambda: im.drop_impact(0, crush_distance_mm=10),
        lambda: im.drop_impact(1000),                              # neither mode
        lambda: im.drop_impact(1000, crush_distance_mm=10,
                               deceleration_limit_g=50),           # both modes
        lambda: im.drop_impact(1000, crush_distance_mm=0),
        lambda: im.drop_impact(1000, deceleration_limit_g=-5),
        lambda: im.drop_impact(1000, crush_distance_mm=10, pulse="square_wave"),
        lambda: im.drop_impact(1000, crush_distance_mm=10, mass_g=0),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


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

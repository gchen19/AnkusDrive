"""Design-for-Cost toys — two-sided oracles for driftpin.analysis.cost.

Pure-Python, no FreeCAD. Each check pins a closed-form / handbook cost against a
hand calculation AND verifies a deliberately-bad input is caught, mirroring
tests/TOYS.md and docs/SIMULATION_EXAMPLES.md (family 9).

Run:  python3 tests/test_cost.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import cost  # noqa: E402


def test_material_cost_closed_form():
    # AL6061-T6: density 2700 kg/m^3, price 4.5 USD/kg.
    # volume 1e6 mm^3 -> mass 1e6 * 1e-9 * 2700 = 2.7 kg
    # material_cost = 2.7 * 4.5 = 12.15 USD (the exact anchor)
    r = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    assert abs(r["mass_kg"] - 2.7) < 1e-6, r["mass_kg"]
    assert abs(r["material_cost"] - 12.15) < 1e-2, r["material_cost"]
    assert r["breakdown"]["density_basis"] == "material"
    assert r["breakdown"]["price_basis"] == "material"
    # ties to Materials DB §2: material_cost == volume * density * price / 1e9
    expect = 1e6 * 2700.0 * 4.5 * 1e-9
    assert abs(r["material_cost"] - expect) < 1e-2, (r["material_cost"], expect)


def test_scrap_fraction_scales_material_cost():
    # scrap adds (1+scrap_fraction) onto the buy-to-fly material cost
    base = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    scr = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", scrap_fraction=0.20)
    assert abs(scr["material_cost"] - 1.20 * base["material_cost"]) < 1e-2, scr["material_cost"]
    # deliberately-wrong: pretend scrap is free -> the inflated cost must NOT match base
    assert abs(scr["material_cost"] - base["material_cost"]) > 1e-2


def test_unit_cost_drops_with_quantity_monotonic():
    # With tooling + setup as fixed costs, amortizing over a larger lot must lower
    # the unit cost: 10000-off cheaper than 1-off.
    one = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                             tooling_usd=5000.0, quantity=1)
    many = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                              tooling_usd=5000.0, quantity=10000)
    assert many["unit_cost"] < one["unit_cost"], (one["unit_cost"], many["unit_cost"])
    # tooling amortizes exactly: 5000/1 vs 5000/10000
    assert abs(one["tooling_amortized"] - 5000.0) < 1e-6, one["tooling_amortized"]
    assert abs(many["tooling_amortized"] - 0.5) < 1e-6, many["tooling_amortized"]
    # at high volume the unit cost approaches the per-unit floor (material+machining)
    floor = one["material_cost"] + one["breakdown"]["machining_cost"]
    assert many["unit_cost"] - floor < 1.0, (many["unit_cost"], floor)
    # deliberately-wrong: cost must NOT be flat across quantity when tooling > 0
    assert one["unit_cost"] - many["unit_cost"] > 1.0


def test_unit_cost_is_sum_of_parts():
    r = cost.cost_estimate(volume_mm3=5e5, material="ABS", process="injection",
                           tooling_usd=8000.0, quantity=10000, setup_min=30.0)
    parts = r["material_cost"] + r["process_cost"] + r["tooling_amortized"]
    assert abs(r["unit_cost"] - parts) < 1e-3, (r["unit_cost"], parts)
    # process_cost is itself setup_amortized + machining_cost
    b = r["breakdown"]
    assert abs(r["process_cost"] - (b["setup_amortized"] + b["machining_cost"])) < 1e-3


def test_process_factor_orders_cnc_above_injection():
    # Same part: subtractive CNC must cost more machine time than net-shape injection.
    cnc = cost.cost_estimate(volume_mm3=1e6, material="ABS", process="cnc")
    inj = cost.cost_estimate(volume_mm3=1e6, material="ABS", process="injection")
    assert cnc["breakdown"]["machine_time_hr"] > inj["breakdown"]["machine_time_hr"]
    assert cnc["breakdown"]["machining_cost"] > inj["breakdown"]["machining_cost"]
    # an unknown process is caught (no silent default to cnc)
    try:
        cost.cost_estimate(volume_mm3=1e6, material="ABS", process="wishful")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on an unknown process")


def test_override_density_and_price():
    # explicit overrides bypass the DB and are flagged in the breakdown
    r = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                           density_kg_m3=1000.0, price_usd_kg=2.0)
    assert abs(r["mass_kg"] - 1.0) < 1e-6, r["mass_kg"]
    assert abs(r["material_cost"] - 2.0) < 1e-2, r["material_cost"]
    assert r["breakdown"]["density_basis"] == "explicit"
    assert r["breakdown"]["price_basis"] == "explicit"


def test_unknown_material_raises():
    # unknown material with no override -> ValueError (no silent default)
    try:
        cost.cost_estimate(volume_mm3=1e6, material="NoSuchAlloy")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown material")
    # an override on one missing property still needs the other -> raise
    try:
        cost.cost_estimate(volume_mm3=1e6, material="NoSuchAlloy", density_kg_m3=2700.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without a usable price")
    # but supplying both overrides lets an unknown material through
    r = cost.cost_estimate(volume_mm3=1e6, material="NoSuchAlloy",
                           density_kg_m3=2700.0, price_usd_kg=4.5)
    assert abs(r["material_cost"] - 12.15) < 1e-2, r["material_cost"]


def test_bad_volume_and_quantity_raise():
    try:
        cost.cost_estimate(volume_mm3=0.0, material="AL6061-T6")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on non-positive volume")
    try:
        cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", quantity=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on quantity < 1")


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

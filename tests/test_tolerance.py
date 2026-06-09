"""Tolerance & GD&T toys — two-sided oracles for driftpin.analysis.tolerance.

Pure-Python, no FreeCAD. Each check pins a closed-form result against a hand
calculation AND verifies a deliberately-bad input is caught, mirroring
tests/TOYS.md and docs/SIMULATION_EXAMPLES.md (family 1).

Run:  python3 tests/test_tolerance.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import tolerance as tol  # noqa: E402


def test_worstcase_three_links():
    # three identical 10.00 ±0.10 links -> nominal 30, worst-case ±0.30
    chain = [{"name": f"l{i}", "nominal": 10.0, "tol": 0.10} for i in range(3)]
    r = tol.stackup(chain, method="worstcase")
    assert r["nominal"] == 30.0, r["nominal"]
    assert abs(r["worstcase"]["min"] - 29.70) < 1e-6, r["worstcase"]
    assert abs(r["worstcase"]["max"] - 30.30) < 1e-6, r["worstcase"]
    assert "rss" not in r  # worstcase method stops before the rss block


def test_rss_three_links():
    # RSS half-band = sqrt(0.10^2 * 3) = 0.1732 -> [29.83, 30.17]
    # sigma (plus/minus read as 3-sigma) = 0.1732/3 = 0.05774
    chain = [{"name": f"l{i}", "nominal": 10.0, "tol": 0.10} for i in range(3)]
    r = tol.stackup(chain, method="rss")
    assert abs(r["rss"]["sigma"] - 0.057735) < 1e-4, r["rss"]["sigma"]
    assert abs(r["rss"]["min_3s"] - 29.8268) < 1e-3, r["rss"]["min_3s"]
    assert abs(r["rss"]["max_3s"] - 30.1732) < 1e-3, r["rss"]["max_3s"]


def test_doc_example_with_directions():
    # SIMULATION_EXAMPLES.md §1: bore 50 - shoulder 30 - shim 2 = gap 18.0
    chain = [
        {"name": "housing_bore_depth", "nominal": 50.0, "tol": 0.10, "direction": 1},
        {"name": "shoulder", "nominal": 30.0, "tol": 0.05, "direction": -1},
        {"name": "shim", "nominal": 2.0, "tol": 0.02, "direction": -1},
    ]
    r = tol.stackup(chain, method="montecarlo", samples=20000)
    assert r["nominal"] == 18.0, r["nominal"]
    assert abs(r["worstcase"]["min"] - 17.83) < 1e-6, r["worstcase"]
    assert abs(r["worstcase"]["max"] - 18.17) < 1e-6, r["worstcase"]
    assert abs(r["rss"]["sigma"] - 0.037859) < 1e-4, r["rss"]["sigma"]
    # Cpk vs the worst-case envelope: 0.17 / (3*0.0379) ~ 1.49 (the doc's number)
    assert abs(r["montecarlo"]["cpk"] - 1.49) < 0.06, r["montecarlo"]["cpk"]


def test_montecarlo_converges_to_rss():
    # MC std must track the analytical RSS sigma; pct_in_spec against an explicit
    # ±2.6-sigma spec must match the normal-model prediction within ±0.3%.
    import math
    chain = [{"name": f"l{i}", "nominal": 10.0, "tol": 0.10} for i in range(3)]
    rss = tol.stackup(chain, method="rss")["rss"]["sigma"]
    spec = 0.15  # ±0.15 about nominal 30 -> 0.15 / 0.057735 = 2.598 sigma
    r = tol.stackup(chain, method="montecarlo", samples=20000,
                    spec_min=30 - spec, spec_max=30 + spec)
    assert abs(r["montecarlo"]["std"] - rss) / rss < 0.05, (r["montecarlo"]["std"], rss)
    analytic_pct = 100.0 * math.erf((spec / rss) / math.sqrt(2.0))
    assert abs(r["montecarlo"]["pct_in_spec"] - analytic_pct) < 0.3, \
        (r["montecarlo"]["pct_in_spec"], analytic_pct)


def test_fit_check_clearance():
    # ISO H7/g6 on Ø20: hole +0.021/0, shaft -0.007/-0.020
    r = tol.fit_check(hole={"nominal": 20.0, "plus": 0.021, "minus": 0.0},
                      shaft={"nominal": 20.0, "plus": -0.007, "minus": -0.020})
    assert r["fit_class"] == "clearance", r["fit_class"]
    assert abs(r["min_clearance"] - 0.007) < 1e-6, r["min_clearance"]
    assert abs(r["max_clearance"] - 0.041) < 1e-6, r["max_clearance"]
    assert r["prob_interference"] < 1e-4, r["prob_interference"]


def test_fit_check_interference_is_caught():
    # shaft entirely larger than the hole -> always interference, prob ~ 1
    r = tol.fit_check(hole={"nominal": 20.0, "plus": 0.013, "minus": 0.0},
                      shaft={"nominal": 20.0, "plus": 0.035, "minus": 0.022})
    assert r["fit_class"] == "interference", r["fit_class"]
    assert r["max_clearance"] <= 0, r["max_clearance"]
    assert r["prob_interference"] > 0.99, r["prob_interference"]


def test_fit_check_transition_straddles_zero():
    r = tol.fit_check(hole={"nominal": 20.0, "plus": 0.021, "minus": 0.0},
                      shaft={"nominal": 20.0, "plus": 0.015, "minus": 0.002})
    assert r["fit_class"] == "transition", r["fit_class"]
    assert r["min_clearance"] < 0 < r["max_clearance"], r


def test_iso286_h7g6():
    # the handbook spot-check: Ø20 H7/g6
    r = tol.fit_class(20.0, "H7/g6")
    assert abs(r["hole"]["upper_dev"] - 0.021) < 1e-9, r["hole"]
    assert abs(r["hole"]["lower_dev"] - 0.0) < 1e-9, r["hole"]
    assert abs(r["shaft"]["upper_dev"] - (-0.007)) < 1e-9, r["shaft"]
    assert abs(r["shaft"]["lower_dev"] - (-0.020)) < 1e-9, r["shaft"]
    assert r["fit_class"] == "clearance", r["fit_class"]


def test_iso286_other_bands_and_h_shaft():
    # H7/h6 is line-to-line: shaft 0/-IT6. Ø50 band: IT7=25, IT6=16 µm.
    r = tol.fit_class(50.0, "H7/h6")
    assert abs(r["hole"]["upper_dev"] - 0.025) < 1e-9, r["hole"]
    assert abs(r["shaft"]["upper_dev"] - 0.0) < 1e-9, r["shaft"]
    assert abs(r["shaft"]["lower_dev"] - (-0.016)) < 1e-9, r["shaft"]


def test_iso286_unsupported_is_explicit():
    # interference shaft letter not in the v1 set -> explicit NotImplementedError
    for bad in ("H7/s6", "H7/p6"):
        try:
            tol.fit_class(20.0, bad)
        except NotImplementedError:
            pass
        else:
            raise AssertionError(f"expected NotImplementedError for {bad}")
    # non-H hole basis also refused
    try:
        tol.fit_class(20.0, "G7/h6")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("expected NotImplementedError for non-H hole")
    # out-of-table size
    try:
        tol.fit_class(800.0, "H7/g6")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for >500 mm")


def test_gdt_position_and_bonus():
    # true position: 0.1 mm offset in x -> diametral 0.2; zone 0.25 -> pass
    r = tol.gdt_check("position", zone=0.25, offset={"x": 0.1, "y": 0.0})
    assert abs(r["actual"] - 0.2) < 1e-9, r["actual"]
    assert r["pass"] is True and abs(r["margin"] - 0.05) < 1e-9, r
    # tighter zone fails...
    assert tol.gdt_check("position", zone=0.15, offset={"x": 0.1})["pass"] is False
    # ...but MMC bonus restores it
    bonus = tol.gdt_check("position", zone=0.15, offset={"x": 0.1}, mmc_bonus=0.10)
    assert bonus["pass"] is True, bonus


def test_gdt_form_and_bad_control():
    assert tol.gdt_check("flatness", zone=0.05, actual=0.03)["pass"] is True
    assert tol.gdt_check("flatness", zone=0.05, actual=0.06)["pass"] is False
    try:
        tol.gdt_check("teleportation", zone=0.1, actual=0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown control")


def test_sign_convention_and_empty_chain():
    # plus < minus is a sign-convention error (caught, not silently swapped)
    try:
        tol.stackup([{"nominal": 10.0, "plus": -0.1, "minus": 0.1}])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when plus < minus")
    # empty chain and unknown method both raise
    for bad in (lambda: tol.stackup([]),
                lambda: tol.stackup([{"nominal": 1, "tol": 0.1}], method="bogus")):
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

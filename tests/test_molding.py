"""Injection-molding screening toys — two-sided oracles for
driftpin.analysis.molding.

Pure-Python, no FreeCAD. Pins the one-term cooling solution against the hand
formula and its exact s^2 scaling, the spiral-flow fill check two-sided, and
the data-dependent fidelity labeling.

Run:  python3 tests/test_molding.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import molding as mo  # noqa: E402


def test_cooling_hand_anchor_abs():
    # ABS defaults (melt 240, mold 60, eject 95, alpha 0.09), s = 2 mm:
    # t = s^2/(pi^2 a) * ln(8*180/(pi^2*35)) = 6.43 s — recomputed by hand here
    r = mo.molding_screen(2.0, material="ABS")
    expected = 4.0 / (math.pi ** 2 * 0.09) * math.log(
        8 * 180 / (math.pi ** 2 * 35))
    assert abs(r["cooling_time_s"] / expected - 1) < 1e-3, r["cooling_time_s"]
    assert r["fidelity"] == "exact" and r["band_pct"] is None, r


def test_cooling_scales_with_thickness_squared():
    # exact identity: t_cool ~ s^2 — doubling the wall quadruples the cooling
    thin = mo.molding_screen(1.5, material="ABS")
    thick = mo.molding_screen(3.0, material="ABS")
    assert abs(thick["cooling_time_s"] / thin["cooling_time_s"] - 4.0) < 1e-3


def test_cooling_monotone_in_eject_temperature():
    # ejecting hotter (riskier) must shorten the cooling time
    cold = mo.molding_screen(2.0, material="ABS", t_eject_c=80)
    hot = mo.molding_screen(2.0, material="ABS", t_eject_c=110)
    assert hot["cooling_time_s"] < cold["cooling_time_s"], (hot, cold)


def test_fill_check_two_sided():
    # 2 mm ABS wall (L/t limit 175): a 300 mm path fills (ratio 150)...
    ok = mo.molding_screen(2.0, material="ABS", flow_length_mm=300)
    assert ok["fill_ok"] is True and ok["flow_ratio"] == 150.0, ok
    assert ok["fidelity"] == "correlation" and ok["band_pct"] == 30.0, ok
    # ...a 400 mm path does not (ratio 200), and says so loudly
    short_shot = mo.molding_screen(2.0, material="ABS", flow_length_mm=400)
    assert short_shot["fill_ok"] is False and short_shot["warnings"], short_shot
    # cooling time itself is unchanged by the fill check (it stays exact)
    assert ok["cooling_time_s"] == mo.molding_screen(2.0, material="ABS")["cooling_time_s"]


def test_easy_flow_pp_outreaches_stiff_pc():
    # chart ordering identity: PP (L/t 280) fills a path PC (L/t 130) cannot
    path = 2.0 * 200  # ratio 200 in a 2 mm wall
    pp = mo.molding_screen(2.0, material="PP", flow_length_mm=path)
    pc = mo.molding_screen(2.0, material="PC", flow_length_mm=path)
    assert pp["fill_ok"] is True and pc["fill_ok"] is False, (pp, pc)


def test_explicit_properties_and_thick_wall_flag():
    # no material needed when everything is explicit
    r = mo.molding_screen(2.0, t_melt_c=240, t_mold_c=60, t_eject_c=95,
                          alpha_mm2_s=0.09)
    assert abs(r["cooling_time_s"] - mo.molding_screen(2.0, material="ABS")
               ["cooling_time_s"]) < 1e-9
    # a 6 mm wall is flagged as cycle-dominating, not silently accepted
    thick = mo.molding_screen(6.0, material="ABS")
    assert thick["valid_range_ok"] is False and thick["warnings"], thick


def test_input_validation():
    for bad in (
        lambda: mo.molding_screen(0, material="ABS"),
        lambda: mo.molding_screen(2.0, material="unobtanium"),
        lambda: mo.molding_screen(2.0),                          # no properties
        lambda: mo.molding_screen(2.0, material="ABS", t_eject_c=300),  # > melt
        lambda: mo.molding_screen(2.0, material="ABS", t_mold_c=100),   # > eject
        lambda: mo.molding_screen(2.0, material="ABS", flow_length_mm=0),
        lambda: mo.molding_screen(2.0, t_melt_c=240, t_mold_c=60, t_eject_c=95,
                                  alpha_mm2_s=0.09, flow_length_mm=100),  # no L/t
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

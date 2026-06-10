"""Convection-coefficient screening toys — two-sided oracles for
driftpin.analysis.convection.

Pure-Python, no FreeCAD. Pins the Churchill–Chu / flat-plate / Hilpert
correlations against independent handbook anchors and exact scaling
identities, plus the SIMULATION_NEXT.md fidelity contract
(fidelity='correlation', band_pct, escalate_to).

Run:  python3 tests/test_convection.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import convection as cv  # noqa: E402


def test_air_film_properties_match_table_air():
    # table air at 300 K: k=0.0263 W/m·K, nu=1.589e-5 m^2/s, Pr=0.707 — the
    # Sutherland fits must land within ~2% (far inside every correlation band)
    p = cv._air_film_properties(300.0)
    assert abs(p["k_w_mk"] / 0.0263 - 1) < 0.02, p["k_w_mk"]
    assert abs(p["nu_m2_s"] / 1.589e-5 - 1) < 0.02, p["nu_m2_s"]
    assert abs(p["pr"] / 0.707 - 1) < 0.02, p["pr"]
    assert abs(p["beta_per_k"] - 1 / 300.0) < 1e-12, p["beta_per_k"]


def test_natural_vertical_plate_handbook_band():
    # independent anchor: the engineering simplified law for laminar air on a
    # vertical plate, h ~= 1.42*(dT/L)^(1/4) -> 6.35 W/m^2K at dT=40 K, L=0.1 m.
    # Churchill-Chu must agree within its own +-20% band.
    r = cv.h_estimate("vertical_plate", 100, t_surface_c=65, t_ambient_c=25)
    simplified = 1.42 * (40 / 0.1) ** 0.25
    assert abs(r["h_conv_w_m2k"] / simplified - 1) < 0.20, (r["h_conv_w_m2k"], simplified)
    assert r["mode"] == "natural" and r["correlation"] == "churchill_chu_vertical_plate"
    assert r["valid_range_ok"] is True, r["warnings"]


def test_natural_cylinder_handbook_band():
    # same independent cross-check for a horizontal cylinder: h ~= 1.32*(dT/D)^(1/4)
    # (simplified laminar air law) at dT=50 K, D=25 mm -> 8.83 W/m^2K.
    r = cv.h_estimate("horizontal_cylinder", 25, t_surface_c=75, t_ambient_c=25)
    simplified = 1.32 * (50 / 0.025) ** 0.25
    assert abs(r["h_conv_w_m2k"] / simplified - 1) < 0.20, (r["h_conv_w_m2k"], simplified)
    assert r["correlation"] == "churchill_chu_horizontal_cylinder"


def test_natural_zero_dt_is_conduction_limit():
    # dT=0 -> Ra=0 -> Churchill-Chu collapses to its exact conduction-limit
    # constant: Nu = 0.825^2 (plate) / 0.60^2 (cylinder); flagged, not silent
    p = cv.h_estimate("vertical_plate", 100, t_surface_c=25, t_ambient_c=25)
    assert abs(p["nusselt"] - 0.825 ** 2) < 1e-4, p["nusselt"]  # 4-decimal rounding
    assert p["valid_range_ok"] is False and p["warnings"], p
    c = cv.h_estimate("horizontal_cylinder", 25, t_surface_c=25, t_ambient_c=25)
    assert abs(c["nusselt"] - 0.60 ** 2) < 1e-4, c["nusselt"]


def test_forced_plate_sqrt_velocity_identity():
    # exact identity: laminar flat-plate Nu = 0.664*Re^(1/2)*Pr^(1/3), so at a
    # fixed film temperature h(2V)/h(V) == sqrt(2) exactly
    lo = cv.h_estimate("flat_plate", 100, 60, 20, velocity_m_s=2.0)
    hi = cv.h_estimate("flat_plate", 100, 60, 20, velocity_m_s=4.0)
    assert lo["correlation"] == hi["correlation"] == "flat_plate_laminar"
    ratio = hi["h_conv_w_m2k"] / lo["h_conv_w_m2k"]
    assert abs(ratio - math.sqrt(2)) < 1e-3, ratio


def test_forced_plate_magnitude_and_transition():
    # air at 5 m/s over a 100 mm plate: forced h must land in the handbook
    # 20-60 W/m^2K window and far exceed the natural-convection value
    f = cv.h_estimate("flat_plate", 100, 60, 20, velocity_m_s=5.0)
    assert 20 < f["h_conv_w_m2k"] < 60, f["h_conv_w_m2k"]
    n = cv.h_estimate("vertical_plate", 100, 60, 20)
    assert f["h_conv_w_m2k"] > 3 * n["h_conv_w_m2k"], (f["h_conv_w_m2k"], n["h_conv_w_m2k"])
    # past Re_c = 5e5 the mixed correlation takes over and h jumps above the
    # laminar extrapolation (turbulence enhances transport)
    m = cv.h_estimate("flat_plate", 1000, 60, 20, velocity_m_s=15.0)
    assert m["correlation"] == "flat_plate_mixed", m
    laminar_extrap = 0.664 * math.sqrt(m["reynolds"]) * m["prandtl"] ** (1 / 3)
    assert m["nusselt"] > laminar_extrap, (m["nusselt"], laminar_extrap)


def test_hilpert_crossflow_band_and_row_identity():
    # air at 5 m/s across a 25 mm cylinder (Re ~ 7e3): handbook h ~ 40-50 W/m^2K
    r = cv.h_estimate("cylinder_crossflow", 25, 75, 25, velocity_m_s=5.0)
    assert r["correlation"] == "hilpert_crossflow", r
    assert 30 < r["h_conv_w_m2k"] < 60, r["h_conv_w_m2k"]
    # exact identity within one Hilpert row (4e3-4e4: m=0.618): h(2V)/h(V) = 2^m
    hi = cv.h_estimate("cylinder_crossflow", 25, 75, 25, velocity_m_s=10.0)
    assert 4000 < r["reynolds"] and hi["reynolds"] < 40000, (r["reynolds"], hi["reynolds"])
    ratio = hi["h_conv_w_m2k"] / r["h_conv_w_m2k"]
    assert abs(ratio - 2 ** 0.618) < 1e-3, ratio


def test_hilpert_out_of_range_is_flagged_not_silent():
    # Re below the 0.4 table floor: still answers (nearest row) but flags it
    r = cv.h_estimate("cylinder_crossflow", 1, 75, 25, velocity_m_s=1e-6)
    assert r["reynolds"] < 0.4, r["reynolds"]
    assert r["valid_range_ok"] is False and r["warnings"], r


def test_radiation_combination_mirrors_lumped_screen():
    # emissivity=0 -> pure convection; emissivity>0 adds the linearized screen
    # h_rad ~= 4*eps*sigma*T^3 (= 5.51 W/m^2K for eps=0.9 near 300 K)
    base = cv.h_estimate("vertical_plate", 100, 30, 25)
    assert base["h_rad_w_m2k"] == 0.0, base
    assert base["h_total_w_m2k"] == base["h_conv_w_m2k"], base
    r = cv.h_estimate("vertical_plate", 100, 30, 25, emissivity=0.9)
    assert abs(r["h_rad_w_m2k"] - 5.51) < 0.15, r["h_rad_w_m2k"]
    assert abs(r["h_total_w_m2k"] - (r["h_conv_w_m2k"] + r["h_rad_w_m2k"])) < 1e-6, r
    assert r["h_conv_w_m2k"] == base["h_conv_w_m2k"], (r, base)


def test_fidelity_contract_fields():
    # every return carries the SIMULATION_NEXT.md screening contract
    for r in (
        cv.h_estimate("vertical_plate", 100, 65, 25),
        cv.h_estimate("flat_plate", 100, 65, 25, velocity_m_s=3.0),
        cv.h_estimate("cylinder_crossflow", 25, 65, 25, velocity_m_s=5.0),
    ):
        assert r["fidelity"] == "correlation", r
        assert 0 < r["band_pct"] <= 20, r["band_pct"]
        assert r["escalate_to"] == "cht_channel_submit", r


def test_non_air_fluid_needs_explicit_properties():
    # water must NOT silently get air properties...
    try:
        cv.h_estimate("flat_plate", 100, 60, 20, velocity_m_s=1.0, fluid="water")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for fluid without properties")
    # ...but with explicit film properties (water ~40C: k=0.631, nu=6.58e-7,
    # Pr=4.34) the laminar plate h lands in the handbook hundreds-of-W/m^2K range
    r = cv.h_estimate("flat_plate", 100, 60, 20, velocity_m_s=0.5, fluid="water",
                      k_w_mk=0.631, nu_m2_s=6.58e-7, pr=4.34)
    assert 500 < r["h_conv_w_m2k"] < 3000, r["h_conv_w_m2k"]


def test_geometry_velocity_mismatch_raises():
    for bad in (
        lambda: cv.h_estimate("vertical_plate", 100, 60, 20, velocity_m_s=2.0),
        lambda: cv.h_estimate("flat_plate", 100, 60, 20),          # forced, V=0
        lambda: cv.h_estimate("sphere", 100, 60, 20),              # unknown geometry
        lambda: cv.h_estimate("vertical_plate", 0, 60, 20),        # zero length
        lambda: cv.h_estimate("vertical_plate", 100, 60, 20, emissivity=1.5),
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

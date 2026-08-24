"""Plate-bending toys — two-sided oracles for ankusdrive.analysis.plates.

Pure-Python, no FreeCAD. Pins the Roark/Timoshenko coefficients against the
exact 1-D beam-strip limits and the exact circular closed forms, plus the
theory-limit flags (thin plate, small deflection).

Run:  python3 tests/test_plates.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import plates as pl  # noqa: E402


def test_square_ss_handbook_anchor():
    # square simply supported, q=10 kPa, b=200, t=2, E=200 GPa:
    # sigma = 0.2874*0.01*200^2/4 = 28.74 MPa; delta = 0.0444*0.01*200^4/(2e5*8)
    r = pl.plate_check("rectangular", thickness_mm=2, pressure_kpa=10,
                       a_mm=200, b_mm=200, youngs_gpa=200)
    assert abs(r["sigma_max_mpa"] - 28.74) < 0.01, r["sigma_max_mpa"]
    assert abs(r["deflection_max_mm"] - 0.444) < 0.001, r["deflection_max_mm"]
    assert r["fidelity"] == "exact" and r["escalate_to"] == "fem_run", r


def test_long_plate_is_the_exact_beam_strip():
    # asymptotic identity: a/b -> inf collapses to the 1-D strip closed forms.
    # SS strip: sigma = 6*(qb^2/8)/t^2 = 0.75*q*b^2/t^2;
    # delta = 5qb^4/(384D) = 5*12*(1-0.09)/384 * qb^4/(Et^3) = 0.14219*qb^4/(Et^3)
    r = pl.plate_check("rectangular", thickness_mm=2, pressure_kpa=10,
                       a_mm=5000, b_mm=100, youngs_gpa=200)
    assert abs(r["beta"] - 0.75) < 1e-9, r["beta"]
    assert abs(r["sigma_max_mpa"] - 0.75 * 0.01 * 100 ** 2 / 4) < 1e-6
    # clamped strip: sigma = 6*(qb^2/12)/t^2 = 0.5*q*b^2/t^2; delta = qb^4/(384D)
    c = pl.plate_check("rectangular", thickness_mm=2, pressure_kpa=10,
                       a_mm=5000, b_mm=100, support="clamped", youngs_gpa=200)
    assert abs(c["beta"] - 0.5) < 1e-9, c["beta"]
    strip_delta = 12 * (1 - 0.3 ** 2) / 384 * 0.01 * 100 ** 4 / (200e3 * 8)
    assert abs(c["deflection_max_mm"] / strip_delta - 1) < 0.01, c["deflection_max_mm"]


def test_circular_exact_closed_forms():
    # clamped circle: delta = qR^4/(64D), sigma = 0.75*q*R^2/t^2 — exact
    q, r_mm, t, e = 0.01, 100.0, 2.0, 200e3
    d_flex = e * t ** 3 / (12 * (1 - 0.09))
    r = pl.plate_check("circular", thickness_mm=t, pressure_kpa=10,
                       diameter_mm=200, support="clamped", youngs_gpa=200)
    assert abs(r["deflection_max_mm"] - q * r_mm ** 4 / (64 * d_flex)) < 1e-5, r
    assert abs(r["sigma_max_mpa"] - 0.75 * q * r_mm ** 2 / t ** 2) < 1e-6, r
    # reciprocity of the two supports: delta_ss/delta_clamped = (5+nu)/(1+nu) exactly
    s = pl.plate_check("circular", thickness_mm=t, pressure_kpa=10,
                       diameter_mm=200, support="simply_supported", youngs_gpa=200)
    assert abs(s["deflection_max_mm"] / r["deflection_max_mm"] - 5.3 / 1.3) < 1e-3


def test_clamping_stiffens_and_aspect_monotone():
    # clamped < simply supported in both sigma and delta (same plate)
    kw = dict(thickness_mm=2, pressure_kpa=10, a_mm=200, b_mm=200, youngs_gpa=200)
    ss = pl.plate_check("rectangular", **kw)
    cl = pl.plate_check("rectangular", support="clamped", **kw)
    assert cl["sigma_max_mpa"] > 0 and cl["deflection_max_mm"] < ss["deflection_max_mm"]
    # beta rises monotonically with aspect ratio (interpolation sanity)
    betas = [pl.plate_check("rectangular", thickness_mm=2, pressure_kpa=10,
                            a_mm=100 * ar, b_mm=100, youngs_gpa=200)["beta"]
             for ar in (1.0, 1.3, 1.7, 2.5, 10.0)]
    assert betas == sorted(betas), betas
    # a_mm/b_mm order must not matter (short side drives)
    flip = pl.plate_check("rectangular", thickness_mm=2, pressure_kpa=10,
                          a_mm=100, b_mm=170, youngs_gpa=200)
    same = pl.plate_check("rectangular", thickness_mm=2, pressure_kpa=10,
                          a_mm=170, b_mm=100, youngs_gpa=200)
    assert flip["sigma_max_mpa"] == same["sigma_max_mpa"], (flip, same)


def test_theory_limit_flags_two_sided():
    # comfortable thin plate, small load -> both flags ok
    ok = pl.plate_check("rectangular", thickness_mm=5, pressure_kpa=1,
                        a_mm=200, b_mm=200, youngs_gpa=200)
    assert ok["thin_plate_ok"] and ok["small_deflection_ok"] and ok["valid_range_ok"]
    # span/t < 10 -> thick-plate flag
    thick = pl.plate_check("rectangular", thickness_mm=30, pressure_kpa=1,
                           a_mm=200, b_mm=200, youngs_gpa=200)
    assert thick["thin_plate_ok"] is False and thick["valid_range_ok"] is False
    # big load on a thin plate -> membrane flag (delta > t/2), not silence
    floppy = pl.plate_check("rectangular", thickness_mm=1, pressure_kpa=50,
                            a_mm=300, b_mm=300, youngs_gpa=200)
    assert floppy["small_deflection_ok"] is False and floppy["warnings"], floppy


def test_material_yield_safety_factor():
    # AL6061-T6 (E=68.9 GPa, yield=276 MPa): sf = 276/sigma
    r = pl.plate_check("rectangular", thickness_mm=3, pressure_kpa=20,
                       a_mm=150, b_mm=150, material="AL6061-T6")
    assert r["yield_safety_factor"] is not None
    assert abs(r["yield_safety_factor"] - 276.0 / r["sigma_max_mpa"]) < 0.01, r
    # explicit E with no material -> no silent yield, sf is None
    e = pl.plate_check("rectangular", thickness_mm=3, pressure_kpa=20,
                       a_mm=150, b_mm=150, youngs_gpa=69)
    assert e["yield_safety_factor"] is None


def test_input_validation():
    for bad in (
        lambda: pl.plate_check("triangular", 2, 10, a_mm=100, b_mm=100,
                               youngs_gpa=200),
        lambda: pl.plate_check("rectangular", 2, 10, a_mm=100, youngs_gpa=200),
        lambda: pl.plate_check("rectangular", 0, 10, a_mm=100, b_mm=100,
                               youngs_gpa=200),
        lambda: pl.plate_check("circular", 2, 10, youngs_gpa=200),  # no diameter
        lambda: pl.plate_check("rectangular", 2, 10, a_mm=100, b_mm=100),  # no E
        lambda: pl.plate_check("rectangular", 2, 10, a_mm=100, b_mm=100,
                               youngs_gpa=200, support="glued"),
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

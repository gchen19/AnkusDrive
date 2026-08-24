"""Column-buckling toys — two-sided oracles for ankusdrive.analysis.buckling.

Pure-Python, no FreeCAD. Pins Euler + Johnson against exact identities: the
K-factor ratios, the Euler closed form, the sigma_y/2 continuity at the
transition slenderness, and the Johnson sigma_y limit at zero slenderness.

Run:  python3 tests/test_buckling.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import buckling as bk  # noqa: E402


def test_euler_closed_form_anchor():
    # 1 m pinned-pinned steel rod d=20 mm: P_cr = pi^2*E*I/L^2 exactly
    r = bk.beam_buckling(1000, diameter_mm=20, youngs_gpa=200, yield_mpa=350)
    i = math.pi * 20 ** 4 / 64
    expected = math.pi ** 2 * 200e3 * i / 1000 ** 2
    assert r["governing"] == "euler", r
    assert abs(r["p_cr_n"] / expected - 1) < 1e-4, (r["p_cr_n"], expected)
    assert r["fidelity"] == "exact" and r["escalate_to"] == "fem_buckling", r


def test_k_factor_exact_ratios():
    # exact identities: P_cr scales as 1/K^2 — fixed_free = pinned/4,
    # fixed_fixed = 4*pinned (same column, all in the Euler regime)
    kw = dict(diameter_mm=20, youngs_gpa=200, yield_mpa=350)
    pinned = bk.beam_buckling(1500, end_condition="pinned_pinned", **kw)
    free = bk.beam_buckling(1500, end_condition="fixed_free", **kw)
    fixed = bk.beam_buckling(1500, end_condition="fixed_fixed", **kw)
    assert abs(free["p_cr_n"] / pinned["p_cr_n"] - 0.25) < 1e-4
    assert abs(fixed["p_cr_n"] / pinned["p_cr_n"] - 4.0) < 1e-3
    assert all(x["governing"] == "euler" for x in (pinned, free, fixed))


def test_continuity_at_transition_is_half_yield():
    # at lambda_t = sqrt(2 pi^2 E / sigma_y) Euler and Johnson are BOTH exactly
    # sigma_y/2 — the curve is continuous by construction
    e_mpa, sy = 200e3, 350.0
    lam_t = math.sqrt(2 * math.pi ** 2 * e_mpa / sy)
    r_gyr = 5.0  # d=20 mm solid round
    at = bk.beam_buckling(lam_t * r_gyr, diameter_mm=20, youngs_gpa=200,
                          yield_mpa=350)
    assert abs(at["sigma_cr_mpa"] - sy / 2) < 0.01, at["sigma_cr_mpa"]
    just_above = bk.beam_buckling(lam_t * r_gyr * 1.001, diameter_mm=20,
                                  youngs_gpa=200, yield_mpa=350)
    just_below = bk.beam_buckling(lam_t * r_gyr * 0.999, diameter_mm=20,
                                  youngs_gpa=200, yield_mpa=350)
    assert just_above["governing"] == "euler" and just_below["governing"] == "johnson"
    assert abs(just_above["sigma_cr_mpa"] - just_below["sigma_cr_mpa"]) < 1.0


def test_johnson_limits_to_yield_at_zero_slenderness():
    # a stub column cannot exceed yield: lambda -> 0 gives sigma_cr -> sigma_y,
    # flagged as plain compression
    r = bk.beam_buckling(20, diameter_mm=20, youngs_gpa=200, yield_mpa=350)
    assert r["governing"] == "johnson", r
    assert abs(r["sigma_cr_mpa"] - 350.0) < 2.0, r["sigma_cr_mpa"]
    assert r["valid_range_ok"] is False and r["warnings"], r


def test_weak_axis_drives_rectangles():
    # a 10x20 rectangle buckles about its thin direction: I = 20*10^3/12 —
    # identical to passing the explicit area + I_min
    r = bk.beam_buckling(800, width_mm=10, height_mm=20, youngs_gpa=200,
                         yield_mpa=350)
    explicit = bk.beam_buckling(800, area_mm2=200.0, i_min_mm4=20 * 10 ** 3 / 12,
                                youngs_gpa=200, yield_mpa=350)
    assert abs(r["p_cr_n"] - explicit["p_cr_n"]) < 1e-6, (r, explicit)
    # the orientation of the inputs must not matter
    flip = bk.beam_buckling(800, width_mm=20, height_mm=10, youngs_gpa=200,
                            yield_mpa=350)
    assert flip["p_cr_n"] == r["p_cr_n"]


def test_safety_factor_and_materials_db():
    r = bk.beam_buckling(1000, diameter_mm=20, youngs_gpa=200, yield_mpa=350,
                         load_n=5000)
    assert abs(r["safety_factor"] - r["p_cr_n"] / 5000) < 0.01, r
    # E and yield resolve from the Materials DB (AL6061-T6: 68.9 GPa / 276 MPa)
    al = bk.beam_buckling(1000, diameter_mm=20, material="AL6061-T6")
    steel_like = bk.beam_buckling(1000, diameter_mm=20, youngs_gpa=68.9,
                                  yield_mpa=276)
    assert abs(al["p_cr_n"] - steel_like["p_cr_n"]) < 1.0, (al, steel_like)


def test_input_validation():
    for bad in (
        lambda: bk.beam_buckling(1000, diameter_mm=20, youngs_gpa=200,
                                 yield_mpa=350, end_condition="welded"),
        lambda: bk.beam_buckling(1000, youngs_gpa=200, yield_mpa=350),  # no section
        lambda: bk.beam_buckling(1000, diameter_mm=20),                 # no material
        lambda: bk.beam_buckling(0, diameter_mm=20, youngs_gpa=200, yield_mpa=350),
        lambda: bk.beam_buckling(1000, area_mm2=200, youngs_gpa=200,
                                 yield_mpa=350),                        # area w/o I
        lambda: bk.beam_buckling(1000, diameter_mm=20, youngs_gpa=200,
                                 yield_mpa=350, load_n=0),
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

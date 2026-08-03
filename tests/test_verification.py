"""Solution-verification toys — the Grid Convergence Index (issue #225).

Pure-Python, no FreeCAD, no solver. Unlike every other gate in the suite this one has a
*constructed* oracle rather than a physical one: a sequence built as f(h) = f_exact +
C·h^p has a known order and a known limit, so the procedure can be checked exactly
rather than banded. If `grid_convergence` cannot recover p=2 and f_exact from a
second-order sequence to machine precision, it cannot be trusted to put an error bar on
a drag coefficient.

The two-sided half matters as much: a study that oscillates, that is nowhere near the
asymptotic range, or that has only two meshes must SAY so — a GCI reported without those
caveats is worse than no GCI, because it looks like rigour.

Run:  python3 tests/test_verification.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import verification as ver  # noqa: E402


def _sequence(exact, coeff, order, sizes):
    """f(h) = exact + coeff·h^order, finest first."""
    return [exact + coeff * h ** order for h in sizes]


def test_recovers_order_and_limit_of_a_constructed_sequence():
    sizes = [1.0, 2.0, 4.0]
    for order in (1.0, 1.5, 2.0, 3.0):
        vals = _sequence(1000.0, 3.0, order, sizes)
        r = ver.grid_convergence(vals, cell_sizes=sizes)
        assert abs(r["observed_order"] - order) < 1e-6, (order, r["observed_order"])
        assert abs(r["extrapolated_value"] - 1000.0) < 1e-6, r["extrapolated_value"]
        assert r["monotonic"] and r["order_used"] == r["observed_order"]
        assert r["safety_factor"] == 1.25 and not r["order_clamped"]
    # a well-behaved second-order sequence is in the asymptotic range and says nothing
    r = ver.grid_convergence(_sequence(1000.0, 3.0, 2.0, sizes), cell_sizes=sizes)
    assert abs(r["asymptotic_ratio"] - 1.0) < 0.02, r["asymptotic_ratio"]
    assert not r["warnings"], r["warnings"]


def test_unequal_refinement_ratios_still_fit():
    """The ASME fixed point exists because real mesh ladders are not equally spaced."""
    sizes = [1.0, 1.5, 3.0]                       # r21 = 1.5, r32 = 2.0
    r = ver.grid_convergence(_sequence(500.0, -7.0, 2.0, sizes), cell_sizes=sizes)
    assert abs(r["observed_order"] - 2.0) < 1e-6, r["observed_order"]
    assert abs(r["extrapolated_value"] - 500.0) < 1e-6, r["extrapolated_value"]
    assert r["refinement_ratios"] == [1.5, 2.0]
    # a negative coefficient converges from ABOVE — just as monotone, and the sign of
    # the approach must not change the fit
    assert r["monotonic"] and r["values"][0] > r["values"][1] > r["values"][2]


def test_cell_counts_convert_to_a_representative_size():
    sizes = [1.0, 2.0, 4.0]
    vals = _sequence(1000.0, 3.0, 2.0, sizes)
    # N ∝ h^-3 in 3-D: 4^3 apart per level
    counts = [1000000.0, 1000000.0 / 8, 1000000.0 / 64]
    r = ver.grid_convergence(vals, cell_counts=counts)
    assert abs(r["observed_order"] - 2.0) < 1e-6, r["observed_order"]
    assert abs(r["extrapolated_value"] - 1000.0) < 1e-6
    # a 2-D (wedge) mesh refines differently for the same count ratio
    r2 = ver.grid_convergence(vals, cell_counts=[10000.0, 2500.0, 625.0], dimensions=2)
    assert abs(r2["observed_order"] - 2.0) < 1e-6, r2["observed_order"]


def test_gci_brackets_the_exact_answer():
    """The point of the band: the exact value must lie inside it. Checked across
    orders and error magnitudes, since a band that only works for tidy cases is not a
    band."""
    sizes = [1.0, 2.0, 4.0]
    for order in (1.0, 2.0):
        for coeff in (0.5, 5.0, 50.0):
            vals = _sequence(1000.0, coeff, order, sizes)
            r = ver.grid_convergence(vals, cell_sizes=sizes)
            fine = vals[0]
            half = r["gci_pct"] / 100.0 * abs(fine)
            assert abs(fine - 1000.0) <= half + 1e-9, (order, coeff, fine, half)


def test_two_meshes_assume_the_order_and_widen_the_band():
    sizes = [1.0, 2.0]
    vals = _sequence(1000.0, 3.0, 2.0, sizes)
    two = ver.grid_convergence(vals, cell_sizes=sizes)
    assert two["observed_order"] is None and two["order_used"] == 2.0
    assert two["safety_factor"] == 3.0
    assert any("ASSUMED" in w for w in two["warnings"]), two["warnings"]
    assert two["gci_coarse_pct"] is None and two["asymptotic_ratio"] is None
    # same data, three meshes: order measured, band 2.4x tighter (1.25 vs 3.0)
    three = ver.grid_convergence(_sequence(1000.0, 3.0, 2.0, [1.0, 2.0, 4.0]),
                                 cell_sizes=[1.0, 2.0, 4.0])
    assert three["gci_pct"] < two["gci_pct"], (three["gci_pct"], two["gci_pct"])
    assert abs(two["gci_pct"] / three["gci_pct"] - 3.0 / 1.25) < 1e-6
    # a wrong assumed order still extrapolates, just to the wrong place — that is
    # exactly the risk the 3.0 factor covers
    wrong = ver.grid_convergence(vals, cell_sizes=sizes, assumed_order=1.0)
    assert abs(wrong["extrapolated_value"] - 1000.0) > 1.0, wrong["extrapolated_value"]


def test_oscillation_and_non_asymptotic_are_reported_not_smoothed():
    # f goes down then up: Richardson's monotone-convergence assumption is violated
    osc = ver.grid_convergence([10.0, 10.5, 10.2], cell_sizes=[1.0, 2.0, 4.0])
    assert osc["monotonic"] is False
    assert any("NOT monotone" in w for w in osc["warnings"]), osc["warnings"]
    # a huge discretization error relative to the value is not asymptotic
    far = ver.grid_convergence(_sequence(10.0, 3.0, 2.0, [1.0, 2.0, 4.0]),
                               cell_sizes=[1.0, 2.0, 4.0])
    assert far["asymptotic_ratio"] < 0.85
    assert any("asymptotic ratio" in w for w in far["warnings"]), far["warnings"]
    # an unphysical fitted order is clamped, loudly
    wild = ver.grid_convergence([100.0, 100.001, 140.0], cell_sizes=[1.0, 2.0, 4.0])
    assert wild["order_clamped"] is True and wild["order_used"] <= 4.0
    assert any("outside" in w for w in wild["warnings"]), wild["warnings"]
    # identical solutions carry no information: no order, and the band collapses
    same = ver.grid_convergence([7.0, 7.0, 7.0], cell_sizes=[1.0, 2.0, 4.0])
    assert same["observed_order"] is None and same["gci_pct"] == 0.0
    assert abs(same["extrapolated_value"] - 7.0) < 1e-12


def test_input_validation():
    sizes = [1.0, 2.0, 4.0]
    vals = [1003.0, 1012.0, 1048.0]
    bad = [
        dict(values=[1.0], cell_sizes=[1.0]),                     # one level
        dict(values=[1.0] * 4, cell_sizes=[1.0, 2, 3, 4]),        # four levels
        dict(values=vals),                                        # no mesh description
        dict(values=vals, cell_sizes=sizes, cell_counts=[1, 2, 3]),   # both
        dict(values=vals, cell_sizes=[1.0, 2.0]),                 # length mismatch
        dict(values=vals, cell_sizes=[4.0, 2.0, 1.0]),            # coarsest first
        dict(values=vals, cell_sizes=[1.0, 1.0, 2.0]),            # not strictly ordered
        dict(values=vals, cell_sizes=[0.0, 2.0, 4.0]),            # zero size
        dict(values=vals, cell_counts=[0, 100, 200]),             # zero count
        dict(values=vals, cell_counts=[8000, 1000, 125], dimensions=4),
    ]
    for kwargs in bad:
        try:
            ver.grid_convergence(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"grid_convergence({kwargs}) should have raised")
    # a ratio below the ASME recommendation is a warning, not a refusal
    tight = ver.grid_convergence(_sequence(1000.0, 3.0, 2.0, [1.0, 1.1, 1.21]),
                                 cell_sizes=[1.0, 1.1, 1.21])
    assert any("below the 1.3" in w for w in tight["warnings"]), tight["warnings"]


# --- runner -------------------------------------------------------------------

def _discover():
    return [(name, fn) for name, fn in sorted(globals().items())
            if name.startswith("test_") and callable(fn)]


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
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
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

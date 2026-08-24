"""Tolerance–cost coupling (issue #235) — pure-core unit tests.

No FreeCAD, no worker, no LLM: :mod:`ankusdrive.analysis.tolerance_cost` is arithmetic
over the ISO 286 tables that :mod:`ankusdrive.analysis.tolerance` already owns, so
these run on the host interpreter in a second. They prove the four things the layer
promises:

  * the IT-grade axis is REAL — it reproduces the tabulated grades exactly, is its
    own inverse, and extrapolates past IT11 onto the standard's own R5 series
    within a tenth of a grade;
  * the cost curve is MONOTONE and CALIBRATED — strictly increasing as the grade
    tightens, everywhere on the axis, with the IT6/IT9 ratio for a machined bore
    landing inside the handbook's 2-6x;
  * flagging is TWO-SIDED — an IT6 bore on an FDM part flags, an IT11 slot on CNC
    does not, and the flag names the operation the part quietly acquired;
  * the loosen loop is HONEST — it lowers the cost index while cpk still clears the
    target, refuses a chain that has no margin to give, and returns "no savings"
    on an already-loose chain instead of inventing one.

Run: python3 tests/test_tolerance_cost.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.analysis import cost, tolerance, tolerance_cost as tc  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _ok(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _near(label, got, want, tol):
    _ok(label, abs(got - want) <= tol, f"got {got!r}, want {want!r} +/-{tol}")


# --- the IT-grade axis --------------------------------------------------------

def test_it_grade_reproduces_the_iso_table():
    print("test_it_grade_reproduces_the_iso_table")
    # Every tabulated cell must come back as its own integer grade — if it does
    # not, the module is not reading the same table fit_class is.
    worst = 0.0
    for grade, row in tolerance._IT.items():
        for i, um in enumerate(row):
            # a representative size inside band i
            size = (tolerance._BANDS[i - 1] + tolerance._BANDS[i]) / 2.0 if i else 2.0
            got = tc.it_grade(size, um / 1000.0)
            worst = max(worst, abs(got - grade))
    _ok(f"every IT4-IT11 cell round-trips (worst error {worst:.4g} grades)",
        worst < 1e-6, f"worst {worst}")


def test_it_grade_and_band_are_inverses():
    print("test_it_grade_and_band_are_inverses")
    worst = 0.0
    for size in (2.0, 12.0, 25.0, 80.0, 300.0):
        for g in (4.0, 5.5, 7.0, 8.3, 11.0, 13.0, 16.0):
            band = tc.band_for_grade(size, g)
            worst = max(worst, abs(tc.it_grade(size, band) - g))
    _ok(f"band_for_grade inverts it_grade (worst {worst:.4g} grades)",
        worst < 1e-3, f"worst {worst}")


def test_extrapolation_past_the_table_tracks_the_r5_series():
    print("test_extrapolation_past_the_table_tracks_the_r5_series")
    # tolerance._IT stops at IT11; #235 needs IT12-IT16 for cast/moulded/printed
    # features. ISO 286-1 builds those on the R5 series, so the extrapolation is
    # checkable against the standard's published numbers for the 3-6 mm band.
    published_um = {12: 120.0, 13: 180.0, 14: 300.0, 15: 480.0, 16: 750.0}
    worst = 0.0
    for grade, um in published_um.items():
        worst = max(worst, abs(tc.it_grade(4.5, um / 1000.0) - grade))
    _ok(f"IT12-IT16 land within 0.15 grades of ISO 286-1 (worst {worst:.3f})",
        worst < 0.15, f"worst {worst}")
    # deliberately-wrong: a naive linear extrapolation would be far off by IT16
    _ok("and IT16 is not simply IT11 + a constant step",
        abs(tc.it_grade(4.5, 0.750) - tc.it_grade(4.5, 0.075)) > 4.0)


def test_a_band_between_two_grades_reports_a_fraction():
    print("test_a_band_between_two_grades_reports_a_fraction")
    # Ø20: IT6 = 13 um, IT7 = 21 um. A 15 um band is neither.
    g = tc.it_grade(20.0, 0.015)
    _ok("15 um on Ø20 is strictly between IT6 and IT7", 6.0 < g < 7.0, f"got {g}")
    _check("IT6 is exact", tc.it_grade(20.0, 0.013), 6.0)
    _check("IT7 is exact", tc.it_grade(20.0, 0.021), 7.0)


def test_zero_and_offtable_inputs_raise():
    print("test_zero_and_offtable_inputs_raise")
    for label, fn in (
        ("a zero band has no IT grade", lambda: tc.it_grade(20.0, 0.0)),
        ("an off-table size raises", lambda: tc.it_grade(900.0, 0.05)),
    ):
        try:
            fn()
        except ValueError:
            _ok(label, True)
        else:
            _ok(label, False, "no ValueError")


# --- the cost curve is monotone and calibrated --------------------------------

def test_cost_index_is_strictly_monotone_across_the_whole_axis():
    print("test_cost_index_is_strictly_monotone_across_the_whole_axis")
    # The gate the issue names. It must hold on BOTH sides of the knee — a
    # flat-lining region above natural capability would make suggest_loosening
    # unable to rank two already-cheap links.
    for process in ("cnc", "fdm", "grinding", "injection"):
        grades = [4.0 + 0.25 * i for i in range(49)]     # IT4 -> IT16
        costs = [tc.relative_cost(g, process) for g in grades]
        strictly = all(a > b for a, b in zip(costs, costs[1:]))
        _ok(f"{process}: cost strictly falls as the grade loosens", strictly)
    # and it crosses 1.0 exactly at natural capability
    _check("cnc costs 1.0 at its natural IT9", tc.relative_cost(9.0, "cnc"), 1.0)


def test_it6_over_it9_for_a_machined_bore_is_in_the_handbook_band():
    print("test_it6_over_it9_for_a_machined_bore_is_in_the_handbook_band")
    # The calibration gate. Handbooks put a machined bore at IT6 somewhere between
    # 2x and 6x the same bore at IT9 (ream/grind + 100% gauging vs. bore-and-go).
    ratio = tc.relative_cost(6.0, "cnc") / tc.relative_cost(9.0, "cnc")
    _ok(f"IT6/IT9 = {ratio:.2f}x is inside the handbook 2-6x", 2.0 <= ratio <= 6.0,
        f"got {ratio}")
    _near("and it sits at the centre of the band", ratio, 4.0, 0.01)
    # deliberately-wrong: a linear-in-grade model would give ~1.5x and fail the band
    _ok("the curve is exponential, not linear", ratio > 3.0)


def test_the_curve_has_a_knee_at_natural_capability():
    print("test_the_curve_has_a_knee_at_natural_capability")
    # One grade tighter than natural costs more than one grade looser saves — that
    # asymmetry IS the engineering story (below the knee you are buying a process;
    # above it you are only buying gauging), and a symmetric curve would be wrong.
    import math
    tighter = tc.relative_cost(8.0, "cnc") / tc.relative_cost(9.0, "cnc")
    looser = tc.relative_cost(9.0, "cnc") / tc.relative_cost(10.0, "cnc")
    _ok(f"tightening one grade costs {tighter:.3f}x, loosening saves only "
        f"{looser:.3f}x", tighter > looser, f"{tighter} vs {looser}")
    # The exact asymmetry is the ratio of the two documented rates. (Tolerance is
    # 1e-4, not 0: relative_cost rounds its index to 6 decimals, so the logs carry
    # that rounding — an exact compare would be testing the rounding, not the curve.)
    _near("the knee ratio is exactly FREE/DOUBLING",
          math.log(tighter) / math.log(looser),
          tc.FREE_GRADES_PER_HALVING / tc.GRADES_PER_DOUBLING, 1e-4)


def test_cheapest_process_walks_the_operation_ladder():
    print("test_cheapest_process_walks_the_operation_ladder")
    _check("IT11 -> drilling", tc.cheapest_process(11.0)["operation"], "drilling")
    _check("IT10 -> milling", tc.cheapest_process(10.0)["operation"], "milling")
    _check("IT9 -> turning", tc.cheapest_process(9.0)["operation"], "turning")
    _check("IT7 -> reaming", tc.cheapest_process(7.0)["operation"], "reaming")
    _check("IT6 -> grinding", tc.cheapest_process(6.0)["operation"], "grinding")
    # A fractional grade takes the operation that actually HOLDS it, never the one
    # it rounds to. IT8.4 is tighter than turning's natural IT9, so turning does not
    # hold it; boring (IT8) is the coarsest that does. Rounding 8.4 to 8 and then to
    # "turning" would be the classic off-by-one that under-quotes the part.
    _check("IT8.4 -> boring, not the turning it rounds toward",
           tc.cheapest_process(8.4)["operation"], "boring")
    # coarser than any tabulated operation: nothing to choose
    _check("IT14 -> the coarsest operation", tc.cheapest_process(14.0)["operation"],
           "drilling")


# --- two-sided flagging -------------------------------------------------------

def test_an_it6_bore_on_an_fdm_part_flags():
    print("test_an_it6_bore_on_an_fdm_part_flags")
    # The must-fail half of the issue's gate.
    bore = [{"name": "bore", "nominal": 25.0, "plus": 0.0065, "minus": -0.0065}]
    _check("Ø25 +/-0.0065 is IT6", tc.it_grade(25.0, 0.013), 6.0)
    res = tc.tolerance_cost_check(bore, process="fdm")
    _check("the scheme fails", res["pass"], False)
    _check("exactly one flag", len(res["flagged"]), 1)
    _check("with the secondary-operation verdict", res["flagged"][0]["verdict"],
           tc.SECONDARY)
    _ok("and it names the operation the part just acquired",
        res["links"][0]["cheapest_operation"] == "grinding",
        res["links"][0]["cheapest_operation"])
    _ok("the premium is enormous", res["links"][0]["cost_index"] > 20.0,
        res["links"][0]["cost_index"])


def test_an_it11_slot_on_cnc_does_not_flag():
    print("test_an_it11_slot_on_cnc_does_not_flag")
    # The must-pass half.
    band = tc.band_for_grade(40.0, 11.0)
    slot = [{"name": "slot", "nominal": 40.0, "tol": band / 2.0}]
    res = tc.tolerance_cost_check(slot, process="cnc")
    _check("the scheme passes", res["pass"], True)
    _check("no flags", res["flagged"], [])
    _check("verdict is ok", res["links"][0]["verdict"], tc.OK)
    _ok("and it is CHEAPER than natural capability",
        res["links"][0]["cost_index"] < 1.0, res["links"][0]["cost_index"])


def test_the_in_process_band_sits_between_the_two_verdicts():
    print("test_the_in_process_band_sits_between_the_two_verdicts")
    # cnc holds IT9 naturally and can be pushed 2 grades in-process; IT8 and IT7
    # are a premium but not a new operation, IT6 is.
    def verdict(grade):
        band = tc.band_for_grade(25.0, grade)
        return tc.tolerance_cost_check(
            [{"nominal": 25.0, "tol": band / 2.0}], "cnc")["links"][0]["verdict"]
    _check("IT9 -> ok", verdict(9.0), tc.OK)
    _check("IT8 -> in-process", verdict(8.0), tc.IN_PROCESS)
    _check("IT7 -> in-process (the last grade before a new op)", verdict(7.0),
           tc.IN_PROCESS)
    _check("IT6 -> secondary operation", verdict(6.0), tc.SECONDARY)


def test_a_zero_band_dimension_is_reported_not_priced():
    print("test_a_zero_band_dimension_is_reported_not_priced")
    # A basic dimension is held by its geometric control; charging it here would
    # double-count, and silently dropping it would hide it.
    res = tc.tolerance_cost_check(
        [{"name": "basic", "nominal": 30.0, "plus": 0.0, "minus": 0.0},
         {"name": "real", "nominal": 30.0, "tol": 0.05}], "cnc")
    _check("verdict says so", res["links"][0]["verdict"], tc.UNTOLERANCED)
    _check("no IT grade", res["links"][0]["it_grade"], None)
    _check("not priced", res["links"][0]["cost_index"], None)
    _check("the total counts only the priced link", res["total_cost_index"],
           res["links"][1]["cost_index"])


def test_the_total_index_compares_two_schemes():
    print("test_the_total_index_compares_two_schemes")
    # The point of returning a total: scheme A vs scheme B on the same chain.
    tight = [{"name": "a", "nominal": 25.0, "tol": 0.0065},
             {"name": "b", "nominal": 25.0, "tol": 0.0065}]
    loose = [{"name": "a", "nominal": 25.0, "tol": 0.026},
             {"name": "b", "nominal": 25.0, "tol": 0.026}]
    t = tc.tolerance_cost_check(tight, "cnc")
    ell = tc.tolerance_cost_check(loose, "cnc")
    _ok("the tight scheme costs strictly more",
        t["total_cost_index"] > ell["total_cost_index"],
        f"{t['total_cost_index']} vs {ell['total_cost_index']}")
    _check("a chain at natural capability totals exactly n_links",
           tc.tolerance_cost_check(
               [{"nominal": 25.0, "tol": tc.band_for_grade(25.0, 9.0) / 2.0}] * 3,
               "cnc")["total_cost_index"], 3.0)


def test_check_labels_its_fidelity_and_rejects_bad_input():
    print("test_check_labels_its_fidelity_and_rejects_bad_input")
    res = tc.tolerance_cost_check([{"nominal": 25.0, "tol": 0.05}], "cnc")
    _check("fidelity", res["fidelity"], "correlation")
    _check("band", res["band_pct"], 50.0)
    _check("escalates to the loosen loop", res["escalate_to"], "suggest_loosening")
    _ok("the basis states the doubling rate", "doubles every 1.5" in res["basis"],
        res["basis"])
    for label, fn in (
        ("an empty chain raises", lambda: tc.tolerance_cost_check([], "cnc")),
        ("an unknown process raises",
         lambda: tc.tolerance_cost_check([{"nominal": 1.0, "tol": 0.1}], "wishful")),
    ):
        try:
            fn()
        except ValueError:
            _ok(label, True)
        else:
            _ok(label, False, "no ValueError")


# --- the loosen loop ----------------------------------------------------------
#
# The golden chain: a shaft, a spacer and a housing bore stacked into an end-float
# spec of +/-0.35 mm. Every link is drawn far tighter than the spec needs — the
# classic over-toleranced print the loop exists to fix.

def _golden():
    return [
        {"name": "shaft", "nominal": 50.0, "tol": 0.008},      # IT6-ish
        {"name": "spacer", "nominal": 10.0, "tol": 0.010},
        {"name": "housing", "nominal": 60.0, "tol": 0.030, "direction": -1},
    ]


def _golden_spec():
    return dict(spec_min=-0.35, spec_max=0.35)


def test_loosening_lowers_cost_while_the_stack_still_passes():
    print("test_loosening_lowers_cost_while_the_stack_still_passes")
    res = tc.suggest_loosening(_golden(), **_golden_spec())
    _check("the loop ran", res["ok"], True)
    _ok("it found moves", len(res["steps"]) > 0, res["note"])
    _ok("cost strictly fell",
        res["cost_index_after"] < res["cost_index_before"],
        f"{res['cost_index_before']} -> {res['cost_index_after']}")
    _ok(f"and by a lot ({res['saving_pct']:.0f}%)", res["saving_pct"] > 25.0)

    # The gate that matters: the SUGGESTED SCHEME, re-stacked independently, still
    # clears the target. Not the loop's own bookkeeping — a fresh stackup call.
    check = tolerance.stackup(res["chain"], method="montecarlo", samples=20000,
                              **_golden_spec())
    _ok(f"the suggested scheme re-stacks at cpk {check['montecarlo']['cpk']}",
        check["montecarlo"]["cpk"] >= res["target_cpk"],
        f"cpk {check['montecarlo']['cpk']} < {res['target_cpk']}")
    _check("and every unit is in spec", check["montecarlo"]["pct_in_spec"], 100.0)


def test_loosening_preserves_each_links_mean():
    print("test_loosening_preserves_each_links_mean")
    # If loosening shifted a link's mean, the stack's nominal would move and the
    # cpk the loop is protecting would change for the wrong reason.
    before = _golden()
    res = tc.suggest_loosening(before, **_golden_spec())
    for b, a in zip(before, res["chain"]):
        nb, pb, mb, _ = tolerance._devs(b)
        na, pa, ma, _ = tolerance._devs(a)
        _near(f"{b['name']}: mean unchanged", na + (pa + ma) / 2.0,
              nb + (pb + mb) / 2.0, 1e-9)


def test_loosening_opens_the_most_expensive_link_first():
    print("test_loosening_opens_the_most_expensive_link_first")
    res = tc.suggest_loosening(_golden(), **_golden_spec())
    _check("the tightest (dearest) link goes first", res["steps"][0]["link"],
           "shaft")
    savings = [s["saving"] for s in res["steps"]]
    _ok("and the savings are non-increasing (greedy on cost)",
        all(a >= b - 1e-9 for a, b in zip(savings, savings[1:])), savings)


def test_an_already_loose_chain_returns_no_savings():
    print("test_an_already_loose_chain_returns_no_savings")
    # The honest-refusal gate: this chain's links are already so wide that opening
    # any one of them another grade drops cpk under target. The loop must say
    # "nothing to do", not manufacture a saving.
    # Sized deliberately: as drawn the stack sits just above cpk 1.33, and opening
    # EITHER link by one IT grade (a x1.585 band) drops it under.
    chain = [{"name": "a", "nominal": 50.0, "tol": 0.10},
             {"name": "b", "nominal": 10.0, "tol": 0.10}]
    res = tc.suggest_loosening(chain, spec_min=59.80, spec_max=60.20)
    _ok(f"the fixture really does start with margin (cpk {res['cpk_before']})",
        1.33 <= res["cpk_before"] < 1.6, res["cpk_before"])
    _check("the loop ran", res["ok"], True)
    _check("no steps", res["steps"], [])
    _check("no saving", res["saving"], 0.0)
    _check("it converged rather than hitting the cap", res["stopped"],
           "no_further_move")
    _check("the cost index is unchanged", res["cost_index_after"],
           res["cost_index_before"])
    _ok("and the note says why", "already as loose" in res["note"], res["note"])


def test_a_chain_with_no_margin_is_refused_not_loosened():
    print("test_a_chain_with_no_margin_is_refused_not_loosened")
    # cpk is already under target: loosening would make a failing stack worse. The
    # only honest answer is to refuse and say which direction the fix lies in.
    chain = [{"name": "a", "nominal": 50.0, "tol": 0.20},
             {"name": "b", "nominal": 10.0, "tol": 0.20}]
    res = tc.suggest_loosening(chain, spec_min=59.9, spec_max=60.1)
    _check("refused", res["ok"], False)
    _check("with no steps", res["steps"], [])
    _check("no saving invented", res["saving"], 0.0)
    _check("reason is recorded", res["stopped"], "no_margin")
    _ok("and it points the other way", "do not loosen" in res["note"], res["note"])


def test_the_loop_is_reproducible():
    print("test_the_loop_is_reproducible")
    # The cpk gate is Monte-Carlo; an unseeded RNG would make the recommended
    # scheme drift between runs, which is unusable in a design record.
    a = tc.suggest_loosening(_golden(), **_golden_spec())
    b = tc.suggest_loosening(_golden(), **_golden_spec())
    _check("two runs agree exactly", a, b)


def test_target_cpk_controls_how_far_it_goes():
    print("test_target_cpk_controls_how_far_it_goes")
    strict = tc.suggest_loosening(_golden(), target_cpk=3.0, **_golden_spec())
    lax = tc.suggest_loosening(_golden(), target_cpk=1.0, **_golden_spec())
    _ok("a laxer target saves at least as much",
        lax["saving"] >= strict["saving"], f"{lax['saving']} vs {strict['saving']}")
    _ok("and the strict run still clears its own target",
        strict["cpk_after"] >= 3.0, strict["cpk_after"])


# --- the cost_estimate hook ---------------------------------------------------

def test_cost_estimate_default_is_byte_identical_to_before():
    print("test_cost_estimate_default_is_byte_identical_to_before")
    # The contract: new inputs are optional and default to today's behaviour.
    r = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    _check("no tolerance class", r["breakdown"]["tolerance_class"], None)
    _check("factor is exactly 1", r["breakdown"]["tolerance_factor"], 1.0)
    _check("nothing applied", r["breakdown"]["tolerance_applied"], False)
    _check("time still comes from the table", r["breakdown"]["machine_time_basis"],
           "table")
    _check("machine time unchanged", r["breakdown"]["machine_time_hr"],
           r["breakdown"]["base_machine_time_hr"])
    _check("band unchanged", r["band_pct"], 100.0)


def test_tolerance_class_scales_machine_time_by_the_corpus_curve():
    print("test_tolerance_class_scales_machine_time_by_the_corpus_curve")
    base = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    tight = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                               tolerance_class="IT6")
    loose = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                               tolerance_class="IT12")
    _near("IT6 multiplies machine time by the IT6/IT9 ratio",
          tight["breakdown"]["machine_time_hr"]
          / base["breakdown"]["machine_time_hr"], 4.0, 1e-3)
    _ok("IT12 is cheaper than the untoleranced default",
        loose["breakdown"]["machine_time_hr"]
        < base["breakdown"]["machine_time_hr"])
    _ok("and the unit cost follows", tight["unit_cost"] > base["unit_cost"]
        > loose["unit_cost"])
    _check("the breakdown records what was charged",
           tight["breakdown"]["tolerance_applied"], True)
    # accepted spellings, and a rejection
    for spelling in ("IT7", "it7", "7", 7, 7.0):
        _check(f"{spelling!r} parses", tc.parse_tolerance_class(spelling), 7.0)
    for bad in ("H7", "tight", 2.0, 20.0):
        try:
            tc.parse_tolerance_class(bad)
        except ValueError:
            _ok(f"{bad!r} rejected", True)
        else:
            _ok(f"{bad!r} rejected", False, "accepted a bad tolerance class")


def test_a_supplied_machine_time_replaces_the_table_and_tightens_the_band():
    print("test_a_supplied_machine_time_replaces_the_table_and_tightens_the_band")
    r = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                           machine_time_hr=0.5)
    _check("time is the supplied one", r["breakdown"]["machine_time_hr"], 0.5)
    _check("basis says so", r["breakdown"]["machine_time_basis"], "supplied")
    _check("band tightened", r["band_pct"], 50.0)
    _near("machining cost follows the supplied time", r["breakdown"]["machining_cost"],
          0.5 * 60.0, 1e-6)
    # and the tolerance factor is NOT re-applied on top (cnc_time_estimate already
    # applied the same curve) — double-charging precision is the bug this prevents.
    both = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                              machine_time_hr=0.5, tolerance_class="IT6")
    _check("supplied time is untouched by the class",
           both["breakdown"]["machine_time_hr"], 0.5)
    _check("and it is recorded as not applied",
           both["breakdown"]["tolerance_applied"], False)
    _check("while the factor is still reported for the reader",
           both["breakdown"]["tolerance_factor"], 4.0)
    _check("an explicit band wins",
           cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                              machine_time_hr=0.5,
                              machine_time_band_pct=30.0)["band_pct"], 30.0)
    try:
        cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", machine_time_hr=0.0)
    except ValueError:
        _ok("a non-positive supplied time raises", True)
    else:
        _ok("a non-positive supplied time raises", False, "no ValueError")


def main():
    for fn in (
        test_it_grade_reproduces_the_iso_table,
        test_it_grade_and_band_are_inverses,
        test_extrapolation_past_the_table_tracks_the_r5_series,
        test_a_band_between_two_grades_reports_a_fraction,
        test_zero_and_offtable_inputs_raise,
        test_cost_index_is_strictly_monotone_across_the_whole_axis,
        test_it6_over_it9_for_a_machined_bore_is_in_the_handbook_band,
        test_the_curve_has_a_knee_at_natural_capability,
        test_cheapest_process_walks_the_operation_ladder,
        test_an_it6_bore_on_an_fdm_part_flags,
        test_an_it11_slot_on_cnc_does_not_flag,
        test_the_in_process_band_sits_between_the_two_verdicts,
        test_a_zero_band_dimension_is_reported_not_priced,
        test_the_total_index_compares_two_schemes,
        test_check_labels_its_fidelity_and_rejects_bad_input,
        test_loosening_lowers_cost_while_the_stack_still_passes,
        test_loosening_preserves_each_links_mean,
        test_loosening_opens_the_most_expensive_link_first,
        test_an_already_loose_chain_returns_no_savings,
        test_a_chain_with_no_margin_is_refused_not_loosened,
        test_the_loop_is_reproducible,
        test_target_cpk_controls_how_far_it_goes,
        test_cost_estimate_default_is_byte_identical_to_before,
        test_tolerance_class_scales_machine_time_by_the_corpus_curve,
        test_a_supplied_machine_time_replaces_the_table_and_tightens_the_band,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

"""Inspection artifacts (issue #232) — pure-core unit tests.

No FreeCAD, no worker, no LLM: :mod:`driftpin.inspection` is descriptor arithmetic
over the same dim descriptors the completeness gate consumes, so these run on the
host interpreter in milliseconds. They prove the three things the layer promises:

  * balloons are an IDENTITY — numbering is deterministic, re-running on an
    unchanged set reassigns nothing, adding a dimension appends rather than
    renumbers, and deleting one never recycles a number onto a different feature;
  * the method follows the tolerance — the gauge-maker's 10:1 rule walks a
    per-family instrument ladder, a bore never gets a micrometer, a GD&T control
    with datums always gets the CMM, and an untoleranced size is reported
    unmeasurable rather than silently passed;
  * evaluation is two-sided — an in-limits part is all-pass, and nudging exactly one
    measurement past its limit flips exactly that row.

Run: python3 tests/test_inspection.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import inspection as insp  # noqa: E402

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


# --- the golden fixture -------------------------------------------------------
#
# The plate from the drawing-gate suite, dimensioned the way a production print
# would be: 60x40x8 with a Ø12 H7 through hole located X/Y from the datum corner,
# plus a position tolerance on the hole and a flatness callout on the seating face.

def _dims():
    return [
        {"name": "Dim_W", "type": "DistanceX", "value": 60.0, "view": "Front",
         "plus": 0.2, "minus": -0.2},
        {"name": "Dim_D", "type": "DistanceY", "value": 40.0, "view": "Top",
         "plus": 0.2, "minus": -0.2},
        {"name": "Dim_T", "type": "DistanceY", "value": 8.0, "view": "Front",
         "plus": 0.1, "minus": -0.1},
        {"name": "Dim_Hole", "type": "Diameter", "value": 12.0, "view": "Top",
         "plus": 0.018, "minus": 0.0, "internal": True},        # Ø12 H7
        {"name": "Dim_HX", "type": "DistanceX", "value": 30.0, "view": "Top",
         "plus": 0.1, "minus": -0.1},
        {"name": "Dim_HY", "type": "DistanceY", "value": 20.0, "view": "Top",
         "plus": 0.1, "minus": -0.1},
    ]


def _gdt():
    return [
        {"name": "Fcf_Pos", "control": "position", "zone": 0.15,
         "datums": ["A", "B", "C"], "feature": "H1", "view": "Top"},
        {"name": "Fcf_Flat", "control": "flatness", "zone": 0.05, "datums": [],
         "feature": "F_seat", "view": "Front"},
    ]


def _plan(**kw):
    return insp.inspection_plan(_dims(), _gdt(), **kw)


# --- balloons are an identity -------------------------------------------------

def test_every_characteristic_gets_exactly_one_balloon():
    print("test_every_characteristic_gets_exactly_one_balloon")
    plan = _plan()
    chars = plan["characteristics"]
    _check("characteristic count == dims + gdt", plan["count"],
           len(_dims()) + len(_gdt()))
    balloons = [c["balloon"] for c in chars]
    _check("one balloon per characteristic", len(set(balloons)), len(chars))
    _check("balloons are 1..N with no gaps", sorted(balloons),
           list(range(1, len(chars) + 1)))
    _check("returned sorted by balloon", balloons, sorted(balloons))


def test_numbering_is_deterministic():
    print("test_numbering_is_deterministic")
    a = insp.balloon_map(_plan()["characteristics"])
    b = insp.balloon_map(_plan()["characteristics"])
    _check("two independent runs agree", a, b)
    # reversing the input order must not move a single balloon: the fresh-assignment
    # sort key is total, so numbering cannot depend on descriptor arrival order.
    rev = insp.inspection_plan(list(reversed(_dims())), list(reversed(_gdt())))
    _check("input order does not matter", insp.balloon_map(rev["characteristics"]), a)


def test_rerun_on_unchanged_page_reassigns_nothing():
    print("test_rerun_on_unchanged_page_reassigns_nothing")
    existing = insp.balloon_map(_plan()["characteristics"])
    again = insp.assign_balloons(insp.characteristics(_dims(), _gdt()), existing)
    _check("no new assignments", again["assigned"], [])
    _check("every characteristic kept its number", again["kept"], len(existing))
    _check("map is identical", insp.balloon_map(again["characteristics"]), existing)


def test_added_dimension_appends_and_never_renumbers():
    print("test_added_dimension_appends_and_never_renumbers")
    first = _plan()
    existing = insp.balloon_map(first["characteristics"])
    n = first["count"]
    # rev B adds a chamfer callout — a print an inspector already wrote against
    dims = _dims() + [{"name": "Dim_Cham", "type": "Distance", "value": 1.0,
                       "view": "Front", "plus": 0.1, "minus": -0.1}]
    second = insp.inspection_plan(dims, _gdt(), existing=existing)
    after = insp.balloon_map(second["characteristics"])
    _check("every pre-existing balloon unchanged",
           {k: after[k] for k in existing}, existing)
    _check("the new characteristic takes the next number", after["Dim_Cham"], n + 1)


def test_deleted_dimension_retires_its_number_forever():
    print("test_deleted_dimension_retires_its_number_forever")
    # rev A ballooned a chamfer; rev B deletes it and adds a different dimension.
    # If balloon numbers were recycled, an inspection record written against the
    # chamfer's balloon would silently read as the new feature.
    existing = insp.balloon_map(_plan()["characteristics"])
    gone = existing["Dim_Cham"] = max(existing.values()) + 1
    dims = _dims() + [{"name": "Dim_New", "type": "Distance", "value": 2.0,
                       "view": "Front", "plus": 0.1, "minus": -0.1}]
    plan = insp.inspection_plan(dims, _gdt(), existing=existing)
    after = insp.balloon_map(plan["characteristics"])
    _ok("the retired number is not reissued", after["Dim_New"] != gone,
        f"Dim_New took retired balloon {gone}")
    _check("it is reported as retired", plan["retired"], [gone])
    _check("the survivors keep their numbers",
           {k: after[k] for k in after if k != "Dim_New"},
           {k: v for k, v in existing.items() if k != "Dim_Cham"})


# --- method follows the tolerance --------------------------------------------

def _method_of(plan, source_id):
    for c in plan["characteristics"]:
        if c["id"] == source_id:
            return c["method"]["method"]
    return None


def test_method_ladder_tracks_the_tolerance_band():
    print("test_method_ladder_tracks_the_tolerance_band")
    plan = _plan()
    # ±0.2 on 60 -> band 0.4 -> needs 0.04 resolution -> a caliper (0.02) does it
    _check("loose linear -> caliper", _method_of(plan, "Dim_W"), "caliper")
    # ±0.1 -> band 0.2 -> needs 0.02 -> still a caliper, right at the boundary
    _check("moderate linear -> caliper", _method_of(plan, "Dim_T"), "caliper")
    # H7 on Ø12 -> band 0.018 -> needs 0.0018 -> too fine for a pin gauge (0.005)
    # and it is a BORE, so a micrometer is not on the ladder at all
    _check("H7 bore -> bore gauge", _method_of(plan, "Dim_Hole"), "bore gauge")
    # a datum-referenced control is CMM work no matter how loose the zone
    _check("position|A|B|C -> CMM", _method_of(plan, "Fcf_Pos"), "CMM")
    # a datum-free form control is surface-plate work
    _check("flatness -> surface plate",
           _method_of(plan, "Fcf_Flat"), "surface plate + dial indicator")


def test_a_tight_external_size_climbs_to_the_micrometer():
    print("test_a_tight_external_size_climbs_to_the_micrometer")
    shaft = {"name": "D", "type": "Diameter", "value": 20.0, "view": "F",
             "plus": 0.0, "minus": -0.021, "internal": False}   # Ø20 h7 shaft
    plan = insp.inspection_plan([shaft])
    _check("h7 shaft -> micrometer", _method_of(plan, "D"), "micrometer")
    _check("external ladder chosen", plan["characteristics"][0]["method"]["family"],
           "shaft")


def test_ratio_is_settable():
    print("test_ratio_is_settable")
    d = {"name": "L", "type": "Distance", "value": 50.0, "view": "F",
         "plus": 0.05, "minus": -0.05}                    # band 0.1
    # 10:1 wants 0.01 resolution -> caliper (0.02) is too coarse -> micrometer
    _check("10:1 -> micrometer", _method_of(insp.inspection_plan([d]), "L"),
           "micrometer")
    # 4:1 wants 0.025 -> the caliper now qualifies
    _check("4:1 -> caliper", _method_of(insp.inspection_plan([d], ratio=4.0), "L"),
           "caliper")


def test_untoleranced_size_is_reported_unmeasurable():
    print("test_untoleranced_size_is_reported_unmeasurable")
    dims = _dims()
    dims[0].pop("plus")
    dims[0].pop("minus")
    plan = insp.inspection_plan(dims, _gdt())
    _check("plan is not ok", plan["ok"], False)
    codes = [u["code"] for u in plan["unmeasurable"]]
    _check("exactly one finding, no_tolerance", codes, ["no_tolerance"])
    _check("it names the untoleranced dim", plan["unmeasurable"][0]["id"], "Dim_W")
    _check("clean fixture is ok", _plan()["ok"], True)


def test_impossible_tolerance_is_flagged_not_silently_cmm_d():
    print("test_impossible_tolerance_is_flagged_not_silently_cmm_d")
    d = {"name": "L", "type": "Distance", "value": 10.0, "view": "F",
         "plus": 0.0005, "minus": -0.0005}    # band 0.001 -> needs 0.0001
    plan = insp.inspection_plan([d])
    _check("flagged", [u["code"] for u in plan["unmeasurable"]], ["no_instrument"])
    _check("still recommends the finest rung", _method_of(plan, "L"), "CMM")
    _ok("the reason says gauge R&R will be poor",
        "gauge R&R" in plan["unmeasurable"][0]["reason"])


def test_plan_labels_its_fidelity():
    print("test_plan_labels_its_fidelity")
    plan = _plan()
    _check("fidelity", plan["fidelity"], "correlation")
    _ok("basis names the rule", "gauge-maker" in plan["basis"], plan["basis"])
    _check("method histogram sums to the count", sum(plan["by_method"].values()),
           plan["count"])


# --- evaluation is two-sided --------------------------------------------------

def _nominal_results(chars):
    """Every characteristic measured dead on nominal (GD&T: zero deviation)."""
    out = {}
    for c in chars:
        out[c["balloon"]] = 0.0 if c["kind"] == insp.GEOMETRIC else c["nominal"]
    return out


def test_in_limits_part_is_all_pass():
    print("test_in_limits_part_is_all_pass")
    chars = _plan()["characteristics"]
    rows = insp.evaluate(chars, _nominal_results(chars))
    _check("every row passes", sorted({r["status"] for r in rows}), ["pass"])
    _ok("every margin is non-negative", all(r["margin"] >= 0 for r in rows))


def test_one_out_of_limit_measurement_flips_exactly_that_row():
    print("test_one_out_of_limit_measurement_flips_exactly_that_row")
    chars = _plan()["characteristics"]
    results = _nominal_results(chars)
    hole = next(c for c in chars if c["id"] == "Dim_Hole")
    results[hole["balloon"]] = hole["upper"] + 0.001      # 1 µm over the H7 limit
    rows = insp.evaluate(chars, results)
    failed = [r["balloon"] for r in rows if r["status"] == "fail"]
    _check("exactly the hole fails", failed, [hole["balloon"]])
    _check("everything else passes",
           len([r for r in rows if r["status"] == "pass"]), len(chars) - 1)


def test_limits_are_inclusive_at_the_boundary():
    print("test_limits_are_inclusive_at_the_boundary")
    chars = _plan()["characteristics"]
    hole = next(c for c in chars if c["id"] == "Dim_Hole")
    for label, val in (("at upper limit", hole["upper"]),
                       ("at lower limit", hole["lower"])):
        rows = insp.evaluate(chars, {hole["balloon"]: val})
        row = next(r for r in rows if r["balloon"] == hole["balloon"])
        _check(label, row["status"], "pass")


def test_position_accepts_an_xy_offset_like_gdt_check():
    print("test_position_accepts_an_xy_offset_like_gdt_check")
    chars = _plan()["characteristics"]
    pos = next(c for c in chars if c["id"] == "Fcf_Pos")   # zone 0.15 diametral
    # 2*hypot(0.05, 0.0) = 0.10 -> inside; 2*hypot(0.05, 0.05) = 0.1414 -> inside;
    # 2*hypot(0.06, 0.06) = 0.1697 -> outside
    inside = insp.evaluate(chars, {pos["balloon"]: {"x": 0.05, "y": 0.05}})
    outside = insp.evaluate(chars, {pos["balloon"]: {"x": 0.06, "y": 0.06}})
    _check("inside the zone",
           next(r for r in inside if r["balloon"] == pos["balloon"])["status"], "pass")
    _check("outside the zone",
           next(r for r in outside if r["balloon"] == pos["balloon"])["status"], "fail")


def test_mmc_bonus_widens_the_zone():
    print("test_mmc_bonus_widens_the_zone")
    gdt = [{"name": "P", "control": "position", "zone": 0.1, "datums": ["A"],
            "mmc_bonus": 0.05}]
    chars = insp.inspection_plan([], gdt)["characteristics"]
    b = chars[0]["balloon"]
    _check("0.12 is inside 0.1+0.05",
           insp.evaluate(chars, {b: 0.12})[0]["status"], "pass")
    _check("0.16 is outside 0.1+0.05",
           insp.evaluate(chars, {b: 0.16})[0]["status"], "fail")


def test_missing_measurement_is_not_a_silent_pass():
    print("test_missing_measurement_is_not_a_silent_pass")
    chars = _plan()["characteristics"]
    rows = insp.evaluate(chars, {})
    _check("all rows unevaluated", sorted({r["status"] for r in rows}),
           ["not_evaluated"])
    _check("with a reason", rows[0]["reason"], "no measurement supplied")


def test_untoleranced_characteristic_cannot_pass():
    print("test_untoleranced_characteristic_cannot_pass")
    dims = _dims()
    dims[0].pop("plus")
    dims[0].pop("minus")
    chars = insp.inspection_plan(dims, _gdt())["characteristics"]
    w = next(c for c in chars if c["id"] == "Dim_W")
    row = next(r for r in insp.evaluate(chars, {w["balloon"]: 60.0})
               if r["balloon"] == w["balloon"])
    _check("measured but not judged", row["status"], "not_evaluated")
    _check("measurement is still recorded", row["measured"], 60.0)


# --- the FAI report -----------------------------------------------------------

def test_reference_location_falls_back_from_view_to_sheet():
    print("test_reference_location_falls_back_from_view_to_sheet")
    # a page-level control frame is anchored to no view; AS9102 field 6 must still
    # say where to find it, so it falls back to the sheet.
    gdt = [{"name": "P", "control": "flatness", "zone": 0.05}]
    chars = insp.inspection_plan(_dims(), gdt)["characteristics"]
    rows = {r["Characteristic Designator"]: r["Reference Location"]
            for r in insp.fai_rows(chars, sheet="SHEET 1")["rows"]}
    _check("a view-anchored dim references its view", rows["60.00"], "Front")
    _check("a page-level frame references the sheet", rows["flatness 0.05"],
           "SHEET 1")
    override = insp.fai_rows(chars, reference="ZONE B3")["rows"]
    _check("an explicit reference wins everywhere",
           sorted({r["Reference Location"] for r in override}), ["ZONE B3"])


def test_fai_report_shape_and_disclaimer():
    print("test_fai_report_shape_and_disclaimer")
    chars = _plan()["characteristics"]
    rep = insp.fai_rows(chars)
    _check("one row per characteristic", len(rep["rows"]), len(chars))
    for f in ("Char No.", "Reference Location", "Characteristic Designator",
              "Requirement", "Results", "Designed / Qualified Tooling",
              "Nonconformance Number", "Notes"):
        _ok(f"AS9102 Form 3 field present: {f}", f in rep["columns"])
    _ok("disclaimer says it is not certified", "NOT a certified" in rep["disclaimer"])
    _check("blank form is unevaluated, not passing",
           rep["summary"], {"pass": 0, "fail": 0, "not_evaluated": len(chars)})
    _check("char numbers are the balloon numbers",
           [r["Char No."] for r in rep["rows"]], [c["balloon"] for c in chars])


def test_fai_requirement_column_reads_like_the_print():
    print("test_fai_requirement_column_reads_like_the_print")
    chars = _plan()["characteristics"]
    rep = insp.fai_rows(chars)
    req = {r["Characteristic Designator"]: r["Requirement"] for r in rep["rows"]}
    _check("symmetric tolerance", req["60.00"], "60.00 ±0.2")
    _check("asymmetric H7 hole", req["Ø12.00"], "Ø12.00 +0.018/0")
    _check("gdt frame carries its datums", req["position 0.15|A|B|C"],
           "position 0.15|A|B|C")


def test_fai_report_two_sided():
    print("test_fai_report_two_sided")
    chars = _plan()["characteristics"]
    results = _nominal_results(chars)
    good = insp.fai_rows(chars, results)
    _check("in-limits part passes the report", good["ok"], True)
    _check("summary all-pass", good["summary"]["pass"], len(chars))

    hole = next(c for c in chars if c["id"] == "Dim_Hole")
    results[hole["balloon"]] = hole["upper"] + 0.001
    bad = insp.fai_rows(chars, results)
    _check("one nonconformance flips the report", bad["ok"], False)
    _check("exactly one fail", bad["summary"]["fail"], 1)
    _check("and it is the hole row",
           [r["Char No."] for r in bad["rows"] if r["Status"] == "fail"],
           [hole["balloon"]])


def test_fai_csv_is_parseable_and_carries_identity():
    print("test_fai_csv_is_parseable_and_carries_identity")
    import csv
    import io
    chars = _plan()["characteristics"]
    text = insp.fai_csv(insp.fai_rows(chars, _nominal_results(chars)),
                        part="PN-1001", rev="B")
    _ok("disclaimer is in the preamble", text.startswith("# AS9102-SHAPED"))
    _ok("part number recorded", "# Part: PN-1001" in text)
    _ok("revision recorded", "# Revision: B" in text)
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(body))))
    _check("every characteristic survives the round trip", len(rows), len(chars))
    _check("statuses came through", sorted({r["Status"] for r in rows}), ["pass"])
    _check("methods came through", rows[0]["Measurement Method"] != "", True)


def test_csv_quoting_survives_a_comma_in_a_field():
    print("test_csv_quoting_survives_a_comma_in_a_field")
    import csv
    import io
    notes = [{"name": "N1", "feature": "FREEFORM1",
              "text": "profile per CAD model, see table 2"}]
    chars = insp.inspection_plan([], [], notes)["characteristics"]
    text = insp.fai_csv(insp.fai_rows(chars))
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(body))))
    _check("the comma did not split the row", len(rows), 1)
    _check("text intact", rows[0]["Requirement"],
           "profile per CAD model, see table 2")


def test_notes_are_attribute_characteristics():
    print("test_notes_are_attribute_characteristics")
    notes = [{"name": "N1", "feature": "FREEFORM1", "text": "profile per CAD model"}]
    plan = insp.inspection_plan(_dims(), _gdt(), notes)
    _check("the note is ballooned too", plan["count"],
           len(_dims()) + len(_gdt()) + 1)
    note = next(c for c in plan["characteristics"] if c["id"] == "N1")
    _check("inspected by attribute", note["method"]["method"], "attribute / visual")
    _ok("a note never lands in unmeasurable",
        "N1" not in [u["id"] for u in plan["unmeasurable"]])


def main():
    for fn in (
        test_every_characteristic_gets_exactly_one_balloon,
        test_numbering_is_deterministic,
        test_rerun_on_unchanged_page_reassigns_nothing,
        test_added_dimension_appends_and_never_renumbers,
        test_deleted_dimension_retires_its_number_forever,
        test_method_ladder_tracks_the_tolerance_band,
        test_a_tight_external_size_climbs_to_the_micrometer,
        test_ratio_is_settable,
        test_untoleranced_size_is_reported_unmeasurable,
        test_impossible_tolerance_is_flagged_not_silently_cmm_d,
        test_plan_labels_its_fidelity,
        test_in_limits_part_is_all_pass,
        test_one_out_of_limit_measurement_flips_exactly_that_row,
        test_limits_are_inclusive_at_the_boundary,
        test_position_accepts_an_xy_offset_like_gdt_check,
        test_mmc_bonus_widens_the_zone,
        test_missing_measurement_is_not_a_silent_pass,
        test_untoleranced_characteristic_cannot_pass,
        test_reference_location_falls_back_from_view_to_sheet,
        test_fai_report_shape_and_disclaimer,
        test_fai_requirement_column_reads_like_the_print,
        test_fai_report_two_sided,
        test_fai_csv_is_parseable_and_carries_identity,
        test_csv_quoting_survives_a_comma_in_a_field,
        test_notes_are_attribute_characteristics,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

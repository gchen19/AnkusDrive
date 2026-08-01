"""Inspection artifacts (issue #232) — end-to-end through the FreeCAD worker.

The pure core is unit-tested in test_inspection.py; this drives the real
integration shim on a live TechDraw page: characteristics read off actual
DrawViewDimensions and feature control frames, balloon numbers persisted on the
FreeCAD objects, the internal/external split resolved from the enumerated solid,
balloons rendered into the composed SVG, and the FAI report written to disk.

Builds the same 60×40×8 plate the drawing-gate suite uses (Ø12 H7 through hole)
and asserts:

  * balloon count == characteristic count == dimensions + GD&T callouts;
  * re-ballooning an unchanged page changes NOTHING (stability), and adding a
    dimension appends rather than renumbering;
  * the numbers survive a save/reopen round trip — they live on the document, not
    in worker memory;
  * a Ø on a hole is recognised as INTERNAL off the real solid and gets the bore
    ladder, not a micrometer;
  * balloons actually render into the exported SVG, and the legibility gate sees
    them;
  * two-sided: an in-limits part yields an all-pass FAI, and nudging one measured
    value past its limit flips exactly that row;
  * drawing_gate's require_ballooned fails an unballooned page and passes a
    ballooned one.

Run: .venv/bin/python3 tests/test_inspection_worker.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _eq(label, got, want):
    _check(label, got == want, f"got {got!r}, want {want!r}")


def _plate(w, name):
    """60×40×8 plate, Ø12 H7 through hole at (30, 20), fully dimensioned with
    tolerances — the print a production part would actually ship with.
    Returns (page, hole_edge_tag, part_handle)."""
    w.call("new_document", name=name)
    plate = w.call("add_primitive", kind="box", w=60, d=40, h=8)
    drill = w.call("add_primitive", kind="cylinder", r=6, h=8, placement=[30, 20, 0])
    part = w.call("boolean_op", op="cut", base=plate["handle"], tool=drill["handle"])
    edges = w.call("list_edges", handle=part["handle"])
    hole = next(e["tag"] for e in edges
                if e.get("radius") and abs(e["radius"] - 6.0) < 1e-6)
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=part["handle"],
           views=["Front", "Top"])
    w.call("add_dimension", page=page, view="Front", kind="horizontal",
           from_point=[0, 0, 0], to_point=[60, 0, 0], tolerance={"sym": 0.2})
    w.call("add_dimension", page=page, view="Front", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 0, 8], tolerance={"sym": 0.1})
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 40, 0], tolerance={"sym": 0.2})
    w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole,
           tolerance={"fit": "H7"})
    w.call("add_dimension", page=page, view="Top", kind="horizontal",
           from_point=[0, 20, 0], to_point=[30, 20, 0], tolerance={"sym": 0.1})
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[30, 0, 0], to_point=[30, 20, 0], tolerance={"sym": 0.1})
    return page, hole, part["handle"]


def _n_dims(w, page):
    return len(w.call("drawing_gate", page=page, process="prismatic")["dimensions"]) \
        if isinstance(w.call("drawing_gate", page=page,
                             process="prismatic")["dimensions"], list) \
        else w.call("drawing_gate", page=page, process="prismatic")["dimensions"]


# --- ballooning ---------------------------------------------------------------

def test_balloon_covers_every_characteristic():
    print("test_balloon_covers_every_characteristic")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_count")
        w.call("add_gdt_callout", page=page, control="position", zone=0.15,
               datums=["A", "B", "C"], feature="H1", x=20, y=50)
        w.call("add_gdt_callout", page=page, control="flatness", zone=0.05,
               x=20, y=40)
        res = w.call("balloon_drawing", page=page)
        # 6 dimensions + 2 feature control frames
        _eq("ballooned 8 characteristics", res["count"], 8)
        _eq("all newly assigned", len(res["assigned"]), 8)
        _eq("none retired", res["retired"], [])
        _eq("numbers are 1..8", sorted(res["balloons"].values()), list(range(1, 9)))
        plan = w.call("inspection_plan", page=page)
        _eq("plan agrees with the balloon count", plan["count"], res["count"])
        _eq("plan balloons match the stamped ones",
            sorted(c["balloon"] for c in plan["characteristics"]),
            sorted(res["balloons"].values()))
        _check("plan is inspectable", plan["ok"], plan["unmeasurable"])


def test_reballooning_an_unchanged_page_is_a_no_op():
    print("test_reballooning_an_unchanged_page_is_a_no_op")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_stable")
        first = w.call("balloon_drawing", page=page)
        second = w.call("balloon_drawing", page=page)
        _eq("nothing reassigned", second["assigned"], [])
        _eq("everything kept", second["kept"], first["count"])
        _eq("the map is identical", second["balloons"], first["balloons"])
        third = w.call("balloon_drawing", page=page)
        _eq("still identical on a third run", third["balloons"], first["balloons"])


def test_added_dimension_appends_without_renumbering():
    print("test_added_dimension_appends_without_renumbering")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_revb")
        before = w.call("balloon_drawing", page=page)
        # rev B adds a chamfer dimension to a print already in inspection's hands
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 8], to_point=[1, 0, 8], tolerance={"sym": 0.1})
        after = w.call("balloon_drawing", page=page)
        _eq("one more characteristic", after["count"], before["count"] + 1)
        _eq("exactly one new number", len(after["assigned"]), 1)
        kept = {k: v for k, v in after["balloons"].items() if k in before["balloons"]}
        _eq("every pre-existing balloon is unchanged", kept, before["balloons"])
        _eq("the new one took the next number",
            after["assigned"][0]["balloon"], before["count"] + 1)


def test_renumber_is_explicit_and_starts_over():
    print("test_renumber_is_explicit_and_starts_over")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_renum")
        w.call("add_gdt_callout", page=page, control="flatness", zone=0.05,
               x=20, y=40)
        first = w.call("balloon_drawing", page=page)
        again = w.call("balloon_drawing", page=page, renumber=True)
        _eq("everything reassigned", len(again["assigned"]), first["count"])
        _eq("numbering restarts at 1", min(again["balloons"].values()), 1)
        _eq("still one number per characteristic",
            sorted(again["balloons"].values()),
            list(range(1, first["count"] + 1)))


def test_balloons_survive_save_and_reopen():
    print("test_balloons_survive_save_and_reopen")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "ballooned.FCStd")
        with Worker() as w:
            page, _hole, _part = _plate(w, "insp_persist")
            before = w.call("balloon_drawing", page=page)
            w.call("save_document", path=path)
        with Worker() as w2:
            doc = w2.call("open_document", path=path)
            pages = [o for o in w2.call("list_objects", document=doc.get("name"))
                     if "Page" in str(o.get("type", ""))]
            _check("the page reopened", bool(pages), pages)
            handle = w2.call("register_handle", object=pages[0]["name"])["handle"]
            after = w2.call("balloon_drawing", page=handle)
            _eq("nothing reassigned after a reopen", after["assigned"], [])
            _eq("the numbers came back off disk",
                {k: v for k, v in after["balloons"].items() if k in before["balloons"]},
                before["balloons"])


# --- reading the real solid ---------------------------------------------------

def test_hole_diameter_is_recognised_as_internal():
    print("test_hole_diameter_is_recognised_as_internal")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_bore")
        w.call("balloon_drawing", page=page)
        plan = w.call("inspection_plan", page=page)
        dia = [c for c in plan["characteristics"] if c["type"] == "Diameter"]
        _eq("found the Ø characteristic", len(dia), 1)
        _check("read off the solid as internal", dia[0].get("internal") is True,
               dia[0])
        _eq("so it takes the bore ladder", dia[0]["method"]["family"], "bore")
        _check("and never a micrometer",
               "micrometer" not in dia[0]["method"]["method"], dia[0]["method"])
        # H7 on Ø12 is a 0.018 band -> 0.0018 required at 10:1 -> dial bore gauge
        _eq("H7 hole -> bore gauge", dia[0]["method"]["method"], "bore gauge")


def test_turned_step_diameter_is_external():
    print("test_turned_step_diameter_is_external")
    with Worker() as w:
        w.call("new_document", name="insp_shaft")
        c1 = w.call("add_primitive", kind="cylinder", r=10, h=30)
        c2 = w.call("add_primitive", kind="cylinder", r=6, h=20, placement=[0, 0, 30])
        shaft = w.call("boolean_op", op="fuse", base=c1["handle"], tool=c2["handle"])
        page = w.call("make_drawing_page", name="Page")["handle"]
        w.call("add_projection_group", page=page, body=shaft["handle"],
               views=["Front", "Top"])
        edges = w.call("list_edges", handle=shaft["handle"])
        big = next(e["tag"] for e in edges
                   if e.get("radius") and abs(e["radius"] - 10.0) < 1e-6)
        w.call("add_dimension", page=page, view="Top", kind="diameter", edge=big,
               tolerance={"sym": 0.01})       # band 0.02 -> needs 0.002
        plan = w.call("inspection_plan", page=page)
        dia = [c for c in plan["characteristics"] if c["type"] == "Diameter"]
        _eq("found the Ø characteristic", len(dia), 1)
        _check("an outer turned step is not internal",
               dia[0].get("internal") is not True, dia[0])
        _eq("so it takes the external ladder", dia[0]["method"]["family"], "shaft")
        _eq("and a micrometer measures it", dia[0]["method"]["method"], "micrometer")


def test_gdt_callout_round_trips_into_the_plan():
    print("test_gdt_callout_round_trips_into_the_plan")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_gdt")
        made = w.call("add_gdt_callout", page=page, control="position", zone=0.15,
                      datums=["A", "B", "C"], feature="H1", x=20, y=50)
        _check("frame text reads like a print", "⌖" in made["text"], made["text"])
        _check("zone is diametral on the frame", "Ø0.15" in made["text"],
               made["text"])
        w.call("balloon_drawing", page=page)
        plan = w.call("inspection_plan", page=page)
        pos = [c for c in plan["characteristics"] if c["kind"] == "gdt"]
        _eq("one geometric characteristic", len(pos), 1)
        _eq("control survived", pos[0]["gdt"]["control"], "position")
        _eq("datum frame survived", pos[0]["gdt"]["datums"], ["A", "B", "C"])
        _eq("datum-referenced control is CMM work", pos[0]["method"]["method"], "CMM")


def test_untoleranced_dimension_is_reported_unmeasurable():
    print("test_untoleranced_dimension_is_reported_unmeasurable")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_untol")
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 8], to_point=[10, 0, 8])       # no tolerance
        plan = w.call("inspection_plan", page=page)
        _check("plan is not ok", not plan["ok"], plan["unmeasurable"])
        _eq("flagged as untoleranced",
            [u["code"] for u in plan["unmeasurable"]], ["no_tolerance"])


# --- the balloons are really drawn --------------------------------------------

def test_balloons_render_into_the_exported_svg():
    print("test_balloons_render_into_the_exported_svg")
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            page, _hole, _part = _plate(w, "insp_svg")
            w.call("add_gdt_callout", page=page, control="flatness", zone=0.05,
                   x=20, y=40)
            w.call("fit_page", page=page)
            plain = os.path.join(td, "plain.svg")
            w.call("export_drawing", page=page, path=plain)
            before = Path(plain).read_text(encoding="utf-8")

            res = w.call("balloon_drawing", page=page)
            out = os.path.join(td, "ballooned.svg")
            w.call("export_drawing", page=page, path=out)
            after = Path(out).read_text(encoding="utf-8")

            # NB: the part geometry draws <circle> elements of its own (the hole is
            # one), so balloons are counted by their class, not by element type.
            _eq("an unballooned page draws no balloons",
                before.count('class="driftpin-balloon"'), 0)
            _eq("one balloon per characteristic",
                after.count('class="driftpin-balloon"'), res["count"])
            _check("the feature control frame is drawn as a frame",
                   "⏥" in after, "flatness symbol missing from the SVG")
            _check("the ballooned export is larger", len(after) > len(before))


def test_legibility_gate_sees_the_balloons():
    print("test_legibility_gate_sees_the_balloons")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_legible")
        w.call("fit_page", page=page)
        before = w.call("drawing_legibility", page=page)
        res = w.call("balloon_drawing", page=page)
        after = w.call("drawing_legibility", page=page)
        _eq("balloons are counted as placed labels",
            after["labels"], before["labels"] + res["count"])


# --- drawing_gate's release requirement ---------------------------------------

def test_require_ballooned_gate_is_two_sided():
    print("test_require_ballooned_gate_is_two_sided")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_gate")
        plain = w.call("drawing_gate", page=page, process="prismatic")
        _check("the drawing is manufacturing-complete to begin with",
               plain["ok"], plain["violations"])
        _check("and reports it is not ballooned", not plain["ballooned"]["ok"],
               plain["ballooned"])

        strict = w.call("drawing_gate", page=page, process="prismatic",
                        require_ballooned=True)
        codes = [v["code"] for v in strict["violations"]]
        _check("requiring balloons fails an unballooned print", not strict["ok"],
               strict["violations"])
        _check("with a not_ballooned code", "not_ballooned" in codes, codes)
        _eq("one violation per unballooned characteristic",
            codes.count("not_ballooned"), strict["ballooned"]["total"])

        w.call("balloon_drawing", page=page)
        passed = w.call("drawing_gate", page=page, process="prismatic",
                        require_ballooned=True)
        _check("and passes once ballooned", passed["ok"], passed["violations"])
        _check("coverage is complete", passed["ballooned"]["ok"],
               passed["ballooned"])


# --- the FAI report -----------------------------------------------------------

def _nominal_results(plan):
    out = {}
    for c in plan["characteristics"]:
        out[str(c["balloon"])] = 0.0 if c["kind"] == "gdt" else c["nominal"]
    return out


def test_fai_report_two_sided():
    print("test_fai_report_two_sided")
    with Worker() as w:
        page, _hole, _part = _plate(w, "insp_fai")
        w.call("add_gdt_callout", page=page, control="position", zone=0.15,
               datums=["A", "B", "C"], x=20, y=50)
        w.call("balloon_drawing", page=page)
        plan = w.call("inspection_plan", page=page)

        blank = w.call("fai_report", page=page)
        _eq("a blank form has a row per characteristic", len(blank["rows"]),
            plan["count"])
        # AS9102 field 6 must read like the print, not like FreeCAD's object names
        refs = {r["Reference Location"] for r in blank["rows"]}
        _check("reference locations are drawing view codes",
               refs <= {"Front", "Top", "Page"}, refs)
        _eq("and passes nothing silently", blank["summary"]["pass"], 0)
        _eq("every row is unevaluated", blank["summary"]["not_evaluated"],
            plan["count"])
        _check("the disclaimer rides along", "NOT a certified" in blank["disclaimer"],
               blank["disclaimer"])

        good = w.call("fai_report", page=page, results=_nominal_results(plan))
        _check("an in-limits part passes", good["ok"], good["summary"])
        _eq("all rows pass", good["summary"]["pass"], plan["count"])

        hole = next(c for c in plan["characteristics"] if c["type"] == "Diameter")
        results = _nominal_results(plan)
        results[str(hole["balloon"])] = hole["upper"] + 0.001    # 1 µm over H7
        bad = w.call("fai_report", page=page, results=results)
        _check("one bad measurement fails the report", not bad["ok"], bad["summary"])
        _eq("exactly one nonconformance", bad["summary"]["fail"], 1)
        _eq("and it is the hole row",
            [r["Char No."] for r in bad["rows"] if r["Status"] == "fail"],
            [hole["balloon"]])


def test_fai_report_writes_csv_and_pdf():
    print("test_fai_report_writes_csv_and_pdf")
    import csv
    import io
    with tempfile.TemporaryDirectory() as td:
        with Worker() as w:
            page, _hole, _part = _plate(w, "insp_files")
            w.call("set_title_block", page=page, part="PLATE-1001", rev="B",
                   material="6061-T6")
            w.call("balloon_drawing", page=page)
            plan = w.call("inspection_plan", page=page)
            results = _nominal_results(plan)

            out_csv = os.path.join(td, "fai.csv")
            rep = w.call("fai_report", page=page, results=results, path=out_csv)
            _check("csv written", os.path.getsize(out_csv) > 0, rep)
            _eq("identity came from the title block", rep["rev"], "B")
            text = Path(out_csv).read_text(encoding="utf-8")
            _check("disclaimer in the preamble", text.startswith("# AS9102-SHAPED"),
                   text[:60])
            _check("part number recorded", "PLATE-1001" in text)
            body = [ln for ln in text.splitlines() if not ln.startswith("#")]
            rows = list(csv.DictReader(io.StringIO("\n".join(body))))
            _eq("one csv row per characteristic", len(rows), plan["count"])
            _eq("statuses survived the round trip",
                sorted({r["Status"] for r in rows}), ["pass"])

            out_svg = os.path.join(td, "fai.svg")
            w.call("fai_report", page=page, results=results, path=out_svg)
            svg = Path(out_svg).read_text(encoding="utf-8")
            _check("svg table written", "AS9102-SHAPED" in svg, svg[:200])
            _check("the table names its characteristics",
                   "Char No." in svg and "Requirement" in svg)

            out_pdf = os.path.join(td, "fai.pdf")
            try:
                w.call("fai_report", page=page, results=results, path=out_pdf)
            except Exception as exc:
                _check("pdf export degrades gracefully without svglib",
                       "svglib" in str(exc) or "reportlab" in str(exc), str(exc))
            else:
                _check("pdf written", os.path.getsize(out_pdf) > 0)
                _check("it is a real pdf",
                       Path(out_pdf).read_bytes().startswith(b"%PDF"))


def main():
    for fn in (
        test_balloon_covers_every_characteristic,
        test_reballooning_an_unchanged_page_is_a_no_op,
        test_added_dimension_appends_without_renumbering,
        test_renumber_is_explicit_and_starts_over,
        test_balloons_survive_save_and_reopen,
        test_hole_diameter_is_recognised_as_internal,
        test_turned_step_diameter_is_external,
        test_gdt_callout_round_trips_into_the_plan,
        test_untoleranced_dimension_is_reported_unmeasurable,
        test_balloons_render_into_the_exported_svg,
        test_legibility_gate_sees_the_balloons,
        test_require_ballooned_gate_is_two_sided,
        test_fai_report_two_sided,
        test_fai_report_writes_csv_and_pdf,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

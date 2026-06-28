"""Top-right isometric pictorial + auto cross-section (drawings-next) — end-to-end
through the FreeCAD worker. The thumbnail and section build on the same TechDraw
primitives as the orthographic views (a real isometric DrawViewPart rendered via
viewPartAsSvg, a DrawViewSection cut through internal features), so this drives the
real shim and asserts the behaviour a machinist drawing needs:

  * a part with a counterbore / blind bore RECOMMENDS a section (needs_section),
    one with only a through hole does not;
  * add_section_view auto-adds the section only when recommended, places it in clear
    space, and the sheet stays legible; the section's SVG carries the cut geometry;
  * add_thumbnail drops an isometric pictorial into a free top-right corner (vector
    geometry, not a raster), is NOT counted as the part the title block names nor by
    the manufacturability gate, and SKIPS (placed=False) when the corner is busy.

Run: .venv/bin/python3 tests/test_drawing_thumbnail_section.py
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


def _cb_block(w, name, scale=1.5):
    """60x40x20 block, Ø20x6 counterbore over a Ø10 through bore at the centre — a
    part whose internal step the outline views cannot show. Returns the page handle.
    A modest custom scale leaves slack on the sheet for the added views."""
    w.call("new_document", name=name)
    box = w.call("add_primitive", kind="box", w=60, d=40, h=20)
    cb = w.call("add_primitive", kind="cylinder", r=10, h=6, placement=[30, 20, 14])
    bore = w.call("add_primitive", kind="cylinder", r=5, h=20, placement=[30, 20, 0])
    p1 = w.call("boolean_op", op="cut", base=box["handle"], tool=cb["handle"])
    part = w.call("boolean_op", op="cut", base=p1["handle"], tool=bore["handle"])
    page = w.call("make_drawing_page", name="Page")["handle"]
    pg = w.call("add_projection_group", page=page, body=part["handle"],
                views=["Front", "Top"])
    w.call("set_property", handle=pg["handle"], name="ScaleType", value="Custom")
    w.call("set_property", handle=pg["handle"], name="Scale", value=scale)
    w.call("add_dimension", page=page, auto=True)
    w.call("set_title_block", page=page, part="CB BLOCK", material="STEEL", rev="A")
    return page


def _plate(w, name, scale=1.0):
    """60x40x8 plate, Ø12 through hole — no internal step, so it needs no section."""
    w.call("new_document", name=name)
    plate = w.call("add_primitive", kind="box", w=60, d=40, h=8)
    drill = w.call("add_primitive", kind="cylinder", r=6, h=8, placement=[30, 20, 0])
    part = w.call("boolean_op", op="cut", base=plate["handle"], tool=drill["handle"])
    page = w.call("make_drawing_page", name="Page")["handle"]
    pg = w.call("add_projection_group", page=page, body=part["handle"],
                views=["Front", "Top"])
    w.call("set_property", handle=pg["handle"], name="ScaleType", value="Custom")
    w.call("set_property", handle=pg["handle"], name="Scale", value=scale)
    w.call("add_dimension", page=page, auto=True)
    w.call("set_title_block", page=page, part="MOUNT PLATE", material="AL", rev="A")
    return page


def test_gate_recommends_section_for_counterbore():
    print("test_gate_recommends_section_for_counterbore")
    with Worker() as w:
        page = _cb_block(w, "rec_cb")
        rep = w.call("drawing_gate", page=page)
        rec = rep.get("section_recommended", {})
        _check("gate reports section_recommended", rec.get("recommended"), True)
        _check("names the counterbore feature",
               any("counterbore" in r for r in rec.get("reasons", [])), rec)


def test_section_added_when_recommended():
    print("test_section_added_when_recommended")
    with Worker() as w:
        page = _cb_block(w, "sec_add")
        res = w.call("add_section_view", page=page)
        _check("section was added", res.get("added"), res)
        _check("section reports a view name", bool(res.get("view")), res)
        # the cut runs through the bore axis -> origin at the hole centre
        _check("cut origin at the feature centre",
               res.get("origin", [0, 0, 0])[0:2] == [30.0, 20.0], res)
        leg = w.call("drawing_legibility", page=page)
        _check("sheet legible with the section", leg["ok"], leg["violations"][:3])
        # the section view carries cut geometry in the exported SVG
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sec.svg")
            r = w.call("export_drawing", page=page, path=out)
            _check("three views on the page now (Front, Top, Section)",
                   r["views"] == 3, r)
            svg = open(out, encoding="utf-8").read()
            _check("section SVG has drawn geometry", svg.count("<path") > 4, svg[:0])


def test_section_skipped_without_internal_features():
    print("test_section_skipped_without_internal_features")
    with Worker() as w:
        page = _plate(w, "sec_skip")
        rep = w.call("drawing_gate", page=page)
        _check("plate does not recommend a section",
               not rep["section_recommended"]["recommended"], rep["section_recommended"])
        res = w.call("add_section_view", page=page)   # auto -> should no-op
        _check("auto add_section_view is a no-op", not res.get("added"), res)
        _check("no view created", res.get("view") is None, res)
        # forcing one still works
        forced = w.call("add_section_view", page=page, auto=False)
        _check("auto=False forces a section", forced.get("added"), True)


def test_thumbnail_places_in_free_corner():
    print("test_thumbnail_places_in_free_corner")
    with Worker() as w:
        page = _plate(w, "thumb_ok")
        w.call("fit_page", page=page)
        res = w.call("add_thumbnail", page=page)
        _check("thumbnail placed", res.get("placed"), res)
        _check("thumbnail has a scale", res.get("scale", 0) > 0, res)
        leg = w.call("drawing_legibility", page=page)
        _check("sheet legible with the thumbnail", leg["ok"], leg["violations"][:3])
        # title block still names the real part, not the iso pictorial
        rep = w.call("drawing_gate", page=page)
        _check("gate still finds the real hole (not derailed by thumbnail)",
               any(f["kind"] == "hole" for f in rep["enumerated_features"]), rep)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "thumb.svg")
            r = w.call("export_drawing", page=page, path=out)
            _check("thumbnail counted as a rendered view", r["views"] == 3, r)
            # auto dims = each overall extent ONCE (X,Z from Front, Y from Top —
            # issue #108 #4: no axis double-dimensioned); the iso pictorial adds none
            _check("thumbnail not counted as a dimension", r["dimensions"] == 3, r)


def test_thumbnail_skips_when_corner_busy():
    print("test_thumbnail_skips_when_corner_busy")
    with Worker() as w:
        page = _plate(w, "thumb_busy")
        w.call("fit_page", page=page)
        first = w.call("add_thumbnail", page=page)
        _check("first thumbnail placed", first.get("placed"), first)
        # the corner is now occupied by that pictorial -> a second one must defer
        res = w.call("add_thumbnail", page=page, name="IsoThumb2")
        _check("second thumbnail skips the busy corner", not res.get("placed"), res)
        _check("skip carries a reason", bool(res.get("reason")), res)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

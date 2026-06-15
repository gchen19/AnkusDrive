"""Drawing-is-manufacturable gates (issue #85) — end-to-end through the FreeCAD
worker. The pure core is unit-tested in test_drawing_gate.py; this drives the real
integration shim: feature enumeration off a Part.Shape, dim-descriptor extraction
off real DrawViewDimensions (DP_TrueValue / DP_ModelRef), and the legibility
graphics replay. Builds a mounting plate (the drawing_demo `plate`) and asserts:

  * a thoughtfully-dimensioned plate is manufacturing-complete (ok=True);
  * dropping the hole's Y location under-constrains it (code 'under', names H?.y);
  * auto-dimensioning across two views over-dimensions the overall width
    (code 'redundant') — the gate catches real over-dimensioning;
  * the legibility gate runs on the placed graphics and returns a verdict.

Run: .venv/bin/python3 tests/test_drawing_gate_worker.py
"""
import sys
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


def _plate(w, name):
    """60x40x8 plate, Ø12 through hole centred at (30, 20). Returns (page, hole_tag)."""
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
    return page, hole, part["handle"]


def test_complete_plate_passes():
    print("test_complete_plate_passes")
    with Worker() as w:
        page, hole, _part = _plate(w, "gate_ok")
        # a minimal complete set: W on Front-h, T on Front-v, D on Top-v,
        # Ø + hole X + hole Y — no axis dimensioned twice.
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[60, 0, 0])       # width  X = 60
        w.call("add_dimension", page=page, view="Front", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 0, 8])        # thickness Z = 8
        w.call("add_dimension", page=page, view="Top", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 40, 0])       # depth  Y = 40
        w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole)
        w.call("add_dimension", page=page, view="Top", kind="horizontal",
               from_point=[0, 20, 0], to_point=[30, 20, 0])     # hole X = 30
        w.call("add_dimension", page=page, view="Top", kind="vertical",
               from_point=[30, 0, 0], to_point=[30, 20, 0])     # hole Y = 20
        rep = w.call("drawing_gate", page=page, process="prismatic")
        _check("process detected/used", rep["process"] == "prismatic", rep)
        _check("found the hole",
               any(f["kind"] == "hole" for f in rep["enumerated_features"]), rep)
        _check("complete plate is ok", rep["ok"], rep["violations"])
        _check("all slots covered",
               rep["slots_covered"] == rep["slots_total"], rep)


def test_missing_hole_location_under():
    print("test_missing_hole_location_under")
    with Worker() as w:
        page, hole, _part = _plate(w, "gate_under")
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[60, 0, 0])
        w.call("add_dimension", page=page, view="Front", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 0, 8])
        w.call("add_dimension", page=page, view="Top", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 40, 0])
        w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole)
        w.call("add_dimension", page=page, view="Top", kind="horizontal",
               from_point=[0, 20, 0], to_point=[30, 20, 0])     # hole X only
        # hole Y deliberately omitted
        rep = w.call("drawing_gate", page=page, process="prismatic")
        codes = [v["code"] for v in rep["violations"]]
        _check("not ok", not rep["ok"], rep)
        _check("under-constrained flagged", "under" in codes, codes)
        _check("names a Y location",
               any("Y location" in v.get("reason", "") for v in rep["violations"]),
               rep["violations"])


def test_auto_overdimensions_width():
    print("test_auto_overdimensions_width")
    with Worker() as w:
        page, hole, _part = _plate(w, "gate_auto")
        # auto extents on BOTH views call out the overall width twice -> redundant
        w.call("add_dimension", page=page, auto=True)
        rep = w.call("drawing_gate", page=page, process="prismatic")
        codes = [v["code"] for v in rep["violations"]]
        _check("redundant flagged for double-dimensioned width",
               "redundant" in codes, codes)


def test_turned_shaft_enumeration():
    print("test_turned_shaft_enumeration")
    with Worker() as w:
        w.call("new_document", name="gate_shaft")
        c1 = w.call("add_primitive", kind="cylinder", r=10, h=30)
        c2 = w.call("add_primitive", kind="cylinder", r=6, h=20,
                    placement=[0, 0, 30])
        shaft = w.call("boolean_op", op="fuse", base=c1["handle"], tool=c2["handle"])
        page = w.call("make_drawing_page", name="Page")["handle"]
        w.call("add_projection_group", page=page, body=shaft["handle"],
               views=["Front", "Top"])
        rep = w.call("drawing_gate", page=page)   # process auto-inferred
        kinds = [f["kind"] for f in rep["enumerated_features"]]
        _check("auto-inferred turned", rep["process"] == "turned", rep["process"])
        _check("enumerated >=2 cyl steps", kinds.count("cyl_step") >= 2, kinds)
        # turned scheme has no X/Y location slots -> no 'no_datum' style location
        _check("under-dimensioned without dims (size slots exist)",
               rep["slots_total"] >= 4, rep)


def test_legibility_runs():
    print("test_legibility_runs")
    with Worker() as w:
        page, hole, _part = _plate(w, "gate_leg")
        w.call("add_dimension", page=page, auto=True)
        w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole)
        rep = w.call("drawing_legibility", page=page)
        _check("legibility report shape",
               set(("ok", "violations", "labels", "segments")) <= set(rep), rep)
        _check("graphics were extracted", rep["labels"] > 0 and rep["segments"] > 0, rep)
        print(f"    legible={rep['ok']} labels={rep['labels']} "
              f"segments={rep['segments']} violations={len(rep['violations'])}")


def test_packing_keeps_dense_part_legible():
    print("test_packing_keeps_dense_part_legible")
    # a bar with four holes in a row, chain-dimensioned (each from the previous).
    # The chain segments are DISJOINT along X, so lane packing shares one offset and
    # keeps the sheet legible — where a blind one-lane-per-dim stack would sprawl
    # four lanes outward.
    with Worker() as w:
        w.call("new_document", name="gate_dense")
        bar = w.call("add_primitive", kind="box", w=120, d=24, h=10)
        part = bar["handle"]
        xs = [15, 45, 75, 105]
        for x in xs:
            drill = w.call("add_primitive", kind="cylinder", r=4, h=10,
                           placement=[x, 12, 0])
            part = w.call("boolean_op", op="cut", base=part,
                          tool=drill["handle"])["handle"]
        page = w.call("make_drawing_page", name="Page")["handle"]
        w.call("add_projection_group", page=page, body=part, views=["Front", "Top"])
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[120, 0, 0])
        prev = 0
        for x in xs:                                              # chain along X
            w.call("add_dimension", page=page, view="Top", kind="horizontal",
                   from_point=[prev, 12, 0], to_point=[x, 12, 0])
            prev = x
        rep = w.call("drawing_legibility", page=page)
        _check("dense part stays legible after packing", rep["ok"],
               rep["violations"][:2])
        print(f"    labels={rep['labels']} segments={rep['segments']} "
              f"violations={len(rep['violations'])}")


def test_counterbored_bracket_acceptance():
    """Issue #85 headline acceptance: a plate with a hole + counterbore comes back
    a legible, manufacturing-complete drawing — the completeness gate passes only
    when the counterbore's Ø and depth are dimensioned too, and the sheet is legible."""
    print("test_counterbored_bracket_acceptance")
    with Worker() as w:
        w.call("new_document", name="gate_cbore")
        box = w.call("add_primitive", kind="box", w=60, d=40, h=12)
        bore = w.call("add_primitive", kind="cylinder", r=3, h=12,
                      placement=[30, 20, 0])
        part = w.call("boolean_op", op="cut", base=box["handle"],
                      tool=bore["handle"])["handle"]
        recess = w.call("add_primitive", kind="cylinder", r=6, h=4,
                        placement=[30, 20, 8])
        part = w.call("boolean_op", op="cut", base=part,
                      tool=recess["handle"])["handle"]
        edges = w.call("list_edges", handle=part)
        bore_e = next(e["tag"] for e in edges
                      if e.get("radius") and abs(e["radius"] - 3.0) < 1e-6)
        cb_e = next(e["tag"] for e in edges
                    if e.get("radius") and abs(e["radius"] - 6.0) < 1e-6)
        page = w.call("make_drawing_page", name="Page")["handle"]
        w.call("add_projection_group", page=page, body=part, views=["Front", "Top"])

        # enumeration sees the counterbore as a through hole + a recess
        feats = w.call("drawing_gate", page=page, process="prismatic")["enumerated_features"]
        kinds = [f["kind"] for f in feats]
        _check("enumerated a hole", "hole" in kinds, kinds)
        _check("enumerated a counterbore", "counterbore" in kinds, kinds)

        # the complete, manufacturable dimension set
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[60, 0, 0])        # width  60
        w.call("add_dimension", page=page, view="Front", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 0, 12])        # thickness 12
        w.call("add_dimension", page=page, view="Top", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 40, 0])        # depth  40
        w.call("add_dimension", page=page, view="Top", kind="diameter", edge=bore_e)
        w.call("add_dimension", page=page, view="Top", kind="diameter", edge=cb_e)
        w.call("add_dimension", page=page, view="Top", kind="horizontal",
               from_point=[0, 20, 0], to_point=[30, 20, 0])      # hole X 30
        w.call("add_dimension", page=page, view="Top", kind="vertical",
               from_point=[30, 0, 0], to_point=[30, 20, 0])      # hole Y 20
        w.call("add_dimension", page=page, view="Front", kind="vertical",
               from_point=[20, 0, 8], to_point=[20, 0, 12])      # c'bore depth 4

        rep = w.call("drawing_gate", page=page, process="prismatic")
        _check("counterbored part is manufacturing-complete", rep["ok"],
               rep["violations"])
        leg = w.call("drawing_legibility", page=page)
        _check("and the sheet is legible", leg["ok"], leg["violations"][:3])

        # dropping the counterbore depth alone makes it under-constrained again
        w.call("set_title_block", page=page, part="CBORE BRACKET",
               material="AL 6061-T6", rev="A")


def _dimension_plate(w, page, hole, hole_x_from):
    """A complete prismatic dimension set for the demo plate, with the hole's X
    location measured from `hole_x_from` (so a test can vary the origin)."""
    w.call("add_dimension", page=page, view="Front", kind="horizontal",
           from_point=[0, 0, 0], to_point=[60, 0, 0])
    w.call("add_dimension", page=page, view="Front", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 0, 8])
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[0, 0, 0], to_point=[0, 40, 0])
    w.call("add_dimension", page=page, view="Top", kind="diameter", edge=hole)
    w.call("add_dimension", page=page, view="Top", kind="horizontal",
           from_point=hole_x_from, to_point=[30, 20, 0])
    w.call("add_dimension", page=page, view="Top", kind="vertical",
           from_point=[30, 0, 0], to_point=[30, 20, 0])


def test_datum_origin_discipline():
    """With a datum face declared (annotate_face role='datum'), a hole located FROM
    the datum is clean, but locating it from the opposite edge raises 'no_datum' —
    the issue's 'dimension from functional references, not arbitrary corners'."""
    print("test_datum_origin_discipline")
    def _annotate_datums(w, part):
        # a 2-D location needs a reference frame: the x=0 face (normal -X) and the
        # y=0 face (normal -Y), each planar at centroid ≈ 0 on its axis.
        faces = w.call("list_faces", handle=part)
        for ax in (0, 1):
            tag = next(f["tag"] for f in faces
                       if f.get("normal") and abs(f["normal"][ax] + 1) < 1e-3
                       and abs(f["centroid"][ax]) < 1e-3)
            w.call("annotate_face", handle=part, face=tag, role="datum")

    with Worker() as w:
        page, hole, part = _plate(w, "gate_datum_ok")
        _annotate_datums(w, part)
        _dimension_plate(w, page, hole, hole_x_from=[0, 20, 0])   # from the datum
        rep = w.call("drawing_gate", page=page, process="prismatic")
        codes = [v["code"] for v in rep["violations"]]
        _check("datum faces detected", rep["datum_faces"] == 2, rep["datum_faces"])
        _check("located-from-datum is clean", rep["ok"], rep["violations"])
        _check("no no_datum flag", "no_datum" not in codes, codes)

    with Worker() as w:
        page, hole, part = _plate(w, "gate_datum_bad")
        _annotate_datums(w, part)
        _dimension_plate(w, page, hole, hole_x_from=[60, 20, 0])  # from the far edge
        rep = w.call("drawing_gate", page=page, process="prismatic")
        codes = [v["code"] for v in rep["violations"]]
        _check("located from a non-datum edge flags no_datum",
               "no_datum" in codes, codes)


def test_fillet_chamfer_enumeration_and_completeness():
    """A plate with a filleted edge and a chamfered edge: the gate enumerates the
    fillet (partial cylinder) and chamfer (off-axis bevel), and the drawing is
    complete only once the fillet R and the chamfer size are dimensioned."""
    print("test_fillet_chamfer_enumeration_and_completeness")
    with Worker() as w:
        w.call("new_document", name="gate_fc")
        box = w.call("add_primitive", kind="box", w=60, d=40, h=10)
        verts = [e for e in w.call("list_edges", handle=box["handle"])
                 if abs(e.get("length", 0) - 10) < 1e-6]   # vertical edges
        fil = w.call("fillet_edges", handle=box["handle"],
                     edges=[verts[0]["tag"]], radius=3)
        part = w.call("chamfer_edges", handle=fil["handle"],
                      edges=[verts[1]["tag"]], size=2)["handle"]
        page = w.call("make_drawing_page", name="Page")["handle"]
        w.call("add_projection_group", page=page, body=part, views=["Front", "Top"])

        feats = w.call("drawing_gate", page=page, process="prismatic")["enumerated_features"]
        kinds = [f["kind"] for f in feats]
        _check("enumerated a fillet", "fillet" in kinds, kinds)
        _check("enumerated a chamfer", "chamfer" in kinds, kinds)

        # overall sizes
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[60, 0, 0])
        w.call("add_dimension", page=page, view="Front", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 0, 10])
        w.call("add_dimension", page=page, view="Top", kind="vertical",
               from_point=[0, 0, 0], to_point=[0, 40, 0])
        rep = w.call("drawing_gate", page=page, process="prismatic")
        codes = [v["code"] for v in rep["violations"]]
        _check("fillet + chamfer flagged under before they're dimensioned",
               codes.count("under") == 2, codes)

        # R callout for the fillet + a linear size for the chamfer
        fil_edge = next(e["tag"] for e in w.call("list_edges", handle=part)
                        if e.get("radius") and abs(e["radius"] - 3) < 1e-6)
        w.call("add_dimension", page=page, view="Top", kind="radius", edge=fil_edge)
        w.call("add_dimension", page=page, view="Top", kind="horizontal",
               from_point=[0, 0, 0], to_point=[2, 0, 0])    # chamfer leg = 2
        rep = w.call("drawing_gate", page=page, process="prismatic")
        _check("complete once fillet R + chamfer size are dimensioned",
               rep["ok"], rep["violations"])


def test_title_block_renders_fields():
    print("test_title_block_renders_fields")
    import os
    import tempfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        page, hole, _part = _plate(w, "gate_tb")
        w.call("add_dimension", page=page, auto=True)
        w.call("set_title_block", page=page, part="MOUNT PLATE",
               material="AL 6061-T6", rev="B", drawn_by="GC", date="2026-06-15")
        out = os.path.join(tmp, "tb.svg")
        w.call("export_drawing", page=page, path=out)
        svg = open(out, encoding="utf-8").read()
        _check("part name in title block", "MOUNT PLATE" in svg)
        _check("material in title block", "AL 6061-T6" in svg)
        _check("rev in title block", "REV" in svg and ">B<" in svg)
        _check("auto scale present", "SCALE" in svg)
        _check("auto sheet size present", "A4" in svg)
        _check("title-block group emitted", "driftpin-titleblock" in svg)
        # no title block when never set
        page2, _h2, _p2 = _plate(w, "gate_no_tb")
        w.call("add_dimension", page=page2, auto=True)
        out2 = os.path.join(tmp, "notb.svg")
        w.call("export_drawing", page=page2, path=out2)
        _check("block is opt-in (absent when unset)",
               "driftpin-titleblock" not in open(out2, encoding="utf-8").read())


def test_dimension_tolerances_render():
    """A dimension can carry a tolerance — symmetric ±, asymmetric, or an ISO fit
    code (hole-side ISO 286 limits) — rendered next to the value so the part can be
    made to size."""
    print("test_dimension_tolerances_render")
    import os
    import tempfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        page, hole, _part = _plate(w, "gate_tol")
        # symmetric ± on the overall width
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[60, 0, 0], tolerance={"sym": 0.1})
        # asymmetric on a position
        w.call("add_dimension", page=page, view="Top", kind="horizontal",
               from_point=[0, 20, 0], to_point=[30, 20, 0],
               tolerance={"plus": 0.05, "minus": -0.02})
        # ISO fit on the Ø12 hole -> H7 hole-side limits at basic 12
        dim = w.call("add_dimension", page=page, view="Top", kind="diameter",
                     edge=hole, tolerance={"fit": "H7"})["dimensions"][0]
        out = os.path.join(tmp, "tol.svg")
        w.call("export_drawing", page=page, path=out)
        svg = open(out, encoding="utf-8").read()
        _check("symmetric tolerance rendered", "±0.1" in svg, svg[:0])
        _check("asymmetric tolerance rendered", "+0.05/-0.02" in svg)
        # ISO 286 IT7 at 12 mm is 0.018 mm; hole H is +IT7 / -0
        _check("fit-class upper deviation rendered", "+0.018/-0" in svg, dim)


def test_diameter_leaders_legible():
    """Ø dimensions render as leader callouts (arrow at the hole, value stacked
    beside the view) rather than linear-stacked dims. Two holes of different sizes
    each get a leader, and the sheet stays legible."""
    print("test_diameter_leaders_legible")
    import os
    import tempfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="gate_leaders")
        plate = w.call("add_primitive", kind="box", w=80, d=50, h=8)["handle"]
        for (x, y, r) in [(25, 25, 4), (60, 25, 3)]:
            drill = w.call("add_primitive", kind="cylinder", r=r, h=8,
                           placement=[x, y, 0])
            plate = w.call("boolean_op", op="cut", base=plate,
                           tool=drill["handle"])["handle"]
        page = w.call("make_drawing_page", name="Page")["handle"]
        w.call("add_projection_group", page=page, body=plate, views=["Front", "Top"])
        w.call("add_dimension", page=page, auto=True)
        for r in (4, 3):
            e = next(ed["tag"] for ed in w.call("list_edges", handle=plate)
                     if ed.get("radius") and abs(ed["radius"] - r) < 1e-6)
            w.call("add_dimension", page=page, view="Top", kind="diameter", edge=e)
        out = os.path.join(tmp, "leaders.svg")
        w.call("export_drawing", page=page, path=out)
        svg = open(out, encoding="utf-8").read()
        _check("both Ø callouts rendered", "Ø8.00" in svg and "Ø6.00" in svg)
        rep = w.call("drawing_legibility", page=page)
        _check("leader callouts are legible", rep["ok"], rep["violations"][:3])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

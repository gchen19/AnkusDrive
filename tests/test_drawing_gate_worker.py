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
    return page, hole


def test_complete_plate_passes():
    print("test_complete_plate_passes")
    with Worker() as w:
        page, hole = _plate(w, "gate_ok")
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
        page, hole = _plate(w, "gate_under")
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
        page, hole = _plate(w, "gate_auto")
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
        page, hole = _plate(w, "gate_leg")
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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

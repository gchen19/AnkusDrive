"""Legibility regression set (issue #85 Part A "Done when") — drive a range of
geometries (a hole grid, a high-aspect-ratio bar, a small part, a tight cluster)
through the real export + dimensioning pipeline and assert the legibility gate's
verdict on each. This is what hardens the A2 lane packing across shapes: a packing
change that starts overlapping labels or running dims off the sheet trips here.

Each case prints its label/segment counts and any violations, so the set doubles as
a readable snapshot of current behaviour. Cases engineered to be well-spaced must be
clean (ok=True); the verdict is asserted, not just the run.

Run: .venv/bin/python3 tests/test_drawing_legibility_regression.py
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


def _hole_edges(w, part, radius):
    """All circular-edge tags of the given radius on a part (one per hole face)."""
    return [e["tag"] for e in w.call("list_edges", handle=part)
            if e.get("radius") and abs(e["radius"] - radius) < 1e-6]


def _page_for(w, part, views=("Front", "Top")):
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=part, views=list(views))
    return page


def _report(w, page, name):
    rep = w.call("drawing_legibility", page=page)
    print(f"    [{name}] labels={rep['labels']} segments={rep['segments']} "
          f"views={rep['views']} violations={len(rep['violations'])}")
    for v in rep["violations"][:4]:
        print(f"        - {v['code']}: {v['reason']}")
    return rep


def _cut_holes(w, part, radius, height, centres):
    for (x, y) in centres:
        drill = w.call("add_primitive", kind="cylinder", r=radius, h=height,
                       placement=[x, y, 0])
        part = w.call("boolean_op", op="cut", base=part, tool=drill["handle"])["handle"]
    return part


def test_high_aspect_ratio_bar():
    print("test_high_aspect_ratio_bar")
    with Worker() as w:
        w.call("new_document", name="reg_bar")
        bar = w.call("add_primitive", kind="box", w=200, d=16, h=8)["handle"]
        page = _page_for(w, bar)
        w.call("add_dimension", page=page, auto=True)
        rep = _report(w, page, "aspect-bar")
        _check("high-aspect bar legible", rep["ok"], rep["violations"][:2])


def test_small_part():
    print("test_small_part")
    with Worker() as w:
        w.call("new_document", name="reg_small")
        blk = w.call("add_primitive", kind="box", w=12, d=10, h=4)["handle"]
        part = _cut_holes(w, blk, 1.5, 4, [(6, 5)])
        page = _page_for(w, part)
        w.call("add_dimension", page=page, auto=True)
        for e in _hole_edges(w, part, 1.5)[:1]:
            w.call("add_dimension", page=page, view="Top", kind="diameter", edge=e)
        rep = _report(w, page, "small-part")
        _check("small part legible", rep["ok"], rep["violations"][:2])


def test_hole_grid():
    print("test_hole_grid")
    with Worker() as w:
        w.call("new_document", name="reg_grid")
        plate = w.call("add_primitive", kind="box", w=90, d=50, h=6)["handle"]
        centres = [(x, y) for x in (18, 45, 72) for y in (16, 34)]
        part = _cut_holes(w, plate, 3, 6, centres)
        page = _page_for(w, part)
        w.call("add_dimension", page=page, auto=True)
        # one Ø call (all holes share radius -> the first circular edge of r=3)
        for e in _hole_edges(w, part, 3)[:1]:
            w.call("add_dimension", page=page, view="Top", kind="diameter", edge=e)
        rep = _report(w, page, "hole-grid")
        _check("hole grid legible", rep["ok"], rep["violations"][:3])


def test_oversize_part_fit_page():
    """A large part dimensioned at 1:1 pushes its Top-view dims off the top of the A4
    sheet — the gate flags out_of_border. fit_page (A3+) then scales + recentres so
    everything fits, and the gate goes clean."""
    print("test_oversize_part_fit_page")
    with Worker() as w:
        w.call("new_document", name="reg_oversize")
        # tall enough that the Top view's overall-depth dim runs off the top at 1:1
        # (each overall extent is now placed once — issue #108 #4 — so the overflow
        # must come from the geometry's height, not a duplicated cross-view callout)
        plate = w.call("add_primitive", kind="box", w=120, d=120, h=6)["handle"]
        part = _cut_holes(w, plate, 4, 6,
                          [(x, y) for x in (20, 60, 100) for y in (30, 90)])
        page = _page_for(w, part)
        w.call("add_dimension", page=page, auto=True)
        before = _report(w, page, "oversize-before")
        codes = [v["code"] for v in before["violations"]]
        _check("overflow flagged before fit", "out_of_border" in codes, codes)

        fit = w.call("fit_page", page=page)
        print(f"    fit -> scale={fit['scale']:.3f} fits={fit['fits']}")
        after = _report(w, page, "oversize-after")
        _check("fit_page reports it fits", fit["fits"], fit)
        _check("legible after fit_page", after["ok"], after["violations"][:3])


def test_tight_cluster_chain():
    print("test_tight_cluster_chain")
    with Worker() as w:
        w.call("new_document", name="reg_cluster")
        plate = w.call("add_primitive", kind="box", w=60, d=40, h=5)["handle"]
        xs = [16, 24, 32]
        part = _cut_holes(w, plate, 2, 5, [(x, 20) for x in xs])
        page = _page_for(w, part)
        w.call("add_dimension", page=page, view="Front", kind="horizontal",
               from_point=[0, 0, 0], to_point=[60, 0, 0])
        # chain-locate the cluster along X (disjoint spans -> should pack to one lane)
        prev = 0
        for x in xs:
            w.call("add_dimension", page=page, view="Top", kind="horizontal",
                   from_point=[prev, 20, 0], to_point=[x, 20, 0])
            prev = x
        rep = _report(w, page, "tight-cluster")
        _check("tight cluster chain legible", rep["ok"], rep["violations"][:3])


def test_giant_part_auto_scaled_and_fit():
    """A part far wider than the A4 printable area: the projection group's Automatic
    scale shrinks it below 1:1, the scale-aware dim placement keeps the dimensions
    on the (shrunk) geometry, and fit_page recentres so the whole thing is legible."""
    print("test_giant_part_auto_scaled_and_fit")
    with Worker() as w:
        w.call("new_document", name="reg_giant")
        slab = w.call("add_primitive", kind="box", w=340, d=180, h=10)["handle"]
        page = _page_for(w, slab)
        w.call("add_dimension", page=page, auto=True)
        fit = w.call("fit_page", page=page)
        print(f"    fit -> scale={fit['scale']:.3f} fits={fit['fits']}")
        _check("auto-scaled below 1:1", fit["scale"] < 0.95, fit["scale"])
        _check("fits after recentre", fit["fits"], fit)
        rep = _report(w, page, "giant-after")
        _check("legible once scaled + centred", rep["ok"], rep["violations"][:3])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

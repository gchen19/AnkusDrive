"""Curved & periodic feature gating (issue #108) — end-to-end through the FreeCAD
worker. The prismatic completeness gate (test_drawing_gate*.py) is edge+bbox+hole
based: it produces drawings that LOOK complete but leave the DEFINING geometry of
curved/periodic parts undimensioned, and never warns. This drives the surface-based
enumeration + pattern recognition + coverage invariant that closes that gap, on
three programmatically-built fixtures:

  1. a coned part (conical countersinks) — the gate must enumerate the cone and flag
     its half-angle as `angle_undimensioned`, then pass once an angle dim is added;
  2. a BSpline-walled part (a molded pocket with a freeform wall) — flagged
     `freeform_undimensioned`, satisfiable only by a controlling section/profile NOTE;
  3. a linear Fresnel comb (~30 sawtooth teeth) — recognised as ONE
     `pattern_undimensioned`, NOT exploded into N per-instance chamfers, satisfiable
     by a single pattern-table note.

It also asserts the auto-dimension fix (#108 #4): `add_dimension(auto=True)` places
each overall extent ONCE, so no bbox axis comes back `redundant`.

Run: python3 tests/test_drawing_gate_curved.py
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


# --- programmatic fixtures (full geometry kernel via run_script) ------------- #

# A 60x40x12 plate with three identical conical countersinks (90° included =>
# 45° half-angle). The cone WALLS are conical faces the edge/bbox gate is blind to.
_CONE = """
import math
import Part, FreeCAD as App
out = Part.makeBox(60, 40, 12)
for (x, y) in [(15, 20), (30, 20), (45, 20)]:
    out = out.cut(Part.makeCone(6, 2, 4, App.Vector(x, y, 8)))   # r1=6 -> r2=2 over h=4
obj = App.ActiveDocument.addObject("Part::Feature", "Coned")
obj.Shape = out
"""

# A 60x40x20 block with a BSpline-walled pocket cut through it: the pocket wall is a
# single freeform BSplineSurface, no number defines it. (Through-cut keeps the outer
# box — and so the bbox — clean.)
_LOFT = """
import math
import Part, FreeCAD as App
box = Part.makeBox(60, 40, 20)
def sw(z, pts):
    vs = [App.Vector(x, y, z) for (x, y) in pts]
    bs = Part.BSplineCurve(); bs.interpolate(vs, PeriodicFlag=True)
    return Part.Wire(bs.toShape())
lo = [(15, 12), (42, 16), (46, 22), (34, 32), (16, 30)]
hi = [(17, 14), (40, 18), (43, 23), (33, 30), (18, 28)]
plug = Part.makeLoft([sw(-5, lo), sw(25, hi)], True, False)
obj = App.ActiveDocument.addObject("Part::Feature", "Molded")
obj.Shape = box.cut(plug)
"""

# An 80x10x4 base with 30 sawtooth teeth at 2 mm pitch fused on top — a periodic
# micro-feature array (a linear Fresnel comb). The old gate would explode the angled
# facets into N independent chamfers.
_COMB = """
import math
import Part, FreeCAD as App
out = Part.makeBox(80, 10, 4)
pitch = 2.0
for i in range(30):
    x0 = 5 + i * pitch
    wire = Part.makePolygon([App.Vector(x0, 0, 4), App.Vector(x0 + pitch, 0, 4),
                             App.Vector(x0 + pitch, 0, 5), App.Vector(x0, 0, 4)])
    out = out.fuse(Part.Face(wire).extrude(App.Vector(0, 10, 0)))
out = out.removeSplitter()
obj = App.ActiveDocument.addObject("Part::Feature", "Comb")
obj.Shape = out
"""


def _build(w, name, code):
    """Build a fixture solid via run_script, drop it on a Front+Top drawing page.
    Returns (page_handle, part_handle)."""
    w.call("new_document", name=name)
    part = w.call("run_script", code=code)["registered"][0]["handle"]
    page = w.call("make_drawing_page", name="Page")["handle"]
    w.call("add_projection_group", page=page, body=part, views=["Front", "Top"])
    return page, part


def _kinds(rep):
    return [f["kind"] for f in rep["enumerated_features"]]


def _codes(rep):
    return [v["code"] for v in rep["violations"]]


def test_cone_half_angle_undimensioned():
    """Case 1: conical features are enumerated (grouped by half-angle — three
    identical countersinks are ONE callout), the half-angle is required, and a drawing
    with only overall extents flags `angle_undimensioned`. Adding the cone angle
    dimension closes it; auto-dimensioning never reports a bbox axis as redundant."""
    print("test_cone_half_angle_undimensioned")
    with Worker() as w:
        page, part = _build(w, "gate_cone", _CONE)
        rep = w.call("drawing_gate", page=page, process="prismatic")
        cones = [f for f in rep["enumerated_features"] if f["kind"] == "cone"]
        _check("a cone feature is enumerated", len(cones) == 1, _kinds(rep))
        _check("half-angle is ~45 deg", abs(cones[0].get("semi_angle", 0) - 45) < 1,
               cones)
        _check("three identical cones grouped to one callout",
               cones[0].get("count") == 3, cones)
        _check("cone half-angle flagged angle_undimensioned",
               "angle_undimensioned" in _codes(rep), _codes(rep))

        # auto extents + the cone angle dimension -> manufacturing-complete
        w.call("add_dimension", page=page, auto=True)
        rep_auto = w.call("drawing_gate", page=page, process="prismatic")
        _check("auto places each extent once (no redundant bbox)",
               "redundant" not in _codes(rep_auto), _codes(rep_auto))
        faces = w.call("list_faces", handle=part)
        cone_tag = next(f["tag"] for f in faces if f.get("kind") == "conical")
        ang = w.call("add_dimension", page=page, view="Top", kind="angle",
                     face=cone_tag)["dimensions"][0]
        _check("angle dim reads the included angle (~90 deg)",
               abs(float(ang["value"]) - 90) < 1, ang)
        rep2 = w.call("drawing_gate", page=page, process="prismatic")
        _check("complete once the cone angle is dimensioned", rep2["ok"],
               rep2["violations"])


def test_bspline_wall_freeform_undimensioned():
    """Case 2: a BSpline/freeform face is enumerated as a freeform feature and flagged
    `freeform_undimensioned` — a number alone can't capture it, so it is closed only
    by an explicit 'per CAD model / profile table' note (add_feature_note)."""
    print("test_bspline_wall_freeform_undimensioned")
    with Worker() as w:
        page, _part = _build(w, "gate_freeform", _LOFT)
        rep = w.call("drawing_gate", page=page, process="prismatic")
        ff = [f for f in rep["enumerated_features"] if f["kind"] == "freeform"]
        _check("a freeform feature is enumerated", len(ff) == 1, _kinds(rep))
        _check("freeform wall flagged freeform_undimensioned",
               "freeform_undimensioned" in _codes(rep), _codes(rep))

        # auto extents leave the freeform wall flagged; a dimension can't satisfy it
        w.call("add_dimension", page=page, auto=True)
        rep_auto = w.call("drawing_gate", page=page, process="prismatic")
        _check("freeform still undimensioned after auto extents",
               "freeform_undimensioned" in _codes(rep_auto), _codes(rep_auto))
        _check("no redundant bbox axis", "redundant" not in _codes(rep_auto),
               _codes(rep_auto))

        # an explicit profile note closes it -> manufacturing-complete
        w.call("add_feature_note", page=page, feature="FREEFORM1",
               text="FREEFORM1: wall profile per CAD model / profile table")
        rep2 = w.call("drawing_gate", page=page, process="prismatic")
        _check("complete once a profile note documents the freeform wall",
               rep2["ok"], rep2["violations"])


def test_fresnel_pattern_single_callout():
    """Case 3: a periodic array of bevels is recognised as ONE pattern, not N
    chamfers. The gate enumerates a single pattern feature, emits a single
    `pattern_undimensioned` finding (with pitch/count), and is closed by one
    pattern-table note."""
    print("test_fresnel_pattern_single_callout")
    with Worker() as w:
        page, _part = _build(w, "gate_pattern", _COMB)
        rep = w.call("drawing_gate", page=page, process="prismatic")
        pats = [f for f in rep["enumerated_features"] if f["kind"] == "pattern"]
        _check("exactly one pattern feature enumerated", len(pats) == 1, _kinds(rep))
        _check("teeth NOT exploded into per-instance chamfers",
               _kinds(rep).count("chamfer") == 0, _kinds(rep))
        _check("pattern recovered the tooth count (~30)",
               abs(pats[0].get("count", 0) - 30) <= 1, pats)
        _check("pattern recovered the pitch (~2 mm)",
               abs(pats[0].get("pitch", 0) - 2.0) < 0.1, pats)
        pat_codes = [v for v in rep["violations"]
                     if v["code"] == "pattern_undimensioned"]
        _check("ONE pattern_undimensioned finding, not 30",
               len(pat_codes) == 1, _codes(rep))

        # auto extents + one pattern-table note -> manufacturing-complete
        w.call("add_dimension", page=page, auto=True)
        rep_auto = w.call("drawing_gate", page=page, process="prismatic")
        _check("no redundant bbox axis", "redundant" not in _codes(rep_auto),
               _codes(rep_auto))
        w.call("add_feature_note", page=page, feature="PAT1",
               text="PAT1: 30x @ 2mm pitch, 1mm depth, 45 deg facet — see detail")
        rep2 = w.call("drawing_gate", page=page, process="prismatic")
        _check("complete once the pattern is documented by one note",
               rep2["ok"], rep2["violations"])


def test_coverage_ratio_spans_all_feature_classes():
    """The coverage invariant accounts EVERY feature class — a cone left
    undimensioned drops the ratio below 1, and it reaches 1 only when the curved slot
    is satisfied too (not just bbox + holes)."""
    print("test_coverage_ratio_spans_all_feature_classes")
    with Worker() as w:
        page, part = _build(w, "gate_cov", _CONE)
        w.call("add_dimension", page=page, auto=True)
        rep = w.call("drawing_gate", page=page, process="prismatic")
        _check("coverage < 1 while the cone angle is undimensioned",
               rep["coverage"] < 1.0, rep["coverage"])
        faces = w.call("list_faces", handle=part)
        cone_tag = next(f["tag"] for f in faces if f.get("kind") == "conical")
        w.call("add_dimension", page=page, view="Top", kind="angle", face=cone_tag)
        rep2 = w.call("drawing_gate", page=page, process="prismatic")
        _check("coverage == 1 once every class is satisfied",
               rep2["coverage"] == 1.0 and rep2["ok"], rep2)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

"""Tight bounding box (issue #284) — driven through the real FreeCAD worker.

FreeCAD/OCC's analytic `shape.BoundBox` is an UPPER bound: BRepBndLib boxes each
face from its untrimmed carrier surface, so a planar cut through fillets/lofts/a
sphere reports material that is not there. The field report behind #284 saw a
trim plane at X=-32.0 come back as X=-36.7 and burned a debugging pass exonerating
a correct part.

The quirk REPRODUCES on this FreeCAD build (1.1.0). Fixture: a Ø40 x 30 cylinder
with 4mm fillets on both circular edges, cut by a half-space so the remaining
solid starts at exactly X = -6. Measured here:

    analytic  X -8.118 .. 21.648      (phantom 2.118mm low, 1.648mm high)
    tight     X -6.000 .. 20.000      (the trim plane, and the true Ø40)
    a real vertex sits at X = -6.000  (the trim edge)

Asserted below:
  * a prismatic part is certified "exact" by the FREE vertex sweep — no
    tessellation, no warning, payload unchanged for existing callers;
  * so is a filleted box (fillets alone are not the problem — trimming is);
  * the fixture's analytic box overshoots, `tight` lands on the true trim plane,
    `verified` == "over_estimate", and the warning names the offending face;
  * a curved part with no `tight` request is honestly labelled "unverified"
    rather than silently trusted;
  * a trimmed plain SPHERE overshoots by 14mm — the quirk is not spline-specific,
    so no cheap surface-type shortcut could stand in for the mesh;
  * the vertex box is never returned AS the answer — a plain cylinder's two seam
    vertices are 40mm inside a perfectly tight analytic box, so a vertex sweep
    can certify tightness but can never detect looseness;
  * tight=True does not mutate the model: tessellating caches a triangulation that
    OCC would then prefer, silently changing later analytic readings.

Run: python3 tests/test_bbox_tight.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402


def _filleted_cylinder(w, radius=20.0, height=30.0, fillet=4.0):
    """Ø(2*radius) x height cylinder with `fillet` on both circular edges."""
    cyl = w.call("add_primitive", kind="cylinder", r=radius, h=height)
    circles = [e["tag"] for e in w.call("list_edges", handle=cyl["handle"])
               if e["kind"] == "circle"]
    assert len(circles) == 2, f"expected 2 circular edges, got {circles}"
    return w.call("fillet_edges", handle=cyl["handle"], radius=fillet, edges=circles)


def _trim_at_x(w, handle, x0, span=600.0):
    """Cut everything with X < x0 away with a big half-space box."""
    cutter = w.call("add_primitive", kind="box", w=span, d=span, h=span,
                    placement=[x0 - span, -span / 2, -span / 2])
    return w.call("boolean_op", op="cut", base=handle, tool=cutter["handle"])


def test_prismatic_box_is_certified_free():
    """A box is proven exact by the vertex sweep alone: no tessellation, no
    warning, and every pre-#284 key still carries the same value."""
    with Worker() as w:
        w.call("new_document", name="bb284a")
        box = w.call("add_primitive", kind="box", w=10, d=20, h=5)
        r = w.call("bounding_box", handle=box["handle"])
        assert r["size"] == [10.0, 20.0, 5.0], r
        assert r["min"] == [0.0, 0.0, 0.0] and r["max"] == [10.0, 20.0, 5.0], r
        assert r["oriented"] is None, r
        assert r["verified"] == "exact", r
        assert r["tight"] is None, r          # not requested -> not computed
        assert r["warnings"] == [], r         # nothing to warn about
        assert "handle" not in r, r


def test_filleted_box_is_certified_free():
    """Fillets alone do not defeat the free certificate — a filleted box's
    vertices still reach all six analytic faces. This is what keeps the
    'unverified' advisory from becoming noise on ordinary machined parts."""
    with Worker() as w:
        w.call("new_document", name="bb284b")
        box = w.call("add_primitive", kind="box", w=60, d=40, h=20)
        edges = [e["tag"] for e in w.call("list_edges", handle=box["handle"])]
        fil = w.call("fillet_edges", handle=box["handle"], radius=6.0, edges=edges)
        r = w.call("bounding_box", handle=fil["handle"])
        assert r["verified"] == "exact", r
        assert r["size"] == [60.0, 40.0, 20.0], r
        assert r["warnings"] == [], r


def test_curved_part_is_labelled_unverified():
    """A plain cylinder's analytic box IS tight, but only two seam vertices
    reach it, so the free sweep cannot certify it. The handler must say
    'unverified' and advise tight=true rather than claim either way — and it
    must NOT report the vertex box (X 20..20 here) as the answer."""
    with Worker() as w:
        w.call("new_document", name="bb284c")
        cyl = w.call("add_primitive", kind="cylinder", r=20, h=30)
        r = w.call("bounding_box", handle=cyl["handle"])
        assert r["verified"] == "unverified", r
        assert r["min"][0] < -19.9 and r["max"][0] > 19.9, r   # not the vertex box
        assert r["warnings"] and "tight=true" in r["warnings"][0], r
        # ...and asking for tight resolves it: this box really is tight.
        rt = w.call("bounding_box", handle=cyl["handle"], tight=True)
        assert rt["verified"] == "mesh_agrees", rt
        assert rt["warnings"] == [], rt
        assert abs(rt["tight"]["size"][2] - 30.0) < 1e-6, rt


def test_trimmed_fillet_over_estimate_is_caught():
    """THE #284 regression. Filleted cylinder cut at X = -6: the analytic box
    claims XMin -8.118 (2.118mm of phantom material) and XMax 21.648 on a Ø40
    part, while the tessellated box lands on the real trim plane and the real
    diameter. The discrepancy must be flagged, not silently returned."""
    with Worker() as w:
        w.call("new_document", name="bb284d")
        fil = _filleted_cylinder(w)
        cut = _trim_at_x(w, fil["handle"], -6.0)

        r = w.call("bounding_box", handle=cut["handle"], tight=True)
        analytic_lo, analytic_hi = r["min"][0], r["max"][0]
        tight_lo, tight_hi = r["tight"]["min"][0], r["tight"]["max"][0]

        # the quirk is real on this build (guard against a silent no-op test)
        assert analytic_lo < -6.5, f"expected an analytic over-shoot, got {analytic_lo}"
        assert analytic_hi > 20.5, f"expected an analytic over-shoot, got {analytic_hi}"
        # the mesh box lands on the truth: the trim plane and the Ø40 cylinder
        assert abs(tight_lo - (-6.0)) < 0.01, r
        assert abs(tight_hi - 20.0) < 0.01, r
        assert abs(r["tight"]["size"][0] - 26.0) < 0.02, r

        assert r["verified"] == "over_estimate", r
        assert r["warnings"], r
        msg = r["warnings"][0]
        assert "#284" in msg, msg
        # both offending faces are named, with how far each overshoots
        assert "Xmin by 2.118 mm" in msg, msg
        assert "Xmax by 1.648 mm" in msg, msg
        assert r["tight"]["triangles"] > 0 and r["tight"]["deflection"] > 0, r

        # the true trim plane is also a real VERTEX of the solid — the field
        # report's independent confirmation. It bounds, but cannot certify.
        verts = w.call("run_script", code=(
            "s = _resolve(%r).Shape\n"
            "__result__ = min(v.Point.x for v in s.Vertexes)\n" % cut["handle"]))
        assert abs(verts["result"] - (-6.0)) < 1e-6, verts

        # without tight=True the payload is the legacy one plus the advisory
        plain = w.call("bounding_box", handle=cut["handle"])
        assert plain["tight"] is None, plain
        assert plain["verified"] == "unverified", plain
        assert plain["min"] == r["min"], (plain, r)


def test_trimmed_sphere_over_estimates_too():
    """The quirk is not a BSpline-only affair, which is why there is no cheap
    'does this part contain a spline?' shortcut: a plain Ø40 SPHERE cut at
    X = -6 still reports XMin -20 — 14mm of phantom material on an elementary
    surface. Only meshing the trimmed patch finds it."""
    with Worker() as w:
        w.call("new_document", name="bb284g")
        sph = w.call("add_primitive", kind="sphere", r=20)
        cut = _trim_at_x(w, sph["handle"], -6.0)
        r = w.call("bounding_box", handle=cut["handle"], tight=True)
        assert abs(r["min"][0] - (-20.0)) < 0.01, r          # the phantom
        assert abs(r["tight"]["min"][0] - (-6.0)) < 0.01, r  # the truth
        assert r["verified"] == "over_estimate", r
        assert "Xmin by 14.0" in r["warnings"][0], r["warnings"]


def test_tight_does_not_mutate_the_model():
    """tessellate() caches a triangulation on the shape and OCC then PREFERS it,
    so tessellating the live object would tighten -8.118 to -6.000 for every
    later caller. A measurement must not do that: tessellate a copy."""
    with Worker() as w:
        w.call("new_document", name="bb284e")
        fil = _filleted_cylinder(w)
        cut = _trim_at_x(w, fil["handle"], -6.0)
        before = w.call("bounding_box", handle=cut["handle"])
        w.call("bounding_box", handle=cut["handle"], tight=True)
        after = w.call("bounding_box", handle=cut["handle"])
        assert after["min"] == before["min"] and after["max"] == before["max"], (before, after)
        assert after["min"][0] < -6.5, after   # still the (un-tightened) analytic box


def test_deflection_is_honoured_and_costed():
    """A coarser deflection is echoed back and still catches a millimetre-scale
    phantom — the cheap way to use tight=True on a big part."""
    with Worker() as w:
        w.call("new_document", name="bb284f")
        fil = _filleted_cylinder(w)
        cut = _trim_at_x(w, fil["handle"], -6.0)
        t0 = time.time()
        r = w.call("bounding_box", handle=cut["handle"], tight=True, deflection=0.25)
        dt = time.time() - t0
        assert abs(r["tight"]["deflection"] - 0.25) < 1e-9, r
        assert abs(r["tight"]["min"][0] - (-6.0)) < 0.05, r
        assert r["verified"] == "over_estimate", r
        print(f"       (deflection=0.25 tessellation: {r['tight']['triangles']} tris, "
              f"{dt * 1000:.0f} ms round trip)")


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t0:.2f}s)")
    total = time.time() - t_suite
    if failures:
        print(f"\n== {len(failures)} failed ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"\n== all passed ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

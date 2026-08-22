"""
Fillet / chamfer post-apply validation (issue #283).

This FreeCAD/OCC build's blend kernel is edge- and ORDER-sensitive enough to
return SILENTLY CORRUPT geometry, and before #283 the handlers returned a
handle to it: no isValid() check, no envelope comparison, no per-edge fallback.
A field build (Spectra S1 flange insert) caught it only by independently
measuring the part's outer envelope, then hand-rolled the one-edge-at-a-time
apply-and-check loop that now lives in the tool.

The fixtures here are constructed, not found — `AC review/flange_insert.step`
from the report is not in this checkout. Both reproduce the report's failure
CLASS on the same geometry family (a thin brim / boss whose blend radius the
adjacent face cannot carry), and both are verified to fail OCC here:

  CORRUPT_CUBE  20mm cube, all 12 edges at r=11 -> isValid() False, still ONE
                solid, volume LARGER than the input, envelope 13mm too big.
                No exception: this is the silent case in its purest form.
  CORRUPT_PLATE 40x40x1 plate, all 12 edges at r=0.6 -> the brim shape. Top and
                bottom fillets on a 1mm wall overlap; isValid() False, one
                solid, volume 2110 against 1600 in.
  NULL_FILLET   20mm cube, the 4 vertical edges at r=10 -> adjacent fillets meet
                exactly and OCC refuses: a null Shape, which used to surface as
                a bare "RuntimeError: shape is invalid" from reading .Volume,
                after the handle was registered.

  test_clean_fillet_reports_all_checks        : happy path carries checks{}
  test_corrupt_batch_raises_not_returns       : silent corruption -> structured error
  test_error_names_the_offending_edges        : per-edge diagnosis is actionable
  test_failed_fillet_leaves_document_intact   : no corrupt object, no handle
  test_null_shape_failure_is_attributed       : OCC's outright refusal, named
  test_allow_partial_is_loudly_marked         : opt-in partial, unmistakable
  test_per_edge_matches_batch_on_clean_input  : opt-in slow path is not a
                                                different answer
  test_chamfer_shares_the_treatment           : Part::Chamfer, same contract
  test_partdesign_fillet_is_validated         : PartDesign has the same defect
  test_partdesign_failure_restores_body_tip   : rollback puts the Body back
  test_envelope_check_survives_curved_input   : no false positive on a shape
                                                whose BoundBox is inflated

Run: python3 tests/test_fillet_validation.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker, WorkerError  # noqa: E402

# The fixtures, as (primitive kwargs, radius). See the module docstring.
CORRUPT_CUBE = (dict(kind="box", w=20, d=20, h=20), 11.0)
CORRUPT_PLATE = (dict(kind="box", w=40, d=40, h=1.0), 0.6)
NULL_FILLET = (dict(kind="box", w=20, d=20, h=20), 10.0)


def _box_edges(w, name, prim):
    """Fresh document + primitive; returns (handle, [edge tags])."""
    w.call("new_document", name=name)
    h = w.call("add_primitive", **prim)["handle"]
    return h, [e["tag"] for e in w.call("list_edges", handle=h)]


def _expect_blend_failure(w, method, **kwargs):
    try:
        w.call(method, **kwargs)
    except WorkerError as e:
        assert e.type == "BlendCheckFailed", f"{e.type}: {e.remote_message}"
        return e
    raise AssertionError(f"{method} returned a handle for known-corrupt geometry")


# --- tests --------------------------------------------------------------------

def test_clean_fillet_reports_all_checks():
    """An ordinary fillet returns the checks it performed, so an agent doesn't
    have to re-derive them (the verify_feature convention)."""
    with Worker() as w:
        h, tags = _box_edges(w, "f283_clean", dict(kind="box", w=20, d=20, h=20))
        r = w.call("fillet_edges", handle=h, edges=tags, radius=2.0)
        c = r["checks"]
        assert c["valid"] is True, c
        assert c["solids"] == 1, c
        assert c["envelope_ok"] is True, c
        assert c["envelope_growth_mm"] <= c["envelope_tol_mm"], c
        assert r["mode"] == "batch", r          # one apply, no retry
        assert r["partial"] is False, r
        assert "skipped_edges" not in r, r
        assert "warnings" not in r, r
        assert r["edges"] == list(range(1, 13)), r
        # And the geometry is real: a 2mm fillet on every edge of a 20mm cube
        # removes a little material and nothing else.
        assert 7700 < r["volume"] < 8000, r["volume"]


def test_corrupt_batch_raises_not_returns():
    """THE regression. Pre-#283 this call returned {handle, volume} for a shape
    with isValid() False and a bounding box 13mm too big."""
    with Worker() as w:
        prim, radius = CORRUPT_CUBE
        h, tags = _box_edges(w, "f283_corrupt", prim)
        e = _expect_blend_failure(w, "fillet_edges", handle=h, edges=tags,
                                  radius=radius)
        msg = e.remote_message
        assert "fillet_edges" in msg, msg
        assert "isValid" in msg, msg
        # The envelope check is what the field agent had to do by hand.
        assert "bounding box grew" in msg, msg
        assert w.call("ping") == "pong"


def test_error_names_the_offending_edges():
    """Per-edge diagnosis: the reply must name which edges fail AND which
    subset works, so the agent can act without a second round trip."""
    with Worker() as w:
        prim, radius = CORRUPT_PLATE
        h, tags = _box_edges(w, "f283_diag", prim)
        e = _expect_blend_failure(w, "fillet_edges", handle=h, edges=tags,
                                  radius=radius)
        msg = e.remote_message
        assert "corrupts the result when added" in msg, msg
        assert "blend cleanly together" in msg, msg
        assert "r=0.6 mm" in msg, msg
        # At least one specific edge is named on both sides of the diagnosis.
        assert any(f"Edge{i}" in msg for i in range(1, 13)), msg
        # And it says what to do next.
        assert "allow_partial" in msg, msg


def test_failed_fillet_leaves_document_intact():
    """No handle to corrupt geometry, and no corrupt OBJECT either: the failed
    feature is rolled out, so the document is exactly as it was."""
    with Worker() as w:
        prim, radius = CORRUPT_CUBE
        h, tags = _box_edges(w, "f283_intact", prim)
        before = [o["name"] for o in w.call("list_objects")]
        _expect_blend_failure(w, "fillet_edges", handle=h, edges=tags,
                              radius=radius)
        after = [o["name"] for o in w.call("list_objects")]
        assert before == after, (before, after)
        # The base is untouched and still sound.
        chk = w.call("check_shape", handle=h)
        assert chk["valid"] and chk["solids"] == 1, chk
        assert abs(chk["volume_mm3"] - 8000.0) < 1e-6, chk


def test_null_shape_failure_is_attributed():
    """OCC refusing outright used to surface as a bare 'shape is invalid' with
    no idea which operation raised it. Now it names the op and the edges."""
    with Worker() as w:
        prim, radius = NULL_FILLET
        h, _ = _box_edges(w, "f283_null", prim)
        vert = [e["tag"] for e in w.call("list_edges", handle=h)
                if abs(e["centroid"][2] - 10.0) < 1e-9]
        assert len(vert) == 4, vert
        e = _expect_blend_failure(w, "fillet_edges", handle=h, edges=vert,
                                  radius=radius)
        assert "null shape" in e.remote_message, e.remote_message
        assert "fillet_edges" in e.remote_message, e.remote_message
        assert w.call("ping") == "pong"


def test_allow_partial_is_loudly_marked():
    """The partial result is opt-in AND unmistakable: partial=True, the skipped
    edges enumerated, and a warning saying it is not the part that was asked
    for. A fallback that quietly drops a fillet is its own defect (#269, #282)."""
    with Worker() as w:
        prim, radius = CORRUPT_PLATE
        h, tags = _box_edges(w, "f283_partial", prim)
        r = w.call("fillet_edges", handle=h, edges=tags, radius=radius,
                   allow_partial=True)
        assert r["partial"] is True, r
        assert r["skipped_edges"], r
        assert set(r["skipped_edges"]).isdisjoint(r["edges"]), r
        assert len(r["edges"]) + len(r["skipped_edges"]) == 12, r
        assert r["mode"] == "per_edge", r
        assert r["warnings"], r
        joined = " ".join(r["warnings"])
        assert "PARTIAL RESULT" in joined, joined
        assert "not the part that was asked for" in joined, joined
        # What DID come back is sound — that is the whole point of keeping it.
        assert r["checks"]["valid"] is True, r["checks"]
        assert r["checks"]["envelope_ok"] is True, r["checks"]
        assert w.call("check_shape", handle=r["handle"])["valid"], r


def test_per_edge_matches_batch_on_clean_input():
    """The opt-in slow path must reach the SAME geometry as the fast one on
    input that blends cleanly — otherwise it is a different tool, not a safer
    one. (It also guards the stale-Shape trap: a Part::Fillet whose recompute
    once failed silently serves its previous shape forever, so a retry that
    reuses the object would report a 1-edge fillet's volume for 12 edges.)"""
    with Worker() as w:
        h, tags = _box_edges(w, "f283_pe_a", dict(kind="box", w=20, d=20, h=20))
        batch = w.call("fillet_edges", handle=h, edges=tags, radius=2.0)
        h2, tags2 = _box_edges(w, "f283_pe_b", dict(kind="box", w=20, d=20, h=20))
        per = w.call("fillet_edges", handle=h2, edges=tags2, radius=2.0,
                     per_edge=True)
        assert per["mode"] == "per_edge" and batch["mode"] == "batch"
        assert per["partial"] is False, per
        assert per["edges"] == batch["edges"], (per["edges"], batch["edges"])
        assert abs(per["volume"] - batch["volume"]) < 1e-6, (per, batch)


def test_chamfer_shares_the_treatment():
    """chamfer_edges is the same code path over Part::Chamfer, and gets the same
    contract: checks on success, structured error on failure."""
    with Worker() as w:
        h, tags = _box_edges(w, "f283_ch", dict(kind="box", w=40, d=40, h=1.0))
        ok = w.call("chamfer_edges", handle=h, edges=tags, size=0.3)
        assert ok["checks"]["valid"] is True, ok
        assert ok["checks"]["envelope_ok"] is True, ok
        assert ok["partial"] is False, ok

        h2, tags2 = _box_edges(w, "f283_ch2", dict(kind="box", w=40, d=40, h=1.0))
        before = [o["name"] for o in w.call("list_objects")]
        e = _expect_blend_failure(w, "chamfer_edges", handle=h2, edges=tags2,
                                  size=0.6)
        assert "chamfer_edges" in e.remote_message, e.remote_message
        assert [o["name"] for o in w.call("list_objects")] == before


def test_partdesign_fillet_is_validated():
    """The #283 audit found PartDesign::Fillet carries the SAME silent defect:
    r=0.6 on a 40x40x1 pad reports State 'Up-to-date', one solid, and a volume
    15% larger than the pad it was cut from."""
    with Worker() as w:
        pad = _pad_40x40x1(w, "f283_pd")
        tags = [e["tag"] for e in w.call("list_edges", handle=pad)]
        ok = w.call("partdesign_fillet", feature=pad, edges=tags, radius=0.3)
        assert ok["checks"]["valid"] is True, ok
        assert ok["checks"]["envelope_ok"] is True, ok
        assert ok["volume"] < 1600.0, ok      # a fillet REMOVES material here

        pad2 = _pad_40x40x1(w, "f283_pd2")
        tags2 = [e["tag"] for e in w.call("list_edges", handle=pad2)]
        e = _expect_blend_failure(w, "partdesign_fillet", feature=pad2,
                                  edges=tags2, radius=0.6)
        assert "partdesign_fillet" in e.remote_message, e.remote_message


def test_partdesign_failure_restores_body_tip():
    """Rolling a PartDesign feature back needs more than removeObject — that
    alone leaves the Body with Tip = None. The pad must still be the tip, and
    still be usable, after the failure."""
    with Worker() as w:
        pad = _pad_40x40x1(w, "f283_pdtip")
        tags = [e["tag"] for e in w.call("list_edges", handle=pad)]
        before = [o["name"] for o in w.call("list_objects")]
        _expect_blend_failure(w, "partdesign_fillet", feature=pad, edges=tags,
                              radius=0.6)
        assert [o["name"] for o in w.call("list_objects")] == before
        chk = w.call("check_shape", handle=pad)
        assert chk["valid"] and abs(chk["volume_mm3"] - 1600.0) < 1e-6, chk
        # The body is still buildable: a radius that DOES work still works.
        ok = w.call("partdesign_fillet", feature=pad, edges=tags, radius=0.3)
        assert ok["checks"]["valid"] is True, ok


def test_envelope_check_survives_curved_input():
    """No false positives. Shape.BoundBox is tessellation-inflated for curved
    faces — a perfectly valid filleted cylinder measures 0.82mm of 'growth' by
    that metric — so the check falls back to the tight box before condemning
    anything. If this ever fails, every fillet on a round part is blocked."""
    with Worker() as w:
        w.call("new_document", name="f283_cyl")
        h = w.call("add_primitive", kind="cylinder", r=10, h=30)["handle"]
        circles = [e["tag"] for e in w.call("list_edges", handle=h)
                   if e["kind"] == "circle"]
        assert len(circles) >= 2, circles
        r = w.call("fillet_edges", handle=h, edges=circles, radius=2.0)
        assert r["checks"]["envelope_ok"] is True, r["checks"]
        assert r["checks"]["valid"] is True, r["checks"]
        assert r["partial"] is False, r


# --- fixtures -----------------------------------------------------------------

def _pad_40x40x1(w, doc_name):
    """A 40x40x1mm PartDesign pad — the thin-brim shape whose top and bottom
    fillets collide at r=0.6. Returns the pad handle."""
    w.call("new_document", name=doc_name)
    body = w.call("make_body")
    sk = w.call("make_sketch", body=body["handle"], plane="XY")
    w.call("add_sketch_geometry", sketch=sk["handle"], items=[
        {"type": "line", "start": [0, 0], "end": [40, 0]},
        {"type": "line", "start": [40, 0], "end": [40, 40]},
        {"type": "line", "start": [40, 40], "end": [0, 40]},
        {"type": "line", "start": [0, 40], "end": [0, 0]},
    ])
    w.call("close_sketch", sketch=sk["handle"])
    return w.call("pad", sketch=sk["handle"], length=1.0)["handle"]


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


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
            print(f"  FAIL {name:50s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:50s} ({time.time() - t0:.2f}s)")

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

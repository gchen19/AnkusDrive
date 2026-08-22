"""
Degenerate-subtraction detection (issue #282).

`boolean_op` used to return {handle, volume} with no interpretation, so two
broken cut outcomes looked exactly like a healthy one:

  annihilation — the tool swallowed the base (result ~ 0), and every later
                 feature silently operated on nothing. This is the failure that
                 filed #282: a mis-sized trim cutter box "ate the whole part"
                 and the agent only noticed several stages downstream.
  miss         — the tool never intersected the base (result == base), so the
                 cut removed nothing at all.

Both now surface as a `warnings` list in the payload — absent on a clean op —
alongside removed_volume / volume_ratio for all three ops, and strict=True
turns both into an error. pocket/hole share the pattern and got the same
treatment (minus strict, which they don't expose).

  test_clean_cut_has_no_warnings_key       : healthy cut → no `warnings` at all
  test_oversized_tool_warns_annihilation   : tool contains base → warning
  test_nonintersecting_tool_warns_miss     : tool misses base → warning
  test_strict_raises_on_annihilation       : strict=True upgrades to an error
  test_strict_raises_on_miss               : strict=True upgrades to an error
  test_strict_is_silent_on_a_clean_cut     : strict only fires on degenerates
  test_fuse_and_common_report_volume_ratio : the sanity numbers, all three ops
  test_backwards_compatible_payload        : handle + volume still there
  test_email_trim_cutter_reproduces        : #282's exact reported failure
  test_pocket_that_removes_nothing_warns   : PartDesign shares the pattern
  test_pocket_away_from_body_is_not_warned : ...but an intentional no-op isn't
  test_hole_reports_removed_volume         : hole carries the numbers too

Run: python3 tests/test_boolean_degenerate.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker, WorkerError  # noqa: E402


# --- helpers ------------------------------------------------------------------

def _cube_and_tool(w, doc, tool_kw):
    """A 10 mm cube at the origin plus a second box described by `tool_kw`.
    Returns (base_handle, tool_handle)."""
    w.call("new_document", name=doc)
    base = w.call("add_primitive", kind="box", w=10, d=10, h=10)
    tool = w.call("add_primitive", **tool_kw)
    return base["handle"], tool["handle"]


def _expect_error(callable_, *args, **kwargs):
    try:
        callable_(*args, **kwargs)
    except WorkerError as e:
        return e
    raise AssertionError(f"expected WorkerError; got success from {callable_}")


def _build_cube_body(w, side=20.0):
    """Pad a `side` cube in a fresh body. Returns {body, pad}."""
    body = w.call("make_body")
    sk = w.call("make_sketch", body=body["handle"], plane="XY")
    g = w.call(
        "add_sketch_geometry",
        sketch=sk["handle"],
        items=[
            {"type": "line", "start": [0, 0], "end": [side, 0]},
            {"type": "line", "start": [side, 0], "end": [side, side]},
            {"type": "line", "start": [side, side], "end": [0, side]},
            {"type": "line", "start": [0, side], "end": [0, 0]},
        ],
    )
    idxs = g["indices"]
    for i in range(4):
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Coincident",
            refs=[[idxs[i], 2], [idxs[(i + 1) % 4], 1]],
        )
    pad = w.call("pad", sketch=sk["handle"], length=side)
    return {"body": body["handle"], "pad": pad["handle"]}


def _top_face_sketch(w, h, radius=4.0, side=20.0):
    """A circle sketched on a datum attached to the body's top face."""
    top = w.call(
        "query_faces", handle=h["pad"],
        predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
    )
    plane = w.call(
        "make_datum_plane", body=h["body"],
        base={"handle": h["pad"], "tag": top[0]["tag"]},
    )
    sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
    w.call(
        "add_sketch_geometry", sketch=sk["handle"],
        items=[{"type": "circle", "center": [side / 2, side / 2], "radius": radius}],
    )
    return sk["handle"]


# --- boolean_op: the three outcomes -------------------------------------------

def test_clean_cut_has_no_warnings_key():
    """A cut that removes some-but-not-all material is clean: no `warnings` key
    at all (backwards compatibility is explicit in #282 — callers that never
    heard of warnings must see the payload they always saw)."""
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_clean", dict(kind="cylinder", r=2, h=10, placement=[5, 5, 0]),
        )
        r = w.call("boolean_op", op="cut", base=base, tool=tool)
        assert "warnings" not in r, f"clean cut must not warn: {r}"
        removed = 3.14159265 * 2 * 2 * 10
        assert abs(r["removed_volume"] - removed) / removed < 0.01, r
        assert 0.85 < r["volume_ratio"] < 0.9, r


def test_oversized_tool_warns_annihilation():
    """The tool fully contains the base → result is empty. The warning must say
    so at the boolean_op call, not leave an empty shape for stage N+3."""
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_annihilate",
            dict(kind="box", w=40, d=40, h=40, placement=[-10, -10, -10]),
        )
        r = w.call("boolean_op", op="cut", base=base, tool=tool)
        assert r["volume"] == 0.0, r
        assert r["volume_ratio"] == 0.0, r
        assert abs(r["removed_volume"] - 1000.0) < 1e-6, r
        assert "warnings" in r, f"annihilated cut must warn: {r}"
        assert any("entire base solid" in wmsg for wmsg in r["warnings"]), r["warnings"]


def test_nonintersecting_tool_warns_miss():
    """The tool never touches the base → the cut removed nothing, and the
    result is just a copy of the base. Same family as the pre-#269 unrun-check
    problem: an absence presented as a success."""
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_miss", dict(kind="box", w=5, d=5, h=5, placement=[100, 0, 0]),
        )
        r = w.call("boolean_op", op="cut", base=base, tool=tool)
        assert abs(r["volume"] - 1000.0) < 1e-9, r
        assert abs(r["removed_volume"]) < 1e-9, r
        assert abs(r["volume_ratio"] - 1.0) < 1e-9, r
        assert "warnings" in r, f"missed cut must warn: {r}"
        assert any("does not intersect" in wmsg for wmsg in r["warnings"]), r["warnings"]


# --- strict mode --------------------------------------------------------------

def test_strict_raises_on_annihilation():
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_strict_ann",
            dict(kind="box", w=40, d=40, h=40, placement=[-10, -10, -10]),
        )
        e = _expect_error(
            w.call, "boolean_op", op="cut", base=base, tool=tool, strict=True,
        )
        assert "entire base solid" in e.remote_message, e.remote_message
        assert w.call("ping") == "pong"      # worker survives the strict abort


def test_strict_raises_on_miss():
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_strict_miss",
            dict(kind="box", w=5, d=5, h=5, placement=[100, 0, 0]),
        )
        e = _expect_error(
            w.call, "boolean_op", op="cut", base=base, tool=tool, strict=True,
        )
        assert "does not intersect" in e.remote_message, e.remote_message
        assert w.call("ping") == "pong"


def test_strict_is_silent_on_a_clean_cut():
    """strict=True must not make healthy cuts fail — it only upgrades the two
    degenerate outcomes."""
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_strict_ok", dict(kind="cylinder", r=2, h=10, placement=[5, 5, 0]),
        )
        r = w.call("boolean_op", op="cut", base=base, tool=tool, strict=True)
        assert "warnings" not in r, r
        assert r["removed_volume"] > 0, r


# --- the sanity numbers, for all three ops ------------------------------------

def test_fuse_and_common_report_volume_ratio():
    """removed_volume / volume_ratio are reported for fuse and common too, so
    the number an agent needs for a sanity check is already in the reply. On a
    fuse, removed_volume is negative: the tool ADDED that much material. An
    empty `common` is the expected answer for clearance checks, so neither op
    ever warns."""
    with Worker() as w:
        w.call("new_document", name="bd_ops")
        a = w.call("add_primitive", kind="box", w=10, d=10, h=10)["handle"]
        b = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                   placement=[5, 0, 0])["handle"]
        fused = w.call("boolean_op", op="fuse", base=a, tool=b)
        assert abs(fused["volume"] - 1500.0) < 1e-6, fused
        assert abs(fused["removed_volume"] + 500.0) < 1e-6, fused   # negative: added
        assert abs(fused["volume_ratio"] - 1.5) < 1e-9, fused
        assert "warnings" not in fused, fused

        c = w.call("add_primitive", kind="box", w=10, d=10, h=10)["handle"]
        d = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                   placement=[5, 0, 0])["handle"]
        common = w.call("boolean_op", op="common", base=c, tool=d)
        assert abs(common["volume"] - 500.0) < 1e-6, common
        assert abs(common["volume_ratio"] - 0.5) < 1e-9, common
        assert "warnings" not in common, common


def test_backwards_compatible_payload():
    """Existing callers read result['handle'] and result['volume']; #282 only
    ADDS keys. Both must still be there, and the handle must still resolve."""
    with Worker() as w:
        base, tool = _cube_and_tool(
            w, "bd_compat", dict(kind="cylinder", r=2, h=10, placement=[5, 5, 0]),
        )
        r = w.call("boolean_op", op="cut", base=base, tool=tool)
        assert r["handle"].startswith("cut_"), r
        assert r["volume"] > 0, r
        obj = w.call("get_object", handle=r["handle"])
        assert abs(obj["volume"] - r["volume"]) < 1e-9, (obj, r)


def test_email_trim_cutter_reproduces_as_an_immediate_warning():
    """#282's acceptance criterion, as reported: a flange plate is trimmed by a
    'medial trim cutter' box that was sized wrong (60 mm cutter for a 50 mm
    plate, spanning it completely). The build used to succeed and go hollow;
    now the annihilation is named in the reply to the very call that caused
    it."""
    with Worker() as w:
        w.call("new_document", name="bd_trim_cutter")
        plate = w.call("add_primitive", kind="box", w=50, d=50, h=6)
        cutter = w.call(
            "add_primitive", kind="box", w=60, d=60, h=20, placement=[-5, -5, -5],
        )
        r = w.call("boolean_op", op="cut", base=plate["handle"], tool=cutter["handle"])
        assert "warnings" in r, f"the reported failure must warn here: {r}"
        assert r["volume"] == 0.0, r
        # The agent is told what to look at, not merely that something is off.
        assert "tool" in r["warnings"][0] and "size" in r["warnings"][0], r["warnings"]


# --- PartDesign shares the pattern --------------------------------------------

def test_pocket_that_removes_nothing_warns():
    """A pocket extruded the wrong way removes nothing and reports the body's
    unchanged volume — the same silent no-op as a missed cut. Exactly one of
    the two legacy `reversed` values cuts into the body, so exactly one of them
    must warn."""
    warned = []
    for rev in (False, True):
        with Worker() as w:
            w.call("new_document", name=f"bd_pocket_{int(rev)}")
            h = _build_cube_body(w)
            sk = _top_face_sketch(w, h)
            r = w.call("pocket", sketch=sk, length=5.0, reversed=rev)
            msgs = r.get("warnings", [])
            if msgs:
                assert any("removed nothing" in m for m in msgs), msgs
                assert abs(r["removed_volume"]) < 1e-9, r
                warned.append(rev)
            else:
                assert r["removed_volume"] > 0, r
                assert r["volume_ratio"] < 1.0, r
    assert len(warned) == 1, (
        f"exactly one Reversed value should be a no-op; warned on {warned}"
    )


def test_pocket_away_from_body_is_not_warned():
    """direction='away_from_body' is an explicit request for a feature that
    removes no material, so the miss warning there would be noise, not news."""
    with Worker() as w:
        w.call("new_document", name="bd_pocket_away")
        h = _build_cube_body(w)
        sk = _top_face_sketch(w, h)
        r = w.call("pocket", sketch=sk, length=5.0, direction="away_from_body")
        assert "warnings" not in r, f"an intentional no-op must not warn: {r}"
        assert abs(r["removed_volume"]) < 1e-9, r


def test_hole_reports_removed_volume():
    """hole carries the same sanity numbers: a healthy drilling removes roughly
    the drilled cylinder and does not warn."""
    with Worker() as w:
        w.call("new_document", name="bd_hole")
        h = _build_cube_body(w)
        sk = _top_face_sketch(w, h, radius=3.0)
        r = w.call(
            "hole", sketch=sk, diameter=6.0, depth_type="Dimension", depth=10.0,
            direction="into_body",
        )
        assert "warnings" not in r, r
        # A PartDesign Hole drills a 118° conical point past the nominal depth,
        # so it removes the bore PLUS ~17 mm³ of tip cone — bracket, don't
        # pretend the answer is the plain cylinder.
        drilled = 3.14159265 * 3 * 3 * 10
        assert drilled <= r["removed_volume"] <= drilled * 1.10, r
        assert 0.9 < r["volume_ratio"] < 1.0, r


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

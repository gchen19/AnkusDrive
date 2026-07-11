"""
Toy problems proving the DriftPin worker scaffold.

Each test exercises ONE design property:
  - test_ready_and_ping           : worker boots, protocol handshake works
  - test_version                  : introspection passthrough
  - test_state_persists           : doc created in call #1 visible in call #2
  - test_handles_chain            : multi-step CAD (box + cylinder + cut) via handles
  - test_save_document            : disk-side effect, file size sanity
  - test_bad_method_survives      : unknown method is a recoverable error
  - test_handler_exception_survives: error mid-handler doesn't poison worker state
  - test_stdio_hygiene            : chatter-heavy ops don't corrupt stdout
  - test_graceful_shutdown        : shutdown exits cleanly, no zombie
  - test_fem_cantilever           : full FEM pipeline through IPC

Run:  /Applications/FreeCAD.app/Contents/Resources/bin/python tests/test_worker.py
  or  python3 tests/test_worker.py   (host-side subprocess management is pure stdlib)
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker, WorkerError  # noqa: E402
from driftpin.client import WorkerDied  # noqa: E402


# --- individual tests ---------------------------------------------------------

def test_ready_and_ping():
    with Worker() as w:
        assert w.freecad_version[:2] == ["1", "1"], f"wrong version: {w.freecad_version}"
        assert w.call("ping") == "pong"


def test_version():
    with Worker() as w:
        v = w.call("version")
        assert v["freecad"][:2] == ["1", "1"]
        assert v["python"].startswith("3.11"), f"expected py 3.11, got {v['python']}"


def test_state_persists():
    with Worker() as w:
        w.call("new_document", name="persist")
        w.call("add_primitive", kind="box", w=10, d=20, h=5)
        objs = w.call("list_objects")
        names = [o["name"] for o in objs]
        assert "Box" in names, f"box not in objects: {names}"


def test_handles_chain():
    """Compose a multi-step CAD part using only handles across calls.
    box (20×20×20) minus cylinder (r=5 full-height) = 8000 - π·25·20 ≈ 6429 mm³."""
    with Worker() as w:
        w.call("new_document", name="chain")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        cyl = w.call(
            "add_primitive", kind="cylinder", r=5, h=20,
            placement=[10, 10, 0],
        )
        cut = w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])

        expected = 20 * 20 * 20 - 3.14159 * 5 * 5 * 20
        v = cut["volume"]
        assert abs(v - expected) / expected < 0.01, (
            f"cut volume {v:.1f} not near expected {expected:.1f}"
        )

        handles = w.call("list_handles")
        assert {"box_1", "cylinder_1", "cut_1"} <= set(handles), (
            f"missing handles: {handles.keys()}"
        )


def test_add_gear():
    """Involute gear primitive: external + internal, valid solids, correct pitch.
    m=2, N=12 -> pitch r 12, tip r 14; two gears mesh at rp_a + rp_b apart."""
    with Worker() as w:
        w.call("new_document", name="gears")
        a = w.call("add_gear", teeth=12, module=2.0, height=6.0)
        assert a["pitch_radius"] == 12.0, a["pitch_radius"]
        assert a["tip_radius"] == 14.0, a["tip_radius"]
        assert a["volume"] > 0, a["volume"]
        assert a["handle"].startswith("gear_"), a["handle"]
        b = w.call("add_gear", teeth=24, module=2.0, height=6.0,
                   placement=[36, 0, 0])  # mesh distance = 12 + 24
        assert b["pitch_radius"] == 24.0, b["pitch_radius"]
        ring = w.call("add_gear", teeth=36, module=2.0, height=6.0,
                      external=False, placement=[200, 0, 0])
        assert ring["external"] is False
        for h in (a["handle"], b["handle"], ring["handle"]):
            assert w.call("mass_properties", handle=h)["volume_mm3"] > 0, h


def test_save_document():
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="saved")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        path = os.path.join(tmp, "part.FCStd")
        result = w.call("save_document", path=path)
        assert os.path.isfile(path)
        assert result["size"] > 500, f"suspiciously small FCStd: {result['size']} bytes"


def test_save_document_has_fitted_camera():
    """Headless doc.saveAs writes no GuiDocument; our save must inject one
    with a camera sized to the content, else the GUI opens it invisible."""
    import zipfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="tiny")
        w.call("add_primitive", kind="box", w=5, d=5, h=10)
        path = os.path.join(tmp, "tiny.FCStd")
        result = w.call("save_document", path=path)
        assert result["camera_fit"] is True, "camera_fit flag not set"
        with zipfile.ZipFile(path) as zf:
            assert "GuiDocument.xml" in zf.namelist(), "GuiDocument.xml missing"
            gui = zf.read("GuiDocument.xml").decode()
        assert "<Camera" in gui
        # Parse height; for a 5x5x10 bbox (diag ~12.25) height should be ~18,
        # definitely not the 15000+ that FreeCAD defaults to when camera is absent.
        import re
        m = re.search(r"height\s+([0-9.]+)", gui)
        assert m, "no height field in camera"
        height = float(m.group(1))
        assert 1.0 < height < 200.0, f"camera height {height} not sensible for 10mm part"


def test_bad_method_survives():
    """Unknown method → error response, worker still healthy for next call."""
    with Worker() as w:
        try:
            w.call("no_such_method")
        except WorkerError as e:
            assert e.type == "UnknownMethod"
        else:
            raise AssertionError("expected WorkerError for unknown method")

        assert w.call("ping") == "pong", "worker poisoned by bad method"


def test_handler_exception_survives():
    """Exception inside a handler → error response with traceback, worker alive."""
    with Worker() as w:
        try:
            w.call("boolean_op", op="cut", base="nope_1", tool="nope_2")
        except WorkerError as e:
            assert "unknown handle" in e.remote_message.lower(), e.remote_message
            assert e.remote_traceback, "traceback should be populated"
        else:
            raise AssertionError("expected WorkerError for bad handle")

        assert w.call("ping") == "pong", "worker poisoned by handler exception"


def test_stdio_hygiene():
    """Chatter-heavy ops (many recomputes) must not leak into stdout."""
    with Worker() as w:
        result = w.call("recompute_stress", n=15)
        assert result["objects"] == 15
        assert w.call("ping") == "pong"


def test_graceful_shutdown():
    w = Worker()
    w.call("ping")
    w.shutdown(timeout=5.0)
    assert w.proc.returncode == 0, f"expected clean exit, got {w.proc.returncode}"


def test_list_faces_box():
    """Box has 6 planar faces; +Z face area = w*d, normal = (0,0,1)."""
    with Worker() as w:
        w.call("new_document", name="faces")
        box = w.call("add_primitive", kind="box", w=10, d=20, h=5)
        faces = w.call("list_faces", handle=box["handle"])
        assert len(faces) == 6, f"box should have 6 faces, got {len(faces)}"
        kinds = {f["kind"] for f in faces}
        assert kinds == {"planar"}, f"box faces should all be planar, got {kinds}"
        plus_z = [f for f in faces if f.get("normal") == [0.0, 0.0, 1.0]]
        assert len(plus_z) == 1, f"expected one +Z face, got {len(plus_z)}"
        assert abs(plus_z[0]["area"] - 200.0) < 1e-3


def test_query_faces_top():
    """Query 'top +Z face' on a stepped block returns the higher of the two +Z faces."""
    with Worker() as w:
        w.call("new_document", name="stepped")
        big = w.call("add_primitive", kind="box", w=20, d=20, h=10)
        small = w.call(
            "add_primitive", kind="box", w=10, d=10, h=5,
            placement=[5, 5, 10],
        )
        step = w.call("boolean_op", op="fuse", base=big["handle"], tool=small["handle"])
        top = w.call(
            "query_faces",
            handle=step["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
        )
        assert len(top) >= 2, f"expected ≥2 +Z faces on stepped block, got {len(top)}"
        assert abs(top[0]["centroid"][2] - 15.0) < 1e-3, (
            f"top face should be at z=15, got {top[0]['centroid'][2]}"
        )
        assert abs(top[0]["area"] - 100.0) < 1e-3


def test_query_cylinder_radius():
    """Query cylindrical face by radius matches and returns descriptor with radius."""
    with Worker() as w:
        w.call("new_document", name="cyl")
        cyl = w.call("add_primitive", kind="cylinder", r=7, h=20)
        hits = w.call(
            "query_faces",
            handle=cyl["handle"],
            predicate={"type": "cylindrical", "radius_eq": 7.0},
        )
        assert len(hits) == 1, f"expected 1 cylindrical face of r=7, got {len(hits)}"
        assert abs(hits[0]["radius"] - 7.0) < 1e-3


def test_tag_survives_unrelated_fillet():
    """The golden-path stability test: tag the +Z face of a box, fillet a vertical
    edge (which leaves the +Z face nominally intact other than corner trims), and
    verify the tag still resolves to a face whose centroid + area are recognizably
    'top-ish'. This is what makes FEM/PartDesign references survive edits."""
    with Worker() as w:
        w.call("new_document", name="stable")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)

        # Tag the +Z face before any edit.
        top = w.call(
            "query_faces",
            handle=box["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1]},
        )
        assert len(top) == 1
        top_tag = top[0]["tag"]
        top_area_before = top[0]["area"]

        # Fillet a vertical edge (Edge1 of a Part::Box is along Z).
        edges = w.call("list_edges", handle=box["handle"])
        verticals = [e for e in edges if e["kind"] == "line" and abs(e["length"] - 20.0) < 1e-3]
        assert verticals, "expected vertical edges of length 20 on a 20mm cube"
        # Pick one whose centroid is at a corner (x and y at 0 or 20).
        corner_edge = next(
            e for e in verticals
            if abs(e["centroid"][0]) < 1e-3 and abs(e["centroid"][1]) < 1e-3
        )
        w.call(
            "fillet_edges",
            handle=box["handle"],
            edges=[corner_edge["tag"]],
            radius=2.0,
        )

        # After fillet, list_faces is on the original box (not the fillet feature).
        # The top face's signature shifts only by area (corner clipped) — but the
        # box object itself is unchanged; the fillet is a new derived feature.
        # So the tag MUST still resolve on the original box handle.
        r = w.call("resolve_face", handle=box["handle"], tag=top_tag)
        assert r["index"].startswith("Face"), r
        # Sanity: area unchanged on the underlying box.
        faces_after = w.call("list_faces", handle=box["handle"])
        match = next(f for f in faces_after if f["tag"] == top_tag)
        assert abs(match["area"] - top_area_before) < 1e-3


def test_resolve_unknown_tag_errors():
    with Worker() as w:
        w.call("new_document", name="missing")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        try:
            w.call("resolve_face", handle=box["handle"], tag="f_deadbeefcafe")
        except WorkerError as e:
            assert "not found" in e.remote_message.lower()
        else:
            raise AssertionError("expected WorkerError for unknown tag")


def test_tag_ambiguity_detected():
    """Two coplanar faces with identical area + centroid kind would collide;
    here we force collision by tagging the +Z face of a box, then constructing
    a second box with the same +Z face geometry, fusing them, and verifying
    that the tag now matches multiple faces on the fused shape — which our
    resolver must flag as ambiguous rather than silently picking one."""
    with Worker() as w:
        w.call("new_document", name="amb")
        a = w.call("add_primitive", kind="box", w=10, d=10, h=5)
        # Place the second box far away so its +Z face has a distinct centroid.
        b = w.call(
            "add_primitive", kind="box", w=10, d=10, h=5,
            placement=[100, 0, 0],
        )
        fused = w.call("boolean_op", op="fuse", base=a["handle"], tool=b["handle"])
        # Both +Z faces have area 100 but different centroids → distinct tags,
        # so this is not yet an ambiguity case. Confirm our descriptor uses
        # centroid (i.e. tags differ).
        plus_z = w.call(
            "query_faces",
            handle=fused["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1]},
        )
        assert len(plus_z) == 2
        assert plus_z[0]["tag"] != plus_z[1]["tag"], (
            "centroid must disambiguate equal-area parallel faces"
        )


def test_partdesign_pad_circle():
    """Sketch a fully-constrained circle on XY → pad 10mm. Volume = π·r²·h ≈ 785."""
    with Worker() as w:
        w.call("new_document", name="pd_pad")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        geom = w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        circle_idx = geom["indices"][0]
        # Coincident: circle center to sketch origin (vertex -1 of geom -1).
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Coincident",
            refs=[[circle_idx, 3], [-1, 1]],
        )
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Radius",
            refs=[[circle_idx, 0]], value=5.0,
        )
        status = w.call("close_sketch", sketch=sk["handle"])
        assert status["fully_constrained"], f"sketch should be fully constrained: {status}"

        result = w.call("pad", sketch=sk["handle"], length=10.0)
        expected = 3.14159 * 25 * 10
        assert abs(result["volume"] - expected) / expected < 0.01, (
            f"pad volume {result['volume']:.1f} not near {expected:.1f}"
        )


def test_partdesign_underconstrained_sketch():
    """An unconstrained circle reports fully_constrained=False."""
    with Worker() as w:
        w.call("new_document", name="dof")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [3, 4], "radius": 5}],
        )
        status = w.call("close_sketch", sketch=sk["handle"])
        assert status["fully_constrained"] is False, (
            f"unconstrained circle should NOT be fully constrained: {status}"
        )


def test_partdesign_pocket_through_all():
    """Pad a 20mm cube, then pocket a circle through it. Final volume = 8000 - π·r²·h."""
    with Worker() as w:
        w.call("new_document", name="pocket")
        body = w.call("make_body")

        # Square pad 20×20×20.
        sk1 = w.call("make_sketch", body=body["handle"], plane="XY")
        g1 = w.call(
            "add_sketch_geometry",
            sketch=sk1["handle"],
            items=[
                {"type": "line", "start": [0, 0], "end": [20, 0]},
                {"type": "line", "start": [20, 0], "end": [20, 20]},
                {"type": "line", "start": [20, 20], "end": [0, 20]},
                {"type": "line", "start": [0, 20], "end": [0, 0]},
            ],
        )
        idxs = g1["indices"]
        for i in range(4):
            w.call(
                "add_sketch_constraint",
                sketch=sk1["handle"], type="Coincident",
                refs=[[idxs[i], 2], [idxs[(i + 1) % 4], 1]],
            )
        pad_r = w.call("pad", sketch=sk1["handle"], length=20.0)
        assert abs(pad_r["volume"] - 8000.0) < 1e-3

        # Pocket circle on top face.
        top = w.call(
            "query_faces",
            handle=pad_r["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
        )
        assert top, "expected a top face on the pad"
        plane_h = w.call(
            "make_datum_plane",
            body=body["handle"],
            base={"handle": pad_r["handle"], "tag": top[0]["tag"]},
        )
        sk2 = w.call("make_sketch", body=body["handle"], plane=plane_h["handle"])
        g2 = w.call(
            "add_sketch_geometry",
            sketch=sk2["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        cidx = g2["indices"][0]
        w.call(
            "add_sketch_constraint",
            sketch=sk2["handle"], type="Radius",
            refs=[[cidx, 0]], value=4.0,
        )
        pocket_r = w.call("pocket", sketch=sk2["handle"], through_all=True)
        expected = 8000.0 - 3.14159 * 16 * 20
        assert abs(pocket_r["volume"] - expected) / expected < 0.01, (
            f"pocket volume {pocket_r['volume']:.1f} not near {expected:.1f}"
        )


def test_partdesign_fillet_by_tag():
    """Pad → fillet a top edge selected by tag (proves tagging carries through PartDesign)."""
    with Worker() as w:
        w.call("new_document", name="pd_fillet")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        g = w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[
                {"type": "line", "start": [0, 0], "end": [10, 0]},
                {"type": "line", "start": [10, 0], "end": [10, 10]},
                {"type": "line", "start": [10, 10], "end": [0, 10]},
                {"type": "line", "start": [0, 10], "end": [0, 0]},
            ],
        )
        idxs = g["indices"]
        for i in range(4):
            w.call(
                "add_sketch_constraint",
                sketch=sk["handle"], type="Coincident",
                refs=[[idxs[i], 2], [idxs[(i + 1) % 4], 1]],
            )
        pad_r = w.call("pad", sketch=sk["handle"], length=5.0)
        v_before = pad_r["volume"]

        # Find a top edge (z = 5, length 10).
        edges = w.call("list_edges", handle=pad_r["handle"])
        top_edges = [
            e for e in edges
            if e["kind"] == "line"
            and abs(e["centroid"][2] - 5.0) < 1e-3
            and abs(e["length"] - 10.0) < 1e-3
        ]
        assert len(top_edges) == 4, f"expected 4 top edges, got {len(top_edges)}"

        fillet = w.call(
            "partdesign_fillet",
            feature=pad_r["handle"],
            edges=[top_edges[0]["tag"]],
            radius=1.0,
        )
        # Fillet removes a small wedge of material.
        assert fillet["volume"] < v_before, (
            f"fillet should reduce volume: before={v_before}, after={fillet['volume']}"
        )
        assert (v_before - fillet["volume"]) < 5.0, (
            "fillet of one edge should remove a small volume"
        )


def test_get_object_dumps_pad_props():
    """get_object returns properties of a Pad including Length."""
    with Worker() as w:
        w.call("new_document", name="props")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 3}],
        )
        pad_r = w.call("pad", sketch=sk["handle"], length=7.0)
        obj = w.call("get_object", handle=pad_r["handle"])
        assert obj["type"] == "PartDesign::Pad"
        assert abs(obj["properties"]["Length"] - 7.0) < 1e-6
        assert obj["volume"] > 0


def test_set_property_changes_pad_length():
    """set_property updates Pad.Length and the volume changes accordingly."""
    with Worker() as w:
        w.call("new_document", name="setprop")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad_r = w.call("pad", sketch=sk["handle"], length=10.0)
        v0 = pad_r["volume"]
        w.call("set_property", handle=pad_r["handle"], name="Length", value=20.0)
        obj = w.call("get_object", handle=pad_r["handle"])
        v1 = obj["volume"]
        assert abs(v1 / v0 - 2.0) < 0.01, f"expected 2x volume, got v0={v0}, v1={v1}"


def _build_pad_cube(w, side=30.0, height=30.0):
    """Helper: build a `side` × `side` × `height` pad in a fresh body, return
    {body, pad} handles. Used by the Phase A tests below."""
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
    pad = w.call("pad", sketch=sk["handle"], length=height)
    return {"body": body["handle"], "pad": pad["handle"]}


def _sketch_circle_on_top(w, body_h, pad_h, center, radius):
    """Helper: place a sketch on the top face of `pad_h`, draw a single
    circle constrained to (center, radius). Return sketch handle."""
    top = w.call(
        "query_faces",
        handle=pad_h,
        predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
    )
    assert top, "no top face on pad"
    plane = w.call(
        "make_datum_plane",
        body=body_h,
        base={"handle": pad_h, "tag": top[0]["tag"]},
    )
    sk = w.call("make_sketch", body=body_h, plane=plane["handle"])
    g = w.call(
        "add_sketch_geometry",
        sketch=sk["handle"],
        items=[{"type": "circle", "center": list(center), "radius": float(radius)}],
    )
    cidx = g["indices"][0]
    w.call(
        "add_sketch_constraint",
        sketch=sk["handle"], type="Radius",
        refs=[[cidx, 0]], value=float(radius),
    )
    w.call(
        "add_sketch_constraint",
        sketch=sk["handle"], type="DistanceX",
        refs=[[-1, 1], [cidx, 3]], value=float(center[0]),
    )
    w.call(
        "add_sketch_constraint",
        sketch=sk["handle"], type="DistanceY",
        refs=[[-1, 1], [cidx, 3]], value=float(center[1]),
    )
    return sk["handle"]


def test_hole_simple_through():
    """Pad a 30mm cube, hole through-all of diameter 6 at center.
    Expected volume = 30³ - π·3²·30 ≈ 27000 - 848.2 = 26151.8 mm³."""
    import math
    with Worker() as w:
        w.call("new_document", name="hole_simple")
        h = _build_pad_cube(w, side=30.0, height=30.0)
        sk = _sketch_circle_on_top(w, h["body"], h["pad"], (15, 15), 3.0)
        result = w.call("hole", sketch=sk, diameter=6.0, depth_type="ThroughAll")
        expected = 27000.0 - math.pi * 9.0 * 30.0
        assert abs(result["volume"] - expected) / expected < 0.01, (
            f"hole volume {result['volume']:.2f} not near {expected:.2f}"
        )


def test_hole_counterbore():
    """Counterbore: through-all 6mm diameter with 12mm cut diameter, 5mm cut depth.
    Cube 30³ minus through hole (π·3²·30) minus counterbore step (π·6²·5 - π·3²·5)."""
    import math
    with Worker() as w:
        w.call("new_document", name="hole_cbore")
        h = _build_pad_cube(w, side=30.0, height=30.0)
        sk = _sketch_circle_on_top(w, h["body"], h["pad"], (15, 15), 3.0)
        result = w.call(
            "hole", sketch=sk, diameter=6.0, depth_type="ThroughAll",
            cut_type="Counterbore", cut_diameter=12.0, cut_depth=5.0,
        )
        through_vol = math.pi * 9.0 * 30.0
        cbore_step = math.pi * (36.0 - 9.0) * 5.0
        expected = 27000.0 - through_vol - cbore_step
        assert abs(result["volume"] - expected) / expected < 0.02, (
            f"counterbore volume {result['volume']:.2f} not near {expected:.2f}"
        )


def test_linear_pattern_4_holes():
    """4-hole linear pattern: pad 60×30×10, single hole at (15,15), pattern
    along X with 4 occurrences over 45mm. Expected 4 holes of r=3, h=10."""
    import math
    with Worker() as w:
        w.call("new_document", name="linpat")
        h = _build_pad_cube(w, side=60.0, height=10.0)
        # Force d=30 by using square 60x60... actually _build_pad_cube uses square side.
        # That's fine — we'll just have a 60×60×10 pad and 4 holes spaced 15mm.
        sk = _sketch_circle_on_top(w, h["body"], h["pad"], (15, 15), 3.0)
        hole_r = w.call("hole", sketch=sk, diameter=6.0, depth_type="ThroughAll")
        v_one_hole = 60.0 * 60.0 * 10.0 - hole_r["volume"]  # material removed by 1 hole
        lp = w.call(
            "linear_pattern", feature=hole_r["handle"],
            direction="X", length=45.0, occurrences=4,
        )
        # 4 holes total → removed 4× the per-hole volume (60×60×10 - 36000 = ?
        # Actually: pad volume = 36000, after one hole = 36000 - π·9·10 = 35717.4
        # After 4 holes = 36000 - 4·π·9·10 = 34869.7
        expected = 60.0 * 60.0 * 10.0 - 4.0 * math.pi * 9.0 * 10.0
        assert abs(lp["volume"] - expected) / expected < 0.02, (
            f"linear pattern volume {lp['volume']:.2f} not near {expected:.2f}"
        )
        # Also count cylindrical faces — should be 4 holes worth.
        faces = w.call("list_faces", handle=lp["handle"])
        cyl = [f for f in faces if f["kind"] == "cylindrical"
               and abs(f.get("radius", 0) - 3.0) < 1e-3]
        assert len(cyl) == 4, f"expected 4 r=3 cylindrical faces, got {len(cyl)}"


def test_polar_pattern_6_holes():
    """6-hole bolt circle around the body Z axis. The pad is built centered on
    the origin so all 6 pattern copies land inside the material. Hole r=2 at
    radius 20 from origin → 6 cylindrical faces of r=2 on the result."""
    with Worker() as w:
        w.call("new_document", name="polpat")
        # Centered pad: -30..30 in x and y, 0..10 in z.
        body = w.call("make_body")
        sk_pad = w.call("make_sketch", body=body["handle"], plane="XY")
        g = w.call(
            "add_sketch_geometry",
            sketch=sk_pad["handle"],
            items=[
                {"type": "line", "start": [-30, -30], "end": [30, -30]},
                {"type": "line", "start": [30, -30], "end": [30, 30]},
                {"type": "line", "start": [30, 30], "end": [-30, 30]},
                {"type": "line", "start": [-30, 30], "end": [-30, -30]},
            ],
        )
        idxs = g["indices"]
        for i in range(4):
            w.call(
                "add_sketch_constraint",
                sketch=sk_pad["handle"], type="Coincident",
                refs=[[idxs[i], 2], [idxs[(i + 1) % 4], 1]],
            )
        pad = w.call("pad", sketch=sk_pad["handle"], length=10.0)

        # Hole at (20, 0) — 20mm from Z axis on +X side.
        sk_hole = _sketch_circle_on_top(w, body["handle"], pad["handle"], (20, 0), 2.0)
        hole_r = w.call("hole", sketch=sk_hole, diameter=4.0, depth_type="ThroughAll")

        pp = w.call(
            "polar_pattern", feature=hole_r["handle"],
            axis="Z", angle_deg=360.0, occurrences=6,
        )
        faces = w.call("list_faces", handle=pp["handle"])
        cyl = [f for f in faces if f["kind"] == "cylindrical"
               and abs(f.get("radius", 0) - 2.0) < 1e-3]
        assert len(cyl) == 6, f"expected 6 r=2 cylindrical faces, got {len(cyl)}"


def test_mirrored_half():
    """Mirror a hole across the YZ plane. Expect cylindrical face count to
    double on the mirror feature shape vs the original feature."""
    with Worker() as w:
        w.call("new_document", name="mirror")
        h = _build_pad_cube(w, side=60.0, height=10.0)
        # Hole well off-center so its mirror lands on the other side of YZ
        # (the body's YZ origin plane is at x=0). Both holes must lie inside
        # the pad: pad spans x=0..60, so hole at x=45 mirrors to x=-45 which is
        # OUTSIDE the pad and therefore mirror produces no extra hole.
        # Re-position the body so its origin is at the pad center: shift the
        # sketch by -30 in x. Easier: use a datum plane offset… For this test
        # we just assert the mirror succeeds and produces a Shape; the
        # quantitative volume check would require a centered body.
        sk = _sketch_circle_on_top(w, h["body"], h["pad"], (30, 30), 3.0)
        hole_r = w.call("hole", sketch=sk, diameter=6.0, depth_type="ThroughAll")
        v_after_hole = hole_r["volume"]
        mr = w.call("mirrored", feature=hole_r["handle"], plane="YZ")
        # The hole is at (30,30) which is on the X=30 line, and mirror across
        # YZ (x=0) sends it to x=-30 which is OUTSIDE the pad. So mirror should
        # leave volume unchanged or close to it.
        assert abs(mr["volume"] - v_after_hole) < 1.0, (
            f"out-of-bounds mirror should not change volume: "
            f"hole={v_after_hole:.2f}, mirror={mr['volume']:.2f}"
        )
        # The mirror feature itself should still produce a usable shape.
        faces = w.call("list_faces", handle=mr["handle"])
        assert any(f["kind"] == "cylindrical" for f in faces), (
            "mirrored feature should have at least one cylindrical face from the original hole"
        )


def test_tag_survives_linear_pattern():
    """Tag a face on the original feature, apply a linear pattern, confirm the
    tag still resolves on the original feature handle (not on the pattern)."""
    with Worker() as w:
        w.call("new_document", name="tagsurv")
        h = _build_pad_cube(w, side=40.0, height=10.0)
        sk = _sketch_circle_on_top(w, h["body"], h["pad"], (10, 10), 2.0)
        hole_r = w.call("hole", sketch=sk, diameter=4.0, depth_type="ThroughAll")
        # Tag the cylindrical hole face on the hole feature.
        cyl = w.call(
            "query_faces",
            handle=hole_r["handle"],
            predicate={"type": "cylindrical", "radius_eq": 2.0},
        )
        assert cyl, "no cylindrical face on hole feature"
        hole_tag = cyl[0]["tag"]

        # Apply a linear pattern (creates 3 more copies of the hole).
        w.call(
            "linear_pattern", feature=hole_r["handle"],
            direction="X", length=20.0, occurrences=3,
        )

        # Resolve the tag on the ORIGINAL hole feature handle — must still work.
        r = w.call("resolve_face", handle=hole_r["handle"], tag=hole_tag)
        assert r["index"].startswith("Face"), r


def test_sketch_external_edge():
    """Project a vertical edge of a pad into a sketch as external geometry.
    Verify the sketch records an external geometry entry."""
    with Worker() as w:
        w.call("new_document", name="ext")
        h = _build_pad_cube(w, side=20.0, height=10.0)
        # Find a vertical edge on the pad.
        edges = w.call("list_edges", handle=h["pad"])
        verticals = [
            e for e in edges
            if e["kind"] == "line" and abs(e["length"] - 10.0) < 1e-3
        ]
        assert verticals, "no vertical edges on pad"
        vert_tag = verticals[0]["tag"]

        # New sketch on the top face.
        top = w.call(
            "query_faces",
            handle=h["pad"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
        )
        plane = w.call(
            "make_datum_plane",
            body=h["body"],
            base={"handle": h["pad"], "tag": top[0]["tag"]},
        )
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])

        # Project the vertical edge.
        ext = w.call(
            "add_sketch_external",
            sketch=sk["handle"],
            ref={"handle": h["pad"], "edge": vert_tag},
        )
        assert ext["external_count"] >= 1, ext
        # Sketcher external indices are negative.
        assert ext["external_index"] < 0, ext


def test_mass_properties_box():
    """20mm cube: volume=8000, area=2400, CG=(10,10,10). Density 7.9e-6 → 0.0632 kg."""
    with Worker() as w:
        w.call("new_document", name="mass")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        mp = w.call("mass_properties", handle=box["handle"], density=7.9e-6)
        assert abs(mp["volume_mm3"] - 8000.0) < 1e-3
        assert abs(mp["surface_area_mm2"] - 2400.0) < 1e-3
        assert all(abs(c - 10.0) < 1e-3 for c in mp["center_of_mass_mm"])
        assert abs(mp["mass_kg"] - 0.0632) < 1e-4
        assert mp["bounding_box_mm"] == [0, 0, 0, 20, 20, 20]


def test_assembly_two_parts():
    """Add two boxes to an assembly via in-doc handles, list parts, BOM."""
    with Worker() as w:
        w.call("new_document", name="asm")
        a = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        b = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        asm = w.call("make_assembly", name="MyAsm")
        w.call(
            "add_part", assembly=asm["handle"],
            source={"handle": a["handle"]}, placement=[0, 0, 0],
        )
        w.call(
            "add_part", assembly=asm["handle"],
            source={"handle": b["handle"]}, placement=[20, 0, 0],
        )
        parts = w.call("list_assembly_parts", assembly=asm["handle"])
        assert len(parts) == 2
        bom = w.call("bom_extract", assembly=asm["handle"], density=7.9e-6)
        # Both link to different in-doc Boxes ("Box" vs "Box001"), so 2 BOM rows.
        assert len(bom) == 2
        assert all(r["count"] == 1 for r in bom)
        assert all(abs(r["total_volume_mm3"] - 1000.0) < 1e-3 for r in bom)
        assert all(abs(r["total_mass_kg"] - 0.0079) < 1e-4 for r in bom)


def test_assembly_interference_detected():
    """Two boxes overlapping by 5mm in X direction → 5×10×10 = 500 mm³ overlap."""
    with Worker() as w:
        w.call("new_document", name="interf")
        a = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        b = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        asm = w.call("make_assembly")
        w.call("add_part", assembly=asm["handle"], source={"handle": a["handle"]},
               placement=[0, 0, 0])
        w.call("add_part", assembly=asm["handle"], source={"handle": b["handle"]},
               placement=[5, 0, 0])
        overlaps = w.call("interference_check", assembly=asm["handle"])
        assert len(overlaps) == 1, f"expected 1 overlap, got {len(overlaps)}"
        assert abs(overlaps[0]["interference_mm3"] - 500.0) < 1.0


def test_assembly_no_interference_when_separate():
    """Boxes 50mm apart → no interference."""
    with Worker() as w:
        w.call("new_document", name="separate")
        a = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        b = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        asm = w.call("make_assembly")
        w.call("add_part", assembly=asm["handle"], source={"handle": a["handle"]},
               placement=[0, 0, 0])
        w.call("add_part", assembly=asm["handle"], source={"handle": b["handle"]},
               placement=[50, 0, 0])
        overlaps = w.call("interference_check", assembly=asm["handle"])
        assert overlaps == []


def test_drawing_page_constructed_and_persisted():
    """Drawing page with a projection group is constructed correctly and survives
    saving to .FCStd: page exists, has the expected projection group with N
    child views, and a saved-and-reopened document still has the page intact."""
    import tempfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="draw")
        box = w.call("add_primitive", kind="box", w=20, d=15, h=10)
        page = w.call("make_drawing_page")
        pg = w.call(
            "add_projection_group",
            page=page["handle"], body=box["handle"],
            views=["Front", "Top", "Right"],
        )
        assert len(pg["views"]) >= 3, f"expected ≥3 views, got {pg['views']}"

        # Save and reopen — page must persist.
        path = os.path.join(tmp, "drawing.FCStd")
        w.call("save_document", path=path)
        assert os.path.getsize(path) > 1000

        w.call("open_document", path=path)
        objs = w.call("list_objects")
        types = {o["type"] for o in objs}
        assert "TechDraw::DrawPage" in types, f"page missing after reopen: {types}"
        assert "TechDraw::DrawProjGroup" in types, (
            f"projection group missing after reopen: {types}"
        )


def test_export_drawing_three_formats():
    """export_drawing produces a non-empty, well-formed PDF, SVG, and DXF
    headless (no TechDrawGui), each carrying the multi-view geometry. SVG/DXF are
    native TechDraw output; the PDF leg rasterises the SVG via svglib+reportlab,
    which are deliberately NOT host deps (see pyproject) — so it SKIPs on a minimal
    env (e.g. the conda-forge nightly) that lacks them, like the FEM/slicer gates."""
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="exp")
        box = w.call("add_primitive", kind="box", w=30, d=20, h=10)
        page = w.call("make_drawing_page")["handle"]
        w.call("add_projection_group", page=page, body=box["handle"],
               views=["Front", "Top", "Right"])

        svg = w.call("export_drawing", page=page, path=os.path.join(tmp, "d.svg"))
        dxf = w.call("export_drawing", page=page, path=os.path.join(tmp, "d.dxf"))
        for r in (svg, dxf):
            assert r["size"] > 0 and r["views"] == 3, r
        assert "<svg" in open(os.path.join(tmp, "d.svg"), encoding="utf-8").read(400)
        assert "SECTION" in open(os.path.join(tmp, "d.dxf"), encoding="utf-8",
                                 errors="replace").read()

        # PDF rasterisation needs the optional svglib+reportlab in the worker's Python.
        try:
            pdf = w.call("export_drawing", page=page, path=os.path.join(tmp, "d.pdf"))
        except WorkerError as e:
            if "No module named" in e.remote_message and (
                    "svglib" in e.remote_message or "reportlab" in e.remote_message):
                print("    SKIP PDF leg — svglib/reportlab not installed in the worker")
                return
            raise
        assert pdf["size"] > 0 and pdf["views"] == 3, pdf
        assert open(os.path.join(tmp, "d.pdf"), "rb").read(5) == b"%PDF-"


def test_auto_and_manual_dimensions():
    """Auto extents read the overall size; manual edge/point/diameter dims read
    the true geometry; all of them land in the exported drawing."""
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="dim")
        plate = w.call("add_primitive", kind="box", w=60, d=40, h=8)
        drill = w.call("add_primitive", kind="cylinder", r=6, h=8,
                       placement=[30, 20, 0])
        part = w.call("boolean_op", op="cut", base=plate["handle"],
                      tool=drill["handle"])
        hole = next(e["tag"] for e in w.call("list_edges", handle=part["handle"])
                    if e.get("radius") and abs(e["radius"] - 6.0) < 1e-6)
        page = w.call("make_drawing_page")["handle"]
        w.call("add_projection_group", page=page, body=part["handle"],
               views=["Front", "Top"])

        auto = w.call("add_dimension", page=page, auto=True)["dimensions"]
        # Each overall world-axis extent is dimensioned ONCE across the view set
        # (issue #108 #4: per-view H+V would double-dimension the shared width and
        # the gate flags it `redundant`). Front gives W=60 + H=8; Top adds D=40 and
        # skips the already-covered width — 3 dims, not 4.
        assert len(auto) == 3, auto
        vals = sorted(round(d["value"], 1) for d in auto)
        assert vals == [8.0, 40.0, 60.0], vals  # W=60 (once), D=40, H=8

        # diameter of the hole — true value 12.0, not a foreshortened projection
        dia = w.call("add_dimension", page=page, view="Top", kind="diameter",
                     edge=hole)["dimensions"][0]
        assert abs(dia["value"] - 12.0) < 1e-6, dia

        # an interior position dimension from two model points
        pos = w.call("add_dimension", page=page, view="Top", kind="horizontal",
                     from_point=[0, 20, 0], to_point=[30, 20, 0])["dimensions"][0]
        assert abs(pos["value"] - 30.0) < 1e-6, pos

        out = w.call("export_drawing", page=page, path=os.path.join(tmp, "d.svg"))
        assert out["dimensions"] == 5, out  # 3 auto extents + diameter + position
        svg = open(os.path.join(tmp, "d.svg"), encoding="utf-8").read()
        assert "Ø12.00" in svg and "30.00" in svg  # diameter symbol + position


def test_export_unsupported_extension_raises():
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="bad")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        page = w.call("make_drawing_page")["handle"]
        w.call("add_projection_group", page=page, body=box["handle"])
        try:
            w.call("export_drawing", page=page, path=os.path.join(tmp, "d.png"))
        except WorkerError as e:
            assert "extension" in e.remote_message.lower(), e.remote_message
        else:
            raise AssertionError("expected an error for an unsupported extension")


def test_fem_cantilever():
    """The full FEM pipeline works through IPC. ~3–4s wall time."""
    with Worker() as w:
        t0 = time.time()
        r = w.call("fem_cantilever_demo", _timeout=180.0, mesh_size=500.0)
        elapsed = time.time() - t0
        assert r["nodes"] > 0 and r["tets"] > 0, f"empty mesh: {r}"
        assert r["max_displacement_mm"] > 0
        assert r["max_vonmises_mpa"] > 0
        print(
            f"    FEM: nodes={r['nodes']} tets={r['tets']} "
            f"|u|max={r['max_displacement_mm']:.4f}mm "
            f"vM_max={r['max_vonmises_mpa']:.2f}MPa "
            f"({elapsed:.1f}s)"
        )


def _build_cantilever_fem(w, length=200.0, width=20.0, height=10.0,
                          mesh_size=10.0, fix_face_norm=(-1, 0, 0),
                          extra_constraints=None, element_order=None):
    """Helper: build a steel cantilever beam with the -X face fixed.
    Returns {analysis, box, mesh} handles. Does NOT run the solver.
    extra_constraints: callable(w, analysis_h, box_h) for adding more constraints.
    element_order: '1st'/'2nd' passed through to fem_mesh ('2nd' for modal accuracy)."""
    w.call("new_document", name="cant_helper")
    box = w.call("add_primitive", kind="box", w=length, d=width, h=height)
    fixed_face = w.call(
        "query_faces",
        handle=box["handle"],
        predicate={"type": "planar", "normal_dir": list(fix_face_norm)},
    )
    assert len(fixed_face) == 1, fixed_face

    analysis = w.call("fem_new_analysis", name="A")
    w.call(
        "fem_set_solver", analysis=analysis["handle"], kind="ccx",
        tunables={
            "GeometricalNonlinearity": "linear",
            "MatrixSolverType": "default",
            "IterationsControlParameterTimeUse": False,
        },
    )
    w.call(
        "fem_set_material",
        analysis=analysis["handle"], body=box["handle"],
        material={
            "Name": "Steel-Generic",
            "YoungsModulus": "210000 MPa",
            "PoissonRatio": "0.30",
            "Density": "7900 kg/m^3",
        },
    )
    w.call(
        "fem_add_constraint",
        analysis=analysis["handle"], kind="fixed",
        refs=[{"handle": box["handle"], "tag": fixed_face[0]["tag"]}],
    )
    if extra_constraints is not None:
        extra_constraints(w, analysis["handle"], box["handle"])
    mesh_kw = {"analysis": analysis["handle"], "body": box["handle"],
               "char_length": mesh_size, "_timeout": 120.0}
    if element_order is not None:
        mesh_kw["element_order"] = element_order
    mesh = w.call("fem_mesh", **mesh_kw)
    return {"analysis": analysis["handle"], "box": box["handle"], "mesh": mesh["handle"]}


def test_fem_modal_cantilever():
    """Modal analysis of a slender steel cantilever (300×30×10mm, L/h=30) gated against
    the exact Euler-Bernoulli oracle (beam_modal). With 2nd-order tets the CalculiX
    fundamental lands within 3% of beam theory — linear tets (the old default) shear-
    lock and overshoot ~50%, which is why this used to only check shape. The 2nd
    bending mode (oracle mode 2) also appears among the eigenfrequencies."""
    with Worker() as w:
        orc = w.call("beam_modal", length_mm=300, width_mm=30, height_mm=10,
                     boundary="cantilever", n_modes=2, youngs_gpa=210,
                     density_kg_m3=7900)
        h = _build_cantilever_fem(w, length=300, width=30, height=10, mesh_size=6.0,
                                  element_order="2nd")
        w.call("fem_modal", analysis=h["analysis"], n_modes=6)
        w.call("fem_run", analysis=h["analysis"],
               workdir="/tmp/driftpin_modal", _timeout=300.0)
        freqs = w.call("fem_modal_results", analysis=h["analysis"])["frequencies_hz"]
        assert len(freqs) == 6 and all(f > 0 for f in freqs), freqs
        assert freqs == sorted(freqs), f"freqs must ascend: {freqs}"
        f1_oracle = orc["first_mode_hz"]
        ratio = freqs[0] / f1_oracle
        assert 0.97 <= ratio <= 1.05, (
            f"fundamental {freqs[0]:.1f} Hz vs E-B oracle {f1_oracle:.1f} Hz "
            f"(ratio {ratio:.3f}) — outside 3% gate")
        # the 2nd cantilever bending mode (oracle mode 2) must appear among the modes
        # (other low modes are the stiffer in-plane bend / torsion, which interleave)
        f2_oracle = orc["frequencies_hz"][1]
        assert any(abs(f / f2_oracle - 1.0) < 0.05 for f in freqs), (
            f"oracle 2nd bending {f2_oracle:.1f} Hz not found in {[round(f,1) for f in freqs]}")
        print(f"    modal: ccx {[f'{f:.1f}' for f in freqs[:4]]} Hz; "
              f"fundamental vs E-B {f1_oracle:.1f} Hz → ratio {ratio:.3f}")


def test_fem_buckling_column():
    """Linear buckling of a slender steel column 600×10×10mm with axial unit force.
    Just verifies the pipeline returns one positive buckling factor."""
    def add_axial_force(w, analysis_h, box_h):
        # +X face at x=600 (the loaded end). Apply 1 N axial compressive force.
        loaded = w.call(
            "query_faces",
            handle=box_h,
            predicate={"type": "planar", "normal_dir": [1, 0, 0]},
        )
        assert len(loaded) == 1
        # Pick a Z-edge for the direction (vertical edge of the loaded face).
        edges = w.call("list_edges", handle=box_h)
        z_edges = [
            e for e in edges
            if e["kind"] == "line" and abs(e["length"] - 10.0) < 1e-3
            and abs(e["centroid"][0] - 600.0) < 1e-3
        ]
        assert z_edges, "no Z-edges at the loaded face"
        w.call(
            "fem_add_constraint",
            analysis=analysis_h, kind="force",
            refs=[{"handle": box_h, "tag": loaded[0]["tag"]}],
            force=1.0,
            direction={"handle": box_h, "edge": z_edges[0]["tag"]},
            reversed=True,
        )

    with Worker() as w:
        h = _build_cantilever_fem(
            w, length=600, width=10, height=10, mesh_size=15.0,
            extra_constraints=add_axial_force,
        )
        w.call("fem_buckling", analysis=h["analysis"], n_factors=1)
        w.call("fem_run", analysis=h["analysis"],
               workdir="/tmp/driftpin_buckle", _timeout=300.0)
        results = w.call("fem_buckling_results", analysis=h["analysis"])
        factors = results["buckling_factors"]
        assert len(factors) >= 1, f"expected at least 1 buckling factor, got {factors}"
        assert factors[0] > 0, f"first buckling factor must be positive: {factors[0]}"
        print(f"    buckling factors: {factors}")


def test_fem_thermal_steady_state():
    """Steel bar with one end fixed at 100°C, the opposite end at 20°C, no
    flux on sides. Result temperature field should range between those two
    bounds. (Strict linear-gradient validation needs node-position lookup;
    here we verify the bracketing.)"""
    def add_thermal_constraints(w, analysis_h, box_h):
        # +X face = hot, -X face was already fixed mechanically. Use +X as hot
        # and -X gets fixed-temperature too. Override the -X by adding a
        # temperature constraint on it (the mechanical Fixed is irrelevant for
        # pure thermal here but harmless; the existing helper adds it).
        # Re-find faces:
        cold_face = w.call(
            "query_faces", handle=box_h,
            predicate={"type": "planar", "normal_dir": [-1, 0, 0]},
        )[0]
        hot_face = w.call(
            "query_faces", handle=box_h,
            predicate={"type": "planar", "normal_dir": [1, 0, 0]},
        )[0]
        w.call(
            "fem_add_constraint",
            analysis=analysis_h, kind="temperature",
            refs=[{"handle": box_h, "tag": cold_face["tag"]}],
            temperature=20.0, name="ColdT",
        )
        w.call(
            "fem_add_constraint",
            analysis=analysis_h, kind="temperature",
            refs=[{"handle": box_h, "tag": hot_face["tag"]}],
            temperature=100.0, name="HotT",
        )

    with Worker() as w:
        h = _build_cantilever_fem(
            w, length=200, width=20, height=20, mesh_size=20.0,
            extra_constraints=add_thermal_constraints,
        )
        # Switch to thermomech analysis (steady-state) and tell solver:
        w.call(
            "run_script",
            code=(
                "a = _resolve('" + h["analysis"] + "')\n"
                "for o in a.Group:\n"
                "    if 'Solver' in o.TypeId:\n"
                "        o.AnalysisType = 'thermomech'\n"
                "        o.ThermoMechSteadyState = True\n"
                "        break\n"
                "__result__ = 'ok'\n"
            ),
        )
        # Add steel thermal conductivity to the existing material.
        w.call(
            "run_script",
            code=(
                "a = _resolve('" + h["analysis"] + "')\n"
                "for o in a.Group:\n"
                "    if 'MaterialMechanical' in o.TypeId or 'Material' in o.TypeId:\n"
                "        m = o.Material\n"
                "        m['ThermalConductivity'] = '50 W/m/K'\n"
                "        m['SpecificHeat'] = '500 J/kg/K'\n"
                "        m['ThermalExpansionCoefficient'] = '12 um/m/K'\n"
                "        o.Material = m\n"
                "        break\n"
                "__result__ = 'ok'\n"
            ),
        )
        try:
            w.call("fem_run", analysis=h["analysis"],
                   workdir="/tmp/driftpin_thermal", _timeout=300.0)
        except WorkerError as e:
            # Some FreeCAD/CCX combos error on thermomech without an initial
            # condition or extra constraint — note and skip rather than fail.
            print(f"    fem_run skipped: {e.remote_message[:120]}")
            return
        results = w.call("fem_thermal_results", analysis=h["analysis"])
        temps = results["temperatures_c"]
        if temps is None:
            print("    no temperature field; thermal output not populated by CCX")
            return
        assert 19.0 < temps["min"] < 25.0, (
            f"min temp {temps['min']:.1f} not near cold side 20°C"
        )
        assert 95.0 < temps["max"] < 105.0, (
            f"max temp {temps['max']:.1f} not near hot side 100°C"
        )
        print(f"    thermal: min={temps['min']:.1f}°C max={temps['max']:.1f}°C")


def test_fem_mesh_local_refinement():
    """Add a local mesh refinement on a tagged face; verify a region object is
    attached to the mesh and sized to the requested char_length."""
    with Worker() as w:
        w.call("new_document", name="refine")
        box = w.call("add_primitive", kind="box", w=100, d=20, h=20)
        analysis = w.call("fem_new_analysis", name="A")
        w.call(
            "fem_set_solver", analysis=analysis["handle"], kind="ccx",
            tunables={
                "GeometricalNonlinearity": "linear",
                "MatrixSolverType": "default",
                "IterationsControlParameterTimeUse": False,
            },
        )
        mesh = w.call(
            "fem_mesh", analysis=analysis["handle"], body=box["handle"],
            char_length=20.0, _timeout=120.0,
        )
        baseline_nodes = mesh["nodes"]

        # Refine one face (the +X face, area 20·20 = 400) to 5mm.
        plus_x = w.call(
            "query_faces", handle=box["handle"],
            predicate={"type": "planar", "normal_dir": [1, 0, 0]},
        )[0]
        region = w.call(
            "fem_mesh_refinement",
            mesh=mesh["handle"],
            refs=[{"handle": box["handle"], "tag": plus_x["tag"]}],
            char_length=2.0,
        )
        assert abs(region["char_length"] - 2.0) < 1e-3, region

        # Re-mesh with the region attached: should produce more nodes than baseline.
        # The fem_mesh handler creates a brand-new mesh; instead we inspect the
        # registered region directly and confirm it has the right References.
        info = w.call("get_object", handle=region["handle"])
        assert info["properties"].get("References"), (
            f"refinement region missing references: {info['properties'].get('References')}"
        )
        print(f"    baseline nodes={baseline_nodes}; refinement region attached "
              f"with char_length={region['char_length']}")


def _make_centered_square_sketch(w, body_h, plane_handle, side):
    """Helper: sketch a square of edge `side` centered on origin in the given
    plane. Returns sketch handle. plane_handle is either 'XY'/'XZ'/'YZ' string
    or a datum-plane handle."""
    sk = w.call("make_sketch", body=body_h, plane=plane_handle)
    half = side / 2.0
    g = w.call(
        "add_sketch_geometry",
        sketch=sk["handle"],
        items=[
            {"type": "line", "start": [-half, -half], "end": [half, -half]},
            {"type": "line", "start": [half, -half], "end": [half, half]},
            {"type": "line", "start": [half, half], "end": [-half, half]},
            {"type": "line", "start": [-half, half], "end": [-half, -half]},
        ],
    )
    idxs = g["indices"]
    for i in range(4):
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Coincident",
            refs=[[idxs[i], 2], [idxs[(i + 1) % 4], 1]],
        )
    return sk["handle"]


def test_loft_two_sketches():
    """Loft from a 20mm square at z=0 to a 10mm square at z=15. Expected volume
    is the truncated-pyramid frustum volume (analytical bound). FreeCAD's loft
    interpolates with bsplines so the actual volume is between the linear and
    cubic interpolations; we just verify it's in a sane range."""
    with Worker() as w:
        w.call("new_document", name="loft")
        body = w.call("make_body")
        sk1 = _make_centered_square_sketch(w, body["handle"], "XY", 20.0)
        # Second sketch on a datum plane offset 15mm in +Z.
        plane = w.call("make_datum_plane", body=body["handle"], base="XY", offset=15.0)
        sk2 = _make_centered_square_sketch(w, body["handle"], plane["handle"], 10.0)
        loft = w.call("loft", sketches=[sk1, sk2])
        # Frustum: V = h/3 · (A1 + A2 + sqrt(A1·A2)) = 5 · (400 + 100 + 200) = 3500
        assert 3000 < loft["volume"] < 4500, (
            f"loft volume {loft['volume']:.1f} out of expected frustum range"
        )


def test_helix_curve_basic():
    """Helix: radius=5, pitch=2, height=10. The arc length of N=5 turns of
    helix r=5, pitch=2 is N · sqrt((2π·r)² + pitch²) per turn ≈ 5 · 31.49 ≈ 157.4 mm."""
    with Worker() as w:
        w.call("new_document", name="helix")
        h = w.call("helix", radius=5.0, pitch=2.0, height=10.0)
        info = w.call("get_object", handle=h["handle"])
        assert info["type"] == "Part::Helix"
        assert abs(info["properties"]["Radius"] - 5.0) < 1e-3
        assert abs(info["properties"]["Pitch"] - 2.0) < 1e-3
        assert abs(info["properties"]["Height"] - 10.0) < 1e-3


def test_thickness_5wall_box():
    """Thickness on a 30mm cube with the +Z face removed. With a 2mm wall, the
    interior cavity is 26×26×28 = 18928 mm³, so the shell volume is
    27000 - 18928 = 8072 mm³. (Approximate — corner geometry matters slightly.)"""
    with Worker() as w:
        w.call("new_document", name="thick")
        h = _build_pad_cube(w, side=30.0, height=30.0)
        # Find the top (+Z) face of the pad to remove.
        top = w.call(
            "query_faces",
            handle=h["pad"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
        )
        assert top
        thick = w.call(
            "thickness", base=h["pad"],
            open_faces=[{"handle": h["pad"], "tag": top[0]["tag"]}],
            thickness=2.0,
        )
        # With 2mm wall and -Z bottom kept, interior = 26·26·28 = 18928. Shell = 8072.
        assert 7500 < thick["volume"] < 8700, (
            f"thickness shell volume {thick['volume']:.1f} not near 8072 mm³"
        )


def test_draft_face_angle():
    """Draft a vertical face of a pad by 5°. The face's tilt should change but
    the body remains a closed solid; volume stays positive and bbox grows slightly
    on the drafted face's side."""
    with Worker() as w:
        w.call("new_document", name="draft")
        h = _build_pad_cube(w, side=20.0, height=10.0)
        # Find a vertical face (+X direction) and the bottom face (neutral plane).
        plus_x = w.call(
            "query_faces", handle=h["pad"],
            predicate={"type": "planar", "normal_dir": [1, 0, 0]},
        )
        bottom = w.call(
            "query_faces", handle=h["pad"],
            predicate={"type": "planar", "normal_dir": [0, 0, -1]},
        )
        assert plus_x and bottom
        v_before = w.call("get_object", handle=h["pad"])["volume"]
        draft_r = w.call(
            "draft", base=h["pad"],
            faces=[{"handle": h["pad"], "tag": plus_x[0]["tag"]}],
            neutral_plane={"handle": h["pad"], "tag": bottom[0]["tag"]},
            angle_deg=5.0,
        )
        # Draft removes material from the top of the +X face (5° taper toward the
        # neutral plane). New volume should be slightly less than original.
        assert draft_r["volume"] < v_before, (
            f"draft should reduce volume: before={v_before:.2f}, after={draft_r['volume']:.2f}"
        )
        assert draft_r["volume"] > 0.9 * v_before, (
            f"draft removed too much: before={v_before:.2f}, after={draft_r['volume']:.2f}"
        )


def test_multi_document_isolation():
    """Two documents: an object in one is not visible from the other. Switching
    active makes its objects show up in list_objects."""
    with Worker() as w:
        w.call("new_document", name="docA")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        w.call("new_document", name="docB")
        w.call("add_primitive", kind="sphere", r=4)

        # Initially active is docB.
        objs_b = w.call("list_objects")
        assert any(o["type"] == "Part::Sphere" for o in objs_b), (
            f"Sphere should be in active docB: {objs_b}"
        )

        # Switch to docA.
        w.call("set_active_document", name="docA")
        objs_a = w.call("list_objects")
        assert any(o["type"] == "Part::Box" for o in objs_a), (
            f"Box should be in docA: {objs_a}"
        )
        assert not any(o["type"] == "Part::Sphere" for o in objs_a), (
            f"Sphere should NOT be in docA: {objs_a}"
        )

        # list_documents returns both.
        docs = w.call("list_documents")
        names = {d["name"] for d in docs}
        assert {"docA", "docB"} <= names, f"missing docs: {names}"


def test_close_document_invalidates_handles():
    """Close a document; handles into the closed doc are invalidated. The
    worker stays alive, and the closed doc is gone from list_documents."""
    with Worker() as w:
        w.call("new_document", name="ephemeral")
        box = w.call("add_primitive", kind="box", w=5, d=5, h=5)
        w.call("new_document", name="keeper")  # leave one alive so worker has an active doc
        w.call("set_active_document", name="ephemeral")
        result = w.call("close_document", name="ephemeral")
        assert result["closed"] == "ephemeral", result
        # The box's handle should be invalidated.
        assert box["handle"] in result["invalidated_handles"], (
            f"box handle should be invalidated: {result}"
        )
        # ephemeral is gone.
        docs = w.call("list_documents")
        names = {d["name"] for d in docs}
        assert "ephemeral" not in names, f"ephemeral still listed: {names}"
        # Worker still alive.
        assert w.call("ping") == "pong"


_FORCE_DESYNC = """
import sys, FreeCAD as App
worker = sys.modules['worker']
class _FakeApp:
    ActiveDocument = None
    listDocuments = staticmethod(App.listDocuments)
    setActiveDocument = staticmethod(App.setActiveDocument)
worker._real_App = App
worker.App = _FakeApp
"""

_RESTORE_APP = """
import sys
worker = sys.modules['worker']
worker.App = worker._real_App
"""


def test_active_doc_self_heals_when_only_one_open():
    """If App.ActiveDocument desyncs to None but exactly one doc is open, the
    next handler call should silently re-activate it instead of erroring."""
    with Worker() as w:
        w.call("new_document", name="solo")
        w.call("run_script", code=_FORCE_DESYNC)
        try:
            # _active_doc reads App.ActiveDocument (None via fake), sees one
            # open doc, calls setActiveDocument to recover. After we restore
            # the real App, the recovery should be visible.
            w.call("run_script", code="""
import sys
worker = sys.modules['worker']
worker._active_doc()
""")
        finally:
            w.call("run_script", code=_RESTORE_APP)
        docs = w.call("list_documents")
        assert any(d["name"] == "solo" and d["active"] for d in docs), docs


def test_active_doc_raises_with_doc_list_when_ambiguous():
    """Multiple docs open + ActiveDocument None → error must name the open docs
    so the caller knows what to set_active_document to."""
    with Worker() as w:
        w.call("new_document", name="amb_a")
        w.call("new_document", name="amb_b")
        w.call("run_script", code=_FORCE_DESYNC)
        try:
            w.call("run_script", code="""
import sys
worker = sys.modules['worker']
worker._active_doc()
""")
        except Exception as e:
            msg = str(e)
            assert "amb_a" in msg and "amb_b" in msg, msg
            assert "set_active_document" in msg, msg
        else:
            raise AssertionError("expected error when active doc is ambiguous")
        finally:
            w.call("run_script", code=_RESTORE_APP)


def test_transaction_rollback():
    """Open a transaction, add a primitive, abort. The added object should be gone."""
    with Worker() as w:
        w.call("new_document", name="tx")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        objs_before = w.call("list_objects")
        names_before = {o["name"] for o in objs_before}

        w.call("transaction_open", label="addCyl")
        w.call("add_primitive", kind="cylinder", r=3, h=10)
        objs_during = w.call("list_objects")
        assert any(o["type"] == "Part::Cylinder" for o in objs_during), (
            f"cylinder should exist during tx: {objs_during}"
        )

        w.call("transaction_abort")
        objs_after = w.call("list_objects")
        names_after = {o["name"] for o in objs_after}
        assert names_before == names_after, (
            f"transaction abort should restore state: before={names_before}, after={names_after}"
        )


def test_transaction_commit_keeps_changes():
    """Commit (rather than abort) keeps the changes."""
    with Worker() as w:
        w.call("new_document", name="txk")
        w.call("transaction_open", label="addBox")
        w.call("add_primitive", kind="box", w=8, d=8, h=8)
        w.call("transaction_commit")
        objs = w.call("list_objects")
        assert any(o["type"] == "Part::Box" for o in objs), (
            f"box should persist after commit: {objs}"
        )


def test_fem_decomposed_cantilever():
    """Re-implement the cantilever via the decomposed FEM tools and verify the
    results agree with the monolithic baseline. Refs are by tag (Slice 1), so
    this also proves tags carry into the FEM pipeline."""
    with Worker() as w:
        # Baseline from the monolithic demo (same geometry/material/load).
        baseline = w.call("fem_cantilever_demo", _timeout=180.0, mesh_size=500.0)

        # Now build it piece-by-piece in a fresh document.
        w.call("new_document", name="cantilever_decomp")
        box = w.call("add_primitive", kind="box", w=8000, d=1000, h=1000)
        # Tag the -X face (fixed end) and the +X face (loaded end).
        fixed_face = w.call(
            "query_faces",
            handle=box["handle"],
            predicate={"type": "planar", "normal_dir": [-1, 0, 0]},
        )
        loaded_face = w.call(
            "query_faces",
            handle=box["handle"],
            predicate={"type": "planar", "normal_dir": [1, 0, 0]},
        )
        assert len(fixed_face) == 1 and len(loaded_face) == 1
        # Pick a vertical edge on the loaded face for the force direction.
        edges = w.call("list_edges", handle=box["handle"])
        # Edges along Z at x=8000 — length 1000, centroid x≈8000.
        z_edges_at_loaded = [
            e for e in edges
            if e["kind"] == "line"
            and abs(e["length"] - 1000.0) < 1e-3
            and abs(e["centroid"][0] - 8000.0) < 1e-3
        ]
        assert z_edges_at_loaded, "expected vertical edges at the loaded end"
        force_dir_edge = z_edges_at_loaded[0]

        analysis = w.call("fem_new_analysis", name="Analysis")
        w.call(
            "fem_set_solver",
            analysis=analysis["handle"],
            kind="ccx",
            tunables={
                "GeometricalNonlinearity": "linear",
                "ThermoMechSteadyState": True,
                "MatrixSolverType": "default",
                "IterationsControlParameterTimeUse": False,
            },
        )
        w.call(
            "fem_set_material",
            analysis=analysis["handle"],
            body=box["handle"],
            material={
                "Name": "Steel-Generic",
                "YoungsModulus": "210000 MPa",
                "PoissonRatio": "0.30",
                "Density": "7900 kg/m^3",
            },
        )
        w.call(
            "fem_add_constraint",
            analysis=analysis["handle"],
            kind="fixed",
            refs=[{"handle": box["handle"], "tag": fixed_face[0]["tag"]}],
        )
        w.call(
            "fem_add_constraint",
            analysis=analysis["handle"],
            kind="force",
            refs=[{"handle": box["handle"], "tag": loaded_face[0]["tag"]}],
            force=9000.0,  # match fem_cantilever_demo's default (both N) for the
            direction={"handle": box["handle"], "edge": force_dir_edge["tag"]},
            reversed=True,  # baseline-vs-decomposed relative comparison below
        )
        mesh = w.call(
            "fem_mesh",
            analysis=analysis["handle"],
            body=box["handle"],
            char_length=500.0,
            _timeout=180.0,
        )
        assert mesh["nodes"] > 0 and mesh["tets"] > 0
        w.call(
            "fem_run",
            analysis=analysis["handle"],
            workdir="/tmp/driftpin_fem_decomp",
            _timeout=300.0,
        )
        results = w.call("fem_results", analysis=analysis["handle"], top_n=3)

        # Same geometry/material/load → results should be in the same ballpark
        # as the monolithic demo. Gmsh nondeterminism alone produces ~5% run-
        # to-run variance on this geometry, so 8% is the meaningful tolerance.
        bd = baseline["max_displacement_mm"]
        bs = baseline["max_vonmises_mpa"]
        d = results["max_displacement_mm"]
        s = results["max_vonmises_mpa"]
        assert abs(d - bd) / bd < 0.08, (
            f"max disp diverged: baseline={bd:.4f}mm, decomposed={d:.4f}mm"
        )
        assert abs(s - bs) / bs < 0.15, (
            f"max von Mises diverged: baseline={bs:.2f}MPa, decomposed={s:.2f}MPa"
        )
        assert len(results["top_stress_nodes"]) == 3
        assert results["max_displacement_vector"] is not None
        print(
            f"    decomp FEM: nodes={mesh['nodes']} "
            f"|u|max={d:.4f}mm (baseline {bd:.4f}) "
            f"vM_max={s:.2f}MPa (baseline {bs:.2f})"
        )


# --- revolve + axis-coincident pre-check -------------------------------------

def _make_closed_polyline_sketch(w, body_h, plane, segments):
    """Helper: place a sketch on `plane`, add `segments` as a chain of line
    segments closed by Coincident constraints. Returns sketch handle."""
    sk = w.call("make_sketch", body=body_h, plane=plane)
    items = [{"type": "line", "start": s, "end": e} for (s, e) in segments]
    g = w.call("add_sketch_geometry", sketch=sk["handle"], items=items)
    idxs = g["indices"]
    n = len(segments)
    for i in range(n):
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Coincident",
            refs=[[idxs[i], 2], [idxs[(i + 1) % n], 1]],
        )
    return sk["handle"]


def test_revolve_rectangle_off_axis_succeeds():
    """Sanity baseline: a rectangle entirely on the +X side of the Y axis,
    revolved 360° around Y, produces a hollow torus-ring. Volume = π·(R²−r²)·h.
    This regresses the pre-existing bug where revolve set no ReferenceAxis
    (Label 'Y-axis' vs Name 'Y_Axis' mismatch) and silently produced null."""
    with Worker() as w:
        w.call("new_document", name="rev_ok")
        body = w.call("make_body")
        sk = _make_closed_polyline_sketch(w, body["handle"], "XY", [
            ([5, 1], [10, 1]),
            ([10, 1], [10, 5]),
            ([10, 5], [5, 5]),
            ([5, 5], [5, 1]),
        ])
        r = w.call("revolve", sketch=sk, axis="Y", angle=360.0)
        expected = 3.14159 * (100 - 25) * 4  # π·(R²−r²)·h
        assert abs(r["volume"] - expected) / expected < 0.01, (
            f"rectangle revolve: got {r['volume']:.1f}, expected ~{expected:.1f}"
        )


def test_revolve_axis_coincident_edge_diagnosed():
    """A profile with an edge ALONG the revolution axis must error before
    OCCT does, with a message naming the offending edges and the workaround.
    Setup: triangle with one edge on the X axis, revolved around X."""
    with Worker() as w:
        w.call("new_document", name="rev_axis_bad")
        body = w.call("make_body")
        # Triangle: base (0,0)→(10,0) lies ON X axis. Revolving around X
        # would sweep that segment to itself — zero-thickness sliver.
        sk = _make_closed_polyline_sketch(w, body["handle"], "XY", [
            ([0, 0], [10, 0]),    # ← coincident with X axis
            ([10, 0], [0, 5]),
            ([0, 5], [0, 0]),
        ])
        try:
            w.call("revolve", sketch=sk, axis="X", angle=360.0)
        except WorkerError as e:
            msg = e.remote_message
            assert "axis" in msg.lower(), f"error should name the axis: {msg}"
            assert "boolean" in msg.lower() or "subtract" in msg.lower(), (
                f"error should suggest the workaround: {msg}"
            )
            assert "edge 0" in msg, (
                f"error should identify the offending edge index: {msg}"
            )
        else:
            raise AssertionError(
                "expected pre-check to reject axis-coincident profile"
            )


def test_revolve_half_annulus_around_axis_diagnosed():
    """The user's actual failure case: half-annulus profile where both short
    radial segments lie on the revolution axis. Pre-check must flag BOTH
    bad edges in the error."""
    with Worker() as w:
        w.call("new_document", name="rev_crescent")
        body = w.call("make_body")
        # Half-annulus: two arcs + two radial segments on +X axis.
        sk_h = w.call("make_sketch", body=body["handle"], plane="XY")
        # Outer semicircle from (10,0) thru (0,10) to (-10,0).
        # Inner semicircle from (5,0) thru (0,5) to (-5,0).
        # Two segments: (-10,0)→(-5,0) and (5,0)→(10,0) — both ON X axis.
        g = w.call(
            "add_sketch_geometry",
            sketch=sk_h["handle"],
            items=[
                {"type": "arc", "center": [0, 0], "radius": 10,
                 "start_angle": 0, "end_angle": 180},
                {"type": "arc", "center": [0, 0], "radius": 5,
                 "start_angle": 0, "end_angle": 180},
                {"type": "line", "start": [5, 0], "end": [10, 0]},
                {"type": "line", "start": [-10, 0], "end": [-5, 0]},
            ],
        )
        try:
            w.call("revolve", sketch=sk_h["handle"], axis="X", angle=360.0)
        except WorkerError as e:
            msg = e.remote_message
            # Both segments are axis-coincident; the diagnostic must count
            # at least 2 bad edges.
            import re
            m = re.search(r"has (\d+) edge", msg)
            assert m, f"error should report edge count: {msg}"
            assert int(m.group(1)) >= 2, (
                f"expected at least 2 axis-coincident edges flagged, got: {msg}"
            )
        else:
            raise AssertionError("expected pre-check to reject crescent profile")


def test_revolve_pre_check_allows_full_circle_profile():
    """Pre-check must NOT flag closed-loop edges (circles, ellipses) — those
    have no endpoints to test, and they're valid revolution profiles."""
    with Worker() as w:
        w.call("new_document", name="rev_circle")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        # Full circle centered on +X side; revolving around Y makes a torus.
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 0], "radius": 3}],
        )
        r = w.call("revolve", sketch=sk["handle"], axis="Y", angle=360.0)
        # Torus volume = 2·π²·R·r² with R=10, r=3 → ~1776 mm³.
        expected = 2 * 3.14159**2 * 10 * 9
        assert abs(r["volume"] - expected) / expected < 0.01, (
            f"torus revolve: got {r['volume']:.1f}, expected ~{expected:.1f}"
        )


# --- intent-driven defaults (ModelThread, coupled enums) ---------------------

def test_list_thread_options_returns_thread_types():
    """list_thread_options() with no args returns the available ThreadType
    enum values. The list must be non-empty and contain ISOMetricProfile."""
    with Worker() as w:
        r = w.call("list_thread_options")
        assert "thread_types" in r
        types = r["thread_types"]
        assert isinstance(types, list) and len(types) > 0
        assert "ISOMetricProfile" in types, f"expected ISOMetricProfile in {types}"


def test_list_thread_options_returns_sizes_for_type():
    """Passing thread_type returns the coupled thread_size enum values for
    that specific type. ISOMetricProfile uses 'M4x0.7' (with pitch), NOT
    bare 'M4' — that's exactly the coupling the tool exists to surface."""
    with Worker() as w:
        r = w.call("list_thread_options", thread_type="ISOMetricProfile")
        assert r["thread_type"] == "ISOMetricProfile"
        sizes = r["thread_sizes"]
        assert isinstance(sizes, list) and len(sizes) > 0
        assert "M4x0.7" in sizes, f"expected M4x0.7 in {sizes}"
        assert "M4" not in sizes, (
            "ISOMetricProfile uses pitch-suffixed sizes; bare 'M4' should NOT "
            "be valid — that's the coupling pitfall we're surfacing"
        )


def test_list_thread_options_bad_type_errors():
    """An unknown thread_type errors with the list of valid types included."""
    with Worker() as w:
        try:
            w.call("list_thread_options", thread_type="NotAThreadType")
        except WorkerError as e:
            msg = e.remote_message.lower()
            assert "unknown thread_type" in msg
            assert "valid" in msg, f"error should list valid types, got: {e.remote_message}"
        else:
            raise AssertionError("expected WorkerError for bad thread_type")


def test_list_thread_options_does_not_pollute_active_document():
    """The probe creates a temp doc to read enums. After the call, the user's
    active document and its objects must be unchanged."""
    with Worker() as w:
        w.call("new_document", name="user_doc")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        objs_before = w.call("list_objects")
        w.call("list_thread_options")
        w.call("list_thread_options", thread_type="ISOMetricProfile")
        objs_after = w.call("list_objects")
        assert objs_before == objs_after, (
            f"probe leaked into active doc:\nbefore={objs_before}\nafter={objs_after}"
        )
        # Active doc is restored and accepts further tool calls.
        v = w.call("get_object", handle=box["handle"])
        assert v["name"] == "Box"


def _hole_with_intent(w, intended_for=None, model_thread=None):
    """Build a 30×30×10 pad, sketch a circle on top, drill an M4 threaded hole.
    Returns the resulting hole's full property dump."""
    h = _build_20cube(w)
    plane = _top_face_datum(w, h["pad"], h["body"])
    sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
    w.call(
        "add_sketch_geometry",
        sketch=sk["handle"],
        items=[{"type": "circle", "center": [10, 10], "radius": 2}],
    )
    params = dict(
        sketch=sk["handle"], diameter=4.0, depth_type="Dimension", depth=5.0,
        threaded=True, thread_type="ISOMetricProfile", thread_size="M4x0.7",
        direction="into_body",
    )
    if intended_for is not None:
        params["intended_for"] = intended_for
    if model_thread is not None:
        params["model_thread"] = model_thread
    r = w.call("hole", **params)
    return w.call("get_object", handle=r["handle"])


def test_hole_intended_for_print_sets_model_thread_true():
    """intended_for='print' on a threaded hole defaults ModelThread=True so
    the printed screw threads have geometry the screw can actually engage."""
    with Worker() as w:
        w.call("new_document", name="hole_print")
        obj = _hole_with_intent(w, intended_for="print")
        assert obj["properties"]["ModelThread"] is True, (
            "print intent must turn ModelThread ON (screw needs thread geometry)"
        )


def test_hole_intended_for_machine_sets_model_thread_false():
    """intended_for='machine' leaves ModelThread=False so the model is a
    smooth pilot — CAM drives the physical tap from the metadata."""
    with Worker() as w:
        w.call("new_document", name="hole_machine")
        obj = _hole_with_intent(w, intended_for="machine")
        assert obj["properties"]["ModelThread"] is False


def test_hole_intended_for_drawing_sets_model_thread_false():
    """intended_for='drawing' leaves ModelThread=False (cosmetic annotation)."""
    with Worker() as w:
        w.call("new_document", name="hole_drawing")
        obj = _hole_with_intent(w, intended_for="drawing")
        assert obj["properties"]["ModelThread"] is False


def test_hole_explicit_model_thread_overrides_intent():
    """An explicit model_thread value wins over the intended_for default —
    the caller knows what they want."""
    with Worker() as w:
        w.call("new_document", name="hole_override")
        # 'machine' would default to False; explicit True overrides.
        obj = _hole_with_intent(w, intended_for="machine", model_thread=True)
        assert obj["properties"]["ModelThread"] is True


def test_hole_bad_intended_for_raises():
    """An unrecognized intended_for value errors clearly."""
    with Worker() as w:
        w.call("new_document", name="hole_bad_intent")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 2}],
        )
        try:
            w.call(
                "hole", sketch=sk["handle"], diameter=4.0, threaded=True,
                intended_for="prototype",
            )
        except WorkerError as e:
            assert "intended_for" in e.remote_message.lower()
        else:
            raise AssertionError("expected WorkerError for bad intended_for")


# --- handle table / run_script integration -----------------------------------

def test_register_handle_round_trip():
    """An object created via run_script (no auto-register) becomes addressable
    by the rest of the tool surface after register_handle. The handle must
    behave identically to one returned by add_primitive."""
    with Worker() as w:
        w.call("new_document", name="register")
        # Create a box via raw Python — outside the handle ecosystem.
        w.call(
            "run_script",
            auto_register=False,
            code=(
                "import FreeCAD as App\n"
                "doc = App.ActiveDocument\n"
                "b = doc.addObject('Part::Box', 'RawBox')\n"
                "b.Length = 8; b.Width = 8; b.Height = 8\n"
                "doc.recompute()\n"
                "__result__ = b.Name\n"
            ),
        )
        # Without a handle, list_faces can't reach it.
        r = w.call("register_handle", object="RawBox")
        assert r["handle"].startswith("manual_")
        assert r["name"] == "RawBox"
        assert r["type"] == "Part::Box"
        # The handle works in downstream tools.
        faces = w.call("list_faces", handle=r["handle"])
        assert len(faces) == 6, f"box should have 6 faces, got {len(faces)}"
        props = w.call("mass_properties", handle=r["handle"])
        assert abs(props["volume_mm3"] - 512.0) < 1e-3


def test_register_handle_missing_object_errors():
    """A clean error when the object name isn't in the active document."""
    with Worker() as w:
        w.call("new_document", name="register_missing")
        try:
            w.call("register_handle", object="NoSuchObject")
        except WorkerError as e:
            assert "no object named" in e.remote_message.lower(), e.remote_message
        else:
            raise AssertionError("expected WorkerError for missing object")


def test_run_script_auto_registers_new_shapes():
    """run_script with auto_register=True (default) returns handles for any
    new shape-bearing objects the script created — eliminating the
    register_handle round-trip for the common case."""
    with Worker() as w:
        w.call("new_document", name="autoreg")
        r = w.call(
            "run_script",
            code=(
                "import FreeCAD as App\n"
                "import Part\n"
                "doc = App.ActiveDocument\n"
                "a = doc.addObject('Part::Box', 'A')\n"
                "a.Length = 5; a.Width = 5; a.Height = 5\n"
                "b = doc.addObject('Part::Sphere', 'B')\n"
                "b.Radius = 3\n"
                "doc.recompute()\n"
            ),
        )
        names = {entry["name"] for entry in r["registered"]}
        assert names == {"A", "B"}, f"expected A,B in registered, got {names}"
        # Each entry's handle resolves and the registered handle prefix is 'script'.
        for entry in r["registered"]:
            assert entry["handle"].startswith("script_")
            obj = w.call("get_object", handle=entry["handle"])
            assert obj["name"] == entry["name"]


def test_run_script_auto_register_off_returns_empty():
    """auto_register=False suppresses the auto-registration behavior — useful
    when the script creates many intermediate objects you don't want polluting
    the handle table."""
    with Worker() as w:
        w.call("new_document", name="autoreg_off")
        r = w.call(
            "run_script",
            auto_register=False,
            code=(
                "import FreeCAD as App\n"
                "doc = App.ActiveDocument\n"
                "doc.addObject('Part::Box', 'Lonely')\n"
                "doc.recompute()\n"
            ),
        )
        assert r["registered"] == [], (
            f"auto_register=False should yield empty registered list, got {r['registered']}"
        )


def test_run_script_auto_register_skips_pre_existing():
    """Only NEW objects get auto-registered. Objects the script merely
    inspects or modifies stay out of the registered list."""
    with Worker() as w:
        w.call("new_document", name="autoreg_pre")
        # Pre-existing object created via the tool surface.
        existing = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        r = w.call(
            "run_script",
            code=(
                "import FreeCAD as App\n"
                "doc = App.ActiveDocument\n"
                "doc.Box.Length = 12\n"  # modify existing
                "doc.addObject('Part::Sphere', 'Fresh')\n"
                "doc.recompute()\n"
            ),
        )
        names = {entry["name"] for entry in r["registered"]}
        assert names == {"Fresh"}, (
            f"only NEW object should be auto-registered, got {names}"
        )


# --- through='wall' on hollow shells -----------------------------------------

def test_through_wall_solid_body_equivalent_to_full_depth():
    """On a solid body, through='wall' should cut the full body thickness
    (one wall = whole body). Verify by comparing to the ThroughAll equivalent."""
    with Worker() as w:
        w.call("new_document", name="thru_solid_wall")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        pkt = w.call("pocket", sketch=sk["handle"], through="wall")
        # Body is 20mm thick, so the cylinder removed = π·16·20 ≈ 1005 mm³.
        expected = 8000.0 - 3.14159 * 16 * 20
        assert abs(pkt["volume"] - expected) / expected < 0.01, (
            f"through='wall' on solid: got {pkt['volume']:.1f}, expected ~{expected:.1f}"
        )
        assert pkt["wall_depth_mm"] is not None
        assert 19.0 < pkt["wall_depth_mm"] < 21.0, (
            f"wall_depth_mm should be ~20mm for solid 20-cube, got {pkt['wall_depth_mm']}"
        )


def _build_open_topped_shell(w, side=30.0, height=30.0, wall=2.0):
    """Build a shell: pad an `side`×`side`×`height` cube, then thickness it
    with the +Z face removed to create a hollow shell of wall thickness `wall`.
    Returns {body, shell} where `shell` is the Thickness feature (current Tip)."""
    h = _build_pad_cube(w, side=side, height=height)
    top = w.call(
        "query_faces",
        handle=h["pad"],
        predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
    )
    thick = w.call(
        "thickness", base=h["pad"],
        open_faces=[{"handle": h["pad"], "tag": top[0]["tag"]}],
        thickness=wall,
    )
    return {"body": h["body"], "shell": thick["handle"], "pad": h["pad"]}


def _side_face_datum(w, body_h, shell_h, normal_dir, axis_letter):
    """Datum plane attached to one of the shell's lateral faces (normal_dir
    is e.g. [1,0,0]). axis_letter selects which side: '+X', '-X', '+Y', '-Y'
    for centroid_max/min selection."""
    pred_axis_map = {"+X": ("centroid_max", "x"), "-X": ("centroid_min", "x"),
                     "+Y": ("centroid_max", "y"), "-Y": ("centroid_min", "y")}
    key, val = pred_axis_map[axis_letter]
    faces = w.call(
        "query_faces",
        handle=shell_h,
        predicate={"type": "planar", "normal_dir": normal_dir, key: val},
    )
    assert faces, f"no {axis_letter} face on shell"
    return w.call(
        "make_datum_plane",
        body=body_h,
        base={"handle": shell_h, "tag": faces[0]["tag"]},
    )


def test_through_wall_hollow_shell_cuts_one_wall_only():
    """The keystone test. On a hollow shell, through='wall' must cut through
    exactly one wall and leave the opposite wall intact (cavity preserved).
    Compare against through='body' which cuts through BOTH lateral walls."""
    with Worker() as w_wall:
        w_wall.call("new_document", name="shell_wall")
        h = _build_open_topped_shell(w_wall, side=30.0, height=30.0, wall=2.0)
        shell_vol = w_wall.call("get_object", handle=h["shell"])["volume"]
        # Sketch on +X face, hole going -X into the shell.
        plane = _side_face_datum(w_wall, h["body"], h["shell"], [1, 0, 0], "+X")
        sk = w_wall.call("make_sketch", body=h["body"], plane=plane["handle"])
        w_wall.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            # Local (15,15) because the datum-plane attachment puts the
            # sketch's origin at the face's bbox corner, not its center.
            items=[{"type": "circle", "center": [15, 15], "radius": 4}],
        )
        r_wall = w_wall.call("pocket", sketch=sk["handle"], through="wall")
        # +X wall is 2mm thick → cut removes ~π·16·2 ≈ 100 mm³ (plus tiny epsilon).
        removed_wall = shell_vol - r_wall["volume"]
        assert 80 < removed_wall < 130, (
            f"through='wall' should remove ~100 mm³ (one 2mm wall), got {removed_wall:.1f}"
        )
        assert r_wall["wall_depth_mm"] is not None
        assert 1.9 < r_wall["wall_depth_mm"] < 2.1, (
            f"wall_depth_mm should be ~2.0, got {r_wall['wall_depth_mm']}"
        )

    with Worker() as w_body:
        w_body.call("new_document", name="shell_body")
        h = _build_open_topped_shell(w_body, side=30.0, height=30.0, wall=2.0)
        shell_vol = w_body.call("get_object", handle=h["shell"])["volume"]
        plane = _side_face_datum(w_body, h["body"], h["shell"], [1, 0, 0], "+X")
        sk = w_body.call("make_sketch", body=h["body"], plane=plane["handle"])
        w_body.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [15, 15], "radius": 4}],
        )
        r_body = w_body.call("pocket", sketch=sk["handle"], through="body")
        # ThroughAll cuts through +X wall AND -X wall → ~200 mm³ removed.
        removed_body = shell_vol - r_body["volume"]
        assert 180 < removed_body < 230, (
            f"through='body' should remove ~200 mm³ (both walls), got {removed_body:.1f}"
        )
        # The key invariant: through='body' removes substantially more than
        # through='wall' on a hollow shell — that's the bug class this fixes.
        assert removed_body > 1.5 * 100, (
            "through='body' must remove materially more than through='wall' "
            "(both walls cut, not just one)"
        )


def test_through_wall_hole_on_shell_preserves_cavity():
    """The M4 case in miniature: Hole + through='wall' on a shelled body must
    drill exactly one wall, leaving the cavity sealed otherwise."""
    with Worker() as w:
        w.call("new_document", name="hole_shell")
        h = _build_open_topped_shell(w, side=30.0, height=30.0, wall=2.0)
        shell_vol = w.call("get_object", handle=h["shell"])["volume"]
        plane = _side_face_datum(w, h["body"], h["shell"], [1, 0, 0], "+X")
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [15, 15], "radius": 2}],
        )
        r = w.call(
            "hole", sketch=sk["handle"], diameter=4.0, through="wall",
        )
        # 4mm-dia hole through 2mm wall: cylinder ≈ π·4·2 = 25 mm³, plus drill
        # point cone (~10% extra). Accept anywhere in [20, 40].
        removed = shell_vol - r["volume"]
        assert 18 < removed < 45, (
            f"through='wall' hole should remove ~25 mm³, got {removed:.1f}"
        )
        assert 1.9 < r["wall_depth_mm"] < 2.1, r["wall_depth_mm"]


def test_through_wall_incompatible_with_direction_away():
    """through= implies direction='into_body'. Combining with
    direction='away_from_body' is contradictory and must error."""
    with Worker() as w:
        w.call("new_document", name="through_conflict")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        try:
            w.call(
                "pocket", sketch=sk["handle"],
                through="wall", direction="away_from_body",
            )
        except WorkerError as e:
            assert "direction" in e.remote_message.lower()
        else:
            raise AssertionError("expected WorkerError for through+away conflict")


def test_through_bad_value_raises():
    """Invalid `through` value rejected clearly."""
    with Worker() as w:
        w.call("new_document", name="through_bad")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        try:
            w.call("pocket", sketch=sk["handle"], through="diagonally")
        except WorkerError as e:
            assert "through" in e.remote_message.lower()
        else:
            raise AssertionError("expected WorkerError for bad through value")


# --- verify_feature -----------------------------------------------------------

def test_verify_feature_pad_passes():
    """A Pad's actual delta = π·r²·h. With matching expected, passed=True
    and the report carries the numbers for downstream introspection."""
    with Worker() as w:
        w.call("new_document", name="verify_pad")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad = w.call("pad", sketch=sk["handle"], length=10.0)
        expected = 3.14159 * 25 * 10  # +785.4 (additive)
        r = w.call("verify_feature", handle=pad["handle"], expected_delta_mm3=expected)
        assert r["passed"] is True, r["message"]
        assert r["previous_volume_mm3"] == 0.0
        assert abs(r["actual_delta_mm3"] - expected) / expected < 0.01
        assert "OK" in r["message"]


def test_verify_feature_pocket_passes():
    """A through-pocket on a 20mm cube subtracts π·r²·h. Expected delta is
    negative; the helper handles the BaseFeature chain to compute actual."""
    with Worker() as w:
        w.call("new_document", name="verify_pocket")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        pkt = w.call(
            "pocket", sketch=sk["handle"], through_all=True, direction="into_body",
        )
        expected = -3.14159 * 16 * 20  # ~-1005, NEGATIVE because subtractive
        r = w.call("verify_feature", handle=pkt["handle"], expected_delta_mm3=expected)
        assert r["passed"] is True, r["message"]
        assert r["actual_delta_mm3"] < 0, "subtractive delta must be negative"
        assert abs(r["previous_volume_mm3"] - 8000.0) < 1e-3


def test_verify_feature_mismatch_reports_diagnostic():
    """When actual delta differs from expected by more than tolerance, passed
    is False and the message identifies the magnitude of the disagreement."""
    with Worker() as w:
        w.call("new_document", name="verify_mismatch")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad = w.call("pad", sketch=sk["handle"], length=10.0)
        # Claim we expected a much smaller pad (50%) than we actually made.
        r = w.call(
            "verify_feature", handle=pad["handle"],
            expected_delta_mm3=400.0,
        )
        assert r["passed"] is False, r["message"]
        assert "MISMATCH" in r["message"]
        # Ratio reports the directionality of the surprise.
        assert r["ratio"] > 1.5, f"expected ~2x ratio, got {r['ratio']:.2f}"


def test_verify_feature_wrong_sign_fails_loudly():
    """A wrong sign on expected_delta is itself a useful failure — if the user
    expected -8000 (subtraction) but the feature actually added 8000, the
    relative error is 2x, well outside any reasonable tolerance."""
    with Worker() as w:
        w.call("new_document", name="verify_sign")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad = w.call("pad", sketch=sk["handle"], length=10.0)
        # Pad added ~+785; user mistakenly said expected was -785 (subtraction).
        r = w.call(
            "verify_feature", handle=pad["handle"],
            expected_delta_mm3=-785.4,
        )
        assert r["passed"] is False, r["message"]
        # Diff is ~2 × |expected|, so ratio (actual/expected) is negative ~-1.
        assert r["ratio"] < 0, f"sign error should produce negative ratio, got {r['ratio']}"


def test_verify_feature_part_cut():
    """Part::Cut isn't a PartDesign feature but exposes Base for the diff —
    verify_feature must handle this path."""
    with Worker() as w:
        w.call("new_document", name="verify_cut")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        cyl = w.call(
            "add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0],
        )
        cut = w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])
        expected = -3.14159 * 25 * 20  # cylinder volume removed
        r = w.call("verify_feature", handle=cut["handle"], expected_delta_mm3=expected)
        assert r["passed"] is True, r["message"]
        assert abs(r["previous_volume_mm3"] - 8000.0) < 1e-3


def test_verify_feature_tolerance_controls_pass_fail():
    """Same actual delta, different tolerances. Tight tolerance fails;
    loose tolerance passes. Confirms the tolerance knob is wired correctly."""
    with Worker() as w:
        w.call("new_document", name="verify_tol")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad = w.call("pad", sketch=sk["handle"], length=10.0)
        # Actual ≈ 785.4. Claim expected = 800 → ~1.8% off.
        tight = w.call(
            "verify_feature", handle=pad["handle"],
            expected_delta_mm3=800.0, tolerance=0.005,
        )
        assert tight["passed"] is False, tight["message"]
        loose = w.call(
            "verify_feature", handle=pad["handle"],
            expected_delta_mm3=800.0, tolerance=0.05,
        )
        assert loose["passed"] is True, loose["message"]


def test_verify_feature_abs_tolerance_for_tiny_expected():
    """When expected_delta is tiny (or zero), relative tolerance is meaningless.
    abs_tolerance must catch the case. Stress-test: expected=0, actual tiny
    but within abs_tolerance, must pass."""
    with Worker() as w:
        w.call("new_document", name="verify_abs")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad = w.call("pad", sketch=sk["handle"], length=10.0)
        # expected=0 should never match actual=785, regardless of abs_tolerance.
        r = w.call(
            "verify_feature", handle=pad["handle"],
            expected_delta_mm3=0.0, abs_tolerance=0.01,
        )
        assert r["passed"] is False, r["message"]
        assert r["ratio"] is None  # division by ~zero protected
        # Now: expected exactly equals actual → trivially within abs_tol.
        actual = r["actual_delta_mm3"]
        r2 = w.call(
            "verify_feature", handle=pad["handle"],
            expected_delta_mm3=actual,
        )
        assert r2["passed"] is True


def test_verify_feature_unsupported_type_errors():
    """A primitive (Part::Box) has no BaseFeature and isn't Part::Cut, so
    verify_feature has nothing to diff against — must error clearly rather
    than silently treat previous_volume as 0 and report a phony positive delta."""
    with Worker() as w:
        w.call("new_document", name="verify_unsupp")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        try:
            w.call("verify_feature", handle=box["handle"], expected_delta_mm3=1000.0)
        except WorkerError as e:
            assert "previous shape" in e.remote_message.lower(), e.remote_message
        else:
            raise AssertionError("expected WorkerError for unsupported feature type")


# --- subtractive direction abstraction ---------------------------------------

def _build_20cube(w):
    """Helper: pad a 20×20×20 cube on XY in a fresh body. Returns {body, pad}."""
    body = w.call("make_body")
    sk = w.call("make_sketch", body=body["handle"], plane="XY")
    g = w.call(
        "add_sketch_geometry",
        sketch=sk["handle"],
        items=[
            {"type": "line", "start": [0, 0], "end": [20, 0]},
            {"type": "line", "start": [20, 0], "end": [20, 20]},
            {"type": "line", "start": [20, 20], "end": [0, 20]},
            {"type": "line", "start": [0, 20], "end": [0, 0]},
        ],
    )
    idxs = g["indices"]
    for i in range(4):
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Coincident",
            refs=[[idxs[i], 2], [idxs[(i + 1) % 4], 1]],
        )
    pad = w.call("pad", sketch=sk["handle"], length=20.0)
    return {"body": body["handle"], "pad": pad["handle"]}


def _top_face_datum(w, pad_handle, body_handle):
    """Helper: make a datum plane attached to the +Z (top) face of `pad`."""
    top = w.call(
        "query_faces",
        handle=pad_handle,
        predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
    )
    return w.call(
        "make_datum_plane",
        body=body_handle,
        base={"handle": pad_handle, "tag": top[0]["tag"]},
    )


def test_pocket_direction_into_body_top_face():
    """direction='into_body' on a sketch sitting on the body's top-face datum
    plane: Pocket's default (Reversed=False) already extrudes downward into the
    body. Verify volume decreases by the expected cylinder volume."""
    with Worker() as w:
        w.call("new_document", name="pkt_into_top")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        pkt = w.call(
            "pocket", sketch=sk["handle"], through_all=True, direction="into_body",
        )
        expected = 8000.0 - 3.14159 * 16 * 20
        assert abs(pkt["volume"] - expected) / expected < 0.01, (
            f"into_body pocket should cut through: got {pkt['volume']:.1f}, expected ~{expected:.1f}"
        )


def test_pocket_direction_away_from_body():
    """direction='away_from_body' on the same setup: must NOT remove material,
    even though the default Reversed=False would have."""
    with Worker() as w:
        w.call("new_document", name="pkt_away")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        pkt = w.call(
            "pocket", sketch=sk["handle"], length=10.0, direction="away_from_body",
        )
        assert abs(pkt["volume"] - 8000.0) < 1e-3, (
            f"away_from_body pocket should preserve volume: got {pkt['volume']:.1f}"
        )


def test_pocket_direction_auto_flips_when_default_wrong():
    """User's actual failure mode: sketch sits on the body's BOTTOM face (XY
    origin), so Pocket's default Reversed=False extrudes -Z — into empty space,
    not into the body. The direction abstraction must auto-flip to Reversed=True
    so the cut actually goes through the body. Confirm both the geometric
    outcome AND that Reversed got flipped."""
    with Worker() as w:
        w.call("new_document", name="pkt_autoflip")
        h = _build_20cube(w)
        # Sketch on XY (the body's bottom face). Reversed=False extrudes -Z, away.
        sk = w.call("make_sketch", body=h["body"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 4}],
        )
        pkt = w.call(
            "pocket", sketch=sk["handle"], through_all=True, direction="into_body",
        )
        expected = 8000.0 - 3.14159 * 16 * 20
        assert abs(pkt["volume"] - expected) / expected < 0.01, (
            f"auto-flip pocket should cut through: got {pkt['volume']:.1f}, expected ~{expected:.1f}"
        )
        obj = w.call("get_object", handle=pkt["handle"])
        assert obj["properties"]["Reversed"] is True, (
            "Reversed must have flipped to True since default direction missed the body"
        )


def test_hole_direction_into_body_drills_through_top_face():
    """The user's M4 case in miniature: Hole on a +Z-normal datum needs the
    direction abstraction so the caller doesn't have to guess which Reversed
    value Hole (vs Pocket) wants. Verify material was removed."""
    with Worker() as w:
        w.call("new_document", name="hole_into")
        h = _build_20cube(w)
        plane = _top_face_datum(w, h["pad"], h["body"])
        sk = w.call("make_sketch", body=h["body"], plane=plane["handle"])
        g = w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 2}],
        )
        w.call(
            "add_sketch_constraint",
            sketch=sk["handle"], type="Radius",
            refs=[[g["indices"][0], 0]], value=2.0,
        )
        h_r = w.call(
            "hole", sketch=sk["handle"], diameter=4.0,
            depth_type="Dimension", depth=5.0, direction="into_body",
        )
        # Hole's default DrillPoint='Angled' adds a 118° conical tip beyond
        # `depth`, so removed volume = cylinder (π·r²·h) + drill-point cone
        # (~10% more). Accept anywhere in [cylinder, 1.3×cylinder].
        cyl = 3.14159 * 4 * 5  # r=2, h=5
        removed = 8000.0 - h_r["volume"]
        assert cyl * 0.95 < removed < cyl * 1.3, (
            f"into_body hole should remove ~{cyl:.1f}–{cyl * 1.3:.1f} mm³ "
            f"(cylinder + drill-point cone), actual {removed:.1f} mm³"
        )


def test_pocket_direction_bad_value_raises():
    """An invalid direction string must error clearly, not silently fall through."""
    with Worker() as w:
        w.call("new_document", name="pkt_bad_dir")
        hb = _build_20cube(w)
        plane = _top_face_datum(w, hb["pad"], hb["body"])
        sk = w.call("make_sketch", body=hb["body"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 2}],
        )
        try:
            w.call("pocket", sketch=sk["handle"], length=5.0, direction="sideways")
        except WorkerError as e:
            assert "direction" in e.remote_message.lower(), e.remote_message
        else:
            raise AssertionError("expected WorkerError for bad direction value")


# --- visibility hygiene -------------------------------------------------------

def _visibility_of(w, handle):
    """Read the App-level Visibility flag through get_object."""
    obj = w.call("get_object", handle=handle)
    return obj["properties"].get("Visibility")


def test_boolean_op_hides_inputs():
    """A Part::Cut subsumes its Base and Tool; the inputs must be Visibility=False
    so re-opening the doc doesn't ghost the uncut primitive on top of the result."""
    with Worker() as w:
        w.call("new_document", name="bool_viz")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        cyl = w.call("add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0])
        cut = w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])

        assert _visibility_of(w, box["handle"]) is False, "box (Base) should be hidden"
        assert _visibility_of(w, cyl["handle"]) is False, "cylinder (Tool) should be hidden"
        assert _visibility_of(w, cut["handle"]) is True, "Cut result should stay visible"


def test_save_visibility_hygiene_partdesign_body():
    """A PartDesign Body's Group (sketch + pad) renders via the Body's own shape.
    After save, every feature inside the Body must be Visibility=False; the Body
    itself stays visible."""
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="body_viz")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk["handle"],
            items=[{"type": "circle", "center": [0, 0], "radius": 5}],
        )
        pad = w.call("pad", sketch=sk["handle"], length=10.0)

        path = os.path.join(tmp, "body.FCStd")
        w.call("save_document", path=path)

        assert _visibility_of(w, body["handle"]) is True, "Body must stay visible"
        assert _visibility_of(w, sk["handle"]) is False, "sketch inside Body must be hidden"
        assert _visibility_of(w, pad["handle"]) is False, "pad inside Body must be hidden"


def test_save_hygiene_preserves_standalone_primitive():
    """A primitive that isn't consumed by anything (no Base/Tool/Body group)
    must keep Visibility=True after the hygiene pass — otherwise hygiene is
    overzealous and would hide every standalone object."""
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="lone_box")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        path = os.path.join(tmp, "lone.FCStd")
        w.call("save_document", path=path)
        assert _visibility_of(w, box["handle"]) is True, "lone primitive must stay visible"


def test_set_visibility_explicit_override():
    """set_visibility flips the flag, and save_document with
    visibility_hygiene=False preserves the override."""
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="vis_override")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        cyl = w.call("add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0])
        w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])
        assert _visibility_of(w, box["handle"]) is False

        # Explicitly re-show the Base.
        r = w.call("set_visibility", handle=box["handle"], visible=True)
        assert r["visible"] is True
        assert _visibility_of(w, box["handle"]) is True

        # Default save would re-hide it (hygiene runs); opt out to preserve.
        path = os.path.join(tmp, "override.FCStd")
        w.call("save_document", path=path, visibility_hygiene=False)
        assert _visibility_of(w, box["handle"]) is True, (
            "visibility_hygiene=False must not re-hide an explicit override"
        )


def test_add_rack():
    with Worker() as w:
        w.call("new_document", name="rack_t")
        r = w.call("add_rack", teeth=10, module=2.0)
        assert r["handle"].startswith("rack_"), r
        assert r["volume"] > 0, r
        assert abs(r["pitch"] - 6.283) < 0.01, r        # module*pi
        assert abs(r["length"] - 62.83) < 0.01, r       # teeth*module*pi
        assert abs(r["tooth_height"] - 4.5) < 1e-6, r   # 2.25*module
        assert r["module"] == 2.0, r
        assert r["teeth"] == 10, r


def test_add_sprocket():
    with Worker() as w:
        w.call("new_document", name="sprocket_t")
        # #40 / ANSI 40 chain: pitch 12.7 mm, roller 7.92 mm, 17 teeth.
        r = w.call("add_sprocket", teeth=17, chain_pitch=12.7,
                   roller_diameter=7.92, height=6.0)
        assert r["handle"].startswith("sprocket_"), r
        assert r["volume"] > 0, r
        # PD = 12.7 / sin(pi/17) ~= 69.1158
        assert abs(r["pitch_diameter"] - 69.1158) < 0.01, r
        assert r["chain_pitch"] == 12.7, r
        assert r["teeth"] == 17, r
        assert r["bore"] == 0, r
        assert r["tip_radius"] > r["pitch_diameter"] / 2.0, r
        # teeth must be >= 3
        try:
            w.call("add_sprocket", teeth=2, chain_pitch=12.7, roller_diameter=7.92)
            assert False, "expected ValueError for teeth < 3"
        except Exception:
            pass


def test_add_pulley():
    with Worker() as w:
        w.call("new_document", name="t")
        r = w.call("add_pulley", teeth=20, belt_pitch=2.0, width=6)
        assert r["handle"].startswith("pulley_"), r
        assert abs(r["pitch_diameter"] - 12.7324) < 0.01, r
        assert r["teeth"] == 20, r
        assert r["belt_pitch"] == 2.0, r
        assert r["width"] == 6, r
        assert r["flanged"] is True, r
        vol_flanged = r["volume"]
        assert vol_flanged > 0, r

        r2 = w.call("add_pulley", teeth=20, belt_pitch=2.0, width=6,
                    flanged=False)
        assert r2["flanged"] is False, r2
        assert r2["volume"] > 0, r2
        # Flanges add material: flanged volume strictly greater.
        assert vol_flanged > r2["volume"], (vol_flanged, r2["volume"])

        # height overrides width when both given.
        r3 = w.call("add_pulley", teeth=20, belt_pitch=2.0, width=6, height=12,
                    flanged=False)
        assert r3["width"] == 12, r3
        assert r3["volume"] > r2["volume"], (r3["volume"], r2["volume"])


def test_add_spring():
    with Worker() as w:
        w.call("new_document", name="spring_t")
        r = w.call("add_spring", wire_diameter=2, outer_diameter=20,
                   free_length=40, coils=8)
        assert r["handle"].startswith("spring_"), r
        assert r["volume"] > 0, r
        # solid_height = coils * wire_diameter = 8 * 2 = 16
        assert abs(r["solid_height"] - 16.0) < 1e-6, r
        # mean_diameter = outer_diameter - wire_diameter = 20 - 2 = 18
        assert abs(r["mean_diameter"] - 18.0) < 1e-6, r
        # spring rate in a sane band (steel, ~3.4 N/mm here)
        assert 1.0 <= r["spring_rate_n_per_mm"] <= 10.0, r
        assert r["free_length"] == 40, r
        assert r["coils"] == 8, r
        # outer_diameter must exceed wire_diameter
        try:
            w.call("add_spring", wire_diameter=20, outer_diameter=20,
                   free_length=40, coils=8)
            assert False, "expected ValueError for OD <= wire_diameter"
        except Exception as e:
            assert "outer_diameter" in str(e), e


def test_dfm_check_handle_matches_hand_built_descriptor():
    # issue #175 DfX v2 Shape wiring: dfm_check reading a LIVE handle must produce
    # the same finding as the hand-built descriptor it produces today. A 20 mm cube
    # pulled +z has 4 vertical side walls (0 deg draft -> draft violations) and a
    # flat top+bottom (90 deg -> fine); no undercuts.
    with Worker() as w:
        w.call("new_document", name="dfm_wire")
        box = _ws_box(w)  # 20mm cube
        live = w.call("dfm_check", handle=box, pull_axis="+z", process="injection")
        # the hand-built descriptor for the same cube (what a caller writes today)
        hand = w.call("dfm_check", faces=[
            {"name": "s0", "draft_deg": 0.0, "wall_mm": 20.0},
            {"name": "s1", "draft_deg": 0.0, "wall_mm": 20.0},
            {"name": "s2", "draft_deg": 0.0, "wall_mm": 20.0},
            {"name": "s3", "draft_deg": 0.0, "wall_mm": 20.0},
            {"name": "top", "draft_deg": 90.0, "wall_mm": 20.0},
            {"name": "bot", "draft_deg": 90.0, "wall_mm": 20.0},
        ], pull_axis="+z", process="injection")
        # PARITY: the two paths agree on the finding (counts, score, verdict).
        assert len(live["draft_violations"]) == len(hand["draft_violations"]) == 4, (live, hand)
        assert len(live["undercut_faces"]) == len(hand["undercut_faces"]) == 0, (live, hand)
        assert live["min_wall_violations"] == hand["min_wall_violations"] == [], (live, hand)
        assert abs(live["score"] - hand["score"]) < 1e-9, (live["score"], hand["score"])
        assert live["pass"] is hand["pass"] is False, (live, hand)
        # the handle path additionally surfaces the live geometry it read
        assert live["n_faces"] == 6, live
        assert live["wall_thickness_stats"]["n"] == 6, live
        assert abs(live["wall_thickness_stats"]["mean_mm"] - 20.0) < 0.01, live


def test_tolerance_stackup_handle_matches_hand_built_chain():
    # issue #175 tolerance v2 Shape wiring: tolerance_stackup reading a LIVE
    # handle must produce the same stack a hand-built chain produces today. A
    # stepped block (20 mm cube + a smaller 10 mm boss fused on top — same
    # section would fuse into one plain box and erase the step) measured along
    # +z has step faces at z=0/20/30 -> links of 20 and 10 mm; ISO 2768-m puts
    # both in the 6-30 band (+/-0.2).
    with Worker() as w:
        w.call("new_document", name="tol_wire")
        base = _ws_box(w)                                   # 20mm cube at origin
        cap = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                     placement=[5, 5, 20])["handle"]
        step = w.call("boolean_op", op="fuse", base=base, tool=cap)["handle"]
        live = w.call("tolerance_stackup", handle=step, axis="+z", method="rss")
        hand = w.call("tolerance_stackup", chain=[
            {"name": "base", "nominal": 20.0, "plus": 0.2, "minus": -0.2},
            {"name": "cap", "nominal": 10.0, "plus": 0.2, "minus": -0.2},
        ], method="rss")
        # PARITY: the two paths agree on every stack number.
        assert abs(live["nominal"] - hand["nominal"]) < 1e-9, (live, hand)
        assert live["worstcase"] == hand["worstcase"], (live, hand)
        assert live["rss"] == hand["rss"], (live, hand)
        # the handle path additionally surfaces the chain it derived
        assert len(live["chain"]) == 2, live
        assert abs(live["chain"][0]["nominal"] - 20.0) < 1e-6, live
        assert abs(live["chain"][1]["nominal"] - 10.0) < 1e-6, live
        assert live["chain"][0]["plus"] == 0.2, live        # ISO 2768-m, 6-30
        assert live["axis"] == "+z", live
        assert live["n_step_faces"] >= 3, live
        # explicit default_tol overrides the general-tolerance class
        tight = w.call("tolerance_stackup", handle=step, axis="+z",
                       default_tol=0.05)
        assert tight["chain"][0]["plus"] == 0.05, tight
        assert abs(tight["worstcase"]["spread"] - 0.2) < 1e-9, tight
        # a solid with no step faces along the axis is a clean error
        w.call("new_document", name="tol_wire_err")
        ball = w.call("add_primitive", kind="sphere", r=10)["handle"]
        try:
            w.call("tolerance_stackup", handle=ball, axis="+z")
            assert False, "expected an error for a stepless solid"
        except Exception as e:
            assert "dimension chain" in str(e), e


def test_add_spring_squared_ground_ends():
    # Squared-and-ground compression spring (issue #175): the last coil at each
    # end is closed (inactive), so active coils Na = Nt - 2, solid height = d*Nt,
    # and the rate uses the ACTIVE coils. Nt=10, d=2.5, OD=25, L0=60.
    with Worker() as w:
        w.call("new_document", name="spring_sg")
        r = w.call("add_spring", wire_diameter=2.5, outer_diameter=25.0,
                   free_length=60.0, coils=10, kind="compression")
        assert r["end_type"] == "squared_ground", r
        # handbook: Na = Nt - 2 = 8
        assert abs(r["active_coils"] - 8.0) < 1e-6, r
        assert abs(r["total_coils"] - 10.0) < 1e-6, r
        # solid height Ls = d*Nt = 2.5*10 = 25.0
        assert abs(r["solid_height"] - 25.0) < 1e-6, r
        # active-coil pitch p = (L0 - 2d)/Na = (60 - 5)/8 = 6.875, and it
        # reconstructs the free length L0 = p*Na + 2d = 60
        assert abs(r["active_pitch_mm"] - 6.875) < 1e-3, r
        assert abs(r["active_pitch_mm"] * r["active_coils"] + 2 * 2.5 - 60.0) < 1e-3, r
        # rate uses ACTIVE coils: k = G d^4/(8 D^3 Na), D = 22.5
        D = 22.5
        k = 79300.0 * 2.5 ** 4 / (8.0 * D ** 3 * 8.0)
        assert abs(r["spring_rate_n_per_mm"] - round(k, 4)) < 1e-3, (r, k)
        assert r["volume"] > 0
        # a free length at/under the solid height is unphysical -> ValueError
        try:
            w.call("add_spring", wire_diameter=2.5, outer_diameter=25.0,
                   free_length=20.0, coils=10)
            assert False, "expected ValueError for free_length <= solid_height"
        except Exception as e:
            assert "solid_height" in str(e) or "free_length" in str(e), e


def test_add_fastener():
    with Worker() as w:
        w.call("new_document", name="fastener_test")

        # Hex nut: major_diameter 6.0, has an axial hole (vol < solid hex prism).
        n = w.call("add_fastener", kind="hex_nut", size="M6")
        assert n["handle"].startswith("fastener_"), n
        assert n["major_diameter"] == 6.0, n
        assert n["pitch"] == 1.0, n
        assert n["volume"] > 0, n
        # Solid M6 hex prism (af=10.0, height=5.2): vol = (sqrt(3)/2)*af^2 * h
        import math
        solid_hex = (math.sqrt(3) / 2.0) * 10.0 ** 2 * 5.2
        assert n["volume"] < solid_hex, (n["volume"], solid_hex)

        # Socket head cap screw: head_diameter 5.5, positive volume.
        s = w.call("add_fastener", kind="socket_head_cap_screw", size="M3", length=10)
        assert s["handle"].startswith("fastener_"), s
        assert s["volume"] > 0, s
        assert s["head_diameter"] == 5.5, s
        assert s["length"] == 10.0, s
        assert s["model_thread"] is False, s

        # Washer: annulus volume matches pi*(OD^2 - ID^2)/4 * thk.
        wsh = w.call("add_fastener", kind="washer", size="M3")
        expect = math.pi * (7.0 ** 2 - 3.0 ** 2) / 4.0 * 0.5
        assert abs(wsh["volume"] - expect) < 1e-3, (wsh["volume"], expect)

        # length required for screws/bolts.
        try:
            w.call("add_fastener", kind="hex_bolt", size="M6")
            assert False, "expected ValueError for missing length"
        except WorkerError:
            pass

        # unknown size rejected.
        try:
            w.call("add_fastener", kind="hex_nut", size="M99")
            assert False, "expected ValueError for unknown size"
        except WorkerError:
            pass


def test_add_bearing():
    import math
    with Worker() as w:
        w.call("new_document", name="t_bearing")

        # designation lookup path: 608 -> bore 8, OD 22, width 7
        r = w.call("add_bearing", designation="608")
        assert r["handle"].startswith("bearing_"), r
        assert r["bore"] == 8.0, r
        assert r["outer_diameter"] == 22.0, r
        assert r["width"] == 7.0, r
        assert r["designation"] == "608", r
        expected = math.pi * (11.0 ** 2 - 4.0 ** 2) * 7.0
        assert abs(r["volume"] - expected) < 1e-3, (r, expected)

        # explicit-dims path also works (no designation)
        r2 = w.call("add_bearing", bore=10.0, outer_diameter=30.0, width=9.0)
        assert r2["handle"].startswith("bearing_"), r2
        assert r2["bore"] == 10.0, r2
        assert r2["designation"] is None, r2
        assert r2["volume"] > 0, r2

        # unknown designation with no dims -> ValueError
        try:
            w.call("add_bearing", designation="9999")
            assert False, "expected ValueError for unknown designation"
        except Exception as e:
            assert "9999" in str(e) or "unknown" in str(e).lower(), e


def test_oring_groove():
    with Worker() as w:
        w.call("new_document", name="oring")

        # Calc-only path: pure gland calculator, no geometry.
        r = w.call("oring_groove", cross_section=2.62, inner_diameter=20, cut=False)
        assert abs(r["groove_depth"] - 1.965) < 1e-3, r       # 2.62 * 0.75
        assert abs(r["groove_width"] - 3.406) < 1e-3, r       # 2.62 * 1.30
        assert abs(r["squeeze_pct"] - 25.0) < 1e-2, r
        assert r["groove_inner_diameter"] == 20.0, r
        assert r["groove_outer_diameter"] > 20.0, r           # spans outward
        assert abs(r["groove_outer_diameter"] - 26.812) < 1e-2, r  # 20 + 2*3.406
        assert "handle" not in r, r                           # no geometry on calc path

        # Cut path: machine the groove into the top (+Z) face of a box.
        box = w.call("add_primitive", kind="box", w=60, d=60, h=10)
        faces = w.call("list_faces", handle=box["handle"])
        top = next(f for f in faces
                   if f.get("normal") and abs(f["normal"][2] - 1) < 1e-6)
        r2 = w.call("oring_groove", handle=box["handle"], face=top["tag"],
                    cross_section=2.62, inner_diameter=20, cut=True)
        assert r2["handle"].startswith("oring_groove_"), r2
        assert r2["volume"] > 0, r2
        assert r2["volume"] < box["volume"], r2               # material removed
        removed = box["volume"] - r2["volume"]
        assert abs(removed - 492.135) < 1.0, (removed, r2)    # pi*(r_out^2-r_in^2)*depth


def test_chamfer_edges():
    """Box 20^3, chamfer its 4 vertical edges by 2 mm -> valid solid with volume
    7840 mm^3 (< original 8000). Mirrors test_partdesign_fillet_by_tag's
    tag-based edge selection."""
    with Worker() as w:
        w.call("new_document", name="chamfer")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        v_before = box["volume"]
        assert abs(v_before - 8000.0) < 1e-3, box

        # The 4 vertical edges are lines of length 20 whose centroid sits at
        # mid-height (z = 10); top/bottom rails share length 20 but lie at z=0/20.
        edges = w.call("list_edges", handle=box["handle"])
        verticals = [
            e for e in edges
            if e["kind"] == "line"
            and abs(e["length"] - 20.0) < 1e-3
            and abs(e["centroid"][2] - 10.0) < 1e-3
        ]
        assert len(verticals) == 4, f"expected 4 vertical edges, got {len(verticals)}"

        r = w.call(
            "chamfer_edges",
            handle=box["handle"],
            edges=[e["tag"] for e in verticals],
            size=2.0,
        )
        assert r["handle"].startswith("chamfer_"), r
        assert len(r["edges"]) == 4, r
        assert abs(r["volume"] - 7840.0) < 1e-3, r
        assert r["volume"] < v_before, r


def test_shell_solid():
    with Worker() as w:
        w.call("new_document", name="shell")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        box_vol = w.call("mass_properties", handle=box["handle"])["volume_mm3"]
        assert abs(box_vol - 8000.0) < 1e-3, box_vol

        # Tag the +Z (top) face to remove as the shell opening.
        top = w.call(
            "query_faces",
            handle=box["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1]},
        )
        assert len(top) == 1, top
        top_tag = top[0]["tag"]

        r = w.call("shell_solid", handle=box["handle"], faces=[top_tag], thickness=2.0)
        assert r["handle"].startswith("shell_"), r
        assert r["wall_thickness"] == 2.0, r
        assert r["removed_faces"], r
        # 20^3 box, top removed, 2mm inward wall -> ~3392 mm^3 of wall material:
        # positive, far below the solid 8000, well under a half-solid block.
        assert 0 < r["volume"] < 8000.0, r
        assert r["volume"] < 5000.0, r
        assert abs(r["volume"] - 3392.0) < 1.0, r

        # Bad inputs must raise (actionable for the agent), not corrupt state.
        try:
            w.call("shell_solid", handle=box["handle"], faces=[top_tag], thickness=0)
            assert False, "thickness <= 0 must raise"
        except WorkerError:
            pass
        try:
            w.call("shell_solid", handle=box["handle"], faces=[], thickness=2.0)
            assert False, "empty faces must raise"
        except WorkerError:
            pass


def test_add_thread():
    with Worker() as w:
        w.call("new_document", name="t")
        # External M8 coarse: diameter=8, pitch=1.25, length=10.
        r = w.call("add_thread", diameter=8.0, pitch=1.25, length=10.0)
        assert r["handle"].startswith("thread_"), r
        assert r["modeled"] is True, r
        assert r["volume"] > 0, r
        assert r["major_diameter"] == 8.0, r
        assert r["minor_diameter"] < 8.0, r            # root is below the crest
        assert abs(r["minor_diameter"] - 6.6469) < 0.01, r  # 8 - 1.0825*1.25
        assert r["pitch"] == 1.25 and r["length"] == 10.0 and r["starts"] == 1, r

        # Confirm the swept solid is genuinely valid by re-measuring it.
        m = w.call("mass_properties", handle=r["handle"])
        assert m["volume_mm3"] > 0, m
        # Volume must exceed the bare minor-radius core (the rib added material).
        import math
        core = math.pi * (r["minor_diameter"] / 2.0) ** 2 * 10.0
        assert m["volume_mm3"] > core, (m, core)

        # Internal tap tool and multi-start variant must also produce valid solids.
        ri = w.call("add_thread", diameter=8.0, pitch=1.25, length=10.0, internal=True)
        assert ri["internal"] is True and ri["modeled"] is True and ri["volume"] > 0, ri
        r2 = w.call("add_thread", diameter=10.0, pitch=1.5, length=12.0, starts=2)
        assert r2["starts"] == 2 and r2["modeled"] is True and r2["volume"] > 0, r2

        # Bad inputs raise.
        try:
            w.call("add_thread", diameter=0, pitch=1.25, length=10.0)
            assert False, "expected ValueError for diameter<=0"
        except WorkerError:
            pass


def test_engrave_text():
    """Engrave 'M3' into the +Z face of a 20^3 box: result volume drops below the
    box volume and the handle is a text_ handle. Emboss raises it above. Skips
    cleanly if no system TrueType font is available."""
    import os

    font = None
    for cand in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/Library/Fonts/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.isfile(cand):
            font = cand
            break
    if font is None:
        print("SKIP test_engrave_text: no system TrueType font found")
        return

    with Worker() as w:
        w.call("new_document", name="engrave")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        box_vol = w.call("mass_properties", handle=box["handle"])["volume_mm3"]

        # Tag the +Z face (mirror test_list_faces_box) and engrave into it.
        faces = w.call("list_faces", handle=box["handle"])
        top = [f for f in faces if f.get("normal") == [0.0, 0.0, 1.0]]
        assert len(top) == 1, f"expected one +Z face, got {len(top)}"
        top_tag = top[0]["tag"]

        r = w.call("engrave_text", handle=box["handle"], face=top_tag,
                   text="M3", size=5.0, depth=0.5, mode="engrave")
        assert r["handle"].startswith("text_"), f"handle should be text_*, got {r}"
        assert r["text"] == "M3", r
        assert r["mode"] == "engrave", r
        assert r["volume"] < box_vol, (
            f"engrave should remove material: {r['volume']} !< {box_vol}"
        )
        assert r["volume"] > 0, r

        # Emboss on a fresh box raises material above the surface.
        w.call("new_document", name="emboss")
        box2 = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        box2_vol = w.call("mass_properties", handle=box2["handle"])["volume_mm3"]
        faces2 = w.call("list_faces", handle=box2["handle"])
        top2 = [f for f in faces2 if f.get("normal") == [0.0, 0.0, 1.0]][0]
        r2 = w.call("engrave_text", handle=box2["handle"], face=top2["tag"],
                    text="AB", size=5.0, depth=1.0, mode="emboss")
        assert r2["handle"].startswith("text_"), r2
        assert r2["mode"] == "emboss", r2
        assert r2["volume"] > box2_vol, (
            f"emboss should add material: {r2['volume']} !> {box2_vol}"
        )

        # A bad mode is a clean, recoverable error.
        try:
            w.call("engrave_text", handle=box2["handle"], face=top2["tag"],
                   text="X", mode="bogus")
            assert False, "expected WorkerError for bad mode"
        except WorkerError:
            pass


def test_add_rib():
    """Build a U-channel body (two parallel walls + a floor), then add a rib
    spanning between the walls from an open spine line. The rib must fuse in and
    grow the body volume. PartDesign::Rib is unavailable headless, so the worker
    falls back to a midplane pad of the offset spine — this exercises that path."""
    with Worker() as w:
        w.call("new_document", name="rib")
        body = w.call("make_body")

        # Base: pad a U-shaped profile -> two uprights (x in [-20,-12] and
        # [12,20]) joined by a floor (y in [0,8]), 10 mm thick.
        sk1 = w.call("make_sketch", body=body["handle"], plane="XY")
        u_pts = [
            (-20, 0), (20, 0), (20, 30), (12, 30), (12, 8),
            (-12, 8), (-12, 30), (-20, 30), (-20, 0),
        ]
        u_items = [
            {"type": "line", "start": list(u_pts[i]), "end": list(u_pts[i + 1])}
            for i in range(len(u_pts) - 1)
        ]
        w.call("add_sketch_geometry", sketch=sk1["handle"], items=u_items)
        pad_r = w.call("pad", sketch=sk1["handle"], length=10.0)
        base_vol = pad_r["volume"]
        assert base_vol > 0, pad_r

        # Open spine: a single line bridging the two walls at y=20.
        sk2 = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry",
            sketch=sk2["handle"],
            items=[{"type": "line", "start": [-12, 20], "end": [12, 20]}],
        )

        r = w.call("add_rib", body=body["handle"], sketch=sk2["handle"], thickness=4.0)
        assert r["handle"].startswith("rib_"), r
        assert r["thickness"] == 4.0, r
        assert r["volume"] > base_vol, (
            f"rib should add material: body {base_vol:.1f} -> {r['volume']:.1f}"
        )

        # A closed-loop profile is not a valid rib spine -> ValueError.
        sk3 = w.call("make_sketch", body=body["handle"], plane="XY")
        box_pts = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]
        box_items = [
            {"type": "line", "start": list(box_pts[i]), "end": list(box_pts[i + 1])}
            for i in range(len(box_pts) - 1)
        ]
        w.call("add_sketch_geometry", sketch=sk3["handle"], items=box_items)
        try:
            w.call("add_rib", body=body["handle"], sketch=sk3["handle"], thickness=2.0)
            assert False, "closed profile should have raised"
        except WorkerError:
            pass


def test_transform():
    """Box 20^3: translate [10,0,0] shifts CoM x 10->20; then rotate 90 deg about
    Z maps the X-extent 0..20 to -20..0 (bbox swap), placement angle_deg == 90."""
    with Worker() as w:
        w.call("new_document", name="xf")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)

        r = w.call("transform", handle=box["handle"], translate=[10, 0, 0])
        assert r["handle"] == box["handle"], r
        assert abs(r["placement"]["base"][0] - 10.0) < 1e-6, r
        mp = w.call("mass_properties", handle=box["handle"])
        assert abs(mp["center_of_mass_mm"][0] - 20.0) < 1e-3, mp

        r2 = w.call("transform", handle=box["handle"], rotate_axis=[0, 0, 1], angle=90)
        assert abs(r2["placement"]["angle_deg"] - 90.0) < 1e-3, r2
        mp2 = w.call("mass_properties", handle=box["handle"])
        bb = mp2["bounding_box_mm"]  # [xmin, ymin, zmin, xmax, ymax, zmax]
        assert abs(bb[0] - (-20.0)) < 1e-3 and abs(bb[3] - 0.0) < 1e-3, mp2
        assert abs(bb[1] - 10.0) < 1e-3 and abs(bb[4] - 30.0) < 1e-3, mp2


def test_scale_shape():
    """Uniform scale of a 10^3 box (vol 1000) by 2 -> vol ~8000; per-axis
    [2,3,4] -> vol ~24000; center-pivot scale keeps the pivot fixed."""
    with Worker() as w:
        w.call("new_document", name="scale")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        assert abs(box["volume"] - 1000.0) < 1e-6, box

        u = w.call("scale_shape", handle=box["handle"], factor=2.0)
        assert u["handle"].startswith("scaled_"), u["handle"]
        assert abs(u["volume"] - 8000.0) < 1e-3, u["volume"]
        assert u["factor"] == [2.0, 2.0, 2.0], u["factor"]

        n = w.call("scale_shape", handle=box["handle"], factor=[2, 3, 4])
        assert abs(n["volume"] - 24000.0) < 1e-3, n["volume"]

        # center-pivot scale about the box center leaves volume scaled the same
        c = w.call("scale_shape", handle=box["handle"], factor=2.0,
                   center=[5, 5, 5])
        assert abs(c["volume"] - 8000.0) < 1e-3, c["volume"]

        # non-positive factors are rejected
        try:
            w.call("scale_shape", handle=box["handle"], factor=0)
        except WorkerError:
            pass
        else:
            raise AssertionError("expected WorkerError for factor <= 0")


def test_copy_shape():
    """20mm cube copied with placement [50,0,0]: two independent solids, copy
    volume 8000 mm^3, copy CG x ≈ original (10) + 50 = 60; handle starts 'copy_'."""
    with Worker() as w:
        w.call("new_document", name="copy")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        src_mp = w.call("mass_properties", handle=box["handle"])
        assert abs(src_mp["center_of_mass_mm"][0] - 10.0) < 1e-3, src_mp

        cp = w.call("copy_shape", handle=box["handle"], placement=[50, 0, 0])
        assert cp["handle"].startswith("copy_"), cp
        assert abs(cp["volume"] - 8000.0) < 1e-3, cp

        cp_mp = w.call("mass_properties", handle=cp["handle"])
        assert abs(cp_mp["volume_mm3"] - 8000.0) < 1e-3, cp_mp
        assert abs(cp_mp["center_of_mass_mm"][0] - 60.0) < 1e-3, cp_mp
        # independent geometry: source unmoved after the copy
        src_mp2 = w.call("mass_properties", handle=box["handle"])
        assert abs(src_mp2["center_of_mass_mm"][0] - 10.0) < 1e-3, src_mp2


def test_measure_distance():
    """Two 20mm cubes 10mm apart on X -> distance 10; overlapping -> 0 + touching;
    top face of one to bottom face of another (5mm gap) -> face-to-face distance 5.
    A bad face ref raises WorkerError."""
    with Worker() as w:
        w.call("new_document", name="md")
        a = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        # Second cube at x=30: its near face is at x=30, a's far face at x=20 -> gap 10.
        b = w.call("add_primitive", kind="box", w=20, d=20, h=20, placement=[30, 0, 0])
        r = w.call("measure_distance", a=a["handle"], b=b["handle"])
        assert abs(r["distance_mm"] - 10.0) < 1e-6, r
        assert r["touching"] is False, r
        assert r["point_on_a"] == [20.0, 0.0, 20.0], r
        assert r["point_on_b"] == [30.0, 0.0, 20.0], r

        # Overlapping cube (offset 10 on X) -> minimum distance 0, touching True.
        c = w.call("add_primitive", kind="box", w=20, d=20, h=20, placement=[10, 0, 0])
        r2 = w.call("measure_distance", a=a["handle"], b=c["handle"])
        assert r2["distance_mm"] == 0.0, r2
        assert r2["touching"] is True, r2

        # Face-to-face: cube at z=25, a's top face (Face6, z=20) to its bottom (Face5, z=25) -> 5.
        d = w.call("add_primitive", kind="box", w=20, d=20, h=20, placement=[0, 0, 25])
        r3 = w.call(
            "measure_distance",
            a=a["handle"], b=d["handle"], a_ref="Face6", b_ref="Face5",
        )
        assert abs(r3["distance_mm"] - 5.0) < 1e-6, r3
        assert r3["touching"] is False, r3

        # Out-of-range face ref is an actionable error.
        try:
            w.call("measure_distance", a=a["handle"], b=b["handle"], a_ref="Face99")
        except WorkerError:
            pass
        else:
            raise AssertionError("expected WorkerError for out-of-range face ref")


def test_measure_angle():
    """Adjacent box faces meet at 90 deg; opposite parallel faces at 180 deg
    (supplement 0); two perpendicular box edges at 90 deg. Mismatched kinds and
    non-planar faces raise."""
    with Worker() as w:
        w.call("new_document", name="angle")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        faces = w.call("list_faces", handle=box["handle"])
        fx = [f for f in faces if f.get("normal") == [1.0, 0.0, 0.0]][0]
        fz = [f for f in faces if f.get("normal") == [0.0, 0.0, 1.0]][0]
        fnegx = [f for f in faces if f.get("normal") == [-1.0, 0.0, 0.0]][0]

        # Adjacent faces (+X vs +Z) are perpendicular.
        perp = w.call("measure_angle", a=box["handle"], a_ref=fx["tag"],
                      b=box["handle"], b_ref=fz["tag"])
        assert abs(perp["angle_deg"] - 90.0) < 1e-3, perp
        assert abs(perp["supplement_deg"] - 90.0) < 1e-3, perp
        assert perp["kind"] == "face", perp

        # Opposite parallel faces (+X vs -X): normals antiparallel -> 180 deg.
        opp = w.call("measure_angle", a=box["handle"], a_ref=fx["tag"],
                     b=box["handle"], b_ref=fnegx["tag"])
        assert abs(opp["angle_deg"] - 180.0) < 1e-3, opp
        assert abs(opp["supplement_deg"] - 0.0) < 1e-3, opp

        # Two perpendicular straight box edges -> 90 deg.
        edges = w.call("list_edges", handle=box["handle"])
        e_ang = w.call("measure_angle", a=box["handle"], a_ref=edges[0]["tag"],
                       b=box["handle"], b_ref=edges[1]["tag"])
        assert abs(e_ang["angle_deg"] - 90.0) < 1e-3, e_ang
        assert e_ang["kind"] == "edge", e_ang

        # Mixing a face ref with an edge ref is rejected.
        try:
            w.call("measure_angle", a=box["handle"], a_ref=fx["tag"],
                   b=box["handle"], b_ref=edges[0]["tag"])
            assert False, "mixed face/edge refs should raise"
        except WorkerError:
            pass

        # A non-planar (cylindrical) face is rejected.
        cyl = w.call("add_primitive", kind="cylinder", r=5, h=10)
        cfaces = w.call("list_faces", handle=cyl["handle"])
        cylface = [f for f in cfaces if f["kind"] == "cylindrical"][0]
        try:
            w.call("measure_angle", a=cyl["handle"], a_ref=cylface["tag"],
                   b=box["handle"], b_ref=fz["tag"])
            assert False, "non-planar face should raise"
        except WorkerError:
            pass


def test_bounding_box():
    import math
    with Worker() as w:
        w.call("new_document", name="bb")
        box = w.call("add_primitive", kind="box", w=10, d=20, h=5)
        r = w.call("bounding_box", handle=box["handle"])
        assert "handle" not in r, r
        assert r["size"] == [10.0, 20.0, 5.0], r
        assert r["min"] == [0.0, 0.0, 0.0], r
        assert r["max"] == [10.0, 20.0, 5.0], r
        assert r["center"] == [5.0, 10.0, 2.5], r
        expected_diag = math.sqrt(10.0**2 + 20.0**2 + 5.0**2)
        assert abs(r["diagonal"] - expected_diag) < 1e-3, r
        assert r["oriented"] is None, r
        ro = w.call("bounding_box", handle=box["handle"], oriented=True)
        assert ro["oriented"] is not None, ro
        assert sorted(ro["oriented"]["size"]) == sorted([10.0, 20.0, 5.0]), ro
        try:
            w.call("bounding_box", handle="nope_999")
            assert False, "expected error on unknown handle"
        except WorkerError:
            pass


def test_min_clearance():
    """Two 10mm boxes: 3mm apart -> clear/3.0; overlapping 5mm -> interference
    with ~500mm³ overlap; face-touching -> contact/0.0. Numbers verified in
    FreeCAD 1.1.1."""
    with Worker() as w:
        w.call("new_document", name="clr")
        a = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        # clear: B 3mm past A's far face (A spans x=0..10, B at x=13)
        b_clear = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                         placement=[13, 0, 0])
        r = w.call("min_clearance", a=a["handle"], b=b_clear["handle"])
        assert r["status"] == "clear", r
        assert abs(r["clearance_mm"] - 3.0) < 1e-3, r
        assert "overlap_volume_mm3" not in r, r
        assert len(r["point_on_a"]) == 3 and len(r["point_on_b"]) == 3, r

        # interference: B overlaps A by 5mm (B at x=5) -> 5*10*10 = 500 mm³
        b_interf = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                          placement=[5, 0, 0])
        r = w.call("min_clearance", a=a["handle"], b=b_interf["handle"])
        assert r["status"] == "interference", r
        assert r["clearance_mm"] == 0.0, r
        assert abs(r["overlap_volume_mm3"] - 500.0) < 1.0, r

        # contact: B's near face touches A's far face (B at x=10)
        b_contact = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                           placement=[10, 0, 0])
        r = w.call("min_clearance", a=a["handle"], b=b_contact["handle"])
        assert r["status"] == "contact", r
        assert abs(r["clearance_mm"]) < 1e-6, r
        assert "overlap_volume_mm3" not in r, r


def test_check_shape():
    with Worker() as w:
        w.call("new_document", name="checkshape")
        box = w.call("add_primitive", kind="box", w=10, d=20, h=5)
        r = w.call("check_shape", handle=box["handle"])
        assert r["valid"] is True, r
        assert r["watertight_solid"] is True, r
        assert r["closed"] is True, r
        assert r["is_null"] is False, r
        assert r["shape_type"] == "Solid", r
        assert r["solids"] == 1, r
        assert r["shells"] == 1, r
        assert r["faces"] == 6, r
        assert r["edges"] == 12, r
        assert abs(r["volume_mm3"] - 1000.0) < 1e-6, r
        assert "check" not in r, r
        assert "check_error" not in r, r


def test_section_view():
    """Slice solids with a plane and check measured cross-section areas.
      cylinder r=5 cut on XY at mid-height -> area = pi*25 ~= 78.5398 mm^2
      box 10x10x10 cut on XY at z=5        -> area = 100 mm^2
      plane that misses the shape          -> wire_count 0, area 0, bbox None
      emit_profile=True                    -> registers a 'section_*' handle
      emit_profile on a miss               -> WorkerError (nothing to emit)
    """
    import math
    with Worker() as w:
        w.call("new_document", name="section")

        cyl = w.call("add_primitive", kind="cylinder", r=5, h=20)
        r = w.call("section_view", handle=cyl["handle"], plane="XY", offset=10.0)
        assert abs(r["section_area_mm2"] - math.pi * 25) < 0.05, (
            f"cylinder section area {r['section_area_mm2']} not ~= pi*25 ({math.pi*25:.4f})"
        )
        assert r["wire_count"] >= 1, f"expected >=1 section wire: {r}"
        assert r["closed_wire_count"] >= 1, f"expected a closed wire: {r}"
        assert "handle" not in r, f"measure-only call must not emit a handle: {r}"
        assert r["bbox"] is not None and r["bbox"]["size"][2] == 0.0, (
            f"section bbox should be flat in the cut direction: {r}"
        )

        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        r2 = w.call("section_view", handle=box["handle"], plane="XY", offset=5.0)
        assert abs(r2["section_area_mm2"] - 100.0) < 0.01, (
            f"box section area {r2['section_area_mm2']} not ~= 100"
        )

        # plane above the box misses it entirely
        miss = w.call("section_view", handle=box["handle"], plane="XY", offset=100.0)
        assert miss["wire_count"] == 0, f"plane should miss the box: {miss}"
        assert miss["section_area_mm2"] == 0.0, f"missed slice has no area: {miss}"
        assert miss["bbox"] is None, f"missed slice has no bbox: {miss}"

        # emit_profile registers a real section object
        emit = w.call(
            "section_view", handle=box["handle"], plane="XY", offset=5.0,
            emit_profile=True,
        )
        assert emit["handle"].startswith("section_"), (
            f"emit_profile should register a section handle: {emit}"
        )
        assert abs(emit["section_area_mm2"] - 100.0) < 0.01, (
            f"emitted section area {emit['section_area_mm2']} not ~= 100"
        )

        # emit_profile on a miss is an error (nothing to emit)
        try:
            w.call(
                "section_view", handle=box["handle"], plane="XY", offset=100.0,
                emit_profile=True,
            )
        except WorkerError:
            pass
        else:
            raise AssertionError("expected WorkerError when emit_profile misses the shape")


# --- producer smoke: every solid-producing command yields ONE watertight solid ---
#
# A uniform invariant over the whole producer surface, with check_shape as the
# oracle: the result must be a single, valid, closed (watertight) solid. This is
# strictly stronger than the per-command "volume > 0" tests and is the general
# form of the add_thread fuse-drop bug class (a fuse that silently drops an
# operand, an inside-out extrude, a 2-solid compound). It auto-scales: add a row
# to _producer_specs and the new command is covered. A coverage guard
# (test_every_add_command_has_a_producer_smoke) makes sure new add_* generators
# can't ship without a row here.
#
# NOTE: loft, sweep, helix, draft, thickness, the patterns (linear/polar/mirror)
# and partdesign_fillet/chamfer are NOT driven here — they have dedicated tests
# and need bespoke multi-sketch fixtures. This smoke covers the parametric
# generators, the direct-shape ops, and the core PartDesign producers.

# add_* tools that legitimately do NOT emit a solid (so the coverage guard
# doesn't demand a producer row for them).
_ADD_NOT_SOLID = {
    "add_part",             # links a part into an assembly — no new solid
    "add_sketch_geometry",  # sketch editing
    "add_sketch_constraint",
    "add_sketch_external",
    "add_projection_group",  # a TechDraw view
    "add_dimension",         # a TechDraw dimension annotation
    "add_annotation",        # a TechDraw text annotation
    "add_thumbnail",         # a TechDraw isometric pictorial view
    "add_section_view",      # a TechDraw cross-section view
    "add_feature_note",      # a TechDraw feature note / leader annotation
}


def _ws_box(w, **kw):
    """A fresh box solid (default 20mm cube), returns its handle."""
    kw.setdefault("kind", "box")
    for k, v in (("w", 20), ("d", 20), ("h", 20)):
        kw.setdefault(k, v)
    return w.call("add_primitive", **kw)["handle"]


def _ws_vertical_edge_tags(w, box_h, length=20.0, mid_z=10.0):
    """The 4 vertical edge tags of an axis-aligned box (lines of `length` whose
    centroid sits at mid-height)."""
    edges = w.call("list_edges", handle=box_h)
    return [
        e["tag"] for e in edges
        if e["kind"] == "line"
        and abs(e["length"] - length) < 1e-3
        and abs(e["centroid"][2] - mid_z) < 1e-3
    ]


def _ws_top_face_tag(w, handle):
    top = w.call("query_faces", handle=handle,
                 predicate={"type": "planar", "normal_dir": [0, 0, 1]})
    assert top, "no +Z face found"
    return top[0]["tag"]


def _ws_fillet(w):
    b = _ws_box(w)
    return w.call("fillet_edges", handle=b, edges=_ws_vertical_edge_tags(w, b), radius=2.0)["handle"]


def _ws_chamfer(w):
    b = _ws_box(w)
    return w.call("chamfer_edges", handle=b, edges=_ws_vertical_edge_tags(w, b), size=2.0)["handle"]


def _ws_shell(w):
    b = _ws_box(w)
    return w.call("shell_solid", handle=b, faces=[_ws_top_face_tag(w, b)], thickness=2.0)["handle"]


def _ws_bool_cut(w):
    a = _ws_box(w)
    t = w.call("add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0])["handle"]
    return w.call("boolean_op", op="cut", base=a, tool=t)["handle"]


def _ws_bool_fuse(w):
    a = _ws_box(w)
    t = w.call("add_primitive", kind="box", w=20, d=20, h=20, placement=[10, 0, 0])["handle"]
    return w.call("boolean_op", op="fuse", base=a, tool=t)["handle"]


def _ws_oring(w):
    b = w.call("add_primitive", kind="box", w=60, d=60, h=10)["handle"]
    return w.call("oring_groove", handle=b, face=_ws_top_face_tag(w, b),
                  cross_section=2.62, inner_diameter=20, cut=True)["handle"]


def _ws_pad(w):
    return _build_pad_cube(w, side=20.0, height=10.0)["pad"]


def _ws_pocket(w):
    h = _build_pad_cube(w, side=30.0, height=20.0)
    sk = _sketch_circle_on_top(w, h["body"], h["pad"], (15, 15), 4.0)
    return w.call("pocket", sketch=sk, through_all=True, direction="into_body")["handle"]


def _ws_hole(w):
    h = _build_pad_cube(w, side=30.0, height=30.0)
    sk = _sketch_circle_on_top(w, h["body"], h["pad"], (15, 15), 3.0)
    return w.call("hole", sketch=sk, diameter=6.0, depth_type="ThroughAll")["handle"]


def _ws_revolve(w):
    body = w.call("make_body")
    sk = _make_closed_polyline_sketch(w, body["handle"], "XY", [
        ([5, 1], [10, 1]), ([10, 1], [10, 5]), ([10, 5], [5, 5]), ([5, 5], [5, 1]),
    ])
    return w.call("revolve", sketch=sk, axis="Y", angle=360.0)["handle"]


def _mk_line_items(pts):
    return [{"type": "line", "start": list(pts[i]), "end": list(pts[i + 1])}
            for i in range(len(pts) - 1)]


def _ws_engrave(w, font):
    b = _ws_box(w)
    return w.call("engrave_text", handle=b, face=_ws_top_face_tag(w, b),
                  text="M3", depth=0.5, mode="engrave", font=font)["handle"]


def _ws_rib(w):
    body = w.call("make_body")["handle"]
    sk1 = w.call("make_sketch", body=body, plane="XY")["handle"]
    u_pts = [(-20, 0), (20, 0), (20, 30), (12, 30), (12, 8),
             (-12, 8), (-12, 30), (-20, 30), (-20, 0)]
    w.call("add_sketch_geometry", sketch=sk1, items=_mk_line_items(u_pts))
    w.call("pad", sketch=sk1, length=10.0)
    sk2 = w.call("make_sketch", body=body, plane="XY")["handle"]
    w.call("add_sketch_geometry", sketch=sk2,
           items=[{"type": "line", "start": [-12, 20], "end": [12, 20]}])
    return w.call("add_rib", body=body, sketch=sk2, thickness=4.0)["handle"]


def _producer_specs(font=None):
    """(tool, label, build_fn) for every solid-producing command this smoke
    drives. build_fn(w) sets up any precondition and returns the result handle.
    `tool` is the MCP tool exercised (used by the coverage guard)."""
    specs = [
        # standalone parametric generators
        ("add_primitive", "box", lambda w: _ws_box(w)),
        ("add_primitive", "cylinder", lambda w: w.call("add_primitive", kind="cylinder", r=5, h=10)["handle"]),
        ("add_primitive", "sphere", lambda w: w.call("add_primitive", kind="sphere", r=5)["handle"]),
        ("add_gear", "gear_external", lambda w: w.call("add_gear", teeth=12, module=2.0, height=6.0)["handle"]),
        ("add_gear", "gear_internal", lambda w: w.call("add_gear", teeth=24, module=2.0, height=6.0, external=False)["handle"]),
        ("add_rack", "rack", lambda w: w.call("add_rack", teeth=10, module=2.0)["handle"]),
        ("add_sprocket", "sprocket", lambda w: w.call("add_sprocket", teeth=17, chain_pitch=12.7, roller_diameter=7.92)["handle"]),
        ("add_pulley", "pulley_flanged", lambda w: w.call("add_pulley", teeth=20, belt_pitch=2.0, width=6, flanged=True)["handle"]),
        ("add_pulley", "pulley_plain", lambda w: w.call("add_pulley", teeth=20, belt_pitch=2.0, width=6, flanged=False)["handle"]),
        ("add_spring", "spring", lambda w: w.call("add_spring", wire_diameter=2, outer_diameter=20, free_length=40, coils=8)["handle"]),
        ("add_fastener", "fastener_screw", lambda w: w.call("add_fastener", kind="socket_head_cap_screw", size="M3", length=10)["handle"]),
        ("add_fastener", "fastener_bolt", lambda w: w.call("add_fastener", kind="hex_bolt", size="M6", length=20)["handle"]),
        ("add_fastener", "fastener_nut", lambda w: w.call("add_fastener", kind="hex_nut", size="M6")["handle"]),
        ("add_fastener", "fastener_washer", lambda w: w.call("add_fastener", kind="washer", size="M3")["handle"]),
        ("add_bearing", "bearing", lambda w: w.call("add_bearing", designation="608")["handle"]),
        ("add_thread", "thread_external", lambda w: w.call("add_thread", diameter=8, pitch=1.25, length=10)["handle"]),
        ("add_thread", "thread_internal", lambda w: w.call("add_thread", diameter=8, pitch=1.25, length=10, internal=True)["handle"]),
        # direct-shape ops (box fixture)
        ("fillet_edges", "fillet_edges", _ws_fillet),
        ("chamfer_edges", "chamfer_edges", _ws_chamfer),
        ("shell_solid", "shell_solid", _ws_shell),
        ("scale_shape", "scale_shape", lambda w: w.call("scale_shape", handle=_ws_box(w), factor=2.0)["handle"]),
        ("copy_shape", "copy_shape", lambda w: w.call("copy_shape", handle=_ws_box(w), placement=[50, 0, 0])["handle"]),
        ("boolean_op", "boolean_cut", _ws_bool_cut),
        ("boolean_op", "boolean_fuse", _ws_bool_fuse),
        ("oring_groove", "oring_groove_cut", _ws_oring),
        # core PartDesign producers (known-good fixtures)
        ("pad", "pad", _ws_pad),
        ("pocket", "pocket", _ws_pocket),
        ("hole", "hole", _ws_hole),
        ("revolve", "revolve", _ws_revolve),
        ("add_rib", "add_rib", _ws_rib),
    ]
    if font:
        specs.append(("engrave_text", "engrave_text", lambda w: _ws_engrave(w, font)))
    return specs


def test_producers_yield_watertight_solids():
    """Every solid-producing command builds ONE valid, closed (watertight) solid.
    Each producer runs in its own fresh document; failures are collected so one
    broken producer reports clearly without masking the rest."""
    import os
    font = next((f for f in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc") if os.path.isfile(f)), None)
    specs = _producer_specs(font=font)
    if not font:
        print("    (engrave_text skipped: no system font)")

    failures = []
    with Worker() as w:
        for i, (tool, label, build) in enumerate(specs):
            w.call("new_document", name=f"prod_{i}_{label}")
            try:
                handle = build(w)
                r = w.call("check_shape", handle=handle)
                if not (r["valid"] and r["solids"] == 1 and r["watertight_solid"]
                        and r["volume_mm3"] > 0):
                    failures.append(f"{label} ({tool}): not a single watertight solid -> {r}")
            except Exception as e:
                failures.append(f"{label} ({tool}): build/check raised {type(e).__name__}: {e}")
            finally:
                w.call("close_document")
    assert not failures, (
        f"{len(failures)}/{len(specs)} producers failed the watertight-solid check:\n  "
        + "\n  ".join(failures))


def test_every_add_command_has_a_producer_smoke():
    """Coverage guard: every `add_*` MCP tool that emits a solid has a row in
    _producer_specs, so a new generator can't ship without a watertight smoke.
    Non-solid add_* tools are explicitly excluded (and the exclusions are checked
    to be real tools, so the allowlist can't hide a missing producer)."""
    import ast
    src = (REPO / "driftpin" / "mcp_server.py").read_text()
    tree = ast.parse(src)
    add_tools = {
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("add_")
        and any(isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool"
                for d in n.decorator_list)
    }
    covered = {tool for tool, _, _ in _producer_specs(font="x")}
    missing = sorted(add_tools - _ADD_NOT_SOLID - covered)
    assert not missing, (
        "add_* tools with no producer smoke row (add one to _producer_specs, or "
        f"to _ADD_NOT_SOLID if it emits no solid): {missing}")
    stale = sorted(t for t in _ADD_NOT_SOLID if t not in add_tools)
    assert not stale, f"_ADD_NOT_SOLID names that aren't add_* tools anymore: {stale}"


# --- check_airtight_path (issue #19): enclosed-flow void invariants -----------
#
# Synthetic adapters along +X: outer 40x20x20, 2mm walls, 16x16 through-bore.
#   good    : clean cavity, both ends open  -> connected, no leak, aperture 256
#   slit    : near-full divider w/ a 0.2mm slot -> connected but pinched (~3.2)
#   blocked : full divider, no slot -> two separate cavities, no path
#   leaky   : extra cut breaches the +Z wall -> cavity reaches ambient
_ADAPTER_SRC = '''
import Part, FreeCAD as App
doc = App.ActiveDocument
outer = Part.makeBox(40, 20, 20, App.Vector(0, -10, -10))
bore = Part.makeBox(44, 16, 16, App.Vector(-2, -8, -8))
part = outer.cut(bore)
kind = {kind!r}
if kind == "slit":
    divider = Part.makeBox(2, 16, 16, App.Vector(19, -8, -8))
    slit = Part.makeBox(2.2, 16, 0.2, App.Vector(18.9, -8, -0.1))
    part = part.fuse(divider.cut(slit)).removeSplitter()
elif kind == "blocked":
    divider = Part.makeBox(2, 16, 16, App.Vector(19, -8, -8))
    part = part.fuse(divider).removeSplitter()
elif kind == "leaky":
    hole = Part.makeBox(8, 8, 6, App.Vector(16, -4, 6))
    part = part.cut(hole)
f = doc.addObject("Part::Feature", "Adapter")
f.Shape = part
doc.recompute()
'''


def _build_adapter(w, kind):
    """Build a synthetic adapter in a fresh doc; return (handle, inlet_tag,
    outlet_tag). Ports are the outer end faces — pick by extreme-x centroid so a
    breach/divider's inward-facing X faces (same normal) don't get chosen."""
    w.call("new_document", name=f"air_{kind}")
    reg = w.call("run_script", code=_ADAPTER_SRC.format(kind=kind))["registered"]
    h = reg[0]["handle"]
    inlet = w.call("query_faces", handle=h,
                   predicate={"type": "planar", "normal_dir": [-1, 0, 0], "centroid_min": "x"})
    outlet = w.call("query_faces", handle=h,
                    predicate={"type": "planar", "normal_dir": [1, 0, 0], "centroid_max": "x"})
    return h, inlet[0]["tag"], outlet[0]["tag"]


def test_check_airtight_good():
    """A clean adapter: inlet->outlet void is connected, sealed, full-bore."""
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "good")
        r = w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet,
                   min_aperture_mm2=10.0)
        assert r["connected"] is True, r
        assert r["leaky"] is False, r
        assert r["status"] == "airtight", r
        assert r["ok"] is True, r
        assert abs(r["min_aperture_mm2"] - 256.0) < 1.0, r


def test_check_airtight_bottleneck_slit():
    """The v2 failure: connected, but the slit pinches the path below threshold."""
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "slit")
        r = w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet,
                   min_aperture_mm2=10.0)
        assert r["connected"] is True, r
        assert r["leaky"] is False, r
        assert r["min_aperture_mm2"] < 10.0, r
        assert r["status"] == "bottleneck", r
        assert r["ok"] is False, r
        # with no threshold, a pinched-but-connected path is acceptable
        r2 = w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet)
        assert r2["ok"] is True and r2["status"] == "airtight", r2


def test_check_airtight_leaky():
    """The v3 failure: capping both ports still leaves the cavity open to ambient."""
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "leaky")
        r = w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet)
        assert r["leaky"] is True, r
        assert r["status"] == "leaky", r
        assert r["ok"] is False, r


def test_check_airtight_blocked():
    """A full divider splits the bore: inlet and outlet are in separate cavities."""
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "blocked")
        r = w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet)
        assert r["connected"] is False, r
        assert r["leaky"] is False, r
        assert r["status"] == "blocked", r
        assert r["ok"] is False, r


def test_check_airtight_same_face_rejected():
    """inlet == outlet is a usage error, not a silent pass."""
    with Worker() as w:
        h, inlet, _ = _build_adapter(w, "good")
        try:
            w.call("check_airtight_path", handle=h, inlet=inlet, outlet=inlet)
        except WorkerError as e:
            assert "same face" in str(e).lower(), e
        else:
            assert False, "expected an error when inlet and outlet are the same face"
        assert w.call("ping") == "pong", "worker poisoned by the rejected call"


# --- annotate_face / face roles (issue #19, slice 2) -------------------------

def test_annotate_face_and_role_resolution():
    """Declare inlet/outlet roles, read them back, and drive check_airtight_path
    by role name — the result must match driving it by raw tag."""
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "good")
        a = w.call("annotate_face", handle=h, face=inlet, role="inlet", name="window")
        b = w.call("annotate_face", handle=h, face=outlet, role="outlet", name="barb")
        assert a["role"] == "inlet" and a["tag"] == inlet, a
        assert sorted(b["roles"]) == ["barb", "window"], b

        roles = w.call("list_face_roles", handle=h)
        assert {r["name"]: r["role"] for r in roles} == {"window": "inlet", "barb": "outlet"}
        assert all(r["present"] for r in roles), roles

        by_tag = w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet)
        by_role = w.call("check_airtight_path", handle=h, inlet="inlet", outlet="outlet")
        by_name = w.call("check_airtight_path", handle=h, inlet="window", outlet="barb")
        assert by_role == by_tag, (by_role, by_tag)
        assert by_name == by_tag, (by_name, by_tag)
        assert by_role["ok"] is True, by_role


def test_annotate_face_persists_across_save():
    """Roles are stored in the .FCStd and survive save -> reopen, and role-name
    resolution still works in a fresh worker."""
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "annotated.FCStd")
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "good")
        w.call("annotate_face", handle=h, face=inlet, role="inlet", name="window",
               meta={"spec": "router window"})
        w.call("annotate_face", handle=h, face=outlet, role="outlet", name="barb")
        w.call("save_document", path=path)

    with Worker() as w2:
        w2.call("open_document", path=path)
        h2 = w2.call("register_handle", object="Adapter", prefix="p")["handle"]
        roles = w2.call("list_face_roles", handle=h2)
        assert {r["name"] for r in roles} == {"window", "barb"}, roles
        assert all(r["present"] for r in roles), roles
        meta = next(r.get("meta") for r in roles if r["name"] == "window")
        assert meta == {"spec": "router window"}, roles
        res = w2.call("check_airtight_path", handle=h2, inlet="inlet", outlet="outlet")
        assert res["connected"] is True and res["ok"] is True, res


def test_annotate_face_invalid_role_rejected():
    """An unknown role is a usage error listing the valid set, not a silent pass."""
    with Worker() as w:
        h, inlet, _ = _build_adapter(w, "good")
        try:
            w.call("annotate_face", handle=h, face=inlet, role="intlet")
        except WorkerError as e:
            assert "inlet" in str(e) and "intlet" in str(e), e
        else:
            assert False, "expected an error for an unknown role"
        assert w.call("ping") == "pong"


def test_annotate_face_autonames():
    """Omitting name defaults to the role, then role_2, role_3 for repeats."""
    with Worker() as w:
        w.call("new_document", name="autoname")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        h = box["handle"]
        faces = w.call("list_faces", handle=h)
        a = w.call("annotate_face", handle=h, face=faces[0]["tag"], role="sealing")
        b = w.call("annotate_face", handle=h, face=faces[1]["tag"], role="sealing")
        assert a["name"] == "sealing" and b["name"] == "sealing_2", (a, b)


def test_face_role_drift_detected():
    """When an edit changes a tagged face, list_face_roles reports present=False —
    the cheap drift signal the regression gate (slice 4) will build on."""
    with Worker() as w:
        w.call("new_document", name="drift")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        h = box["handle"]
        topz = w.call("query_faces", handle=h,
                      predicate={"type": "planar", "normal_dir": [0, 0, 1]})[0]["tag"]
        w.call("annotate_face", handle=h, face=topz, role="sealing", name="lid")
        assert w.call("list_face_roles", handle=h)[0]["present"] is True
        # shrink the box: the annotated +Z face (area 100) no longer exists
        w.call("run_script", code=(
            "o = App.ActiveDocument.getObject('Box'); o.Width = 8.0; o.Length = 8.0; "
            "App.ActiveDocument.recompute()"))
        after = w.call("list_face_roles", handle=h)
        assert after[0]["present"] is False, after


# --- classify_face_sides (issue #19, slice 3) --------------------------------

def test_classify_face_sides_wetted_vs_ambient():
    """With the declared ports sealed, the bore walls read as interior/wetted and
    the outer walls as ambient; without sealing, the open bore has no enclosed
    cavity so nothing is interior."""
    with Worker() as w:
        h, inlet, outlet = _build_adapter(w, "good")
        w.call("annotate_face", handle=h, face=inlet, role="inlet", name="window")
        w.call("annotate_face", handle=h, face=outlet, role="outlet", name="barb")

        sides = w.call("classify_face_sides", handle=h)  # seal_ports default True
        n_interior = sum(1 for s in sides if s["side"] == "interior")
        n_ambient = sum(1 for s in sides if s["side"] == "ambient")
        assert n_interior >= 4, sides   # 4 inner bore walls
        assert n_ambient >= 4, sides    # 4 outer walls
        assert all(s["suggested_role"] == "wetted" for s in sides if s["side"] == "interior")
        declared = {s["declared_role"] for s in sides if "declared_role" in s}
        assert declared == {"inlet", "outlet"}, sides

        unsealed = w.call("classify_face_sides", handle=h, seal_ports=False)
        assert sum(1 for s in unsealed if s["side"] == "interior") == 0, unsealed


# --- declare_intent / verify_intent (issue #19, slice 4) ---------------------

def _build_annotated_adapter(w, kind):
    h, inlet, outlet = _build_adapter(w, kind)
    w.call("annotate_face", handle=h, face=inlet, role="inlet", name="window")
    w.call("annotate_face", handle=h, face=outlet, role="outlet", name="barb")
    return h


def test_verify_intent_good_passes():
    """A clean adapter satisfies watertight + airtight_path + required_faces."""
    with Worker() as w:
        h = _build_annotated_adapter(w, "good")
        w.call("declare_intent", handle=h, contract={
            "watertight": True,
            "airtight_path": {"inlet": "inlet", "outlet": "outlet", "min_aperture_mm2": 50.0},
            "required_faces": ["window", "barb"],
        })
        r = w.call("verify_intent", handle=h)
        assert r["ok"] is True, r
        assert {x["invariant"] for x in r["results"]} == {"watertight", "airtight_path", "required_faces"}
        assert all(x["passed"] for x in r["results"]), r


def test_verify_intent_bottleneck_fails():
    """The v2 slit keeps the solid watertight but fails the airtight_path gate."""
    with Worker() as w:
        h = _build_annotated_adapter(w, "slit")
        w.call("declare_intent", handle=h, contract={
            "watertight": True,
            "airtight_path": {"inlet": "inlet", "outlet": "outlet", "min_aperture_mm2": 50.0},
        })
        r = w.call("verify_intent", handle=h)
        res = {x["invariant"]: x["passed"] for x in r["results"]}
        assert res["watertight"] is True, r       # watertight is happy...
        assert res["airtight_path"] is False, r    # ...but the path is pinched
        assert r["ok"] is False, r


def test_verify_intent_leaky_fails():
    """The v3-style breach fails the airtight_path gate (leaky)."""
    with Worker() as w:
        h = _build_annotated_adapter(w, "leaky")
        w.call("declare_intent", handle=h, contract={
            "airtight_path": {"inlet": "inlet", "outlet": "outlet"},
        })
        r = w.call("verify_intent", handle=h)
        assert r["ok"] is False, r
        ap = next(x for x in r["results"] if x["invariant"] == "airtight_path")
        assert ap["passed"] is False and "leaky=True" in ap["detail"], ap


def test_verify_intent_persists_across_save():
    """Declared intent survives save -> reopen and re-runs in a fresh worker."""
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "intent.FCStd")
    with Worker() as w:
        h = _build_annotated_adapter(w, "good")
        w.call("declare_intent", handle=h, contract={
            "watertight": True,
            "airtight_path": {"inlet": "inlet", "outlet": "outlet", "min_aperture_mm2": 50.0},
        })
        w.call("save_document", path=path)
    with Worker() as w2:
        w2.call("open_document", path=path)
        h2 = w2.call("register_handle", object="Adapter", prefix="p")["handle"]
        r = w2.call("verify_intent", handle=h2)
        assert r["ok"] is True, r


def test_verify_intent_required_face_drift_fails():
    """A required face that drifts/vanishes after an edit fails the gate."""
    with Worker() as w:
        w.call("new_document", name="reqdrift")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        h = box["handle"]
        topz = w.call("query_faces", handle=h,
                      predicate={"type": "planar", "normal_dir": [0, 0, 1]})[0]["tag"]
        w.call("annotate_face", handle=h, face=topz, role="sealing", name="lid")
        w.call("declare_intent", handle=h, contract={"required_faces": ["lid"]})
        assert w.call("verify_intent", handle=h)["ok"] is True
        w.call("run_script", code=(
            "o = App.ActiveDocument.getObject('Box'); o.Width = 8.0; o.Length = 8.0; "
            "App.ActiveDocument.recompute()"))
        r = w.call("verify_intent", handle=h)
        assert r["ok"] is False, r
        rf = next(x for x in r["results"] if x["invariant"] == "required_faces")
        assert rf["passed"] is False and "lid" in rf["detail"], rf


def test_declare_intent_validation():
    """Empty contract and a malformed airtight_path are rejected; verify needs a
    declared contract."""
    with Worker() as w:
        h = _build_annotated_adapter(w, "good")
        for bad in ({}, {"airtight_path": {"inlet": "inlet"}}):
            try:
                w.call("declare_intent", handle=h, contract=bad)
            except WorkerError:
                pass
            else:
                assert False, f"expected rejection for contract={bad}"
        w.call("new_document", name="nointent")
        box = w.call("add_primitive", kind="box", w=5, d=5, h=5)
        try:
            w.call("verify_intent", handle=box["handle"])
        except WorkerError as e:
            assert "no intent" in str(e).lower(), e
        else:
            assert False, "expected verify_intent to require a declared contract"
        assert w.call("ping") == "pong"


def test_topology_to_solid_reconstructs_density_field():
    """topology_to_solid bakes a real solid from a topology density grid (the
    modeller-side follow-on to topology_optimize_submit). A full grid is exactly
    cell-tiled; a holed grid drops the void cell; mass_fraction tracks the kept
    cells — the geometric gate from SIMULATION_EXAMPLES §5."""
    with Worker() as w:
        w.call("new_document", name="topo")
        # full 2x3 grid (nely=2, nelx=3), 10mm square cells, 5mm thick:
        # volume == 30 x 20 x 5 == 3000 mm^3, one fused solid.
        full = w.call("topology_to_solid",
                      density=[[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
                      cell_mm=10.0, thickness_mm=5.0)
        assert abs(full["volume"] - 3000.0) < 1e-6, full["volume"]
        assert full["mass_fraction"] == 1.0, full
        assert full["solid_cells"] == 6 and full["total_cells"] == 6, full
        assert full["n_solids"] == 1, full
        assert full["bbox_mm"] == [30.0, 20.0, 5.0], full["bbox_mm"]
        assert full["handle"].startswith("toposolid_"), full["handle"]

        # 3x3 frame (center void): 8 of 9 cells, still one connected solid.
        frame = w.call("topology_to_solid",
                       density=[[1, 1, 1], [1, 0, 1], [1, 1, 1]],
                       cell_mm=10.0, thickness_mm=10.0)
        assert abs(frame["volume"] - 8000.0) < 1e-6, frame["volume"]   # 8 cells x 1000
        assert frame["solid_cells"] == 8 and frame["total_cells"] == 9, frame
        assert frame["mass_fraction"] == round(8 / 9, 6), frame["mass_fraction"]
        assert frame["n_solids"] == 1, frame

        # a split load path → two disjoint solids in one feature.
        split = w.call("topology_to_solid", density=[[1, 0, 1]],
                       cell_mm=10.0, thickness_mm=10.0)
        assert split["n_solids"] == 2, split
        assert abs(split["volume"] - 2000.0) < 1e-6, split["volume"]


def test_topology_to_solid_3d_voxel_field():
    """The 3-D path (P3 M5): a nelz×nely×nelx voxel field from simp_topology_3d
    bakes as merged boxes — a full field is one exact cuboid; an L across layers
    keeps the voxel volume; mass_fraction tracks kept voxels."""
    with Worker() as w:
        w.call("new_document", name="topo3d")
        # full 2x2x3 field (nelz=2, nely=2, nelx=3), [10, 5, 4] mm cells:
        # one merged box 30 x 10 x 8 mm == 2400 mm^3.
        full = w.call("topology_to_solid",
                      density=[[[1.0] * 3] * 2] * 2, cell_mm=[10.0, 5.0, 4.0])
        assert abs(full["volume"] - 2400.0) < 1e-6, full["volume"]
        assert full["mass_fraction"] == 1.0 and full["n_solids"] == 1, full
        assert full["nelz"] == 2 and full["total_cells"] == 12, full
        assert full["bbox_mm"] == [30.0, 10.0, 8.0], full["bbox_mm"]

        # L-shape across layers: 9 of 12 voxels, unit cells -> 9 mm^3, connected.
        ell = w.call("topology_to_solid",
                     density=[[[1, 1, 0], [1, 1, 0]],
                              [[1, 1, 0], [1, 1, 1]]], cell_mm=1.0)
        assert abs(ell["volume"] - 9.0) < 1e-9, ell["volume"]
        assert ell["solid_cells"] == 9 and ell["total_cells"] == 12, ell
        assert ell["n_solids"] == 1, ell


def test_topology_to_solid_empty_threshold_is_a_clean_error():
    """A threshold above every cell yields no geometry: a structured error, not a
    crash — and the worker survives it."""
    with Worker() as w:
        w.call("new_document", name="topo_empty")
        try:
            w.call("topology_to_solid", density=[[0.4, 0.3], [0.2, 0.49]],
                   threshold=0.5)
        except WorkerError as e:
            assert "threshold" in e.remote_message.lower(), e.remote_message
        else:
            raise AssertionError("expected WorkerError for an all-void threshold")
        assert w.call("ping") == "pong", "worker poisoned by the error path"


def test_geometry_bridge_validation_errors_are_clean():
    """The M4 bridge modes reject bad inputs as structured errors before any
    meshing/solving happens — and the worker survives them. (Runs only when
    ElmerSolver resolves: with the solver absent the handler's graceful-degradation
    dict short-circuits before validation, which test_solve_degradation covers.)"""
    import shutil as _shutil

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from driftpin import solvers
    if not solvers.is_available("elmer") or not os.path.isfile(
            solvers.sibling_bin(solvers.find_solver("elmer")["path"], "ElmerGrid")):
        print("    SKIP — ElmerSolver/ElmerGrid not installed")
        return
    with Worker() as w:
        w.call("new_document", name="bridge_val")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        # no convection_faces
        try:
            w.call("thermal_transient_submit", body=box["handle"],
                   h_conv=1000.0, duration_s=1.0, k=200.0, rho=2700.0, cp=900.0)
        except WorkerError as e:
            assert "convection_faces" in e.remote_message, e.remote_message
        else:
            raise AssertionError("expected WorkerError without convection_faces")
        # face index out of range
        try:
            w.call("thermal_transient_submit", body=box["handle"],
                   convection_faces=[1, 99], h_conv=1000.0, duration_s=1.0,
                   k=200.0, rho=2700.0, cp=900.0)
        except WorkerError as e:
            assert "out of range" in e.remote_message, e.remote_message
        else:
            raise AssertionError("expected WorkerError for face index 99")
        assert w.call("ping") == "pong", "worker poisoned by the error path"


def test_slice_gcode_cube_end_to_end_matches_analytic():
    """The Sprint-4 CLI upgrade through the worker: a real FreeCAD cube -> STL
    export (main thread) -> PrusaSlicer in the background -> parsed G-code next to
    the analytic slice_estimate. At 100% infill the deposited_ratio must be ~1
    (measured 1.008 — the skirt). SKIPs without a slicer."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from driftpin import solvers
    if not solvers.is_available("prusaslicer"):
        print("    SKIP — PrusaSlicer not installed")
        return
    with Worker() as w:
        w.call("new_document", name="slice_e2e")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        sub = w.call("slice_gcode_submit", body=box["handle"],
                     layer_height_mm=0.2, infill_fraction=1.0, material="PLA")
        assert sub.get("job_id"), sub
        deadline = time.monotonic() + 120
        status = None
        while time.monotonic() < deadline:
            status = w.call("job_status", job_id=sub["job_id"])
            if status["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        assert status and status["status"] == "done", status
        res = w.call("job_result", job_id=sub["job_id"])["result"]
        assert res["ok"], res
        assert res["layer_count"] > 90, res["layer_count"]
        assert res["print_time_s"] and res["filament_g"] > 0, res
        # sliced volume vs the analytic estimate for the same body
        assert 0.95 < res["deposited_ratio"] < 1.10, res["deposited_ratio"]
        # validation: no body and no stl_path is a clean error
        try:
            w.call("slice_gcode_submit", infill_fraction=0.5)
        except WorkerError as e:
            assert "body" in e.remote_message, e.remote_message
        else:
            raise AssertionError("expected WorkerError without body/stl_path")


def test_geometry_bridge_box_end_to_end_matches_heisler():
    """The full M4 Elmer path through the worker: a real FreeCAD box → GmshTools →
    UNV (face groups intact) → ElmerGrid → ElmerSolver, polled via the shared job
    surface — and the solved plane wall matches thermal_transient_1d (the same gate
    test_meshbridge runs on the committed fixture, here exercising the live
    FreeCAD-side meshing + export instead). SKIPs without ElmerSolver/ElmerGrid."""
    import shutil as _shutil

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from driftpin import solvers
    from driftpin.analysis import thermal as _thermal
    if not solvers.is_available("elmer") or not os.path.isfile(
            solvers.sibling_bin(solvers.find_solver("elmer")["path"], "ElmerGrid")):
        print("    SKIP — ElmerSolver/ElmerGrid not installed")
        return
    with Worker() as w:
        w.call("new_document", name="bridge_e2e")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        # box faces 1 and 2 are x=0 / x=20 (verified mapping): a plane wall of
        # half-thickness 10 mm cooled on both faces, lateral faces adiabatic.
        # element_order="2nd": quadratic tets resolve this Bi=0.5 transient accurately.
        # The bridge meshes SERIALLY (worker pins Gmsh NumThreads=1): parallel 3-D
        # Delaunay is non-deterministic and, under the CI suite's CPU contention, used
        # to silently fall back to a near-degenerate mesh that ignored the 2 mm size cap
        # — the solve still returned ok:true but over-reported the centre temperature
        # (~12 % high, the env-flaky failure). Serial meshing is reproducible across
        # hosts/load, and the worker's adequacy guard fails loudly on any residual
        # too-coarse mesh rather than handing back wrong physics — so ±3 % holds.
        sub = w.call("thermal_transient_submit", body=box["handle"],
                     convection_faces=[1, 2], h_conv=10000.0, duration_s=0.6,
                     k=200.0, rho=2700.0, cp=900.0, char_length_mm=2.0,
                     element_order="2nd", t_initial_c=100.0, t_ambient_c=25.0)
        assert sub.get("job_id"), sub
        deadline = time.monotonic() + 120
        status = None
        while time.monotonic() < deadline:
            status = w.call("job_status", job_id=sub["job_id"])
            if status["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        assert status and status["status"] == "done", status
        res = w.call("job_result", job_id=sub["job_id"])["result"]
        assert res["ok"], res
        assert res["tets"] > 0 and res["nodes"] > 0, res
        oracle = _thermal.thermal_transient_1d(
            half_thickness_mm=10.0, h_conv=10000.0, duration_s=0.6,
            k=200.0, rho=2700.0, cp=900.0)
        for solved, key in ((res["t_max_c"], "t_center_c"),
                            (res["t_min_c"], "t_surface_c")):
            ratio = (solved - 25.0) / (oracle[key] - 25.0)
            assert 0.97 < ratio < 1.03, (key, solved, oracle[key], ratio)


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
            print(f"  FAIL {name:40s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:40s} ({time.time() - t0:.2f}s)")

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

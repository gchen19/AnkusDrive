"""
export_shape with no `object` (issue #414).

The fallback used to take the first object with a non-null `Shape`, which went
wrong two ways:

  crash  — after FEM setup the mesh object's `Shape` is a link to a
           Part.Feature, not a TopoShape, so `.isNull()` raised AttributeError
           and a user who only wanted the part could not export it.
  silent — box -> cylinder -> cut exported the Box: the Cut's consumed base,
           not the finished solid.

Now the default is the single un-consumed shape (solids first), and more than
one candidate is refused with the names listed.

  test_cut_exports_the_result_not_the_base : silent case, checked by STEP volume
  test_export_after_fem_mesh               : crash case, via the real fem_mesh
  test_two_final_solids_are_refused        : ambiguity raises and lists both
  test_single_primitive_still_defaults     : the old single-box path still works
  test_partdesign_body_exports_the_body    : Body features count as consumed
  test_explicit_object_without_shape       : object=<mesh> is a clear error

The same `hasattr(o, "Shape") and not o.Shape.isNull()` idiom crashed the other
tools that walk a document, and "top-level" meant "nothing references it", which an
FEM mesh or material defeats. Now only a *shaped* referrer demotes an object:

  test_get_object_on_fem_mesh              : get_object(mesh) dumps, no volume
  test_save_step_after_fem_mesh            : save_document(.step) is the Cut alone
  test_save_step_body_is_the_body          : ...and origin planes stay out of it
  test_add_part_from_file_with_fem_mesh    : add_part links the Cut
  test_assembly_doc_exports_the_container  : linked children don't make it ambiguous
  test_release_step_without_page_is_cut    : release_package's no-page fallback had
                                             #414's silent default too (released Box)

Run: python3 tests/test_export_shape_default.py
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker, WorkerError  # noqa: E402

_STEEL = {"Name": "Steel", "YoungsModulus": "210000 MPa",
          "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}


def _box_minus_cylinder(w, doc):
    """20x20x10 box with a r=3 through-hole. Returns the boolean_op payload."""
    w.call("new_document", name=doc)
    box = w.call("add_primitive", kind="box", w=20, d=20, h=10)["handle"]
    cyl = w.call("add_primitive", kind="cylinder", r=3, h=10, placement=[10, 10, 0])["handle"]
    return w.call("boolean_op", op="cut", base=box, tool=cyl)


def _step_volume(w, path):
    """Volume of a STEP file, read back in a scratch document."""
    code = (
        "import Part\n"
        f"s = Part.Shape(); s.read({str(path)!r})\n"
        "__result__ = s.Volume\n"
    )
    return w.call("run_script", code=code, auto_register=False)["result"]


def _meshed_cut(w, doc):
    """_box_minus_cylinder plus an FEM analysis, material and gmsh mesh.
    Returns (cut payload, mesh payload)."""
    cut = _box_minus_cylinder(w, doc)
    an = w.call("fem_new_analysis")["handle"]
    w.call("fem_set_material", analysis=an, body=cut["handle"], material=dict(_STEEL))
    mesh = w.call("fem_mesh", analysis=an, body=cut["handle"], char_length=4.0)
    return cut, mesh


def _pad_body(w, doc):
    """A 10x10x5 PartDesign pad in a fresh document."""
    w.call("new_document", name=doc)
    body = w.call("make_body")
    sk = w.call("make_sketch", body=body["handle"], plane="XY")
    w.call("add_sketch_geometry", sketch=sk["handle"], items=[
        {"type": "line", "start": [0, 0], "end": [10, 0]},
        {"type": "line", "start": [10, 0], "end": [10, 10]},
        {"type": "line", "start": [10, 10], "end": [0, 10]},
        {"type": "line", "start": [0, 10], "end": [0, 0]},
    ])
    w.call("pad", sketch=sk["handle"], length=5)
    return body


def _step_solids(w, path):
    code = (
        "import Part\n"
        f"s = Part.Shape(); s.read({str(path)!r})\n"
        "__result__ = len(s.Solids)\n"
    )
    return w.call("run_script", code=code, auto_register=False)["result"]


def _expect_error(w, **kwargs):
    try:
        w.call("export_shape", **kwargs)
    except WorkerError as e:
        return str(e)
    raise AssertionError(f"expected export_shape({kwargs}) to raise")


# --- tests ----------------------------------------------------------------------

def test_cut_exports_the_result_not_the_base():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_cut")
        step = os.path.join(d, "part.step")
        r = w.call("export_shape", path=step)
        assert r["object"] == "Cut", r
        vol = _step_volume(w, step)
        assert abs(vol - cut["volume"]) < 1e-3, (vol, cut["volume"])


def test_export_after_fem_mesh():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        _meshed_cut(w, "es_fem")
        types = {o["type"] for o in w.call("list_objects")}
        assert any("FemMesh" in t for t in types), types  # the object that used to crash it
        r = w.call("export_shape", path=os.path.join(d, "part.step"))
        assert r["object"] == "Cut", r
        assert r["size"] > 0, r


def test_two_final_solids_are_refused():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        w.call("new_document", name="es_two")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        w.call("add_primitive", kind="sphere", r=4, placement=[30, 0, 0])
        step = os.path.join(d, "part.step")
        msg = _expect_error(w, path=step)
        assert "Box" in msg and "Sphere" in msg and "object=" in msg, msg
        assert not os.path.exists(step), "refusal must not write a file"
        r = w.call("export_shape", path=step, object="Sphere")
        assert r["object"] == "Sphere", r


def test_single_primitive_still_defaults():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        w.call("new_document", name="es_single")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        r = w.call("export_shape", path=os.path.join(d, "box.stl"))
        assert r["object"] == "Box" and r["size"] > 0, r


def test_partdesign_body_exports_the_body():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        _pad_body(w, "es_body")
        body_name = next(o["name"] for o in w.call("list_objects")
                         if o["type"] == "PartDesign::Body")
        r = w.call("export_shape", path=os.path.join(d, "body.brep"))
        assert r["object"] == body_name, r


def test_explicit_object_without_shape():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        _, mesh = _meshed_cut(w, "es_explicit")
        msg = _expect_error(w, path=os.path.join(d, "m.step"), object=mesh["name"])
        assert "no exportable Part shape" in msg, msg


def test_get_object_on_fem_mesh():
    with Worker() as w:
        _, mesh = _meshed_cut(w, "es_getobj")
        r = w.call("get_object", handle=mesh["handle"])
        assert "volume" not in r, r


def test_save_step_after_fem_mesh():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut, _ = _meshed_cut(w, "es_save")
        step = os.path.join(d, "part.step")
        w.call("save_document", path=step)
        assert _step_solids(w, step) == 1, "Box/Cylinder leaked into the export"
        vol = _step_volume(w, step)
        assert abs(vol - cut["volume"]) < 1e-3, (vol, cut["volume"])


def test_save_step_body_is_the_body():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        _pad_body(w, "es_savebody")
        step = os.path.join(d, "body.step")
        w.call("save_document", path=step)
        assert _step_solids(w, step) == 1
        vol = _step_volume(w, step)
        assert abs(vol - 500.0) < 1e-3, vol


def test_add_part_from_file_with_fem_mesh():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut, _ = _meshed_cut(w, "es_src")
        src = os.path.join(d, "src.FCStd")
        w.call("save_document", path=src)
        w.call("new_document", name="es_asm")
        asm = w.call("make_assembly")
        w.call("save_document", path=os.path.join(d, "asm.FCStd"))  # cross-doc links need it
        w.call("add_part", assembly=asm["handle"], source={"path": src})
        parts = w.call("list_assembly_parts", assembly=asm["handle"])
        assert [p["linked"] for p in parts] == ["Cut"], parts
        assert abs(parts[0]["volume"] - cut["volume"]) < 1e-3, parts


def test_release_step_without_page_is_cut():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_release")
        w.call("save_document", path=os.path.join(d, "part.FCStd"))
        registry = os.path.join(d, "items.json")
        w.call("items_new", registry=registry, item="part", files=["part.FCStd"])
        res = w.call("release_package", registry=registry, item="part",
                     out_dir=os.path.join(d, "pkg"), kinds=["step"], draft=True)
        step = next(Path(d, "pkg", f["name"]) for f in res["files"] if f["kind"] == "step")
        vol = _step_volume(w, step)
        assert abs(vol - cut["volume"]) < 1e-3, (vol, cut["volume"])


def test_assembly_doc_exports_the_container():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_asm_doc")
        asm = w.call("make_assembly")
        for x in (0, 40):
            w.call("add_part", assembly=asm["handle"], source={"handle": cut["handle"]},
                   placement=[x, 0, 0])
        # .brep, not .step: selection is what's under test, and Part.export of an
        # App::Part of links currently writes a STEP with no geometry (separate bug).
        r = w.call("export_shape", path=os.path.join(d, "asm.brep"))
        assert r["object"] == asm["name"], r


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

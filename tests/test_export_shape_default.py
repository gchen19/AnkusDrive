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

Exporting that container to STEP then wrote a 1.6 kB file with no faces in it, for
a container of links and of plain features alike, and reported success (#434). The
resolved shape is exported now, and a STEP/IGES that carries no geometry raises:

  test_assembly_step_carries_the_geometry  : both parts, volumes and placements
  test_assembly_iges_carries_the_geometry  : the same for IGES
  test_single_part_step_unchanged          : the ordinary path still exports
  test_step_geometry_scanner               : the guard's scan, without FreeCAD
  test_release_of_an_assembly_has_geometry : the vendor bundle carried the empty file
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


def _step_stats(w, path):
    """Solid count, volume and bounding-box X length of an exported file."""
    code = (
        "import Part\n"
        f"s = Part.Shape(); s.read({str(path)!r})\n"
        "bb = s.BoundBox\n"
        "__result__ = {'solids': len(s.Solids), 'faces': len(s.Faces),\n"
        "              'vol': s.Volume, 'xlen': bb.XLength}\n"
    )
    return w.call("run_script", code=code, auto_register=False)["result"]


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
        step = os.path.join(d, "asm.step")
        r = w.call("export_shape", path=step)
        assert r["object"] == asm["name"], r
        got = _step_stats(w, step)
        # both parts, their volumes, and 40 mm apart -- placements survive (#434)
        assert got["solids"] == 2, got
        assert abs(got["vol"] - 2 * cut["volume"]) < 1e-3, (got, cut["volume"])
        assert abs(got["xlen"] - 60.0) < 1e-6, got


def test_assembly_step_carries_the_geometry():
    """#434: Part.export of an App::Part dropped every face; the shape is exported now."""
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_asm_step")
        asm = w.call("make_assembly")
        for x in (0, 40):
            w.call("add_part", assembly=asm["handle"], source={"handle": cut["handle"]},
                   placement=[x, 0, 0])
        step = os.path.join(d, "asm.step")
        r = w.call("export_shape", path=step, object=asm["name"])
        got = _step_stats(w, step)
        assert got["solids"] == 2, got
        assert abs(got["vol"] - 2 * cut["volume"]) < 1e-3, (got, cut["volume"])
        assert r["size"] > 4096, r  # the broken file was 1640 bytes


def test_assembly_iges_carries_the_geometry():
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_asm_iges")
        asm = w.call("make_assembly")
        for x in (0, 40):
            w.call("add_part", assembly=asm["handle"], source={"handle": cut["handle"]},
                   placement=[x, 0, 0])
        iges = os.path.join(d, "asm.iges")
        w.call("export_shape", path=iges, object=asm["name"])
        got = _step_stats(w, iges)
        # IGES is surface-based: faces and volume, not solids
        assert got["faces"] == 14, got
        assert abs(got["vol"] - 2 * cut["volume"]) < 1e-1, (got, cut["volume"])


def test_single_part_step_unchanged():
    """The ordinary path is untouched: same geometry as before the #434 fix."""
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_plain_step")
        step = os.path.join(d, "part.step")
        w.call("export_shape", path=step, object="Cut")
        got = _step_stats(w, step)
        assert got["solids"] == 1 and got["faces"] == 7, got
        assert abs(got["vol"] - cut["volume"]) < 1e-3, (got, cut["volume"])


def test_step_geometry_scanner():
    """The guard's scan, on bytes, with no FreeCAD in the loop."""
    from ankusdrive import export_check

    with tempfile.TemporaryDirectory() as d:
        empty = Path(d, "empty.step")
        empty.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
                          b"#3 = SHAPE_DEFINITION_REPRESENTATION(#4,#10);\nENDSEC;\n")
        assert export_check.step_has_geometry(empty) is False

        real = Path(d, "real.step")
        real.write_bytes(b"DATA;\n#12 = ADVANCED_FACE('',(#13),#40,.T.);\n")
        assert export_check.step_has_geometry(real) is True

        # a marker straddling the chunk boundary is still found
        split = Path(d, "split.step")
        split.write_bytes(b"x" * 1023 + b"MANIFOLD_SOLID_BREP('',#9)")
        assert export_check.file_contains(
            split, export_check.STEP_GEOMETRY_MARKERS, chunk_bytes=1024) is True


def test_release_of_an_assembly_has_geometry():
    """release_package exports its STEP through export_shape, so a released
    assembly shipped the empty file to the vendor (#434)."""
    with Worker() as w, tempfile.TemporaryDirectory() as d:
        cut = _box_minus_cylinder(w, "es_rel_asm")
        asm = w.call("make_assembly")
        for x in (0, 40):
            w.call("add_part", assembly=asm["handle"], source={"handle": cut["handle"]},
                   placement=[x, 0, 0])
        w.call("save_document", path=os.path.join(d, "asm.FCStd"))
        registry = os.path.join(d, "items.json")
        w.call("items_new", registry=registry, item="asm", files=["asm.FCStd"])
        res = w.call("release_package", registry=registry, item="asm",
                     out_dir=os.path.join(d, "pkg"), kinds=["step"], draft=True)
        step = next(Path(d, "pkg", f["name"]) for f in res["files"] if f["kind"] == "step")
        got = _step_stats(w, step)
        assert got["solids"] == 2, got
        assert abs(got["vol"] - 2 * cut["volume"]) < 1e-3, (got, cut["volume"])


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

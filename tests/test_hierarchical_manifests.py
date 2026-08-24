"""
Hierarchical manifests (RFC §11.4) — free, no LLM, no key. A component entry may
reference a child `manifest` instead of a `file`; merge_assembly merges + gates
the child first (recursively, any depth), links its merged root, rolls a failed
child up into the parent, and the lockfile propagates child changes up the tree.

Proves: nested merge passes for a sound tree; a broken CHILD fails the parent
(surfaced, not hidden); parent-level clashes between a subassembly and a sibling
are still caught; two-level nesting flattens; and assembly_lock_check flags a
changed subassembly.

Run: .venv/bin/python3 tests/test_hierarchical_manifests.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# --- scripted leaf builders --------------------------------------------------

def _plate(path, hole_d=16.0):
    with Worker() as w:
        w.call("new_document", name="plate")
        w.call("add_primitive", kind="box", w=60, d=60, h=10, name="box")
        w.call("add_primitive", kind="cylinder", r=hole_d / 2.0, h=30,
               placement=[30, 30, -10], name="bore")
        w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")
        w.call("save_document", path=str(path))


def _peg(path, peg_d=15.6):
    with Worker() as w:
        w.call("new_document", name="peg")
        w.call("add_primitive", kind="cylinder", r=peg_d / 2.0, h=20, name="peg")
        w.call("save_document", path=str(path))


def _box(path, w_, d_, h_, name="part"):
    with Worker() as w:
        w.call("new_document", name=name)
        w.call("add_primitive", kind="box", w=w_, d=d_, h=h_, name=name)
        w.call("save_document", path=str(path))


def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _sub_manifest(sub_dir, peg_d=15.6):
    """A peg-in-hole subassembly directory; returns its manifest path."""
    sub_dir.mkdir(parents=True, exist_ok=True)
    _plate(sub_dir / "plate.FCStd")
    _peg(sub_dir / "peg.FCStd", peg_d)
    return _write(sub_dir / "manifest.json", {
        "name": "sub", "root": "sub.FCStd",
        "components": {"plate": {"file": "plate.FCStd"},
                       "peg": {"file": "peg.FCStd"}},
        "instances": [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                      {"component": "peg", "name": "peg", "placement": [30, 30, -5]}]})


def _merge(mpath):
    with Worker() as w:
        return w.call("merge_assembly", manifest=str(mpath))


# --- tests -------------------------------------------------------------------

def test_nested_reference_passes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _sub_manifest(tmp / "sub")
        _box(tmp / "lid.FCStd", 60, 60, 4, "lid")
        m = _write(tmp / "manifest.json", {
            "name": "top", "root": "top.FCStd",
            "components": {"drivetrain": {"manifest": "sub/manifest.json"},
                           "lid": {"file": "lid.FCStd"}},
            "instances": [{"component": "drivetrain", "name": "drivetrain",
                           "placement": [0, 0, 0]},
                          {"component": "lid", "name": "lid", "placement": [0, 0, 20]}]})
        rep = _merge(m)
        _check("nested merge ok", rep["ok"], True)
        _check("child gated ok", rep["children"]["drivetrain"]["ok"], True)
        bom = {r["part"]: r["count"] for r in rep["gates"]["bom"]}
        _check("BOM flattens through subassembly to leaves",
               bom, {"plate": 1, "peg": 1, "lid": 1})


def test_broken_child_fails_parent():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _sub_manifest(tmp / "sub", peg_d=16.4)   # peg too fat -> child interferes
        _box(tmp / "lid.FCStd", 60, 60, 4, "lid")
        m = _write(tmp / "manifest.json", {
            "name": "top", "root": "top.FCStd",
            "components": {"drivetrain": {"manifest": "sub/manifest.json"},
                           "lid": {"file": "lid.FCStd"}},
            "instances": [{"component": "drivetrain", "name": "drivetrain",
                           "placement": [0, 0, 0]},
                          {"component": "lid", "name": "lid", "placement": [0, 0, 20]}]})
        rep = _merge(m)
        _check("broken child -> parent NOT ok", rep["ok"], False)
        _check("offending child surfaced", rep["gates"]["children"],
               {"drivetrain": False})


def test_parent_level_clash_caught():
    """The subassembly is sound, but a sibling placed INTO it clashes — a clash
    the child can't see must still be caught at the parent."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _sub_manifest(tmp / "sub")           # plate spans z[0,10]
        _box(tmp / "lid.FCStd", 60, 60, 10, "lid")
        m = _write(tmp / "manifest.json", {
            "name": "top", "root": "top.FCStd",
            "components": {"drivetrain": {"manifest": "sub/manifest.json"},
                           "lid": {"file": "lid.FCStd"}},
            "instances": [{"component": "drivetrain", "name": "drivetrain",
                           "placement": [0, 0, 0]},
                          {"component": "lid", "name": "lid",
                           "placement": [0, 0, 5]}]})  # lid overlaps the plate
        rep = _merge(m)
        _check("child sound", rep["children"]["drivetrain"]["ok"], True)
        _check("parent-level clash -> NOT ok", rep["ok"], False)
        _check("clash is interference, not a child failure",
               bool(rep["gates"]["interference"]) and "children" not in
               {k for k, v in rep["gates"].get("children", {}).items() if not v},
               True)


def test_two_level_nesting():
    """A subassembly whose own component is itself a subassembly — recursion to
    depth 2, flattening all the way to leaves."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _sub_manifest(tmp / "sub" / "inner")   # innermost: plate + peg
        # mid level: links the inner subassembly + a spacer
        _box(tmp / "sub" / "spacer.FCStd", 10, 10, 10, "spacer")
        _write(tmp / "sub" / "manifest.json", {
            "name": "mid", "root": "mid.FCStd",
            "components": {"inner": {"manifest": "inner/manifest.json"},
                           "spacer": {"file": "spacer.FCStd"}},
            "instances": [{"component": "inner", "name": "inner", "placement": [0, 0, 0]},
                          {"component": "spacer", "name": "spacer",
                           "placement": [0, 0, 40]}]})
        m = _write(tmp / "manifest.json", {
            "name": "top", "root": "top.FCStd",
            "components": {"assy": {"manifest": "sub/manifest.json"}},
            "instances": [{"component": "assy", "name": "assy", "placement": [0, 0, 0]}]})
        rep = _merge(m)
        _check("two-level nesting ok", rep["ok"], True)
        bom = {r["part"]: r["count"] for r in rep["gates"]["bom"]}
        _check("flattens to all leaves across two levels",
               bom, {"plate": 1, "peg": 1, "spacer": 1})


def test_lockfile_propagates_child_change():
    """assembly_lock on the parent, then change a child component and re-merge the
    child: the parent's lock check flags the subassembly as modified (a child
    change propagates UP without the parent reading geometry)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sub = tmp / "sub"
        sm = _sub_manifest(sub)
        _box(tmp / "lid.FCStd", 60, 60, 4, "lid")
        m = _write(tmp / "manifest.json", {
            "name": "top", "root": "top.FCStd",
            "components": {"drivetrain": {"manifest": "sub/manifest.json"},
                           "lid": {"file": "lid.FCStd"}},
            "instances": [{"component": "drivetrain", "name": "drivetrain",
                           "placement": [0, 0, 0]},
                          {"component": "lid", "name": "lid", "placement": [0, 0, 20]}]})
        with Worker() as w:
            w.call("merge_assembly", manifest=str(m))
            w.call("assembly_lock", manifest=str(m))
            clean = w.call("assembly_lock_check", manifest=str(m))
        _check("freshly locked parent is clean", clean["ok"], True)
        # change a child leaf, re-merge ONLY the child (updates sub.FCStd root)
        _peg(sub / "peg.FCStd", peg_d=15.0)      # different (still fits) peg
        with Worker() as w:
            w.call("merge_assembly", manifest=str(sm))
            chk = w.call("assembly_lock_check", manifest=str(m))
        _check("child change propagates -> subassembly modified",
               "drivetrain" in chk["modified"], True)
        _check("unchanged sibling not flagged", "lid" in chk["modified"], False)


def main():
    print("== hierarchical manifests — nested merge/gate/lock (no API) ==")
    for t in (test_nested_reference_passes, test_broken_child_fails_parent,
              test_parent_level_clash_caught, test_two_level_nesting,
              test_lockfile_propagates_child_change):
        try:
            t()
        except Exception as e:
            global _FAIL
            _FAIL += 1
            import traceback
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

"""
Project / workspace container (issue #143, D1 of docs/DESIGN_HIERARCHY.md §2
Theme D). Free, no LLM, no key. Two halves:

  * PURE (no FreeCAD): scaffold lays out a well-formed project that loads clean
    (validate_project); the reference-integrity guard flags a deliberately broken
    reference (a moved/renamed component file, a dangling item-ref) and a naming-
    convention violation while a clean project passes; an item-ref component lowers
    to a file component against the registry (the deferred #140 seam), and a
    dangling item-ref fails loudly.

  * WORKER (FreeCAD): a scaffolded project's manifest is consumed by merge_assembly
    UNCHANGED; a master/skeleton publishes interface geometry two child components
    subscribe to and mate against; an item-ref component resolves through the
    manifest and merges; the reference-integrity guard catches a broken item-ref
    BEFORE a merge while the clean project merges green; the worker's
    validate_manifest accepts the {item} source-kind.

Run: python3 tests/test_project_container.py     (or .venv/bin/python3 for the
worker half — the harness interpreter never imports FreeCAD itself).
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import project  # noqa: E402
from ankusdrive import items  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _flags(label, problems, substr):
    """Assert `problems` is non-empty and some problem mentions `substr`."""
    ok = bool(problems) and any(substr in p for p in problems)
    _check(label, ok, True)
    if not ok:
        print(f"       (problems were: {problems})")


# =============================================================================
# PURE — scaffold + validate + guard + lowering (no FreeCAD)
# =============================================================================

def test_scaffold_lays_out_wellformed_project():
    """scaffold writes project.json + items.json + manifest.json, creates the
    convention dirs, and the result loads clean (validate_project, base_dir set)."""
    with tempfile.TemporaryDirectory() as td:
        layout = project.scaffold(td, "gearbox")
        _check("project.json written", Path(layout["project_file"]).exists(), True)
        _check("items.json written", Path(layout["registry"]).exists(), True)
        _check("manifest.json written", Path(layout["manifest"]).exists(), True)
        _check("components/ created", Path(layout["components_dir"]).is_dir(), True)
        _check(".dp_lib/ created", Path(layout["lib_dir"]).is_dir(), True)
        proj = project.load_project(layout["project_file"])
        _check("schema stamp", proj["schema"], "ankusdrive.project/1")
        _check("loads clean (validate_project)",
               project.validate_project(proj, td), [])
        reg = items.load_registry(layout["registry"])
        _check("seeded an empty, valid item registry",
               items.validate_registry(reg), [])


def test_scaffold_seeds_items_and_master():
    """scaffold can seed the item registry and record the master/skeleton slot;
    the seeded items get sequential part numbers and the project stays clean."""
    with tempfile.TemporaryDirectory() as td:
        layout = project.scaffold(
            td, "drive", master="skeleton",
            components={"skeleton": {"file": "components/skeleton.FCStd"}},
            instances=[{"component": "skeleton", "placement": [0, 0, 0]}],
            items={"skeleton": {"files": ["components/skeleton.FCStd"]}})
        proj = project.load_project(layout["project_file"])
        _check("master slot recorded", proj.get("master"), "skeleton")
        reg = items.load_registry(layout["registry"])
        _check("seeded item got a part number",
               reg["items"]["skeleton"]["part_number"], "DP-001001")
        # validate_project confirms the master IS a real component of the manifest
        # (the slot is well-formed); it does not dereference component files — that is
        # the reference-integrity guard's job, exercised separately below.
        _check("master-as-real-component validates clean",
               project.validate_project(proj, td), [])
        bad_master = dict(proj, master="ghost")
        _flags("master naming a non-component flagged",
               project.validate_project(bad_master, td), "not a component")


def test_validate_project_catches_violations():
    bad_name = {"schema": project.SCHEMA, "name": "has space"}
    _flags("name violating convention flagged",
           project.validate_project(bad_name), "naming convention")
    bad_schema = {"schema": "ankusdrive.project/9", "name": "ok"}
    _flags("unknown schema flagged",
           project.validate_project(bad_schema), "unknown schema")
    no_name = {"schema": project.SCHEMA, "name": ""}
    _flags("empty name flagged", project.validate_project(no_name), "name")


def test_reference_guard_clean_passes():
    """A manifest whose component files all exist and ids are clean passes the
    reference-integrity guard."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "components").mkdir()
        (Path(td) / "components" / "housing.FCStd").write_text("x", encoding="utf-8")
        man = {"components": {"housing": {"file": "components/housing.FCStd"}},
               "instances": [{"component": "housing", "placement": [0, 0, 0]}]}
        _check("clean manifest -> no problems",
               project.check_references(man, registry=None, base_dir=td), [])


def test_reference_guard_flags_moved_file():
    """A component file that has been moved/renamed/never-built is caught."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "components").mkdir()
        man = {"components": {"housing": {"file": "components/housing.FCStd"}},
               "instances": []}
        _flags("missing component file flagged",
               project.check_references(man, base_dir=td), "missing on disk")


def test_reference_guard_flags_dangling_item():
    """An item-ref pointing at an unknown item id is caught against the registry."""
    reg = items.empty_registry()
    items.new_item(reg, "bracket", files=["components/bracket.FCStd"])
    man = {"components": {"part": {"item": "no_such_item"}}, "instances": []}
    _flags("dangling item-ref flagged",
           project.check_references(man, registry=reg), "no_such_item")
    # a live item-ref (whose file is absent on disk only because we pass no base_dir)
    good = {"components": {"part": {"item": "bracket"}}, "instances": []}
    _check("live item-ref -> no dangle",
           project.check_references(good, registry=reg), [])


def test_reference_guard_flags_naming_and_unknown_instance():
    reg = items.empty_registry()
    bad = {"components": {"bad name": {"file": "x.FCStd"}}, "instances": []}
    _flags("component id with a space flagged",
           project.check_references(bad, registry=reg), "naming convention")
    unknown = {"components": {"a": {"library": {"tool": "add_fastener"}}},
               "instances": [{"component": "ghost", "placement": [0, 0, 0]}]}
    _flags("instance naming an unknown component flagged",
           project.check_references(unknown), "unknown component")


def test_lower_item_refs_resolves_and_dangles_loud():
    """An {item} component lowers to a {file} component (CAD artifact from the
    registry, the #140 seam); a dangling item-ref raises loudly."""
    reg = items.empty_registry()
    items.new_item(reg, "bracket",
                   files=["components/bracket.step", "components/bracket.FCStd"])
    man = {"schema": "ankusdrive.manifest/1", "name": "a", "root": "a.FCStd",
           "components": {"brk": {"item": "bracket", "envelope": {"min": [0, 0, 0],
                                                                  "max": [9, 9, 9]}}},
           "instances": [{"component": "brk", "placement": [0, 0, 0]}]}
    lowered = project.lower_item_refs(man, reg)
    _check("item-ref lowered to the CAD (.FCStd) artifact",
           lowered["components"]["brk"]["file"], "components/bracket.FCStd")
    _check("envelope carried over",
           lowered["components"]["brk"].get("envelope"),
           {"min": [0, 0, 0], "max": [9, 9, 9]})
    bad = {"components": {"x": {"item": "ghost"}}, "instances": []}
    raised = False
    try:
        project.lower_item_refs(bad, reg)
    except KeyError:
        raised = True
    _check("dangling item-ref in lowering raises", raised, True)


# =============================================================================
# WORKER — merge, master/skeleton publish+subscribe, item-ref merge (FreeCAD)
# =============================================================================

def _box(path, w_, d_, h_, name="part"):
    from ankusdrive import Worker
    with Worker() as w:
        w.call("new_document", name=name)
        w.call("add_primitive", kind="box", w=w_, d=d_, h=h_, name=name)
        w.call("save_document", path=str(path))


def _skeleton(path):
    """A lean master/skeleton: a thin plate carrying ONLY interface geometry — two
    published slot frames, offset above the plate so children mate in free air."""
    from ankusdrive import Worker
    with Worker() as w:
        w.call("new_document", name="skeleton")
        r = w.call("add_primitive", kind="box", w=100, d=40, h=4, name="plate")
        h = r["handle"]
        w.call("publish_interface", handle=h, name="slot_a",
               frame={"origin": [15, 20, 14], "z_axis": [0, 0, 1]})
        w.call("publish_interface", handle=h, name="slot_b",
               frame={"origin": [85, 20, 14], "z_axis": [0, 0, 1]})
        w.call("save_document", path=str(path))


def _child(path, name):
    """A child component publishing a single `mount` frame at its base center, which
    subscribes (mates) to a master slot."""
    from ankusdrive import Worker
    with Worker() as w:
        w.call("new_document", name=name)
        r = w.call("add_primitive", kind="box", w=10, d=10, h=10, name=name)
        w.call("publish_interface", handle=r["handle"], name="mount",
               frame={"origin": [5, 5, 0], "z_axis": [0, 0, 1]})
        w.call("save_document", path=str(path))


def test_scaffold_merges_unchanged():
    """scaffold_project lays out a project; merge_assembly consumes the scaffolded
    manifest UNCHANGED once the component files are built into components/."""
    from ankusdrive import Worker
    with tempfile.TemporaryDirectory() as td:
        layout = project.scaffold(
            td, "stack",
            components={"base": {"file": "components/base.FCStd"},
                        "cap": {"file": "components/cap.FCStd"}},
            instances=[{"component": "base", "name": "base", "placement": [0, 0, 0]},
                       {"component": "cap", "name": "cap", "placement": [0, 0, 20]}])
        _box(Path(layout["components_dir"]) / "base.FCStd", 40, 40, 10, "base")
        _box(Path(layout["components_dir"]) / "cap.FCStd", 40, 40, 4, "cap")
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=layout["manifest"])
        _check("scaffolded manifest merges ok", rep["ok"], True)
        _check("both parts in the BOM", len(rep["gates"]["bom"]), 2)


def test_master_skeleton_publishes_children_subscribe():
    """A master/skeleton publishes interface geometry that two child components
    subscribe to and mate against — top-down / skeleton-driven design through the
    existing publish/subscribe machinery, given a home by the project container."""
    from ankusdrive import Worker
    with tempfile.TemporaryDirectory() as td:
        layout = project.scaffold(
            td, "skeldrive", master="skeleton",
            components={"skeleton": {"file": "components/skeleton.FCStd"},
                        "child_a": {"file": "components/child_a.FCStd"},
                        "child_b": {"file": "components/child_b.FCStd"}},
            instances=[
                {"component": "skeleton", "name": "skeleton", "placement": [0, 0, 0]},
                {"component": "child_a", "name": "child_a",
                 "mate": {"child_iface": "mount", "parent": "skeleton",
                          "parent_iface": "slot_a",
                          "verify_align": {"child_iface": "mount",
                                           "parent_iface": "slot_a"}}},
                {"component": "child_b", "name": "child_b",
                 "mate": {"child_iface": "mount", "parent": "skeleton",
                          "parent_iface": "slot_b",
                          "verify_align": {"child_iface": "mount",
                                           "parent_iface": "slot_b"}}},
            ])
        cdir = Path(layout["components_dir"])
        _skeleton(cdir / "skeleton.FCStd")
        _child(cdir / "child_a.FCStd", "child_a")
        _child(cdir / "child_b.FCStd", "child_b")
        proj = project.load_project(layout["project_file"])
        _check("master slot validates as a real component",
               project.validate_project(proj, td), [])
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=layout["manifest"])
        _check("master+children merge ok", rep["ok"], True)
        _check("both children mate to the skeleton without interference",
               rep["gates"]["interference"], [])
        # interface_align_check returns the list of MISALIGNED pairs; empty == every
        # subscribed frame coincides with the master slot it mated to.
        _check("both subscribed frames verified aligned to the master slots",
               rep["gates"].get("interface_align"), [])


def test_item_ref_resolves_through_manifest_and_merges():
    """An item-ref component (the deferred #140 seam) resolves through the manifest:
    the worker's validate_manifest accepts the {item} source-kind, project_resolve_
    manifest lowers it to a file, and merge_assembly consumes the result green."""
    from ankusdrive import Worker
    with tempfile.TemporaryDirectory() as td:
        layout = project.scaffold(
            td, "byid",
            components={"brk": {"item": "bracket"}},
            instances=[{"component": "brk", "name": "brk", "placement": [0, 0, 0]}],
            items={"bracket": {"files": ["components/bracket.FCStd"]}})
        _box(Path(layout["components_dir"]) / "bracket.FCStd", 30, 20, 8, "bracket")
        with Worker() as w:
            vm = w.call("validate_manifest", manifest=layout["manifest"])
            _check("validate_manifest accepts the {item} source-kind", vm["ok"], True)
            res = w.call("project_resolve_manifest", project=layout["project_file"])
            _check("item-ref lowered to its file",
                   res["lowered"]["components"]["brk"]["file"],
                   "components/bracket.FCStd")
            rep = w.call("merge_assembly", manifest=res["path"])
        _check("resolved item-ref manifest merges ok", rep["ok"], True)
        _check("the item's part is in the BOM", len(rep["gates"]["bom"]), 1)


def test_reference_guard_catches_break_before_merge():
    """The pre-merge guard: a clean project passes its reference check and merges;
    moving the item's file makes the guard flag the broken reference BEFORE any
    merge is attempted (the chronic-PDM failure caught early)."""
    from ankusdrive import Worker
    with tempfile.TemporaryDirectory() as td:
        layout = project.scaffold(
            td, "guarded",
            components={"brk": {"item": "bracket"}},
            instances=[{"component": "brk", "name": "brk", "placement": [0, 0, 0]}],
            items={"bracket": {"files": ["components/bracket.FCStd"]}})
        fpath = Path(layout["components_dir"]) / "bracket.FCStd"
        _box(fpath, 30, 20, 8, "bracket")
        with Worker() as w:
            clean = w.call("project_check_references", project=layout["project_file"])
            _check("clean project passes the guard", clean["ok"], True)
            # now MOVE the file out from under the item-ref (the chronic PDM break)
            fpath.rename(Path(td) / "bracket.FCStd")
            broken = w.call("project_check_references", project=layout["project_file"])
        _check("moved file caught by the guard before merge", broken["ok"], False)
        _flags("guard names the missing file",
               broken["problems"], "missing on disk")


_PURE = [
    test_scaffold_lays_out_wellformed_project,
    test_scaffold_seeds_items_and_master,
    test_validate_project_catches_violations,
    test_reference_guard_clean_passes,
    test_reference_guard_flags_moved_file,
    test_reference_guard_flags_dangling_item,
    test_reference_guard_flags_naming_and_unknown_instance,
    test_lower_item_refs_resolves_and_dangles_loud,
]

_WORKER = [
    test_scaffold_merges_unchanged,
    test_master_skeleton_publishes_children_subscribe,
    test_item_ref_resolves_through_manifest_and_merges,
    test_reference_guard_catches_break_before_merge,
]


def main():
    print("== project container — scaffold + guard + master-skeleton (no API) ==")
    for t in _PURE + _WORKER:
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

"""
Typed-interface merge gates (RFC §11.2) — free, no LLM, no key. Proves the
gates merge_assembly dispatches by `kind` DISCRIMINATE: each typed contract
passes a scripted reference build and is caught on every scripted negative,
against the real merge_assembly primitive. Same M1 discipline as
test_multiagent_m1.py — never trust a gate you haven't shown catches a wrong
answer.

Kinds covered:
  bore_fit          — clearance band; CLOSES the exact-touch blind spot (§6:
                      interference reads zero for tangent solids, so a slip fit
                      must gate on min clearance, not non-interference).
  gear_mesh         — external pair; pitch radii sum to centre distance, placed
                      axes at that distance, ratio on target (gearbox oracle).
  frame_orientation — published frames angularly aligned, not just origin-
                      coincident (the gap interface_align does not cover).

Run: .venv/bin/python3 tests/test_typed_interfaces.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402


# --- scripted component builders --------------------------------------------

def _plate_with_hole(w, path, hole_d, name="plate"):
    w.call("new_document", name=name)
    w.call("add_primitive", kind="box", w=60, d=60, h=10, name="plate")
    w.call("add_primitive", kind="cylinder", r=hole_d / 2.0, h=30,
           placement=[30, 30, -10], name="bore")
    w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")
    w.call("save_document", path=str(path))


def _peg(w, path, peg_d, name="peg"):
    w.call("new_document", name=name)
    w.call("add_primitive", kind="cylinder", r=peg_d / 2.0, h=20, name="peg")
    w.call("save_document", path=str(path))


def _gear(w, path, teeth, module=2.0, name="gear"):
    w.call("new_document", name=name)
    w.call("add_gear", teeth=int(teeth), module=module, height=6, name="gear")
    w.call("save_document", path=str(path))


def _bored_gear(w, path, teeth, module=2.0):
    """A gear with a centre bore — a boolean cut, so its shape is a Part.Compound
    (whose .CenterOfMass is not directly available; the gate must be robust to it)."""
    w.call("new_document", name="g")
    g = w.call("add_gear", teeth=int(teeth), module=module, height=6, name="gear")
    b = w.call("add_primitive", kind="cylinder", r=5.2, h=18,
               placement=[0, 0, -6], name="bore")
    w.call("boolean_op", op="cut", base=g["handle"], tool=b["handle"])
    w.call("save_document", path=str(path))


def _plate_with_frame(w, path, z_axis, name="plate"):
    w.call("new_document", name=name)
    r = w.call("add_primitive", kind="box", w=40, d=40, h=8, name="plate")
    w.call("publish_interface", handle=r["handle"], name="seat",
           frame={"origin": [20, 20, 8], "z_axis": z_axis})
    w.call("save_document", path=str(path))


def _merge(tmp, components, instances, checks):
    """Write a manifest and run the real merge_assembly. Returns its report."""
    man = {"name": "typed", "root": "typed.FCStd",
           "components": components, "instances": instances, "checks": checks}
    mpath = tmp / "manifest.json"
    mpath.write_text(json.dumps(man))
    with Worker() as w:
        return w.call("merge_assembly", manifest=str(mpath))


# --- bore_fit ----------------------------------------------------------------

def test_bore_fit():
    HOLE = 16.0
    check = [{"kind": "bore_fit", "pin": "peg", "bore": "plate",
              "min_clearance_mm": 0.1, "max_clearance_mm": 0.5}]
    # (label, peg Ø, expect_ok, reason-substring)
    cases = [
        ("reference Ø15.6 (gap 0.2)", 15.6, True, None),
        ("exact-touch Ø16.0 (gap 0)", 16.0, False, "exact-touch"),  # the blind spot
        ("interference Ø16.4", 16.4, False, "too tight"),
        ("too loose Ø14.0 (gap 1.0)", 14.0, False, "too loose"),
    ]
    for label, peg_d, expect_ok, sub in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with Worker() as w:
                _plate_with_hole(w, tmp / "plate.FCStd", HOLE)
            with Worker() as w:
                _peg(w, tmp / "peg.FCStd", peg_d)
            rep = _merge(
                tmp,
                {"plate": {"file": "plate.FCStd"}, "peg": {"file": "peg.FCStd"}},
                [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                 {"component": "peg", "name": "peg", "placement": [30, 30, -5]}],
                check)
            typed = rep["gates"].get("typed", [])
            ok = not typed
            _assert_case("bore_fit", label, expect_ok, ok, typed, sub)


# --- gear_mesh ---------------------------------------------------------------

def test_gear_mesh():
    M, C = 2.0, 48.0   # module 2: teeth 12 (rp12) + 36 (rp36) sum to C=48
    # ratio is rp_b/rp_a (b relative to a, matching the gearbox out:in oracle):
    # a=12, b=36 -> 3.0.
    check = [{"kind": "gear_mesh", "a": "gearA", "b": "gearB",
              "module_mm": M, "center_distance_mm": C, "ratio": 3.0,
              "tol_mm": 0.5}]
    # (label, teethA, teethB, bx, expect_ok, sub)
    cases = [
        ("reference 12/36 @ C=48", 12, 36, 48.0, True, None),
        ("wrong teeth 12/40 (rp sum 52)", 12, 40, 48.0, False, "will not mesh"),
        ("placed at C=52 not 48", 12, 36, 52.0, False, "centre distance"),
        ("wrong ratio 24/24", 24, 24, 48.0, False, "ratio"),
    ]
    for label, ta, tb, bx, expect_ok, sub in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with Worker() as w:
                _gear(w, tmp / "a.FCStd", ta, M)
            with Worker() as w:
                _gear(w, tmp / "b.FCStd", tb, M)
            rep = _merge(
                tmp,
                {"gearA": {"file": "a.FCStd"}, "gearB": {"file": "b.FCStd"}},
                [{"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
                 {"component": "gearB", "name": "gearB", "placement": [bx, 0, 0]}],
                check)
            typed = rep["gates"].get("typed", [])
            ok = not typed
            _assert_case("gear_mesh", label, expect_ok, ok, typed, sub)


# --- frame_orientation -------------------------------------------------------

def test_frame_orientation():
    check = [{"kind": "frame_orientation", "child": "lid", "parent": "base",
              "child_iface": "seat", "parent_iface": "seat", "max_angle_deg": 1.0}]
    # (label, child frame z_axis, expect_ok, sub)
    cases = [
        ("reference aligned +Z", [0, 0, 1], True, None),
        ("tilted 0.1 in Y (~5.7°)", [0, 0.1, 1], False, "off"),
        ("flipped −Z (180°)", [0, 0, -1], False, "off"),
    ]
    for label, zc, expect_ok, sub in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with Worker() as w:
                _plate_with_frame(w, tmp / "base.FCStd", [0, 0, 1], "base")
            with Worker() as w:
                _plate_with_frame(w, tmp / "lid.FCStd", zc, "lid")
            rep = _merge(
                tmp,
                {"base": {"file": "base.FCStd"}, "lid": {"file": "lid.FCStd"}},
                [{"component": "base", "name": "base", "placement": [0, 0, 0]},
                 {"component": "lid", "name": "lid", "placement": [0, 0, 20]}],
                check)
            typed = rep["gates"].get("typed", [])
            ok = not typed
            _assert_case("frame_orientation", label, expect_ok, ok, typed, sub)


# --- unknown kind fails loudly ----------------------------------------------

def test_unknown_kind_is_violation():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _peg(w, tmp / "peg.FCStd", 10)
        rep = _merge(
            tmp, {"peg": {"file": "peg.FCStd"}},
            [{"component": "peg", "name": "peg", "placement": [0, 0, 0]}],
            [{"kind": "telepathy", "a": "peg"}])
        typed = rep["gates"].get("typed", [])
        assert typed and "unknown" in typed[0].get("error", ""), typed
        assert rep["ok"] is False
        print("  PASS unknown_kind -> violation, ok=False")


# --- merge ok-flip + back-compat --------------------------------------------

def test_ok_reflects_typed_and_no_checks_is_backcompat():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _plate_with_hole(w, tmp / "plate.FCStd", 16.0)
        with Worker() as w:
            _peg(w, tmp / "peg.FCStd", 16.4)  # interferes -> typed fail
        comps = {"plate": {"file": "plate.FCStd"}, "peg": {"file": "peg.FCStd"}}
        insts = [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                 {"component": "peg", "name": "peg", "placement": [30, 30, 20]}]
        # peg ABOVE the plate (z=20): no geometric interference, but bore_fit on
        # the (non-overlapping, far-apart) pair fails the clearance band -> ok=False
        rep = _merge(tmp, comps, insts,
                     [{"kind": "bore_fit", "pin": "peg", "bore": "plate",
                       "min_clearance_mm": 0.1, "max_clearance_mm": 0.5}])
        assert rep["ok"] is False and rep["gates"]["typed"], rep["gates"]
        # no `checks` at all -> no typed gate, ok decided by the classic gates only
        rep2 = _merge(tmp, comps, insts, [])
        assert "typed" not in rep2["gates"], rep2["gates"].keys()
        print("  PASS ok-flip on typed violation; absent checks = back-compat")


def test_mesh_exclusion_is_targeted():
    """A gear_mesh pair's overlapping teeth must NOT trip the interference gate
    (the typed gate owns that pair) — but a genuine collision with a THIRD part
    that no check covers must still be caught. Proves the exclusion is targeted,
    not a blanket interference suppressor."""
    M, C = 2.0, 48.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _gear(w, tmp / "a.FCStd", 12, M)   # rp12, tip14
        with Worker() as w:
            _gear(w, tmp / "b.FCStd", 36, M)   # rp36, tip38 — meshes at C=48
        with Worker() as w:
            _peg(w, tmp / "intruder.FCStd", 20)  # Ø20 bar, no check covers it
        comps = {"gearA": {"file": "a.FCStd"}, "gearB": {"file": "b.FCStd"},
                 "intruder": {"file": "intruder.FCStd"}}
        check = [{"kind": "gear_mesh", "a": "gearA", "b": "gearB",
                  "module_mm": M, "center_distance_mm": C, "tol_mm": 0.5}]
        # meshing pair alone: teeth overlap, but excluded -> ok True
        rep = _merge(
            tmp, comps,
            [{"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
             {"component": "gearB", "name": "gearB", "placement": [C, 0, 0]},
             {"component": "intruder", "name": "intruder", "placement": [0, 200, 0]}],
            check)
        assert rep["ok"], ("mesh pair should pass; interference="
                           f"{rep['gates']['interference']}")
        # now drive the intruder INTO gear A — uncovered pair, must be caught
        rep2 = _merge(
            tmp, comps,
            [{"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
             {"component": "gearB", "name": "gearB", "placement": [C, 0, 0]},
             {"component": "intruder", "name": "intruder", "placement": [0, 0, 0]}],
            check)
        clash = rep2["gates"]["interference"]
        assert not rep2["ok"] and clash, "intruder-into-gearA must be caught"
        assert all("intruder" in (r["a"], r["b"]) for r in clash), clash
        print("  PASS mesh-exclusion targeted: mesh pair excused, real clash caught")


def test_gear_mesh_bored_gears_compound():
    """Regression: bored gears are Part.Compound shapes, whose .CenterOfMass is not
    directly accessible. The gear_mesh gate must measure pitch radius + centre
    distance via the robust solids-based centre of mass, not crash on a compound."""
    M, C = 2.0, 48.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _bored_gear(w, tmp / "a.FCStd", 12, M)
        with Worker() as w:
            _bored_gear(w, tmp / "b.FCStd", 36, M)
        rep = _merge(
            tmp, {"gearA": {"file": "a.FCStd"}, "gearB": {"file": "b.FCStd"}},
            [{"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
             {"component": "gearB", "name": "gearB", "placement": [C, 0, 0]}],
            [{"kind": "gear_mesh", "a": "gearA", "b": "gearB", "module_mm": M,
              "center_distance_mm": C, "ratio": 3.0, "tol_mm": 0.5}])
        typed = rep["gates"].get("typed", [])
        _assert_case("gear_mesh", "bored gears (compound shape)", True,
                     not typed, typed, None)


def _assert_case(kind, label, expect_ok, ok, typed, sub):
    if ok != expect_ok:
        raise AssertionError(
            f"{kind} [{label}]: expected ok={expect_ok}, got ok={ok}  typed={typed}")
    if not expect_ok:
        reasons = " | ".join(v.get("reason", v.get("error", "")) for v in typed)
        if sub and sub not in reasons:
            raise AssertionError(
                f"{kind} [{label}]: caught but reason {reasons!r} lacks {sub!r}")
    tag = "PASS" if ok == expect_ok else "FAIL"
    detail = "clean" if ok else (typed[0].get("reason") or typed[0].get("error"))
    print(f"  {tag} {kind:18s} {label:34s} -> {'clean' if ok else 'caught: ' + detail}")


def main():
    tests = [test_bore_fit, test_gear_mesh, test_gear_mesh_bored_gears_compound,
             test_frame_orientation, test_mesh_exclusion_is_targeted,
             test_unknown_kind_is_violation,
             test_ok_reflects_typed_and_no_checks_is_backcompat]
    failed = 0
    t0 = time.time()
    print("== typed-interface gates — discrimination on scripted builds (no API) ==")
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n== {failed} typed-gate check(s) FAILED ==")
        sys.exit(1)
    print(f"\n== every typed contract passes its reference AND catches every "
          f"negative  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()

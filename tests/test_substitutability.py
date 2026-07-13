"""Liskov-substitutability gate (issue #147, T1; DESIGN_HIERARCHY §7.1) — free,
no LLM, no key. Proves the headline modularity test operationalizes Form/Fit/
Function as code, two-sided, against the REAL merge_assembly primitive and the
existing typed-interface fixtures:

  - swapping a SAME-interface variant into a slot keeps the assembly green
    (substitutable ⇒ a compatible MINOR/PATCH change ⇒ revise); and
  - swapping a deliberately OFF-interface variant FAILS, and the report NAMES the
    broken gate (interface break ⇒ a new part number).

Same M1 discipline as tests/test_typed_interfaces.py — never trust a gate you
haven't shown both passes its reference AND catches a wrong answer. Both sides go
through the single `substitutability_check` handler, which drives merge_assembly
twice (baseline + swap) and diffs the gate outcomes.

Run: .venv/bin/python3 tests/test_substitutability.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin.gates import substitutability as subst  # noqa: E402


# --- scripted component builders (mirror test_typed_interfaces.py) -----------

def _plate_with_hole(w, path, hole_d):
    w.call("new_document", name="plate")
    w.call("add_primitive", kind="box", w=60, d=60, h=10, name="plate")
    w.call("add_primitive", kind="cylinder", r=hole_d / 2.0, h=30,
           placement=[30, 30, -10], name="bore")
    w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")
    w.call("save_document", path=str(path))


def _peg(w, path, peg_d):
    w.call("new_document", name="peg")
    w.call("add_primitive", kind="cylinder", r=peg_d / 2.0, h=20, name="peg")
    w.call("save_document", path=str(path))


def _gear(w, path, teeth, module=2.0):
    w.call("new_document", name="gear")
    w.call("add_gear", teeth=int(teeth), module=module, height=6, name="gear")
    w.call("save_document", path=str(path))


def _write_manifest(tmp, components, instances, checks, name="base"):
    man = {"name": name, "root": str(tmp / f"{name}.FCStd"),
           "components": components, "instances": instances, "checks": checks}
    mpath = tmp / "manifest.json"
    mpath.write_text(json.dumps(man), encoding="utf-8")
    return mpath


# --- pure-unit checks (no FreeCAD) -------------------------------------------

def test_pure_helpers():
    """The pure logic (no FreeCAD): failing_gates mirrors merge_assembly's ok
    predicate; swap_manifest swaps exactly one slot; classify maps F3→semver."""
    # failing_gates: bom is informational (never a failure); list-gates + children.
    assert subst.failing_gates({"gates": {"interference": [], "bom": [{"x": 1}]}}) == {}
    fg = subst.failing_gates({"gates": {"typed": [{"reason": "nope"}],
                                        "children": {"sub": False, "ok": True}}})
    assert fg == {"typed": [{"reason": "nope"}], "children": ["sub"]}, fg

    base = {"components": {"a": {"file": "a.FCStd"}, "b": {"file": "b.FCStd"}},
            "instances": [{"component": "a", "name": "a"}], "checks": []}
    swapped = subst.swap_manifest(base, "a", {"file": "a2.FCStd"})
    assert swapped["components"]["a"] == {"file": "a2.FCStd"}
    assert swapped["components"]["b"] == {"file": "b.FCStd"}  # untouched
    assert base["components"]["a"] == {"file": "a.FCStd"}     # deep-copied, not mutated

    # a missing slot and a malformed variant are loud
    for bad in (lambda: subst.swap_manifest(base, "zzz", {"file": "x"}),
                lambda: subst.swap_manifest(base, "a", {"file": "x", "library": {}}),
                lambda: subst.swap_manifest(base, "a", {})):
        try:
            bad()
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    assert subst.classify(True)["decision"] == "revise existing part number"
    assert subst.classify(False)["semver"] == "MAJOR"
    print("  PASS pure helpers: failing_gates / swap_manifest / classify")


# --- bore_fit slot: a clearance interface ------------------------------------

def test_bore_fit_substitutability():
    """Base: plate (Ø16 bore) + reference peg Ø15.6 (gap 0.2) gates green on a
    bore_fit band [0.1, 0.5]. Swap the peg:
      same-interface Ø15.7 (gap 0.15, still in band) -> still green -> substitutable;
      off-interface  Ø16.4 (interference)            -> typed gate fails -> NOT."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _plate_with_hole(w, tmp / "plate.FCStd", 16.0)
        with Worker() as w:
            _peg(w, tmp / "pegA.FCStd", 15.6)      # reference (in band)
        with Worker() as w:
            _peg(w, tmp / "pegB_ok.FCStd", 15.7)   # same-interface variant
        with Worker() as w:
            _peg(w, tmp / "pegB_bad.FCStd", 16.4)  # off-interface (interferes)
        mpath = _write_manifest(
            tmp,
            {"plate": {"file": "plate.FCStd"}, "peg": {"file": "pegA.FCStd"}},
            [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
             {"component": "peg", "name": "peg", "placement": [30, 30, -5]}],
            [{"kind": "bore_fit", "pin": "peg", "bore": "plate",
              "min_clearance_mm": 0.1, "max_clearance_mm": 0.5}])

        with Worker() as w:
            ok = w.call("substitutability_check", manifest=str(mpath),
                        slot="peg", variant={"file": "pegB_ok.FCStd"})
            bad = w.call("substitutability_check", manifest=str(mpath),
                         slot="peg", variant={"file": "pegB_bad.FCStd"})

    _assert_substitutable("bore_fit", "Ø15.7 same-interface", ok)
    _assert_broken("bore_fit", "Ø16.4 interference", bad, "typed")


# --- gear_mesh slot: a meshing interface -------------------------------------

def test_gear_mesh_substitutability():
    """Base: gearA 12T + gearB 36T meshing at C=48 (ratio 3.0) gates green. Swap
    gearB:
      same-interface 36T (re-cut, same pitch radius) -> still green -> substitutable;
      off-interface  40T (rp sum 52 != 48)           -> won't mesh -> typed fails."""
    M, C = 2.0, 48.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _gear(w, tmp / "a.FCStd", 12, M)
        with Worker() as w:
            _gear(w, tmp / "bA.FCStd", 36, M)       # reference
        with Worker() as w:
            _gear(w, tmp / "bB_ok.FCStd", 36, M)    # same-interface variant
        with Worker() as w:
            _gear(w, tmp / "bB_bad.FCStd", 40, M)   # off-interface (won't mesh)
        mpath = _write_manifest(
            tmp,
            {"gearA": {"file": "a.FCStd"}, "gearB": {"file": "bA.FCStd"}},
            [{"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
             {"component": "gearB", "name": "gearB", "placement": [C, 0, 0]}],
            [{"kind": "gear_mesh", "a": "gearA", "b": "gearB", "module_mm": M,
              "center_distance_mm": C, "ratio": 3.0, "tol_mm": 0.5}])

        with Worker() as w:
            ok = w.call("substitutability_check", manifest=str(mpath),
                        slot="gearB", variant={"file": "bB_ok.FCStd"})
            bad = w.call("substitutability_check", manifest=str(mpath),
                         slot="gearB", variant={"file": "bB_bad.FCStd"})

    _assert_substitutable("gear_mesh", "36T same-interface", ok)
    _assert_broken("gear_mesh", "40T won't mesh", bad, "typed")


# --- off-interface that breaks a DIFFERENT gate (interference, not typed) -----

def test_off_interface_breaks_interference_gate():
    """The swap need not break the typed gate to be caught: a variant that is the
    right interface but the wrong SIZE collides with a neighbor and trips the
    blunt interference gate. Proves the gate names whichever gate actually broke,
    not just `typed`."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # two pegs side by side at x=0 and x=30; no checks (pure geometry gates)
        with Worker() as w:
            _peg(w, tmp / "left.FCStd", 10.0)
        with Worker() as w:
            _peg(w, tmp / "rightA.FCStd", 10.0)   # Ø10 -> 15mm gap, clears
        with Worker() as w:
            _peg(w, tmp / "rightB_ok.FCStd", 12.0)   # Ø12 -> still clears
        with Worker() as w:
            _peg(w, tmp / "rightB_bad.FCStd", 60.0)  # Ø60 (r30) -> overlaps left peg
        mpath = _write_manifest(
            tmp,
            {"left": {"file": "left.FCStd"}, "right": {"file": "rightA.FCStd"}},
            [{"component": "left", "name": "left", "placement": [0, 0, 0]},
             {"component": "right", "name": "right", "placement": [30, 0, 0]}],
            [])  # no typed checks: interference + envelope are the only gates

        with Worker() as w:
            ok = w.call("substitutability_check", manifest=str(mpath),
                        slot="right", variant={"file": "rightB_ok.FCStd"})
            bad = w.call("substitutability_check", manifest=str(mpath),
                         slot="right", variant={"file": "rightB_bad.FCStd"})

    _assert_substitutable("interference", "Ø12 same-interface", ok)
    _assert_broken("interference", "Ø60 collides", bad, "interference")


# --- baseline-not-green premise is reported, not silently compared ------------

def test_baseline_not_green_is_reported():
    """The gate is only meaningful over an assembly already green with variant A.
    If the base is NOT green, say so loudly (verdict 'baseline_not_green') rather
    than comparing against a broken baseline."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _plate_with_hole(w, tmp / "plate.FCStd", 16.0)
        with Worker() as w:
            _peg(w, tmp / "pegBad.FCStd", 16.4)   # interferes -> base NOT green
        with Worker() as w:
            _peg(w, tmp / "pegOther.FCStd", 15.6)
        mpath = _write_manifest(
            tmp,
            {"plate": {"file": "plate.FCStd"}, "peg": {"file": "pegBad.FCStd"}},
            [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
             {"component": "peg", "name": "peg", "placement": [30, 30, -5]}],
            [{"kind": "bore_fit", "pin": "peg", "bore": "plate",
              "min_clearance_mm": 0.1, "max_clearance_mm": 0.5}])
        with Worker() as w:
            rep = w.call("substitutability_check", manifest=str(mpath),
                         slot="peg", variant={"file": "pegOther.FCStd"})
    assert rep["verdict"] == "baseline_not_green", rep
    assert rep["substitutable"] is False and rep["baseline_ok"] is False, rep
    assert rep["baseline_failing"].get("typed"), rep
    print("  PASS baseline_not_green  premise violated -> reported, not compared")


# --- assertions --------------------------------------------------------------

def _assert_substitutable(slot, label, rep):
    assert rep["schema"] == subst.SCHEMA, rep
    if not rep["substitutable"]:
        raise AssertionError(
            f"{slot} [{label}]: expected substitutable, got broken_gates="
            f"{rep['broken_gates']}  broken={rep['broken']}")
    assert rep["verdict"] == "substitutable" and rep["broken_gates"] == [], rep
    assert rep["classification"]["semver"] == "MINOR/PATCH", rep
    print(f"  PASS {slot:14s} {label:28s} -> substitutable (revise)")


def _assert_broken(slot, label, rep, gate):
    if rep["substitutable"]:
        raise AssertionError(
            f"{slot} [{label}]: expected NOT substitutable, but it passed")
    assert rep["verdict"] == "not_substitutable", rep
    if gate not in rep["broken_gates"]:
        raise AssertionError(
            f"{slot} [{label}]: broke, but the named gate(s) {rep['broken_gates']} "
            f"do not include {gate!r}")
    assert rep["classification"]["decision"] == "new part number", rep
    print(f"  PASS {slot:14s} {label:28s} -> NOT substitutable "
          f"(broke {rep['broken_gates']} -> new part number)")


def main():
    tests = [test_pure_helpers, test_bore_fit_substitutability,
             test_gear_mesh_substitutability,
             test_off_interface_breaks_interference_gate,
             test_baseline_not_green_is_reported]
    failed = 0
    t0 = time.time()
    print("== Liskov substitutability gate — F3 as code, two-sided (no API) ==")
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n== {failed} substitutability check(s) FAILED ==")
        sys.exit(1)
    print(f"\n== same-interface variants stay green; off-interface variants fail "
          f"and name the broken gate  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()

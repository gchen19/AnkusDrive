"""
Revision + lifecycle state machine + the Form/Fit/Function predicate (issue #141,
C2 — docs/DESIGN_HIERARCHY.md §2 Theme C) — free, no LLM, no key, no FreeCAD.
C2 is pure data/text (the state machine, the rev counter, and the F3 predicate
on items.json), so this suite imports driftpin.lifecycle directly and runs fast.

Two-sided gate-validated (the house standard, MULTI_AGENT.md §11.x):

  reference (must pass)
    - an item walks the legal path in_work -> in_review -> released and freezes
    - the F3 predicate classifies an interface-preserving edit as *revise* and an
      interface-breaking edit as *new part number* (both legs)
    - releasing stamps the first revision (- -> A); a revise bumps it (A -> B)

  negative controls (each must be caught)
    - an illegal transition (skip review: in_work -> released) is rejected
    - editing a Released item is rejected (immutability)
    - apply_change demands a new part number for an F3-breaking change

Mirrors tests/test_items.py. Run: python3 tests/test_lifecycle.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import items as I       # noqa: E402
from driftpin import lifecycle as L   # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "lifecycle"

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _has(label, problems, substr):
    _check(label, any(substr in pp for pp in problems), True)


def _raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True


# --- reference: the legal lifecycle walk (must pass) -------------------------

def test_legal_lifecycle_walk_and_freeze():
    reg = I.empty_registry()
    I.new_item(reg, "part", files=["p.FCStd"], metadata={"bore_dia_mm": 10.0})
    _check("new item starts in_work", L.item_state(reg, "part"), "in_work")
    _check("new item starts at no-rev", reg["items"]["part"]["rev"], "-")
    _check("in_work is editable", L.item_editable(reg, "part"), True)

    # submit for review -> editing is now frozen
    L.transition(reg, "part", "in_review", actor="agent-a")
    _check("transitioned to in_review", L.item_state(reg, "part"), "in_review")
    _check("in_review is NOT editable", L.item_editable(reg, "part"), False)

    # release -> stamps the first revision and freezes the item
    L.transition(reg, "part", "released", note="approved")
    _check("transitioned to released", L.item_state(reg, "part"), "released")
    _check("release stamps first revision A", reg["items"]["part"]["rev"], "A")
    _check("released is NOT editable", L.item_editable(reg, "part"), False)

    # the transition log recorded the walk deterministically (no timestamps)
    log = L.get_log(reg, "part")
    _check("log records both edges", [e["to"] for e in log],
           ["in_review", "released"])
    _check("log records the release action", log[-1]["action"], "release")
    # the registry still validates under C1 + the lifecycle coherence checks
    _check("registry still C1-valid", I.validate_registry(reg), [])
    _check("registry lifecycle-coherent", L.validate_lifecycle(reg), [])


def test_open_revision_off_released():
    reg = I.load_registry(FIXTURES / "items_lifecycle.json")
    # 'cover' is released at Rev A; opening a new revision bumps to B, in_work
    pn_before = reg["items"]["cover"]["part_number"]
    L.open_revision(reg, "cover", note="add internal rib")
    _check("open_revision bumps rev A -> B", reg["items"]["cover"]["rev"], "B")
    _check("open_revision reopens as in_work", L.item_state(reg, "cover"), "in_work")
    _check("open_revision keeps the SAME part number",
           reg["items"]["cover"]["part_number"], pn_before)
    _check("reopened item is editable again", L.item_editable(reg, "cover"), True)


# --- the Form/Fit/Function predicate, both ways (must pass) -------------------

def test_f3_predicate_revise_vs_new_part_number():
    # an interface-PRESERVING edit (internal wall thickness / rib) => revise
    before = {"bore_dia_mm": 47.0, "bolt_circle_mm": 80.0,
              "wall_thickness_mm": 4.0, "rib_count": 6}
    after_internal = dict(before, wall_thickness_mm=5.0, rib_count=8)
    v = L.form_fit_function(before, after_internal)
    _check("internal-only change classifies as revise", v["disposition"], "revise")
    _check("internal change is not F3", v["f3"], False)
    _check("internal change lists the changed keys",
           v["changed"], ["rib_count", "wall_thickness_mm"])
    _check("classify_change terse API agrees (revise)",
           L.classify_change(before, after_internal), "revise")

    # an interface-BREAKING edit (mating bore diameter) => new part number
    after_iface = dict(before, bore_dia_mm=50.0)
    v2 = L.form_fit_function(before, after_iface)
    _check("interface change classifies as new_part_number",
           v2["disposition"], "new_part_number")
    _check("interface change IS F3", v2["f3"], True)
    _check("interface change reports the F3 leg (fit)",
           v2["categories"], {"bore_dia_mm": "fit"})
    _check("classify_change terse API agrees (new_part_number)",
           L.classify_change(before, after_iface), "new_part_number")

    # form (mass/envelope) and function (ratio) legs also break interchangeability
    _check("form change (mass) is F3",
           L.classify_change(before, dict(before, mass_g=999.0)), "new_part_number")
    _check("function change (ratio) is F3",
           L.classify_change({"ratio": 1.0}, {"ratio": 2.0}), "new_part_number")
    # no change at all is a noop
    _check("identical attrs => noop", L.classify_change(before, dict(before)), "noop")


def test_apply_change_drives_revise_and_renumber():
    reg = I.load_registry(FIXTURES / "items_lifecycle.json")
    # 'cover' is released Rev A. An F3-preserving change => revise, same PN, rev B.
    pn = reg["items"]["cover"]["part_number"]
    after_ok = dict(reg["items"]["cover"]["metadata"], wall_thickness_mm=4.0)
    res = L.apply_change(reg, "cover", after_ok, note="thicker wall")
    _check("apply_change revise disposition", res["disposition"], "revise")
    _check("apply_change revise keeps part number", res["part_number"], pn)
    _check("apply_change revise bumps to B", res["rev"], "B")
    _check("apply_change revise reopens in_work", L.item_state(reg, "cover"), "in_work")

    # an F3-breaking change on a released item => a NEW part number / new item
    reg2 = I.load_registry(FIXTURES / "items_lifecycle.json")
    next_before = reg2["part_number_format"]["next"]
    after_break = dict(reg2["items"]["cover"]["metadata"], bore_dia_mm=50.0)
    res2 = L.apply_change(reg2, "cover", after_break, new_item_id="cover_b50")
    _check("apply_change renumber disposition", res2["disposition"], "new_part_number")
    _check("apply_change renumber allocates a NEW part number",
           res2["part_number"] != reg2["items"]["cover"]["part_number"], True)
    _check("apply_change renumber advanced the PN counter",
           reg2["part_number_format"]["next"], next_before + 1)
    _check("new item records what it supersedes",
           reg2["items"]["cover_b50"]["metadata"]["supersedes"], "cover")
    _check("original released item is left untouched",
           L.item_state(reg2, "cover"), "released")
    _check("renumbered registry still C1-valid", I.validate_registry(reg2), [])


# --- negative controls (each must be caught) ---------------------------------

def test_illegal_transition_skip_review_rejected():
    reg = I.empty_registry()
    I.new_item(reg, "part", files=["p.FCStd"])
    # in_work -> released skips the mandatory review step
    _check("skip-review transition raises",
           _raises(lambda: L.transition(reg, "part", "released"), L.LifecycleError),
           True)
    # and can_transition reports it as illegal without raising
    _check("can_transition(in_work, released) is False",
           L.can_transition("in_work", "released"), False)
    # an unknown target state is also rejected loudly
    _check("unknown target state raises",
           _raises(lambda: L.transition(reg, "part", "shipped"), L.LifecycleError),
           True)
    # the item never moved
    _check("rejected item still in_work", L.item_state(reg, "part"), "in_work")


def test_editing_released_item_rejected():
    reg = I.load_registry(FIXTURES / "items_lifecycle.json")
    # 'cover' is released => immutable
    _check("released item is not editable", L.item_editable(reg, "cover"), False)
    _check("assert_editable on released raises",
           _raises(lambda: L.assert_editable(reg["items"]["cover"]), L.LifecycleError),
           True)
    # the only legal move off released is retire (-> obsolete); reopening in place
    # (released -> in_work) is rejected — you must open_revision instead
    _check("released -> in_work rejected",
           _raises(lambda: L.transition(reg, "cover", "in_work"), L.LifecycleError),
           True)
    _check("released -> obsolete (retire) allowed",
           L.can_transition("released", "obsolete"), True)
    # apply_change on a non-released item is rejected (use assert_editable instead)
    _check("apply_change on in_work item rejected",
           _raises(lambda: L.apply_change(reg, "housing", {}), L.LifecycleError),
           True)


def test_f3_break_demands_new_part_number():
    reg = I.load_registry(FIXTURES / "items_lifecycle.json")
    after_break = dict(reg["items"]["cover"]["metadata"], bolt_circle_mm=90.0)
    # an F3-breaking change with no new_item_id supplied must fail loudly
    _check("F3-breaking change without new_item_id rejected",
           _raises(lambda: L.apply_change(reg, "cover", after_break), ValueError),
           True)


def test_validate_lifecycle_catches_released_without_rev():
    reg = I.load_registry(FIXTURES / "items_lifecycle.json")
    # force an incoherent state: released but rev reset to the no-rev sentinel
    reg["items"]["cover"]["rev"] = "-"
    _has("released-without-revision flagged",
         L.validate_lifecycle(reg), "released but has no revision")


def test_bump_rev_alpha_and_numeric():
    _check("bump - -> A", L.bump_rev("-"), "A")
    _check("bump A -> B", L.bump_rev("A"), "B")
    _check("bump Z -> AA", L.bump_rev("Z"), "AA")
    _check("bump AZ -> BA", L.bump_rev("AZ"), "BA")
    _check("bump 1.0 -> 1.1", L.bump_rev("1.0"), "1.1")
    _check("bump 2.9 -> 2.10", L.bump_rev("2.9"), "2.10")


def main():
    print("== revision + lifecycle state machine + F3 predicate (C2, #141) "
          "— no API, no FreeCAD ==")
    for t in (test_legal_lifecycle_walk_and_freeze,
              test_open_revision_off_released,
              test_f3_predicate_revise_vs_new_part_number,
              test_apply_change_drives_revise_and_renumber,
              test_illegal_transition_skip_review_rejected,
              test_editing_released_item_rejected,
              test_f3_break_demands_new_part_number,
              test_validate_lifecycle_catches_released_without_rev,
              test_bump_rev_alpha_and_numeric):
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

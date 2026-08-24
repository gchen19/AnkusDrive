"""
Feature templates (issue #139, B2 — PowerCopy/UDF analog) — declared-input
feature recipes stamped onto reference geometry BY NAME. Free, no LLM, no key.
Two halves:

  * PURE (no FreeCAD): the FROZEN contract — the ref+input schema matches the
    golden fixture; the structural door discriminates a well-formed instantiation
    from every malformed one (unknown template, unknown/missing reference, a
    malformed reference value, and every scalar-input failure recipes already
    catches — missing required, out of range, wrong type, bad unit, unknown key);
    the typed-units layer accepts a unit-bearing length on a scalar input.

  * WORKER (FreeCAD): a template registered ONCE is instantiated TWICE onto two
    different reference frames published on one host body — correct geometry both
    times (a boss of the supplied diameter appears at each seat), deterministic
    across two independent workers; a reference supplied by NAME (a published
    interface, an f_ face tag) resolves against the host's current geometry; a
    reference tag / interface name that does NOT resolve fails LOUDLY.

Run: python3 tests/test_feature_templates.py   (worker half needs freecadcmd)
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import feature_templates as ft  # noqa: E402

FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "feature_mounting_boss.json").read_text(encoding="utf-8"))

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _rejects(label, template, refs, inputs, substr):
    """Assert the structural door flags an instantiation mentioning substr."""
    problems = ft.validate_instantiation(
        {"template": template, "refs": refs, "inputs": inputs})
    ok = bool(problems) and any(substr in p for p in problems)
    _check(label, ok, True)
    if not ok:
        print(f"       (problems were: {problems})")


# =============================================================================
# PURE — the frozen contract + the structural door (no FreeCAD)
# =============================================================================

def test_schema_matches_golden_fixture():
    _check("feature_schema(mounting_boss) == golden fixture",
           ft.feature_schema("mounting_boss"), FIXTURE["feature_schema"])
    _check("feature_schema(bolt_pattern) == golden fixture",
           ft.feature_schema("bolt_pattern"), FIXTURE["bolt_schema"])
    _check("contract schema stamp", ft.SCHEMA, "ankusdrive.feature_template/1")


def test_valid_instantiation_passes():
    _check("well-formed instantiation -> no problems",
           ft.validate_instantiation(FIXTURE["instantiation"]), [])
    resolved = ft.validate_inputs("mounting_boss",
                                  FIXTURE["instantiation"]["inputs"])
    _check("resolved fills the optional default bore",
           resolved, {"boss_dia_mm": 12.0, "height_mm": 8.0, "bore_dia_mm": 5.0})


def test_unknown_template_is_loud():
    _rejects("unknown template -> loud", "no_such_template",
             {"seat": "s"}, {"boss_dia_mm": 12.0}, "unknown feature template")


def test_missing_required_reference_is_loud():
    _rejects("missing required reference -> loud", "mounting_boss",
             {}, {"boss_dia_mm": 12.0}, "missing required reference")


def test_unknown_reference_key_is_loud():
    _rejects("unknown reference key -> loud", "mounting_boss",
             {"seat": "s", "bogus": "x"}, {"boss_dia_mm": 12.0},
             "unknown reference")


def test_malformed_reference_value_is_loud():
    _rejects("frame ref that is neither name/tag/dict -> loud", "mounting_boss",
             {"seat": 123}, {"boss_dia_mm": 12.0}, "seat")
    _rejects("literal frame missing origin -> loud", "mounting_boss",
             {"seat": {"z_axis": [0, 0, 1]}}, {"boss_dia_mm": 12.0}, "origin")


def test_scalar_input_failures_are_loud():
    _rejects("out-of-range scalar -> loud", "mounting_boss",
             {"seat": "s"}, {"boss_dia_mm": 9999.0}, "above the maximum")
    _rejects("unknown scalar key -> loud", "mounting_boss",
             {"seat": "s"}, {"boss_dia_mm": 12.0, "nope": 1}, "unknown input")
    _rejects("non-integer count -> loud", "bolt_pattern",
             {"frame": "s"}, {"circle_dia_mm": 40.0, "count": 4.5},
             "expected an integer")
    _rejects("thread off the allow-list -> loud", "bolt_pattern",
             {"frame": "s"}, {"circle_dia_mm": 40.0, "count": 4,
                              "thread": "M99"}, "not in allowed")


def test_typed_units_on_a_scalar_input():
    resolved = ft.validate_inputs(
        "mounting_boss", {"boss_dia_mm": "0.5 in"})
    _check("0.5 in -> 12.7 mm canonical", round(resolved["boss_dia_mm"], 4), 12.7)
    _rejects("a force given for a length scalar -> refused", "mounting_boss",
             {"seat": "s"}, {"boss_dia_mm": "5 N"}, "boss_dia_mm")


def test_non_object_instantiation_is_loud():
    _check("non-object instantiation flagged",
           bool(ft.validate_instantiation("nope")), True)
    _check("missing 'template' key flagged",
           bool(ft.validate_instantiation({"refs": {}})), True)


# =============================================================================
# WORKER — real geometry, name/tag resolution, determinism (needs FreeCAD)
# =============================================================================

def _make_host(w):
    """An 80x40x10 plate with two seat frames published on its top face."""
    w.call("new_document", name="ft")
    box = w.call("add_primitive", kind="box", w=80, d=40, h=10)["handle"]
    w.call("publish_interface", handle=box, name="seat_a",
           frame={"origin": [20, 20, 10], "z_axis": [0, 0, 1], "x_axis": [1, 0, 0]})
    w.call("publish_interface", handle=box, name="seat_b",
           frame={"origin": [60, 20, 10], "z_axis": [0, 0, 1], "x_axis": [1, 0, 0]})
    return box


def _summary(w, res):
    mp = w.call("mass_properties", handle=res["handle"], density=1e-6)
    return {
        "volume": round(mp["volume_mm3"], 3),
        "area": round(mp["surface_area_mm2"], 3),
        "cg": tuple(round(c, 3) for c in mp["center_of_mass_mm"]),
        "interfaces": tuple(res["interfaces"]),
        "intent": tuple(sorted(res["intent"].items())),
    }


def _stamp_two(w):
    """Stamp mounting_boss onto seat_a (dia 12) and seat_b (dia 16) of one host."""
    host = _make_host(w)
    ra = w.call("feature_instantiate", template="mounting_boss", host=host,
                refs={"seat": "seat_a"}, inputs={"boss_dia_mm": 12.0,
                                                 "height_mm": 8.0})
    rb = w.call("feature_instantiate", template="mounting_boss", host=host,
                refs={"seat": "seat_b"}, inputs={"boss_dia_mm": 16.0,
                                                 "height_mm": 10.0})
    return ra, rb


def _has_cyl_face(w, handle, radius):
    faces = w.call("query_faces", handle=handle,
                   predicate={"kind": "cylindrical", "radius_eq": radius,
                              "radius_tol": 0.01})
    return len(faces) >= 1


def test_instantiate_twice_on_one_body():
    """The DoD: a template registered ONCE, instantiated TWICE onto two different
    reference frames on one body — correct geometry both times."""
    from ankusdrive import Worker
    with Worker() as w:
        ra, rb = _stamp_two(w)
        # Correct geometry both times: a boss wall of the SUPPLIED diameter exists.
        a_boss = _has_cyl_face(w, ra["handle"], 6.0)    # dia 12 -> r6
        a_bore = _has_cyl_face(w, ra["handle"], 2.5)    # default bore dia 5 -> r2.5
        b_boss = _has_cyl_face(w, rb["handle"], 8.0)    # dia 16 -> r8
        # The two stamps land at the two distinct seats (boss_top origins differ).
        ia = w.call("get_interface", handle=ra["handle"], name="boss_top")["frame"]
        ib = w.call("get_interface", handle=rb["handle"], name="boss_top")["frame"]
        sa = _summary(w, ra)
    _check("seat_a boss has the dia-12 wall (r6)", a_boss, True)
    _check("seat_a boss has the dia-5 bore (r2.5)", a_bore, True)
    _check("seat_b boss has the dia-16 wall (r8)", b_boss, True)
    _check("boss_top on seat_a at [20,20,18]",
           [round(c, 3) for c in ia["origin"]], [20.0, 20.0, 18.0])
    _check("boss_top on seat_b at [60,20,20]",
           [round(c, 3) for c in ib["origin"]], [60.0, 20.0, 20.0])
    _check("each stamp publishes boss_top", "boss_top" in sa["interfaces"], True)
    _check("each stamp declares watertight", sa["intent"],
           (("watertight", True),))
    _check("a stamped boss really adds material (vol > bare 32000 plate)",
           sa["volume"] > 32000.0, True)


def test_instantiation_is_deterministic_two_workers():
    """Same host + inputs -> byte-equivalent geometry across two independent
    workers (rides the determinism envelope #123/#127)."""
    from ankusdrive import Worker
    with Worker() as wa:
        ra1, rb1 = _stamp_two(wa)
        sa1, sb1 = _summary(wa, ra1), _summary(wa, rb1)
    with Worker() as wb:
        ra2, rb2 = _stamp_two(wb)
        sa2, sb2 = _summary(wb, ra2), _summary(wb, rb2)
    _check("seat_a stamp identical across workers", sa1, sa2)
    _check("seat_b stamp identical across workers", sb1, sb2)


def test_reference_by_f_tag_resolves():
    """A reference supplied as an f_ face tag (no UI picking) reduces to a frame at
    the face's centroid+normal and stamps a bolt pattern there."""
    from ankusdrive import Worker
    with Worker() as w:
        host = _make_host(w)
        top = [f for f in w.call("query_faces", handle=host,
                                 predicate={"kind": "planar",
                                            "normal_dir": [0, 0, 1]})]
        tag = top[0]["tag"]
        res = w.call("feature_instantiate", template="bolt_pattern", host=host,
                     refs={"frame": tag},
                     inputs={"circle_dia_mm": 24.0, "count": 4,
                             "hole_dia_mm": 4.0, "depth_mm": 6.0})
        bore = _has_cyl_face(w, res["handle"], 2.0)  # hole dia 4 -> r2
        sumr = _summary(w, res)
    _check("bolt_pattern resolved an f_ tag to a frame", res["template"],
           "bolt_pattern")
    _check("a dia-4 hole wall (r2) exists", bore, True)
    _check("publishes a bolt_circle interface",
           "bolt_circle" in sumr["interfaces"], True)
    _check("drilled holes remove material (vol < bare 32000 plate)",
           sumr["volume"] < 32000.0, True)


def test_unresolvable_reference_is_loud():
    """A face tag that doesn't resolve, and an unpublished interface name, each
    fail loudly at instantiation — the DoD's loud half."""
    from ankusdrive import Worker
    from ankusdrive.client import WorkerError
    with Worker() as w:
        host = _make_host(w)
        bad_tag = False
        try:
            w.call("feature_instantiate", template="mounting_boss", host=host,
                   refs={"seat": "f_deadbeef0000"},
                   inputs={"boss_dia_mm": 12.0})
        except WorkerError:
            bad_tag = True
        bad_name = False
        try:
            w.call("feature_instantiate", template="mounting_boss", host=host,
                   refs={"seat": "no_such_seat"}, inputs={"boss_dia_mm": 12.0})
        except WorkerError:
            bad_name = True
    _check("an f_ tag that doesn't resolve -> loud", bad_tag, True)
    _check("an unpublished interface name -> loud", bad_name, True)


def test_worker_surface_handlers():
    """feature_list / feature_schema / feature_validate are exposed and
    discriminate good from bad."""
    from ankusdrive import Worker
    with Worker() as w:
        lst = w.call("feature_list")
        sch = w.call("feature_schema", template="mounting_boss")
        good = w.call("feature_validate", template="mounting_boss",
                      refs={"seat": "seat_a"}, inputs={"boss_dia_mm": 12.0})
        bad = w.call("feature_validate", template="mounting_boss",
                     refs={"seat": "seat_a"}, inputs={"boss_dia_mm": 9999.0})
    _check("feature_list lists mounting_boss",
           "mounting_boss" in lst["templates"], True)
    _check("feature_list lists bolt_pattern",
           "bolt_pattern" in lst["templates"], True)
    _check("feature_schema matches fixture", sch, FIXTURE["feature_schema"])
    _check("feature_validate ok on good inputs", good["ok"], True)
    _check("feature_validate loud on bad inputs", bad["ok"], False)


_PURE = [
    test_schema_matches_golden_fixture,
    test_valid_instantiation_passes,
    test_unknown_template_is_loud,
    test_missing_required_reference_is_loud,
    test_unknown_reference_key_is_loud,
    test_malformed_reference_value_is_loud,
    test_scalar_input_failures_are_loud,
    test_typed_units_on_a_scalar_input,
    test_non_object_instantiation_is_loud,
]

_WORKER = [
    test_instantiate_twice_on_one_body,
    test_instantiation_is_deterministic_two_workers,
    test_reference_by_f_tag_resolves,
    test_unresolvable_reference_is_loud,
    test_worker_surface_handlers,
]


def main():
    print("== feature templates — frozen contract + structural door (no API) ==")
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

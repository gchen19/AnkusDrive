"""
Interface-type registry + conformance gate (issue #146, RFC §6.3) — free, no LLM,
no key. A part DECLARES conformance to a named, versioned interface type
(nema17_face@1, bore_h7@1) — the mechanical `implements SomeInterface` — and a
conformance check gates that claim against the type's contract, measured from the
REAL geometry through the same merge_assembly machinery the §11.2 typed gates use.

Two-sided, the house M1 discipline (mirrors test_typed_interfaces.py): every
positive passes a scripted reference build AND every negative is caught —

  * a part implementing nema17_face@1 / bore_h7@1 PASSES conformance;
  * an off-spec part (wrong pilot, oversize bore) FAILS;
  * an UNKNOWN interface type is itself a violation — fails LOUDLY, never a silent
    pass (the §6.3 row: same discipline as an unknown typed-gate kind);
  * a BUS — one registry entry (bore_h7@1) referenced by >=2 parts — gates green
    for every attached part.

Plus the PURE layer (no FreeCAD): the registry directory/schema, the
type-id door (name@version), the conformance predicate over scripted features,
and the implements -> checks lowering seam.

Run: .venv/bin/python3 tests/test_iface_registry.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import iface_registry as ir  # noqa: E402

THK = 6.0  # plate thickness (mm)


# --- PURE layer (no FreeCAD) -------------------------------------------------

def test_registry_directory_and_schema():
    d = ir.list_types()
    assert d["schema"] == "driftpin.iface/1", d
    assert "bore_h7@1" in d["types"] and "nema17_face@1" in d["types"], d
    sch = ir.type_schema("nema17_face@1")
    assert sch["name"] == "nema17_face" and sch["version"] == 1, sch
    assert sch["iface"] == "nema17_face" and "contract" in sch, sch
    print("  PASS registry directory + per-type schema")


def test_type_id_door():
    assert ir.parse_ref("bore_h7@1") == ("bore_h7", 1)
    for bad in ("bore_h7", "bore_h7@", "bore_h7@x", "@1", "1@1", 7):
        try:
            ir.parse_ref(bad)
        except ir.IfaceError:
            continue
        raise AssertionError(f"malformed id {bad!r} was not rejected")
    print("  PASS type-id door: name@version enforced, malformed rejected")


def test_unknown_type_is_a_violation_not_a_raise():
    # well-formed but unregistered -> a loud VIOLATION (never a silent pass)
    v = ir.check_conformance("no_such_iface@1", [])
    assert v and "unknown interface type" in v[0]["error"], v
    # malformed -> also a violation (carries the syntactic error)
    v2 = ir.check_conformance("nope", [])
    assert v2 and "error" in v2[0], v2
    print("  PASS unknown / malformed type -> conformance violation, not a pass")


def test_conformance_predicate_over_scripted_features():
    # bore_h7@1: a central Ø8.008 bore conforms; Ø8.1 and Ø9 do not; none -> fail
    ok = [{"dia_mm": 8.01, "x_mm": 0, "y_mm": 0, "center_r_mm": 0.0}]
    assert ir.check_conformance("bore_h7@1", ok) == []
    for bad in ([{"dia_mm": 8.1, "x_mm": 0, "y_mm": 0, "center_r_mm": 0.0}],
                [{"dia_mm": 9.0, "x_mm": 0, "y_mm": 0, "center_r_mm": 0.0}],
                []):
        assert ir.check_conformance("bore_h7@1", bad), bad
    # nema17_face@1: pilot + 4 holes conforms
    feats = [{"dia_mm": 22.02, "x_mm": 0, "y_mm": 0, "center_r_mm": 0.0}]
    for sx in (1, -1):
        for sy in (1, -1):
            feats.append({"dia_mm": 3.0, "x_mm": sx * 15.5, "y_mm": sy * 15.5,
                          "center_r_mm": 21.92})
    assert ir.check_conformance("nema17_face@1", feats) == [], feats
    # drop a hole -> mounting_holes violation; wrong pilot -> pilot violation
    assert any(v.get("feature") == "mounting_holes"
               for v in ir.check_conformance("nema17_face@1", feats[:-1]))
    bad_pilot = [{"dia_mm": 21.0, "x_mm": 0, "y_mm": 0, "center_r_mm": 0.0}] + feats[1:]
    assert any(v.get("feature") == "pilot"
               for v in ir.check_conformance("nema17_face@1", bad_pilot))
    print("  PASS conformance predicate discriminates on scripted features")


def test_lowering_implements_to_checks():
    man = {
        "name": "m", "components": {
            "motor": {"file": "motor.FCStd", "implements": ["nema17_face@1"]},
            "shaftA": {"file": "a.FCStd"},
            "shaftB": {"file": "b.FCStd"}},
        "instances": [
            {"component": "motor", "name": "motor"},
            {"component": "shaftA", "name": "a", "implements": ["bore_h7@1"]},
            {"component": "shaftB", "name": "b", "implements": ["bore_h7@1"]}],
    }
    low = ir.lower_manifest(man)
    confs = [c for c in low["checks"] if c["kind"] == "interface_conformance"]
    got = {(c["part"], c["type"]) for c in confs}
    assert got == {("motor", "nema17_face@1"), ("a", "bore_h7@1"),
                   ("b", "bore_h7@1")}, got
    assert all(c.get("iface") for c in confs), confs            # locating frame set
    # idempotent: re-lowering adds no duplicate checks
    assert ir.lower_manifest(low)["checks"] == low["checks"]
    # a malformed declared id is loud at the door
    try:
        ir.lower_manifest({"components": {"x": {"file": "x", "implements": ["bore_h7"]}},
                           "instances": [{"component": "x", "name": "x"}]})
    except ir.IfaceError:
        pass
    else:
        raise AssertionError("malformed implements id was not rejected at lowering")
    print("  PASS implements -> interface_conformance lowering (seam, idempotent, loud)")


# --- scripted component builders (FreeCAD) -----------------------------------

def _bored_plate(w, path, side, bore_r, frame_name, holes=()):
    """A square plate, centred on the origin, with a central bore (Ø=2*bore_r) and
    optional extra holes [(x, y, r), ...]. Publishes the locating interface frame
    on the part's axis. The bore/holes are real cylindrical cuts, so the conformance
    gate reads their circular edges from the as-built shape."""
    w.call("new_document", name="p")
    box = w.call("add_primitive", kind="box", w=side, d=side, h=THK,
                 placement=[-side / 2.0, -side / 2.0, 0], name="plate")
    solid = box["handle"]
    bore = w.call("add_primitive", kind="cylinder", r=bore_r, h=THK + 10,
                  placement=[0, 0, -5], name="bore")
    solid = w.call("boolean_op", op="cut", base=solid, tool=bore["handle"])["handle"]
    for (hx, hy, hr) in holes:
        h = w.call("add_primitive", kind="cylinder", r=hr, h=THK + 10,
                   placement=[hx, hy, -5], name="hole")
        solid = w.call("boolean_op", op="cut", base=solid, tool=h["handle"])["handle"]
    w.call("publish_interface", handle=solid, name=frame_name,
           frame={"origin": [0, 0, 0], "z_axis": [0, 0, 1]})
    w.call("save_document", path=str(path))


def _nema17_holes():
    return [(sx * 15.5, sy * 15.5, 1.5) for sx in (1, -1) for sy in (1, -1)]


# --- merge harness (the real merge_assembly) ---------------------------------

def _merge(tmp, components, instances, checks):
    from driftpin import Worker
    man = {"name": "iface", "root": "iface.FCStd",
           "components": components, "instances": instances, "checks": checks}
    mpath = tmp / "manifest.json"
    mpath.write_text(json.dumps(man))
    with Worker() as w:
        return w.call("merge_assembly", manifest=str(mpath))


def _conf_violations(rep):
    return [v for v in rep["gates"].get("typed", [])
            if v.get("kind") == "interface_conformance"]


# --- bore_h7@1 conformance (reference + negatives) ---------------------------

def test_bore_h7_conformance():
    from driftpin import Worker
    cases = [
        ("reference Ø8.008 H7 bore", 4.004, True, None),
        ("oversize Ø8.10 bore", 4.05, False, "H7 band"),
        ("wrong Ø9.0 bore", 4.5, False, "H7 band"),
    ]
    for label, r, expect_ok, sub in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with Worker() as w:
                _bored_plate(w, tmp / "plate.FCStd", 40.0, r, "bore_h7")
            rep = _merge(
                tmp, {"plate": {"file": "plate.FCStd"}},
                [{"component": "plate", "name": "plate", "placement": [0, 0, 0]}],
                [{"kind": "interface_conformance", "part": "plate",
                  "type": "bore_h7@1", "iface": "bore_h7"}])
            _assert_case("bore_h7@1", label, expect_ok, rep, sub)


# --- nema17_face@1 conformance (reference + negatives) -----------------------

def test_nema17_face_conformance():
    from driftpin import Worker
    cases = [
        ("reference NEMA17 face", 11.01, _nema17_holes(), True, None),
        ("undersize pilot Ø21", 10.5, _nema17_holes(), False, "pilot"),
        ("missing two mounting holes", 11.01, _nema17_holes()[:2], False,
         "mounting hole"),
    ]
    for label, pilot_r, holes, expect_ok, sub in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with Worker() as w:
                _bored_plate(w, tmp / "face.FCStd", 42.3, pilot_r, "nema17_face",
                             holes=holes)
            rep = _merge(
                tmp, {"face": {"file": "face.FCStd"}},
                [{"component": "face", "name": "face", "placement": [0, 0, 0]}],
                [{"kind": "interface_conformance", "part": "face",
                  "type": "nema17_face@1", "iface": "nema17_face"}])
            _assert_case("nema17_face@1", label, expect_ok, rep, sub)


# --- unknown type is a loud violation at merge -------------------------------

def test_unknown_type_fails_loudly_at_merge():
    from driftpin import Worker
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            _bored_plate(w, tmp / "plate.FCStd", 40.0, 4.004, "bore_h7")
        rep = _merge(
            tmp, {"plate": {"file": "plate.FCStd"}},
            [{"component": "plate", "name": "plate", "placement": [0, 0, 0]}],
            [{"kind": "interface_conformance", "part": "plate",
              "type": "no_such_iface@1"}])
        confs = _conf_violations(rep)
        assert rep["ok"] is False, rep["gates"]
        assert confs and "unknown interface type" in confs[0].get("error", ""), confs
    print("  PASS unknown type at merge -> violation, ok=False (never a silent pass)")


# --- a BUS: one registry entry, many parts attach ----------------------------

def test_bus_two_parts_conform_to_one_entry():
    """A bus interface (Ulrich's modular type, RFC §6.3): one registry entry
    (bore_h7@1) referenced by TWO distinct parts. Both must gate green — and the
    ergonomic `implements` declaration is exercised end-to-end through the
    lowering seam, not hand-authored checks."""
    from driftpin import Worker
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:                       # two DIFFERENT plates, same bore
            _bored_plate(w, tmp / "p1.FCStd", 40.0, 4.004, "bore_h7")
        with Worker() as w:
            _bored_plate(w, tmp / "p2.FCStd", 52.0, 4.004, "bore_h7")
        man = {
            "name": "bus", "root": "bus.FCStd",
            "components": {
                "plate1": {"file": "p1.FCStd", "implements": ["bore_h7@1"]},
                "plate2": {"file": "p2.FCStd", "implements": ["bore_h7@1"]}},
            "instances": [
                {"component": "plate1", "name": "p1", "placement": [0, 0, 0]},
                {"component": "plate2", "name": "p2", "placement": [120, 0, 0]}],
        }
        low = ir.lower_manifest(man)              # implements -> conformance checks
        mp = tmp / "bus.manifest.json"
        mp.write_text(json.dumps(low))
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(mp))
    confs = _conf_violations(rep)
    assert rep["ok"] is True and not confs, (rep["ok"], confs)
    parts = {c["part"] for c in low["checks"]
             if c["kind"] == "interface_conformance"}
    assert parts == {"p1", "p2"}, parts
    assert len(rep["gates"]["bom"]) == 2, rep["gates"]["bom"]
    print("  PASS bus: 2 parts conform to one bore_h7@1 entry, both gate green")


# --- helpers -----------------------------------------------------------------

def _assert_case(type_id, label, expect_ok, rep, sub):
    confs = _conf_violations(rep)
    ok = not confs
    if ok != expect_ok:
        raise AssertionError(
            f"{type_id} [{label}]: expected ok={expect_ok}, got ok={ok}  "
            f"violations={confs}")
    if not expect_ok:
        reasons = " | ".join(v.get("reason", v.get("error", "")) for v in confs)
        if sub and sub not in reasons:
            raise AssertionError(
                f"{type_id} [{label}]: caught but reason {reasons!r} lacks {sub!r}")
        # a real conformance failure must also flip the merge result
        assert rep["ok"] is False, f"{type_id} [{label}]: violation but ok stayed True"
    tag = "PASS" if ok == expect_ok else "FAIL"
    detail = "conforms" if ok else "caught: " + (confs[0].get("reason")
                                                 or confs[0].get("error"))
    print(f"  {tag} {type_id:16s} {label:30s} -> {detail}")


def main():
    pure = [test_registry_directory_and_schema, test_type_id_door,
            test_unknown_type_is_a_violation_not_a_raise,
            test_conformance_predicate_over_scripted_features,
            test_lowering_implements_to_checks]
    worker = [test_bore_h7_conformance, test_nema17_face_conformance,
              test_unknown_type_fails_loudly_at_merge,
              test_bus_two_parts_conform_to_one_entry]
    failed = 0
    t0 = time.time()
    print("== interface-type registry + conformance gate (issue #146, no API) ==")
    print("-- pure (registry / door / predicate / lowering) --")
    for t in pure:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print("-- conformance through the real merge_assembly --")
    for t in worker:
        try:
            t()
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n== {failed} conformance check(s) FAILED ==")
        sys.exit(1)
    print(f"\n== every interface type passes its reference AND catches every "
          f"negative; unknown is loud; the bus conforms  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()

"""Orderable standard parts (issue #234) — end-to-end through the FreeCAD worker.

The pure core is unit-tested in test_orderable.py; this drives the real integration
shim on a live document. It builds the fixture assembly the issue describes — a
hand-modelled bracket plus generated purchased parts — and asserts:

  * every add_fastener / add_bearing / add_thread part carries a canonical
    designation the moment it is created, with ZERO network access;
  * the hand-modelled bracket does NOT — no false designations, which is the half
    of the property that matters most;
  * the designation lives on the DOCUMENT: it survives a save/reopen round trip and
    travels with a linked component into the assembly;
  * bom_extract's DEFAULT return is byte-identical to what it was before this
    change (a bare list) — release_package and everything else consume it;
  * bom_extract(orderable=True) resolves the purchased rows against the off-the-shelf
    catalog, surfaces the O-ring a groove calls for as a consumable that no BOM would
    otherwise contain, and reports a part nobody stocks LOUDLY rather than emitting a
    parts list that reads as buildable;
  * catalog_nearest answers the question that changes a design — a modelled M4×13
    gets 12 and 16, not a silently rounded size.

Nothing here touches the network: the catalog is a corpus checked into the repo.

Run: .venv/bin/python3 tests/test_orderable_worker.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _eq(label, got, want):
    _check(label, got == want, f"got {got!r}, want {want!r}")


def _label(w, handle, text):
    """Name a primitive. add_primitive does not take a label, and the BOM's `part`
    column (and the purchased-name heuristic) both read the Label — so a test about
    names has to actually set one."""
    w.call("set_property", handle=handle, name="Label", value=text)
    return handle


def _fixture(w):
    """The assembly the issue's gate describes: one hand-modelled bracket, four
    identical screws, two sealed bearings. Returns the assembly handle."""
    w.call("new_document", name="orderable")
    bracket = w.call("add_primitive", kind="box", w=60, d=40, h=8)
    _label(w, bracket["handle"], "Bracket")
    screw = w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                   length=12, grade="A2")
    bearing = w.call("add_bearing", designation="608", seals="2RS")
    asm = w.call("make_assembly", name="Rig")
    w.call("add_part", assembly=asm["handle"],
           source={"handle": bracket["handle"]}, placement=[0, 0, 0])
    for i in range(4):
        w.call("add_part", assembly=asm["handle"],
               source={"handle": screw["handle"]}, placement=[10 * i, 0, 20])
    for i in range(2):
        w.call("add_part", assembly=asm["handle"],
               source={"handle": bearing["handle"]}, placement=[0, 30 * i, 40])
    return asm["handle"], bracket, screw, bearing


# --- designations are stamped at creation, offline ----------------------------

def test_generators_stamp_a_designation():
    print("test_generators_stamp_a_designation")
    with Worker() as w:
        w.call("new_document", name="stamp")
        screw = w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                       length=12, grade="A2")
        _eq("fastener designation", screw["designation"], "ISO 4762 M4×12 A2")
        _check("and it is complete", screw["orderable"]["complete"])

        nut = w.call("add_fastener", kind="hex_nut", size="M6", grade="8")
        _eq("nut takes the ISO 898-2 class", nut["designation"], "ISO 4032 M6 8")

        washer = w.call("add_fastener", kind="washer", size="M6", grade="A2")
        _eq("washer designates by bare nominal", washer["designation"],
            "ISO 7089 6 A2")

        brg = w.call("add_bearing", designation="608", seals="2RS")
        _eq("bearing carries its seal suffix", brg["orderable"]["designation"],
            "608-2RS")

        rod = w.call("add_thread", diameter=8, pitch=1.25, length=20,
                     grade="A2")
        _eq("external thread is threaded rod", rod["designation"],
            "DIN 976-1 M8×20 A2")

        # read it back off the object — the stamp, not the return value
        card = w.call("standard_part_designate", handle=screw["handle"])
        _eq("read back off the object", card["designation"], "ISO 4762 M4×12 A2")
        _eq("sourced from the stamp", card["source"], "stamp")


def test_an_ungraded_fastener_is_designated_but_incomplete():
    print("test_an_ungraded_fastener_is_designated_but_incomplete")
    with Worker() as w:
        w.call("new_document", name="ungraded")
        screw = w.call("add_fastener", kind="socket_head_cap_screw", size="M6",
                       length=20)
        _eq("still named", screw["designation"], "ISO 4762 M6×20")
        _eq("but not orderable", screw["orderable"]["complete"], False)
        _check("with a reason naming the gap",
               "grade" in screw["orderable"]["reason"])


def test_a_bad_grade_fails_before_any_geometry_is_left_behind():
    print("test_a_bad_grade_fails_before_any_geometry_is_left_behind")
    with Worker() as w:
        w.call("new_document", name="badgrade")
        before = len(w.call("list_objects"))
        try:
            w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                   length=12, grade="8.9")
            _check("a typo'd grade raises", False, "no exception")
        except Exception as exc:
            _check("a typo'd grade raises", "8.9" in str(exc), str(exc))
        _eq("and left no orphan solid in the document",
            len(w.call("list_objects")), before)


def test_a_hand_modelled_part_gets_no_designation():
    print("test_a_hand_modelled_part_gets_no_designation")
    # The other half of the property: nothing infers a designation from geometry,
    # so a machined part can never acquire a false one.
    with Worker() as w:
        w.call("new_document", name="handmade")
        box = w.call("add_primitive", kind="box", w=60, d=40, h=8)
        _label(w, box["handle"], "Bracket")
        card = w.call("standard_part_designate", handle=box["handle"])
        _eq("no designation", card["designation"], None)
        _eq("not ok", card["ok"], False)
        _eq("and its name does not read purchased",
            card["looks_purchased"]["purchased"], False)

        # ... but a hand-modelled part NAMED like hardware is called out
        fake = w.call("add_primitive", kind="cylinder", r=3, h=12)
        _label(w, fake["handle"], "M6Screw")
        card = w.call("standard_part_designate", handle=fake["handle"])
        _eq("still no designation", card["designation"], None)
        _eq("but the name is flagged", card["looks_purchased"]["purchased"], True)


def test_an_envelope_bearing_is_not_designated():
    print("test_an_envelope_bearing_is_not_designated")
    with Worker() as w:
        w.call("new_document", name="envelope")
        brg = w.call("add_bearing", bore=8, outer_diameter=22, width=7)
        _eq("no catalog number, no designation",
            brg["orderable"]["designation"], None)
        _check("with a reason", "envelope" in brg["orderable"]["reason"],
               brg["orderable"]["reason"])


def test_an_internal_thread_is_a_tool_not_a_part():
    print("test_an_internal_thread_is_a_tool_not_a_part")
    with Worker() as w:
        w.call("new_document", name="tap")
        tap = w.call("add_thread", diameter=8, pitch=1.25, length=20,
                     internal=True)
        _eq("no designation", tap["designation"], None)
        _eq("and not purchased", tap["orderable"]["purchased"], False)


def test_the_designation_survives_save_and_reopen():
    print("test_the_designation_survives_save_and_reopen")
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "parts.FCStd")
        with Worker() as w:
            w.call("new_document", name="persist")
            w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                   length=12, grade="A2")
            w.call("save_document", path=path)
        with Worker() as w:
            doc = w.call("open_document", path=path)
            objs = w.call("list_objects", document=doc.get("name"))
            name = next(o["name"] for o in objs
                        if "Screw" in o["name"] or "Cap" in o["name"])
            h = w.call("register_handle", object=name)["handle"]
            card = w.call("standard_part_designate", handle=h)
            _eq("designation came back off disk", card["designation"],
                "ISO 4762 M4×12 A2")


# --- the BOM ------------------------------------------------------------------

def test_bom_extract_default_shape_is_unchanged():
    print("test_bom_extract_default_shape_is_unchanged")
    # release_package (#233) and every existing caller consume the bare list. The
    # orderable view is strictly opt-in, so this is the regression that matters.
    with Worker() as w:
        asm, _, _, _ = _fixture(w)
        bom = w.call("bom_extract", assembly=asm)
        _check("still a list", isinstance(bom, list), type(bom).__name__)
        _eq("three rows", len(bom), 3)
        _eq("counts", sorted(r["count"] for r in bom), [1, 2, 4])
        _check("no orderable fields leaked into the default view",
               all("designation" not in r for r in bom), bom[0])


def test_orderable_bom_resolves_the_purchased_rows():
    print("test_orderable_bom_resolves_the_purchased_rows")
    with Worker() as w:
        asm, _, _, _ = _fixture(w)
        out = w.call("bom_extract", assembly=asm, orderable=True)
        _check("a dict now", isinstance(out, dict), type(out).__name__)
        by_part = {r["part"]: r for r in out["rows"]}
        _eq("everything is designated and buyable", out["ok"], True)
        _eq("screws designated", by_part["SocketHeadCapScrew"]["designation"],
            "ISO 4762 M4×12 A2")
        _eq("bearings designated", by_part["Bearing"]["designation"], "608-2RS")
        _eq("bracket is not purchased",
            by_part["Bracket"].get("designation"), None)
        _check("the bracket gets no catalog verdict",
               "catalog" not in by_part["Bracket"])
        _eq("screws are stocked", by_part["SocketHeadCapScrew"]["stocked"], True)
        _eq("bearings are stocked", by_part["Bearing"]["catalog_code"], "stocked")
        _eq("both counted", out["stocked_count"], 2)
        _check("stamped with the corpus date", bool(out["captured"]))
        _eq("and its fidelity labelled", out["fidelity"], "curated snapshot")


def test_a_part_nobody_stocks_is_reported_loudly():
    print("test_a_part_nobody_stocks_is_reported_loudly")
    # The issue's gate: a BOM containing a part that does not exist must NOT read as
    # a buildable parts list.
    with Worker() as w:
        asm, _, _, _ = _fixture(w)
        odd = w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                     length=13, grade="A2", name="OddScrew")
        _label(w, odd["handle"], "OddScrew")
        w.call("add_part", assembly=asm, source={"handle": odd["handle"]},
               placement=[0, 0, 80])
        out = w.call("bom_extract", assembly=asm, orderable=True)
        _eq("the BOM is not ok", out["ok"], False)
        _eq("the offender is named",
            [u["designation"] for u in out["not_stocked"]],
            ["ISO 4762 M4×13 A2"])
        _eq("with the stocked lengths either side",
            [n["length"] for n in out["not_stocked"][0]["nearest"]], [12.0, 16.0])
        _check("and it is still IN the BOM, not dropped",
               any(r["part"] == "OddScrew" for r in out["rows"]))
        _eq("the good lines are still marked stocked", out["stocked_count"], 2)


def test_designation_check_gates_the_assembly():
    print("test_designation_check_gates_the_assembly")
    with Worker() as w:
        asm, _, _, _ = _fixture(w)
        _eq("a fully designated assembly passes",
            w.call("designation_check", assembly=asm)["ok"], True)
        ungraded = w.call("add_fastener", kind="hex_nut", size="M4",
                          name="LooseNut")
        w.call("add_part", assembly=asm, source={"handle": ungraded["handle"]},
               placement=[0, 0, 60])
        v = w.call("designation_check", assembly=asm)
        _eq("an ungraded fastener fails it", v["ok"], False)
        _eq("with the right code",
            [f["code"] for f in v["findings"]], ["incomplete_designation"])


def test_the_oring_a_groove_calls_for_reaches_the_bom():
    print("test_the_oring_a_groove_calls_for_reaches_the_bom")
    # The ring is a purchased part that is never a modelled object, so without the
    # consumable stamp it would appear on no BOM at all.
    with Worker() as w:
        w.call("new_document", name="seal")
        plate = w.call("add_primitive", kind="box", w=80, d=80, h=10)
        _label(w, plate["handle"], "SealPlate")
        faces = w.call("list_faces", handle=plate["handle"])
        top = next(f["tag"] for f in faces
                   if f["kind"] == "planar" and abs(f["centroid"][2] - 10.0) < 1e-6)
        gl = w.call("oring_groove", handle=plate["handle"], face=top,
                    cross_section=3.53, inner_diameter=24.99, compound="NBR70")
        _eq("the ring is designated", gl["oring"]["designation"],
            "AS568-214 NBR70")

        asm = w.call("make_assembly", name="SealRig")
        w.call("add_part", assembly=asm["handle"],
               source={"handle": gl["handle"]}, placement=[0, 0, 0])
        out = w.call("bom_extract", assembly=asm["handle"], orderable=True)
        _eq("one consumable line", len(out["consumables"]), 1)
        _eq("it is the ring", out["consumables"][0]["designation"],
            "AS568-214 NBR70")
        _eq("and it resolves to a stocked AS568 size",
            out["consumables"][0]["catalog_code"], "stocked")
        _check("the grooved plate itself is NOT marked purchased",
               out["rows"][0].get("designation") is None)


def test_an_off_table_groove_names_no_dash_number():
    print("test_an_off_table_groove_names_no_dash_number")
    with Worker() as w:
        w.call("new_document", name="oddseal")
        gl = w.call("oring_groove", cross_section=3.53, inner_diameter=25.8,
                    cut=False, compound="NBR70")
        _eq("no designation", gl["oring"]["designation"], None)
        _check("the nearest sizes are named", len(gl["oring"]["nearest"]) >= 2)


# --- the catalog tools --------------------------------------------------------

def test_the_generators_report_stocked_ness_at_creation_time():
    print("test_the_generators_report_stocked_ness_at_creation_time")
    # Creation time is the loudest place to say "nobody stocks an M4×13": the agent
    # is choosing the number right then, before geometry hardens around it.
    with Worker() as w:
        w.call("new_document", name="stock")
        good = w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                      length=12, grade="A2")
        _eq("a stocked screw says so", good["catalog"]["code"], "stocked")
        odd = w.call("add_fastener", kind="socket_head_cap_screw", size="M4",
                     length=13, grade="A2")
        _eq("an unstocked length is flagged at once", odd["catalog"]["code"],
            "not_stocked")
        _eq("with the rungs either side",
            [n["length"] for n in odd["catalog"]["nearest"]], [12.0, 16.0])
        _check("but the geometry was still built (it is a finding, not a refusal)",
               odd["volume"] > 0)


def test_catalog_nearest_and_search_through_the_worker():
    print("test_catalog_nearest_and_search_through_the_worker")
    with Worker() as w:
        near = w.call("catalog_nearest", standard="ISO 4762", size="M4",
                      length=13, grade="A2")
        _eq("13 is not stocked", near["ok"], False)
        _eq("below", near["below"], 12.0)
        _eq("above", near["above"], 16.0)
        _eq("and it hands back a usable designation", near["designation"],
            "ISO 4762 M4×12 A2")

        hits = w.call("catalog_search", family="screw", size="M4", length=12)
        _check("several head styles exist at M4×12",
               {"ISO 4762", "ISO 7380-1", "ISO 10642"} <= set(hits["standards"]),
               hits["standards"])
        _check("the corpus declares its omissions",
               len(hits["not_covered"]) >= 3, hits["not_covered"])

        chk = w.call("catalog_check", designation="608-2RS")
        _eq("a stocked bearing checks out", chk["code"], "stocked")


def test_catalog_check_reads_the_stamp_off_a_handle():
    print("test_catalog_check_reads_the_stamp_off_a_handle")
    with Worker() as w:
        w.call("new_document", name="chk")
        screw = w.call("add_fastener", kind="socket_head_cap_screw", size="M6",
                       length=20, grade="A2")
        v = w.call("catalog_check", handle=screw["handle"])
        _eq("resolved from the stamp alone", v["code"], "stocked")
        _eq("naming the standard", v["standard"], "ISO 4762")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        _eq("an undesignated object is undesignated, not 'unavailable'",
            w.call("catalog_check", handle=box["handle"])["code"], "undesignated")


def test_normalising_a_designation_through_the_worker():
    print("test_normalising_a_designation_through_the_worker")
    with Worker() as w:
        card = w.call("standard_part_designate", designation="iso4762 m4x12 a2")
        _eq("canonical spelling", card["designation"], "ISO 4762 M4×12 A2")
        spec = w.call("standard_part_designate", family="bearing",
                      spec={"designation": "6205", "seals": "2Z"})
        _eq("built from a spec", spec["designation"], "6205-2Z")


def main():
    for fn in (
        test_generators_stamp_a_designation,
        test_an_ungraded_fastener_is_designated_but_incomplete,
        test_a_bad_grade_fails_before_any_geometry_is_left_behind,
        test_a_hand_modelled_part_gets_no_designation,
        test_an_envelope_bearing_is_not_designated,
        test_an_internal_thread_is_a_tool_not_a_part,
        test_the_designation_survives_save_and_reopen,
        test_bom_extract_default_shape_is_unchanged,
        test_orderable_bom_resolves_the_purchased_rows,
        test_a_part_nobody_stocks_is_reported_loudly,
        test_designation_check_gates_the_assembly,
        test_the_oring_a_groove_calls_for_reaches_the_bom,
        test_an_off_table_groove_names_no_dash_number,
        test_the_generators_report_stocked_ness_at_creation_time,
        test_catalog_nearest_and_search_through_the_worker,
        test_catalog_check_reads_the_stamp_off_a_handle,
        test_normalising_a_designation_through_the_worker,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

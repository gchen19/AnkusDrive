"""
Item model + part numbering (issue #140, C1 — docs/DESIGN_HIERARCHY.md §2 Theme C)
— free, no LLM, no key, no FreeCAD. C1 is pure data/text (items.json identity +
validation), so this suite imports driftpin.items directly and runs fast.

Two-sided gate-validated (the house standard, MULTI_AGENT.md §11.x): the reference
registry loads + resolves an item-ref to its file(s); every negative control —
a dangling item reference, a duplicate part number, a malformed schema — is caught
at the door. Plus: sequential non-significant part-number allocation is
deterministic + monotonic. Mirrors tests/test_manifest_schema.py.

Run: python3 tests/test_items.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import items as I  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "items"

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


# --- reference (must pass) ---------------------------------------------------

def test_golden_registry_valid_and_resolves():
    reg = I.load_registry(FIXTURES / "items.json")
    _check("golden registry validates clean", I.validate_registry(reg), [])
    _check("schema stamp", reg["schema"], "driftpin.items/1")
    # resolve an item-ref (both forms) to its declared file(s)
    files = I.resolve_item_ref(reg, "bracket")
    _check("bare-string ref resolves to files",
           files, ["components/bracket.FCStd", "components/bracket.step"])
    _check("object-form ref resolves identically",
           I.resolve_item_ref(reg, {"item": "bracket"}), files)
    _check("second item resolves",
           I.resolve_item_ref(reg, {"item": "pin"}), ["components/dowel_pin.FCStd"])
    # part numbers are non-significant + unique
    pns = {iid: rec["part_number"] for iid, rec in reg["items"].items()}
    _check("part numbers unique", len(set(pns.values())), len(pns))


def test_manifest_item_refs_resolve():
    import json
    reg = I.load_registry(FIXTURES / "items.json")
    man = json.loads((FIXTURES / "manifest_with_items.json").read_text(encoding="utf-8"))
    _check("manifest item-refs all resolve", I.validate_manifest_refs(man, reg), [])


# --- negative controls (each must be caught) ---------------------------------

def test_dangling_item_reference_caught():
    reg = I.load_registry(FIXTURES / "items.json")
    raised = False
    try:
        I.resolve_item_ref(reg, "ghost")
    except KeyError as e:
        raised = "dangling item reference" in str(e)
    _check("resolve of unknown item id raises (dangling)", raised, True)
    # ... and the manifest-level guard reports it instead of resolving silently
    man = {"components": {"x": {"item": "ghost"}},
           "instances": [{"component": "x", "item": "ghost"}]}
    _has("manifest dangling item-ref flagged (component)",
         I.validate_manifest_refs(man, reg), "component 'x' references unknown item")
    _has("manifest dangling item-ref flagged (instance)",
         I.validate_manifest_refs(man, reg), "instance 0 references unknown item")


def test_duplicate_part_number_caught():
    reg = I.load_registry(FIXTURES / "items.json")
    # collide pin's part number onto bracket's
    reg["items"]["pin"]["part_number"] = reg["items"]["bracket"]["part_number"]
    _has("duplicate part number flagged",
         I.validate_registry(reg), "duplicate part_number")


def test_malformed_schema_rejected():
    reg = I.load_registry(FIXTURES / "items.json")
    # bad schema version
    bad = dict(reg, schema="driftpin.items/99")
    _has("bad schema version flagged", I.validate_registry(bad), "unknown schema")
    # items not an object
    _has("non-object items flagged",
         I.validate_registry({"schema": I.SCHEMA, "items": []}), "items must be an object")
    # item record missing a part number
    r = {"schema": I.SCHEMA, "items": {"x": {"files": ["a.FCStd"]}}}
    _has("missing part_number flagged",
         I.validate_registry(r), "part_number must be a non-empty string")
    # reserved lifecycle field validated as held vocabulary (not a state machine)
    r = {"schema": I.SCHEMA,
         "items": {"x": {"part_number": "DP-1", "lifecycle": "shipped"}}}
    _has("out-of-vocab lifecycle flagged", I.validate_registry(r), "lifecycle 'shipped'")
    # files must be path strings
    r = {"schema": I.SCHEMA, "items": {"x": {"part_number": "DP-1", "files": [1, 2]}}}
    _has("non-string files flagged",
         I.validate_registry(r), "files must be a list of path strings")
    # not a dict at all
    _has("non-object registry flagged",
         I.validate_registry([1, 2, 3]), "registry must be a JSON object")


# --- allocation: sequential, non-significant, monotonic ----------------------

def test_sequential_allocation_is_deterministic_and_monotonic():
    reg = I.empty_registry({"prefix": "DP-", "digits": 6, "next": 1001})
    a = I.allocate_part_number(reg)
    b = I.allocate_part_number(reg)
    c = I.allocate_part_number(reg)
    _check("first allocation", a, "DP-001001")
    _check("sequential", [a, b, c], ["DP-001001", "DP-001002", "DP-001003"])
    _check("counter advanced", reg["part_number_format"]["next"], 1004)
    # new_item allocates + seeds reserved fields with held defaults
    reg2 = I.empty_registry()
    I.new_item(reg2, "widget", files=["w.FCStd"], metadata={"category": "x"})
    rec = reg2["items"]["widget"]
    _check("new_item allocates a PN", rec["part_number"], "DP-001001")
    _check("new_item seeds reserved rev (held, default)", rec["rev"], "-")
    _check("new_item seeds reserved lifecycle (held, default)",
           rec["lifecycle"], "in_work")
    _check("new_item result still validates", I.validate_registry(reg2), [])
    # a freshly-allocated number never collides with an existing item
    I.new_item(reg2, "widget2", files=["w2.FCStd"])
    _check("two new items, distinct PNs",
           reg2["items"]["widget"]["part_number"]
           != reg2["items"]["widget2"]["part_number"], True)
    _check("duplicate item id rejected",
           _raises(lambda: I.new_item(reg2, "widget")), True)


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def main():
    print("== item model + part numbering (C1, #140) — no API, no FreeCAD ==")
    for t in (test_golden_registry_valid_and_resolves,
              test_manifest_item_refs_resolve,
              test_dangling_item_reference_caught,
              test_duplicate_part_number_caught,
              test_malformed_schema_rejected,
              test_sequential_allocation_is_deterministic_and_monotonic):
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

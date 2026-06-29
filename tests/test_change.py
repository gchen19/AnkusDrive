"""
Change orders + where-used impact + baselines (issue #142, C3 —
docs/DESIGN_HIERARCHY.md §2 Theme C) — free, no LLM, no key, no FreeCAD. C3 is
pure data/text (the lockfile `depends_on` graph + the items.json registry), so
this suite imports driftpin.change directly and runs fast.

Two-sided gate-validated (the house standard, MULTI_AGENT.md §11.x; the C3 row of
DESIGN_HIERARCHY §7):

  reference (must pass)
    - a WHERE-USED query on an item returns its full parent set from a known
      lockfile fixture (validated against a hand-computed nested manifest)
    - a BASELINE pins {item: rev} and a rebuild from it reproduces identical bytes
    - an ECO lists affected items + an effectivity, and its impact report surfaces
      the §9 `stale` consumers of a changed interface

  negative controls (each must be caught)
    - a missed/extra parent in the where-used set is caught (exact-set assertion)
    - a baseline rebuild with a DRIFTED input (a changed artifact / bumped rev) is
      caught
    - a stale consumer of a changed interface is flagged (the §9 mechanism)
    - a malformed ECO (no affected items / a two-kinded effectivity) is rejected

Mirrors tests/test_items.py + tests/test_lifecycle.py.
Run: python3 tests/test_change.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import change as C   # noqa: E402
from driftpin import items as I    # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "change"

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


def _lock():
    return C.load_lockfile(FIXTURES / "gearbox.lock.json")


def _registry():
    return I.load_registry(FIXTURES / "items.json")


# --- where-used / impact: reference (must pass) ------------------------------
#
# The fixture lockfile encodes a nested manifest. The depends_on edges (consumer
# -> consumed) are:
#     lid   -> housing
#     shaft -> housing
#     gear  -> shaft
#     cover -> lid
# Hand-computed where-used (transitive reverse reachability):
#     housing : {lid, shaft, gear, cover}   (everything reaches housing)
#     shaft   : {gear}
#     gear    : {}                            (a top consumer; nothing uses it)
#     lid     : {cover}

def test_where_used_full_parent_set():
    lock = _lock()
    _check("where_used(housing) = full nested parent set",
           C.where_used(lock, "housing"), ["cover", "gear", "lid", "shaft"])
    _check("where_used(shaft) = transitive consumer",
           C.where_used(lock, "shaft"), ["gear"])
    _check("where_used(lid)", C.where_used(lock, "lid"), ["cover"])
    _check("where_used(gear) = top consumer, used by nobody",
           C.where_used(lock, "gear"), [])
    # the §9 immediate neighbour layer (direct dependents only)
    _check("direct_dependents(housing) = immediate mates",
           C.direct_dependents(lock, "housing"), ["lid", "shaft"])


def test_where_used_missed_or_extra_parent_caught():
    # an exact-set assertion: a missed parent (gear) or an extra one would fail.
    lock = _lock()
    full = set(C.where_used(lock, "housing"))
    _check("a MISSED parent is caught (gear must be in the set)",
           "gear" in full, True)
    _check("no EXTRA parent (housing is not its own consumer)",
           "housing" in full, False)
    _check("unknown item fails loudly (no silent empty result)",
           _raises(KeyError, C.where_used, lock, "nonexistent"), True)


# --- baselines: reference (must pass) + drift caught ------------------------

def test_baseline_pins_and_rebuild_reproduces_bytes():
    reg = _registry()
    base = C.create_baseline("v1.0", reg, base_dir=str(FIXTURES))
    _check("baseline validates clean", C.validate_baseline(base), [])
    _check("baseline pins every item", sorted(base["items"]),
           ["cover", "gear", "housing", "lid", "shaft"])
    _check("baseline pins the rev", base["items"]["gear"]["rev"], "B")
    # byte-reproducible: pinning the same unchanged state twice is byte-identical
    base2 = C.create_baseline("v1.0", reg, base_dir=str(FIXTURES))
    _check("rebuild from baseline is byte-reproducible (deterministic bytes)",
           C.serialize_baseline(base), C.serialize_baseline(base2))
    # verify against the unchanged registry: clean rebuild
    rep = C.verify_baseline(base, reg, base_dir=str(FIXTURES))
    _check("unchanged rebuild verifies ok", rep["ok"], True)
    _check("nothing drifted", rep["drifted"], [])


def test_baseline_rebuild_with_drifted_input_caught():
    reg = _registry()
    base = C.create_baseline("v1.0", reg, base_dir=str(FIXTURES))
    # a DRIFTED rev: the gear was revised after the baseline was pinned
    reg["items"]["gear"]["rev"] = "C"
    rep = C.verify_baseline(base, reg, base_dir=str(FIXTURES))
    _check("a drifted rev breaks the baseline rebuild", rep["ok"], False)
    _check("the drift names the item + field",
           any(d["item"] == "gear" and d["field"] == "rev" for d in rep["drifted"]),
           True)
    # a DRIFTED artifact: same rev, but the bytes changed (a silent file edit)
    reg2 = _registry()
    base_fp = base["items"]["housing"]["fingerprint"]
    base["items"]["housing"]["fingerprint"] = base_fp[:-1] + ("0" if base_fp[-1] != "0" else "1")
    rep2 = C.verify_baseline(base, reg2, base_dir=str(FIXTURES))
    _check("a drifted artifact fingerprint is caught",
           any(d["item"] == "housing" and d["field"] == "fingerprint"
               for d in rep2["drifted"]), True)
    # a vanished item is caught as missing, not silently skipped
    reg3 = _registry()
    del reg3["items"]["lid"]
    rep3 = C.verify_baseline(C.create_baseline("v1.0", _registry(), base_dir=str(FIXTURES)),
                             reg3, base_dir=str(FIXTURES))
    _check("a missing pinned item is caught", "lid" in rep3["missing"], True)


# --- ECO record + impact: reference (must pass) -----------------------------

def test_eco_record_and_effectivity():
    eco = C.make_eco("ECO-0001", affected=["housing"], disposition="revise",
                     effectivity={"revision": "B"}, title="raise bore tol",
                     interface_change=True)
    _check("eco validates clean", C.validate_eco(eco), [])
    _check("eco lists affected items", eco["affected"], ["housing"])
    _check("eco carries an effectivity", eco["effectivity"], {"revision": "B"})
    _check("eco disposition", eco["disposition"], "revise")
    # serialisation is deterministic (the diff IS the change order)
    _check("eco serialises deterministically",
           C.serialize_eco(eco), C.serialize_eco(C.make_eco(
               "ECO-0001", ["housing"], "revise", {"revision": "B"},
               title="raise bore tol", interface_change=True)))


def test_eco_impact_flags_stale_consumers():
    # the §9 stale mechanism surfaced through the ECO impact report: changing
    # housing's interface impacts everything that consumes it.
    lock = _lock()
    eco = C.make_eco("ECO-0002", affected=["housing"], disposition="revise",
                     effectivity={"date": "2026-07-01"}, interface_change=True)
    rep = C.eco_impact(eco, lock)
    _check("§9 stale = the immediate consumers re-dispatch first",
           rep["stale"], ["lid", "shaft"])
    _check("blast radius = the full transitive where-used set",
           rep["where_used"], ["cover", "gear", "lid", "shaft"])
    _check("an impacting change is not ok-to-skip", rep["ok"], False)
    # a top consumer change impacts nobody (encapsulation: internal-only)
    eco_leaf = C.make_eco("ECO-0003", ["gear"], "revise", {"serial": 42})
    rep_leaf = C.eco_impact(eco_leaf, lock)
    _check("changing a top consumer impacts nobody", rep_leaf["where_used"], [])
    _check("no blast radius => ok", rep_leaf["ok"], True)
    # the annotated record carries its own impact
    annotated = C.eco_with_impact(eco, lock)
    _check("eco_with_impact embeds the report",
           annotated["impact"]["stale"], ["lid", "shaft"])


# --- ECO: negative controls (each must be caught) ---------------------------

def test_malformed_eco_rejected():
    _has("an ECO with no affected items is rejected",
         C.validate_eco(C.make_eco("ECO-X", [], "revise", {"revision": "A"})),
         "affected must be a non-empty list")
    _has("a two-kinded effectivity is rejected",
         C.validate_eco(C.make_eco("ECO-X", ["housing"], "revise",
                                   {"revision": "A", "date": "2026-01-01"})),
         "exactly one of")
    _has("a no-kind effectivity is rejected",
         C.validate_eco(C.make_eco("ECO-X", ["housing"], "revise", {})),
         "exactly one of")
    _has("a missing disposition is rejected",
         C.validate_eco({"schema": C.ECO_SCHEMA, "id": "ECO-X",
                         "affected": ["housing"], "effectivity": {"revision": "A"}}),
         "disposition must be a non-empty string")
    _has("a bad schema is rejected",
         C.validate_eco({"schema": "wrong/9", "id": "X", "affected": ["a"],
                         "disposition": "revise", "effectivity": {"revision": "A"}}),
         "unknown schema")
    # an ECO against an item nothing in the graph knows fails loudly
    _check("eco impact against an unknown item fails loudly",
           _raises(KeyError, C.eco_impact,
                   C.make_eco("ECO-X", ["ghost"], "revise", {"revision": "A"}), _lock()),
           True)


def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except exc:
        return True


def main():
    print("== change orders + where-used impact + baselines (C3, #142) "
          "— no API, no FreeCAD ==")
    for t in (test_where_used_full_parent_set,
              test_where_used_missed_or_extra_parent_caught,
              test_baseline_pins_and_rebuild_reproduces_bytes,
              test_baseline_rebuild_with_drifted_input_caught,
              test_eco_record_and_effectivity,
              test_eco_impact_flags_stale_consumers,
              test_malformed_eco_rejected):
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

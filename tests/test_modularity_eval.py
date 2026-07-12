"""
Modularity eval ladder — Layer M1 driver (scripted, no LLM, no key).

See tests/MODULARITY_EVAL.md. Four toys, each two-sided: the REFERENCE proves the
modular path works; the NEGATIVE CONTROL proves the failure mode is caught. The
gates are the oracle — all deterministic, all free, runnable in run_all.sh:

  1. family_regen     — one design table -> N items+part-numbers (#138), identical
                        geometry to a hand-rolled baseline, at 1 edit vs N to change
                        the family build rule (the measured modularity payoff).
  2. substitutability — substitutability_check (#147): same-interface swap stays
                        green; off-interface swap fails, NAMING the broken gate.
  3. encapsulation    — assembly_lock_check (§9): an internal change is no-neighbor-
                        impact; a moved published frame reports the stale blast radius.
  4. interface_break  — the F3 predicate (#141): an F3-breaking change with no
                        part-number bump is rejected; an internal change is a revise.

Layer M2 (live agents vs. a hand-rolled baseline — does the abstraction raise the
single-shot pass-rate / cut rounds-to-converge?) is documented as methodology in
tests/MODULARITY_EVAL.md, not run here; this M1 driver runs free in CI.

Run: .venv/bin/python3 tests/test_modularity_eval.py
"""
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import items as _items  # noqa: E402
from driftpin import lifecycle as _lifecycle  # noqa: E402
import modularity_toys as toys  # noqa: E402


# =============================================================================
# Toy 1 — family regen (the headline)
# =============================================================================

def test_family_regen_matches_baseline_geometry():
    """REFERENCE: a whole gear family from ONE table (family_materialize, #138)
    builds the same geometry as the hand-rolled Python-loop baseline — but each
    variant also gets an item + a sequential part number, derived for free."""
    rows = toys.GEAR_FAMILY["rows"]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tbl = tmp / "spur_family.json"
        tbl.write_text(json.dumps(toys.GEAR_FAMILY), encoding="utf-8")
        with Worker() as w:
            mod = toys.family_regen_modular(w, str(tbl))
            base = toys.family_regen_baseline(w, rows)

    assert mod["count"] == base["count"] == len(rows), (mod, base)
    # correctness: identical geometry, variant for variant (same recipe params)
    for vm, vb in zip(mod["volumes"], base["volumes"]):
        assert abs(vm - vb) < 1e-6, (vm, vb)
    # the family payoff the baseline does NOT give: one item + one sequential part
    # number per variant, allocated by the table (not hand-written)
    assert mod["part_numbers"] == ["DP-001001", "DP-001002", "DP-001003",
                                   "DP-001004"], mod["part_numbers"]
    assert len(set(mod["items"])) == len(rows), mod["items"]
    print(f"  PASS family_regen reference: {len(rows)} variants, identical "
          f"geometry to the baseline, each with item + part number")


def test_family_regen_edit_count_payoff():
    """The modularity payoff, MEASURED off the two design representations: changing
    the family-wide build rule is 1 edit (the shared recipe) vs. N (one per inline
    copy in the baseline); adding a member is 1 table row vs. build+bookkeeping."""
    rows = toys.GEAR_FAMILY["rows"]
    n = len(rows)
    modular = toys.modular_family(rows)
    baseline = toys.baseline_family(rows)

    rule = toys.edits_to_change_build_rule(modular, baseline)
    assert rule["modular"] == 1, rule          # one shared recipe
    assert rule["baseline"] == n, rule         # one copy per variant
    payoff = rule["baseline"] / rule["modular"]
    assert payoff == n and payoff > 1, payoff

    add = toys.edits_to_add_variant(baseline)
    assert add["modular"] == 1, add            # one table row
    assert add["baseline"] == 1 + len(toys.DERIVED_FIELDS), add  # build + 3 fields
    print(f"  PASS family_regen payoff: change-the-family-rule {rule['baseline']}:"
          f"{rule['modular']} edits (x{payoff:.0f}); add-a-variant "
          f"{add['baseline']}:{add['modular']}")


def test_family_regen_negative_bad_row_is_caught():
    """NEGATIVE CONTROL: a single bad row (teeth below the recipe minimum) is caught
    LOUDLY at the door, naming the row+column — the whole family refuses to
    materialize, so a broken variant can never silently ship."""
    from driftpin import families
    bad = dict(toys.GEAR_FAMILY,
               rows=toys.GEAR_FAMILY["rows"] + [{"size": "Z2", "module_mm": 2.0,
                                                 "teeth": 2}])
    problems = families.validate_table(bad)
    assert any("'Z2'" in p and "teeth" in p for p in problems), problems
    try:
        families.materialize(bad)
        raise AssertionError("expected the bad family table to be refused")
    except families.FamilyError as e:
        assert "Z2" in str(e), e
    print("  PASS family_regen negative: a bad row is caught, naming row+column")


# =============================================================================
# Toy 2 — substitutability
# =============================================================================

def test_substitutability_same_and_off_interface():
    """REFERENCE: swapping a same-interface variant (Ø15.7, still in the clearance
    band) keeps the assembly green => substitutable => revise.
    NEGATIVE CONTROL: an off-interface variant (Ø16.4, interferes) FAILS and the
    report NAMES the broken gate => a new part number."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            mpath = toys.substitutability_setup(w, tmp)
        with Worker() as w:
            ok = w.call("substitutability_check", manifest=mpath, slot="peg",
                        variant={"file": "peg_ok.FCStd"})
            bad = w.call("substitutability_check", manifest=mpath, slot="peg",
                         variant={"file": "peg_bad.FCStd"})

    # reference swap: still green, classified as a compatible (revise) change
    assert ok["substitutable"] is True, ok
    assert ok["verdict"] == "substitutable" and not ok["broken_gates"], ok
    assert ok["classification"]["decision"] == "revise existing part number", ok

    # negative control: broke a gate, named it, classified MAJOR / new part number
    assert bad["substitutable"] is False, bad
    assert bad["verdict"] == "not_substitutable", bad
    assert bad["broken_gates"], bad                      # NAMES the broken gate
    assert "typed" in bad["broken_gates"], bad           # the bore_fit gate
    assert bad["classification"]["semver"] == "MAJOR", bad
    print(f"  PASS substitutability: same-interface swap stays green (revise); "
          f"off-interface fails naming {bad['broken_gates']} (new part number)")


# =============================================================================
# Toy 3 — encapsulation / no-neighbor-impact
# =============================================================================

def test_encapsulation_internal_change_no_blast_radius():
    """REFERENCE: an INTERNAL change (a hidden pocket, published 'seat' untouched)
    is seen by assembly_lock_check as a modified file with NO stale neighbor — a
    re-merge needs no re-dispatch.
    NEGATIVE CONTROL: moving the PUBLISHED 'seat' frame makes the mating lid stale
    and the blast radius is reported (ok=False)."""
    with tempfile.TemporaryDirectory() as td, Worker() as w:
        tmp = Path(td)
        s = toys.encapsulation_setup(w, tmp)
        man, lock = s["manifest"], s["lockfile"]

        fresh = w.call("assembly_lock_check", manifest=man, lockfile=lock)
        assert fresh["ok"] and not fresh["modified"] and not fresh["stale"], fresh

        # internal change: file differs, interface intact -> no neighbor impacted
        toys.encapsulation_internal_change(w, tmp)
        ic = w.call("assembly_lock_check", manifest=man, lockfile=lock)
        assert "housing" in ic["modified"], ic
        assert "housing" not in ic["interface_changed"], ic
        assert ic["stale"] == [] and ic["ok"], ic       # no re-dispatch needed

        # re-lock to the new baseline, then move the published frame
        w.call("assembly_lock", manifest=man, lockfile=lock)
        toys.encapsulation_interface_move(w, tmp)
        xc = w.call("assembly_lock_check", manifest=man, lockfile=lock)
        assert "housing" in xc["interface_changed"], xc
        assert "lid" in xc["stale"], xc                 # the blast radius
        assert not xc["ok"], xc
    print("  PASS encapsulation: internal change has no blast radius; moving a "
          "published frame reports stale neighbor 'lid' (re-dispatch needed)")


# =============================================================================
# Toy 4 — interface break is loud
# =============================================================================

def test_interface_break_pure_predicate():
    """REFERENCE: the F3 predicate classifies an internal-only edit (more ribs) as a
    'revise' (same part number).
    NEGATIVE CONTROL: an F3-breaking edit (the mating bore grows) is classified
    'new_part_number', naming the FIT leg it broke."""
    revise = _lifecycle.form_fit_function(toys.RELEASED_META, toys.INTERNAL_EDIT)
    assert revise["disposition"] == "revise", revise
    assert revise["f3"] is False and not revise["f3_changed"], revise

    brk = _lifecycle.form_fit_function(toys.RELEASED_META, toys.F3_BREAK_EDIT)
    assert brk["disposition"] == "new_part_number", brk
    assert brk["f3"] is True and brk["f3_changed"] == ["bore_dia_mm"], brk
    assert brk["categories"]["bore_dia_mm"] == "fit", brk
    print("  PASS interface_break predicate: internal edit -> revise; bore change "
          "-> new_part_number (FIT)")


def test_interface_break_rejected_without_renumber():
    """The bite: applying an F3-breaking change to a RELEASED item WITHOUT allocating
    a new part number is REJECTED (apply_change demands a new_item_id) — a Released
    item's interface can never silently change. The compatible (internal) change,
    by contrast, is accepted in place as a revise."""
    # NEGATIVE CONTROL: F3-break with no new part number -> rejected
    reg = toys.released_registry("bracket:A")
    try:
        _lifecycle.apply_change(reg, "bracket:A", toys.F3_BREAK_EDIT,
                                new_item_id=None)
        raise AssertionError("expected the un-renumbered F3 break to be rejected")
    except ValueError as e:
        assert "new_item_id" in str(e), e
    # the released item is untouched — its API did not silently change
    assert _items.get_item(reg, "bracket:A")["metadata"]["bore_dia_mm"] == 8.0

    # the SAME break IS allowed once it is renumbered (a new part number)
    res = _lifecycle.apply_change(reg, "bracket:A", toys.F3_BREAK_EDIT,
                                  new_item_id="bracket:B")
    assert res["disposition"] == "new_part_number", res
    assert res["supersedes"] == "bracket:A", res
    assert res["part_number"] != _items.get_item(reg, "bracket:A")["part_number"]

    # REFERENCE: an internal (F3-preserving) change is a clean in-place revise,
    # same part number, bumped revision
    reg2 = toys.released_registry("bracket:A")
    pn_before = _items.get_item(reg2, "bracket:A")["part_number"]
    rev = _lifecycle.apply_change(reg2, "bracket:A", toys.INTERNAL_EDIT)
    assert rev["disposition"] == "revise", rev
    assert rev["part_number"] == pn_before, rev          # same part number
    assert rev["rev"] == "B", rev                        # A -> B
    print("  PASS interface_break gate: F3 break without renumber rejected; "
          "renumber accepted (supersedes); internal change is a clean revise")


# --- runner (same shape as tests/test_multiagent_m1.py) ----------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    tests = _discover()
    failures = []
    t0 = time.time()
    for name, fn in tests:
        ts = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time()-ts:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time()-ts:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({time.time()-t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({time.time()-t0:.1f}s) ==")


if __name__ == "__main__":
    main()

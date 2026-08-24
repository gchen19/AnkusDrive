"""
Modularity eval ladder — scripted reference builders (Layer M1, no LLM).

See tests/MODULARITY_EVAL.md. The companion of tests/multiagent_toys.py: where
that ladder proves a *team* can partition+merge a design, this one measures whether
AnkusDrive's *modularity* abstractions — recipes (#136), design-table families
(#138), the Liskov substitutability gate (#147), the lockfile change detector
(MULTI_AGENT.md §9) and the Form/Fit/Function predicate (#141) — make the design
itself more modular: a family from one table, a swappable module, a part whose
internals change without touching its neighbors, and an interface break that is
loud.

Each of the four toys is two-sided: a REFERENCE (the modular path works) and a
NEGATIVE CONTROL (the failure mode is caught). The gates are the oracle — all
deterministic, all free, all runnable in run_all.sh with no API key:

  1. family_regen     -> family_materialize (#138) builds N items+part-numbers from
                         one table; identical geometry to a hand-rolled baseline,
                         at a fraction of the edits-to-change-the-family.
  2. substitutability -> substitutability_check (#147) keeps the assembly green on a
                         same-interface swap; an off-interface swap fails, NAMING
                         the broken gate.
  3. encapsulation    -> assembly_lock_check (§9) sees an INTERNAL change as
                         no-neighbor-impact (re-merge, no re-dispatch); a moved
                         PUBLISHED frame reports the stale blast radius.
  4. interface_break  -> the F3 predicate (#141) rejects an F3-breaking change that
                         is not renumbered; an internal change is a clean revise.

The builders take a live ankusdrive.client.Worker and a tmp dir (toys 1-3) or run in
pure Python (toy 4); test_modularity_eval.py drives the two-sided assertions.
"""
import json

from ankusdrive import items as _items
from ankusdrive import lifecycle as _lifecycle


# =============================================================================
# Toy 1 — family regen (the headline). One design table -> a whole gear family,
# each an item + a sequential part number, vs. the hand-rolled Python-loop
# baseline (example/gearbox_manifest.py shape). Metric: correctness (identical
# geometry) + edits-to-change-the-family (the modularity payoff).
# =============================================================================

# A small spur-gear family: same module, varying tooth count + one feature-flag
# (an internal ring gear). The TABLE is the only thing that varies per variant;
# the build RULE is shared (the recipe). N variants, deterministic table order.
GEAR_FAMILY = {
    "schema": "ankusdrive.family/1",
    "family": "spur_family",
    "recipe": "spur_gear",
    "mode": "instances",
    "key": "size",
    "rows": [
        {"size": "Z18", "module_mm": 2.0, "teeth": 18, "width_mm": 6.0},
        {"size": "Z24", "module_mm": 2.0, "teeth": 24, "width_mm": 6.0},
        {"size": "Z36", "module_mm": 2.0, "teeth": 36, "width_mm": 6.0},
        {"size": "Z48", "module_mm": 2.0, "teeth": 48, "width_mm": 6.0},
    ],
}

# The shared build RULE a spur gear obeys — THREE steps (wrap add_gear, publish a
# gear_mesh interface, declare the watertight intent). The spur_gear recipe states
# this ONCE; the hand-rolled baseline copy-pastes it per variant. This is the
# Baldwin&Clark visible-design-rule / hidden-parameter split made countable.
RECIPE_STEPS = ("add_gear", "publish_interface:gear_mesh", "declare_intent:watertight")
# The bookkeeping the family table DERIVES for free per row (item id, part number,
# file path); the baseline must hand-write each.
DERIVED_FIELDS = ("item", "part_number", "file")


def modular_family(rows=None):
    """The modular design: ONE shared recipe + a row of data per variant. The build
    rule appears exactly once; the table is pure per-variant data."""
    rows = rows if rows is not None else GEAR_FAMILY["rows"]
    return {"recipe": list(RECIPE_STEPS), "table": [dict(r) for r in rows]}


def baseline_family(rows=None):
    """The hand-rolled baseline (example/gearbox_manifest.py shape): each variant is
    an INDEPENDENT inline block that re-spells the whole build rule AND its own
    bookkeeping (part number, item, file path) — there is no shared recipe and no
    family/item abstraction, so every variant is a standalone static artifact."""
    rows = rows if rows is not None else GEAR_FAMILY["rows"]
    blocks = []
    for i, r in enumerate(rows):
        blocks.append({
            "build": list(RECIPE_STEPS),                  # copy-pasted build rule
            "item": f"gear_{r['size']}",                  # hand-written item
            "part_number": f"DP-{1001 + i:06d}",          # hand-allocated number
            "file": f"components/gear_{r['size']}.FCStd",  # hand-written path
        })
    return {"blocks": blocks}


def _count_rule(struct):
    """Count how many places the shared build RULE (its first step) is spelled out
    across a design representation. The modular form holds it once (the recipe); the
    baseline holds one copy per variant."""
    step = RECIPE_STEPS[0]
    if "recipe" in struct:
        return struct["recipe"].count(step)            # one shared definition
    return sum(b["build"].count(step) for b in struct["blocks"])  # N copies


def edits_to_change_build_rule(modular, baseline):
    """A family-wide change to the BUILD RULE (e.g. 'every gear also publishes a
    bore interface'): the modular path edits the ONE recipe; the baseline edits
    every inline copy. Returned counts are MEASURED off the structures, not
    asserted."""
    return {"modular": _count_rule(modular), "baseline": _count_rule(baseline)}


def edits_to_add_variant(baseline):
    """Adding a family member: the modular path appends ONE data row (item, part
    number and file path are DERIVED by family_materialize); the baseline must
    hand-write the build block AND each derived bookkeeping field."""
    sample = baseline["blocks"][0]
    return {"modular": 1,  # one table row; materialize derives the rest
            "baseline": 1 + len([k for k in DERIVED_FIELDS if k in sample])}


def _vol(w, handle):
    return w.call("mass_properties", handle=handle, density=7.9e-6)["volume_mm3"]


def family_regen_modular(w, table_path):
    """Build the whole family from one table via the family_materialize handler
    (#138). Returns {volumes, part_numbers, items, count} — one item + one
    sequential part number per variant, geometry derived."""
    w.call("new_document", name="modular_family")
    res = w.call("family_materialize", table=table_path)
    return {
        "volumes": [_vol(w, r["handle"]) for r in res["rows"]],
        "part_numbers": [r["part_number"] for r in res["rows"]],
        "items": [r["item"] for r in res["rows"]],
        "count": res["count"],
    }


def family_regen_baseline(w, rows):
    """Build the SAME family the hand-rolled way: a Python loop that calls add_gear
    directly per variant, with no recipe / item / part-number abstraction. Returns
    the same shape so correctness can be diffed against the modular path."""
    w.call("new_document", name="baseline_family")
    volumes = []
    for r in rows:
        g = w.call("add_gear", teeth=int(r["teeth"]), module=r["module_mm"],
                   height=r["width_mm"], name=f"gear_{r['size']}")
        volumes.append(_vol(w, g["handle"]))
    return {"volumes": volumes, "count": len(rows)}


# =============================================================================
# Toy 2 — substitutability. An assembly that gates green with module A; swap a
# same-interface variant B (still green => interchangeable) vs. an off-interface
# variant (a gate fails, NAMING the break). Drives the real substitutability_check
# handler (#147) over a bore_fit clearance interface.
# =============================================================================

def _plate_with_bore(w, path, bore_d):
    w.call("new_document", name="plate")
    w.call("add_primitive", kind="box", w=60, d=60, h=10, name="plate")
    w.call("add_primitive", kind="cylinder", r=bore_d / 2.0, h=30,
           placement=[30, 30, -10], name="bore")
    w.call("boolean_op", op="cut", base="box_1", tool="cylinder_1")
    w.call("save_document", path=str(path))


def _peg(w, path, peg_d):
    w.call("new_document", name="peg")
    w.call("add_primitive", kind="cylinder", r=peg_d / 2.0, h=20, name="peg")
    w.call("save_document", path=str(path))


def substitutability_setup(w, tmp):
    """Build the base assembly: a plate (Ø16 bore) + reference peg Ø15.6 (gap 0.2)
    that gates green on a bore_fit clearance band [0.1, 0.5], plus a same-interface
    swap variant (Ø15.7, gap 0.15 — still in band) and an off-interface variant
    (Ø16.4 — interferes). Returns the manifest path."""
    _plate_with_bore(w, tmp / "plate.FCStd", 16.0)
    w.call("new_document", name="_reset")
    _peg(w, tmp / "pegA.FCStd", 15.6)       # reference module
    w.call("new_document", name="_reset")
    _peg(w, tmp / "peg_ok.FCStd", 15.7)     # same-interface variant
    w.call("new_document", name="_reset")
    _peg(w, tmp / "peg_bad.FCStd", 16.4)    # off-interface variant (interferes)

    man = {
        "name": "subst_base", "root": str(tmp / "subst_base.FCStd"),
        "components": {"plate": {"file": "plate.FCStd"},
                       "peg": {"file": "pegA.FCStd"}},
        "instances": [
            {"component": "plate", "name": "plate", "placement": [0, 0, 0]},
            {"component": "peg", "name": "peg", "placement": [30, 30, -5]}],
        "checks": [{"kind": "bore_fit", "pin": "peg", "bore": "plate",
                    "min_clearance_mm": 0.1, "max_clearance_mm": 0.5}],
    }
    mpath = tmp / "subst_manifest.json"
    mpath.write_text(json.dumps(man), encoding="utf-8")
    return str(mpath)


# =============================================================================
# Toy 3 — encapsulation / no-neighbor-impact. Change a part's INTERNAL feature and
# prove via assembly_lock_check (§9) that no neighbor is stale (re-merge needs no
# re-dispatch). Negative control: move a PUBLISHED frame -> the stale blast radius
# is reported. Mirrors multiagent_toys toy6 with its own builders.
# =============================================================================

def _iface_box(w, path, name, sx, sy, sz, interfaces, pocket=False):
    """An interface-publishing box; optionally cut an internal pocket so the FILE
    changes WITHOUT touching any published interface (the encapsulation move)."""
    w.call("new_document", name=name)
    box = w.call("add_primitive", kind="box", w=sx, d=sy, h=sz, name=name)
    top = box
    if pocket:
        tool = w.call("add_primitive", kind="cylinder", r=5, h=sz,
                      placement=[sx / 2, sy / 2, sz / 2], name="cavity")
        top = w.call("boolean_op", op="cut", base=box["handle"], tool=tool["handle"])
    for iname, frame in interfaces.items():
        w.call("publish_interface", handle=top["handle"], name=iname, frame=frame)
    w.call("save_document", path=str(path))


def encapsulation_setup(w, tmp):
    """Build housing+lid (lid mates to the housing's published 'seat' frame), write
    the manifest, and lock it. Returns {manifest, lockfile}."""
    house = tmp / "enc_housing.FCStd"
    lid = tmp / "enc_lid.FCStd"
    _iface_box(w, house, "enc_housing", 80, 80, 40,
               {"seat": {"origin": [40, 40, 40], "z_axis": [0, 0, 1]}})
    _iface_box(w, lid, "enc_lid", 80, 80, 8,
               {"seat": {"origin": [40, 40, 0], "z_axis": [0, 0, 1]}})
    man = {"name": "enc", "root": "enc.FCStd",
           "components": {"housing": {"file": "enc_housing.FCStd"},
                          "lid": {"file": "enc_lid.FCStd"}},
           "instances": [
               {"component": "housing", "name": "housing", "placement": [0, 0, 0]},
               {"component": "lid", "name": "lid",
                "mate": {"child_iface": "seat", "parent": "housing",
                         "parent_iface": "seat"}}]}
    mpath = tmp / "enc_manifest.json"
    mpath.write_text(json.dumps(man), encoding="utf-8")
    lock = w.call("assembly_lock", manifest=str(mpath))
    return {"manifest": str(mpath), "lockfile": lock["lockfile"]}


def encapsulation_internal_change(w, tmp):
    """Rebuild the housing with an internal pocket — same published 'seat' frame.
    A hidden-parameter change; no neighbor should be affected."""
    _iface_box(w, tmp / "enc_housing.FCStd", "enc_housing", 80, 80, 40,
               {"seat": {"origin": [40, 40, 40], "z_axis": [0, 0, 1]}}, pocket=True)


def encapsulation_interface_move(w, tmp):
    """Rebuild the housing with its published 'seat' frame MOVED — a visible-design-
    rule change. The lid (which mates to that seat) is deliberately NOT rebuilt, so
    it must be reported stale (the blast radius)."""
    _iface_box(w, tmp / "enc_housing.FCStd", "enc_housing", 80, 80, 40,
               {"seat": {"origin": [20, 20, 40], "z_axis": [0, 0, 1]}})


# =============================================================================
# Toy 4 — interface break is loud. An F3-breaking change without a part-number bump
# must be rejected by the Form/Fit/Function predicate (#141). Pure Python — no
# FreeCAD, no key. The released item is the immutable public API.
# =============================================================================

# A released bracket's attributes: FIT (bore_dia_mm) is interface-defining; the rib
# count / wall thickness are hidden internals (free to change).
RELEASED_META = {"bore_dia_mm": 8.0, "wall_thickness_mm": 2.0, "rib_count": 4}
# An internal-only edit (more ribs) — F3-preserving => a clean revise.
INTERNAL_EDIT = {"bore_dia_mm": 8.0, "wall_thickness_mm": 2.0, "rib_count": 6}
# An F3-breaking edit (the mating bore grows) — not interchangeable => new number.
F3_BREAK_EDIT = {"bore_dia_mm": 10.0, "wall_thickness_mm": 2.0, "rib_count": 4}


def released_registry(item_id="bracket:A"):
    """A registry holding one RELEASED bracket item (lifecycle 'released', rev 'A')
    — the immutable public API a change must be classified against."""
    reg = _items.empty_registry()
    _items.new_item(reg, item_id, files=["components/bracket.FCStd"],
                    metadata=dict(RELEASED_META))
    _lifecycle.transition(reg, item_id, "in_review")
    _lifecycle.transition(reg, item_id, "released")
    return reg

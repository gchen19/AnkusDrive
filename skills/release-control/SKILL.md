---
name: release-control
description: >-
  Run this when you are about to change a part that already exists — to decide
  whether the change is a REVISE (same part number, new revision) or a NEW PART
  NUMBER, and to drive it through the item / revision / ECO workflow cleanly. It
  walks the lifecycle: allocate an item identity, classify the edit by Form/Fit/
  Function, respect released-immutability, and propagate the change through
  where-used / ECO / baseline. Companion to the design-modularly skill: that one
  helps you cut the design well; this one helps you release and revise it without
  breaking downstream consumers. Use it whenever you create, revise, or supersede
  a part number, or touch a Released item.
---

# release-control

The design-modularly skill helps you *cut* the design. This one helps you
*release and revise* it. Identity and revision are not bookkeeping — they are the
mechanical analog of semantic versioning, and getting them right is what lets a
downstream consumer pin to your part the way it pins to a `@1.2.3` dependency.

> **The frame:** *Released-immutable = API stability.* *Form/Fit/Function =
> backward compatibility.* A change that preserves F3 is a compatible bump
> (revise); a change that breaks F3 is a MAJOR bump (new part number). Make that
> decision *testable*, not a matter of taste.

## When to run this

- Before you **create** a new part — allocate an item + part number (C1).
- Before you **change** an existing part — classify revise vs. new-number (C2).
- Before you **propagate** a change — run where-used / ECO / baseline (C3).
- Whenever you are tempted to **edit a Released item** — stop; it is immutable.

## The workflow

### C1 — Item & part number (identity first)

Every part is an **item** with a stable identity, separate from its files and its
revisions. Allocate a part number from the registry; prefer **non-significant**
(meaningless) numbers — intelligent part numbers rot as the taxonomy changes
(Arena; Beyond PLM, *Why Intelligent Part Numbers Must Die*). The item is the
namespace that stops "two components both named *Box*" from colliding
(`MULTI_AGENT.md` §5).

- **Leans on:** the item model (#140, `driftpin/items.py`) —
  `allocate_part_number`, `new_item`, `resolve_item_ref`, `validate_manifest_refs`.
- **Do:** `allocate_part_number(registry)` → an opaque number; attach files +
  metadata as an item.
  **Don't:** encode size/material into the number — it will lie after the first
  variant.

### C2 — Revision & lifecycle (revise vs. new number)

Classify the edit by **Form / Fit / Function**:

| Edit | F3 | Decision | semver |
|---|---|---|---|
| Added an internal rib; changed wall thickness | preserved | **Revise** (same number, new rev) | PATCH |
| Tightened a tolerance within the published fit | preserved | **Revise** | MINOR |
| Moved a hole pattern; changed a mating face | **broken** | **New part number** | MAJOR |

> **Ask: "is this edit interchangeable with what shipped?"** If a fielded
> assembly would accept the new part with no other change, it is F3-preserving →
> revise. If not → new part number.

- **Leans on:** the lifecycle / F3 predicate (#141, `driftpin/lifecycle.py`) — a
  pure, unit-testable function over the published interface.
- **Released-immutable:** a Released item **cannot mutate.** A change forces a new
  revision (F3-preserving) or a new part number (F3-breaking) — never a silent
  edit. The gate rejects a mutate attempt on a Released item and rejects an
  illegal lifecycle transition.
- **Prove the drop-in:** before you call a revision interchangeable, run the
  **substitutability gate** (#147) — swap the new revision into a green assembly
  and re-run `merge_assembly` + all gates. Green ⇒ interchangeable by
  construction; a failure ⇒ you actually broke F3 and owe a new number.

### C3 — ECO, where-used & baseline (propagate)

A change does not stop at one part. Drive it through:

- **Where-used** — from the lockfile / `depends_on` graph, list every parent that
  consumes the changed item. A **stale** consumer of a changed interface must be
  flagged (the §9 stale mechanism), not silently rebuilt.
- **ECO (engineering change order)** — the record that bundles the change, its
  affected items, and effectivity (date / serial / revision).
- **Baseline** — pin `{item: rev}` for a configuration so a rebuild reproduces
  identical bytes; a rebuild with a drifted input is caught.

- **Leans on:** the ECO / where-used / baseline layer (#142, `driftpin/change.py`)
  reusing the `depends_on` graph; the determinism suite (#123/#127) guarantees a
  baseline rebuild is byte-reproducible.

## Pre-flight summary

- [ ] **Identity** — item + non-significant part number allocated (#140).
- [ ] **Classified** — edit judged by Form/Fit/Function → revise *or* new number
      (#141).
- [ ] **Immutability respected** — no silent edit to a Released item (#141).
- [ ] **Drop-in proven** — substitutability gate run before claiming
      interchangeable (#147).
- [ ] **Propagated** — where-used flagged, ECO recorded, baseline pinned (#142).

## Sources

semver.org; buyPLM *Form, Fit, Function* (revise vs. new part number); Arena and
Beyond PLM on non-significant part numbering; PTC Windchill *About Effectivity*.
Full citations and the item/revision/ECO scoping: `docs/DESIGN_HIERARCHY.md`
Theme C (§2) and §9.

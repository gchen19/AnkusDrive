# Design Hierarchy, Variants & Design Control — scoping

Written 2026-06-28. Companion to [`MULTI_AGENT.md`](MULTI_AGENT.md) (which solves
*partition + merge* across a team) and [`ROADMAP.md`](ROADMAP.md) (deepening a
single agent). This doc scopes the **third axis**: making a *design* (not just a
part) — parameter hierarchies that drive geometry, variant families generated from
a table, and the formal release/change control a team needs to evolve a design over
time. It maps the mechanisms proven in SolidWorks / Creo / NX / Inventor / CATIA and
PLM/PDM practice onto DriftPin's deterministic, headless, text-first grain.

---

## 0. The question this answers

> *Does the MCP command surface enable a design hierarchy — component parameters
> driven from master/simple parameters; fast generation of all variants of a part
> (e.g. every gear in a family); and a formal design-control / release process — and
> how should files be structured for a team?*

**Verdict: not yet, and the gaps are specific.** DriftPin has the *coupling*
substrate (manifest + resolve step + published interfaces + lockfile) but not the
*hierarchy*, *variant*, or *lifecycle* layers. Concretely:

| Capability | Today | Evidence |
|---|---|---|
| Master/global parameters | **Partial** — `shared_parameters` + the resolve step (`sum`/`grid_mm`) write literal values into slices coordinator-side | `MULTI_AGENT.md` §11.1; `driftpin/manifest.py:resolve_constraints` |
| Driving→driven relations (formulas) | **No** — resolve does sum-to-target only; no general expression DAG, no driving/driven distinction | `driftpin/manifest.py` |
| Intra-part parametric model (change a number → regen) | **No** — every generator bakes a static B-rep solid and deletes the parametric helper | `worker.py:_h_add_gear` (`doc.removeObject(g.Name)  # keep a static solid`) |
| Variant / family / design table | **No** — multi-part is hand-rolled Python loops; each output is an independent static file | `example/gearbox_manifest.py` |
| Feature templates with declared inputs (PowerCopy/UDF) | **No** | — |
| Revision / version / lifecycle state | **No** — `.FCStd` files carry no rev or maturity; lockfile is a change-*detector*, not a version store | `worker.py:assembly_lock` |
| ECO / change order / where-used | **Partial** — `assembly_lock_check` classifies drift+staleness over the dependency graph, but there is no change *record*, effectivity, or item-level impact report | `MULTI_AGENT.md` §9 |
| Item vs document vs file; part numbers | **No** — a part *is* its file path; no item identity, no part number, no metadata layer | — |
| Project/workspace container | **No** — the directory layout in `MULTI_AGENT.md` §3 is a *convention*, unenforced; no manifest-of-manifests, no reference-integrity guard | `MULTI_AGENT.md` §3, §7 |

The good news: DriftPin's existing decisions point the same direction the industry
settled on. The resolve step already embodies *"hand agents resolved results, not
derivations"* (`MULTI_AGENT.md` §11.1) — that is the **driving→driven** rule. The
manifest is already the **single source of truth**. `publish_interface` /
`interface_align_check` are already **publish/subscribe interface geometry** (the
WAVE-over-interpart-expressions lesson). The lockfile is already a **baseline**
primitive. This program extends those, it doesn't fight them.

---

## 1. The one architectural decision that frames everything

Other CAD systems store an *editable feature tree* in the file: change a dimension,
the kernel replays the tree, geometry regenerates. DriftPin deliberately does **not**
do this — generators bake a static solid and drop the parametric helper, and the team
explicitly **rejected live FreeCAD expression links across files as fragile-headless**
(`MULTI_AGENT.md` §11.1, §13). That is a sound call for a headless, deterministic,
git-diffable tool. But it means DriftPin needs a *different* answer to "what is the
parametric model," and the answer is already latent in the codebase:

> **The build recipe is the feature tree. The parameters are its inputs.
> Regeneration is re-running the recipe.**

`example/gearbox_manifest.py`'s `build_manifest(params)` is exactly this — a pure
function from parameters to geometry, deterministic by construction (the determinism
suite, #123/#127, already guarantees same-inputs→same-bytes). Formalizing *that* as a
first-class, named, declared-input object is the keystone (§2.1). It gives us
PowerCopy/UDF (a reusable recipe), family tables (a recipe over rows of a table), and
clean regen (re-run with new inputs) — without ever needing a live in-file expression
engine. The text recipe + text params are the source; geometry is a derived artifact,
exactly as a headless/git workflow wants.

---

## 2. Work program

Four themes, in dependency order. Each work item is sized to one GitHub issue.

### Theme A — Parametric hierarchy (master params → driven dims)

**A1. Part recipes — named, parameterized, declared-input build templates.**
The keystone. A *recipe* is a named build function with a declared **input schema**
(its driving parameters, with types/units/defaults/ranges) that deterministically
emits a part (geometry + published interfaces + intent). This is DriftPin's
PowerCopy/UDF *and* its intra-part parametric model in one: "regenerate with new
parameters" = "re-run the recipe." Builds directly on the existing
`build_manifest(params)` pattern and the determinism envelope. Deliverables: a recipe
registry + manifest reference (`{ "recipe": "spur_gear", "inputs": {...} }` alongside
`file`/`manifest`/`library`), input-schema validation at the door (mirrors
`validate_manifest`), and the typed units layer (#102) on inputs. *Unlocks B1, B2.*

**A2. Relations — driving vs driven parameters over the manifest.**
Generalize the resolve step (`sum`/`grid_mm`) into a small, *deterministic*
expression layer: named parameters, formulas (`pitch_d = module * teeth`), a
resolvable dependency **DAG**, and a hard **driving-vs-driven** distinction — driven
values are read-only outputs of a relation, and a value driven twice (table *and*
relation) is a loud error (Creo's "table OR relation, never both"). Stays plain code
the coordinator can reason about (the §13 open question: *where is the line before it
becomes a solver?* — answer it explicitly: arithmetic + lookup, no iterative solve).
Feeds literal driven values into recipe inputs and component slices. *Depends on
nothing; strengthens A1/B1.*

### Theme B — Variant families ("all variants of a gear, fast")

**B1. Design tables — a variant family from a row×column table.**
A *family* is a table where **row = a variant** (keyed by a size designator) and
**column = a parameter / feature-flag / suppression / material**. Materialize the
family deterministically: iterate rows → resolve relations (A2) → run the recipe (A1)
→ emit one part + one item/part-number (C1) per row. "Make all the gears" becomes a
table, not a loop. Subsumes the standard-part catalogs (#101 ISO tables) — a bearing
catalog *is* a family table keyed by designation. Two materialization modes, both
supported: **configurations** (variants share one artifact, cheap) and **instances**
(each variant a released file with its own part number — needed for BOM/revision).
*Depends on A1; A2 optional but recommended.*

**B2. Feature templates — instantiate a captured feature recipe onto new references.**
The feature-level (sub-part) analog of A1: a reusable recipe with declared
**reference-geometry inputs** (a placement frame, an axis, a face tag) plus published
parameters, instantiated repeatedly into new contexts — PowerCopy/UDF/iFeature. An
agent supplies inputs *by name* (frame tags, resolved faces — DriftPin already has
content-addressed `f_`/`e_` tags), no UI picking. Example: a "mounting boss" template
taking `{plane, axis, boss_dia}`. *Depends on A1; reuses the resolve/handle system.*

### Theme C — Design control & release process

**C1. Item model + part numbering — separate the *thing* from the *file*.**
PLM's item/document/file split: introduce an **item** (the logical part — owns a
**part number**, revision, lifecycle, metadata) distinct from the **file**
(`.FCStd`/STEP, the artifact). Default to **non-significant sequential** part numbers
with meaning pushed into queryable JSON metadata (the strong modern best practice —
intelligent numbers run out of space and "become wrong"). A lightweight `items.json`
sidecar (git-friendly) maps item → {part_number, rev, file(s), metadata}; the manifest
references items, not bare paths, removing the "a part is its filename" fragility.
*Foundation for C2/C3.*

**C2. Revision + lifecycle state machine.**
Per item: a **revision** (Rev A/B/C or major.minor — a deliberate released milestone,
distinct from every-save *version*) and a **lifecycle state** —
`in_work → in_review → released → obsolete` — with **permissioned, guarded
transitions** (Released ⇒ immutable; further change forces a *new* revision). Encode
the **Form/Fit/Function predicate** as a deterministic, testable function:
change affects F3 ⇒ new part number; else ⇒ bump revision — an agent can apply it and
a test can gate it. State + transition table live in `items.json`; "is this editable?"
becomes a cheap check any builder runs. *Depends on C1.*

**C3. Change orders + where-used impact + baselines.**
Turn change into a *record*, not a silent mutation. An **ECO** object lists the
**affected items**, the disposition, and an **effectivity** (date / serial / revision).
Compute **where-used / impact** by traversing the lockfile dependency graph
(`depends_on` already exists, §9) — before a change, report every parent that consumes
the item, so the blast radius is known. A **baseline** is a labeled, immutable snapshot
= a pinned `{item: revision}` set (a git tag / lockfile over the item graph) for
reproducible rebuilds. The diff *is* the change order — this maps onto git natively.
*Depends on C1/C2; reuses the §9 lockfile graph.*

### Theme D — File & project organization

**D1. Project/workspace container — make the convention a primitive.**
`MULTI_AGENT.md` §3/§7 describe a directory layout (`manifest.json`, `components/`,
`.dp_lib/`, lockfile) but it is **unenforced convention**. Promote it to a **project
manifest** (a manifest-of-manifests / item registry root) with: a scaffolding tool
(lay out a well-formed project), a **single-source-of-truth / master-skeleton** slot
(the lean interface-geometry master that children subscribe to — top-down design), a
**reference-integrity guard** (catch broken cross-file references *before* a merge —
the chronic PDM failure mode), and naming-convention checks. This is where item
identity (C1), recipes (A1), and families (B1) get a home on disk that survives moves
and renames. *Ties the program together; depends on C1 for item identity.*

---

## 3. Dependency graph & suggested order

```
A2 (relations) ─┐
                ├─> A1 (recipes) ─┬─> B1 (design tables) ─┐
                                  └─> B2 (feature templates)│
C1 (item model) ─┬─> C2 (revision+lifecycle) ─> C3 (ECO/where-used/baseline)
                 └─> D1 (project container) <── B1, A1
```

Recommended sequence: **A1 → A2 → B1** (the parametric+variant spine, immediately
useful and demoable: "generate the whole gear family from one table"), then
**C1 → C2 → C3** (the control layer), with **B2** and **D1** slotting in once A1/C1
exist. A1 and C1 are the two keystones; everything else hangs off them.

## 4. Definition of done (program level)

- A **gear family** (or bearing/fastener catalog) generated from a single design
  table — N variants, deterministic, each with an item + part number — replacing a
  hand-rolled loop. *The headline demo.*
- A part **regenerates** from changed master parameters via its recipe, with
  driving→driven relations resolved and double-driven values rejected loudly.
- An item carries a **revision + lifecycle state**; a Released item is immutable; the
  F3 predicate decides rename-vs-revise and is gate-tested.
- A change emits an **ECO record** with a where-used impact list computed from the
  lockfile graph, and a **baseline** pins a reproducible `{item: rev}` snapshot.
- A **project** scaffolds to a well-formed layout and a reference-integrity check
  catches a broken cross-file reference before merge.
- Two-sided gate-validated (reference passes; every negative caught), in `run_all.sh`,
  no key — the house standard (`MULTI_AGENT.md` §11.x).

## 5. Non-goals (consistent with prior decisions)

- **Live in-file FreeCAD expression links** across files — rejected as fragile-headless
  (`MULTI_AGENT.md` §11.1, §13); the recipe+resolve model replaces them.
- **A general constraint solver** in the resolve/relation layer — A2 stays arithmetic +
  table lookup (the §13 line); iterative geometric constraint solving is out.
- **A PDM server / database / GUI vault** — design control stays *text sidecars on the
  filesystem* (git is the vault), matching the thin-primitive, host-agnostic boundary.
- **Shared co-editing of one file** — still the deferred Phase 4 (`MULTI_AGENT.md` §12).

## 6. Patterns borrowed (sources)

Driving-vs-driven & "table OR relation, never both" (Creo); design/family tables as
row×column variant DBs (SolidWorks/Creo/Inventor iParts); PowerCopy/UDF declared-input
feature templates (CATIA); publish/subscribe interface geometry over interpart
expressions (NX WAVE); rules-as-data variant automation (DriveWorks); F3
rename-vs-revise rule, non-significant part numbers, item/document/file split,
lifecycle state machines, ECO + effectivity + where-used, baselines (PLM/PDM practice —
Arena/PTC/buyplm). Full citations in the research brief accompanying this scoping.

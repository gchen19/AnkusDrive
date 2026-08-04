# Design Hierarchy, Variants & Design Control — scoping

> **✅ Shipped design record (epic #135, closed 2026-07).** This began as a
> *scoping* doc — the §0 verdict below reads "not yet" because that was true when
> it was written (2026-06-28). Every gap it identified has since shipped and the
> verdict is now **yes**:
> [#136 part recipes](../driftpin/recipes.py) (PR #151) ·
> [#137 driving/driven relation DAG](../driftpin/relations.py) (PR #153) ·
> [#138 variant families / design tables](../driftpin/families.py) (PR #156) ·
> [#139 declared-input feature templates](../driftpin/feature_templates.py) (PR #157) ·
> [#140 item model + part numbering](../driftpin/items.py) (PR #150) ·
> [#141 revision + lifecycle state machine](../driftpin/lifecycle.py) (PR #154) ·
> [#142 ECO / where-used / baselines](../driftpin/change.py) (PR #160) ·
> [#143 project container + ref-integrity](../driftpin/project.py) (PR #158).
> Runnable showcase: `example/design_hierarchy_*` (PRs #163, #164). Read the §0
> table below as the *original problem statement*; the "Today" column is a
> historical snapshot, not current state.

Written 2026-06-28. Companion to [`MULTI_AGENT.md`](MULTI_AGENT.md) (which solves
*partition + merge* across a team) and [`ROADMAP.md`](ROADMAP.md) (deepening a
single agent). This doc scopes the **third axis**: making a *design* (not just a
part) — parameter hierarchies that drive geometry, variant families generated from
a table, and the formal release/change control a team needs to evolve a design over
time. It maps the mechanisms proven in SolidWorks / Creo / NX / Inventor / CATIA and
PLM/PDM practice onto DriftPin's deterministic, headless, text-first grain.

The unifying lens — DriftPin is positioned to make MCAD modular the way *software* is
modular (encapsulation, interfaces, composition, versioning) — is drawn out in **§6**;
how every item is checked deterministically and evaluated is **§7**; and the agent-facing
**`design-modularly` skill** that turns these primitives into design *judgment* is **§8**.

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

---

## 6. Modularity — the software module/class-structure view

This is the lens that ties the whole program together, and it is more than an analogy:
the constructs that make *software* modular have exact mechanical-CAD counterparts, and
DriftPin's text-first, deterministic grain lets us implement them more like a programming
language than like a GUI CAD kernel. The keystone fact, from the modularity literature:

> Baldwin & Clark (*Design Rules*, MIT Press, 2000) split a modular design's parameters
> into **visible design rules** — the architecture, interfaces, and integration standards
> every module must obey — and **hidden parameters**, internal decisions that "do not
> affect decisions in other modules." That split *is* **public API vs. private
> implementation.** Ulrich (1995) states the same in mechanical terms: a **modular**
> architecture has a near one-to-one function→component mapping and **decoupled
> interfaces**, so a module changes independently; an **integral** one couples everything.
> That is **low coupling / high cohesion** for steel.

DriftPin already lives on the right side of this split — `publish_interface` is the act of
declaring public API; the baked internal geometry is private. The program below makes the
rest of the software-module toolbox first-class.

### 6.1 The mapping

| Software construct | Mechanical-CAD analog | DriftPin primitive (✅ exists / ⛏ proposed) |
|---|---|---|
| **Information hiding / encapsulation** (Parnas 1972 — hide each likely-to-change decision behind an interface) | Published mating interface (bolt circle, bore, datum frame) is public; wall thickness, ribs, pocketing are private and free to change | ✅ `publish_interface` + `verify_contract`; the recipe (A1) is the encapsulation boundary |
| **Interface / abstract type** (a contract independent of any implementation) | Standardized mating face / ICD — many parts satisfy one interface (NEMA flange, bearing seat) | ✅ typed interfaces (`gear_mesh`/`bore_fit`/`frame_orientation`, `MULTI_AGENT.md` §11.2); ⛏ a named **interface-type registry** (§6.3) |
| **Composition over inheritance** (GoF — HAS-A, swappable parts, over IS-A subclass trees) | An assembly *composes* gears+bearings+housing (each swappable) vs. one monolithic integral casting | ✅ `make_assembly`/`merge_assembly` + hierarchical manifests (§11.4); ⛏ recipes calling sub-recipes |
| **Dependency injection / IoC** (collaborators supplied externally, not hard-constructed) | Parameterize against an injected fit/standard-part spec instead of hard-coding one neighbor | ✅ `library` standard parts (§11.5) + recipe inputs (A1) as the injection point |
| **Semantic versioning** (semver.org — MAJOR = public-API break, MINOR/PATCH = compatible) | Interface-tied revision: moved hole pattern = MAJOR (new part number); added internal rib = PATCH (revise) | ⛏ C2's **Form/Fit/Function predicate** *is* semver's backward-compat rule, made testable |
| **Namespacing / packages** (scoped unique identifiers, no collisions) | Part-number scheme, library/project prefixes, the assembly tree scoping names | ⛏ item model (C1) + project container (D1); fixes today's "two components named *Box* collide" class of bug (`MULTI_AGENT.md` §5) |
| **Class invariants / unit tests** (an object must always satisfy its contract) | "This part is *for* an airtight flow path"; "this seat stays Ø within tol" | ✅ `declare_intent`/`verify_intent` + `verify_contract` (the per-module test) |
| **Performance contracts** (an object must always meet a *quantitative* spec) | "Cd ≤ 0.30 at 30 m/s"; "Δp ≤ 50 Pa at 10 L/min"; "first mode ≥ 200 Hz" | ✅ `declare_performance`/`verify_performance` (issue #226) — metric-agnostic, laddered screen→solver evidence, and a three-state verdict where a band straddling the limit is `indeterminate` rather than a pass. Consulted at every gate since #261 — `merge_assembly` / `substitutability_check` / `component_contract_check` read the recorded verdict, and "not yet verified" is its own reported outcome, never a pass |

Two equivalences are worth stating outright because they convert a fuzzy CAD convention
into a hard, testable rule:

- **Released-immutable = API stability.** A released item cannot mutate (C2); downstream
  consumers can rely on it exactly as a published semver API. Change forces a new
  revision/number, never a silent edit — the same guarantee a pinned dependency gives.
- **Form/Fit/Function = backward compatibility.** F3-preserving ⇒ interchangeable ⇒
  a compatible (MINOR/PATCH) change ⇒ **revise**. F3-breaking ⇒ **new part number** ⇒ a
  MAJOR bump. This makes "did the interface break?" a function a test can evaluate (§7).

### 6.2 Composition over inheritance, concretely

The industry's variant mechanism — multi-level **family tables** (Creo) where an instance
is itself a generic, or nested **configurations** (SolidWorks) — is *inheritance*: a child
derives from a parent and overrides cells. It works, but it inherits inheritance's
problems (fragile base model, override sprawl, the well-documented brittleness of
interpart expression links — NX since v10 *preserves* broken WAVE links rather than
deleting them, precisely because they break so often). DriftPin should **favor composition**:

- A **recipe** (A1) is a module/class: a named build function with a declared public input
  schema. Reuse is *calling* it with new inputs, not subclassing a master.
- A **design table** (B1) is a recipe applied over rows — parameterization, not an
  inheritance hierarchy. The whole gear family is one recipe × one table, flat.
- A **sub-assembly / child manifest** (§11.4) is HAS-A composition; an assembly is built by
  *linking* gated modules, never by one model reaching into another's internals.
- When inheritance-like sharing is genuinely wanted (a base profile every variant extends),
  prefer a **skeleton/master** the children *subscribe* to via published geometry (D1's
  master-skeleton slot) over live cross-file expression links — the same lesson NX WAVE,
  Creo skeletons, and the §11.1 resolve step all converged on: publish/subscribe a control
  structure, don't entangle implementations.

### 6.3 One new primitive worth its own issue — the interface-type registry

Ulrich's three modular types name the interface patterns directly: **slot** (each interface
unique), **bus** (many modules attach to one common interface), **sectional** (all
interfaces identical, parts chain end-to-end). Today a typed interface's contract is
re-specified per manifest. Promote it: a small **interface-type registry** — named,
versioned interface definitions (`nema17_face@1`, `bore_h7@1`) a part *declares conformance
to*, the mechanical analog of `implements SomeInterface`. Benefits mirror the software
case: a part swap is safe if both sides conform to the same interface version; a bus is a
registry entry many parts reference; reuse stops being copy-paste. This is the natural home
for the "standardize the interface, vary the implementation" half of modularity that the
current typed-checks list (per-manifest, ad hoc) only half-covers. *Depends on A1/C1;
strengthens B1 and §11.2.*

### 6.4 Finding the module boundaries (DSM)

Where to cut is itself a modularity question with a known tool: the **Design Structure
Matrix**. Clustering a component-interaction DSM "identifies highly interactive groups…
which can form good modules" and exposes missing interfaces (Eppinger & Browning). This is
the formal statement of the `MULTI_AGENT.md` §10 eval finding — **partition where
constraints couple, not where parts merely multiply** (tchain's shared derivation broke
agents; eight *independent* interface pairs did not). The coupled-constraint cut *is* DSM
clustering: keep a tightly-coupled cluster inside one module (resolve it coordinator-side,
§11.1), and cut along the sparse, decoupled interfaces. The modular-design skill (§8)
should hand the agent this heuristic explicitly.

---

## 7. Determinism, testing & evaluation

Every item above must be checkable the way the rest of DriftPin is: **two-sided
gate-validated (reference passes; every negative caught), runnable free with no API key, in
`run_all.sh`** — the house standard (`MULTI_AGENT.md` §11.x). Modularity is unusually
friendly to this because its core claims *are* deterministic predicates, not judgment
calls. The general shape, then the per-item gates:

- **Determinism is the substrate.** Same recipe + same inputs ⇒ same bytes is already
  guaranteed (the determinism suite, #123/#127). Every new object below is a pure function
  of text inputs, so its test is "run twice, diff bytes" plus "reference inputs → reference
  artifact." Scripted reference builders stand in for agents so the suite runs free —
  exactly as `example/gearbox_manifest.py` and `tests/test_typed_interfaces.py` already do.

| Work item | Reference (must pass) | Negative controls (must each be caught) |
|---|---|---|
| **A1 recipes** | reference inputs → byte-identical reference part; re-run idempotent | out-of-range/typed-wrong input rejected at the door (mirrors `validate_manifest`); missing required input loud |
| **A2 relations** | `pitch_d = module·teeth` computes the known value; infeasible contract fails *before* fan-out | a value driven by **both** table and relation → loud error (the Creo "table OR relation, never both" invariant as a gate-test); cyclic DAG caught |
| **B1 design tables** | N rows → N deterministic parts, each with its item + part number; configuration and instance modes yield identical geometry | a row violating a constraint caught; a duplicate size-key caught; catalog (ISO) row matches the standard table value |
| **C1/C2 item + revision** | F3 predicate classifies a known interface-preserving edit as *revise*, an interface-breaking edit as *new part number* | a mutate attempt on a **Released** item rejected (immutability); an illegal lifecycle transition rejected |
| **C3 ECO / where-used / baseline** | where-used list from the lockfile graph equals a known fixture's parents; a baseline pins `{item: rev}` and a rebuild reproduces identical bytes | a stale consumer of a changed interface flagged (the §9 `stale` mechanism); a baseline rebuild with a drifted input caught |
| **D1 project container** | scaffold → well-formed layout that loads clean | a broken cross-file reference caught *before* merge; a naming-convention violation flagged |
| **§6.3 interface registry** | a part declaring `implements nema17_face@1` passes conformance | a part off-spec on that interface fails conformance; an *unknown* interface type is itself a violation (never a silent pass — same discipline as the typed gates) |

### 7.1 The substitutability gate (the headline modularity test)

The single most valuable new deterministic test, because it operationalizes Form/Fit/
Function as code: **the Liskov-substitutability gate.** Take an assembly that gates green
with variant A in a slot; swap in variant B (a different row of the same family, or a
different part claiming the same interface); re-run `merge_assembly` + all gates. If it
still passes, B is interchangeable with A — *by construction* a compatible (MINOR/PATCH)
change; if a gate now fails, the swap broke Form/Fit/Function and demands a new part number.
This is a purely deterministic check (no API, no judgment) that *proves* the interface
abstraction holds, and it doubles as the test for B1 (every family member must be
substitutable at the shared interface) and §6.3 (registry conformance ⇒ substitutability).

### 7.2 An eval ladder (mirroring `MULTI_AGENT_EVAL.md`)

The substrate tests prove the machinery is correct; an **eval** measures whether handing the
agent these abstractions actually makes it design better — the same Layer-M1 (scripted,
every gate vs. negative controls) / Layer-M2 (live agent vs. baseline) split the multi-agent
work uses. A modularity toy ladder, each with a negative control:

1. **Family regen (headline).** "Generate the whole gear/bearing family from one table" —
   N variants, deterministic, each an item + part number — vs. the hand-rolled Python loop
   baseline. Metric: correctness + lines/edits to change the family (the modularity payoff).
2. **Substitutability.** Swap a module for a same-interface variant; the assembly must still
   gate (§7.1). Negative control: a deliberately off-interface variant must fail.
3. **Encapsulation / no-neighbor-impact.** Change a part's *internal* rib; prove via
   where-used (C3) that no neighbor is stale and a re-merge needs no re-dispatch (§9's
   internal-vs-interface distinction). Negative control: move a *published* frame and prove
   the blast radius is reported.
4. **Interface break is loud.** Make an F3-breaking change without bumping the part number;
   the F3 gate (C2) must reject it. Measures that the immutability/versioning rule bites.

The M2 question is the one the multi-agent evals already taught us to ask: does the
abstraction **reduce rounds-to-converge / raise single-shot pass-rate** vs. a baseline that
hand-rolls the same result (as the resolve step took `tchainu` 2/20 → 20/20)? Re-baseline
per model; keep both conditions on the same total contract and budget (the §10.5 fairness
rule). Determinism throughout: reference builders so the ladder runs in `run_all.sh`.

---

## 8. Agent guidance — a modular-design skill

Primitives gate *correctness*; they do not teach *judgment*. An agent handed recipes,
tables, and an interface registry can still cut the design in the wrong place — make one
giant recipe, publish a bloated interface, double-drive a value, remodel a bolt it should
have pulled from the library. The gates catch some of this *after* geometry is spent; a
**skill** helps the agent avoid it *before*. This is the Knowledge-Based-Engineering /
"rules-as-data" layer (DriveWorks' model: capture the engineer's reasoning as inspectable
rules, not buried code) applied to the agent itself — guidance as a document, host-agnostic,
never enforced by the worker.

**Proposal: ship a `design-modularly` skill** — a `SKILL.md`-style playbook plus the
deterministic checks (§7) it points at, surfaced to whatever host drives the agent. It
should encode, as a checklist the agent runs
*before* decomposing and *before* publishing an interface:

- **Encapsulate what's likely to change** (Parnas). Put the volatile decision — wall
  thickness, internal ribbing, the exact pocket pattern — *behind* the interface; expose
  only the mating contract. Ask: "what will a future change touch, and is it hidden?"
- **Cut where constraints couple, not where parts multiply** (DSM / `MULTI_AGENT.md` §10).
  A shared derivation (a center distance, a grid, a chain total) belongs *inside* one
  module and gets resolved coordinator-side (§11.1); decoupled interfaces are the cut lines.
- **Favor composition over inheritance** (§6.2). Reach for a parameterized recipe + a table
  (flat) and sub-assemblies (HAS-A) before a multi-level family-table hierarchy.
- **Publish the minimal interface.** Every published frame/field is public API you must keep
  stable — narrow it. The smaller the visible design rule set, the freer the hidden
  parameters (Baldwin & Clark).
- **Single source of truth.** A value is an *input* or *derived*, never both — the agent
  must never double-drive (the "table OR relation, never both" invariant, A2).
- **Prefer library/standard parts** over remodeling (§11.5): a bolt's diameter is *computed*,
  not designed, so it can't drift.
- **Use F3 to decide revise-vs-new-number** (C2), and run the substitutability gate (§7.1)
  before claiming a variant is a drop-in.

The skill is the inverse of the gates: the gates are the *test suite*, the skill is the
*style guide / design review*. Together they are how an agent both *can* and *knows how to*
build modularly. (A second, smaller skill — `release-control` — could similarly walk the
C1–C3 item/revision/ECO workflow; bundle or split per how the issues land.)

---

## 9. Patterns borrowed (sources)

Driving-vs-driven & "table OR relation, never both" (Creo); design/family tables as
row×column variant DBs (SolidWorks/Creo/Inventor iParts); PowerCopy/UDF declared-input
feature templates (CATIA); publish/subscribe interface geometry over interpart
expressions (NX WAVE); rules-as-data variant automation (DriveWorks); F3
rename-vs-revise rule, non-significant part numbers, item/document/file split,
lifecycle state machines, ECO + effectivity + where-used, baselines (PLM/PDM practice —
Arena/PTC/buyplm). The §6 modularity framing draws the software↔CAD mapping from the
modularity and software-design literature.

**Software modularity & architecture theory**

- Parnas, *On the Criteria To Be Used in Decomposing Systems into Modules* (1972) —
  information hiding / encapsulation. http://sunnyday.mit.edu/16.355/parnas-criteria.html
- Baldwin & Clark, *Design Rules, Vol. 1: The Power of Modularity* (MIT Press, 2000) —
  visible design rules vs. hidden parameters; modular operators.
  https://direct.mit.edu/books/monograph/1856/
- Ulrich, *The role of product architecture in the manufacturing firm*, Research Policy
  24(3):419–440 (1995) — integral vs. modular; slot/bus/sectional; function→component
  mapping. https://ideas.repec.org/a/eee/respol/v24y1995i3p419-440.html
- Eppinger & Browning, *Design Structure Matrix Methods and Applications* (MIT Press) —
  DSM clustering for module identification.
  https://mitpress.mit.edu/9780262528887/
- *Semantic Versioning 2.0.0* — MAJOR/MINOR/PATCH against a declared public API.
  https://semver.org/
- Fowler, *Inversion of Control Containers and the Dependency Injection pattern*.
  https://martinfowler.com/articles/injection.html
- *Composition over inheritance* (GoF principle).
  https://en.wikipedia.org/wiki/Composition_over_inheritance

**MCAD variant / template / top-down mechanisms**

- PTC — *About Family Tables* (generic/instance, table-driven dims/features).
  https://support.ptc.com/help/creo/creo_pma/r10.0/usascii/fundamentals/fundamentals/About_Family_Tables_1.html
- PTC — *Modifying Dimensions Driven by Relations* (relation-driven ⇒ read-only; the
  "table OR relation" basis).
  http://support.ptc.com/help/creo/creo_pma/usascii/fundamentals/fundamentals/fund_seven_sub/Modifying_Dimensions_Driven_by_Relations.html
- PTC — *About Skeleton Models in Top-Down Design*.
  https://support.ptc.com/help/creo/creo_pma/r12/usascii/assembly/asm/About_Skeleton_Models_in_Top-Down_Design.html
- SOLIDWORKS — *Design Table Configurations*.
  https://help.solidworks.com/2021/english/SolidWorks/sldworks/c_Design_Table_Configurations.htm
- Autodesk — *iPart/iAssembly functions (iLogic)*.
  https://help.autodesk.com/cloudhelp/2022/ENU/Inventor-iLogic/files/GUID-4BA100AA-B55A-4A08-AD8F-79AA27771C7E.htm
- *An Overview of Power Copies and User Features* (CATIA — declared inputs).
  https://www.maruf.ca/files/caadoc/CAAMcaTechArticles/CAAMcaPowerCopyAndUserFeatures.htm
- Applied CAx — *NX WAVE Link Best Practices* (interpart-link fragility).
  https://www.appliedcax.com/wave-linking-best-practices/
- DriveWorks — *What is design automation?* (rules-as-data).
  https://www.driveworks.co.uk/articles/what-is-design-automation/

**PLM/PDM design control**

- Arena — *Intelligent vs. Non-Intelligent Part Numbering*.
  https://www.arenasolutions.com/resources/articles/part-numbering/
- Beyond PLM (Shilovitsky) — *Why Intelligent Part Numbers Must Die*.
  https://beyondplm.com/2023/05/15/why-intelligent-part-numbers-must-die-in-the-future-plm-erp-data-management-best-practices/
- buyPLM — *Form, Fit, Function* (revise vs. new part number).
  https://www.buyplm.com/plm-good-practice/plm-software-part-revision-form-fit-function.aspx
- PTC Windchill — *About Effectivity* (date/serial/revision).
  https://support.ptc.com/help/windchill/plus/r12.0.2.0/en/Windchill_Help_Center/ChgMgmtEffectivityAbout.html

---

## 10. Parallelization plan — waves, owned files, and the integration discipline

The §2 dependency graph reads as a *chain* (A1→A2→B1, C1→C2→C3), which would serialize a
team. It needn't. The fix is the one this whole program preaches and the one
`MULTI_AGENT.md` §11.1 already proved: **fix the shared contracts first, then fan out —
hand each agent a resolved interface, never a derivation.** Almost every dependency here is
a dependency on a *schema* (the recipe-reference shape, the `items.json` shape), not on
working code. Freeze those text schemas + fixtures in a first wave, and the rest of the
work parallelizes against fixtures with no agent blocked on another's geometry.

### 10.1 The two rules that make it parallel-safe

1. **Contracts before code.** Each keystone's *deliverable schema* (a JSON shape + a golden
   fixture + a `validate_*` stub that accepts it) lands **before** its full implementation.
   A downstream item builds against the fixture, exactly as a component builder builds
   against the manifest without seeing its neighbors (`MULTI_AGENT.md` §3). The schema *is*
   the ICD between work items.

2. **One module, one owner — keep agents off the shared files.** The collision hazard is
   real and already in the project's memory (the multi-agent git-worktree hazard, the
   parallel-sprint integration gap): `driftpin/worker.py` and `driftpin/mcp_server.py` are
   giant shared files that *every* item wants to touch to register a handler/tool. So:
   **each work item implements its logic in its own new module** (`driftpin/recipes.py`,
   `driftpin/items.py`, `driftpin/families.py`, …) exposing a single registration function;
   the only shared edit is **one import + one `register()` line** per feature, appended (not
   interleaved) to `worker.py`/`mcp_server.py`. Append-only touches to a shared file rarely
   conflict; interleaved edits to `merge_assembly` do. Run each builder in a **worktree**,
   and re-run the **full suite on integrated `main`** after the sprint (not just per-PR
   green) — the two lessons already paid for.

### 10.2 The wave board

Each item names the module it **owns** (no other agent writes it), the schema/fixtures it
**consumes**, and who it is **parallel-safe with**.

| Wave | Item | Owns (new module) | Consumes | Notes |
|---|---|---|---|---|
| **0 — keystones** (run first, parallel *with each other*; publish schema+fixture in PR #1) | **A1** recipes | `driftpin/recipes.py` + recipe-ref schema | units (#102) | emits the `{recipe,inputs}` contract B1/B2/registry build on |
| | **C1** item model | `driftpin/items.py` + `items.json` schema | — | emits the item-ref contract B1/C2/C3/D1 build on |
| **1 — fan out** (all parallel once Wave 0 schemas are frozen; each owns a disjoint module) | **A2** relations | resolve layer in `driftpin/manifest.py` | param-dict shape | independent of A1; meet at "driven values → recipe inputs" |
| | **B1** design tables | `driftpin/families.py` + table format | A1, C1 (fixtures) | the headline demo |
| | **B2** feature templates | `driftpin/feature_templates.py` | A1 (fixtures) | reuses `resolve_face`/`f_`,`e_` tags |
| | **C2** revision + lifecycle | `driftpin/lifecycle.py` (+ items.json fields) | C1 (fixtures) | F3 predicate is pure + unit-testable |
| | **M1** interface-type registry (§6.3) | `driftpin/iface_registry.py` | A1, C1 (fixtures) | strengthens B1 + the §11.2 typed gates |
| | **D1** project container | `driftpin/project.py` | C1 (fixtures) | scaffold + ref-integrity guard |
| | **T1** substitutability gate (§7.1) | `driftpin/gates/substitutability.py` | `merge_assembly` (exists) | builds against existing typed-interface fixtures *today* |
| | **G1** design-modularly skill (§8) | `docs`/skill files only | — | **zero code dep — can start immediately**, fully parallel |
| **2 — integration** (need multiple Wave-1 outputs) | **C3** ECO / where-used / baseline | `driftpin/change.py` + baseline format | C1, C2, lockfile (§9) | reuses the `depends_on` graph |
| | **E1** modularity eval ladder (§7.2) | `tests/MODULARITY_EVAL.md` + scripted builders | A1, B1, C2, T1 | capstone; mirrors `MULTI_AGENT_EVAL.md` |

### 10.3 Critical path & crew sizing

- **Critical path:** Wave 0 (A1 *or* C1, whichever is slower) → its longest Wave-1 dependent
  (B1 or C2) → Wave 2 (C3 / E1). Three serial hops, not the twelve a naïve chain implies.
- **Max useful concurrency:** 2 agents in Wave 0, up to **8** in Wave 1, 2 in Wave 2. G1
  (skill) and A2 (relations) have no Wave-0 dependency and can launch on day one alongside
  the keystones — so even Wave 0 runs ≥4 wide.
- **The F3 predicate, the substitutability gate (T1), and the skill (G1)** are pure
  logic/doc with no geometry dependency — assign them early to keep agents productive while
  the keystone schemas settle.

### 10.4 Definition of done (parallelization)

- Wave 0 ships each keystone's schema + golden fixture + `validate_*` stub in its first PR,
  and a Wave-1 item is demonstrated building green against that fixture **before** the
  keystone's full implementation lands (proving the contract, not the code, was the dep).
- No two merged PRs in a sprint contain conflicting edits to `worker.py`/`mcp_server.py`
  beyond appended registration lines; the full `run_all.sh` passes on integrated `main`.

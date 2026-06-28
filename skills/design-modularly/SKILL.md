---
name: design-modularly
description: >-
  Run this BEFORE you decompose a design into modules and BEFORE you publish an
  interface. A rules-as-data design review that helps you cut the design in the
  right place — encapsulate what changes, cut at decoupled interfaces, compose
  instead of subclass, publish a minimal interface, keep one source of truth,
  pull library parts, and version by Form/Fit/Function. The deterministic gates
  (verify_contract, the substitutability gate, the F3 predicate) are the test
  suite that catches a bad cut after geometry is spent; this skill is the style
  guide that avoids it beforehand. Use it whenever you are about to split work,
  call publish_interface, write a recipe, build a variant family, or claim a part
  is a drop-in replacement.
---

# design-modularly

Primitives gate *correctness*; they do not teach *judgment*. You can be handed
recipes, items, tables and an interface registry and still cut the design in the
wrong place — make one giant recipe, publish a bloated interface, double-drive a
value, remodel a bolt you should have pulled from the library. The gates catch
some of this *after* geometry is spent. This skill helps you avoid it *before*.

It is the Knowledge-Based-Engineering / "rules-as-data" layer (DriveWorks'
model: capture the engineer's reasoning as inspectable rules, not buried code)
applied to the agent itself. It is guidance, host-agnostic, **never enforced by
the worker** — the gates do the enforcing. Think of it as the design review you
run on yourself.

> **The one-line frame:** Baldwin & Clark split a modular design into *visible
> design rules* (architecture + interfaces every module must obey) and *hidden
> parameters* (internal decisions that do not affect other modules). That split
> is **public API vs. private implementation.** Your job at every cut is to make
> the visible set as small as possible and hide everything else.

## When to run this

Run the checklist at three moments, every time:

1. **Before decomposing** — before you split a problem into modules / sub-recipes
   / parallel agents. (Rules 1, 2, 3.)
2. **Before publishing an interface** — before you call `publish_interface` or
   declare a typed interface in a manifest. (Rules 4, 5.)
3. **Before claiming a drop-in** — before you swap a variant into a slot or call
   a new revision interchangeable. (Rules 6, 7.)

If you cannot answer the question in **bold** under a rule, stop and resolve it
before spending geometry.

---

## The checklist

### 1. Encapsulate what's likely to change (Parnas)

Put the volatile decision — wall thickness, internal ribbing, the exact pocket
pattern, the cooling-channel layout — *behind* the interface. Expose only the
mating contract (the bolt circle, the bore, the datum frame).

> **Ask: "what will a future change touch, and is it hidden?"** List the
> decisions most likely to change; if any of them is visible across a module
> boundary, you have leaked an implementation detail — pull it back inside.

- **Leans on:** the **recipe** (#136, `driftpin/recipes.py`) is the encapsulation
  boundary — a named build function with a *declared public input schema*; its
  baked internal geometry is private. `publish_interface` is the act of declaring
  what is public.
- **Gate that catches the violation:** `verify_contract` — the per-module unit
  test that the published seat/face still holds tolerance. If a neighbor depends
  on something you did not publish, the dependency is illegitimate.
- **Do:** expose `bore_d`, hide the rib pattern that reaches it.
  **Don't:** let a neighbor reference your internal pocket — that freezes a
  decision you wanted free.

### 2. Cut where constraints couple, not where parts multiply (DSM)

A shared derivation — a center distance, a grid pitch, a chain-length total —
belongs *inside one module* and is resolved **coordinator-side** (the resolve
step). Decoupled interfaces are the cut lines. The Design Structure Matrix names
this: cluster the tightly-coupled group into one module, cut along the sparse,
decoupled interfaces.

> **Ask: "does this cut sever a shared derivation, or a clean interface?"** If
> two proposed modules both need to compute the same value, you have cut through
> a coupled constraint — merge them, resolve the value once, and hand each side
> the *resolved* slice, never the derivation.

- **Leans on:** the resolve step (`MULTI_AGENT.md` §11.1) — shared parameters
  become resolved slices before fan-out. Relations (#137) are how a derived value
  is computed once, coordinator-side.
- **Why it bites:** the multi-agent evals proved it — `tchain`'s *shared
  derivation* broke agents (2/20) until a resolve step handed each a resolved
  value (20/20); eight *independent* interface pairs never broke. Coupled
  constraint = bad cut; decoupled interface = good cut.
- **Do:** compute center distance once, give each gear its resolved seat.
  **Don't:** hand two agents the same `module·(z1+z2)/2` to each re-derive — they
  will disagree on rounding and the mesh will not close.

### 3. Favor composition over inheritance (§6.2)

Reach for a **parameterized recipe + a flat table + sub-assemblies (HAS-A)**
before a multi-level family-table hierarchy (IS-A). Inheritance — Creo family
tables where an instance is itself a generic, SolidWorks nested configurations —
works but inherits inheritance's problems: fragile base model, override sprawl,
brittle interpart links (NX *preserves* broken WAVE links rather than deleting
them, precisely because they break so often).

> **Ask: "am I subclassing a master, or calling a function with new inputs?"**
> Prefer the latter.

- **Leans on:** a recipe (#136) is a module/class — reuse is *calling* it with
  new inputs, not subclassing. A design table (#138 families) is one recipe × one
  table, flat. A sub-assembly / child manifest (`make_assembly` / `merge_assembly`)
  is HAS-A composition — link gated modules, never reach into another's internals.
- **When inheritance-like sharing is genuinely wanted** (a base profile every
  variant extends): prefer a **skeleton/master the children subscribe to via
  published geometry** over live cross-file expression links — the lesson NX WAVE,
  Creo skeletons and the resolve step all converged on: publish/subscribe a
  control structure, don't entangle implementations.
- **Do:** `spur_gear` recipe applied over a 12-row table → the whole family, flat.
  **Don't:** a generic gear with 12 instance-overrides nested two levels deep.

### 4. Publish the minimal interface

Every published frame/field is **public API you must keep stable** — narrow it.
The smaller the visible design-rule set, the freer the hidden parameters.

> **Ask: "is every frame/field I'm publishing actually a mating contract, or am
> I leaking convenience?"** Delete anything a consumer does not strictly need to
> mate. If you publish it, you have promised not to move it without a major bump.

- **Leans on:** `publish_interface` declares the public API; the interface-type
  registry (#146) lets a part *declare conformance to* a named, versioned
  interface (`nema17_face@1`, `bore_h7@1`) — the mechanical analog of
  `implements SomeInterface`. Conform to a registry type instead of re-specifying
  the contract per manifest.
- **Gate that catches the violation:** registry conformance + the
  substitutability gate (#147). A bloated interface makes more changes
  "interface-breaking" than they need to be, shrinking your room to revise.
- **Do:** publish the bore axis + seat plane.
  **Don't:** publish the whole outer profile because it was easy.

### 5. Single source of truth — a value is an input *or* derived, never both

Never double-drive a value. If `pitch_d = module · teeth`, it is **derived** —
it must not also appear as a table column you set by hand. This is the Creo
"table OR relation, never both" invariant.

> **Ask: "is this value an input I set, or a relation I compute? Pick one."** If
> a number can be reached by two paths, the two paths *will* drift.

- **Leans on:** recipe inputs (#136) are the *input* path; relations (#137) are
  the *derived* path. A value lives in exactly one.
- **Gate that catches the violation:** the relations gate — a value driven by
  **both** a table and a relation is a loud error (cyclic DAGs caught too). The
  determinism suite (#123/#127) makes "same inputs ⇒ same bytes" the substrate, so
  a double-driven value is a contradiction the build refuses.
- **Do:** set `module` and `teeth`; let `pitch_d` be computed.
  **Don't:** set `module`, `teeth`, *and* `pitch_d` — now a row can lie.

### 6. Prefer library / standard parts over remodeling

A bolt's diameter is **computed, not designed**, so it cannot drift. Pull
fasteners, bearings, o-rings, gears-to-standard from the library before you model
geometry by hand. Injecting a standard-part spec is dependency injection — the
collaborator is supplied, not hard-constructed.

> **Ask: "is there a standard/library part that satisfies this interface?"** If
> yes, pull it and parameterize against its spec. Only model from scratch when no
> standard fits.

- **Leans on:** the `library` of standard parts (`add_fastener`, `add_bearing`,
  `add_gear`, …) + recipe inputs as the injection point. A computed size (from
  `bolted_joint_check`, `bearing_life`, `gear_rating`) feeds the library selector;
  you never type a diameter you should have derived.
- **Why it bites:** a remodeled bolt is a private re-derivation of a public
  standard — it can drift out of spec silently, and it is geometry you spent for
  nothing.
- **Do:** size the joint, then `add_fastener(M6)` from the library.
  **Don't:** sweep a custom thread because it was faster than looking it up.

### 7. Use F3 to decide revise-vs-new-number, and run the substitutability gate before claiming a drop-in

**Form / Fit / Function = backward compatibility.** An F3-preserving edit is
interchangeable ⇒ a compatible (MINOR/PATCH) change ⇒ **revise** the item. An
F3-breaking edit ⇒ **new part number** ⇒ a MAJOR bump. A *Released* item is
immutable — it cannot mutate; a change forces a new revision/number, never a
silent edit (a pinned-dependency guarantee).

> **Ask, before calling a variant a drop-in: "does this preserve Form, Fit and
> Function? Did I actually run the substitutability gate?"** Don't claim
> interchangeability you haven't tested.

- **Leans on:** the lifecycle / F3 predicate (#141) classifies the edit;
  released-immutable enforces it. The substitutability gate (#147) is the headline
  modularity test — the Liskov check made deterministic: take an assembly that
  gates green with variant A in a slot, swap in variant B (another row of the
  family, or any part claiming the same interface), re-run `merge_assembly` + all
  gates. Still green ⇒ B is interchangeable *by construction*; a gate now fails ⇒
  the swap broke F3 and demands a new part number.
- **Gate that catches the violation:** the F3 gate rejects an F3-breaking change
  that did not bump the part number; the substitutability gate proves (or
  disproves) the drop-in claim with no judgment and no API key.
- **Do:** added an internal rib → F3-preserving → revise (PATCH).
  **Don't:** move the hole pattern and call it the same part — that's a new number.

---

## Pre-flight summary (the thirty-second version)

Before you decompose or publish, confirm all seven:

- [ ] **Encapsulated** — volatile decisions are hidden behind the interface (#136).
- [ ] **Cut at decoupled interfaces** — no shared derivation severed; coupled
      constraints resolved coordinator-side (`MULTI_AGENT.md` §11.1, #137).
- [ ] **Composed, not subclassed** — recipe + flat table + sub-assemblies, not a
      family-table hierarchy (#136/#138).
- [ ] **Minimal interface** — every published frame/field is a real mating
      contract; conform to a registry type (#146).
- [ ] **Single source of truth** — every value is an input *xor* derived (#137).
- [ ] **Library parts** — standard parts pulled and computed, not remodeled.
- [ ] **F3 + substitutability** — revise-vs-new-number decided by Form/Fit/
      Function; drop-in claims run through the substitutability gate (#141/#147).

## The skill is the inverse of the gates

| The gates (test suite, *after*) | This skill (style guide, *before*) |
|---|---|
| `verify_contract` — module holds its contract | Rule 1: encapsulate what changes |
| relations gate — no double-driven value | Rule 5: single source of truth |
| substitutability gate (#147) — swap still green | Rules 4 & 7: minimal interface, drop-in |
| F3 predicate (#141) — interface break is loud | Rule 7: revise-vs-new-number |
| determinism suite (#123/#127) — same inputs, same bytes | the substrate all of the above stand on |

Together they are how you both *can* and *know how to* build modularly.

## Companion skill

`release-control` (sibling file `../release-control/SKILL.md`) walks the
item → revision → ECO workflow (#140 / #141 / #142) once you have decided to
revise or new-number a part.

## Sources

Parnas 1972 (information hiding); Baldwin & Clark, *Design Rules* (2000, visible
rules vs. hidden parameters); Ulrich 1995 (integral vs. modular; slot/bus/
sectional); Eppinger & Browning, *DSM Methods and Applications* (clustering for
module identification); semver.org; Fowler on Dependency Injection; GoF
composition-over-inheritance; Creo "table OR relation, never both"; NX WAVE link
fragility; DriveWorks rules-as-data; buyPLM Form/Fit/Function. Full citations:
`docs/DESIGN_HIERARCHY.md` §6–§9.

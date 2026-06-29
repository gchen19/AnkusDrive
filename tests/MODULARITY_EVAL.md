# Modularity eval ladder — evaluation plan

How we decide whether handing the agent DriftPin's **modularity** abstractions —
recipes, design-table families, the substitutability gate, the lockfile change
detector, and the Form/Fit/Function predicate — actually makes it *design better*.
Companion to [`MULTI_AGENT_EVAL.md`](MULTI_AGENT_EVAL.md) (a *team* that partitions
and merges) and the scoping doc
[`docs/DESIGN_HIERARCHY.md`](../docs/DESIGN_HIERARCHY.md) §7.2 (this ladder), §7.1
(the substitutability gate), §6 (the software-module lens) and §10 (fairness).

This is the **capstone eval** for the design-hierarchy epic (#135). Where the
substrate tests (§7 of the scoping doc) prove each mechanism is *correct*, this
ladder measures whether the abstraction *pays off*: the same Layer-M1 (scripted,
every gate vs. negative controls) / Layer-M2 (live agent vs. a hand-rolled
baseline) split the multi-agent work already uses.

---

## Why modularity is friendly to a deterministic eval

The keystone fact from the modularity literature (Baldwin & Clark, *Design Rules*,
2000): a modular design splits its parameters into **visible design rules** — the
architecture, interfaces, integration standards every module obeys — and **hidden
parameters**, internal decisions that "do not affect decisions in other modules."
That split *is* public API vs. private implementation. Every claim modularity makes
is therefore a **deterministic predicate**, not a judgment call:

- *Is this family one recipe over a table?* — count the edits to change it.
- *Is B substitutable for A?* — swap and re-run the gates (§7.1).
- *Did this internal change touch a neighbor?* — ask the lockfile (§9).
- *Did the interface break?* — the Form/Fit/Function predicate (#141) is the rule.

So most of this ladder needs **no model at all**, and the part that does (Layer M2)
is graded against the same hard oracle, not a vibe — exactly the move
`MULTI_AGENT_EVAL.md` rests on.

---

## Two layers

### Layer M1 — mechanism (no LLM, runs in CI)

Does the substrate work, deterministically, when the builders are *scripted*? Every
M1 toy ships three things, mirroring `MULTI_AGENT_EVAL.md`:

1. the **abstraction** under test (a recipe + table, a manifest + slot, a lockfile,
   an item + F3 predicate);
2. a **reference** path — the modular path works (the gate readings a correct,
   modular build produces);
3. **negative controls** — a deliberately broken input the gate that owns the
   failure mode must catch.

M1 asserts: every reference **passes**; every negative control is **caught** by the
gate that owns its failure mode. No API key; lives in `run_all.sh`
(`tests/modularity_toys.py` + `tests/test_modularity_eval.py`).

### Layer M2 — agents in the loop (gated, needs key) — methodology, not run here

The real question, the one the multi-agent evals taught us to ask: does handing the
agent these abstractions **reduce rounds-to-converge / raise the single-shot
pass-rate** vs. a baseline that hand-rolls the same result (as the resolve step took
`tchainu` **2/20 → 20/20**)? Each condition gets a fresh model, a contract, and a
budget; the deterministic gates below are the oracle for both. The protocol is
documented here as methodology; it is **not** run in the suite (it costs API
credits, like Layer M2 of the multi-agent work).

The **fairness rule** (§10.5): re-baseline per model, and keep both conditions on
the **same total contract and budget** — the modular condition is given the recipe /
table / gate tools, the baseline condition the raw geometry tools, but neither gets
more turns or a richer spec than the other. A win only counts if the abstraction,
not the budget, moved the number.

---

## The toy ladder

Each toy isolates one modularity property and has a deterministic oracle. **All four
are built and green** in Layer M1 (`tests/test_modularity_eval.py`), exercising the
real tools (`family_materialize`, `substitutability_check`, `assembly_lock` /
`assembly_lock_check`, and the `lifecycle.form_fit_function` / `apply_change`
predicate).

| # | Toy | Property | Oracle (reference passes) | Negative control (caught) |
|---|-----|----------|---------------------------|---------------------------|
| 1 | **Family regen** (headline) | composition over inheritance — one recipe × a table | family_materialize builds N items+part-numbers; geometry identical to a hand-rolled loop | a bad row (teeth below the recipe min) refuses the whole family, naming row+column |
| 2 | **Substitutability** | decoupled interface — swap the implementation | a same-interface variant swap keeps the assembly green (revise) | an off-interface variant FAILS, NAMING the broken gate (new part number) |
| 3 | **Encapsulation** | information hiding — internal change ≠ neighbor impact | an internal pocket: file modified, interface intact, **no** stale neighbor (no re-dispatch) | a moved *published* frame leaves the mating lid **stale** — the blast radius is reported |
| 4 | **Interface break is loud** | semver — F3 = backward compatibility | an internal-only edit classifies as **revise** (same part number) | an F3-breaking edit with **no** part-number bump is **rejected** (`apply_change` demands a new number) |

### Toy 1 — family regen, and the measured payoff

The modular design is **one shared recipe + a row of data per variant**; the
hand-rolled baseline (the `example/gearbox_manifest.py` shape) is **N independent
inline blocks**, each re-spelling the build rule and its own bookkeeping (part
number, item, file path). Both build byte-identical geometry — that is the
correctness half. The payoff half is **edits-to-change-the-family**, measured off
the two representations:

- **Change the family-wide build rule** (e.g. "every gear also publishes a bore
  interface"): the modular path edits the **one** recipe; the baseline edits **every
  copy**. For the 4-variant family in the toy: **1 edit vs. 4** (an N× payoff that
  grows with the family).
- **Add a member**: the modular path appends **1** table row (item, part number and
  file path are *derived* by `family_materialize`); the baseline must hand-write the
  build block **plus** all three bookkeeping fields — **1 vs. 4**.

This is the modularity payoff made countable — Baldwin & Clark's visible-rule /
hidden-parameter split: the recipe is the one place the visible build rule lives.

### Toys 2–4 — the gates as oracle

- **Toy 2** is the §7.1 Liskov gate operationalized: `substitutability_check` drives
  the real `merge_assembly` twice (baseline + swap) and diffs the gate outcomes. A
  same-interface swap stays green ⇒ *by construction* a compatible (MINOR/PATCH)
  change ⇒ revise; a gate that fails on the swap is named ⇒ a MAJOR break ⇒ a new
  part number. The negative control here is the whole point: a gate that never fails
  is worthless.
- **Toy 3** uses the existing lockfile mechanism (`MULTI_AGENT.md` §9): a file whose
  bytes changed but whose published interfaces are intact is `modified` but not
  `interface_changed`, and no neighbor goes `stale` — a re-merge needs no
  re-dispatch. Move a *published* frame and the mating neighbor is flagged `stale`
  with `ok=False`: the blast radius is reported, not silently absorbed.
- **Toy 4** is pure Python — the Form/Fit/Function predicate (#141) as code. The
  bite is the negative control: `apply_change` on a *Released* item refuses an
  F3-breaking edit unless it is renumbered (a Released item's interface can never
  silently change), while an internal-only edit is accepted in place as a clean
  revise (same part number, bumped revision).

---

## Metrics

| Metric | Definition | Layer | Gate |
|---|---|---|---|
| **Reference-pass** | the modular path clears every gate | M1 | hard pass |
| **Negative-catch** | every negative control is caught by the gate that owns it | M1 | hard pass (the trust-killer if it isn't) |
| **Edits-to-change-the-family** | source edits to apply a family-wide change | M1 | modular ≪ baseline (measured N×) |
| **Determinism** | same toy twice → same readings | M1 | hard pass |
| **Single-shot pass-rate** | % of agent runs whose first build clears every gate | M2 | modular ≥ baseline |
| **Rounds-to-converge** | renegotiation cycles to all-green (1 = first try) | M2 | modular ≤ baseline |
| **Cost vs. baseline** | tokens + wall-clock, same contract + budget (§10.5) | M2 | the abstraction must justify itself |

The two non-negotiable comparisons carry over from `MULTI_AGENT_EVAL.md`:
**false-pass against the negative controls must be ~0** (a silent interface break is
worse than a loud build failure), and **the abstraction must beat the hand-rolled
baseline** under the same budget or the toy is telling us the abstraction does not
earn its keep.

---

## Running

```bash
# Layer M1 — free, deterministic, in run_all.sh
.venv/bin/python3 tests/test_modularity_eval.py
```

Layer M2 is documented above as methodology; it is not wired into the suite (it
costs API credits, like the multi-agent Layer M2). When run, it re-baselines per
model and holds both conditions to the same contract + budget (§10.5).

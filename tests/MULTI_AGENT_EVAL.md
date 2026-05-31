# Multi-agent partition+merge — evaluation plan

How we decide whether a *team* of agents can split a design, build the pieces
independently, and merge them into a product that actually fits. Companion to the
RFC [`docs/MULTI_AGENT.md`](../docs/MULTI_AGENT.md) and the single-agent
[`RELIABILITY.md`](RELIABILITY.md) harness.

---

## Why this is different (and easier) than Layer A/B/C

`RELIABILITY.md` grades a hard question — "does this part *look* right?" — with a
soft oracle (a vision model + keyword match). Partition+merge has the opposite
shape: the question "do these pieces fit?" has a **hard geometric oracle**. The
merge gates *are* the ground truth:

- `interference_check` → do parts collide?
- envelope / keep-out → did a component exceed the box it promised neighbors?
- recursive `bom_extract` → right parts, right counts?
- `mass_properties` rollup → right total mass / CG?
- bolt-circle / frame coincidence → do mating interfaces line up?

All numeric, all deterministic, all runnable without an API key. So most of this
suite needs **no model at all** — and the part that does (real agents building to
a contract) is graded against the same hard oracle, not a vibe.

The whole eval rests on one move: **prove the oracle is trustworthy first, then
trust the agent numbers it produces.**

---

## Two layers

### Layer M1 — mechanism (no LLM, runs in CI)

Does the substrate — manifest → merge → gates — work, deterministically, when the
builders are *scripted*? `example/phase0_walkthrough.py` is the seed of this layer
already. M1 answers "is the machinery sound and do the gates discriminate?" before
a single agent is trusted to it. No API key; belongs in `run_all.sh`.

Every M1 toy ships three things:

1. **Manifest** — the interface contract (the ICD from RFC §3).
2. **Reference solution** — scripted, ground-truth-correct builders. Proves the
   toy is solvable and *defines the oracle baseline* (the gate readings a correct
   build produces).
3. **Negative controls** — deliberately broken builders (peg too fat, bolt circle
   offset, bracket oversized, component built against a stale contract). These are
   the point: a gate that never fails is worthless.

M1 asserts: every reference solution **passes** all gates; every negative control
is **caught** by the gate that owns its failure mode. Plus determinism (same toy,
twice, same result — the discipline in `test_determinism.py`).

### Layer M2 — agents in the loop (gated, needs key)

Real agents each receive a manifest slice and build their component independently;
a coordinator merges and runs the gates; on failure it renegotiates and
re-dispatches. Gated behind `RUN_RELIABILITY=1` + an API key, alongside Layer
A/B/C. Reuses their cache discipline: cache scripted/reference builds and renders,
spend tokens only on the agent calls.

M2 answers the real question: **does partitioning work when LLMs drive it, and is
it better than one agent doing the whole thing?**

---

## Metrics

Reported per toy and in aggregate.

| Metric | Definition | Gate |
|---|---|---|
| **Merge-pass rate** | % of agent runs whose merged product clears every gate | headline number |
| **False-pass rate** | gates pass but the reference oracle says the build is wrong | **must be ~0** — the trust-killer |
| **Rounds-to-converge** | renegotiation cycles until all gates pass (1 = first try) | lower is better |
| **Interface-match accuracy** | per interface: published frame / bolt circle within tol of contract | ≥ threshold |
| **Determinism / variance** | same toy + same seed → same outcome; bounded variance | hard pass (M1) |
| **Cost vs. single-agent** | tokens + wall-clock vs. one agent building the whole toy | must justify the split |

### The two non-negotiable comparisons

- **False-pass against negative controls.** If the gates ever green-light a broken
  variant, partition+merge cannot be trusted — a silent fit failure is worse than a
  loud build failure. Measured in M1 (scripted breaks) and M2 (agent mistakes that
  the oracle catches but the gates miss).
- **Single-agent baseline.** Run *every* toy both ways: N agents partition+merge
  vs. one agent building it whole. The RFC's premise (parallelism, smaller
  per-agent context, isolation) only holds if multi-agent is at least as reliable.
  If one agent is just as good and cheaper, the toy is telling us not to partition
  it.

---

## The toy ladder

Each isolates one failure mode and has a deterministic oracle. **All six are built
and green** in Layer M1 (`tests/multiagent_toys.py` + `tests/test_multiagent_m1.py`),
exercising the real tools (`merge_assembly`, `publish_interface` + mate-by-frame,
recursive BOM/interference, `envelope_check`, `interface_align_check`,
`assembly_lock`/`assembly_lock_check`).

| # | Toy | Isolates | Oracle | ✓ |
|---|-----|----------|--------|---|
| 1 | **Peg-in-hole** (clearance fit) | a shared *dimension* contract | clearance > 0, no interference | ✓ |
| 2 | **Bolted flange** (bolt circle) | a shared *parametric* interface | every bolt clears both plates | ✓ |
| 3 | **Bracket → housing face + keep-out** | mating face + **envelope gate** | bracket bbox ⊂ envelope, no interference | ✓ |
| 4 | **Nested subassembly** (A+B under C) | fan-in nesting + recursive BOM | BOM flattens to leaves, no cross-level interference | ✓ |
| 5 | **Enclosure: lid mates to housing** | *multiple* interfaces per part | both interfaces coincide (mate + align gate) | ✓ |
| 6 | **Move an interface mid-design** | **change propagation** (RFC §9) | lockfile flags only the stale neighbors | ✓ |

Toy #6 is the odd one: it's about file *versions over time*, not one merged
assembly's gates, so it has its own driver (`toy6_*`) and `test_change_propagation`
rather than the Variant/`run_gates` shape. Its ancestor is
`example/phase0_walkthrough.py` — a peg-*on*-plate with an embedded-peg clash as a
first negative control; toy #1 proper tightens it to a clearance fit with a
*shared diameter* contract.

### Negative controls per toy (the actual work)

- **#1** peg Ø > hole Ø (interference); peg off-axis (misalignment); peg too short (no engagement).
- **#2** bolt circle pitch wrong; pattern rotated; hole count mismatch.
- **#3** bracket exceeds envelope; mating face at wrong Z; bolt pattern offset.
- **#4** middle part flipped; BOM count off by one; cross-level overlap.
- **#5** lid bore smaller than shaft; one of two interfaces satisfied but not the other.
- **#6** neighbor *not* re-dispatched after an interface move → stale component slips through (lockfile catches it: file hash + interface-frame hash + mate deps → `stale`).

A toy is "done" when its gates pass **only** the reference solution and catch
**every** negative control. Until then its agent numbers are meaningless.

---

## Layout (proposed)

Mirrors the existing reliability files so it slots into the same harness:

```
tests/
  multiagent_toys.py          # toy registry: manifest + reference + negative builders
  test_multiagent_m1.py       # Layer M1 — scripted, no key, in run_all.sh
  test_multiagent_m2.py       # Layer M2 — agents in loop, gated RUN_RELIABILITY=1
  multiagent_cache/           # gitignored — cached reference builds + renders
```

M1 runs in the default suite (it's just geometry + assertions). M2 is gated exactly
like Layer A/B/C and shares `reliability_cache` conventions.

---

## Build order

1. **Promote the walkthrough into M1.** Toys #1–3 with reference solutions + 2–3
   negative controls each; assert gates catch every break and pass every reference;
   add a determinism check. No key. This validates the whole mechanism and proves
   the oracle — the prerequisite for trusting anything else.
2. **Add toys #4–6** as the merge primitives land in RFC Phase 1 (recursive BOM,
   mate-by-frame, change propagation each unlock a toy).
3. **Stand up M2** once a key is available (tracked in
   `project_driftpin_reliability.md`): run the M1 toys with real agents, report the
   metrics above, always against the single-agent baseline.

The order is deliberate: never measure agent reliability against an oracle you
haven't first proven catches a wrong answer.

---

## M2 results so far (live agent runs)

All runs use `tests/test_multiagent_m2.py`, gated behind `RUN_RELIABILITY=1` + a key
(the `anthropic-key` shell helper). Each toy runs in two conditions — **partition**
(one cold agent per component) and **single** (one agent builds every component in
sequence) — judged by the deterministic merge gates. `built` = agent(s) produced +
saved geometry; `pass` = the merged assembly cleared every gate.

### Gate validity (free, no API)

`M2_SELFTEST=1` shows every toy's gate **passes a scripted correct build AND catches
each scripted negative**; `M2_DRYRUN=1` exercises the full agent loop with a stubbed
model. Both green for all four toys — so a passing agent genuinely built a fitting
part and a failing one genuinely didn't. (The eval plan's non-negotiable: never
measure agents against an oracle you haven't shown catches a wrong answer.)

### Easy toys — at the ceiling (Haiku 4.5, n=5)

| Toy | partition | single | shared contract |
|---|---|---|---|
| peg-in-hole | 5/5 | 5/5 | one dimension (bore Ø) |
| bolted flange | 5/5 | 5/5 | a bolt circle |
| bracket+housing | 5/5 | 5/5 | a keep-out size cap |

**100% both conditions → the comparison is uninformative here.** A single shared
*value* is easy enough that Haiku never misses, so there's no gap to attribute to
partitioning. Negative-control runs (`M2_NEGATIVES=1`, n=3) confirmed agents
faithfully build the *wrong* thing and the gate catches it 3/3 on every toy — the
100% is real, not a rubber stamp.

### Hard toy — off the ceiling (Haiku 4.5, n=30/condition, ~$2.0)

`twopin` — two pins must seat into two holes simultaneously. Two-point alignment
removes the rotational slack a single peg hides error in, and both agents must
independently derive the same `x = center ± spacing/2`.

| condition | pass | 95% CI (Wilson) |
|---|---|---|
| partition | 17/30 = **57%** | [39%, 73%] |
| single | 18/30 = **60%** | [42%, 75%] |

**diff = −3%, two-proportion z = −0.26, p = 0.79 → statistically indistinguishable.**
`built` was 30/30 in both. Failure magnitudes near-identical across conditions:
mostly small near-misses (151/603 mm³ — a pin grazing hole material) plus a few
gross errors (~3k–12k mm³ — a part built fundamentally wrong), in both.

**Reading.** Two-point alignment drops Haiku from 100% to ~58% (the toy works), but
partition ≈ single: for a two-component product, how the work is divided doesn't
change the pass rate. The matched failure distributions say the errors are intrinsic
to Haiku building this part from the spec — single isn't context-overloaded holding
both parts, and partition's slice-only view doesn't diverge because the shared
derivation is stated explicitly in each slice.

### What this does and doesn't establish

- ✅ Gates discriminate; agents build a single-value contract reliably and a
  two-point derived contract ~58% of the time.
- ✅ Harder toys break the 100% ceiling, giving a measurable pass-rate.
- ❌ Does **not** show partition beating single — they tie wherever measured. The
  regime where partition *should* win (a contract too large for one agent's context)
  needs a **many-component** product, not a two-part toy.
- Pass-rate ignores partition's real edge — **parallel wall-clock** (its builders run
  concurrently; single's are sequential). The harness records per-agent time; the
  headline metric doesn't credit it.

---

## Proposed harder toys (designed, not yet run)

To probe where partition and single actually diverge, these stress the two axes a
two-part toy can't: **context load** (many components → one agent holds the whole
contract) and **interface chains** (an error in one part propagates). All are
gate-judgeable with existing primitives.

1. **N-slot rail (context-load sweep).** One rail with *k* evenly-spaced slots, *k*
   matching pegs (k = 2, 4, 8). As *k* grows, single must track all *k* positions in
   one context while each partition agent still sees only its one slot. *Hypothesis:*
   single's pass-rate decays with *k*, partition's holds — the first place the two
   should separate. Gate: interference, every peg in its slot.
2. **Tolerance-stack chain (error propagation).** A linear chain A–B–C–D, each part's
   right face mating the next's left face, total length fixed by contract. Small
   per-part errors accumulate. *Hypothesis:* partition (each agent blind to neighbors)
   drifts out of total-length tolerance faster than single (which sees the running
   sum). Gate: assembled length within tolerance + interference. **The case partition
   should LOSE** — important to have, because it makes any partition *win* elsewhere
   credible.
3. **Hub-and-spokes (fan-out breadth).** A central hub with *n* ports; *n* arms each
   mate to one port on a shared bolt pattern. *Hypothesis:* partition parallelizes the
   arms cleanly; single's pass-rate drops as *n* grows. Tests breadth, not depth.
4. **Two-interface bracket (per-part complexity).** One bracket mating to *two*
   parents at once (a wall and a floor) — both interfaces must satisfy their contracts
   simultaneously. A harder, tighter-tolerance variant of toy #5's structure. Gate:
   `interface_align_check` on both + interference.

**Sequencing.** #1 first — cleanest test of the core hypothesis (context load
separates the conditions) and cheapest to sweep. #2 most valuable (the partition-loses
case). Both are real spend and bigger per-trial (more components = more agent turns);
a proper sweep is tens of dollars, not the ~$2 a two-part toy costs.

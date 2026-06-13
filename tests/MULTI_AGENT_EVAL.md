# Multi-agent partition+merge — evaluation plan

How we decide whether a *team* of agents can split a design, build the pieces
independently, and merge them into a product that actually fits. Companion to the
RFC [`docs/MULTI_AGENT.md`](../docs/MULTI_AGENT.md) and the single-agent
[`RELIABILITY.md`](RELIABILITY.md) harness.

---

## Status & results at a glance — Phase 3 (2026-06-12)

Phases 0–2 (the partition+merge substrate) shipped 2026-05-30. Phase 3 is in
progress; the sections below are the chronological detail, this table is the
summary. Every feature ships with a free, two-sided discrimination test; the two
with behavioral claims also have billed agent evidence. All Haiku 4.5, n=20/cond.

| RFC § | Feature | Status | Free test | Billed result |
|---|---|---|---|---|
| 11.1 | Resolve step (global constraints → literal slices) | ✅ shipped | `test_manifest_resolve.py` (9) | `tchainu_r` partition **2/20 → 20/20**; single 0→8 (must also hand each builder only its slice) |
| 11.2 | Typed interfaces (`bore_fit`, `gear_mesh`, `frame_orientation`) | ✅ shipped | `test_typed_interfaces.py` | gearbox now a manifest; closes exact-touch + orientation gate edges |
| 11.3 | `verify_contract` (builder-side self-check) | ✅ shipped | `test_verify_contract.py` (17) | **every condition → 20/20**; `tchainu_r` single **8/20 → 20/20** (+12) |
| 11.4 | Hierarchical manifests (nested merge/gate/lock) | ✅ shipped | `test_hierarchical_manifests.py` (13) | n/a (geometric oracle) |
| 11.5 | Standard parts (`library` components generated at merge) | ✅ shipped | `test_standard_parts.py` (18) | n/a (geometric oracle) |
| 11.6–11.8 | requirements gates · schema formalization · pipelined orchestration | ⬜ planned | — | — |

**Headline findings (2026-06-12 billed rounds, ~$30 total):**

- **Breadth alone doesn't separate partition from single** (k-sweep refuted, k ≤ 8,
  after fixing two harness artifacts) — but a shared *derivation* reliably breaks
  whoever holds it (tchain6 partition 20/20 vs single 4/20).
- **`tchainu` had no winner** (partition 2/20, single 0/20) → the strongest mandate
  for the resolve step (11.1), which then took partition to **20/20**.
- **`verify_contract` (11.3) lifted every measured condition to 20/20** — shifting a
  contract check left, into the builder, before the expensive merge.

**Open PR stack (all against `main`, stacked in order):** #60 design+eval → #62
resolve step → #63 typed interfaces → #64 verify_contract → #65 verify_contract
eval → #66 hierarchical manifests. Shipped status per item also tracked in the RFC
§12 roadmap.

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
model. Both green for every toy (selftest 18/18 across 9 toys) — so a passing agent genuinely built a fitting
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

## Harder toys (designed; nslot8 + tchain6 now run)

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

---

## Divergence results (Haiku 4.5, n=20/condition, ~$5.7)

First runs of the extreme configs — **nslot8** (9 components) and **tchain6** (6).
These are the first results where partition and single **diverge**, after twopin
tied. In both, partition ≥ single, and the mechanism is the same: single's burden of
holding the whole multi-part contract in one context is the failure source.

| toy | condition | pass | built | note |
|---|---|---|---|---|
| nslot8 | partition | 0/20 | **12/20** | 8-distinct-pair contract too hard to *pass*… |
| nslot8 | single | 0/20 | **4/20** | …but single completes far less often |
| tchain6 | partition | **20/20** | 20/20 | each agent builds one 100/6 segment → exact |
| tchain6 | single | **0/20** | 20/20 | every chain too long (102.7–113.3 mm) |

**nslot8 — context load, seen in completion not pass.** Eight distinct slot↔peg
pairings is past Haiku's cliff: neither condition produces a *passing* assembly. But
`built` diverges as the context-load hypothesis predicts — partition (each peg agent
sees one diameter) completes 12/20; single (tracking all 8 pairs in one conversation)
only 4/20, mostly failing to even save all 9 parts. The pass-rate crossover the
hypothesis wants is presumably at smaller k (nslot4/6, unrun); nslot8 saturates both.

**tchain6 — a clean, decisive divergence, and it reversed the prediction.** Partition
passed 20/20, single 0/20 — but the doc above predicted *partition* would lose here.
The prediction was wrong, instructively: with **equal** segments, each partition agent
independently builds the same correct 100/6 = 16.67 mm part, and identical rounding
*cancels* (6 × 16.67 = 100.0) — there is no accumulation, because they aren't summing
each other's errors. The drift came from **single**, which consistently built segments
too long (avg 106.7 mm) — it appears to round 100/6 up and never reconcile the total.
Seeing the whole chain made it worse, not better. Lesson: error accumulates when one
agent juggles a running total and fumbles it, *not* across independent identical parts.

**What this establishes (and doesn't).**
- ✅ First measured evidence **partition ≥ single** — partition completes more
  (nslot8) and passes where single can't (tchain6). After twopin's tie, this is the
  result the easy toys couldn't give.
- ✅ The cause in both is the context-load axis these toys target: single carrying the
  whole contract is the failure source.
- ❌ Still one model (Haiku), n=20, two configs. nslot8 is decided on `built`
  (weaker than pass). tchain6's win rests on a prediction being wrong — worth a
  rerun to confirm it isn't a prompt artifact before leaning on it hard.
- Open: the nslot **k-sweep** (k=4,6) to find the pass-rate crossover, and a tchain
  variant with *unequal* required segments (where independent rounding genuinely
  *would* accumulate — the original partition-loses case, which equal segments
  accidentally dodged).

---

## Free probe results — gate-value layer (2026-05-31, no API)

Before spending the key on the open follow-ups, ran cheap **MCP-driven probes**
(local FreeCAD worker, $0, Opus-as-builder under the Max plan) to decide *which*
billed experiments are worth running and whether any contract / worker-loop change
should precede them. Each probe gates a decision; the takeaway is **no contract or
worker-loop refactor is warranted — the probes cleared two experiments and killed
one candidate workstream.**

| Probe | What it checked | Result |
|---|---|---|
| **A — gate boundary** | interference gate at Ø15.6 / 16.0 / 16.4 in a Ø16 hole | Boundary is exactly where claimed. Clearance +0.4 **and exact-touch (0.0) report no interference** (zero-volume contact isn't flagged — the "ambiguous touch" risk doesn't materialise); −0.2 caught at 101.8 mm³ (= hand-calc π(8.2²−8²)·10). Gates trustworthy. |
| **C — unequal-tchain separability** | can an unequal/grid chain make partition lose? | **Separates.** The lever is a **coarse manufacturing grid**: with no grid each agent builds an exact float and the chain sums to T (why tchain6 partition passed 20/20); force whole-mm stock and local rounding can't reconcile a *global* total. Nominals chosen so all six round up → partition 102 mm (drift 2.0) vs single 100 mm (drift 0), tol 0.8 → **2.5× margin**. MCP-verified FreeCAD reproduces mandated lengths exactly (bbox X = 12.6 / 16.6667 to the digit), so `_part_x_length` measures the real choice. |
| **D — nslot k=6 oracle** | do both gate checks fire at k=6 | **Sound.** Cylinder bbox X = diameter exactly (r=4.8→9.6, r=6.8→13.6); slot diameters [10,14,18,22,12,16] → targets differ ≥2 mm ≫ 0.6 threshold, so wrong-slot (Δ=4.0) always caught; oversized caught by interference (Probe A). k-sweep oracle is billable. |
| **B — contract clarity** | is wording the confound? | nslot task strings already battle-tested at k=4/8. The one real risk is the **new unequal-tchain single-task**: it must make reconciliation salient ("segments need not be equal — choose them so the total is exactly 100 on the mm grid") or single repeats its equal-case failure (rounds each up, never reconciles) and the conditions tie at *both-lose* instead of single-wins. Wording done accordingly. |
| **E — self-verify preview** | would verify-before-save help? | **No (for this axis).** tchain error is reconciliation / structural blindness, not a measurable per-part defect: a partition agent building one grid-snapped segment *can't* see the total is wrong, so verify-before-save can't help it; it would only help *single*, **narrowing** the very divergence we want to surface. Skip the generic loop tweak — it's a contract-wording issue, not a worker-loop one. |

**Toy added:** `tchainu` (toy 7) — UNEQUAL tolerance chain on a whole-mm grid, the
**real partition-LOSES case** the equal-segment toy dodged. Reuses the `_tchain_gate`
length-sum oracle. Selftest two-sided valid (reconciled build → pass; un-reconciled
round-up 102 mm → caught). The "partition loses" result hinges on single reconciling,
so it's also a clean test of whether single uses its whole-chain view at all.

**Billed experiments the probes unlock** (Haiku n=20, decide ceiling when scheduling):
1. **nslot k-sweep (k=4,6)** — find the pass-rate crossover nslot8 missed (~$6–10).
2. **`tchainu`** — the partition-loses credibility case (~$5).
3. *(cheap, optional)* **tchain6 rerun** — confirm the 20/0 win isn't a prompt artifact (~$3).

Total ≈ **$12–15**, in line with the prior ~$5.7 round but covering more ground.

---

## Toys added 2026-05-31 — exact constraint + GD&T (free; selftest 28/28, 14 toys)

Five new toys plus a builder-surface change, all gate-validated for free:

**Toy 8 — `pinslot` (pin-and-slot exact constraint).** The textbook way to lock the
three in-plane DOF: a **round hole** (locks x,y) + an **oriented slot** (locks rotation
θ, frees the spacing axis). Distinct from `twopin`, which uses *two round holes* — an
*over-constrained* scheme that jams on any spacing error. Functional two-assembly gate:
both parts seat at nominal **and** a reference carrier with pin 2 shifted along the slot
axis must *still* seat. The over-constrained design (round hole at P2) passes nominal
but is caught by the perturbed assembly (106 mm³ jam) — exactly the failure the slot
exists to prevent.

**Toys 9–11 — GD&T LOCATION family.** The first gates that **measure a feature against
a tolerance zone** (via `run_script` reading the as-built axis) rather than testing fit
by interference: `gdt_position` (hole true position within a Ø0.4 zone off datums A/B),
`gdt_concentric` (bore coaxial with its boss), `gdt_symmetry` (hole pair about the
median plane). Two *independent* parts per toy with different nominals (context load for
single). Each negative displaces a feature 0.5 mm and is caught at the 0.2 mm zone edge.

**Toy 12 — `gdt_angularity` + the `rotate` builder tool.** GD&T **coverage is bounded by
the builder surface**: form (flatness/straightness/circularity/cylindricity), profile,
and runout have **no agent-controllable deviation** — perfect primitives can't be made
imperfect — so they're not testable. Orientation needed a real tilt, so the agent tool
surface gained a **`rotate`** tool (rotate a solid about an axis through its centroid;
kept test-local via `run_script`, worker untouched). `gdt_angularity` makes a slender
post stand at a called-out angle from Z within ±1°; the gate reads the as-built long
axis (least-inertia principal axis). **Caveat for comparability:** adding `rotate`
changes the tool surface every toy sees, so post-change pass-rates are not a clean
baseline against the earlier Haiku numbers — re-baseline if mixing.

GD&T coverage map: **Location ✅** (position, concentricity, symmetry — clean fits);
**Orientation ⚠️** (angularity/perpendicularity/parallelism — reachable now via `rotate`);
**Form / Profile / Runout ❌** (no deviation possible with exact primitives).

---

## Kinematic mechanisms added 2026-05-31 (free; selftest 48/48, 20 toys)

Seven mechanism toys — gear trains, linkages, and moving assemblies — a different
class from the static-fit toys. Two new gate capabilities back them:

- **Gear geometry** via a new first-class DriftPin primitive **`add_gear`** (FreeCAD's
  core involute generator, extruded to a solid; external + internal/ring). Committed
  separately (`feat(worker): add_gear`). The agent builder surface gains `add_gear`
  too (gears can't be built from box/cylinder). Pitch radius is read back from the
  as-built tip radius (`rp = tip − module`; internal ring from inner-tip + module).
- **Swept-motion interference** (`_sweep_clear`): the gate poses every part at each
  motion step via forward kinematics it encodes, then interference-checks. Proven on
  the slider-crank (a too-wide piston jams the bore at 3180 mm³ mid-stroke).

| Toy | Class | Gate checks |
|---|---|---|
| `kin_gearbox6` / `kin_gearbox3` | gear geometry | every input+output pair meshes at one shared centre distance C; each ratio hits target (shared-constraint partition gem; 6-speed = 12 gears) |
| `kin_planetary` | gear geometry | Nring = Nsun + 2·Nplanet (measured pitch radii); carrier's n planets equally spaced at the sun-planet centre distance; equal-spacing assembly condition |
| `kin_ackermann` | linkage (static) | each steering arm's kingpin→tie-rod line aims at the rear-axle midpoint; catches parallel-arm steering |
| `kin_slidercrank` | moving | closure L>R (no bind), stroke = 2R, rod swing < 20°, **posed-interference sweep** of the piston through its stroke in the bore |
| `kin_geneva` | moving | drive-pin radius = C·sin(π/n) (tangency, no jam); n slots equally spaced → 1/n index |
| `kin_sarrus` | moving | the two leaves' hinge axes are perpendicular (X⊥Y) → 1-DOF straight-line motion; catches parallel hinges |
| `kin_wishbone` | moving | SLA geometry (upper arm shorter than lower → camber gain); upright length closes the four-bar loop |

**Honest finding on motion gates.** For these mechanisms the *robust discriminator*
is **measured geometric relations + analytic forward-kinematics over the motion
range**, not posed-solid interference. Posed interference (`_sweep_clear`) is the
right test only for **collision-type** failures (slider-crank piston-in-bore) — for
ratio/closure/tangency/aiming failures it either doesn't discriminate (an idealised
pose keeps parts on their kinematic path regardless of size) or risks false failures
from posing math. So the suite uses analytic kinematics for correctness and the posed
sweep where a collision is the natural failure.

**Abstractions (kept honest, like pitch-cylinders earlier).** Gears use *real*
involute teeth (`add_gear`); Geneva slots are abstracted as n equally-spaced
engagement holes (captures the index ratio + tangency, not slot-sliding); Sarrus is
reduced to its defining perpendicular-hinge-axes property; wishbone to SLA + four-bar
loop closure (camber gain implied by upper<lower).

**Comparability caveat (again):** `add_gear` and `rotate` enlarge the agent tool
surface, so kinematic-era pass-rates are not a clean baseline against the original
static-toy Haiku numbers — re-baseline before mixing.

---

## Physics, mass, fastening & multi-physics added 2026-05-31 (families A/B/C/D/F)

Nine more toys taking the suite from geometric/kinematic oracles into **physics**.
Two new gate capabilities, both on the existing FreeCAD FEM stack (CalculiX), plus a
mass oracle. Selftest two-sided valid for every toy. **FEM toys are slower per trial
(mesh+solve) but free.**

New gate capabilities (`tests/test_multiagent_m2.py`):
- **`_fem_stress`** — structural FEM: fix the support face, pressure on the load face
  (normal-aligned, no edge-picking), mesh, CalculiX, returns max von Mises + tip
  displacement.
- **`_fem_thermal`** — steady-state thermal FEM: heat flux into one face, convection
  on the rest, returns max temperature.
- **`_part_volume` / `_part_bbox`** — mass (volume·density) and extents.
- FEM gates are **RELATIVE**: the agent's part is compared to a scripted reference
  built and solved under the SAME setup (cached in `_FEM_REF_CACHE`). CalculiX-through-
  worker magnitudes are monotonic/discriminating but not certified absolute stress, so
  any consistent offset cancels; plus an absolute mass budget the agent is given.

| Toy | Family | Oracle | Window / failure modes caught |
|---|---|---|---|
| `fem_bracket` | A physics | structural FEM | too thin → von Mises over limit; too thick → over mass budget |
| `fem_beam_stiffness` | A physics | structural FEM | too thin → tip deflection over limit (~1/t³); too thick → over mass |
| `cg_target` | B mass | assembly centre of mass | counterweight height must balance the lever about the pivot (coupling) |
| `press_fit` | C fastening | measured Ø | interference in a holding band — too loose slips, too tight cracks |
| `thread_engagement` | C fastening | measured Ø + depth | tap-drill must match thread minor Ø (not clearance); engagement ≥ 0.8·D |
| `rack_pinion` | D kinematics | gear + spacing | rack tooth pitch = pinion circular pitch (π·module) |
| `fourbar_crankrocker` | D kinematics | link lengths | Grashof condition + the crank is the shortest link |
| `cam_follower` | D kinematics | eccentric lift | cam lift (2·offset) = follower travel |
| `thermo_structural` | F capstone | **thermal + structural FEM + mass** | too thin → runs hot AND over-stresses; too thick → over mass |

**Capstone note.** `thermo_structural` runs *both* solvers on the agent's geometry —
a real coupled multi-physics gate (proven: a too-thin sink caught at 357 > 289 °C; a
too-thick one at 341 > 213 g). Here both physics favour more material and the mass
budget is the opposing constraint; a genuine thermal-vs-structural *shape* tradeoff
(thin tall fins cool but flex, stubby is strong but hot) is the natural next extension.

**Suite status:** 30 toys, selftest two-sided green throughout, dryrun OK. Dedicated
simulation tooling (promoting these gate helpers to typed tools) follows
`docs/SIMULATION_TOOLS.md`.

---

## k-sweep, tchainu & tchain6 rerun — 2026-06-12 (Haiku, n=20/cond, ~$20.8 total)

Ran the three billed experiments the probes unlocked — and found **three
harness-validity bugs by code review mid-run**, two of which had manufactured
the entire apparent nslot divergence and forced fix + rerun rounds (the third
lost a killed run's trials and motivated per-trial checkpointing).

### Three harness findings (the actual headline)

1. **The nslot single task was underspecified — single's 0/20 was an artifact.**
   `_nslot_single_task` gave the single agent the peg diameters but *not* the plate
   spec: no plate dimensions, no hole positions, no hole diameters — while
   partition's plate agent got all of it. Single dutifully built plates with holes
   where the gate doesn't look; round-1 failures were "peg fully embedded in solid
   plate" signatures (2433 mm³ = π·8.8²·10 to the digit), guaranteed regardless of
   agent skill. Round 1 measured **0/20 at both k=4 and k=6**; with the contract
   equalized, single recovered to **16/20 / 15/20**. Two corollaries: the eval's
   own fairness rule ("run every toy both ways") needs a stronger form — *both
   conditions must receive the same total contract* — and **the earlier nslot8
   single numbers (built 4/20, §divergence) carry the same taint; re-baseline
   before citing them.** Also: registered `nslot6` (Probe D's validated oracle,
   selftest green) and added `M2_COND` for single-condition reruns.

2. **"Single" is k+1 cold calls, not one growing conversation.** `run_single`
   re-prompts a *fresh* agent per component (full contract + "now build X");
   nothing carries between calls. So the single condition measures
   **full-contract-in-prompt** (a retrieval/consistency load), not a conversation
   that could hold a running total. For the chain toys this changes the story:
   single *cannot* remember its own reconciliation between segments — each cold
   call re-derives the global plan, and disagreement between calls *is* the
   observed drift (tchainu single failed in both directions, 96–108 mm). RFC §10's
   "single fumbles the running total" mechanism is adjusted accordingly.

3. **`MAX_TURNS = 12` censored k=8 — for *both* conditions.** The fixed-prompt
   nslot8 rerun still collapsed (partition 2/20 pass, 10/20 built; single 0/20,
   1/20 built) — but the per-agent data showed every unsaved plate agent dying at
   *exactly* turns=12. An 8-hole plate is ~19 sequential tool calls (box + 8
   cylinders + 8 cuts + save); at a 12-turn cap only agents that happen to batch
   tool calls per turn can finish, so k=8 was measuring batching luck, not context
   load. The budget is now an env knob (`M2_MAX_TURNS`, default 12 unchanged for
   comparability); at 30 the collapse vanishes entirely (table below) and no agent
   runs within 5 turns of the cap (one exception). **May's nslot8 numbers carried
   both this artifact and #1 — superseded by the budget-30 run.**

Tooling that fell out of the kill-and-relaunch churn: **per-trial checkpointing**
(`checkpoint_m2.jsonl`, fsynced line per finished trial; crashed billed runs
resume free; keys embed a prompt-contract hash so edited contracts can never
resume stale results; auto-deleted when a run completes; `M2_FRESH=1` discards) —
verified free by `scratch/test_ckpt_m2.py`. Plus `M2_COND` (rerun one condition)
and per-component agent stats in `run_single`.

### Results (gates unchanged; selftest green incl. nslot6)

| toy | partition | single (fair prompt) | reading |
|---|---|---|---|
| nslot4 | 18/20 | 16/20 | tie at k=4 (p≈0.66) |
| nslot6 | 19/20 | 15/20 | trend, not significant (p≈0.08) |
| nslot8 @turns=12 | 2/20 (built 10/20) | 0/20 (built 1/20) | budget-censored — discard |
| nslot8 @turns=30 | 16/20 | 14/20 | tie at k=8 |
| tchain6 rerun | **20/20** | 4/20 | original 20/0 **replicates** — not a prompt artifact |
| tchainu | **2/20** | **0/20** | **both lose** — see below |

- **k-sweep verdict: no breadth divergence through k=8.** With a fair prompt AND
  a fair turn budget, single tracks partition at every width — diffs of 2/4/2
  points (pooled 53/60 vs 45/60, p≈0.06: at most a small *flat* partition edge,
  with no k-dependence). The hypothesis "single's pass-rate decays with k while
  partition's holds" is **refuted for Haiku at k≤8**: picking k distinct values
  out of a full-contract prompt doesn't crack by eight pairs. The dramatic
  separations earlier rounds showed were the two artifacts stacked. At budget 30
  the residual failures on *both* sides are ordinary wrong-slot geometry slips,
  and partition's plate agent — the one component whose contract grows with k —
  is where its few failures concentrate. Breadth alone is not where partition
  wins; coupled constraints are.
- **tchain6 replicates.** Partition 20/20 vs single 4/20 — the original 20/0 was
  not a wording fluke. Single's drifts cluster at unreconciled per-segment guesses
  (102–113 mm), as the cold-call mechanism predicts.
- **tchainu — designed as "partition loses"; actually NOBODY wins.** Partition
  2/20: 13 of 18 failures are the *exact* predicted unreconciled-round-up
  signature (102.0 mm); the two passes were lucky disobedient roundings that
  happened to sum to 100. Single 0/20: failures in *both* directions (96–108 mm)
  plus two not-saved — each cold call reconciled differently. Probe C's prediction
  that single reconciles (probed with Opus as builder) did not transfer to Haiku.
  **This is the strongest empirical case yet for RFC §11.1:** no agent condition
  reliably reconciles a global constraint against a grid — resolve it
  coordinator-side in plain code and hand every builder a literal value.

### Follow-ups

1. **A true single-conversation condition** — one agent, one growing thread,
   k `save_component` calls — to separate *conversation-held* context load from
   *prompt-held* contract size. Today's "single" only measures the latter; it is
   also the condition that could genuinely hold a running total, so it's the right
   baseline for the chain toys.
2. The small flat partition edge (~13 points pooled across the sweep, p≈0.06)
   needs larger n to call — or larger k / a busier contract to find where
   prompt-held breadth finally cracks. (The original nslot8-rerun follow-up is
   done: see the budget-30 row.)

Cost accounting for the day: round 1 $9.65 + nslot single fix-rerun $3.15 +
nslot8 fixed-prompt $3.98 + nslot8 budget-30 $4.06 ≈ **$20.8**.

---

## The resolve step closes the loop — `tchainu_r` (2026-06-12, Haiku, n=20, $1.74)

`tchainu` measured the failure (partition 2/20, single 0/20: no agent reconciles
a global total against a grid). The RFC §11.1 fix — `resolve_constraints`
(`driftpin/manifest.py`) evaluates the constraint in plain code and hands each
builder a literal length — shipped, and `tchainu_r` is the same contract with the
resolve step in front of it. The toy's resolved lengths are computed *by the
shipped resolver* at import, so the eval gates exactly what the contract
machinery emits.

| toy | partition | single | |
|---|---|---|---|
| tchainu (reconcile yourself) | 2/20 | 0/20 | the measured failure |
| **tchainu_r (resolved slices)** | **20/20** | **8/20** | the fix |

- **Partition 2/20 → 20/20: a clean, decisive fix.** Each builder is handed its
  *one* literal length ("build it at exactly 18 mm") and nails it every time. This
  is the §11.1 result the whole eval arc was building toward — the first Phase 3
  feature, validated against the toy that motivated it.
- **Single 0/20 → 8/20, and the shortfall is the sharper lesson.** The single
  condition is handed the full list of six resolved lengths and must self-select
  its own slice per cold call; it still drifts long (102–107 mm), because picking
  one value out of six is itself error-prone. So **resolving the values is
  necessary but not sufficient — each builder must also receive *only its own*
  resolved slice.** Resolve + partition-slice = 20/20; resolve dumped into one
  shared prompt = 8/20. Same "bound the contract each agent sees" principle the
  partition design rests on, now measured on resolved values. The gate's
  `ignores_resolved` negative (a builder that re-rounds its raw nominal instead of
  using its resolved value, chain 101 mm) is caught, so the 20/20 is real
  obedience, not a slack gate.

The resolver itself is covered free by `tests/test_manifest_resolve.py` (9 unit
tests: the exact tchainu reconciliation, deficit direction, no-grid residual
spread, determinism/purity, and loud failures on infeasible/unknown/bad-ref
contracts) — in `run_all.sh`, no key.

---

## verify_contract shifts failure left — measured lift (2026-06-12, Haiku, n=20, $6.8)

`verify_contract` (RFC §11.3) lets a builder self-check its part against its slice
*before* saving, turning the expensive loop (build → merge → gate-fail → rebuild)
into a cheap local one (build → self-check → fix). `M2_VERIFY=1` hands each builder
its slice as a `verify_contract` contract and tells it to repair any failing check
before `save_component`; the agents got `verify_contract` added to their tool
surface. Run on the two toys whose failures are *local, measurable* per-part
defects (so a self-check can catch them): `tchainu_r` (each segment's resolved
length, an `extent` check) and `nslot6` (each plate hole a `bore` check, each peg
an `extent` check).

| toy | condition | baseline | + verify_contract | Δ |
|---|---|---|---|---|
| tchainu_r | partition | 20/20 | 20/20 | 0 (ceiling) |
| tchainu_r | **single** | **8/20** | **20/20** | **+12** |
| nslot6 | partition | 19/20 | 20/20 | +1 |
| nslot6 | single | 15/20 | 20/20 | +5 |

**Every condition reached 20/20**, and the lift lands exactly where the §11.3
prediction puts it: where there is headroom *and* a locally-measurable defect.
The single conditions — which make selection/copy errors picking their slice out
of a full-contract prompt — lift most (tchainu_r single +12, nslot6 single +5);
near-ceiling partition barely moves (+0 / +1). The self-check gives a builder an
oracle for its own slice, so the single builder that was building a 19 mm segment
when its contract said 18 mm now catches it and fixes it.

**The agents genuinely self-checked** (not luck): mean builder turns rose with
the added `verify_contract` call + repair — tchainu_r single 2.9 → 4.0, partition
3.5 → 4.3; nslot6 builders ran to 5+ turns (max 15–16) on the multi-hole plate's
verify→fix loop. Pre-flight (free) confirmed every `_vc_contract` oracle passes
its toy's *reference* build, so a self-checking builder converges rather than
loops against a wrong oracle.

**Honest scope.** This is single-shot *pass-rate* lift from a within-builder
self-check-and-repair loop — the per-builder half of §11.3. It is not literally
coordinator "rounds-to-converge" (the M2 toys don't run the fan-in re-dispatch
loop), but the mechanism is the same one that would cut those rounds: catch and
fix a contract violation locally instead of after the expensive merge. It also
confirms Probe E's earlier prediction (verify-before-save helps the condition
with headroom and *narrows* the partition-vs-single gap) — here that narrowing is
the success signal, with tchainu_r single closing the entire 8→20 gap to
partition. Probe E's caution (don't add it when trying to *surface* a divergence)
and this result (do add it to *converge* faster) are the same finding read two
ways.

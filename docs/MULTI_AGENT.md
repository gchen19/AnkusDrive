# DriftPin multi-agent collaboration

How a *team* of agents — Claude subagents, another AI coding tool's agents, or
humans at terminals — can design one mechanical product together: splitting it
into components and subassemblies, building those in parallel, and constructing
the whole back up with the pieces actually fitting.

Companion to [`ROADMAP.md`](ROADMAP.md). The roadmap is about deepening what *one*
agent can do; this doc is about *many* agents sharing the work.

**Status (2026-06): Phases 0–2 are shipped and eval-validated.** The original RFC
("Phase 0 is runnable today, everything past it is a plan") is history — the
partition+merge substrate (`publish_interface`, `merge_assembly`, mate-by-frame,
recursive BOM, the envelope / interference / alignment gates, the lockfile) landed
2026-05-30, and a 30-toy eval suite has since produced the first empirical results
on when partitioning helps (§10). The forward-looking part of this doc is now
**Phase 3 (§11): the contract and gate extensions needed for assemblies of
*advanced* components** — gear trains, fits, kinematic interfaces, physics
requirements — driven directly by what the evals found.

**Related docs:**

- [`README.md`](../README.md) — architecture (the long-lived worker, stdio JSON).
- [`docs/ROADMAP.md`](ROADMAP.md) — Slice 5 Status records the Phase 1/2 landings.
- [`tests/RELIABILITY.md`](../tests/RELIABILITY.md) — the "can the agent see what it
  built?" loop. The merged assembly has to pass it too.
- [`tests/MULTI_AGENT_EVAL.md`](../tests/MULTI_AGENT_EVAL.md) — the eval: hard-oracle
  merge gates, the toy ladder with negative controls, and live agent results
  (partition vs single-agent baselines). §10 summarizes its design-relevant findings.

---

## 1. Problem & non-goals

There are two things people mean by "agents working on a part together":

- **(a) Shared co-editing** — several agents mutating *one* document at the same
  time, like a Google-Docs cursor pile-up on a single FreeCAD model.
- **(b) Partition + merge** — split the product into components/subassemblies, each
  owned and built independently, then assembled into the whole.

**This design targets (b), and treats (a) as a non-goal** (revisited, narrowed, as
the final deferred phase). The reason is structural, not incidental:

DriftPin's worker is a single long-lived `freecadcmd` process with a single
synchronous dispatch loop (`worker.py:_main`): read one newline-JSON request, run
one `HANDLERS[method]`, write one response. *All* mutable state is two process
globals — `_handles` (handle-string → live FreeCAD object, in-memory, dies with
the process) and FreeCAD's `App.ActiveDocument`. Handles are not addressable
across processes and do not survive a restart.

To make two agents safely co-edit *one* `App.ActiveDocument` you'd need per-agent
handle namespaces, object-level locking, ownership arbitration, and geometric
conflict resolution (two agents filleting overlapping edges). That is a large,
invasive build that fights the design.

> **Guiding principle:** don't make the worker concurrent — make the *artifacts*
> composable, and let agents parallelize by *partitioning*. Concurrency comes for
> free when each agent owns a separate file in a separate process.

The goal, then, is a **fan-out / fan-in design tree**: a top assembly composed of
subassemblies composed of components — each node a single `.FCStd` owned by exactly
one agent.

```text
                     ┌─────────────────────┐
                     │  root assembly .FCStd │   (coordinator)
                     └──────────┬───────────┘
                ┌───────────────┼───────────────┐
        ┌───────▼──────┐ ┌──────▼───────┐ ┌──────▼───────┐
        │ subasm A.FCStd│ │ component B  │ │ component C  │
        └───────┬──────┘ └──────────────┘ └──────────────┘
          ┌─────┴─────┐
   ┌──────▼───┐ ┌─────▼────┐
   │ comp A1  │ │ comp A2  │        each box = one file, one owner, one worker
   └──────────┘ └──────────┘        leaves build in parallel; parents merge up
```

---

## 2. Collaboration model — the part-DAG

Decompose the product into a DAG of nodes. One file, one owner, per node.

- **Leaf components** are built independently and in parallel — there is no shared
  state between them, so there is nothing to contend on.
- **Subassemblies** merge their children: link each child file, place it, verify.
- **The root** merges the subassemblies the same way.

This maps 1:1 onto shipped machinery:

- `make_assembly` creates an `App::Part` container.
- `add_part` (`worker.py:4602`) links **an in-doc handle *or* an external `.FCStd`
  by path** (`App.openDocument(path, True)` → `App::Link`) with a placement *or a
  mate-by-frame* (§4).
- `publish_interface` (`worker.py:4120`) records named mating frames on a component.
- `merge_assembly` (`worker.py:4843`) turns a manifest into an assembled, gated
  document in one call (§5).
- `interference_check`, recursive `bom_extract`, `envelope_check`,
  `interface_align_check` analyze the assembled result with world transforms (§6).
- `assembly_lock` / `assembly_lock_check` (`worker.py:4533/4554`) detect drift and
  staleness across the team without reading geometry (§9).

All of it **thin, tool-agnostic primitives**. No orchestration lives in DriftPin.

---

## 3. The interface contract — `manifest.json` (the hard part)

Cutting a *part* into pieces is easy. The hard part of cutting up a *design* is the
**interfaces**: where pieces bolt together, the shared bolt circle, the mating face
location, the envelope each piece may not exceed, the fits and tolerances. If agent
A builds a bracket and agent B builds the housing it bolts to, they will not fit
unless they agreed, up front, on the bolt pattern and the mating frame.

The agents never talk directly. Their only shared truth is a **design manifest** —
an Interface Control Document (ICD) — a plain JSON file on the shared filesystem.
It is the single thing every host (Claude, Cursor, a human) hands to its agents.

The shape `merge_assembly` consumes today:

```jsonc
{
  "name": "gearbox",
  "root": "gearbox.FCStd",            // output path, relative to the manifest
  "shared_parameters": {              // global facts every component must honor
    "bolt": "M4",                     // (read by builders, not by merge)
    "wall_mm": 3.0
  },
  "components": {
    "housing": {
      "owner": "agent-1",
      "file": "components/housing.FCStd",
      "object": "Body",               // optional explicit link target
      "envelope": { "min": [0,0,0], "max": [80,80,40] }   // keep-out box (mm)
    },
    "lid": {
      "owner": "agent-2",
      "file": "components/lid.FCStd",
      "envelope": { "min": [0,0,0], "max": [80,80,8] }
    }
  },
  "instances": [                      // what gets linked into the root, in order
    { "component": "housing", "placement": [0,0,0] },     // anchor: raw placement
    { "component": "lid",                                  // everyone else: mated
      "mate": { "child_iface": "mount",
                "parent": "housing", "parent_iface": "lid_mating_face",
                "verify_align": { "child_iface": "pin", "parent_iface": "pin" } } }
  ],
  "mates": []                         // alternative: top-level mate list
}
```

Interface *frames* are not in the manifest — they are published onto the component
files themselves by their owners (`publish_interface`, §4), which keeps the frame
definition next to the geometry that realizes it. The manifest names which frames
mate; the component files carry where those frames are.

Key properties: it is **declarative** (no geometry, just contracts), **tool-agnostic**
(any host reads/writes it), and **the only coupling** between component owners. An
owner reads only its own slice plus `shared_parameters` and `mates` that touch it.

**Gap (Phase 3):** the schema is implemented by code, not by a spec — there is no
`driftpin.manifest/1` version string or validation, and Phase 0 artifacts still
carry `driftpin.manifest/0-phase0`. Formalizing the schema (and stamping a manifest
version the lockfile can reference) is part of §11.

---

## 4. Stable cross-file references — mate by named frame *(shipped)*

`add_part` can place a child by a raw `[x,y,z]` (or position/axis/angle), but raw
placements are brittle across a team: a magic number in one agent's head, re-derived
by hand in another's. DriftPin already lets agents select geometry by stable,
content-addressed tags rather than positional `Face2` indices (`list_faces` /
`query_faces` / `resolve_face`, the `f_<hash>`/`e_<hash>` signatures). The same
philosophy, extended from faces/edges to **published interface frames**:

- **`publish_interface(handle, name, frame)`** — record a named datum frame
  (`{origin, z_axis?, x_axis?}`) on a component, persisted as a JSON property bag
  (`DP_Interfaces`) inside the `.FCStd` itself. This is the component owner's act of
  saying "*here* is where you bolt to me, and this is its orientation." Because the
  frames travel with the file, they survive process restarts and are readable by any
  worker that opens the file.
- **`add_part(..., mate={child_iface, parent, parent_iface})`** — place the child by
  aligning its published frame to the parent's:
  `LinkPlacement = Pp · Fp · Fc⁻¹` (`_apply_mate`, `worker.py:4398`). The placement
  is *derived* from the contract, so it can't drift out of sync with it.

Of the two implementation paths the original RFC weighed, **simple frame alignment
won**: deterministic, no solver, easy to reason about. The native FreeCAD 1.0
Assembly workbench (JCS joints, DOF, motion) remains a future option if true joint
semantics are ever needed — but note the kinematic eval toys (§10) got gear meshes,
slider-cranks, and four-bars *gate-checked* with analytic kinematics and posed
interference sweeps, without a joint solver in the build path.

---

## 5. Merge / construct-up protocol — `merge_assembly` *(shipped)*

A single primitive, callable by any host, that turns a manifest into an assembled,
verified document. It contains **no orchestration** — it neither spawns nor waits on
agents; it just assembles whatever component files currently exist.

```text
merge_assembly(manifest_path) ->
  1. new_document(root); make_assembly
  2. for each instance: add_part(source={path: component file}) → App::Link
  3. anchors get raw placements; everything else is mated by frame (§4)
  4. recompute
  5. run gates: interference, recursive BOM, envelope, interface_align (§6)
  6. save root; return { assembly, root, placed[], gates{...}, ok: bool }
```

Three properties it guarantees:

- **Deterministic** — same manifest + same component files ⇒ same result, every time.
- **Idempotent** — re-running on an unchanged tree changes nothing.
- **Re-runnable / live** — `App::Link` reloads geometry from the source `.FCStd`, so
  when a component owner saves a new version, re-running `merge_assembly` picks it up.
  Merge is cheap and repeatable; that's what makes the verify loop in §8 viable.

`bom_extract` (`worker.py:4759`) is recursive: it flattens through linked
`App::Part` subassemblies to leaf parts, keyed by `(file, object)` rather than bare
object name (so two components both named "Box" don't collapse into one row), with
optional mass rollup via density.

---

## 6. Verification gates — "is the whole actually consistent?"

Partition + merge only pays off if the *whole* can be checked without any single
agent holding the whole design in its head. After merge, run:

- **Interference** (`worker.py:4730`) — pairwise `Part.common().Volume`, flattened
  through subassemblies. Anything above threshold is a fit failure.
- **Envelope / keep-out** (`worker.py:4814`) — each component's world-space bounding
  box must stay inside the `envelope` it declared in the manifest. This is what stops
  one agent's "internal" growth from silently colliding with a neighbor it never sees.
- **Interface alignment** (`worker.py:4416`) — for multi-interface mates, verify the
  *secondary* interface pairs actually coincide in world space (the primary mate is
  satisfied by construction; the others are where misfits hide).
- **Recursive BOM + mass rollup** — counts and mass across the nested tree, so the
  coordinator can sanity-check part counts and total weight.
- **Visual** — render the merged assembly via `render_views` and run it through the
  Layer A/B/C reliability loop ([`RELIABILITY.md`](../tests/RELIABILITY.md)): the
  merged result must *look* like the intended product, not just pass numeric checks.

The gates return structured data, not prose — so any host's coordinator can branch on
them mechanically (re-dispatch the offending component, tighten an envelope, etc.).

**Known blind spot (measured, eval Probe A) — closed by the `bore_fit` typed gate
(§11.2):** the interference gate reports *zero* for exact-touch contact —
`Part.common()` of tangent solids has no volume. "Doesn't collide" is therefore
not "fits as specified": a contract that requires a clearance fit must be gated on
**minimum clearance**, not on non-interference alone. The `bore_fit` typed
interface now does exactly this in `merge_assembly` (gap 0 fails its clearance
band), so a clearance contract is gated systematically rather than relying on the
coordinator to remember to call `min_clearance` by hand.

---

## 7. Concurrency & isolation model

**One agent = one MCP server = one worker = one `.FCStd`.**

- True parallelism: N components build at once in N OS processes.
- Zero shared mutable state: each file has exactly one writer.
- No locks, no namespaces, no arbitration: there is nothing to contend on. The
  manifest is a read-mostly contract; component files are single-writer.

This is the whole payoff of choosing partition over co-editing. Contrast the cost of
the rejected shared path: a multi-client worker, per-agent handle namespaces, an
object-level lock manager, ownership arbitration, and geometric conflict resolution —
plus the single dispatch loop would still serialize every operation anyway, so the
"parallelism" would be illusory. Partition gives real parallelism *and* deletes all of
that machinery.

Two practical isolation rules for orchestration templates:

- **Each builder gets its own working directory** (its component file plus scratch
  space). The shared filesystem is the coordination bus, but only the manifest, the
  lockfile, and the finished component files live in the shared area — the same
  discipline as isolating concurrent reviewers in git worktrees.
- **The coordinator's worker treats component files as quiescent**: lock-state
  hashing opens and closes component files (`_interfaces_of_file`), so don't run it
  while a builder is mid-save on the same file. Builders report done *after*
  `save_document` returns; the coordinator locks after that.

---

## 8. Orchestration responsibilities (host-agnostic — roles, not tools)

DriftPin provides primitives; **the host provides orchestration.** Described as roles
so any host maps them to its own mechanism. The manifest + component files are the
*only* interface between roles — roles never share process state.

- **Coordinator** — decomposes the product into the part-DAG; drafts the manifest /
  ICD; **resolves global constraints into per-slice values (§11.1)**; hands each
  builder its slice; after builders report, calls `merge_assembly` and the gates; on
  a gate failure, renegotiates the contract (e.g. moves a frame, grows an envelope)
  and re-dispatches *only* the affected components (per the lockfile, §9).
- **Component-builder** — reads its manifest slice + `shared_parameters`; builds its
  `.FCStd` to contract; `publish_interface` for each mating frame it owns;
  self-verifies before reporting (`verify_contract`, §11.3 — envelope, contracted
  interfaces, features, and intent in one call, plus a `render_views` look); saves;
  reports its file + status.

The loop is **fan-out (builders) → fan-in (merge + gates) → re-dispatch on failure**.
Nothing in it is specific to a vendor. A reference coordinator implementing these
roles lives in [`orchestration/coordinator.py`](../orchestration/coordinator.py)
(decompose → fan out → merge → gate → renegotiate → re-dispatch, budgeted rounds).
Appendix A shows the Claude binding; Appendix B shows what a different host needs.

Two orchestration upgrades the current loop should grow (§11.8):

- **Dependency-aware pipelining.** The reference loop is barrier-shaped: all builders
  finish, then one merge. A part-DAG wants fan-in *per node*: a subassembly merges
  and gates as soon as *its* children land, and a leaf failure re-dispatches while
  unrelated branches keep building. Merge is cheap; run it early and often.
- **A round 0 contract review.** Today the only feedback path from builder to
  contract is the expensive one (build → gate fail → renegotiate). A cheap pre-build
  round — each builder reads its slice and returns *accept* or *amend* ("this
  envelope can't hold a gear of this module") — catches infeasible contracts before
  any geometry is spent on them.

---

## 9. Change propagation & provenance *(shipped)*

Re-running merge is cheap, but re-*building* components is not, so distinguish:

- **Internal change** — geometry moved but stays within the declared `envelope` and no
  published interface frame moved ⇒ **no neighbor impact**. Just re-run `merge_assembly`;
  links reload the new geometry.
- **Interface change** — a published frame moved ⇒ **every neighbor that mates
  against it must re-evaluate.** The coordinator renegotiates the manifest and
  re-dispatches those neighbors only.

Provenance lives in a **lockfile** (a manifest sidecar), written by `assembly_lock`
after a clean merge:

```jsonc
// gearbox.lock.json
{ "manifest": "gearbox.manifest.json",
  "components": {
    "housing": { "file": "components/housing.FCStd",
                 "file_hash": "…",          // blake2b of the file bytes
                 "interfaces_hash": "…",    // blake2b of its published frames
                 "depends_on": [] },
    "lid":     { "file": "components/lid.FCStd",
                 "file_hash": "…", "interfaces_hash": "…",
                 "depends_on": ["housing"] }   // lid mates to housing
} }
```

`assembly_lock_check` compares current files to the lock and classifies drift:

- `modified` — file changed since lock (internal change if interfaces held);
- `interface_changed` — published frames moved (the dangerous kind);
- `stale` — mates to an `interface_changed` component and was **not** itself
  rebuilt — the neighbor that needs re-dispatch (eval toy #6's failure mode);
- `new` / `removed` — manifest membership changed.

`ok` means: safe to re-merge without re-dispatching anyone. The file hash detects
*that* a component changed; the interfaces hash detects whether the change was
contract-relevant; `depends_on` propagates it to exactly the affected neighbors —
all without reading geometry.

---

## 10. What the evals taught (and what it changes)

The eval suite ([`MULTI_AGENT_EVAL.md`](../tests/MULTI_AGENT_EVAL.md)) ran the
shipped substrate with scripted builders (Layer M1, 30 toys, every gate validated
against negative controls) and live agents (Layer M2, partition vs a single-agent
baseline on every toy). Five findings change the design:

1. **Partition's measured wins are on *coupled-constraint* toys; breadth alone
   does not separate the conditions (k ≤ 8).** Easy two-part toys tie (twopin:
   57% vs 60%), and the completed nslot k-sweep (2026-06-12, after *two*
   harness-validity fixes — §10.5) shows a fairly-run single tracking partition
   at every width: 18v16 (k=4), 19v15 (k=6), 16v14 (k=8) out of 20 — at most a
   small flat partition edge (pooled p≈0.06), with **no decay in k**. The
   decisive divergences are the chains: tchain6 partition 20/20 vs single 4/20
   (replicated across two rounds). **Partition by context load** (§11,
   granularity rule) stands, but the load that demonstrably kills agents is a
   *shared derivation*, not how many distinct values sit in the contract.
   Pass-rate also undersells partition: its builders run concurrently —
   wall-clock is the unmeasured second win.

2. **The dominant failure mode is agents re-deriving shared math — and *no* agent
   condition escapes it.** twopin fails when two agents independently derive
   `x = center ± spacing/2` and one slips. tchainu (the unequal grid chain) made
   it conclusive: partition 2/20 — 13 of 18 failures the exact unreconciled
   round-up signature — and single 0/20, drifting in *both* directions. The
   contract should not hand agents *derivations* — it should hand them *results*
   (§11.1, the resolve step). That gate-validated failure pair is §11.1's direct
   empirical mandate.

3. **Holding the whole contract doesn't rescue a global constraint.** A caveat
   first: the eval's "single" condition is k+1 *cold* calls each carrying the full
   contract — not one growing conversation — so it measures prompt-held contract
   load, and *no* condition could actually hold a running total (a true
   single-conversation baseline is queued in the eval follow-ups). With that lens,
   tchain6/tchainu single's failures are independent cold calls re-deriving the
   global plan and disagreeing — drift comes from re-derivation, not accumulation
   across correct parts (equal-segment partition: identical rounding cancels,
   20/20). Either way the design conclusion is the same: resolved per-slice values
   beat whole-design context.

4. **The gates are trustworthy, with two sharp edges — both now closed by §11.2.**
   Every reference build passes, every negative control is caught, the
   interference boundary sits exactly where hand calculation puts it — but
   exact-touch reports no interference (§6), so clearance-fit contracts need a
   clearance gate, not a collision gate (now the `bore_fit` typed gate). A second
   edge found by code review: `interface_align_check` verifies only that frame
   *origins* coincide — a secondary interface positioned right but **rotated**
   passes (now the `frame_orientation` typed gate). Both shipped and
   gate-validated 2026-06-12.

5. **The baseline is part of the experiment.** *Two* artifacts manufactured the
   entire apparent nslot divergence before harness review found them. (i) The
   single task named the peg diameters but omitted the plate spec entirely
   (dimensions, hole positions, hole diameters), so single's plates put holes
   where the gate doesn't look — 0/20 by construction; with the contract
   equalized, single recovered to 75–80%. (ii) The 12-turn agent budget censored
   k=8 for *both* conditions: an 8-hole plate is ~19 sequential tool calls, and
   every unsaved plate agent died at exactly the cap; lifting it
   (`M2_MAX_TURNS=30`) took nslot8 from 2v0 pass (10v1 built) to 16v14 pass
   (20v20 built). The fairness rule the eval now encodes: **both conditions must
   receive the same total contract, and the harness budget must not bind before
   the model does.** (All pre-fix nslot8 numbers are superseded by the budget-30
   run.)

---

## 11. Phase 3 — contracts and gates for advanced assemblies

Phases 0–2 prove the substrate on boxes, pegs, and bolt circles. The eval toys
already exercise far richer components — gear trains, planetaries, slider-cranks,
press fits, FEM-gated brackets — but that knowledge lives in *test code*. Phase 3
moves it into the contract and the merge gates, so a team of agents can build a
gearbox, not just an enclosure. In priority order:

### 11.1 The resolve step — shared parameters become resolved slices *(shipped)*

Highest-leverage change, directly at the measured #1 failure mode (§10.2).
**Shipped 2026-06-12** (`driftpin/manifest.py:resolve_constraints`, wired into
`orchestration/coordinator.py`; v0 supports sum-to-target on an optional grid).
The manifest gains a `constraints` section for *global* relations — totals,
chains, center distances, ratios, grids:

```jsonc
"constraints": {
  "total_length_mm": { "sum": ["segA.len", "segB.len", "segC.len"], "equals": 100,
                       "grid_mm": 1.0 },
  "mesh1": { "center_distance": ["pinion.rp", "gear.rp"], "equals": 36.0 }
}
```

A deterministic **resolve step** (coordinator-side; plain code or a tiny solver, not
an LLM) evaluates the constraints and writes *literal resolved values* into each
component's slice before fan-out. Builders receive only numbers — `len: 33`,
`rp: 12.0` — never a formula to re-derive, never a sibling's value to guess. Agents
must never share a *derivation*, only a *result*. This also answers the old
"cross-file parametric coupling" open question: no live FreeCAD expression links
(fragile headless); the manifest stays the only coupling, and the resolve step is
where parameters propagate.

**Validated against the eval that motivated it (`tchainu`, the unequal grid
chain).** The toy was added because *no* agent condition could reconcile the
chain (partition 2/20, single 0/20, §10.2). With the resolve step handing each
builder its literal length, `tchainu_r` measured **partition 20/20** (was 2/20)
— a clean, decisive fix. The single condition rose only to **8/20** (was 0/20),
and the *reason* is the sharpest lesson: single is handed the full list of six
resolved lengths and must self-select its own slice per cold call, and selecting
one value out of six is itself error-prone (it still drifts long, 102–107 mm).
So **resolving the values is necessary but not sufficient — each builder must
also receive *only its own* resolved slice.** Resolve + partition-slice is the
fix; resolve dumped into one shared prompt is not. This is the same "bound the
contract each agent sees" principle the partition design rests on, now measured
on resolved values too. The resolver is deterministic plain code (`sum`,
optional `grid_mm`), fails loudly on infeasible contracts before any builder is
billed, and the eval toy's reference lengths are computed *by* the shipped
resolver — so the test gates exactly what the contract machinery emits.

### 11.2 Typed interfaces with type-specific gates *(first three kinds shipped)*

Today an interface is an untyped frame. Advanced components need a small taxonomy,
each kind carrying its contract fields and owning its merge gate — and nearly every
gate already exists as a tool or as an M2 gate helper waiting to be promoted into
`merge_assembly`:

| `kind` | contract fields | gate | status |
|---|---|---|---|
| `bore_fit` | min/max clearance band | `min_clearance` (overlap → too tight; gap < min → too tight, **incl. exact-touch**; gap > max → too loose) | ✅ shipped |
| `gear_mesh` | module, center distance, ratio | pitch-radii-sum + as-placed axis distance + ratio (the `kin_gearbox` oracle, promoted) | ✅ shipped |
| `frame_orientation` | child/parent frames, max angle | angle between published frame axes — the orientation `interface_align` does *not* check | ✅ shipped |
| `bolt_circle` | count, pitch, thread | frame mate + alignment check (shipped) | future |
| `thread` | callout, min engagement | the `thread_engagement` toy gate | future |
| `press_fit` | interference band | `press_fit_stress` | future |
| `sliding` / `kinematic` | axis, travel, DOF | `_sweep_clear` posed-interference sweep | future |

**Shipped 2026-06-12** (`driftpin/worker.py`): the manifest gains a `checks` list,
and `merge_assembly` dispatches each entry to its `kind`'s gate, folding the
violations into `gates["typed"]` and the merge `ok`. The gates return structured
violations (empty == pass), same shape as the classic gates, and an *unknown*
kind is itself a violation — a typed contract that silently does not run is worse
than one that fails loudly.

```jsonc
"checks": [
  { "kind": "gear_mesh", "a": "in0", "b": "out0",
    "module_mm": 2.0, "center_distance_mm": 48.0, "ratio": 2.0, "tol_mm": 0.5 },
  { "kind": "bore_fit", "pin": "peg", "bore": "plate", "min_clearance_mm": 0.1 },
  { "kind": "frame_orientation", "child": "lid", "parent": "housing",
    "child_iface": "seat", "parent_iface": "seat", "max_angle_deg": 1.0 }
]
```

**`bore_fit` closes the exact-touch blind spot** (§6, finding 4): `interference`
reads *zero* for tangent solids, so a slip fit gated only on non-interference
passes an exact-touch part; `bore_fit` requires positive clearance in a band, so
gap 0 fails. **`frame_orientation` closes the orientation gap** (§10, finding 4's
second edge): `interface_align` verifies frame *origins* only, so a secondary
interface positioned right but rotated slips through — `frame_orientation` gates
the angle.

**The gearbox becomes a manifest** (the canonical shared-constraint partition —
12 gears, one shared centre distance per mesh): `example/gearbox_manifest.py`
builds it as 12 components + 6 `gear_mesh` checks and merges + gates it in one
call. This surfaced a real gate interaction worth recording: **a typed contact
gate must be authoritative for its pair, because the blunt interference gate
false-positives on it.** Meshing involute teeth legitimately interpenetrate at
the pitch line in a static pose (the eval's "posed interference isn't the right
test for a gear ratio" finding) — so `merge_assembly` excludes a `gear_mesh`
pair (and any check flagged `"expected_contact": true`) from the interference
list. The exclusion is *targeted*: only the owned pair is excused, so a genuine
collision with a third part is still caught (tested). All three gates are
two-sided gate-validated free in `tests/test_typed_interfaces.py` (reference
passes; every negative — wrong size, wrong placement, wrong ratio, exact touch,
tilt — caught), in `run_all.sh`, no key.

Still future for §11.2: `bore_fit` carrying a named ISO fit *class* (resolving to
a clearance band via `fit_check`) rather than explicit mm; the internal-gear
mesh (planetary ring); and `thread` / `press_fit` / `sliding` from the table.

### 11.3 `verify_contract` — shift failure left *(shipped)*

One tool a builder calls before saving, checking its part against *its own slice*:
envelope self-check (local bbox vs declared envelope), every contracted interface
published and matching the contract (frame within tolerance), self-checkable
features (gear module, bore Ø, overall extent), declared intent invariants
(`verify_intent`) green. Returns `{handle, ok, results[]}` like `verify_intent`,
never raises. The pieces exist separately — `envelope_check` is assembly-level,
`verify_intent` is per-part, `interface_align_check` is post-merge — but unifying
them per-component turns the expensive loop (build → merge → gate fail → rebuild)
into a cheap local one, which the rounds-to-converge metric directly rewards.

**Shipped 2026-06-12** (`driftpin/worker.py:verify_contract`, exposed as an MCP
tool). The contract slice carries any of `envelope`, `interfaces`
(name → expected frame + tol; flags "forgot to publish" *and* "published in the
wrong place/orientation"), `features` (`gear` / `bore` / `extent` self-checks,
each tied to a toy we measured — gear module to the gearbox, bore Ø to
peg-in-hole, extent to the resolved tchain length), and `intent`. Each check
becomes a `{check, passed, detail}` row; an unsatisfiable check is a failed row,
never an exception. Two-sided gate-validated free in
`tests/test_verify_contract.py` (correct part → ok; every violation — too tall,
missing/misplaced/rotated frame, wrong teeth, absent bore, wrong length, intent
undeclared — caught as exactly the right row), in `run_all.sh`, no key.

The build-time checks deliberately mirror what `merge_assembly` will later verify
(an interface published in the right place here ⇒ it mates and aligns there; a
local bbox inside the envelope here ⇒ it clears the envelope gate there), so a
green `verify_contract` is a strong predictor of a green merge — the point of
shifting it left.

**Measured (2026-06-12, Haiku, n=20).** Handing each builder its slice as a
`verify_contract` contract and telling it to repair before saving (`M2_VERIFY=1`)
lifted **every** condition to 20/20 on the two toys whose failures are local
measurable defects: `tchainu_r` single **8/20 → 20/20** (+12), `nslot6` single
15/20 → 20/20 (+5), `nslot6` partition 19/20 → 20/20 (+1), `tchainu_r` partition
held at the 20/20 ceiling. The lift lands exactly where headroom and a
locally-measurable defect coincide — the single conditions that make
selection/copy errors — and barely moves near-ceiling partition. Builder turns
rose with the added self-check + repair (the agents genuinely re-verified, not
luck). This is the per-builder half of the shift-left loop measured as single-shot
pass-rate; the coordinator fan-in "rounds-to-converge" it would also cut is the
same mechanism (catch locally, not after merge). See
[`MULTI_AGENT_EVAL.md`](../tests/MULTI_AGENT_EVAL.md).

### 11.4 Hierarchical manifests — real nesting *(shipped)*

"Subassemblies nest the same pattern" needs mechanism, not a sentence. A component
entry may reference a child manifest instead of a file:

```jsonc
"components": {
  "drivetrain": { "manifest": "drivetrain/manifest.json" },   // a subassembly node
  "housing":    { "file": "components/housing.FCStd" }
}
```

**Shipped 2026-06-12** (`driftpin/worker.py`). `merge_assembly` recurses (any
depth): a child-manifest component is merged + gated *first*, the parent links its
merged root `.FCStd`, and BOM/interference/the typed gates flatten through the
nested `App::Part` to leaves. **A failed child fails the parent** — surfaced as
`gates["children"]` (`id → ok`) and a `children` block in the report, so a
coordinator can re-dispatch into the offending child manifest rather than guess.
The lockfile propagates change up the tree: a subassembly's lock entry hashes the
child's *merged root* (any geometry change → parent `modified`) and the child's
*lockfile* (a child interface move → `interface_changed`, carried to the parent's
neighbors via `depends_on`). A subassembly coordinator is then just an agent whose
deliverable is a merged, gate-passing manifest — bounding context per *coordinator*
the same way partitioning bounds it per *builder*.

Two-sided gate-validated free in `tests/test_hierarchical_manifests.py` (nested
reference passes and BOM flattens; a broken child fails the parent; a parent-level
clash between a subassembly and a sibling is still caught; two-level nesting
flattens to all leaves; `assembly_lock_check` flags a changed subassembly and
leaves an unchanged sibling alone), in `run_all.sh`, no key.

### 11.5 Standard parts — components without owners *(shipped)*

Parallel agents designing a gearbox should not each model their own bearings and
bolts. The tool surface already generates standard parts deterministically
(`add_fastener`, `add_bearing`, `add_gear`, `add_spring`, `add_sprocket`,
`add_pulley`, `add_rack`, `add_thread`); the manifest references them as ownerless
library components — `spec` is the generator tool's own kwargs verbatim:

```jsonc
"components": {
  "m6_bolt": { "library": { "tool": "add_fastener",
                            "spec": { "kind": "hex_bolt", "size": "M6", "length": 20 } } }
}
```

**Shipped 2026-06-12** (`driftpin/worker.py`). `merge_assembly` generates a
`library` component on the fly from `{tool, spec}` into a deterministic cache
(`.dp_lib/`, keyed by a spec hash so identical specs share one file), links it,
and reports it under `library` — **no builder agent, no owner, no file an agent
has to produce.** The tool is restricted to an allow-list of the deterministic
standard-part generators; a non-allow-listed tool or a bad spec fails loudly at
merge. The **lockfile keys a library part by its spec hash, not its bytes** — so
regenerating it never reads as drift, and only a *spec* change flags it
`modified`. This shrinks the fan-out *and* anchors interfaces: a bolt circle
mating against a generated fastener can't drift, because one side of the contract
is computed (`add_fastener` returns the major diameter a hole is sized to), not
designed.

Two-sided gate-validated free in `tests/test_standard_parts.py` (generates +
gates; deterministic/idempotent; the generated geometry matches the spec;
interference still catches a clashing standard part; the lockfile flags a spec
change but not a regenerate; a bad/non-allow-listed spec fails loudly), in
`run_all.sh`, no key.

### 11.6 Requirements-level gates *(tier-1 shipped)*

§6's gates predate the simulation tier; "the pieces fit" is not "the product
works." The manifest gains an optional `requirements` block gated at merge:

```jsonc
"requirements": {
  "density_kg_mm3": 7.9e-6,                          // material for the mass budget
  "max_mass_g": 450,
  "cg_window": { "min": [30,30,0], "max": [50,50,20] },
  "min_first_mode_hz": 120        // physics tier — deferred (reported as skipped)
}
```

**Tier-1 shipped 2026-06-12** (`driftpin/worker.py`). The always-on, cheap tier —
**total mass against a budget** and **centre-of-mass inside a window** — runs over
the merged tree's world-space leaves at every merge (`mass_properties` math + the
recursive leaf walk). It's tiered exactly because the doc warned: mass/CG always,
physics on demand. The measured numbers ride in `report["requirements"]` (visible
on a pass, so a coordinator sees the mass/CG even when it's within budget); only
the violations fail the merge. A `max_mass_g` without `density_kg_mm3` fails loudly
(can't compute mass), and an **unimplemented requirement key surfaces as `skipped`,
never a silent pass** — the same "don't drop a contract silently" discipline as the
typed gates.

The **physics tier** (`min_first_mode_hz` via FEM modal on the merged tree) is
*deferred*: FEM is proven on *single* agent-built parts (the `thermo_structural`
capstone runs thermal + structural CalculiX on one), but a modal gate on a merged
*assembly* opens real modelling questions — part bonding/contact and where the
product is fixed (boundary conditions) — that a clean v0 shouldn't fake. It is
reserved in the schema and reported as `skipped` so it can't be mistaken for met.

Two-sided gate-validated free in `tests/test_requirements_gates.py` (mass and CG
measured correctly; an over-budget mass and an off-window CG each caught; mass
without density loud; the physics tier surfaced as skipped; absent `requirements`
= back-compat), in `run_all.sh`, no key.

### 11.7 Manifest formalization

Stamp `"schema": "driftpin.manifest/1"`, validate on load, and version the manifest
content (a hash or counter in the lockfile) so "built against a stale contract" is
detectable as such, not only inferable from interface hashes.

### 11.8 Orchestration: ship it, pipeline it

Promote `orchestration/coordinator.py` to a shipped skill/workflow template (the
Phase 2 line item that hasn't landed), upgraded with §8's pipelined fan-in, the
round 0 contract review, the resolve step, and the per-builder isolation rules
(§7). The partition rule it should encode, from §10.1–10.2: **partition where
constraints couple, not merely where parts multiply** — the corrected k-sweep
found no breadth cliff through eight distinct interface pairs for Haiku-class
builders (re-baseline per model), but a single *shared derivation* reliably
breaks whoever holds it (tchain, twopin). So split along coupled constraints,
resolve them coordinator-side first (§11.1), and lean on hierarchy (§11.4)
rather than width when the contract grows.

---

## 12. Phased roadmap

- **Phase 0 — file handoff. ✅ shipped.** Two builders each `new_document` → build →
  `save_document`; a coordinator links the files and runs the gates.
  `example/phase0_walkthrough.py`.
- **Phase 1 — connective tissue. ✅ shipped 2026-05-30.** Manifest-driven
  `merge_assembly`; `publish_interface` + mate-by-frame (`add_part(mate=…)`);
  `interface_align_check`; recursive BOM + mass rollup; envelope gate.
- **Phase 2 — team mechanics. ✅ shipped 2026-05-30.** Lockfile
  (`assembly_lock`/`assembly_lock_check`) with internal-vs-interface change
  classification and staleness propagation; reference coordinator
  (`orchestration/coordinator.py`). *Outstanding:* the shipped-skill packaging
  (moved to §11.8).
- **Phase 3 — advanced assemblies. ← current.** In priority order: the resolve step
  (11.1) **✅ shipped 2026-06-12** (`driftpin/manifest.py`; validated `tchainu`
  partition 2/20 → 20/20); typed interfaces + promoted gates (11.2) **✅ first
  three kinds shipped 2026-06-12** (`bore_fit`, `gear_mesh`, `frame_orientation`
  in `merge_assembly`; closes the exact-touch + orientation gate edges; gearbox
  now a manifest); `verify_contract` (11.3) **✅ shipped 2026-06-12**
  (`driftpin/worker.py`; builder-side envelope/interface/feature/intent
  self-check); hierarchical manifests (11.4) **✅ shipped 2026-06-12**
  (`merge_assembly` recurses into child manifests, gates roll up, lockfile
  propagates staleness up the tree); standard parts (11.5) **✅ shipped
  2026-06-12** (`library` components generated from a spec at merge — no owner,
  lock keyed by spec hash); requirements gates (11.6) **✅ tier-1 shipped
  2026-06-12** (mass + CG over the merged product; physics/FEM tier deferred,
  reported as skipped); schema formalization (11.7); shipped, pipelined
  orchestration (11.8).
- **Phase 4 — deferred.** Shared co-editing, *reframed* as **claimed-region
  editing**: an agent claims a sub-tree/feature region of one document, edits only
  there, and merges back — never free-for-all cursors. Carries the full concurrency
  cost from §1 and §7; only worth it if a real use case demands single-document
  collaboration.

---

## 13. Open questions

Settled since the original RFC:

- ~~Mate solver depth~~ — **simple frame alignment shipped** (`Pc = Pp·Fp·Fc⁻¹`);
  the kinematic toys showed analytic gates cover mechanism checking without a joint
  solver. JCS joints remain a future option only if motion *authoring* is needed.
- ~~Partition granularity~~ — **answered empirically** (§10.1–10.2): partition
  along coupled constraints (no breadth cliff found through 8 interface pairs);
  depth via hierarchy, not width.
- ~~Cross-file parametric coupling~~ — **resolved by design** (§11.1): no live
  expression links; the resolve step propagates parameters through the manifest.

Still open:

- **Tolerance & units contract** — typed interfaces (§11.2) carry fits per
  interface, but a true tolerance *stack-up* across a chain of mates
  (`tolerance_stackup` exists part-level) has no merge-level gate yet. Where does
  the stack-up budget live in the manifest, and who owns it when it fails?
- **Interface conflict resolution** — when two components both need a *shared*
  interface to change, who wins, and how does the coordinator mediate without a
  human? Round 0 amendments (§8) surface the conflict early but don't arbitrate it.
- **Requirements-gate cost** — FEM at merge is minutes, not milliseconds. When is a
  physics gate part of the loop vs a final-candidate check, and does a failed
  physics requirement re-dispatch components the way a fit failure does (whose
  part made it too heavy)?
- **Resolve-step expressiveness** — 11.1 starts with sums/equalities/grids. Where
  is the line before it becomes a constraint solver the coordinator can't reason
  about deterministically?

---

## Appendix A — Claude reference orchestration (one worked example)

This is *one* way to drive the roles in §8, using Claude Code's own agent tooling. It is
**not part of the protocol** — the protocol is the manifest + primitives; this is a host
binding. `orchestration/coordinator.py` implements the same loop host-side.

1. **Decompose & draft.** The coordinator (main session) breaks the product into the
   part-DAG, writes the manifest — components, envelopes, mates, constraints — and
   runs the **resolve step** (§11.1) so every slice carries literal values.
2. **Round 0 review.** Cheap parallel pass: each prospective builder reads its slice
   and returns accept/amend. Amendments fold back into the manifest before any
   geometry is built.
3. **Fan out.** One subagent per leaf component (the `Agent` tool, or a `Workflow`
   `pipeline` stage), each prompted with *only* its resolved slice. Each runs against
   its own DriftPin MCP server → its own worker → its own file, in its own working
   directory (§7).
4. **Build & self-verify.** Each component agent builds to contract, calls
   `publish_interface` for its mating frames, self-checks with `verify_contract`
   (§11.3 — envelope, contracted interfaces, features, intent; plus a `render_views`
   look), saves, and returns its file path + status.
5. **Fan in per node, pipelined.** Each subassembly merges and gates as soon as its
   own children land (`merge_assembly` on the child manifest, §11.4); the root merges
   last. A leaf failure re-dispatches that leaf while unrelated branches keep going.
6. **Re-dispatch on failure.** On a gate failure, the coordinator edits the manifest
   (move a frame, grow an envelope, re-resolve constraints), writes the lock, and
   `assembly_lock_check` tells it *exactly* which components are stale (§9). Only
   those rebuild. Loop until the gates pass.

Sketch (`Workflow` shape — note `pipeline`, not a single barrier):

```text
manifest = coordinator.decompose(spec)             // draft ICD + resolve constraints
amends  = parallel(slices.map(s => () => agent(reviewPrompt(s))))   // round 0
manifest = coordinator.fold(amends)
results = pipeline(manifest.nodes,                  // leaves and subassemblies
  node => agent(buildPrompt(node), {schema: BUILD_REPORT}),
  node => merge_when_children_done(node))           // per-node fan-in
while (!root_report.ok) {                           // verify loop
  manifest = coordinator.renegotiate(root_report)   // re-resolve, bump version
  stale = assembly_lock_check(manifest)             // exact re-dispatch set
  parallel(stale.map(c => () => agent(rebuildPrompt(c, manifest))))
  root_report = merge_assembly(manifest)
}
```

---

## Appendix B — Notes for other AI coding tools

Nothing above is Claude-specific below the orchestration line. Any host can play the
roles in §8 if it has three things:

1. **Parallel sessions** — the ability to run N independent DriftPin MCP sessions at once
   (one per component builder) plus a coordinating session. Cursor/Cline tasks, a CI
   matrix, a shell script spawning N `driftpin mcp` processes, or even N humans all
   qualify.
2. **A shared filesystem** — for the manifest, the `.lock.json`, and the component
   `.FCStd` files. That filesystem *is* the coordination bus; there is no other channel.
3. **The DriftPin primitives** — `publish_interface`, `merge_assembly`, the gates,
   `assembly_lock`/`assembly_lock_check`. These are plain MCP tools; any MCP-capable
   host calls them identically.

A host with no agent-spawning at all can still use the model serially: one operator builds
each component in turn, then runs `merge_assembly`. The contract is the same; only the
parallelism is lost. That is the portability the thin-primitive boundary buys.

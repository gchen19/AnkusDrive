# DriftPin multi-agent collaboration (RFC)

How a *team* of agents — Claude subagents, another AI coding tool's agents, or
humans at terminals — can design one mechanical product together: splitting it
into components and subassemblies, building those in parallel, and constructing
the whole back up with the pieces actually fitting.

Companion to [`ROADMAP.md`](ROADMAP.md). The roadmap is about deepening what *one*
agent can do; this doc is about *many* agents sharing the work. It is a design
proposal, not shipped surface — Phase 0 is runnable today, everything past it is
a plan.

**Related docs:**
- [`README.md`](../README.md) — architecture (the long-lived worker, stdio JSON).
- [`docs/ROADMAP.md`](ROADMAP.md) — Slice 5 "Status" already defers two pieces this
  doc depends on: *mating constraints (JCS machinery)* and *BOM across nested
  sub-assemblies (recursion)*. This RFC is where those land.
- [`tests/RELIABILITY.md`](../tests/RELIABILITY.md) — the "can the agent see what it
  built?" loop. The merged assembly has to pass it too.
- [`tests/MULTI_AGENT_EVAL.md`](../tests/MULTI_AGENT_EVAL.md) — how we decide whether
  this whole scheme works reliably: hard-oracle merge gates, a toy-problem ladder
  with negative controls, and the metrics (false-pass rate, single-agent baseline).

---

## 1. Problem & non-goals

There are two things people mean by "agents working on a part together":

- **(a) Shared co-editing** — several agents mutating *one* document at the same
  time, like a Google-Docs cursor pile-up on a single FreeCAD model.
- **(b) Partition + merge** — split the product into components/subassemblies, each
  owned and built independently, then assembled into the whole.

**This RFC targets (b), and treats (a) as a non-goal for now** (revisited, narrowed,
in Phase 3). The reason is structural, not incidental:

DriftPin's worker is a single long-lived `freecadcmd` process with a single
synchronous dispatch loop (`worker.py:_main`, ~3086): read one newline-JSON
request, run one `HANDLERS[method]`, write one response. *All* mutable state is two
process globals — `_handles` (handle-string → live FreeCAD object, in-memory, dies
with the process) and FreeCAD's `App.ActiveDocument`. Handles are not addressable
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

```
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

This maps 1:1 onto machinery DriftPin already has:

- `make_assembly` (`worker.py:2123`) creates an `App::Part` container.
- `add_part` (`worker.py:2133`) links **an in-doc handle *or* an external `.FCStd`
  by path** (`App.openDocument(path, True)` → `App::Link`) with a placement.
- `_world_shape` (`2203`), `interference_check` (`2215`), `bom_extract` (`2248`),
  `list_assembly_parts` (`2186`) analyze the assembled result with world transforms.

So the skeleton of partition + merge **works today** (see Phase 0). What's missing
is everything that makes it *reliable between agents who never talk directly*: a
shared contract, stable references to mate against, a deterministic merge, rollup
verification, and change propagation. The rest of this doc fills those gaps — as
**thin, tool-agnostic primitives**. No orchestration lives in DriftPin.

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

```jsonc
{
  "schema": "driftpin.manifest/1",
  "name": "gearbox",
  "root": "gearbox.FCStd",
  "shared_parameters": {            // global facts every component must honor
    "bolt": "M4",
    "wall_mm": 3.0,
    "fit_clearance_mm": 0.2
  },
  "components": {
    "housing": {
      "owner": "agent-1",
      "file": "components/housing.FCStd",
      "datum_frame": "LCS_origin",  // the component's mating coordinate system
      "envelope": { "min": [0,0,0], "max": [80,80,40] },   // keep-out box (mm)
      "interfaces": {
        "lid_mating_face": {        // a published, named frame other parts mate to
          "frame": { "origin": [40,40,40], "z_axis": [0,0,1], "x_axis": [1,0,0] },
          "bolt_circle": { "count": 4, "pitch_mm": 64, "hole": "M4" }
        }
      }
    },
    "lid": {
      "owner": "agent-2",
      "file": "components/lid.FCStd",
      "datum_frame": "LCS_origin",
      "envelope": { "min": [0,0,0], "max": [80,80,8] },
      "interfaces": {
        "mount": {                  // lid's own frame that aligns to housing's
          "frame": { "origin": [40,40,0], "z_axis": [0,0,-1], "x_axis": [1,0,0] },
          "bolt_circle": { "count": 4, "pitch_mm": 64, "hole": "M4" }
        }
      }
    }
  },
  "mates": [                        // how the tree is assembled
    { "child": "lid", "child_iface": "mount",
      "parent": "housing", "parent_iface": "lid_mating_face" }
  ]
}
```

Key properties: it is **declarative** (no geometry, just contracts), **tool-agnostic**
(any host reads/writes it), and **the only coupling** between component owners. An
owner reads only its own slice plus `shared_parameters` and `mates` that touch it.

---

## 4. Stable cross-file references — mate by named frame

`add_part` today places a child by a raw `[x,y,z]` (or position/axis/angle). That's
brittle across a team: a magic number in one agent's head, re-derived by hand in
another's. We want to mate by **named frame**, the same way DriftPin already lets
agents select geometry by stable, content-addressed tags rather than positional
`Face2` indices (`list_faces`/`query_faces`/`resolve_face`, the `f_<hash>`/`e_<hash>`
signatures in `worker.py`).

Extend that philosophy from faces/edges to **published interface frames**:

- **`publish_interface(handle, name, frame)`** — record a named datum (a datum
  plane / Local Coordinate System / `App::Placement`) on a component and write it
  into the component's manifest entry. This is the component owner's act of saying
  "*here* is where you bolt to me, and this is its orientation."
- **Merge places a child by aligning its published frame to the parent's target
  frame**, per the manifest `mates` list — not by a hand-computed translation. The
  placement is *derived* from the contract, so it can't drift out of sync with it.

Two implementation paths, to be chosen in Phase 1:

- **Simple frame alignment** — compute the rigid transform that maps the child's
  published `App::Placement` onto the parent's, set `link.Placement`. Deterministic,
  no solver, easy to reason about. Good default.
- **Native Assembly workbench joints** — FreeCAD 1.0 ships an integrated Assembly
  workbench with a constraint solver (the `JCS` machinery the ROADMAP's Slice 5
  Status already names as deferred). Richer (true joints, DOF, motion) but heavier
  and less deterministic. Flagged as an open question (§11).

---

## 5. Merge / construct-up protocol — `merge_assembly`

A single primitive, callable by any host, that turns a manifest into an assembled,
verified document. It contains **no orchestration** — it neither spawns nor waits on
agents; it just assembles whatever component files currently exist.

```
merge_assembly(manifest_path) ->
  1. new_document(root)
  2. make_assembly(root_part)
  3. for each component: add_part(source={path: file}) → App::Link
  4. for each mate: place child by aligning child_iface frame to parent_iface frame
  5. recompute
  6. run verification gates (§6)
  7. return a structured report { placed[], gates{...}, ok: bool }
```

Three properties this primitive must guarantee:

- **Deterministic** — same manifest + same component files ⇒ same result, every time.
- **Idempotent** — re-running on an unchanged tree changes nothing.
- **Re-runnable / live** — `App::Link` reloads geometry from the source `.FCStd`, so
  when a component owner saves a new version, re-running `merge_assembly` picks it up.
  Merge is cheap and repeatable; that's what makes the verify loop in §8 viable.

`bom_extract` is single-level today (`worker.py:2248`); the merged tree is nested, so
this phase also adds **recursive BOM + mass rollup** (the ROADMAP Slice 5 Status lists
"BOM extraction across nested sub-assemblies (recursion)" as deferred — it lands here).

---

## 6. Verification gates — "is the whole actually consistent?"

Partition + merge only pays off if the *whole* can be checked without any single
agent holding the whole design in its head. After merge, run:

- **Interference** — `interference_check` (exists): pairwise `Part.common().Volume`.
  Anything above threshold is a fit failure.
- **Envelope / keep-out** (new) — each component's world-space bounding box must stay
  inside the `envelope` it declared in the manifest. This is what stops one agent's
  "internal" growth from silently colliding with a neighbor it never sees.
- **Recursive BOM + mass rollup** (new, see §5) — counts and mass across the nested
  tree, so the coordinator can sanity-check part counts and total weight.
- **Visual** — render the merged assembly via `render_views` and run it through the
  Layer A/B/C reliability loop ([`RELIABILITY.md`](../tests/RELIABILITY.md)): the
  merged result must *look* like the intended product, not just pass numeric checks.

The gates return structured data, not prose — so any host's coordinator can branch on
them mechanically (re-dispatch the offending component, tighten an envelope, etc.).

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

---

## 8. Orchestration responsibilities (host-agnostic — roles, not tools)

DriftPin provides primitives; **the host provides orchestration.** Described as roles
so any host maps them to its own mechanism. The manifest + component files are the
*only* interface between roles — roles never share process state.

- **Coordinator** — decomposes the product into the part-DAG; drafts the manifest /
  ICD; hands each builder its slice; after builders report, calls `merge_assembly` and
  the gates; on a gate failure, renegotiates the contract (e.g. moves a frame, grows
  an envelope) and re-dispatches *only* the affected components.
- **Component-builder** — reads its manifest slice + `shared_parameters`; builds its
  `.FCStd` to contract; `publish_interface` for each mating frame it owns;
  self-verifies before reporting (`verify_feature`, an envelope self-check, a
  `render_views` look); saves; reports its file + status.

The loop is **fan-out (builders) → fan-in (merge + gates) → re-dispatch on failure**.
Nothing in it is specific to a vendor. Appendix A shows Claude doing it concretely;
Appendix B shows what a different host needs.

---

## 9. Change propagation & provenance

Re-running merge is cheap, but re-*building* components is not, so distinguish:

- **Internal change** — geometry moved but stays within the declared `envelope` and no
  published interface frame moved ⇒ **no neighbor impact**. Just re-run `merge_assembly`;
  links reload the new geometry.
- **Interface change** — an `envelope` grew or a published `frame` / `bolt_circle`
  moved ⇒ **every neighbor that mates against it must re-evaluate.** The coordinator
  renegotiates the manifest and re-dispatches those neighbors only.

Track provenance in a **lockfile** (a manifest sidecar), tool-agnostic and file-based:

```jsonc
// gearbox.lock.json
{ "housing": { "file": "components/housing.FCStd", "hash": "blake2b:…",
               "owner": "agent-1", "built_against": "manifest@7",
               "verified": "ok" },
  "lid":     { "file": "…", "hash": "…", "built_against": "manifest@6",
               "verified": "stale" } }   // built against an older contract → rebuild
```

The hash detects whether a component changed; `built_against` detects whether it was
built against the current contract. Together they let any coordinator spot stale and
drifted components without reading geometry.

---

## 10. Phased roadmap

- **Phase 0 — runnable today, zero new code.** Document and exercise the file-handoff
  pattern with existing tools: two builders each `new_document` → build → `save_document`;
  a coordinator `make_assembly` → `add_part(source={path:…})` per file → `interference_check`
  → `bom_extract` → `render_views`. This *is* the proof the model works (see Verification).
- **Phase 1 — connective tissue.** Manifest/ICD schema; `publish_interface` + datum
  tooling; `merge_assembly` with mate-by-frame; recursive BOM + mass rollup; envelope /
  keep-out gate.
- **Phase 2 — team mechanics.** Change propagation (internal vs interface); lockfile +
  staleness/drift detection; reference orchestration templates (Appendix A as a shipped
  skill/workflow; Appendix B as docs).
- **Phase 3 — deferred / open.** Shared co-editing, *reframed* as **claimed-region
  editing**: an agent claims a sub-tree/feature region of one document, edits only there,
  and merges back — never free-for-all cursors. Carries the full concurrency cost from §1
  and §7; only worth it if a real use case demands single-document collaboration.

---

## 11. Open questions

- **Mate solver depth** — simple frame alignment (deterministic, no solver) vs. the
  native FreeCAD 1.0 Assembly workbench joint solver (richer, heavier, less
  deterministic). Start simple; revisit if joints/motion are needed.
- **Cross-file parametric coupling** — FreeCAD supports external expression links
  between documents, but they are fragile headless. Is the manifest's
  `shared_parameters` enough, or do components need live parametric links (change the
  bolt size once, all parts follow)?
- **Tolerance & units contract** — where do fits/clearances live, and how are they
  enforced at merge (the envelope gate is coarse; true tolerance stack-up is more)?
- **Partition granularity** — per-part vs per-subsystem. Too fine and the manifest
  dominates; too coarse and parallelism evaporates.
- **Interface conflict resolution** — when two components both need a *shared* interface
  to change, who wins, and how does the coordinator mediate without a human?

---

## Appendix A — Claude reference orchestration (one worked example)

This is *one* way to drive the roles in §8, using Claude Code's own agent tooling. It is
**not part of the protocol** — the protocol is the manifest + primitives; this is a host
binding.

1. **Decompose & draft.** The coordinator (main session) breaks the product into the
   part-DAG and writes `manifest.json` — including envelopes, published frames, and the
   `mates` list — before any geometry exists.
2. **Fan out.** The coordinator spawns one subagent per leaf component (the `Agent` tool,
   or a `Workflow` `parallel`/`pipeline` stage), each prompted with *only* its manifest
   slice + `shared_parameters`. Each runs against its own DriftPin MCP server → its own
   worker → its own file.
3. **Build & self-verify.** Each component agent builds to contract, calls
   `publish_interface` for its mating frames, self-checks (`verify_feature`, envelope
   self-check, a `render_views` look), saves, and returns its file path + status.
4. **Fan in.** The coordinator calls `merge_assembly(manifest)` and the §6 gates.
5. **Re-dispatch on failure.** On an interference or envelope violation, the coordinator
   edits the manifest (move a frame, grow an envelope), bumps the contract version, and
   re-dispatches *only* the affected components (§9). Loop until the gates pass.

Sketch (`Workflow` shape):

```
manifest = coordinator.decompose(spec)            // draft ICD
parts = parallel(manifest.leaves.map(c =>          // fan-out: one builder per component
  () => agent(buildPrompt(c, manifest), {schema: BUILD_REPORT})))
report = merge_assembly(manifest)                  // fan-in: assemble + gates
while (!report.ok) {                               // verify loop
  manifest = coordinator.renegotiate(report)       // move frames / grow envelopes
  parallel(report.affected.map(c => () => agent(rebuildPrompt(c, manifest))))
  report = merge_assembly(manifest)
}
```

Subassemblies nest the same pattern: a subassembly node is itself a coordinator over its
children, merged before the root merges it.

---

## Appendix B — Notes for other AI coding tools

Nothing above is Claude-specific below the orchestration line. Any host can play the
roles in §8 if it has three things:

1. **Parallel sessions** — the ability to run N independent DriftPin MCP sessions at once
   (one per component builder) plus a coordinating session. Cursor/Cline tasks, a CI
   matrix, a shell script spawning N `driftpin mcp` processes, or even N humans all
   qualify.
2. **A shared filesystem** — for `manifest.json`, the `.lock.json`, and the component
   `.FCStd` files. That filesystem *is* the coordination bus; there is no other channel.
3. **The DriftPin primitives** — `publish_interface`, `merge_assembly`, the gates. These
   are plain MCP tools; any MCP-capable host calls them identically.

A host with no agent-spawning at all can still use the model serially: one operator builds
each component in turn, then runs `merge_assembly`. The contract is the same; only the
parallelism is lost. That is the portability the thin-primitive boundary buys.

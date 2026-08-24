---
name: parallel-build
description: >-
  Run this when you are the ORCHESTRATOR fanning out several agents to build one
  mechanical assembly in parallel — a Claude Code main session spawning subagents,
  a Cursor task tree, a CI matrix, or any MCP host. It is the host-agnostic recipe
  for AnkusDrive's partition-and-merge contract: decompose the product into standalone
  per-component `builder brief`s, hand each builder its brief plus the FULL AnkusDrive
  tool surface (not a bundled 6-tool loop), isolate each builder in its own workspace,
  have each self-gate with `component_contract_check` before saving, then merge and
  gate the integrated result with `merge_assembly` + `interference_check` +
  `interface_align_check` + `assembly_lock`. Use it whenever you split a build across
  agents, write a builder brief, or assemble independently-built components. Companion
  to `design-modularly` (that one helps you decide WHERE to cut; this one runs the cut).
---

# parallel-build

AnkusDrive builds an assembly by **partition and merge**, never by co-editing one
file (docs/MULTI_AGENT.md §1). Each component is one `.FCStd` with exactly one
writer; the agents never share process state — their only shared truth is data on
the filesystem: the per-component **builder brief** and the assembly **manifest**.
Because that boundary is pure data, *any* MCP host can play the orchestrator, and
each builder is *any* MCP-connected agent with the full tool surface — not a
special bundled loop.

This skill is the runnable recipe. Its falsifiable proof is
[`example/host_agnostic_builder_demo.py`](../../example/host_agnostic_builder_demo.py):
a plain-code orchestrator that decomposes a 3-part stacked bracket into three
briefs, builds each in its own worker, self-gates each, then merges and passes
every integration gate. Run it: `.venv/bin/python3 example/host_agnostic_builder_demo.py`.

## The loop

### 1. Decompose into builder briefs

Cut the product along **coupled constraints, not merely part count** (the eval
finding, §10.1–10.2). Emit one `ankusdrive.builder_brief/1` per component
(`ankusdrive/builder_brief.py`, `validate_builder_brief`):

```json
{
  "schema": "ankusdrive.builder_brief/1",
  "component": "housing", "assembly": "gearbox",
  "task": "Build an 80x80x40 mm housing ...; publish 'lid_seat' at (0,0,40) +Z; save.",
  "output": "components/housing.FCStd",
  "envelope": {"min": [0,0,0], "max": [80,80,40]},
  "interfaces": {"lid_seat": {"origin": [0,0,40], "z_axis": [0,0,1]}},
  "shared_parameters": {"bolt": "M4", "wall_mm": 3.0},
  "material": "AISI 1045", "constraints": {"process": "cnc_milling"}
}
```

- **Restate every dimension** in the `task` — a builder sees ONLY its brief.
- **Resolve shared math coordinator-side.** Hand builders *results*, never a
  derivation (`resolve_constraints`); the #1 measured failure is two agents
  re-deriving `x = center ± spacing/2` and one slipping.
- Put any value two parts must agree on into BOTH briefs (or a `shared_parameters`
  fact), and pin the mating **frame** in `interfaces`.

> **Ask: "could a builder satisfy this brief without ever seeing another's?"** If
> not, the cut leaks — move the shared value into the contract.

### 2. Fan out — one builder, one workspace, full tool surface

Each builder is an independent agent (a Claude Code subagent, a Cursor task, a
shell). Give it the rendered brief (`builder_brief_text(brief)`) and let it drive
the whole 240+ tool surface — sketches, PartDesign, fillets, gears, FEM — not a
6-tool subset.

**Isolate every builder.** Two equivalent options (docs/MULTI_AGENT.md §7):

- **Own MCP server / worker** — the classic model: one agent = one `ankusdrive mcp`
  process = one worker = one file. Real OS-level parallelism, zero shared state.
- **Own named workspace on a shared server** (issue #167) — one MCP server, each
  builder calls `use_workspace("<component-id>")` once up front to claim its own
  freecadcmd process; handles and documents do NOT cross workspaces
  (`list_workspaces` to see the pool, `close_workspace` to free a slot; cap
  `ANKUSDRIVE_MAX_WORKSPACES`, default 4). Use this when you cannot spawn N servers.

Either way: **handles never cross a builder boundary.** A handle from one
builder is meaningless to another; the only cross-builder currency is the saved
`.FCStd` and its published frames.

### 3. Self-gate before saving — `component_contract_check`

Before `save_document`, each builder runs the builder-side half of the merge gate
on its own part and repairs any failure:

```
component_contract_check(handle, brief)
  -> {ok, checks:[{check, passed, detail}], reasons:[...]}
```

It checks exactly what the coordinator re-runs at merge: **watertight**
(`check_shape` — one clean solid), **envelope** (local bbox inside the declared
keep-out box), and every required **interface** published with a sane frame (and
within tol of a pinned origin/axis). Catching a violation here turns the expensive
loop (build → merge → gate-fail → rebuild) into a cheap local one. It never raises,
so call it in a repair loop. (`verify_contract` is its richer sibling — add it for
per-feature self-checks like a gear module or bore Ø.)

> **Ask: "is `component_contract_check(handle, brief).ok` true?"** If not, do not
> report done — fix the failing check first.

### 4. Fan in — merge and gate the integrated result

The orchestrator projects the built files + their mates into a
`ankusdrive.manifest/1` and calls `merge_assembly`, which links each component by
file, seats it by published frame (mate-by-frame), and runs the integration gates:

- **`interference_check`** — no pair of parts clashes.
- **envelope** — each part's WORLD bbox stays inside its assembly-frame envelope.
- **`interface_align_check`** — secondary interface datums (`verify_align`) coincide
  in world space, not just the primary mate.
- then **`assembly_lock`** / `assembly_lock_check` — write the provenance lockfile
  so a later edit re-dispatches only the stale components.

`merge_assembly` returns `{ok, gates, placed}`. On a gate failure, renegotiate the
manifest (move a frame, grow an envelope, re-resolve constraints) and re-dispatch
only the implicated builders — `assembly_lock_check` tells you exactly which.

> **Ask: "does the merged report's `ok` hold with every gate empty?"** That — not
> any single builder's green self-check — is the definition of done.

## Done means

The integrated `merge_assembly` report is `ok`, with `interference`, `envelope`,
and `interface_align` all empty, and `assembly_lock_check` clean. The proof script
asserts exactly this; make your build pass the same bar.

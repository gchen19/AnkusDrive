# DriftPin examples

## Phase 0 multi-agent walkthrough

`phase0_walkthrough.py` is the runnable proof-of-concept for
[`docs/MULTI_AGENT.md`](../docs/MULTI_AGENT.md) §10 (Phase 0): a team of agents
**partitions** a design into components, builds them independently, then a
coordinator **merges** them into one assembly and runs the verification gates —
all with **today's** DriftPin tools, zero new code.

The point it proves: agents that never share process state can still build one
product together, because their only coupling is a shared **interface contract**
(`phase0_manifest.json`) plus the component `.FCStd` files on disk. That file
boundary is exactly what the RFC's Phase 1 primitives (`publish_interface`,
`merge_assembly`, mate-by-frame) will formalize — here we make the same moves by
hand so the model is visible.

### Run it

```bash
.venv/bin/python3 example/phase0_walkthrough.py
```

(The host venv needs Pillow + numpy for the render step; renders are skipped
gracefully if absent. Requires a working `freecadcmd` — see the top-level README.)

### What it does

The demo product is a plate with two pegs standing on it.

1. **Builders (fan-out).** One `Worker` session per component — separate OS
   process, separate file, single writer. Each reads its slice of the manifest,
   builds to the `build` spec, runs an **envelope self-check** (its bbox must stay
   inside the box it promised neighbors), renders itself, and saves:
   - `phase0_plate.FCStd` — 60×60×8 box (owner `agent-plate`)
   - `phase0_peg.FCStd` — Ø16×20 cylinder (owner `agent-peg`)
2. **Coordinator (fan-in).** A new `Worker` opens an assembly document,
   `make_assembly`, saves it (a cross-document `App::Link` needs the owner doc on
   disk first), then `add_part(source={path: ...})` once per instance — linking
   each component **by file path** and placing it. This is the handoff: the
   assembly references the component files on disk.
3. **Gates.**
   - **Interference** (`interference_check`) — the intended fit returns `[]`
     (parts touch, don't clash).
   - **BOM** (`bom_extract`) — rolls up `Box ×1, Cylinder ×2` with mass.
   - **Visual** — an `App::Part` has no single Shape, so the script compounds the
     placed parts (via the `run_script` escape hatch), meshes with `tessellate`,
     and rasterizes `phase0_assembly_iso.png` with `driftpin.render`.
4. **Negative control.** The same interference gate is re-run on a deliberately
   broken layout (a peg embedded 4 mm into the plate) and must **catch** the clash
   (~804 mm³) — proving the gate discriminates fit from collision, not just passes.

### Manifest → RFC concept map

| `phase0_manifest.json` field | RFC concept (`docs/MULTI_AGENT.md`) |
|---|---|
| `components[*].owner` / `file` | one file, one owner per part-DAG node (§2) |
| `components[*].build` | the builder's contract (§8) |
| `components[*].envelope` | keep-out box → envelope gate (§6) |
| `instances[*].placement` | raw placement *today*; mate-by-named-frame in Phase 1 (§4) |
| (whole file) | the interface contract / ICD (§3) |

### Outputs (git-ignored; regenerated on each run)

`phase0_plate.FCStd`, `phase0_peg.FCStd`, `phase0_assembly.FCStd`,
`phase0_clash.FCStd`, `phase0_{plate,peg,assembly}_iso.png`. Open
`phase0_assembly.FCStd` in the FreeCAD GUI to see the merged result with its live
`App::Link`s back to the component files.

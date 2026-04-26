# DriftPin

A CLI + MCP server that drives [FreeCAD](https://www.freecad.org/) through its Python API so LLMs (and humans at a terminal) can design mechanical parts and run FEM simulations without clicking through the GUI.

## Why

FreeCAD exposes almost everything it does through a Python API — create documents, build sketches, extrude solids, mesh them, run CalculiX/Elmer FEM solves, read back stress/displacement fields. But that API lives inside FreeCAD's embedded Python (`freecadcmd`), which is awkward to call from anywhere else. DriftPin wraps it behind two surfaces:

- **CLI** — one-shot commands (`driftpin run script.py`, `driftpin box --w 10 --d 20 --h 5 -o part.FCStd`) for scripts, CI, and quick iteration.
- **MCP server** — structured tools (`create_document`, `add_primitive`, `boolean_cut`, `make_fem_analysis`, `run_solver`, `get_results`) so an LLM agent can model and simulate iteratively.

## Target environment

- FreeCAD 1.0+ (tested path: `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd`)
- Bundled Python, `ccx` (CalculiX), and `gmsh` already ship inside the `.app` — no extra install needed for basic FEM.

## Architecture sketch

```
 ┌────────────┐      ┌────────────┐      ┌──────────────────────┐
 │  MCP host  │ ───► │ DriftPin   │ ───► │  freecadcmd worker   │
 │  (Claude)  │      │ (Python)   │ IPC  │  (long-lived Python) │
 └────────────┘      └────────────┘      └──────────────────────┘
       ▲                    ▲                        │
       │                    │                        ▼
       └── CLI user ────────┘               .FCStd / .inp / .vtk
```

Key decision: **long-lived worker with JSON-over-stdin/stdout**, not subprocess-per-call. FreeCAD startup is ~1–2s; re-paying that per tool call is unacceptable for an interactive agent. The worker is a small Python loop launched under `freecadcmd`, reading commands, dispatching to handlers, returning structured results (including object IDs so follow-up calls can reference created geometry).

## FreeCAD API surface we care about

Notes gathered from the scripting docs and the FEM Python tutorial:

**Core (App):**
- `App.newDocument(name)` / `App.ActiveDocument` / `doc.recompute()` / `doc.save(path)`
- `doc.addObject("Part::Box", "name")` — typed object creation; properties set after (`box.Height = 5`)
- `doc.supportedTypes()` for introspection; `obj.TypeId`, `obj.isDerivedFrom("Part::Feature")`

**Modeling:**
- `Part` — `makeBox`, `makeCylinder`, `makeSphere`, boolean `cut/common/fuse`, fillets, lofts (OpenCASCADE under the hood)
- `Draft` — 2D primitives, `move`, arrays
- `Sketcher` + `PartDesign` — parametric sketch-driven solids (most "real" mechanical design happens here)
- `FreeCAD.Vector`, `Placement` for positioning

**FEM (`ObjectsFem` + `femtools`):**
- `ObjectsFem.makeAnalysis(doc, "Analysis")` — container
- `makeSolverCalculixCcxTools` / `makeSolverElmer` — solver objects with tunables (`GeometricalNonlinearity`, `ThermoMechSteadyState`, …)
- `makeMaterialSolid` — assign `YoungsModulus`, `PoissonRatio`, `Density`
- Constraints: `makeConstraintFixed`, `makeConstraintForce`, `makeConstraintPressure`, `makeConstraintDisplacement`, contact/tie/spring, thermal
- Mesh: `makeMeshGmsh` + `femmesh.gmshtools.GmshTools(...).create_mesh()` (or Netgen)
- Run: `femtools.ccxtools.FemToolsCcx().run()`
- Results: iterate `analysis.Group` for `Fem::FemResultObject`; read `.DisplacementVectors`, stress fields

**Headless invocation:**
- `freecadcmd script.py` — runs script then exits
- `freecadcmd` with no args — interactive Python REPL (what the worker will drive)
- `--console`, `-M <moddir>`, `-P <pypath>`, `--pass <args>`, `FreeCAD.ConfigGet(...)` for env info
- `FreeCADGui` is **not** available headless — keep design logic in `App`/`Part`/`Fem` only

## How an agent reaches FreeCAD: three layers

DriftPin exposes FreeCAD through three layers, each with a different audience
and a different cost-of-use. Knowing which layer a feature lives in tells you
how to invoke it.

### Layer 1 — typed MCP tools (the agent surface)

~70 first-class MCP tools cover the **core mechanical-design surface area**.
They have validated parameters, structured returns, and stable handles for
chaining. This is the happy path — what an agent uses for things people do
every day.

| Domain | What's covered |
|---|---|
| Document lifecycle | `new_document`, `open_document`, `save_document`, `list_documents`, `set_active_document`, `close_document` |
| Geometry primitives | `add_primitive` (box/cyl/sphere), `boolean_op`, `export_shape` (STEP/IGES/BREP/STL) |
| Selection (stable refs) | `list_faces`, `list_edges`, `query_faces`, `resolve_face`, `resolve_edge` |
| PartDesign | `make_body`, `make_datum_plane`, `make_sketch`, `add_sketch_geometry`, `add_sketch_constraint`, `add_sketch_external`, `close_sketch`, `pad`, `pocket`, `revolve`, `hole`, `loft`, `sweep`, `helix`, `partdesign_fillet`, `partdesign_chamfer`, `linear_pattern`, `polar_pattern`, `mirrored`, `thickness`, `draft` |
| Generic property access | `get_object`, `set_property` |
| Mass / assembly / drawings | `mass_properties`, `make_assembly`, `add_part`, `list_assembly_parts`, `interference_check`, `bom_extract`, `make_drawing_page`, `add_projection_group` |
| Visual feedback | `render_view`, `render_views` (8 preset views, multi-view sheets) |
| FEM | `fem_new_analysis`, `fem_set_solver`, `fem_set_material`, `fem_add_constraint` (fixed/force/pressure/displacement/temperature/heatflux/initial_temperature), `fem_mesh`, `fem_mesh_refinement`, `fem_modal`, `fem_buckling`, `fem_run`, `fem_results`, `fem_modal_results`, `fem_buckling_results`, `fem_thermal_results`, plus the legacy `fem_cantilever_demo` |
| Operations | `transaction_open`, `transaction_commit`, `transaction_abort` |

All tools return JSON; geometry-creating tools return a `handle` (e.g.
`pad_1`) that subsequent calls reference.

### Layer 2 — generic property reflection

For the long tail of "I just need to tweak this one property" without a
dedicated tool:

- **`get_object(handle)`** — dump every entry in `obj.PropertiesList` with
  Quantities → float (mm/deg), Vectors → list, Placements → dict.
- **`set_property(handle, name, value)`** — set any single property by name.

Use this when a typed tool exists for the object kind but doesn't expose the
exact property you need (e.g. `Refine` on a Pad, `Sections` ordering on a
Loft, internal tunables on a CCX solver).

### Layer 3 — `run_script` (the universal escape hatch)

For features that have **no first-class MCP tool at all** — e.g. Path
workbench (CAM toolpaths), Surface workbench, Arch/BIM, Spreadsheet,
TechDraw dimensions, contact/spring FEM constraints, B-spline sketcher
operations, expression-engine bindings, anything in a workbench DriftPin
doesn't wrap.

```python
run_script(code='''
import Path
job = Path.Job.Create("Job", [_resolve("pad_1")])
__result__ = {"job_name": job.Name}
''')
```

Inside the script, the worker pre-injects: `App` / `FreeCAD`, `Part`,
`ObjectsFem`, plus `_register(prefix, obj)` / `_resolve(handle)` /
`_handles` so scripts can register new objects into the same handle
registry that typed tools use. Set `__result__ = ...` to a JSON-serializable
value to return data; print statements go to /dev/null.

The escape hatch costs more (the agent has to write FreeCAD Python) but
makes the entire FreeCAD API reachable. The Phase 2 plan's "After Phase 2"
section calls out which run_script patterns deserve promotion to typed
tools — that's how the surface grows over time.

### What the CLI is (and isn't)

The CLI is **not the agent surface** — it's a human-debugging + transport
tool. Seven subcommands:

| Command | Purpose |
|---|---|
| `driftpin ping` / `version` | Health check — boot a worker, prove FreeCAD is reachable |
| `driftpin box` / `cylinder` | Single-shot primitive → .FCStd (manual smoke tests) |
| `driftpin export <in.FCStd> -o <out.step>` | Headless format conversion |
| `driftpin run <script.py>` | Execute arbitrary FreeCAD Python in a live worker (set `__result__` to return JSON) |
| `driftpin mcp` | **Start the MCP server over stdio** — this is how an MCP host launches DriftPin |
| `driftpin fem cantilever` | Run the built-in canned demo |

Agents do not invoke the CLI. They speak MCP via stdio after the host has
launched `driftpin mcp`. The CLI's job is (a) to start that server and
(b) to give a human a way to poke at the worker without writing an MCP
client.

### Decision rule

| Need | Use |
|---|---|
| Standard CAD/FEM operation | First-class MCP tool (Layer 1) |
| Tool exists but I need property X | `get_object` / `set_property` (Layer 2) |
| Workbench / API not wrapped at all | `run_script` (Layer 3) |
| Smoke test from a shell, or stand up MCP | CLI |

## Roadmap

1. **Worker loop** — `freecadcmd` subprocess, newline-delimited JSON RPC, a handful of handlers (`new_document`, `add_primitive`, `save`).
2. **CLI skeleton** — `driftpin run`, `driftpin box`, `driftpin export`. Shakes out the worker protocol end-to-end.
3. **MCP server** — wrap the same handlers as MCP tools. stdio transport.
4. **FEM happy path** — port the cantilever tutorial into a single `fem_run_simple` tool; then decompose into primitives.
5. **Sketches & PartDesign** — the real unlock for mechanical design; add once the basic shape flow is solid.
6. **Result introspection** — return summarized results (max von Mises, max displacement, failing nodes) rather than dumping raw VTK.

## Open questions

- **Error model**: FreeCAD raises plain Python exceptions from C++; worker needs to catch + serialize without losing stack context.
- **Selection references**: FEM constraints use `(object, "Face1")` tuples. Face/edge indices are not stable across edits — we'll probably need a "tag + resolve" layer so an agent can refer to "the face with max +Z normal" instead of `Face2`.
- **Concurrency**: one worker = one active document. Multi-doc workflows need either a pool or explicit document switching.
- **macOS Gatekeeper / sandboxing**: `freecadcmd` launched from a non-interactive context may hit quarantine issues — verify early.

## References

- [FreeCAD Scripting Basics](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FreeCAD_Scripting_Basics.md)
- [Python scripting tutorial](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Python_scripting_tutorial.md)
- [Start up and Configuration](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Start_up_and_Configuration.md)
- [FEM Workbench](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FEM_Workbench.md)
- [FEM Tutorial Python](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FEM_Tutorial_Python.md) — full cantilever example
- [Model Context Protocol](https://modelcontextprotocol.io/)

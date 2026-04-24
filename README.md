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

## Planned CLI surface (first cut)

```
driftpin run <script.py>              # execute arbitrary script against worker
driftpin shell                        # interactive REPL with helpers preloaded
driftpin box --w 10 --d 20 --h 5 -o out.FCStd
driftpin export <in.FCStd> --format step|stl|iges -o out.step
driftpin fem run <doc.FCStd> --solver ccx -o results/
driftpin fem report <results/>        # summarize max stress/displacement
```

## Planned MCP tools (first cut)

| Tool | Purpose |
|---|---|
| `open_document` / `new_document` / `save_document` | Document lifecycle |
| `list_objects` / `get_object` | Introspection, returns ID + TypeId + key props |
| `add_primitive` | Box/Cylinder/Sphere/Cone with dims + placement |
| `boolean_op` | cut / fuse / common over object IDs |
| `make_sketch` / `add_sketch_constraint` / `pad_sketch` | Parametric modeling |
| `export_shape` | STEP / STL / IGES / BREP |
| `fem_new_analysis` | Create analysis container + default solver |
| `fem_set_material` | Assign material to solid by object ID |
| `fem_add_constraint` | Fixed / force / pressure / displacement on face(s) |
| `fem_mesh` | Gmsh or Netgen, with element-size param |
| `fem_run` | Invoke CalculiX (or Elmer) |
| `fem_results` | Max/min stress, displacement, per-node if requested |

All tools return JSON with stable object IDs so an agent can chain calls.

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

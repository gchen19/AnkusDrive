# DriftPin

A CLI + MCP server that drives [FreeCAD](https://www.freecad.org/) through its Python API so LLMs (and humans at a terminal) can design mechanical parts and run FEM simulations without clicking through the GUI.

## Why

FreeCAD exposes almost everything it does through a Python API — create documents, build sketches, extrude solids, mesh them, run CalculiX/Elmer FEM solves, read back stress/displacement fields. But that API lives inside FreeCAD's embedded Python (`freecadcmd`), which is awkward to call from anywhere else. DriftPin wraps it behind two surfaces:

- **CLI** — one-shot commands (`driftpin run script.py`, `driftpin box --w 10 --d 20 --h 5 -o part.FCStd`) for scripts, CI, and quick iteration.
- **MCP server** — ~100 structured tools (`new_document`, `add_primitive`, `boolean_op`, `pad`, `add_gear`, `fem_new_analysis`, `fem_run`, `fem_results`) so an LLM agent can model, inspect, and simulate iteratively.
- **Multi-agent orchestration** — a host-side reference layer that lets a *team* of agents partition one product into components, build them in parallel, and merge the pieces back together with the joints actually fitting (see [Multi-agent design](#multi-agent-design)).

## Target environment

- FreeCAD 1.1.x (default tested path: `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd`; override via `$DRIFTPIN_FREECADCMD` or rely on PATH).
- Bundled Python, `ccx` (CalculiX), and `gmsh` already ship inside the `.app` — no extra install needed for basic FEM.
- Host-side rendering needs `Pillow` and `numpy`; both are installed by DriftPin as regular pip deps.
- **One optional exception:** drawing **PDF/SVG** export (`export_drawing`) renders inside FreeCAD's *bundled* Python, so it needs `reportlab` + `svglib` installed **there** — see [Drawing export (PDF/SVG)](#drawing-export-pdfsvg). DXF export and everything else leave FreeCAD's Python untouched.

## Setup

DriftPin is a `pip`-installable package; FreeCAD itself is the only thing you
install separately. The host-side dependencies (`mcp`, `Pillow`, `numpy`) come
along with the install. `freecadcmd` is launched as a subprocess and uses its
own bundled Python — DriftPin doesn't touch it.

```bash
# 1. Install FreeCAD 1.1.x from https://www.freecad.org/
#    (macOS: drag to /Applications; Linux: distro package or AppImage)

# 2. Install DriftPin. Pick one:
pipx install git+https://github.com/gchen19/DriftPin.git    # isolated app, `driftpin` on PATH
# or for development from a clone:
git clone https://github.com/gchen19/DriftPin.git && cd DriftPin
python3 -m venv .venv && .venv/bin/pip install -e .         # `.venv/bin/driftpin`

# 3. Smoke-test that the worker can reach FreeCAD
driftpin ping
# → ping=pong freecad=1.1.1
```

A PyPI release (`pipx install driftpin`) is staged behind v0.3.0 — see
[`docs/PUBLISHING_PLAN.md`](docs/PUBLISHING_PLAN.md).

### Telling DriftPin where FreeCAD lives

DriftPin auto-discovers `freecadcmd` in this order: `$DRIFTPIN_FREECADCMD`,
then `shutil.which("freecadcmd")` on PATH, then a small list of standard
locations (macOS app bundle, `/usr/bin`, `/usr/local/bin`, snap). For
non-default installs, set:

```bash
export DRIFTPIN_FREECADCMD=/path/to/freecadcmd
```

### Drawing export (PDF/SVG)

`export_drawing` builds 2-D mechanical drawings (multi-view PDF/SVG/DXF with
dimensions) entirely headless. **DXF** uses FreeCAD's own writer and needs
nothing extra. **PDF and SVG** are composed and rasterised with `reportlab` +
`svglib`, and because that runs inside the *worker* — FreeCAD's bundled Python,
not the host venv — the two packages must be installed into **FreeCAD's Python**:

```bash
# Resolve FreeCAD's bundled Python from freecadcmd itself (portable across the
# macOS .app, a Linux distro package, and an extracted AppImage). freecadcmd
# prints a startup banner after the script output, so match a marker line
# rather than taking the last line:
printf 'import sys; print("DPREFIX="+sys.prefix)\n' > /tmp/_fcprefix.py
FREECAD_PREFIX="$(freecadcmd /tmp/_fcprefix.py 2>/dev/null | sed -n 's/^DPREFIX=//p')"
FREECAD_PY="$FREECAD_PREFIX/bin/python"      # some builds: $FREECAD_PREFIX/bin/python3

# Pin svglib<1.6 — newer svglib pulls rlPyCairo -> pycairo, a native build we
# don't use (our drawings are line art, no gradients).
"$FREECAD_PY" -m pip install reportlab "svglib<1.6"

# Verify:
"$FREECAD_PY" -c "import reportlab, svglib; print('drawing export ready')"
```

Without this, `export_drawing` still produces `.dxf`; `.pdf`/`.svg` raise a clear
`ModuleNotFoundError`. FreeCAD already bundles `Pillow` (reportlab needs it), so
no separate install is required.

### Wiring it into an MCP host

The MCP server speaks stdio. Point your host at the `driftpin` binary and
let it run the `mcp` subcommand.

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "driftpin": {
      "command": "driftpin",
      "args": ["mcp"]
    }
  }
}
```

If `driftpin` isn't on the host process's PATH, use an absolute path —
e.g. `/Users/<you>/.local/bin/driftpin` (pipx default) or
`/absolute/path/to/DriftPin/.venv/bin/driftpin` (clone+venv).

**Claude Code** — register once:

```bash
claude mcp add driftpin -- driftpin mcp
```

**Other hosts (Cursor, Continue, custom MCP clients)** — same shape: stdio
transport, command = `driftpin`, args = `["mcp"]`.

After restarting the host, you should see ~100 `driftpin__*` tools become
available. If startup hangs or the host reports a closed connection, run
`driftpin ping` directly — that exercises the same worker boot path with
cleaner error messages.

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

~100 first-class MCP tools cover the **core mechanical-design surface area**.
They have validated parameters, structured returns, and stable handles for
chaining. This is the happy path — what an agent uses for things people do
every day.

| Domain | What's covered |
|---|---|
| Document lifecycle | `new_document`, `open_document`, `save_document`, `list_documents`, `set_active_document`, `close_document`, `restart_worker` |
| Geometry primitives | `add_primitive` (box/cyl/sphere), `boolean_op`, `export_shape` (STEP/IGES/BREP/STL) |
| Selection (stable refs) | `list_faces`, `list_edges`, `query_faces`, `resolve_face`, `resolve_edge`, `register_handle`, `verify_feature` |
| PartDesign | `make_body`, `make_datum_plane`, `make_sketch`, `add_sketch_geometry`, `add_sketch_constraint`, `add_sketch_external`, `close_sketch`, `pad`, `pocket`, `revolve`, `hole`, `loft`, `sweep`, `helix`, `partdesign_fillet`, `partdesign_chamfer`, `linear_pattern`, `polar_pattern`, `mirrored`, `thickness`, `draft` |
| Direct modeling & feature ops | `fillet_edges`, `chamfer_edges`, `shell_solid`, `add_rib`, `engrave_text`, `oring_groove`, `transform`, `scale_shape`, `copy_shape` |
| Parametric components | `add_gear`, `add_rack`, `add_sprocket`, `add_pulley`, `add_spring`, `add_fastener`, `add_bearing`, `add_thread`, `list_thread_options` |
| Metrology & inspection | `measure_distance`, `measure_angle`, `bounding_box`, `check_shape`, `section_view`, `min_clearance`, `envelope_check`, `interference_check` |
| Generic property access | `get_object`, `set_property` |
| Functional intent & invariants | `annotate_face`, `list_face_roles`, `classify_face_sides`, `check_airtight_path`, `declare_intent`, `verify_intent` |
| Assembly & interfaces | `make_assembly`, `add_part`, `list_assembly_parts`, `merge_assembly`, `publish_interface`, `interface_align_check`, `assembly_lock`, `assembly_lock_check`, `bom_extract` |
| Drawings | `make_drawing_page`, `add_projection_group`, `mass_properties` |
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

## Multi-agent design

The roadmap above is about deepening what *one* agent can do. The
[`orchestration/`](orchestration/) layer is about *many* agents sharing the
work: split a product into components and subassemblies, build those in
parallel (each agent cold, seeing only its own contract slice), then merge the
whole back up with the joints actually fitting. The design is written up in
[`docs/MULTI_AGENT.md`](docs/MULTI_AGENT.md); it targets **partition + merge**,
not shared co-editing of one live document (a single worker = one
`App.ActiveDocument`, so concurrent mutation is a non-goal for now).

The split of responsibilities is deliberate:

- **DriftPin ships the thin, tool-agnostic primitives** that make a merge
  verifiable — `publish_interface` (declare a component's mating frames),
  `merge_assembly` (combine component files into one assembly), and the
  **gates** that decide whether a merge is sound: `interface_align_check`
  (do published frames line up?), `interference_check` (do solids collide?),
  `envelope_check` (does it fit its bounding budget?), plus an
  `assembly_lock` / `assembly_lock_check` contract lockfile. These are real
  MCP tools usable by any host.
- **`orchestration/` is the host-side *reference* coordinator** — explicitly
  **not** part of the `driftpin` package. Given a free-text brief it
  `decompose`s it into a validated manifest, fans out one builder agent per
  component, `merge_assembly`s them, reads the gates, and on failure
  **renegotiates** — re-dispatching only the components implicated by the
  failing gate — up to a round budget. It runs against a real Anthropic client
  or a scripted stub (`ScriptedClient`) for free dry runs; the merge and gates
  are real worker calls either way. Any host (Claude, another tool, a human)
  can use it, replace it, or ignore it — the only contract that matters is the
  manifest + the component files on disk.

How well partition+merge holds up is measured by a dedicated eval ladder
(`tests/test_multiagent_m1.py` / `_m2.py`, runnable in CI) with hard-oracle
merge gates and a single-agent baseline — see
[`tests/MULTI_AGENT_EVAL.md`](tests/MULTI_AGENT_EVAL.md). Early experiments have
partition performing at or above the single-agent baseline on the harder toys.

## Status

Phase 3 closed 2026-05-10 (v0.3.0). The core mechanical-design surface from
Phase 2 (2026-04-25) is intact; Phase 3 layered intent-encoding APIs on top
of it. Since then two waves landed: a **command-tier expansion** (21 new MCAD
tools) and the **multi-agent orchestration** layer.

- **Worker + transport** — long-lived `freecadcmd` worker, newline-JSON over stdio with stdio hygiene (FreeCAD C++ chatter redirected off the protocol fd).
- **CLI** — `ping`, `version`, `box`, `cylinder`, `export`, `run`, `mcp`, `fem cantilever`, plus top-level `--version`.
- **MCP server** — FastMCP over stdio, ~100 typed tools across document lifecycle, primitives, selection (face/edge tags), full PartDesign (sketcher + pad/pocket/revolve/hole/loft/sweep/helix/fillet/chamfer/pattern/mirror/thickness/draft), direct-modeling feature ops, parametric components, metrology/inspection, generic property reflection, mass properties, assembly + interface gates, TechDraw, multi-view rendering, FEM (static + modal + buckling + thermal), and transactions.
- **Command tiers 1–3** — 21 new tools: parametric components (`add_gear`, `add_rack`, `add_sprocket`, `add_pulley`, `add_spring`, `add_fastener`, `add_bearing`, `add_thread`), direct feature ops (`fillet_edges`, `chamfer_edges`, `shell_solid`, `add_rib`, `engrave_text`, `oring_groove`, `transform`, `scale_shape`, `copy_shape`), and metrology/inspection (`measure_distance`, `measure_angle`, `bounding_box`, `check_shape`, `section_view`, `min_clearance`).
- **Multi-agent orchestration** — DriftPin ships the thin merge primitives + gates (`publish_interface`, `merge_assembly`, `interface_align_check`, `envelope_check`, `assembly_lock`/`_check`); the host-side reference coordinator (`orchestration/`) decomposes a brief, fans out per-component builders, merges, gates, and renegotiates. See [Multi-agent design](#multi-agent-design) and [`docs/MULTI_AGENT.md`](docs/MULTI_AGENT.md).
- **Phase 3 intent-encoding additions** — `direction='into_body'|'away_from_body'` and `through='wall'|'body'` on pocket/hole (ray-cast wall depth handles hollow shells correctly); `intended_for='print'|'machine'|'drawing'` on hole drives ModelThread; `verify_feature` diffs actual-vs-expected volume change to catch silent failures; visibility hygiene at save hides consumed inputs; `register_handle` + `run_script` auto_register close the escape-hatch one-way trapdoor; `list_thread_options` surfaces the coupled ThreadType/ThreadSize enums dynamically; revolve has an OCCT pre-check that flags axis-coincident edges with an actionable error.
- **Selection layer** — `list_faces` / `list_edges` / `query_faces` / `resolve_*` produce stable geometric tags that survive edits; FEM constraints take tags directly.
- **Rendering** — host-side software rasterizer (`driftpin/render.py`) with per-pixel z-buffer (`render_view` / `render_views` return PNGs as MCP `ImageContent`), plus photoreal `render_photoreal` via the FreeCAD Render addon + an external renderer (POV-Ray / LuxCore / Appleseed). Support matrix, install, and limitations: [`docs/RENDERING.md`](docs/RENDERING.md).
- **Tests** — 153 test functions across worker / MCP / CLI / render / determinism / edit stability / negative paths / perf / multi-agent (M1+M2), runnable via `tests/run_all.sh`. Reliability harness (Layer A classification, B diff-detection, C agent-loop closure) is gated behind `RUN_RELIABILITY=1`; see [`tests/RELIABILITY.md`](tests/RELIABILITY.md).

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the per-slice changelog and the
"After Phase 2" backlog (FEM contact/spring/tie, async `fem_run`,
dimensioned TechDraw, headless PDF/SVG export, `feature_tree` introspection,
external solver integrations starting with the in-house optics pipeline).

## Open questions

- **Error model**: FreeCAD raises plain Python exceptions from C++; worker catches and serializes them, but stack context across the JSON boundary is still lossy.
- **Async / concurrency**: one worker = one active document is no longer a hard limit (multi-doc shipped via `list_documents` / `set_active_document` / `close_document`), but long FEM solves still block the MCP channel — async `fem_run` is open.
- **Headless TechDraw export**: PDF/SVG export lives in `TechDrawGui` and isn't reachable from `freecadcmd`. `export_drawing` raises `NotImplementedError` today; fix path is a separate GUI-launch helper or third-party page renderer.
- **macOS Gatekeeper / sandboxing**: `freecadcmd` launched from a non-interactive context may hit quarantine issues — still worth verifying under MCP-host launch paths.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Contributions
submitted to this project are licensed under the same terms (Apache 2.0
§5: inbound = outbound), which means contributors retain copyright but
grant the project — and everyone downstream — a perpetual, irrevocable
license to use their work, including a patent grant. The intent is to keep
the project welcoming to contributors while ensuring nobody can later
re-proprietize what they contributed.

## References

- [FreeCAD Scripting Basics](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FreeCAD_Scripting_Basics.md)
- [Python scripting tutorial](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Python_scripting_tutorial.md)
- [Start up and Configuration](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Start_up_and_Configuration.md)
- [FEM Workbench](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FEM_Workbench.md)
- [FEM Tutorial Python](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/FEM_Tutorial_Python.md) — full cantilever example
- [Model Context Protocol](https://modelcontextprotocol.io/)

# DriftPin

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="logo/wordmark/driftpin-wordmark-dark-1280.png">
  <img src="logo/wordmark/driftpin-wordmark-1280.png" alt="DriftPin" width="420">
</picture>

A CLI + MCP server that drives [FreeCAD](https://www.freecad.org/) through its Python API so LLMs (and humans at a terminal) can design mechanical parts and run FEM simulations without clicking through the GUI.

## Why

FreeCAD exposes almost everything it does through a Python API — create documents, build sketches, extrude solids, mesh them, run CalculiX/Elmer FEM solves, read back stress/displacement fields. But that API lives inside FreeCAD's embedded Python (`freecadcmd`), which is awkward to call from anywhere else. DriftPin wraps it behind two surfaces:

- **CLI** — one-shot commands (`driftpin run script.py`, `driftpin box --w 10 --d 20 --h 5 -o part.FCStd`) for scripts, CI, and quick iteration.
- **MCP server** — 240+ structured tools (`new_document`, `add_primitive`, `boolean_op`, `pad`, `add_gear`, `fem_new_analysis`, `fem_run`, `fem_results`) so an LLM agent can model, inspect, and simulate iteratively. Beyond core CAD/FEM this now spans a broad **simulation surface** (thermal, CFD/CHT, EM, acoustics, FSI, injection molding, granular/DEM, optics, multibody) and a **design-control layer** (item/part numbers, recipes, variant families, lifecycle/revision, ECO change orders, versioned interfaces).
- **Multi-agent orchestration** — a host-side reference layer that lets a *team* of agents partition one product into components, build them in parallel, and merge the pieces back together with the joints actually fitting (see [Multi-agent design](#multi-agent-design)).

## Target environment

- FreeCAD 1.1.x. The `freecadcmd` binary is auto-discovered per-OS (macOS `.app` bundle, Linux `/usr/bin` etc., **Windows** `C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe` — version-globbed); override via `$DRIFTPIN_FREECADCMD` or rely on PATH. Run `driftpin doctor` to see exactly what resolved.
- Bundled Python, `ccx` (CalculiX), and `gmsh` already ship **inside every FreeCAD install** — the macOS `.app`, the Linux package, and the Windows `bin\` — so core CAD + structural FEM work on all three with no extra install.
- Host-side rendering needs `Pillow` and `numpy`; both are installed by DriftPin as regular pip deps.
- **One optional exception:** drawing **PDF/SVG** export (`export_drawing`) renders inside FreeCAD's *bundled* Python, so it needs `reportlab` + `svglib` installed **there** — see [Drawing export (PDF/SVG)](#drawing-export-pdfsvg). DXF export and everything else leave FreeCAD's Python untouched.

## Setup

DriftPin is a `pip`-installable package; FreeCAD itself is the only thing you
install separately. The host-side dependencies (`mcp`, `Pillow`, `numpy`) come
along with the install. `freecadcmd` is launched as a subprocess and uses its
own bundled Python — DriftPin doesn't touch it.

```bash
# 1. Install FreeCAD 1.1.x from https://www.freecad.org/
#    (macOS: drag to /Applications; Linux: distro package or AppImage;
#     Windows: run the installer — default C:\Program Files\FreeCAD 1.1)

# 2. Install DriftPin. Pick one:
pipx install driftpin                                       # on release — from PyPI, `driftpin` on PATH
# until the first PyPI upload (v0.4.0), install straight from the repo:
pipx install git+https://github.com/gchen19/DriftPin.git    # isolated app, `driftpin` on PATH
# or for development from a clone:
git clone https://github.com/gchen19/DriftPin.git && cd DriftPin
python3 -m venv .venv && .venv/bin/pip install -e .         # `.venv/bin/driftpin`

# 3. Smoke-test that the worker can reach FreeCAD, and see the full setup report
driftpin ping        # → ping=pong freecad=1.1.1
driftpin doctor      # per-item FreeCAD + solver checklist with the exact fix each
```

> **Windows (PowerShell):** the clone path is `py -m venv .venv` then
> `.venv\Scripts\pip install -e .`, and the resulting entry point is
> `.venv\Scripts\driftpin.exe`. FreeCAD's own `freecadcmd.exe` needs nothing on
> PATH — DriftPin globs `C:\Program Files\FreeCAD *\bin\` automatically. Everything
> in step 3 works from a stock FreeCAD 1.1 install (verified: core CAD + a
> CalculiX cantilever solve via the bundled `ccx.exe`).

The `pipx install driftpin` path lights up with the first PyPI release
(v0.4.0), tracked in [`docs/PUBLISHING_PLAN.md`](docs/PUBLISHING_PLAN.md); until
then use the `git+https://…` or clone paths above.

### Telling DriftPin where FreeCAD lives

DriftPin auto-discovers `freecadcmd` in this order: `$DRIFTPIN_FREECADCMD`,
then `shutil.which(...)` on PATH (trying `freecadcmd`, `FreeCADCmd`, and
`freecad.cmd`), then a **per-OS** list of standard install locations:

| OS | Auto-discovered locations (newest version wins) |
|---|---|
| macOS | `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd` |
| Linux | `/usr/bin`, `/usr/local/bin`, `/snap/bin/freecad.cmd`, `~/.local/bin` |
| Windows | `C:\Program Files\FreeCAD *\bin\freecadcmd.exe` (version-globbed), `C:\Program Files (x86)\…`, `%LOCALAPPDATA%\Programs\FreeCAD *\bin\…` |

So a stock installer on any of the three needs **no configuration**. For a
non-default install, point DriftPin at the binary directly:

```bash
export DRIFTPIN_FREECADCMD=/path/to/freecadcmd            # macOS/Linux
```
```powershell
$env:DRIFTPIN_FREECADCMD = "D:\Apps\FreeCAD\bin\freecadcmd.exe"   # Windows
```

`driftpin doctor` prints which of the three layers (env / PATH / auto) actually
resolved FreeCAD, plus every candidate it checked — the fastest way to debug a
"FreeCAD not found" on a new box.

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

After restarting the host, you should see 240+ `driftpin__*` tools become
available. If startup hangs or the host reports a closed connection, run
`driftpin ping` directly — that exercises the same worker boot path with
cleaner error messages.

## Simulation solvers & review-video demos

The base install (FreeCAD + `pip install driftpin`) covers geometry, the analytic
oracles, and the MCP surface. The heavy simulation families each shell out to an
**external solver**, discovered at runtime by [`driftpin/solvers.py`](driftpin/solvers.py)
(`$DRIFTPIN_<SOLVER>_PATH` → `PATH` → standard install dirs). A family whose solver is
absent degrades to a clean `{ok: false, reason, install}` dict instead of crashing — check
what currently resolves with **`driftpin doctor`** (cross-platform, no server boot needed),
the `solve_capabilities` MCP tool, or the install script's `list`. The install script
installs the pip-wheel solvers and provisions the native ones —
`scripts/install-solvers.sh` on Linux/macOS (apt/conda + source builds), and
[`scripts/install-solvers.ps1`](scripts/install-solvers.ps1) on Windows (pip extras +
portable SU2/Elmer/PrusaSlicer downloads; CalculiX auto-detected from FreeCAD's bundle).

**Persistent config:** every `DRIFTPIN_*` path can instead live in
`~/.config/driftpin/config.toml` (`%APPDATA%\driftpin\config.toml` on Windows;
`DRIFTPIN_CONFIG` overrides): `freecadcmd = "..."` at top level, one lowercased key per
solver var under `[solvers]` (`su2_path`, `elmer_path`, `openfoam_bashrc`, ...). Env vars
still win when set; the file is the layer that survives an MCP host's minimal launch
environment. `driftpin doctor` reports the file and which layer resolved each value.

**Platform note:** the solver *discovery* layer is fully cross-platform (per-OS install
dirs, Windows `PATHEXT`/`.exe`, env overrides), so `driftpin doctor` gives an honest report
on macOS/Linux/Windows. The **pip-wheel** families (MBD, topology, optics, fluids) install
identically everywhere. The **native-binary** families differ by OS — CalculiX ships inside
every FreeCAD install; SU2 and PrusaSlicer have good Windows/macOS binaries; Elmer has a
portable Windows zip but no macOS binaries; the
**OpenFOAM-backed** families (CFD, FSI, injection molding) still rely on a Linux shell +
linker glue and are Linux/WSL/Docker for now. See
[`docs/WINDOWS.md`](docs/WINDOWS.md) and [`docs/MACOS.md`](docs/MACOS.md) for the full
per-solver reality and setup on each OS.

The review-video demos under [`scratch/`](scratch/) turn a solver result into a GIF a human
can watch — the **real exported geometry** in motion with the matching oracle overlaid on
the frame (written to `artifacts/`). Each needs its family's solver plus `matplotlib`, and
the CFD one needs `meshio` (on top of the base `numpy`/`Pillow`):

```bash
pip install matplotlib meshio        # frame rendering + reading OpenFOAM's VTK output
```

| Review-video demo (`scratch/…`) | Solver it drives | Install |
|---|---|---|
| `dog_clutch_cad_sim.py` — rigid-body contact via `p.vhacd` | **PyBullet** (pip wheel) | `pip install 'driftpin[mbd]'` |
| `meshing_gears_video.py` — MBD gear train | **PyBullet** (pip wheel) | `pip install 'driftpin[mbd]'` |
| `modal_shape_video.py` — FEM modal shapes | **CalculiX** `ccx` (FreeCAD FEM) | `apt install calculix-ccx` (Linux); FreeCAD finds `ccx` on `PATH` |
| `thermal_field_video.py` — transient thermal field | **Elmer** | `apt install elmerfem-csc`; ensure `ElmerSolver` on `PATH` (or set `DRIFTPIN_ELMER_PATH`) |
| `cfd_field_video.py` — CFD field (lid-driven cavity) | **OpenFOAM** + `meshio` | OpenFOAM via apt/conda, then `source <install>/etc/bashrc` (or set `DRIFTPIN_OPENFOAM_BASHRC`); `pip install meshio` |

All of them also use FreeCAD for the geometry/meshing, so run each with the same
interpreter that launches the worker — e.g. `.venv/bin/python3 scratch/cfd_field_video.py`.

### Optics

Two optics engines sit behind the MCP surface, in two licensing/runtime lanes:

| Lane | Tools | Engine | Install |
|---|---|---|---|
| Sequential — lens design + optimization | `optics_lens_design`, `optics_lens_optimize`, `optics_raytrace` | **optiland** / rayoptics (MIT/BSD, in-process) | `pip install 'driftpin[optics]'` — or `scripts/install-solvers.sh optics` |
| Non-sequential — tracing through STL solids | `optics_solid_trace` | **KrakenOS** (GPL-3.0, **out-of-process only**) | `pip install 'driftpin[optics_gpl]'` — or `scripts/install-solvers.sh optics_gpl` |

The sequential engines import in-process, so install the `optics` extra into the **same
interpreter that launches the worker** (like the other wheels). The non-sequential engine
is GPL-3.0 and is therefore **never imported by DriftPin** — it runs in a separate
subprocess ([`driftpin/optics_gpl_runner.py`](driftpin/optics_gpl_runner.py)), the same
arm's-length boundary used for the GPL Elmer/OpenFOAM binaries. The worker locates a
Python that can import KrakenOS automatically (from where the wheel is installed); override
with `DRIFTPIN_OPTICS_GPL_PYTHON=/path/to/python`. Because of that isolation the GPL extra
is **opt-in**: the no-argument `install-solvers.sh` run installs only the permissive
extras and prints how to add `optics_gpl`. Rendered examples for both lanes (lens layout,
spot diagram, optimization, prism TIR, and a ball-lens spherical-aberration study) live in
[`examples/optics_gallery/`](examples/optics_gallery/) — regenerate with
`.venv/bin/python examples/optics_gallery.py` (and `…_3d.py`, `optics_ball_lens.py`), or
bootstrap everything in one shot (installs both lanes, then renders every figure):

```bash
scripts/install-solvers.sh --optics-gallery
```

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

240+ first-class MCP tools span the **core mechanical-design surface**, a broad
**engineering-analysis / simulation surface**, and a **design-control (PLM)
layer**. They have validated parameters, structured returns, and stable handles
for chaining. This is the happy path — what an agent uses for things people do
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
| Drawings (TechDraw, headless) | `make_drawing_page`, `add_projection_group`, `add_section_view`, `add_thumbnail`, `add_dimension`, `add_annotation`, `add_feature_note`, `set_title_block`, `fit_page`, `export_drawing` (PDF/SVG/DXF), plus completeness/legibility gates `drawing_gate`, `drawing_legibility` |
| Visual feedback | `render_view`, `render_views` (8 preset views, multi-view sheets), `render_photoreal` / `render_photoreal_submit`, `render_capabilities` |
| FEM (FreeCAD/CalculiX/Elmer) | `fem_new_analysis`, `fem_set_solver`, `fem_set_material`, `fem_set_nonlinear_material`, `fem_add_constraint` (fixed/force/pressure/displacement/temperature/heatflux/initial_temperature), `contact_setup`, `fem_mesh`, `fem_mesh_refinement`, `fem_modal`, `fem_buckling`, `fem_run`, `fem_results`, `fem_result_probe` (stress/disp/temp at a point or face), `fem_modal_results`, `fem_buckling_results`, `fem_thermal_results`, plus the legacy `fem_cantilever_demo` |
| Engineering oracles & hand-calcs | machine elements (`gear_rating`, `bearing_life`, `belt_drive`, `spring_check`, `bolted_joint_check`, `press_fit_stress`, `seal_check`), structural (`beam_modal`, `beam_buckling`, `plate_check`, `hertz_contact`, `elastica_deflection`, `plastic_collapse`, `random_vibration`, `harmonic_response`), durability (`fatigue_check`, `fracture_check`, `creep_flag`, `wear_estimate`), thermal (`thermal_lumped`, `thermal_transient_1d`, `thermal_composite_wall`, `h_estimate`), tolerance/GD&T (`tolerance_stackup`, `fit_check`, `fit_class`, `gdt_check`) |
| Simulation families (external solvers, async) | screens + full solves that shell out to OpenFOAM/Elmer/CalculiX/openEMS/YADE/KrakenOS, most via a submit→poll job pattern: thermal/CHT (`cht_channel_submit`, `cht_graetz_submit`, `thermal_transient_submit`, `thermal_radiation_submit`), CFD (`cfd_pipe_flow`, `cfd_body_drag`, `cfd_internal_flow_submit`, `cfd_external_flow_submit` — including the virtual wind tunnel: hand it a solid and get Cd/Cl/Cm from an integrated force, gated against the sphere drag curve), EM (`em_skin_depth`, `em_dc_resistance`, `em_field`, `em_conduction_submit`, `em_induction_submit`, `em_fullwave_submit`), acoustics (`acoustic_screen`, `acoustic_fem_submit`, `acoustic_radiation_submit`), FSI (`fsi_*`), molding (`molding_screen`, `molding_fill_submit`, `molding_warpage_submit`), granular/DEM (`granular_screen`, `dem_pack_submit`, `dem_flow_submit`), optics (`optics_lens_design`, `optics_lens_optimize`, `optics_raytrace`, `optics_solid_trace`), multibody (`mechanism_kinematics`, `mechanism_simulate_submit`), topology (`topology_optimize_submit`, `topology_to_solid`) |
| Async jobs | `job_status`, `job_result`, `job_list` — poll/collect any `*_submit` long-running solve; `solve_capabilities` reports which solvers currently resolve |
| Materials & fluids | `material_list`, `material_get`, `material_select`, `fluid_props` — mechanical-property / molding / CoolProp thermophysical corpora behind a typed lookup |
| Manufacturing & Design-for-X | `dfm_check`, `dfa_check`, `moldability_check`, `optics_moldability_check`, `pack_check`, `cost_estimate`, `slice_estimate`, `slice_gcode_submit`, `laminate_properties`, `drop_impact` |
| Design control / PLM | items & part numbers (`items_new`, `items_validate`, `items_resolve`, `items_check_manifest`), recipes (`recipe`, `recipe_list`, `recipe_schema`, `recipe_validate`), feature templates (`feature_instantiate`, `feature_list`, `feature_schema`, `feature_validate`), variant families (`family_materialize`, `family_validate`), lifecycle/revision (`lifecycle_transition`, `lifecycle_editable`, `lifecycle_classify_change`, `lifecycle_apply_change`), change control (`eco_create`, `eco_validate`, `change_impact`, `where_used`, `baseline_create`, `baseline_verify`), interface registry + substitutability (`get_interface`, `substitutability_check`), projects (`scaffold_project`, `project_validate`, `project_check_references`, `project_resolve_manifest`) |
| Operations | `transaction_open`, `transaction_commit`, `transaction_abort` |

All tools return JSON; geometry-creating tools return a `handle` (e.g.
`pad_1`) that subsequent calls reference. The heavy simulation families return
a `{ok: false, reason, install}` dict (rather than crashing) when their solver
isn't installed — see [Simulation solvers](#simulation-solvers--review-video-demos).

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

**Concurrent agents on one MCP server — workspaces.** FastMCP runs sync tools in
a thread pool, so a host can have several tool calls in flight at once. The
server keeps a *pool* of named **workspaces**, each its own freecadcmd process
with its own `App.ActiveDocument` and handle registry. Each concurrent agent
claims its own workspace with `use_workspace(name)` at the start of its session;
**handles and documents do not cross workspaces**. A client that never calls
`use_workspace` sees the historical single-worker behavior byte-for-byte
(everything routes to the `default` workspace). `Worker.call()` is internally
serialized so two threads can never interleave the stdin/stdout protocol on one
process. The pool is capped (`DRIFTPIN_MAX_WORKSPACES`, default 4) and idle
workspaces are reaped (`DRIFTPIN_WORKSPACE_IDLE_S`, default 900s) so abandoned
sessions don't leak processes; `list_workspaces` / `close_workspace` manage it.

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

## Designs, not just parts — the design-control layer

Multi-agent orchestration partitions *one* product across a team. A separate
axis makes a *design* (not just a part) something you can parameterize, vary,
and evolve under control — the mechanisms a PLM/PDM workflow expects, mapped
onto DriftPin's deterministic, headless, git-diffable grain. The keystone
insight: **the build recipe is the feature tree; the parameters are its inputs;
regeneration is re-running the recipe** — so DriftPin gets parametric regen and
family tables without a live in-file expression engine. The full scoping and
rationale is in [`docs/DESIGN_HIERARCHY.md`](docs/DESIGN_HIERARCHY.md); the
agent-facing judgment lives in the [`design-modularly`](skills/design-modularly)
skill.

- **Parametric hierarchy** — *recipes* (`recipe`, `recipe_validate`) are named,
  declared-input build templates (DriftPin's PowerCopy/UDF *and* its intra-part
  parametric model); a *relations* DAG drives driven dimensions from master
  parameters by formula (`pitch_d = module * teeth`, arithmetic only — no
  iterative solve, no double-driving); *feature templates* (`feature_instantiate`)
  graft reusable features onto reference geometry by name; the typed
  [`units`](driftpin/units.py) layer rejects dimensionally-wrong inputs at the
  door (`"5 N"` for a length is an error, not a silent mis-scale).
- **Variant families** — `family_materialize` expands a row × column design
  table into a set of variants deterministically, running the recipe per row and
  allocating part numbers in table order.
- **Identity & lifecycle** — *items* (`items_new`) give a part a stable
  part-number identity decoupled from its file path; a *lifecycle* state machine
  (`lifecycle_transition`: in_work → in_review → released → obsolete) enforces
  released-immutability, and a Form/Fit/Function predicate decides revision bump
  vs. new part number on a change.
- **Change control** — *ECOs* (`eco_create`) are first-class change records;
  `where_used` / `change_impact` compute blast radius over the dependency graph
  before you commit; `baseline_create` / `baseline_verify` pin reproducible
  snapshots.
- **Versioned interfaces** — an interface-type registry (`get_interface`,
  `nema17_face@1`-style named/versioned types) plus a Liskov
  `substitutability_check` gate enforce Form/Fit/Function compatibility as code,
  so a swapped part is verified to actually mate.
- **Projects** — `scaffold_project` + `project_validate` /
  `project_check_references` promote the multi-agent directory convention to a
  first-class `project.json` (manifest-of-manifests) with a master/skeleton
  single-source-of-truth slot and reference-integrity guards.

Like the merge gates, these are thin, deterministic, mostly FreeCAD-free
primitives — the logic layers import and test without launching a worker.

## Status

Phase 3 closed 2026-05-10 (v0.3.0). The core mechanical-design surface from
Phase 2 (2026-04-25) is intact; Phase 3 layered intent-encoding APIs on top of
it. Since then the tool surface has grown from ~100 to **240+ tools** across
several waves: a command-tier expansion (parametric components + direct feature
ops + metrology), the **multi-agent orchestration** layer, a broad
**engineering-analysis + external-solver simulation surface** (thermal/CFD/CHT/
EM/acoustics/FSI/molding/granular/optics/multibody), and a **design-control
(PLM) layer** (items, recipes, variant families, lifecycle, ECO/change,
versioned interfaces, projects).

- **Worker + transport** — long-lived `freecadcmd` worker, newline-JSON over stdio with stdio hygiene (FreeCAD C++ chatter redirected off the protocol fd).
- **CLI** — `ping`, `version`, `box`, `cylinder`, `export`, `run`, `mcp`, `fem cantilever`, plus top-level `--version`.
- **MCP server** — FastMCP over stdio, 240+ typed tools across document lifecycle, primitives, selection (face/edge tags), full PartDesign (sketcher + pad/pocket/revolve/hole/loft/sweep/helix/fillet/chamfer/pattern/mirror/thickness/draft), direct-modeling feature ops, parametric components, metrology/inspection, generic property reflection, mass properties, assembly + interface gates, TechDraw (incl. headless PDF/SVG/DXF export, dimensions, gates), multi-view + photoreal rendering, FEM (static + modal + buckling + thermal + nonlinear + result-probe), the engineering-analysis oracles and external-solver simulation families (sync + async `*_submit`/`job_*`), the materials/fluids corpora, Design-for-X / manufacturing checks, the design-control (PLM) layer, and transactions.
- **Command tiers 1–3** — 21 new tools: parametric components (`add_gear`, `add_rack`, `add_sprocket`, `add_pulley`, `add_spring`, `add_fastener`, `add_bearing`, `add_thread`), direct feature ops (`fillet_edges`, `chamfer_edges`, `shell_solid`, `add_rib`, `engrave_text`, `oring_groove`, `transform`, `scale_shape`, `copy_shape`), and metrology/inspection (`measure_distance`, `measure_angle`, `bounding_box`, `check_shape`, `section_view`, `min_clearance`).
- **Multi-agent orchestration** — DriftPin ships the thin merge primitives + gates (`publish_interface`, `merge_assembly`, `interface_align_check`, `envelope_check`, `assembly_lock`/`_check`); the host-side reference coordinator (`orchestration/`) decomposes a brief, fans out per-component builders, merges, gates, and renegotiates. See [Multi-agent design](#multi-agent-design) and [`docs/MULTI_AGENT.md`](docs/MULTI_AGENT.md).
- **Phase 3 intent-encoding additions** — `direction='into_body'|'away_from_body'` and `through='wall'|'body'` on pocket/hole (ray-cast wall depth handles hollow shells correctly); `intended_for='print'|'machine'|'drawing'` on hole drives ModelThread; `verify_feature` diffs actual-vs-expected volume change to catch silent failures; visibility hygiene at save hides consumed inputs; `register_handle` + `run_script` auto_register close the escape-hatch one-way trapdoor; `list_thread_options` surfaces the coupled ThreadType/ThreadSize enums dynamically; revolve has an OCCT pre-check that flags axis-coincident edges with an actionable error.
- **Selection layer** — `list_faces` / `list_edges` / `query_faces` / `resolve_*` produce stable geometric tags that survive edits; FEM constraints take tags directly.
- **Rendering** — host-side software rasterizer (`driftpin/render.py`) with per-pixel z-buffer (`render_view` / `render_views` return PNGs as MCP `ImageContent`), plus photoreal `render_photoreal` via the FreeCAD Render addon + an external renderer (POV-Ray / LuxCore / Appleseed / Cycles / OSPRay / PBRT). Support matrix, install, and limitations: [`docs/RENDERING.md`](docs/RENDERING.md).
- **Simulation surface** — engineering-analysis oracles (machine elements, structural, durability, thermal, tolerance/GD&T) plus external-solver families that shell out to OpenFOAM / Elmer / CalculiX / openEMS / YADE / KrakenOS, discovered at runtime by [`driftpin/solvers.py`](driftpin/solvers.py) and degrading cleanly when absent. Long solves use an async submit→poll job pattern (`*_submit` + `job_status`/`job_result`/`job_list`). Catalog and result schemas: [`docs/SIMULATION_TOOLS.md`](docs/SIMULATION_TOOLS.md); proof harness: [`docs/SIMULATION_EXAMPLES.md`](docs/SIMULATION_EXAMPLES.md). Materials/fluids back these via `material_*` and `fluid_props` (mechanical-property / molding / CoolProp corpora).
- **Design-control (PLM) layer** — recipes + a relations DAG (parametric regen), feature templates, variant families from a design table, item/part-number identity, a lifecycle/revision state machine, ECO change records with where-used/impact + baselines, a versioned interface registry + Liskov substitutability gate, and project containers with reference-integrity guards. Scoping + rationale: [`docs/DESIGN_HIERARCHY.md`](docs/DESIGN_HIERARCHY.md). See [Designs, not just parts](#designs-not-just-parts--the-design-control-layer).
- **Tests** — ~980 test functions across ~90 files (worker / MCP / CLI / render / determinism / edit stability / negative paths / perf / multi-agent / simulation families / molding / PLM layer), runnable via `tests/run_all.sh` (Linux/macOS) or `tests/run_all.ps1` (Windows — single-interpreter, skips the Linux-only solver families; see [`docs/WINDOWS.md`](docs/WINDOWS.md)). Reliability harness (Layer A classification, B diff-detection, C agent-loop closure) is gated behind `RUN_RELIABILITY=1`; see [`tests/RELIABILITY.md`](tests/RELIABILITY.md).

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the per-slice changelog and remaining
backlog (FEM contact/spring/tie refinements, fully async `fem_run`,
`feature_tree` introspection, deeper external-solver integrations).

## Open questions

- **Error model**: FreeCAD raises plain Python exceptions from C++; worker catches and serializes them, but stack context across the JSON boundary is still lossy.
- **Async / concurrency**: multi-doc shipped (`list_documents` / `set_active_document` / `close_document`), and the long-running external solvers run off the channel via the `*_submit` + `job_*` pattern, but the in-worker `fem_run` itself is still synchronous and blocks the MCP channel for the duration of a CalculiX/Elmer solve.
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

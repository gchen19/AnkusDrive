# DriftPin roadmap

Living plan for getting DriftPin from "CSG primitives + one FEM demo" to a substrate
an LLM agent can use to drive end-to-end mechanical design: parametric geometry,
robust selection, simulation, assembly, and manufacturing outputs.

Companion to the README. The README describes what *is*; this file describes what
*should be next* and why.

**Related docs:**
- [`docs/PHASE_2_PLAN.md`](PHASE_2_PLAN.md) — **next session pickup point.**
  Self-contained plan for completing the remaining ~20% of core mechanical-
  design surface area (PartDesign holes / patterns / loft / sweep / draft /
  thickness; FEM modal / buckling / thermal; multi-doc; transactions).
- [`tests/TEST_PLAN.md`](../tests/TEST_PLAN.md) — cross-slice test strategy,
  what's covered today and what's left, in priority order.
- [`tests/RELIABILITY.md`](../tests/RELIABILITY.md) — Layer A/B/C reliability
  harness ("can the agent see what it built?").
- [`docs/MULTI_AGENT.md`](MULTI_AGENT.md) — RFC for agent *teams*: partition a
  design into components/subassemblies, build in parallel, merge + verify the
  whole. Thin tool-agnostic primitives; orchestration left to the host.

---

## Current state (as of 2026-04-25)

Working today:
- Long-lived `freecadcmd` worker, newline-JSON over stdio, with stdio hygiene
  (fd1→fd2 redirect, preserved response fd) so FreeCAD's C++ chatter doesn't
  corrupt the protocol channel.
- Document lifecycle: `new_document`, `open_document`, `save_document` (with
  bbox-fitted camera patch so headless saves open visually in the GUI),
  `list_objects`.
- Geometry: `add_primitive` (box/cyl/sphere), `boolean_op`, `export_shape`
  (STEP/IGES/BREP/STL).
- FEM happy path: monolithic `fem_cantilever_demo` (geometry → Gmsh →
  CalculiX → von Mises read-back).
- `run_script` escape hatch with `App` / `Part` / `ObjectsFem` / handle-registry
  helpers in scope.
- FastMCP wrapper exposing the above as MCP tools.
- CLI: `ping`, `version`, `box`, `cylinder`, `export`, `run`, `mcp`,
  `fem cantilever`.
- Tests for cli / mcp / worker.

Known gaps (also see "Open questions" in README):
- No parametric modeling — only CSG primitives.
- Face/edge references are positional indices (`"Face2"`); not stable across
  edits.
- FEM is one monolithic demo, not composable tools.
- No way for the agent to *see* the model — it works blind off IDs and volumes.
- No assembly, no drawings, no manufacturing outputs.
- Single document, single worker; long FEM runs block the channel.

---

## Priority order

The sequencing rationale: face tagging is the foundation because it stops
everything downstream from breaking under edits. PartDesign comes next because
it's the actual design substrate. FEM decomposition rides on top of tagging.
Visual feedback closes the agent's perception loop. Assembly + drawings come
last because they presuppose the rest.

1. Face/edge tagging layer
2. PartDesign / Sketcher
3. FEM decomposition
4. Visual feedback (agent eyes)
5. Assembly + TechDraw + manufacturing outputs

---

## Slice 1 — face/edge tagging layer

**Goal:** an agent can refer to faces/edges by stable, semantic queries instead
of `Face2`, and references survive geometry edits.

**Why first:** `(box, "Face2")` indices are not stable across edits. The moment
the agent edits geometry, every FEM constraint and feature reference breaks.
README flags this in "Open questions". Without it, no feature built on top is
robust.

### Worker additions (`worker.py`)

- `list_faces(handle)` →
  `[{tag, area, type, normal, centroid, u_range, v_range, is_planar, is_cylindrical, radius?}, ...]`
  - Iterate `obj.Shape.Faces`; classify via `face.Surface`
    (`Plane`, `Cylinder`, `Cone`, `Sphere`, `Torus`, `BSplineSurface`).
  - Planar: `normal = surf.Axis`, area, centroid.
  - Cylindrical: axis, radius, finite/infinite.
  - `tag` is a stable hash of (type, rounded normal, rounded centroid,
    rounded area). Survives reordering.
- `list_edges(handle)` — same idea, with `length`, `type`
  (line/circle/bspline), endpoints, midpoint, axis for circles.
- `query_faces(handle, predicate)` — predicate is structured:
  - `{type: "planar", normal_dir: [0,0,1], tol: 1e-3}`
  - `{type: "cylindrical", radius_eq: 5.0}`
  - `{centroid_max: "z"}` / `{centroid_min: "x"}`
  - `{area_min: 100, area_max: 200}`
  Returns ordered list of tags.
- `resolve_face(handle, tag)` → returns the current `FaceN` index by re-running
  the tag's geometric signature against the live shape.
- `resolve_face_ref(handle, tag)` → `(obj, "FaceN")` tuple ready for FEM
  constraint `References`.

### Stability strategy

Tags are *not* persisted indices — they're recomputed each call from geometric
properties. A face matches if its (type, normal±tol, centroid±tol, area±rel_tol)
all match. If multiple match, return ambiguity error so the agent can refine
the query.

### MCP tools

`list_faces`, `list_edges`, `query_faces` (return tags + descriptors).
Existing constraint/boolean tools accept tags transparently.

### Tests

- Create a box, tag the +Z face, fillet an unrelated edge, re-resolve the tag
  — must still resolve to the +Z face.
- Query "max +Z normal" on a stepped block, get the right face.
- Ambiguity case: two coplanar faces of equal area → error, not silent pick.

### Effort

~2 days (1 day listing + tag generation, 1 day query/resolve + stability test).

---

## Slice 2 — PartDesign / Sketcher

**Goal:** the agent can build real parametric parts, not just CSG.

**Depends on:** Slice 1 (so sketches can be placed on tagged faces, fillets can
target tagged edges).

### Worker additions

- `make_body(name)` — `PartDesign::Body` container; subsequent features go into
  the active body.
- Datums: `make_datum_plane(body, base="XY"|face_tag, offset=0, rotation=...)`,
  `make_datum_axis`, `make_datum_point`.
- `make_sketch(body, plane_ref)` → returns sketch handle.
- `add_sketch_geometry(sketch, [{type: "line"|"circle"|"arc"|"point", ...}])` →
  returns geom indices.
- `add_sketch_constraint(sketch, type, refs, value=None)` — types:
  `Coincident`, `Horizontal`, `Vertical`, `Distance`, `DistanceX`, `DistanceY`,
  `Radius`, `Diameter`, `Angle`, `Equal`, `Symmetric`, `Parallel`,
  `Perpendicular`, `Tangent`, `Block`. `refs` are `(geom_idx, vertex_role)`
  tuples.
- `close_sketch(sketch)` — recompute, return DOF status
  (`fully_constrained: true/false`).
- Features:
  - `pad(sketch, length, symmetric=False, reversed=False)`
  - `pocket(sketch, length|through_all)`
  - `revolve(sketch, axis_ref, angle=360)`
  - `hole(sketch, diameter, depth, type="simple"|"counterbore"|"countersink", thread=None)`
  - `fillet(body, edge_tags, radius)`
  - `chamfer(body, edge_tags, size)`
  - `linear_pattern(feature, direction_ref, count, length)`
  - `polar_pattern(feature, axis_ref, count, angle)`
  - `mirror(feature, plane_ref)`
- `set_property(handle, name, value)` / `get_object(handle)` — generic; needed
  for the inevitable property the wrapper missed.

### MCP tools

Mirror the worker handlers 1:1.

### Tests

Sketch a circle on XY → pad → fillet top edge → pocket a slot through it.
Assert face count, volume, fully-constrained sketch.

### Effort

~5 days, decomposed into PRs per feature group:
- Body + datums + sketch + constraints (2–3 days)
- Pad/Pocket/Revolve/Hole (1–2 days)
- Fillet/Chamfer/Pattern/Mirror (1 day)
- `set_property` / `get_object` (0.5 day)

### Status (2026-04-25)

**Shipped:**
- `make_body`, `make_datum_plane` (origin-plane name *or* face-tag attachment + offset)
- `make_sketch`, `add_sketch_geometry` (line/circle/arc/point + construction flag)
- `add_sketch_constraint` (15 types: Coincident, Horizontal, Vertical, Distance,
  DistanceX, DistanceY, Radius, Diameter, Equal, Parallel, Perpendicular,
  Tangent, Block, Symmetric, Angle)
- `close_sketch` — reports DoF, conflicts, redundant, malformed via
  `sketch.DoF` / `sketch.FullyConstrained` (the legacy `OpenVertices` check is
  unreliable — it's a list of points, not a DOF count)
- `pad`, `pocket` (with `through_all`), `revolve`
- `partdesign_fillet`, `partdesign_chamfer` — both accept face/edge tags
- `get_object` / `set_property` — generic property reflection

**Phase 2 Slice 2.5 shipped (2026-04-25):**
- `hole` — full PartDesign::Hole wrapper with Dimension/ThroughAll depth,
  Counterbore/Countersink/Counterdrill cut types, optional thread tap.
- `linear_pattern`, `polar_pattern`, `mirrored` — accept body axis 'X'|'Y'|'Z'
  or edge-tag refs (and face-tag for mirror plane). Tag-survival regression
  test (`test_tag_survives_linear_pattern`) confirms tags on the original
  feature still resolve after a pattern is applied.
- `add_sketch_external` — projects an external edge/face/vertex into a sketch
  as construction geometry, returns the negative external index for use in
  subsequent constraints. Enables parametric chains where a sketch stays
  anchored to upstream geometry.

**Phase 2 Slice 2.6 shipped (2026-04-25):**
- `loft` — PartDesign::AdditiveLoft between 2+ sketches.
- `sweep` — PartDesign::AdditivePipe of profile along spine.
- `helix` — Part::Helix curve (combine with sweep to make threads).
- `thickness` — PartDesign::Thickness to hollow into a shell with N open
  faces. Default Reversed=True (inward shell — the natural use case).
- `draft` — PartDesign::Draft with neutral plane and angle.

---

## Slice 3 — FEM decomposition

**Goal:** replace the monolithic `fem_cantilever_demo` with composable tools an
agent can chain.

**Depends on:** Slice 1 (constraints reference tags, not `Face1`).

### Tools

- `fem_new_analysis(name)`
- `fem_set_solver(analysis, kind="ccx"|"elmer", **tunables)` — exposes
  `GeometricalNonlinearity`, `ThermoMechSteadyState`, etc.
- `fem_set_material(analysis, body_handle, material_name)` — link to FreeCAD's
  materials library, not inline strings.
- `fem_add_constraint(analysis, kind, refs, **params)` — kinds:
  `fixed`, `force`, `pressure`, `displacement`, `contact`, `tie`, `spring`,
  `thermal`. `refs` are face/edge tags from Slice 1.
- `fem_mesh(handle, mesher="gmsh"|"netgen", char_length, refinements=[])` —
  `refinements` allow local refinement on tagged faces.
- `fem_run(analysis, async=False)` — returns when solver finishes; streaming
  progress + async are stretch goals (see Slice 6).
- `fem_results(analysis)` — max von Mises + location, top-N hot nodes, max
  displacement vector, per-object summaries. Not raw VTK.
- `fem_modal(analysis, n_modes)` — natural frequencies + mode shapes.
- `fem_thermal(analysis, ...)` — steady-state heat (CCX supports it).

### Migration

Re-implement `fem_cantilever_demo` on top of these primitives as an integration
test. Old monolithic version stays for backwards compat until callers move.

### Effort

~3 days. Most of the FreeCAD plumbing already exists in
`worker.py::_h_fem_cantilever`; the work is decomposing it cleanly.

### Status (2026-04-25)

**Shipped:**
- `fem_new_analysis`, `fem_set_solver` (CCX/Elmer with arbitrary tunables;
  CCX gets sensible defaults), `fem_set_material` (inline material spec),
  `fem_add_constraint` (fixed/force/pressure/displacement, refs by face/edge
  tag), `fem_mesh` (Gmsh with characteristic length), `fem_run` (CCX),
  `fem_results` (max von Mises + location, max displacement vector, top-N
  hot nodes).
- Golden-path test re-implements the cantilever via decomposed tools and
  asserts results match the monolithic baseline within 8% disp / 15% stress
  (Gmsh nondeterminism alone is ~5% on this geometry; tighter would be
  flaky). Refs are by tag — proves Slice 1 carries through into FEM.

**Phase 2 Slice 3.5 shipped (2026-04-25):**
- `fem_modal` + `fem_modal_results` — natural-frequency extraction.
  Sets solver AnalysisType='frequency', EigenmodesCount=N. Returns sorted
  frequency list and per-mode info.
- `fem_buckling` + `fem_buckling_results` — linear buckling. Apply unit force,
  result factors are the load multipliers. Wiki-drift note: CCX encodes the
  buckling factor in the result *object name*, not in EigenmodeFrequency —
  the worker parses it from the name.
- `fem_add_constraint` extended with thermal kinds: 'temperature',
  'heatflux' (DFlux/Convection/Radiation), 'initial_temperature'.
- `fem_thermal_results` — pulls min/max/mean temperature plus top-N hot nodes.
- `fem_mesh_refinement` — `ObjectsFem.makeMeshRegion` (NOT `makeMeshGmshRegion`);
  refines the parent mesh on tagged faces with a per-region CharacteristicLength.

**Deferred (TODO):**
- Constraint kinds beyond static + thermal: contact, tie, spring, bearing,
  transform.
- Streaming/async `fem_run` so long solves don't block the MCP channel.
- Result interpolation: "stress at point (x, y, z)" or "stress on this face
  tag" — currently we only return globals + top-N. This is the agent-friendly
  form for design iteration.

---

## Slice 4 — visual feedback (agent eyes)

**Goal:** the agent can *see* the model. Right now it gets back IDs and volumes
but can't catch geometric mistakes a human would spot in 0.5s.

**Why this matters:** any agent loop without perception eventually produces a
plausible-but-wrong part. A render closes the loop.

### Constraint

`freecadcmd` has no `FreeCADGui`. Options:

1. **Tessellate + offscreen render in host** (recommended). Worker tessellates
   the shape and ships triangles back; the host renders via `pyrender` /
   `trimesh` / `matplotlib` and returns PNG bytes as MCP image content. No
   FreeCAD GUI needed.
2. Export STL, render in host via independent pipeline. Same result, more
   roundtrip.
3. Launch full FreeCAD with virtual display. Works on Linux (`xvfb`); harder
   on macOS. Avoid unless we need GUI-only features.

### Tools

- `render_view(handle=None, view="iso"|"top"|"front"|"side"|custom_camera, width=512, height=512)`
  → PNG bytes, returned as MCP `ImageContent`.
- `render_assembly(views=["iso", "top", "front"])` → multi-view sheet.
- `render_fem_results(analysis, field="vonMises"|"displacement", scale=auto)`
  → colored mesh render.

### Effort

~2 days for the basic isometric render. Multi-view + FEM colormap is +1 day.

### Status (2026-04-25)

**Shipped:**
- Worker `tessellate(handle, deflection)` returning `{vertices, triangles, bbox}`.
- Host renderer (`driftpin/render.py`): NumPy + Pillow software rasterizer.
  Orthographic projection, auto-fit camera, painter's-algorithm depth sort
  via per-triangle mean z, Lambertian flat shading from a fixed light, edge
  lines on by default. Views: iso/top/bottom/front/back/left/right/side.
  Adds `Pillow` and `numpy` as host-side deps (FreeCAD's bundled Python is
  untouched).
- MCP tools `render_view` (single view → base64 PNG) and `render_views`
  (multi-view sheet).
- 7 toy-problem tests in `tests/test_render.py`, all passing in ~1.2s:
  valid PNG, non-blank, bbox centered, bigger-box-bigger-silhouette,
  sphere-silhouette-is-disc (theoretical std/mean = 0.354 — falsifiable
  shape-sensitivity test), view-dir-changes-aspect-ratio, multi-view-distinct.

**Painter's algorithm replaced with per-pixel z-buffer.** Box-with-hole now
renders correctly: the hole's interior shows background (not back-wall
triangles bleeding through). Verified by `test_concave_hole_is_empty_top_view`,
which would have failed under the original painter's-algorithm sort.

**Reliability harness scaffolded** (Layer A only).
`tests/test_reliability.py` + `tests/reliability_shapes.py` render 10
known shapes, send each to Claude, and grade the response by keyword match.
Gated behind `RUN_RELIABILITY=1` because it costs API credits. Full
documentation in **`tests/RELIABILITY.md`** — read that before running. Not
exercised yet (waiting on Anthropic API key); shape library + grader specs
are in place. Layers B (diff detection) and C (agent-loop closure with
geometric ground truth) are designed but unimplemented — see RELIABILITY.md
for sequencing.

**Known limitations (TODO):**
- No anti-aliasing. Silhouettes are hard-edged. Cheap fix: render at 2× then
  downsample with bilinear in PIL.
- Edge lines include all triangulation diagonals (every triangle is outlined),
  not just feature/silhouette edges. Cleanup: only draw an edge between
  adjacent triangles whose face normals differ by > some angle threshold.
- `render_fem_results` (colormap of stress field on the deformed mesh) not
  yet implemented. Path: pull `result.NodeNumbers`, `result.vonMises`,
  `result.DisplacementVectors` from the worker; build a per-vertex color
  from a `viridis`-like colormap; rasterize using barycentric color interp.
- No way for the agent to specify a custom camera (yaw/pitch/orbit). The 8
  preset views are sufficient for now; a 9th `custom_camera` arg taking
  (azimuth_deg, elevation_deg) would be cheap to add.

---

## Slice 5 — assembly + drawings + manufacturing outputs

**Goal:** the agent can produce buildable artifacts, not just parts.

### Assembly

- Pick `Assembly4` or `App::Link` directly. Assembly4 has more abstractions
  but adds a workbench dependency; `App::Link` is core.
- Tools: `make_assembly`, `add_part(assembly, doc_path, placement)`,
  `add_constraint(kind="planar"|"axial"|"distance", refs)`,
  `interference_check`, `bom_extract` (counts, masses, materials).

### TechDraw

- `make_drawing_page(template)`, `add_projection_group(page, part, scale, view_dirs)`,
  `add_dimension(page, kind, refs)`, `add_annotation(page, text, position)`,
  `export_drawing(page, format="pdf"|"svg")`.

### Manufacturing instructions

Mostly an LLM task. Expose what the model needs:
- `bounding_box(handle)`, `mass_properties(handle)` (mass, volume, surface
  area, CG, inertia tensor), `material(handle)`, `tolerances(handle)`,
  `feature_tree(handle)` — structured summary of the design intent.
- `generate_assembly_instructions(assembly)` — walks the tree, emits
  structured steps. Possibly defer to higher-level agent prompting and just
  expose the data.

### Effort

Big. ~1–2 weeks total, easy to slice further.

### Status (2026-04-25)

**Shipped:**
- `mass_properties` (volume, area, CG, bbox, inertia tensor; mass when density given).
- Assembly via `App::Part` + `App::Link`: `make_assembly`, `add_part` (in-doc
  handle or external `.FCStd` path), `list_assembly_parts`.
- `interference_check` — pairwise `Part.common().Volume`, descending.
- `bom_extract` — grouped by linked-object name, with optional mass via density.
- TechDraw: `make_drawing_page` (auto-finds built-in landscape template),
  `add_projection_group` (Front/Top/Right/etc., third-angle). Page +
  projection group build correctly and persist in saved `.FCStd`.
- 5 new tests, all green: mass-of-cube, two-part assembly + BOM,
  interference detected at 5mm overlap, no-interference when separated,
  drawing page constructed + persists across save/reopen.

**Deferred:**
- Mating constraints (planar, axial, distance) — needs the Assembly
  workbench's `JCS` machinery; deferred until there's a use case beyond
  manual `placement` arguments.
- Dimension annotations on TechDraw pages (`add_dimension(page, kind, refs)`
  with face/edge tags from Slice 1) — the agent-friendly form. Currently
  only multi-view projection is supported.
- BOM extraction across nested sub-assemblies (recursion).
- **PDF/SVG export of TechDraw pages**: not possible from headless
  `freecadcmd` in FreeCAD 1.1. The export functions live in `TechDrawGui`,
  which can't be imported headless. `export_drawing` raises
  `NotImplementedError` with the workaround (save the .FCStd, export from
  GUI). Fix path: ship a separate small CLI helper that launches FreeCAD
  GUI with a render-and-quit script, OR find a third-party SVG/PDF
  renderer that consumes the page object directly. Filed in
  `project_freecad_api_drift.md` so future-me doesn't re-discover it.
- `generate_assembly_instructions` — pure LLM task once `feature_tree`
  introspection lands.

**Known sharp edges (found 2026-05-30, building the multi-agent M1 toy suite —
`tests/multiagent_toys.py`):**
- **`add_part` mislinks consumed boolean inputs.** When a component `.FCStd` is
  linked by path, `add_part` picks "the first `PartDesign::Body` or
  `Part::Feature`" in the file. After an `add_primitive` + `boolean_op` chain the
  consumed `Part::Box`/`Part::Cylinder` inputs remain in the document, so the
  *un-holed solid* can get linked instead of the `Cut` result — silently, and
  nondeterministically (object order decides). The toy suite works around it by
  building each component as one clean `Part::Feature` (shape assigned via
  `run_script`). Fix path: have `add_part` prefer the body tip / last shaped
  feature, or skip objects that are consumed boolean inputs.
- **`bom_extract` collides components by object name.** It groups by
  `LinkedObject.Name`, but `add_primitive` names every box `"Box"` and every
  cylinder `"Cylinder"` internally — so two distinct single-primitive components
  merge into one BOM row with an inflated count. Fix path: group by a more stable
  identity (source file path + object, or Label), not the bare internal `Name`.

  Both are what the RFC's Phase 1 `merge_assembly` / `publish_interface` should
  harden — see [`docs/MULTI_AGENT.md`](MULTI_AGENT.md) and
  [`tests/MULTI_AGENT_EVAL.md`](../tests/MULTI_AGENT_EVAL.md).

---

## Slice 6 — operational polish

Smaller items, parallelizable:

- ~~`close_document` and multi-document support~~ ✓ shipped 2026-04-25 (Phase 2):
  `list_documents`, `set_active_document`, `close_document` (with handle
  invalidation tracking).
- ~~Transactions / undo bracketing so the agent can speculatively edit and roll
  back.~~ ✓ shipped 2026-04-25 (Phase 2): `transaction_open`/`commit`/`abort`.
  Wiki-drift note: FreeCAD 1.1 `doc.abortTransaction()` no longer rolls back
  in headless mode — the worker uses commit-then-undo for correct semantics.
- Geometry validation: `check_shape` (find faulty solids), unit sanity checks.
- "Session transcript" tool: dump the call history as a re-runnable Python
  script — important for reproducibility and human audit.
- Worker pool or async `fem_run` so long solves don't block the MCP channel.
- Verify macOS Gatekeeper / quarantine path under non-interactive launch
  (still flagged open in README).
- `mass_properties`, `bounding_box` — useful enough to pull in earlier than
  Slice 5; cheap.

---

## External simulation tools — what to stitch in

FreeCAD's bundled FEM (CalculiX + Elmer) covers mainstream **structural**,
**thermal**, **modal**, and basic **multiphysics** work. That's most of what
mechanical design needs day-to-day: stress, deflection, natural frequencies,
steady-state heat, contact.

Outside that envelope, FreeCAD is a *geometry source*, not a solver. The
canonical example in this house is the `~/diffuser` project: FreeCAD held the
`.FCStd` files, but the actual physics — 2D Snell/TIR ray tracing, moldability
scoring, BSpline parameter sweep — lived in a separate Python pipeline that
*imported* FreeCAD just for geometry extraction. That's the right pattern for
any domain FreeCAD doesn't natively cover.

### Pattern

Keep DriftPin focused on FreeCAD geometry + FEM. For each external solver, add
a thin MCP tool that takes a DriftPin handle (or its exported STEP/STL/mesh)
and returns structured results. The MCP server hosts both: `driftpin_*` tools
for FreeCAD, `optics_*` / `cfd_*` / `slicer_*` tools that feed off DriftPin
outputs.

### Domains worth integrating

| Domain | Tooling | Notes |
|---|---|---|
| **Optics** | `~/diffuser` pipeline (in-house), `rayoptics`, `optiland` | High value — already exists, ties to real work. Wrap the diffuser pipeline as MCP tools first. |
| **CFD** | OpenFOAM (via CfdOF or directly), SU2 | Elmer covers basics; OpenFOAM for serious work. |
| **Transient thermal / radiation** | Elmer, OpenFOAM `chtMultiRegionFoam` | CCX is steady-state only. |
| **Multibody dynamics / IK** | MuJoCo, PyBullet, pinocchio | Critical for anything that *moves*. Assembly4 has constraints but no dynamics. |
| **Tolerance stack-up / GD&T** | gdt-tools, tolstack, in-house Monte Carlo | Big agent unlock — "tighten this dim to ±0.05, assembly fits 99.7%". |
| **Injection molding** | Commercial only at quality (Moldex3D-class); open-source is thin | Diffuser project's heuristic moldability scoring is the pragmatic move. |
| **Machining** | FreeCAD Path workbench, pycam, kiri:moto | Toolpaths from geometry. |
| **3D-print slicing** | PrusaSlicer / OrcaSlicer CLI, CuraEngine | Slicer-as-tool is huge: print time, support volume, layer count back to the agent. |
| **Topology optimization** | FreeCAD-fenics, topopt, solidspy | Generate optimal geometry from load cases. |
| **Electromagnetics** | Elmer (basic), OpenEMS / FEniCSx (RF), FEMM (2D motors) | |
| **Acoustics** | Elmer, pyfar, acoular | |
| **Mass properties / inertia / CG** | FreeCAD native (`Shape.Mass`, `MatrixOfInertia`, `CenterOfMass`) | Already there — just expose. Drives a lot of agent decisions. |

### First external integration to ship

The `~/diffuser` optics pipeline is the natural first one — it already imports
FreeCAD, runs under DriftPin's worker today, and wraps existing in-house code.
Concrete tools:

- `optics_raytrace(model_path, source_config, n_refractive, n_rays)` →
  `{exit_distribution, leakage_fraction, hotspot_locations}`
- `optics_moldability_check(model_path, pull_axis)` →
  `{undercut_faces, draft_violations, wall_thickness_stats}`
- `optics_optimize(baseline_path, target_metrics, parameter_space)` →
  `{best_candidate_path, score_breakdown}`

This validates the "DriftPin-as-geometry-substrate, external-tool-as-physics"
pattern before generalizing it to CFD / MBD / etc.

---

## Sequencing summary

```
Slice 1 (face tags)      ─┐
                          ├─► Slice 3 (FEM decomp) ──┐
Slice 2 (PartDesign)     ─┘                          │
                                                     ├─► Slice 5 (assembly/drawings/mfg)
Slice 4 (visual feedback) ───────────────────────────┤
                                                     │
External integrations (start with diffuser/optics) ──┘

Slice 6 (operational polish) — parallel, pull in items as they bite.
```

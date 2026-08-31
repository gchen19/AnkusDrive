# AnkusDrive Phase 2 — completing the core

A fresh-session-executable plan for finishing the "core mechanical design"
surface area. Phase 1 (the original 5 slices + test scaffolding) shipped on
2026-04-25 and is documented in [`ROADMAP.md`](../ROADMAP.md). This document
picks up where that left off.

A new Claude Code session should be able to read this doc, the linked context
docs, and execute Phase 2 without backstory.

---

## Cold-start context (read these first)

- **[`README.md`](../../README.md)** — overall pitch and architecture sketch.
- **[`docs/ROADMAP.md`](../ROADMAP.md)** — what each slice shipped, what was
  deferred, why. The "Status" sections under each slice list known gaps.
- **[`tests/TEST_PLAN.md`](../../tests/TEST_PLAN.md)** — coverage map across the
  six test tiers + how to run them.
- **[`tests/RELIABILITY.md`](../../tests/RELIABILITY.md)** — the gated reliability
  harness (Layers A/B/C, all scaffolded but pending API key).
- **Memory:** `~/.claude/projects/<project-dir-slug>/memory/`
  - `project_freecad_api_drift.md` — **READ THIS** before adding any new
    handler. Lists 8 known wiki-vs-reality drifts in the FreeCAD API. The
    pattern of "introspect the live API via `dir()` + `getEnumerationsOfProperty()`
    before trusting docs" applies to every handler in this plan.
  - `project_ankusdrive_reliability.md` — reliability suite is pending API key.

## What Phase 1 shipped (the 80%)

**48 MCP tools across 5 slices:**
- Slice 1: face/edge tagging — `list_faces`, `list_edges`, `query_faces`,
  `resolve_face`, `resolve_edge`. Stable hashes survive geometry edits.
- Slice 2: PartDesign — Body, datum_plane, sketch (15 constraint types),
  pad/pocket/revolve, partdesign_fillet/chamfer.
- Slice 3: FEM (decomposed) — analysis/solver/material/constraint/mesh/run/results.
- Slice 4: visual feedback — software z-buffer rasterizer, 8 preset views.
- Slice 5: mass_properties + assembly + interference + BOM + TechDraw page.

**58 tests** in 7 files run via `bash tests/run_all.sh` in ~14s.

## What Phase 2 needs to ship (the remaining core)

These aren't exotic — they're mainline PartDesign + FEM features that any
real mechanical design uses. Without them, an agent can't make a hollow
plastic enclosure with a bolt pattern, can't run a modal analysis, can't
sweep a profile along a path.

Three phases, each independently shippable. **A → B → C** order is
recommended (B and C are not blocked by A but A unblocks the most user-
facing features).

---

## Phase A — Slice 2.5: PartDesign feature completeness

**Goal:** an agent can model the parts a junior mechanical designer would
draw on day one: holes with counterbores, bolt patterns, mirrored halves,
sketches that reference upstream geometry.

**Effort:** ~2 days.

**Files to edit:**
- `ankusdrive/worker.py` — add handlers (paste in the `# --- PartDesign /
  Sketcher ---` block, near the existing `pad`/`pocket`/`partdesign_fillet`).
- `ankusdrive/mcp_server.py` — add MCP wrappers right after the existing
  `partdesign_chamfer` tool.
- `tests/test_worker.py` — add tests after the existing
  `test_partdesign_*` tests.

### A1. PartDesign Hole

Probe first: `obj = doc.addObject("PartDesign::Hole", "Hole"); obj.PropertiesList`
to see the property surface in the installed FreeCAD version. The Hole
feature has many properties: `Diameter`, `Depth`, `DepthType`, `HoleCutType`
(Counterbore/Countersink/None), `HoleCutDiameter`, `HoleCutDepth`,
`ThreadType`, `ThreadDirection`, `Tapped`, etc.

```python
@handler("hole")
def _h_hole(p):
    """Drill a parametric hole. sketch must contain one or more circles
    defining hole locations. type: 'simple' | 'counterbore' | 'countersink'.
    Threading: thread=True applies a tap; thread_size like 'M5' picks a
    standard. depth_type: 'Dimension' (uses depth) | 'ThroughAll' | 'UpToFirst'.
    """
```

Tests:
- `test_hole_simple_through` — pad a 30mm cube, sketch a circle at center,
  hole through-all → final volume = pad - π·r²·h.
- `test_hole_counterbore` — set `HoleCutType="Counterbore"` with cut diameter
  > hole diameter; verify cylindrical face count and inner volume.
- `test_hole_by_face_tag` — sketch on a tagged face, hole survives if you
  add another feature later.

### A2. LinearPattern / PolarPattern / Mirrored

Probe: `PartDesign::LinearPattern`, `PartDesign::PolarPattern`,
`PartDesign::Mirrored`. Each has an `Originals` (the feature to repeat),
`Direction` (a body axis or edge), `Length`, `Occurrences`.

```python
@handler("linear_pattern")
def _h_linear_pattern(p):
    """Repeat a feature linearly. direction: body-axis name 'X'|'Y'|'Z' or
    {handle, edge_tag} for an edge-aligned direction. occurrences: int >= 2."""

@handler("polar_pattern")
def _h_polar_pattern(p):
    """Repeat a feature around an axis. axis: 'X'|'Y'|'Z' or {handle, edge_tag}.
    angle_deg: total swept angle. occurrences: int >= 2."""

@handler("mirrored")
def _h_mirrored(p):
    """Mirror a feature across a plane. plane: 'XY'|'XZ'|'YZ' or datum-plane handle."""
```

Tests:
- `test_linear_pattern_4_holes` — sketch + hole + linear pattern (4×, 30mm
  spacing) → final shape has 4 cylindrical faces of the hole's radius.
- `test_polar_pattern_6_holes` — circle of bolt holes; expect 6 cylindrical
  faces at radius from center axis.
- `test_mirrored_half` — pad a half-shape, mirror across YZ, total volume
  doubles (within float epsilon).

### A3. Sketcher external geometry

This is the load-bearing one for parametric chains. Today our sketches
can't reference existing geometry from inside the sketch — they can only be
*placed on* a face. With external geometry, a sketch can project an edge or
vertex from an existing feature into the sketch and constrain to it. This
is how you make a hole "always 5mm from the top edge" no matter how the
pad changes.

Probe: `Sketcher::SketchObject.addExternal(obj, "Edge3")` is the API. Also
need to surface external geometry in `add_sketch_geometry` somehow, or as a
new handler `add_sketch_external`.

```python
@handler("add_sketch_external")
def _h_add_sketch_external(p):
    """Project an external edge/vertex into the sketch as construction geometry
    that can be constrained to. ref: {handle, tag} for the face/edge/vertex.
    Returns the index assigned by Sketcher (negative — convention for external)."""
```

Tests:
- `test_sketch_constraints_to_external_edge` — pad a cube, start a sketch on
  the top face, project a side edge into the sketch, constrain a circle's
  center to be 5mm from that edge. Edit pad length → re-evaluate sketch →
  circle's absolute position moved with the edge.

### A4. Tag-survival regression tests

The Phase 1 cross-slice integration test proves tags survive a PartDesign
fillet (`test_partdesign_fillet_by_tag`). Phase 2 should extend this to
patterns/mirror — these change face counts dramatically and may break tag
hashes if the underlying signature collides.

- `test_tag_survives_linear_pattern` — tag a face on the original feature,
  apply a linear pattern, confirm the tag still resolves to the *original*
  face (not a patterned copy) on the source feature handle.

### Phase A acceptance

`bash tests/run_all.sh` shows worker tests up from 29 → ~36, all green.
Cross-slice integration test (`test_integration.py`) extended to include
a hole + pattern in the body — confirms feature interoperability across
the new tools.

---

## Phase B — Slice 3.5: FEM completeness

**Goal:** the FEM pipeline covers natural frequencies, buckling, and
thermal — not just static stress. Local mesh refinement so the agent can
trust results at stress concentrations.

**Effort:** ~1.5 days.

**Files to edit:**
- `ankusdrive/worker.py` — extend `fem_set_solver` tunables; add new
  `fem_modal_results`, `fem_thermal_results`, `fem_mesh_refinement`.
- `ankusdrive/mcp_server.py` — wrappers.
- `tests/test_worker.py` — new tests after the existing
  `test_fem_decomposed_cantilever`.

### B1. Modal analysis

CCX supports modal analysis via `AnalysisType="frequency"` plus
`EigenmodesCount` / `EigenmodeHighLimit` / `EigenmodeLowLimit`. The
solver tunables whitelist already contains these (see
`worker.py::_CCX_TUNABLES`); the missing piece is exposing the analysis
type as a first-class arg and reading frequency results.

```python
@handler("fem_modal")
def _h_fem_modal(p):
    """Configure analysis for modal (frequency) extraction. n_modes: count
    of eigenmodes to extract. f_low / f_high: bounding frequencies in Hz
    (optional). Caller still does fem_run + fem_results."""

@handler("fem_modal_results")
def _h_fem_modal_results(p):
    """Extract natural frequencies and (optionally) mode shapes. Returns
    {frequencies_hz: [...], mode_shapes?: [{mode, displacements: [...]}]}."""
```

Test:
- `test_fem_modal_cantilever` — the same cantilever beam as
  `test_fem_decomposed_cantilever`. Run a modal analysis, expect the first
  bending frequency near the analytical Euler-Bernoulli result
  `1.875² · √(EI/ρA·L⁴) / 2π`. Match within 5%.

### B2. Buckling

CCX supports linear buckling via `AnalysisType="buckling"`. Reads buckling
factors (load multipliers) from results.

```python
@handler("fem_buckling")
def _h_fem_buckling(p):
    """Configure analysis for linear buckling. n_factors: how many buckling
    modes to compute. Caller adds a force constraint at unit magnitude;
    result factors are the multipliers at which buckling occurs."""
```

Test:
- `test_fem_buckling_column` — long thin column, fixed at base, axial unit
  force at top. First buckling factor should be near Euler critical load
  `π²EI/L²`.

### B3. Thermal constraints + steady-state thermal

```python
@handler("fem_add_constraint")  # extend existing handler
# kinds: + 'temperature', + 'heatflux', + 'initial_temperature'
```

Test:
- `test_fem_thermal_steady_state` — bar with one end at 100°C, other end
  at 20°C, no heat flux on sides. Expect linear temperature gradient.

### B4. Local mesh refinement

`mesh.MeshRegionList` lets you specify per-face characteristic length.
Useful for stress concentrations.

```python
@handler("fem_mesh_refinement")
def _h_fem_mesh_refinement(p):
    """Add a local mesh refinement to an existing FEM mesh. refs: list of
    {handle, face_tag} (refines on those faces). char_length: smaller than
    the global setting on the mesh."""
```

Test:
- `test_fem_local_refinement` — cantilever with a fillet near fixed end,
  refine the mesh on the fillet face, run FEM, observe that node count
  near that face is materially higher than baseline mesh.

### Phase B acceptance

Worker tests up another ~5 to ~41. Cross-slice integration test optionally
adds a `fem_modal` step on the parametric body for end-to-end validation.

---

## Phase C — Slice 2.6 + worker plumbing

**Goal:** the more advanced PartDesign features (loft/sweep/helix,
thickness/draft) plus worker-level capabilities (multi-document,
transactions) that unlock complex workflows.

**Effort:** ~3 days. Larger than A and B because each item has its own
non-trivial probe + design.

### C1. Loft / Sweep / Helix

These take multiple sketches or sketch + path. Each is its own probe.

- **Loft** — `PartDesign::AdditiveLoft` with `Sections` = list of sketches.
- **Sweep** — `PartDesign::AdditivePipe` with `Profile` (sketch) + `Spine`
  (path object/sketch).
- **Helix** — `PartDesign::Helix` (additive) or via Pad of a sketch along a
  helical path; FreeCAD provides a `Part::Helix` primitive that can be
  used as a sweep spine.

```python
@handler("loft")
def _h_loft(p):
    """Loft between two or more sketches. sketches: list of handles, ordered."""

@handler("sweep")
def _h_sweep(p):
    """Sweep a profile along a spine. profile: sketch handle. spine:
    sketch handle (defines the path)."""

@handler("helix")
def _h_helix(p):
    """Generate a helical solid (e.g. for threads). center_axis: 'X'|'Y'|'Z'.
    radius, pitch, height, profile (sketch handle for thread cross-section)."""
```

Tests: a swept circle along a curved path produces a torus-like solid;
a loft between a circle and a square produces a transition shape with the
expected bbox.

### C2. Thickness / Draft

- **Thickness** — `PartDesign::Thickness`. Takes a base feature and a list
  of faces to *remove* (the open faces of the resulting shell).
- **Draft** — `PartDesign::Draft`. Takes faces, a neutral plane, a
  reversed flag, and a draft angle.

```python
@handler("thickness")
def _h_thickness(p):
    """Hollow out a solid into a shell. base: feature handle. open_faces:
    list of {handle, face_tag} that become the shell's openings.
    thickness: wall thickness in mm. Reversed/Outward flags optional."""

@handler("draft")
def _h_draft(p):
    """Apply a draft angle to faces (for moldability). faces: list of
    {handle, face_tag}. neutral_plane: {handle, face_tag} for the
    plane along which the angle is measured. angle_deg: float."""
```

Tests: thickness on a 30mm cube with one face removed produces a 5-walled
box; volume = original - (interior_volume). Draft on a vertical face
shifts the top edge by `height·tan(angle)`.

This unblocks the `~/diffuser` project's moldability work — currently it
runs heuristics on geometry imported from external `.FCStd` files; with
`draft` exposed, the diffuser pipeline could *generate* draft-compliant
candidates directly via AnkusDrive.

### C3. Multi-document support

The current worker has one `App.ActiveDocument` and most handlers assume
it. For assembly workflows that pull in multiple parts from separate
files, this is awkward.

```python
@handler("close_document")
def _h_close_document(p):
    """Close a document by name or 'active'. Frees its objects from memory."""

@handler("set_active_document")
def _h_set_active(p):
    """Switch the active document. name: doc.Name (returned by new/open)."""

@handler("list_documents")
def _h_list_documents(p):
    """Return [{name, label, file_path, dirty}, ...] for all open documents."""
```

Handle registry implication: `_handles` currently doesn't track which
document each handle belongs to. When closing a document, its handles
should be invalidated. Add a `_handle_to_doc: dict[str, str]` mapping.

Tests:
- `test_multi_document_isolation` — open two docs, geometry in each is
  visible only when that doc is active.
- `test_close_document_invalidates_handles` — after closing, resolving an
  old handle raises a clear error (not a segfault).

### C4. Transactions

```python
@handler("transaction_open")
def _h_tx_open(p):
    """Begin a transaction. label appears in undo history."""

@handler("transaction_commit")
def _h_tx_commit(p):
    """Finalize the current transaction."""

@handler("transaction_abort")
def _h_tx_abort(p):
    """Roll back changes since the last transaction_open."""
```

FreeCAD: `doc.openTransaction(label)`, `doc.commitTransaction()`,
`doc.abortTransaction()`.

Test:
- `test_transaction_rollback` — open transaction, add a primitive, abort,
  list_objects shows nothing was added.

This is critical for agent-loop self-correction: the agent can try an
edit, render it, decide it's wrong, and roll back without rebuilding the
entire history.

### Phase C acceptance

Worker tests up another ~12 to ~53. The diffuser project's moldability
pipeline could in principle now run end-to-end on AnkusDrive-generated
geometry (a follow-up project, not a Phase 2 deliverable).

---

## After Phase 2

When all three phases land, AnkusDrive has the full **core** mechanical-
design surface area. What remains is:

1. **External integrations** — diffuser/optics, OpenFOAM CFD, MuJoCo
   dynamics. The roadmap pattern: each is a separate MCP tool that
   consumes AnkusDrive geometry as STEP/STL.
2. **TechDraw dimensions and annotations** — agent-friendly form
   (`add_dimension(page, kind, refs_with_tags)`).
3. **Visual polish** — antialiasing, edge cleanup (silhouette/feature
   edges only, not all triangulation), FEM result colormap render.
4. **Layer D reliability** — full agentic harness where the model uses
   AnkusDrive tools via MCP, then self-evaluates.
5. **Operational** — async `fem_run` with progress streaming, session
   transcript export, expression-engine bindings.

These are real but each is independently scoped and not blocking the
"core complete" milestone Phase 2 closes.

---

## Working notes for the next session

- **Probe before writing.** The FreeCAD wiki is wrong in 8 known places
  (memory `project_freecad_api_drift.md`). For every new handler, write
  a one-off probe via `worker.run_script` to dump `obj.PropertiesList` and
  `obj.getEnumerationsOfProperty(...)` for any enum-valued properties.
  Catching wiki drift adds 10 minutes; debugging a wrong call costs an
  hour.

- **Use the `__result__` convention** in probe scripts so output flows
  through the JSON channel, not the redirected stdout. Print statements
  in `run_script` go to /dev/null.

- **Handle registry hygiene.** Every new handler that creates an object
  should call `_register(prefix, obj)` and return `{"handle": ..., "name":
  ...}`. Use a stable prefix per object type (`"hole"`, `"linear_pattern"`,
  etc.) so handle counters stay readable.

- **Tag pass-through.** Any handler that takes a face/edge reference must
  accept either a tag (`"f_..."`/`"e_..."`) or a `FaceN`/`EdgeN` index
  string. Use `_resolve_face_ref` / `_resolve_edge_ref` from `worker.py`
  for the conversion — they handle both forms uniformly.

- **MCP wrapper conventions.** Mirror the worker handler 1:1. Convert
  optional parameters to typed kwargs with defaults. Never invent new
  semantics in the wrapper — the wrapper's job is type-safe MCP exposure,
  the worker's job is FreeCAD logic.

- **Test patterns.**
  - Build the artifact, then verify a property (volume, face count,
    frequency) against a known answer.
  - Then verify the **edit-stability invariant**: create the same artifact
    a second way, or apply an unrelated downstream edit, and confirm the
    answer is unchanged.
  - Use the existing `_build_pad` helper in `tests/test_edit_stability.py`
    as the template for sketch-pad-based test setup.

- **Run `bash tests/run_all.sh` after every phase ships.** All tiers must
  stay green. If a new test breaks an old one, the diff likely changed
  shared state in `worker.py` (the registry, the script globals, or a
  helper like `_solid_of`).

- **Update `ROADMAP.md` "Status" sections** as each phase ships, and add
  any new wiki-drift discoveries to memory `project_freecad_api_drift.md`.

- **The reliability suite stays gated.** Don't run it (it costs API
  credits) unless the user explicitly asks. The `RUN_RELIABILITY=1`
  branch in `tests/run_all.sh` covers it.

## Phase tally (when complete)

- Phase A: ~7 new handlers, ~7 new tests, +0 deps.
- Phase B: ~5 new handlers (incl. constraint kinds), ~3 new tests, +0 deps.
- Phase C: ~12 new handlers, ~6 new tests, +0 deps.

Total new MCP tools: ~24. Total tests: 58 → ~80. No new third-party
dependencies; everything is FreeCAD's bundled API.

End state: AnkusDrive exposes ~72 MCP tools covering the core of CAD design,
parametric modeling, FEM (static/modal/buckling/thermal), assembly, and
drawings — sufficient for an agent to drive end-to-end mechanical design
of a typical part without falling back to the `run_script` escape hatch.

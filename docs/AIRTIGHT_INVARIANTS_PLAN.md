# AnkusDrive — Functional-invariant checks for enclosed-flow parts

A fresh-session-executable plan for closing the gap reported in
[issue #19](https://github.com/gchen19/AnkusDrive/issues/19): *"Agent loses
airtight-shell invariant across iterative geometry edits."* A new Claude Code
session should be able to read this doc + the linked context and execute it
without backstory.

---

## Context — why this exists

An external user (Matthew Petney, `@mpetne`) modeled a Makita-router →
Dewalt-shop-vac adapter — an **enclosed-airflow** part — through an LLM agent on
the AnkusDrive MCP server. Across three edit iterations the agent "fixed the last
problem" while silently breaking a different geometric invariant, because
AnkusDrive's tool surface measures *geometry* (volume, face counts, watertightness)
but has no concept of **functional intent**:

1. **v1 — interface oversimplified.** Router-side plug modeled as a flat
   rectangle, missing the cylindrical housing curvature → no contact patch →
   leaks. (A *sealing-surface* problem.)
2. **v2 — interface fixed, airflow path broken.** The cavity→barb transition
   was a near-zero "almond-slit" where an angled cavity grazed the inner shell
   — topologically a doorway, functionally a wall. Air had nowhere to go. Volume
   looked plausible, so nothing flagged it. (A *connectivity / bottleneck*
   problem.)
3. **v3 — doorway added, material over-cut.** A rectangular pass-through cut
   nicked the snap-tab stems and (likely) the curved shell at oblique angles,
   opening leak paths. (A *leak / unintended-opening* problem.)

The reporter's diagnosis is correct and is the design target: the agent had **no
cheap, re-runnable way** to express or check (a) which faces are *inside the
airflow path* vs *ambient*, (b) whether a single connected void joins the inlet
to the outlet bounded by solid everywhere else, (c) semantic roles on faces
("inlet"/"outlet"/"sealing"), and (d) a regression gate to re-confirm "this is
still a working adapter" after every edit.

`check_shape` already reports `watertight_solid`, but a watertight solid can
still have a blocked or leaky *flow path* — watertightness is necessary, not
sufficient. This plan adds the functional layer on top.

### Cold-start context (read these first)

- **[`README.md`](../README.md)** — architecture; the three layers (typed tools
  / `get_object`+`set_property` / `run_script`); the Phase-3 "intent-encoding"
  framing this feature extends.
- **[`docs/ROADMAP.md`](ROADMAP.md)** — slice changelog + deferred backlog.
- **[`docs/MULTI_AGENT.md`](MULTI_AGENT.md)** §4 — the `publish_interface`
  named-frame property-bag pattern this feature mirrors for persistence.
- **[`tests/TEST_PLAN.md`](../tests/TEST_PLAN.md)** + **`tests/run_all.sh`** —
  test tiers and how to run them.
- **Project memory** `project_freecad_api_drift.md` (if present) — **the FreeCAD
  wiki lies; introspect the live API** (`dir()`,
  `getEnumerationsOfProperty()`) before trusting any signature. The API-drift
  notes in §7 below were gathered that way against FreeCAD 1.1.

---

## The big design decision: pure-BREP void analysis, **not** voxel flood-fill

Issue #19 suggests "ray / flood-fill analysis on the void." We evaluated that
against a **pure-BREP (boundary-representation) void-solid** approach and
**recommend BREP**, validated empirically in a live FreeCAD 1.1 worker.

**Core insight:** building the void as `padded_bbox.cut(part)` makes OCCT's
boolean engine do the connectivity analysis for free. A void region fully
enclosed by solid emerges as its **own separate entry in `void.Solids`**; a void
region open to ambient is **fused into the single large ambient solid**.

Behaviors verified live (FreeCAD 1.1.0, not from docs):

| Test shape | `padded_bbox.cut(...)` result | Meaning |
|---|---|---|
| Solid box | 1 solid, 2 shells | ambient only |
| Through-tunnel, both ends open | 1 solid | bore continuous with ambient → *open channel* |
| Capped/sealed cavity | 2 solids: ambient + enclosed cavity | enclosure ⇒ a **distinct** void sub-solid |
| Good adapter, inlet+outlet **plugged** by overlapping caps | ambient + **one** enclosed flow void containing *both* port probe points | **connected = true** |
| Almond-slit (0.2 mm bridge) | 1 enclosed void; `slice()` area sweep → **min section 1.6 mm²** vs max 103 mm² | bottleneck detected though topologically connected (the v2 bug) |
| Leaky doorway | flow void **merges into ambient** (`Solids` 2→1) | leak detected (the v3 bug) |

So one primitive — `padded_bbox.cut(capped_part).Solids` plus a 1-D `slice()`
area sweep — distinguishes **connected**, **leaky**, and **bottlenecked**.

**Why not voxelization:**
- *Sub-resolution failures are silent.* Catching a 0.2 mm almond-slit or a thin
  leak needs a grid fine enough to be unaffordable in `freecadcmd`; a coarse
  grid straddles the aperture and reports "fine." BREP measures the **analytic**
  bottleneck area, exact to OCCT's boolean tolerance (~1e-7 mm).
- *Cost.* Ray-march voxelization is O(grid²) `section()` calls (full BOPs); BREP
  is a handful of booleans + one slice sweep — low single-digit seconds on
  representative geometry.
- *Determinism.* The repo has a load-bearing bit-for-bit determinism test
  (`tests/test_determinism.py`). Flood-fill over float spans invites
  ordering/tie nondeterminism; rounded BREP volumes/areas are stable.

Voxelization's only edge — robustness on a dirty/non-manifold input where the
boolean fails — is handled by a guarded structured error + fallback (§7), not by
making it the primary path.

---

## Primitives already in the codebase to reuse (do not reinvent)

| Need | Reuse | Location |
|---|---|---|
| Ray ∩ boundary distances | `_ray_hit_distances(shape, origin, dir, max_d)` | `ankusdrive/worker.py:2608` |
| Point-in-solid | `shape.isInside(pt, tol, True)` (see usage) | `ankusdrive/worker.py:2648` |
| Inward-nudge probe trick | `_first_wall_depth` nudge | `ankusdrive/worker.py:2642` |
| Outward face normal | `_outward_normal(...)` | `ankusdrive/worker.py:935` |
| Stable face tags + signature | `_face_signature` / `_face_descriptor`, `query_faces`/`resolve_face` | `ankusdrive/worker.py:348`, `:1904+`, `mcp_server.py:925+` |
| Cross-section areas (slice sweep) | `section_view` slice/closed-wire-area pattern | `ankusdrive/worker.py:~1859` |
| Watertight verdict + topology | `check_shape` | `ankusdrive/worker.py:1768` |
| **Semantic property-bag persistence** | `publish_interface` → `AD_Interfaces` (`_IFACE_PROP`, `_read_interfaces`, `_shaped_top`) | `ankusdrive/worker.py:3756-3826` |
| Contract / re-runnable gate pattern | `assembly_lock` / `assembly_lock_check`, `verify_feature` | `mcp_server.py:1741-1762`, `:1491` |
| Composing handlers from a handler | `merge_assembly` calls other handlers | `ankusdrive/worker.py:~4300` |
| Headless fixture construction in tests | `run_script` test pattern | `tests/test_worker.py:~1064` |

Everything geometric runs **worker-side** (`@handler` in `worker.py`) — it needs
OCCT (`cut`/`fuse`/`common`/`isInside`/`slice`/`removeSplitter`/`.Solids`). Each
gets a thin `@mcp.tool()` wrapper in `mcp_server.py` calling `_call(...)`
(required by the `tests/test_contracts.py` registry-parity test). The host-side
rasterizer in `render.py` does **not** help (silhouette z-buffer, no interior
depth) — leave it out.

---

## Tool surface

All persistence mirrors the `publish_interface` idiom: a single
`App::PropertyString` JSON bag on `_shaped_top(obj)`, persisted in the `.FCStd`.
New keys: **`AD_FaceRoles`**, **`AD_Intent`** (add `_FACEROLE_PROP`,
`_INTENT_PROP` constants + `_read_face_roles`/`_read_intent` helpers next to
`_read_interfaces`).

### 1. `check_airtight_path` — flagship (pure inspection, no persistence)

```
check_airtight_path(handle, inlet, outlet,
                    min_aperture_mm2: float | None = None,
                    pad_mm: float | None = None) -> dict
```

`inlet`/`outlet` accept an `f_*` tag, `"FaceN"`, an int index, **or** a role name
resolved through `AD_FaceRoles` (so the agent can pass `inlet="inlet"`).

**Algorithm:**
1. Resolve inlet/outlet to faces (reuse the `_h_resolve_face` path used by
   `oring_groove`, `worker.py:~921`).
2. **Cap the ports.** For each port build a *plug* solid that **overlaps the
   wall** (prism the port face along its `_outward_normal` outward, thicken
   slightly inward). Abutting caps leave disjoint solids and break the boolean —
   verified; caps must overlap. `capped = part.fuse(incap).fuse(outcap).removeSplitter()`.
3. **Build the void.** `pad = pad_mm or max(2.0, 0.05*bbox.DiagonalLength)`;
   `big = Part.makeBox(...)` over `bbox` expanded by `2*pad`;
   `void = big.cut(capped)`.
4. **Classify components.** `ambient = max(void.Solids, key=Volume)` (tie-break
   by centroid lexicographic order for determinism); `enclosed = the rest`.
5. **Connectivity.** Interior probe = port face centroid nudged inward by a hair
   (mirror `_first_wall_depth`'s nudge). `connected = any(s.isInside(p_in) and
   s.isInside(p_out) for s in enclosed)`. If neither enclosed solid contains the
   inlet point but `ambient` does → the inlet is open to ambient with caps off →
   report `inlet_open_to_ambient` (degenerate-port guard).
6. **Leaks.** If capping the declared inlet+outlet does **not** yield exactly one
   enclosed flow void joining them (it stays merged with ambient), there is
   another opening = leak. Localize by sampling the flow-void shell face
   centroids and ray-casting a short outward probe (`_ray_hit_distances`): a
   centroid that reaches ambient without crossing solid is on a leak opening.
7. **Bottleneck.** Sweep `slice(axis, d)` along the inlet→outlet centroid axis,
   sum closed-wire face areas per station (reuse the `section_view` pattern),
   take the min over interior stations → `min_aperture_mm2` + `bottleneck_point`.
   Station count modest (~40) and configurable.
8. `ok = connected and not leaks and (min_aperture_mm2 is None or
   min_aperture_mm2 >= threshold)`.

**Returns:**
```jsonc
{ "connected": bool,
  "min_aperture_mm2": float | null,
  "bottleneck_point": [x,y,z] | null,
  "leaks": [ {"point":[x,y,z], "area_mm2": float} ],   // [] = airtight
  "void_components": int,
  "flow_void_volume_mm3": float,
  "inlet": "FaceN", "outlet": "FaceN",
  "pad_mm": float,
  "ok": bool }
```

### 2. `annotate_face` — declare semantic role (persists in `.FCStd`)

```
annotate_face(handle, face, role, name: str | None = None, **meta) -> dict
```
`role ∈ {inlet, outlet, sealing, wetted, ambient, mating}` (extensible). Stores
`{name: {role, tag, signature_snapshot, **meta}}` in `AD_FaceRoles`.
`signature_snapshot = _face_signature(face)` so `verify_intent` can detect a
vanished/changed tagged face after edits. Returns `{handle, name, role, tag,
roles:[...]}`. Mirrors `publish_interface` exactly.

### 3. `classify_face_sides` — inside/outside topology

```
classify_face_sides(handle) -> list
```
For each face, probe `isInside` at centroid ± nudge·normal → label
`inside`/`outside`/`ambiguous`; cross-reference the enclosed flow-void shell to
suggest `wetted`. Returns `[{tag, index, side, suggested_role}]`. Answers the
"inside-vs-outside topology" ask and auto-suggests wetted faces.

### 4. `declare_intent` / `verify_intent` — the re-runnable regression gate

```
declare_intent(handle, contract) -> dict      # writes AD_Intent
verify_intent(handle) -> dict                  # re-runs every declared invariant
```
`contract = {watertight: bool, airtight_path: {inlet, outlet, min_aperture_mm2},
required_faces: [tags]}`. `verify_intent` composes the other handlers
(`check_shape`, `check_airtight_path`, tag resolution) — the way `merge_assembly`
calls handlers — and returns `{ok, results:[{invariant, passed, detail}]}`. This
is the "run after every edit" tool the issue asks for.

### 5. Docstring caveats (no behavior change)

`check_shape`, `mass_properties`, `verify_feature` get one line: watertight /
volume / face-count do **not** confirm an unobstructed, leak-free flow path —
use `check_airtight_path` / `verify_intent` for enclosed-flow parts. (New tools
must document a ≥40-char return value or `test_contracts.py` fails.)

---

## Slices (each independently shippable + testable via `tests/run_all.sh`)

1. **`check_airtight_path`** (flagship, no persistence; face refs by tag/index).
   Highest value, self-contained, validates the BREP core. **Ship first.**
2. **`annotate_face` + `AD_FaceRoles`** — lets `check_airtight_path` accept role
   names; adds the signature snapshot.
3. **`classify_face_sides`** — inside/outside topology + wetted suggestions.
4. **`declare_intent` / `verify_intent` + `AD_Intent`** — composes 1–3 into the
   regression gate.
5. **Docstring caveats** — trivial; can land with slice 1.

Each slice = worker `@handler` + MCP `@mcp.tool()` wrapper (registry parity) +
tests.

---

## Test strategy

Build synthetic fixtures **headlessly via `run_script`** (the established test
pattern; all three confirmed buildable in probes):

- **Good adapter** → `connected=True, leaks=[], min_aperture≈64 mm², ok=True`.
- **Almond-slit (v2)** → `connected=True` but `min_aperture≈1.6 mm²` →
  `ok=False` when `min_aperture_mm2` threshold set (e.g. 10).
- **Leaky doorway (v3)** → capped void collapses to 1 solid → `leaks` non-empty,
  `ok=False`.
- **Flat interface (v1)** → `classify_face_sides` / sealing-role check flags the
  missing seal.

Plus:
- **Edge/negative:** oblique (non-axis-aligned) inlet face; `inlet==outlet`
  error; port face that isn't actually a hole; degenerate/zero-area port.
- **Determinism** (`test_determinism.py`): two workers → bit-identical
  `check_airtight_path` JSON on the good adapter. *(Load-bearing — guards
  void-component ordering.)*
- **Perf** (`test_perf.py`, `RUN_PERF=1`): bound `check_airtight_path` runtime on
  a representative part; budget set after measuring the real golden `.FCStd`.
  Catches accidental quadratic regressions.
- **Edit-stability** (`test_edit_stability.py`): roles + intent survive an
  unrelated fillet/recompute.
- **Golden fixtures:** the reporter offered the real **v1/v2/v3 `.FCStd`**.
  Wire them as an *optional* skip-if-absent suite (like `test_render_photoreal.py`
  skips when the addon is missing), under `tests/fixtures/issue19/`. Keep out of
  the default CI gate (large/external); synthetic fixtures are the always-on
  regression.

---

## Risks / edge cases

- **API drift (verify live before trusting).** Confirmed present on
  `Part.Shape` in FreeCAD 1.1: `cut`, `fuse`, `common`, `removeSplitter`,
  `isInside(pt, tol, checkFace)`, `slice(normal, d)`, `section`, `.Solids`,
  `.Shells`. **Gotchas found:** `Part.Vector` does **not** exist → use
  `App.Vector`. `Part.getSortedClusters` works on **edges only**, not faces.
  `connectedFaces` is **not** a method here. The plan deliberately uses only
  verified calls.
- **Caps must overlap, not abut.** Abutting cap solids leave disjoint solids
  (`fuse` → 3 solids) and break `common()` identification; overlapping plugs fuse
  to 1. Use `removeSplitter()` after fusing.
- **Face-tag stability after boolean cuts.** Tags hash area/normal/centroid; a
  cut that clips the inlet face (v3 doorway nicking a corner) changes its area →
  tag drifts. Mitigation: `annotate_face` stores a signature snapshot;
  `verify_intent` falls back to nearest-centroid+normal match and reports
  `face_drifted` rather than hard-failing. Same fragility `interface_align_check`
  lives with — document it.
- **Wetted faces: re-derive, don't freeze.** Re-derive wetted membership from
  current geometry on each call; only *freeze* the user-declared semantic roles
  (inlet/outlet/sealing). Freezing wetted membership goes stale after every edit.
- **Non-planar / oblique ports.** Cap prism extrudes along the face's true
  normal (`_outward_normal`), not a world axis; bottleneck axis is the
  inlet→outlet centroid vector. Curved inlet faces (the v1 housing curvature)
  work via prism-of-face; a zero-area port is an explicit error.
- **`bbox.cut(part)` robustness.** On a non-manifold/invalid part the cut may
  fail or produce garbage. Guard with `isValid()` first; on failure return a
  structured error directing the agent to inspect/fix, not a silent wrong answer.
- **Sub-tolerance leaks.** BREP is exact to OCCT boolean tolerance (~1e-7 mm),
  far finer than any voxel grid, but a leak thinner than that won't register.
  Acceptable; documented.
- **Determinism of `max(void.Solids, key=Volume)`** on symmetric parts with
  equal-volume void components — tie-break by centroid lexicographic order.

---

## Done when

- Slices 1–5 land with green tests in `tests/run_all.sh`.
- The three synthetic fixtures reproduce + correctly flag the v1/v2/v3 failure
  modes (slit → bottleneck, doorway → leak, flat → sealing).
- `README.md` tool table + `docs/ROADMAP.md` get a "Functional invariants" entry;
  `check_shape`/`mass_properties`/`verify_feature` docstrings carry the caveat.
- (Stretch) the reporter's real `.FCStd` files wired as optional golden fixtures.

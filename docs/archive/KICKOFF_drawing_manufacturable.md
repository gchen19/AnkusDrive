# Kickoff — Drawing-is-manufacturable + legible gates (issue #85, the "Next layer")

Written 2026-06-15. The TechDraw export work (PR #80,
`docs/KICKOFF_techdraw_export.md`) shipped the **renderer + dimension palette**:
the agent can place any extent / linear / Ø / R dimension and get a clean,
true-valued (AD_TrueValue) drawing out. It deliberately stopped short of two
things, both tracked by issue #85:

1. **Legibility** is heuristic (offset-stacking) and never validated.
2. **Manufacturing completeness** — nothing checks whether the chosen dimensions
   are *enough to make the part*.

> **Status (2026-06-15): both gates shipped.** A green render is not a
> manufacturable drawing — the same failure mode the geometry-realizes-declaration
> gate (`ankusdrive/realize.py`, §11.10) closed for assemblies. These are that gate,
> one layer up: validate the *drawing*. The placement *rewrite* (A2/A3 below) is
> still open — these gates make it measurable first.

## What shipped

`ankusdrive/drawing_gate.py` — pure, FreeCAD-free, unit-tested
(`tests/test_drawing_gate.py`, 29 cases). Two checks, both returning realize-style
violation lists (`[]` == pass; each violation has a `code` + human `reason`):

* **`check_completeness(features, dims, process, …)`** — degrees-of-freedom
  accounting. A part is manufacturing-complete iff every feature is **located** and
  **sized**, with no DOF pinned twice by *disagreeing* numbers. Process-aware:
  - `prismatic` (milled / plate): block sized W×H×T; each hole sized Ø + located
    X/Y from a datum (+ depth if blind); counterbores add Ø + depth.
  - `turned`: concentric by construction — a step needs only Ø + axial length, a
    bore Ø + depth, plus overall length. **No radial location** (the scheme
    difference the issue asked for).
  Codes: `under` (missing), `redundant` (one DOF dimensioned twice, agreeing),
  `conflict` (twice, disagreeing), `extra` (a dim pinning nothing),
  `no_datum` (a location not taken from a declared datum face).

* **`check_legibility(labels, segments, views, border, …)`** — 2-D geometry on the
  *actual* placed graphics. Codes: `overlap` (two dim labels collide),
  `crosses_view` (a dim line crosses a view it doesn't reference),
  `out_of_border` (anything off the sheet).

### Worker + MCP wiring (`worker.py`, `mcp_server.py`)

* `_dim_to_svg` refactored onto a single `_dim_layout` so the renderer **and** the
  legibility gate read one source of truth — the gate checks the real layout, not a
  re-derivation. (Rendered SVG is byte-identical; the existing drawing tests pass.)
* `_h_add_dimension` now stamps **`AD_ModelRef`** (the model-space circle/span a dim
  references) on each manual dim, so the completeness gate can tell *which* feature
  DOF a dim pins — a Ø on hole A vs the X location of hole B — which the projected
  2-D dim alone cannot recover.
* `_h_drawing_gate` enumerates features off the real `Part.Shape` (bounding box;
  inward cylinders grouped coaxially so a counterbore is recognised; outer
  cylindrical steps for a turned part), infers `process` (or takes an override),
  builds dim descriptors (kind via AD_Prefix, value via AD_TrueValue, ref via
  AD_ModelRef), and calls `completeness_report`.
* `_h_drawing_legibility` replays the composer's stacking via `_dim_layout` to
  extract label boxes + line segments + view boxes, then calls `legibility_report`.
* MCP tools `drawing_gate(page, process='auto', datums_declared=False)` and
  `drawing_legibility(page, min_gap=0.5)`.

End-to-end coverage in `tests/test_drawing_gate_worker.py` (13 cases, real worker):
a thoughtfully-dimensioned plate passes; dropping the hole's Y location is `under`;
auto-dimensioning across two views over-dimensions the width (`redundant`); a
stepped shaft auto-infers `turned` and enumerates its steps; legibility runs on the
placed graphics.

## Design notes / honest edges

* **True-valued dims rarely "conflict".** Because every AnkusDrive dim reads the real
  solid, two dims on one DOF normally *agree* → `redundant`. `conflict` exists for
  manually-overridden values; it binds by reference (the worker's resolved refs /
  AD_ModelRef), not by value, so a wrong number still lands on its slot.
* **Datums (DONE 2026-06-15).** `annotate_face` gained a `datum` role; the gate reads
  the source body's datum faces (`_datum_faces`) and sets each location dim's
  `from_datum` by whether an endpoint actually sits on a datum face
  (`_point_on_any_face`). The `no_datum` check turns on automatically when any datum
  face is declared. A 2-D hole location needs a reference *frame* (a primary + a
  secondary datum), as `test_datum_origin_discipline` shows: with the x=0 and y=0
  faces declared, locating the hole from them is clean; locating it from the far edge
  flags `no_datum`. (GD&T position/tolerance frames remain future work.)
* **Enumeration is conservative.** An unrecognised face yields *no* slot rather than
  a wrong one (no false `under`). Counterbore through-ness is read from the UNION of a
  hole's coaxial cylinder spans, so a counterbored through-hole isn't mistaken for a
  blind bore. **Fillets** (partial cylinders, U-extent < 1.5π — distinct from a full
  2π bore) and **chamfers** (off-axis narrow planar bevels) are enumerated to DISTINCT
  sizes (a drawing calls out "R3" once, not per edge); a fillet is matched by an R
  dimension (a bare R is never credited to a hole Ø), a chamfer by a linear size.
  Threads are still future. Example: `artifacts/drawings/fc_bracket_demo.*`
  (`scratch/drawing_demo.py:fillet_chamfer`).
* **Tolerances (DONE 2026-06-15).** `add_dimension` takes a `tolerance`:
  `{"sym": 0.1}` (±0.1), `{"plus": .., "minus": ..}` (asymmetric), or
  `{"fit": "H7"}` / `{"fit": "H7/g6"}` — ISO 286 hole-side limits looked up at the
  dimension's basic size via `tolerance.fit_class`. Stamped (`AD_TolPlus/Minus`) and
  rendered next to the value (`Ø12.00 +0.018/-0`). This is the *palette* layer; a
  gate check for *which* features must carry a tolerance (fits, positions) is future
  work, building on `gdt_check` / `tolerance_stackup`.

## Acceptance — the issue's headline

`test_counterbored_bracket_acceptance`: a plate with a Ø6 through hole + Ø12×4
counterbore is enumerated (hole + counterbore), the fully-dimensioned drawing
(W/T/D, hole Ø + X/Y, counterbore Ø + depth) passes `drawing_gate` (ok) AND
`drawing_legibility` (ok) — a machinist could cut it from the sheet. This shook out
a latent FreeCAD trap: `Vector.multiply(k)` scales IN PLACE and returns self, so
`axis.multiply(t)` was corrupting the grouping axis (counterbore split into two
holes) and the Ø-dimension's `xdir` (its dim line rendered mis-scaled). Fixed with a
non-mutating `_vscale`; the Ø dimension line is now the correct length.

## Legibility — Part A progress

The gate (A1) is the prerequisite; it makes "legible" a regression assertion.

* **A1 — legibility gate.** DONE (above).
* **A2 — collision-driven lane packing.** DONE 2026-06-15. The fixed
  `_DIM_OFFSET_MM + idx*_DIM_STACK_MM` stack (one lane per dim, blind) is replaced
  by `drawing_gate.pack_lanes`: dims are ordered smallest-span-first and each takes
  the lowest lane whose occupants its footprint (lines + label box) does not
  overlap. Disjoint dims SHARE a lane (compact — fewer lanes pushed off-sheet or
  across views); an overall extent that spans nested feature dims overlaps them all
  and is pushed outward, so the smallest-inside / overall-outside nesting falls out
  for free. Placement now flows through one `_iter_placed_dims` consumed by both the
  renderer and the legibility gate, so the gate scores exactly what is drawn. A
  four-hole chain-dimensioned bar stays legible on one lane where the blind stack
  would sprawl four lanes out (`test_packing_keeps_dense_part_legible`). Pure tests:
  `test_pack_lanes_*`.

* **A3 — title block.** DONE 2026-06-15. FreeCAD's default A4 template is a BARE
  sheet (no frame, no title block — which is why drawings exported blank), so
  AnkusDrive composes its own bottom-right block (`_title_block_svg`): scale, sheet
  size, units and part name auto-derived from the page; material / rev / drawn-by /
  date / project supplied via `set_title_block` (stamped `AD_TitleBlock` JSON, MCP
  tool). Opt-in (rendered only once `set_title_block` is called). Its box is a
  keep-out in the legibility gate, so a dim line crossing it is flagged. Demo
  artifacts (`artifacts/drawings/{plate,lbracket}_demo.pdf`) now carry it. Test:
  `test_title_block_renders_fields`.

* **A2+ — leaders (DONE 2026-06-15).** Ø/R dimensions now render as leader callouts
  — an arrow touching the hole/arc and the value stacked in open space beside the
  view (`_leader_layout`) — instead of linear-stacked dimensions. This is the
  conventional hole callout and exactly "a label placed away from its feature with a
  leader"; it also frees the linear lanes for true length/location dims.
  `_iter_placed_dims` splits leaders from linear dims; the legibility gate scores
  both. `test_diameter_leaders_legible` stacks two and stays clean.

Still open, in cost order:
* **Regression set.** DONE 2026-06-15 (`tests/test_drawing_legibility_regression.py`):
  a high-aspect bar, a hole grid, a small part, and a tight chain-dimensioned cluster
  are each driven through the real pipeline and asserted clean by the legibility gate
  — so a packing change that starts overlapping or overflowing trips here. It also
  pinned the motivating case for A3+: a 120×80 plate at 1:1 pushes its Top-view dims
  ~3 mm off the A4 sheet, and the gate flags `out_of_border` rather than shipping it
  (`test_oversize_part_flags_overflow`).
* **A3+ — auto-fit (DONE 2026-06-15).** `fit_page` recentres the views so the part +
  its dimension envelope sit inside the printable border (clear of the title block);
  the oversize and giant regression cases now go clean after it. The projection
  group's Automatic scale sizes the part; `fit_page` deliberately does NOT rescale
  (a dimension's points are baked at creation, so changing Scale afterwards would
  misalign them). This slice also fixed a latent bug: `viewPartAsSvg` emits geometry
  PRE-SCALED by `view.Scale`, but `_project_centred` returned unscaled coords —
  invisible at the usual Scale=1, but it misplaced every dimension once a view was
  auto-reduced to fit. `_project_centred` now scales to match.

## Drawings-next — pictorial + auto cross-section (DONE 2026-06-15)

Two readability touches a machinist expects, built on the same TechDraw primitives
the rest of the path uses (so both stay vector line drawings, decided automatically):

* **Top-right isometric pictorial (`add_thumbnail`).** A real isometric
  `DrawViewPart` (Direction (1,1,1), XDirection (1,−1,0)) rendered through the very
  same `viewPartAsSvg` the orthographic views use — a vector line drawing, not an
  embedded raster — scaled to a reserved top-right box (the mirror of the bottom-right
  title block) and pinned there (`AD_Thumbnail`; `_page_main_view` keeps it out of the
  title block / scale / gate, `_page_top_views` keeps `fit_page` from dragging it off
  its corner). Best-effort: skips with `placed: False` when the corner is occupied.
  Crucial fix: occupancy is tested **element-wise** (`_region_is_clear`), not against
  the union bounding box — the bottom-right title block alone stretches that bbox
  across the whole sheet and would read the empty corner as full.
* **Auto cross-section (`add_section_view`).** `drawing_gate.needs_section` decides
  from the enumerated features whether internal geometry the outline views convey
  ambiguously is present — a counterbore (stepped bore), a blind hole/bore, or a
  pocket; a plain through-hole does NOT trigger one. When recommended (and surfaced
  advisory-only as `section_recommended` on the gate report), a `DrawViewSection` is
  cut lengthwise through the feature (normal ⊥ the feature axis, origin at its
  centre) and placed in genuinely clear space (grid scan via `_region_is_clear`,
  nearest the base view). `auto=False` forces one. Golden: `artifacts/drawings/cbblock_demo.*`
  (Front + Top + auto Section showing the bore profile + iso thumbnail). Tests:
  `tests/test_drawing_thumbnail_section.py` (e2e) + `needs_section` cases in
  `tests/test_drawing_gate.py`.

## Anchors

- Pure core: `ankusdrive/drawing_gate.py` (incl. `needs_section`). Tests:
  `tests/test_drawing_gate.py`, `tests/test_drawing_gate_worker.py`,
  `tests/test_drawing_thumbnail_section.py`.
- Worker thumbnail/section: `_h_add_thumbnail`, `_h_add_section_view`,
  `_thumbnail_box`, `_region_is_clear`, `_place_view_outline_at`, `_section_cut`,
  `_place_section`, `_page_main_view`, `_is_thumbnail`/`_is_section`.
- Worker: `_dim_layout`, `_h_drawing_gate`, `_h_drawing_legibility`,
  `_enumerate_features`, `_infer_process`, `_dim_descriptors`, `_page_dim_graphics`,
  `_stamp_model_ref` in `ankusdrive/worker.py`.
- MCP: `drawing_gate`, `drawing_legibility` in `ankusdrive/mcp_server.py`.
- Gate pattern this mirrors: `ankusdrive/realize.py`,
  `docs/KICKOFF_validate_the_artifact.md`.

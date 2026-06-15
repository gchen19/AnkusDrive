# Kickoff — Drawing-is-manufacturable + legible gates (issue #85, the "Next layer")

Written 2026-06-15. The TechDraw export work (PR #80,
`docs/KICKOFF_techdraw_export.md`) shipped the **renderer + dimension palette**:
the agent can place any extent / linear / Ø / R dimension and get a clean,
true-valued (DP_TrueValue) drawing out. It deliberately stopped short of two
things, both tracked by issue #85:

1. **Legibility** is heuristic (offset-stacking) and never validated.
2. **Manufacturing completeness** — nothing checks whether the chosen dimensions
   are *enough to make the part*.

> **Status (2026-06-15): both gates shipped.** A green render is not a
> manufacturable drawing — the same failure mode the geometry-realizes-declaration
> gate (`driftpin/realize.py`, §11.10) closed for assemblies. These are that gate,
> one layer up: validate the *drawing*. The placement *rewrite* (A2/A3 below) is
> still open — these gates make it measurable first.

## What shipped

`driftpin/drawing_gate.py` — pure, FreeCAD-free, unit-tested
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
* `_h_add_dimension` now stamps **`DP_ModelRef`** (the model-space circle/span a dim
  references) on each manual dim, so the completeness gate can tell *which* feature
  DOF a dim pins — a Ø on hole A vs the X location of hole B — which the projected
  2-D dim alone cannot recover.
* `_h_drawing_gate` enumerates features off the real `Part.Shape` (bounding box;
  inward cylinders grouped coaxially so a counterbore is recognised; outer
  cylindrical steps for a turned part), infers `process` (or takes an override),
  builds dim descriptors (kind via DP_Prefix, value via DP_TrueValue, ref via
  DP_ModelRef), and calls `completeness_report`.
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

* **True-valued dims rarely "conflict".** Because every DriftPin dim reads the real
  solid, two dims on one DOF normally *agree* → `redundant`. `conflict` exists for
  manually-overridden values; it binds by reference (the worker's resolved refs /
  DP_ModelRef), not by value, so a wrong number still lands on its slot.
* **Datums default off.** `datums_declared=False` until the `DP_FaceRoles`
  (`annotate_face`) datum hookup lands; the `no_datum` logic is wired and unit-tested
  but won't false-positive in the meantime. Next: read the datum face, set each
  location dim's `from_datum` by whether an endpoint sits on it.
* **Enumeration is conservative.** An unrecognised face yields *no* slot rather than
  a wrong one (no false `under`). Chamfers/fillets/threads/tolerances are not yet
  enumerated; `gdt_check` / `tolerance_stackup` / `fit_class` are the hooks for the
  GD&T layer.

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

Still open, in cost order:

* **A2+ — leaders.** When even its own lane can't fit a label at its feature, emit
  `makeLeader` into open space. (Packing handles the common dense case; leaders are
  the overflow valve for pathological clusters.)
* **A3 — auto sheet/scale + title block.** Pick A4/A3 and a scale so the part + its
  dim envelope fit the border; fill title-block fields from
  `mass_properties`/`material_get`/`bom_extract`.
* **Regression set** — golden artifacts spanning the stress cases (many holes, high
  aspect ratio, tight clusters, tiny features), each asserted clean by A1.

## Anchors

- Pure core: `driftpin/drawing_gate.py`. Tests: `tests/test_drawing_gate.py`,
  `tests/test_drawing_gate_worker.py`.
- Worker: `_dim_layout`, `_h_drawing_gate`, `_h_drawing_legibility`,
  `_enumerate_features`, `_infer_process`, `_dim_descriptors`, `_page_dim_graphics`,
  `_stamp_model_ref` in `driftpin/worker.py`.
- MCP: `drawing_gate`, `drawing_legibility` in `driftpin/mcp_server.py`.
- Gate pattern this mirrors: `driftpin/realize.py`,
  `docs/KICKOFF_validate_the_artifact.md`.

# AnkusDrive docs — index

Orientation for the `docs/` tree. The top-level [`README.md`](../README.md)
describes what AnkusDrive *is*; these docs go deep on individual subsystems and
record the design decisions behind shipped work.

Docs fall into two kinds:

- **Living reference** — describes current behavior; keep it accurate as the
  code changes.
- **Design record (shipped)** — a kickoff/plan/scoping doc for work that has
  since landed. Kept as a decision/rationale record (and referenced from code
  and tests), not a to-do list. Don't read these as a description of what's
  *missing* — check git log / the README for current state.

---

## Living reference

| Doc | What it covers |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | Per-slice changelog + remaining backlog. Companion to the README. |
| [`DESIGN_HIERARCHY.md`](DESIGN_HIERARCHY.md) | The design-control / PLM layer — recipes, relations, families, items, lifecycle, ECO/change, interface registry, projects. Scoping + rationale. |
| [`MULTI_AGENT.md`](MULTI_AGENT.md) | Agent-team partition + merge architecture; the merge primitives and gates. |
| [`SIMULATION_TOOLS.md`](SIMULATION_TOOLS.md) | The analysis-family catalog + result schemas (pure-Python oracle vs external-solver split). |
| [`SIMULATION_EXAMPLES.md`](SIMULATION_EXAMPLES.md) | Per-family proof harness (toy problems with known answers). |
| [`SIMULATION_NEXT.md`](SIMULATION_NEXT.md) | Forward-looking assessment of the next simulation work. |
| [`MOLDING_FILL_SOLVER.md`](MOLDING_FILL_SOLVER.md) | Injection-molding fill/pack/cool/warp solver reference + gotchas. |
| [`RENDERING.md`](RENDERING.md) | Render support matrix, install, limitations. |
| [`RENDER_WORKBENCH.md`](RENDER_WORKBENCH.md) | Photoreal rendering architecture + operator guide. |
| [`RENDER_RENDERER_INSTALL.md`](RENDER_RENDERER_INSTALL.md) | Per-renderer provisioning detail (6 backends). |
| [`RENDER_TEXTURE_CHECK.md`](RENDER_TEXTURE_CHECK.md) | Manual QA runbook for POV-Ray textures. |
| [`PUBLISHING_PLAN.md`](PUBLISHING_PLAN.md) | PyPI publishing plan (Phase A done; B/C optional). |

## Design records (work shipped)

Archived under [`archive/`](archive/) — kickoff/plan/scoping docs whose work has
landed. Useful for *why* something was built the way it was; not a description of
current gaps.

| Doc | Shipped work it scoped |
|---|---|
| [`PHASE_2_PLAN.md`](archive/PHASE_2_PLAN.md) | Core mechanical-design surface (PartDesign, FEM decomposition, multi-doc, transactions). |
| [`SIMULATION_SPRINTS.md`](archive/SIMULATION_SPRINTS.md) | The P0–P3 simulation tiers (marked ✅ complete). |
| [`SIMULATION_P2_KICKOFF.md`](archive/SIMULATION_P2_KICKOFF.md) | P2 solver provisioning. |
| [`SIMULATION_P3_KICKOFF.md`](archive/SIMULATION_P3_KICKOFF.md) / [`SIMULATION_P3_GEOMETRY_DRIVEN.md`](archive/SIMULATION_P3_GEOMETRY_DRIVEN.md) | P3 geometry-driven solves + optics. |
| [`ADVANCED_TOYS_KICKOFF.md`](archive/ADVANCED_TOYS_KICKOFF.md) / [`ADVANCED_TOYS_PLAN.md`](archive/ADVANCED_TOYS_PLAN.md) | The advanced toy-problem batches. |
| [`KICKOFF_techdraw_export.md`](archive/KICKOFF_techdraw_export.md) | Headless TechDraw PDF/SVG/DXF export. |
| [`KICKOFF_drawing_manufacturable.md`](archive/KICKOFF_drawing_manufacturable.md) | Drawing completeness + legibility gates. |
| [`KICKOFF_validate_the_artifact.md`](archive/KICKOFF_validate_the_artifact.md) | The validate-the-artifact oracle + sim-from-CAD items. |
| [`KICKOFF_simulation_video_capture.md`](archive/KICKOFF_simulation_video_capture.md) | Review-video capture (motion → GIF, modal/thermal/CFD fields). |
| [`KICKOFF_molding_pack_cool_warp.md`](archive/KICKOFF_molding_pack_cool_warp.md) | Molding packing/cooling/warpage stages (see `MOLDING_FILL_SOLVER.md` for the live reference). |
| [`DESIGN_TO_SPEC_KICKOFF.md`](archive/DESIGN_TO_SPEC_KICKOFF.md) | Epic #222, closed 2026-08-03 — wind tunnel, trust layer, performance contracts. Also carries the CFD gotchas worth not rediscovering. |
| [`VALIDATE_THE_ARTIFACT.md`](archive/VALIDATE_THE_ARTIFACT.md) | RFC §11.10 geometry-realizes-declaration, delivered — the companion record to `KICKOFF_validate_the_artifact.md`. |
| [`AIRTIGHT_INVARIANTS_PLAN.md`](archive/AIRTIGHT_INVARIANTS_PLAN.md) | Issue #19, closed — functional-invariant checks for enclosed-flow parts (`declare_intent`/`verify_intent`). |

> **Note:** several of these are cited from source code and tests (e.g.
> `ankusdrive/realize.py`, `ankusdrive/solvers.py`, `ankusdrive/worker.py`, `tests/`)
> via their `docs/archive/…` path. If you relocate one again, update the
> referencing code in the same change.

## Galleries

`render_gallery*.png` / `render_renderers_gallery.png` are referenced by the
`RENDER*` docs above as inline examples.

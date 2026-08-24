# P3 plan — from canonical cases to *real geometry*

P2 ([`SIMULATION_P2_KICKOFF.md`](SIMULATION_P2_KICKOFF.md)) stood up the heavy
external solvers — OpenFOAM 1912 and Elmer v26.2 are provisioned on the runner, run
real solves, and are oracle-gated end-to-end. **P3 closes the remaining gap: those
solvers today solve *canonical parametric cases* (a straight pipe, a 1‑D slab), not
the user's actual part.** This phase makes every solver consume an arbitrary FreeCAD
solid, maps results back onto its faces, and lands the two families still on the
shelf (optics, DfX‑v2).

Companion docs:
- [`SIMULATION_TOOLS.md`](../SIMULATION_TOOLS.md) — family catalog + result schemas.
- [`SIMULATION_EXAMPLES.md`](../SIMULATION_EXAMPLES.md) — the closed-form acceptance toy per family.
- [`SIMULATION_SPRINTS.md`](SIMULATION_SPRINTS.md) — sprint sequence (this doc adds Sprints 10–14).

---

## Assessment of the current P2 work — does it look good?

**Yes — the integration is solid and the discipline is right.** Verified this review:

- **Every fast-lane oracle is green** — `cfd` 4, `thermal` 8, `vibration` 8,
  `topology` 8, `kinematics` 8, plus `openfoam`/`elmer` structure suites 7 each, the
  `solve_degradation` contract 7, and `contracts` 10/10. Solver-execution paths
  degrade to `{ok:false, reason, install}` cleanly with no binary present.
- **Physics is correct where it's checked.** OpenFOAM pipe Δp matches
  Hagen–Poiseuille (`hp_ratio ≈ 1.00`, < 3 %) and holds the D⁴ scaling law; Elmer
  transient matches the Heisler 1‑D analytic to < 0.1 % and the lumped limit at small
  Biot. The axisymmetric-wedge pipe and native-Elmer slab are textbook-clean.
- **Architecture is consistent** — FreeCAD-free `analysis/` modules, the
  `*_submit → jobs.py → job_result` async contract, `solve_capabilities` discovery,
  content-hash caching. It mirrors the renderer pattern faithfully.

**Caveats to carry into P3 (none are defects — they're the scope edge):**

1. **Canonical-case-only.** `cfd_internal_flow_submit` builds a *pipe* from
   `diameter/length/velocity`; `thermal_transient_submit` builds a *slab*. Neither
   meshes an exported STEP/STL of a real part yet. This is the P3 spine.
2. **`cfd_external_flow_submit` is effectively a "run a prepared case_dir" stub** —
   there is no FreeCAD→surface-mesh→snappyHexMesh pipeline behind it. It should be
   labelled as such until Sprint 10 lands.
3. **Pressure is scraped from the `p` field** (the `surfaceFieldValue` writer is
   broken in this OpenFOAM build). Fine, but fragile across versions — pin the
   OpenFOAM version and add a `checkMesh`/residual assertion to the gate.
4. **Single mesh resolution.** Gates run at one mesh density; there's no
   mesh-independence check. Add a 2-point Richardson check so a too-coarse mesh can't
   silently pass.
5. **Elmer is 1‑D slab only** — the *radiation* and 3‑D-conduction gaps from family 4
   are still open.

---

## The P3 through-line: a geometry → mesh → solve → map pipeline

Every P3 solver family needs the same new spine, which P2 deliberately deferred:

```text
FreeCAD solid (handle)
  → export        STEP (Elmer/structural) | STL (OpenFOAM surface)     [main thread]
  → mesh          Gmsh → ElmerGrid  |  surfaceFeatureExtract → snappyHexMesh
  → boundary tag  map exported face tags → solver patches/boundaries
  → solve         jobs.py background subprocess (P2 contract, unchanged)
  → parse + map   field → typed dict AND scalar-per-face back onto the FreeCAD shape
```

Two new cross-cutting tools fall straight out of it and unlock everything else:

- **`mesh_solid`** — export + volume/surface mesh of a handle with a quality report
  (`{n_cells, min_quality, non_orthogonality_max, n_boundary_patches, mesh_path}`).
  One meshing entry point both CFD and thermal/structural call.
- **`map_field_to_faces`** — parse a solver field (VTK/`.vtu` via `meshio`) and
  reduce it to per-face scalars (`{face_tag: {max, mean}}`) so results become
  **face-addressable gates** ("max temp on `f_chip_seat` ≤ 85 °C") and feed the
  existing render pipeline as a colour map.

---

## Tooling to acquire (the only new external pieces)

| Tool | For | Install | Notes |
|---|---|---|---|
| **Gmsh** | volume mesh STEP→Elmer/CCX | `pip install gmsh` (wheel, bundles binary) | also has a Python API; cleanest STEP mesher |
| **snappyHexMesh + surfaceFeatureExtract** | CFD mesh from STL | ships with OpenFOAM (already provisioned) | no new install — just wire it |
| **`meshio`** | parse `.vtu`/`.foam`/Elmer output → fields | `pip install meshio` (pure-Python) | the field→face mapper reads this |
| **`cfMesh`** *(optional)* | alternative CFD mesher | with OpenFOAM | fallback if snappy is fiddly on a part |
| **PrusaSlicer / OrcaSlicer CLI** | real slice (Sprint 13) | apt / AppImage | upgrades the analytic `slice_estimate` |
| **`rayoptics` / `optiland`** *(optional)* | general lenses beyond `~/diffuser` | `pip` | optics corpus already shipped |

Net new heavy deps: **just Gmsh and meshio** (both pip), plus wiring snappyHexMesh
which is already on the box. Add a `ankusdrive[mesh]` extra (`gmsh`, `meshio`) and a
`mesh` section to `scripts/install-solvers.sh`.

---

## Sprints

Ordered by leverage. Sprints 10–11 are the spine; everything after rides on it.

### Sprint 10 — Geometry-driven CFD  *(the headline unlock)*
- **Goal:** "pressure drop through *this* manifold / drag on *this* housing," not a pipe.
- **Build:** `mesh_solid` (STL surface + snappyHexMesh) → extend
  `cfd_internal_flow_submit`/`cfd_external_flow_submit` to accept a `model` handle,
  auto-build the case, tag inlet/outlet/wall from face tags, solve, parse.
- **Tools:** OpenFOAM (have) + snappyHexMesh (have) + `meshio`.
- **Gate:** **relative** to the canonical pipe (already passing) re-run through the
  *snappy* path — the meshed straight pipe must still hit Hagen–Poiseuille ±10%; a
  **backward-facing step** reattachment length (≈7·step height, well-known) and a
  **sphere drag** Cd≈0.47 (Re~10³) are the shape-aware absolute checks. Mesh-
  independence: refine once, Δp must move < 5%.
- **Why first:** turns the entire CFD family from demo into product; proves the
  meshing spine the thermal family reuses.

### Sprint 11 — Geometry-driven thermal + radiation
- **Goal:** transient/steady conduction + convection + **radiation** on a real solid.
- **Build:** `mesh_solid` (Gmsh STEP→tet) → ElmerGrid → extend
  `thermal_transient_submit` to take a `model` handle + face-tagged BCs; add
  `thermal_radiation` (Elmer view-factor / `Heat Gap` radiation) — the open family-4 item.
- **Tools:** Gmsh + ElmerGrid (have) + Elmer (have).
- **Gate:** relative to `thermal_lumped` at small Biot (must agree), absolute vs the
  1‑D slab (have) on a box mesh, and a **two-surface radiation** closed form
  (q = εσA(T₁⁴−T₂⁴)) for the radiation path.

### Sprint 12 — Result-field → geometry (mapping + viz)  *(cross-cutting)*
- **Goal:** make solver output face-addressable and visual.
- **Build:** `map_field_to_faces` (meshio parse → per-face max/mean) + a colour-mapped
  `render_view` overlay of the field on the part.
- **Tools:** `meshio` + existing render pipeline.
- **Gate:** on the canonical pipe, per-face wall-shear max matches the analytic τ_w;
  the mapped peak equals the dict's `max_*`. Unlocks merge gates like "max von Mises
  on `f_fillet` ≤ limit."

### Sprint 13 — Optics (Sprint 5 carried forward) + slicer CLI upgrade
- **Optics:** wrap the in-house `~/diffuser` pipeline behind `optics_raytrace` /
  `optics_moldability_check`; the **optical corpus already shipped** (family 2,
  `refractive_index_at`). Gate: Snell 30°→19.6°, Fresnel 3.9%, energy closes ±1%,
  TIR above θc=42.2° (all already written in `SIMULATION_EXAMPLES.md` §7).
- **Slicer:** real PrusaSlicer/OrcaSlicer CLI on exported STL behind `ankusdrive[slice]`,
  gated *relative* to the shipped analytic `slice_estimate` (mass/volume must agree;
  CLI adds real supports + travel/accel). Missing CLI → graceful dict.

### Sprint 14 — Closure & polish (small, high-ROI)
- `fit_class` **interference letters** (p, s, …) — the documented tolerance v2 gap.
- **DfX v2 geometry wiring** — `dfm_check`/`pack_check`/`cost_estimate` read draft/
  thickness/bbox/volume off a `Shape` via `draft`/`thickness`/`query_faces`/
  `mass_properties` (today explicit-input v1).
- **In-process pure-Python fast-path** — let `material_*`, rating, tolerance, etc.
  answer without the `freecadcmd` round-trip (latency; noted in `SIMULATION_SPRINTS.md`).
- **Mesh-independence gate helper** — 2-point Richardson check, reused by Sprints 10–11.

### Horizon (table-only, unchanged priority)
| Domain | Tooling | Note |
|---|---|---|
| Acoustics | **Elmer (already provisioned)** — Helmholtz | cheapest horizon item; reuses the mesh spine |
| Electromagnetics | OpenEMS / Elmer / FEMM | low |
| Machining toolpaths | FreeCAD Path | low |

---

## Per-sprint definition of done (unchanged from P2)

1. `analysis/<module>.py` (or extension) lands, FreeCAD-free where possible.
2. `*_submit` wired through `jobs.py`; **never touches FreeCAD on the background thread**.
3. Graceful degradation: solver/mesher absent → `{ok:false, reason, install}`, gated by
   a no-binary CI test (extend `tests/test_solve_degradation.py`).
4. The two-sided toy from `SIMULATION_EXAMPLES.md` promoted into the gate harness;
   **relative gate** for solver output, absolute only where the physics is exact.
5. `tests/test_contracts.py` green (every tool↔handler, documented return).
6. Heavy solves run only on the provisioned runner; fast lane stays solver-free.

---

## Recommended first PR

**Sprint 10's `mesh_solid` + the snappy-meshed straight pipe.** It adds the one
capability the whole phase pivots on (export → mesh → solve a *meshed* geometry), and
its acceptance is free: the existing Hagen–Poiseuille gate must still pass when the
pipe is reached through the snappyHexMesh path instead of the hand-built wedge. Land
`meshio` + the `ankusdrive[mesh]` extra alongside it.

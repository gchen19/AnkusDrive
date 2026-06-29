# Kickoff — finishing the catalog (optics) + the geometry-driven solve tier

The P2 heavy-solver tier shipped: CFD, multibody dynamics, topology, transient thermal,
contact, and random vibration are all live and oracle-gated
([`SIMULATION_P2_KICKOFF.md`](SIMULATION_P2_KICKOFF.md), M0–M5 + both follow-ons). This
doc kicks off **the next wave**, which has two halves:

1. **Finish the catalog.** Of the families specced in
   [`SIMULATION_EXAMPLES.md`](../SIMULATION_EXAMPLES.md) (§0–§9) and
   [`SIMULATION_SPRINTS.md`](SIMULATION_SPRINTS.md) (Sprints 1–9), exactly one is
   unshipped — **Optics (Sprint 5 / §7)** — plus two *partial* gaps inside families we
   marked "done": **radiation thermal** (`thermal_radiation`, specced in Sprint 9, never
   built) and the **external-flow case builder** (`cfd_external_flow_submit` exists but
   only runs a *prepared* case).
2. **Promote the heavy solves from toys to parts.** Every P2 solve today rides on
   *parametric/analytic* geometry — a 1-D slab (Elmer), an axisymmetric pipe (OpenFOAM),
   a 2-D grid (SIMP). The unshipped follow-on the P2 kickoff itself flagged is the
   **geometry-driven meshing bridge**: mesh an *arbitrary FreeCAD solid* → solve → map
   results back. That one enabler turns every heavy family into a real engineering tool
   (3-D transient thermal on a real heatsink, internal flow through a real manifold,
   external aero on a real body, 3-D topology under real load cases).

It complements:
- [`SIMULATION_TOOLS.md`](../SIMULATION_TOOLS.md) — family catalog + result schemas.
- [`SIMULATION_EXAMPLES.md`](../SIMULATION_EXAMPLES.md) — the closed-form **acceptance toy** per family.
- [`SIMULATION_SPRINTS.md`](SIMULATION_SPRINTS.md) — sprint sequence (this is **Sprint 5 + Sprints 10–12**).

---

## Where we are

**Shipped:** the materials DB, the entire pure-Python closed-form wave (tolerance,
durability, lumped thermal, DfX, cost, slicing, machine-element rating), the async
facility ([`driftpin/jobs.py`](../../driftpin/jobs.py)), and the **complete P2 tier** —
[`analysis/{cfd,mbd,kinematics,topology,vibration,elmer,openfoam}.py`](../../driftpin/analysis/)
with their `*_submit` tools, the [`solvers.py`](../../driftpin/solvers.py) discovery +
degradation glue, and the case-builders that build a slab/pipe from physical params,
run **the real ElmerSolver / OpenFOAM**, and gate against the analytic oracle.

**Missing — four things, none of them new infrastructure:**
1. **Optics (Sprint 5).** `optics_raytrace` + `optics_moldability_check`. The `optics`
   extra and the `rayoptics`/`optiland` solver are **already registered**
   ([`solvers.py`](../../driftpin/solvers.py), [`pyproject.toml`](../../pyproject.toml)); the
   optical material corpus is in
   [`analysis/materials/optical.json`](../../driftpin/analysis/materials/optical.json)
   (PMMA n=1.49062, etc.). Snell + Fresnel + TIR are **exact** closed-form anchors. This
   is the lowest-risk, most de-risked item — **ship it first.**
2. **Radiation thermal.** `thermal_radiation` (Elmer's enclosure/view-factor radiation),
   the sibling of the shipped `thermal_transient`. Exact two-surface anchor.
3. **External-flow CFD builder.** A from-geometry builder behind
   `cfd_external_flow_submit` (drag/lift), the counterpart to the shipped internal-pipe
   builder. Needs a new analytic oracle (Stokes / flat-plate) in
   [`analysis/cfd.py`](../../driftpin/analysis/cfd.py).
4. **The geometry-driven meshing bridge.** The cross-cutting unlock (see below).

---

## The contract every family follows (unchanged)

Reused verbatim from the P2 tier and `jobs.py`:

1. **Compose + export geometry on the main thread** (STEP/STL/mesh out of FreeCAD).
2. **`<family>_submit`** builds a content key, calls `jobs.submit(...)`, returns
   `{job_id, status, cache_hit}` immediately.
3. **The background `fn` runs ONLY the solver subprocess** — never touches FreeCAD
   (the `jobs.py` threading contract).
4. **Poll** with the shared `job_status` / `job_result`.
5. **Parse solver output → a small typed dict on the main thread.**
6. **Degrade + discover + cache:** a missing solver/wheel returns
   `{ok:false, reason, install}` (never an import crash); `solve_capabilities` reports
   what resolves now; identical inputs hit the cache.

Plus the pattern proven across all of P2: a **pure-Python analytic core** in
`analysis/` that is exact and fast-lane gated (the "toy"), with the heavy solver only on
the provisioned runner. Optics keeps this exactly (Snell/Fresnel are the core); the
geometry bridge adds one new idea — **relative gates** (mesh-from-FreeCAD must reproduce
the parametric result), since an arbitrary mesh has no closed form.

---

## Per-item kickoff

Ordered by ascending weight — **do the catalog-completing, no-new-solver wins first.**

| Item | Backend | Install | Tool(s) | Analytic toy |
|---|---|---|---|---|
| Optics | rayoptics/optiland (or `~/diffuser`) | `pip` (`optics` extra) | `optics_raytrace`, `optics_moldability_check` | Snell 19.6°, Fresnel 3.9%, TIR θc=42.2° |
| Radiation thermal | Elmer (already provisioned) | — | `thermal_radiation` | 2-plate exchange q=σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1) |
| External CFD | OpenFOAM (already provisioned) | — | `cfd_external_flow_submit` (builder) | Stokes Cd=24/Re; flat-plate Cf=1.328/√Re |
| Geometry bridge | Gmsh + ElmerGrid / snappyHexMesh | — | (extends every `*_submit`) | relative: mesh-from-FreeCAD == parametric |
| 3-D topology | in-house SIMP (3-D) | — | `topology_optimize_submit` (3-D mode) | 3-D MBB; mass ≤ keep_fraction; keep-outs |

### 1. Optics — finish the catalog (Sprint 5, §7)
`optics_raytrace(model, source_config, n_refractive, n_rays)` and
`optics_moldability_check(model, pull_axis)`. The **core is pure-Python and exact**: a
single flat PMMA (n=1.49) interface at 30° must refract to asin(sin30°/1.49) = **19.6°**,
the normal-incidence Fresnel reflectance is ((1.49−1)/(1.49+1))² = **3.9%**, the critical
angle is asin(1/1.49) = **42.2°** (above it, *zero* transmission), and rays must conserve
energy (`leakage + efficiency + absorbed ≈ 1.0` within 1%). Put Snell + Fresnel + TIR in
`analysis/optics.py` as the fast-lane oracle; back the full diffuser/lens trace with the
`rayoptics` wheel (the `optics` extra) behind `_require_solver('rayoptics')`, degrading
cleanly when absent. `optics_moldability_check` is **geometric** (draft-angle + undercut
analysis vs `pull_axis`) and reuses the existing draft/DfM face machinery — a re-entrant
feature must populate `undercut_faces`. **Note:** the `~/diffuser` pipeline the sprint
plan named is *not* present on this box, so the rayoptics wheel is the portable backend;
optical n vs wavelength comes from `optical.json` (add a Sellmeier eval if dispersion is
needed). **Recommended first PR — light dep, exact oracle, completes §0–§9.**

### 2. Radiation thermal — close the Sprint 9 gap
`thermal_radiation(analysis, …)` via Elmer's radiation solver (gray-body, view factors).
The exact anchor is the **two infinite parallel plates** enclosure: net flux
q = σ(T₁⁴−T₂⁴)/(1/ε₁ + 1/ε₂ − 1). Build the trivial two-surface case, run Elmer, gate the
solved exchange against that closed form; and a **relative** gate vs `thermal_lumped`'s
radiation screen (`h_rad`) at small ΔT. Same `analysis/elmer.py` + `thermal_*_submit`
wiring as the shipped transient slab.

### 3. CFD external flow — the builder counterpart
A from-geometry builder behind the existing `cfd_external_flow_submit` (it currently only
runs a prepared `case_dir`). The unambiguous external anchors are **Stokes drag on a
sphere** at Re≪1 (Cd = 24/Re, F = 6πμRV — exact) and the **laminar flat plate**
(Blasius Cf = 1.328/√Re_L). Add these to `analysis/cfd.py` as the oracle, build the
external domain (a body in a far-field box, `snappyHexMesh` around an STL — see the
bridge below), run simpleFoam, integrate the surface force, and gate Cd against
Stokes/Blasius within 10–15%.

### 4. Geometry-driven meshing bridge — the real unlock 🔴
The deep follow-on. Today the solves mesh *parametrically*; this meshes an **arbitrary
FreeCAD solid**:
- **Elmer path:** FreeCAD solid → Gmsh (the worker's existing `femmesh.gmshtools`,
  already used by `fem_mesh`) → UNV/`.msh` → **ElmerGrid** converts to the Elmer mesh →
  solve. (Gmsh meshing is verified to work locally; ElmerGrid is provisioned.)
- **OpenFOAM path:** FreeCAD solid → STL → `snappyHexMesh` inside a `blockMesh`
  background box → solve.
Verification is **relative** (an arbitrary mesh has no closed form): mesh a *box* slab or
a *cylinder* pipe **from FreeCAD geometry**, solve, and require the result to match the
parametric slab/pipe builder (and thus the analytic oracle) within mesh tolerance. Once
this lands, 3-D transient thermal on a real part, internal flow through a real duct, and
external aero all become single `*_submit` calls on a model handle.

### 5. 3-D topology — rides on the bridge
Promote the 2-D SIMP to **3-D** (8-node hex elements) and drive it from **real FreeCAD
load cases + keep-out regions** instead of the fixed 2-D cantilever; reconstruct with the
shipped `topology_to_solid` (extended to a 3-D voxel field). Gate geometrically (mass ≤
keep_fraction·envelope, `interference_check` vs keep-outs) on the canonical 3-D MBB beam.

### 6. Frontier (optional) — new physics beyond §0–§9
Elmer/OpenFOAM already on the runner open three genuinely new families: **modal /
harmonic response & vibroacoustics** (Elmer elasticity + the existing `fem_modal`),
**conjugate heat transfer** (OpenFOAM `chtMultiRegionFoam` / coupled Elmer — solid+fluid
thermal), and **low-frequency electromagnetics / induction heating** (Elmer's EM
solvers). Each would follow the same oracle-gated pattern; scope as a later tier.

---

## Milestones

```
M0  Provisioning   ⏳ mostly reused — solve_capabilities · the optics extra · the
                       degradation contract already exist. New: verify the rayoptics
                       wheel resolves; add the radiation + external-flow + Stokes oracles.
M1  Optics         ✅ analysis/optics.py (Snell/Fresnel/TIR oracle + energy-closing
                       trace) + optics_raytrace (rayoptics, degrades) +
                       optics_moldability_check (geometric: draft + ray-cast undercut).
                       Gated: Snell 19.60°, Fresnel 3.88%, TIR θc 42.13°, energy Σ=1;
                       rayoptics matches Snell to <1e-6°. Example D + optics.png.
M2  Radiation thermal  ✅ thermal_radiation_submit (Elmer diffuse-gray enclosure +
                       ViewFactors) vs the 2-plate σ(T⁴)/(1/ε₁+1/ε₂−1) exchange —
                       oracle_ratio ~0.998 (sym + asym ε); analysis/thermal.radiation_exchange
                       + analysis/elmer.write_radiation_plates_case. Example E + radiation.png.
M3  CFD external   ✅ cfd_external_flow_submit flat-plate builder (blockMesh+simpleFoam,
                       clean LE) + Stokes/Blasius drag oracle (analysis/cfd.py). Drag
                       read from the U field (force function objects abort 'sha1' here);
                       blasius_ratio ~1.09 (within 15%) + U^1.5 law. Example F + external.png.
M4  Geometry bridge ✅ analysis/meshbridge.py + body modes on thermal_transient_submit
                       (body, convection_faces) and cfd_internal_flow_submit (body,
                       inlet_face/outlet_face). Elmer path: FreeCAD solid → Gmsh UNV
                       (face groups → boundary tags, tag i == Faces[i-1]) → ElmerGrid →
                       HeatSolver; gated vs Heisler (box: 0.2–1.5%). OpenFOAM path:
                       per-face STL regions → blockMesh box → snappyHexMesh → simpleFoam;
                       gated vs Hagen–Poiseuille on the developed Δp (cylinder:
                       hp_ratio 1.0006). Example G + bridge.png.
M5  3-D topology   ✅ simp_topology_3d (trilinear hexes; scipy/dense/PCG backends) +
                       point loads / fixed_nodes / keep_out / keep_in element boxes →
                       topology_optimize_submit(nelz=…) → topology_to_solid consumes
                       the voxel field (density_to_boxes greedy merge). Gated: volume
                       exact, z-symmetry, keep-outs void, uniform cantilever within
                       the Euler-Bernoulli+Timoshenko band. (FreeCAD-extracted load
                       cases ride on the M4 bridge.)
M6  Frontier       ✅ modal: beam_modal exact Euler-Bernoulli oracle + fem_modal
                       (CalculiX, 2nd-order tets) gate — fundamental within ~0.5% of
                       E-B; fem_mesh element_order added. Example H + modal.png.
                       CHT: analysis/cht.py — thermal_composite_wall exact network +
                       cht_channel_submit (one Elmer solve, coupled plug-flow fluid +
                       solid wall) gated h-free: energy balance 0.5%, q″t/k drop 0.2%
                       (cell-Péclet ≤25 envelope documented). Example I + cht.png.
                       EM: analysis/em.py — skin_depth/dc_resistance/wire/solenoid
                       exact oracles + em_conduction_submit (StatCurrentSolver,
                       R = L/σA machine-exact) and em_induction_submit
                       (MagnetoDynamics2DHarmonic — |A| AND phase e-fold at δ to
                       0.1%). Example J + em.png.
```

Each Mn is the established vertical slice: a module/handler/tool, the `*_submit` wired
through `jobs.py` (where a solver runs), a graceful-degradation path, and the two-sided
toy promoted into the gate harness. M1–M3 complete the catalog and need *no* new
infrastructure; M4 is the structural investment that M5 (and every real-part heavy solve)
rides on.

---

## Verification discipline (unchanged + one addition)

- **Exact closed-form bands where the physics is exact:** Snell/Fresnel/TIR (optics),
  the two-plate radiation exchange, Stokes/Blasius drag — absolute gates, like
  Hagen–Poiseuille and Miles' equation before them.
- **Relative gates for the geometry bridge.** A mesh-from-FreeCAD solve has no closed
  form, so gate it against the *parametric* builder solving the same shape (a consistent
  solver/mesh offset cancels) — the same rule the FEM family uses.
- **Degradation tests gate every PR and need no solver:** `<family>_submit` and
  `solve_capabilities` return the `{ok:false, reason, install}` dict when the
  binary/wheel is absent and never raise — runnable on the no-FreeCAD CI lane.
- **Heavy solves run on the provisioned runner only** (this box now has ElmerSolver,
  OpenFOAM, Gmsh; the optics wheel installs via the `optics` extra). Keep them out of the
  fast lane, gated behind a `solvers.is_available(...)` skip (the `test_elmer.py` /
  `test_openfoam.py` pattern).
- **Runnable acceptance evidence:** extend
  [`examples/run_simulation_examples.py`](../../examples/run_simulation_examples.py) (+ its
  `--plots` figures) with each new family, the way A/B/C cover topology/thermal/CFD.

---

## First-PR checklist (M1 — the optics slice)

1. `analysis/optics.py` — pure-Python Snell (`refract_angle`), Fresnel
   (`fresnel_reflectance`), critical angle / TIR, and an energy-balance helper. Exact,
   FreeCAD-free, fast-lane.
2. `optics_raytrace` MCP tool + worker handler — `_require_solver('rayoptics')` first
   (clean degradation), then the rayoptics/optiland trace; parse → `{exit_distribution,
   leakage_fraction, efficiency, hotspot_locations}`.
3. `optics_moldability_check` MCP tool + handler — draft-angle + undercut analysis vs
   `pull_axis`, reusing the existing draft/DfM face machinery → `{undercut_faces,
   draft_violations, wall_thickness_stats, score}`.
4. `tests/test_optics.py` — Snell within 0.1°, Fresnel within 0.2%, TIR zero-transmission,
   energy closes within 1% (fast lane, always); the rayoptics trace gated/skipped when the
   wheel is absent. Wire into `run_all.sh`.
5. Registry parity (`test_contracts.py`) stays green; document return values.
6. Add an **Example D — optics** to `examples/run_simulation_examples.py` (+ `--plots`).

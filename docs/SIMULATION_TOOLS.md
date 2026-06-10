# DriftPin simulation & analysis tool families

A menu of Python tool add-ons that turn DriftPin from "build the part" into
"build the part *and reason about whether it works*" — tolerance, materials,
fatigue, thermal, fluids, optics, multibody, and Design-for-X. Expands
[`ROADMAP.md`](ROADMAP.md) Slice 7 ("External simulation tools") into a full
catalog, a prioritized rollout, and a build-ready scaffold for the first family.

**Related docs:**
- [`ROADMAP.md`](ROADMAP.md) — slice history and the original Slice 7 stub this grows from.
- [`MULTI_AGENT.md`](MULTI_AGENT.md) — partition/merge model; sim results become *merge gates* (§4).
- [`../README.md`](../README.md) — the three-layer tool strategy and encode-intent philosophy these families inherit.

**Thesis:** FreeCAD is a *geometry source*, not a physics engine. The bundled FEM
(CalculiX + Elmer) covers mainstream structural / thermal / modal work, but
everything past that envelope — fit, fatigue, flow, light, motion,
manufacturability — is physics that *consumes* geometry. The right shape is a thin
DriftPin tool that takes a handle (or its exported STEP/STL/mesh), runs the math
or an external solver, and hands back structured JSON the agent reasons over. This
doc enumerates those tools and sequences them by build risk.

---

## The pattern

Two compute locations, with very different dependency and packaging stories:

1. **Pure-Python in the worker.** Tolerance math, materials lookups, fatigue/
   fracture closed-form checks, cost rollups. Needs at most a `Shape` read for
   volume/bbox/mass — no external solver, no new heavy deps. These are days of
   work each and ship first.
2. **External solver fed by exported geometry.** CFD, optics ray-tracing, slicing,
   multibody dynamics. DriftPin exports STEP/STL/mesh, shells out to the solver,
   parses results back. These carry install weight and long run-times; they ride
   behind optional extras and the async-solve work.

Both reuse the **compose → run → extract** shape the existing FEM family already
proves:

```
fem_new_analysis → fem_set_solver → fem_set_material → fem_add_constraint
                 → fem_mesh → fem_run → fem_results / fem_modal_results / ...
```

New families mirror it: a setup call (or explicit args), a run, and a results
extractor that returns numbers — never a raw solver dump.

**Promotion path** (the three-layer strategy): a new capability is born as a
`run_script` recipe, gets a `get_object`/`set_property` shim if it only needs a
property nudge, and is promoted to a typed `@mcp.tool()` once it's used enough to
deserve validated params and a stable handle. Don't wrap on day one — wrap when hot.

**Encode intent, not solver flags.** Every tool below takes "what you want"
params (`fit='clearance'`, `process='cnc'`, `method='rss'`), the same way Phase 3
tools take `through='wall'` and `intended_for='print'`. No raw FreeCAD or solver
flags leak into the tool surface.

---

## Tool family catalog

Each family lists: the agent question it answers · backend · new-dependency weight
· proposed signatures (intent-encoded) · result schema sketch.

### 1. Tolerance & GD&T  *(anchor — shipped)*

- **Answers:** "Will these parts fit? Tighten which dim to make the assembly fit 99.7%?"
- **Backend:** pure-Python (stdlib `random`/`statistics` for Monte-Carlo). **Weight: none.**
- **Signatures:**
  ```
  tolerance_stackup(chain=[{name,nominal,plus,minus}, ...],
                    method='worstcase'|'rss'|'montecarlo', samples=10000)
  fit_check(hole={nominal,plus,minus}, shaft={nominal,plus,minus})
  fit_class(basic_size, fit='H7/g6')          # ISO 286 lookup
  gdt_check(feature, control='position'|'flatness'|..., zone, datum_refs)
  ```
- **Result:** `{nominal, worstcase:{min,max}, rss:{sigma,min_3s,max_3s},
  montecarlo:{mean,std,cpk,pct_in_spec}}`; `fit_check` →
  `{fit_class, min_clearance, max_clearance, prob_interference}`.
- **Status: shipped (P0)** in `driftpin/analysis/tolerance.py` —
  `tolerance_stackup`, `fit_check`, `fit_class`, `gdt_check`, with 13 two-sided
  toys in `tests/test_tolerance.py`. Signed-deviation convention
  (`plus`=upper, `minus`=lower, half-band = 3σ); stack links carry an optional
  `direction` (±1) for gap/subtractive chains. `fit_class` v1 covers hole-basis H
  with clearance shaft letters (h, g, f, e); interference letters extend the same
  table next.

### 2. Materials & selection  *(foundational — many families cite it)*

- **Answers:** "What's 6061-T6's yield? Pick the cheapest alloy that survives this load."
- **Backend:** pure-Python over a JSON material library (mechanical / thermal /
  fatigue / fracture / density / rough cost). **Weight: none.**
- **Signatures:**
  ```
  material_get(name='AL6061-T6')              # full property bag
  material_select(criteria={min_yield_mpa: 200, max_density: 3.0},
                  rank_by='specific_strength'|'cost'|'stiffness')   # Ashby-style
  material_list(category='aluminum'|'steel'|'polymer'|...)
  ```
- **Result:** property dict with SI-tagged quantity strings matching the FEM
  convention (`"210000 MPa"`, `"7900 kg/m^3"`).
- **Integration:** lets `fem_set_material(..., material='Steel-A36')` take a *name*
  instead of a hand-typed property dict — and is the lookup table fatigue, fracture,
  and cost all read from.
- **Status: shipped.** Lives in `driftpin/analysis/materials/` (`material_get` /
  `material_select` / `material_list` tools). Mechanical/thermal cards are
  hand-seeded in `seed.json` and enriched at runtime from FreeCAD's installed
  `.FCMat` cards (`fcmat.py`).
- **Optical corpus (submodule + extract).** Optical members (n_d, Abbe, Sellmeier
  dispersion) come from the CC0 [refractiveindex.info-database](https://github.com/polyanskiy/refractiveindex.info-database),
  vendored as a git submodule at `vendor/refractiveindex.info-database` pinned to
  tag **`v2026-05-24`**. `tools/extract_optical_corpus.py` (dev-only, PyYAML)
  extracts a curated set into the committed `optical.json`, which the loader merges
  *field-wise* onto the seed cards — so the worker needs no YAML or submodule at
  runtime, only to regenerate. `materials.refractive_index_at(card, nm)` evaluates
  the Sellmeier formula for the optics family's `n_refractive`. Full sourcing
  rationale + per-source reliability/license notes:
  [`SIMULATION_EXAMPLES.md`](SIMULATION_EXAMPLES.md) (materials-corpus section).

### 3. Wear, fatigue & fracture

- **Answers:** "Does this survive 10⁶ cycles? Will the crack propagate? How fast does it wear?"
- **Backend:** pure-Python closed-form, consuming **Materials DB** + existing
  **`fem_results`** stress output. **Weight: none.**
- **Signatures:**
  ```
  fatigue_check(stress_range_mpa, mean_stress_mpa, cycles, material)   # S-N + Goodman
  fracture_check(stress_mpa, crack_len_mm, material)                   # K_applied vs K_IC
  wear_estimate(load_n, sliding_dist_m, material_pair)                 # Archard
  creep_flag(stress_mpa, temp_c, material)                            # service-temp screen
  ```
- **Result:** `{safety_factor, life_cycles, pass, governing_mode}` etc.
- Turns a one-shot FEM stress number into a *durability* answer.
- **Status: shipped (P0)** in `driftpin/analysis/durability.py` —
  `fatigue_check` (S-N Basquin + Goodman), `fracture_check` (LEFM K vs K_IC with
  critical-crack inversion), `wear_estimate` (Archard), `creep_flag`
  (service-temp screen), with 9 two-sided toys in `tests/test_durability.py`
  (LEFM K=13.3, a_c=9.5 mm; Goodman SF=0.94; Archard 45 mm³). Reads σ_e / σ_uts /
  K_IC / service temp from the Materials DB with explicit overrides.

### 4. Thermal (beyond steady-state CCX)

- **Answers:** "How hot does it get after 5 min? Does radiation matter?"
- **Backend:** lumped-mass quick estimates pure-Python; transient / radiation via
  Elmer or OpenFOAM `chtMultiRegionFoam`. **Weight: none (lumped) → heavy (CFD-class).**
- **Signatures (implemented):**
  ```
  thermal_lumped(mass_g, c_p, power_w, h_conv, area_mm2, t_ambient_c, duration_s)
  thermal_transient_1d(half_thickness_mm, h_conv, duration_s, k, rho, cp, …)  # Heisler oracle
  thermal_transient_submit(half_thickness_mm|body+convection_faces|case_dir, …)  # Elmer, async
    # body mode (P3 M4 bridge): Gmsh-mesh a real FreeCAD solid (UNV face groups →
    # boundary tags, tag i == Faces[i-1]) → ElmerGrid → HeatSolver; convection_faces
    # get h_conv/t_ambient_c, the rest are adiabatic -> {t_max_c, t_min_c, nodes, tets}
  thermal_radiation_submit(t1_c, t2_c, emissivity_1, emissivity_2, …)  # Elmer enclosure, async
    -> {ok:false, reason, install}                               # when ElmerSolver absent
     | {job_id, status, cache_hit}  # poll job_result for {ok, flux_w_m2, q_net_w,
       two_plate_flux_w_m2, oracle_ratio (≈1), t1_c, t2_c, emissivity_1, emissivity_2}
  ```
- CCX already covers steady-state conduction via `fem_thermal_results`; this fills
  the *time* and *radiation* gaps. Lumped version ships in the pure-Python wave.
- **Status: lumped + transient + radiation shipped.** `thermal_lumped` (P0; first-order
  RC: ΔT_ss, τ, T(t), plus an h_rad-vs-h_conv radiation screen). `thermal_transient_*`
  (P2 M4; Elmer 1-D plane-wall vs the one-term Heisler oracle to <0.1%). **`thermal_radiation_submit`
  (P3 M2):** Elmer **diffuse-gray** two-plate enclosure radiation (ViewFactors + HeatSolver),
  gated against the exact two infinite parallel plates exchange
  q = σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1) — `oracle_ratio` lands within ~0.2% (residual is
  finite-plate edge leakage), and the small-ΔT limit matches `thermal_lumped`'s h_rad
  screen. The exact closed form is `analysis/thermal.radiation_exchange` (general
  two-surface network + the two-plate limit); the Elmer case builder is
  `analysis/elmer.write_radiation_plates_case`. Acceptance: **Example E** +
  `radiation.png`; oracle gates in `tests/test_thermal.py`, the ElmerSolver gate in
  `tests/test_elmer.py`.

### 5. Structural extensions

- **Answers:** "Where can I remove material? Does it survive random vibration?"
- **Backend:** CCX nonlinear/contact flags (partly exposed already);
  `topopt` / `solidspy` / FEniCS for topology; PSD math on top of `fem_modal`.
- **Signatures:**
  ```
  topology_optimize(body, load_cases, keep_fraction, keep_out_regions)
  random_vibration(analysis, psd_profile)              # builds on fem_modal results
  contact_setup(analysis, face_pairs, friction)        # promotes existing CCX flags
  ```
- `topology_optimize` *returns geometry*, not just numbers — the one family here
  that closes the loop back into the modeller.

### 6. Fluids / CFD  ✅ shipped (P2 M5 internal; P3 M3 external)

- **Answers:** "What's the pressure drop through this manifold? Drag on this housing?"
- **Backend:** OpenFOAM (`blockMesh`+`simpleFoam`, laminar) / SU2; the exact analytic
  oracles are pure-Python in [`analysis/cfd.py`](../driftpin/analysis/cfd.py).
- **Signatures (implemented):**
  ```
  cfd_pipe_flow(diameter_mm, length_mm, flow_rate_lpm|velocity_m_s, fluid)  # Hagen–Poiseuille oracle
  cfd_internal_flow_submit(diameter_mm,length_mm,…|body+inlet_face+outlet_face|case_dir)  # async
    -> {ok, reynolds, pressure_drop_pa, hagen_poiseuille_pa, hp_ratio (≈1), …}
    # body mode (P3 M4 bridge): tessellate a real FreeCAD solid into per-face STL
    # regions (inlet/outlet by 1-based face index, the rest no-slip walls) →
    # blockMesh box → snappyHexMesh → simpleFoam; use the developed pressure_drop_pa
  cfd_external_flow_submit(velocity_m_s, plate_length_mm, fluid, …|case_dir)  # OpenFOAM flat plate, async
    -> {ok:false, reason, install}                            # when no CFD solver resolves
     | {job_id, status, cache_hit}  # poll job_result for {ok, reynolds_l, cd, cf_solved,
       cf_blasius, blasius_ratio (≈1, ~15%), drag_force_n, drag_momentum_n, n_cells}
  ```
- **External-flow oracles** (`analysis/cfd.py`): **Stokes sphere** Cd = 24/Re,
  F = 6πμUR (exact at Re≪1) and the **laminar flat plate** (Blasius
  Cf = 1.328/√Re_L). The external builder (P3 M3) builds a 2-D flat plate with a clean
  leading edge (slip→plate→slip, far-field top), runs simpleFoam, and integrates the
  **wall-shear drag straight from the converged U field** — OpenFOAM's force /
  wallShearStress function objects abort with a `sha1` IOstream error in this build, so
  drag is read from fields (τ_w ≈ μ·u₁/y₁ over the plate; trailing-edge momentum
  thickness as a cross-check), gating Cd vs Blasius within ~15% (it lands ~9% high and
  converges with Re). Acceptance: **Example F** + `external.png`; oracle gates in
  `tests/test_cfd.py`, the simpleFoam gate (Blasius + U^1.5 law) in
  `tests/test_openfoam.py`.
- Long solves run async via [`jobs.py`](../driftpin/jobs.py) so a CFD run never blocks
  the MCP channel.

### 7. Optics  ✅ shipped (P3 M1)

- **Answers:** "Where does the light land? Is this lens moldable?"
- **Backend:** the exact closed-form core (Snell / Fresnel / TIR + an
  energy-conserving ray-bundle trace) is pure-Python in
  [`analysis/optics.py`](../driftpin/analysis/optics.py) (stdlib `math`, no NumPy,
  fast-lane). The full lens/diffuser trace rides on the **`rayoptics`** wheel (the
  `optics` extra) behind `_require_solver('rayoptics')`, degrading cleanly when
  absent. (`~/diffuser` is not present on the runner; `rayoptics` is the portable
  backend. Optical n vs wavelength comes from
  [`materials/optical.json`](../driftpin/analysis/materials/optical.json).)
- **Exact anchors (the gate):** 30° air→PMMA (n=1.49062) → **19.60°**;
  normal-incidence Fresnel reflectance **3.88%**; PMMA→air critical angle
  **42.13°** (zero transmission above it); the bundle trace conserves energy
  (`leakage + efficiency + absorbed == 1` to floating point). rayoptics reproduces
  Snell to **< 1e-6°** (`oracle_max_dev_deg`).
- **Signatures (implemented):**
  ```
  optics_raytrace(n_refractive=1.49062, source_config=None, n_rays=64, model=None)
    -> {ok:false, reason, install}                  # when rayoptics is absent
     | {ok, backend:'rayoptics', rayoptics_version, n_rays, n1, n2,
        critical_angle_deg, efficiency, leakage_fraction, absorbed_fraction,
        tir_fraction, energy_balance, oracle_max_dev_deg,
        exit_distribution:[{angle_deg, intensity}], hotspot_locations}
  optics_moldability_check(model, pull_axis='+z', process='injection',
                           min_draft_deg=1.0, min_wall_mm=None)
    -> {process, pull_axis, n_faces, undercut_faces, draft_violations,
        min_wall_violations, wall_thickness_stats:{min_mm,mean_mm,max_mm,n},
        score, pass}
  ```
  `optics_moldability_check` is purely geometric: per-face draft vs the pull axis
  (draft_deg = 90 − angle(normal, pull)) plus a ray-cast occlusion test on the live
  FreeCAD solid — a face the straight pull frees in neither direction is a
  re-entrant **undercut** — scored through `analysis/dfx.dfm_check`.
- Acceptance evidence: **Example D** in
  [`run_simulation_examples.py`](../examples/run_simulation_examples.py) (+ the
  `optics.png` figure); fast-lane gates in
  [`tests/test_optics.py`](../tests/test_optics.py).

### 8. Multibody dynamics / kinematics

- **Answers:** "Does the linkage reach? Does it collide *through its motion*, not just at rest?"
- **Backend:** MuJoCo / PyBullet / pinocchio. **Weight: medium-heavy.**
- **Signatures:**
  ```
  mechanism_simulate(assembly, joints, drivers, duration_s)
    -> {trajectories, max_torques, collisions_through_motion, reachable_envelope}
  ```
- Assembly carries constraints but no dynamics today — this is the only family that
  reasons about *time-varying* geometry.

### 9. Design for X (DfX)

A cluster of mostly-heuristic checks that grade a design against a downstream
process. Several reuse existing DriftPin tools directly.

- **DfM / Manufacturing** — pure-Python + face-tagging. Generalizes the diffuser
  moldability scorer; reuses `draft`, `thickness`, `query_faces`.
  ```
  dfm_check(model, process='cnc'|'injection'|'sheet'|'fdm')
    -> {draft_violations, undercut_faces, min_wall_violations,
        min_radius_violations, tool_access_issues, score}
  ```
- **DfA / Assembly** — Boothroyd-Dewhurst-lite over the assembly graph +
  `interference_check`.
  ```
  dfa_check(assembly)
    -> {part_count, fastener_count, insertion_axes, handling_difficulty, symmetry_score}
  ```
- **Slicing / print** — PrusaSlicer / OrcaSlicer / CuraEngine CLI on exported STL.
  Self-contained external CLI, high value.
  ```
  slice_estimate(model, profile='draft'|'standard'|'fine', material)
    -> {print_time_min, support_volume_mm3, layer_count, filament_g, mass_g}
  ```
- **Packaging / shipping** — reuse `mass_properties`, `envelope_check`.
  ```
  pack_check(model_or_assembly, carton)
    -> {fits, void_fraction, dim_weight_kg, carrier_tier, drop_crush_flag}
  ```
- **Cost / DfC** — `material_volume × Materials DB price` + process-time estimate.
  ```
  cost_estimate(model, process, material, quantity)
    -> {material_cost, process_cost, unit_cost, breakdown}
  ```
- **Status: shipped (P0, v1 explicit-input)** — `dfm_check` (draft/undercut/min-wall
  from explicit face descriptors), `dfa_check` (Boothroyd-lite), `pack_check`
  (carton fit + dimensional weight) in `driftpin/analysis/dfx.py`; `cost_estimate`
  (exact `material_cost = volume·density·price` + a per-process machine-time model)
  in `cost.py`; `slice_estimate` (first-order FDM estimate; a PrusaSlicer/OrcaSlicer
  CLI on STL is the P1 upgrade) in `slicing.py`. 21 two-sided toys across
  `tests/test_dfx.py` / `test_cost.py` / `test_slicing.py`. v1 takes **explicit**
  geometry summaries (face draft angles, bbox, volume) like `tolerance.py` takes an
  explicit chain; reading those off a `Shape` (via `draft`/`thickness`/`query_faces`/
  `mass_properties`) is the v2 wiring.

### 10. Machine-element rating  *(highest leverage on existing tools)*

Closed-form ratings that pair **1:1 with DriftPin's existing component
generators** — `add_fastener`/`add_thread`, `add_bearing`, `add_spring`,
`add_gear`, `add_pulley`/`add_sprocket`, `oring_groove`. The generator already
encodes the geometry; the rating tool consumes it (by handle or explicit dims) and
returns life / safety-factor, turning "I drew a gear" into "this gear survives."

- **Answers:** "Will this bolt hold preload without yielding? How many hours does
  this bearing last? Does this spring buckle or fatigue?"
- **Backend:** pure-Python textbook closed-form (VDI 2230, ISO 281, Wahl, AGMA/
  Lewis, Lamé), reading strengths/moduli from the **Materials DB**. **Weight: none.**
- **Signatures (intent-encoded):**
  ```
  bolted_joint_check(bolt_dia_mm, pitch_mm, torque_nm|preload_n, k_factor,
                     external_load_n, joint_stiffness_ratio, material)   # VDI 2230-lite
    -> {preload_n, bolt_stress_mpa, preload_pct_proof, separation_margin, pass}
  bearing_life(dynamic_load_c_n, equivalent_load_p_n, speed_rpm, kind)   # ISO 281 L10
    -> {l10_million_rev, l10_hours, load_ratio, pass}
  spring_check(wire_dia_mm, coil_mean_dia_mm, active_coils, force_n, material)  # Wahl
    -> {rate_n_mm, shear_stress_mpa, wahl_factor, deflection_mm, buckling_flag}
  gear_rating(module_mm, teeth, face_width_mm, tangential_force_n|power_w,
              pinion_speed_rpm, material)                                # Lewis bending
    -> {bending_stress_mpa, bending_sf, pitch_line_velocity_m_s, pass}
  belt_drive(power_w, small_pulley_dia_mm, large_pulley_dia_mm,
             center_distance_mm, small_pulley_rpm, friction_coef,
             vbelt_groove_deg)                                           # Eytelwein
    -> {wrap_angle_deg, belt_speed_m_s, tension_ratio, tight_side_n, slack_side_n}
  press_fit_stress(shaft_dia_mm, hub_outer_dia_mm, interference_mm,
                   engagement_length_mm, material, friction_coef)        # Lamé
    -> {contact_pressure_mpa, hub_hoop_stress_mpa, torque_capacity_nm, hub_yield_sf}
  seal_check(cross_section_dia_mm, groove_depth_mm, groove_width_mm,
             application)                                                # O-ring gland
    -> {squeeze_pct, gland_fill_pct, within_squeeze, within_fill, pass}
  ```
- **Result:** every tool returns a safety-factor / life number and a `pass` bool,
  so it drops straight into a **merge gate** (every rated element passes).
- **Integration:** the natural durability counterpart to the component-generator
  tools, exactly as Wear/Fatigue/Fracture (family 3) is to FEM stress.
  `belt_drive`↔`add_pulley`, `press_fit_stress`↔press-fit geometry,
  `seal_check`↔`oring_groove`.
- **Status: shipped (P0)** in `driftpin/analysis/machine_elements.py` —
  `bolted_joint_check`, `bearing_life`, `spring_check`, `gear_rating`,
  `belt_drive`, `press_fit_stress`, `seal_check`, all with hand-verified toys in
  `tests/test_machine_elements.py`. Chain/sprocket and weld-group ratings extend
  the same module next.

### 11. Horizon (table-only)

| Domain | Tooling | Priority |
|---|---|---|
| Acoustics | Elmer, pyfar, acoular | low |
| Electromagnetics | OpenEMS / FEniCSx (RF), FEMM (2D motors), Elmer | low |
| Machining toolpaths | FreeCAD Path, pycam, kiri:moto | low |

---

## Cross-cutting concerns

- **Dependencies / packaging.** Pure-Python families add nothing. External-solver
  families ride behind optional extras (`pip install driftpin[cfd]`,
  `driftpin[optics]`) and discover their CLI/solver at runtime, **degrading
  gracefully** — a missing OpenFOAM yields a clear "solver not installed" result,
  not an import crash.
- **Units discipline.** Everything SI-tagged with quantity strings, matching the
  FEM material convention (`"210000 MPa"`). Tolerances carry their unit; no bare floats.
- **Caching.** Expensive solves are keyed on a geometry content-hash (the same
  hashing the multi-agent lockfile already uses) so an unchanged part isn't re-solved.
- **Async / long solves. Shipped** in `driftpin/jobs.py` — a FreeCAD-free job
  registry + background-thread runner + content-hash cache, with a shared
  `job_status`/`job_result`/`job_list` poll surface (and `async_demo_submit` as the
  reference). Any `*_submit` tool runs its work off the MCP channel; the submitted
  callable must not touch FreeCAD (pure-Python compute, or polling an external
  solver subprocess), so CFD/transient-thermal/MBD background only their solver
  the way `render_photoreal_submit` does. This is the unblock for the P2 families.
- **Merge gates** ([`MULTI_AGENT.md`](MULTI_AGENT.md)). Sim results become hard
  oracles: a partitioned design can gate merge on "every part passes `dfm_check` and
  its interfaces fit within the `tolerance_stackup` budget" — deterministic,
  numeric, agent-independent, exactly like the existing interference/envelope gates.

---

## Prioritized roadmap

```
                Materials DB ──┬──► Wear / Fatigue / Fracture
                               ├──► Cost (DfC)
   FEM stress results ─────────┘
   (existing fem_results)

   Materials DB ──────────────────► Machine-element rating
   add_* component generators ─────┘ (bolt/bearing/spring/gear/…)

   Geometry export (STEP/STL/mesh) ──┬──► CFD
                                     ├──► Optics
                                     └──► Slicer estimate

   async/long-solve infra ──────────────► CFD · transient thermal · MBD
```

**P0 — pure-Python, zero new deps, ship first.** Days each, no installs,
immediately useful:
1. **Tolerance / GD&T** (Appendix A) — validates the new `analysis/` extension point. *(shipped)*
2. **Materials DB** — foundational; unblocks fatigue, fracture, cost. *(shipped)*
3. **Wear / fatigue / fracture** — turns FEM stress into durability. *(shipped)*
4. **DfM / DfA heuristics** + lumped thermal — grade against process, reuse existing tools. *(shipped: dfm/dfa/pack/cost + thermal_lumped)*
5. **Machine-element rating** — closed-form life/SF on the existing `add_*` component tools. *(shipped: bolt, bearing, spring, gear, belt, press-fit, seal)*

**P1 — external CLI, self-contained.** One new tool dependency each, bounded runtime:
5. **Slicer estimate** (PrusaSlicer/OrcaSlicer CLI on STL). *(first-order analytic `slice_estimate` shipped; CLI upgrade pending)*
6. **Optics** (wrap `~/diffuser`).
7. **Cost** rollup (depends on Materials DB + a process-time model).

**P2 — heavy external solvers.** Gated on async-solve + optional-extras packaging:
8. **CFD**, **transient/radiation thermal**, **topology optimization**, **MBD**.

---

## Appendix A — build-ready scaffold: Tolerance & GD&T

The first family, concrete enough to implement without re-deriving the
architecture. Establishes the pattern every other pure-Python family follows.

**New home for non-FreeCAD physics.** Create a `driftpin/analysis/` subpackage —
pure-Python math importable both by worker handlers *and* standalone (so it's
unit-testable without spawning FreeCAD). Tolerance is the first module.

```
driftpin/
  analysis/
    __init__.py
    tolerance.py        # stackup math, fit classes, ISO 286 table, gdt zones
  worker.py             # @handler("tolerance_stackup") -> analysis.tolerance.*
  mcp_server.py         # @mcp.tool() thin wrappers
tests/
  test_tolerance.py     # hand-checked; wired into run_all.sh
```

**Layering — identical to every existing tool:**

1. **`driftpin/analysis/tolerance.py`** — the math. No FreeCAD import. Functions:
   `stackup(chain, method, samples)`, `fit(hole, shaft)`, `iso286(basic_size, code)`,
   `gdt_zone(feature, control, zone, datums)`.
2. **`worker.py`** — handlers calling into the module:
   ```python
   @handler("tolerance_stackup")
   def _h_tolerance_stackup(p):
       from driftpin.analysis import tolerance
       return tolerance.stackup(p["chain"], p.get("method", "worstcase"),
                                p.get("samples", 10000))
   ```
   plus `tolerance_fit_check`, `tolerance_fit_class`, `tolerance_gdt_check`.
3. **`mcp_server.py`** — thin typed wrappers mirroring the FEM tool style:
   ```python
   @mcp.tool()
   def tolerance_stackup(chain: list, method: str = "worstcase",
                         samples: int = 10000) -> dict:
       """Stack a dimension chain. method: worstcase | rss | montecarlo."""
       return _call("tolerance_stackup", chain=chain, method=method, samples=samples)
   ```

**Signatures & results:**
```
tolerance_stackup(chain=[{name,nominal,plus,minus}, ...], method, samples)
  -> {nominal,
      worstcase:{min,max},
      rss:{sigma, min_3s, max_3s},
      montecarlo:{mean,std,cpk,pct_in_spec}}   # montecarlo block only when requested

fit_check(hole={nominal,plus,minus}, shaft={nominal,plus,minus})
  -> {fit_class:'clearance'|'transition'|'interference',
      min_clearance, max_clearance, prob_interference}

fit_class(basic_size, fit='H7/g6')   -> {hole:{plus,minus}, shaft:{plus,minus}}
gdt_check(feature, control, zone, datum_refs) -> {pass, actual, margin}
```

**Dependencies:** none beyond numpy (already in `pyproject.toml`). v1 takes
explicit chains (no geometry read) so it's testable in isolation; reading
tolerances off a `Shape` is a v2 nicety.

**Tests** (`tests/test_tolerance.py`, wired into `tests/run_all.sh`):
- worst-case and RSS against hand-computed reference chains;
- fit-class boundary cases (clearance/transition/interference transitions);
- Monte-Carlo convergence + `pct_in_spec` sanity vs. RSS on a normal chain;
- ISO 286 spot-checks (e.g. `H7/g6` on a 20 mm shaft).

**Why this one first:** highest agent value per unit of build risk — no external
solver, no new deps, no async concern — and it stands up the `analysis/`
subpackage that Materials DB, fatigue, fracture, and cost all reuse.

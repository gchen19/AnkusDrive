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
  `tolerance_stackup`, `fit_check`, `fit_class`, `gdt_check`, with 16 two-sided
  toys in `tests/test_tolerance.py`. Signed-deviation convention
  (`plus`=upper, `minus`=lower, half-band = 3σ); stack links carry an optional
  `direction` (±1) for gap/subtractive chains. `fit_class` covers hole-basis H
  with clearance (h, g, f, e), transition (js, k, m, n) and interference
  (p, r, s) shaft letters; an interference result returns an interference band
  and hands off to `press_fit_stress`.

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
  h_estimate(geometry, characteristic_mm, t_surface_c, …)  # screening h: Churchill–Chu /
    # flat-plate / Hilpert correlations + the h_rad screen; fidelity='correlation',
    # band_pct ±15–20%, escalate_to=cht_channel_submit (SIMULATION_NEXT.md Tier A)
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
  thermal_composite_wall(layers, t_in_c, t_out_c, h_in, h_out)  # exact U/q/interface temps, no solver
  cht_channel_submit(flux_w_m2, velocity_m_s, …)  # P3 M6: ONE Elmer solve over coupled
    # plug-flow fluid + solid wall regions; gated h-free (energy balance q″L=ṁ·c_p·ΔT
    # ≈0.5%, solid drop q″t/k ≈0.2%; the builder rejects cell Péclet > 25)
  cht_graetz_submit(velocity_m_s, gap_m, …)  # B4: FlowSolve + Convection=Computed between
    # isothermal walls — the TRUE Nusselt validation: solved parabola u_max/u_mean ≡ 3/2,
    # developed mixing-cup decay fits Nu vs the Graetz 7.5407 (slug π² is the discriminator);
    # closes the loop with h_estimate. tests/test_cht.py
  ```
- CCX already covers steady-state conduction via `fem_thermal_results`; this fills
  the *time* and *radiation* gaps. Lumped version ships in the pure-Python wave.
- **Status: lumped + transient + radiation shipped.** `h_estimate` (Tier A screening,
  `analysis/convection.py`): handbook convection coefficients — Churchill–Chu natural
  (vertical plate / horizontal cylinder), averaged flat-plate + Hilpert crossflow
  forced, film-temperature air properties built in — so the h_conv every tool below
  consumes is a labeled ±20 % correlation instead of a guess; toys in
  `tests/test_convection.py`. `thermal_lumped` (P0; first-order
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
- **Signatures (implemented):**
  ```
  topology_optimize_submit(body, keep_fraction, nelz?, loads?, keep_out?, keep_in?)  # SIMP, returns geometry
  random_vibration(analysis|frequencies_hz, psd_profile)   # Miles SRSS on fem_modal results
  harmonic_response(natural_frequency_hz, damping_ratio, frequency_hz?, …)  # exact SDOF FRF:
    # |H|, phase, Q=1/(2ζ√(1−ζ²)), f_peak, half-power bandwidth — bridges beam_modal & random_vibration
  harmonic_response_submit(length_m, height_m, damping_ratio, span_pct, …)  # Tier B2: Elmer
    # StressSolve Harmonic Analysis, async — tip-driven plane-stress cantilever swept through
    # resonance; gates: f1_ratio (Re(H)=0 exactly at f_n, vs beam_modal), static_ratio
    # (F·L³/3EI), q_ratio (max|Re|/static ≡ Q/2); tests/test_harmonic_fem.py
  beam_modal(length_mm, width_mm, height_mm, boundary, n_modes, youngs_gpa, density_kg_m3|material)
    -> {boundary, frequencies_hz, beta_l, first_mode_hz, area_mm2, I_mm4, slenderness}
  beam_buckling(length_mm, end_condition, width/height|diameter|area+I, E, sigma_y|material, load_n?)
    # Tier A: exact Euler + Johnson — the closed-form twin of fem_buckling
    -> {governing, slenderness, transition_slenderness, sigma_cr_mpa, p_cr_n, safety_factor, …}
  plate_check(shape, thickness_mm, pressure_kpa, a_mm+b_mm|diameter_mm, support, E|material)
    # Tier A: Roark/Timoshenko uniform-load plates, "do I need FEM at all?"
    -> {sigma_max_mpa, deflection_max_mm, thin_plate_ok, small_deflection_ok, …}
  contact_setup(analysis, face_pairs, friction)            # promotes existing CCX flags
  ```
- `topology_optimize` *returns geometry*, not just numbers — the one family here
  that closes the loop back into the modeller.
- **3-D mode shipped (P3 M5):** `topology_optimize_submit(nelz=…)` runs the
  in-house SIMP on trilinear hexahedra (`analysis/topology.simp_topology_3d` —
  scipy.sparse / dense / matrix-free-PCG backends, all agreeing to 1e-4) with
  point `loads`=[[i,j,k,axis,value]], clamped `fixed_nodes`, and `keep_out` /
  `keep_in` element boxes (forced void / forced solid). Returns a
  nelz×nely×nelx voxel `density` field; `topology_to_solid` consumes it
  directly (greedy voxel→box merge, `density_to_boxes`). Gates in
  `tests/test_topology.py`: volume held exactly, z-mirror symmetry of a
  mid-plane-loaded design, keep-out/keep-in respected, and the uniform-density
  cantilever within the Euler-Bernoulli+Timoshenko band.
- **Modal (P3 M6):** `beam_modal` is the exact Euler-Bernoulli natural-frequency oracle
  (f_n = (βL)_n²/(2π)·√(EI/ρAL⁴), cantilever / simply-supported / clamped-clamped /
  free-free / clamped-pinned) in [`analysis/vibration.py`](../driftpin/analysis/vibration.py).
  The existing CalculiX `fem_modal` eigen-solve is gated against it — within ~0.5% of the
  fundamental once `fem_mesh(element_order='2nd')` is used (linear C3D4 tets shear-lock
  and overshoot ~50%; quadratic C3D10 fix it). Acceptance: **Example H** + `modal.png`;
  oracle gates in `tests/test_vibration.py`, the CalculiX gate in `tests/test_worker.py`
  (`test_fem_modal_cantilever`).

### 6. Fluids / CFD  ✅ shipped (P2 M5 internal; P3 M3 external; B3 kOmegaSST RANS)

- **Answers:** "What's the pressure drop through this manifold? Drag on this housing?"
- **Backend:** OpenFOAM (`blockMesh`+`simpleFoam`, laminar) / SU2; the exact analytic
  oracles are pure-Python in [`analysis/cfd.py`](../driftpin/analysis/cfd.py).
- **Signatures (implemented):**
  ```
  cfd_pipe_flow(diameter_mm, length_mm, flow_rate_lpm|velocity_m_s, fluid, roughness_mm)
    # Hagen–Poiseuille oracle (exact laminar) + Blasius/Colebrook turbulent screen
    # (fidelity='correlation', band_pct=10); flat_plate_drag_turbulent is the external twin
  cfd_internal_flow_submit(diameter_mm,length_mm,…|body+inlet_face+outlet_face|case_dir)  # async
    -> {ok, reynolds, pressure_drop_pa, hagen_poiseuille_pa, hp_ratio (≈1), …}
    # body mode (P3 M4 bridge): tessellate a real FreeCAD solid into per-face STL
    # regions (inlet/outlet by 1-based face index, the rest no-slip walls) →
    # blockMesh box → snappyHexMesh → simpleFoam; use the developed pressure_drop_pa
  cfd_external_flow_submit(velocity_m_s, plate_length_mm, fluid, …|case_dir)  # OpenFOAM flat plate, async
    -> {ok:false, reason, install}                            # when no CFD solver resolves
     | {job_id, status, cache_hit}  # poll job_result for {ok, reynolds_l, cd, cf_solved,
       cf_blasius, blasius_ratio (≈1, ~15%), drag_force_n, drag_momentum_n, n_cells}
  cfd_body_drag(shape, diameter_mm|frontal_area_mm2|model, velocity_m_s, fluid)  # screen, no solver
    -> {cd, drag_force_n, frontal_area_m2, reynolds, regime, fidelity, band_pct,
        valid_range_ok, warnings, escalate_to}                 # sphere / cylinder / Cd table
  cfd_external_flow_submit(model, velocity_m_s, flow_direction, …)  # THE WIND TUNNEL, async
    -> {job_id, …}  # poll job_result for {ok, cd, cl, cm, drag_force_n, drag_pressure_n,
       drag_viscous_n, lift_force_n, force_total_n, moment_total_nm, force_drift_pct,
       reynolds, frontal_area_m2, blockage_ratio, converged, gated, warnings}
  ```
- **The virtual wind tunnel** (issue #223/#224): pass `cfd_external_flow_submit` a
  **solid handle** and it solves THAT BODY, not a stand-in. Faces tessellate into an
  STL, `external_domain_box` sizes a farfield box by standard practice (5L up / 10L
  down / 5L lateral; `blockage_ratio` warns past 5 %), snappyHexMesh carves the body
  out, and the OpenFOAM `forces` function object integrates pressure + viscous traction
  over the body patch — Cd/Cl/Cm on the **measured silhouette** (`projected_area`,
  exact for a convex body). Two guards exist because their failure modes are SILENT: a
  background cell as coarse as the body makes snappyHexMesh mesh an **empty tunnel**
  and report ~1e-14 N with rc=0, so a too-coarse `base_cell_mm` is refused; and a
  steady laminar request past Re≈1000 is flagged in `warnings` rather than answered
  quietly. Gates: sphere vs the Clift–Gauvin drag curve at
  Re=1 and Re=100 (live 0.8 % / 2.0 %, banded 10 %) and a same-body broadside-vs-edge-on
  ordering, in `tests/test_meshbridge.py`; end to end through the handler in
  `tests/test_wind_tunnel.py`.
- **`gated` is per (turbulence model, case family)** (issue #262) — one registry,
  `cfd.solve_gate`, decides it for every CFD payload; it used to be the single line
  `turbulence == "laminar"`, so **every** turbulent external-flow solve shipped
  `gated:false` and a requirement demanding `trust: {gated: true}`
  (`performance.check_trust`) was unsatisfiable at a realistic Reynolds number: laminar
  is gated but physically wrong above Re≈1000, RANS was right but unproven. The
  turbulent body path is now gated on a **cube face-on vs the tabulated bluff-body Cd**
  (1.05, ±20 %) over `Re = 1e4–2e5` — **live 1.0017 at Re=1e4 and 1.0035 at Re=1e5**
  (`tests/test_wind_tunnel.py`), holding at 1.0136 on a finer surface refinement
  (min level 3, 4× the cells). Outside that Reynolds envelope, or for any pair nobody has verified, the
  payload comes back `gated:false` **with the reason in `warnings`** rather than
  inheriting a neighbour's credibility. Trust evidence is still not validation:
  the same Re=1e5 cube whose Cd is right is `trusted:false` because measured y+ 583
  is past the wall-function band.
  - **The boundary, measured:** a sharp-edged body is where steady RANS is credible —
    separation is pinned to the edges by geometry. On a **smooth** body it is the
    model's job to predict, and it does it badly: the sphere at Re=1e4 reads **1.091**
    of Clift–Gauvin (1.097 at the next refinement level, so model error, not mesh
    error) — inside that correlation's ±10 % band, but only just. Recorded live in
    `tests/test_wind_tunnel.py`.
  - **Why not the cylinder in crossflow**, which #262 proposed: Sucker–Brauer is the
    INFINITE-cylinder value and the handler cannot build a spanwise-periodic case —
    `external_domain_box` pads both non-flow axes with the single `lateral_factor`, so
    asking for a near-2-D span crushes the cross-stream domain into the 5 % blockage
    warning. A finite L/D = 4 cylinder solved live at Re=1e4 lands at Cd 0.769,
    **ratio 0.70** vs Sucker–Brauer — end relief, exactly as
    `cylinder_crossflow_drag`'s own `L/D < 10` warning predicts, and outside its ±15 %
    band for a geometric reason with nothing to do with turbulence modelling.
- **External-flow oracles** (`analysis/cfd.py`): **Stokes sphere** Cd = 24/Re,
  F = 6πμUR (exact at Re≪1); the **standard sphere drag curve** (Clift–Gauvin, ±10 %,
  collapsing to Stokes as Re→0 and stopping at the drag crisis); the **cylinder in
  crossflow** (Sucker–Brauer, ±15 %); a **bluff/streamlined Cd table** (±20 %); and the
  **laminar flat plate** (Blasius Cf = 1.328/√Re_L). The external builder (P3 M3) builds a 2-D flat plate with a clean
  leading edge (slip→plate→slip, far-field top), runs simpleFoam, and integrates the
  **wall-shear drag straight from the converged U field** — originally because
  OpenFOAM's force / wallShearStress function objects aborted with a `sha1` IOstream
  error in the build it was written against (they work on v2512, and the arbitrary-body
  wind tunnel below relies on them), and still, because it is the gated number
  (τ_w ≈ μ·u₁/y₁ over the plate; trailing-edge momentum
  thickness as a cross-check), gating Cd vs Blasius within ~15% (it lands ~9% high and
  converges with Re). Acceptance: **Example F** + `external.png`; oracle gates in
  `tests/test_cfd.py`, the simpleFoam gate (Blasius + U^1.5 law) in
  `tests/test_openfoam.py`.
- **B3 turbulent RANS (SIMULATION_NEXT):** `turbulence='kOmegaSST'` on both submit
  tools upgrades the validation cases past Re≈2300 — wall-function k/ω/ν_t (first-cell
  y+ targeted ~30–100, reported), upwind convection, and **banded** gates only: the
  pipe fits the developed dp/dx over its second half against Colebrook
  (`colebrook_ratio`, ±10 % — live 0.93 at Re=10⁵), the plate gates the
  trailing-edge momentum-thickness Cf against the mixed-transition 1/7-power form
  (`cf_mixed_ratio`, ±15 % — live 1.02 at Re_L=2·10⁶, with the (ν+ν_t)-corrected
  wall shear as cross-check), and — since #262 — the arbitrary-body wind tunnel
  against the bluff-body Cd table (live 1.0017 at Re=10⁴). Builders/parsers in
  `analysis/openfoam.py` (`*_rans_*`), oracles in `analysis/cfd.py`
  (`colebrook_friction_factor`, `flat_plate_drag_turbulent`, `bluff_body_drag`), the
  per-pair verdict in `cfd.solve_gate`, gates in `tests/test_openfoam.py` and
  `tests/test_wind_tunnel.py`.
- **The trust layer** (issue #225): every steady CFD result now carries a `trust` block
  saying what the numerics actually did, because a solve that hit its iteration cap
  unconverged is otherwise indistinguishable in the payload from one that converged.
  `converged` + `iterations` + per-field `final_residuals` come from the solver log
  (`openfoam.parse_residuals`; `_run_foam` tees every app to `log.<app>`), `mesh` from a
  post-solve `checkMesh` (`parse_checkmesh` — it exits 0 either way, so the verdict is
  read from the TEXT), and `y_plus` from the `yPlus` function object — **measured** from
  the solved wall shear, next to the a-priori `y_plus_estimate` the RANS builders
  report. `trusted` is the AND of every check that could be run and `reasons` names each
  failure. The audit runs as a separate pass so it can never fail a good solve.
  - This layer's first catch was in DriftPin's own flagship case: the axisymmetric wedge
    pipe never satisfied `residualControl` and always ran to `endTime`, because the
    out-of-plane `Uz` residual is normalized by a near-zero field and floors at ~1.6e-5
    at ANY mesh density while `Ux` reaches 5.8e-16. Controlling on `p` alone converges it
    in 74 iterations to a pressure drop identical to the 3000-iteration one to six
    decimals — and the live pipe gates got ~5x faster as a side effect.
- **Solution verification** — `grid_convergence` ([`analysis/verification.py`](../driftpin/analysis/verification.py))
  is Roache's GCI as codified in ASME V&V 20: the same quantity on 2-3 refined meshes,
  finest first, gives the observed order of convergence, the Richardson extrapolation to
  h→0, and the percentage band around the finest value. It is family-agnostic on purpose
  (three drag coefficients, three peak stresses, three modal frequencies all work), and
  `cfd_mesh_independence_submit` is the driver that produces the CFD values — one job for
  the whole ladder, coarsest level = the mesh a plain submit would build, refining from
  there, with the iteration cap scaled per level. Where validation compares a solve to an
  oracle, this compares a solve to ITSELF, which is the only band available on geometry
  with no closed form. Gated two ways: exactly, against constructed sequences with known
  order and limit (`tests/test_verification.py`), and live on the pipe, where the band
  computed with no reference must contain the analytic answer — measured order 2.01,
  extrapolation within 0.04 % of Hagen-Poiseuille, band 0.11 %.
- **Performance contracts** (issue #226, [`analysis/performance.py`](../driftpin/analysis/performance.py)):
  `declare_performance` persists a quantitative spec on the part the way `declare_intent`
  persists a geometric one, and `verify_performance` re-proves it. The contract layer is a
  thin orchestrator — a requirement names the DriftPin tool that measures its metric, so
  Cd, Δp, first mode and ΔT are the same machinery. Three things make it more than a
  comparison: the verdict has a third state (`indeterminate`) for a measurement whose
  uncertainty band straddles the limit, evidence is laddered (`tier='auto'` screens first
  and escalates only what the screen cannot decide or what declares
  `fidelity_floor: 'solver'`), and the trust block above is enforced — a requirement
  asking for `converged: true` or a `band_max_pct` cap can never be satisfied by a solve
  that did not converge. Gates in `tests/test_performance.py`, including the epic's
  headline workflow live: declare "Cd ≤ N" on a sphere, verify at solver tier, get a
  verdict whose measurement came from a real solve with its trust block attached.
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
- **Status: shipped.** `mechanism_kinematics` (exact closed-form planar gates —
  four-bar Grashof class + coupler path, slider-crank stroke ≡ 2·R, Grübler DOF;
  `analysis/kinematics.py`, toys in `tests/test_kinematics.py`) +
  `mechanism_simulate_submit` (PyBullet rigid-link dynamics behind the `mbd`
  extra, async with through-motion contact; `analysis/mbd.py`, gates in
  `tests/test_mbd.py` — kinematics gates the dynamics).

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
  in `cost.py`; `slice_estimate` (first-order FDM estimate) +
  `slice_gcode_submit` (the **shipped CLI upgrade**: the `prusaslicer` solver +
  headless argv builder + G-code footer/layer parser in `slicing.py` — a real
  PrusaSlicer slice of an exported body, async, degrading cleanly; live gate: a
  20 mm cube at 100% infill slices to within 1% of its exact volume). 21 two-sided toys across
  `tests/test_dfx.py` / `test_cost.py` / `test_slicing.py`. v1 takes **explicit**
  geometry summaries (face draft angles, bbox, volume) like `tolerance.py` takes an
  explicit chain. **v2 Shape wiring (issue #175):** `dfm_check` now also reads a
  live `handle` — per-face draft/undercut/wall descriptors are derived off the
  solid (`_dfm_face_descriptors`, the moldability face-classification machinery)
  and scored identically, with a golden parity test vs the hand-built descriptor.
  The tolerance half shipped too: `tolerance_stackup` reads a live `handle` —
  planar step faces perpendicular to the measurement `axis` become
  station-to-station links (ISO 2768-1 general tolerances by default,
  `default_tol` to override), stacked identically, with the same parity-test
  pattern (`test_worker.py::test_tolerance_stackup_handle_matches_hand_built_chain`).
- **Tier A screens (SIMULATION_NEXT):** `molding_screen`
  (`analysis/molding.py` — exact one-term cooling time t ∝ s² + the ±30 %
  spiral-flow fill check, per-polymer defaults, feeding dfm/cost) and
  `drop_impact` (`analysis/impact.py` — exact energy-balance G = h/d with
  pulse-shape bounds, fragility ↔ crush-stroke inversion for packaging). Toys in
  `tests/test_molding.py` / `tests/test_impact.py`.

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
  `belt_drive`, `press_fit_stress`, `seal_check`, `chain_drive` (ANSI roller-chain
  power rating, ASME B29.1) and `weld_group` (fillet-weld group, Blodgett
  treat-weld-as-a-line), all with hand-verified toys in
  `tests/test_machine_elements.py`.

### Frontier: low-frequency EM  ✅ shipped (P3 M6)

- **Answers:** "What's this conductor's resistance and Joule heat? How deep does
  induction heating reach at this frequency?"
- **Backend:** Elmer's `StatCurrentSolver` (DC) and `MagnetoDynamics2DHarmonic`
  (AC); exact closed forms in
  [`analysis/em.py`](../driftpin/analysis/em.py). The conductor σ / µ_r values
  live in the **Materials DB electrical layer** (`electrical_conductivity` +
  `relative_permeability` with provenance, issue #175) — `em_*` and `material_get`
  share one source of truth; `em.py` keeps a handbook fallback only for offline use.
- **Signatures (implemented):**
  ```
  em_skin_depth(frequency_hz, conductivity_s_m|conductor, mu_r)   # δ=√(2/ωμσ) + R_s, exact
  em_dc_resistance(length_mm, area_mm2, …, voltage_v)             # R=L/σA + Ohm/Joule, exact
  em_field(kind='wire'|'solenoid', …)                             # μ₀I/2πr · μ₀μ_r·n·I, exact
  em_conduction_submit(voltage_v, length_m, width_m, …)   # Elmer DC strip, async
    -> resistance_ratio == 1.000000 (machine-exact vs R=L/σA, live)
  em_induction_submit(frequency_hz, conductor, mu_r, …)   # Elmer harmonic skin slab, async
    -> decay_ratio/phase_ratio ≈ 1 (|A| AND phase e-fold at exactly δ; 0.1% live)
  em_induction_heating_submit(frequency_hz, a_surface, heat_duration_s, …)  # B5: the
    # harmonic solve + CalcFields Joule field + transient adiabatic HeatSolver — the
    # thermal answer: joule_power_ratio vs exact R_s|H₀|²/2 (live 1.0003) and
    # energy_balance_ratio ΔT=P·t/(m·cₚ) (live 1.005). tests/test_em.py
  ```
- Gates in `tests/test_em.py`; acceptance Example J + `em.png`. RF/wave (full-wave)
  EM shipped separately via openEMS (issue #93, `analysis/em_fullwave.py`).

### 11. Horizon (table-only)

| Domain | Tooling | Priority |
|---|---|---|
| Acoustics | Elmer, pyfar, acoular | **shipped, both tiers** — `acoustic_screen` (Tier A: exact cavity modes / duct cutoff + Helmholtz ±10 % + mass law ±3 dB) + `acoustic_fem_submit` (Tier B1: Elmer `HelmholtzSolve`, async — driven duct gated machine-tight on the exact 1/cos(kL) standing wave; flux-driven cavity sweep localizes the exact eigenfrequencies to <0.1 % via the in-phase sign flip). Both in `analysis/acoustics.py`; gates in `tests/test_acoustic_fem.py` |
| Electromagnetics (RF/wave) | OpenEMS / FEniCSx (RF), FEMM (2D motors) | **shipped** — full-wave via openEMS (issue #93, `analysis/em_fullwave.py`) |
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
6. **Slicer estimate** (PrusaSlicer CLI on STL). *(shipped: analytic `slice_estimate` + the `slice_gcode_submit` CLI upgrade)*
7. **Optics** (`rayoptics` wheel behind the `optics` extra). *(shipped, P3 M1: `optics_raytrace` + `optics_moldability_check`)*
8. **Cost** rollup (depends on Materials DB + a process-time model). *(shipped: `cost_estimate` in `cost.py`)*

**P2 — heavy external solvers.** Gated on async-solve + optional-extras packaging:
9. **CFD**, **transient/radiation thermal**, **topology optimization**, **MBD**.
   *(all shipped — P2 M5 / P3 M2-M5 / SIMULATION_NEXT B1-B5; see the family
   sections above for the gates)*

**Remaining stubs** (the only unbuilt items in this catalog; each family section
names its own):
- Horizon table row (§11) — machining toolpaths — on concrete need only.

*(Shipped since this list was last pruned: **tolerance v2 Shape wiring** —
`tolerance_stackup` reading a dimension chain off a live handle (planar step
faces along the measurement axis → station-to-station links, ISO 2768 general
tolerances as the untoleranced default), issue #175, closing the catalog's last
stub pair with the DfX half; **fit_class** interference/transition
shaft letters — k/m/n/p/r/s, issue #168, `analysis/tolerance.py`;
**chain/sprocket + weld-group ratings** — issue #175, `machine_elements.py`;
**materials DB electrical layer** — issue #175; **nonlinear structural** — CCX
plasticity / large deflection, issue #90 / PR #94, `analysis/nonlinear.py`,
SIMULATION_NEXT Tier B ✅; **RF/wave EM** — openEMS full-wave, issue #93,
`analysis/em_fullwave.py`.)*

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

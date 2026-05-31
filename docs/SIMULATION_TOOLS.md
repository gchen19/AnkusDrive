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

### 1. Tolerance & GD&T  *(anchor — fully specced in Appendix A)*

- **Answers:** "Will these parts fit? Tighten which dim to make the assembly fit 99.7%?"
- **Backend:** pure-Python (numpy for Monte-Carlo, already a dep). **Weight: none.**
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
- The largest agent unlock for the smallest build. **Ships first.**

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

### 4. Thermal (beyond steady-state CCX)

- **Answers:** "How hot does it get after 5 min? Does radiation matter?"
- **Backend:** lumped-mass quick estimates pure-Python; transient / radiation via
  Elmer or OpenFOAM `chtMultiRegionFoam`. **Weight: none (lumped) → heavy (CFD-class).**
- **Signatures:**
  ```
  thermal_lumped(mass_g, c_p, power_w, h_conv, area_mm2, t_ambient_c, duration_s)
  thermal_transient(analysis, duration_s, dt_s)        # Elmer-backed, later phase
  thermal_radiation(analysis, emissivity, view_factors)
  ```
- CCX already covers steady-state conduction via `fem_thermal_results`; this fills
  the *time* and *radiation* gaps. Lumped version ships in the pure-Python wave.

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

### 6. Fluids / CFD

- **Answers:** "What's the pressure drop through this manifold? Drag on this housing?"
- **Backend:** OpenFOAM (via CfdOF or directly) / SU2; Elmer for light cases.
  **Weight: heavy** (large install, long solves) — **later phase.**
- **Signatures:**
  ```
  cfd_internal_flow(model, inlet={flow_or_pressure}, outlet, fluid)
    -> {pressure_drop_pa, flow_rate, recirculation_zones}
  cfd_external_flow(model, velocity, fluid)
    -> {drag_n, lift_n, cd, cl}
  ```
- Gated on the async/long-solve work (below) — a CFD run can't block the MCP channel.

### 7. Optics

- **Answers:** "Where does the light land? Is this lens moldable? Optimize the BSpline."
- **Backend:** wrap the existing in-house `~/diffuser` pipeline (already imports
  FreeCAD, already runs under the worker); `rayoptics` / `optiland` for general lenses.
  **Weight: light** (in-house code exists).
- **Signatures:**
  ```
  optics_raytrace(model, source_config, n_refractive, n_rays)
    -> {exit_distribution, leakage_fraction, hotspot_locations}
  optics_moldability_check(model, pull_axis)
    -> {undercut_faces, draft_violations, wall_thickness_stats}
  optics_optimize(baseline, target_metrics, parameter_space)
    -> {best_candidate_path, score_breakdown}
  ```
- ROADMAP's original first pick; sequenced after tolerance here because it depends
  on an external (if in-house) pipeline rather than being self-contained math.

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

### 10. Horizon (table-only)

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
- **Async / long solves.** Already a known ROADMAP open issue (worker pool or async
  `fem_run`). CFD, transient thermal, and MBD make it *blocking* — these P2 families
  cannot ship until a long solve can run without holding the MCP channel.
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

   Geometry export (STEP/STL/mesh) ──┬──► CFD
                                     ├──► Optics
                                     └──► Slicer estimate

   async/long-solve infra ──────────────► CFD · transient thermal · MBD
```

**P0 — pure-Python, zero new deps, ship first.** Days each, no installs,
immediately useful:
1. **Tolerance / GD&T** (Appendix A) — validates the new `analysis/` extension point.
2. **Materials DB** — foundational; unblocks fatigue, fracture, cost.
3. **Wear / fatigue / fracture** — turns FEM stress into durability.
4. **DfM / DfA heuristics** + lumped thermal — grade against process, reuse existing tools.

**P1 — external CLI, self-contained.** One new tool dependency each, bounded runtime:
5. **Slicer estimate** (PrusaSlicer/OrcaSlicer CLI on STL).
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

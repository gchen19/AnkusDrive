# DriftPin simulation tools — dedicated sprint plan

The **execution sequence** for building out DriftPin's physical-simulation
algorithms and exposing them over MCP. Sits between the two existing docs:

- [`SIMULATION_TOOLS.md`](SIMULATION_TOOLS.md) — the *scope map* (what each of the
  11 families answers, its backend, its build risk, P0/P1/P2 priority).
- [`SIMULATION_EXAMPLES.md`](SIMULATION_EXAMPLES.md) — the *proof harness* (a
  concrete call sequence and a closed-form **toy with a known answer** per family).

This doc is the *plan of record*: it slices that catalog into **dedicated,
shippable sprints**, each a self-contained vertical that lands tools an agent can
call. Every acceptance criterion below is already written as a two-sided oracle in
`SIMULATION_EXAMPLES.md` — a sprint is "done" when that toy passes *and* its
negative is caught.

---

## Status snapshot

| Family (SIMULATION_TOOLS.md) | State |
|---|---|
| 2 · Materials & selection | ✅ **shipped** — `material_get` / `material_select` / `material_list`, seed corpus + CC0 optical layer (Sellmeier `refractive_index_at`) |
| 10 · Machine-element rating | ✅ **shipped** — bolt / bearing / spring / gear / belt / press-fit / seal, 12 hand-verified toys |
| 1 · Tolerance & GD&T | ✅ **shipped** — `tolerance_stackup` / `fit_check` / `fit_class` / `gdt_check`, 13 hand-verified toys (Sprint 1) |
| 3 · Wear / fatigue / fracture | ✅ **shipped** — `fatigue_check` / `fracture_check` / `wear_estimate` / `creep_flag`, 9 hand-verified toys (Sprint 2) |
| 4 · Thermal (lumped) | ✅ **shipped** — `thermal_lumped` (RC transient + radiation screen), 5 hand-verified toys (Sprint 3) |
| 9 · Design for X | ✅ **shipped (v1 explicit-input)** — `dfm_check` / `dfa_check` / `pack_check` / `cost_estimate` / `slice_estimate`, 21 hand-verified toys (Sprints 3–4) |
| 7 · Optics | ✅ **shipped** (P3 M1) — Snell/Fresnel/TIR exact oracle + `optics_raytrace` (rayoptics, degrades) + `optics_moldability_check`, 16 toys |
| 6 · Async / long-solve infra | ✅ **shipped** — `driftpin/jobs.py` (submit/poll/cache) + `job_status`/`job_result`/`job_list`, 7 toys (Sprint 6); unblocks all P2 |
| 6 · CFD · 5 · structural ext · 8 · MBD · 4 · transient thermal | ✅ **shipped** (P2 M0–M5 + P3) — OpenFOAM pipe/flat-plate vs Hagen–Poiseuille/Blasius, Elmer slab/radiation vs Heisler/σ-exchange, in-house SIMP (2-D+3-D), PyBullet MBD, Miles PSD + `beam_modal` vs CalculiX `fem_modal` |
| — · Geometry bridge | ✅ **shipped** (P3 M4) — `meshbridge.py`: FreeCAD solid → Gmsh/ElmerGrid (thermal) · STL/snappyHexMesh (internal flow), body modes on the `*_submit` tools |

Pure-Python `analysis/` is FreeCAD-free and standalone-testable; that extension
point is proven across every family above. The P3 M6 frontier (modal, conjugate
heat transfer, low-frequency EM) shipped with
[`SIMULATION_P3_KICKOFF.md`](SIMULATION_P3_KICKOFF.md); the only item still open
from this plan is the Sprint 4 **slicer external-CLI upgrade** (PrusaSlicer/Orca
behind an extra).

---

## Sprint anatomy (every sprint is this vertical slice)

The pattern families 2 and 10 already shipped, repeated verbatim per sprint:

1. **`driftpin/analysis/<module>.py`** — the math. No FreeCAD import. Pure
   functions returning a small typed dict with a `pass` bool.
2. **`driftpin/worker.py`** — `@handler("<tool>")` shims calling into the module.
3. **`driftpin/mcp_server.py`** — thin `@mcp.tool()` wrappers (intent-encoded
   params, documented return shape).
4. **`tests/test_<module>.py`** — the two-sided toys from `SIMULATION_EXAMPLES.md`,
   wired into `tests/run_all.sh`. Prove the oracle catches the negative first.
5. **Contract parity** — `tests/test_contracts.py` must stay green (every tool
   dispatches to a real handler, documents its return value).

**Encode intent, not solver flags** — `fit='clearance'`, `process='cnc'`,
`method='rss'`; never a raw FreeCAD or solver flag on the tool surface.

---

## P0 — pure-Python, zero deps, no async (ship next)

### Sprint 1 — Tolerance & GD&T ✅ shipped
- **Goal:** "Will these parts fit? Tighten which dim to hit 99.7%?"
- **Tools:** `tolerance_stackup(chain, method, samples)` · `fit_check(hole, shaft)`
  · `fit_class(basic_size, fit)` · `gdt_check(feature, control, zone, datum_refs)`.
- **Backend / deps:** pure-Python; numpy (already a dep) for Monte-Carlo. None new.
- **Reuses:** nothing — v1 takes explicit chains (no geometry read), per Appendix A.
- **Acceptance:** three 10.00 ±0.10 links → worst-case ±0.30, RSS ±0.173;
  Monte-Carlo `pct_in_spec` converges to RSS within ±0.3% at 10 000 samples; ISO 286
  `H7/g6` on Ø20 reproduces handbook deviations. **Negatives:** an interference pair
  → `fit_class:"interference"`, `prob_interference > 0`.
- **Depends on:** nothing. **Shipped** — `driftpin/analysis/tolerance.py` +
  worker/MCP wiring + `tests/test_tolerance.py` (13 toys, incl. the §1 gap-stack
  reproduction at Cpk≈1.49 and the ISO 286 H7/g6 handbook check). v1 `fit_class`
  covers hole-basis H + clearance shaft letters (h, g, f, e); interference letters
  (p, s, …) are the documented next extension.

### Sprint 2 — Wear / Fatigue / Fracture ✅ shipped
- **Goal:** turn a one-shot FEM stress into a durability verdict.
- **Tools:** `fatigue_check` (S-N + Goodman) · `fracture_check` (LEFM K vs K_IC) ·
  `wear_estimate` (Archard) · `creep_flag` (service-temp screen).
- **Backend / deps:** pure-Python closed-form. None new.
- **Reuses:** the **shipped Materials DB** (σ_e, σ_uts, K_IC) + existing
  `fem_results` stress output. Highest leverage on what already ships.
- **Acceptance:** edge crack Y≈1.12, σ=150 MPa, a=2 mm → K=13.3 MPa·√m, vs
  AL6061 K_IC≈29 → SF≈2.2; invert to a_c≈9.5 mm at K=K_IC. Goodman σ_a=90/σ_m=40 on
  6061 → SF≈0.94 `pass:false`. **Negatives:** tensile mean > UTS forces `pass:false`
  regardless of cycles; crack > a_c reports negative margin, never a positive SF.
- **Depends on:** Materials DB (done). **Shipped** — `driftpin/analysis/durability.py`
  + worker/MCP wiring + `tests/test_durability.py` (9 toys: LEFM K=13.3 / a_c=9.5 mm,
  Goodman SF=0.94 with the pass/fail crossing, Archard 45 mm³, two-sided negatives
  for static overload, past-critical-crack, and missing material data). Strengths,
  endurance, toughness and service temp read from the Materials DB with overrides.

### Sprint 3 — Lumped thermal + Design-for-X heuristics ✅ shipped
- **Goal:** "How hot after 5 min? Grade this part for its process and cost."
- **Tools:** `thermal_lumped` · `dfm_check` · `dfa_check` · `pack_check` ·
  `cost_estimate` — all shipped.
- **Status:** `thermal_lumped` in `analysis/thermal.py` (5 toys); `dfm_check` /
  `dfa_check` / `pack_check` in `analysis/dfx.py` and `cost_estimate` in
  `analysis/cost.py` (15 toys total) built in parallel. **Design decision:** the DfX
  checks ship as **v1 explicit-input** functions — `dfm_check` takes pre-computed
  face draft angles, `pack_check`/`cost_estimate` take a bbox/volume — exactly as
  `tolerance.py` takes an explicit chain. This keeps them in the FreeCAD-free toy
  harness; reading those summaries off a `Shape` (via `draft`/`thickness`/
  `query_faces`/`mass_properties`) is the v2 wiring. `cost_estimate`'s
  `material_cost = volume·density·price` is exact; its process/tooling model is a
  documented heuristic.
- **Backend / deps:** pure-Python. None new.
- **Reuses:** existing `draft`, `thickness`, `query_faces`, `interference_check`,
  `mass_properties`, `envelope_check` + Materials DB price/density.
- **Acceptance:** RC warm-up m=0.12 kg, c_p=900, P=15 W, h=12, A=0.02 m² →
  τ=450 s, ΔT_ss=62.5 K, T(300 s)=55.4 °C (exact exponential). `cost_estimate`
  `material_cost = volume × density × price` to rounding. **Negatives:** 0° draft box
  lists every side face; part > carton → `fits:false`.
- **Depends on:** Materials DB (done).

---

## P1 — one external CLI each, bounded runtime

### Sprint 4 — Slicer estimate 🟡 first-order shipped
- **Tools:** `slice_estimate(volume_mm3, bbox_mm, material, infill_fraction,
  layer_height_mm, …)` → mass, filament, deposited volume, layer count, print time.
- **Status:** the **analytic first-order estimator** shipped in
  `analysis/slicing.py` (6 toys, `tests/test_slicing.py`) — `filament_g == mass_g`
  at 100% infill, `layer_count = ceil(height/layer_height)`, print time from nozzle
  volumetric flow. The **external-CLI upgrade** (PrusaSlicer/OrcaSlicer on an
  exported STL, behind an optional extra with graceful degradation — adding real
  supports, travel/accel, per-feature speeds) is still pending.
- **Acceptance (met):** `filament_g == mass_g` for a solid 100%-infill print;
  strictly less filament at 20% infill; finer layer height → more layers + longer
  print. **CLI-upgrade negative (pending):** missing CLI returns
  `{ok:false, reason:"slicer not installed"}`, not a stack trace.

### Sprint 5 — Optics ✅ shipped (P3 M1)
- **Tools:** `optics_raytrace(model, source_config, n_refractive, n_rays)` ·
  `optics_moldability_check(model, pull_axis)`.
- **Backend / deps:** wrap the in-house `~/diffuser` pipeline (already imports
  FreeCAD, already runs under the worker). The **optical corpus this needs already
  shipped** with family 2 (`refractive_index_at` Sellmeier eval), so this is
  de-risked to a wrap rather than a from-scratch build.
- **Acceptance:** flat PMMA (n=1.49) at 30° → refract 19.6°, Fresnel 3.9% at normal
  incidence; energy closes `leakage + efficiency + absorbed ≈ 1.0` within 1%.
  **Negatives:** above θ_c=42.2° zero transmission; a re-entrant feature populates
  `undercut_faces`.

---

## Cross-cutting enabler (critical path for all of P2)

### Sprint 6 — Async / long-solve infrastructure ✅ shipped
- **Goal:** run a multi-minute solve without holding the MCP channel; cache by
  content-hash so unchanged work isn't re-solved.
- **Status: shipped** as a **FreeCAD-free `driftpin/jobs.py`** facility — a job
  registry + background-thread runner + content-hash cache + bounded eviction,
  generalizing the render-job pattern (`render_photoreal_submit` + `render_job`)
  into one shared poll surface. MCP surface: `async_demo_submit` (the reference
  long-solve) + generic `job_status` / `job_result` / `job_list` that **every**
  future `*_submit` reuses. 7 two-sided toys in `tests/test_jobs.py` (deterministic
  via a `threading.Event`, no sleeps) + an end-to-end worker check: submit returns
  in ~0.01 s, `ping` answers in ~0 s **while a 1.5 s job runs**, the result arrives,
  and an identical resubmit is a cache hit.
- **Threading contract (documented in `jobs.py`):** the submitted callable runs on
  a background thread, so it must NOT touch FreeCAD. Two safe shapes — pure-Python
  compute, or *polling an external subprocess* (ccx/OpenFOAM/slicer) whose
  FreeCAD-side setup ran on the calling thread first, exactly how the renderer
  executor backgrounds only its subprocess. That is the wiring path for the P2
  solves below.
- **Depends on:** nothing — but **everything in P2 depends on it** (now unblocked).

---

## P2 — heavy external solvers (each gated on Sprint 6)

### Sprint 7 — CFD ✅ shipped (P2 M2 + P3 M3/M4)
- **Tools:** `cfd_internal_flow` · `cfd_external_flow`. OpenFOAM (CfdOF/SU2) behind
  `pip install driftpin[cfd]`.
- **Acceptance:** straight Ø10 mm pipe, water, Re≈1700 → Hagen–Poiseuille
  Δp≈53 Pa within 10%; halving D raises laminar Δp ~16× (D⁴). **Negative:** missing
  OpenFOAM → graceful "solver not installed".

### Sprint 8 — Structural extensions ✅ shipped (P2 M3/M4 + P3 M5/M6-modal)
- **Tools:** `random_vibration(analysis, psd_profile)` (PSD math on `fem_modal`) ·
  `contact_setup` (promotes existing CCX flags) · `topology_optimize` (returns
  *geometry* — closes the loop back into the modeller).
- **Acceptance:** Miles' equation GRMS = √((π/2)·f_n·W·Q) within 10% for a near-SDOF
  part; doubling Q raises GRMS by √2. `topology_optimize` result gated by
  `interference_check` + `mass_properties` (mass ≤ keep_fraction·original).

### Sprint 9 — Transient/radiation thermal + Multibody dynamics ✅ shipped (P2 M1/M5 + P3 M2)
- **Tools:** `thermal_transient` / `thermal_radiation` (Elmer) ·
  `mechanism_simulate(assembly, joints, drivers, duration_s)` (MuJoCo/PyBullet).
- **Acceptance (MBD):** slider-crank stroke = 2R within 0.5% (independent of conrod
  length); four-bar `mobility_dof`=1 (Gruebler 3·3−2·4); a Grashof-violating link set
  reports "cannot fully rotate"; a part overlapping at one crank angle lists exactly
  that angle in `collisions_through_motion` — time-varying interference a static
  `interference_check` misses.

---

## Cross-cutting notes

- **Pure-Python fast-path (optional, benefits every P0 sprint).** Today even
  FreeCAD-free tools (`material_*`, the rating tools, and the P0 sprints above)
  route through `_call()` → the `freecadcmd` worker. An in-process dispatch path for
  pure-Python tools would cut latency and let them answer with no live worker —
  worth a small cross-cutting task since the bulk of this roadmap is pure-Python.
- **Merge gates** ([`MULTI_AGENT.md`](MULTI_AGENT.md)). Every tool returns a numeric
  margin + `pass` bool, so each sprint's output drops straight into a deterministic
  merge gate ("every rated element passes, every fit is within budget") — the same
  discipline as the existing interference/envelope gates.
- **Prove the oracle first.** Per `SIMULATION_EXAMPLES.md`: run the correct build →
  must pass; run each negative → must be caught (`M2_SELFTEST=1`). A gate that never
  fails is worthless. Prefer *relative* gates for solver families (FEM/CFD/MBD) so a
  consistent solver offset cancels; use absolute closed-form bands only where the
  physics is exact (tolerance, Snell, Hagen–Poiseuille, slider-crank stroke).

---

## Definition of done (per sprint)

1. `analysis/<module>.py` lands, FreeCAD-free, importable standalone.
2. Worker handlers + typed MCP wrappers, intent-encoded params, documented returns.
3. `tests/test_<module>.py` wired into `run_all.sh`; every toy two-sided; oracle
   self-test proves the negatives are caught.
4. `tests/test_contracts.py` green (registry parity + docstrings).
5. External-solver sprints (4, 5, 7, 9) degrade gracefully when the solver is absent.

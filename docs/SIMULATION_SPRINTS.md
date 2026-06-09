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
| 1 · Tolerance & GD&T | 📋 specced (Appendix A) — **not built** (`analysis/tolerance.py` absent) |
| 3 · Wear / fatigue / fracture | 📋 specced + toys — not built |
| 4 · Thermal (lumped) | 📋 specced + toys — not built |
| 9 · Design for X | 📋 specced + toys — not built |
| 7 · Optics | 📋 specced; optical **corpus already shipped** with family 2 |
| 6 · CFD · 5 · structural ext · 8 · MBD · 4 · transient thermal | 📋 specced — gated on async-solve infra |

Pure-Python `analysis/` is FreeCAD-free and standalone-testable; that extension
point is proven by families 2 and 10. The remaining P0 families slot into the same
shape with zero new dependencies.

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

### Sprint 1 — Tolerance & GD&T
- **Goal:** "Will these parts fit? Tighten which dim to hit 99.7%?"
- **Tools:** `tolerance_stackup(chain, method, samples)` · `fit_check(hole, shaft)`
  · `fit_class(basic_size, fit)` · `gdt_check(feature, control, zone, datum_refs)`.
- **Backend / deps:** pure-Python; numpy (already a dep) for Monte-Carlo. None new.
- **Reuses:** nothing — v1 takes explicit chains (no geometry read), per Appendix A.
- **Acceptance:** three 10.00 ±0.10 links → worst-case ±0.30, RSS ±0.173;
  Monte-Carlo `pct_in_spec` converges to RSS within ±0.3% at 10 000 samples; ISO 286
  `H7/g6` on Ø20 reproduces handbook deviations. **Negatives:** an interference pair
  → `fit_class:"interference"`, `prob_interference > 0`.
- **Depends on:** nothing. **This is the recommended first sprint** — it's already
  fully specced (Appendix A ships the scaffold) and is the last unbuilt P0 anchor.

### Sprint 2 — Wear / Fatigue / Fracture
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
- **Depends on:** Materials DB (done). Optionally Sprint 1's `analysis/` precedent.

### Sprint 3 — Lumped thermal + Design-for-X heuristics
- **Goal:** "How hot after 5 min? Grade this part for its process and cost."
- **Tools:** `thermal_lumped` · `dfm_check` · `dfa_check` · `pack_check` ·
  `cost_estimate`.
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

### Sprint 4 — Slicer estimate
- **Tools:** `slice_estimate(model, profile, material)` → print time, support volume,
  layers, filament mass.
- **Backend / deps:** PrusaSlicer / OrcaSlicer / CuraEngine CLI on exported STL,
  behind an optional extra; **degrade gracefully** when the CLI is absent.
- **Acceptance:** `filament_g == mass_g` for a solid 100%-infill print
  (density·volume); drops ~proportionally at 20% infill; print time rises
  draft→standard→fine. **Negative:** missing CLI returns the
  `{ok:false, reason:"solver not installed"}` dict, not a stack trace.

### Sprint 5 — Optics
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

### Sprint 6 — Async / long-solve infrastructure
- **Goal:** run a multi-minute solve without holding the MCP channel; cache by
  geometry content-hash so an unchanged part isn't re-solved.
- **Scope:** worker pool / async `fem_run`; `submit → job-id → poll` tool pair;
  content-hash cache keyed like the multi-agent lockfile.
- **Key reuse — do not reinvent:** the **render pipeline already ships this exact
  pattern** (`render_photoreal_submit` + `render_job` poll, with bounded async-job
  memory). Lift that submit/poll/cache template; it is the lowest-risk path to the
  async primitive every P2 family blocks on.
- **Acceptance:** a long solve returns a job id immediately, the channel stays
  responsive, polling yields the result, and a re-submit of an unchanged part is a
  cache hit (no re-solve).
- **Depends on:** nothing — but **everything in P2 depends on it.** Sequence it
  before any P2 sprint.

---

## P2 — heavy external solvers (each gated on Sprint 6)

### Sprint 7 — CFD
- **Tools:** `cfd_internal_flow` · `cfd_external_flow`. OpenFOAM (CfdOF/SU2) behind
  `pip install driftpin[cfd]`.
- **Acceptance:** straight Ø10 mm pipe, water, Re≈1700 → Hagen–Poiseuille
  Δp≈53 Pa within 10%; halving D raises laminar Δp ~16× (D⁴). **Negative:** missing
  OpenFOAM → graceful "solver not installed".

### Sprint 8 — Structural extensions
- **Tools:** `random_vibration(analysis, psd_profile)` (PSD math on `fem_modal`) ·
  `contact_setup` (promotes existing CCX flags) · `topology_optimize` (returns
  *geometry* — closes the loop back into the modeller).
- **Acceptance:** Miles' equation GRMS = √((π/2)·f_n·W·Q) within 10% for a near-SDOF
  part; doubling Q raises GRMS by √2. `topology_optimize` result gated by
  `interference_check` + `mass_properties` (mass ≤ keep_fraction·original).

### Sprint 9 — Transient/radiation thermal + Multibody dynamics
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

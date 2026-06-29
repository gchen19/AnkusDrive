# Kickoff — standing up the P2 simulation families (external solvers)

The async/long-solve infrastructure ([`driftpin/jobs.py`](../../driftpin/jobs.py),
Sprint 6) is the last cross-cutting blocker, and it shipped. This doc kicks off the
**P2 tier** — the heavy-solver families that ride on it: CFD, multibody dynamics,
topology optimization, and transient/radiation thermal. It is the onboarding +
provisioning plan, the way [`RENDER_RENDERER_INSTALL.md`](../RENDER_RENDERER_INSTALL.md)
was for the external renderers.

It complements:
- [`SIMULATION_TOOLS.md`](../SIMULATION_TOOLS.md) — family catalog + result schemas (families 5–8).
- [`SIMULATION_EXAMPLES.md`](../SIMULATION_EXAMPLES.md) — the closed-form **acceptance toy** per family.
- [`SIMULATION_SPRINTS.md`](SIMULATION_SPRINTS.md) — sprint sequence (Sprints 7–9).

---

## Where we are

**In place:** the materials DB, the entire pure-Python closed-form wave (tolerance,
durability, lumped thermal, DfX, cost, slicing, machine-element rating — 6 families,
89 toys), and the async facility. Solves can now run off the MCP channel with a
`*_submit` → `job_status`/`job_result` poll surface and content-hash caching.

**Missing for P2 — three things, none of them physics:**
1. **The external solvers themselves** (OpenFOAM, MuJoCo/PyBullet, a topology
   optimizer, Elmer) — not installed; not redistributable in the repo.
2. **The provisioning + discovery + packaging glue** — the exact pattern the
   renderers already use (`render_capabilities`, `scripts/install-renderers.sh`,
   `install_hint` graceful degradation, optional extras).
3. **The per-family wiring** — export → background-solve → parse, against the
   `jobs.py` threading contract.

This doc kicks off (2) and (3). It also flags two families that need **no external
solver at all** and should ship first.

---

## The contract every P2 family follows

One shape, reused from the render async path and `jobs.py`:

1. **Compose + export geometry on the main thread.** STEP/STL/mesh out of FreeCAD,
   exactly as `render_photoreal_submit` exports its scene *before* launching.
2. **`<family>_submit`** builds a content key and calls
   `jobs.submit(kind, fn, key=jobs.content_key(kind, payload), meta=…)`, returning
   `{job_id, status, cache_hit}` immediately.
3. **The background `fn` runs ONLY the solver subprocess** — `subprocess.run([...])`
   over the exported files. **It must not touch FreeCAD** (the `jobs.py` threading
   contract; FreeCAD's document API is not thread-safe). This is the same reason the
   renderer executor backgrounds only its subprocess.
4. **Poll** with the shared `job_status` / `job_result` — no per-family poll tool.
5. **Parse solver output → a small typed dict on the main thread**, once the job is
   done (numbers, never a raw solver dump).
6. **Degrade gracefully + discover + cache:** a missing solver returns
   `{ok:false, reason:"solver not installed", install:"…"}` (never an import crash);
   `solve_capabilities` reports what resolves right now; identical inputs hit the
   `jobs.py` cache instead of re-solving.

```text
cfd_internal_flow_submit(model="body_1", inlet={…}, outlet={…}, fluid="water-20c")
  -> export STEP/mesh (main thread)
  -> jobs.submit("cfd_internal_flow", run_openfoam_subprocess, key=…)  -> {job_id}
job_result(job_id)   # poll the shared surface
  -> {pressure_drop_pa, reynolds, regime, residuals_converged, solver}
```

---

## Provisioning & packaging — mirror the renderers

The renderers already solved "an external binary the agent must discover, install,
and degrade around." Copy it verbatim for solvers:

- **`solve_capabilities` (new MCP tool + handler)** — the `render_capabilities`
  twin: for each solver, resolve the binary (`DRIFTPIN_<SOLVER>_PATH` env → PATH →
  per-OS install dirs) **without running it**, and report `{available, binaries,
  path|install_hint}`. The agent picks a working solver instead of trial-and-error.
- **`_require_solver(name)` helper** — the `_require_render` twin: raise/return the
  structured `{ok:false, reason, install}` dict so every family degrades identically.
- **`scripts/install-solvers.sh`** — the `install-renderers.sh` twin: idempotent,
  checksum-verified, writes PATH wrappers; for solvers that are pip wheels
  (PyBullet, MuJoCo, topology libs) it's a thin `pip install` into the worker env.
- **Optional extras** in `pyproject.toml` `[project.optional-dependencies]`:
  `cfd`, `mbd`, `optics`, `topology` — so `pip install driftpin[mbd]` pulls the
  pip-installable solvers; the apt/conda ones (OpenFOAM, Elmer) are documented, not
  vendored.
- **Sandbox note** (same as the renderers): install + wiring + discovery are
  CI-verifiable on any box; **executing** a heavy solver belongs on the
  **self-hosted FreeCAD runner** that already runs the gated suite. Degradation
  tests (solver absent → clean dict) gate every PR; full end-to-end solves run only
  where the solver is provisioned.

---

## Per-family kickoff

Ordered by ascending install weight — **do the no-new-dependency wins first.**

| Family | Solver | Install | Tool(s) | Analytic toy (EXAMPLES) |
|---|---|---|---|---|
| Random vibration | *none* — PSD math on `fem_modal_results` | — | `random_vibration` | Miles' GRMS = √((π/2)·f_n·W·Q) |
| Contact | *none* — existing CCX flags | — | `contact_setup` | relative vs bonded reference |
| MBD | PyBullet **or** MuJoCo | `pip` (extra) | `mechanism_simulate_submit` | slider-crank stroke = 2R; four-bar DOF = 1 |
| Topology | `topopt`/`solidspy` (or FEniCS) | `pip` (extra) | `topology_optimize_submit` | mass ≤ keep_fraction; returns geometry |
| Transient thermal | Elmer | apt/conda | `thermal_transient_submit` | relative vs `thermal_lumped` at small Biot |
| CFD | OpenFOAM (or SU2) | apt/conda | `cfd_internal_flow_submit`, `cfd_external_flow_submit` | straight-pipe Hagen–Poiseuille ±10% |

### 0. Quick wins — no external solver (ship like P0)
- **`random_vibration`** — pure-Python PSD math layered on the **existing**
  `fem_modal` / `fem_modal_results`. Miles' equation is an exact SDOF anchor; the
  full part is a relative gate (monotone in f_n, +√2 per Q-doubling). No new
  dependency, no `jobs.py` needed (modal already runs). **This is the lowest-risk
  first PR of the P2 tier** and proves the structural-extension surface.
- **`contact_setup`** — promotes the CCX nonlinear/contact flags already partly
  exposed in the FEM path; gated relative to a bonded reference under the same mesh.

### 1. MBD — lightest external dep
**PyBullet** (`pip install pybullet`) or **MuJoCo** (`pip install mujoco`, now
open-source) — both pip wheels, so `driftpin[mbd]` is a clean install with no system
package. `mechanism_simulate_submit(assembly, joints, drivers, duration_s)` exports
the link geometry/inertias, runs the engine in the background job, returns
`{trajectories, max_torques, collisions_through_motion, reachable_envelope,
mobility_dof}`. Toys are **exact**: slider-crank stroke = 2R (independent of conrod),
four-bar Grübler DOF = 1, Grashof feasibility, and a one-angle collision the static
`interference_check` misses. **Recommended first external family** — easiest install,
hardest-edged toys.

### 2. Topology optimization — pip, returns geometry
A pure-Python optimizer (`topopt`, `solidspy`, or the classic 88-line SIMP) keeps
the install to a wheel; FEniCS is the heavier upgrade. `topology_optimize_submit(
body, load_cases, keep_fraction, keep_out_regions)` **returns geometry**, so its gate
is geometric: `mass_properties` (mass ≤ keep_fraction·original) + `interference_check`
against keep-outs, plus a tip-stiffness bound. This is the one P2 family that closes
the loop back into the modeller.

### 3. Transient / radiation thermal — Elmer
Elmer (apt `elmerfem-csc` / conda) fills the *time* and *radiation* gaps the lumped
model only screens. `thermal_transient_submit(analysis, duration_s, dt_s)`. Gate
**relative** to `thermal_lumped` in the lumped limit (small Biot number the two must
agree), and absolute against an analytic 1-D transient where the mesh is trivial.

### 4. CFD — heaviest, do last
OpenFOAM (apt/conda, or via the FreeCAD **CfdOF** workbench) or SU2.
`cfd_internal_flow_submit` / `cfd_external_flow_submit`. The unambiguous gate is the
**straight circular pipe**: laminar Δp = 128·μ·L·Q/(π·D⁴) (Hagen–Poiseuille) within
10%, and the D⁴ scaling law (halving D → ~16× Δp) catches a mis-scaled solver. Long
solves + large install → last, fully behind `driftpin[cfd]` and the async path.

---

## Milestones

```
M0  Provisioning glue   ✅ solve_capabilities · _require_solver · install-solvers.sh
                             skeleton · driftpin[mbd|cfd|topology|optics] extras ·
                             the degradation contract + its CI test (solver absent)
M1  Structural (no new dep)   random_vibration (Miles) ✅ · contact_setup ✅
M2  MBD                ✅ mechanism_simulate_submit (PyBullet) + mechanism_kinematics
M3  Topology           ✅ topology_optimize_submit (in-house SIMP) — returns geometry
                          ✅ topology_to_solid — density field → fused FreeCAD solid
M4  Transient thermal  ✅ thermal_transient_1d (analytic) + thermal_transient_submit (Elmer)
                          ✅ slab case builder — real ElmerSolver gated vs the Heisler oracle
M5  CFD                ✅ cfd_pipe_flow (Hagen–Poiseuille) + cfd_{internal,external}_flow_submit
                          ✅ axisymmetric pipe builder — real blockMesh+simpleFoam gated vs Hagen–Poiseuille
```

> **Status (branch `feat/sim-p2-provisioning`):** M0 landed —
> [`driftpin/solvers.py`](../../driftpin/solvers.py) (FreeCAD-free registry +
> env→PATH→per-OS resolution), `solve_capabilities` + `_require_solver` in the
> worker/MCP surfaces, [`scripts/install-solvers.sh`](../../scripts/install-solvers.sh),
> the `mbd/topology/optics/cfd` extras, and the gating degradation test
> [`tests/test_solve_degradation.py`](../../tests/test_solve_degradation.py). M1
> `random_vibration` landed — [`driftpin/analysis/vibration.py`](../../driftpin/analysis/vibration.py)
> + [`tests/test_vibration.py`](../../tests/test_vibration.py) (Miles toy: f_n=312 Hz,
> W=0.01, Q=10 → 7.0 g). Still open: `contact_setup`, then M2–M5.
>
> **Status (branch `feat/sim-p2-mbd`, stacked on the above):** M2 landed — the first
> external family, proving the M0 provisioning glue end-to-end.
> [`driftpin/analysis/kinematics.py`](../../driftpin/analysis/kinematics.py) is the
> closed-form, solver-free gate (Grübler DOF, Grashof, slider-crank stroke = 2R,
> four-bar sweep) behind the `mechanism_kinematics` tool;
> [`driftpin/analysis/mbd.py`](../../driftpin/analysis/mbd.py) is the PyBullet executor
> behind `mechanism_simulate_submit` (async via `jobs.py`, `_require_solver('pybullet')`
> degradation). Toys: [`tests/test_kinematics.py`](../../tests/test_kinematics.py) (exact,
> fast lane) + [`tests/test_mbd.py`](../../tests/test_mbd.py) (pendulum torque = m·g·L/2,
> swept envelope, through-motion contact; skips when PyBullet is absent).
>
> **Status (branch `feat/sim-p2-remaining`, stacked on the above):** the rest of the
> P2 tier landed — M1 `contact_setup` (CCX surface contact + nonlinear flag), M3
> `topology_optimize_submit` (in-house NumPy SIMP — no new dep — in
> [`driftpin/analysis/topology.py`](../../driftpin/analysis/topology.py)), M4
> `thermal_transient_1d` (analytic Heisler oracle) + `thermal_transient_submit`
> (Elmer), M5 `cfd_pipe_flow` (Hagen–Poiseuille) + `cfd_{internal,external}_flow_submit`
> (OpenFOAM/SU2). The pure-Python oracles (SIMP volume/compliance, 1-D transient vs
> lumped, Hagen–Poiseuille + D⁴) are gated on the fast lane
> ([`test_topology.py`](../../tests/test_topology.py), [`test_cfd.py`](../../tests/test_cfd.py),
> [`test_thermal.py`](../../tests/test_thermal.py)); the Elmer/OpenFOAM **execution** paths
> degrade cleanly when the binary is absent (verified) and run only on the provisioned
> runner. **The P2 tier (M0–M5) is complete; the heavy-solver case-from-FreeCAD export
> and topology→solid reconstruction remain as follow-ons.**
>
> **Status (branch `feat/sim-p2-topo-solid`, stacked on the above):** the
> topology→solid follow-on landed — `topology_to_solid` closes the loop back into
> the modeller. The pure-Python core `density_to_rects`
> ([`driftpin/analysis/topology.py`](../../driftpin/analysis/topology.py)) thresholds a
> `topology_optimize_submit` density grid and run-length-merges each row into maximal
> solid spans (so the kernel fuses O(runs), not O(cells), boxes); the
> `topology_to_solid` worker handler ([`driftpin/worker.py`](../../driftpin/worker.py))
> tiles each span as a `cell_mm`×`thickness_mm` box, `multiFuse`+`removeSplitter`s
> them into one static `Part::Feature`, and reports `{volume, mass_fraction, n_solids,
> bbox_mm, …}`. It runs **synchronously** (it builds geometry — unlike the `*_submit`
> solves it does NOT touch `jobs.py`), the modeller-side counterpart to the async
> optimizer. Toys: [`test_topology.py`](../../tests/test_topology.py) gates
> `density_to_rects` (run merging, thresholding, validation) on the fast lane;
> [`test_worker.py`](../../tests/test_worker.py) gates the live reconstruction
> end-to-end (full grid cell-tiled exactly, a holed frame, a split-load-path two-solid
> result, and the all-void-threshold clean error). Verified end-to-end: a real SIMP
> run's density reconstructs to a valid solid whose thresholded mass fraction is
> ≤ `keep_fraction` (the SIMULATION_EXAMPLES §5 geometric gate). **Only the
> heavy-solver case-from-FreeCAD export (Elmer/OpenFOAM/SU2) now remains as a P2 follow-on.**
>
> **Status (branch `feat/sim-p2-solver-cases`, stacked on the above):** the
> heavy-solver case-build follow-on landed — with ElmerSolver/ElmerGrid (v26.2) and
> OpenFOAM (1912) **provisioned on the runner**, both families now build their case
> from physical parameters, run the real solver, and gate against the analytic oracle.
> - **M4 Elmer** — [`driftpin/analysis/elmer.py`](../../driftpin/analysis/elmer.py)
>   generates the 1-D plane-wall transient case (native Elmer mesh + `.sif`, symmetry
>   at the centre, convection at the surface, SaveScalars max/min → centre/surface
>   temps). `thermal_transient_submit` now takes the slab params (or still a prepared
>   `case_dir`), runs ElmerSolver via `jobs.py`, and returns the temperatures —
>   matching `thermal_transient_1d` (Heisler) to **< 0.1 %** and the lumped limit at
>   small Biot. Toys: [`test_elmer.py`](../../tests/test_elmer.py) (structure always;
>   solver gate when ElmerSolver resolves).
> - **M5 CFD** — [`driftpin/analysis/openfoam.py`](../../driftpin/analysis/openfoam.py)
>   generates the **axisymmetric wedge pipe** (collapsed-axis blockMesh + simpleFoam,
>   laminar); Δp is read straight from the converged `p` field (the `surfaceFieldValue`
>   function object is broken in this build). `cfd_internal_flow_submit` now takes the
>   pipe params (or a prepared `case_dir`), runs blockMesh+simpleFoam via `jobs.py`, and
>   returns the solved Δp next to the Hagen–Poiseuille reference — **`hp_ratio` ≈ 1.00**
>   (within ~1 %), and the **D⁴ scaling law** holds (halving the bore → ~16× Δp). Toys:
>   [`test_openfoam.py`](../../tests/test_openfoam.py). The OpenFOAM env-sourcing bug
>   (`solvers.openfoam_bashrc()`, sourced in `_run_foam`) is fixed — foam apps need
>   `WM_PROJECT_DIR` exported. **All P2 milestones (M0–M5) and both named follow-ons
>   are now complete and oracle-gated end-to-end on the provisioned runner.**

Each Mn is a vertical slice in the established pattern: a module/handler/tool, the
`*_submit` wired through `jobs.py`, a graceful-degradation path, and the two-sided
toy from `SIMULATION_EXAMPLES.md` promoted into the gate harness.

---

## Verification discipline

- **Relative gates for solvers.** Compare the solve to a *scripted reference solved
  under the same mesh + solver setup*, so a consistent solver offset cancels — the
  same rule `TOYS.md` uses for FEM. Use absolute closed-form bands only where the
  physics is exact: Hagen–Poiseuille, slider-crank stroke, Miles' equation.
- **Degradation tests gate every PR and need no solver.** Assert that with the
  binary absent, `<family>_submit` (and `solve_capabilities`) return the
  `{ok:false, reason, install}` dict and never raise — runnable on the no-FreeCAD
  CI lane.
- **Heavy end-to-end solves run on the provisioned/self-hosted runner only** — the
  same box that runs the gated FreeCAD suite and the renderers. Keep them out of the
  fast lane.
- **Async correctness is already covered** by `tests/test_jobs.py`; a P2 family only
  adds its *solver* assertions, not new job-lifecycle ones.

---

## First-PR checklist (M0 — the provisioning slice)

1. `solve_capabilities` MCP tool + handler — resolve each solver binary/wheel
   without executing it; mirror `render_capabilities` (incl. `install_hint`).
2. `_require_solver(name)` in `worker.py` — the structured-degradation helper, with
   the `DRIFTPIN_<SOLVER>_PATH` env → PATH → per-OS-dir resolution order.
3. `scripts/install-solvers.sh` skeleton — pip extras for MBD/topology now; documented
   apt/conda steps for OpenFOAM/Elmer; idempotent + checksum-verified like
   `install-renderers.sh`.
4. `pyproject.toml` — add `mbd` / `cfd` / `topology` / `optics` optional-dependency
   groups.
5. `tests/test_solve_degradation.py` — solver-absent path returns the clean dict for
   every planned `*_submit`; wire into `run_all.sh` (no-FreeCAD lane).
6. Land **M1 `random_vibration`** in the same or the next PR — it needs none of the
   above and proves the structural surface immediately.

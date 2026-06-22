# Kickoff — injection-molding packing/cooling + warpage (issue #113)

**Status:** STARTED 2026-06-22 on branch `feat/molding-pack-cool`. This doc is the
handoff so any session can continue without the prior context. Read it top-to-bottom;
everything needed to resume is here.

GitHub: **issue #113**. Builds directly on **#105 / PR #112** (the *fill* stage,
merged to `main`).

---

## 1. Where we are (the foundation, already on `main`)

`molding_fill_submit` generates and runs **openInjMoldSim** (GPL-3.0, OpenFOAM-7
.org — modified compressibleInterFoam, Cross-WLF + 2-domain Tait) for the **fill**
stage and answers the strongest moldability gate: short-shot / fill ability,
`fidelity="solve"`. Validated: PS 20 mm × 1 mm plaque → "Filled to 0.98 and
terminating". See `docs/MOLDING_FILL_SOLVER.md`.

Code map (all paths relative to repo root):

| Thing | Location |
|---|---|
| Case generator (fill) | `driftpin/analysis/molding_fill.py` → `openinjmoldsim_case_files` / `write_openinjmoldsim_case` |
| Field parser + gate | `molding_fill.py` → `parse_fill` (reads `alpha.poly`), `fill_gate` |
| Worker handler | `driftpin/worker.py` → `_molding_openinjmoldsim_build_and_run` (gen+run), `_molding_openinjmoldsim_submit` (prepared case), dispatch in `_h_molding_fill_submit` |
| Foam runner | `worker.py` → `_run_foam(case_dir, argv_list, env_bashrc, unset_sigfpe=False)` |
| Solver resolvers | `driftpin/solvers.py` → `openinjmoldsim_bin()`, `openinjmoldsim_bashrc()` |
| MCP tool | `driftpin/mcp_server.py` → `molding_fill_submit(...)` |
| Toy runner + GIF | `tools/openinjmoldsim_toy.py` |
| Tests | `tests/test_molding_fill.py` |
| Built solver | `~/OpenFOAM/george-7/platforms/linux64GccDPInt32Opt/bin/openInjMoldSim` (auto-resolved); bashrc `~/OpenFOAM/OpenFOAM-7/etc/bashrc` |
| Reference tutorial | `~/opt/openInjMoldSim/tutorials/demo/fill_pack/` (the proven fill+pack case) |

**The 5 fill gotchas that ALSO apply to pack** (do not relearn these):
1. Inline every `#calc`/`#codeStream` (this build's SHA1 dynamic-code path is broken).
2. Emit no `functions{}` functionObject block (`probes`/`libsampling.so` aborts the run).
3. Small `maxDeltaT` (fill used 3 µs) — a large cap lets `deltaT` grow during quiescent
   phases then the first fast step diverges *within* the step. **Pack is stiffer still.**
4. Low `maxCo` (0.05) + under-relaxed non-final PIMPLE iters (`p_rgh` 0.3, `U` 0.5).
5. **`unset FOAM_SIGFPE`** before running (bashrc *exports* it → FPE trap on → aborts on
   transient `exp` infinities). `_run_foam(..., unset_sigfpe=True)` already does this.

Physics caveat carried over: keep `mold_temp_k` **above** the Cross-WLF singularity
`D2 − A2` (~321 K for corpus PS).

---

## 2. The two parts (scope from #113)

### Part A — Packing / cooling (no new solver; openInjMoldSim continuation)

After the cavity fills, **seal the gate/outlet and hold while the part cools**. This is
a *continuation* run of the same solver from the filled state. It yields the physics a
molder actually cares about beyond "did it fill":
- **volumetric shrinkage** from the Tait PVT as the melt cools and densifies (a real
  solve — the higher-fidelity twin of the #104 CTE estimate),
- **residual/holding pressure** decay,
- **sink-mark risk** (local under-packed/low-density regions),
- **freeze / cooling time** (cycle-time driver).

### Part B — Warpage / residual stress (FEM post-step; depends on A)

No OSS injection-molding warpage solver exists. Mirror the preCICE OpenFOAM↔CalculiX
pattern: take the frozen-in temperature / differential-shrinkage field from Part A and
hand it to **CalculiX or Elmer** as a thermo-mechanical (eigenstrain/thermal-load) solve
for the part's distortion. **Larger lift; may become its own issue once A lands.**

---

## 3. The pack mechanics (exact, from the tutorial `AllRun`)

The tutorial runs fill then THREE pack phases. The relevant shell functions:

```bash
new_deltaT(){  # reset the restart step so the continuation eases in
  foamDictionary <latestTime>/uniform/time -entry deltaT -set 1e-10
}
close_outlet(){           # seal the gate + let the part cool through the former outlet
  copy_bcP <latestTime>/p_rgh   # boundaryField.outlet.type -> fixedFluxPressure
  copy_bcT <latestTime>/T       # boundaryField.outlet.h    -> (the walls' h value)
  copy_bcU <latestTime>/U       # boundaryField.outlet.{value -> uniform (0 0 0), type -> fixedValue}
}
time_extend <endTime> <writeInterval> <maxDeltaT>  # rewrite system/controlDict
```

Sequence (serial translation — driftpin runs serial, NO decomposePar/reconstructPar):
1. **fill:** `controlDict` endTime≈0.6, run `openInjMoldSim -fillEnd 0.98`.
2. **pack1:** `new_deltaT` (reset latest step) → `close_outlet` → run `openInjMoldSim`
   (NO `-fillEnd`); the solver restarts from `<latestTime>` (needs `startFrom latestTime`).
3. **pack2:** `time_extend 0.5 0.1 1e-4` → run again.
4. **pack3:** `time_extend 6 0.5 1e-3` → run again.

Key dictionary change for continuation: the fill `controlDict` we generate uses
`startFrom startTime; startTime 0`. **Pack needs `startFrom latestTime`** so each phase
resumes from the previous end. Simplest: generate the fill `controlDict` with
`startFrom latestTime` from the start (latestTime=0 initially, so fill still starts at 0,
exactly like the tutorial's `controlDict0`).

The outlet BC edits can be done with `foamDictionary` (env is sourced in `_run_foam`) OR
by regex-rewriting the field files in Python. `foamDictionary` is what the tutorial uses
and is robust — prefer it (one `foamDictionary ... -set ...` per entry, chained in the
bash script `_run_foam` builds).

---

## 4. Implementation plan (Part A)

Incremental, each step independently testable. **[x] = done (branch `feat/molding-pack-cool`).**

- [x] **Generator: `startFrom latestTime`** — done; fill regression-verified.
- [x] **Pack controlDict rewrite + close_outlet helpers** — `close_outlet_cmds`,
      `time_extend_cmds`, `reset_restart_deltaT_cmd`, `set_walls_h_cmd`, `pack_phase_plan`
      (all pure, unit-tested).
- [x] **Worker: multi-phase run** — `_molding_openinjmoldsim_build_and_run` gained
      `stages="fill"|"fill_pack"`; fill_pack runs reset → set-walls-cooling → close_outlet
      → per-phase time_extend + re-run. Validated STABLE (nan=0).
- [x] **Parser: `parse_pack`** — melt-masked `rho`/`T`; volumetric shrinkage (1 − ρ_fill/
      ρ_final), min density (sink), frozen fraction, cooling time, residual pressure.
      Field names confirmed (`rho`, `T`, `alpha.poly`, `p`).
- [x] **Gate: `pack_gate`** — house verdict shape. ⚠️ band reference still open (see
      "CONFIRMED pack findings" — raw PVT densification ≠ net mold shrinkage).
- [x] **Generator `elastic` param** — default OFF (viscLimEl > etaMax) to dodge the
      elSigDev cooling divergence; True reserved for Part B.
- [x] **Shrinkage gate band recalibrated** — pass/fail on sink risk; shrinkage checked for
      Tait-EOS faithfulness (`tait_density`/`tait_densification_pct`). Validated on real
      run (solved 3.70% vs EOS 4.03%, faithful, pass). See findings above.
- [x] **Toy validation + artifact** — `tools/openinjmoldsim_toy.py --pack` runs fill+pack
      and renders an air-masked cooling-T GIF. `artifacts/openinjmoldsim_pack_cool.gif`
      (+ filmstrip) shows the textbook frozen-skin/molten-core profile (walls cool first).
- [x] **Structural tests** — controlDict latestTime, close_outlet cmds, time_extend/plan/
      walls_h, Tait density, pack_gate sink+faithfulness, elastic toggle (all pass).
- [x] **Slow solver-backed test** — `test_openinjmoldsim_fill_pack_cools_and_densifies`
      (skip-guarded; asserts stable/no-nan, densifies, cools, gate passes, pvt_faithful).
- [x] **Docs** — packing/cooling section in `docs/MOLDING_FILL_SOLVER.md` (mechanics, the
      two pack gotchas, gate design).
- [x] **MCP tool** — `stages`/`pack_phases`/`cool_window_s`/`eject_temp_c`/
      `pack_wall_h_w_m2k` surfaced on `molding_fill_submit`; contracts 10/10.
- [x] **Sink false-positive fix** — melt mask 0.9→0.99 (packed core; 0.9 caught air-diluted
      interface cells → false sink). rho_min 978.8, no sink, shrinkage matches EOS (4.43 vs
      4.44%).

**Part A is COMPLETE.** Remaining for the issue = Part B (warpage; its own lift — needs
`elastic=True` stabilised) and net mold shrinkage (packing-feed modelling).

### CONFIRMED pack findings (2026-06-22 runs — read before iterating)

- **Elastic-stress (elSigDev) divergence — FIXED.** As the part cools past `viscLimEl`,
  openInjMoldSim's elastic shear-stress model activates and on this coarse,
  constant-cp/kappa case it goes violently unstable (max(U) → 2.8e8, 5578 nan lines).
  **Fix: disable it for Part A** — the generator now sets `viscLimEl` ABOVE `etaMax`
  (param `elastic=False`, default) so elasticity never triggers. Shrinkage/cooling
  don't need it. `elastic=True` (viscLimEl = etaMax·0.5, tutorial behaviour) is reserved
  for the Part-B residual-stress/warpage path — it will need stabilisation work
  (under-relax elSigDev, finer mesh, tabulated thermo) before it's usable.
- **Fill-adiabatic → pack-cool design WORKS.** Fill with `wall_h≈1` completes to 0.98
  (hot/fast); the pack transition switches walls to cooling (`set_walls_h_cmd`, default
  1250) + `close_outlet`. Validated stable: nan=0, fill 967.9 → pack 1005.1 kg/m³,
  residual pressure ~2 MPa, full 1 s cool window.
- **Shrinkage gate band — DECIDED & IMPLEMENTED (option a+b hybrid).** The solved
  `volumetric_shrinkage_pct = 1 − ρ_fill/ρ_final` is RAW PVT densification on cooling, NOT
  the net "mold shrinkage" the corpus card quotes (post-packing-feed compensation — out of
  scope without modelling the feed). So `pack_gate` no longer pass/fails on a net-shrinkage
  band. Instead: **pass/fail = sink risk** (`rho_min < sink_rel·rho_mean_final`, default
  0.92 — measured against the part's OWN mean, no external reference); **shrinkage is
  checked for FAITHFULNESS** against the resin's own 2-domain Tait EOS
  (`tait_density`/`tait_densification_pct`). Validated on the real run: solved 3.70% vs
  Tait-EOS-expected **4.03%** (493.8→420 K @ 2 MPa) → ratio 0.92, `pvt_faithful=true`,
  gate **pass=true** (score 0.925, no sink). The number is trustworthy AND honest. (Net
  mold shrinkage — the cavity-sizing number — still needs packing-feed modelling; deferred.)
- **1 mm wall cools slowly (conduction-limited, ~10 s).** In the 1 s toy `frozen_fraction`
  is 0 and `cooling_time_s` is null (never reached eject temp). For a frozen toy use a
  thinner wall (0.4–0.5 mm) and/or a longer window (tutorial runs to 6 s). The parser
  already flags partial cooling as a lower bound.

### Likely-new pack gotchas to watch (predictions — confirm/expand as found)
- **Stiffness at solidification.** As cells cross `TnoFlow`, viscosity jumps to `etaMax`
  and `maxSolidCo` (0.005) forces tiny steps. Expect very slow wall-clock for full cooling;
  the tutorial runs pack to t=6 s. For the *toy*, cool only until the gate freezes or a
  short fixed window — don't chase full 6 s cooling.
- **`new_deltaT` reset is essential** — without easing the restart step the continuation
  diverges immediately (same class as the fill `maxDeltaT` lesson).
- **Density field name** — confirm whether it's `rho`, `thermo:rho.poly`, or only derivable
  via the Tait EOS from `T`,`p`. Inspect a real pack-run time dir before coding the parser.
- **Mold temp above the Cross-WLF singularity** still applies (cooling drives T toward it).

---

## 5. How to run / validate (commands)

```bash
# fill-only (the working baseline — regression check)
python3 tools/openinjmoldsim_toy.py            # → build/oims_toy/{case, fill.gif}

# manual pack experiment (until the worker path lands): generate a fill case, run it,
# then hand-apply close_outlet + extend and re-run, inspecting the latest time dir.
#   source ~/OpenFOAM/OpenFOAM-7/etc/bashrc; unset FOAM_SIGFPE
#   blockMesh && setFields && openInjMoldSim -fillEnd 0.98
#   foamListTimes -latestTime          # find <T_fill>
#   foamDictionary <T_fill>/uniform/time -entry deltaT -set 1e-10
#   foamDictionary <T_fill>/p_rgh -entry boundaryField.outlet.type -set fixedFluxPressure
#   foamDictionary <T_fill>/U   -entry boundaryField.outlet.type  -set fixedValue
#   foamDictionary <T_fill>/U   -entry boundaryField.outlet.value -set "uniform (0 0 0)"
#   H=$(foamDictionary <T_fill>/T -entry boundaryField.walls.h -value)
#   foamDictionary <T_fill>/T   -entry boundaryField.outlet.h -set "$H"
#   foamDictionary system/controlDict -entry endTime -set 1.0
#   foamDictionary system/controlDict -entry maxDeltaT -set 1e-4
#   openInjMoldSim                      # pack/cool continuation
```

Test subset (fast, no solver):
```bash
python3 -c "import sys;sys.path.insert(0,'.');sys.path.insert(0,'tests');import tests.test_molding_fill as t;[getattr(t,n)() for n in dir(t) if n.startswith('test_') and 'fills' not in n and 'reaches' not in n and 'stalls' not in n]"
```

---

## 6. Decisions / open questions for the owner

- **Pack gate pass criteria** — what shrinkage band is "pass"? Suggest cross-checking against
  the resin's #106 `mold_shrinkage_pct` corpus card (PS "0.4 0.7", HDPE "1.5 4.0") — pack
  PASS if the solved shrinkage lands in that band.
- **How far to cool the toy** — full freeze (slow, ~realistic cycle) vs a fixed short window
  (fast CI). Recommend: cool until max(T) < `eject_temp`, capped at a few hundred ms for the
  toy so the test stays < ~5 min.
- **Part B now or later** — recommend landing Part A first, then split Part B into its own
  issue (it's a CalculiX/Elmer coupling, a different skill set).

---

## 7. Conventions (house rules, don't violate)

- GPL solver stays at the **subprocess boundary** — never `import` it.
- Verdict shape: `{pass, score, fidelity, band_pct, warnings, ...}`; `fidelity="solve"`.
- Degrade gracefully when the OF7 build is absent (return `{ok:false, reason, install}`).
- Contract test (`tests/test_contracts.py`) must stay green — MCP tool params ↔ worker handler.
- Commit/PR only when the owner asks. Branch off `main`. Co-author trailer on commits.

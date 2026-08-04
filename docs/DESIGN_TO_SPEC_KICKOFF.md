# Kickoff — finishing the design-to-spec epic (#222)

> **Status 2026-08-03: epic #222 is closed — all six children merged, and the target
> workflow below runs end to end.** This doc is now a record rather than a handoff.
> What did NOT get done is tracked: [#260](https://github.com/gchen19/DriftPin/issues/260)
> adaptive **shape** optimization over a recipe (blocked on a main-thread work queue —
> see the boundary note under #228), and
> [#262](https://github.com/gchen19/DriftPin/issues/262) no verified oracle for RANS
> external flow. [#261](https://github.com/gchen19/DriftPin/issues/261) — nothing
> consulting a performance contract — is **built** (see below). The remaining loose
> threads at the bottom are still untracked.

Epic [#222](https://github.com/gchen19/DriftPin/issues/222) set a goal one step past "an
agent can construct parts": **an agent designs to a quantitative performance spec and
proves it hit the spec with a solver run.** Four of its six children are built. This doc
hands off the remaining two, plus the operational knowledge that is expensive to
rediscover.

Read alongside:
- [`SIMULATION_TOOLS.md`](SIMULATION_TOOLS.md) — the CFD family, the trust layer, the
  verification layer, and the performance contract, with their result schemas.
- [`MACOS.md`](MACOS.md) — how OpenFOAM runs here at all (it lives in a Multipass VM).
- [`DESIGN_HIERARCHY.md`](DESIGN_HIERARCHY.md) — where performance contracts sit next to
  the geometric contract layer.

---

## Where the epic stands

| # | What | State |
|---|------|-------|
| #223 | CFD external-flow geometry bridge — the virtual wind tunnel | **merged** (PR #253) |
| #224 | Force/moment extraction (Cd/Cl/Cm) | **merged** with #223 |
| #236 | `cfd_external_flow_submit` silently ignored `model` | **merged** with #223 |
| #225 | CFD trust layer + Richardson/GCI | **merged** (PR #255) |
| #226 | Performance contracts | **merged** (PR #256) |
| #227 | `study_submit` — DOE / parameter sweep | **merged** (PR #258) |
| #228 | `optimize_submit` — optimize-to-spec | **merged** (PR #259) |

The target workflow from the epic now runs end to end:

```
spec ──► declare_performance                                    ✅ #226
             │
             ▼
     screening tier (cfd_body_drag / cfd_pipe_flow / …)         ✅ #226 tier='auto'
             │
             ▼
     study_submit (DOE over recipe params × solver)             ✅ #227
             │
             ▼
     optimize_submit (vary params until the contract is met)    ✅ #228
             │
             ▼
     verify_performance (full-fidelity + trust gates)           ✅ #225 + #226
```

**Merge order matters**: #256 is branched from #255. Merge #255 first, and do **not**
pass `--delete-branch` — deleting a base branch auto-closes the stacked PR, and GitHub
then refuses to reopen it (this already happened once; #254 had to be rebased and
reopened as #255).

---

## Running any of this live

The CFD gates cannot run without OpenFOAM, and on this Mac OpenFOAM lives inside a
Multipass VM. Three exports and nothing else:

```bash
export TMPDIR=$HOME/fsi-run                          # the multipass mount, same path both sides
export DRIFTPIN_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc
export DRIFTPIN_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/linuxARM64GccDPInt32Opt/bin/simpleFoam
export RUN_HEAVY_SOLVES=1                            # or every live gate SKIPs
```

`TMPDIR` is load-bearing: `bash_argv` becomes `multipass exec … -- bash -c "cd <case> && …"`,
so the case dir must exist at the *same absolute path* inside the VM. Verify the whole
substrate with `bash scripts/ci-macos-preflight.sh` before blaming a solve.

The macOS heavy lane runs the CFD files:

```bash
bash tests/run_macos_heavy.sh            # includes test_openfoam / test_meshbridge / test_wind_tunnel
python3 tests/test_performance.py        # + the solver-tier contract gate
```

Reference timings on this box (M-series, in-VM OpenFOAM): sphere wind tunnel ~8 s,
pipe mesh-independence study (3 levels) ~14 s, full `test_openfoam.py` ~4 min (the two
flat-plate cases dominate).

---

## #227 — `study_submit`: DOE over recipe params × solver — **built (PR #258, merged)**

Shipped as `driftpin/analysis/study.py` (pure sampling + table arithmetic) plus a
`study_submit` worker handler that fans out and collects the way `verify_performance`
does. Gates in `tests/test_study.py` (20, fast lane). What the notes below predicted
correctly, and the two things they missed, are recorded after the original text.

**The gap.** Nothing connects `recipe(params) → *_submit → objective`. An agent
hand-rolls every sweep, and nothing records the search.

**What already exists that makes this cheap.**
- `jobs.py` has true thread concurrency and blake2b content-hash caching, so an
  identical re-submit is free — a resumed or overlapping study costs nothing for points
  it already has. **This is the single most important fact for #227**: the DOE engine
  does not need its own cache, it needs to not defeat the one it has.
- `recipes.py` / `family_materialize` regenerate geometry deterministically from params.
- `cfd_mesh_independence_submit` (#225) is a working precedent for the exact shape:
  build N cases on the **main thread** (FreeCAD tessellation must stay there — see the
  `jobs.py` threading contract), then run them from one job.

**Design notes from building #225/#226:**

1. **Fan out, then collect.** `_h_verify_performance` (#226) submits each solver
   measurement from the main thread and hands the job ids to a single collector job that
   waits on all of them. That gives real concurrency *and* keeps FreeCAD off the worker
   threads. Copy that shape rather than running points sequentially inside one job
   (which is what `cfd_mesh_independence_submit` does, and is why its ladder is serial).
2. **`_MAX_JOBS = 32` in `jobs.py` will bite.** A 5×5 grid plus the collector is 26
   jobs; anything larger evicts completed jobs and the collector's `jobs.result(jid)`
   will raise `JobNotFound` mid-study. Raise the cap, or have the collector copy each
   result out as soon as it lands. **Do this before the first big sweep, not after.**
3. **The objective should be a `verify_performance` verdict, not a raw number.** That is
   what makes a study comparable across fidelity tiers and gives every point a band and
   a trust block for free.
4. Record the search. `{points: [{params, value, state, job_id, cache_hit}], best,
   n_cached}` — `n_cached` is what tells a user their re-run was free.

**What building it actually turned up.** Notes 1–4 all held. Two things they missed,
both worth carrying into #228:

- **The retention fix needed a pin, not just a bigger cap.** Raising `_MAX_JOBS`
  alone is "big enough until it isn't" — eviction takes *terminal* jobs first, which
  are exactly the points that finished early, so a long study still loses its cheapest
  results. `jobs.pin()` / `jobs.unpin()` now make the dependency explicit (pins nest);
  the cap also went 32 → 256 so unpinned interactive work is never the cause.
- **The content cache cannot dedupe a live fan-out.** `jobs.submit` serves a cache hit
  only from a *completed* job, so two responses reading different metrics off the same
  tool call (a solved Δp and its `hp_ratio`) both miss and both solve — measured live
  as 6 OpenFOAM solves for 3 cases. `study_submit` therefore dedupes identical
  `(tool, conditions)` calls itself, before dispatch. **Any future fan-out driver has
  this same hole**; do not assume the content key covers it.

Live result on the in-VM OpenFOAM pipe (3 diameters at fixed flow rate, 7.1 s wall):
solved Δp rides D⁻⁴ at exponent −3.9986, r² = 0.99999999, `hp_ratio` 1.0884/1.0887/
1.0890. A re-submit reported 6/6 cached in 0.02 s.

---

## #228 — `optimize_submit`: vary params until the contract is met — **built (PR #259, merged)**

Shipped as `driftpin/analysis/optimize.py` (pure bounded Nelder-Mead, no scipy) plus an
`optimize_submit` handler. Gates in `tests/test_optimize.py` (15, fast lane). The
predictions below all held; what they did not cover is recorded after the original text.

**Its shape is much clearer post-#226 than the original issue sketch.** The objective is
a **`verify_performance` verdict**, not a solver number, and that changes the loop:

- "vary params until the contract is met" is literally "until every requirement's
  `state` is `pass`".
- An `indeterminate` mid-search is **not** a failed step — it means the current
  measurement's band straddles the limit. The correct response is to **escalate
  fidelity** for that point (screen → solver), not to keep stepping the parameters. An
  optimizer that treats indeterminate as a fail will walk away from good designs; one
  that treats it as a pass will converge on unproven ones.
- The trust block gives a natural stopping condition beyond "the objective stopped
  moving": if `gci_pct` for the winning point is wider than the margin the contract
  clears by, the answer is not converged *enough*, regardless of the optimizer's own
  tolerance.

**Precedent:** `optics_lens_optimize` is still the only shipped "vary variables until
targets met" tool — read it before designing the driver.

**What #227 hands it.** The evaluate function already exists: one `study_submit` call
with a single-point `variables` list *is* one objective evaluation, and it already
handles recipe materialization on the main thread, the solver fan-out, the collector
join, and the cache. An optimizer should drive that rather than re-implementing the
dispatch. Two shapes it inherits for free:

- A response may name **`verify_performance`**, so "the objective is a contract verdict"
  needs no new plumbing — the cell carries `state` alongside `value`, which is exactly
  the pass / fail / **indeterminate** signal the escalation rule below keys off.
- The point table is deliberately self-describing (parameters attached to each row, not
  positional), so the accumulated history is already in the shape a surrogate would
  consume when solve counts start to hurt.

**Budget the search.** Each solver-tier point is a real solve. The epic's own horizon
notes surrogate-assisted search (RBF/GP over study results) for when solve counts hurt;
#227's recorded points are exactly the training set for that, so keep the study record
in a shape a surrogate could consume.

**What building it actually turned up.**

- **A budget is three separate mistakes waiting to happen**, and the shipped code makes
  all three explicit because each was live at some point during the build: cache hits
  must not count (a simplex revisits coordinates constantly — charging for memo hits
  moved the answer from 0.0022 mm to 0.026 mm off a known optimum), `max_evals` must be
  a TOTAL ceiling rather than a per-leg allowance (an `auto` run was costing 1.6x what
  was asked for), and the 60/40 screen/solver split must apply only when a solver leg
  actually follows (a screen-only run was silently buying 60 % of its budget).
- **Clamp trial points into the box; do not reject them.** The optimum of a real design
  problem sits against the envelope, not in the interior, and a search that rejects
  out-of-box trials stalls just short of the bound it should be riding.

**The boundary this could not cross.** The search runs on a background thread — its
budget is minutes, far past the client's 120 s per-call timeout — and the `jobs.py`
threading contract forbids FreeCAD there. So optimizer responses must be
parameter-driven, and one naming live geometry is refused at the door. **Shape
optimization over a recipe therefore does not exist yet**: it needs a main-thread work
queue the worker services between requests, which is an architectural change rather than
a feature. `study_submit` is the workaround (it builds every point on the main thread up
front, so it CAN sweep recipe geometry — just on a fixed grid, not adaptively). This is
the most valuable remaining piece of the epic's ambition and is not tracked as an issue
yet.

Live result, `tier='auto'`, budget 24, 78 s wall on in-VM OpenFOAM: the screen leg spent
14 correlation evaluations to reach D = 9.100 mm, the solver leg polished with 10 real
solves to D = 9.1875 mm against a closed form of 9.2132 mm, `proven: true`, total spend
exactly 24.

---

## Hard-won facts (do not rediscover these)

**snappyHexMesh finds a surface by background-cell *edge* intersection.** A cell as
coarse as the body's largest dimension marks zero cells for refinement, meshes an
**empty tunnel**, and reports ~1e-14 N of drag with `returncode 0`. Verified live at
cell = L (fails) and L/2 (works). `external_domain_box` defaults to L/2 and refuses
anything coarser — if you add a new snappy case, add the same guard.

**snappyHexMesh v2512 rejects a `locationInMesh` on a cell vertex** ("Point (…) is not
inside the mesh or on a face or edge") — which the bbox centre of any symmetric solid on
a symmetric background box *is*. Both bridges nudge the seed off the grid by a fraction
of a cell.

**The wedge pipe's `Uz` residual is meaningless.** The wedge patches pin an essentially
2-D solution into a 3-D solver, so the out-of-plane momentum residual is normalized by a
near-zero field: it floors at ~1.6e-5 at *any* mesh density while `Ux` reaches 5.8e-16.
Gating convergence on U therefore never trips and the case runs to `endTime`. The pipe
cases control on `p` alone. Any future wedge/axisymmetric case must do the same.

**`multipass exec` forwards stdin into the VM.** A solve launched from a worker job
thread inherits — and consumes — the client's JSON-RPC pipe: the solve finishes fine
while every `job_status` poll times out, which looks nothing like the cause. Every
substrate launch pins `stdin=subprocess.DEVNULL`. **Rule: any `subprocess` started from
a worker job thread must do this**, not just the relay ones.

**A post-process pass reuses the solve binary** (`simpleFoam -postProcess -func yPlus`),
so a log named after `argv[0]` alone lets the audit clobber the solve's own log — and
the residual parser then reports a converged run as unconverged. `_run_foam` puts the
function name in the log name.

**The `forces` function object works.** The `'sha1'` IOstream abort that the epic (and
several docstrings) treated as an OpenFOAM property was specific to the build the
flat-plate work was written against. On v2512 it writes cleanly and matches the
Hagen–Poiseuille wall traction to ~1 %. It needs `writeInterval 1`: a converged run
stops at an arbitrary iteration, so a coarser interval often writes no file at all.

**Registration checklist.** A new tool family touches seven places; the contract tests
catch each omission far from the code you wrote. `driftpin/analysis/<name>.py` (pure
core) → `@handler` in `worker.py` → `@mcp.tool()` in `mcp_server.py` (docstring **must**
document the return value) → `tests/determinism_registry.py` → **both** `tests/run_all.sh`
*and* `run_all.ps1` → README capability row → `tests/test_worker.py` producer spec for
any `add_*` tool.

---

## Loose threads not tracked as issues

- ~~**#226's integration section is unbuilt.**~~ Tracked as
  [#261](https://github.com/gchen19/DriftPin/issues/261) and **built 2026-08-03**:
  `merge_assembly`, `substitutability_check` and `component_contract_check` all consult
  the contract now, through the pure `driftpin/gates/performance.py`. The design
  decision worth remembering is the **async policy** — a gate reads the verdict
  `verify_performance` last *recorded* on the part and never measures, because
  verification may return a job and a gate must answer synchronously. Its corollary is
  that "no verdict" is its own outcome (`unverified` / `stale` / `indeterminate` →
  `skipped`), never a pass. See MULTI_AGENT.md §11.12.
- ~~**The RANS wind-tunnel path ships `gated: false`.**~~ Now tracked as
  [#262](https://github.com/gchen19/DriftPin/issues/262). Worth knowing regardless: a
  requirement may demand `trust: {gated: true}`, which is therefore **unsatisfiable for
  any turbulent external-flow spec** until that oracle exists.
- **#237 items 2 and 3 are still open** (state-aware capabilities/hints; an SU2 case
  builder). Item 1 — wiring the built-in cases through Multipass — is effectively done.
- **Mesh independence for non-CFD families.** `grid_convergence` is family-agnostic and
  works today on three FEM stresses or three modal frequencies, but only the CFD
  *driver* exists. An FEM ladder would need its own refinement machinery.

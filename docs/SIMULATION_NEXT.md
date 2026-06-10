# Simulation — what's next after the completed sprint plan

[`SIMULATION_SPRINTS.md`](SIMULATION_SPRINTS.md) and the
[P3 kickoff](SIMULATION_P3_KICKOFF.md) are **fully executed**: eleven families,
oracle-gated, from closed-form tolerance math to geometry-driven Elmer/OpenFOAM
solves. This doc assesses what's worth building next. It is an *assessment*, not
a committed plan — promote items into a kickoff when one is picked up.

The existing problems are deliberately contained and testable. The two gaps that
framing leaves, and the two tiers this doc proposes to fill them:

1. **Screening estimators** ("ballpark" tier) — instant, pure-Python numbers that
   *focus the design into a solution space* before any solve runs. Today an agent
   sizing an enclosure must either guess a convection coefficient or stand up a
   CHT case; a ±20 % correlation answer in 10 ms is what actually steers early
   design. These are cheap (each is the Sprint-1 vertical: module → handler →
   tool → two-sided toys).
2. **Higher-order solves** — upgrades where the contained toy isn't enough:
   turbulence beyond Re≈2300, plasticity beyond first yield, acoustics, forced
   response, flow-coupled CHT. Almost all of these run on **backends already
   provisioned on the runner** (verified: Elmer ships `HelmholtzSolve.so`,
   `FlowSolve.so`, `StressSolve.so`, `MagnetoDynamics*.so`, even `EMWaveSolver.so`;
   CCX 2.21 does plasticity and steady-state dynamics; OpenFOAM 1912 has the RANS
   models) — so the cost is case-builders + oracles, not infrastructure.

---

## Two contract additions (small, do these first)

These address the "ballpark" need directly and cost almost nothing:

- **Fidelity labeling.** Screening tools return
  `fidelity: "exact" | "correlation"` and, for correlations, a `band_pct` (the
  literature scatter, e.g. Churchill–Chu ±20 %). The agent then knows whether a
  number is a *gate* or a *focusing estimate* — today every return looks equally
  authoritative. Backfill onto the few existing heuristic returns
  (`cost_estimate`'s process model, `dfa_check`).
- **The escalation pair.** Every screening tool's docstring names its
  higher-order twin (`h_estimate` → `cht_channel_submit`; `acoustic_screen` →
  the Helmholtz FEM; `plate_check` → `fem_*`), with the rule of thumb: escalate
  when the margin is within ~2× the correlation band. This turns the two tiers
  into one workflow an agent can follow mechanically.

---

## Tier A — screening estimators (pure-Python, no solver, S-effort each)

Ordered by leverage. Each is a 1–2 day Sprint-1-style vertical with handbook
two-sided toys.

| Candidate | Answers | Closed form / correlation | Fidelity |
|---|---|---|---|
| **Convection coefficients** (`h_estimate`) | "what h do I feed thermal_lumped / the bridge?" | natural: Churchill–Chu (cylinder, vertical plate); forced: flat-plate Nu, Hilpert crossflow; combined with the existing h_rad screen | correlation ±20 % |
| **Acoustics screening** (`acoustic_screen`) | room/cavity modes, resonator tuning, wall attenuation | rectangular-cavity modes f=(c/2)·√(Σ(n/L)²) (exact); Helmholtz resonator f=(c/2π)·√(A/V·L_eff) (±10 %); mass-law TL=20·log₁₀(f·m″)−47 dB (±3 dB); duct cutoff (exact) | mixed, labeled |
| **Plate/beam handbook** (`plate_check`) | "do I need FEM at all?" — bending σ/δ of standard plates | Roark/Timoshenko rectangular + circular plate cases (exact within thin-plate theory) | exact (theory limits stated) |
| **Column buckling** (`beam_buckling`) | the missing closed-form twin of `fem_buckling` (like `beam_modal` ↔ `fem_modal`) | Euler P_cr=π²EI/(KL)², Johnson parabola below the transition slenderness | exact |
| **Injection-molding screening** | cooling time + fill reach, feeding dfm/cost | t_cool=(s²/π²α)·ln(8(T_melt−T_wall)/π²(T_eject−T_wall)) (exact 1-term); flow-length/thickness ratio vs material charts | exact + correlation |
| **Drop/impact screening** | peak G and crush from drop height | energy method G=h_drop/d_crush (exact); cushion-curve lookup | exact + correlation |

Tier A alone is roughly two weeks of work and immediately changes how an agent
explores a design: it can sweep ten enclosure variants through correlations in a
second, then spend solver minutes only on the surviving two.

## Tier B — higher-order solves (provisioned backends, M-effort each)

Each follows the proven milestone shape: pure oracle → case builder → `*_submit`
→ degradation → live gate. Ordered by (value ÷ risk).

| Candidate | Backend (status) | Oracle / gate | Effort, risk |
|---|---|---|---|
| **Acoustic FEM** | Elmer `HelmholtzSolve` (**installed**) | rigid rectangular cavity modes — exact eigenfrequencies; duct plane-wave standing field | M, low — the M2/M6 deck pattern verbatim |
| **Harmonic forced response** | CCX 2.21 `*STEADY STATE DYNAMICS` or Elmer `StressSolve` (**installed**) | SDOF FRF: peak = Q·x_static, half-power bandwidth f_n/Q (exact); cross-links `beam_modal` + `random_vibration` | M, low |
| **Turbulent RANS** (internal + external) | OpenFOAM kOmegaSST (**installed**) | turbulent flat plate Cf=0.0592·Re^−1/5, Colebrook/Moody pipe Δp (correlations, ±10–15 % band) | M, medium — y+/wall-function discipline; gates must be banded, not exact |
| **Flow-coupled CHT** | Elmer `FlowSolve`+Heat (**installed**) or `chtMultiRegionFoam` | Graetz–Nusselt developed Nu=3.66 / 7.54 (exact eigenvalue results) — upgrades M6's plug flow to a *true Nusselt validation*, closing the loop with `h_estimate` | M/L, medium |
| **Coupled induction heating** | Elmer `MagnetoDynamics` + HeatSolver (**installed**) | total Joule power = R_s·|H|²/2 over the face (from the shipped `em_skin_depth`) + adiabatic ΔT energy balance | M, medium — completes `em_induction_submit` into a thermal answer |
| **Nonlinear structural** (plasticity, large deflection) | CCX 2.21 (**installed**) | plastic-hinge collapse load M_p=σ_y·Z (exact); large-deflection cantilever vs the elliptic-integral solution | M/L, medium |
| Free-surface flow (`interFoam`), RF/wave EM (`EMWaveSolver` — waveguide cutoff f_c=c/2a is exact), explicit impact dynamics | installed / partial | exact anchors exist for the first two | L — horizon; pick up only on a concrete need |

## Recommended sequence (when picked up)

1. The two **contract additions** (days — they retrofit value onto everything).
2. **Tier A** as one sprint wave (`h_estimate` first — it unblocks honest inputs
   to four existing thermal tools).
3. **B1 acoustics + B2 harmonic response** (zero new installs, low risk, and the
   pair finally covers the Horizon table's "Acoustics" row).
4. **B3 RANS** (extends the CFD validity envelope past Re≈2300 — the most common
   real-world escape from the current laminar gates).
5. **B4 flow-CHT and B5 induction heating** as the coupled-physics capstones.

The verification discipline is unchanged: exact anchors where physics is exact,
**banded** anchors where the literature itself is a correlation (a turbulent gate
pretending to be exact would be a lie — the band IS the honest oracle), relative
gates where only a sibling solve exists, and a two-sided negative for every toy.

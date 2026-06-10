# Kickoff — remaining advanced-toy batches (2, 4, 5)

Build-ready companion to [`ADVANCED_TOYS_PLAN.md`](ADVANCED_TOYS_PLAN.md). All new
toys append to `tests/test_toys_advanced.py` (pure-Python/NumPy, no FreeCAD/solver,
already in `tests/run_all.sh` under the venv lane). Run with
`.venv/bin/python tests/test_toys_advanced.py`.

## Done so far
- **Shipped on main:** optics, thermal-radiation, durability (Goodman), topology (2-D/3-D).
- **In flight:** Batch 1 (#47 — tolerance, materials, cfd) and Batch 3 (#48 — cht, em),
  stacked; 29 toys total. Merge #47 then #48, then branch the batches below off `main`.

## The contract for a new advanced toy
1. It must be **one of the five kinds** (state which in a one-line comment): exact
   identity · reciprocity/symmetry · conservation/composition · asymptotic/scaling
   law · regression guard. (See the plan doc's table.)
2. It must be **sharper than the basic** `tests/test_<module>.py` anchor — never a
   restatement. If the basic test already pins it, use the *sharper sibling*
   (an invariant/independence/scaling law, not the dimensional point value).
3. Group by family with a `# --- family: theme ---` banner; add the module import
   (`from driftpin.analysis import X`) alphabetically in the import block.

## Discipline (cost real time if skipped)
- **Verify every identity numerically in a throwaway `.venv/bin/python -` first**,
  then write the assert around the confirmed number. Don't assert a formula you
  haven't run.
- **Returned dict values are display-rounded** (often 3–8 decimals). Two traps hit in
  Batch 1/3:
  - A product/ratio of two rounded fields amplifies error at extreme inputs — e.g.
    `f·Re≡64` is masked by Re rounded to 3 dp at creeping Re≈1 (test at moderate Re;
    tolerance ~0.02). Same for `Stokes Cd·Re≡24` (tolerance ~0.05).
  - Monte-Carlo / sampled fields: `stackup` MC samples around each link's **center**,
    not its nominal — use symmetric links if you want MC mean ≈ nominal.
- Choose inputs that keep the relevant regime (laminar Re, slender beam, Re≪1, etc.);
  assert the regime/validity flag too.
- Keep grids/sample counts tiny — the `.venv` BLAS is unoptimized (see
  [memory: p2-simulation-env]); SIMP cross-checks use `max_iter=1`, `penal=1.0`.

---

## Batch 2 — scaling laws (machine_elements · vibration · durability extras)

The theme is "double an input → a *known* factor", plus pure invariants. APIs
(`driftpin/analysis/machine_elements.py`, `vibration.py`, `durability.py`):

**machine_elements** (each returns a dict; read the file for exact keys):
- `bearing_life(dynamic_load_c_n, equivalent_load_p_n, speed_rpm, kind)` — **ISO 281
  (C/P)ᵖ**: doubling C (fixed P) multiplies `l10_million_rev` by 2³=8 for `ball`,
  2^(10/3)≈10.08 for `roller`. *Exact power law.*
- `spring_check(wire_dia_mm, coil_mean_dia_mm, active_coils, force_n, material)` —
  rate ∝ d⁴/(D³·Nₐ): 2×Nₐ → ½ rate; 2×d → 16× rate. The Wahl factor depends on the
  index C=D/d **only** (same C → same Kw regardless of absolute size). *Scaling +
  invariant.*
- `gear_rating(module_mm, teeth, …)` — Lewis form factor Y(Z) is **monotone↑ in
  teeth** (Z=10<20<50 → Y rises → bending stress falls at fixed load). *Monotonicity.*
- `belt_drive(power_w, …, mu, wrap_angle…)` — **Eytelwein** T₁/T₂ = e^(μθ): doubling μ
  *squares* the tension ratio. *Exact scaling.*
- `press_fit_stress(shaft_dia_mm, hub_outer_dia_mm, interference_mm, …)` — **Lamé**
  contact pressure & hoop stress are **linear in interference** (2×δ → 2×p). *Linear.*
- `bolted_joint_check(...)` — separation load is independent of the external load;
  margin scales inversely. (Confirm keys before asserting.)

**vibration** (`beam_natural_frequencies`, `random_vibration`, `psd_at`):
- SRSS **energy independence**: two well-separated modes → `rms_g` ≡ √(g₁²+g₂²) (no
  cross term). Drive with a flat PSD; compare to the per-mode contributions.
- **Out-of-band → exactly 0**: a mode whose frequency sits outside the PSD band
  contributes 0 (the ruggedization rule). *Regression-flavoured.*
- Beam **f ∝ 1/L²** and **f ∝ h** scalings (I=b·h³/12, A=b·h → f∝h); doubling L → ¼ f.
- Simply-supported **βL ≡ nπ** exact (the `beta_l` field), and f_n ∝ n².
  *Exclude the basic Miles 7.0 g anchor and the cantilever βL=1.875 point value.*

**durability** (beyond the shipped Goodman compressive-mean toy):
- Goodman **fully-reversed collapse**: σ_m=0 → equiv_reversed ≡ σ_a (the correction
  vanishes). *Exact limit.*
- **Archard linear**: `wear_estimate` volume ∝ load·distance, inverse in hardness
  (2×load → 2×volume). *Linear.*
- **S-N slope**: 2× stress amplitude → a fixed life ratio set by the log-log Basquin
  slope (compute the expected ratio from σ_e, the 10³-cycle fraction). *Scaling.*
- **Static-overload guard**: σ_m ≥ σ_uts fails immediately regardless of cycles
  (governing_mode flips). *Regression guard.*

---

## Batch 4 — design-for-X & throughput (dfx · cost · slicing · kinematics)

Mostly monotonicity / limit envelopes (lower physics-sharpness, still real gates).
APIs in `dfx.py`, `cost.py`, `slicing.py`, `kinematics.py`:

**dfx**: `dfm_check` score ≡ 1 − violations/max(#faces,1) **exact** (3 faces, 1
violation → 0.667); `dfa_check` `assembly_score` **monotone non-increasing** as
part/fastener count rises; `pack_check` billable ≡ **max(actual_mass, dim_weight)**
with sort-to-fit reorientation (a part fits if each *sorted* dim ≤ the carton's).

**cost**: `cost_estimate` unit cost **monotone↓ in quantity** → asymptotes to the
material+process floor as tooling/setup amortize to 0 (1/qty); scrap fraction is
linear (scrap=0.1 → material ×1.1); process-factor *ratio* between processes is fixed.

**slicing**: `slice_estimate` solid mass ≡ ρ·V; filament **linear in infill** at
fixed wall fraction; `layer_count` ≡ ceil(bbox_z/layer_height) (sharp at 9.99 vs
10.01 mm); print time ∝ deposited_volume/flow (2×speed → ½ time).

**kinematics** (`slider_crank`, `grashof_classify`, `gruebler_dof`):
- slider stroke **independent of conrod L** (L>R+|e|): L=50 vs 200 → identical stroke.
  *(Exclude the basic stroke=2R anchor; this independence is the sharper sibling.)*
- Grashof **change-point** boundary: S+L = P+Q (within 1e-6) → `change_point`.
- Grübler **sequence**: 4-bar → DOF 1, 5-bar → 2, an over-constrained triad → 0.

---

## Batch 5 (optional round-out) — deepen the four covered families

- **topology**: SIMP penalty E(x)=Emin+xᵖ(E0−Emin) is sublinear (E(0)=Emin, E(1)=E0,
  E(0.5)<midpoint); compliance **monotone↓** in keep_fraction (more material→stiffer);
  volume constraint exact to the OC tol. Keep grids tiny + `max_iter` small.
- **optics**: `trace_bundle` energy balance closes to ~1e-12 (efficiency+leakage+
  absorbed); TIR **sharp edge** (T>0 just below θc, ≡0 just above).
- **thermal**: `thermal_transient_1d` → lumped in the Bi→0 limit (`lumped_agrees`);
  `thermal_lumped` at t=τ reaches exactly 1−e⁻¹ ≈ 63.2 % of the steady rise.

---

## Sequencing & wrap
One PR per batch, each ~8–12 toys, stacked or off `main` once #47/#48 land. After the
last batch, update the plan doc's coverage table (all ✅) and consider folding the
"advanced toys" notion into `docs/SIMULATION_EXAMPLES.md` as the standard second tier
per family. Registry/contract tests are unaffected (these add no MCP tools).

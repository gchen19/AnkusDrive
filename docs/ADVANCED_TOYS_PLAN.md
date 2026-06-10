# Advanced toys — expansion plan

`tests/test_toys_advanced.py` holds the **advanced** acceptance toys: fast-lane,
pure-Python/NumPy gates that probe the *exact closed-form limits* of each
`driftpin/analysis/` module — sharper than the happy-path anchors in the per-module
`tests/test_<module>.py` and in [`SIMULATION_EXAMPLES.md`](SIMULATION_EXAMPLES.md).

An advanced toy must be one of five kinds (and its inline comment should say which):

1. **Exact identity** — a one-point gate with no band (Brewster θ_B → r_p ≡ 0).
2. **Reciprocity / symmetry** — Helmholtz R(θ₁,n₁→n₂)=R(θ₂,n₂→n₁); fit hole↔shaft sign flip.
3. **Conservation / composition** — radiation shield halves flux; energy balance Σ=1.
4. **Asymptotic / scaling law** — h_rad·ΔT ≡ two-plate flux at any ΔT; ISO 281 (C/P)ᵖ.
5. **Regression guard** — a defect a probe once caught (neg-incidence TIR, F-reciprocity).

It must NOT merely restate a happy-path anchor already pinned in the basic test.

## Coverage

| Family | Advanced toys | Status |
|---|---|---|
| optics | Brewster, Helmholtz reciprocity, grazing→1, neg-incidence TIR, Sellmeier | ✅ shipped |
| thermal (radiation) | view-factor reciprocity, concentric spheres, shield network, h_rad factorization | ✅ shipped |
| durability | Goodman compressive-mean asymmetry | ✅ shipped |
| topology | 2-D plane-stress vs thin-3-D slab | ✅ shipped |
| tolerance | √N RSS-vs-worstcase, MC↔RSS, fit reciprocity, subtractive sign | ☐ Batch 1 |
| materials | numeric round-trip, specific-strength identity, ranking monotone | ☐ Batch 1 |
| cfd | Poiseuille f·Re≡64, Blasius Cf·√Re≡1.328, Stokes Cd·Re≡24, regime edges | ☐ Batch 1 |
| machine_elements | ISO 281 (C/P)ᵖ, Wahl C-only, Lewis Y(Z)↑, belt Eytelwein, Lamé linear | ☐ Batch 2 |
| vibration | SRSS energy independence, out-of-band→0, f∝1/L²/∝h, SS βL≡nπ | ☐ Batch 2 |
| durability (more) | Goodman fully-reversed collapse, Archard linear, S-N slope, overload guard | ☐ Batch 2 |
| cht | series R-sum, interface walk, h-free outlet T, solid ΔT decoupled | ✅ shipped |
| em | δ∝1/√f, R=L/σA + Joule, wire B∝1/r, solenoid uniform | ✅ shipped |
| dfx | DfM score exact, DfA monotone, pack billable=max() | ☐ Batch 4 |
| cost | unit cost monotone↓, amortization 1/qty, scrap linear | ☐ Batch 4 |
| slicing | mass≡ρV, infill linear, layer≡ceil, time∝deposited/flow | ☐ Batch 4 |
| kinematics | stroke independent of L, Grashof change-point, Grübler sequence | ☐ Batch 4 |
| topology (more) | SIMP E(x) penalty, compliance monotone↓ in keep_fraction | ☐ Batch 5 |
| optics (more) | energy balance to 1e-12, TIR sharp edge | ☐ Batch 5 |
| thermal (more) | Heisler→lumped (Bi→0), lumped t=τ → 63.2% | ☐ Batch 5 |

## Batches (each ≈ one PR appended to `tests/test_toys_advanced.py`)

Ordered by design-criticality × identity-sharpness × low risk. Each toy carries a
one-line comment naming its kind (1–5 above) and the formula; all stay pure-Python/
NumPy on the fast lane (already wired into `tests/run_all.sh` under the venv).

- **Batch 1 — pure-math cores (tolerance · materials · cfd).** ~11 toys. Exact,
  zero-risk, high reuse downstream.
- **Batch 2 — scaling laws (machine_elements · vibration · durability extras).** Power
  laws and invariants; double-an-input → known factor.
- **Batch 3 — M6 newcomers (cht · em).** ✅ shipped — pinned the new oracles' exact
  identities while the code is fresh.
- **Batch 4 — design-for-X & throughput (dfx · cost · slicing · kinematics).**
  Monotonicity / limit envelopes.
- **Batch 5 (optional round-out)** — deepen the four already-covered families.

Excluded as duplicative of the basic tests: CFD D⁴ scaling, Miles 7.0 g, slider
stroke = 2R, beam mode ratios — their *sharper* siblings (invariants/independence)
are used instead.

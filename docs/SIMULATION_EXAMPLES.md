# DriftPin simulation tools — execution examples & verification toys

Companion to [`SIMULATION_TOOLS.md`](SIMULATION_TOOLS.md). That doc is the *scope
map* — what each family answers, its backend, and its build risk. This doc is the
*proof harness*: for every family, a concrete call sequence, the JSON it should
hand back, and a **toy problem with a known answer** you can check the
implementation against before trusting it on real geometry.

**How to read each section**

- **Example** — the intent-encoded call sequence an agent would actually issue,
  in `compose → run → extract` order.
- **Expected result** — the shape of the returned JSON, with representative
  numbers.
- **Toy** — a problem whose answer is known from a closed-form solution, a
  handbook value, or a hand calculation, plus the **pass criterion** and at least
  one **negative** (a deliberately-wrong input the tool must flag). A test that
  only ever passes proves nothing — every toy is two-sided, the same discipline
  [`tests/TOYS.md`](../tests/TOYS.md) uses for the multi-agent gates.

**The advanced tier.** The happy-path **Toy** in each section is the first tier.
Every analysis family also carries a sharper *second tier* — the **advanced toys**
in [`tests/test_toys_advanced.py`](../tests/test_toys_advanced.py) (64 fast-lane
gates) — that probes the *exact closed-form limit* rather than one dimensional
point value: invariants (`f·Re≡64`, Wahl-is-index-only), scaling laws (ISO 281
`(C/P)ᵖ`, beam `f∝1/L²`), reciprocity (Helmholtz, fit hole↔shaft), conservation
(SRSS energy, trace-bundle energy balance to 1e-12), and regression guards
(out-of-band→0, static-overload). See [`ADVANCED_TOYS_PLAN.md`](ADVANCED_TOYS_PLAN.md)
for the per-family coverage map.

**Status legend**

- ✅ **Implemented today** — FEM family (`fem_*`), callable now.
- 🟡 **P0 proposed** — pure-Python, zero new deps, specced in `SIMULATION_TOOLS.md`.
- 🟠 **P1 proposed** — one external CLI / in-house pipeline.
- 🔴 **P2 proposed** — heavy external solver, gated on async-solve.

The toys are written so they can be promoted straight into the M2 gate harness
([`tests/test_multiagent_m2.py`](../tests/test_multiagent_m2.py)): each one is a
deterministic oracle returning `{ok, reason}`.

---

## Conventions every example assumes

- **Units are SI quantity strings**, never bare floats: `"210000 MPa"`,
  `"7900 kg/m^3"`, `"0.30"`. This matches what `fem_set_material` already
  consumes, so the Materials DB and FEM share one vocabulary.
- **Geometry is passed by handle** (`{"handle": "body_3"}`), or by a resolved
  face/edge tag (`{"handle": "body_3", "tag": "f_top"}`) that survives unrelated
  edits — the same ref model `fem_add_constraint` uses today.
- **Results return numbers, not solver dumps.** Every extractor hands back a small
  typed dict the agent can branch on.
- **External solvers degrade gracefully.** A missing OpenFOAM/PrusaSlicer yields
  `{"ok": false, "reason": "solver not installed", "install": "pip install driftpin[cfd]"}`,
  never an import crash.

---

## 0. Anchor: the FEM family, end-to-end ✅

The one family you can run today. Use it as the reference for what "fleshed-out
example + verifiable toy" means; every proposed family mirrors its shape.

### Example — cantilever beam, full pipeline

```text
new_document(name="beam")
add_primitive(kind="box", length=8000, width=1000, height=1000)   -> body_1
fem_new_analysis(name="bend")                                     -> analysis_1
fem_set_solver(analysis="analysis_1", solver="ccx")
fem_set_material(analysis="analysis_1", body="body_1",
                 material={"YoungsModulus":"210000 MPa",
                           "PoissonRatio":"0.30",
                           "Density":"7900 kg/m^3",
                           "Name":"Steel-Generic"})
fem_add_constraint(analysis="analysis_1", kind="fixed",
                   refs=[{"handle":"body_1","tag":"f_x0"}])
fem_add_constraint(analysis="analysis_1", kind="force", force=9_000_000,
                   refs=[{"handle":"body_1","tag":"f_xL"}],
                   direction={"axis":"-z"})
fem_mesh(analysis="analysis_1", size=500)
fem_run(analysis="analysis_1")
fem_results(analysis="analysis_1", top_n=5)
```

Or the same thing in one shot via the built-in:
`fem_cantilever_demo(length=8000, width=1000, height=1000, force=9_000_000, mesh_size=500)`.

### Expected result

Actual output from the bundled demo at default args (verified by running it):

```json
{
  "nodes": 164, "tets": 412,
  "max_displacement_mm": 0.0538,
  "max_vonmises_mpa": 0.224,
  "workdir": "/tmp/driftpin_fem"
}
```

### Toy — verify by *scaling laws*, not absolute beam theory

The closed-form reference is Euler–Bernoulli tip deflection
δ = P·L³ / (3·E·I), with I = b·h³/12 (= h⁴/12 for a square section). For
L=8000 mm, b=h=1000 mm, P=9×10⁶ N, E=210 000 MPa: I=8.33×10¹⁰ mm⁴, δ≈**88 mm**.

The bundled demo returns ~0.054 mm — *three orders off* slender-beam theory. That
is expected and is the whole lesson: the section is stubby (L/h=8, so shear and the
coarse 164-node mesh dominate) **and** CalculiX-through-the-worker magnitudes are
not certified-absolute (see `TOYS.md`). So **do not gate FEM on an absolute
closed-form number.** Verify instead by relations that survive the offset:

**Pass (scaling laws — these the solver must obey exactly):**
- Halve E → `max_displacement_mm` doubles (±5%). Linear elasticity.
- Double P → displacement and von Mises both double (±5%).
- Double L → deflection rises ~8× (L³), holding section fixed.

**Pass (relative gate):** compare the agent's part to a scripted reference solved
under the *same* mesh + solver setup; the consistent offset cancels — exactly how
`fem_bracket` / `fem_beam_stiffness` are gated in `TOYS.md`.
**Negatives:** swap the fixed face onto the loaded end → solver must return a
rigid-body / unconstrained error, not a small number; zero force → zero
displacement.

---

## 1. Tolerance & GD&T 🟡

### Example — a 3-link shaft-in-housing stack

```text
tolerance_stackup(
  chain=[
    {"name":"housing_bore_depth", "nominal":50.0, "plus":0.10, "minus":0.10},
    {"name":"shoulder_to_shoulder","nominal":30.0, "plus":0.05, "minus":0.05},
    {"name":"shim",                "nominal":2.0,  "plus":0.02, "minus":0.02}],
  method="rss", samples=10000)

fit_check(hole={"nominal":20.0,"plus":0.021,"minus":0.0},
          shaft={"nominal":20.0,"plus":-0.007,"minus":-0.020})   # ISO H7/g6

fit_class(basic_size=20, fit="H7/g6")
```

### Expected result

```json
{
  "nominal": 18.0,
  "worstcase": {"min": 17.83, "max": 18.17},
  "rss": {"sigma": 0.038, "min_3s": 17.89, "max_3s": 18.11},
  "montecarlo": {"mean": 18.0, "std": 0.038, "cpk": 1.49, "pct_in_spec": 99.86}
}
```

`fit_check` → `{"fit_class":"clearance","min_clearance":0.007,"max_clearance":0.041,"prob_interference":0.0}`.

### Toy — worst-case vs. RSS on a hand-computed chain

Take three identical links, each 10.00 ±0.10 mm.

- Nominal sum = 30.00; **worst-case** spread = ±0.30 → [29.70, 30.30].
- **RSS** spread = ±√(0.10²·3) = ±0.173 → [29.83, 30.17].

**Pass:** worst-case bounds exact to 1e-6; RSS sigma within 1e-3 of 0.173/3 if
plus/minus are read as 3σ, or of 0.173 if read as the half-band (state the
convention in the docstring and test the one you ship). Monte-Carlo `pct_in_spec`
must converge to RSS within ±0.3% at samples=10000.
**Negatives:** an interference pair (`shaft.max > hole.min`) must return
`fit_class:"interference"` with `prob_interference > 0`; ISO 286 `H7/g6` on a
20 mm shaft must reproduce the handbook deviations (hole +0.021/0, shaft
−0.007/−0.020) — a wrong table entry is caught here.

> This is the family that stands up the `driftpin/analysis/` subpackage
> (Appendix A of `SIMULATION_TOOLS.md`). It's pure math, so the toy runs without
> spawning FreeCAD.

---

## 2. Materials & selection 🟡

### Example — fetch one, then Ashby-rank candidates

```text
material_get(name="AL6061-T6")

material_select(
  criteria={"min_yield_mpa":200, "max_density_g_cc":3.0, "min_service_temp_c":-50},
  rank_by="specific_strength")

material_list(category="aluminum")
```

### Expected result

```json
{
  "name": "AL6061-T6", "category": "aluminum",
  "YoungsModulus": "68900 MPa", "PoissonRatio": "0.33",
  "Density": "2700 kg/m^3",
  "yield_mpa": "276 MPa", "uts_mpa": "310 MPa",
  "fatigue_endurance_mpa": "96 MPa",
  "fracture_toughness_mpa_sqrt_m": "29",
  "cte_per_k": "23.6e-6", "thermal_conductivity_w_mk": "167",
  "specific_strength_kn_m_kg": "102",
  "rough_cost_usd_kg": "4.5",
  "source": "FreeCAD FCMat: Aluminum-6061-T6 / MMPDS Rev N (allowables)"
}
```

`material_select` → ranked list:
`[{"name":"AL7075-T6","specific_strength":183,"passes":true}, {"name":"AL6061-T6",...}, ...]`.

### Toy — round-trip + monotonic ranking

**Pass (round-trip):** `material_get("AL6061-T6").YoungsModulus` parses to
68.9 GPa ±1%, density to 2700 kg/m³ ±1%, and feeds straight into
`fem_set_material` without reformatting — i.e. the Materials DB output *is* a valid
FEM material card (proves the shared-vocabulary integration).
**Pass (ranking):** under `rank_by="specific_strength"`, 7075-T6 (≈570 MPa /
2810 kg/m³) outranks 6061-T6 (≈276 / 2700) outranks pure aluminum — a known
ordering. Under `rank_by="cost"` the order inverts toward the commodity alloy.
**Negatives:** a filter `min_yield_mpa: 9999` returns an empty set (not a crash,
not the closest miss); requesting an unknown name returns
`{"ok":false,"reason":"not found","did_you_mean":[...]}`.

See [§ Materials data corpus](#materials-data-corpus--where-the-numbers-come-from)
for where these numbers should actually come from.

---

## 3. Wear, fatigue & fracture 🟡

### Example — turn an FEM stress into a durability verdict

```text
# stress_range comes from two fem_results runs (load on / load off),
# or from a single run's max_vonmises_mpa for fully-reversed loading.
fatigue_check(stress_range_mpa=180, mean_stress_mpa=40,
              cycles=1_000_000, material="AL6061-T6")        # S-N + Goodman

fracture_check(stress_mpa=150, crack_len_mm=2.0, material="AL6061-T6")  # K vs K_IC

wear_estimate(load_n=200, sliding_dist_m=5000,
              material_pair=["steel-1045","bronze-c93200"])  # Archard
```

### Expected result

```json
{
  "fatigue": {"safety_factor": 0.93, "life_cycles": 740000,
              "pass": false, "governing_mode": "goodman_mean_stress"},
  "fracture": {"k_applied_mpa_sqrt_m": 13.3, "k_ic": 29.0,
               "safety_factor": 2.18, "pass": true,
               "critical_crack_mm": 9.5},
  "wear": {"volume_loss_mm3": 45.0, "depth_loss_mm": 0.09, "pass": true}
}
```

### Toy — closed-form anchors

- **Fracture (Griffith/LEFM):** K = Y·σ·√(π·a) with Y≈1.12 for an edge crack.
  σ=150 MPa, a=0.002 m → K = 1.12·150·√(π·0.002) = **13.3 MPa·√m**. Against
  AL6061 K_IC≈29, SF≈2.2 and `pass:true`. Increase a to the value where K=K_IC and
  `fracture_check` must report `pass:false` and a `critical_crack_mm` matching the
  hand inversion a_c = (K_IC/(Y·σ))²/π ≈ 9.5 mm.
- **Fatigue (Goodman):** with σ_e=96 MPa (6061 endurance), σ_a=90, σ_m=40,
  σ_uts=310: Goodman SF = 1/(σ_a/σ_e + σ_m/σ_uts) = 1/(0.938+0.129) = **0.94** →
  `pass:false`. Drop the load 20% and it must cross to `pass:true`.
- **Wear (Archard):** V = k·F·s/H. With k=1e-4, F=200 N, s=5000 m, H≈2.2 GPa
  for bronze → V = 1e-4·200·5000/2.2e9 m³ ≈ **45 mm³** (calibrate k to the pair).

**Pass:** each within 5% of the hand value.
**Negatives:** a tensile-mean stress above UTS must force `pass:false` regardless
of cycle count; a crack longer than a_c must report negative margin, never a
positive SF.

---

## 4. Thermal beyond steady-state 🟡 (lumped) / 🔴 (transient/radiation)

### Example — lumped transient warm-up

```text
thermal_lumped(mass_g=120, c_p="900 J/kg/K", power_w=15,
               h_conv=12, area_mm2=20000, t_ambient_c=25, duration_s=300)
```

### Expected result

```json
{
  "t_final_c": 55.4, "t_steady_c": 87.5,
  "time_constant_s": 450.0,
  "reached_steady_pct": 48.7,
  "radiation_significant": false
}
```

### Toy — first-order RC against the exact exponential

A lumped mass has steady-state rise ΔT_ss = P/(h·A) and time constant
τ = m·c_p/(h·A); the exact response is T(t) = T_amb + ΔT_ss·(1−e^(−t/τ)).

m=0.12 kg, c_p=900, P=15 W, h=12, A=0.02 m² (20 000 mm²):
- ΔT_ss = 15/(12·0.02) = **62.5 K** above ambient → t_steady = **87.5 °C**,
- τ = (0.12·900)/(12·0.02) = **450 s**,
- at t=300 s: T = 25 + 62.5·(1−e^(−300/450)) = 25 + 62.5·0.487 = **55.4 °C**.

**Pass:** `t_steady_c` matches T_amb+ΔT_ss within 1%; `t_final_c` at t=τ equals
T_amb + 0.632·ΔT_ss within 1%; `time_constant_s` exact.
**Negatives:** set power=0 → temperature must stay at ambient; push duration ≫ 5τ →
`t_final` must converge to `t_steady` (no overshoot from a numerical scheme).
For the radiation flag, a hot small body (T>150 °C, low h) must flip
`radiation_significant:true` once σ·ε·T⁴ area-loss rivals convective loss.

---

## 5. Structural extensions 🔴 (topology / random-vibration / contact)

### Example — random vibration off an existing modal run

```text
fem_modal(analysis="analysis_1", n_modes=6)
fem_run(analysis="analysis_1")
random_vibration(analysis="analysis_1",
                 psd_profile=[{"hz":20,"g2_hz":0.01},{"hz":2000,"g2_hz":0.01}])
```

### Expected result

```json
{
  "rms_g": 7.4, "first_mode_hz": 312.0,
  "rms_stress_mpa": 58.0, "three_sigma_stress_mpa": 174.0,
  "fatigue_damage_per_hr": 0.002, "pass": true
}
```

### Toy — Miles' equation sanity

For a single-DOF system the GRMS response to a flat PSD W (g²/Hz) at natural
frequency f_n with amplification Q is Miles' equation:
GRMS = √( (π/2)·f_n·W·Q ).

f_n=312 Hz, W=0.01 g²/Hz, Q=10 → GRMS = √(1.571·312·0.01·10) = **7.0 g**.

**Pass:** `rms_g` within 10% of Miles for a near-SDOF part (one dominant mode).
**Negatives:** doubling Q must raise GRMS by √2; a part with the first mode below
the PSD band must report a much higher response (resonant amplification) than one
stiffened above it — the tool must be monotonic in f_n.
For `topology_optimize`, the toy is geometric: a fixed-free beam under tip load
must converge to a tapered/truss-like shape whose mass ≤ `keep_fraction`·original
and whose tip stiffness stays within a stated bound — and it **returns geometry**,
so the gate is `interference_check` + `mass_properties` on the result.

---

## 6. Fluids / CFD 🔴

### Example — internal flow pressure drop

```text
cfd_internal_flow(model="body_1",
                  inlet={"flow_rate_lpm":10},
                  outlet={"pressure_pa":0},
                  fluid="water-20c")
```

### Expected result

```json
{
  "pressure_drop_pa": 5800.0, "flow_rate_lpm": 10.0,
  "reynolds": 21000, "regime": "turbulent",
  "recirculation_zones": 1, "solver": "OpenFOAM simpleFoam",
  "residuals_converged": true
}
```

### Toy — straight pipe vs. Hagen–Poiseuille / Darcy

The unambiguous CFD verification is a straight circular pipe, where the answer is
analytic.

- **Laminar (Re<2300):** Δp = 128·μ·L·Q/(π·D⁴) (Hagen–Poiseuille). For water
  (μ=1.0e-3 Pa·s), D=10 mm, L=1 m, Q=1.31e-5 m³/s (Re≈1700):
  Δp = 128·1e-3·1·1.31e-5/(π·(0.01)⁴) ≈ **53 Pa**.
- **Turbulent:** Δp = f·(L/D)·(ρV²/2) with f from the Colebrook/Moody value at the
  run's Re and roughness.

**Pass:** simulated Δp within 10% of the analytic value for the straight pipe;
this calibrates mesh + turbulence model before any real manifold is trusted.
**Negatives:** halving D must raise laminar Δp ~16× (D⁴ law) — a tool that scales
wrong is caught immediately; a missing OpenFOAM must return the graceful
`solver not installed` dict, not a stack trace.

> CFD cannot block the MCP channel — this family is gated on the async/long-solve
> work and ships behind `driftpin[cfd]`.

---

## 7. Optics 🟠

### Example — ray-trace a diffuser, then check moldability

```text
optics_raytrace(model="body_1",
                source_config={"type":"lambertian","rays":100000,"wavelength_nm":550},
                n_refractive=1.49)                       # PMMA at 550 nm

optics_moldability_check(model="body_1", pull_axis="+z")
```

### Expected result

```json
{
  "raytrace": {"exit_distribution":"fwhm_deg:42",
               "leakage_fraction":0.03,
               "hotspot_locations":[[0,0]],
               "efficiency":0.91},
  "moldability": {"undercut_faces":[], "draft_violations":[{"face":"f_side","draft_deg":0.4}],
                  "wall_thickness_stats":{"min_mm":0.8,"max_mm":2.4},
                  "score":0.86}
}
```

### Toy — Snell's law + energy conservation

Two analytic anchors that need no full lens model:

- **Single flat interface:** a ray hitting a planar PMMA surface (n=1.49) at 30°
  must refract to θ₂ = asin(sin30°/1.49) = **19.6°**, and the Fresnel reflectance
  at normal incidence must be ((1.49−1)/(1.49+1))² = **3.9%**. The ray tracer must
  reproduce both.
- **Energy conservation:** `leakage_fraction + efficiency + absorbed ≈ 1.0` to
  within 1% — rays can't be created or destroyed.

**Pass:** Snell angle within 0.1°, Fresnel within 0.2%, energy closes to 1%.
**Negatives:** total internal reflection above the critical angle
θ_c = asin(1/1.49) = 42.2° must show zero transmission, not a refracted ray; a
flat-bottom part with a re-entrant feature must populate `undercut_faces` (a
moldability scorer that passes a known undercut is broken).

> Backed by the in-house `~/diffuser` pipeline (already imports FreeCAD, runs
> under the worker), so this is P1 rather than a from-scratch build. Optical
> material data — refractive index vs. wavelength — comes from the
> refractiveindex.info corpus (see below).

---

## 8. Multibody dynamics / kinematics 🔴

### Example — drive a four-bar through a full rotation

```text
mechanism_simulate(
  assembly="assembly_1",
  joints=[{"a":"ground","b":"crank","type":"revolute","axis":"z","at":"p_O2"},
          {"a":"crank","b":"coupler","type":"revolute","axis":"z","at":"p_A"},
          {"a":"coupler","b":"rocker","type":"revolute","axis":"z","at":"p_B"},
          {"a":"rocker","b":"ground","type":"revolute","axis":"z","at":"p_O4"}],
  drivers=[{"joint":0,"rate_dps":360}],
  duration_s=1.0)
```

### Expected result

```json
{
  "trajectories": {"coupler_point": [[x,y], ...]},
  "max_torques": {"joint_0_nm": 4.2},
  "collisions_through_motion": [],
  "reachable_envelope": {"bbox_mm": [120, 80, 5]},
  "mobility_dof": 1
}
```

### Toy — Grashof + a slider-crank with exact stroke

- **Grashof check (static, no solver):** for link lengths S(shortest), L(longest),
  P, Q a crank-rocker exists iff S+L ≤ P+Q **and** the crank is the shortest link.
  This is the same relation the `fourbar_crankrocker` toy in `TOYS.md` already
  gates — reuse it as the geometry pre-check.
- **Slider-crank stroke (kinematic, exact):** for crank radius R and conrod L>R,
  the piston stroke is exactly **2R**, independent of L, and the piston must clear
  the bore through the whole rotation. Drive it a full turn and measure peak-to-peak
  piston travel.

**Pass:** measured stroke = 2R within 0.5%; `mobility_dof` = 1 for the four-bar
(Gruebler: 3·(n−1) − 2·j = 3·3 − 2·4 = 1); `collisions_through_motion` empty for a
clean linkage.
**Negatives:** a link set violating Grashof must report the mechanism cannot fully
rotate (drag-link/triple-rocker), not silently grind through; introduce a part that
overlaps only at one crank angle and `collisions_through_motion` must list exactly
that angle — proving the family sees *time-varying* interference a static
`interference_check` misses.

---

## 9. Design for X (DfX) 🟡 (DfM/DfA/cost) / 🟠 (slicing)

### Example — grade one part four ways

```text
dfm_check(model="body_1", process="injection")
dfa_check(assembly="assembly_1")
slice_estimate(model="body_1", profile="standard", material="PLA")
cost_estimate(model="body_1", process="injection", material="ABS", quantity=10000)
pack_check(model_or_assembly="assembly_1", carton={"l_mm":300,"w_mm":200,"h_mm":150})
```

### Expected result

```json
{
  "dfm": {"draft_violations":[{"face":"f_rib","draft_deg":0.0}],
          "undercut_faces":[], "min_wall_violations":[],
          "min_radius_violations":[], "tool_access_issues":[], "score":0.78},
  "dfa": {"part_count":12, "fastener_count":8, "insertion_axes":3,
          "handling_difficulty":"medium", "symmetry_score":0.6},
  "slice": {"print_time_min":92, "support_volume_mm3":4100,
            "layer_count":250, "filament_g":34.5, "mass_g":34.5},
  "cost": {"material_cost":0.42,"process_cost":0.18,"unit_cost":0.60,
           "breakdown":{"tooling_amortized":0.05}},
  "pack": {"fits":true,"void_fraction":0.34,"dim_weight_kg":1.8,
           "carrier_tier":"standard","drop_crush_flag":false}
}
```

### Toy — analytic / handbook anchors per check

- **DfM (draft):** a box with vertical walls (0° draft) pulled along +z must list
  every side face as a draft violation; add 2° draft and the list empties. A part
  with a side hole perpendicular to the pull axis must populate `undercut_faces`.
- **DfA (Boothroyd-lite):** a 12-part assembly with 8 separate fasteners must score
  worse than the same function achieved with snap-fits and 4 parts — the metric must
  reward part-count reduction monotonically.
- **Cost:** `material_cost` must equal `mass_properties.volume × density ×
  price_per_kg` from the Materials DB to within rounding — a closed-form check that
  ties DfC to §2.
- **Slicing:** `filament_g` must equal `mass_g` for a solid print at 100% infill
  (density·volume); at 20% infill it must drop roughly proportionally. `print_time`
  must rise as profile goes draft→standard→fine (finer layers, more passes).
- **Packaging:** `pack_check` must agree with `envelope_check`/`mass_properties` —
  a part larger than the carton returns `fits:false`.

**Pass:** each anchor within 5% (cost/slice mass) or exact (boolean fit/draft flags).
**Negatives:** a known-unmoldable part (re-entrant undercut, no draft) must score
low and list the offending faces; a part exceeding the carton must return
`fits:false`, never silently truncate.

> DfM/DfA/cost are pure-Python and reuse existing tools (`draft`, `thickness`,
> `query_faces`, `interference_check`, `mass_properties`, `envelope_check`) — they
> ship in the P0 wave. Slicing shells out to a PrusaSlicer/OrcaSlicer CLI and is P1.

---

## Wiring toys into the existing harness

Every toy above is shaped to drop into the M2 gate harness as a
`gate(tmp, files) -> {ok, reason}` oracle, exactly like the 31 toys catalogued in
[`tests/TOYS.md`](../tests/TOYS.md). The discipline carries over verbatim:

1. **Prove the oracle first.** Run the correct build → must pass; run each negative
   → must be caught. A gate that never fails is worthless (`M2_SELFTEST=1`).
2. **Prefer relative gates for solver families** (FEM/CFD/MBD): compare the result
   to a scripted reference solved under the *same* setup, so any consistent solver
   offset cancels. Use absolute closed-form bands only where the physics is exact
   (tolerance math, Snell, Hagen–Poiseuille, slider-crank stroke).
3. **Pure-Python families need no FreeCAD** — their toys run in milliseconds and
   belong in `tests/test_*.py` wired into `tests/run_all.sh`, alongside the planned
   `test_tolerance.py`.

---

## Materials data corpus — where the numbers come from

The Materials DB (§2) and every family that reads it (fatigue, fracture, cost,
optics) are only as trustworthy as their source data. The short answer to "what's
more reliable than a web search": **there is no single free, machine-readable
corpus that covers both mechanical and optical properties — use a layered corpus,
seeded from data DriftPin already ships.**

### Mechanical / thermal — recommended layering

1. **Seed from FreeCAD's bundled material cards (start here).** FreeCAD already
   ships ~100+ `.FCMat` cards (the Material workbench / `Mod/Material` library) with
   Young's modulus, Poisson ratio, density, yield, CTE, thermal conductivity — and
   they're already in the exact `"210000 MPa"` quantity-string format
   `fem_set_material` consumes. Zero new dependency, zero format translation, and
   it's the natural backing store for `material_get`. This should be the v1 corpus.
2. **MatWeb** — ~170 000 entries (metals, polymers, ceramics, composites),
   manufacturer-supplied, the broadest free-tier reference. Good for *breadth* and
   filling gaps, but data is vendor-reported (not always traceable) and bulk
   scraping is against their terms — treat it as a manual lookup to curate cards
   from, not an automated feed. <https://matweb.com>
3. **MMPDS** (formerly MIL-HDBK-5) and **CMH-17** (composites) — the gold standard
   for *design allowables*: statistically derived A/B-basis values with full
   provenance. This is what you cite for certified fatigue/fracture work. Licensed
   (paid), so reference it in a card's `source` field rather than redistributing the
   numbers.
4. **ASM Handbooks / ASM Alloy Center** and **Granta MI** (now Ansys, the CES /
   Materials Data Library) — curated, experimentally measured, the engineer's
   standard references. Licensed; best for authoritative values you transcribe into
   cards. Granta is also the canonical source of Ashby-chart data if
   `material_select(rank_by=...)` grows real chart logic.
5. **NIST** — free and traceable but patchy for everyday alloys: TRC Alloy Data
   (critically-evaluated thermophysical data with uncertainties), the NIST Materials
   Data Repository, and the Standard Reference Data program. Best for
   thermophysical/cryogenic properties where it has coverage.
   <https://www.nist.gov/mgi/materials-data-resources>
6. **Open schema to model your store on:**
   [`mvernacc/material-properties-interchange`](https://github.com/mvernacc/material-properties-interchange)
   — a small open database of engineering materials with a Python interface and a
   clean YAML schema; a good template for `driftpin/analysis/materials/`. And
   [`sedaoturak/data-resources-for-materials-science`](https://github.com/sedaoturak/data-resources-for-materials-science)
   catalogs further open datasets.

**Recommendation:** ship v1 from the FreeCAD cards (consistent units, already
present), extend it with a curated JSON layer transcribed from MMPDS/ASM for the
handful of alloys you actually design in, and record each property's `source` +
basis (typical vs. A/B-basis) in the card. Don't auto-scrape MatWeb; do mirror the
schema of the open interchange project so cards stay portable.

### Optical — there is a clear winner

For refractive index, dispersion, and absorption (everything the optics family
needs), use the **refractiveindex.info database** directly:

- **[`polyanskiy/refractiveindex.info-database`](https://github.com/polyanskiy/refractiveindex.info-database)**
  — ~3 100 records on ~600 materials, stored as **YAML** (Sellmeier/Cauchy
  coefficients *and* tabulated n,k vs. wavelength). Crucially it is released
  **CC0 (public domain)** — you may vendor it into the repo and redistribute, even
  commercially, no permission needed. This is the corpus for `optics_raytrace`'s
  `n_refractive` and any wavelength-dependent work. Offline-usable; no API call
  required, just read the YAML.
  ([dataset paper, *Scientific Data* 2024](https://www.nature.com/articles/s41597-023-02898-2))
- **Manufacturer glass catalogs** for full optical-glass property bags (Abbe
  number, dn/dT, internal transmission, CTE, Knoop hardness, chemical resistance):
  [SCHOTT optical glass datasheets](https://refractiveindex.info/download/data/2017/schott_2017-01-20.pdf)
  and the [OHARA glass catalog](https://oharacorp.com/glass-catalog/) (both also
  mirrored on refractiveindex.info, and both publish Zemax/CSV catalogs). Use these
  when you need the *thermo-mechanical* properties of a specific glass, not just n.

**Recommendation:** vendor the CC0 refractiveindex.info YAML for n/k(λ), and pull
the manufacturer catalogs for the mechanical/thermal properties of any specific
optical glass — that combination is both authoritative and redistributable, which
no general mechanical database gives you for free.

**Implemented (submodule + extract).** This is now wired in:

- The database is vendored as a git submodule at
  `vendor/refractiveindex.info-database`, pinned to release tag **`v2026-05-24`**.
  (A submodule pins to the *commit* the tag points at — reproducible; re-pin by
  checking out a newer tag and committing the gitlink.)
- `tools/extract_optical_corpus.py` reads a curated `TARGETS` set from the
  submodule YAML (Sellmeier coefficients + catalog n_d / Abbe / density / CTE) and
  emits the lean, committed `driftpin/analysis/materials/optical.json`. The runtime
  loader reads only that JSON — **no PyYAML or submodule needed at runtime**, only
  to regenerate.
- `materials._load_corpus()` merges `optical.json` onto `seed.json`
  *field-wise*, so N-BK7 keeps its hand-authored Young's modulus and gains vendor
  n_d / Abbe / dispersion. `materials.refractive_index_at(card, wavelength_nm)`
  evaluates the Sellmeier formula for wavelength-dependent n (feeds the optics
  family's `n_refractive`).
- Current optical set: N-BK7, N-SF11, F2, Fused-Silica, PMMA, Polycarbonate,
  Polystyrene. Add more by extending `TARGETS` and re-running the extractor.

---

## Sources

- [refractiveindex.info database (GitHub, CC0)](https://github.com/polyanskiy/refractiveindex.info-database) · [dataset paper, Scientific Data 2024](https://www.nature.com/articles/s41597-023-02898-2)
- [SCHOTT optical glass datasheets](https://refractiveindex.info/download/data/2017/schott_2017-01-20.pdf) · [OHARA glass catalog](https://oharacorp.com/glass-catalog/)
- [MatWeb](https://matweb.com/) · [Ansys Granta Materials Data Library](https://www.ansys.com/products/materials/materials-data-library) · [NIST Materials Data Resources](https://www.nist.gov/mgi/materials-data-resources)
- [mvernacc/material-properties-interchange](https://github.com/mvernacc/material-properties-interchange) · [sedaoturak/data-resources-for-materials-science](https://github.com/sedaoturak/data-resources-for-materials-science)
- [CMU LibGuide: Find Properties of Materials](https://guides.library.cmu.edu/c.php?g=215385&p=1423024) (MMPDS, ASM, Smithells overview)

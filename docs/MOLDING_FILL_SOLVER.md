# Injection-molding fill solver (issue #105)

The higher-fidelity twin of the `molding_screen` correlation: a real two-phase
(melt + air) flow solve that answers *"can this geometry actually be molded?"* —
**short-shot / fill ability** (the strongest, most reliable gate), fill time, and a
peak injection-pressure proxy.

## Two backends, one tool — `molding_fill_submit`

| | headline solver | runnable today | physics | validation |
|---|---|---|---|---|
| **openInjMoldSim** | yes (issue target) | **YES — built, case-gen + worker wired, validated** (2026-06-22; see below) | compressibleInterFoam + Cross-WLF + Tait; fill (pack/cool scaffolded) | peer-reviewed (MDPI Fluids 5(2):84, 2020) |
| **interFoam VOF** (fallback) | — | **yes**, on the existing OpenFOAM (.com/ESI) | incompressible VOF, Newtonian or BirdCarreau melt; fill only | we own the case + gate |

`molding_fill_submit` now **generates and runs openInjMoldSim** whenever its binary
resolves (`solvers.openinjmoldsim_bin()`) — building the OF7-org case from the
cavity + process params (or running a prepared `case_dir` if one is supplied). It
falls back to building and running the **interFoam** 2-D plaque-cavity fill case on
the existing OpenFOAM only when the OF7 build is absent (or an explicit `application`
forces it). The GPL solver is held at the subprocess boundary — never imported into
Python — the same arm's-length posture as the Elmer/OpenFOAM/YADE/openEMS/preCICE
binaries.

### Why the fallback still exists

openInjMoldSim targets **OpenFOAM 7 (.org / openfoam.org)**; this host also builds
OpenFOAM **v19xx/v25xx (.com / ESI)**. The forks are not drop-in compatible, so the
headline path needs a **parallel OpenFOAM-7 build** (`tools/build_openinjmoldsim.sh`,
a multi-hour host-heavy compile — now done on this host). Where that build is absent,
the interFoam fallback needs **no new build**, runs on the existing solver in ~2 s
for a coarse 2-D case, and still answers the single most reliable molding question
(short-shot). It loses the packing/cooling stage and the published IM validation;
those ride on the OF7 path, which is now the default wherever the build is present.

## The interFoam fill case (`driftpin/analysis/molding_fill.py`)

A **2-D rectangular plaque cavity** (`length` × `wall_thickness`, one cell deep,
`empty` front/back). The gate is a short inlet patch at the bottom of the left edge;
**vents** sit at the far (right) corners so the displaced air has somewhere to exit
— without a vent the incompressible VOF cannot fill and the front stalls spuriously.
The cavity starts full of air (`alpha.melt = 0`); melt is injected at a fixed mean
velocity (from the volumetric rate / gate area), and `interFoam` tracks the
`alpha = 0.5` melt front. Melt viscosity is Newtonian by default, or **BirdCarreau**
(`carreau={nu0,nuInf,k,n}`) as the shear-thinning stand-in for openInjMoldSim's
Cross-WLF.

Fields are read straight from the final `alpha.melt` (no function-object writers —
`surfaceFieldValue` is sha1-broken in this build, same reason `openfoam.py` reads
`p` directly):

- `filled_fraction` — mean alpha over the cavity (1.0 = full, < 1 = short shot).
- `front_x_frac` — furthest axial column at least half full (how far the front got).
- `last_to_fill_x_frac` — least-filled column (where a short shot starves).
- `max_pressure_pa` — max of the converged pressure field (peak-injection proxy).

## The moldability gate (`fill_gate`)

Returns the house verdict shape:

```
{ pass, score, fidelity:"solve", band_pct:20.0,
  filled_fraction, short_shot, last_to_fill_x_frac, front_x_frac,
  max_pressure_pa, pressure_ok, fill_time_s, warnings }
```

`pass` is true when the cavity is essentially full (`filled_fraction ≥ 0.97`, no
short shot) **and** the peak pressure is within the machine limit (default 180 MPa).
`fidelity="solve"` (a real VOF fill, not a correlation); `band_pct=20` reflects the
case-setup-owned validation. This is the `escalate_to` target of `molding_screen`
(set whenever the screen's fill check runs).

## Validation (`tests/test_molding_fill.py`)

Skip-guarded like the other solver tests. The solver-backed half builds two real
cavities and runs blockMesh + interFoam:

- **fillable** (50 mm × 2 mm, 0.4 s): the front reaches the far end
  (`front_x_frac ≈ 1.0`, ~99 % filled) → `pass=True`.
- **short shot** (200 mm × 1.5 mm, viscous melt, 0.12 s): the front stalls at
  ~13 % of the flow length → `pass=False, short_shot=True`.

Both ran on the host's OpenFOAM v2512 (`interFoam` resolves via the sourced bashrc).

## openInjMoldSim — BUILT & VALIDATED (2026-06-22)

The parallel OpenFOAM-7 (.org) stack is built and the solver runs. Steps that worked
on this host (Ubuntu 24.04, gcc 13.3):

1. **OpenFOAM-7 (.org)** — `ThirdParty-7` (scotch) then `OpenFOAM-7` (`version-7`
   branch) under `~/OpenFOAM/`. **Compiles clean on gcc-13** with no patching — the
   `version-7` branch already absorbed the modern-toolchain fixes (~24 min at `-j8`).
2. **openInjMoldSim v7.2** — cloned to `~/opt/openInjMoldSim`, built via
   `applications/solvers/multiphase/openInjMoldSim/Allwmake` against the sourced OF7
   `etc/bashrc`. Binaries `openInjMoldSim`/`openInjMoldSimF` land in `$FOAM_USER_APPBIN`.
3. `solvers.openinjmoldsim_bin()` **auto-resolves** the binary via its
   `~/OpenFOAM/*/platforms/*/bin` glob — `DRIFTPIN_OPENINJMOLDSIM*` env vars are
   optional, not required.
4. **Validated** on the bundled `tutorials/demo/fill_pack` (9600 cells), serial,
   `-fillEnd 0.98` → *"Filled to 0.98005119 and terminating"* (~9.5 min). The full
   Cross-WLF + Tait fill physics runs; `foamToVTK -latestTime -ascii` → meshio reads
   the result (20434 pts) — the same extraction path the worker uses.

> `tools/build_openinjmoldsim.sh --build` automates steps 1–2 (capped `-j8`, ccache).

### Case-prep gotchas (REQUIRED for any openInjMoldSim case on this host)

The OSHA1stream SHA1 path is broken in this toolchain (same breakage noted for the
ESI build's functionObjects). Two consequences when preparing a case:

- **Inline every `#calc` / `#codeStream` directive** — OpenFOAM's on-the-fly code
  compilation SHA1-hashes the generated snippet and aborts. e.g. the tutorial's
  `constant/solidificationProperties` had `viscLimEl #calc "$etaMax*0.5"`; replace
  with the literal (`viscLimEl 5e6;`).
- **Emit no `functions{}` functionObject block** in `controlDict` (the tutorial ships
  a `probes`/`libsampling.so` sampler) — its SHA1 write aborts the run at *"Starting
  time loop"*.

The worker generates cases programmatically, so it simply emits neither.

## Programmatic OF7 case generation — DONE & VALIDATED (2026-06-22)

`molding_fill.py` now generates a complete, runnable **OF7-org openInjMoldSim** case
(`openinjmoldsim_case_files` / `write_openinjmoldsim_case`), and `molding_fill_submit`
**runs that headline GPL solver** whenever its binary resolves — no prepared
`case_dir` needed. The interFoam path is now only the fallback for when the OF7 build
is absent (or when an explicit `application` is passed).

The generated case is a **pressure-driven, non-isothermal** 2-D plaque fill (the way
a real press is controlled): melt enters hot at the gate against an injection-pressure
ramp, the y=0/y=H mold walls draw heat out, and the **Cross-WLF** viscosity climbs as
the melt cools — so a too-thin/long/cold cavity freezes off (a real short shot), not
just a kinematic one. The melt phase is `alpha.poly`; the **2-domain Tait** EOS gives
the compressible PVT behaviour. The Cross-WLF + Tait coefficients are pulled straight
from the **#106 materials corpus** (`materials.get(resin)['cross_wlf'|'tait_pvt']`) —
this is the path that actually consumes that corpus — falling back to tutorial-proven
PS coefficients for resins the corpus does not card.

### Toy validation (the green run)

`tools/openinjmoldsim_toy.py` generates → runs → parses → renders end-to-end:

```bash
python3 tools/openinjmoldsim_toy.py        # → build/oims_toy/{case, fill.gif}
```

A PS, 20 mm × 1 mm plaque (60×8 cells) **filled to 0.98005 and terminated**
(`-fillEnd 0.98`, ~3 min serial); the gate returns `pass=True, fidelity="solve"`,
`front_x_frac=1.0`, peak ≈ the 2 MPa inlet. The melt front advancing gate→far-end:

![openInjMoldSim fill](../artifacts/openinjmoldsim_fill.gif)

(filmstrip: `artifacts/openinjmoldsim_fill_filmstrip.png` — 3.3 % → 27 % → 61 % →
86 % → 98 %.) The same generate-and-run path is covered by
`tests/test_molding_fill.py::test_openinjmoldsim_generated_case_fills` (skips unless
the OF7 build is present).

### Three more case-prep gotchas the generator handles

Beyond the two SHA1 gotchas above, the **violent compressible fill** is numerically
touchy. The advancing melt front opens a low-pressure region that pins to `pMin` and
sets the PIMPLE outer correctors oscillating (U → 100s of m/s within a single step →
`nan`). Three knobs, baked into the generator's defaults, keep it converged:

1. **Small `maxDeltaT`** (3 µs). A large cap lets `deltaT` grow during the quiescent
   pressure ramp; then the first fast-flow step is far too big and diverges *within*
   the step before `adjustTimeStep` can react. This was *the* fix.
2. **Low `maxCo`** (0.05) and **under-relaxed non-final PIMPLE iterations**
   (`p_rgh` 0.3, `U` 0.5; the `*Final` iterations stay 1.0 for time-accuracy).
3. **`FOAM_SIGFPE` unset before the run.** This build's `etc/bashrc` *exports*
   `FOAM_SIGFPE` (and even an empty value counts as "set"), so the FPE trap is on by
   default and aborts on the transient `exp` infinities the fill startup throws.
   Unsetting it lets the `etaMax`/`pMin` clamps recover the step. The worker's
   `_run_foam(..., unset_sigfpe=True)` and `tools/openinjmoldsim_toy.py` both do this.

One physics caveat: keep `mold_temp_k` **above** the Cross-WLF singularity `D2 − A2`
(~321 K / 48 °C for corpus PS) or near-wall cells cool through it. The default wall is
near-adiabatic (`wall_h_w_m2k=1`) for a clean fill demo; raise it (with a safe mold
temp) to model freeze-off short shots.

**Out of scope** (separate follow-ups): end-to-end warpage / residual stress (hand
the T/p history to CalculiX/Elmer), first-class weld-line/air-trap labels, the
Cross-WLF + Tait material corpus (sibling of #99).

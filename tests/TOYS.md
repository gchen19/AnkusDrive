# Multi-agent design toys — catalog & how to run

The toy problems used to measure whether a *team* of agents can split a mechanical
design, build the pieces independently, and merge them into something that actually
works. Companion to [`MULTI_AGENT_EVAL.md`](MULTI_AGENT_EVAL.md) (the eval plan +
results) and the RFC [`../docs/MULTI_AGENT.md`](../docs/MULTI_AGENT.md).

All toys live in [`test_multiagent_m2.py`](test_multiagent_m2.py).

## What a toy is

Each toy isolates **one** design failure mode and ships four things:

- **`components`** — one buildable part per entry. In *partition* mode each part is
  built by its own cold agent (only its contract slice); in *single* mode one agent
  builds them all in sequence. That contrast is the experiment.
- **`single_task`** — the overview prompt for the single-agent baseline.
- **`gate(tmp, files)`** — a deterministic oracle that judges the built parts:
  `{ok, reason}`. Interference, envelope, a measured tolerance, a kinematic relation,
  or an FEM/mass result — never a vibe.
- **`negatives`** — deliberately-broken builds the gate **must** catch. A gate that
  never fails is worthless, so every toy is validated two-sided: the correct build
  **passes** and each negative is **caught**.

The headline discipline: **prove the oracle is trustworthy first, then trust the
agent numbers.** `M2_SELFTEST` does exactly that for free.

## How to run

Everything is driven by environment variables on `test_multiagent_m2.py`.

### Free (no API key) — these are what you run day to day

```bash
# validate every toy's gate against scripted correct + broken builds (no API)
M2_SELFTEST=1 .venv/bin/python3 tests/test_multiagent_m2.py

# just one or a few toys (comma-separated keys)
M2_SELFTEST=1 M2_TOYS=peg,pinslot,thermo_structural .venv/bin/python3 tests/test_multiagent_m2.py

# exercise the full agent loop with a stubbed model — proves wiring, no API
M2_DRYRUN=1 .venv/bin/python3 tests/test_multiagent_m2.py
```

`M2_SELFTEST` prints `PASS/FAIL` per toy (correct build + each negative) and exits
non-zero if any gate fails to discriminate. This is the gate that must be green
before any billed run. The deterministic-mechanism layer (M1) also runs free in the
default suite via `tests/run_all.sh` → `tests/test_multiagent_m1.py`.

### Billed (real agents) — needs an Anthropic key

```bash
anthropic-key            # load the key into the shell (keeps it out of Claude Code's env)
RUN_RELIABILITY=1 M2_MODEL=haiku M2_TRIALS=20 M2_TOYS=tchainu,nslot4 \
    .venv/bin/python3 tests/test_multiagent_m2.py
```

Each component is built by a fresh Anthropic API call (a cold model with only its
contract slice + a small DriftPin tool surface). The run reports merge-pass rate per
toy for **partition** vs **single**, plus cost. Results write to the gitignored
`tests/multiagent_cache/report_m2.json`. Launch long runs in the background and never
report numbers before the run completes.

### Environment knobs

| Var | Default | Effect |
|---|---|---|
| `M2_SELFTEST=1` | — | free gate-validation (correct + negatives), no API |
| `M2_DRYRUN=1` | — | free full-loop wiring check with a stubbed model |
| `RUN_RELIABILITY=1` | — | **required** to make real (billed) agent calls |
| `ANTHROPIC_API_KEY` | — | required when `RUN_RELIABILITY=1` |
| `M2_TOYS` | all | comma-separated toy keys to run (e.g. `peg,flange`) |
| `M2_MODEL` | `haiku` | `haiku` \| `sonnet` \| `opus` |
| `M2_TRIALS` | `1` | independent runs per toy/condition |
| `M2_NEGATIVES=1` | — | run only the negative controls (deliberately-wrong builds) |

### Notes

- **FEM toys are slower** (`fem_bracket`, `fem_beam_stiffness`, `thermo_structural`):
  the gate meshes and solves with CalculiX. Free, but a full `M2_SELFTEST` takes a few
  minutes because of them. Select a subset with `M2_TOYS` while iterating.
- **FEM gates are relative.** The agent's part is compared to a scripted reference
  built and solved under the *same* setup, plus an absolute mass budget the agent is
  told. CalculiX-through-the-worker magnitudes are monotonic/discriminating but not
  certified absolute stress, so any consistent offset cancels.
- **Agent tool surface.** Most toys give the agent only `new_document`,
  `add_primitive` (box/cylinder/sphere), `boolean_op`, `mass_properties`,
  `save_component`. Two capabilities were added for specific families: **`rotate`**
  (orientation: `gdt_angularity`, `kin_sarrus`) and **`add_gear`** (involute gears:
  the gearboxes, planetary, rack-pinion). Those enlarge the surface, so post-addition
  pass-rates aren't a clean baseline against the original static-toy numbers.

---

## Catalog (31 toys)

### Fit & contract — static geometric oracles

| Key | Isolates | Components | Gate |
|---|---|---|---|
| `peg` | one shared dimension | plate, peg | peg clears the bore (no interference) |
| `flange` | a shared bolt circle | plateA, plateB | bolts clear both plates |
| `bracket` | a keep-out envelope | housing, bracket | bracket bbox ⊂ envelope, no interference |
| `twopin` | two-point alignment (derived spacing) | base, link | both pins seat (no rotational slack) |
| `nslot4` / `nslot8` | context load: k distinct slot↔peg pairs | plate, peg0…pegk-1 | each peg fits its own slot (interference + per-peg Ø) |
| `tchain3` / `tchain6` | cumulative drift, equal segments | seg0…segn-1 | chain length within tolerance |
| `tchainu` | the partition-LOSES case (unequal, whole-mm grid) | seg0…seg5 | sum within tol; local rounding can't reconcile a global total |
| `pinslot` | exact constraint (round hole + oriented slot) | carrier, fixture | seats at nominal AND tolerates a spacing perturbation (a two-round-hole design jams) |

### GD&T — measure a feature against a tolerance zone

| Key | Isolates | Components | Gate |
|---|---|---|---|
| `gdt_position` | true position off datums | plateA, plateB | hole axis within a Ø0.4 zone of true position |
| `gdt_concentric` | coaxiality | bossA, bossB | bore axis within tol of the boss axis |
| `gdt_symmetry` | symmetry about a median plane | plateA, plateB | hole-pair midplane within tol of the datum |
| `gdt_angularity` | orientation (uses `rotate`) | postA, postB | post long-axis within ±1° of the called-out angle from Z |

### Gears & mechanisms — kinematic relations + motion

| Key | Isolates | Components | Gate |
|---|---|---|---|
| `kin_gearbox6` / `kin_gearbox3` | shared center distance across speeds | in0,out0,… | every pair meshes at one center distance + hits its ratio |
| `kin_planetary` | planetary meshing + assembly condition | sun, planet, ring, carrier | Nring=Nsun+2·Nplanet, planets equally spaced, (Nsun+Nring)%n=0 |
| `kin_ackermann` | steering-arm aiming | knuckle_L, knuckle_R | each kingpin→tie-rod line aims at the rear-axle midpoint |
| `kin_slidercrank` | slider-crank closure | crank, conrod, piston, guide | L>R (no bind), stroke=2R, piston clears the bore through its stroke |
| `kin_geneva` | intermittent indexing | driver, wheel | drive-pin radius = C·sin(π/n) (tangency) + n equally-spaced slots |
| `kin_sarrus` | straight-line 1-DOF (uses `rotate`) | leafA, leafB | the two hinge axes are perpendicular |
| `kin_wishbone` | SLA suspension geometry | upperarm, lowerarm, upright | upper shorter than lower (camber gain) + upright closes the four-bar |
| `rack_pinion` | rack/pinion mesh | pinion, rack | rack tooth pitch = pinion circular pitch (π·module) |
| `fourbar_crankrocker` | linkage mobility | ground, crank, coupler, rocker | Grashof condition + the crank is the shortest link |
| `cam_follower` | cam lift law | cam, follower | eccentric cam lift (2·offset) = follower travel |

### Physics, mass & fastening

| Key | Isolates | Components | Gate |
|---|---|---|---|
| `fem_bracket` | strength sizing | bracketA, bracketB | structural FEM: von Mises ≤ limit AND mass ≤ budget |
| `fem_beam_stiffness` | stiffness sizing | beamA, beamB | structural FEM: tip deflection ≤ limit AND mass ≤ budget |
| `cg_target` | mass balance (coupling) | w_a, w_b, w_c | assembly centre of mass on the pivot |
| `press_fit` | interference fit | shaft, hub | shaft−bore interference inside a holding band |
| `thread_engagement` | threaded fastening | bolt, plate | tap-drill = thread minor Ø (not clearance) + engagement ≥ 0.8·D |

### Multi-physics capstone

| Key | Isolates | Components | Gate |
|---|---|---|---|
| `thermo_structural` | coupled thermal + structural sizing | sinkA, sinkB | **thermal FEM** max temp ≤ limit AND **structural FEM** von Mises ≤ limit AND mass ≤ budget |

---

## Layers (M1 vs M2)

- **M1** ([`test_multiagent_m1.py`](test_multiagent_m1.py), in `run_all.sh`, no key) —
  the deterministic substrate: manifest → merge → gates with *scripted* builders.
  Proves the machinery and the gates discriminate before any agent is trusted.
- **M2** ([`test_multiagent_m2.py`](test_multiagent_m2.py), this catalog) — the same
  gates judging real LLM-built (or scripted, in selftest) components, partition vs
  single.

See [`MULTI_AGENT_EVAL.md`](MULTI_AGENT_EVAL.md) for the metrics, the partition-vs-
single comparisons, recorded findings, and the gate-helper internals. Promotion of
the FEM/measurement gate helpers into typed DriftPin tools is mapped in
[`../docs/SIMULATION_TOOLS.md`](../docs/SIMULATION_TOOLS.md).

# Validate the Artifact, Not a Model of It — §11.10 delivered

Companion to `docs/KICKOFF_validate_the_artifact.md`. Written 2026-06-14.

The kickoff named a recurring failure in the gearbox arc: **a green validation passed
while the exported geometry was wrong**, because the check ran on a *model of the
mechanism* rather than the *artifact itself*. This session adds the missing layer —
**RFC §11.10, geometry-realizes-declaration** — and proves it regression-closes the
exact bug that a human eye, not a gate, had to catch.

## Concept validated vs artifact validated

The whole point is that these are *different claims* and they get conflated whenever
the validation and the artifact are two separate objects. Per gearbox claim:

| Claim | "Concept validated" (already had) | "Artifact validated" (added §11.10) |
|---|---|---|
| Selective engagement gives a single-DOF power path at the design ratio | Motion oracle §11.9 (Grübler + ratio consistency) over the declared topology | — (topology is the right altitude for this claim) |
| Overall train ratio = product of stage ratios | Train-ratio gate §11.9 | — |
| Output gear **freewheels** on its shaft | declared `freewheel` in the manifest | `bore_keying`: the exported bore is **round** (no radial flat) |
| Input gear / collar **keyed** to its shaft | declared `keyed` | `bore_keying`: the exported bore carries a **radial flat** (D-key/keyway) |
| Dog collar **engages** the gear | declared in the engagement schedule; a separate PyBullet rig built its *own* clean dog ring and confirmed couple/free | `dog_ring`: the exported collar has **gaps** (interleaving teeth), not a solid face; `contact_band`: the engaged overlap is **in-family** |

The left column is "the concept works." The right column is "this part is right." Before
§11.10 only the left column had gates; the right column was eyeballed — and three times
the eye was needed because a gate was missing.

## What was built

`driftpin/realize.py` — a FreeCAD-bound oracle that consumes the **real `Part.Shape`**
(never a separate idealised model) and asserts the geometry implements the declared
topology. Two primitives, both read about the part's own axis so they are
placement-independent (a property of the part, not of one assembled pose):

- **`bore_keying`** — a `keyed` bore has a radial flat (the D-key chord sits *inside*
  the round bore); a `freewheel` bore is round. The strict "flat centroid inside the
  bore radius" cut is what separates a real keyway from the dog-band faces further out.
- **`dog_ring`** — sampling the material angularly at the dog radius gives a **fill
  fraction** (≈0.47 for an interleaved N-tooth ring, **1.0 for a solid face**) and a
  **sector count** (N teeth vs a single 360° arc). Gaps present ⇒ it can interlock.

Wired into `merge_assembly` as three typed-interface gates (`driftpin/worker.py`):

- **`contact_band`** (kickoff item #1, the cheap anomaly gate) — an *expected* contact
  (engaged dog clutch, press band) has a characteristic overlap volume; one far outside
  its band is the red flag that was waved through. It **owns its pair** (excluded from
  the blunt interference list) and bounds the overlap instead of forbidding it.
- **`dog_ring`** and **`bore_keying`** (item #2, the core) — per-part geometry checks.

Thresholds are not guessed: `scratch/calibrate_realize.py` measures them on real CAD
(interleaved fill 0.47 vs solid 1.0; the gate's 0.80 is a wide moat between them).

## Regression evidence

`tests/test_realize.py` builds a minimal engaged dog-clutch assembly (a freewheel gear
+ a collar, coaxial) two ways that differ **only** in the collar's engagement face, and
runs each through `merge_assembly`:

```
GOOD interleaved collar :  ok=True   (no typed violations)
BAD  solid-face collar  :  ok=False  dog_ring: "SOLID FACE where the dog gaps belong"
                                     contact_band: overlap 249.5 mm³ exceeds 60 mm³ band
```

The solid-face overlap measures **249.5 mm³** — essentially the *250 mm³* the kickoff
cited as the historical jam, against a few-mm³ interleaved clutch. The rig faithfully
reproduces the original bug's signature, and the merge now **FAILS** it. A second test
flips a round-bored gear's declaration to `keyed` and confirms `bore_keying` catches the
"loose gear keyed to nothing" case.

This satisfies the definition of done: a merge that fails if the collar is solid-faced
again, dog-clutch and gear validations that assert against the **exported** geometry,
and this writeup.

## A finding from the calibration itself

The first calibration pass copied its "good" collar from `scratch/dog_clutch_unit.py`
and measured **identical** overlap (339 mm³) for good and bad. That was the oracle
working before it was even finished: `dog_clutch_unit.py`'s collar sleeve is *solid over
the whole dog band*, so the gear teeth jam into it regardless of the dog phase — the same
class of bug. The fixed geometry is in `scratch/gearbox_multispeed.py`, whose sleeve is
raised *above* the dog band so the teeth protrude into open space. Measuring the real
artifact caught a second instance of the bug that reading the declaration never would.

## Scope and deferrals (honest accounting)

- **Done:** §11.10 gates + `realize.py`, calibrated thresholds, end-to-end regression
  through `merge_assembly`, registered in `tests/run_all.sh`.
- **Deliberately minimal:** the regression rig is a 2-part dog clutch, not the full
  `gearbox_multispeed`. It reproduces the exact bug signature (≈250 mm³) and is the
  faithful regression; converting the monolithic `gearbox_multispeed.py` (one
  `run_script` compound) into a component-file manifest so these gates run on the whole
  box is a mechanical follow-up, not a new idea.
- **Not attempted (kickoff items #3–#5):** sim-from-CAD via `p.vhacd`, the slide-and-
  catch contact sim, and re-doing the shift-animation dog teeth. Item #3's own note says
  item #2 is an acceptable substitute when mesh contact is finicky — and the geometry
  check *is* the artifact-consuming validation those sims were meant to provide. The
  half-pitch *interleave* between collar and gear (vs each part merely having gaps) is the
  natural next increment for `realize.py`: a relative-phase check needs both parts in a
  common frame, where the per-part "gaps present" check is placement-independent.

## Pointers

- Oracle: `driftpin/realize.py`; gates in `driftpin/worker.py` (`_gate_contact_band`,
  `_gate_dog_ring`, `_gate_bore_keying`, `_TYPED_GATES`, `_CONTACT_KINDS`).
- Regression: `tests/test_realize.py`. Calibration: `scratch/calibrate_realize.py`.
- Lineage: §11.9 motion oracle (`driftpin/mechanism.py`, `tests/test_mechanism.py`).

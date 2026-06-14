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
topology. The two per-part primitives read about the part's own axis so they are
placement-independent (a property of the part, not of one assembled pose):

- **`bore_keying`** — a `keyed` bore has a radial flat (the D-key chord sits *inside*
  the round bore); a `freewheel` bore is round. The strict "flat centroid inside the
  bore radius" cut is what separates a real keyway from the dog-band faces further out.
- **`dog_ring`** — sampling the material angularly at the dog radius gives a **fill
  fraction** (≈0.47 for an interleaved N-tooth ring, **1.0 for a solid face**) and a
  **sector count** (N teeth vs a single 360° arc). Gaps present ⇒ it can interlock.

A third primitive reads the **relative phase** of two parts in a common (engaged) frame:

- **`interleave`** — sampling co-occupancy at the dog radius gives the **both-occupied
  fraction**: ≈0 when the collar's teeth fall in the gear's gaps (half-pitch offset),
  ≈the tooth fill (≈0.46) when the two rings are **in phase** (teeth-on-teeth). This is
  the layer `dog_ring` can't see — each ring can have gaps yet still jam if they are not
  offset. `dog_ring` asks "does this part have gaps?"; `interleave` asks "do a's teeth
  fall in b's gaps?".

Wired into `merge_assembly` as four typed-interface gates (`driftpin/worker.py`):

- **`contact_band`** (kickoff item #1, the cheap anomaly gate) — an *expected* contact
  (engaged dog clutch, press band) has a characteristic overlap volume; one far outside
  its band is the red flag that was waved through. It **owns its pair** (excluded from
  the blunt interference list) and bounds the overlap instead of forbidding it.
- **`dog_ring`** and **`bore_keying`** (item #2, the core) — per-part geometry checks.
- **`interleave`** — the relative-phase check on an engaged pair; like `contact_band` it
  owns its pair (the meshing teeth) in `_CONTACT_KINDS`.

Thresholds are not guessed: `scratch/calibrate_realize.py` measures them on real CAD
(interleaved fill 0.47 vs solid 1.0, the gate's 0.80 a wide moat; half-pitch
both-occupied 0.0 vs in-phase 0.47, the gate's 0.12 the moat). It builds GOOD / solid /
in-phase collars against a freewheeling gear and prints all four signals — overlap
(4.6 vs 250 mm³), fill, both-occupied, and bore flat — so every threshold is grounded.

## Regression evidence

`tests/test_realize.py` builds a minimal engaged dog-clutch assembly (a freewheel gear
+ a collar, coaxial) two ways that differ **only** in the collar's engagement face, and
runs each through `merge_assembly`:

```
GOOD interleaved collar :  ok=True   (no typed violations)
BAD  solid-face collar  :  ok=False  dog_ring: "SOLID FACE where the dog gaps belong"
                                     contact_band: overlap 249.5 mm³ exceeds 60 mm³ band
IN-PHASE collar (gaps!) :  ok=False  dog_ring PASSES (it has gaps) but interleave fails:
                                     "teeth IN PHASE (46% carry both) — they jam"
```

The solid-face overlap measures **249.5 mm³** — essentially the *250 mm³* the kickoff
cited as the historical jam, against a few-mm³ interleaved clutch. The rig faithfully
reproduces the original bug's signature, and the merge now **FAILS** it. A second test
flips a round-bored gear's declaration to `keyed` and confirms `bore_keying` catches the
"loose gear keyed to nothing" case. A third builds a collar that *has* real gaps but
whose teeth are **in phase** with the gear — it sails through `dog_ring` yet `interleave`
catches the teeth-on-teeth jam, the failure mode the per-part gap check is blind to.

This satisfies the definition of done: a merge that fails if the collar is solid-faced
again, dog-clutch and gear validations that assert against the **exported** geometry,
and this writeup.

## A finding from the calibration itself — and its fix

The first calibration pass copied its "good" collar from `scratch/dog_clutch_unit.py`
and measured **identical** overlap (339 mm³) for good and bad. That was the oracle
working before it was even finished: `dog_clutch_unit.py`'s collar sleeve was *solid over
the whole dog band*, so the gear teeth jammed into it regardless of the dog phase — the
same class of bug. The reference fix already lived in `scratch/gearbox_multispeed.py`,
whose sleeve is raised *above* the dog band so the teeth protrude into open space.
Measuring the real artifact caught a second instance of the bug that reading the
declaration never would.

**That second instance is now fixed.** `dog_clutch_unit.py`'s collar sleeve is raised to
`GH+DOG_H` (above the dog band), mirroring `gearbox_multispeed.collar`, so only
interleaving teeth occupy the band. The fix is verified *on the artifact*, not the source:
`scratch/verify_dog_clutch_unit.py` builds the old and new collars through the §11.10
oracle (`realize`) and then reads the **exported `artifacts/dog_clutch_unit.step`** back
and re-checks the as-shipped collar:

```
BUG   collar (solid face)   : dog_band fill=1.000  sectors=1  overlap=339.3 mm³  -> FAIL
FIXED collar (raised sleeve): dog_band fill=0.456  sectors=6  overlap=  4.6 mm³  -> PASS
EXPORTED dog_clutch_unit.step: fill=0.456  sectors=6  overlap=4.6 mm³  -> PASS (metal realizes the declaration)
```

The 339 mm³ jam collapses to a 4.6 mm³ in-family interleave — the same signature the
headline regression reproduces, now closed in the demo geometry too.

## Scope and deferrals (honest accounting)

- **Done:** §11.10 gates + `realize.py`, calibrated thresholds, end-to-end regression
  through `merge_assembly`, registered in `tests/run_all.sh`.
- **Relative phase, now done:** the half-pitch `interleave` check closed the one real
  hole in the per-part checks — two rings can each have gaps yet still jam if they are in
  phase. It needs both parts in a common frame; calibrated 0.0 (meshed) vs 0.47 (in
  phase) and regression-tested (in-phase collar passes `dog_ring`, fails `interleave`).
- **Whole box now validated on the artifact:** `scratch/verify_gearbox_box.py` runs all
  four checks over every collar and gear of the 3-speed `gearbox_multispeed` — the
  headline assembly that was previously only eyeballed. All 10 parts pass: keyed gears
  carry D-flats, freewheel gears are round-bored 6-tooth rings (fill 0.45), both collars
  are real dog rings, the engaged pair overlaps 4.6 mm³ and interleaves (both-occupied
  0.0), the neutral collar is clear (0 mm³). A full component-file manifest through
  `merge_assembly` would exercise the gate plumbing too, but the 2-part rig already covers
  that; this validates the *geometry* of the real box, which was the point.
- **Shift animation redone (kickoff #5):** `scratch/gearbox_shift_animate.py` now draws
  the dog teeth as half-pitch combs that mesh into each other's gaps when engaged (and
  show clear daylight in neutral), instead of same-phase solid blocks.
- **Slide-and-catch, now done (kickoff #4):** `scratch/dog_clutch_slide_sim.py` drives
  the REAL exported collar axially into the gear and records it as a GIF for human
  review, with the §11.10 oracle overlaid on each frame — a clean CATCH (overlap 0 → 4.6
  mm³, both-occupied 0.0) vs an in-phase JAM (254 mm³, 0.46). Done with kinematics + the
  geometry oracle rather than finicky rigid-body mesh contact (which item #3's note
  accepts as a substitute). Part of the simulation-video-capture kickoff
  (`docs/KICKOFF_simulation_video_capture.md`).
- **Sim-from-CAD, now done as a spike (kickoff #3):** full rigid-body mesh contact via
  `p.vhacd` on the REAL exported parts — `scratch/dog_clutch_cad_sim.py`. It drives the
  decomposed metal in PyBullet with no constraint imposing the coupling, and the
  dog-clutch selector emerges from contact: engaged transmits (~96%), disengaged
  freewheels (0%). See the dedicated section below. The geometry checks + the kinematic
  slide-and-catch were already the artifact-consuming validation, so this is the gravy
  that turned out tractable — and it surfaced a lesson one level down (a real dog clutch
  needs running clearance) that the static checks never could.

## Sim-from-CAD — kickoff #3, done as a spike

`scratch/dog_clutch_cad_sim.py` closes the last open item: a *dynamic* rigid-body sim
that couples through the metal itself, with **no constraint imposing the coupling** — the
opposite of the circular Tier-2 sim that imposed the gear ratio and measured it back. It
drives the **real exported** dog clutch in PyBullet and the selector *emerges from
contact*:

```
[1] §11.10 oracle on the Part.Shape : gear/collar fill 0.45, 6 sectors;
                                       engaged both-occupied 0.00 / in-phase 0.46
[2] p.vhacd the dog-tooth band       : gear/good/bad -> 6 convex hulls each (~2 s)
[3] FAITHFULNESS GATE (the crux)     : §11.10 signal re-read on the DECOMPOSED collision
       gear/collar dog ring : fill 0.489  sectors 6     (matches oracle 0.45 / 6)
       GOOD engaged         : both-occupied 0.000       (teeth interleave)
       BAD  in-phase        : both-occupied 0.489       (teeth-on-teeth)
       -> decomposition PRESERVES the gaps; physics geometry agrees with the oracle
[4] DYNAMIC CONTACT (gear driven; does the collar follow?)
       ENGAGED    collar = +96% of gear speed   (driven through the interleaved teeth)
       DISENGAGED collar =   0% of gear speed    (collar lifted clear; gear free at 4.0)
       -> SELECTS — emergent from contact on the REAL decomposed metal
```

The kickoff named the exact reason this was deferred as finicky: a *rotating* body in
PyBullet needs **convex** collision, and a naive convex hull of a dog ring **fills the
gaps**, so engaged would look like disengaged. The make-or-break is step [3]: convex
**decomposition** (`p.vhacd`) loaded as a **compound of per-hull pieces** — not one
convexified mesh — keeps the six teeth distinct (both-occupied 0.000 engaged vs 0.489
in phase, fill/sectors matching the oracle). So "engaged ≠ disengaged" *is* representable
in rigid-body contact on the real part. That refutes the specific worry the kickoff
raised, measured on the actual exported metal. The run records the engaged/disengaged
trajectories into `artifacts/dog_clutch_cad_sim.gif` (real STLs rendered along the
simulated joint angles — collar tracks the gear, then freewheels when lifted).

**Two findings the rigid-body sim surfaced that the static checks could not** — itself the
validate-the-artifact lesson, one level down:

1. **The collision must be the dog-tooth band, not the whole part.** Including the
   collar's *sleeve* face made it cap axially onto the gear-teeth tops, and the assembly
   locked solid (rotation 0). Clipping to z∈[GH, GH+DOG_H] — the real metal that actually
   clutches — fixed it. The disk and power teeth play no role in engagement.
2. **The idealised CAD carries zero running clearance.** The few-mm³ "in-family" root
   overlap the oracle measures (4.6 mm³) is real interference at the box-tooth roots; a
   real dog clutch is a *slip fit*. With zero clearance the engaged gear runs tight (it
   drags from 4.0 to ~3.8 rad/s) where a slip-fit clutch would spin free. The selector
   still holds on the part as-exported, but only a contact sim makes "this needs
   clearance" visible — neither the declaration nor a clean from-scratch PyBullet rig
   ever would.

Honest scope: this is the spike the kickoff asked for (#3 explicitly accepts the
geometry check as a substitute). The dynamic *slide-and-seat* under contact (collar
pushed axially into the spinning gear until the teeth catch / jam) is left to the
robust kinematic version already shipped as #4 (`scratch/dog_clutch_slide_sim.py`);
the rotational selector above is the artifact-consuming dynamic result #3 was after.

## Pointers

- Oracle: `driftpin/realize.py` (`bore_keying`, `dog_ring`, `interleave`); gates in
  `driftpin/worker.py` (`_gate_contact_band`, `_gate_dog_ring`, `_gate_bore_keying`,
  `_gate_interleave`, `_TYPED_GATES`, `_CONTACT_KINDS`).
- Regression: `tests/test_realize.py` (solid-face, keyed-to-nothing, in-phase jam).
  Calibration: `scratch/calibrate_realize.py` (all four signals on real CAD).
- Demo-artifact fix + as-exported check: `scratch/verify_dog_clutch_unit.py` (reads
  `artifacts/dog_clutch_unit.step` back); fixed geometry in `scratch/dog_clutch_unit.py`.
- Whole-box artifact check: `scratch/verify_gearbox_box.py` (every collar/gear of
  `gearbox_multispeed`). Shift animation: `scratch/gearbox_shift_animate.py`.
- Sim-from-CAD (#3): `scratch/dog_clutch_cad_sim.py` (vhacd-decomposed real metal driven
  in PyBullet; faithfulness gate + emergent selector; `artifacts/dog_clutch_cad_sim.gif`).
- Lineage: §11.9 motion oracle (`driftpin/mechanism.py`, `tests/test_mechanism.py`).

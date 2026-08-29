# Kickoff — Validate the Artifact, Not a Model of It

Scope of work for the next session, written 2026-06-14 at the end of the gearbox arc.

> **Status (2026-06-14):** ALL FIVE items are now done. Items #1 (anomaly gate) and #2
> (geometry-realizes-declaration) shipped as **RFC §11.10** — see
> `docs/archive/VALIDATE_THE_ARTIFACT.md` for the writeup, `ankusdrive/realize.py` for the oracle,
> and `tests/test_realize.py` for the regression (a merge that FAILS the solid-face collar
> at 249.5 mm³). Since extended: a **`interleave`** gate (relative half-pitch phase —
> catches teeth-on-teeth that pass the per-part gap check), the whole **`gearbox_multispeed`**
> box validated on the artifact (`scratch/verify_gearbox_box.py`), and the **shift animation
> redone** (#5) to show the real interleave. **#4 (slide-and-catch)** —
> `scratch/dog_clutch_slide_sim.py` records the collar sliding into the gear on the real
> CAD with the oracle overlaid (catch 4.6 mm³ vs jam 254 mm³); see
> `docs/KICKOFF_simulation_video_capture.md`. **#3 (sim-from-CAD via `p.vhacd`), now done
> as a spike** — `scratch/dog_clutch_cad_sim.py` drives the REAL exported dog clutch in
> PyBullet as a vhacd convex decomposition (compound of per-hull pieces, which preserves
> the dog gaps the kickoff feared a hull would bridge — faithfulness gate confirms it),
> and the selector emerges from contact with no constraint imposing it: engaged transmits
> (~96%), disengaged freewheels (0%). It also surfaced a lesson the static checks can't —
> the idealised CAD has zero running clearance, so a real dog clutch needs a slip fit
> (`artifacts/dog_clutch_cad_sim.gif`).

## Why this exists

Three times in the gearbox work a **green validation passed while the exported
geometry was actually wrong**, because the validation ran on a *model of the
mechanism* rather than on the *artifact itself*:

1. **"Loose gears."** The manifest declared selective engagement; the CAD was plain
   gears bored onto plain shafts — no keys, no dog clutch. The motion oracle (§11.9)
   certified the *declaration*. The metal couldn't move.
2. **Circular Tier-2 sim.** It imposed the gear ratio as a PyBullet `JOINT_GEAR`
   constraint and then measured that same ratio back. It could not fail.
3. **Dog clutch sim vs CAD.** `scratch/dog_clutch_sim.py` built its *own* clean
   box-tooth dog ring in PyBullet and confirmed engaged→couples / disengaged→frees.
   The CAD collar, built separately, had a **solid face where the gaps belonged**, so
   it could not interlock. The sim never consumed the CAD, so it could not see the
   bug. The interference scan *did* flag it — the engaged clutch overlapped **250 mm³**
   where an interleaved clutch is ~5 mm³ — but that anomaly was rationalized as
   "intended (engaged dog clutch)" and waved through. A human eye caught it instead.

**Throughline:** a green sim means *"the concept works,"* not *"the part is right."*
Those are different claims, and they get conflated whenever the validation and the
artifact are two separate objects. The reliable guard is to **make the check consume
the actual exported geometry**, and to **treat out-of-family numbers as red flags**
rather than narrating them as expected.

## Goal

Build a validation layer that judges whether the **exported CAD geometry realizes the
declared mechanism**, and that drives the **real geometry** (not an abstraction) where
feasible. Candidate RFC slot: **§11.10 — geometry-realizes-declaration**.

## Work items (priority order)

1. **Anomaly gate on interference (cheap, highest value).** In `merge_assembly`,
   classify each solid-overlap by its declared interface type (gear mesh, bore fit,
   engaged dog clutch, …) and assert the overlap volume sits in the expected band for
   that type. An engaged dog clutch at 250 mm³ when interleaved clutches are ~5 mm³ is
   a hard FAIL, not a footnote. This converts the signal that was waved through into a
   gate. *Regression target: with the old solid-face collar, this FAILS.*

2. **Geometry-realizes-declaration check (the core).** Given the manifest `mechanism`
   block + the exported CAD, verify the metal backs the declaration:
   - `keyed` link → the gear bore is actually non-round (D-flat/keyway) matched to the
     shaft, not a round freewheel bore.
   - `freewheel` → round bore with clearance.
   - dog clutch / engagement → collar and gear have **interleaving** dog teeth (gaps
     present, half-pitch offset), not solid faces.
   The motion oracle judges the declared *topology*; this judges whether the geometry
   *implements* it. This is the missing layer.

3. **Sim-from-CAD (structural fix; scope as a spike).** Drive the ACTUAL exported
   geometry in PyBullet — load the real collar + gear via mesh collision / convex
   decomposition (`p.vhacd`), attempt slide-and-engage, confirm transmit-when-engaged /
   free-when-disengaged on the real part. Rigid-body mesh contact is finicky; if it
   does not converge, item #2 is the pragmatic substitute and that's an acceptable
   outcome — just say so.

4. **Slide-and-catch contact sim.** The fork physically sliding the collar (prismatic
   drive) until the dog teeth catch — currently only a kinematic schematic
   (`scratch/gearbox_shift_animate.py`).

5. **Re-do the shift animation dog teeth** to show the real interleave (the schematic
   draws solid blocks).

## Definition of done

- A merge of the gearbox manifest that **FAILS if the collar is solid-faced again** —
  i.e. regression-proof against this exact bug, via the anomaly gate (#1) or the
  realize-declaration check (#2).
- The dog-clutch and gear validations **assert against the exported STEP/STL**, not a
  separate idealized model.
- A writeup that separates, per claim, *"concept validated"* from *"artifact
  validated."*

## Pointers (this session's outputs)

- CAD: `scratch/gearbox_multispeed.py` (countershaft box + shift fork/rail + dog
  clutch), `scratch/gearbox_real.py`, `scratch/dog_clutch_unit.py`.
- Sims (abstract / not CAD-coupled): `scratch/gear_contact_sim.py`,
  `scratch/dog_clutch_sim.py`, `scratch/gearbox_multispeed_sim.py`.
- Sim-from-CAD (#3, CAD-coupled rigid body): `scratch/dog_clutch_cad_sim.py` — vhacd
  decomposition of the real exported parts driven in PyBullet; faithfulness gate +
  emergent selector; `artifacts/dog_clutch_cad_sim.gif`.
- Renders: `scratch/render_dogclutch.py` (the two-colour interleave),
  `artifacts/dogclutch_engaged.png`, `artifacts/gearbox_multispeed.{step,stl}`.
- Motion oracle: `ankusdrive/mechanism.py` + the `merge_assembly` mobility/typed gates
  in `ankusdrive/worker.py`.
- The interference-magnitude signal: the pairwise common-volume scan run this session
  (250 mm³ jammed vs ~5 mm³ interleaved) — formalize that into item #1.
- Narrative + corrections: `tests/MULTI_AGENT_EVAL.md` (gearbox sections).

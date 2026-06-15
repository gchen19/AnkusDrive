# Kickoff — Simulations → Video Recordings for Human Review

Written 2026-06-14, at the end of the validate-the-artifact arc. A *potential next
kickoff*: make the simulations DriftPin already runs produce **video** a human can
watch, not just scalar tables and one-off scratch GIFs.

> **Status (2026-06-14):** item **A** (motion → video on the real geometry) is **DONE**,
> both halves. (1) `scratch/sim_video.py` (reusable frames→GIF pipeline, GIF via Pillow) +
> `scratch/dog_clutch_slide_sim.py` record the dog-clutch slide-and-catch on the exported
> CAD with the §11.10 oracle overlaid: the collar slides home, turns green at the catch
> (overlap 0 → 4.6 mm³, both-occupied 0.0), while an in-phase collar jams (254 mm³,
> 0.46) — this also closes validate-kickoff #4. (2) The MBD half:
> `scratch/meshing_gears_video.py` drives a real 12T→24T involute-gear pair through the
> actual MBD executor (`mbd.run_mbd`, what `mechanism_simulate_submit` delegates to) and
> renders the real exported metal turning at each per-link world placement — measured
> ω_out/ω_in = −0.500 realises the declared −Na/Nb, overlaid on every frame
> (`artifacts/meshing_gears_spin.gif`). This required two solver changes: `run_mbd` now
> also returns per-link **`orientations`** (a revolute link's COM sits on its spin axis,
> so `trajectories` alone show no rotation — position-only data renders frozen gears), and
> `mechanism_simulate_submit` now forwards a **`gears`** coupling (it never did, though
> `run_mbd` and the §11.9 oracle already supported it). `sim_video.place()` is the generic
> bridge: apply any (pos, quat) sample to a real mesh. **Item B (FEM modal-shape
> animation) is also DONE** — `scratch/modal_shape_video.py` solves a real steel cantilever
> in CalculiX, extracts the tet-mesh SURFACE (tet faces on exactly one element) + each
> eigenvector at the surface nodes, and animates one GIF per mode
> (`artifacts/modal_mode{1,2,3}.gif`) as `node + amp·sin(2π·phase)·eigenvector`, coloured
> by modal amplitude, with the closed-form `beam_modal` oracle overlaid per frame:
> CalculiX 93 / 276 / 580 Hz match Euler-Bernoulli at **ratio 1.00** (1st out-of-plane
> bend, 1st in-plane bend, 2nd out-of-plane). Items **C** (transient thermal) and **D**
> (CFD) open.

## Why this exists

Two gaps, one principle.

1. **The outputs are numbers.** Every solver returns scalars or short time series —
   MBD gives trajectories + max torques, FEM gives peak von-Mises / mode frequencies,
   transient thermal gives a centre/surface temperature history, CFD gives a pressure
   drop. A reviewer cannot *see* the mechanism move, the mode shape oscillate, or the
   part heat up. The one visual layer that exists (`scratch/gearbox_animate.py`,
   `scratch/gearbox_shift_animate.py`) is **2-D matplotlib schematics**, hand-built per
   demo, living outside the worker, drawing *abstractions* of the parts.

2. **No integrated video.** There is no path from a solver result to a recording. There
   is a static still renderer (`driftpin/render.py` NumPy rasterizer; `render_photoreal`
   via POV-Ray/LuxCore) but it has no temporal dimension — no camera-pose sequence, no
   frame stacking, no encoder hook.

**Throughline (inherited from §11.10):** a video for human review must show the **real
artifact** — the exported `Part.Shape` / STL in motion — never a separate idealised
sketch of it. The whole reason the gearbox arc needed a human eye was that the
*validation* and the *artifact* were different objects (see
`docs/KICKOFF_validate_the_artifact.md`). A review video drawn from an abstraction would
reintroduce exactly that gap. So every item below renders the geometry the merge
actually produced, and — where there is a claim to check — overlays the §11.10 oracle's
verdict on the frame, so the reviewer sees the metal move *and* the gate agreeing.

## What exists today (survey, 2026-06-14)

| Family | Computes | Visual today | Gap to video |
|---|---|---|---|
| **MBD** (`driftpin/analysis/mbd.py`, `mechanism_simulate_submit`) | trajectories, **orientations**, max torques, collisions-through-motion | **DONE** — `meshing_gears_video.py` renders real gears at each placement | ✓ real geometry along the trajectory → GIF |
| **FEM modal** (`fem_modal_results`) | eigenfreqs + displacement vectors | none | scale eigenvector by sin(phase), deform mesh, sweep |
| **FEM transient thermal** (`driftpin/analysis/elmer.py`) | centre/surface temp vs time | none | colour the part / a profile by T(t) |
| **FEM harmonic/buckling** | peak stress, freqs (no time domain) | none | single annotated still, not a video |
| **CFD** (OpenFOAM, `cfd_*`) | ΔP, drag coeff (fields only as `.foam`) | none | parse field snapshots, contour per step (hard) |
| **Rendering** (`driftpin/render.py`, `render_photoreal`) | one PNG per view | static only | accept a pose/parameter sequence |

**Encoding landscape:** `Pillow` (GIF, in-tree, works today), `matplotlib` +
`numpy` in the venv; **no `ffmpeg` on PATH and no `imageio`** → **MP4 is not available
without adding a dependency.** Decision for now: **ship GIF via Pillow** (the existing
`gearbox_shift_animate.py` already does), and gate any MP4 path behind a capability
check (`shutil.which("ffmpeg")`) that degrades to GIF — the same solver-family
degradation pattern the repo uses elsewhere. Don't hard-require ffmpeg.

## Work items (priority order)

**A. Motion → video, on the real geometry (lowest effort, highest value).** A reusable
pipeline: given a list of per-part placements over time + each part's real mesh, render
each frame (multi-part, colour-coded) and encode a GIF. Drives the actual exported STL,
not a sketch. First consumers:
   - the **dog-clutch slide-and-catch** (validate-kickoff #4): the collar slides axially
     into the gear until the dog teeth catch — recorded, with the §11.10 interleave
     oracle confirming a clean catch (overlap 0 → in-family ~4.6 mm³, both-occupied ≈0)
     vs a jam (an in-phase collar spikes the overlap). This is the artifact-consuming
     "slide-and-catch" the validate arc deferred, done robustly with kinematics + the
     geometry oracle rather than finicky rigid-body mesh contact.
   - the **MBD trajectory** from `mechanism_simulate_submit` (**done**,
     `scratch/meshing_gears_video.py`): a real 12T→24T gear pair, driven through
     `mbd.run_mbd`, rendered as the real exported metal at each per-link world placement
     (position + the new `orientations` quaternion) with the gear-ratio oracle overlaid —
     measured ω_out/ω_in = −0.500 realises declared −Na/Nb. Enabled by recording
     orientations (a gear's COM is on its spin axis, so positions alone show no rotation)
     and by the handler forwarding a `gears` coupling.

**B. FEM modal shape animation — DONE** (`scratch/modal_shape_video.py`). The eigenvector
per mode is scaled by `sin(2π·phase)` over N frames, the real result mesh is deformed, and
each mode loops as its own GIF coloured by modal amplitude. Two things the kickoff sketch
glossed: (1) `fem_modal_results` only exposes the *scalar* max-displacement, so the script
reads each mode's `DisplacementVectors` + `NodeNumbers` off the result objects directly in
the worker. (2) A box tessellates to 12 flat triangles, so deforming its corners shows
nothing — the script instead extracts the *tet-mesh surface* (the tet corner-faces that
belong to exactly one element) as the deformable skin. Self-checking per the §11.10
principle: the `beam_modal` Euler-Bernoulli oracle rides on each frame, and CalculiX
93/276/580 Hz match it at ratio 1.00 (the dominant-axis classifier labels each mode and
matches it to its bending plane). `artifacts/modal_mode{1,2,3}.gif`.

**C. Transient thermal animation.** `elmer.py` returns T(t) at centre/surface. Colour
the real part (or a section) by interpolated temperature over the time steps; overlay the
scalar curve. Reuses the pipeline from A. (Field-on-mesh colouring is the new bit; the
1-D fallback — an animated profile + curve — is trivial and a fine first cut.)

**D. CFD field animation (deferred — scope as a spike).** OpenFOAM writes full transient
U/p fields, but only as native `.foam` binaries; rendering them needs field parsing + mesh
reconstruction (or a VTK/ParaView dependency). Highest effort, lowest near-term value;
punt unless a CFD review specifically needs it.

**Cross-cutting (after A proves out):** promote the pipeline from `scratch/sim_video.py`
into `driftpin/render.py` and expose a `render_motion` / `*_animate` worker tool so a
video is a first-class job result (like `render_photoreal_submit`), not a scratch script.

## Definition of done (per item)

- A solver result becomes a **GIF that shows the real exported geometry**, written to
  `artifacts/`, runnable from one script under the venv.
- Where the video backs a *claim* (engagement, mode, thermal limit), the §11.10-style
  oracle verdict is computed on the same geometry and overlaid on the frame — the video
  shows the metal moving AND the gate's agreement, so review is "watch + read the number,"
  not "trust the picture."
- A capability check degrades MP4→GIF rather than hard-failing when ffmpeg is absent.

## Pointers

- Survey basis: `driftpin/analysis/mbd.py`, `driftpin/analysis/elmer.py`,
  `driftpin/render.py`, `mechanism_simulate_submit`/`fem_modal_results` in
  `driftpin/worker.py`; existing 2-D animators `scratch/gearbox_animate.py`,
  `scratch/gearbox_shift_animate.py`; still renderer `scratch/render_gearbox.py`.
- Item A (done): `scratch/sim_video.py` (now with `place()` — apply an MBD (pos, quat)
  to a real mesh), `scratch/dog_clutch_slide_sim.py`, `scratch/meshing_gears_video.py`
  (MBD-driven). Solver changes: `orientations` in `mbd.run_mbd`; `gears` forwarded by the
  `mechanism_simulate_submit` handler. Tests: `tests/test_mbd.py`
  (`test_orientations_track_driven_revolution`).
- Item B (done): `scratch/modal_shape_video.py` — real CalculiX cantilever modal solve,
  tet-mesh surface extraction + eigenvector animation, `beam_modal` oracle overlaid
  (`artifacts/modal_mode{1,2,3}.gif`). Modal pipeline: `fem_modal`/`fem_modal_results` +
  `_build_cantilever_fem` in `tests/test_worker.py` (`test_fem_modal_cantilever`).
- Lineage: validate-the-artifact arc (`docs/VALIDATE_THE_ARTIFACT.md`, RFC §11.10) — the
  geometry oracle whose verdicts these videos overlay.

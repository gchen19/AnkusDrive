# Variant B — one camera, flip the plate, light pad floor

Same instrument, half the parts. Chosen 2026-09-09 after the question "what if I just flip the
plate and use one camera". Rev 2.0 files here: `build_scanner_v2.py` (parametric source), `frame.stl`,
`tower.stl`, `door.stl`, `colony_scanner_v2.step`, renders, `section_v2.png`. **The current
design is rev 3.1 in `v21/`** (Pi bay, control cassette, real cables, focus mode); use its STLs, STEP
and renders, and `v21/README.md` for what changed.

## How you use it

The box stands on an A5 LED tracing pad (the pad sticks out at the sides; that is fine).

1. Open the flap, slide the plate in **lid up**, drop the flap, press the button. The camera
   photographs the lid: your marker labels, backlit, sharp. `lid_labels.png`.
2. The status light blinks. Pull the plate out, **flip it over** (lid down, well bottoms up),
   slide it back in, press again. The camera now sees the well floors through the plate bottom in
   transmission, and the lid ink is on the far side, in contact with the pad, where it casts only
   a smooth ≤10 % dimming (`../optics_model.py`, "pad on lid": 2.8 sr source, 70 mm penumbra).
3. Done. Same outputs as variant A.

**Orientation rule (so A1 is A1):** load lid-up with **A1 at the rear-left** (the plate's cut corner
toward you on the right, on CELLTREAT plates). Flip the plate **toward you, about its long axis**, so
A1 lands front-left. `instrument.flip_axis: long` in the config mirrors the well map for the colony
shot. If the six-ring fit residual in the run record exceeds 0.5 mm, the plate went in wrong way
round; the label photo shows which.

Both planes, the lid top in shot 1 and the well floors in shot 2, sit 21 to 22.5 mm above the
pad, so one fixed focus serves both.

## What changed from variant A

| | Variant A (two cameras) | Variant B (flip) |
|---|---|---|
| Cameras | HQ + 6 mm below, Module 3 above | HQ + **8 mm** above |
| Light | COB cove in a white hood | A5 tracing pad, always on |
| Prints | 5 (base, deck, hood, ceiling, door) ≈ 17 h | 3 (frame, tower, door) ≈ 11 h |
| Size | 176 × 118 × 315 mm | 176 × 107 × 226 mm + pad |
| Parts cost | ≈ $260 | ≈ $225 |
| Button presses per plate | 1 | 2 (plus one flip) |
| Lid photo | separate camera, same instant | same camera, first press |

## Parallax, and why the lens is 8 mm now

A camera at finite distance sees the corner wells at an angle; the far side of each floor is
viewed through the 17 mm well wall. The software now excludes exactly that crescent (the floor
disc ∩ the well-mouth disc projected along the rays, `instrument.camera_height_mm`), and
reports colony area fraction on the usable area. Longer working distance = smaller crescent:

| Lens | Sensor to colonies | Corner-well angle | Crescent | Usable area lost, corner wells | Tower height |
|---|---|---|---|---|---|
| 6 mm | 150 mm | 16.3° | 5.0 mm | 20 % | 176 mm |
| **8 mm** | **200 mm** | **12.3°** | **3.7 mm** | **15 %** | **226 mm** |
| 12 mm | 300 mm | 8.3° | 2.5 mm | 10 % | 326 mm |

8 mm is the compromise; `SENSOR_Z` in the build script and `camera_height_mm` in the config are
the two numbers to change for a 12 mm lens. (Variant A had the same crescent from below and did
not say so; the cut in `quantify.py` applies to both.)

## How you use it, including cancelling and recovering

See `OPERATION.md`: tap = go, hold = no, footer on the screen always says what each does; timeouts,
power-loss recovery and rejected runs are handled and recorded.

## Verify before printing (in addition to `../MEASUREMENTS-REQUIRED.md`)

- **Lid outer size**: datasheets (Eppendorf, SPL) show lids are the same size or *smaller* than
  the plate footprint, so the pocket is now sized on the plate skirt (0.4 mm/side) and the lid
  sits in it with 0.4–1.0 mm slop in the flipped shot; the software's ring fit absorbs that.
  See `../PLATE-COMPATIBILITY.md`.
- **Pad surface**: the plate slides on it. If yours is soft or textured, glue a 0.5 mm clear
  PET sheet over the lit area.
- **Pad brightness**: 3000 lux gives ~4 ms at F5.6. Anything ≥ 1000 lux works; adjust `exposure_us`.

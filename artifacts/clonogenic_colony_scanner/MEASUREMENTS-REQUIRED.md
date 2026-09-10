# Measurements required before printing — read first

**Update 2026-09-09:** the lab's plates are CELLTREAT 229105 and the manufacturer's drawing covers
M1, M2, M4, M5, M9 with tolerances; M6 and M8 are verified from the Raspberry Pi drawings. See
`PLATE-COMPATIBILITY.md`. Only M3 (skirt rim width, not on the drawing) and M10 (your LED strip)
remain, and the frame test print covers both.

Every dimension below was taken from a standard or a datasheet I could not open in this
session, so it is `distilled` until you put a caliper on the real thing. Each one drives a
named parameter in `build_scanner.py`; change the number, re-run the build, re-export.

| ID | Measure | Drives | Design value | Fails if |
|---|---|---|---|---|
| M1 | Your 6-well plate + lid: total height | `PLATE_H`, `SLOT_Z1` | 22.5 mm (slot 34 mm) | plate + lid > 31 mm won't pass the slot; > 30 mm hits the cove lip's shadow line |
| M2 | Plate footprint L × W at the skirt | `PLATE_L`, `PLATE_W` | 127.76 × 85.47 (SLAS) | most brands hold this within ±0.3; the nest clearance is 0.3/side |
| M3 | Skirt rim width (flat bottom perimeter) | `APER_X/Y` ledge = 3.4 mm | ≥ 2.5 mm needed | a plate whose skirt is narrower than 2.5 mm sits on the well floors instead |
| M4 | Well-floor height above the skirt bottom | `WELL_FLOOR` in optics_model | 1.5 mm | only shifts focus; refocus the lens |
| M5 | Well ID (growth-area diameter) | `well_id_mm` in config.yaml | 34.8 mm | the ring template in quantify.py needs it within ±0.5 mm |
| M6 | HQ camera mounting-hole pattern | floor bosses at ±15 mm | 30 × 30 mm square, M2.5 | if it's not 30 × 30, edit the boss loop (4 lines) |
| M7 | HQ camera: sensor plane to PCB top (with the 6 mm lens fitted, lens front height) | `SENSOR_Z`, `CAM_STANDOFF` | sensor ≈ PCB top | ±2 mm is absorbed by focus; ±10 mm changes pixel scale 7 % — recalibrate anyway |
| M8 | Camera Module 3 hole pattern & lens position | ceiling M2 holes at ±10.5, ±6.25 | 21 × 12.5 mm | lens must be centred on the Ø12 hole; slot the holes if not |
| M9 | Lid overhang: how far the lid skirt drops below the plate's top edge | pocket wall height 6 mm | lid skirt bottom must be > 6 mm above the skirt bottom | a deep-skirt lid would ride on the nest walls instead of the plate seating |
| M10 | COB strip width & thickness | `COVE_W` 9 mm shelf, `LIP_H` 6 mm | 8 mm wide, ≤ 2 mm thick | a 10 mm strip needs `COVE_W = 11` |

How to check M1–M5, M9 in two minutes: caliper on one plate with its lid. Check M6–M8 on the
Raspberry Pi mechanical drawings (raspberrypi.com/documentation → camera → mechanical).

Print the **deck first** (2.5 h): it proves M1–M3, M9 with the real plate before the two big
prints. If the plate seats flat on the ledge, slides in against the rear wall, and the lid
clears the pocket walls, the rest is safe.

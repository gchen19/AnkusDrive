# Bill of materials — clonogenic 6-well plate scanner

Prices are 2026 US list, rounded; check current ones before ordering. Nothing here is
exotic: two Raspberry Pi cameras, one Pi, one LED strip, four printed parts.

## Electronics

| # | Part | Qty | USD | Notes |
|---|---|---|---|---|
| 1 | Raspberry Pi 5, 4 GB | 1 | 60 | two CSI ports → both cameras on one board |
| 2 | Pi 5 27 W USB-C PSU | 1 | 12 | also feeds the 5 V LED strip |
| 3 | Pi Active Cooler | 1 | 5 | |
| 4 | microSD 32 GB A2 | 1 | 8 | |
| 5 | Raspberry Pi HQ Camera (IMX477, CS-mount) | 1 | 50 | **bottom / quantification camera** |
| 6 | Raspberry Pi 6 mm CS lens (PT361060M3MP12) | 1 | 25 | wide; F5.6 for the shots |
| 7 | Raspberry Pi Camera Module 3 (standard, 66°) | 1 | 25 | **top / label camera** |
| 8 | Pi 5 camera cable 22→15 pin, 300 mm | 1 | 3 | HQ camera |
| 9 | Pi 5 camera cable 22→15 pin, 500 mm | 1 | 4 | top camera, runs down the outside of the hood |
| 10 | 5 V COB LED strip, 8 mm, CRI ≥ 90, 5000 K, 1 m | 1 | 12 | cut to 4 × ~190 / ~130 mm for the cove |
| 11 | Logic-level MOSFET module (IRLZ44N / D4184 board) | 1 | 4 | GPIO 18 switches the strip |
| 12 | 24 mm arcade button, LED-lit | 1 | 5 | front of base, GPIO 17 |
| 13 | 5 mm LED + 330 Ω | 1 | 1 | status, GPIO 27 |
| 14 | Dupont wire / 22 AWG hookup | — | 5 | |

Subtotal electronics ≈ **$219**

## Mechanical

| # | Part | Qty | USD | Notes |
|---|---|---|---|---|
| 15 | Black PETG or ASA filament (matte) | ~0.35 kg | 12 | base, deck, door |
| 16 | White PETG filament (matte) | ~0.25 kg | 10 | hood, ceiling |
| 17 | N35 disc magnets Ø6 × 2 mm | 4 | 3 | door catch (2 pairs) |
| 18 | 1.75 mm filament offcut | 2 × 12 mm | 0 | door hinge pins |
| 19 | M2.5 × 6 screws (HQ camera to floor bosses) | 4 | 2 | self-tap into Ø2.2 holes, or M2.5 heat-set inserts |
| 20 | M2 × 6 screws + 2 mm nylon spacers (Camera Module 3 to ceiling) | 4 | 2 | |
| 21 | M3 × 10 screws (ceiling to hood corner bosses) | 4 | 2 | self-tap into Ø2.6 holes |
| 22 | M2.5 × 11 mm nylon standoffs + screws (Pi to rear wall) | 4 | 4 | Pi 5 hole pattern 58 × 49 mm |
| 23 | Black self-adhesive flock paper, A4 | 1 | 6 | line the base cavity interior (optional, kills reflections) |
| 24 | Blank 6-well plate with lid (calibration flat + geometry) | 1 | — | you already have these |

Subtotal mechanical ≈ **$41**

## Total ≈ **$260** — under the $300 floor of the budget.

Optional upgrades that stay under $500:
- Pi Touch Display 2 ($60): on-device preview and a "scan" button on screen instead of the arcade button.
- Amber 590 nm 5 V LED strip ($10) as a second cove segment on GPIO 23: matches the crystal
  violet absorption peak and buys ~1.5× OD contrast over the green channel of white light.
- 8 mm CS lens ($20) at a 200 mm working distance: less barrel distortion than the 6 mm; needs
  a 50 mm taller base (change `SENSOR_Z` in `build_scanner.py`).

## Printed parts (PETG, 0.2 mm layers, 0.4 nozzle, 20 % infill, 3 walls)

| Part | Colour | Bbox (mm) | Est. time | Est. filament |
|---|---|---|---|---|
| base | black | 176 × 118 × 160 | ~7 h | ~200 g |
| hood | white | 176 × 123 × 141 | ~5.5 h | ~160 g |
| deck | black | 176 × 118 × 13 | ~2.5 h | ~70 g |
| ceiling + door | white / black | 176 × 118 × 3, 160 × 3 × 50 | ~1.7 h | ~50 g |

Orientations: base floor-down; deck spigot-down; hood tongue-down (the cove's 45° underside
and the hinge-lug wedges print support-free); ceiling flat; door flat on its outer face.
Estimates from `slice_estimate` (analytic, ±50 %); slice for real before printing.

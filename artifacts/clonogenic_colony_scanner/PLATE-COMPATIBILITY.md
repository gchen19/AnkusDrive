# Plate compatibility — what the datasheets actually say (checked 2026-09-09)

**The lab's plates are CELLTREAT 229105/229106.** CELLTREAT publishes a full dimensioned drawing
(`refs/CELLTREAT_6-Well-Plate_drawing_2020-07-02.pdf`), so every number below for that column is
from the manufacturer. The design and the software default to it (`plate: celltreat_229105`).

| | **CELLTREAT 229105** | Corning 3516 | Falcon 353046 | Eppendorf 0030720 | TPP 92006 | SPL 30006 | Greiner / Nunc |
|---|---|---|---|---|---|---|---|
| Footprint L × W | **127.8 × 85.38** (±0.2) | 127.76 × 85.47 | ~128 × 86 | 127.8 × 85.5 | 127.8 × 85.5 | 127.6 × 85.4 | 127.76 × 85.46 |
| Height, no lid | **20.2** (±0.15) | 20.27 | — | 20.0 | 20.2 | 20.2 | — |
| Height with lid | **23.0** | — | — | 23.2 | 22.4 | ~22.5 | — |
| Lid outer L × W × H | **127.0 × 84.8 × 9.9** | — | — | 127.6 × 84.3 × 9.1 | — | 127.1 × 84.9 × 10.0 | — |
| Well ID bottom / top | **34.7 / 35.5** | 34.80 / 35.43 | 35.0 | 34.6 / 35.0 | 33.9 / 34.5 | 35.0 | 35.0 |
| Well depth | **17.2** | 17.4 | 18 | 17.0 | 17.2 | 17.5 | — |
| Bottom thickness | **1.5** | 1.27 | — | 1.4 | 1.35 | — | — |
| Well pitch X × Y | **39.04 × 39.04** | 39.12 | 39.12 | **40.0 × 38.0** | **37.5 × 37.5** | 39.12 | SBS |
| A1 centre from left / top | **24.71 / 23.17** | 24.76 / 23.16 | — | 23.9 / 23.7 | 24.4 / 24.0 | — | — |
| Well-grid centre vs plate centre | −0.15 / 0 | 0 / 0 | — | 0 / 0 | **−1.95 / 0** | — | — |
| Floor above plate bottom | ~3.0 (20.2 − 17.2) | 3.81 | — | — | 3.1 (+1.35) | — | — |

Sources: CELLTREAT "6 Well Plate" drawing 7/2/20 KG; Corning 3516 specification via Sigma cls3516;
Falcon 353046 listing; Eppendorf TDS "Cell Culture Plate 6-Well"; TPP "TechDoc Test Plate
Measurements" 04/2026; SPL TDS CCP6 v5; Greiner shop 657160 and Thermo 140675 catalogue pages.
PDFs in `refs/`.

## CELLTREAT against the design, item by item

| Design feature | Needs | CELLTREAT | |
|---|---|---|---|
| Pocket 128.6 × 86.2 (0.4 mm/side on the skirt) | footprint ≤ 128.2 × 85.8 | 127.8 ± 0.2 × 85.38 ± 0.2 | ✔ 0.3–0.5 mm clearance per side |
| Lid in the pocket, flipped shot | lid ≤ pocket | 127.0 × 84.8 | ✔ 0.8 / 0.7 mm slop per side, absorbed by the six-ring fit |
| Loading slot 34 mm | height with lid ≤ 31 | 23.0 | ✔ 11 mm spare |
| Pocket rim 6 mm must not lift the lid (lid-up shot) | lid skirt bottom > 6 mm above the plate bottom | plate 20.2, lid 9.9 tall, with-lid 23.0 → lid skirt bottom at 23.0 − 9.9 = 13.1 | ✔ |
| Camera focus, flipped | colony plane = with-lid − floor | 23.0 − 3.0 = **20.0 mm** above the pad | ✔ model rebuilt at 220 mm sensor height |
| Camera focus, lid-up label shot | lid top | 23.0 mm | ✔ 3 mm from the colony plane, DOF ±10 |
| Ring template radius | well ID | 34.7 → r 17.35 | ✔ preset |
| Well map | pitch + grid offset | 39.04, offset −0.15 | ✔ preset; 0.08 mm from SLAS at A3, irrelevant |
| Parallax crescent | well depth | 17.2 | ✔ 3.7 mm at the corner wells, excluded |
| Ink shadow, flipped | ink-to-colony distance | lid top-to-floor 20.0 | ✔ pad-on-lid case, ≤ 10 % smooth |

Nothing in the CELLTREAT drawing is outside the margins the design already had. The two
parameters that actually moved: the flipped colony plane is 20.0 mm (was 19–21 assumed) and the
well ID is 34.7 (was 34.8).

## The other brands, if a plate from a neighbour ever goes in

- **Corning 3516 / Falcon / Greiner / Nunc / SPL**: SLAS grid, wells 34.8–35.0, all fit; pick the
  preset. Corning's floor is 3.81 mm up (2.54 elevation + 1.27 bottom): focus shifts 0.8 mm, inside DOF.
- **Eppendorf**: 40.0 × 38.0 pitch. Preset handles it; without it the finder would still land
  inside the ±3 mm search but fit the wrong scale.
- **TPP 92006**: 37.5 × 37.5 pitch **and** the grid is centred 1.95 mm toward A1 along the long
  axis. Preset carries both (`grid_offset_mm`). Wells are 33.9, the smallest of the set.
- Lids are never larger than the plate (four datasheets agree); the pocket is sized on the skirt.

## Orientation rule for the flipped shot

Load lid-up with A1 rear-left; flip toward you about the long axis, so A1 lands front-left.
`instrument.flip_axis: long` mirrors the well map for the colony shot. A wrong-way-round plate
shows up as a six-ring fit residual > 0.5 mm in the run record, and the label photo tells you which
way it went in.

## Camera drawings (verified from Raspberry Pi mechanical drawings, PDFs in `refs/`)

- **HQ Camera**: 38 × 38 mm PCB, four Ø2.5 holes 4.0 mm from each edge → **30 × 30 mm pattern** as
  modelled; sensor and CS mount centred on the board. The tripod-mount block protrudes **6.5 mm
  behind the PCB** at one edge: the 8 mm standoffs in both variants clear it. ✔
- **Camera Module 3**: 25 × 23.86 mm PCB, four Ø2.2 holes on **21 × 12.5 mm** as modelled; lens
  centred left–right, 10.8 mm from the top edge (≈ 1 mm off the hole-pattern centre, inside the
  Ø12 ceiling hole). ✔
- **8 mm CS lens**: Arducam "CS-Mount 8 mm for HQ camera", 50° HFOV, 1/2.3", Ø28 × 23 mm, ~$20–30.
  At 200 mm it images 152 mm across, the same scale as the 6 mm at 150. ✔

## Nothing left that needs a caliper for CELLTREAT

Every dimension the design touches is on the manufacturer's drawing with a tolerance. The one
thing still worth doing with a real plate is the deck/frame test print, because it also checks the
print's own shrinkage.

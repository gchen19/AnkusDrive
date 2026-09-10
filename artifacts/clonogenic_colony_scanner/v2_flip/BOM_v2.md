# Parts list — variant B rev 3.0, direct-to-Mac (2026-09 prices, US list, rounded)

Raspberry Pi prices rose in 2026 (memory-driven); the 2 GB boards are enough here because the Pi
only captures — your Mac runs the models.

## Buy

| # | Part | Qty | USD | Where / notes |
|---|---|---|---|---|
| 1 | **Raspberry Pi 5, 2 GB** (or Pi 4 Model B 2 GB, $45, if the Pi 5 2 GB is out of stock; both have the one camera port this needs) | 1 | 50 | official resellers: PiShop, Adafruit, CanaKit |
| 2 | Raspberry Pi 27 W USB-C power supply (Pi 4: the 15 W one) | 1 | 12 | |
| 3 | Raspberry Pi Active Cooler (Pi 5) | 1 | 5 | skip for a Pi 4 |
| 4 | microSD 32 GB, A2 class | 1 | 8 | |
| 5 | **Raspberry Pi High Quality Camera** (IMX477, CS mount) | 1 | 70 | official list; Amazon runs higher |
| 6 | **Arducam CS-mount 8 mm lens for the HQ camera, manual focus + adjustable aperture** | 1 | 28 | arducam.com or Amazon (B08GLZFY81). 50° field, 1/2.3" |
| 7 | Camera cable: Pi 5 needs the 22-pin-to-15-pin 300 mm; Pi 4 uses the 15-pin cable in the HQ camera box | 1 | 4 | |
| 8 | **Huion L4S LED light pad, A4** | 1 | 40 | see "Why this pad" |
| 9 | **19 mm stainless anti-vandal momentary pushbutton with RGB ring**, Adafruit **3425** (PM192-11E/42RGB, 6 V ring) | 1 | 10 | Ø19.4 hole, 38 mm behind the panel, IP67; ring = status light |
| 10 | **Waveshare 2inch LCD Module** (ST7789V, 58 × 35 mm, 40.8 × 30.6 active, 8-pin header) | 1 | 14 | in the printed bezel; PCB on four 5 mm bosses, 4 × M2 × 6 |
| 11 | **Perma-Proto HAT** (Adafruit 2310) + **tall 2×20 stacking header** (~19 mm pins) | 1 + 1 | 12 | the panel harness plugs in here |
| 11b | JST-XH 2.5 mm pigtails: 8-way and 5-way, 100 mm, with 2 side-entry sockets (S8B-XH-A, S5B-XH-A); ULN2003 DIP | 1 set | 6 | inside the cassette; the ULN switches the ring |
| 12 | Black **matte** PETG or ASA filament | 0.65 kg | 22 | five parts in rev 2.1 (frame, tower with Pi bay, Pi cover, camera cover, door) |
| 13 | N35 disc magnets Ø6 × 2 mm | 8 | 4 | door 4, Pi sled 4 |
| 14 | M2.5 × 8 screws (camera to bosses) | 4 | 2 | self-tap into Ø2.2 PETG, or heat-set inserts |
| 15 | M2.5 × 8 screws (Pi to the sled bosses) + M2 × 6 (display) | 4 + 4 | 4 | rev 2.2: no cover screws — the sled is held by magnets |
| 16 | Felt dots, 10 mm | 4 | 1 | under the frame corners |
| 17 | Ethernet cable, Cat 5e/6, 1–2 m | 1 | 5 | |
| 18 | USB-C to Gigabit Ethernet adapter for the Mac | 1 | 20 | Anker, UGREEN, Apple ($30); any UVC-free plain adapter |
| 19 | (rev 2.1: no hinge pins — the door snaps onto printed pins) | — | 0 | |

**Total ≈ $315** (rev 2.2: display, ring-lit button, HAT and harness) (≈ $265 if you already own the Ethernet adapter; ≈ $255 with the cheap A5 pad).

Optional, under the $500 ceiling:
- Logic-level MOSFET module ($4) on GPIO 18 to switch the pad's 5 V line, for automatic dark frames.
- Amber 590 nm LED strip in a second cove is *not* applicable here (the pad is the light); an amber
  gel filter sheet (Lee 158, $8) laid on the pad under the frame gives the same crystal-violet
  contrast boost and is trivially removable.
- Pi Touch Display 2 ($60) is unnecessary now: the Mac's browser is the screen.

## Why this pad: Huion L4S

- **Lit area 310 × 210 mm**, so the 176 × 106 mm box sits anywhere on it with the pad's touch
  button and USB lead well clear. The cheap A5 pads have a lit area of about 170 × 108 mm; the
  plate (128 × 85) fits, but the box walls land on the unlit rim and the touch button ends up under
  the frame on some models.
- **1500 lux, stepless dimming, remembers its last setting** when repowered, so the exposure is
  the same every morning. At F5.6 that is about 8 ms per frame.
- 5 mm thick, hard acrylic top (the plate slides on it without scuffing), USB 5 V, no battery,
  no auto-off timer (some A5 pads switch off after 30 min, which would ruin a batch).
- Widely stocked (Amazon B00J0UUHPO / B00J3NRAV2, B&H), reviewed for evenness, about $40.

If you would rather have the smallest footprint: any USB A5 pad without an auto-off timer
(e.g. Amazon B091BFMQY3, ~$15), and rotate the box so its long side runs along the pad's long
side with the button at the far end.

## Already in the lab / free

CELLTREAT 229105 plates and one empty one for calibration; a Mac with a browser; a 3D printer or a
print service (three parts, ~11 h).

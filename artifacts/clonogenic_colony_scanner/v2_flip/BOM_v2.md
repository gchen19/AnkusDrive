# Parts list — variant B rev 3.1, direct-to-Mac (prices checked on the linked pages, 2026-09-09)

The Pi only captures; your Mac runs the models, so the 2 GB boards are enough. Every link below was
opened and the price read off the page that day; "≈" marks a price the page did not show in text.

## Cart 1 — PiShop.us (official reseller), ≈ $172

| # | Part | Qty | USD | Link |
|---|---|---|---|---|
| 1 | **Raspberry Pi 5, 2 GB** | 1 | 65.00 | https://www.pishop.us/product/raspberry-pi-5-2gb/ |
| 1b | *or* Raspberry Pi 4 Model B, 2 GB (fits the same cassette; then skip the cooler, use the 15 W supply and the 15-pin cable in the camera box) | 1 | 55.00 | https://www.pishop.us/product/raspberry-pi-4-model-b-2gb/ |
| 2 | Raspberry Pi 27 W USB-C power supply, white, US | 1 | 12.95 | https://www.pishop.us/product/raspberry-pi-27w-usb-c-power-supply-white-us/ |
| 3 | Raspberry Pi Active Cooler (Pi 5) | 1 | 10.95 | https://www.pishop.us/product/raspberry-pi-active-cooler/ |
| 4 | **Raspberry Pi HQ Camera, CS mount** (IMX477; box includes the C-CS adapter and a 200 mm 15-pin cable) | 1 | 55.00 | https://www.pishop.us/product/raspberry-pi-hq-camera-cs/ |
| 5 | Camera cable for Raspberry Pi 5, 22-to-15 pin, choose **300 mm** | 1 | 3.95 | https://www.pishop.us/product/camera-cable-for-raspberry-pi-5/ |
| 6 | Official Raspberry Pi microSD, 32 GB, A2, blank | 1 | ≈ 10 | https://www.pishop.us/product/raspberry-pi-sd-card-32gb/ |
| 7 | **Waveshare 2inch IPS LCD Module, 240 × 320, ST7789** (SKU 1746; comes with its PH2.0 8-pin 20 cm cable to Dupont females) | 1 | 13.95 | https://www.pishop.us/product/240-320-general-2inch-ips-lcd-display-module/ |

## Cart 2 — Adafruit, ≈ $31

| # | Part | Qty | USD | Link |
|---|---|---|---|---|
| 8 | **ChromaTek 19 mm rugged momentary metal pushbutton with NeoPixel ring, 19-B-M-F1** (Adafruit 3425, the 6 V RGB one this design started with, is discontinued; the 16 mm RGB, 3350, is out of stock) | 1 | 19.95 | https://www.adafruit.com/product/5236 |
| 9 | Adafruit Perma-Proto HAT for Pi Mini Kit, no EEPROM | 1 | 4.95 | https://www.adafruit.com/product/2310 |
| 10 | Stacking header 2×20, extra tall (23 mm body), clears the Active Cooler | 1 | ≈ 3 | https://www.adafruit.com/product/1979 |
| 11 | Right-angle 2.54 mm male pin header strip (break off 8 and 7 pins) | 1 | ≈ 2 | any; Adafruit 1540 or an Amazon strip |

## Cart 3 — Amazon and the print service, ≈ $190

| # | Part | Qty | USD | Link |
|---|---|---|---|---|
| 12 | **Arducam CS-mount 8 mm lens for the HQ camera, manual focus and adjustable aperture** | 1 | ≈ 28 | https://www.amazon.com/dp/B08GLZFY81 (maker page: https://www.arducam.com/arducam-cs-mount-lens-for-raspberry-pi-hq-camera-8mm-focal-length-with-manual-focus-and-adjustable-aperture.html) |
| 13 | **Huion L4S LED light pad, A4** (5 mm thick, USB, stepless brightness that it remembers) | 1 | ≈ 35 | https://www.amazon.com/dp/B00J3NRAV2 |
| 14 | Anker USB-C to Gigabit Ethernet adapter (for the Mac) | 1 | ≈ 20 | https://www.amazon.com/dp/B00ZZ6NW5E |
| 15 | N35 disc magnets Ø6 × 2 mm (door 4, cassette 4) | 8 | ≈ 5 | search "6x2mm neodymium magnets" |
| 16 | M2 × 6 and M2.5 × 8 screws (display 4, Pi 4, camera 4) | 12 | ≈ 6 | search "M2 M2.5 screw assortment" |
| 17 | Felt dots 10 mm | 4 | ≈ 3 | search "felt pads 10mm self adhesive" |
| 18 | Cat 6 Ethernet cable, 1 to 2 m | 1 | ≈ 5 | any |
| 19 | Five printed parts, PETG black, Craftcloud (Corvallis3D) | 1 | ≈ 85 | https://craftcloud3d.com/en/upload with `v21/frame.stl tower.stl pi_sled.stl cam_cover.stl door.stl` |

**Parts ≈ $310, whole build ≈ $395.** Nothing else: the ChromaTek button brings its own 7-wire harness, the
display its own cable, so there are no pigtails, no ULN2003 and no resistors on the HAT any more.

## Not this
- **Waveshare Pico-LCD-2**: the same 2-inch 320 × 240 ST7789 panel, but built as a hat for a Raspberry Pi
  Pico: two 20-pin female headers on the back, four corner buttons, a 52 × 35 mm outline with different
  mounting holes and no cable. It would need the Pico's pins wired by hand and a new bezel. The
  "2inch LCD Module" above is the one the cassette face is drawn for.

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

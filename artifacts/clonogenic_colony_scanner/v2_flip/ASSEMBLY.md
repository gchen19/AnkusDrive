# Assembly — rev 4.0 (2026-09-10)

Three prints, no soldering, about ninety minutes. Tools: a small Phillips and hex driver, flush cutters, superglue
for the magnets. The tower is the one part that needs care in printing; everything else is plug and screw.

## 1. Print (or order) three parts

| File | Orientation | Supports | Time |
|---|---|---|---|
| `frame.stl` (plinth) | floor down | none | ~4 h |
| `tower.stl` | **upside down**: the flat ceiling on the bed, open end up; the display window, port cutout and vent slots bridge | none | ~12 h |
| `door.stl` | flat on its outer face | none | ~1 h |

Black PETG or ASA, 0.2 mm layers, 3 walls, 20% infill. Matte black if offered.

## 2. Camera and Pi under the ceiling

1. Screw the 8 mm lens into the HQ camera. Aperture ring to F5.6, focus ring mid-travel; leave the C-CS adapter ring off.
2. Stand the tower upside down. Drop the camera board onto the four central bosses, lens pointing up (which is down
   once the tower is upright), FPC connector toward the back wall. Four M2.5 × 8.
3. The Pi goes on the four bosses to the right, component side toward you (down once upright), Ethernet and USB
   stacks into the back-wall cutout, USB-C port toward the right-wall slot. Four M2.5 × 8. Fit the Active Cooler first.
4. Plug the 22-to-15 pin camera cable into the camera (blue side per the camera's marking) and into the Pi's
   CAM/DISP 1 connector; lay it along the ceiling and down the gap at the right wall.

## 3. Button on the roof

5. Push the 16 mm switch through the roof hole (front-left, beside the camera) from outside, O-ring under the
   bezel, nut inside. Two 20 cm Dupont female-to-bare wires on its screw terminals; the female ends go onto the Pi's
   header pins 11 (GPIO 17) and 9 (GND). Route them around the lens.

## 4. Display in the front wall

6. From inside, hold the Waveshare module against the front wall with its glass in the window and screw it to the
   four bosses with M2 × 6. Its own 8-way cable plugs onto the module's header; the Dupont ends go onto the Pi:
   3V3 (pin 1), GND (6), MOSI (19), SCLK (23), CE0 (24), DC (22), RST (18), BL (12). Run the cable up past the lens.

## 5. Door and plinth

7. Two Ø6 × 2 magnets in the door's lower corners, two in the plinth's front face, polarity checked so they attract.
8. Hold the door at 45°, align its pins with the C-slots in the plinth lugs, push back until it clicks. It drops shut
   on the magnets, lifts by its lip, and parks open against the tower when lifted past vertical.

## 6. Stack

9. Tower onto the plinth: the tongue drops into the groove, display and door both at the front.
10. Onto the light pad with felt dots under the plinth's corners, clear of the pad's touch switch. Plug the 27 W
    supply into the right-wall slot, Ethernet and the pad's lead into the back-wall cutout. The pad's lead runs
    round to its port on the pad's left edge. One power cable for everything.

## Does it fit? Clearances and the order of assembly

Measured in the CAD (`build_scanner_v4.py` computes them): the tube's open end is 171 × 102 mm and it is 178 mm
deep to the ceiling. Under the ceiling the Pi's PCB edge is 3.0 mm from the camera's PCB edge, its cooler 9.7 mm
from the lens body, its USB-C edge 6.1 mm from the right wall (the plug's shell reaches through that gap and the
2.4 mm wall), and the Dupont housings on its header end 12 mm above the lens front. The display module is 16 mm
from the Pi and 14 mm from the button body. Everything is reached from the open end with the tower upside down,
and every screw is driven straight down except the four display screws, which sit 126 to 156 mm in from the
open end and want a driver at least 150 mm long.

Two connectors cannot be reached once their board is mounted, so the order matters:
1. Display first, while the tube is empty.
2. Plug the camera cable into the camera on the bench, then screw the camera to the ceiling.
3. Plug the cable's other end into the Pi's CAM/DISP 1 on the bench (the Pi dangles within the 200 mm of cable),
   lay the cable along the ceiling, lower the Pi onto its bosses, screw it down.
4. Button through the roof, then the Dupont ends onto the header from below: they slide straight down onto pins
   that point up at you.

## 7. Software

11. Raspberry Pi OS 64-bit on the card (preloaded card, or Raspberry Pi Imager: hostname `scanner`, user `pi`, SSH on).
    `apt install python3-picamera2 python3-gpiozero`, `pip install luma.lcd pillow numpy scipy scikit-image pyyaml`,
    copy `scanner_sw/`, enable `scanner.service`. Fixed address 10.42.0.2 on the Ethernet port; the Mac's adapter at 10.42.0.1.
12. Focus: tower on blocks over a flipped stained plate, hold the button at READY, turn the lens ring until the bar
    peaks, lock its screw, tap. Then `capture.py --flat` with an empty plate, unplug the pad, `capture.py --dark`.

# Rev 3.3 — a plain button (2026-09-10)

"The RGB button seems a bit complex; feedback all happens on the screen anyway." So the button is now a plain
stainless 16 mm momentary switch (Ulincos U16A1S, or the $0.95 Adafruit 1505), two wires to GPIO 17 and ground,
no lamp, no NeoPixel, no SPI1, no driver. The colour bar across the top of the 2-inch screen is the status
light: it blinks amber for FLIP, pulses purple for DONE and follows sharpness in FOCUS. Hole Ø16.2 on the
cassette face; the button is 20 mm deep, so the sled plate no longer needs a clearance hole.

# Rev 3.2 — everything on one face, and a plinth that cannot be rocked (2026-09-10)

The Pi bay and its control cassette moved from the back wall to the **front wall, directly above the
door**: slot at the bottom, screen and button above it, nothing to walk round. The door now lifts past
vertical and parks against the tower under the cassette, so both hands are free for the plate. The
power cable uses a right-angle USB-C plug and runs under the sled foot and out of the side window with
the Ethernet and the pad lead; nothing hangs in front of the slot.

The frame became a **flared plinth**: sides and back taper out 30 and 40 mm to the floor, two chamfered
toes reach forward under the cassette, 3 mm skin, 2 mm floor. The build now estimates mass (≈ 0.88 kg),
centre of mass and the push at button height that would tip the box about each edge (`_stability` in
`parts.json`): about 6 N toward the back or sides, 4 N toward the front, against a button that needs
about 2 to 3 N. Nothing is taped to the light pad.

Layout is done by mirroring: the cassette bodies and their cable routes are built in the rev 3.0
rear-bay coordinates and reflected with `mY()` (y' = Y_FRONT + Y_REAR − y), so every dimension of the
cassette, HAT and looms is unchanged. Camera board turned 180° so its FPC connector faces the bay;
cover slot on the front.

# Rev 3.1 — real cables, focus mode, and three bugs the cables found (2026-09-09)

Every flexible run is now a named **route** in `cables.json`, written by `build_scanner_v21.py`
(waypoints, bend radius, conductor colours). The CAD sweeps each route into a smooth solid
(`sweep_round` in `reference_parts.py`: cylinders on the straights, revolved bends), Blender
rebuilds it as bevelled curves with parallel-transport frames (`cables_blender.py`: rainbow looms with
heat-shrink, tapered FPC strip, USB-C, Cat 6, the pad lead) and the explorer builds the same geometry in
Three.js. Rigid connector bodies are the new `plugs` part. Routing the wires properly exposed and fixed:
the HAT's sockets 0.7 mm into the control face (bay now 55 mm deep, side-entry JST-XH sockets); the Pi
model had the Pi 4 port order and the bay window only covered one USB stack (Pi 5 order now, window spans
the whole port edge so either Pi fits); the pigtails ran through the 40-pin header (8-way down the left
channel, 5-way down the right in front of the USB stacks). The interference check now includes cables.

Software: **focus mode** — hold the button at READY and the display shows a live sharpness meter with a
peak marker; the ring's brightness follows the score and turns green at the peak (`capture.py
focus_score`, `runflow.py focus_mode`, `ui.py`); `capture.py --focus` prints it over SSH.

Renders (`render_blender.py`, 13 views) need the CC0 Poly Haven HDRIs in `env/` (`studio_small_09_2k.hdr`,
not in git: download from polyhaven.com). `renders/*.png` are not in git either; `renders_jpg/` is.

# Rev 3.0 — control cassette at the back (2026-09-09)

The button and display moved onto the Pi sled's outer plate. Pi, HAT, display, button and their
8 cm of wiring are one cassette; the tube has no ducts, no pod and no holes except the roof slot for
the camera ribbon. The build now also checks every body against the camera's view cone over the
plate (the rev 2.2 front-panel button would have shadowed well B3; that is how it was caught). You
use the box with the cassette face toward you and the loading slot on your left.

# Rev 2.3 — full-spec pass: every bought part to its datasheet, cables routed, photoreal renders

What changed from rev 2.0, and why:

| Loose end in 2.0 | Rev 2.1 |
|---|---|
| Pi bolted bare to the tower's back wall, cables in the open | **Pi bay** printed as part of the tower: a pocket with four M2.5 bosses on the Pi 5 pattern, open at the bottom (USB-C power and HDMI drop straight to the bench), a side window for USB and Ethernet, a slot in its roof for the camera ribbon, and a **vented cover** on four M3 screws |
| HQ camera board exposed on the top plate | **Camera cover** on four pegs, ribbon slot toward the rear |
| Door hinge on two loose filament pins | **Snap-fit hinge**: integral Ø3 pins on the door's ears, C-slots in the frame lugs; push the door in from the front and it clicks |
| No orientation cue | Small raised triangle on the frame rim, rear-left = A1 |

Rev 2.2 on top of 2.1:

| | Rev 2.2 |
|---|---|
| Arcade button + bare LED | **19 mm stainless anti-vandal pushbutton with an RGB ring** (the ring is the status light) and a **2-inch 320 × 240 IPS display** in a printed bezel that says what to do next and shows the six counts |
| Pi screwed into the bay, screwed cover | **Pi sled**: front plate with the Pi bosses and two rails, a foot notched under the plugs, a vented rear plate that is the cover; slides up two grooves, held by two magnets in the roof; thumb tab to pull |
| Loose panel wires | **Printed ducts** inside the tube (front wall → under the top plate → rear wall → bay window) carry one JST-XH harness to a **Perma-Proto HAT** on the Pi; nothing soldered to the Pi |
| Finger hole in the door | **Solid pull lip**; no opening, no light path |
| Two supplies | The pad's USB lead plugs into the Pi: **one power cable** |

Rev 2.3 on top of 2.2: `reference_parts.py` models every bought part to its datasheet (Pi 5 with
USB-C, two micro-HDMI, two USB stacks, RJ45, 40-pin header, Active Cooler; Perma-Proto HAT on a tall
header with the two JST sockets and the ULN2003; HQ camera board with CS mount, tripod block and FPC
connector; Arducam 8 mm lens; Waveshare 2-inch module with glass, PCB, header; the PM192 button with
bezel, M19 body, hex nut and seven lugs; Huion L4S with its lit area, touch switch and port; the
CELLTREAT plate with hollow skirt, condensation rings and cut corner), every cable as a routed path
(camera ribbon 15 mm flat, JST harness, USB-C, Cat 6 with RJ45, the pad's USB lead) and every screw.
The build reports **zero interference across all 21 bodies**. Renders use a studio HDRI, layer-line
bump and bevelled edges on the prints, real glass on the plate, depth of field on the close-ups.

Printed parts are five: `frame`, `tower` (with bay, bezel and ducts), `pi_sled`, `cam_cover`, `door`.
Extra hardware over the rev 2.0 list: the button, the display, a Perma-Proto HAT with a tall
header, two JST-XH pigtails, four more magnets, 4 × M2 × 6 (display). Print time about +2 h. Everything else in `../BOM_v2.md` and `../ASSEMBLY.md` still
applies; where they say "Pi on standoffs on the back wall", read "Pi on the four bosses inside
the bay, cover on last".

Files: `build_scanner_v21.py` (AnkusDrive source), one STL per part (printed parts plus every
bought part as a reference body), `cut_*.stl` (section halves), `colony_scanner_v21_assembly.step`,
`colony_scanner_v21_printed_parts.step`, `parts.json` (bounding boxes, hinge axis, flip centre),
`render_blender.py` (Cycles renders → `renders/`), `viewer_template.html` + `build_viewer.py`
(the interactive explorer).

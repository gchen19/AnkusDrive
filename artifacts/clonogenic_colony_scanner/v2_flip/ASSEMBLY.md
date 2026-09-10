# Assembly — variant B (one camera, flip the plate, light pad floor)

> **Rev 3 (current):** five printed parts — frame, tower (blank walls, Pi bay at the back), Pi sled,
> camera cover, door. The sled's outer plate is the control face: the 2-inch display and the 19 mm
> ring-lit button mount on it, so Pi, HAT, display, button and all their wiring are one cassette that
> slides up into the bay and is held by magnets. No ducts, no holes in the tube. You use the box with
> the cassette face toward you and the loading slot on your left.

Time: about 2 hours of hands-on work once the prints are done. Tools: hex/Phillips drivers,
flush cutters, a 2.5 mm drill bit or a 1.75 mm filament offcut, superglue, calipers.

## 0. Print (about 11 h total, black matte PETG or ASA, 0.2 mm layers, 3 walls, 20 % infill)

| Part | Orientation on the bed | Supports | Time |
|---|---|---|---|
| `frame.stl` | as modelled, floor ring down | none (lug wedges are 45°) | ~2 h |
| `tower.stl` | **upside down**: camera plate on the bed, open end up; the Pi bay's roof slot and side window bridge fine | none | ~7.5 h |
| `pi_sled.stl` | rear (vented) plate down, front plate up; the foot bridges 26 mm, fine | none | ~55 min |
| `cam_cover.stl` | open side down, pegs up | none | ~30 min |
| `door.stl` | flat on its outer face (the two pins lie on the bed) | none | ~40 min |

Matte black matters for the tower interior: glossy black still reflects the pad. If you only have
glossy, line the inside of the tower with black flock paper or black construction paper later.

**Fit check before anything else:** put a CELLTREAT plate lid-up in the frame's pocket. It should
drop in flat with a hair of side play and stop against the rear wall. Flip it lid-down; the lid should
sit in the same pocket. If either binds, your printer is over-extruding: scale X/Y by 100.3 % in the
slicer and reprint the frame only.

## 1. Camera on the tower

1. Screw the **8 mm CS lens** into the HQ camera. Leave the C-CS adapter ring *off* (it is only for
   C-mount lenses). Set the aperture ring to about F5.6, focus ring mid-travel.
2. Set the tower upright (camera plate up). Drop the lens down through the Ø34 hole and seat the
   camera board on the four bosses, ribbon connector toward the BACK (the bay side); the tripod
   block on the board's edge hangs down beside the hole and clears the plate.
3. Four **M2.5 × 8** screws through the board into the bosses. The Ø2.2 holes are self-tapping in
   PETG; go gently, do not strip. (Or heat-set M2.5 inserts if you prefer.)
4. Plug the **camera ribbon** into the board: contacts facing the board, latch closed. Route it over
   the edge of the tower and down the *back* wall; tape it every 50 mm.

## 2. The cassette face: button and display

5. **Button** (Adafruit 3425, datasheet PM192-11E/42RGB): drop it through the Ø19.4 hole in the
   sled's outer plate from the outside, seal ring under the bezel, hex nut on the inside (0.8 N·m
   max). Its 38 mm body points into the bay above the Pi. Seven solder lugs: C1 and NO1 are the
   switch (GPIO 17 and GND); C+ is the ring's common anode (5 V); Red, Green, Blue are the ring
   cathodes, switched to ground by a ULN2003 on the HAT (inputs from GPIO 22, 27, 23). The 6 V ring
   has its resistors built in.
6. **Display**: seat the 2-inch module against the inside of the sled's outer plate, glass to the
   window, four M2 × 6 screws into the bosses.

## 2b. Wiring: 8 cm, inside the cassette

7. **HAT.** Solder two right-angle 2.54 mm pin headers to the Perma-Proto HAT: an 8-pin one whose
   pins point toward the Pi's left edge (the display's own PH2.0-to-Dupont cable plugs onto it) and a
   7-pin one pointing toward the right edge (the ChromaTek button's harness). Wire the 8-pin to 3V3,
   GND, MOSI GPIO 10, SCLK GPIO 11, CE0 GPIO 8, DC GPIO 25, RST GPIO 24, BL GPIO 18. Wire the 7-pin:
   NeoPixel +5V → 5 V, GND → GND, DIN → GPIO 20 (SPI1 MOSI), switch C → GPIO 17, NO → GND; NC and
   DOUT unused. Put the 23 mm stacking header under the HAT. Add `dtoverlay=spi1-1cs` to
   `/boot/firmware/config.txt`. Nothing else goes on the HAT.
8. The display's cable runs from its header down the channel beside the Pi's left edge and across
   the HAT; the button's harness down the right side in front of the USB stacks. Both stay inside the
   cassette.
9. The light pad's USB lead plugs into one of the Pi's USB ports in the side window: **one power
   cable** (the Pi's 27 W supply) runs the whole instrument.

## 3. Pi on its sled

11. Glue two **Ø6 × 2 magnets** into the sled's top blocks and two into the pockets in the bay roof,
    polarity checked so they attract.
12. Fit the **Active Cooler** to the Pi, then the HAT on its tall header. Screw the Pi to the sled's
    four bosses (four **M2.5 × 8**), components facing the vented rear plate, USB-C/HDMI edge DOWN
    over the foot's notch, USB/Ethernet stack toward the +X side (the bay's side window spans the whole port edge, so a Pi 4 or a Pi 5 lines up).
13. With the sled still out: camera ribbon (hanging through the roof slot) into CAM/DISP 0; the two
    pigtails into the HAT sockets. The cassette now carries Pi, HAT, display and button.
14. Slide the sled up the two grooves until the magnets click. USB-C power into the port over the
    foot notch; Ethernet and the pad's USB lead through the side window. To service: pull the thumb tab.
10. Button to GPIO 17 and GND; LED to GPIO 27 and GND. Two Dupont pairs. GPIO 18 stays free
    (it can switch the pad through a MOSFET later if you ever want automatic dark frames).

## 4. Door and camera cover

15. Two **Ø6 × 2 mm magnets** into the pockets in the frame's front face, two into the door, a drop
    of superglue each. Check polarity before gluing: they must attract with the door hung.
16. Hang the door: hold it at about 45°, line its two pins up with the C-slots in the frame lugs,
    and push straight back until both pins click into the round seats. It swings freely, drops
    shut, and the magnets hold it; lift it by the solid lip along its lower edge. To remove, pull it
    forward at the same angle.
17. Set the lens aperture to F5.6, then press the **camera cover** onto its four pegs with the
    ribbon slot facing the back.

## 5. Stack it

13. Tower onto the frame: the tongue drops into the groove, the slot and door at the front. Two
    strips of black tape over the seam if you want it fully light-tight (it is not critical; the pad
    outshines the room a thousandfold).
14. Set the whole box on the **light pad**, centred, the pad's touch button and cable clear of the box.
    Four small felt dots under the frame corners stop it sliding.

## 6. Cables and first light

15. Pad USB to any 5 V USB socket (the Pi's own USB port is fine). Turn it on, brightness to max;
    it remembers the setting.
16. Pi power supply to the wall. Ethernet from the Pi to your Mac's USB-C adapter.
17. Focus without a laptop: with the tower on blocks and a stained plate flipped on the pad, **hold the
    button at READY**. The screen becomes a sharpness meter and the ring's brightness follows it (green
    within 3% of the best value seen). Reach up through the tower's open bottom, turn the lens focus ring
    until the bar peaks, lock the ring's set screw, tap to leave. `capture.py --focus` prints the same
    meter over SSH if you prefer a terminal.
18. Calibrate once (empty CELLTREAT plate, lid on, no writing): `python3 capture.py --flat`, then
    `python3 calibrate.py /home/pi/scans/_calibration/flat`, then unplug the pad and
    `python3 capture.py --dark`, plug it back in.
19. `sudo systemctl enable --now scanner`. Press the button. Done.

## Loading rule (stick a label on the box)

Lid up, A1 rear-left, push to the stop, press. When the light blinks: pull out, flip toward you about
the long side (lid now down), push back, press.

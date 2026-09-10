"""
Clonogenic plate scanner, variant B rev 3.0 (control cassette at the back) — "all in one" + front panel (19 mm ring-lit button, 2" display): the loose ends of rev 2.0 resolved.
  * Pi bay integrated into the tower's rear wall: the Pi 5 sits on four bosses inside a printed
    pocket, ports facing DOWN (power/HDMI) and +X (USB/Ethernet window); a vented cover closes it.
  * Camera cover over the HQ camera on the top plate, ribbon slot toward the rear, four pegs.
  * Snap-fit door hinge: the door's ears carry integral pins; the frame lugs have C-slots.
  * Reference bodies for everything bought: pad, plate (lid-up; flip = rotate 180 deg about X),
    camera board + lens, Pi + cooler, button, LED, ribbon, cables, magnets.
Runs inside the AnkusDrive worker: exec(open(path).read(), globals()).
Frame: X along the plate's long axis, Y short axis (front = -Y), Z up, pad surface Z = 0.
"""
import json
import FreeCAD as App
import Part, MeshPart
from FreeCAD import Vector as V

if App.ActiveDocument is not None and App.ActiveDocument.Name.startswith("colony_scanner"):
    App.closeDocument(App.ActiveDocument.Name)
doc = App.newDocument("colony_scanner_v21")
OUT = "/home/user/dev/ankusdrive/artifacts/clonogenic_colony_scanner/v2_flip/v21/"

# ---- inputs (CELLTREAT 229105 drawing; Raspberry Pi drawings; Huion L4S; Arducam 8 mm)
PLATE_L, PLATE_W, PLATE_H = 127.8, 85.38, 23.0
LID_L, LID_W, LID_H = 127.0, 84.8, 9.9
WELL_ID, WELL_PITCH, WELL_DEPTH = 34.7, 39.04, 17.2
CLR = 0.4
POCKET_X, POCKET_Y = PLATE_L + 2 * CLR, PLATE_W + 2 * CLR
WALL = 3.0
FOOT_X = 176.0
Y_FRONT = -(POCKET_Y / 2 + 1.3 + WALL)     # -47.95
Y_REAR = 59.0
IN_X = FOOT_X - 2 * WALL
FRAME_H, FLOOR_T, RIM_H = 50.0, 2.0, 6.0
SLOT_HALF_X, SLOT_Z1 = 75.0, 34.0
# rev 3.2: the Pi bay and control face sit on the FRONT wall above the door, so everything faces the user;
# the frame is a tapered plinth (flared sides and back, low toes under the bay) so a button press cannot rock it.
FLARE_X, FLARE_Y, TOE_LEN, TOE_H = 30.0, 40.0, 62.0, 6.0
YM = Y_FRONT + Y_REAR                      # mirror constant: y' = YM - y moves a rear-bay body to the front
def mY(shape): return shape.mirror(V(0, YM / 2, 0), V(0, 1, 0))
SENSOR_Z = 220.0                          # 200 mm above the flipped colony plane (Z ~ 20)
TOP_T, CAM_STANDOFF = 3.0, 8.0
TOWER_Z1 = SENSOR_Z - 1.6 - CAM_STANDOFF  # 210.4
LENS_HOLE_R = 17.0
g_i, g_o, g_d = 1.0, 3.0, 2.0
HINGE_Z = 44.0
# Pi bay (exterior) on the rear wall
BAY_X, BAY_Y0, BAY_Y1, BAY_Z0, BAY_Z1 = 52.0, Y_REAR, Y_REAR + 55.0, 92.0, 202.0   # 55 deep: 2.3 mm over the HAT sockets
BAY_WALL = 3.0
PI_STANDOFF = 6.0
PI_X0, PI_Z0 = -42.5, 100.0               # Pi board lower-left corner (board 85 x 56 in X,Z)

def box(x0, y0, z0, x1, y1, z1): return Part.makeBox(x1 - x0, y1 - y0, z1 - z0, V(x0, y0, z0))
def cyl(r, h, x, y, z, axis=V(0, 0, 1)): return Part.makeCylinder(r, h, V(x, y, z), axis)
def wedge_x(x0, x1, y_wall, z_bottom, out, up):
    """45deg support wedge under a lug on a vertical wall at y_wall, growing -Y by `out`."""
    f = Part.Face(Part.makePolygon([V(0, y_wall, z_bottom - up), V(0, y_wall - out, z_bottom), V(0, y_wall, z_bottom), V(0, y_wall, z_bottom - up)]))
    w = f.extrude(V(x1 - x0, 0, 0)); w.translate(V(x0, 0, 0)); return w

# ------------------------------------------------------------------ frame
def rect(x0, y0, x1, y1, z): return Part.makePolygon([V(x0, y0, z), V(x1, y0, z), V(x1, y1, z), V(x0, y1, z), V(x0, y0, z)])
frame = Part.makeLoft([rect(-FOOT_X/2 - FLARE_X, Y_FRONT, FOOT_X/2 + FLARE_X, Y_REAR + FLARE_Y, 0),
                       rect(-FOOT_X/2, Y_FRONT, FOOT_X/2, Y_REAR, FRAME_H)], True, True)                       # plinth: vertical front, flared sides and back
frame = frame.cut(Part.makeLoft([rect(-FOOT_X/2 - FLARE_X + 3.5, Y_FRONT + WALL, FOOT_X/2 + FLARE_X - 3.5, Y_REAR + FLARE_Y - 3.5, FLOOR_T),
                                 rect(-IN_X/2, Y_FRONT + WALL, IN_X/2, Y_REAR - WALL, FRAME_H + 1)], True, True))   # hollow, 3 mm skin, 2 mm floor
for s_ in (1, -1):                                                                                           # toes under the bay, chamfered
    toe = Part.Face(Part.makePolygon([V(0, Y_FRONT, 0), V(0, Y_FRONT - TOE_LEN, 0), V(0, Y_FRONT - TOE_LEN, 2), V(0, Y_FRONT - TOE_LEN + 6, TOE_H), V(0, Y_FRONT, TOE_H), V(0, Y_FRONT, 0)])).extrude(V(FOOT_X/2 + FLARE_X - 82, 0, 0))
    toe.translate(V(82 if s_ > 0 else -(FOOT_X/2 + FLARE_X), 0, 0))
    frame = frame.fuse(toe)
frame = frame.cut(box(-POCKET_X/2, Y_FRONT - 1, -1, POCKET_X/2, POCKET_Y/2, FLOOR_T + 1))
rim = box(-POCKET_X/2 - 2.5, Y_FRONT + WALL, 0, POCKET_X/2 + 2.5, POCKET_Y/2 + 2.5, RIM_H)
rim = rim.cut(box(-POCKET_X/2, Y_FRONT - 1, -1, POCKET_X/2, POCKET_Y/2, RIM_H + 1))
frame = frame.fuse(rim)
frame = frame.cut(box(-SLOT_HALF_X, Y_FRONT - 1, -1, SLOT_HALF_X, Y_FRONT + WALL + 1, SLOT_Z1))
for s in (1, -1):
    tri = Part.makePolygon([V(s*POCKET_X/2, Y_FRONT + WALL, -1), V(s*(POCKET_X/2 + 3), Y_FRONT + WALL, -1),
                            V(s*POCKET_X/2, Y_FRONT + WALL + 3, -1), V(s*POCKET_X/2, Y_FRONT + WALL, -1)])
    frame = frame.cut(Part.Face(tri).extrude(V(0, 0, RIM_H + 2)))
groove = box(-IN_X/2 + g_i, Y_FRONT + WALL + g_i, FRAME_H - g_d, IN_X/2 - g_i, Y_REAR - WALL - g_i, FRAME_H + 1)
groove = groove.cut(box(-IN_X/2 + g_o, Y_FRONT + WALL + g_o, FRAME_H - g_d - 1, IN_X/2 - g_o, Y_REAR - WALL - g_o, FRAME_H + 2))
frame = frame.cut(groove)
# snap-fit hinge lugs: C-slot opening toward the front, pin hole d3.3 at (Y_FRONT-2, HINGE_Z)
for s in (1, -1):
    x0, x1 = min(s*80.5, s*85), max(s*80.5, s*85)
    frame = frame.fuse(box(x0, Y_FRONT - 5.5, 40, x1, Y_FRONT, 48)).fuse(wedge_x(x0, x1, Y_FRONT, 40, 5.5, 5.5))
    frame = frame.cut(cyl(1.65, 5, x0 - 0.2, Y_FRONT - 2.0, HINGE_Z, V(1, 0, 0)))
    frame = frame.cut(box(x0 - 0.2, Y_FRONT - 7, HINGE_Z - 1.2, x1 + 0.2, Y_FRONT - 2.0, HINGE_Z + 1.2))   # C-slot, 2.4 wide
    frame = frame.cut(cyl(3.1, 2.0, s*77.5, Y_FRONT - 0.01, 20, V(0, 1, 0)))                          # magnet pocket
# A1 orientation cue: small raised triangle on the rim, rear-left
frame = frame.fuse(Part.Face(Part.makePolygon([V(-58, 44, RIM_H), V(-50, 44, RIM_H), V(-54, 48, RIM_H), V(-58, 44, RIM_H)])).extrude(V(0, 0, 1.0)))
frame = frame.removeSplitter()

# ------------------------------------------------------------------ tower with Pi bay
tower = box(-FOOT_X/2, Y_FRONT, FRAME_H, FOOT_X/2, Y_REAR, TOWER_Z1)
tower = tower.cut(box(-IN_X/2, Y_FRONT + WALL, FRAME_H - 1, IN_X/2, Y_REAR - WALL, TOWER_Z1 - TOP_T))
tongue = box(-IN_X/2 + g_i + 0.3, Y_FRONT + WALL + g_i + 0.3, FRAME_H - g_d + 0.4, IN_X/2 - g_i - 0.3, Y_REAR - WALL - g_i - 0.3, FRAME_H)
tongue = tongue.cut(box(-IN_X/2 + g_o - 0.3, Y_FRONT + WALL + g_o - 0.3, FRAME_H - g_d - 1, IN_X/2 - g_o + 0.3, Y_REAR - WALL - g_o + 0.3, FRAME_H + 1))
tower = tower.fuse(tongue)
tower = tower.cut(cyl(LENS_HOLE_R, TOP_T + 2, 0, 0, TOWER_Z1 - TOP_T - 1))
for sx in (-15, 15):
    for sy in (-15, 15):
        tower = tower.fuse(cyl(3.0, CAM_STANDOFF, sx, sy, TOWER_Z1)).cut(cyl(1.1, CAM_STANDOFF - 1, sx, sy, TOWER_Z1 + 1))
for sx in (-22, 22):                                                              # camera-cover peg holes
    for sy in (-22, 22):
        tower = tower.cut(cyl(1.6, 4, sx, sy, TOWER_Z1 - 4 + 0.01))
# --- rev 3: blank front wall; the button and display live on the Pi cassette at the back.
DSP_W, DSP_H = 42.0, 32.0

# Pi bay (rev 2.2): a hood open at the bottom and the rear; the Pi rides on a SLED that slides up
# into it on two grooves, stops on the roof, and is held by two magnets. The sled's rear plate is the
# vented cover, so there are no cover screws.
SLED_T, SLED_Y0 = 2.5, BAY_Y0 + 3.5           # front plate thickness, its front face Y
GROOVE_Y0, GROOVE_Y1, GROOVE_D = SLED_Y0 - 0.5, SLED_Y0 + SLED_T + 0.5, 1.6
SLED_Z0, SLED_Z1 = BAY_Z0 + 0.5, BAY_Z1 - BAY_WALL - 0.3     # 92.5 .. 160.7
bay = box(-BAY_X, BAY_Y0, BAY_Z0, BAY_X, BAY_Y1, BAY_Z1)
bay = bay.cut(box(-BAY_X + BAY_WALL, BAY_Y0 - 1, BAY_Z0 - 1, BAY_X - BAY_WALL, BAY_Y1 + 1, BAY_Z1 - BAY_WALL))   # open bottom + open rear
for sx in (1, -1):                                                                                          # rail grooves in the side walls
    bay = bay.cut(box(min(sx*(BAY_X - BAY_WALL - 0.01), sx*(BAY_X - BAY_WALL + GROOVE_D)), GROOVE_Y0, BAY_Z0 - 1,
                      max(sx*(BAY_X - BAY_WALL - 0.01), sx*(BAY_X - BAY_WALL + GROOVE_D)), GROOVE_Y1, BAY_Z1 - BAY_WALL + 0.01))
bay = bay.cut(box(BAY_X - BAY_WALL - 1, SLED_Y0 + SLED_T + 1, PI_Z0 - 2, BAY_X + 1, BAY_Y1 - 3, PI_Z0 + 58))    # side window (+X): the whole port edge, Pi 4 or Pi 5 order
bay = bay.cut(box(-10, SLED_Y0 + SLED_T + 1.5, BAY_Z1 - BAY_WALL - 1, 10, SLED_Y0 + SLED_T + 8, BAY_Z1 + 1))  # ribbon slot in the roof, behind the sled plate
for sx in (-42, 42):                                                                                        # magnet pockets in the roof underside
    bay = bay.cut(cyl(3.1, 2.2, sx, SLED_Y0 + SLED_T + 8, BAY_Z1 - BAY_WALL - 0.01))
tower = tower.fuse(mY(bay))                              # rev 3.2: the bay is on the FRONT wall
tower = tower.removeSplitter()

# ------------------------------------------------------------------ Pi sled (front plate + foot + vented rear plate, one print)
SX = BAY_X - BAY_WALL - 0.3                       # 48.7: rails ride in the grooves with 0.3 mm clearance
PX = BAY_X - BAY_WALL - 3.5                       # 45.5: plate body half-width
sled = box(-SX, SLED_Y0, SLED_Z0, SX, SLED_Y0 + SLED_T, SLED_Z1)                                              # front plate incl. rails
for px in (PI_X0 + 3.5, PI_X0 + 61.5):                                                                          # Pi bosses on the rear face (58 x 49)
    for pz in (PI_Z0 + 3.5, PI_Z0 + 52.5):
        sled = sled.fuse(cyl(3.0, PI_STANDOFF, px, SLED_Y0 + SLED_T, pz, V(0, 1, 0))).cut(cyl(1.1, PI_STANDOFF - 1, px, SLED_Y0 + SLED_T + 1, pz, V(0, 1, 0)))
for sx in (-42, 42):                                                                                            # magnet blocks on the top edge
    sled = sled.fuse(box(sx - 4.5, SLED_Y0 + SLED_T, SLED_Z1 - 3.7, sx + 4.5, SLED_Y0 + SLED_T + 12.5, SLED_Z1)).cut(cyl(3.1, 2.2, sx, SLED_Y0 + SLED_T + 8, SLED_Z1 - 2.2 + 0.01))   # above the Pi's top edge (Z 156)
foot = box(-PX, SLED_Y0, SLED_Z0, PX, BAY_Y1 - 0.5, SLED_Z0 + SLED_T)                                          # foot
foot = foot.cut(box(PI_X0 + 2, SLED_Y0 - 1, SLED_Z0 - 1, PI_X0 + 42, BAY_Y1 - 3.5, SLED_Z0 + SLED_T + 1))      # notch under the USB-C / HDMI ports
sled = sled.fuse(foot)
# outer plate = the CONTROL FACE: display window + bosses, button hole, vents low on the face
DSP_CX, DSP_CZ = -19.0, 176.0
BTN_X, BTN_Z = 37.0, 176.0
CF_Y0, CF_Y1 = BAY_Y1 - 3.0, BAY_Y1 - 0.5
rear = box(-PX, CF_Y0, SLED_Z0, PX, CF_Y1, SLED_Z1)
for i in range(7):
    z = SLED_Z0 + 10 + i * 5.5
    rear = rear.cut(box(-32, CF_Y0 - 1, z, 32, CF_Y1 + 1, z + 2.4))
rear = rear.cut(box(DSP_CX - DSP_W/2, CF_Y0 - 1, DSP_CZ - DSP_H/2, DSP_CX + DSP_W/2, CF_Y1 + 1, DSP_CZ + DSP_H/2))   # display window
for sx in (-26, 26):                                                                                             # display bosses inside, 52 x 30, M2
    for sz in (-15, 15):
        rear = rear.fuse(cyl(2.2, 5.0, DSP_CX + sx, CF_Y0 - 5.0, DSP_CZ + sz, V(0, 1, 0))).cut(cyl(0.9, 5.5, DSP_CX + sx, CF_Y0 - 5.2, DSP_CZ + sz, V(0, 1, 0)))
rear = rear.cut(cyl(8.1, 5, BTN_X, CF_Y0 - 1, BTN_Z, V(0, 1, 0)))                                                # button hole d16.2
sled = sled.fuse(rear)
sled = sled.fuse(box(-18, CF_Y0, SLED_Z0 - 7, 18, CF_Y1, SLED_Z0 + 0.01))                                       # thumb tab below the face
pi_sled = sled.removeSplitter()

# ------------------------------------------------------------------ camera cover
CC, CCH = 28.5, 25.0                      # cover clears the HQ board's tripod block (6.54 behind the edge)
cam_cover = box(-CC, -CC, TOWER_Z1, CC, CC, TOWER_Z1 + CCH)
cam_cover = cam_cover.cut(box(-CC + 2, -CC + 2, TOWER_Z1 - 1, CC - 2, CC - 2, TOWER_Z1 + CCH - 2))
cam_cover = cam_cover.cut(box(-9, -CC - 1, TOWER_Z1 - 1, 9, -CC + 3, TOWER_Z1 + 12))            # ribbon slot (front, toward the bay)
cam_cover = cam_cover.cut(box(-9, CC - 3, TOWER_Z1 - 1, 9, CC + 1, TOWER_Z1 + 8))               # notch for the tripod block (rear)
for sx in (-22, 22):
    for sy in (-22, 22):
        cam_cover = cam_cover.fuse(cyl(1.4, 3.5, sx, sy, TOWER_Z1 - 3.5))                          # pegs
cam_cover = cam_cover.removeSplitter()

# ------------------------------------------------------------------ door (snap pins)
door = box(-80, Y_FRONT - 3.5, 0.3, 80, Y_FRONT - 0.5, 40)
for s in (1, -1):
    door = door.fuse(box(min(s*75.5, s*80), Y_FRONT - 3.5, 38, max(s*75.5, s*80), Y_FRONT - 0.5, 48))
    door = door.fuse(cyl(1.5, 3.5, s*80 if s > 0 else -83.5, Y_FRONT - 2.0, HINGE_Z, V(1, 0, 0)))  # integral pin
    door = door.cut(cyl(3.1, 2.0, s*77.5, Y_FRONT - 0.5 - 2.0 + 0.01, 20, V(0, 1, 0)))
door = door.fuse(box(-60, Y_FRONT - 3.5 - 5.0, 0.3, 60, Y_FRONT - 3.5 + 0.01, 5.5))    # solid pull lip along the bottom edge: no light path
door = door.removeSplitter()

# ------------------------------------------------------------------ reference bodies (rev 2.3: datasheet geometry, see reference_parts.py)
exec(open(OUT + "reference_parts.py").read())
pad, pad_lit, pad_touch, pad_port = make_pad()
plate = make_plate(PLATE_L, PLATE_W, PLATE_H, LID_L, LID_W, LID_H, WELL_ID, WELL_PITCH, WELL_DEPTH)
cam_board, lens = make_hq(SENSOR_Z)
cam_board.rotate(V(0, 0, 0), V(0, 0, 1), 180)                                          # FPC connector toward the front (the bay side)
PIY = SLED_Y0 + SLED_T + PI_STANDOFF
pi, hat = make_pi(PI_X0, PIY, PI_Z0)
button = make_button(BTN_X, BTN_Z, CF_Y1)
M = App.Matrix(); M.A22 = -1; M.A24 = 2 * CF_Y1                                        # the button body points INTO the bay
button = button.transformGeometry(M)
display = make_display_inside(DSP_CX, DSP_CZ, CF_Y0, CF_Y1)
screen = box(DSP_CX - 20.4, CF_Y0 - 2.62, DSP_CZ - 15.3, DSP_CX + 20.4, CF_Y0 - 2.55, DSP_CZ + 15.3)   # the lit 40.8 x 30.6, just inside the window
# cables, routed. Every flexible run is a named ROUTE (waypoints + bend radius); the CAD sweeps it,
# Blender and the explorer rebuild it from cables.json as smooth curves with per-conductor colours.
RZ = TOWER_Z1 + 0.4                                     # ribbon lying on the top plate
PIC = PIY + 1.6                                         # Pi component face
SOCK_Y = PIC + 23.0 + 1.6 + 4.25                       # HAT header mid-height: 23 mm stacking header, HAT pcb 1.6, right-angle header 8.5 tall
# rigid connector bodies (part "plugs"); the cassette bodies are built in rear-bay coordinates and mirrored (y' = YM - y)
YP = YM - (PIC + 1.6)                                    # USB-C port centre line, front layout
usbc_plug = rounded_box(PI_X0 + 11.2 - 5.5, YP - 3.5, PI_Z0 - 12.0, PI_X0 + 11.2 + 5.5 + 11.0, YP + 3.5, PI_Z0 - 1.5, 1.5)   # right-angle USB-C, cable toward +X
rj45_plug = mY(box(PI_X0 + 85 + 3.0, PIC + 6.75 - 4.0, PI_Z0 + 10.25 - 5.85, PI_X0 + 85 + 3.0 + 21.0, PIC + 6.75 + 4.0, PI_Z0 + 10.25 + 5.85)
             .fuse(rounded_box(PI_X0 + 85 + 3.0 + 14.0, PIC + 6.75 - 4.4, PI_Z0 + 10.25 - 6.5, PI_X0 + 85 + 3.0 + 30.0, PIC + 6.75 + 4.4, PI_Z0 + 10.25 + 6.5, 2.0)))   # plug + boot
usba_plug = mY(box(PI_X0 + 85 + 3.0, PIC + 4.6 - 2.25, PI_Z0 + 27 - 6.0, PI_X0 + 85 + 6.5, PIC + 4.6 + 2.25, PI_Z0 + 27 + 6.0)
             .fuse(rounded_box(PI_X0 + 85 + 6.5, PIC + 4.6 - 3.8, PI_Z0 + 27 - 7.5, PI_X0 + 85 + 6.5 + 30.0, PIC + 4.6 + 3.8, PI_Z0 + 27 + 7.5, 2.0)))
dsp_housing = mY(box(DSP_CX - 10.2, CF_Y0 - 5.0 - 1.6 - 11.0 - 14.0, DSP_CZ - 17.5 + 2.7 - 1.3, DSP_CX + 10.2, CF_Y0 - 5.0 - 1.6 - 11.0 + 0.5, DSP_CZ - 17.5 + 2.7 + 1.3))   # 8-way housing on the display header
plugs = usbc_plug.fuse(rj45_plug).fuse(usba_plug).fuse(dsp_housing)
DH_Y = CF_Y0 - 5.0 - 1.6 - 11.0 - 14.0                  # wire exit of the display housing (rear coords)
PAD_R = 1.75
SY = SLED_Y0 + SLED_T + 4.5                              # ribbon plane behind the Pi (rear coords)
ROUTES = {
  "ribbon": {"kind": "ribbon", "width": 15.0, "t": 0.3, "bend_r": 3.0, "color": "ivory",
             "width_end": 12.6, "taper_mm": 45.0,
             "pts": [[0, -19.5, SENSOR_Z + 1.5], [0, -(CC + 2.5), SENSOR_Z + 1.5], [0, -(CC + 2.5), RZ], [0, Y_FRONT - 0.8, RZ],
                     [0, Y_FRONT - 0.8, BAY_Z1 + 1.0], [0, YM - SY, BAY_Z1 + 1.0], [0, YM - SY, PI_Z0 + 22.0],
                     [PI_X0 + 49.0, YM - SY, PI_Z0 - 4.0], [PI_X0 + 49.0, YM - (PIC + 2.0), PI_Z0 - 3.5], [PI_X0 + 49.0, YM - (PIC + 2.0), PI_Z0 + 4.0]]},
  # display cable: housing exit -> behind the Pi's top edge -> down the channel beside the PCB -> across the HAT onto the right-angle header
  "loom_display": {"kind": "loom", "wire_r": 0.55, "bend_r": 7.0, "colors": ["grey", "purple", "blue", "green", "yellow", "orange", "red", "brown"],
             "pts": [[DSP_CX, DH_Y, DSP_CZ - 17.5 + 2.7], [DSP_CX, PIY - 3.0, DSP_CZ - 17.5 + 2.7], [PI_X0 - 3.0, PIY - 3.0, PI_Z0 + 58.0],
                     [PI_X0 - 3.0, PIY + 9.0, PI_Z0 + 40.0], [PI_X0 - 3.0, SOCK_Y - 3.0, PI_Z0 + 31.0], [PI_X0 + 2.0, SOCK_Y, PI_Z0 + 23.0],
                     [PI_X0 + 28.0, SOCK_Y, PI_Z0 + 23.0], [PI_X0 + 38.0, SOCK_Y, PI_Z0 + 23.0]]},
  # button: two wires from its screw terminals -> down the +X side in front of the port stacks -> onto the 2-pin header from +X
  "loom_button": {"kind": "loom", "wire_r": 0.55, "bend_r": 7.0, "colors": ["black", "red"],
             "pts": [[BTN_X, CF_Y1 - 22.0, BTN_Z - 4.0], [BTN_X + 5.0, CF_Y1 - 26.0, BTN_Z - 12.0], [PI_X0 + 88.0, CF_Y1 - 24.0, PI_Z0 + 55.0],
                     [PI_X0 + 88.0, SOCK_Y - 7.0, PI_Z0 + 46.0], [PI_X0 + 70.0, SOCK_Y, PI_Z0 + 35.5], [PI_X0 + 60.1, SOCK_Y, PI_Z0 + 35.0]]},
  # power: right-angle USB-C, cable along the Pi's bottom edge UNDER the sled foot and the bay wall, out to the right, onto the pad and away
  "power": {"kind": "round", "r": 1.75, "bend_r": 12.0, "color": "white",
             "pts": [[PI_X0 + 11.2 + 16.5, YP, PI_Z0 - 7.0], [PI_X0 + 34.0, YP, PI_Z0 - 10.5], [BAY_X + 12.0, YP, PI_Z0 - 10.5], [BAY_X + 25.0, YP, 62.0],
                     [100.0, YP + 6.0, 34.0], [124.0, YP + 16.0, 8.0], [140.0, YP + 30.0, PAD_R], [175.0, 20.0, PAD_R], [190.0, 60.0, -5.1 + PAD_R], [320.0, 200.0, -5.1 + PAD_R]]},
  "ethernet": {"kind": "round", "r": 2.75, "bend_r": 22.0, "color": "blue",
             "pts": [[PI_X0 + 85 + 33.0, YM - (PIC + 6.75), PI_Z0 + 10.25], [95.0, YM - (PIC + 6.75), 100.0], [118.0, -60.0, 60.0], [136.0, -45.0, 20.0], [152.0, -25.0, 2.75],
                     [172.0, 60.0, 2.75], [190.0, 120.0, -5.1 + 2.75], [330.0, 230.0, -5.1 + 2.75]]},
  "pad_usb": {"kind": "round", "r": 1.6, "bend_r": 16.0, "color": "black",
             "pts": [[PI_X0 + 85 + 36.5, YM - (PIC + 4.6), PI_Z0 + 27.0], [98.0, YM - (PIC + 4.6), 126.0], [125.0, -56.0, 80.0], [145.0, -40.0, 30.0], [157.0, -22.0, 1.6],
                     [166.0, 20.0, 1.6], [170.0, 137.0, 1.6], [170.0, 146.0, -5.1 + 1.6], [-170.0, 150.0, -5.1 + 1.6], [-196.0, 138.0, -5.1 + 1.6],
                     [-197.0, 110.0, -2.5], [-194.0, 110.0, -2.5]]},
}
for k in ("loom_display", "loom_button"):                 # the looms were laid out in rear-bay coordinates: mirror them with the cassette
    ROUTES[k]["pts"] = [[x, YM - y, z] for x, y, z in ROUTES[k]["pts"]]
def _v(p): return V(*p)
ribbon = flat_ribbon([_v(p) for p in ROUTES["ribbon"]["pts"]], 15.0, 0.4, V(1, 0, 0))
harness = sweep_round([_v(p) for p in ROUTES["loom_display"]["pts"]], 1.9, 7.0).fuse(sweep_round([_v(p) for p in ROUTES["loom_button"]["pts"]], 1.1, 7.0))
cables = sweep_round([_v(p) for p in ROUTES["power"]["pts"]], 1.75, 14.0).fuse(sweep_round([_v(p) for p in ROUTES["ethernet"]["pts"]], 2.75, 22.0)).fuse(sweep_round([_v(p) for p in ROUTES["pad_usb"]["pts"]], 1.6, 16.0)).fuse(pad_plug)
json.dump(ROUTES, open(OUT + "cables.json", "w"), indent=1)
pi_sled, pi, hat, button, display, screen = [mY(b) for b in (pi_sled, pi, hat, button, display, screen)]   # rev 3.2: cassette on the front
magnets = Part.makeCompound([cyl(3, 2, s*77.5, Y_FRONT - 0.5 - 2.0, 20, V(0, 1, 0)) for s in (1, -1)] + [cyl(3, 2, s*77.5, Y_FRONT, 20, V(0, 1, 0)) for s in (1, -1)]
                            + [mY(cyl(3, 2, sx, SLED_Y0 + SLED_T + 8, SLED_Z1 - 2.2)) for sx in (-42, 42)] + [mY(cyl(3, 2, sx, SLED_Y0 + SLED_T + 8, BAY_Z1 - BAY_WALL)) for sx in (-42, 42)])
screws = Part.makeCompound([cyl(2.25, 1.6, sx, sy, TOWER_Z1 + CAM_STANDOFF + 1.6) for sx in (-15, 15) for sy in (-15, 15)]                   # M2.5 heads on the camera
                           + [mY(cyl(2.25, 1.6, PI_X0 + hx, PIY - 1.6, PI_Z0 + hz, V(0, 1, 0))) for hx in (3.5, 61.5) for hz in (3.5, 52.5)]        # M2.5 heads on the sled bosses (from the wall side)
                           + [mY(cyl(1.9, 1.4, DSP_CX + hx, CF_Y0 - 5.0 - 1.6 - 1.4, DSP_CZ + hz, V(0, 1, 0))) for hx in (-26, 26) for hz in (-15, 15)])  # M2 on the display, from inside

parts = {"frame": frame, "tower": tower, "pi_sled": pi_sled, "cam_cover": cam_cover, "door": door,
         "pad": pad, "pad_lit": pad_lit, "plate": plate, "cam_board": cam_board, "lens": lens, "pi": pi, "hat": hat, "button": button,
         "display": display, "screen": screen, "ribbon": ribbon, "cables": cables, "harness": harness, "plugs": plugs, "magnets": magnets, "screws": screws}
objs = {}
for n, s in parts.items():
    o = doc.addObject("Part::Feature", n); o.Shape = s; objs[n] = o
doc.recompute()
half = Part.makeBox(400, 400, 400, V(-400, -200, -200))     # keep x < 0 for the section
meta = {}
for n, s in parts.items():
    m = MeshPart.meshFromShape(Shape=s, LinearDeflection=0.15, AngularDeflection=0.2)
    m.write(OUT + f"{n}.stl")
    c = s.common(half)
    if not c.isNull() and c.Volume > 1e-6:
        MeshPart.meshFromShape(Shape=c, LinearDeflection=0.15, AngularDeflection=0.2).write(OUT + f"cut_{n}.stl")
    bb = s.BoundBox
    meta[n] = {"bbox": [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]], "volume_mm3": round(s.Volume, 1), "valid": s.isValid()}
printed = ["frame", "tower", "pi_sled", "cam_cover", "door"]
inter = []
names = list(parts)
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        if {names[i], names[j]} & {"magnets", "screen", "screws", "pad_lit"}: continue
        if {names[i], names[j]} in ({"pi", "hat"}, {"pi", "plugs"}, {"display", "plugs"}, {"hat", "harness"}, {"plugs", "cables"}, {"plugs", "harness"}, {"cam_board", "ribbon"}, {"pi", "ribbon"}, {"pad", "cables"}, {"button", "harness"}): continue
        v = parts[names[i]].common(parts[names[j]]).Volume
        if v > 0.5: inter.append((names[i], names[j], round(v, 1)))
meta["_interference"] = inter
# View-cone clearance: pyramid from the lens front down to the 152 x 114 field at the flipped colony plane (Z 20)
LENS_FRONT_Z = SENSOR_Z - 1.6 - 12.04 - 23.0
cone = Part.makeLoft([Part.makePolygon([V(-2, -2, LENS_FRONT_Z), V(2, -2, LENS_FRONT_Z), V(2, 2, LENS_FRONT_Z), V(-2, 2, LENS_FRONT_Z), V(-2, -2, LENS_FRONT_Z)]),
                      Part.makePolygon([V(-65, -44, 20), V(65, -44, 20), V(65, 44, 20), V(-65, 44, 20), V(-65, -44, 20)])], True)
blockers = []
for n, shp in parts.items():
    if n in ("plate", "pad", "pad_lit", "screen", "lens", "cam_board"): continue
    v = shp.common(cone).Volume
    if v > 0.5: blockers.append((n, round(v, 1)))
meta["_view_cone_blockers"] = blockers
meta["_hinge"] = {"axis": "x", "y": Y_FRONT - 2.0, "z": HINGE_Z}
meta["_plate_flip_center_z"] = PLATE_H / 2
meta["_panel"] = {"button_xz": [BTN_X, BTN_Z], "display_center_xz": [DSP_CX, DSP_CZ], "display_window": [DSP_W, DSP_H], "module_hole_pattern": [52, 30],
                  "face_y": YM - CF_Y1, "layout": "front: door below the cassette"}
# stability: rough masses (prints at 0.7 of solid PETG; bought parts from their datasheets), centre of mass, and the
# horizontal push at the button height that would tip the box about each edge of its footprint
MASS = {"frame": None, "tower": None, "pi_sled": None, "cam_cover": None, "door": None, "plate": 60, "cam_board": 30, "lens": 55, "pi": 70,
        "hat": 25, "button": 15, "display": 20, "plugs": 30, "magnets": 8, "screws": 6, "harness": 10, "ribbon": 5}
tot, cx, cy, cz = 0.0, 0.0, 0.0, 0.0
for n, m_ in MASS.items():
    if m_ is None: m_ = parts[n].Volume / 1000 * 1.27 * 0.7
    sh = parts[n]
    if sh.ShapeType == "Compound":                       # compounds (magnets, screws): volume-weighted mean of their solids
        vs = [(sol.Volume, sol.CenterOfMass) for sol in sh.Solids]; vt = sum(v for v, _ in vs)
        c = V(sum(v * cc.x for v, cc in vs) / vt, sum(v * cc.y for v, cc in vs) / vt, sum(v * cc.z for v, cc in vs) / vt)
    else: c = sh.CenterOfMass
    tot += m_; cx += m_ * c.x; cy += m_ * c.y; cz += m_ * c.z
cx, cy, cz = cx / tot, cy / tot, cz / tot
fb = parts["frame"].BoundBox; h_push = BTN_Z
edges = {"front": cy - fb.YMin, "back": fb.YMax - cy, "left": cx - fb.XMin, "right": fb.XMax - cx}
meta["_stability"] = {"mass_g": round(tot), "com_mm": [round(cx, 1), round(cy, 1), round(cz, 1)], "footprint": [[fb.XMin, fb.YMin], [fb.XMax, fb.YMax]],
                      "push_height_mm": h_push, "tip_force_N": {k: round(tot / 1000 * 9.81 * d / h_push, 2) for k, d in edges.items()}}
meta["_constants"] = {"Y_FRONT": Y_FRONT, "Y_REAR": Y_REAR, "TOWER_Z1": TOWER_Z1, "SENSOR_Z": SENSOR_Z, "FRAME_H": FRAME_H}
json.dump(meta, open(OUT + "parts.json", "w"), indent=1)
__result__ = {"printed_volumes": {n: meta[n]["volume_mm3"] for n in printed}, "valid": {n: meta[n]["valid"] for n in printed}, "interference": inter, "view_cone_blockers": blockers}

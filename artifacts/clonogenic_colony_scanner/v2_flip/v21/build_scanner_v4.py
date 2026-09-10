"""
Clonogenic plate scanner, rev 4.0 — "everything up with the camera" (2026-09-10).
  * THREE printed parts: plinth (frame with door slot), tower, door. No cassette, no bay, no cover, no HAT.
  * The HQ camera AND the Raspberry Pi hang under the tower's ceiling, in the dead space above the lens
    front where the camera cannot see. The Pi's ports poke through cutouts in the back and right walls.
  * The 16 mm pushbutton is on the roof, beside the camera: a press goes straight down into the bench.
  * The 2-inch display sits in the front wall above the door slot; its own cable and the button's two
    wires plug directly onto the Pi's 40-pin header. Nothing is soldered.
  * The frame is a plinth with a modest flare (sides 15, back 25) and a vertical front for the door.
Runs inside the AnkusDrive worker: exec(open(path).read(), globals()).
Frame: X along the plate's long axis, Y short axis (front = -Y), Z up, pad surface Z = 0.
"""
import json, math
import FreeCAD as App
import Part, MeshPart
from FreeCAD import Vector as V

if App.ActiveDocument is not None and App.ActiveDocument.Name.startswith("colony_scanner"):
    App.closeDocument(App.ActiveDocument.Name)
doc = App.newDocument("colony_scanner_v4")
OUT = "/home/user/dev/ankusdrive/artifacts/clonogenic_colony_scanner/v2_flip/v21/"

# ---- inputs (CELLTREAT 229105 drawing; Raspberry Pi drawings; Huion L4S; Arducam 8 mm)
PLATE_L, PLATE_W, PLATE_H = 127.8, 85.38, 23.0
LID_L, LID_W, LID_H = 127.0, 84.8, 9.9
WELL_ID, WELL_PITCH, WELL_DEPTH = 34.7, 39.04, 17.2
CLR = 0.4
POCKET_X, POCKET_Y = PLATE_L + 2 * CLR, PLATE_W + 2 * CLR
WALL = 3.0                                 # frame walls (the front wall carries the door slot)
WT = 2.4                                   # tower walls
FOOT_X = 176.0
Y_FRONT = -(POCKET_Y / 2 + 1.3 + WALL)     # -47.95
Y_REAR = 59.0
IN_X = FOOT_X - 2 * WALL
FRAME_H, FLOOR_T, RIM_H = 50.0, 2.0, 6.0
SLOT_HALF_X, SLOT_Z1 = 75.0, 34.0
FLARE_X, FLARE_Y = 15.0, 25.0              # plinth flare: sides, back (front vertical for the door)
SENSOR_Z = 220.0                           # 200 mm above the flipped colony plane (Z ~ 20)
CAM_STANDOFF, PI_STANDOFF = 8.0, 8.0
TOP_T = 3.0
CEIL = SENSOR_Z + CAM_STANDOFF             # 228: ceiling underside (the camera PCB top face is the SENSOR_Z plane, as in make_hq)
TOWER_Z1 = CEIL + TOP_T                    # 231
g_i, g_o, g_d = 1.0, 3.0, 2.0
HINGE_Z = 44.0
BTN_X, BTN_Y = -60.0, -22.0                # button on the roof, front-left, beside the camera
DSP_CX, DSP_CZ, DSP_W, DSP_H = -19.0, 191.0, 42.0, 32.0
PI_X0, PI_Y0 = 22.0, -30.0                 # Pi footprint under the ceiling: x 22..78, y -30..55 (USB-C edge toward +X, Ethernet toward +Y); 3 mm from the camera PCB, 6 mm from the right wall
XI = FOOT_X / 2 - WT                       # 85.6 tower inner half-width
YI0, YI1 = Y_FRONT + WT, Y_REAR - WT       # tower inner faces (front, back)

def box(x0, y0, z0, x1, y1, z1): return Part.makeBox(x1 - x0, y1 - y0, z1 - z0, V(x0, y0, z0))
def cyl(r, h, x, y, z, d=V(0, 0, 1)): return Part.makeCylinder(r, h, V(x, y, z), d)
def rect(x0, y0, x1, y1, z): return Part.makePolygon([V(x0, y0, z), V(x1, y0, z), V(x1, y1, z), V(x0, y1, z), V(x0, y0, z)])
def wedge_x(x0, x1, y_wall, z_bottom, up, out):
    f = Part.Face(Part.makePolygon([V(0, y_wall, z_bottom - up), V(0, y_wall - out, z_bottom), V(0, y_wall, z_bottom), V(0, y_wall, z_bottom - up)]))
    w = f.extrude(V(x1 - x0, 0, 0)); w.translate(V(x0, 0, 0)); return w

# ------------------------------------------------------------------ plinth (frame)
frame = Part.makeLoft([rect(-FOOT_X/2 - FLARE_X, Y_FRONT, FOOT_X/2 + FLARE_X, Y_REAR + FLARE_Y, 0),
                       rect(-FOOT_X/2, Y_FRONT, FOOT_X/2, Y_REAR, FRAME_H)], True, True)
frame = frame.cut(Part.makeLoft([rect(-FOOT_X/2 - FLARE_X + 3.0, Y_FRONT + WALL, FOOT_X/2 + FLARE_X - 3.0, Y_REAR + FLARE_Y - 3.0, FLOOR_T),
                                 rect(-IN_X/2, Y_FRONT + WALL, IN_X/2, Y_REAR - WALL, FRAME_H + 1)], True, True))
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
for s in (1, -1):                                                                     # snap-fit hinge lugs, C-slot toward the front
    x0, x1 = min(s*80.5, s*85), max(s*80.5, s*85)
    frame = frame.fuse(box(x0, Y_FRONT - 5.5, 40, x1, Y_FRONT, 48)).fuse(wedge_x(x0, x1, Y_FRONT, 40, 5.5, 5.5))
    frame = frame.cut(cyl(1.65, 5, x0 - 0.2, Y_FRONT - 2.0, HINGE_Z, V(1, 0, 0)))
    frame = frame.cut(box(x0 - 0.2, Y_FRONT - 7, HINGE_Z - 1.2, x1 + 0.2, Y_FRONT - 2.0, HINGE_Z + 1.2))
    frame = frame.cut(cyl(3.1, 2.0, s*77.5, Y_FRONT - 0.01, 20, V(0, 1, 0)))                          # magnet pocket
frame = frame.fuse(Part.Face(Part.makePolygon([V(-58, 44, RIM_H), V(-50, 44, RIM_H), V(-54, 48, RIM_H), V(-58, 44, RIM_H)])).extrude(V(0, 0, 1.0)))  # A1 cue
frame = frame.removeSplitter()

# ------------------------------------------------------------------ tower: tube + ceiling that carries camera, Pi and button
tower = box(-FOOT_X/2, Y_FRONT, FRAME_H, FOOT_X/2, Y_REAR, TOWER_Z1)
tower = tower.cut(box(-XI, YI0, FRAME_H + 8, XI, YI1, CEIL))                                            # 2.4 mm walls above the tongue band
tower = tower.cut(box(-IN_X/2, Y_FRONT + WALL, FRAME_H - 1, IN_X/2, Y_REAR - WALL, FRAME_H + 8.01))     # 3 mm band at the bottom for the tongue
tongue = box(-IN_X/2 + g_i + 0.3, Y_FRONT + WALL + g_i + 0.3, FRAME_H - g_d + 0.4, IN_X/2 - g_i - 0.3, Y_REAR - WALL - g_i - 0.3, FRAME_H)
tongue = tongue.cut(box(-IN_X/2 + g_o - 0.3, Y_FRONT + WALL + g_o - 0.3, FRAME_H - g_d - 1, IN_X/2 - g_o + 0.3, Y_REAR - WALL - g_o + 0.3, FRAME_H + 1))
tower = tower.fuse(tongue)
for sx in (-15, 15):                                                                                    # camera bosses under the ceiling
    for sy in (-15, 15):
        tower = tower.fuse(cyl(3.0, CAM_STANDOFF, sx, sy, CEIL - CAM_STANDOFF)).cut(cyl(1.1, CAM_STANDOFF - 1, sx, sy, CEIL - CAM_STANDOFF - 0.01))
PI_BOSS = [(PI_X0 + 3.5, PI_Y0 + 3.5), (PI_X0 + 52.5, PI_Y0 + 3.5), (PI_X0 + 3.5, PI_Y0 + 61.5), (PI_X0 + 52.5, PI_Y0 + 61.5)]   # 58 x 49 hole pattern
for bx, by in PI_BOSS:
    tower = tower.fuse(cyl(3.0, PI_STANDOFF, bx, by, CEIL - PI_STANDOFF)).cut(cyl(1.1, PI_STANDOFF - 1, bx, by, CEIL - PI_STANDOFF - 0.01))
tower = tower.cut(cyl(8.1, TOP_T + 2, BTN_X, BTN_Y, CEIL - 1))                                          # button hole d16.2 in the roof
tower = tower.cut(box(DSP_CX - DSP_W/2, Y_FRONT - 1, DSP_CZ - DSP_H/2, DSP_CX + DSP_W/2, YI0 + 1, DSP_CZ + DSP_H/2))   # display window, front wall
for sx in (-26, 26):                                                                                    # display bosses inside the front wall, 52 x 30, M2
    for sz in (-15, 15):
        tower = tower.fuse(cyl(2.2, 5.0, DSP_CX + sx, YI0, DSP_CZ + sz, V(0, 1, 0))).cut(cyl(0.9, 5.5, DSP_CX + sx, YI0 - 0.2, DSP_CZ + sz, V(0, 1, 0)))
PORT_Z0, PORT_Z1 = SENSOR_Z - 1.6 - 16.5, SENSOR_Z - 1.0                                                    # Pi ports hang 16 below its PCB (components down)
tower = tower.cut(box(PI_X0 + 2.0, YI1 - 1, PORT_Z0, PI_X0 + 56.0, Y_REAR + 1, PORT_Z1))              # back wall: Ethernet + two USB stacks
tower = tower.cut(box(XI - 1, PI_Y0 + 4.0, SENSOR_Z - 6.0, FOOT_X/2 + 1, PI_Y0 + 36.0, SENSOR_Z + 3.0))   # right wall: USB-C plug and the microSD
for z in (CEIL - 6.6, CEIL - 3.0):                                                                      # two vent slots high on the back wall (above the lens: unseen)
    tower = tower.cut(box(PI_X0 + 4.0, YI1 - 1, z, PI_X0 + 54.0, Y_REAR + 1, z + 2.4))
tower = tower.removeSplitter()

# ------------------------------------------------------------------ door (snap pins), parks open against the tower
door = box(-80, Y_FRONT - 3.5, 0.3, 80, Y_FRONT - 0.5, 40)
for s in (1, -1):
    door = door.fuse(box(min(s*75.5, s*80), Y_FRONT - 3.5, 38, max(s*75.5, s*80), Y_FRONT - 0.5, 48))
    door = door.fuse(cyl(1.5, 3.5, s*80 if s > 0 else -83.5, Y_FRONT - 2.0, HINGE_Z, V(1, 0, 0)))
    door = door.cut(cyl(3.1, 2.0, s*77.5, Y_FRONT - 0.5 - 2.0 + 0.01, 20, V(0, 1, 0)))
door = door.fuse(box(-60, Y_FRONT - 3.5 - 5.0, 0.3, 60, Y_FRONT - 3.5 + 0.01, 5.5))
door = door.removeSplitter()

# ------------------------------------------------------------------ reference bodies (datasheet geometry, reference_parts.py)
exec(open(OUT + "reference_parts.py").read())
pad, pad_lit, pad_touch, pad_port = make_pad()
plate = make_plate(PLATE_L, PLATE_W, PLATE_H, LID_L, LID_W, LID_H, WELL_ID, WELL_PITCH, WELL_DEPTH)
cam_board, lens = make_hq(SENSOR_Z)                                    # native: board in XY, sensor at SENSOR_Z, lens down
# Pi: built in its native frame (board in XZ, components +Y) then laid under the ceiling, components DOWN,
# Ethernet edge toward +Y (back wall), USB-C edge toward +X (right wall):  (x, y, z) -> (-z, x, -y)
PIC = -SENSOR_Z                                                         # native back-face y: the PCB back lands on the bosses at z = SENSOR_Z
pi, hat = make_pi(PI_Y0, PIC, -(PI_X0 + 56.0))
for shp in (pi,):
    shp.rotate(V(0, 0, 0), V(1, 0, 0), -90); shp.rotate(V(0, 0, 0), V(0, 0, 1), 90)
PI_PCB_Z = SENSOR_Z                                                    # back face on the bosses (220); component face at 218.4
# button on the roof: native panel along Y -> rotate so the bezel is on top and the body hangs into the tube
button = make_button(BTN_X, BTN_Y, -TOWER_Z1)
button.rotate(V(0, 0, 0), V(1, 0, 0), -90)                            # (x, y, z) -> (x, z, -y): bezel at z = TOWER_Z1 + 1.5, body down to CEIL - 20
# display: module on the inside of the front wall, glass toward -Y: build against a +Y-facing plate and mirror
display = make_display_inside(DSP_CX, DSP_CZ, -YI0, -Y_FRONT).mirror(V(0, 0, 0), V(0, 1, 0))
screen = box(DSP_CX - 20.4, YI0 + 2.55, DSP_CZ - 15.3, DSP_CX + 20.4, YI0 + 2.62, DSP_CZ + 15.3)
# cables, routed. Every flexible run is a named ROUTE (waypoints + bend radius) written to cables.json.
HDR_X, HDR_Z = PI_X0 + 3.5, PI_PCB_Z - 1.6 - 8.5                      # 40-pin header pins point down; Dupont housings hang below to HDR_Z - 14
DUP_Z = HDR_Z - 14.0
DH_Y0 = YI0 + 5.0 + 1.6 + 6.0                                          # display header pins start (y), 11 mm long; housing 14 mm beyond
dsp_housing = box(DSP_CX - 10.2, DH_Y0, DSP_CZ - 17.5 + 2.7 - 1.3, DSP_CX + 10.2, DH_Y0 + 14.0, DSP_CZ - 17.5 + 2.7 + 1.3)
USBC_Y, USBC_Z = PI_Y0 + 11.2, SENSOR_Z - 3.2                          # USB-C port centre on the right edge
rj45_x, rj45_z = PI_X0 + 56.0 - 10.25, SENSOR_Z - 1.6 - 6.75
usba_x, usba_z = PI_X0 + 56.0 - 27.0, SENSOR_Z - 1.6 - 4.6
usbc_plug = rounded_box(XI, USBC_Y - 4.5, USBC_Z - 3.5, FOOT_X/2 + 24.0, USBC_Y + 4.5, USBC_Z + 3.5, 1.5)          # straight USB-C plug through the right wall
rj45_plug = (box(rj45_x - 5.85, Y_REAR - 3.0, rj45_z - 4.0, rj45_x + 5.85, Y_REAR + 21.0, rj45_z + 4.0)
             .fuse(rounded_box(rj45_x - 6.5, Y_REAR + 14.0, rj45_z - 4.4, rj45_x + 6.5, Y_REAR + 30.0, rj45_z + 4.4, 2.0)))
usba_plug = (box(usba_x - 6.0, Y_REAR - 2.0, usba_z - 2.25, usba_x + 6.0, Y_REAR + 6.5, usba_z + 2.25)
             .fuse(rounded_box(usba_x - 7.5, Y_REAR + 6.5, usba_z - 3.8, usba_x + 7.5, Y_REAR + 36.5, usba_z + 3.8, 2.0)))
pad_plug = rounded_box(-194.0, 110.0 - 3.6, -2.5 - 3.0, -180.0, 110.0 + 3.6, -2.5 + 3.0, 1.5)
plugs = usbc_plug.fuse(rj45_plug).fuse(usba_plug).fuse(dsp_housing)
PAD_R = 1.75
ROUTES = {
  # camera ribbon: FPC connector on the camera's top face -> along the ceiling -> down the right wall gap into the Pi's CSI connector
  "ribbon": {"kind": "ribbon", "width": 15.0, "t": 0.3, "bend_r": 4.0, "color": "ivory", "width_end": 12.6, "taper_mm": 40.0,
             "pts": [[0, 20.0, SENSOR_Z + 3.0], [0, 34.0, CEIL - 5.0], [40.0, 46.0, CEIL - 5.0], [PI_X0 + 56.0, 46.0, CEIL - 5.0],
                     [PI_X0 + 56.0, 36.0, SENSOR_Z - 0.4], [PI_X0 + 56.0, 22.0, SENSOR_Z - 1.3], [PI_X0 + 56.0, 19.0, SENSOR_Z - 1.3]]},
  # display cable: from the housing on the display header, up past the lens, onto the Pi header from below
  "loom_display": {"kind": "loom", "wire_r": 0.55, "bend_r": 7.0, "colors": ["grey", "purple", "blue", "green", "yellow", "orange", "red", "brown"],
             "pts": [[DSP_CX, DH_Y0 + 14.5, DSP_CZ - 17.5 + 2.7], [DSP_CX, DH_Y0 + 27.0, DSP_CZ - 6.0], [0.0, -26.0, 196.0], [HDR_X - 2.0, -24.0, DUP_Z - 8.0], [HDR_X, -20.0, DUP_Z]]},
  # button: two wires from the switch terminals, around the lens, onto GPIO 17 / GND from below
  "loom_button": {"kind": "loom", "wire_r": 0.55, "bend_r": 7.0, "colors": ["black", "red"],
             "pts": [[BTN_X, BTN_Y, CEIL - 18.5], [BTN_X + 8.0, BTN_Y, CEIL - 28.0], [-20.0, -32.0, 198.0], [10.0, -32.0, 198.0], [HDR_X, -14.0, DUP_Z - 6.0], [HDR_X, -10.0, DUP_Z]]},
  # power: out of the right wall, down beside the plinth, onto the pad and away to the back
  "power": {"kind": "round", "r": PAD_R, "bend_r": 14.0, "color": "white",
             "pts": [[FOOT_X/2 + 24.0, USBC_Y, USBC_Z], [FOOT_X/2 + 40.0, USBC_Y, USBC_Z - 8.0], [135.0, -10.0, 120.0], [140.0, 0.0, PAD_R],
                     [150.0, 60.0, PAD_R], [160.0, 140.0, -5.1 + PAD_R], [200.0, 260.0, -5.1 + PAD_R]]},
  "ethernet": {"kind": "round", "r": 2.75, "bend_r": 22.0, "color": "blue",
             "pts": [[rj45_x, Y_REAR + 30.0, rj45_z], [rj45_x, Y_REAR + 55.0, rj45_z - 10.0], [rj45_x - 2.0, 130.0, 120.0], [rj45_x - 5.0, 145.0, 30.0],
                     [rj45_x - 6.0, 152.0, -5.1 + 2.75], [rj45_x - 10.0, 260.0, -5.1 + 2.75]]},
  "pad_usb": {"kind": "round", "r": 1.6, "bend_r": 16.0, "color": "black",
             "pts": [[usba_x, Y_REAR + 36.5, usba_z], [usba_x, Y_REAR + 58.0, usba_z - 12.0], [usba_x - 6.0, 128.0, 100.0], [usba_x - 16.0, 140.0, 20.0],
                     [usba_x - 26.0, 150.0, -5.1 + 1.6], [-170.0, 150.0, -5.1 + 1.6], [-196.0, 138.0, -5.1 + 1.6], [-197.0, 110.0, -2.5], [-194.0, 110.0, -2.5]]},
}
def _v(p): return V(*p)
ribbon = flat_ribbon_surface([_v(p) for p in ROUTES["ribbon"]["pts"]], 15.0, 0.4)
harness = sweep_round([_v(p) for p in ROUTES["loom_display"]["pts"]], 1.9, 7.0).fuse(sweep_round([_v(p) for p in ROUTES["loom_button"]["pts"]], 1.1, 7.0))
cables = sweep_round([_v(p) for p in ROUTES["power"]["pts"]], PAD_R, 14.0).fuse(sweep_round([_v(p) for p in ROUTES["ethernet"]["pts"]], 2.75, 22.0)).fuse(sweep_round([_v(p) for p in ROUTES["pad_usb"]["pts"]], 1.6, 16.0)).fuse(pad_plug)
json.dump(ROUTES, open(OUT + "cables.json", "w"), indent=1)
magnets = Part.makeCompound([cyl(3, 2, s*77.5, Y_FRONT - 0.5 - 2.0, 20, V(0, 1, 0)) for s in (1, -1)] + [cyl(3, 2, s*77.5, Y_FRONT, 20, V(0, 1, 0)) for s in (1, -1)])
screws = Part.makeCompound([cyl(2.25, 1.6, sx, sy, SENSOR_Z - 1.6 - 1.6) for sx in (-15, 15) for sy in (-15, 15)]                 # M2.5 heads under the camera PCB
                           + [cyl(2.25, 1.6, bx, by, SENSOR_Z - 1.6 - 1.6) for bx, by in PI_BOSS]                                          # M2.5 heads under the Pi PCB
                           + [cyl(1.9, 1.4, DSP_CX + hx, YI0 + 5.0 + 1.6, DSP_CZ + hz, V(0, 1, 0)) for hx in (-26, 26) for hz in (-15, 15)])   # M2 on the display

parts = {"frame": frame, "tower": tower, "door": door,
         "pad": pad, "pad_lit": pad_lit, "plate": plate, "cam_board": cam_board, "lens": lens, "pi": pi, "button": button,
         "display": display, "screen": screen, "ribbon": ribbon, "cables": cables, "harness": harness, "plugs": plugs, "magnets": magnets, "screws": screws}
objs = {}
for n, s in parts.items():
    o = doc.addObject("Part::Feature", n); o.Shape = s; objs[n] = o
doc.recompute()
half = Part.makeBox(400, 400, 400, V(-355, -200, -200))     # keep x < 45 for the section (display, lens, button, and the Pi cut through)
meta = {}
for n, s in parts.items():
    m = MeshPart.meshFromShape(Shape=s, LinearDeflection=0.15, AngularDeflection=0.2)
    m.write(OUT + f"{n}.stl")
    c = s.common(half)
    if not c.isNull() and c.Volume > 1e-6:
        MeshPart.meshFromShape(Shape=c, LinearDeflection=0.15, AngularDeflection=0.2).write(OUT + f"cut_{n}.stl")
    bb = s.BoundBox
    meta[n] = {"bbox": [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]], "volume_mm3": round(s.Volume, 1), "valid": s.isValid()}
printed = ["frame", "tower", "door"]
inter = []
names = list(parts)
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        if {names[i], names[j]} & {"magnets", "screen", "screws", "pad_lit"}: continue
        if {names[i], names[j]} in ({"pi", "plugs"}, {"display", "plugs"}, {"plugs", "cables"}, {"plugs", "harness"}, {"cam_board", "ribbon"}, {"pi", "ribbon"},
                                    {"pad", "cables"}, {"button", "harness"}, {"display", "harness"}, {"pi", "harness"}, {"tower", "plugs"}): continue
        v = parts[names[i]].common(parts[names[j]]).Volume
        if v > 0.5: inter.append((names[i], names[j], round(v, 1)))
meta["_interference"] = inter
LENS_FRONT_Z = SENSOR_Z - 1.6 - 12.04 - 23.0                           # 183.4
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
meta["_panel"] = {"button_xy_roof": [BTN_X, BTN_Y], "display_center_xz": [DSP_CX, DSP_CZ], "display_window": [DSP_W, DSP_H], "module_hole_pattern": [52, 30],
                  "layout": "rev 4: camera and Pi under the ceiling, button on the roof, display in the front wall above the door"}
meta["_constants"] = {"Y_FRONT": Y_FRONT, "Y_REAR": Y_REAR, "TOWER_Z1": TOWER_Z1, "SENSOR_Z": SENSOR_Z, "FRAME_H": FRAME_H, "CEIL": CEIL}
# stability: rough masses (prints at 0.7 of solid PETG; bought parts from their datasheets), centre of mass, and the
# horizontal push at the roof that would tip the box about each edge of its footprint (a button press is vertical: no moment)
MASS = {"frame": None, "tower": None, "door": None, "plate": 60, "cam_board": 30, "lens": 55, "pi": 70, "button": 15, "display": 20, "plugs": 25, "magnets": 4, "screws": 5, "harness": 8, "ribbon": 3}
tot, cx, cy, cz = 0.0, 0.0, 0.0, 0.0
for n, m_ in MASS.items():
    if m_ is None: m_ = parts[n].Volume / 1000 * 1.27 * 0.7
    sh = parts[n]
    if sh.ShapeType == "Compound":
        vs = [(sol.Volume, sol.CenterOfMass) for sol in sh.Solids]; vt = sum(v for v, _ in vs)
        c = V(sum(v * cc.x for v, cc in vs) / vt, sum(v * cc.y for v, cc in vs) / vt, sum(v * cc.z for v, cc in vs) / vt)
    else: c = sh.CenterOfMass
    tot += m_; cx += m_ * c.x; cy += m_ * c.y; cz += m_ * c.z
cx, cy, cz = cx / tot, cy / tot, cz / tot
fb = parts["frame"].BoundBox; h_push = TOWER_Z1
edges = {"front": cy - fb.YMin, "back": fb.YMax - cy, "left": cx - fb.XMin, "right": fb.XMax - cx}
meta["_stability"] = {"mass_g": round(tot), "com_mm": [round(cx, 1), round(cy, 1), round(cz, 1)], "footprint": [[fb.XMin, fb.YMin], [fb.XMax, fb.YMax]],
                      "push_height_mm": h_push, "tip_force_N": {k: round(tot / 1000 * 9.81 * d / h_push, 2) for k, d in edges.items()}, "button_press_moment": 0}
json.dump(meta, open(OUT + "parts.json", "w"), indent=1)
__result__ = {"printed_volumes": {n: meta[n]["volume_mm3"] for n in printed}, "valid": {n: meta[n]["valid"] for n in printed}, "interference": inter,
              "view_cone_blockers": blockers, "stability": meta["_stability"]}

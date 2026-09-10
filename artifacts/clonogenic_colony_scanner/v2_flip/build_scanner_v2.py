"""
Clonogenic plate scanner, variant B: ONE camera, plate flipped for the colony shot, a
cheap A5 LED tracing pad as the floor. Runs inside the AnkusDrive worker via run_script:
    exec(open(path).read(), globals())

Frame: X along the plate's long axis, Y short axis, Z up. Origin = plate centre on the pad
surface (Z = 0). The box STANDS ON the pad; the pad extends beyond the footprint at the sides.

Shot 1: plate lid-up on the pad -> camera sees the marker labels, backlit.
Shot 2: plate flipped (lid down) -> camera sees the well floors through the plate bottom, in
        transmission; the lid ink is now in contact with the pad, 20 mm below the colonies,
        and casts only a ~70 mm-wide, <=10 % dimming (optics_model.py, "pad on lid").
Both planes sit ~21-22.5 mm above the pad, so one fixed focus serves both.

Printed parts (black matte PETG/ASA):
  frame   plate pocket + loading slot + door lugs, stands on the pad          (Z 0..50)
  tower   dark tube with the camera plate on top, printed upside down       (Z 50..212.4)
  door    magnetic front flap (same as variant A)
Reference bodies: pad, plate_flipped (+lid), cam_hq.
"""
import FreeCAD as App
import Part
from FreeCAD import Vector as V

if App.ActiveDocument is not None and App.ActiveDocument.Name.startswith("colony_scanner"):
    App.closeDocument(App.ActiveDocument.Name)
doc = App.newDocument("colony_scanner_v2")

SRC = {
    "plate":  "Corning 3516: 127.76 x 85.47 x 20.27, floor at 3.81; Eppendorf TDS: with lid 23.2, lid 127.6 x 84.3 x 9.1; "
              "SPL TDS: 127.6 x 85.4 x 20.2, lid 127.1 x 84.9 x 10.0 -> lids never exceed the footprint (PLATE-COMPATIBILITY.md)",
    "pad":    "A5 LED tracing pad, typical: 230 x 160 x 5 mm outer, ~210 x 150 lit, USB 5 V ~3 W, "
              "3000-4000 lux (VERIFY the one you buy)",
    "hq_cam": "Raspberry Pi HQ Camera: 38 x 38 PCB, 4 x M2.5 on 30 x 30 (VERIFY), CS mount",
    "lens8":  "8 mm CS-mount lens for the HQ camera (Arducam or similar, 1/2.3 in., ~F1.6-16)",
}

PLATE_L, PLATE_W, PLATE_H = 127.8, 85.38, 23.0     # CELLTREAT 229105 drawing (with lid 23.0)
LID_L, LID_W = 127.0, 84.8            # CELLTREAT lid; lids are <= the plate footprint (Eppendorf 127.6 x 84.3, SPL 127.1 x 84.9)
WELL_ID, WELL_PITCH, WELL_DEPTH = 34.7, 39.04, 17.2
CLR = 0.4                                 # per side on the plate skirt; lids are smaller and get 0.4-1.0 mm slop
POCKET_X, POCKET_Y = PLATE_L + 2 * CLR, PLATE_W + 2 * CLR

WALL = 3.0
FOOT_X = 176.0
Y_FRONT = -(POCKET_Y / 2 + 1.3 + WALL)    # plate front face 1.3 mm behind the front wall: no bright runway
Y_REAR = 59.0
IN_X = FOOT_X - 2 * WALL

FRAME_H = 50.0
FLOOR_T = 2.0                             # floor ring covers the pad everywhere but the pocket window
RIM_H = 6.0
SLOT_HALF_X, SLOT_Z1 = 75.0, 34.0

SENSOR_Z = 200.0 + 20.0                   # 8 mm lens, 200 mm sensor -> colony plane; CELLTREAT flipped: 23.0 with lid - 3.0 floor = 20.0
TOP_T = 3.0
CAM_STANDOFF = 8.0
TOWER_Z1 = SENSOR_Z - 1.6 - CAM_STANDOFF  # top-plate upper face: PCB (1.6) on standoffs above it
LENS_HOLE_R = 17.0

g_i, g_o, g_d = 1.0, 3.0, 2.0             # tongue/groove labyrinth (as variant A)

def box(x0, y0, z0, x1, y1, z1): return Part.makeBox(x1 - x0, y1 - y0, z1 - z0, V(x0, y0, z0))
def cyl(r, h, x, y, z, axis=V(0, 0, 1)): return Part.makeCylinder(r, h, V(x, y, z), axis)
def feature(name, shape):
    o = doc.addObject("Part::Feature", name); o.Shape = shape; return o

# ------------------------------------------------------------------ frame
frame = box(-FOOT_X/2, Y_FRONT, 0, FOOT_X/2, Y_REAR, FRAME_H)
frame = frame.cut(box(-IN_X/2, Y_FRONT + WALL, FLOOR_T, IN_X/2, Y_REAR - WALL, FRAME_H + 1))
# pocket window through the floor ring (plate sits directly on the pad), open to the front
frame = frame.cut(box(-POCKET_X/2, Y_FRONT - 1, -1, POCKET_X/2, POCKET_Y/2, FLOOR_T + 1))
# pocket rim: 2.5 mm walls on the two sides and the rear, RIM_H tall
rim = box(-POCKET_X/2 - 2.5, Y_FRONT + WALL, 0, POCKET_X/2 + 2.5, POCKET_Y/2 + 2.5, RIM_H)
rim = rim.cut(box(-POCKET_X/2, Y_FRONT - 1, -1, POCKET_X/2, POCKET_Y/2, RIM_H + 1))
frame = frame.fuse(rim)
# loading slot in the front wall, floor to SLOT_Z1
frame = frame.cut(box(-SLOT_HALF_X, Y_FRONT - 1, -1, SLOT_HALF_X, Y_FRONT + WALL + 1, SLOT_Z1))
# lead-in chamfer on the pocket side walls at the front (3 mm x 45deg)
for s in (1, -1):
    tri = Part.makePolygon([V(s*POCKET_X/2, Y_FRONT + WALL, -1), V(s*(POCKET_X/2 + 3), Y_FRONT + WALL, -1),
                            V(s*POCKET_X/2, Y_FRONT + WALL + 3, -1), V(s*POCKET_X/2, Y_FRONT + WALL, -1)])
    frame = frame.cut(Part.Face(tri).extrude(V(0, 0, RIM_H + 2)))
# groove in the top face for the tower tongue
groove = box(-IN_X/2 + g_i, Y_FRONT + WALL + g_i, FRAME_H - g_d, IN_X/2 - g_i, Y_REAR - WALL - g_i, FRAME_H + 1)
groove = groove.cut(box(-IN_X/2 + g_o, Y_FRONT + WALL + g_o, FRAME_H - g_d - 1, IN_X/2 - g_o, Y_REAR - WALL - g_o, FRAME_H + 2))
frame = frame.cut(groove)
# door hinge lugs (outboard of the 160 mm door) with 45deg support wedges, pin along X at Z=44
for s in (1, -1):
    x0, x1 = min(s*80.5, s*85), max(s*80.5, s*85)
    frame = frame.fuse(box(x0, Y_FRONT - 5, 40, x1, Y_FRONT, 48))
    wedge = Part.Face(Part.makePolygon([V(0, Y_FRONT, 35), V(0, Y_FRONT - 5, 40), V(0, Y_FRONT, 40), V(0, Y_FRONT, 35)]))
    wedge = wedge.extrude(V(4.5, 0, 0)); wedge.translate(V(x0, 0, 0))
    frame = frame.fuse(wedge)
    frame = frame.cut(cyl(1.0, 5.2, s*80.4 if s > 0 else -85.1, Y_FRONT - 2.5, 44, V(1, 0, 0)))
    frame = frame.cut(cyl(3.1, 2.0, s*77.5, Y_FRONT - 0.01, 20, V(0, 1, 0)))        # door magnet, 1 mm wall left
frame = frame.removeSplitter()

# ------------------------------------------------------------------ tower
tower = box(-FOOT_X/2, Y_FRONT, FRAME_H, FOOT_X/2, Y_REAR, TOWER_Z1)
tower = tower.cut(box(-IN_X/2, Y_FRONT + WALL, FRAME_H - 1, IN_X/2, Y_REAR - WALL, TOWER_Z1 - TOP_T))
tongue = box(-IN_X/2 + g_i + 0.3, Y_FRONT + WALL + g_i + 0.3, FRAME_H - g_d + 0.4, IN_X/2 - g_i - 0.3, Y_REAR - WALL - g_i - 0.3, FRAME_H)
tongue = tongue.cut(box(-IN_X/2 + g_o - 0.3, Y_FRONT + WALL + g_o - 0.3, FRAME_H - g_d - 1, IN_X/2 - g_o + 0.3, Y_REAR - WALL - g_o + 0.3, FRAME_H + 1))
tower = tower.fuse(tongue)
tower = tower.cut(cyl(LENS_HOLE_R, TOP_T + 2, 0, 0, TOWER_Z1 - TOP_T - 1))          # lens through the top plate
for sx in (-15, 15):                                                                 # HQ camera bosses, outside
    for sy in (-15, 15):
        tower = tower.fuse(cyl(3.0, CAM_STANDOFF, sx, sy, TOWER_Z1))
        tower = tower.cut(cyl(1.1, CAM_STANDOFF - 1, sx, sy, TOWER_Z1 + 1))
tower = tower.cut(cyl(12.2, WALL + 2, 60, Y_FRONT - 1, 90, V(0, 1, 0)))               # arcade button, front
tower = tower.cut(cyl(2.6, WALL + 2, 40, Y_FRONT - 1, 90, V(0, 1, 0)))                # status LED
for px in (-49, 9):                                                                  # Pi 5 pattern, rear wall
    for pz in (100, 149):
        tower = tower.cut(cyl(1.4, WALL + 2, px, Y_REAR - WALL - 1, pz, V(0, 1, 0)))
tower = tower.removeSplitter()

# ------------------------------------------------------------------ door
door = box(-80, Y_FRONT - 3.5, 0.3, 80, Y_FRONT - 0.5, 40)      # the box stands on the pad: door cannot dip below Z=0
for s in (1, -1):
    door = door.fuse(box(min(s*75.5, s*80), Y_FRONT - 3.5, 38, max(s*75.5, s*80), Y_FRONT - 0.5, 48))
    door = door.cut(cyl(1.0, 12, s*74.5 if s > 0 else -86.5, Y_FRONT - 2.0, 44, V(1, 0, 0)))
    door = door.cut(cyl(3.1, 2.0, s*77.5, Y_FRONT - 0.5 - 2.0 + 0.01, 20, V(0, 1, 0)))
door = door.cut(cyl(8, 5, 0, Y_FRONT - 5, 8, V(0, 1, 0)))
door = door.removeSplitter()

# ------------------------------------------------------------------ reference bodies
pad = box(-105, -75, -5, 105, 75, 0)
lid = box(-LID_L/2, -LID_W/2, 0, LID_L/2, LID_W/2, 8).cut(box(-LID_L/2 + 1, -LID_W/2 + 1, 1.5, LID_L/2 - 1, LID_W/2 - 1, 9))
body = box(-PLATE_L/2, -PLATE_W/2, 2.0, PLATE_L/2, PLATE_W/2, PLATE_H)              # flipped: skirt up
for wx in (-WELL_PITCH, 0, WELL_PITCH):
    for wy in (-WELL_PITCH/2, WELL_PITCH/2):
        body = body.cut(cyl(WELL_ID/2, WELL_DEPTH, wx, wy, PLATE_H - 1.5 - WELL_DEPTH))
plate = body.fuse(lid)
cam_hq = box(-19, -19, SENSOR_Z - 1.6, 19, 19, SENSOR_Z).fuse(cyl(15, 42, 0, 0, SENSOR_Z - 42))  # lens hangs down

objs = {}
for name, shp in [("frame", frame), ("tower", tower), ("door", door),
                  ("pad_ref", pad), ("plate_flipped_ref", plate), ("cam_hq", cam_hq)]:
    objs[name] = feature(name, shp)
feature("assembly_view", Part.makeCompound([o.Shape for o in objs.values()]))
doc.recompute()

names = list(objs)
inter = []
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        v = objs[names[i]].Shape.common(objs[names[j]].Shape).Volume
        if v > 1e-3: inter.append((names[i], names[j], round(v, 2)))
__result__ = {
    "volumes_mm3": {n: round(objs[n].Shape.Volume, 1) for n in ("frame", "tower", "door")},
    "bbox": {n: [round(c, 1) for c in (objs[n].Shape.BoundBox.XLength, objs[n].Shape.BoundBox.YLength, objs[n].Shape.BoundBox.ZLength)] for n in ("frame", "tower", "door")},
    "valid": {n: objs[n].Shape.isValid() for n in names},
    "interference": inter,
    "footprint_mm": [FOOT_X, round(Y_REAR - Y_FRONT, 1)],
    "overall_height_mm": round(SENSOR_Z + 5, 1),
}

"""
Clonogenic 6-well plate scanner — parametric FreeCAD build (runs inside the
AnkusDrive worker via run_script: exec(open(path).read(), globals())).

Frame: X along the plate's long axis, Y along the short axis, Z up.
Origin = plate centre; the plate skirt rests on the nest ledge at Z = 0.

Parts (all FDM, each fits a 180 x 120 bed footprint, tallest 160 mm):
  base     dark camera cavity (black PETG/ASA), HQ camera bosses on the floor
  deck     SBS plate nest + runway; aperture for the bottom camera
  hood     white light tent walls with a 45deg LED cove, front loading slot
  ceiling  white lid of the hood, top-camera hole + M2 pattern
  door     gravity/magnet front flap over the loading slot
Reference bodies (not printed): plate_ref, lid_ref, cam_hq, cam_top.
"""
import FreeCAD as App
import Part
from FreeCAD import Vector as V

if App.ActiveDocument is not None and App.ActiveDocument.Name.startswith("colony_scanner"):
    App.closeDocument(App.ActiveDocument.Name)
doc = App.newDocument("colony_scanner")

# ------------------------------------------------------------------ inputs
SRC = {
    "plate":    "ANSI/SLAS 1-2004 footprint 127.76 x 85.47 mm; 6-well height "
                "~20.3 mm body, ~22.5 mm with lid (Corning 3516 / Falcon 353046: VERIFY)",
    "wells":    "SLAS 4-2004 6-well: 39.12 mm pitch, wells at x=+-39.12,0; y=+-19.56; "
                "well ID ~34.8 mm (Corning 3516 growth area 9.5 cm2)",
    "hq_cam":   "Raspberry Pi HQ Camera: 38 x 38 mm PCB, 4 x M2.5 on a 30 x 30 mm "
                "square (VERIFY against the mechanical drawing), CS-mount, "
                "sensor 6.287 x 4.712 mm, 4056 x 3040 px",
    "lens6":    "Raspberry Pi 6 mm CS-mount lens (PT361060M3MP12): f=6 mm, F1.2-16",
    "cam3":     "Raspberry Pi Camera Module 3 (IMX708): 25 x 24 mm PCB, 4 x M2 on "
                "21 x 12.5 mm, HFOV 66deg, VFOV 41deg, 4608 x 2592 px, AF 10 cm-inf",
    "led":      "8 mm-wide 5 V COB LED strip, ~4 W/m, CRI>90, 5000 K",
}

PLATE_L, PLATE_W = 127.76, 85.47      # SBS footprint
PLATE_H = 22.5                        # with lid (upper bound of common 6-well plates)
WELL_ID, WELL_PITCH = 34.8, 39.12
CLR = 0.30                            # nest clearance per side

FOOT_X, FOOT_Y, WALL = 176.0, 118.0, 3.0
IN_X, IN_Y = FOOT_X - 2 * WALL, FOOT_Y - 2 * WALL       # 170 x 112 interior

# vertical stack (Z)
BASE_Z0, BASE_Z1 = -163.0, -3.0       # base box
FLOOR_T = 3.0
DECK_Z0, DECK_ZR, DECK_Z1 = -7.0, 0.0, 6.0   # spigot bottom, ledge (plate seat), rim top
HOOD_Z1 = 145.0                       # hood wall top; ceiling 145..148
CEIL_T = 3.0
SENSOR_Z = -150.0                     # HQ sensor plane -> 150 mm to plate seat
CAM_STANDOFF = 8.4                    # = SENSOR_Z - 1.6 (PCB) - floor top

APER_X, APER_Y = 121.0, 79.0          # bottom-camera aperture through the deck
POCKET_X, POCKET_Y = PLATE_L + 2 * CLR, PLATE_W + 2 * CLR
RUNWAY_HALF_X = 75.0                  # runway / slot half width (finger room)
SLOT_Z1 = 34.0                        # loading slot top (plate+lid 22.5 + clearance)

COVE_Z = 43.0                         # LED shelf top surface
COVE_W = 9.0                          # shelf width (8 mm strip + 1)
LIP_H, LIP_T = 6.0, 1.5

def box(x0, y0, z0, x1, y1, z1):
    return Part.makeBox(x1 - x0, y1 - y0, z1 - z0, V(x0, y0, z0))

def cyl(r, h, x, y, z, axis=V(0, 0, 1)):
    return Part.makeCylinder(r, h, V(x, y, z), axis)

def feature(name, shape):
    o = doc.addObject("Part::Feature", name)
    o.Shape = shape
    return o

# ------------------------------------------------------------------ base
base = box(-FOOT_X/2, -FOOT_Y/2, BASE_Z0, FOOT_X/2, FOOT_Y/2, BASE_Z1)
base = base.cut(box(-IN_X/2, -IN_Y/2, BASE_Z0 + FLOOR_T, IN_X/2, IN_Y/2, BASE_Z1 + 1))
floor_top = BASE_Z0 + FLOOR_T
# HQ camera bosses: PCB top = SENSOR_Z; PCB 1.6 thick on standoffs
boss_h = SENSOR_Z - 1.6 - floor_top
assert abs(boss_h - CAM_STANDOFF) < 1e-6, boss_h
for sx in (-15, 15):
    for sy in (-15, 15):
        base = base.fuse(cyl(3.0, boss_h, sx, sy, floor_top))
        base = base.cut(cyl(1.1, boss_h - 1.0, sx, sy, floor_top + 1.0))   # M2.5 self-tap / heat-set
# ribbon-cable slot through the rear wall at floor level
base = base.cut(box(-10, IN_Y/2 - 1, floor_top, 10, FOOT_Y/2 + 1, floor_top + 4))
# arcade button (24 mm) + status LED (5 mm) in the front wall
base = base.cut(cyl(12.2, WALL + 2, 60, -FOOT_Y/2 - 1, -45, V(0, 1, 0)))
base = base.cut(cyl(2.6, WALL + 2, 40, -FOOT_Y/2 - 1, -45, V(0, 1, 0)))
# Pi 5 mounting pattern (58 x 49) through the rear wall, Pi sits outside on standoffs
for px in (-49, 9):
    for pz in (-124.5, -75.5):
        base = base.cut(cyl(1.4, WALL + 2, px, IN_Y/2 - 1, pz, V(0, 1, 0)))
# LED-strip / top-camera wire pass-through near the top of the rear wall
base = base.cut(cyl(3.0, WALL + 2, 70, IN_Y/2 - 1, -12, V(0, 1, 0)))
base = base.removeSplitter()

# ------------------------------------------------------------------ deck
deck = box(-FOOT_X/2, -FOOT_Y/2, BASE_Z1, FOOT_X/2, FOOT_Y/2, DECK_Z1)
spig = box(-IN_X/2 + 0.2, -IN_Y/2 + 0.2, DECK_Z0, IN_X/2 - 0.2, IN_Y/2 - 0.2, BASE_Z1)
deck = deck.fuse(spig)
# aperture
deck = deck.cut(box(-APER_X/2, -APER_Y/2, DECK_Z0 - 1, APER_X/2, APER_Y/2, DECK_ZR))
# plate pocket, open toward the front (-Y): rear datum wall at +POCKET_Y/2
deck = deck.cut(box(-POCKET_X/2, -FOOT_Y/2 - 1, DECK_ZR, POCKET_X/2, POCKET_Y/2, DECK_Z1 + 1))
# runway: finger-wide, from the front edge to the pocket
deck = deck.cut(box(-RUNWAY_HALF_X, -FOOT_Y/2 - 1, DECK_ZR, RUNWAY_HALF_X, -POCKET_Y/2 + 6, DECK_Z1 + 1))
# 45deg lead-in on the front ends of the side walls (3 mm)
for s in (1, -1):
    tri = Part.makePolygon([V(s*POCKET_X/2, -POCKET_Y/2 + 6, DECK_ZR - 1),
                            V(s*(POCKET_X/2 + 3), -POCKET_Y/2 + 6, DECK_ZR - 1),
                            V(s*POCKET_X/2, -POCKET_Y/2 + 9, DECK_ZR - 1),
                            V(s*POCKET_X/2, -POCKET_Y/2 + 6, DECK_ZR - 1)])
    deck = deck.cut(Part.Face(tri).extrude(V(0, 0, DECK_Z1 - DECK_ZR + 2)))
# hood locating groove (3 sides + rear) in the rim top: labyrinth light seal
g_o, g_i, g_d = 3.0, 1.0, 2.0      # groove from 1..3 mm inboard of the interior wall line
groove = box(-IN_X/2 + g_i, -IN_Y/2 + g_i, DECK_Z1 - g_d, IN_X/2 - g_i, IN_Y/2 - g_i, DECK_Z1 + 1)
groove = groove.cut(box(-IN_X/2 + g_o, -IN_Y/2 + g_o, DECK_Z1 - g_d - 1, IN_X/2 - g_o, IN_Y/2 - g_o, DECK_Z1 + 2))
deck = deck.cut(groove)
# magnet pockets for the door in the deck front face: none (magnets live in the hood wall)
deck = deck.removeSplitter()

# ------------------------------------------------------------------ hood walls
hood = box(-FOOT_X/2, -FOOT_Y/2, DECK_Z1, FOOT_X/2, FOOT_Y/2, HOOD_Z1)
hood = hood.cut(box(-IN_X/2, -IN_Y/2, DECK_Z1 - 1, IN_X/2, IN_Y/2, HOOD_Z1 + 1))
# tongue into the deck groove (0.3 mm clearance) on all four sides
tongue = box(-IN_X/2 + g_i + 0.3, -IN_Y/2 + g_i + 0.3, DECK_Z1 - g_d + 0.4, IN_X/2 - g_i - 0.3, IN_Y/2 - g_i - 0.3, DECK_Z1)
tongue = tongue.cut(box(-IN_X/2 + g_o - 0.3, -IN_Y/2 + g_o - 0.3, DECK_Z1 - g_d - 1, IN_X/2 - g_o + 0.3, IN_Y/2 - g_o + 0.3, DECK_Z1 + 1))
hood = hood.fuse(tongue)
# loading slot in the front wall; the cut reaches 8 mm inboard so the seal tongue is
# removed across the runway too (a 2 mm ridge at Z=+4 would block the plate)
hood = hood.cut(box(-RUNWAY_HALF_X, -FOOT_Y/2 - 1, DECK_Z1 - g_d - 1, RUNWAY_HALF_X, -IN_Y/2 + 8, SLOT_Z1))
# LED cove: right-triangle shelf, LED face up at COVE_Z, 45deg underside (support-free)
def cove_prism(along_x, sign):
    # profile in (n, z): n = 0 at the wall, positive inward
    prof = [(0, COVE_Z - COVE_W), (0, COVE_Z), (COVE_W, COVE_Z)]
    if along_x:
        pts = [V(-IN_X/2 - 1, sign * (IN_Y/2 - n), z) for n, z in prof]
        f = Part.Face(Part.makePolygon(pts + [pts[0]]))
        return f.extrude(V(IN_X + 2, 0, 0))
    pts = [V(sign * (IN_X/2 - n), -IN_Y/2 - 1, z) for n, z in prof]
    f = Part.Face(Part.makePolygon(pts + [pts[0]]))
    return f.extrude(V(0, IN_Y + 2, 0))
for s in (1, -1):
    hood = hood.fuse(cove_prism(True, s)).fuse(cove_prism(False, s))
# lip on the inner edge of the shelf
lip = box(-IN_X/2 + COVE_W - LIP_T, -IN_Y/2 + COVE_W - LIP_T, COVE_Z, IN_X/2 - COVE_W + LIP_T, IN_Y/2 - COVE_W + LIP_T, COVE_Z + LIP_H)
lip = lip.cut(box(-IN_X/2 + COVE_W, -IN_Y/2 + COVE_W, COVE_Z - 1, IN_X/2 - COVE_W, IN_Y/2 - COVE_W, COVE_Z + LIP_H + 1))
hood = hood.fuse(lip)
# trim anything that poked outside the wall envelope
hood = hood.common(box(-FOOT_X/2, -FOOT_Y/2, DECK_Z0, FOOT_X/2, FOOT_Y/2, HOOD_Z1))
# ceiling screw bosses in the top corners (M3)
for sx in (1, -1):
    for sy in (1, -1):
        b = box(min(sx*IN_X/2, sx*(IN_X/2 - 9)), min(sy*IN_Y/2, sy*(IN_Y/2 - 9)), HOOD_Z1 - 12,
                max(sx*IN_X/2, sx*(IN_X/2 - 9)), max(sy*IN_Y/2, sy*(IN_Y/2 - 9)), HOOD_Z1)
        hood = hood.fuse(b)
        hood = hood.cut(cyl(1.3, 10, sx*(IN_X/2 - 4.5), sy*(IN_Y/2 - 4.5), HOOD_Z1 - 10))
# LED wire exit through the rear wall just above the shelf
hood = hood.cut(cyl(3.0, WALL + 2, 70, IN_Y/2 - 1, COVE_Z + 2, V(0, 1, 0)))
# door hinge lugs on the front face, either side of the slot, pin along X at Z=44, Y=-61.5
for s in (1, -1):
    # lugs sit OUTBOARD of the 160 mm door (X 80.5..85) so the 45deg support wedge cannot clip it
    lug = box(min(s*80.5, s*85), -FOOT_Y/2 - 5, 40, max(s*80.5, s*85), -FOOT_Y/2, 48)
    wedge = Part.Face(Part.makePolygon([V(0, -FOOT_Y/2, 35), V(0, -FOOT_Y/2 - 5, 40), V(0, -FOOT_Y/2, 40), V(0, -FOOT_Y/2, 35)]))
    wedge = wedge.extrude(V(4.5, 0, 0)); wedge.translate(V(min(s*80.5, s*85), 0, 0))
    hood = hood.fuse(lug).fuse(wedge)
    hood = hood.cut(cyl(1.0, 5.2, s*80.4 if s > 0 else -85.1, -FOOT_Y/2 - 2.5, 44, V(1, 0, 0)))
# door magnets: pockets in the front face at X=+-77.5, Z=+20 (d6 x 2 N35)
for s in (1, -1):
    hood = hood.cut(cyl(3.1, 2.0, s*77.5, -FOOT_Y/2 - 0.01, 20, V(0, 1, 0)))     # 1.0 mm wall left
hood = hood.removeSplitter()

# ------------------------------------------------------------------ ceiling
ceil_ = box(-FOOT_X/2, -FOOT_Y/2, HOOD_Z1, FOOT_X/2, FOOT_Y/2, HOOD_Z1 + CEIL_T)
ceil_ = ceil_.cut(cyl(6.0, CEIL_T + 2, 0, 0, HOOD_Z1 - 1))                 # top-camera lens hole
for sx in (1, -1):
    for sy in (1, -1):
        ceil_ = ceil_.cut(cyl(1.0, CEIL_T + 2, sx*10.5, sy*6.25, HOOD_Z1 - 1))     # Camera Module 3 M2
        ceil_ = ceil_.cut(cyl(1.7, CEIL_T + 2, sx*(IN_X/2 - 4.5), sy*(IN_Y/2 - 4.5), HOOD_Z1 - 1))  # M3 to hood
ceil_ = ceil_.cut(box(-12, 12.5, HOOD_Z1 - 1, 12, 15.5, HOOD_Z1 + CEIL_T + 1))  # ribbon slot next to the module
ceil_ = ceil_.removeSplitter()

# ------------------------------------------------------------------ door
door = box(-80, -FOOT_Y/2 - 3.5, -2, 80, -FOOT_Y/2 - 0.5, 40)
for s in (1, -1):   # ears = the door's outer 4.5 mm rising to the hinge line, inboard of the hood lugs
    door = door.fuse(box(min(s*75.5, s*80), -FOOT_Y/2 - 3.5, 38, max(s*75.5, s*80), -FOOT_Y/2 - 0.5, 48))
    door = door.cut(cyl(1.0, 12, s*74.5 if s > 0 else -86.5, -FOOT_Y/2 - 2.0, 44, V(1, 0, 0)))
    door = door.cut(cyl(3.1, 2.0, s*77.5, -FOOT_Y/2 - 0.5 - 2.0 + 0.01, 20, V(0, 1, 0)))   # magnet, 1.0 mm wall left
# finger pull
door = door.cut(cyl(8, 5, 0, -FOOT_Y/2 - 5, 8, V(0, 1, 0)))
door = door.removeSplitter()

# ------------------------------------------------------------------ reference bodies
plate = box(-PLATE_L/2, -PLATE_W/2, 0, PLATE_L/2, PLATE_W/2, PLATE_H - 2.5)
for wx in (-WELL_PITCH, 0, WELL_PITCH):
    for wy in (-WELL_PITCH/2, WELL_PITCH/2):
        plate = plate.cut(cyl(WELL_ID/2, 30, wx, wy, 1.5))
lid = box(-PLATE_L/2 - 0.4, -PLATE_W/2 - 0.4, PLATE_H - 2.5, PLATE_L/2 + 0.4, PLATE_W/2 + 0.4, PLATE_H)
cam_hq = box(-19, -19, SENSOR_Z - 1.6, 19, 19, SENSOR_Z).fuse(cyl(15, 12.5, 0, 0, SENSOR_Z)).fuse(cyl(15, 30, 0, 0, SENSOR_Z + 12.5))
cam_top = box(-12.5, -12, HOOD_Z1 + CEIL_T + 2, 12.5, 12, HOOD_Z1 + CEIL_T + 3.6)

objs = {}
for name, shp in [("base", base), ("deck", deck), ("hood", hood), ("ceiling", ceil_),
                  ("door", door), ("plate_ref", plate), ("lid_ref", lid),
                  ("cam_hq", cam_hq), ("cam_top", cam_top)]:
    objs[name] = feature(name, shp)

# full assembly compound for rendering; and a cutaway (x > 0 removed) to see inside
asm = Part.makeCompound([o.Shape for o in objs.values()])
feature("assembly_view", asm)
half = box(-200, -200, -200, 0, 200, 200)
cut_parts = [o.Shape.common(half) for o in objs.values()]
feature("assembly_cutaway", Part.makeCompound([s for s in cut_parts if not s.isNull()]))
doc.recompute()

# interference audit between the printed/bought parts (mm^3)
names = ["base", "deck", "hood", "ceiling", "door", "plate_ref", "lid_ref", "cam_hq", "cam_top"]
inter = []
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        v = objs[names[i]].Shape.common(objs[names[j]].Shape).Volume
        if v > 1e-3:
            inter.append((names[i], names[j], round(v, 2)))

__result__ = {
    "volumes_mm3": {n: round(objs[n].Shape.Volume, 1) for n in ["base", "deck", "hood", "ceiling", "door"]},
    "bbox": {n: [round(c, 1) for c in (objs[n].Shape.BoundBox.XLength, objs[n].Shape.BoundBox.YLength, objs[n].Shape.BoundBox.ZLength)]
             for n in ["base", "deck", "hood", "ceiling", "door"]},
    "valid": {n: objs[n].Shape.isValid() for n in names},
    "interference": inter,
    "overall_height_mm": round(HOOD_Z1 + CEIL_T + 3.6 - BASE_Z0, 1),
}

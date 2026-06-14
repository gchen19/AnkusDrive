"""A REAL 3-speed constant-mesh countershaft gearbox — the multi-speed assembly with
all the engagement hardware, built and exported.

Architecture (textbook manual transmission):
  * INPUT shaft (axis A, front) carries one gear, keyed -> constant-mesh to the
    countershaft.
  * COUNTERSHAFT (axis B) carries the constant-mesh gear + one keyed gear per speed.
  * MAINSHAFT (axis A, rear, COAXIAL with the input) carries the speed gears, each
    FREEWHEELING and permanently meshed with its countershaft gear, plus dog teeth.
  * SLIDING DOG COLLARS splined to the mainshaft lock a chosen speed gear to it.

Power for speed k:  input -(constant mesh)-> countershaft -(speed mesh k)->
freewheeling main gear k -(dog collar engaged)-> mainshaft.
Overall ratio_k = (Z_in/Z_cm) * (Z_ck/Z_mk).

All pairs mesh at one centre distance C (tooth-sum 2C/M = 40). The render shows the
low-gear collar ENGAGED (speed 1); the high collar sits in neutral.

  .venv/bin/python3 scratch/gearbox_multispeed.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from driftpin import Worker  # noqa: E402

ART = REPO / "artifacts"
M = 2.0
AX_A, AX_B = 0.0, 40.0          # mainshaft/input axis, countershaft axis; C = 40
GH = 6.0                        # gear face width
Z_CM, Z0, Z1, Z2 = 60.0, 44.0, 28.0, 12.0   # axial planes (constant-mesh, speeds 1-3)
SR = 5.0

# teeth (every pair sums to 40 -> meshes at C=40): constant-mesh + 3 speeds
T_IN, T_CM = 16, 24
SPEEDS = [(12, 28), (20, 20), (28, 12)]       # (countershaft, mainshaft) per speed


def overall_ratios():
    return [(T_IN / T_CM) * (tc / tm) for tc, tm in SPEEDS]


BUILD = r"""
import Part, math, os
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
doc = App.ActiveDocument
M, SR, GH = %f, %f, %f
AX_A, AX_B = %f, %f
Z_CM, Z0, Z1, Z2 = %f, %f, %f, %f
names = %r            # [in, cm, c0, m0, c1, m1, c2, m2]
zsp = [Z0, Z1, Z2]

def dflat(solid, depth=1.0):
    return solid.cut(Part.makeBox(60, 60, 600, Vector(SR - depth, -30, -300)))

def dbore(solid, r, h, z0, depth=1.0):       # D-hole (keyed)
    cyl = Part.makeCylinder(r, h, Vector(0, 0, z0)).cut(
        Part.makeBox(60, 60, h + 2, Vector(r - depth, -30, z0 - 1)))
    return solid.cut(cyl)

def rbore(solid, r, h, z0):                  # round hole (freewheel)
    return solid.cut(Part.makeCylinder(r, h, Vector(0, 0, z0)))

def dogs(radius, z0, h, phase, n=6):
    out = []
    tw = 0.45 * (2 * math.pi * radius / n)
    for k in range(n):
        th = phase + 2 * math.pi * k / n
        b = Part.makeBox(3.0, tw, h, Vector(-1.5, -tw / 2, 0))
        b.Placement = Placement(Vector(radius * math.cos(th), radius * math.sin(th), z0),
                                Rotation(Vector(0, 0, 1), math.degrees(th)))
        out.append(b)
    return out

g = {nm: doc.getObject(nm).Shape for nm in names}
parts = []   # (label, shape_at_origin, x, z)

# --- input shaft + constant-mesh gear (keyed) --------------------------------
parts.append(("input_shaft", dflat(Part.makeCylinder(SR, 34, Vector(0, 0, Z_CM - 6))), AX_A, 0))
parts.append(("g_in", dbore(g[names[0]], SR + 0.1, GH + 2, -1), AX_A, Z_CM))

# --- countershaft + its keyed gears (constant-mesh + one per speed) -----------
cshaft = dflat(Part.makeCylinder(SR, Z_CM + 12, Vector(0, 0, Z2 - 6)))
parts.append(("countershaft", cshaft, AX_B, 0))
parts.append(("g_cm", dbore(g[names[1]], SR + 0.1, GH + 2, -1), AX_B, Z_CM))
for i, nm in enumerate((names[2], names[4], names[6])):
    parts.append((f"g_c{i}", dbore(g[nm], SR + 0.1, GH + 2, -1), AX_B, zsp[i]))

# --- mainshaft (coaxial, rear) + freewheeling speed gears with dog teeth ------
parts.append(("mainshaft", dflat(Part.makeCylinder(SR, Z0 + 4, Vector(0, 0, Z2 - 6))), AX_A, 0))
for i, nm in enumerate((names[3], names[5], names[7])):
    gear = rbore(g[nm], SR + 0.3, GH + 2, -1)                 # round bore -> FREE
    gear = gear.fuse(dogs(SR + 2.5, GH, 4.0, 0.0))            # dog teeth on top face
    parts.append((f"g_m{i}", gear, AX_A, zsp[i]))

# --- two sliding dog collars splined to the mainshaft ------------------------
# low collar ENGAGED with speed-1 gear (dogs interlock just above g_m0);
# high collar in NEUTRAL (slid clear above g_m2).
def collar(engaged_z, dog_z):
    c = dbore(Part.makeCylinder(SR + 4.0, 6.0, Vector(0, 0, engaged_z)), SR + 0.1, 8.0, engaged_z - 1)
    return c.fuse(dogs(SR + 2.5, dog_z, 4.0, math.pi / 6))
parts.append(("collar_low", collar(Z0 + 6, Z0 + GH), AX_A, 0))      # engaged: dogs meet g_m0's
parts.append(("collar_high", collar(Z2 - 12, Z2 - 12), AX_A, 0))    # neutral: slid clear

solids = []
for label, shp, x, z in parts:
    s = shp.copy(); s.translate(Vector(x, 0, z))
    o = doc.addObject("Part::Feature", label); o.Shape = s
    solids.append(s)
doc.recompute()
asm = Part.Compound(solids)
asm.exportStep(%r)
import Mesh
Mesh.Mesh(asm.tessellate(0.25)).write(%r)
__result__ = [os.path.getsize(%r), os.path.getsize(%r)]
"""


def main():
    ART.mkdir(parents=True, exist_ok=True)
    step, stl = ART / "gearbox_multispeed.step", ART / "gearbox_multispeed.stl"
    teeth = [T_IN, T_CM, SPEEDS[0][0], SPEEDS[0][1], SPEEDS[1][0], SPEEDS[1][1],
             SPEEDS[2][0], SPEEDS[2][1]]
    with Worker() as w:
        w.call("new_document", name="msbox")
        names = []
        for tag, t in zip(("g_in", "g_cm", "g_c0", "g_m0", "g_c1", "g_m1", "g_c2", "g_m2"),
                          teeth):
            r = w.call("add_gear", teeth=t, module=M, height=GH, name=tag)
            names.append(r["name"])
        sizes = w.call("run_script", code=BUILD % (
            M, SR, GH, AX_A, AX_B, Z_CM, Z0, Z1, Z2, names,
            str(step), str(stl), str(step), str(stl)))["result"]
    rr = overall_ratios()
    print("== 3-speed constant-mesh countershaft gearbox ==")
    print(f"  constant mesh: {T_IN}T->{T_CM}T   speeds (counter->main): {SPEEDS}")
    print("  overall ratios: " + "  ".join(
        f"speed{i+1}={r:.3f}" for i, r in enumerate(rr)))
    print(f"  STEP -> {step}  ({sizes[0]:,} bytes)")
    print(f"  STL  -> {stl}  ({sizes[1]:,} bytes)")
    try:
        sys.path.insert(0, str(REPO / "scratch"))
        from render_gearbox import render
        png = ART / "gearbox_multispeed_render.png"
        tris, _ = render(stl, png, "3-speed countershaft gearbox (low gear engaged)")
        print(f"  PNG  -> {png}  ({tris:,} facets)")
    except Exception as e:
        print(f"  (render skipped: {e})")


if __name__ == "__main__":
    main()

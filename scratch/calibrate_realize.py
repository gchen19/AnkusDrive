"""Calibrate the geometry-realizes-declaration signals on REAL CAD.

The kickoff (docs/KICKOFF_validate_the_artifact.md) demands we treat out-of-family
numbers as red flags — which presupposes we know the family. This builds the engaged
dog clutch two ways and prints the discriminating signals so the §11.10 gate
thresholds are grounded in measured geometry, not guessed:

  * GOOD  — the FIXED collar (gearbox_multispeed.py): sleeve raised ABOVE the dog
            band so the dog teeth protrude into open space; the gear's teeth enter
            the gaps half-pitch offset.
  * BAD   — the bug: a SOLID disc fills the dog band (a solid face where the gaps
            belong); the gear's teeth jam into it and it cannot interlock.

Both engaged coaxially with the same freewheeling output gear (dog teeth up).
Signals measured on the EXPORTED collar + gear shapes:
  1. engaged overlap volume   collar.common(gear).Volume
  2. dog-band fill fraction    — at the collar's dog radius/z-band, fraction of
     angular samples inside collar material (≈0.5 interleaved, ≈1.0 solid face)
  3. bore keying               — radial flat (D-key) present, or round bore

  .venv/bin/python3 scratch/calibrate_realize.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from driftpin import Worker  # noqa: E402

M = 2.0
NIN, NOUT = 12, 24
SR = 5.0
GH = 6.0
ND = 6

BUILD = r"""
import Part, math
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
doc = App.ActiveDocument
M, SR, GH, ND = %f, %f, %f, %d
og = doc.getObject(%r).Shape

def dbore(solid, r, h, z0, depth=1.0):       # D-hole (keyed)
    cyl = Part.makeCylinder(r, h, Vector(0,0,z0)).cut(
        Part.makeBox(60,60,h+2, Vector(r-depth,-30,z0-1)))
    return solid.cut(cyl)

def dogs(radius, z0, h, phase, n=ND):
    out = []
    tw = 0.45 * (2*math.pi*radius/n)
    for k in range(n):
        th = phase + 2*math.pi*k/n
        b = Part.makeBox(3.0, tw, h, Vector(-1.5, -tw/2, 0))
        b.Placement = Placement(Vector(radius*math.cos(th), radius*math.sin(th), z0),
                                Rotation(Vector(0,0,1), math.degrees(th)))
        out.append(b)
    return out

# freewheeling output gear: round bore + dog teeth up at z in [0,4], phase 0
gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))
gear = gear.fuse(dogs(SR+2.5, 0.0, 4.0, 0.0))

# GOOD collar (gearbox_multispeed.collar): sleeve raised to z in [4,10] (D-bore),
# dogs at z in [0,4.5] half-pitch -> protrude into open space, interleave the gear
good = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,4.0)), SR+0.1, 8.0, 3.0)
good = good.fuse(dogs(SR+2.5, 0.0, 4.5, math.pi/ND))

# BAD collar: same raised sleeve, but a SOLID disc fills the dog band [0,4.5]
bad = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,4.0)), SR+0.1, 8.0, 3.0)
bad = bad.fuse(Part.makeCylinder(SR+4.0, 4.5, Vector(0,0,0.0)))   # solid face

for nm, shp in (("gear", gear), ("collar_good", good), ("collar_bad", bad)):
    o = doc.addObject("Part::Feature", nm); o.Shape = shp.copy()
doc.recompute()

gs = doc.getObject("gear").Shape
def overlap(nm):
    try:
        return round(doc.getObject(nm).Shape.common(gs).Volume, 3)
    except Exception:
        return -1.0

def band_fill(nm, n=240):
    shp = doc.getObject(nm).Shape
    rad, z = SR + 2.5, 2.0       # collar dog radius, middle of the dog band
    inside = sum(1 for k in range(n)
                 if shp.isInside(Vector(rad*math.cos(2*math.pi*k/n),
                                        rad*math.sin(2*math.pi*k/n), z), 1e-6, True))
    return round(inside/n, 3)

def has_radial_flat(nm, bore_r):
    shp = doc.getObject(nm).Shape
    for f in shp.Faces:
        if type(f.Surface).__name__ != "Plane":
            continue
        pr = f.ParameterRange
        n = f.normalAt((pr[0]+pr[1])/2.0, (pr[2]+pr[3])/2.0)
        if abs(n.z) < 0.2:                       # radial (horizontal) normal
            cm = f.CenterOfMass
            if math.hypot(cm.x, cm.y) < bore_r + 1.5:
                return True
    return False

__result__ = {
    "overlap_good": overlap("collar_good"),
    "overlap_bad":  overlap("collar_bad"),
    "fill_good":    band_fill("collar_good"),
    "fill_bad":     band_fill("collar_bad"),
    "gear_has_flat":  has_radial_flat("gear", SR+0.3),
    "good_has_flat":  has_radial_flat("collar_good", SR+0.1),
    "bad_has_flat":   has_radial_flat("collar_bad",  SR+0.1),
}
"""


def main():
    with Worker() as w:
        w.call("new_document", name="cal")
        gout = w.call("add_gear", teeth=NOUT, module=M, height=GH, name="gout")
        r = w.call("run_script", code=BUILD % (
            M, SR, GH, ND, gout["name"]))["result"]
    print("== geometry-realizes-declaration signal calibration ==\n")
    print("  signal 1 — engaged overlap volume (collar.common(gear)):")
    print(f"      GOOD  interleaved : {r['overlap_good']:>8} mm³")
    print(f"      BAD   solid face  : {r['overlap_bad']:>8} mm³")
    print("\n  signal 2 — dog-band fill fraction (≈0.5 interleaved / ≈1.0 solid):")
    print(f"      GOOD  interleaved : {r['fill_good']:>8}")
    print(f"      BAD   solid face  : {r['fill_bad']:>8}")
    print("\n  signal 3 — bore keying (radial flat present == D-keyed):")
    print(f"      gear (freewheel, want round/no-flat): flat={r['gear_has_flat']}")
    print(f"      collar GOOD (keyed, want flat)      : flat={r['good_has_flat']}")
    print(f"      collar BAD  (keyed, want flat)      : flat={r['bad_has_flat']}")


if __name__ == "__main__":
    main()

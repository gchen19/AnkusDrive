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

# freewheeling output gear: round bore, body at z in [0,GH]; dog teeth ABOVE the body
# at z in [GH, GH+4], phase 0 — so the engagement band is open space, as in the real CAD
# (gear dogs fused INTO the solid body would read solid at any angle and defeat the
# two-part interleave measurement).
gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))
gear = gear.fuse(dogs(SR+2.5, GH, 4.0, 0.0))

# GOOD collar (gearbox_multispeed.collar): sleeve raised to z in [GH+4, GH+10] (D-bore),
# dogs at z in [GH, GH+4.5] half-pitch -> protrude into open space, interleave the gear
good = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)
good = good.fuse(dogs(SR+2.5, GH, 4.5, math.pi/ND))

# BAD collar: same raised sleeve, but a SOLID disc fills the dog band [GH, GH+4.5]
bad = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)
bad = bad.fuse(Part.makeCylinder(SR+4.0, 4.5, Vector(0,0,GH)))   # solid face

# IN-PHASE collar: real gaps (like GOOD) but dogs at phase 0 -> teeth-on-teeth with the
# gear; passes the per-part gap check yet jams. Calibrates the relative-phase signal.
inphase = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)
inphase = inphase.fuse(dogs(SR+2.5, GH, 4.5, 0.0))

for nm, shp in (("gear", gear), ("collar_good", good), ("collar_bad", bad),
                ("collar_inphase", inphase)):
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
    rad, z = SR + 2.5, GH + 2.0  # collar dog radius, middle of the dog band (above the body)
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
            # strict < bore_r, as realize.bore_keying does: a keyway chord sits INSIDE
            # the bore wall, while dog-tooth end faces sit OUTSIDE it (a looser margin
            # would falsely flag the dog teeth as a keyway).
            if math.hypot(cm.x, cm.y) < bore_r:
                return True
    return False

def both_frac(nm, n=240):                        # angular fraction where collar AND gear
    shp = doc.getObject(nm).Shape                # both have material -> teeth in phase
    rad, z = SR + 2.5, GH + 2.0
    def pt(k): return Vector(rad*math.cos(2*math.pi*k/n), rad*math.sin(2*math.pi*k/n), z)
    both = sum(1 for k in range(n) if shp.isInside(pt(k), 1e-6, True)
               and gs.isInside(pt(k), 1e-6, True))
    return round(both/n, 3)

__result__ = {
    "overlap_good": overlap("collar_good"),
    "overlap_bad":  overlap("collar_bad"),
    "fill_good":    band_fill("collar_good"),
    "fill_bad":     band_fill("collar_bad"),
    "fill_inphase": band_fill("collar_inphase"),
    "both_good":    both_frac("collar_good"),
    "both_inphase": both_frac("collar_inphase"),
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
    print(f"      IN-PHASE (gaps!)  : {r['fill_inphase']:>8}   <- passes the gap check")
    print("\n  signal 2b — interleave: angular fraction where collar AND gear (≈0 meshed"
          " / ≈tooth-fill in phase):")
    print(f"      GOOD  half-pitch  : {r['both_good']:>8}   (teeth fall in the gaps)")
    print(f"      IN-PHASE          : {r['both_inphase']:>8}   <- teeth-on-teeth, jams")
    print("\n  signal 3 — bore keying (radial flat present == D-keyed):")
    print(f"      gear (freewheel, want round/no-flat): flat={r['gear_has_flat']}")
    print(f"      collar GOOD (keyed, want flat)      : flat={r['good_has_flat']}")
    print(f"      collar BAD  (keyed, want flat)      : flat={r['bad_has_flat']}")


if __name__ == "__main__":
    main()

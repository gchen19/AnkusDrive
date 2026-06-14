"""Validate the ARTIFACT of scratch/dog_clutch_unit.py against the §11.10 oracle.

The calibration finding (docs/VALIDATE_THE_ARTIFACT.md) caught a second instance of
the solid-face bug: dog_clutch_unit.py's collar sleeve was SOLID over the whole dog
band, so the output gear's teeth jam into it regardless of dog phase — it cannot
interlock, exactly the bug the arc is about. This probe builds the freewheeling output
gear plus BOTH collar designs and runs the real driftpin.realize oracle (not an ad-hoc
inline measurement) on each, so the bug and its fix are judged on the as-built metal:

  * BUG    — solid cylinder body across the dog band  -> dog-band fill ≈ 1.0, jam.
  * FIXED  — sleeve raised ABOVE the band (mirrors gearbox_multispeed.collar) so only
             interleaving dog teeth occupy it -> fill ≈ 0.5, in-family overlap.

It then reads the EXPORTED artifacts/dog_clutch_unit.step back (the real handoff file,
not a re-build) and asserts the as-shipped collar realizes the declaration — closing
the loop on the actual artifact, which is the whole point of the §11.10 arc.

  .venv/bin/python3 scratch/verify_dog_clutch_unit.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from driftpin import Worker  # noqa: E402

# geometry shared with scratch/dog_clutch_unit.py
M = 2.0
NOUT = 24
SR = 5.0          # shaft radius
GH = 6.0          # gear face width
DOG_H = 5.0       # dog tooth height (axial)
ND = 6            # dog teeth
RDOG = SR + 3.0   # dog pitch radius = 8.0

BUILD = r"""
import Part, math
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
from driftpin import realize
doc = App.ActiveDocument
SR, GH, DOG_H, ND, RDOG = %f, %f, %f, %d, %f
og = doc.getObject(%r).Shape

def dhole(solid, r, depth, h, z0):    # D-shaped bore (matches a D-shaft)
    cyl = Part.makeCylinder(r, h, Vector(0,0,z0))
    cyl = cyl.cut(Part.makeBox(40,40,h+2, Vector(r-depth,-20,z0-1)))
    return solid.cut(cyl)

def dogs(radius, z0, h, phase):       # ND axial dog teeth (a ring of boxes)
    out = []
    tw = 0.45 * (2*math.pi*radius/ND)
    for k in range(ND):
        th = phase + 2*math.pi*k/ND
        b = Part.makeBox(3.0, tw, h, Vector(-1.5, -tw/2, 0))
        b.Placement = Placement(Vector(radius*math.cos(th), radius*math.sin(th), z0),
                                Rotation(Vector(0,0,1), math.degrees(th)))
        out.append(b)
    return out

# freewheeling output gear: round bore + dog teeth up at z in [GH, GH+DOG_H], phase 0
gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))
gear = gear.fuse(dogs(RDOG, GH, DOG_H, 0.0))

# BUG collar (the shipped dog_clutch_unit.py): a SOLID cylinder body fills the dog band
bad = Part.makeCylinder(SR+5.0, 6.0, Vector(0,0,GH))
bad = dhole(bad, SR+0.1, 1.0, 8.0, GH-1)
bad = bad.fuse(dogs(RDOG, GH, DOG_H, math.pi/ND))

# FIXED collar: sleeve raised ABOVE the dog band so the dogs protrude into open space
good = Part.makeCylinder(SR+5.0, 6.0, Vector(0,0,GH+DOG_H))
good = dhole(good, SR+0.1, 1.0, 8.0, GH+DOG_H-1)
good = good.fuse(dogs(RDOG, GH, DOG_H+0.5, math.pi/ND))

ring = {"dog_radius_mm": RDOG, "z_lo_mm": GH+0.5, "z_hi_mm": GH+DOG_H-0.5, "n_dogs": ND}
bore = {"keyed": True, "bore_radius_mm": SR+0.1}
gbore = {"keyed": False, "bore_radius_mm": SR+0.3}

def report(collar):
    return {
        "dog_ring": realize.check_dog_ring(collar, {"role": "collar", **ring}),
        "bore": realize.check_bore_keying(collar, {"role": "collar", **bore}),
        "overlap_mm3": round(collar.common(gear).Volume, 3),
        "profile": realize.dog_ring_profile(collar, RDOG, GH+0.5, GH+DOG_H-0.5),
    }

__result__ = {
    "BUG": report(bad),
    "FIXED": report(good),
    "gear_bore": realize.check_bore_keying(gear, {"role": "gear", **gbore}),
}
"""


# --- read the EXPORTED STEP back and validate the as-shipped collar -----------
STEP = REPO / "artifacts" / "dog_clutch_unit.step"
CHECK_STEP = r"""
import Part
from driftpin import realize
SR, GH, DOG_H, ND, RDOG = %f, %f, %f, %d, %f
shape = Part.Shape(); shape.read(%r)
sols = shape.Solids
def bb(s): return s.BoundBox
# collar: small XY footprint (dia ~20), starts at the dog band (ZMin ~ GH) — not the
# big output gear (OD ~52, ZMin ~0) nor the tall shaft (ZMin ~ -20).
collar = next(s for s in sols if 15 <= bb(s).XLength <= 25 and 5 <= bb(s).ZMin <= 7)
cx = bb(collar).Center.x
gear = next(s for s in sols if bb(s).XLength > 40 and -1 <= bb(s).ZMin <= 1
            and abs(bb(s).Center.x - cx) < 5)
viol = (realize.check_dog_ring(collar, {"role": "collar", "dog_radius_mm": RDOG,
            "z_lo_mm": GH+0.5, "z_hi_mm": GH+DOG_H-0.5, "n_dogs": ND})
        + realize.check_bore_keying(collar, {"role": "collar", "keyed": True,
            "bore_radius_mm": SR+0.1})
        + realize.check_bore_keying(gear, {"role": "gear", "keyed": False,
            "bore_radius_mm": SR+0.3}))
__result__ = {"n_solids": len(sols), "overlap_mm3": round(collar.common(gear).Volume, 3),
              "profile": realize.dog_ring_profile(collar, RDOG, GH+0.5, GH+DOG_H-0.5),
              "viol": viol}
"""


def main():
    with Worker() as w:
        w.call("new_document", name="verify")
        gout = w.call("add_gear", teeth=NOUT, module=M, height=GH, name="gout")
        r = w.call("run_script", code=BUILD % (
            SR, GH, DOG_H, ND, RDOG, gout["name"]))["result"]

        print("== validate the artifact: dog_clutch_unit collar (§11.10 oracle) ==\n")
        print("  -- bug vs fix, on the as-built collar geometry --")
        for tag in ("BUG", "FIXED"):
            d = r[tag]
            viol = d["dog_ring"] + d["bore"]
            print(f"  {tag:5} collar : dog_band fill={d['profile']['fill']}  "
                  f"sectors={d['profile']['sectors']}  overlap={d['overlap_mm3']} mm³  "
                  f"-> {'PASS' if not viol else 'FAIL'}")
            for v in viol:
                print(f"          ! {v.get('reason','')}")
        print(f"\n  gear bore_keying (freewheel, want round): "
              f"{'PASS' if not r['gear_bore'] else 'FAIL ' + str(r['gear_bore'])}")

        if STEP.exists():
            s = w.call("run_script", code=CHECK_STEP % (
                SR, GH, DOG_H, ND, RDOG, str(STEP)))["result"]
            print(f"\n  -- the EXPORTED {STEP.name} ({s['n_solids']} solids) --")
            print(f"  shipped collar: dog_band fill={s['profile']['fill']}  "
                  f"sectors={s['profile']['sectors']}  overlap={s['overlap_mm3']} mm³  "
                  f"-> {'PASS — exported metal realizes the declaration' if not s['viol'] else 'FAIL'}")
            for v in s["viol"]:
                print(f"          ! {v.get('reason','')}")
        else:
            print(f"\n  ({STEP.name} not present — run scratch/dog_clutch_unit.py to export it)")


if __name__ == "__main__":
    main()

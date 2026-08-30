"""Build a real single-stage dog-clutch gear unit — the engagement hardware the
earlier 'functional gearbox' lacked — and export its CAD.

What makes it actually functional (vs loose gears on plain rods):
  * input gear KEYED to the input shaft by a D-flat (a real torque path);
  * output gear FREEWHEELS on the output shaft (round bore) and carries dog teeth;
  * a dog COLLAR keyed to the output shaft by a D-flat (so it drives the shaft) is
    shown ENGAGED — its dog teeth interlock the output gear's, locking the gear to
    the shaft. Slide it clear and the gear freewheels (neutral).

The contact sims (scratch/gear_contact_sim.py, scratch/dog_clutch_sim.py) validate
the two couplings dynamically; this is the geometry that implements them.

  .venv/bin/python3 scratch/dog_clutch_unit.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ankusdrive import Worker  # noqa: E402

ART = REPO / "artifacts" / "dog_clutch"
M = 2.0          # module
NIN, NOUT = 12, 24
C = M * (NIN + NOUT) / 2     # centre distance = 36
SR = 5.0         # shaft radius
GH = 6.0         # gear face width
DOG_H = 5.0      # dog tooth height (axial)
ND = 6           # dog teeth


BUILD = r"""
import Part, math
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
doc = App.ActiveDocument
M, SR, GH, C, DOG_H, ND = %f, %f, %f, %f, %f, %d
ig = doc.getObject(%r).Shape          # 12T involute gear (from add_gear)
og = doc.getObject(%r).Shape          # 24T involute gear

def dflat(solid, depth):              # cut a chord -> D profile on +x
    box = Part.makeBox(40, 40, 400, Vector(SR - depth, -20, -200))
    return solid.cut(box)

def dhole(solid, r, depth, h, z0):    # D-shaped bore (matches a D-shaft)
    cyl = Part.makeCylinder(r, h, Vector(0,0,z0))
    cyl = cyl.cut(Part.makeBox(40,40,h+2, Vector(r-depth,-20,z0-1)))
    return solid.cut(cyl)

def dogs(radius, z0, h, phase):       # ND axial dog teeth (a ring of boxes)
    out = []
    tw = 0.45 * (2*math.pi*radius/ND)             # tooth width (< half pitch)
    for k in range(ND):
        th = phase + 2*math.pi*k/ND
        b = Part.makeBox(3.0, tw, h, Vector(-1.5, -tw/2, 0))
        b.Placement = Placement(Vector(radius*math.cos(th), radius*math.sin(th), z0),
                                Rotation(Vector(0,0,1), math.degrees(th)))
        out.append(b)
    return out

parts = []

# --- input side: gear D-keyed to the input shaft -----------------------------
in_shaft = dflat(Part.makeCylinder(SR, GH+40, Vector(0,0,-20)), 1.0)
in_gear = dhole(ig, SR+0.1, 1.0, GH+2, -1)        # D-bore matching the flat -> keyed
parts += [("input_shaft", in_shaft, (0,0,0)), ("input_gear", in_gear, (0,0,0))]

# --- output side: gear freewheels (round bore) + dog teeth on its top face ----
out_shaft = dflat(Part.makeCylinder(SR, GH+DOG_H+40, Vector(0,0,-20)), 1.0)  # flat for the collar
out_gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))           # round bore -> FREE
out_gear = out_gear.fuse(dogs(SR+3.0, GH, DOG_H, 0.0))                       # dog teeth up
# --- dog collar: D-keyed to the output shaft, ENGAGED (dogs interlock) --------
# CRUCIAL: the sleeve sits ABOVE the dog band [GH, GH+DOG_H] so the collar's dog teeth
# protrude DOWN into open space, leaving real GAPS the output gear's teeth enter
# (half-pitch offset). A solid collar body across the band — the original bug here,
# caught by measuring the artifact — jams the gear teeth regardless of phase and
# cannot interlock (RFC §11.10; check it with scratch/verify_dog_clutch_unit.py).
collar = Part.makeCylinder(SR+5.0, 6.0, Vector(0,0,GH+DOG_H))                # sleeve raised above the dogs
collar = dhole(collar, SR+0.1, 1.0, 8.0, GH+DOG_H-1)                         # D-bore -> drives shaft
collar = collar.fuse(dogs(SR+3.0, GH, DOG_H+0.5, math.pi/ND))               # mating dogs, half-pitch
parts += [("output_shaft", out_shaft, (C,0,0)), ("output_gear", out_gear, (C,0,0)),
          ("dog_collar", collar, (C,0,0))]

solids = []
for name, shp, (x,y,z) in parts:
    shp = shp.copy(); shp.translate(Vector(x,y,z))
    o = doc.addObject("Part::Feature", name); o.Shape = shp
    solids.append(shp)
doc.recompute()
asm = Part.Compound(solids)
asm.exportStep(%r)
import Mesh
Mesh.Mesh(asm.tessellate(0.2)).write(%r)
import os
__result__ = [os.path.getsize(%r), os.path.getsize(%r)]
"""


def main():
    ART.mkdir(parents=True, exist_ok=True)
    step, stl = ART / "dog_clutch_unit.step", ART / "dog_clutch_unit.stl"
    with Worker() as w:
        w.call("new_document", name="dogclutch")
        gin = w.call("add_gear", teeth=NIN, module=M, height=GH, name="gin")
        gout = w.call("add_gear", teeth=NOUT, module=M, height=GH, name="gout")
        sizes = w.call("run_script", code=BUILD % (
            M, SR, GH, C, DOG_H, ND, gin["name"], gout["name"],
            str(step), str(stl), str(step), str(stl)))["result"]
    print(f"== single-stage dog-clutch unit  (in {NIN}T keyed, out {NOUT}T freewheel + "
          f"dog clutch engaged, ratio {NIN/NOUT:.3f}) ==")
    print(f"  STEP -> {step}  ({sizes[0]:,} bytes)")
    print(f"  STL  -> {stl}  ({sizes[1]:,} bytes)")
    try:
        sys.path.insert(0, str(REPO / "scratch"))
        from render_gearbox import render
        png = ART / "dog_clutch_unit_render.png"
        tris, _ = render(stl, png, "single-stage dog-clutch unit (engaged)")
        print(f"  PNG  -> {png}  ({tris:,} facets)")
    except Exception as e:
        print(f"  (render skipped: {e})")


if __name__ == "__main__":
    main()

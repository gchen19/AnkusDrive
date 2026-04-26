"""Phase A probes (round 2) — actually create working hole + pattern + mirror.

Run from repo root:
  /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd scratch/probe_phase_a2.py 2>&1 | tail -80
"""
import FreeCAD, Part, Sketcher

doc = FreeCAD.newDocument("p2")
body = doc.addObject("PartDesign::Body", "Body")

# Helper: find body's XY plane
def origin_plane(name):
    for o in body.Origin.OriginFeatures:
        if o.Label.startswith(name):
            return o
    raise RuntimeError(f"no plane {name}")

def origin_axis(name):
    for o in body.Origin.OriginFeatures:
        if o.isDerivedFrom("App::Line") and o.Label.startswith(name):
            return o
    raise RuntimeError(f"no axis {name}")

# Pad a 30mm cube first.
sk_pad = doc.addObject("Sketcher::SketchObject", "PadSk")
body.addObject(sk_pad)
sk_pad.AttachmentSupport = [(origin_plane("XY"), "")]
sk_pad.MapMode = "FlatFace"
i0 = sk_pad.addGeometry(Part.LineSegment(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(30, 0, 0)))
i1 = sk_pad.addGeometry(Part.LineSegment(FreeCAD.Vector(30, 0, 0), FreeCAD.Vector(30, 30, 0)))
i2 = sk_pad.addGeometry(Part.LineSegment(FreeCAD.Vector(30, 30, 0), FreeCAD.Vector(0, 30, 0)))
i3 = sk_pad.addGeometry(Part.LineSegment(FreeCAD.Vector(0, 30, 0), FreeCAD.Vector(0, 0, 0)))
for a, b in ((i0, i1), (i1, i2), (i2, i3), (i3, i0)):
    sk_pad.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))
doc.recompute()

pad = doc.addObject("PartDesign::Pad", "Pad")
body.addObject(pad)
pad.Profile = sk_pad
pad.Length = 30.0
doc.recompute()
print(f"pad volume = {pad.Shape.Volume}")

# Find top face of pad (z = 30).
top_face_idx = None
for i, f in enumerate(pad.Shape.Faces):
    n = f.normalAt((f.ParameterRange[0]+f.ParameterRange[1])/2,
                   (f.ParameterRange[2]+f.ParameterRange[3])/2)
    if abs(n.z - 1.0) < 1e-3 and abs(f.CenterOfMass.z - 30.0) < 1e-3:
        top_face_idx = i + 1
        break
print(f"top face = Face{top_face_idx}")

# Sketch a circle on top face for hole.
sk_hole = doc.addObject("Sketcher::SketchObject", "HoleSk")
body.addObject(sk_hole)
sk_hole.AttachmentSupport = [(pad, f"Face{top_face_idx}")]
sk_hole.MapMode = "FlatFace"
ic = sk_hole.addGeometry(Part.Circle(FreeCAD.Vector(15, 15, 0), FreeCAD.Vector(0, 0, 1), 3.0))
sk_hole.addConstraint(Sketcher.Constraint("Radius", ic, 3.0))
sk_hole.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, ic, 3, 15.0))
sk_hole.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, ic, 3, 15.0))
doc.recompute()

# Now create a Hole feature (which should succeed because body has material now).
hole = doc.addObject("PartDesign::Hole", "Hole")
body.addObject(hole)
hole.Profile = sk_hole
hole.DepthType = "ThroughAll"
hole.Diameter = 6.0
doc.recompute()
print(f"hole volume = {hole.Shape.Volume} (pad was 27000, expect ~27000-π·9·30 = ~26152)")

# A LinearPattern over the hole feature.
lp = doc.addObject("PartDesign::LinearPattern", "LP")
body.addObject(lp)
lp.Originals = [hole]
x_axis = origin_axis("X")
lp.Direction = (x_axis, [""])
lp.Length = 20.0
lp.Occurrences = 3
doc.recompute()
print(f"linear pattern volume = {lp.Shape.Volume}, Direction={lp.Direction}")

# Polar pattern around Z axis
pp = doc.addObject("PartDesign::PolarPattern", "PP")
body.addObject(pp)
pp.Originals = [hole]
z_axis = origin_axis("Z")
pp.Axis = (z_axis, [""])
pp.Angle = 360.0
pp.Occurrences = 4
doc.recompute()
print(f"polar pattern volume = {pp.Shape.Volume}, Axis={pp.Axis}")

# Mirrored across YZ plane
mr = doc.addObject("PartDesign::Mirrored", "MR")
body.addObject(mr)
mr.Originals = [hole]
yz = origin_plane("YZ")
mr.MirrorPlane = (yz, [""])
doc.recompute()
print(f"mirror volume = {mr.Shape.Volume}, MirrorPlane={mr.MirrorPlane}")

# Probe sketch addExternal
sk_ext = doc.addObject("Sketcher::SketchObject", "ExtSk")
body.addObject(sk_ext)
# Attach to top face of LP for testing
sk_ext.AttachmentSupport = [(pad, f"Face{top_face_idx}")]
sk_ext.MapMode = "FlatFace"
doc.recompute()

# Find a vertical edge of the pad
vert_edge = None
for i, e in enumerate(pad.Shape.Edges):
    fv, lv = e.firstVertex().Point, e.lastVertex().Point
    if abs(fv.z - lv.z) > 1.0:  # has z-extent
        vert_edge = f"Edge{i+1}"
        break
print(f"\nvert_edge = {vert_edge}")

# addExternal returns negative geom index
try:
    ext_idx = sk_ext.addExternal(pad, vert_edge)
    print(f"addExternal returned: {ext_idx}")
    print(f"sk_ext.ExternalGeometry: {sk_ext.ExternalGeometry}")
except Exception as e:
    print(f"addExternal err: {e!r}")

print("DONE")

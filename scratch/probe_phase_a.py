"""Phase A probes — Hole, LinearPattern, PolarPattern, Mirrored, Sketcher.addExternal.

Run from repo root:
  /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd scratch/probe_phase_a.py 2>&1 | head -200
"""
import FreeCAD, Part, Sketcher

doc = FreeCAD.newDocument("probe_a")
body = doc.addObject("PartDesign::Body", "Body")

# A circular sketch on XY for hole positioning.
sk = doc.addObject("Sketcher::SketchObject", "HoleSk")
body.addObject(sk)
for o in body.Origin.OriginFeatures:
    if o.Label.startswith("XY"):
        sk.AttachmentSupport = [(o, "")]
        sk.MapMode = "FlatFace"
sk.addGeometry(Part.Circle(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), 2.5))
sk.addConstraint(Sketcher.Constraint("Radius", 0, 2.5))
doc.recompute()

# --- A1: PartDesign::Hole ---
hole = doc.addObject("PartDesign::Hole", "Hole")
body.addObject(hole)
hole.Profile = sk
print("\n=== Hole PropertiesList ===")
for p in sorted(hole.PropertiesList):
    print("  ", p)

print("\n=== Hole enumerations ===")
for p in hole.PropertiesList:
    try:
        enums = hole.getEnumerationsOfProperty(p)
        if enums:
            print(f"  {p}: {enums}")
    except Exception:
        pass

# --- A2: LinearPattern / PolarPattern / Mirrored ---
print("\n=== LinearPattern PropertiesList ===")
lp = doc.addObject("PartDesign::LinearPattern", "LP")
body.addObject(lp)
for p in sorted(lp.PropertiesList):
    print("  ", p)
print("\n=== LinearPattern enumerations ===")
for p in lp.PropertiesList:
    try:
        enums = lp.getEnumerationsOfProperty(p)
        if enums:
            print(f"  {p}: {enums}")
    except Exception:
        pass

print("\n=== PolarPattern PropertiesList ===")
pp = doc.addObject("PartDesign::PolarPattern", "PP")
body.addObject(pp)
for p in sorted(pp.PropertiesList):
    print("  ", p)
print("\n=== PolarPattern enumerations ===")
for p in pp.PropertiesList:
    try:
        enums = pp.getEnumerationsOfProperty(p)
        if enums:
            print(f"  {p}: {enums}")
    except Exception:
        pass

print("\n=== Mirrored PropertiesList ===")
mr = doc.addObject("PartDesign::Mirrored", "MR")
body.addObject(mr)
for p in sorted(mr.PropertiesList):
    print("  ", p)
print("\n=== Mirrored enumerations ===")
for p in mr.PropertiesList:
    try:
        enums = mr.getEnumerationsOfProperty(p)
        if enums:
            print(f"  {p}: {enums}")
    except Exception:
        pass

# --- A3: Sketch addExternal ---
# Make a pad first so we have an external edge to project.
sk2 = doc.addObject("Sketcher::SketchObject", "PadSk")
body.addObject(sk2)
for o in body.Origin.OriginFeatures:
    if o.Label.startswith("XY"):
        sk2.AttachmentSupport = [(o, "")]
        sk2.MapMode = "FlatFace"
sk2.addGeometry(Part.LineSegment(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(10, 0, 0)))
sk2.addGeometry(Part.LineSegment(FreeCAD.Vector(10, 0, 0), FreeCAD.Vector(10, 10, 0)))
sk2.addGeometry(Part.LineSegment(FreeCAD.Vector(10, 10, 0), FreeCAD.Vector(0, 10, 0)))
sk2.addGeometry(Part.LineSegment(FreeCAD.Vector(0, 10, 0), FreeCAD.Vector(0, 0, 0)))
doc.recompute()
pad = doc.addObject("PartDesign::Pad", "Pad")
body.addObject(pad)
pad.Profile = sk2
pad.Length = 5.0
doc.recompute()

# Make a sketch on top of the pad
sk3 = doc.addObject("Sketcher::SketchObject", "TopSk")
body.addObject(sk3)
top_face = None
for i, f in enumerate(pad.Shape.Faces):
    if abs(f.normalAt(0, 0).z - 1.0) < 1e-3 and f.CenterOfMass.z > 4:
        top_face = f"Face{i+1}"
        break
print(f"\n=== top_face on pad = {top_face} ===")
sk3.AttachmentSupport = [(pad, top_face)]
sk3.MapMode = "FlatFace"
doc.recompute()

# Try addExternal — project an edge from the pad
side_edge = None
for i, e in enumerate(pad.Shape.Edges):
    # vertical edge of length 5
    if abs(e.Length - 5.0) < 1e-3 and e.firstVertex().Point.z != e.lastVertex().Point.z:
        side_edge = f"Edge{i+1}"
        break
print(f"side_edge = {side_edge}")
try:
    ret = sk3.addExternal(pad, side_edge)
    print(f"addExternal returned: {ret}")
    print(f"sk3.ExternalGeometry = {sk3.ExternalGeometry}")
    print(f"sk3.GeometryFacadeList count = {len(sk3.Geometry)}")
except Exception as e:
    print(f"addExternal failed: {e!r}")

# Also probe addExternal with a vertex
print("\n=== addExternal signature inspection ===")
import inspect
try:
    print("addExternal docstring:", sk3.addExternal.__doc__)
except Exception as e:
    print(f"  {e}")

# What's ObjectsFem-equivalent factory for Hole? Confirm the type lives under PartDesign:
print("\n=== Hole-related TypeIds ===")
for tid in ("PartDesign::Hole", "PartDesign::LinearPattern",
            "PartDesign::PolarPattern", "PartDesign::Mirrored"):
    o = doc.addObject(tid, "x")
    print(f"  {tid}: created {o.Name} ({o.TypeId})")

print("\n=== DONE ===")

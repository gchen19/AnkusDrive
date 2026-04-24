"""Introspection helper — dump factory + property surface for types DriftPin cares about."""
import FreeCAD, Part, ObjectsFem

d = FreeCAD.newDocument("probe")
b = d.addObject("Part::Box", "Box")
d.recompute()

m = ObjectsFem.makeMeshGmsh(d, "Mesh")
print("MeshGmsh geometry-ish props:",
      sorted(p for p in m.PropertiesList if any(k in p.lower() for k in ("part", "shape", "geom"))))

analysis = ObjectsFem.makeAnalysis(d, "A")
solver = ObjectsFem.makeSolverCalculiXCcxTools(d, "S")
print("CcxTools solver PropertiesList:", sorted(solver.PropertiesList))

mat = ObjectsFem.makeMaterialSolid(d, "M")
print("Material PropertiesList:", sorted(mat.PropertiesList))

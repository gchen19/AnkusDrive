"""
Smoke test: cantilever beam FEM simulation via FreeCAD Python API.
Run with: /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd test_cantilever.py
"""
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

import FreeCAD as App
import Part
import ObjectsFem

print("[1/7] Creating document + geometry")
doc = App.newDocument("Cantilever")
box = doc.addObject("Part::Box", "Box")
box.Length = 8000.0
box.Width = 1000.0
box.Height = 1000.0
doc.recompute()
print(f"      box shape volume = {box.Shape.Volume:.1f} mm^3")

print("[2/7] Analysis container + solver")
analysis = ObjectsFem.makeAnalysis(doc, "Analysis")
solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, "CalculiX")
solver.GeometricalNonlinearity = "linear"
solver.ThermoMechSteadyState = True
solver.MatrixSolverType = "default"
solver.IterationsControlParameterTimeUse = False
analysis.addObject(solver)

print("[3/7] Material (Steel-Generic)")
mat_obj = ObjectsFem.makeMaterialSolid(doc, "SolidMaterial")
mat = mat_obj.Material
mat["Name"] = "Steel-Generic"
mat["YoungsModulus"] = "210000 MPa"
mat["PoissonRatio"] = "0.30"
mat["Density"] = "7900 kg/m^3"
mat_obj.Material = mat
mat_obj.References = [(box, "Solid1")]
analysis.addObject(mat_obj)

print("[4/7] Constraints: fixed @ Face1, force @ Face2")
fixed = ObjectsFem.makeConstraintFixed(doc, "FemConstraintFixed")
fixed.References = [(box, "Face1")]
analysis.addObject(fixed)

force = ObjectsFem.makeConstraintForce(doc, "FemConstraintForce")
force.References = [(box, "Face2")]
force.Force = 9_000_000.0  # N
force.Direction = (box, ["Edge5"])
force.Reversed = True
analysis.addObject(force)

print("[5/7] Meshing (Gmsh)")
femmesh_obj = ObjectsFem.makeMeshGmsh(doc, "Box_Mesh")
femmesh_obj.Shape = box
femmesh_obj.CharacteristicLengthMax = 500.0  # coarse, for speed
doc.recompute()
analysis.addObject(femmesh_obj)

from femmesh.gmshtools import GmshTools
err = GmshTools(femmesh_obj).create_mesh()
if err:
    print(f"      gmsh warning: {err}")
print(f"      nodes={femmesh_obj.FemMesh.NodeCount} "
      f"tets={femmesh_obj.FemMesh.TetraCount} "
      f"tris={femmesh_obj.FemMesh.TriangleCount}")

doc.recompute()

print("[6/7] Saving .FCStd before solve")
fcstd_path = os.path.join(OUT_DIR, "cantilever.FCStd")
doc.saveAs(fcstd_path)
print(f"      saved -> {fcstd_path}")

print("[7/7] Running CalculiX")
from femtools import ccxtools
fea = ccxtools.FemToolsCcx(analysis, solver)
fea.purge_results()
fea.update_objects()
fea.setup_working_dir(OUT_DIR)
message = fea.check_prerequisites()
if message:
    print(f"      prereq check failed: {message}")
    sys.exit(1)
fea.write_inp_file()
ret = fea.ccx_run()
print(f"      ccx return: {ret!r}")
fea.load_results()

result_obj = None
for m in analysis.Group:
    if m.isDerivedFrom("Fem::FemResultObject"):
        result_obj = m
        break

if result_obj is None:
    print("ERROR: no result object produced")
    sys.exit(2)

import statistics
disp = list(result_obj.DisplacementLengths) if hasattr(result_obj, "DisplacementLengths") else []
stress = list(result_obj.vonMises) if hasattr(result_obj, "vonMises") else []

print()
print("=== RESULTS ===")
print(f"result object: {result_obj.Name}")
if disp:
    print(f"  |u|  max = {max(disp):.4f} mm   mean = {statistics.mean(disp):.4f}")
if stress:
    print(f"  vonMises max = {max(stress):.2f} MPa  mean = {statistics.mean(stress):.2f}")
print("SUCCESS")

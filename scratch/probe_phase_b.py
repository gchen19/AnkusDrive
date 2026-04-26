"""Phase B probes — FEM modal/buckling/thermal + local mesh refinement APIs."""
import FreeCAD, ObjectsFem
import traceback

doc = FreeCAD.newDocument("pb")

def section(name, fn):
    print(f"\n=== {name} ===")
    try:
        fn()
    except Exception:
        traceback.print_exc()

solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, "Ccx")
section("Solver AnalysisType",
        lambda: print(" ", solver.getEnumerationsOfProperty("AnalysisType")))

mesh = ObjectsFem.makeMeshGmsh(doc, "Mesh")
section("Mesh refinement-related props",
        lambda: print(" ",
                      [p for p in sorted(mesh.PropertiesList)
                       if any(k in p.lower() for k in ("region", "refin", "char"))]))

section("ObjectsFem region/group/refin factories",
        lambda: print(" ",
                      [n for n in dir(ObjectsFem)
                       if n.startswith("make") and ("Region" in n or "Group" in n or "Refin" in n)]))

def probe_meshregion():
    import inspect
    print(" sig:", inspect.signature(ObjectsFem.makeMeshRegion))
    mr = ObjectsFem.makeMeshRegion(doc, mesh, 1.0, "Region")
    print(" props:", sorted(mr.PropertiesList))
    print(" type:", mr.TypeId)
    for p in mr.PropertiesList:
        try:
            v = getattr(mr, p)
            print(f"  {p} = {v!r}")
        except Exception:
            pass
section("MeshRegion", probe_meshregion)

section("ObjectsFem thermal factories",
        lambda: print(" ",
                      [n for n in dir(ObjectsFem)
                       if "Temperature" in n or "Heat" in n or "Flux" in n]))

def probe_temp():
    ct = ObjectsFem.makeConstraintTemperature(doc, "T")
    print(" props:", sorted(ct.PropertiesList))
    for p in ct.PropertiesList:
        try:
            e = ct.getEnumerationsOfProperty(p)
            if e:
                print(f"  {p}: {e}")
        except Exception:
            pass
section("ConstraintTemperature", probe_temp)

def probe_flux():
    cf = ObjectsFem.makeConstraintHeatflux(doc, "F")
    print(" props:", sorted(cf.PropertiesList))
    for p in cf.PropertiesList:
        try:
            e = cf.getEnumerationsOfProperty(p)
            if e:
                print(f"  {p}: {e}")
        except Exception:
            pass
section("ConstraintHeatflux", probe_flux)

def probe_init_temp():
    ci = ObjectsFem.makeConstraintInitialTemperature(doc, "I")
    print(" props:", sorted(ci.PropertiesList))
section("ConstraintInitialTemperature", probe_init_temp)

section("ObjectsFem result factories",
        lambda: print(" ", [n for n in dir(ObjectsFem) if "Result" in n]))

print("\nDONE")

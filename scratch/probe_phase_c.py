"""Phase C probes — Loft, Sweep, Helix, Thickness, Draft, multi-doc, transactions."""
import FreeCAD, Part, Sketcher
import traceback

def section(name, fn):
    print(f"\n=== {name} ===")
    try:
        fn()
    except Exception:
        traceback.print_exc()

doc = FreeCAD.newDocument("pc")

# Probe AdditiveLoft
section("AdditiveLoft props", lambda: print(" ", sorted(
    doc.addObject("PartDesign::AdditiveLoft", "L").PropertiesList)))

section("AdditivePipe (sweep) props", lambda: print(" ", sorted(
    doc.addObject("PartDesign::AdditivePipe", "P").PropertiesList)))

section("Helix (Part::Helix) props", lambda: print(" ", sorted(
    doc.addObject("Part::Helix", "H").PropertiesList)))

section("Thickness props", lambda: print(" ", sorted(
    doc.addObject("PartDesign::Thickness", "T").PropertiesList)))

section("Draft props", lambda: print(" ", sorted(
    doc.addObject("PartDesign::Draft", "D").PropertiesList)))

# Enumerations on those
for tid, label in [
    ("PartDesign::AdditiveLoft", "AdditiveLoft"),
    ("PartDesign::AdditivePipe", "AdditivePipe"),
    ("PartDesign::Thickness", "Thickness"),
    ("PartDesign::Draft", "Draft"),
]:
    o = doc.addObject(tid, "x_" + label)
    print(f"\n=== {label} enums ===")
    for p in o.PropertiesList:
        try:
            e = o.getEnumerationsOfProperty(p)
            if e:
                print(f"  {p}: {e}")
        except Exception:
            pass

# Multi-document handles
section("App.listDocuments / closeDocument / setActiveDocument", lambda: (
    print(" listDocuments:", FreeCAD.listDocuments()),
    print(" newDocument:", FreeCAD.newDocument("alt").Name),
    print(" listDocuments now:", list(FreeCAD.listDocuments())),
    print(" setActiveDocument call:", FreeCAD.setActiveDocument("pc")),
    print(" closeDocument call:", FreeCAD.closeDocument("alt")),
    print(" listDocuments after close:", list(FreeCAD.listDocuments())),
))

# Transactions
section("Document transactions", lambda: (
    print(" openTransaction sig:", doc.openTransaction.__doc__),
    print(" commitTransaction sig:", doc.commitTransaction.__doc__),
    print(" abortTransaction sig:", doc.abortTransaction.__doc__),
))

# Try a transaction roundtrip
def tx_roundtrip():
    doc.openTransaction("test_tx")
    box = doc.addObject("Part::Box", "TxBox")
    print(" before abort, doc.Objects has TxBox:", any(o.Name == "TxBox" for o in doc.Objects))
    doc.abortTransaction()
    print(" after abort, doc.Objects has TxBox:", any(o.Name == "TxBox" for o in doc.Objects))
section("Transaction abort", tx_roundtrip)

print("\nDONE")

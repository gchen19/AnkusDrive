"""
DriftPin worker — runs inside `freecadcmd`, reads JSON requests on stdin,
writes JSON responses on stdout.

Stdio hygiene: fd 1 is redirected to fd 2 at startup so FreeCAD's C++ chatter
(banner, "Recompute..." progress, solver dumps) never corrupts the protocol
channel. Responses are written to a preserved dup of the original fd 1.

Wire format: newline-delimited JSON, one object per line.
  request:  {"id": "r1", "method": "ping", "params": {}}
  response: {"id": "r1", "result": ...}                    or
            {"id": "r1", "error": {"type": ..., "message": ..., "traceback": ...}}

The very first response line is `{"ready": true, "freecad": [...]}`, emitted
before entering the dispatch loop so the host can confirm the worker booted.
"""
import json
import os
import sys
import traceback


_RESPONSE_FD = os.dup(1)
os.dup2(2, 1)

import FreeCAD as App  # noqa: E402
import Part  # noqa: E402
import ObjectsFem  # noqa: E402


def _respond(obj):
    os.write(_RESPONSE_FD, (json.dumps(obj) + "\n").encode())


_handles = {}
_counters = {}


def _new_handle(prefix):
    _counters[prefix] = _counters.get(prefix, 0) + 1
    return f"{prefix}_{_counters[prefix]}"


def _register(prefix, obj):
    h = _new_handle(prefix)
    _handles[h] = obj
    return h


def _resolve(h):
    if h not in _handles:
        raise KeyError(f"unknown handle: {h!r}")
    return _handles[h]


HANDLERS = {}


def handler(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


@handler("ping")
def _h_ping(p):
    return "pong"


@handler("version")
def _h_version(p):
    return {"freecad": list(App.Version()), "python": sys.version.split()[0]}


@handler("new_document")
def _h_new_document(p):
    name = p.get("name", "Unnamed")
    doc = App.newDocument(name)
    return {"doc": doc.Name}


@handler("list_objects")
def _h_list_objects(p):
    doc = App.ActiveDocument
    if doc is None:
        return []
    return [
        {"name": o.Name, "type": o.TypeId, "label": o.Label}
        for o in doc.Objects
    ]


@handler("list_handles")
def _h_list_handles(p):
    return {h: {"name": o.Name, "type": o.TypeId} for h, o in _handles.items()}


@handler("add_primitive")
def _h_add_primitive(p):
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")
    kind = p["kind"]
    if kind == "box":
        obj = doc.addObject("Part::Box", "Box")
        obj.Length = float(p.get("w", 10))
        obj.Width = float(p.get("d", 10))
        obj.Height = float(p.get("h", 10))
    elif kind == "cylinder":
        obj = doc.addObject("Part::Cylinder", "Cylinder")
        obj.Radius = float(p.get("r", 5))
        obj.Height = float(p.get("h", 10))
    elif kind == "sphere":
        obj = doc.addObject("Part::Sphere", "Sphere")
        obj.Radius = float(p.get("r", 5))
    else:
        raise ValueError(f"unknown primitive kind: {kind!r}")

    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)

    doc.recompute()
    h = _register(kind, obj)
    return {"handle": h, "name": obj.Name, "volume": obj.Shape.Volume}


@handler("boolean_op")
def _h_boolean_op(p):
    doc = App.ActiveDocument
    op = p["op"]
    type_map = {"cut": "Part::Cut", "fuse": "Part::Fuse", "common": "Part::Common"}
    if op not in type_map:
        raise ValueError(f"unknown boolean op: {op!r}")
    base = _resolve(p["base"])
    tool = _resolve(p["tool"])
    obj = doc.addObject(type_map[op], op.capitalize())
    obj.Base = base
    obj.Tool = tool
    doc.recompute()
    h = _register(op, obj)
    return {"handle": h, "volume": obj.Shape.Volume}


def _content_bbox(doc):
    """Union bounding box of all objects in the document that have a valid Shape.
    Returns (cx, cy, cz, diag_mm) or None if nothing shaped."""
    bb = None
    for o in doc.Objects:
        shape = getattr(o, "Shape", None)
        if shape is None or shape.isNull():
            continue
        b = shape.BoundBox
        if not b.isValid():
            continue
        bb = b if bb is None else bb.united(b)
    if bb is None:
        return None
    cx = (bb.XMin + bb.XMax) / 2.0
    cy = (bb.YMin + bb.YMax) / 2.0
    cz = (bb.ZMin + bb.ZMax) / 2.0
    diag = bb.DiagonalLength or 1.0
    return cx, cy, cz, diag


def _fit_camera_xml(cx, cy, cz, diag):
    """Produce a FreeCAD <Camera .../> element sized to the bbox, in an axonometric view."""
    dist = diag * 2.5
    # FreeCAD's stock axonometric orientation (rotates iso view)
    orientation = "-0.35740677 0.86285633 -0.35740677 4.565414"
    # Camera position: offset from bbox center along a normalized axonometric direction
    # (direction chosen so the above orientation looks roughly at origin)
    dx, dy, dz = 0.7, 0.5, 0.5  # rough axonometric direction vector
    px = cx + dx * dist
    py = cy + dy * dist
    pz = cz + dz * dist
    body = (
        "OrthographicCamera {\n"
        "  viewportMapping ADJUST_CAMERA\n"
        f"  position {px:.6f} {py:.6f} {pz:.6f}\n"
        f"  orientation {orientation}\n"
        f"  nearDistance {dist * 0.1:.6f}\n"
        f"  farDistance {dist * 10:.6f}\n"
        "  aspectRatio 1\n"
        f"  focalDistance {dist:.6f}\n"
        f"  height {diag * 1.5:.6f}\n"
        "}\n"
    )
    return '<Camera settings="' + body.replace("\n", "&#10;") + '"/>'


def _minimal_gui_document(cam_xml):
    """A minimal GuiDocument.xml with just a Camera. FreeCAD fills in default
    ViewProviders for objects that lack an entry."""
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<Document SchemaVersion="1" HasExpansion="1">\n'
        "    <Expand/>\n"
        f"    {cam_xml}\n"
        '    <ViewProviderData Count="0">\n'
        "    </ViewProviderData>\n"
        '    <CameraSettings/>\n'
        "</Document>\n"
    )


def _patch_fcstd_camera(path, cam_xml):
    """Ensure the .FCStd has a GuiDocument.xml whose Camera is fitted to content.
    Writes a fresh minimal GuiDocument if the zip has none; otherwise replaces
    the existing Camera element."""
    import re
    import shutil
    import zipfile
    tmp = path + ".tmp"
    has_gui = False
    with zipfile.ZipFile(path, "r") as zin:
        names = set(zin.namelist())
        has_gui = "GuiDocument.xml" in names
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "GuiDocument.xml":
                    text = data.decode("utf-8")
                    new_text, n = re.subn(r"\s*<Camera[^/]*/>", "\n    " + cam_xml, text, count=1)
                    if n == 0:
                        new_text = text.replace("</Document>", "    " + cam_xml + "\n</Document>")
                    data = new_text.encode("utf-8")
                zout.writestr(item, data)
            if not has_gui:
                zout.writestr("GuiDocument.xml", _minimal_gui_document(cam_xml))
    shutil.move(tmp, path)


@handler("save_document")
def _h_save_document(p):
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document")
    path = p["path"]
    doc.saveAs(path)

    fitted = False
    bb = _content_bbox(doc)
    if bb is not None:
        cx, cy, cz, diag = bb
        if diag > 0:
            _patch_fcstd_camera(path, _fit_camera_xml(cx, cy, cz, diag))
            fitted = True

    return {"path": path, "size": os.path.getsize(path), "camera_fit": fitted}


@handler("open_document")
def _h_open_document(p):
    doc = App.openDocument(p["path"])
    App.setActiveDocument(doc.Name)
    return {
        "doc": doc.Name,
        "objects": [{"name": o.Name, "type": o.TypeId} for o in doc.Objects],
    }


@handler("export_shape")
def _h_export_shape(p):
    """Export a Part-based object to STEP/IGES/BREP/STL. Format detected from path extension."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document")

    obj_name = p.get("object")
    if obj_name:
        obj = doc.getObject(obj_name)
        if obj is None:
            raise KeyError(f"no object named {obj_name!r}")
    else:
        part_objs = [o for o in doc.Objects if hasattr(o, "Shape") and not o.Shape.isNull()]
        if not part_objs:
            raise RuntimeError("no shaped objects in document")
        obj = part_objs[0]

    path = p["path"]
    ext = os.path.splitext(path)[1].lower()

    if ext in (".step", ".stp", ".iges", ".igs"):
        import Part as _Part
        _Part.export([obj], path)
    elif ext == ".brep":
        obj.Shape.exportBrep(path)
    elif ext == ".stl":
        import Mesh
        import MeshPart
        mesh = MeshPart.meshFromShape(
            Shape=obj.Shape,
            LinearDeflection=float(p.get("linear_deflection", 0.1)),
            AngularDeflection=float(p.get("angular_deflection", 0.5)),
        )
        mesh.write(path)
    else:
        raise ValueError(f"unsupported export extension: {ext!r}")

    return {"path": path, "size": os.path.getsize(path), "object": obj.Name}


_SCRIPT_GLOBALS = None


@handler("run_script")
def _h_run_script(p):
    """Execute an arbitrary Python script inside the worker's live document context.

    The script has App, Part, ObjectsFem, and the handle registry helpers in scope.
    It may set __result__ to a JSON-serializable value which is returned to the host.
    """
    global _SCRIPT_GLOBALS
    if _SCRIPT_GLOBALS is None:
        _SCRIPT_GLOBALS = {
            "App": App,
            "FreeCAD": App,
            "Part": Part,
            "ObjectsFem": ObjectsFem,
            "_register": _register,
            "_resolve": _resolve,
            "_handles": _handles,
            "__name__": "__driftpin_script__",
        }
    code = p["code"]
    _SCRIPT_GLOBALS.pop("__result__", None)
    exec(compile(code, p.get("path", "<script>"), "exec"), _SCRIPT_GLOBALS)
    return {"result": _SCRIPT_GLOBALS.get("__result__")}


@handler("recompute_stress")
def _h_recompute_stress(p):
    """Exercise FreeCAD chatter — each recompute prints progress to fd 1.
    Used by the stdio-hygiene test to prove chatter doesn't leak into stdout."""
    doc = App.ActiveDocument or App.newDocument("chatter")
    n = int(p.get("n", 20))
    for i in range(n):
        box = doc.addObject("Part::Box", f"B{i}")
        box.Length = 1.0 + i
        doc.recompute()
    return {"objects": len(doc.Objects)}


@handler("fem_cantilever_demo")
def _h_fem_cantilever(p):
    """Full FEM pipeline in one call. Proves the FEM stack is reachable through IPC."""
    from femmesh.gmshtools import GmshTools
    from femtools import ccxtools

    doc = App.newDocument(p.get("doc_name", "Cantilever"))
    box = doc.addObject("Part::Box", "Beam")
    box.Length = float(p.get("length", 8000))
    box.Width = float(p.get("width", 1000))
    box.Height = float(p.get("height", 1000))
    doc.recompute()

    analysis = ObjectsFem.makeAnalysis(doc, "Analysis")
    solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, "CcxTools")
    solver.GeometricalNonlinearity = "linear"
    solver.ThermoMechSteadyState = True
    solver.MatrixSolverType = "default"
    solver.IterationsControlParameterTimeUse = False
    analysis.addObject(solver)

    mat_obj = ObjectsFem.makeMaterialSolid(doc, "Steel")
    mat = mat_obj.Material
    mat["Name"] = "Steel-Generic"
    mat["YoungsModulus"] = "210000 MPa"
    mat["PoissonRatio"] = "0.30"
    mat["Density"] = "7900 kg/m^3"
    mat_obj.Material = mat
    mat_obj.References = [(box, "Solid1")]
    analysis.addObject(mat_obj)

    fixed = ObjectsFem.makeConstraintFixed(doc, "Fixed")
    fixed.References = [(box, "Face1")]
    analysis.addObject(fixed)

    force = ObjectsFem.makeConstraintForce(doc, "Force")
    force.References = [(box, "Face2")]
    force.Force = float(p.get("force", 9_000_000.0))
    force.Direction = (box, ["Edge5"])
    force.Reversed = True
    analysis.addObject(force)

    mesh = ObjectsFem.makeMeshGmsh(doc, "Mesh")
    mesh.Shape = box
    mesh.CharacteristicLengthMax = float(p.get("mesh_size", 500.0))
    doc.recompute()
    analysis.addObject(mesh)
    GmshTools(mesh).create_mesh()

    workdir = p.get("workdir", "/tmp/driftpin_fem")
    os.makedirs(workdir, exist_ok=True)
    fea = ccxtools.FemToolsCcx(analysis, solver)
    fea.purge_results()
    fea.update_objects()
    fea.setup_working_dir(workdir)
    prereq = fea.check_prerequisites()
    if prereq:
        raise RuntimeError(f"FEM prereq check failed: {prereq}")
    fea.write_inp_file()
    fea.ccx_run()
    fea.load_results()

    result = None
    for m in analysis.Group:
        if m.isDerivedFrom("Fem::FemResultObject"):
            result = m
            break
    if result is None:
        raise RuntimeError("no result object produced")

    disp = list(result.DisplacementLengths)
    stress = list(result.vonMises)
    return {
        "nodes": mesh.FemMesh.NodeCount,
        "tets": mesh.FemMesh.TetraCount,
        "max_displacement_mm": max(disp),
        "max_vonmises_mpa": max(stress),
        "workdir": workdir,
    }


def _main():
    _respond({"ready": True, "freecad": list(App.Version())[:3]})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        mid = None
        try:
            req = json.loads(line)
            mid = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            if method == "shutdown":
                _respond({"id": mid, "result": "bye"})
                break
            if method not in HANDLERS:
                _respond({"id": mid, "error": {
                    "type": "UnknownMethod",
                    "message": str(method),
                    "traceback": "",
                }})
                continue
            _respond({"id": mid, "result": HANDLERS[method](params)})
        except Exception as e:
            _respond({"id": mid, "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            }})


_main()

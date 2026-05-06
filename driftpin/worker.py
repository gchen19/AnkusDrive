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
import Sketcher  # noqa: E402


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


_TAG_DECIMALS = 3
_TAG_NORMAL_DECIMALS = 4


def _round(v, n=_TAG_DECIMALS):
    return round(float(v), n)


def _vec3(v, n=_TAG_DECIMALS):
    return (round(v.x, n), round(v.y, n), round(v.z, n))


def _surface_kind(face):
    surf = face.Surface
    name = type(surf).__name__
    if name == "Plane":
        return "planar"
    if name == "Cylinder":
        return "cylindrical"
    if name == "Cone":
        return "conical"
    if name == "Sphere":
        return "spherical"
    if name == "Toroid":
        return "toroidal"
    if name in ("BSplineSurface", "BezierSurface"):
        return "spline"
    return name.lower()


def _outward_normal(face):
    """Outward unit normal at the face's parameter midpoint. We can't use
    Plane.Axis directly because two parallel faces of a solid share the same
    surface axis but point in opposite directions. normalAt() already returns
    an orientation-corrected outward normal for OCCT faces."""
    u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2.0
    v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2.0
    return face.normalAt(u_mid, v_mid)


def _face_signature(face):
    """Deterministic, edit-stable signature for a face. Two faces with the same
    surface kind, area, centroid, and (where meaningful) axis/radius will hash
    identically across recomputes."""
    kind = _surface_kind(face)
    area = _round(face.Area)
    cog = _vec3(face.CenterOfMass)
    sig = {"kind": kind, "area": area, "centroid": cog}

    surf = face.Surface
    if kind == "planar":
        sig["normal"] = _vec3(_outward_normal(face), _TAG_NORMAL_DECIMALS)
    elif kind == "cylindrical":
        sig["axis"] = _vec3(surf.Axis, _TAG_NORMAL_DECIMALS)
        sig["radius"] = _round(surf.Radius)
    elif kind == "conical":
        sig["axis"] = _vec3(surf.Axis, _TAG_NORMAL_DECIMALS)
        sig["radius"] = _round(surf.Radius)
        sig["semi_angle"] = _round(surf.SemiAngle, _TAG_NORMAL_DECIMALS)
    elif kind == "spherical":
        sig["radius"] = _round(surf.Radius)
    elif kind == "toroidal":
        sig["axis"] = _vec3(surf.Axis, _TAG_NORMAL_DECIMALS)
        sig["major_radius"] = _round(surf.MajorRadius)
        sig["minor_radius"] = _round(surf.MinorRadius)
    return sig


def _edge_kind(edge):
    curve = edge.Curve
    name = type(curve).__name__
    if name == "Line":
        return "line"
    if name == "Circle":
        return "circle"
    if name == "Ellipse":
        return "ellipse"
    if name in ("BSplineCurve", "BezierCurve"):
        return "spline"
    return name.lower()


def _edge_signature(edge):
    kind = _edge_kind(edge)
    length = _round(edge.Length)
    cog = _vec3(edge.CenterOfMass)
    sig = {"kind": kind, "length": length, "centroid": cog}
    if kind == "circle":
        c = edge.Curve
        sig["axis"] = _vec3(c.Axis, _TAG_NORMAL_DECIMALS)
        sig["radius"] = _round(c.Radius)
    elif kind == "line":
        sig["start"] = _vec3(edge.firstVertex().Point)
        sig["end"] = _vec3(edge.lastVertex().Point)
    return sig


def _hash_sig(sig):
    """Stable short hash of a signature dict. JSON with sort_keys gives a
    canonical form across Python runs."""
    import hashlib
    blob = json.dumps(sig, sort_keys=True).encode()
    return hashlib.blake2b(blob, digest_size=6).hexdigest()


def _face_descriptor(face, idx):
    sig = _face_signature(face)
    desc = {
        "tag": f"f_{_hash_sig(sig)}",
        "index": f"Face{idx}",
        "kind": sig["kind"],
        "area": sig["area"],
        "centroid": list(sig["centroid"]),
    }
    if "normal" in sig:
        desc["normal"] = list(sig["normal"])
    if "axis" in sig:
        desc["axis"] = list(sig["axis"])
    if "radius" in sig:
        desc["radius"] = sig["radius"]
    return desc


def _edge_descriptor(edge, idx):
    sig = _edge_signature(edge)
    desc = {
        "tag": f"e_{_hash_sig(sig)}",
        "index": f"Edge{idx}",
        "kind": sig["kind"],
        "length": sig["length"],
        "centroid": list(sig["centroid"]),
    }
    if "axis" in sig:
        desc["axis"] = list(sig["axis"])
    if "radius" in sig:
        desc["radius"] = sig["radius"]
    return desc


def _shape_of(handle):
    obj = _resolve(handle)
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise RuntimeError(f"object {handle!r} has no usable Shape")
    return obj, shape


@handler("list_faces")
def _h_list_faces(p):
    _, shape = _shape_of(p["handle"])
    return [_face_descriptor(f, i + 1) for i, f in enumerate(shape.Faces)]


@handler("list_edges")
def _h_list_edges(p):
    _, shape = _shape_of(p["handle"])
    return [_edge_descriptor(e, i + 1) for i, e in enumerate(shape.Edges)]


def _matches_predicate(desc, pred):
    if "kind" in pred and desc["kind"] != pred["kind"]:
        return False
    if "type" in pred and desc["kind"] != pred["type"]:
        return False
    if "radius_eq" in pred:
        r = desc.get("radius")
        if r is None or abs(r - float(pred["radius_eq"])) > float(pred.get("radius_tol", 1e-3)):
            return False
    if "area_min" in pred and desc["area"] < float(pred["area_min"]):
        return False
    if "area_max" in pred and desc["area"] > float(pred["area_max"]):
        return False
    if "normal_dir" in pred:
        n = desc.get("normal")
        if n is None:
            return False
        nd = pred["normal_dir"]
        tol = float(pred.get("normal_tol", 1e-3))
        if any(abs(n[i] - float(nd[i])) > tol for i in range(3)):
            return False
    return True


def _ordering_key(pred):
    """Optional ordering: pred may include centroid_max / centroid_min on x/y/z
    or area_max / area_min to sort the result."""
    if "centroid_max" in pred:
        axis = "xyz".index(pred["centroid_max"])
        return lambda d: -d["centroid"][axis]
    if "centroid_min" in pred:
        axis = "xyz".index(pred["centroid_min"])
        return lambda d: d["centroid"][axis]
    if pred.get("order") == "area_desc":
        return lambda d: -d["area"]
    if pred.get("order") == "area_asc":
        return lambda d: d["area"]
    return None


@handler("query_faces")
def _h_query_faces(p):
    handle = p["handle"]
    pred = p.get("predicate") or {}
    _, shape = _shape_of(handle)
    descs = [_face_descriptor(f, i + 1) for i, f in enumerate(shape.Faces)]
    matches = [d for d in descs if _matches_predicate(d, pred)]
    key = _ordering_key(pred)
    if key is not None:
        matches.sort(key=key)
    return matches


@handler("resolve_face")
def _h_resolve_face(p):
    handle = p["handle"]
    tag = p["tag"]
    _, shape = _shape_of(handle)
    hits = []
    for i, face in enumerate(shape.Faces):
        sig = _face_signature(face)
        if f"f_{_hash_sig(sig)}" == tag:
            hits.append(i + 1)
    if not hits:
        raise KeyError(f"tag {tag!r} not found on {handle!r}")
    if len(hits) > 1:
        raise RuntimeError(
            f"tag {tag!r} ambiguous on {handle!r}: matches Face{hits}"
        )
    return {"index": f"Face{hits[0]}", "handle": handle}


@handler("resolve_edge")
def _h_resolve_edge(p):
    handle = p["handle"]
    tag = p["tag"]
    _, shape = _shape_of(handle)
    hits = []
    for i, edge in enumerate(shape.Edges):
        sig = _edge_signature(edge)
        if f"e_{_hash_sig(sig)}" == tag:
            hits.append(i + 1)
    if not hits:
        raise KeyError(f"tag {tag!r} not found on {handle!r}")
    if len(hits) > 1:
        raise RuntimeError(
            f"tag {tag!r} ambiguous on {handle!r}: matches Edge{hits}"
        )
    return {"index": f"Edge{hits[0]}", "handle": handle}


@handler("fillet_edges")
def _h_fillet_edges(p):
    """Fillet specific edges of a shaped object. Edges referenced by tag (preferred)
    or by FaceN-style index. Returns a new handle for the fillet feature."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document")
    obj, shape = _shape_of(p["handle"])
    radius = float(p.get("radius", 1.0))

    edges = []
    for ref in p.get("edges", []):
        if isinstance(ref, str) and ref.startswith("e_"):
            r = _h_resolve_edge({"handle": p["handle"], "tag": ref})
            edges.append(int(r["index"][len("Edge"):]))
        elif isinstance(ref, str) and ref.startswith("Edge"):
            edges.append(int(ref[len("Edge"):]))
        else:
            edges.append(int(ref))

    fillet = doc.addObject("Part::Fillet", "Fillet")
    fillet.Base = obj
    fillet.Edges = [(i, radius, radius) for i in edges]
    doc.recompute()
    h = _register("fillet", fillet)
    return {"handle": h, "volume": fillet.Shape.Volume, "edges": edges}


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
        # Some FreeCAD objects expose a Shape attribute that's not a Part.Shape
        # (e.g. certain PartDesign feature wrappers). Skip anything we can't
        # call isNull/BoundBox on.
        if shape is None or not hasattr(shape, "isNull") or not hasattr(shape, "BoundBox"):
            continue
        try:
            if shape.isNull():
                continue
            b = shape.BoundBox
            if not b.isValid():
                continue
        except Exception:
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


# --- PartDesign / Sketcher ----------------------------------------------------

_DATUM_PLANES = {
    "XY": (App.Vector(0, 0, 0), App.Rotation(App.Vector(0, 0, 1), 0)),
    "XZ": (App.Vector(0, 0, 0), App.Rotation(App.Vector(1, 0, 0), 90)),
    "YZ": (App.Vector(0, 0, 0), App.Rotation(App.Vector(0, 1, 0), -90)),
}


def _active_doc():
    doc = App.ActiveDocument
    if doc is not None:
        return doc
    open_docs = list(App.listDocuments().keys())
    if not open_docs:
        raise RuntimeError("no active document; call new_document first")
    if len(open_docs) == 1:
        App.setActiveDocument(open_docs[0])
        return App.ActiveDocument
    raise RuntimeError(
        f"App.ActiveDocument is None but {len(open_docs)} documents are open "
        f"({open_docs}); call set_active_document"
    )


@handler("make_body")
def _h_make_body(p):
    doc = _active_doc()
    body = doc.addObject("PartDesign::Body", p.get("name", "Body"))
    doc.recompute()
    h = _register("body", body)
    return {"handle": h, "name": body.Name}


def _resolve_body(handle):
    obj = _resolve(handle)
    if not obj.isDerivedFrom("PartDesign::Body"):
        raise TypeError(f"handle {handle!r} is not a Body (got {obj.TypeId})")
    return obj


def _resolve_plane_ref(body, plane_ref):
    """Resolve a plane reference to (object, sublink_list) suitable for
    Sketch.Support. Accepts standard names ('XY', 'XZ', 'YZ') which map to the
    body's origin planes, or a datum-plane handle."""
    if plane_ref in _DATUM_PLANES:
        for o in body.Group:
            if o.isDerivedFrom("App::Plane") and o.Label.startswith(plane_ref):
                return (o, [""])
        for o in body.Origin.OriginFeatures:
            if o.isDerivedFrom("App::Plane") and o.Label.startswith(plane_ref):
                return (o, [""])
        raise RuntimeError(f"could not find {plane_ref} origin plane in body")
    obj = _resolve(plane_ref)
    return (obj, [""])


@handler("make_datum_plane")
def _h_make_datum_plane(p):
    """Create a Datum Plane in a Body. base may be 'XY'/'XZ'/'YZ' or a face tag
    on a referenced shape; offset (mm) shifts along the plane normal."""
    doc = _active_doc()
    body = _resolve_body(p["body"])
    name = p.get("name", "DatumPlane")
    base = p.get("base", "XY")
    offset = float(p.get("offset", 0.0))

    plane = doc.addObject("PartDesign::Plane", name)
    body.addObject(plane)

    if isinstance(base, str) and base in _DATUM_PLANES:
        ref_obj, _sub = _resolve_plane_ref(body, base)
        plane.AttachmentSupport = [(ref_obj, "")]
        plane.MapMode = "FlatFace"
    elif isinstance(base, dict):
        face_handle = base["handle"]
        tag = base["tag"]
        r = _h_resolve_face({"handle": face_handle, "tag": tag})
        ref_obj = _resolve(face_handle)
        plane.AttachmentSupport = [(ref_obj, r["index"])]
        plane.MapMode = "FlatFace"
    else:
        raise ValueError(f"unrecognized base for datum plane: {base!r}")

    if offset:
        plane.AttachmentOffset = App.Placement(
            App.Vector(0, 0, offset), App.Rotation()
        )
    doc.recompute()
    h = _register("datum_plane", plane)
    return {"handle": h, "name": plane.Name}


@handler("make_sketch")
def _h_make_sketch(p):
    """Create a sketch in a Body, attached to a plane reference. plane is one of
    'XY'/'XZ'/'YZ' (origin planes) or a datum-plane handle."""
    doc = _active_doc()
    body = _resolve_body(p["body"])
    plane_ref = p.get("plane", "XY")

    sketch = doc.addObject("Sketcher::SketchObject", p.get("name", "Sketch"))
    body.addObject(sketch)

    ref_obj, sub = _resolve_plane_ref(body, plane_ref)
    sketch.AttachmentSupport = [(ref_obj, sub)]
    sketch.MapMode = "FlatFace"

    doc.recompute()
    h = _register("sketch", sketch)
    return {"handle": h, "name": sketch.Name}


def _resolve_sketch(handle):
    obj = _resolve(handle)
    if not obj.isDerivedFrom("Sketcher::SketchObject"):
        raise TypeError(f"handle {handle!r} is not a Sketch (got {obj.TypeId})")
    return obj


def _vec(xy):
    """2D point → 3D Vector at z=0 (sketch-local coords)."""
    return App.Vector(float(xy[0]), float(xy[1]), 0)


@handler("add_sketch_geometry")
def _h_add_sketch_geometry(p):
    """Append geometric primitives to a sketch. Returns the indices assigned by
    Sketcher (used in subsequent constraint refs)."""
    sketch = _resolve_sketch(p["sketch"])
    items = p["items"]
    indices = []
    for item in items:
        kind = item["type"]
        if kind == "line":
            g = Part.LineSegment(_vec(item["start"]), _vec(item["end"]))
        elif kind == "circle":
            c = _vec(item["center"])
            g = Part.Circle(c, App.Vector(0, 0, 1), float(item["radius"]))
        elif kind == "arc":
            c = _vec(item["center"])
            g = Part.Circle(c, App.Vector(0, 0, 1), float(item["radius"]))
            g = Part.ArcOfCircle(g, float(item["start_angle"]), float(item["end_angle"]))
        elif kind == "point":
            g = Part.Point(_vec(item["pos"]))
        else:
            raise ValueError(f"unknown sketch geometry kind: {kind!r}")
        idx = sketch.addGeometry(g, bool(item.get("construction", False)))
        indices.append(idx)
    sketch.recompute()
    return {"indices": indices}


_SKETCH_CONSTRAINT_BUILDERS = {
    "Coincident": lambda refs, val: Sketcher.Constraint(
        "Coincident", refs[0][0], refs[0][1], refs[1][0], refs[1][1]
    ),
    "Horizontal": lambda refs, val: Sketcher.Constraint("Horizontal", refs[0][0]),
    "Vertical": lambda refs, val: Sketcher.Constraint("Vertical", refs[0][0]),
    "Distance": lambda refs, val: Sketcher.Constraint(
        "Distance", refs[0][0], float(val)
    ) if len(refs) == 1 else Sketcher.Constraint(
        "Distance", refs[0][0], refs[0][1], refs[1][0], refs[1][1], float(val)
    ),
    "DistanceX": lambda refs, val: Sketcher.Constraint(
        "DistanceX", refs[0][0], refs[0][1], refs[1][0], refs[1][1], float(val)
    ) if len(refs) == 2 else Sketcher.Constraint("DistanceX", refs[0][0], float(val)),
    "DistanceY": lambda refs, val: Sketcher.Constraint(
        "DistanceY", refs[0][0], refs[0][1], refs[1][0], refs[1][1], float(val)
    ) if len(refs) == 2 else Sketcher.Constraint("DistanceY", refs[0][0], float(val)),
    "Radius": lambda refs, val: Sketcher.Constraint("Radius", refs[0][0], float(val)),
    "Diameter": lambda refs, val: Sketcher.Constraint("Diameter", refs[0][0], float(val)),
    "Equal": lambda refs, val: Sketcher.Constraint("Equal", refs[0][0], refs[1][0]),
    "Parallel": lambda refs, val: Sketcher.Constraint("Parallel", refs[0][0], refs[1][0]),
    "Perpendicular": lambda refs, val: Sketcher.Constraint(
        "Perpendicular", refs[0][0], refs[1][0]
    ),
    "Tangent": lambda refs, val: Sketcher.Constraint("Tangent", refs[0][0], refs[1][0]),
    "Block": lambda refs, val: Sketcher.Constraint("Block", refs[0][0]),
    "Symmetric": lambda refs, val: Sketcher.Constraint(
        "Symmetric", refs[0][0], refs[0][1], refs[1][0], refs[1][1], refs[2][0], refs[2][1]
    ),
    "Angle": lambda refs, val: Sketcher.Constraint(
        "Angle", refs[0][0], refs[1][0], float(val)
    ),
}


@handler("add_sketch_constraint")
def _h_add_sketch_constraint(p):
    """Add a constraint to a sketch. refs is a list of [geom_idx, vertex_role]
    pairs; vertex_role: 1=start, 2=end, 3=center, 0=edge. value is the numeric
    value for dimensional constraints."""
    sketch = _resolve_sketch(p["sketch"])
    kind = p["type"]
    if kind not in _SKETCH_CONSTRAINT_BUILDERS:
        raise ValueError(f"unknown sketch constraint type: {kind!r}")
    refs_in = p["refs"]
    refs = []
    for r in refs_in:
        if isinstance(r, (list, tuple)):
            refs.append((int(r[0]), int(r[1])))
        else:
            refs.append((int(r), 0))
    value = p.get("value")
    constraint = _SKETCH_CONSTRAINT_BUILDERS[kind](refs, value)
    idx = sketch.addConstraint(constraint)
    sketch.recompute()
    return {"index": idx, "type": kind}


@handler("close_sketch")
def _h_close_sketch(p):
    """Recompute the sketch and return DOF status. fully_constrained is True iff
    the solver reports zero remaining degrees of freedom."""
    sketch = _resolve_sketch(p["sketch"])
    sketch.recompute()
    open_v = sketch.OpenVertices
    if not isinstance(open_v, int):
        open_v = len(open_v)
    return {
        "geometry_count": len(sketch.Geometry),
        "constraint_count": len(sketch.Constraints),
        "open_vertices": open_v,
        "dof": sketch.DoF,
        "fully_constrained": bool(sketch.FullyConstrained),
        "conflicting": list(sketch.ConflictingConstraints),
        "redundant": list(sketch.RedundantConstraints),
        "malformed": list(sketch.MalformedConstraints),
    }


def _add_to_body_of_sketch(sketch, feature):
    """Find the Body containing this sketch and add the feature into it."""
    for o in App.ActiveDocument.Objects:
        if o.isDerivedFrom("PartDesign::Body") and sketch in o.Group:
            o.addObject(feature)
            return o
    raise RuntimeError("sketch is not inside a PartDesign::Body")


@handler("pad")
def _h_pad(p):
    """Pad a sketch by `length` mm. symmetric=True extrudes both directions."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    pad = doc.addObject("PartDesign::Pad", p.get("name", "Pad"))
    pad.Profile = sketch
    pad.Length = float(p.get("length", 10.0))
    pad.Midplane = bool(p.get("symmetric", False))
    pad.Reversed = bool(p.get("reversed", False))
    _add_to_body_of_sketch(sketch, pad)
    doc.recompute()
    h = _register("pad", pad)
    return {"handle": h, "name": pad.Name, "volume": pad.Shape.Volume}


@handler("pocket")
def _h_pocket(p):
    """Pocket (subtract) a sketch from the body. through_all=True ignores length."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    pocket = doc.addObject("PartDesign::Pocket", p.get("name", "Pocket"))
    pocket.Profile = sketch
    if p.get("through_all"):
        pocket.Type = 1  # ThroughAll
    else:
        pocket.Length = float(p.get("length", 10.0))
    pocket.Reversed = bool(p.get("reversed", False))
    _add_to_body_of_sketch(sketch, pocket)
    doc.recompute()
    h = _register("pocket", pocket)
    return {"handle": h, "name": pocket.Name, "volume": pocket.Shape.Volume}


@handler("revolve")
def _h_revolve(p):
    """Revolve a sketch around a body-axis ('X','Y','Z') by `angle` degrees."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    body = _add_to_body_of_sketch(sketch, sketch)  # already in body; re-resolve
    rev = doc.addObject("PartDesign::Revolution", p.get("name", "Revolution"))
    rev.Profile = sketch
    axis = p.get("axis", "Y").upper()
    axis_map = {"X": "X_Axis", "Y": "Y_Axis", "Z": "Z_Axis"}
    if axis in axis_map:
        for o in body.Origin.OriginFeatures:
            if o.isDerivedFrom("App::Line") and axis_map[axis] in o.Label:
                rev.ReferenceAxis = (o, [""])
                break
    rev.Angle = float(p.get("angle", 360.0))
    rev.Reversed = bool(p.get("reversed", False))
    body.addObject(rev)
    doc.recompute()
    h = _register("revolve", rev)
    return {"handle": h, "name": rev.Name, "volume": rev.Shape.Volume}


@handler("partdesign_fillet")
def _h_partdesign_fillet(p):
    """PartDesign Fillet on edges of the body's current tip. edges accepts tags
    (resolved against the body tip) or 'EdgeN' index strings."""
    doc = _active_doc()
    feature_h = p["feature"]
    feature = _resolve(feature_h)
    body = None
    for o in doc.Objects:
        if o.isDerivedFrom("PartDesign::Body") and feature in o.Group:
            body = o
            break
    if body is None:
        raise RuntimeError(f"feature {feature_h!r} not in any Body")

    edge_refs = []
    for ref in p.get("edges", []):
        if isinstance(ref, str) and ref.startswith("e_"):
            r = _h_resolve_edge({"handle": feature_h, "tag": ref})
            edge_refs.append(r["index"])
        elif isinstance(ref, str) and ref.startswith("Edge"):
            edge_refs.append(ref)
        else:
            edge_refs.append(f"Edge{int(ref)}")

    fillet = doc.addObject("PartDesign::Fillet", p.get("name", "PdFillet"))
    fillet.Base = (feature, edge_refs)
    fillet.Radius = float(p.get("radius", 1.0))
    body.addObject(fillet)
    doc.recompute()
    h = _register("pd_fillet", fillet)
    return {"handle": h, "name": fillet.Name, "volume": fillet.Shape.Volume}


@handler("partdesign_chamfer")
def _h_partdesign_chamfer(p):
    doc = _active_doc()
    feature_h = p["feature"]
    feature = _resolve(feature_h)
    body = None
    for o in doc.Objects:
        if o.isDerivedFrom("PartDesign::Body") and feature in o.Group:
            body = o
            break
    if body is None:
        raise RuntimeError(f"feature {feature_h!r} not in any Body")

    edge_refs = []
    for ref in p.get("edges", []):
        if isinstance(ref, str) and ref.startswith("e_"):
            r = _h_resolve_edge({"handle": feature_h, "tag": ref})
            edge_refs.append(r["index"])
        elif isinstance(ref, str) and ref.startswith("Edge"):
            edge_refs.append(ref)
        else:
            edge_refs.append(f"Edge{int(ref)}")

    chamfer = doc.addObject("PartDesign::Chamfer", p.get("name", "PdChamfer"))
    chamfer.Base = (feature, edge_refs)
    chamfer.Size = float(p.get("size", 1.0))
    body.addObject(chamfer)
    doc.recompute()
    h = _register("pd_chamfer", chamfer)
    return {"handle": h, "name": chamfer.Name, "volume": chamfer.Shape.Volume}


def _body_of(feature):
    """Find the PartDesign::Body that owns `feature`."""
    for o in App.ActiveDocument.Objects:
        if o.isDerivedFrom("PartDesign::Body") and feature in o.Group:
            return o
    raise RuntimeError(f"object {feature.Name!r} is not in any Body")


def _origin_axis(body, axis_name):
    """Return the body's origin axis (App::Line) by short name 'X'/'Y'/'Z'.
    Matches on Name (e.g. 'X_Axis') rather than Label, which uses dashes
    ('X-axis') and would break a startswith check."""
    target = {"X": "X_Axis", "Y": "Y_Axis", "Z": "Z_Axis"}[axis_name]
    for o in body.Origin.OriginFeatures:
        if o.isDerivedFrom("App::Line") and o.Name == target:
            return o
    raise RuntimeError(f"could not find {axis_name} origin axis in body")


def _origin_plane(body, plane_name):
    """Return the body's origin plane (App::Plane) by short name 'XY'/'XZ'/'YZ'.
    Matches on Name (e.g. 'XY_Plane')."""
    target = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}[plane_name]
    for o in body.Origin.OriginFeatures:
        if o.isDerivedFrom("App::Plane") and o.Name == target:
            return o
    raise RuntimeError(f"could not find {plane_name} origin plane in body")


def _resolve_direction_ref(body, ref):
    """Resolve a pattern Direction value. Accepts 'X'|'Y'|'Z' (origin axes) or
    {handle, edge: 'e_...'|'EdgeN'} for an edge-aligned direction. Returns the
    (object, [sub]) tuple FreeCAD expects."""
    if isinstance(ref, str) and ref.upper() in ("X", "Y", "Z"):
        return (_origin_axis(body, ref.upper()), [""])
    if isinstance(ref, dict):
        h = ref["handle"]
        edge = ref.get("edge") or ref.get("tag")
        obj = _shape_handle_to_obj(h)
        if isinstance(edge, str) and edge.startswith("e_"):
            idx = _h_resolve_edge({"handle": h, "tag": edge})["index"]
        elif isinstance(edge, str) and edge.startswith("Edge"):
            idx = edge
        else:
            raise ValueError(f"direction edge must be tag or 'EdgeN': {edge!r}")
        return (obj, [idx])
    raise ValueError(f"unrecognized direction ref: {ref!r}")


def _resolve_axis_ref(body, ref):
    """Resolve a polar-pattern Axis value. Same shape as direction: 'X'|'Y'|'Z'
    or {handle, edge}."""
    return _resolve_direction_ref(body, ref)


def _resolve_mirror_plane_ref(body, ref):
    """Resolve a Mirrored.MirrorPlane value. Accepts 'XY'|'XZ'|'YZ' (origin
    planes), a datum-plane handle string, or {handle, face: 'f_...'|'FaceN'}."""
    if isinstance(ref, str) and ref.upper() in ("XY", "XZ", "YZ"):
        return (_origin_plane(body, ref.upper()), [""])
    if isinstance(ref, dict):
        h = ref["handle"]
        face = ref.get("face") or ref.get("tag")
        obj = _shape_handle_to_obj(h)
        if isinstance(face, str) and face.startswith("f_"):
            idx = _h_resolve_face({"handle": h, "tag": face})["index"]
        elif isinstance(face, str) and face.startswith("Face"):
            idx = face
        else:
            raise ValueError(f"mirror face ref must be tag or 'FaceN': {face!r}")
        return (obj, [idx])
    if isinstance(ref, str):
        # Treat plain string as a handle to a datum plane.
        return (_resolve(ref), [""])
    raise ValueError(f"unrecognized mirror plane ref: {ref!r}")


@handler("hole")
def _h_hole(p):
    """Drill a parametric hole from a sketch containing one or more circles.
    sketch: sketch handle (must lie inside a body that already has material).
    diameter: mm.
    depth_type: 'Dimension' (uses depth) | 'ThroughAll'. Default 'ThroughAll'.
    depth: mm (used only when depth_type='Dimension').
    cut_type: 'None' | 'Counterbore' | 'Countersink' | 'Counterdrill'. Default 'None'.
    cut_diameter / cut_depth: mm (used when cut_type != 'None').
    threaded: bool to enable tap (sets Threaded=True). Optional thread_type +
    thread_size (must be valid enum values for the chosen thread_type)."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    body = _body_of(sketch)

    hole = doc.addObject("PartDesign::Hole", p.get("name", "Hole"))
    body.addObject(hole)
    hole.Profile = sketch
    hole.Diameter = float(p.get("diameter", 5.0))

    depth_type = p.get("depth_type", "ThroughAll")
    if depth_type not in ("Dimension", "ThroughAll"):
        raise ValueError(
            f"depth_type must be 'Dimension' or 'ThroughAll' (got {depth_type!r})"
        )
    hole.DepthType = depth_type
    if depth_type == "Dimension":
        hole.Depth = float(p.get("depth", 10.0))

    cut_type = p.get("cut_type", "None")
    if cut_type not in ("None", "Counterbore", "Countersink", "Counterdrill"):
        raise ValueError(
            f"cut_type must be one of None|Counterbore|Countersink|Counterdrill "
            f"(got {cut_type!r})"
        )
    hole.HoleCutType = cut_type
    if cut_type != "None":
        if "cut_diameter" in p:
            hole.HoleCutDiameter = float(p["cut_diameter"])
        if "cut_depth" in p:
            hole.HoleCutDepth = float(p["cut_depth"])

    if p.get("threaded"):
        hole.Threaded = True
        if "thread_type" in p:
            hole.ThreadType = p["thread_type"]
        if "thread_size" in p:
            hole.ThreadSize = p["thread_size"]
        if p.get("model_thread"):
            hole.ModelThread = True

    hole.Reversed = bool(p.get("reversed", False))
    doc.recompute()
    h = _register("hole", hole)
    return {"handle": h, "name": hole.Name, "volume": hole.Shape.Volume}


@handler("linear_pattern")
def _h_linear_pattern(p):
    """Repeat a feature linearly. feature: handle of a PartDesign feature.
    direction: 'X'|'Y'|'Z' (origin axis) or {handle, edge: tag|'EdgeN'}.
    length: total span (mm). occurrences: int >= 2."""
    doc = _active_doc()
    feature = _resolve(p["feature"])
    body = _body_of(feature)
    lp = doc.addObject("PartDesign::LinearPattern", p.get("name", "LinearPattern"))
    body.addObject(lp)
    lp.Originals = [feature]
    lp.Direction = _resolve_direction_ref(body, p.get("direction", "X"))
    lp.Length = float(p.get("length", 10.0))
    occ = int(p.get("occurrences", 2))
    if occ < 2:
        raise ValueError(f"occurrences must be >= 2, got {occ}")
    lp.Occurrences = occ
    lp.Reversed = bool(p.get("reversed", False))
    doc.recompute()
    h = _register("linear_pattern", lp)
    return {"handle": h, "name": lp.Name, "volume": lp.Shape.Volume}


@handler("polar_pattern")
def _h_polar_pattern(p):
    """Repeat a feature around an axis. feature: handle. axis: 'X'|'Y'|'Z' or
    {handle, edge}. angle_deg: total swept angle (default 360). occurrences: int >= 2."""
    doc = _active_doc()
    feature = _resolve(p["feature"])
    body = _body_of(feature)
    pp = doc.addObject("PartDesign::PolarPattern", p.get("name", "PolarPattern"))
    body.addObject(pp)
    pp.Originals = [feature]
    pp.Axis = _resolve_axis_ref(body, p.get("axis", "Z"))
    pp.Angle = float(p.get("angle_deg", 360.0))
    occ = int(p.get("occurrences", 2))
    if occ < 2:
        raise ValueError(f"occurrences must be >= 2, got {occ}")
    pp.Occurrences = occ
    pp.Reversed = bool(p.get("reversed", False))
    doc.recompute()
    h = _register("polar_pattern", pp)
    return {"handle": h, "name": pp.Name, "volume": pp.Shape.Volume}


@handler("mirrored")
def _h_mirrored(p):
    """Mirror a feature across a plane. feature: handle. plane: 'XY'|'XZ'|'YZ',
    a datum-plane handle, or {handle, face: tag|'FaceN'}."""
    doc = _active_doc()
    feature = _resolve(p["feature"])
    body = _body_of(feature)
    mr = doc.addObject("PartDesign::Mirrored", p.get("name", "Mirrored"))
    body.addObject(mr)
    mr.Originals = [feature]
    mr.MirrorPlane = _resolve_mirror_plane_ref(body, p.get("plane", "YZ"))
    doc.recompute()
    h = _register("mirrored", mr)
    return {"handle": h, "name": mr.Name, "volume": mr.Shape.Volume}


@handler("loft")
def _h_loft(p):
    """Loft (additive) between two or more sketches. sketches: list of sketch
    handles, ordered. The first sketch is the Profile, the rest are Sections.
    closed: connects last to first (toroidal). ruled: straight ruled surfaces
    between adjacent sections (vs smooth bspline)."""
    doc = _active_doc()
    handles = p["sketches"]
    if not isinstance(handles, list) or len(handles) < 2:
        raise ValueError(f"loft needs at least 2 sketch handles, got {handles!r}")
    sketches = [_resolve_sketch(h) for h in handles]
    body = _body_of(sketches[0])

    loft = doc.addObject("PartDesign::AdditiveLoft", p.get("name", "Loft"))
    body.addObject(loft)
    loft.Profile = sketches[0]
    # Sections is a list of (sketch, []) tuples in PartDesign convention.
    loft.Sections = [(s, [""]) for s in sketches[1:]]
    loft.Closed = bool(p.get("closed", False))
    loft.Ruled = bool(p.get("ruled", False))
    loft.Reversed = bool(p.get("reversed", False))
    doc.recompute()
    h = _register("loft", loft)
    return {"handle": h, "name": loft.Name, "volume": loft.Shape.Volume}


@handler("sweep")
def _h_sweep(p):
    """Sweep (additive pipe) a profile sketch along a spine. profile: sketch
    handle. spine: sketch handle (defines the path). The two sketches must lie
    in the same body. mode: 'Standard' (default) | 'Frenet' | 'Auxiliary' | 'Binormal'."""
    doc = _active_doc()
    profile = _resolve_sketch(p["profile"])
    spine = _resolve_sketch(p["spine"])
    body = _body_of(profile)

    pipe = doc.addObject("PartDesign::AdditivePipe", p.get("name", "Sweep"))
    body.addObject(pipe)
    pipe.Profile = profile
    # Spine is a (object, [sub_list]) tuple; for a sketch use the entire sketch.
    pipe.Spine = (spine, [""])
    pipe.Mode = p.get("mode", "Standard")
    pipe.Transition = p.get("transition", "Transformed")
    doc.recompute()
    h = _register("sweep", pipe)
    return {"handle": h, "name": pipe.Name, "volume": pipe.Shape.Volume}


@handler("helix")
def _h_helix(p):
    """Generate a Part::Helix primitive (a 1D helical curve). radius (mm),
    pitch (mm/turn), height (mm), angle (cone angle, default 0 for cylindrical).
    Returns a handle to the helix curve — wrap with a Part::Sweep externally
    to make a 3D helical solid (e.g. for threads)."""
    doc = _active_doc()
    helix = doc.addObject("Part::Helix", p.get("name", "Helix"))
    helix.Radius = float(p.get("radius", 5.0))
    helix.Pitch = float(p.get("pitch", 2.0))
    helix.Height = float(p.get("height", 10.0))
    helix.Angle = float(p.get("angle", 0.0))
    doc.recompute()
    h = _register("helix", helix)
    return {"handle": h, "name": helix.Name}


@handler("thickness")
def _h_thickness(p):
    """Hollow out a solid into a shell. base: handle of the body's tip feature.
    open_faces: list of {handle, face|tag} that become the shell's openings
    (the faces removed to expose the interior). thickness: wall thickness (mm).
    reversed: outward shell vs inward (default inward, removing material)."""
    doc = _active_doc()
    base = _resolve(p["base"])
    body = _body_of(base)

    face_indices = []
    for r in p.get("open_faces") or []:
        h = r["handle"]
        sub = r.get("face") or r.get("tag")
        if isinstance(sub, str) and sub.startswith("f_"):
            idx = _h_resolve_face({"handle": h, "tag": sub})["index"]
        elif isinstance(sub, str) and sub.startswith("Face"):
            idx = sub
        else:
            raise ValueError(f"thickness open_face needs tag or 'FaceN': {sub!r}")
        face_indices.append(idx)

    thick = doc.addObject("PartDesign::Thickness", p.get("name", "Thickness"))
    body.addObject(thick)
    thick.Base = (base, face_indices)
    thick.Value = float(p.get("thickness", 1.0))
    # FreeCAD's PartDesign::Thickness.Reversed defaults to True (inward shell).
    # We preserve that default — overriding to False would grow the shell outward
    # which is rarely what an agent asking for "wall thickness" wants.
    thick.Reversed = bool(p.get("reversed", True))
    if "join" in p:
        thick.Join = p["join"]  # 'Arc' or 'Intersection'
    if "mode" in p:
        thick.Mode = p["mode"]  # 'Skin' | 'Pipe' | 'RectoVerso'
    doc.recompute()
    h = _register("thickness", thick)
    return {"handle": h, "name": thick.Name, "volume": thick.Shape.Volume}


@handler("draft")
def _h_draft(p):
    """Apply a draft angle to faces (for moldability). base: feature handle.
    faces: list of {handle, face|tag}. neutral_plane: {handle, face|tag} for
    the plane along which the angle is measured. angle_deg: float (positive).
    reversed: flip the draft direction."""
    doc = _active_doc()
    base = _resolve(p["base"])
    body = _body_of(base)

    face_indices = []
    for r in p.get("faces") or []:
        h = r["handle"]
        sub = r.get("face") or r.get("tag")
        if isinstance(sub, str) and sub.startswith("f_"):
            idx = _h_resolve_face({"handle": h, "tag": sub})["index"]
        elif isinstance(sub, str) and sub.startswith("Face"):
            idx = sub
        else:
            raise ValueError(f"draft face needs tag or 'FaceN': {sub!r}")
        face_indices.append(idx)

    draft = doc.addObject("PartDesign::Draft", p.get("name", "Draft"))
    body.addObject(draft)
    draft.Base = (base, face_indices)
    draft.Angle = float(p.get("angle_deg", 1.0))
    draft.Reversed = bool(p.get("reversed", False))

    np_ref = p.get("neutral_plane")
    if np_ref is not None:
        h = np_ref["handle"]
        sub = np_ref.get("face") or np_ref.get("tag")
        if isinstance(sub, str) and sub.startswith("f_"):
            idx = _h_resolve_face({"handle": h, "tag": sub})["index"]
        elif isinstance(sub, str) and sub.startswith("Face"):
            idx = sub
        else:
            raise ValueError(f"draft neutral_plane needs tag or 'FaceN': {sub!r}")
        draft.NeutralPlane = (_shape_handle_to_obj(h), [idx])

    doc.recompute()
    h_out = _register("draft", draft)
    return {"handle": h_out, "name": draft.Name, "volume": draft.Shape.Volume}


@handler("add_sketch_external")
def _h_add_sketch_external(p):
    """Project an external edge/vertex/face into a sketch as construction geometry
    that subsequent constraints can reference. ref: {handle, edge|face|vertex}
    or {handle, tag} where tag is e_/f_ prefixed.
    Returns {external_index} where external_index is the negative index by which
    the projected element is addressed in subsequent constraint refs."""
    sketch = _resolve_sketch(p["sketch"])
    ref = p["ref"]
    h = ref["handle"]
    obj = _shape_handle_to_obj(h)

    sub = ref.get("edge") or ref.get("face") or ref.get("vertex") or ref.get("tag")
    if not isinstance(sub, str):
        raise ValueError(f"sketch external ref needs edge|face|vertex|tag: {ref!r}")
    if sub.startswith("e_"):
        idx = _h_resolve_edge({"handle": h, "tag": sub})["index"]
    elif sub.startswith("f_"):
        idx = _h_resolve_face({"handle": h, "tag": sub})["index"]
    elif sub.startswith(("Edge", "Face", "Vertex")):
        idx = sub
    else:
        raise ValueError(f"sketch external sub must be tag or Edge/Face/VertexN: {sub!r}")

    sketch.addExternal(obj.Name, idx)
    sketch.recompute()
    # Sketcher numbers external geometry from -3 downward (-1, -2 are reserved for
    # axes/origin internals). The most-recently added external geom's index is the
    # current count negated and offset by 2.
    ext_count = len(sketch.ExternalGeometry)
    return {"external_index": -(ext_count + 2), "external_count": ext_count}


@handler("get_object")
def _h_get_object(p):
    """Dump a handle's properties. Returns {name, type, label, properties: {...}}.
    Property values are JSON-coerced — Quantity → float (mm/deg), Vector → list,
    Placement → {position, rotation_axis, rotation_angle}."""
    obj = _resolve(p["handle"])
    out = {
        "name": obj.Name,
        "type": obj.TypeId,
        "label": obj.Label,
        "properties": {},
    }
    for prop in obj.PropertiesList:
        try:
            v = getattr(obj, prop)
        except Exception:
            continue
        out["properties"][prop] = _coerce_property(v)
    if hasattr(obj, "Shape") and not obj.Shape.isNull():
        out["volume"] = obj.Shape.Volume
        out["area"] = obj.Shape.Area
    return out


def _coerce_property(v):
    if isinstance(v, (int, float, bool, str)) or v is None:
        return v
    if hasattr(v, "Value") and hasattr(v, "Unit"):  # Quantity
        return float(v.Value)
    if hasattr(v, "x") and hasattr(v, "y") and hasattr(v, "z"):  # Vector
        return [v.x, v.y, v.z]
    if hasattr(v, "Base") and hasattr(v, "Rotation"):  # Placement
        b = v.Base
        r = v.Rotation
        return {
            "position": [b.x, b.y, b.z],
            "rotation_axis": [r.Axis.x, r.Axis.y, r.Axis.z],
            "rotation_angle_deg": r.Angle * 180.0 / 3.141592653589793,
        }
    if isinstance(v, (list, tuple)):
        return [_coerce_property(x) for x in v]
    return repr(v)


@handler("set_property")
def _h_set_property(p):
    """Set a single property by name. Coerces float lists to Vector for Vector
    properties; passes scalars through to Quantity properties."""
    obj = _resolve(p["handle"])
    name = p["name"]
    if name not in obj.PropertiesList:
        raise KeyError(f"object {obj.Name} has no property {name!r}")
    cur = getattr(obj, name)
    val = p["value"]
    if hasattr(cur, "x") and hasattr(cur, "y") and hasattr(cur, "z") and isinstance(val, (list, tuple)):
        setattr(obj, name, App.Vector(*val))
    else:
        setattr(obj, name, val)
    if App.ActiveDocument is not None:
        App.ActiveDocument.recompute()
    return {"name": obj.Name, "property": name, "value": _coerce_property(getattr(obj, name))}


# --- script escape hatch ------------------------------------------------------

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


# --- mass properties / assembly / drawings -----------------------------------

def _solid_of(shape):
    """PartDesign features sometimes wrap their result in a Part.Compound.
    CenterOfMass and MatrixOfInertia live on Solids. Unwrap if necessary."""
    if hasattr(shape, "Solids") and shape.Solids:
        # Prefer the largest solid in case the compound has multiple.
        return max(shape.Solids, key=lambda s: s.Volume)
    return shape


@handler("mass_properties")
def _h_mass_properties(p):
    """Volume, surface area, CG, mass (if density given), and principal inertia
    of a shaped object. density in kg/mm³ (e.g. steel = 7.9e-6)."""
    _, shape = _shape_of(p["handle"])
    solid = _solid_of(shape)
    cg = solid.CenterOfMass
    inertia = solid.MatrixOfInertia
    out = {
        "volume_mm3": shape.Volume,
        "surface_area_mm2": shape.Area,
        "center_of_mass_mm": [cg.x, cg.y, cg.z],
        "bounding_box_mm": [
            shape.BoundBox.XMin, shape.BoundBox.YMin, shape.BoundBox.ZMin,
            shape.BoundBox.XMax, shape.BoundBox.YMax, shape.BoundBox.ZMax,
        ],
        "inertia_tensor": [
            [inertia.A11, inertia.A12, inertia.A13],
            [inertia.A21, inertia.A22, inertia.A23],
            [inertia.A31, inertia.A32, inertia.A33],
        ],
    }
    if "density" in p:
        density = float(p["density"])
        mass = shape.Volume * density
        out["density_kg_per_mm3"] = density
        out["mass_kg"] = mass
    return out


@handler("make_assembly")
def _h_make_assembly(p):
    """Create an App::Part container in the active doc to hold linked parts."""
    doc = _active_doc()
    asm = doc.addObject("App::Part", p.get("name", "Assembly"))
    doc.recompute()
    h = _register("assembly", asm)
    return {"handle": h, "name": asm.Name}


@handler("add_part")
def _h_add_part(p):
    """Add a part to an assembly. Source can be:
      - {"handle": "<existing-handle>"} — links a body already in the doc
      - {"path": "/path/to/part.FCStd", "object": "<name>"} — opens the file,
        links the named object (or the first PartDesign Body if not specified)
    placement: optional [x, y, z] translation; or full {position: [...], axis: [...], angle_deg: ...}.
    """
    doc = _active_doc()
    assembly = _resolve(p["assembly"])
    source = p["source"]

    if "handle" in source:
        target = _resolve(source["handle"])
    elif "path" in source:
        ext_doc = App.openDocument(source["path"], True)  # True = hidden
        wanted = source.get("object")
        if wanted:
            target = ext_doc.getObject(wanted)
            if target is None:
                raise KeyError(f"no object {wanted!r} in {source['path']}")
        else:
            for o in ext_doc.Objects:
                if o.isDerivedFrom("PartDesign::Body") or o.isDerivedFrom("Part::Feature"):
                    target = o
                    break
            else:
                raise RuntimeError(f"no shaped object in {source['path']}")
        App.setActiveDocument(doc.Name)
    else:
        raise ValueError(f"source must have 'handle' or 'path': {source!r}")

    link = doc.addObject("App::Link", p.get("name", "Part"))
    link.LinkedObject = target
    assembly.addObject(link)

    placement = p.get("placement")
    if placement is not None:
        if isinstance(placement, list) and len(placement) == 3:
            link.Placement = App.Placement(
                App.Vector(*placement), App.Rotation()
            )
        elif isinstance(placement, dict):
            pos = App.Vector(*placement.get("position", [0, 0, 0]))
            axis = App.Vector(*placement.get("axis", [0, 0, 1]))
            angle = float(placement.get("angle_deg", 0))
            link.Placement = App.Placement(pos, App.Rotation(axis, angle))

    doc.recompute()
    h = _register("link", link)
    return {"handle": h, "name": link.Name, "linked": target.Name}


@handler("list_assembly_parts")
def _h_list_assembly_parts(p):
    asm = _resolve(p["assembly"])
    parts = []
    for o in asm.Group:
        info = {"name": o.Name, "type": o.TypeId, "label": o.Label}
        if o.isDerivedFrom("App::Link") and o.LinkedObject is not None:
            info["linked"] = o.LinkedObject.Name
        if hasattr(o, "Shape") and not o.Shape.isNull():
            info["volume"] = o.Shape.Volume
        if hasattr(o, "Placement"):
            pos = o.Placement.Base
            info["position"] = [pos.x, pos.y, pos.z]
        parts.append(info)
    return parts


def _world_shape(obj):
    """Return obj.Shape transformed to world coordinates by its placement
    chain. App::Link wraps a base shape and applies its own Placement."""
    if obj.isDerivedFrom("App::Link") and obj.LinkedObject is not None:
        base = obj.LinkedObject
        if hasattr(base, "Shape") and not base.Shape.isNull():
            return base.Shape.transformed(obj.Placement.Matrix)
    if hasattr(obj, "Shape") and not obj.Shape.isNull():
        return obj.Shape.transformed(obj.Placement.Matrix)
    return None


@handler("interference_check")
def _h_interference_check(p):
    """Pairwise interference: compute volume of intersection between every
    pair of parts in the assembly. Returns list of overlapping pairs, ordered
    by descending interference volume."""
    asm = _resolve(p["assembly"])
    parts = list(asm.Group)
    shapes = []
    for o in parts:
        s = _world_shape(o)
        if s is not None:
            shapes.append((o.Name, s))

    overlaps = []
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            name_a, sa = shapes[i]
            name_b, sb = shapes[j]
            try:
                common = sa.common(sb)
                vol = common.Volume
            except Exception:
                vol = 0.0
            if vol > 1e-6:
                overlaps.append({
                    "a": name_a,
                    "b": name_b,
                    "interference_mm3": vol,
                })
    overlaps.sort(key=lambda x: -x["interference_mm3"])
    return overlaps


@handler("bom_extract")
def _h_bom_extract(p):
    """Walk an assembly, group its parts by linked object name, return a BOM:
    [{part, count, total_volume_mm3, total_mass_kg?}, ...]. density is
    optional kg/mm³; if given, each row's mass is computed."""
    asm = _resolve(p["assembly"])
    density = float(p["density"]) if "density" in p else None
    counts = {}
    for o in asm.Group:
        if o.isDerivedFrom("App::Link") and o.LinkedObject is not None:
            base = o.LinkedObject
            key = base.Name
        else:
            key = o.Name
        s = _world_shape(o)
        v = s.Volume if s is not None else 0.0
        if key not in counts:
            counts[key] = {"part": key, "count": 0, "total_volume_mm3": 0.0}
        counts[key]["count"] += 1
        counts[key]["total_volume_mm3"] += v
    rows = list(counts.values())
    if density is not None:
        for r in rows:
            r["total_mass_kg"] = r["total_volume_mm3"] * density
    rows.sort(key=lambda r: -r["count"])
    return rows


@handler("make_drawing_page")
def _h_make_drawing_page(p):
    """Create a TechDraw page using a built-in template (A4_Landscape_TD by
    default). Returns handle."""
    import TechDraw  # noqa: F401
    import os as _os
    doc = _active_doc()
    page = doc.addObject("TechDraw::DrawPage", p.get("name", "Page"))
    template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
    fc_resource = App.getResourceDir()
    template_path = p.get("template")
    if template_path is None:
        # FreeCAD's template names changed between 0.21 / 1.0 / 1.1; search.
        roots = [
            _os.path.join(fc_resource, "Mod", "TechDraw", "Templates"),
            _os.path.join(fc_resource, "share", "Mod", "TechDraw", "Templates"),
        ]
        for root in roots:
            if not _os.path.isdir(root):
                continue
            for fn in _os.listdir(root):
                if fn.lower().endswith(".svg") and "landscape" in fn.lower():
                    template_path = _os.path.join(root, fn)
                    break
            if template_path:
                break
        if template_path is None:
            raise RuntimeError(
                f"no built-in TechDraw template found under {roots}"
            )
    template.Template = template_path
    page.Template = template
    doc.recompute()
    h = _register("page", page)
    return {"handle": h, "name": page.Name, "template": template_path}


@handler("add_projection_group")
def _h_add_projection_group(p):
    """Add a multi-view projection group of `body` to a TechDraw page.
    views: list of view names. FreeCAD uses single-letter codes:
      "Front", "Top", "Bottom", "Left", "Right", "Rear", "FrontTopLeft", etc.
    """
    doc = _active_doc()
    page = _resolve(p["page"])
    body_obj = _shape_handle_to_obj(p["body"])

    pg = doc.addObject("TechDraw::DrawProjGroup", p.get("name", "ProjGroup"))
    page.addView(pg)
    pg.Source = [body_obj]
    pg.ScaleType = "Automatic"
    pg.ProjectionType = "Third angle"

    views = p.get("views", ["Front", "Top", "Right"])
    for v in views:
        pg.addProjection(v)

    if hasattr(pg, "Anchor") and pg.Anchor is None:
        pg.Anchor = pg.Views[0] if pg.Views else None

    doc.recompute()
    h = _register("projgroup", pg)
    return {
        "handle": h,
        "name": pg.Name,
        "views": [v.Type if hasattr(v, "Type") else v.Name for v in pg.Views],
    }


@handler("export_drawing")
def _h_export_drawing(p):
    """Export a TechDraw page to PDF/SVG.

    NOTE: FreeCAD 1.1's TechDraw export functions live in `TechDrawGui`, which
    is not available under `freecadcmd`. So PDF/SVG export from a headless
    DriftPin worker is currently impossible. Workaround: save the .FCStd
    (Slice 0 `save_document`) and open it in FreeCAD's GUI to export. The
    drawing page itself — projection groups, views, dimensions — is fully
    constructed by the worker and persists in the saved document.
    """
    raise NotImplementedError(
        "TechDraw PDF/SVG export requires TechDrawGui, unavailable headless. "
        "Save the .FCStd via save_document and export from FreeCAD GUI."
    )


# --- tessellation -------------------------------------------------------------

@handler("tessellate")
def _h_tessellate(p):
    """Tessellate a shaped object's surface into a triangle mesh.
    deflection controls accuracy (mm); smaller = finer, slower.
    Returns {vertices: [[x,y,z],...], triangles: [[i,j,k],...], bbox: [xmin,ymin,zmin,xmax,ymax,zmax]}."""
    _, shape = _shape_of(p["handle"])
    deflection = float(p.get("deflection", 0.5))
    verts, tris = shape.tessellate(deflection)
    bb = shape.BoundBox
    return {
        "vertices": [[v.x, v.y, v.z] for v in verts],
        "triangles": [list(t) for t in tris],
        "bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax],
    }


# --- FEM (decomposed) ---------------------------------------------------------

def _resolve_analysis(handle):
    obj = _resolve(handle)
    if not obj.isDerivedFrom("Fem::FemAnalysis"):
        raise TypeError(f"handle {handle!r} is not an Analysis (got {obj.TypeId})")
    return obj


def _shape_handle_to_obj(handle_or_name):
    """Accept either a DriftPin handle or a raw object name."""
    if handle_or_name in _handles:
        return _handles[handle_or_name]
    obj = App.ActiveDocument.getObject(handle_or_name)
    if obj is None:
        raise KeyError(f"no handle or object named {handle_or_name!r}")
    return obj


def _resolve_face_ref(handle, ref):
    """Convert a tag string or 'FaceN' into a (object, 'FaceN') tuple."""
    obj = _shape_handle_to_obj(handle)
    if isinstance(ref, str) and ref.startswith("f_"):
        r = _h_resolve_face({"handle": handle, "tag": ref})
        return (obj, r["index"])
    if isinstance(ref, str) and ref.startswith("Face"):
        return (obj, ref)
    raise ValueError(f"unrecognized face ref: {ref!r}")


def _resolve_edge_ref(handle, ref):
    obj = _shape_handle_to_obj(handle)
    if isinstance(ref, str) and ref.startswith("e_"):
        r = _h_resolve_edge({"handle": handle, "tag": ref})
        return (obj, r["index"])
    if isinstance(ref, str) and ref.startswith("Edge"):
        return (obj, ref)
    raise ValueError(f"unrecognized edge ref: {ref!r}")


def _build_references(refs):
    """refs is a list of {handle, tag} dicts (face) or {handle, edge} dicts.
    Returns the FreeCAD-style References list."""
    out = []
    for r in refs:
        if "face" in r:
            out.append(_resolve_face_ref(r["handle"], r["face"]))
        elif "edge" in r:
            out.append(_resolve_edge_ref(r["handle"], r["edge"]))
        elif "tag" in r:
            tag = r["tag"]
            if tag.startswith("f_"):
                out.append(_resolve_face_ref(r["handle"], tag))
            elif tag.startswith("e_"):
                out.append(_resolve_edge_ref(r["handle"], tag))
            else:
                raise ValueError(f"unknown tag prefix: {tag!r}")
        else:
            raise ValueError(f"reference dict needs 'face', 'edge', or 'tag': {r!r}")
    return out


@handler("fem_new_analysis")
def _h_fem_new_analysis(p):
    doc = _active_doc()
    analysis = ObjectsFem.makeAnalysis(doc, p.get("name", "Analysis"))
    doc.recompute()
    h = _register("analysis", analysis)
    return {"handle": h, "name": analysis.Name}


_CCX_TUNABLES = {
    "GeometricalNonlinearity",
    "ThermoMechSteadyState",
    "MatrixSolverType",
    "IterationsControlParameterTimeUse",
    "AnalysisType",
    "EigenmodesCount",
    "EigenmodeHighLimit",
    "EigenmodeLowLimit",
    "TimeInitialStep",
    "TimeEnd",
    "BucklingFactors",
}


@handler("fem_set_solver")
def _h_fem_set_solver(p):
    """Add a solver to an analysis. kind='ccx' (CalculiX) | 'elmer'.
    Tunables are passed through to the solver object's properties."""
    doc = _active_doc()
    analysis = _resolve_analysis(p["analysis"])
    kind = p.get("kind", "ccx")
    name = p.get("name", "Solver")
    if kind == "ccx":
        solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, name)
    elif kind == "elmer":
        solver = ObjectsFem.makeSolverElmer(doc, name)
    else:
        raise ValueError(f"unknown solver kind: {kind!r}")

    tunables = p.get("tunables") or {}
    # Sensible defaults for CCX so an agent doesn't have to set them.
    if kind == "ccx":
        tunables.setdefault("GeometricalNonlinearity", "linear")
        tunables.setdefault("ThermoMechSteadyState", True)
        tunables.setdefault("MatrixSolverType", "default")
        tunables.setdefault("IterationsControlParameterTimeUse", False)

    for prop, value in tunables.items():
        if prop not in solver.PropertiesList:
            raise KeyError(
                f"solver has no property {prop!r}; "
                f"available: {sorted(p for p in solver.PropertiesList if not p.startswith('_'))[:20]}"
            )
        setattr(solver, prop, value)

    analysis.addObject(solver)
    doc.recompute()
    h = _register(kind, solver)
    return {"handle": h, "name": solver.Name, "kind": kind}


@handler("fem_set_material")
def _h_fem_set_material(p):
    """Add a material to an analysis, bound to a body handle.
    material is a dict with at minimum: YoungsModulus (e.g. '210000 MPa'),
    PoissonRatio (e.g. '0.30'), Density (e.g. '7900 kg/m^3'). Name is optional."""
    doc = _active_doc()
    analysis = _resolve_analysis(p["analysis"])
    body_h = p["body"]
    body_obj = _shape_handle_to_obj(body_h)
    spec = p["material"]

    mat_obj = ObjectsFem.makeMaterialSolid(doc, p.get("name", "Material"))
    mat = mat_obj.Material
    mat["Name"] = spec.get("Name", "Generic")
    if "YoungsModulus" in spec:
        mat["YoungsModulus"] = spec["YoungsModulus"]
    if "PoissonRatio" in spec:
        mat["PoissonRatio"] = spec["PoissonRatio"]
    if "Density" in spec:
        mat["Density"] = spec["Density"]
    for k, v in spec.items():
        if k in ("Name", "YoungsModulus", "PoissonRatio", "Density"):
            continue
        mat[k] = v
    mat_obj.Material = mat
    mat_obj.References = [(body_obj, "Solid1")]
    analysis.addObject(mat_obj)
    doc.recompute()
    h = _register("material", mat_obj)
    return {"handle": h, "name": mat_obj.Name}


@handler("fem_add_constraint")
def _h_fem_add_constraint(p):
    """Add a constraint to an analysis. Kinds: fixed, force, pressure, displacement.
    refs is a list of {handle, tag} dicts (or {handle, face: 'FaceN'} as escape).
    Force constraints accept `force` (N), `direction` (face|edge ref), `reversed`.
    Pressure accepts `pressure` (MPa), `reversed`.
    Displacement accepts `x`, `y`, `z` (mm) or `xFree`/`yFree`/`zFree` bools."""
    doc = _active_doc()
    analysis = _resolve_analysis(p["analysis"])
    kind = p["kind"]
    refs = _build_references(p.get("refs") or [])

    if kind == "fixed":
        c = ObjectsFem.makeConstraintFixed(doc, p.get("name", "Fixed"))
        c.References = refs
    elif kind == "force":
        c = ObjectsFem.makeConstraintForce(doc, p.get("name", "Force"))
        c.References = refs
        c.Force = float(p["force"])
        if "direction" in p:
            d = p["direction"]
            obj = _shape_handle_to_obj(d["handle"])
            ref = d.get("edge") or d.get("face") or d.get("tag")
            if ref.startswith("e_"):
                idx = _h_resolve_edge({"handle": d["handle"], "tag": ref})["index"]
            elif ref.startswith("f_"):
                idx = _h_resolve_face({"handle": d["handle"], "tag": ref})["index"]
            else:
                idx = ref
            c.Direction = (obj, [idx])
        c.Reversed = bool(p.get("reversed", False))
    elif kind == "pressure":
        c = ObjectsFem.makeConstraintPressure(doc, p.get("name", "Pressure"))
        c.References = refs
        c.Pressure = float(p["pressure"])
        c.Reversed = bool(p.get("reversed", False))
    elif kind == "displacement":
        c = ObjectsFem.makeConstraintDisplacement(doc, p.get("name", "Displacement"))
        c.References = refs
        for axis in ("x", "y", "z"):
            if axis in p:
                setattr(c, f"{axis}Displacement", float(p[axis]))
                setattr(c, f"{axis}Free", False)
            elif p.get(f"{axis}_free") is True:
                setattr(c, f"{axis}Free", True)
    elif kind == "temperature":
        c = ObjectsFem.makeConstraintTemperature(doc, p.get("name", "Temperature"))
        c.References = refs
        c.ConstraintType = "Temperature"
        c.Temperature = float(p["temperature"])
    elif kind == "heatflux":
        c = ObjectsFem.makeConstraintHeatflux(doc, p.get("name", "HeatFlux"))
        c.References = refs
        sub = p.get("flux_type", "DFlux")
        if sub not in ("DFlux", "Convection", "Radiation"):
            raise ValueError(
                f"flux_type must be DFlux|Convection|Radiation (got {sub!r})"
            )
        c.ConstraintType = sub
        if sub == "DFlux":
            c.DFlux = float(p["flux"])
        elif sub == "Convection":
            c.AmbientTemp = float(p["ambient_temp"])
            c.FilmCoef = float(p["film_coef"])
        elif sub == "Radiation":
            c.AmbientTemp = float(p["ambient_temp"])
            c.Emissivity = float(p.get("emissivity", 1.0))
    elif kind == "initial_temperature":
        c = ObjectsFem.makeConstraintInitialTemperature(
            doc, p.get("name", "InitialTemperature"),
        )
        c.References = refs
        c.initialTemperature = float(p["temperature"])
    else:
        raise ValueError(f"unknown constraint kind: {kind!r}")

    analysis.addObject(c)
    doc.recompute()
    h = _register(f"con_{kind}", c)
    return {"handle": h, "name": c.Name, "kind": kind}


@handler("fem_mesh")
def _h_fem_mesh(p):
    """Create a Gmsh mesh on a body handle and add it to an analysis.
    Returns handle + node/element counts."""
    from femmesh.gmshtools import GmshTools
    doc = _active_doc()
    analysis = _resolve_analysis(p["analysis"])
    body_obj = _shape_handle_to_obj(p["body"])
    char_length = float(p.get("char_length", 0.0))

    mesh = ObjectsFem.makeMeshGmsh(doc, p.get("name", "Mesh"))
    mesh.Shape = body_obj
    if char_length > 0:
        mesh.CharacteristicLengthMax = char_length
    doc.recompute()
    analysis.addObject(mesh)

    GmshTools(mesh).create_mesh()
    h = _register("mesh", mesh)
    return {
        "handle": h,
        "name": mesh.Name,
        "nodes": mesh.FemMesh.NodeCount,
        "tets": mesh.FemMesh.TetraCount,
    }


@handler("fem_run")
def _h_fem_run(p):
    """Run the CalculiX solver attached to an analysis. Returns workdir + status."""
    from femtools import ccxtools
    analysis = _resolve_analysis(p["analysis"])
    solver = None
    for o in analysis.Group:
        if o.isDerivedFrom("Fem::FemSolverObjectPython") or "Solver" in o.TypeId:
            solver = o
            break
    if solver is None:
        raise RuntimeError("no solver attached to analysis; call fem_set_solver first")

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
    return {"workdir": workdir, "status": "ok"}


@handler("fem_results")
def _h_fem_results(p):
    """Extract summary results: max von Mises (+location), max displacement
    (+location + vector), top-N hot nodes by stress."""
    analysis = _resolve_analysis(p["analysis"])
    result = None
    for o in analysis.Group:
        if o.isDerivedFrom("Fem::FemResultObject"):
            result = o
            break
    if result is None:
        raise RuntimeError("no result object on analysis (run fem_run first)")

    stress = list(result.vonMises)
    disp_lengths = list(result.DisplacementLengths)
    disp_vectors = list(result.DisplacementVectors)
    nodes = list(result.NodeNumbers) if hasattr(result, "NodeNumbers") else []

    top_n = int(p.get("top_n", 5))
    indexed = sorted(enumerate(stress), key=lambda kv: -kv[1])[:top_n]
    top_stress = [
        {
            "node": nodes[i] if i < len(nodes) else i,
            "vonmises_mpa": s,
            "displacement_mm": disp_lengths[i] if i < len(disp_lengths) else None,
        }
        for i, s in indexed
    ]

    max_disp_idx = disp_lengths.index(max(disp_lengths)) if disp_lengths else None
    if max_disp_idx is not None:
        v = disp_vectors[max_disp_idx]
        max_disp_vec = [v.x, v.y, v.z] if hasattr(v, "x") else list(v)
    else:
        max_disp_vec = None

    return {
        "max_vonmises_mpa": max(stress) if stress else 0.0,
        "max_displacement_mm": max(disp_lengths) if disp_lengths else 0.0,
        "max_displacement_vector": max_disp_vec,
        "top_stress_nodes": top_stress,
    }


def _solver_of(analysis):
    for o in analysis.Group:
        if "Solver" in o.TypeId or o.isDerivedFrom("Fem::FemSolverObjectPython"):
            return o
    raise RuntimeError("no solver attached to analysis; call fem_set_solver first")


@handler("fem_modal")
def _h_fem_modal(p):
    """Configure the analysis for modal (frequency) extraction. Sets the
    solver's AnalysisType='frequency' and EigenmodesCount=n_modes. Optional
    f_low/f_high (Hz) bound the requested mode range. Caller still calls
    fem_run + fem_modal_results."""
    analysis = _resolve_analysis(p["analysis"])
    solver = _solver_of(analysis)
    solver.AnalysisType = "frequency"
    solver.EigenmodesCount = int(p.get("n_modes", 5))
    if "f_low" in p:
        solver.EigenmodeLowLimit = float(p["f_low"])
    if "f_high" in p:
        solver.EigenmodeHighLimit = float(p["f_high"])
    return {
        "analysis_type": solver.AnalysisType,
        "n_modes": int(solver.EigenmodesCount),
    }


@handler("fem_modal_results")
def _h_fem_modal_results(p):
    """Extract natural frequencies (Hz) from a completed modal run. Returns
    {frequencies_hz: [...], modes: [{mode, frequency_hz, max_displacement_mm}, ...]}.
    Each eigenmode produces its own ResultMechanical object in the analysis."""
    analysis = _resolve_analysis(p["analysis"])
    modes = []
    for o in analysis.Group:
        if not o.isDerivedFrom("Fem::FemResultObject"):
            continue
        # Modal result objects expose Eigenmode (int) and EigenmodeFrequency (Hz).
        mode_n = getattr(o, "Eigenmode", None)
        freq = getattr(o, "EigenmodeFrequency", None)
        if mode_n is None or freq is None:
            continue
        max_disp = 0.0
        if hasattr(o, "DisplacementLengths"):
            dl = list(o.DisplacementLengths)
            if dl:
                max_disp = max(dl)
        modes.append({
            "mode": int(mode_n),
            "frequency_hz": float(freq),
            "max_displacement_mm": max_disp,
        })
    modes.sort(key=lambda m: m["mode"])
    return {
        "frequencies_hz": [m["frequency_hz"] for m in modes],
        "modes": modes,
    }


@handler("fem_buckling")
def _h_fem_buckling(p):
    """Configure the analysis for linear buckling. Sets AnalysisType='buckling'
    and BucklingFactors=n_factors. Apply a unit force constraint at the load
    location; the result factors are the multipliers at which buckling occurs."""
    analysis = _resolve_analysis(p["analysis"])
    solver = _solver_of(analysis)
    solver.AnalysisType = "buckling"
    solver.BucklingFactors = int(p.get("n_factors", 1))
    return {
        "analysis_type": solver.AnalysisType,
        "n_factors": int(solver.BucklingFactors),
    }


def _parse_buckling_factor_from_name(name):
    """CCX buckling result objects are named like CCX_BucklingFactor_<int>_<frac>_Results.
    The factor value is `<int>.<frac>`. Returns float or None."""
    import re
    m = re.match(r"CCX_BucklingFactor_(\d+)_(\d+)_Results", name)
    if not m:
        m = re.match(r"CCX_BucklingFactor_(\d+)_Results", name)
        if m:
            return float(m.group(1))
        return None
    return float(f"{m.group(1)}.{m.group(2)}")


@handler("fem_buckling_results")
def _h_fem_buckling_results(p):
    """Extract buckling load multipliers from a completed buckling run.
    CCX writes one Fem::FemResultObject per buckling mode and encodes the load
    multiplier in the object's name (e.g. 'CCX_BucklingFactor_1234_56_Results'
    means a factor of 1234.56). Returns {buckling_factors: [...], modes: [...]}.
    The factor of zero placeholder result is filtered out."""
    analysis = _resolve_analysis(p["analysis"])
    factors = []
    for o in analysis.Group:
        if not o.isDerivedFrom("Fem::FemResultObject"):
            continue
        f = _parse_buckling_factor_from_name(o.Name)
        if f is None or f == 0.0:
            continue
        factors.append({"name": o.Name, "factor": f})
    factors.sort(key=lambda x: x["factor"])
    return {
        "buckling_factors": [f["factor"] for f in factors],
        "modes": factors,
    }


@handler("fem_thermal_results")
def _h_fem_thermal_results(p):
    """Extract temperature-field results from a completed steady-state thermal
    run. Returns {temperatures_c: {min, max, mean}, top_n_hot_nodes: [...]}."""
    analysis = _resolve_analysis(p["analysis"])
    result = None
    for o in analysis.Group:
        if o.isDerivedFrom("Fem::FemResultObject"):
            result = o
            break
    if result is None:
        raise RuntimeError("no result object on analysis (run fem_run first)")
    temps = list(getattr(result, "Temperature", []) or [])
    nodes = list(getattr(result, "NodeNumbers", []) or [])
    if not temps:
        return {"temperatures_c": None, "top_n_hot_nodes": []}
    top_n = int(p.get("top_n", 5))
    indexed = sorted(enumerate(temps), key=lambda kv: -kv[1])[:top_n]
    return {
        "temperatures_c": {
            "min": min(temps),
            "max": max(temps),
            "mean": sum(temps) / len(temps),
        },
        "top_n_hot_nodes": [
            {"node": nodes[i] if i < len(nodes) else i, "temperature_c": t}
            for i, t in indexed
        ],
    }


@handler("fem_mesh_refinement")
def _h_fem_mesh_refinement(p):
    """Add a local mesh refinement to an existing FEM mesh. mesh: handle of
    the FEM mesh object. refs: list of {handle, face|edge|tag} to refine on.
    char_length: characteristic element length on those faces (mm); should be
    smaller than the global setting on the mesh."""
    doc = _active_doc()
    mesh_obj = _resolve(p["mesh"])
    char_length = float(p["char_length"])
    region = ObjectsFem.makeMeshRegion(
        doc, mesh_obj, char_length, p.get("name", "MeshRegion"),
    )
    region.References = _build_references(p.get("refs") or [])
    doc.recompute()
    h = _register("mesh_region", region)
    return {
        "handle": h,
        "name": region.Name,
        "char_length": float(region.CharacteristicLength.Value)
        if hasattr(region.CharacteristicLength, "Value")
        else float(region.CharacteristicLength),
    }


# --- multi-document + transactions --------------------------------------------

@handler("list_documents")
def _h_list_documents(p):
    """Return [{name, label, file_path, dirty, active}, ...] for all open docs."""
    active = App.ActiveDocument.Name if App.ActiveDocument is not None else None
    rows = []
    for name, d in App.listDocuments().items():
        rows.append({
            "name": name,
            "label": d.Label,
            "file_path": d.FileName or "",
            "dirty": bool(d.HasPendingTransaction) if hasattr(d, "HasPendingTransaction") else False,
            "active": name == active,
            "object_count": len(d.Objects),
        })
    return rows


@handler("set_active_document")
def _h_set_active_document(p):
    """Switch the active document by name (the value returned from new/open)."""
    name = p["name"]
    if name not in App.listDocuments():
        raise KeyError(f"no open document named {name!r}")
    App.setActiveDocument(name)
    return {"active": name}


@handler("close_document")
def _h_close_document(p):
    """Close a document by name (or 'active'). Invalidates any handles whose
    owning document was closed."""
    name = p.get("name", "active")
    if name == "active":
        if App.ActiveDocument is None:
            raise RuntimeError("no active document to close")
        name = App.ActiveDocument.Name
    if name not in App.listDocuments():
        raise KeyError(f"no open document named {name!r}")
    doc = App.getDocument(name)
    closed_object_names = {o.Name for o in doc.Objects}
    App.closeDocument(name)
    # Drop any handles that pointed into the closed doc.
    invalidated = []
    for hname in list(_handles.keys()):
        try:
            obj = _handles[hname]
            # Accessing .Name on a freed object will throw.
            obj_name = obj.Name
        except Exception:
            invalidated.append(hname)
            del _handles[hname]
            continue
        if obj_name in closed_object_names and not _object_in_open_docs(obj):
            invalidated.append(hname)
            del _handles[hname]
    return {"closed": name, "invalidated_handles": invalidated}


def _object_in_open_docs(obj):
    """Best-effort check that obj is still referenced by some open document."""
    try:
        for d in App.listDocuments().values():
            if any(o is obj for o in d.Objects):
                return True
    except Exception:
        pass
    return False


_TX_STACK = []  # labels of open transactions, last-in-first-out


@handler("transaction_open")
def _h_transaction_open(p):
    """Begin a transaction on the active document. label appears in the undo
    history. Requires UndoMode=1, which this handler enables."""
    doc = _active_doc()
    label = p.get("label", "Transaction")
    doc.UndoMode = 1
    doc.openTransaction(label)
    _TX_STACK.append((doc.Name, label))
    return {"label": label, "depth": len(_TX_STACK)}


@handler("transaction_commit")
def _h_transaction_commit(p):
    """Finalize the most recent open transaction."""
    if not _TX_STACK:
        raise RuntimeError("no open transaction to commit")
    doc_name, label = _TX_STACK.pop()
    doc = App.getDocument(doc_name)
    doc.commitTransaction()
    return {"label": label, "depth": len(_TX_STACK)}


@handler("transaction_abort")
def _h_transaction_abort(p):
    """Roll back changes since the most recent transaction_open. FreeCAD 1.1's
    abortTransaction is unreliable in headless mode; we commit-then-undo
    instead, which gives correct rollback semantics."""
    if not _TX_STACK:
        raise RuntimeError("no open transaction to abort")
    doc_name, label = _TX_STACK.pop()
    doc = App.getDocument(doc_name)
    doc.commitTransaction()
    doc.undo()
    return {"label": label, "depth": len(_TX_STACK)}


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

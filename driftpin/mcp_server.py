"""
DriftPin MCP server — exposes the worker handlers as MCP tools over stdio.

One Worker is spawned at server startup and reused across tool calls, so
ActiveDocument state persists across an MCP session (the whole point).

Run:   .venv/bin/python3 -m driftpin mcp
"""
import atexit
import base64
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import Worker, WorkerError
from . import render as _render


mcp = FastMCP("driftpin")

_worker: Worker | None = None


def _ensure_worker() -> Worker:
    global _worker
    if _worker is None or _worker.proc.poll() is not None:
        _worker = Worker()
    return _worker


def _call(method: str, **params: Any) -> Any:
    try:
        return _ensure_worker().call(method, **params)
    except WorkerError as e:
        raise RuntimeError(f"{e.type}: {e.remote_message}") from e


@atexit.register
def _cleanup():
    global _worker
    if _worker is not None:
        try:
            _worker.shutdown(timeout=3.0)
        except Exception:
            pass
        _worker = None


# --- tools --------------------------------------------------------------------

@mcp.tool()
def ping() -> str:
    """Check that the FreeCAD worker is alive. Returns 'pong' on success."""
    return _call("ping")


@mcp.tool()
def version() -> dict:
    """Return FreeCAD and bundled Python versions from the worker."""
    return _call("version")


@mcp.tool()
def new_document(name: str = "part") -> dict:
    """Create a new FreeCAD document and make it active. Returns {doc: <name>}."""
    return _call("new_document", name=name)


@mcp.tool()
def open_document(path: str) -> dict:
    """Open an existing .FCStd file, make it active. Returns doc name and object list."""
    return _call("open_document", path=path)


@mcp.tool()
def save_document(path: str) -> dict:
    """Save the active document to the given .FCStd path."""
    return _call("save_document", path=path)


@mcp.tool()
def list_objects() -> list:
    """List objects in the active document. Returns [{name, type, label}, ...]."""
    return _call("list_objects")


@mcp.tool()
def add_primitive(
    kind: str,
    w: float = 10.0,
    d: float = 10.0,
    h: float = 10.0,
    r: float = 5.0,
    placement: list | None = None,
) -> dict:
    """Add a primitive to the active document.

    kind: 'box' (uses w, d, h), 'cylinder' (uses r, h), or 'sphere' (uses r).
    placement: optional [x, y, z] mm translation.
    Returns {handle, name, volume}. The handle (e.g. 'box_1') is how you
    reference this object in subsequent boolean_op calls.
    """
    params = {"kind": kind, "w": w, "d": d, "h": h, "r": r}
    if placement is not None:
        params["placement"] = placement
    return _call("add_primitive", **params)


@mcp.tool()
def list_faces(handle: str) -> list:
    """List all faces of a shaped object with stable tags + geometric descriptors.

    Returns [{tag, index, kind, area, centroid, normal?, axis?, radius?}, ...].
    Tags survive geometry edits as long as the face's surface kind, area,
    centroid, and (where meaningful) normal/axis/radius do not change.
    Use the tag in subsequent calls instead of the FaceN index.
    """
    return _call("list_faces", handle=handle)


@mcp.tool()
def list_edges(handle: str) -> list:
    """List all edges of a shaped object with stable tags + descriptors.

    Returns [{tag, index, kind, length, centroid, axis?, radius?}, ...].
    Same stability story as list_faces.
    """
    return _call("list_edges", handle=handle)


@mcp.tool()
def query_faces(handle: str, predicate: dict) -> list:
    """Filter faces by a structured predicate. Returns matching descriptors.

    Predicate fields (all optional, ANDed):
      - kind / type: 'planar' | 'cylindrical' | 'conical' | 'spherical' | 'toroidal' | 'spline'
      - normal_dir: [x, y, z] unit vector for planar faces (with normal_tol)
      - radius_eq: float, matches cylindrical/conical/spherical (with radius_tol)
      - area_min / area_max: bounds in mm^2
    Optional ordering:
      - centroid_max / centroid_min: 'x' | 'y' | 'z' (sorts result)
      - order: 'area_desc' | 'area_asc'

    Example: {"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"}
    finds the topmost +Z-facing face.
    """
    return _call("query_faces", handle=handle, predicate=predicate)


@mcp.tool()
def resolve_face(handle: str, tag: str) -> dict:
    """Resolve a face tag to the current FaceN index. Errors on miss or ambiguity.

    Use this when you need to pass a (object, 'FaceN') reference into a tool
    that doesn't accept tags directly (e.g. legacy FEM constraints).
    """
    return _call("resolve_face", handle=handle, tag=tag)


@mcp.tool()
def resolve_edge(handle: str, tag: str) -> dict:
    """Resolve an edge tag to the current EdgeN index. Errors on miss or ambiguity."""
    return _call("resolve_edge", handle=handle, tag=tag)


@mcp.tool()
def make_body(name: str = "Body") -> dict:
    """Create a PartDesign Body. Subsequent sketches/features go inside it.

    Returns {handle, name}.
    """
    return _call("make_body", name=name)


@mcp.tool()
def make_datum_plane(
    body: str,
    base: str = "XY",
    offset: float = 0.0,
    name: str = "DatumPlane",
) -> dict:
    """Create a Datum Plane in a Body.

    base: 'XY' | 'XZ' | 'YZ' for body origin planes. Pass a face_tag dict
    {handle, tag} for attachment to a face on another shape.
    offset: shift along the plane normal in mm.
    """
    return _call(
        "make_datum_plane", body=body, base=base, offset=offset, name=name,
    )


@mcp.tool()
def make_sketch(body: str, plane: str = "XY", name: str = "Sketch") -> dict:
    """Create a sketch in a Body, attached to a plane.

    plane: 'XY' | 'XZ' | 'YZ' for origin planes, or a datum-plane handle.
    Returns {handle, name}.
    """
    return _call("make_sketch", body=body, plane=plane, name=name)


@mcp.tool()
def add_sketch_geometry(sketch: str, items: list) -> dict:
    """Append geometric primitives to a sketch.

    items is a list of dicts:
      - {type: 'line', start: [x,y], end: [x,y], construction?: bool}
      - {type: 'circle', center: [x,y], radius: float}
      - {type: 'arc', center: [x,y], radius, start_angle, end_angle}  (radians)
      - {type: 'point', pos: [x,y]}
    Returns {indices: [...]} — Sketcher-assigned indices for use in constraints.
    """
    return _call("add_sketch_geometry", sketch=sketch, items=items)


@mcp.tool()
def add_sketch_constraint(
    sketch: str,
    type: str,
    refs: list,
    value: float | None = None,
) -> dict:
    """Add a constraint to a sketch.

    type: Coincident, Horizontal, Vertical, Distance, DistanceX, DistanceY,
          Radius, Diameter, Equal, Parallel, Perpendicular, Tangent, Block,
          Symmetric, Angle.
    refs: list of [geom_idx, vertex_role] pairs. vertex_role: 0=edge, 1=start,
          2=end, 3=center.
    value: numeric value (mm or radians) for dimensional constraints.
    """
    params = {"sketch": sketch, "type": type, "refs": refs}
    if value is not None:
        params["value"] = value
    return _call("add_sketch_constraint", **params)


@mcp.tool()
def close_sketch(sketch: str) -> dict:
    """Recompute and report DOF status. Returns {geometry_count, constraint_count,
    open_vertices, fully_constrained}."""
    return _call("close_sketch", sketch=sketch)


@mcp.tool()
def pad(
    sketch: str,
    length: float = 10.0,
    symmetric: bool = False,
    reversed: bool = False,
    name: str = "Pad",
) -> dict:
    """Pad a sketch by `length` mm. symmetric=True extrudes both directions."""
    return _call(
        "pad", sketch=sketch, length=length,
        symmetric=symmetric, reversed=reversed, name=name,
    )


@mcp.tool()
def pocket(
    sketch: str,
    length: float = 10.0,
    through_all: bool = False,
    reversed: bool = False,
    name: str = "Pocket",
) -> dict:
    """Subtract a pad of `length` mm from the body. through_all ignores length."""
    return _call(
        "pocket", sketch=sketch, length=length,
        through_all=through_all, reversed=reversed, name=name,
    )


@mcp.tool()
def revolve(
    sketch: str,
    axis: str = "Y",
    angle: float = 360.0,
    reversed: bool = False,
    name: str = "Revolution",
) -> dict:
    """Revolve a sketch around a body origin axis ('X'|'Y'|'Z') by `angle` deg."""
    return _call(
        "revolve", sketch=sketch, axis=axis, angle=angle,
        reversed=reversed, name=name,
    )


@mcp.tool()
def partdesign_fillet(
    feature: str, edges: list, radius: float = 1.0, name: str = "PdFillet",
) -> dict:
    """PartDesign Fillet on edges of a feature in a Body. edges accepts tags
    or 'EdgeN' index strings."""
    return _call(
        "partdesign_fillet", feature=feature, edges=edges,
        radius=radius, name=name,
    )


@mcp.tool()
def partdesign_chamfer(
    feature: str, edges: list, size: float = 1.0, name: str = "PdChamfer",
) -> dict:
    """PartDesign Chamfer on edges of a feature in a Body."""
    return _call(
        "partdesign_chamfer", feature=feature, edges=edges,
        size=size, name=name,
    )


@mcp.tool()
def hole(
    sketch: str,
    diameter: float = 5.0,
    depth_type: str = "ThroughAll",
    depth: float = 10.0,
    cut_type: str = "None",
    cut_diameter: float | None = None,
    cut_depth: float | None = None,
    threaded: bool = False,
    thread_type: str | None = None,
    thread_size: str | None = None,
    model_thread: bool = False,
    reversed: bool = False,
    name: str = "Hole",
) -> dict:
    """Drill a parametric Hole from a sketch (one or more circles).

    sketch: handle of a sketch placed on a face of an existing body feature.
    depth_type: 'Dimension' (use `depth`) or 'ThroughAll'.
    cut_type: 'None' | 'Counterbore' | 'Countersink' | 'Counterdrill'.
              When non-None, cut_diameter (head clearance) and cut_depth apply.
    threaded=True applies a tap. thread_type/thread_size are FreeCAD enum
    values (e.g. 'ISOMetricProfile' / 'M5'); leave defaults if you only need
    a plain hole.
    """
    params = {
        "sketch": sketch, "diameter": diameter, "depth_type": depth_type,
        "depth": depth, "cut_type": cut_type, "threaded": threaded,
        "model_thread": model_thread, "reversed": reversed, "name": name,
    }
    if cut_diameter is not None:
        params["cut_diameter"] = cut_diameter
    if cut_depth is not None:
        params["cut_depth"] = cut_depth
    if thread_type is not None:
        params["thread_type"] = thread_type
    if thread_size is not None:
        params["thread_size"] = thread_size
    return _call("hole", **params)


@mcp.tool()
def linear_pattern(
    feature: str,
    direction: str | dict = "X",
    length: float = 10.0,
    occurrences: int = 2,
    reversed: bool = False,
    name: str = "LinearPattern",
) -> dict:
    """Repeat a PartDesign feature linearly along a direction.

    direction: 'X'|'Y'|'Z' for body origin axes, or {handle, edge: tag|'EdgeN'}
    for an edge-aligned direction.
    length: total span (mm) covered by the pattern.
    occurrences: number of copies (>=2). Includes the original.
    """
    return _call(
        "linear_pattern", feature=feature, direction=direction,
        length=length, occurrences=occurrences, reversed=reversed, name=name,
    )


@mcp.tool()
def polar_pattern(
    feature: str,
    axis: str | dict = "Z",
    angle_deg: float = 360.0,
    occurrences: int = 2,
    reversed: bool = False,
    name: str = "PolarPattern",
) -> dict:
    """Repeat a PartDesign feature around an axis.

    axis: 'X'|'Y'|'Z' for body origin axes, or {handle, edge: tag|'EdgeN'}.
    angle_deg: total swept angle (default 360 = full circle).
    occurrences: number of copies (>=2). Includes the original.
    """
    return _call(
        "polar_pattern", feature=feature, axis=axis,
        angle_deg=angle_deg, occurrences=occurrences,
        reversed=reversed, name=name,
    )


@mcp.tool()
def mirrored(
    feature: str,
    plane: str | dict = "YZ",
    name: str = "Mirrored",
) -> dict:
    """Mirror a PartDesign feature across a plane.

    plane: 'XY'|'XZ'|'YZ' for body origin planes, a datum-plane handle string,
    or {handle, face: tag|'FaceN'} for a face-defined mirror plane.
    """
    return _call("mirrored", feature=feature, plane=plane, name=name)


@mcp.tool()
def loft(
    sketches: list,
    closed: bool = False,
    ruled: bool = False,
    reversed: bool = False,
    name: str = "Loft",
) -> dict:
    """Loft (additively) between two or more sketches.

    sketches: ordered list of sketch handles. The first becomes the Profile,
    the rest become Sections.
    closed: True connects the last section back to the first (toroidal).
    ruled: True uses straight ruled surfaces between adjacent sections.
    """
    return _call(
        "loft", sketches=sketches, closed=closed, ruled=ruled,
        reversed=reversed, name=name,
    )


@mcp.tool()
def sweep(
    profile: str,
    spine: str,
    mode: str = "Standard",
    transition: str = "Transformed",
    name: str = "Sweep",
) -> dict:
    """Sweep a profile sketch along a spine sketch (additive pipe).

    profile: handle of the cross-section sketch.
    spine: handle of the path sketch (in the same body).
    mode: 'Standard' | 'Frenet' | 'Auxiliary' | 'Binormal'.
    transition: 'Transformed' | 'Right corner' | 'Round corner'.
    """
    return _call(
        "sweep", profile=profile, spine=spine,
        mode=mode, transition=transition, name=name,
    )


@mcp.tool()
def helix(
    radius: float = 5.0,
    pitch: float = 2.0,
    height: float = 10.0,
    angle: float = 0.0,
    name: str = "Helix",
) -> dict:
    """Generate a Part::Helix curve. radius, pitch, height in mm. angle (deg)
    is the cone angle (0 = cylindrical helix, >0 = conical).

    Returns a handle to a 1D helical curve. To get a 3D helical solid (e.g.
    for threads), use the curve as the spine of a sweep.
    """
    return _call(
        "helix", radius=radius, pitch=pitch, height=height,
        angle=angle, name=name,
    )


@mcp.tool()
def thickness(
    base: str,
    open_faces: list,
    thickness: float = 1.0,
    reversed: bool = True,
    join: str | None = None,
    mode: str | None = None,
    name: str = "Thickness",
) -> dict:
    """Hollow out a solid into a shell.

    base: handle of the body's tip feature (the solid to hollow).
    open_faces: list of {handle, face: tag|'FaceN'} that become the shell's
    openings.
    thickness: wall thickness (mm).
    reversed: True (default) grows the wall INWARD into the solid (the natural
    "hollow this part" interpretation). False grows outward.
    join: 'Arc' | 'Intersection'.
    mode: 'Skin' | 'Pipe' | 'RectoVerso'.
    """
    params = {
        "base": base, "open_faces": open_faces,
        "thickness": thickness, "reversed": reversed, "name": name,
    }
    if join is not None:
        params["join"] = join
    if mode is not None:
        params["mode"] = mode
    return _call("thickness", **params)


@mcp.tool()
def draft(
    base: str,
    faces: list,
    angle_deg: float = 1.0,
    neutral_plane: dict | None = None,
    reversed: bool = False,
    name: str = "Draft",
) -> dict:
    """Apply a draft angle to faces (for moldability).

    base: feature handle.
    faces: list of {handle, face: tag|'FaceN'}.
    angle_deg: draft angle (positive degrees).
    neutral_plane: {handle, face: tag|'FaceN'} for the plane along which the
    angle is measured (typically the parting plane).
    reversed: flip the direction of the draft.
    """
    params = {
        "base": base, "faces": faces, "angle_deg": angle_deg,
        "reversed": reversed, "name": name,
    }
    if neutral_plane is not None:
        params["neutral_plane"] = neutral_plane
    return _call("draft", **params)


@mcp.tool()
def list_documents() -> list:
    """List all open documents: [{name, label, file_path, dirty, active, object_count}, ...]."""
    return _call("list_documents")


@mcp.tool()
def set_active_document(name: str) -> dict:
    """Switch the active document by name (the value returned from new_document/open_document)."""
    return _call("set_active_document", name=name)


@mcp.tool()
def close_document(name: str = "active") -> dict:
    """Close a document by name (or 'active' for the currently active one).
    Frees its objects and invalidates any handles into the closed doc.
    Returns {closed, invalidated_handles}."""
    return _call("close_document", name=name)


@mcp.tool()
def transaction_open(label: str = "Transaction") -> dict:
    """Begin an undoable transaction on the active document. Pair with
    transaction_commit (keep the changes) or transaction_abort (roll back).
    Transactions nest — the most-recent open is committed/aborted first.
    """
    return _call("transaction_open", label=label)


@mcp.tool()
def transaction_commit() -> dict:
    """Commit the most recent open transaction; changes are kept."""
    return _call("transaction_commit")


@mcp.tool()
def transaction_abort() -> dict:
    """Roll back the most recent open transaction. Implementation note:
    FreeCAD 1.1 headless `abortTransaction` is unreliable, so the worker
    commits then undoes — net effect is a clean rollback.
    """
    return _call("transaction_abort")


@mcp.tool()
def add_sketch_external(sketch: str, ref: dict) -> dict:
    """Project an external edge/face/vertex into a sketch as construction geometry.

    ref: {handle, edge: tag|'EdgeN'} (or face/vertex variant; or {handle, tag}
    where tag is e_/f_ prefixed). The projected element gets a negative geom
    index so subsequent constraints can reference it. Use this to make a sketch
    that stays anchored to upstream geometry (e.g. a hole 5mm from a tagged
    edge that survives pad-length edits).
    """
    return _call("add_sketch_external", sketch=sketch, ref=ref)


@mcp.tool()
def get_object(handle: str) -> dict:
    """Dump a handle's properties + shape stats. Useful when no dedicated tool
    exposes what you need."""
    return _call("get_object", handle=handle)


@mcp.tool()
def set_property(handle: str, name: str, value: Any) -> dict:
    """Set a single property by name on an object. Coerces lists → Vector for
    Vector properties; other values pass through."""
    return _call("set_property", handle=handle, name=name, value=value)


@mcp.tool()
def fillet_edges(handle: str, edges: list, radius: float = 1.0) -> dict:
    """Fillet edges of a shaped object. `edges` accepts tags (e_...) or 'EdgeN' strings.

    Returns {handle, volume, edges} for the new fillet feature.
    """
    return _call("fillet_edges", handle=handle, edges=edges, radius=radius)


@mcp.tool()
def boolean_op(op: str, base: str, tool: str) -> dict:
    """Boolean operation on two existing objects, referenced by their handles.

    op: 'cut' (base minus tool), 'fuse' (union), or 'common' (intersection).
    base, tool: handles returned from add_primitive (e.g. 'box_1', 'cylinder_1').
    Returns {handle, volume}.
    """
    return _call("boolean_op", op=op, base=base, tool=tool)


@mcp.tool()
def export_shape(
    path: str, object: str | None = None
) -> dict:
    """Export a shape to STEP/IGES/BREP/STL. Format inferred from path extension.

    object: FreeCAD object name (NOT a DriftPin handle). If omitted, exports
    the first shaped object in the active document.
    """
    return _call("export_shape", path=path, object=object)


@mcp.tool()
def run_script(code: str) -> Any:
    """Escape hatch: execute Python in the worker with App/Part/ObjectsFem in scope.

    Set `__result__` in the script to return a JSON-serializable value.
    Useful when a needed FreeCAD operation doesn't have a dedicated tool yet.
    """
    return _call("run_script", code=code)


@mcp.tool()
def mass_properties(handle: str, density: float | None = None) -> dict:
    """Mass properties of a shaped object: volume (mm³), surface area (mm²),
    centroid, bounding box, inertia tensor. If density (kg/mm³) is given,
    also returns mass (kg). Steel = 7.9e-6, aluminum = 2.7e-6, ABS = 1.05e-6.
    """
    params = {"handle": handle}
    if density is not None:
        params["density"] = density
    return _call("mass_properties", **params)


@mcp.tool()
def make_assembly(name: str = "Assembly") -> dict:
    """Create an App::Part container to hold linked parts. Returns {handle, name}."""
    return _call("make_assembly", name=name)


@mcp.tool()
def add_part(
    assembly: str,
    source: dict,
    placement: list | dict | None = None,
    name: str = "Part",
) -> dict:
    """Add a part to an assembly via App::Link.

    source is one of:
      {"handle": "<handle>"}                          — link an in-doc body
      {"path": "/path/to/part.FCStd"}                 — link first body in file
      {"path": "/path/to/part.FCStd", "object": "X"}  — link named object
    placement: [x, y, z] or {position: [...], axis: [...], angle_deg: ...}.
    """
    params = {"assembly": assembly, "source": source, "name": name}
    if placement is not None:
        params["placement"] = placement
    return _call("add_part", **params)


@mcp.tool()
def list_assembly_parts(assembly: str) -> list:
    """List parts of an assembly: name, type, linked-target name, position, volume."""
    return _call("list_assembly_parts", assembly=assembly)


@mcp.tool()
def interference_check(assembly: str) -> list:
    """Pairwise interference: compute volume of intersection between every pair
    of parts. Returns [{a, b, interference_mm3}, ...] descending by volume.
    Empty list = no interference.
    """
    return _call("interference_check", assembly=assembly)


@mcp.tool()
def bom_extract(assembly: str, density: float | None = None) -> list:
    """Walk an assembly and return [{part, count, total_volume_mm3, total_mass_kg?}, ...]
    grouped by linked-object name. density (kg/mm³) is optional."""
    params = {"assembly": assembly}
    if density is not None:
        params["density"] = density
    return _call("bom_extract", **params)


@mcp.tool()
def make_drawing_page(name: str = "Page", template: str | None = None) -> dict:
    """Create a TechDraw page using a built-in A4 landscape template by default.
    template: optional absolute path to a .svg template."""
    params = {"name": name}
    if template is not None:
        params["template"] = template
    return _call("make_drawing_page", **params)


@mcp.tool()
def add_projection_group(
    page: str,
    body: str,
    views: list | None = None,
    name: str = "ProjGroup",
) -> dict:
    """Add a multi-view projection group of `body` to a drawing page.
    views: list of FreeCAD view codes ('Front', 'Top', 'Right', 'Left',
    'Bottom', 'Rear', 'FrontTopLeft', etc.). Default: ['Front', 'Top', 'Right'].
    """
    params = {"page": page, "body": body, "name": name}
    if views is not None:
        params["views"] = views
    return _call("add_projection_group", **params)


@mcp.tool()
def export_drawing(page: str, path: str) -> dict:
    """Export a drawing page to PDF or SVG. Format inferred from path extension."""
    return _call("export_drawing", page=page, path=path)


@mcp.tool()
def render_view(
    handle: str,
    view: str = "iso",
    width: int = 512,
    height: int = 512,
    deflection: float = 0.5,
    edges: bool = True,
) -> dict:
    """Render an isometric/orthographic view of a shaped object as a PNG.

    view: 'iso' | 'top' | 'bottom' | 'front' | 'back' | 'left' | 'right' | 'side'.
    deflection: tessellation accuracy in mm (smaller = finer mesh, slower).
    edges: draw triangle edges over filled faces.

    Returns {png_base64, width, height, view, vertices, triangles}.
    """
    mesh = _call("tessellate", handle=handle, deflection=deflection)
    png = _render.render_mesh(
        mesh["vertices"], mesh["triangles"],
        width=width, height=height, view=view, edges=edges,
    )
    return {
        "png_base64": base64.b64encode(png).decode("ascii"),
        "width": width,
        "height": height,
        "view": view,
        "vertices": len(mesh["vertices"]),
        "triangles": len(mesh["triangles"]),
    }


@mcp.tool()
def render_views(
    handle: str,
    views: list[str] | None = None,
    width: int = 384,
    height: int = 384,
    deflection: float = 0.5,
) -> dict:
    """Render multiple views of a single object. Returns {views: {view_name: {png_base64,...}}}.
    Default views: ['iso', 'top', 'front']."""
    if views is None:
        views = ["iso", "top", "front"]
    mesh = _call("tessellate", handle=handle, deflection=deflection)
    out = {}
    for v in views:
        png = _render.render_mesh(
            mesh["vertices"], mesh["triangles"],
            width=width, height=height, view=v,
        )
        out[v] = {
            "png_base64": base64.b64encode(png).decode("ascii"),
            "width": width,
            "height": height,
        }
    return {"views": out, "vertices": len(mesh["vertices"]), "triangles": len(mesh["triangles"])}


@mcp.tool()
def fem_new_analysis(name: str = "Analysis") -> dict:
    """Create a Fem::FemAnalysis container. Returns {handle, name}."""
    return _call("fem_new_analysis", name=name)


@mcp.tool()
def fem_set_solver(
    analysis: str,
    kind: str = "ccx",
    name: str = "Solver",
    tunables: dict | None = None,
) -> dict:
    """Add a solver to an analysis. kind: 'ccx' (CalculiX) or 'elmer'.

    tunables: dict of solver-property values, e.g.
      {"GeometricalNonlinearity": "linear", "ThermoMechSteadyState": True,
       "MatrixSolverType": "default"}.
    Sensible CCX defaults are filled in if omitted.
    """
    params = {"analysis": analysis, "kind": kind, "name": name}
    if tunables is not None:
        params["tunables"] = tunables
    return _call("fem_set_solver", **params)


@mcp.tool()
def fem_set_material(
    analysis: str,
    body: str,
    material: dict,
    name: str = "Material",
) -> dict:
    """Bind a material to a body in an analysis.

    material is a dict with at minimum:
      {"YoungsModulus": "210000 MPa", "PoissonRatio": "0.30",
       "Density": "7900 kg/m^3", "Name": "Steel-Generic"}
    Any extra keys are passed through to the FEM material card.
    """
    return _call(
        "fem_set_material",
        analysis=analysis, body=body, material=material, name=name,
    )


@mcp.tool()
def fem_add_constraint(
    analysis: str,
    kind: str,
    refs: list,
    name: str | None = None,
    force: float | None = None,
    pressure: float | None = None,
    direction: dict | None = None,
    reversed: bool = False,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    x_free: bool | None = None,
    y_free: bool | None = None,
    z_free: bool | None = None,
    temperature: float | None = None,
    flux_type: str | None = None,
    flux: float | None = None,
    ambient_temp: float | None = None,
    film_coef: float | None = None,
    emissivity: float | None = None,
) -> dict:
    """Add a constraint by face/edge tag (Slice 1).

    kind:
      structural: 'fixed' | 'force' | 'pressure' | 'displacement'
      thermal:    'temperature' | 'heatflux' | 'initial_temperature'
    refs: list of {handle, tag} dicts; 'tag' may be a face tag (f_...) or
          edge tag (e_...). Resolved against the live shape so refs survive
          unrelated geometry edits.

    For 'force': force (N), optional direction {handle, edge|tag}.
    For 'pressure': pressure (MPa).
    For 'displacement': x/y/z (mm) or x_free/y_free/z_free.
    For 'temperature' / 'initial_temperature': temperature (°C / K).
    For 'heatflux': flux_type ('DFlux'|'Convection'|'Radiation'), and
      DFlux: flux (W/m²); Convection: ambient_temp (°C) + film_coef (W/m²K);
      Radiation: ambient_temp + emissivity.
    """
    params = {"analysis": analysis, "kind": kind, "refs": refs}
    if name is not None:
        params["name"] = name
    if force is not None:
        params["force"] = force
    if pressure is not None:
        params["pressure"] = pressure
    if direction is not None:
        params["direction"] = direction
    if reversed:
        params["reversed"] = reversed
    for axis, val, free in (
        ("x", x, x_free), ("y", y, y_free), ("z", z, z_free),
    ):
        if val is not None:
            params[axis] = val
        if free is not None:
            params[f"{axis}_free"] = free
    if temperature is not None:
        params["temperature"] = temperature
    if flux_type is not None:
        params["flux_type"] = flux_type
    if flux is not None:
        params["flux"] = flux
    if ambient_temp is not None:
        params["ambient_temp"] = ambient_temp
    if film_coef is not None:
        params["film_coef"] = film_coef
    if emissivity is not None:
        params["emissivity"] = emissivity
    return _call("fem_add_constraint", **params)


@mcp.tool()
def fem_modal(
    analysis: str,
    n_modes: int = 5,
    f_low: float | None = None,
    f_high: float | None = None,
) -> dict:
    """Configure analysis for modal (frequency) extraction.

    Sets solver AnalysisType='frequency' and EigenmodesCount=n_modes.
    f_low / f_high (Hz) optionally bound the requested mode range.
    Caller still calls fem_run, then fem_modal_results to read frequencies.
    """
    params = {"analysis": analysis, "n_modes": n_modes}
    if f_low is not None:
        params["f_low"] = f_low
    if f_high is not None:
        params["f_high"] = f_high
    return _call("fem_modal", **params)


@mcp.tool()
def fem_modal_results(analysis: str) -> dict:
    """Extract natural frequencies from a completed modal run.
    Returns {frequencies_hz: [...], modes: [{mode, frequency_hz, max_displacement_mm}, ...]}."""
    return _call("fem_modal_results", analysis=analysis)


@mcp.tool()
def fem_buckling(analysis: str, n_factors: int = 1) -> dict:
    """Configure analysis for linear buckling. Apply a unit-magnitude force
    constraint at the load location; the result factors are the multipliers
    at which buckling occurs.
    """
    return _call("fem_buckling", analysis=analysis, n_factors=n_factors)


@mcp.tool()
def fem_buckling_results(analysis: str) -> dict:
    """Extract buckling load multipliers from a completed buckling run.
    Returns {buckling_factors: [...], modes: [{mode, factor}, ...]}."""
    return _call("fem_buckling_results", analysis=analysis)


@mcp.tool()
def fem_thermal_results(analysis: str, top_n: int = 5) -> dict:
    """Extract temperature field summary from a completed thermal run.
    Returns {temperatures_c: {min, max, mean}, top_n_hot_nodes: [...]}."""
    return _call("fem_thermal_results", analysis=analysis, top_n=top_n)


@mcp.tool()
def fem_mesh_refinement(
    mesh: str,
    refs: list,
    char_length: float,
    name: str = "MeshRegion",
) -> dict:
    """Add a local mesh-refinement region to an existing FEM mesh.

    mesh: handle of the FEM mesh.
    refs: list of {handle, face|edge|tag} dicts identifying the elements
    (faces/edges) to refine on.
    char_length: characteristic element length (mm) on those elements;
    should be smaller than the global mesh setting to actually refine.
    """
    return _call(
        "fem_mesh_refinement",
        mesh=mesh, refs=refs, char_length=char_length, name=name,
    )


@mcp.tool()
def fem_mesh(
    analysis: str,
    body: str,
    char_length: float = 0.0,
    name: str = "Mesh",
) -> dict:
    """Create a Gmsh mesh of `body`, attached to `analysis`.

    char_length: max characteristic element length in mm. 0 = let Gmsh pick.
    Returns {handle, name, nodes, tets}.
    """
    return _call(
        "fem_mesh", analysis=analysis, body=body,
        char_length=char_length, name=name,
    )


@mcp.tool()
def fem_run(analysis: str, workdir: str = "/tmp/driftpin_fem") -> dict:
    """Run the CalculiX solver on an analysis. Blocks until the solve finishes.
    Returns {workdir, status}."""
    return _call("fem_run", _timeout=600.0, analysis=analysis, workdir=workdir)


@mcp.tool()
def fem_results(analysis: str, top_n: int = 5) -> dict:
    """Extract summary results from an analysis.

    Returns {max_vonmises_mpa, max_displacement_mm, max_displacement_vector,
    top_stress_nodes: [{node, vonmises_mpa, displacement_mm}, ...]}.
    """
    return _call("fem_results", analysis=analysis, top_n=top_n)


@mcp.tool()
def fem_cantilever_demo(
    length: float = 8000.0,
    width: float = 1000.0,
    height: float = 1000.0,
    force: float = 9_000_000.0,
    mesh_size: float = 500.0,
    workdir: str = "/tmp/driftpin_fem",
) -> dict:
    """Run the built-in cantilever FEM demo end-to-end (geometry → mesh → CalculiX).

    Dimensions in mm, force in N. Returns {nodes, tets, max_displacement_mm,
    max_vonmises_mpa, workdir}. A fresh document is created; existing state
    in the session is NOT overwritten but a new document becomes active.
    """
    return _call(
        "fem_cantilever_demo",
        _timeout=300.0,
        length=length,
        width=width,
        height=height,
        force=force,
        mesh_size=mesh_size,
        workdir=workdir,
    )


def run():
    mcp.run()


if __name__ == "__main__":
    run()

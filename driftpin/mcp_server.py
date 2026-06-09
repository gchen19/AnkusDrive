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


def _call(_method: str, _timeout: float | None = None, **params: Any) -> Any:
    # _method is underscored so a tool param named `method` (e.g. tolerance_stackup)
    # passes through **params instead of colliding with this positional argument.
    try:
        worker = _ensure_worker()
        if _timeout is not None:
            return worker.call(_method, _timeout=_timeout, **params)
        return worker.call(_method, **params)
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
def restart_worker() -> dict:
    """Kill the FreeCAD worker process and spawn a fresh one. Use when the worker
    is wedged (e.g. App.ActiveDocument desynced from internal state). All open
    documents, unsaved changes, and handles are lost — save first if needed.
    Returns {restarted: True, freecad: [...]}."""
    global _worker
    if _worker is not None:
        try:
            _worker.shutdown(timeout=3.0)
        except Exception:
            pass
        if _worker.proc.poll() is None:
            _worker.proc.kill()
            _worker.proc.wait()
        _worker = None
    w = _ensure_worker()
    return {"restarted": True, "freecad": w.freecad_version}


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
def save_document(path: str, visibility_hygiene: bool = True) -> dict:
    """Save the active document to the given .FCStd path.

    visibility_hygiene (default True): before saving, hide any object that has
    been consumed as a producer-input (the Base/Tool of a Cut, the BaseFeature
    of a Body, every feature inside a Body's Group, etc.). Without this the
    re-opened doc double-renders intermediates on top of the final shape — a
    failure mode that looks identical to broken geometry. Pass False to keep
    explicit set_visibility overrides intact."""
    return _call("save_document", path=path, visibility_hygiene=visibility_hygiene)


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
def add_gear(
    teeth: int,
    module: float,
    height: float = 6.0,
    pressure_angle: float = 20.0,
    external: bool = True,
    placement: list | None = None,
    name: str = "Gear",
) -> dict:
    """Add an involute spur gear (FreeCAD's core involute generator), extruded to a solid.

    teeth: tooth count (>= 3). module: mm (pitch diameter = module * teeth).
    height: extrusion thickness mm. pressure_angle: deg (default 20).
    external: True for an external gear; False for an internal/ring tooth profile.
    placement: optional [x, y, z] mm translation.
    Returns {handle, name, volume, pitch_radius, tip_radius, root_radius, teeth,
    module, external}. Two external gears MESH when their axes are spaced
    (pitch_radius_a + pitch_radius_b) apart; phase one by half a tooth to avoid
    tooth-on-tooth interference.
    """
    params = {"teeth": teeth, "module": module, "height": height,
              "pressure_angle": pressure_angle, "external": external, "name": name}
    if placement is not None:
        params["placement"] = placement
    return _call("add_gear", **params)


@mcp.tool()
def add_rack(
    teeth: int,
    module: float,
    height: float = 6.0,
    width: float = 10.0,
    pressure_angle: float = 20.0,
    placement: list | None = None,
    name: str = "Rack",
) -> dict:
    """Add a linear gear rack (a spur gear's straight counterpart) as a solid.

    A rack is a gear of infinite radius: straight-flanked teeth on a rail.
    Standard full-depth tooth form (addendum = module, dedendum = 1.25*module,
    tooth height = 2.25*module, flanks at pressure_angle from vertical).

    teeth: number of teeth (>= 1).
    module: mm (sets tooth size; circular pitch = module * pi).
    height: extrusion thickness mm along +Y (the rack's face width; default 6).
    width: mm, rail base-band thickness below the tooth root line (default 10).
    pressure_angle: deg, flank angle from vertical (default 20; 0 < pa < 45).
    placement: optional [x, y, z] mm translation of the rack origin.
    name: object label (default "Rack").

    The profile lies in the XZ plane: root line at z=0, base band from z=-width
    to z=0, teeth from z=0 to z=2.25*module, extruded along +Y by height.

    Returns {handle, name, volume (mm^3), pitch (mm/tooth = module*pi),
    module, teeth, tooth_height (2.25*module mm), length (teeth*module*pi mm)}.
    A spur gear MESHES with this rack when their `pitch` values match
    (gear module*pi == rack pitch); `length` sizes the rail for the travel.
    """
    params = {"teeth": teeth, "module": module, "height": height,
              "width": width, "pressure_angle": pressure_angle, "name": name}
    if placement is not None:
        params["placement"] = placement
    return _call("add_rack", **params)


@mcp.tool()
def add_sprocket(
    teeth: int,
    chain_pitch: float,
    roller_diameter: float,
    height: float = 6.0,
    placement: list | None = None,
    name: str = "Sprocket",
) -> dict:
    """Add a roller-chain sprocket (ISO 606 / ANSI), built as a static solid plate.

    teeth: tooth count (>= 3). chain_pitch: chain link pitch in mm (e.g. 12.7 for
    #40 / ANSI 40 chain). roller_diameter: chain roller diameter in mm.
    height: plate thickness in mm (default 6.0).
    placement: optional [x, y, z] mm translation.

    Build: a disc of tip radius ~= pitch_radius + chain_pitch*0.3 with `teeth`
    roller seats (circular pockets, radius roller_diameter/2 * 1.05) cut on the
    pitch circle, one per tooth. This is a fit/visualisation approximation of the
    true ISO 606 tooth form, not a load-rated profile.

    Returns {handle, name, volume, pitch_diameter, chain_pitch, teeth, tip_radius,
    bore}. pitch_diameter (mm) = chain_pitch / sin(pi/teeth) and chain_pitch are
    the MATING numbers: a chain of the same chain_pitch wraps the sprocket, and the
    centre distance between two sprockets derives from their pitch_diameters. bore
    is 0 (no shaft hole cut yet — drill one with the `hole` command).
    """
    params = {"teeth": teeth, "chain_pitch": chain_pitch,
              "roller_diameter": roller_diameter, "height": height, "name": name}
    if placement is not None:
        params["placement"] = placement
    return _call("add_sprocket", **params)


@mcp.tool()
def add_pulley(
    teeth: int,
    belt_pitch: float,
    width: float,
    flanged: bool = True,
    height: float | None = None,
    placement: list | None = None,
    name: str = "Pulley",
) -> dict:
    """Add a timing-belt (or V) pulley as a static solid. Axis is +Z; toothed
    belt face spans z in [0, width].

    teeth: tooth count (>= 6). belt_pitch: belt tooth pitch mm/tooth (e.g. 2.0
    for GT2, 3.0 for GT3/HTD-3M); pitch diameter PD = belt_pitch * teeth / pi.
    width: belt-face length mm. flanged: True adds two thin guide discs (radius
    PD/2 + 2*belt_pitch) at each end to retain the belt. height: optional mm;
    OVERRIDES width when given (default height = width). placement: optional
    [x, y, z] mm translation of the axis base. name: object label.

    Returns {handle, name, volume, pitch_diameter, belt_pitch, teeth, width,
    flanged}. pitch_diameter (mm) is the mating number: the centre distance to a
    mating pulley plus the required belt length derive from the two pitch
    diameters and the same belt_pitch.
    """
    params = {"teeth": teeth, "belt_pitch": belt_pitch, "width": width,
              "flanged": flanged, "name": name}
    if height is not None:
        params["height"] = height
    if placement is not None:
        params["placement"] = placement
    return _call("add_pulley", **params)


@mcp.tool()
def add_spring(
    wire_diameter: float,
    outer_diameter: float,
    free_length: float,
    coils: float,
    kind: str = "compression",
    placement: list | None = None,
    name: str = "Spring",
) -> dict:
    """Add a helical compression spring: a round wire swept along a cylindrical helix.

    All lengths in mm; angles n/a.
    wire_diameter: wire (stock) diameter d, mm.
    outer_diameter: spring outer diameter OD, mm (must be > wire_diameter).
    free_length: uncompressed overall length along the axis, mm.
    coils: number of turns (active coils), may be fractional.
    kind: 'compression' (only supported mode in v1; end coils are not squared yet).
    placement: optional [x, y, z] mm translation of the spring's base.

    Geometry: mean coil diameter D = outer_diameter - wire_diameter; coil pitch =
    free_length / coils. Spring rate is computed for STEEL (shear modulus
    G = 79.3 GPa) as k = G*d^4 / (8*D^3*coils), reported in N/mm.

    Returns {handle, name, volume (mm^3), mean_diameter (mm), free_length (mm),
    coils, kind, solid_height (mm, = coils*wire_diameter, the fully-compressed
    block height), spring_rate_n_per_mm (N/mm)}. Use free_length, solid_height
    and spring_rate_n_per_mm to spec the spring into a mechanism (available travel
    = free_length - solid_height; force = spring_rate_n_per_mm * deflection).
    """
    params = {"wire_diameter": wire_diameter, "outer_diameter": outer_diameter,
              "free_length": free_length, "coils": coils, "kind": kind, "name": name}
    if placement is not None:
        params["placement"] = placement
    return _call("add_spring", **params)


@mcp.tool()
def add_fastener(
    kind: str,
    size: str,
    length: float | None = None,
    placement: list | None = None,
    name: str | None = None,
) -> dict:
    """Add a standard ISO metric fastener (screw / bolt / nut / washer) as a solid.

    kind: one of "socket_head_cap_screw", "hex_bolt", "hex_nut", "washer".
      - socket_head_cap_screw: cylindrical head with a cosmetic hex socket + plain
        shank (threads not modeled).
      - hex_bolt: hex head (across-flats) + plain shank.
      - hex_nut: hex prism with an axial clearance hole.
      - washer: flat annular ring.
    size: ISO designation, one of "M3","M4","M5","M6","M8","M10","M12".
    length: shank length in mm. REQUIRED for socket_head_cap_screw and hex_bolt;
      ignored for nut/washer.
    placement: optional [x, y, z] mm translation of the fastener origin (head top
      sits at z=0, shank runs in -z for screws/bolts).
    name: optional object name (default derived from kind).

    All dimensions are in mm. Threads are cosmetic (the shank is a plain cylinder
    of the major diameter).

    Returns {handle, name, kind, size, major_diameter, pitch, volume} plus, by kind:
    screws/bolts add {length, head_diameter, head_height, model_thread:false};
    nut adds {head_diameter (wrench across-flats), head_height};
    washer adds {head_diameter (outer diameter), head_height (thickness)}.
    Mating numbers: drill a through-hole of major_diameter (+ clearance) for the
    shank; head_diameter sizes a counterbore.
    """
    params = {"kind": kind, "size": size}
    if length is not None:
        params["length"] = length
    if placement is not None:
        params["placement"] = placement
    if name is not None:
        params["name"] = name
    return _call("add_fastener", **params)


@mcp.tool()
def add_bearing(
    designation: str | None = None,
    bore: float | None = None,
    outer_diameter: float | None = None,
    width: float | None = None,
    placement: list | None = None,
    name: str = "Bearing",
) -> dict:
    """Add a deep-groove ball bearing as an assembly *envelope* solid: an annular
    ring (outer-diameter cylinder minus bore cylinder) of the given width, axis
    along +Z. Balls/races are not modeled — this is the fit envelope a coordinator
    needs to size the shaft, the housing bore, and the shoulder spacing.

    Specify dimensions ONE of two ways:
    - designation: a standard metric series code, looked up in a built-in table.
      Known: "608", "623", "624", "625", "626", "688", "6000", "6200", "6800",
      "6900". (e.g. "608" -> bore 8, OD 22, width 7 mm.)
    - bore + outer_diameter + width: explicit dims in mm (all three required).
      Explicit values override a designation's table values when both are given.

    bore: inner-bore diameter mm (sizes the shaft). outer_diameter: OD mm (sizes
    the housing bore). width: axial length mm (shoulder spacing). placement:
    optional [x, y, z] mm translation of the bearing's near face.

    Raises ValueError if the designation is unknown and dims are incomplete, or if
    outer_diameter <= bore.

    Returns {handle, name, designation, bore, outer_diameter, width, volume}.
    handle starts "bearing_". designation is None when built from explicit dims.
    """
    params = {"name": name}
    if designation is not None:
        params["designation"] = designation
    if bore is not None:
        params["bore"] = bore
    if outer_diameter is not None:
        params["outer_diameter"] = outer_diameter
    if width is not None:
        params["width"] = width
    if placement is not None:
        params["placement"] = placement
    return _call("add_bearing", **params)


@mcp.tool()
def oring_groove(
    cross_section: float,
    inner_diameter: float = 0.0,
    handle: str | None = None,
    face: str | None = None,
    gland_type: str = "static_radial",
    cut: bool = True,
    name: str = "ORingGroove",
) -> dict:
    """Compute a static O-ring gland (groove) and optionally cut it into a face.

    This is the gland calc designers always fumble, plus an optional cut. Given
    the O-ring cross-section it returns standard static-seal gland dimensions;
    with cut=True it also machines the annular groove into a flat face.

    cross_section: O-ring wire cross-section diameter in mm (e.g. 1.78, 2.62). Required, > 0.
    inner_diameter: groove inner diameter in mm (the O-ring's nominal seal ID).
        Required when cut=True; used to size the returned diameters either way.
    handle: host solid to cut into (required only when cut=True).
    face: the flat face to cut the groove into — a stable f_* tag (preferred),
        a 'FaceN' index string, or an int. Required when cut=True. Must be planar.
    gland_type: seal-geometry label, default 'static_radial' (informational).
    cut: True (default) cuts the groove and returns a new solid; False makes this
        a pure calculator (no geometry, no handle).
    name: name for the resulting solid when cut=True.

    Gland rule (static seal): groove_depth = cross_section*0.75 (~25% squeeze,
    clamped to a 20-30% band), groove_width = cross_section*1.30. The groove's
    inner diameter equals inner_diameter and it spans outward by groove_width.

    Returns {groove_depth, groove_width, groove_inner_diameter,
    groove_outer_diameter, squeeze_pct, cross_section, gland_type} (all mm except
    squeeze_pct in percent). When cut=True it ALSO returns {handle, name, volume}
    for the grooved solid; the host input is hidden. mating numbers: cut a groove
    of inner_diameter to seat an O-ring of that ID; groove_outer_diameter sizes
    the radial space the groove occupies.
    """
    params = {"cross_section": cross_section, "inner_diameter": inner_diameter,
              "gland_type": gland_type, "cut": cut, "name": name}
    if handle is not None:
        params["handle"] = handle
    if face is not None:
        params["face"] = face
    return _call("oring_groove", **params)


@mcp.tool()
def chamfer_edges(
    handle: str, edges: list, size: float = 1.0, name: str = "Chamfer"
) -> dict:
    """Chamfer (bevel) specific edges of a shaped Part object — the direct-shape
    counterpart to fillet_edges.

    handle: handle of the object to chamfer (e.g. 'box_1', a boolean result).
    edges: non-empty list of edge references. Each may be a tag ('e_...' from
        list_edges, preferred), an 'EdgeN' string, or a bare 1-based integer
        index.
    size: symmetric chamfer leg distance in mm (applied equally to both faces
        meeting at the edge, i.e. dist1 = dist2 = size). Must be > 0. Default 1.0.
    name: label for the resulting feature object. Default 'Chamfer'.

    The base object is hidden (consumed into the chamfer feature). Returns
    {handle, name, volume, edges} where volume is the resulting Shape volume in
    mm^3 and edges is the list of resolved 1-based edge indices that were
    chamfered.
    """
    params = {"handle": handle, "edges": edges, "size": size, "name": name}
    return _call("chamfer_edges", **params)


@mcp.tool()
def shell_solid(
    handle: str,
    faces: list,
    thickness: float,
    name: str = "Shell",
) -> dict:
    """Hollow a raw Part solid into a thin-walled shell (the direct-shape
    counterpart to `thickness`, which only works on PartDesign bodies).

    handle: handle of the solid to hollow (e.g. a box/cylinder from
    add_primitive, or any shaped Part::Feature).
    faces: NON-EMPTY list of the faces to REMOVE — these become the shell's
    openings. Each entry is a face tag (f_..., from list_faces/query_faces,
    preferred and edit-stable), a 'FaceN' string, or a 1-based integer index.
    thickness: wall thickness in mm, must be > 0. The wall is grown INWARD, so
    the part's outer dimensions are preserved.

    The consumed input solid is hidden (its geometry now lives in the shell).
    Returns {handle (starts 'shell_'), name, volume (mm^3 of the resulting
    walls), wall_thickness (mm), removed_faces (list of 1-based face indices
    that were opened)}. Raises if faces is empty, thickness <= 0, an index is
    out of range, or the offset is too large to produce a valid shell.
    """
    params = {
        "handle": handle,
        "faces": faces,
        "thickness": thickness,
        "name": name,
    }
    return _call("shell_solid", **params)


@mcp.tool()
def add_thread(
    diameter: float,
    pitch: float,
    length: float,
    internal: bool = False,
    starts: int = 1,
    placement: list | None = None,
    name: str = "Thread",
) -> dict:
    """Generate a REAL helical ISO-style 60-degree thread as a static solid.

    Unlike `hole`/`list_thread_options` (which only flag a thread as metadata),
    this cuts actual helical geometry: a truncated triangular rib swept along a
    helix and fused to a core cylinder.

    All lengths in mm, angles in degrees.
    diameter: nominal MAJOR (crest) diameter, mm. For internal=True this is the
      bore the tap fits.
    pitch: thread pitch, mm per turn (e.g. M8 coarse = 1.25).
    length: threaded length along +Z from z=0, mm.
    internal: False (default) -> a finished externally-threaded stud. True -> a
      TAP/insert cutting-tool solid sized to the bore; fuse it into (or cut it
      from) a bored hole in your part to produce a threaded bore.
    starts: number of thread starts, >=1. Multi-start repeats the helix rotated
      by 360/starts and uses lead = pitch*starts.
    placement: optional [x, y, z] mm translation of the solid's base (default at
      the origin, axis along +Z).
    name: optional object name.

    Geometry note: the modeled minor (root) uses the ISO 5H/8 truncation; the
    reported minor_diameter uses the standard ISO formula
    diameter - 1.0825*pitch. Fallback behaviour: if the helical sweep cannot
    produce a valid solid the tool returns a plain cylinder tagged with the
    thread spec and modeled=False (this is rare for sane M-series inputs); always
    check the modeled flag.

    Returns {handle, name, volume (mm^3), major_diameter (mm), minor_diameter
    (mm), pitch (mm), length (mm), starts (int), internal (bool), modeled (bool)}.
    Mating numbers: drill/bore minor_diameter to tap an internal thread; clear a
    major_diameter (+clearance) hole to pass an external stud.
    """
    params = {"diameter": diameter, "pitch": pitch, "length": length,
              "internal": internal, "starts": starts, "name": name}
    if placement is not None:
        params["placement"] = placement
    return _call("add_thread", **params)


@mcp.tool()
def engrave_text(
    handle: str,
    face: str,
    text: str,
    size: float = 5.0,
    depth: float = 0.5,
    mode: str = "engrave",
    position: list | None = None,
    font: str | None = None,
    name: str = "Text",
) -> dict:
    """Engrave (cut) or emboss (add) extruded text onto a planar face of a solid.

    The text is rendered in a system TrueType font, extruded, laid flat on the
    chosen face centred on its centroid, then booleaned into the host solid.

    handle: the host solid to mark.
    face: the planar face to put the text on — a stable f_* tag (preferred), a
        'FaceN' index string, or an int. Must be a flat (planar) face. Get a tag
        from list_faces / query_faces.
    text: the string to render (non-empty).
    size: cap height of the text in mm (default 5.0).
    depth: extrusion/engraving depth in mm (default 0.5). Engrave recesses the
        text this far below the surface; emboss raises it this far above.
    mode: 'engrave' (default) cuts the text into the solid (removes material);
        'emboss' fuses raised text onto the surface (adds material).
    position: optional [u, v] in-face offset in mm from the face centroid, along
        the text's local X (u) and Y (v) axes. Omit to centre on the face.
    font: optional absolute path to a .ttf/.ttc font file. If omitted, common
        macOS fonts are auto-probed (Arial, then Helvetica). If none is found and
        none is supplied, the call raises RuntimeError — pass an explicit path.
    name: label for the resulting solid (default 'Text').

    Returns {handle, name, volume, text, mode, depth} where volume is the mm^3 of
    the resulting solid (less than the input for engrave, more for emboss). The
    host solid is consumed/hidden and replaced by the returned handle.
    """
    params = {"handle": handle, "face": face, "text": text, "size": size,
              "depth": depth, "mode": mode, "name": name}
    if position is not None:
        params["position"] = position
    if font is not None:
        params["font"] = font
    return _call("engrave_text", **params)


@mcp.tool()
def add_rib(
    body: str,
    sketch: str,
    thickness: float,
    midplane: bool = True,
    reversed: bool = False,
    name: str = "Rib",
) -> dict:
    """Add a reinforcing rib/web inside a PartDesign Body by thickening an OPEN
    sketch profile into a wall that fuses with the body's surrounding material.

    Args:
      body: handle of the PartDesign Body (from make_body) to add the rib to.
      sketch: handle of a sketch holding an OPEN spine (a single line, arc, or
        connected polyline) that defines where the rib runs. Must NOT be a
        closed loop. The sketch's attachment plane sets the rib's orientation.
      thickness: rib wall thickness in mm (> 0).
      midplane: if True (default) the wall is centered on the spine, growing
        thickness/2 to each side; if False it grows from one side.
      reversed: flip the extrusion sense (use if the rib lands on the wrong
        side of its sketch plane).
      name: object label.

    Returns a dict: {handle (starts 'rib_'), name, volume (the whole Body's
    Shape.Volume in mm^3 after the rib — strictly greater than before the rib,
    since a rib only adds material), thickness}.

    Fallback behaviour the caller should know: FreeCAD's native PartDesign::Rib
    type is unavailable in DriftPin's headless runtime, so the rib is built as
    an equivalent midplane PartDesign::Pad — the open spine is offset by
    +/-thickness/2 into a closed footprint and padded across the body so it
    reaches the surrounding walls. For the usual straight or smoothly-curved
    spine this matches a Rib; very intricate spines may differ from the native
    tool. Raises ValueError if the profile is closed/empty/degenerate or
    thickness <= 0, and RuntimeError if the rib adds no material (spine does not
    span between walls)."""
    params = {
        "body": body,
        "sketch": sketch,
        "thickness": thickness,
        "midplane": midplane,
        "reversed": reversed,
        "name": name,
    }
    return _call("add_rib", **params)


@mcp.tool()
def transform(
    handle: str,
    translate: list | None = None,
    rotate_axis: list | None = None,
    angle: float = 0.0,
    relative: bool = True,
) -> dict:
    """Move and/or rotate an existing object in place — first-class replacement
    for hand-poking an object's Placement via set_property.

    handle: object to move (any object with a Placement: primitive, body, feature).
    translate: [x, y, z] translation in mm (default no translation).
    rotate_axis: rotation axis as a 3-vector [x, y, z] (need not be unit length;
                 default [0, 0, 1], the Z axis).
    angle: rotation about rotate_axis in DEGREES (default 0 = no rotation).
    relative: True (default) composes this move ONTO the object's current
              placement (incremental); False sets it as the ABSOLUTE placement,
              discarding the object's prior placement.

    The same object is moved — NO new handle is created. The rotation is applied
    about the object's local origin (combine with translate to pivot elsewhere).

    Returns {handle, name, placement: {base:[x,y,z] mm, axis:[x,y,z],
    angle_deg}} describing the object's resulting placement.
    """
    params = {"handle": handle, "angle": angle, "relative": relative}
    if translate is not None:
        params["translate"] = translate
    if rotate_axis is not None:
        params["rotate_axis"] = rotate_axis
    return _call("transform", **params)


@mcp.tool()
def scale_shape(
    handle: str,
    factor: float | list,
    center: list | None = None,
    name: str = "Scaled",
) -> dict:
    """Scale a shape uniformly or per-axis, baking a fresh static solid.

    Scaling breaks parametric history, so this produces a standalone
    Part::Feature (not a linked/parametric feature); the source object is hidden
    since its geometry is consumed into the scaled copy.

    handle: source shape handle.
    factor: scalar for uniform scale, or [sx, sy, sz] for per-axis scale. All
        factors must be > 0.
    center: optional [x, y, z] mm pivot to scale about; when omitted the scale is
        about the world origin (so the shape also moves away from/toward origin).
    name: object label (default 'Scaled').

    Lengths in mm. Returns {handle, name, volume, factor} where `factor` is the
    normalized [sx, sy, sz] applied and `volume` (mm^3) equals the source volume
    times sx*sy*sz.
    """
    params = {"handle": handle, "factor": factor, "name": name}
    if center is not None:
        params["center"] = center
    return _call("scale_shape", **params)


@mcp.tool()
def copy_shape(
    handle: str,
    placement: list | None = None,
    name: str | None = None,
) -> dict:
    """Duplicate a shaped object as an INDEPENDENT static solid.

    Unlike add_part (which creates an App::Link that tracks the source), this
    deep-copies the geometry: later edits to the original do NOT propagate to
    the copy. Use it to seed a mirror/pattern, or to drop a standalone duplicate
    instance into an assembly.

    handle: handle of the source object (must have a Shape).
    placement: optional absolute [x, y, z] translation in mm applied to the
        copy's base. Omit to leave the copy coincident with the source. The
        source object is unchanged and stays visible.
    name: optional name for the new object (default '<SourceName>_copy').

    Returns {handle, name, volume}: handle is a new 'copy_N' handle, name is the
    FreeCAD object name, volume is the copied solid's volume in mm^3.
    """
    params = {"handle": handle}
    if placement is not None:
        params["placement"] = placement
    if name is not None:
        params["name"] = name
    return _call("copy_shape", **params)


@mcp.tool()
def measure_distance(
    a: str,
    b: str,
    a_ref: str | None = None,
    b_ref: str | None = None,
) -> dict:
    """Minimum distance between two entities, in mm. The workhorse measurement
    tool: lets a blind agent verify gaps, clearances, and contact.

    Args:
        a: handle of the first object.
        b: handle of the second object.
        a_ref: optional sub-shape selector on `a` to measure FROM instead of the
               whole solid -- an f_* face tag, an e_* edge tag, or a literal
               "FaceN"/"EdgeN" (1-based). Omit to use the whole shape.
        b_ref: optional sub-shape selector on `b` (same forms as a_ref).

    Measures the minimum (closest-approach) distance, so distance_mm = 0 means
    the two entities touch or interpenetrate. This does NOT report overlap
    volume -- use min_clearance / interference_check for penetration depth.

    Returns a dict (no handle; this is a measurement):
        distance_mm: float -- minimum gap in mm (0.0 when touching/intersecting).
        point_on_a:  [x, y, z] mm -- closest point on a (or its sub-shape).
        point_on_b:  [x, y, z] mm -- closest point on b (or its sub-shape).
        touching:    bool -- True when distance_mm < 1e-7.
    """
    params = {"a": a, "b": b}
    if a_ref is not None:
        params["a_ref"] = a_ref
    if b_ref is not None:
        params["b_ref"] = b_ref
    return _call("measure_distance", **params)


@mcp.tool()
def measure_angle(a: str, a_ref: str, b: str, b_ref: str) -> dict:
    """Angle (degrees) between two planar faces or two straight edges.

    a, b: object handles. a_ref, b_ref: REQUIRED sub-shape references, one per
    handle. Both must be the SAME kind:
      - face tags ('f_*' from list_faces/query_faces, or 'FaceN', or 1-based int)
        -> angle is between the faces' outward normals. Faces must be planar.
      - edge tags ('e_*' from list_edges, or 'EdgeN', or 1-based int)
        -> angle is between the edges' tangent directions. Edges must be straight.
    Mixing a face ref with an edge ref, a non-planar face, or a curved edge raises.

    Units: degrees. Returns:
      - angle_deg:      raw angle between the two direction vectors, 0..180.
      - supplement_deg: 180 - angle_deg (the complementary angle; use this for
                        the acute reading when angle_deg is obtuse).
      - kind:           "face" or "edge".
    Two adjacent box faces -> angle_deg 90. Two opposite parallel box faces ->
    angle_deg 180, supplement_deg 0. Read-only: measures, creates no geometry.
    """
    return _call("measure_angle", a=a, a_ref=a_ref, b=b, b_ref=b_ref)


@mcp.tool()
def bounding_box(handle: str, oriented: bool = False) -> dict:
    """Axis-aligned bounding box (AABB) of a shaped object. All lengths in mm,
    in world coordinates. This is a measurement — it returns numbers, not a new
    object, and does not modify the model.

    handle:   the object to measure.
    oriented: if True, also compute the tightest box at any orientation (the
              oriented bounding box, OBB) and return it under "oriented"; if the
              build can't compute it, "oriented" is null. Default False.

    Returns a dict:
      min      [x,y,z] mm — lower corner of the AABB
      max      [x,y,z] mm — upper corner of the AABB
      size     [x,y,z] mm — extents (max - min) along X, Y, Z
      center   [x,y,z] mm — AABB center point
      diagonal float  mm — space-diagonal length of the AABB
      oriented null, or {size:[x,y,z] mm, center:[x,y,z] mm, diagonal: mm} when
               oriented=True and supported — the minimum-volume box at the
               shape's best orientation (size is its three edge lengths).
    """
    params = {"handle": handle}
    if oriented:
        params["oriented"] = True
    return _call("bounding_box", **params)


@mcp.tool()
def min_clearance(a: str, b: str) -> dict:
    """Closest approach between two solids — the measured gap, richer than the
    binary interference_check. `a` and `b` are object handles. All lengths mm,
    volumes mm³.

    Returns a dict:
      status: "clear" (a positive gap separates them),
              "contact" (faces/edges touch, gap ~ 0), or
              "interference" (the solids interpenetrate / share material).
      clearance_mm: minimum distance between the two solids (mm). 0.0 when they
        are touching or interfering.
      overlap_volume_mm3: volume of interpenetration (mm³). Present ONLY when
        status == "interference".
      point_on_a: [x,y,z] of the closest point on `a`. Present when status is
        "clear" or "contact" (omitted for "interference").
      point_on_b: [x,y,z] of the closest point on `b`. Present when status is
        "clear" or "contact" (omitted for "interference").
    """
    return _call("min_clearance", a=a, b=b)


@mcp.tool()
def check_shape(handle: str) -> dict:
    """Check a shaped object's geometry validity and topology before you build on
    it. Inspection only — measures, returns no handle, mutates nothing, and does
    NOT auto-repair. Use it as a guard after booleans/sweeps/imports to confirm
    you have one clean watertight solid.

    Note: a watertight solid can still have a BLOCKED or LEAKY enclosed-flow path
    — watertightness says the shell is closed, not that an internal channel is
    unobstructed and leak-free. For ducts/manifolds/adapters use
    check_airtight_path(inlet, outlet) to verify the flow path.

    handle: the object to inspect.

    Returns a dict (volumes in mm3):
      valid            (bool)  OCC topology/geometry is sound
      watertight_solid (bool)  exactly one solid AND valid AND closed — the
                               'safe to keep building' verdict
      shape_type       (str)   e.g. 'Solid', 'Shell', 'Compound', 'Wire'
      closed           (bool)  no free boundary edges
      solids           (int)   number of solids (want 1 for a part)
      shells           (int)   number of shells
      faces            (int)   number of faces
      edges            (int)   number of edges
      volume_mm3       (float) total volume (0 for open/2D shapes)
      is_null          (bool)  the shape is empty
      check            (str)   present only when valid is False — diagnostics
                               were printed to the worker log
      check_error      (str)   present only if the diagnostic pass itself raised
    """
    return _call("check_shape", handle=handle)


@mcp.tool()
def check_airtight_path(
    handle: str,
    inlet: str | int,
    outlet: str | int,
    min_aperture_mm2: float | None = None,
    pad_mm: float | None = None,
) -> dict:
    """Functional check for an enclosed-flow part (a vacuum adapter, manifold,
    duct): is there a single connected void joining the inlet to the outlet,
    bounded by solid everywhere else? This catches what `check_shape` cannot — a
    watertight solid can still have a blocked flow path or a hidden leak.
    Inspection only: measures, returns no handle, mutates nothing.

    handle: the part to inspect.
    inlet / outlet: a face reference naming each port OPENING (the rim face around
      the hole) — an f_* tag, 'FaceN', int index, or a role/name declared with
      annotate_face (e.g. "inlet"). Both ports are sealed with cap solids and the
      negative-space void is analysed.
    min_aperture_mm2: optional minimum acceptable bottleneck cross-section; a
      connected-but-pinched path (a near-zero 'almond slit') then fails.
    pad_mm: optional bounding-box margin (default max(2.0, 0.05*diagonal)).

    Returns a dict (lengths mm, areas mm², volumes mm³):
      ok                   (bool)  connected AND not leaky AND aperture >= threshold
      status               (str)   'airtight' | 'bottleneck' | 'blocked' | 'leaky'
      connected            (bool)  one void joins inlet and outlet
      leaky                (bool)  with both ports capped the cavity still reaches
                                   ambient, so an unintended opening exists
      min_aperture_mm2     (float|null) narrowest section of the flow void
      bottleneck_point     ([x,y,z]|null) a point on the narrowest section plane
      flow_void_volume_mm3 (float|null) volume of the connecting void
      void_components      (int)   number of void solids (ambient + enclosed)
      inlet / outlet       (str)   the resolved 'FaceN' references
      pad_mm               (float) the margin used
    """
    params = {"handle": handle, "inlet": inlet, "outlet": outlet}
    if min_aperture_mm2 is not None:
        params["min_aperture_mm2"] = min_aperture_mm2
    if pad_mm is not None:
        params["pad_mm"] = pad_mm
    return _call("check_airtight_path", **params)


@mcp.tool()
def classify_face_sides(handle: str, seal_ports: bool = True) -> list:
    """Inside-vs-outside topology: for every face, decide whether its outward side
    opens into an enclosed cavity (wetted) or ambient (exterior). Answers the
    "which faces are inside the airflow path" question from issue #19 and suggests
    a role per face. Inspection only; returns no handle, mutates nothing.

    With seal_ports=True (default) any declared inlet/outlet roles (annotate_face)
    are capped first, so an OPEN duct's bore reads as the enclosed flow cavity
    rather than as ambient.

    handle: the part. seal_ports: cap declared inlet/outlet before classifying.

    Returns a list (one per face) of dicts:
      tag / index    (str)  stable f_* tag and 'FaceN'
      kind           (str)  surface kind (planar/cylindrical/…)
      side           (str)  'interior' | 'ambient' | 'ambiguous'
      suggested_role (str)  'wetted' for interior, 'ambient' for exterior, else null
      declared_role  (str)  the role already annotated on this face, if any
    """
    return _call("classify_face_sides", handle=handle, seal_ports=seal_ports)


@mcp.tool()
def section_view(
    handle: str,
    plane: str = "XY",
    offset: float = 0.0,
    emit_profile: bool = False,
    name: str = "Section",
) -> dict:
    """Cut a solid with a plane and return the cross-section it exposes. This is
    the best way to "see inside" a part blind: it measures the cut area and its
    extent, and can optionally emit the section outline as a new object for
    rendering/export. Units: mm (lengths), mm^2 (areas).

    handle: the solid to slice (a DriftPin handle).
    plane: "XY", "XZ", or "YZ" (world datum planes) OR a datum-plane handle.
           World normals follow FreeCAD: XY -> +Z, XZ -> -Y, YZ -> +X. A datum
           handle uses its local +Z as the cutting normal.
    offset: shift of the cutting plane along its normal, in mm (default 0 = the
            plane through the world origin / datum origin). E.g. plane="XY",
            offset=10 cuts at z=10.
    emit_profile: when True, add a Part::Feature holding the section wires to the
            document, register it, and return its handle (raises if the plane
            misses the shape). Default False = measure only, no new geometry.
    name: object name for the emitted profile (only used when emit_profile=True).

    Does not modify the input geometry. Returns a dict:
      plane: str (echoed),
      offset_mm: float (echoed),
      normal: [x, y, z] unit cutting-plane normal,
      section_area_mm2: float — total area of the closed cross-section wires,
      wire_count: int — number of section wires found (0 means the plane misses
                  the shape),
      closed_wire_count: int — how many of those wires are closed,
      bbox: {min:[x,y,z], max:[x,y,z], size:[dx,dy,dz]} of the section, or None
            when the plane misses the shape,
      handle: str — handle of the emitted profile (ONLY when emit_profile=True),
      name: str — its FreeCAD object name (ONLY when emit_profile=True).
    """
    params = {
        "handle": handle,
        "plane": plane,
        "offset": offset,
        "emit_profile": emit_profile,
        "name": name,
    }
    return _call("section_view", **params)


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
    through: str | None = None,
    direction: str | None = None,
    reversed: bool = False,
    name: str = "Pocket",
) -> dict:
    """Subtract a pad of `length` mm from the body. through_all ignores length.

    through ('wall'|'body'): preferred over through_all. 'wall' ray-casts the
    body to find the first exit boundary and cuts exactly one wall thick —
    correct for solids (one wall = full thickness) AND shelled bodies. 'body'
    is the legacy ThroughAll; on a shelled body it punches through every wall
    and ruins the cavity. Implies direction='into_body'. Result carries
    wall_depth_mm so the caller can verify.
    direction (preferred over `reversed`): 'into_body' makes the cut actually
    remove material; 'away_from_body' extrudes outside the body. The tool
    probes both Reversed values and picks the one matching intent.
    reversed: legacy raw flag, used only if neither `through` nor `direction`
    is set."""
    params = {
        "sketch": sketch, "length": length,
        "through_all": through_all, "reversed": reversed, "name": name,
    }
    if direction is not None:
        params["direction"] = direction
    if through is not None:
        params["through"] = through
    return _call("pocket", **params)


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
    model_thread: bool | None = None,
    intended_for: str | None = None,
    through: str | None = None,
    direction: str | None = None,
    reversed: bool = False,
    name: str = "Hole",
) -> dict:
    """Drill a parametric Hole from a sketch (one or more circles).

    sketch: handle of a sketch placed on a face of an existing body feature.
    depth_type: 'Dimension' (use `depth`) or 'ThroughAll'.
    cut_type: 'None' | 'Counterbore' | 'Countersink' | 'Counterdrill'.
              When non-None, cut_diameter (head clearance) and cut_depth apply.
    threaded=True applies a tap. thread_type / thread_size are COUPLED enums —
    valid thread_size values DEPEND on thread_type ('M4' fits 'ISOMetricProfile'
    but not 'UNC'). Use list_thread_options() to discover thread_type values
    and list_thread_options(thread_type=...) for that type's valid sizes.
    intended_for ('print'|'machine'|'drawing'): drives ModelThread default when
    threaded=True so the caller doesn't have to know what ModelThread means.
      print   → ModelThread=True. Required for 3D-printed threaded holes —
                the screw must engage the printed thread geometry; a smooth
                pilot won't tap itself.
      machine → ModelThread=False. CAM software reads thread metadata and
                drives a physical tap. Modeling thread bloats files and
                fights patterns/fillets.
      drawing → ModelThread=False. Drawings annotate threads symbolically.
    Explicit model_thread overrides intended_for.
    through ('wall'|'body'): preferred over depth_type/depth. 'wall' ray-casts
    to the first exit boundary and drills exactly one wall thick — critical on
    shelled bodies where 'body' (ThroughAll) would destroy the cavity. Implies
    direction='into_body'. Result carries wall_depth_mm.
    direction (preferred over `reversed`): 'into_body' picks the Reversed value
    that actually removes material; 'away_from_body' picks the value that
    removes none. Hole and Pocket interpret the raw flag differently.
    reversed: legacy raw flag, used only if neither `through` nor `direction`
    is set.
    """
    params = {
        "sketch": sketch, "diameter": diameter, "depth_type": depth_type,
        "depth": depth, "cut_type": cut_type, "threaded": threaded,
        "reversed": reversed, "name": name,
    }
    if model_thread is not None:
        params["model_thread"] = model_thread
    if intended_for is not None:
        params["intended_for"] = intended_for
    if cut_diameter is not None:
        params["cut_diameter"] = cut_diameter
    if cut_depth is not None:
        params["cut_depth"] = cut_depth
    if thread_type is not None:
        params["thread_type"] = thread_type
    if thread_size is not None:
        params["thread_size"] = thread_size
    if direction is not None:
        params["direction"] = direction
    if through is not None:
        params["through"] = through
    return _call("hole", **params)


@mcp.tool()
def list_thread_options(thread_type: str | None = None) -> dict:
    """Discover the COUPLED ThreadType / ThreadSize enums on the hole tool.

    Call with no args to list valid thread_type values. Call with
    thread_type=... to list the valid thread_size values for that type
    (the coupling: thread_size='M4' is valid for 'ISOMetricProfile' but not
    for 'UNC'). Use this BEFORE calling hole(threaded=True, thread_type=...,
    thread_size=...) to avoid a failed enum-value call.

    Returns either {thread_types: [...]} or {thread_type, thread_sizes: [...]}.
    """
    params = {}
    if thread_type is not None:
        params["thread_type"] = thread_type
    return _call("list_thread_options", **params)


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
def verify_feature(
    handle: str,
    expected_delta_mm3: float,
    tolerance: float = 0.05,
    abs_tolerance: float = 0.01,
) -> dict:
    """Compare a feature's actual volume change against an expected signed
    delta. Run after each subtractive/additive operation to catch silent
    failures — Pocket on a curved surface that under-cut, Hole that drilled
    outside the body, Cut whose Tool didn't intersect the Base.

    handle: PartDesign feature (Pad/Pocket/Hole/Revolve/etc.) or Part::Cut.
    expected_delta_mm3: SIGNED expected change. Subtractive → negative,
                        additive → positive. Wrong sign is its own useful
                        error.
    tolerance: relative tolerance (default 0.05 = 5%).
    abs_tolerance: absolute mm³ fallback for tiny expected magnitudes
                   (default 0.01). Pass if EITHER tolerance is satisfied.

    Returns {passed, message, actual_delta_mm3, expected_delta_mm3, ratio,
    previous_volume_mm3, current_volume_mm3, handle, name}. Does NOT raise
    on mismatch — inspect `passed` to decide whether to abort."""
    return _call(
        "verify_feature",
        handle=handle,
        expected_delta_mm3=expected_delta_mm3,
        tolerance=tolerance,
        abs_tolerance=abs_tolerance,
    )


@mcp.tool()
def set_visibility(handle: str, visible: bool) -> dict:
    """Override an object's persistent Visibility flag. By default save_document
    auto-hides producer-inputs (the Base/Tool of a Cut, features inside a Body)
    so the re-opened doc shows just the final composition. Use this to override —
    e.g. to keep a reference primitive visible next to a derived part. Note
    that the next save_document with visibility_hygiene=True (the default) may
    re-hide it; pass visibility_hygiene=False to save_document to lock the
    override in."""
    return _call("set_visibility", handle=handle, visible=visible)


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
def run_script(code: str, auto_register: bool = True) -> Any:
    """Escape hatch: execute Python in the worker with App/Part/ObjectsFem in scope.

    Set `__result__` in the script to return a JSON-serializable value.

    auto_register (default True): any new shape-bearing object the script
    creates is automatically registered into the handle table. The result
    includes a `registered` list of {handle, name, type} entries so the next
    tool call (render_view, list_faces, fillet_edges, mass_properties, etc.)
    can address script-created objects via handle without a separate
    register_handle round-trip.

    Returns {result, registered}.
    """
    return _call("run_script", code=code, auto_register=auto_register)


@mcp.tool()
def register_handle(object: str, prefix: str = "manual") -> dict:
    """Register an existing FreeCAD object into the DriftPin handle table.
    Use after run_script (when auto_register=False) or after open_document to
    bring objects into the handle ecosystem so subsequent tool calls accept
    them via handle.

    object: the FreeCAD object's .Name (e.g. 'Helix001', 'Cut').
    prefix: handle prefix (default 'manual'). Each call returns a fresh handle;
            registering the same object twice produces two aliases.
    Returns {handle, name, type, label}."""
    return _call("register_handle", object=object, prefix=prefix)


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
    mate: dict | None = None,
) -> dict:
    """Add a part to an assembly via App::Link.

    source is one of:
      {"handle": "<handle>"}                          — link an in-doc body
      {"path": "/path/to/part.FCStd"}                 — link first body / subassembly
      {"path": "/path/to/part.FCStd", "object": "X"}  — link named object
    placement: [x, y, z] or {position: [...], axis: [...], angle_deg: ...}.
    mate: place by aligning this part's published interface frame to an
      already-placed parent's, instead of (or after) a raw placement:
      {"child_iface": "<name>", "parent": "<link-name|handle>",
       "parent_iface": "<name>"}. Frames come from publish_interface.
    """
    params = {"assembly": assembly, "source": source, "name": name}
    if placement is not None:
        params["placement"] = placement
    if mate is not None:
        params["mate"] = mate
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
def bom_extract(
    assembly: str, density: float | None = None, recursive: bool = True
) -> list:
    """Walk an assembly and return [{part, count, total_volume_mm3, total_mass_kg?}, ...]
    grouped by source (component file + object), NOT the bare object name, so two
    distinct components both named "Box" don't collapse into one row. density
    (kg/mm³) is optional.

    recursive (default True): descend into linked subassemblies (App::Part) so the
    BOM flattens to leaf parts. False counts each subassembly as one line."""
    params = {"assembly": assembly, "recursive": recursive}
    if density is not None:
        params["density"] = density
    return _call("bom_extract", **params)


@mcp.tool()
def envelope_check(assembly: str, envelopes: dict) -> list:
    """Keep-out gate: assert each named part's world bounding box stays inside its
    declared envelope. envelopes maps a part's link name (or label) to
    {"min": [x,y,z], "max": [x,y,z]} in the assembly frame. Returns violations
    [{part, axis, got, allowed}, ...]; empty means everything is within its box."""
    return _call("envelope_check", assembly=assembly, envelopes=envelopes)


@mcp.tool()
def publish_interface(handle: str, name: str, frame: dict) -> dict:
    """Record a named interface frame on a component so other parts can mate to it
    — the published "here is where you bolt to me, and how it's oriented".

    handle: the component's shaped object.
    name: interface name (e.g. "lid_seat", "bolt_circle", "bore_axis").
    frame: {origin:[x,y,z], z_axis:[...]?, x_axis:[...]?}. z_axis defaults +Z,
      x_axis +X. Extra keys (e.g. bolt-circle metadata) are stored verbatim.

    Persists in the component's .FCStd as a JSON property bag, so merge_assembly
    can mate against it later. Returns {handle, name, frame, interfaces}."""
    return _call("publish_interface", handle=handle, name=name, frame=frame)


@mcp.tool()
def annotate_face(
    handle: str,
    face: str | int,
    role: str,
    name: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Declare the semantic ROLE of a face — what it is FOR — so later edits can be
    checked against intent instead of re-derived from raw geometry. The role binds
    to the face's stable f_* tag and persists in the .FCStd as a JSON property bag
    (same mechanism as publish_interface); it survives save/reopen. Once declared,
    check_airtight_path accepts the role/name directly (e.g. inlet="inlet").

    handle: the part.
    face: an f_* tag, 'FaceN', or int index of the face to annotate.
    role: one of 'inlet' | 'outlet' | 'sealing' | 'wetted' | 'ambient' | 'mating'.
    name: optional unique label for this annotation (default: the role, then
      role_2, role_3, …); re-using a name updates that annotation.
    meta: optional dict stored verbatim (e.g. {"spec": "32mm hose"}).

    Returns a dict: {handle, name (the annotation key used), role, tag (the f_*
    the role is bound to), index ('FaceN' at annotation time), roles (sorted list
    of all annotation names now on the part)}."""
    params = {"handle": handle, "face": face, "role": role}
    if name is not None:
        params["name"] = name
    if meta is not None:
        params["meta"] = meta
    return _call("annotate_face", **params)


@mcp.tool()
def list_face_roles(handle: str) -> list:
    """Read back the semantic face roles declared on a part (see annotate_face).

    Each entry re-resolves its stored tag against the CURRENT geometry, so a
    drifted or deleted face is reported rather than silently resolving wrong.

    Returns a list (sorted by name) of dicts:
      name    (str)   the annotation key
      role    (str)   inlet | outlet | sealing | wetted | ambient | mating
      tag     (str)   the f_* face tag the role is bound to
      present (bool)  whether that tag still resolves on the current shape
      index   (str)   'FaceN' on the current shape (only when present)
      meta    (dict)  the verbatim metadata (only when set)
    """
    return _call("list_face_roles", handle=handle)


@mcp.tool()
def declare_intent(handle: str, contract: dict) -> dict:
    """Record the functional invariants a part must keep satisfying, so they can
    be re-checked after every edit (see verify_intent). Persists in the .FCStd as
    a JSON property bag (DP_Intent); one contract per part — re-declaring replaces.

    handle: the part.
    contract: a dict with any of these (declare at least one):
      watertight     (bool)  require check_shape's watertight_solid verdict.
      airtight_path  (dict)  {inlet, outlet, min_aperture_mm2?}; each port is a
                             face tag / 'FaceN' / int / declared role-or-name.
      required_faces (list)  face tags / 'FaceN' / declared role-or-names that
                             must still resolve (catches a deleted/drifted face).

    Returns {handle, contract} — the stored contract."""
    return _call("declare_intent", handle=handle, contract=contract)


@mcp.tool()
def verify_intent(handle: str) -> dict:
    """Re-run every invariant declared with declare_intent — the regression gate
    to run after each edit. Composes check_shape / check_airtight_path / face-role
    resolution; never raises on a failing invariant (a failure is a passed=False
    row), so it is safe to call in a loop. Inspection only; mutates nothing.

    handle: the part (must have a declared intent contract).

    Returns a dict:
      handle   (str)
      ok       (bool)  True iff every declared invariant passed
      results  (list)  one {invariant, passed, detail} per declared invariant —
                       invariant in {watertight, airtight_path, required_faces},
                       detail a human-readable summary of what was measured
    """
    return _call("verify_intent", handle=handle)


@mcp.tool()
def interface_align_check(assembly: str, pairs: list, tol_mm: float = 1e-3) -> list:
    """Gate: verify declared interface pairs coincide in world space — the
    "do the OTHER interfaces line up?" check for multi-interface mates. After the
    primary mate seats a part, this confirms its secondary interfaces (a second
    bolt pattern, a bore axis) actually meet the parent's.

    pairs: [{child, child_iface, parent, parent_iface}, ...] (child/parent are
    link names in the assembly). Returns misaligned pairs [{..., gap_mm}], empty
    if every pair coincides within tol_mm."""
    return _call("interface_align_check", assembly=assembly, pairs=pairs,
                 tol_mm=tol_mm)


@mcp.tool()
def merge_assembly(manifest: str) -> dict:
    """Construct-up an assembly from a manifest JSON (the coordinator's one call):
    create the doc, link each component by file path, place it, recompute, and run
    the gates. Component files resolve relative to the manifest's directory; links
    auto-reload, so re-running picks up updated components (deterministic,
    idempotent).

    manifest shape:
      { "name": "gearbox", "root": "gearbox.FCStd",
        "components": {"<id>": {"file": "rel/part.FCStd", "object": "<name>"?,
                                "envelope": {"min":[...],"max":[...]}?}},
        "instances": [{"component":"<id>", "name":"<instance>"?,
                       "placement": [x,y,z] | {position,axis,angle_deg},
                       "mate": {"child_iface","parent","parent_iface",
                                "verify_align":{"child_iface","parent_iface"}?}?}],
        "mates": [{"child","parent","child_iface","parent_iface",
                   "verify_align":{...}?}]? }

    Placement positions anchors; mate-by-frame positions everything else by
    aligning published interface frames (see publish_interface). Returns
    {assembly, doc, root, placed, gates:{interference, bom, envelope,
    interface_align?}, ok}."""
    return _call("merge_assembly", manifest=manifest)


@mcp.tool()
def assembly_lock(manifest: str, lockfile: str | None = None) -> dict:
    """Write a lockfile recording each component's content hash, published-
    interface hash, and mate dependencies — the provenance baseline a coordinator
    uses to detect drift across a team. Call after a clean merge. lockfile
    defaults to <manifest>.lock.json. Returns {lockfile, components}."""
    return _call("assembly_lock", manifest=manifest, lockfile=lockfile)


@mcp.tool()
def assembly_lock_check(manifest: str, lockfile: str | None = None) -> dict:
    """Compare current component files to a lockfile and classify drift (change
    propagation, RFC §9):
      modified          — file changed since lock
      interface_changed — published interface frames moved (subset of modified)
      stale             — mates to an interface_changed component and was NOT
                          itself rebuilt: a neighbor that needs re-dispatch
      new / removed     — components added to / dropped from the manifest
    ok = nothing stale and no new/removed (safe to re-merge without re-dispatch).
    Returns {modified, interface_changed, stale, new, removed, ok}."""
    return _call("assembly_lock_check", manifest=manifest, lockfile=lockfile)


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
def render_photoreal(
    handle: str,
    renderer: str = "Povray",
    view: str = "iso",
    material: str | None = None,
    width: int = 800,
    height: int = 600,
) -> dict:
    """Photorealistic render of a shaped object via the FreeCAD Render workbench
    (an external renderer, e.g. POV-Ray) — a presentation-quality "nice picture",
    unlike render_view's fast software-rasterized preview.

    Requires the Render addon and a renderer binary to be installed (see
    docs/RENDER_WORKBENCH.md); raises with install guidance otherwise. Renders in
    an isolated temporary document, so the live model is never modified.

    view: 'iso' | 'top' | 'bottom' | 'front' | 'back' | 'left' | 'right' | 'side'.
    material: optional Render material library card — e.g. 'Gold', 'Glass',
        'Aluminium', 'GlossyPlastic', 'RoughPlastic', 'Iron', 'Brass'. Omitted
        gives a neutral default material; an unknown name raises with the full list.
    Returns {png_base64, png_path, renderer, view, material, width, height}.

    Presentation-only: output is not bit-reproducible, so it is kept out of the
    reliability/golden tests. External renders can take seconds to minutes, so this
    call uses an extended worker timeout.
    """
    return _call(
        "render_photoreal", _timeout=600.0,
        handle=handle, renderer=renderer, view=view, material=material,
        width=width, height=height,
    )


@mcp.tool()
def render_photoreal_submit(
    handle: str,
    renderer: str = "Povray",
    view: str = "iso",
    material: str | None = None,
    width: int = 800,
    height: int = 600,
) -> dict:
    """Start a photorealistic render asynchronously; returns immediately with
    {job_id, status} instead of blocking for the whole render.

    Use this (rather than render_photoreal) for renders that may take a long time —
    heavy materials/renderers, large images — so the worker stays responsive. The
    external renderer runs in the background; poll render_job(job_id) until status is
    'done' (then it returns the PNG) or 'failed'. Same arguments as render_photoreal;
    requires the Render addon + a renderer binary (see docs/RENDER_WORKBENCH.md).
    """
    return _call(
        "render_photoreal_submit",
        handle=handle, renderer=renderer, view=view, material=material,
        width=width, height=height,
    )


@mcp.tool()
def render_job(job_id: str, discard: bool = False) -> dict:
    """Poll an async render started by render_photoreal_submit.

    Returns {job_id, status} where status is 'running', 'done', or 'failed'. When
    'done', also returns {png_base64, png_path, renderer, view, material, width,
    height}; when 'failed', {error}. The result remains available for repeat polls.

    Pass discard=True once you have a terminal result to free the job immediately
    (drops the cached image and closes its temp document); ignored while running.
    Jobs are also auto-evicted oldest-first once finished jobs exceed an internal cap.
    """
    return _call("render_job", job_id=job_id, discard=discard)


@mcp.tool()
def render_capabilities() -> dict:
    """Report which photoreal renderers are usable right now, and whether the FreeCAD
    Render addon imports — so you can pick a working renderer for render_photoreal
    instead of discovering availability by trial and error.

    Takes no arguments. Resolves each renderer's binary exactly as render_photoreal
    would (DRIFTPIN_<R>_PATH env override -> FreeCAD prefs -> PATH -> per-OS install
    dirs), but renders nothing and changes no settings.

    Returns {addon_importable (bool), default_renderer ('Povray'), platform,
    available (sorted list of ready renderer names for the `renderer=` argument),
    renderers: {name: {available, param_key, batch, binaries, and either path (the
    resolved binary) or install_hint}}, materials (library card names usable as
    render_photoreal's material= argument, present only when the addon imports), and
    addon_error (present only when the addon does not import)}.
    """
    return _call("render_capabilities")


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


@mcp.tool()
def material_get(name: str) -> dict:
    """Look up a material by name (e.g. 'AL6061-T6'). Returns the full property
    card as SI quantity strings (YoungsModulus, PoissonRatio, Density,
    yield_strength, fracture_toughness, thermal_conductivity, cte, rough_cost,
    refractive_index where applicable, source, basis). The structural keys are
    FEM-card-compatible, so the result feeds fem_set_material directly. On a miss
    returns {ok:false, reason} with a did_you_mean suggestion."""
    return _call("material_get", name=name)


@mcp.tool()
def material_select(
    criteria: dict | None = None,
    rank_by: str = "specific_strength",
) -> dict:
    """Ashby-style selection: filter the corpus, then rank survivors.

    criteria keys are min_<accessor>/max_<accessor> (e.g. min_yield_mpa,
    max_density_g_cc, min_service_temp_c). rank_by: specific_strength |
    specific_stiffness | strength | stiffness | cost | density. Returns
    {rank_by, count, criteria, candidates:[{name, score, yield_mpa, density_g_cc,
    youngs_gpa, cost_usd_kg}, ...]} best-first; an empty filter returns no
    candidates rather than the closest miss."""
    return _call("material_select", criteria=criteria or {}, rank_by=rank_by)


@mcp.tool()
def material_list(category: str | None = None) -> dict:
    """List available materials, optionally filtered to one category
    ('aluminum' | 'steel' | 'titanium' | 'magnesium' | 'polymer' | 'glass').
    Returns {count, category, materials:[{name, category}, ...]} sorted by name."""
    return _call("material_list", category=category)


@mcp.tool()
def bolted_joint_check(
    bolt_dia_mm: float,
    pitch_mm: float | None = None,
    torque_nm: float | None = None,
    preload_n: float | None = None,
    k_factor: float = 0.2,
    external_load_n: float = 0.0,
    joint_stiffness_ratio: float = 0.3,
    material: str = "Steel-4140-QT",
    proof_strength_mpa: float | None = None,
) -> dict:
    """Rate a bolted joint (VDI 2230-lite). Preload from torque via T=K*F*d (pass
    torque_nm OR preload_n). Returns {preload_n, tensile_stress_area_mm2,
    bolt_stress_mpa, preload_pct_proof, bolt_stress_with_load_mpa,
    separation_load_n, separation_margin, pass, governing}."""
    params = {"bolt_dia_mm": bolt_dia_mm, "k_factor": k_factor,
              "external_load_n": external_load_n,
              "joint_stiffness_ratio": joint_stiffness_ratio, "material": material}
    for k, v in (("pitch_mm", pitch_mm), ("torque_nm", torque_nm),
                 ("preload_n", preload_n), ("proof_strength_mpa", proof_strength_mpa)):
        if v is not None:
            params[k] = v
    return _call("bolted_joint_check", **params)


@mcp.tool()
def bearing_life(
    dynamic_load_c_n: float,
    equivalent_load_p_n: float,
    speed_rpm: float,
    kind: str = "ball",
    target_hours: float | None = None,
) -> dict:
    """Basic rating life L10 (ISO 281): L10=(C/P)^p rev (p=3 ball, 10/3 roller),
    L10h=L10*1e6/(60n). Returns {l10_million_rev, l10_hours, load_ratio, exponent,
    pass} (pass vs target_hours when given)."""
    params = {"dynamic_load_c_n": dynamic_load_c_n,
              "equivalent_load_p_n": equivalent_load_p_n,
              "speed_rpm": speed_rpm, "kind": kind}
    if target_hours is not None:
        params["target_hours"] = target_hours
    return _call("bearing_life", **params)


@mcp.tool()
def spring_check(
    wire_dia_mm: float,
    coil_mean_dia_mm: float,
    active_coils: float,
    force_n: float | None = None,
    deflection_mm: float | None = None,
    material: str = "Steel-1045",
    free_length_mm: float | None = None,
) -> dict:
    """Rate a helical compression spring (Wahl). rate k=G d^4/(8 D^3 Na); corrected
    shear tau=Kw 8FD/(pi d^3). Pass force_n OR deflection_mm. Returns
    {spring_index, wahl_factor, rate_n_mm, force_n, deflection_mm, shear_stress_mpa,
    slenderness, buckling_flag, shear_sf, pass}."""
    params = {"wire_dia_mm": wire_dia_mm, "coil_mean_dia_mm": coil_mean_dia_mm,
              "active_coils": active_coils, "material": material}
    for k, v in (("force_n", force_n), ("deflection_mm", deflection_mm),
                 ("free_length_mm", free_length_mm)):
        if v is not None:
            params[k] = v
    return _call("spring_check", **params)


@mcp.tool()
def gear_rating(
    module_mm: float,
    teeth: int,
    face_width_mm: float,
    tangential_force_n: float | None = None,
    power_w: float | None = None,
    pinion_speed_rpm: float | None = None,
    material: str = "Steel-4140-QT",
    lewis_form_factor: float | None = None,
) -> dict:
    """Rate spur-gear tooth bending (Lewis): sigma=Ft/(b*m*Y). Pass
    tangential_force_n, or power_w + pinion_speed_rpm. Returns {tangential_force_n,
    pitch_dia_mm, pitch_line_velocity_m_s, lewis_form_factor, bending_stress_mpa,
    allowable_bending_mpa, bending_sf, pass}. First-order screen, not full AGMA."""
    params = {"module_mm": module_mm, "teeth": teeth,
              "face_width_mm": face_width_mm, "material": material}
    for k, v in (("tangential_force_n", tangential_force_n), ("power_w", power_w),
                 ("pinion_speed_rpm", pinion_speed_rpm),
                 ("lewis_form_factor", lewis_form_factor)):
        if v is not None:
            params[k] = v
    return _call("gear_rating", **params)


@mcp.tool()
def belt_drive(
    power_w: float,
    small_pulley_dia_mm: float,
    large_pulley_dia_mm: float,
    center_distance_mm: float,
    small_pulley_rpm: float,
    friction_coef: float = 0.3,
    vbelt_groove_deg: float | None = None,
    tight_side_limit_n: float | None = None,
) -> dict:
    """Rate a belt drive (Eytelwein/capstan). Wrap theta=pi-2asin((D-d)/2C),
    Fe=P/V, T1/T2=e^(mu*theta) (V-belt divides mu by sin(beta/2)). Returns
    {wrap_angle_deg, belt_speed_m_s, effective_force_n, tension_ratio, tight_side_n,
    slack_side_n, transmissible_power_w, pass}."""
    params = {"power_w": power_w, "small_pulley_dia_mm": small_pulley_dia_mm,
              "large_pulley_dia_mm": large_pulley_dia_mm,
              "center_distance_mm": center_distance_mm,
              "small_pulley_rpm": small_pulley_rpm, "friction_coef": friction_coef}
    for k, v in (("vbelt_groove_deg", vbelt_groove_deg),
                 ("tight_side_limit_n", tight_side_limit_n)):
        if v is not None:
            params[k] = v
    return _call("belt_drive", **params)


@mcp.tool()
def press_fit_stress(
    shaft_dia_mm: float,
    hub_outer_dia_mm: float,
    interference_mm: float,
    engagement_length_mm: float,
    material: str = "Steel-A36",
    friction_coef: float = 0.15,
) -> dict:
    """Rate an interference (press/shrink) fit via Lamé. p=delta_r E (ro^2-rc^2)/
    (2 rc ro^2); hub bore hoop=p(ro^2+rc^2)/(ro^2-rc^2); torque=2pi mu p rc^2 L.
    interference_mm is diametral. Returns {contact_pressure_mpa, hub_hoop_stress_mpa,
    torque_capacity_nm, axial_force_n, hub_yield_sf, pass}."""
    return _call("press_fit_stress", shaft_dia_mm=shaft_dia_mm,
                 hub_outer_dia_mm=hub_outer_dia_mm, interference_mm=interference_mm,
                 engagement_length_mm=engagement_length_mm, material=material,
                 friction_coef=friction_coef)


@mcp.tool()
def seal_check(
    cross_section_dia_mm: float,
    groove_depth_mm: float,
    groove_width_mm: float,
    application: str = "static_radial",
    max_gland_fill_pct: float = 90.0,
) -> dict:
    """Rate an O-ring gland (pairs with oring_groove): squeeze=W-depth, fill=
    (pi/4 W^2)/(width*depth). Squeeze must sit in the application band (static
    15-30%, dynamic 10-20%), fill below max_gland_fill_pct. Returns {squeeze_mm,
    squeeze_pct, gland_fill_pct, squeeze_range_pct, within_squeeze, within_fill,
    pass}."""
    return _call("seal_check", cross_section_dia_mm=cross_section_dia_mm,
                 groove_depth_mm=groove_depth_mm, groove_width_mm=groove_width_mm,
                 application=application, max_gland_fill_pct=max_gland_fill_pct)


@mcp.tool()
def tolerance_stackup(
    chain: list,
    method: str = "worstcase",
    samples: int = 10000,
    spec_min: float | None = None,
    spec_max: float | None = None,
) -> dict:
    """Stack a dimension chain. Each chain entry is {name, nominal, plus, minus}
    with plus/minus the signed upper/lower deviations (plus>=minus; symmetric
    shorthand {nominal, tol}); add direction:-1 for a subtractive/gap link.
    method: worstcase | rss | montecarlo (each adds a deeper block). Half-bands
    are read as 3-sigma; cpk/pct_in_spec use spec_min/spec_max if given, else the
    worst-case bounds. Returns {nominal, worstcase:{min,max,spread},
    rss:{sigma,min_3s,max_3s}, montecarlo:{mean,std,cpk,pct_in_spec,spec}}."""
    params = {"chain": chain, "method": method, "samples": samples}
    if spec_min is not None:
        params["spec_min"] = spec_min
    if spec_max is not None:
        params["spec_max"] = spec_max
    return _call("tolerance_stackup", **params)


@mcp.tool()
def fit_check(hole: dict, shaft: dict) -> dict:
    """Classify a hole/shaft pair. hole and shaft are {nominal, plus, minus}
    (signed deviations) or {nominal, tol}. Returns {fit_class:'clearance'|
    'transition'|'interference', min_clearance, max_clearance, nominal_clearance,
    prob_interference} (prob from a normal model with half-band = 3-sigma)."""
    return _call("fit_check", hole=hole, shaft=shaft)


@mcp.tool()
def fit_class(basic_size: float, fit: str = "H7/g6") -> dict:
    """ISO 286 limits for a fit code (e.g. 'H7/g6'), in mm. v1 covers a hole-basis
    H with shaft clearance letters (h, g, f, e). Returns {basic_size, fit,
    hole:{upper_dev,lower_dev,min,max}, shaft:{...}, fit_class, min_clearance,
    max_clearance, prob_interference}. Errors on an out-of-table size (>500 mm) or
    an unsupported code (non-H hole or interference shaft letter)."""
    return _call("fit_class", basic_size=basic_size, fit=fit)


@mcp.tool()
def gdt_check(
    control: str,
    zone: float,
    actual: float | None = None,
    offset: dict | None = None,
    mmc_bonus: float = 0.0,
    datum_refs: list | None = None,
) -> dict:
    """Check a measured feature against a GD&T tolerance zone. control: position |
    flatness | straightness | circularity | cylindricity | perpendicularity |
    parallelism | angularity | concentricity | runout | total_runout |
    profile_line | profile_surface. actual is the measured deviation; for position
    pass offset={x,y} to use the diametral 2*hypot(x,y). mmc_bonus adds bonus
    tolerance. Returns {control, zone, effective_zone, actual, margin, pass,
    datum_refs}."""
    params = {"control": control, "zone": zone, "mmc_bonus": mmc_bonus}
    if actual is not None:
        params["actual"] = actual
    if offset is not None:
        params["offset"] = offset
    if datum_refs is not None:
        params["datum_refs"] = datum_refs
    return _call("gdt_check", **params)


@mcp.tool()
def fatigue_check(
    stress_range_mpa: float,
    mean_stress_mpa: float = 0.0,
    cycles: float = 1_000_000.0,
    material: str = "Steel-1045",
    endurance_mpa: float | None = None,
    uts_mpa: float | None = None,
) -> dict:
    """Rate fatigue life (S-N Basquin + Goodman mean-stress correction). σ_a =
    stress_range/2; infinite-life SF = 1/(σ_a/σ_e + σ_m/σ_uts); finite life from
    an equivalent fully-reversed amplitude on a log-log S-N line. σ_e/σ_uts come
    from the material (or overrides). pass = survives `cycles` (σ_ar ≤ σ_e ⇒
    infinite life); a tensile mean ≥ σ_uts fails outright. Returns
    {stress_amplitude_mpa, mean_stress_mpa, endurance_mpa, uts_mpa,
    equiv_reversed_mpa, safety_factor, life_cycles, required_cycles, pass,
    governing_mode, endurance_basis}."""
    params = {"stress_range_mpa": stress_range_mpa, "mean_stress_mpa": mean_stress_mpa,
              "cycles": cycles, "material": material}
    if endurance_mpa is not None:
        params["endurance_mpa"] = endurance_mpa
    if uts_mpa is not None:
        params["uts_mpa"] = uts_mpa
    return _call("fatigue_check", **params)


@mcp.tool()
def fracture_check(
    stress_mpa: float,
    crack_len_mm: float,
    material: str = "Steel-1045",
    geometry_factor: float = 1.12,
    fracture_toughness_mpa_sqrt_m: float | None = None,
) -> dict:
    """Rate brittle fracture (LEFM): K = Y·σ·√(π·a) vs K_IC. a is crack length in
    mm; Y (geometry_factor) defaults 1.12 (edge crack), 1.0 for a centre crack.
    K_IC from the material (or override). Critical crack a_c = (K_IC/(Y·σ))²/π.
    Returns {k_applied_mpa_sqrt_m, k_ic_mpa_sqrt_m, geometry_factor, safety_factor,
    margin, critical_crack_mm, pass}; a crack past a_c gives SF<1 and margin<0."""
    params = {"stress_mpa": stress_mpa, "crack_len_mm": crack_len_mm,
              "material": material, "geometry_factor": geometry_factor}
    if fracture_toughness_mpa_sqrt_m is not None:
        params["fracture_toughness_mpa_sqrt_m"] = fracture_toughness_mpa_sqrt_m
    return _call("fracture_check", **params)


@mcp.tool()
def wear_estimate(
    load_n: float,
    sliding_dist_m: float,
    material_pair: list | None = None,
    wear_coef: float | None = None,
    hardness_mpa: float | None = None,
    apparent_area_mm2: float | None = None,
    max_depth_mm: float | None = None,
) -> dict:
    """Estimate sliding wear (Archard): V = k·F·s/H. k (wear_coef) is empirical —
    pass it, or it's looked up by the material_pair's category pair (order-of-
    magnitude). H (hardness_mpa) defaults to Tabor 3·σ_y of the softer member.
    With apparent_area_mm2 a mean depth is reported and gated by max_depth_mm.
    Returns {wear_coef, hardness_mpa, volume_loss_mm3, depth_loss_mm, coef_basis,
    hardness_basis, pass}."""
    params = {"load_n": load_n, "sliding_dist_m": sliding_dist_m}
    for k, v in (("material_pair", material_pair), ("wear_coef", wear_coef),
                 ("hardness_mpa", hardness_mpa),
                 ("apparent_area_mm2", apparent_area_mm2),
                 ("max_depth_mm", max_depth_mm)):
        if v is not None:
            params[k] = v
    return _call("wear_estimate", **params)


@mcp.tool()
def creep_flag(
    stress_mpa: float,
    temp_c: float,
    material: str = "Steel-1045",
    max_service_temp_c: float | None = None,
) -> dict:
    """Screen for creep risk: compare operating temperature to the material's max
    service temperature (Materials DB, or an override). A screen, not a
    Larson-Miller life model. pass = below the service limit. Returns
    {operating_temp_c, service_temp_c, margin_c, stress_mpa, creep_risk, pass,
    reason}."""
    params = {"stress_mpa": stress_mpa, "temp_c": temp_c, "material": material}
    if max_service_temp_c is not None:
        params["max_service_temp_c"] = max_service_temp_c
    return _call("creep_flag", **params)


@mcp.tool()
def thermal_lumped(
    mass_g: float,
    power_w: float,
    h_conv: float,
    area_mm2: float,
    c_p: str | float | None = None,
    material: str | None = None,
    t_ambient_c: float = 25.0,
    duration_s: float | None = None,
    emissivity: float = 0.8,
) -> dict:
    """Lumped first-order transient warm-up (no mesh). ΔT_ss = P/(h·A),
    τ = m·c_p/(h·A), T(t) = T_amb + ΔT_ss·(1−e^(−t/τ)). c_p is an explicit
    value/quantity-string ('900 J/kg/K') or read from `material`. With duration_s
    the temperature + fraction-of-steady reached are returned. A radiation screen
    flags when the steady-state radiative HTC exceeds h_conv. Returns
    {t_ambient_c, delta_t_steady_k, t_steady_c, time_constant_s, t_final_c,
    reached_steady_pct, h_rad_w_m2k, radiation_significant}."""
    params = {"mass_g": mass_g, "power_w": power_w, "h_conv": h_conv,
              "area_mm2": area_mm2, "t_ambient_c": t_ambient_c,
              "emissivity": emissivity}
    for k, v in (("c_p", c_p), ("material", material), ("duration_s", duration_s)):
        if v is not None:
            params[k] = v
    return _call("thermal_lumped", **params)


def run():
    mcp.run()


if __name__ == "__main__":
    run()

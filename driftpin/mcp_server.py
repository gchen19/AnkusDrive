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
def verify_contract(handle: str, contract: dict) -> dict:
    """Build-time self-check of a component against its manifest slice (RFC §11.3):
    a builder calls this on its OWN part before save, so a contract violation is
    caught locally and cheaply instead of after a fan-in merge (build → merge →
    gate-fail → rebuild becomes build → self-check → fix). Never raises on a
    failing check (a failure is a passed=False row), so it is safe to call in a
    loop. Inspection only; mutates nothing.

    handle: the component's shaped object.
    contract: the component's slice — all keys optional, give at least one:
      envelope   {min:[x,y,z], max:[x,y,z]}   the part's LOCAL bbox must fit in it.
      interfaces {name: {origin:[x,y,z], z_axis?:[x,y,z], tol_mm?, angle_tol_deg?}}
                 each named frame must be PUBLISHED (publish_interface) and within
                 tolerance of the contracted origin (and axis, if z_axis given) —
                 catches "forgot to publish" / "published in the wrong place".
      features   [ {kind, ...} ]  per-feature self-checks:
                   {kind:"gear",   module_mm, teeth, internal?, tol_mm?}
                   {kind:"bore",   diameter_mm, tol_mm?}
                   {kind:"extent", axis:"x"|"y"|"z", length_mm, tol_mm?}
      intent     bool                          also run verify_intent.

    Returns {handle, ok, results:[{check, passed, detail}]} — ok True iff every
    check passed; check names are envelope / interface:<name> / feature:<name> /
    intent."""
    return _call("verify_contract", handle=handle, contract=contract)


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
def validate_manifest(manifest: str) -> dict:
    """Validate a manifest WITHOUT building it (the cheap front door, RFC §11.7).
    Structural + cross-reference checks a JSON shape can't enforce: every component
    has exactly one of file/manifest/library; a library carries a tool; every
    instance references a known component; every mate/check references a known
    instance; a present `schema` is the known version ("driftpin.manifest/1").

    manifest: path to the manifest JSON.

    Returns {ok, problems, schema, manifest_hash} — ok is True iff problems is
    empty; manifest_hash fingerprints the contract content (what the lockfile
    records so a stale contract is detectable). Run this before merge_assembly to
    reject a malformed contract before any geometry is built."""
    return _call("validate_manifest", manifest=manifest)


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
    """Export a drawing page to PDF, SVG, or DXF (format inferred from the path
    extension), headless. PDF/SVG are composed from the template, the per-view
    geometry, and any dimensions/annotations on the page; DXF uses FreeCAD's
    native page writer. Returns {path, size, format, views, dimensions}."""
    return _call("export_drawing", page=page, path=path)


@mcp.tool()
def add_dimension(
    page: str,
    view: str | None = None,
    auto: bool = False,
    edge: str | None = None,
    kind: str = "aligned",
    from_point: list | None = None,
    to_point: list | None = None,
    views: list | None = None,
    tolerance: dict | None = None,
) -> dict:
    """Add dimension(s) to a drawing page.

    Modes (pick one):
      * auto=True: overall horizontal + vertical extent dimensions for every
        part-view (or only those named in `views`, by name or projection code).
      * view + edge=<edge tag>: dimension the true length of a model edge,
        projected into that view. The printed value is the real measured
        length, not the foreshortened projection.
      * view + kind='diameter'|'radius' + edge=<circular edge tag>: a ⌀/R
        dimension of a hole or arc.
      * view + from_point/to_point ([x,y,z] model points): dimension between
        two 3D points.
    view: a view handle, object name, or projection code ('Front', 'Top', ...).
    kind: 'aligned' (default) | 'horizontal' | 'vertical' | 'diameter' | 'radius'.
    tolerance: optional, rendered next to the value (a machinist needs it to make
      the part to size): {"sym": 0.1} for ±0.1, {"plus": .., "minus": ..} for an
      asymmetric tolerance, or {"fit": "H7"} / {"fit": "H7/g6"} to look up ISO 286
      hole-side limits at the dimension's basic size.
    Returns {dimensions: [{handle, name, type, value}, ...]} — value is the
    true measured size of each dimension created.
    """
    params: dict = {"page": page}
    if tolerance is not None:
        params["tolerance"] = tolerance
    if auto:
        params["auto"] = True
        if views is not None:
            params["views"] = views
    else:
        if view is None:
            raise ValueError("add_dimension requires `view` unless auto=True")
        params["view"] = view
        params["kind"] = kind
        if edge is not None:
            params["edge"] = edge
        elif from_point is not None and to_point is not None:
            params["from_point"] = from_point
            params["to_point"] = to_point
        else:
            raise ValueError("manual add_dimension needs edge or from_point/to_point")
    return _call("add_dimension", **params)


@mcp.tool()
def add_annotation(page: str, text: str, x: float = 20.0, y: float = 20.0,
                   name: str = "Note") -> dict:
    """Add a free text annotation to a drawing page at page position (x, y) in
    mm (origin bottom-left, +Y up, matching TechDraw view placement).
    Returns {handle, name, text}."""
    return _call("add_annotation", page=page, text=text, x=x, y=y, name=name)


@mcp.tool()
def set_title_block(
    page: str,
    part: str | None = None,
    material: str | None = None,
    rev: str | None = None,
    drawn_by: str | None = None,
    date: str | None = None,
    project: str | None = None,
    units: str | None = None,
) -> dict:
    """Populate the drawing's title block. FreeCAD's default template is a bare
    sheet, so DriftPin composes its own block in the bottom-right corner on SVG/PDF
    export. Scale, sheet size, units, and part name are auto-derived from the page;
    the fields here override or add to them (a machinist needs material + scale +
    units to cut from the sheet). Calling this opts the page into rendering the
    block. Returns {handle, name, fields}."""
    params: dict = {"page": page}
    for k, v in (("part", part), ("material", material), ("rev", rev),
                 ("drawn_by", drawn_by), ("date", date),
                 ("project", project), ("units", units)):
        if v is not None:
            params[k] = v
    return _call("set_title_block", **params)


@mcp.tool()
def drawing_gate(page: str, process: str = "auto",
                 datums_declared: bool = False) -> dict:
    """Manufacturing-completeness gate for a drawing page: does the placed
    dimension set fully and non-redundantly reconstruct the part? A green render
    is not a manufacturable drawing — this validates the *drawing* itself, the way
    the geometry-realizes-declaration gate validates an assembly.

    Reads the real solid + the placed dimensions and accounts degrees of freedom,
    process-aware: a 'prismatic' (milled/plate) part must locate each hole by X/Y
    from a datum and size the block W×H×T; a 'turned' part is concentric, so a step
    needs only Ø + axial length. process='auto' infers it from the geometry.

    Returns {ok, violations, slots_total, slots_covered, process, features,
    dimensions, enumerated_features, datum_faces, section_recommended}.
    `section_recommended` ({recommended, reasons, feature_ids}) advises whether the
    part has internal geometry that needs a cross-section (see add_section_view).
    Each violation has a `code`
    (under = a feature size/location is missing; redundant = a DOF dimensioned more
    than once; conflict = dimensioned twice with disagreeing values; extra = a dim
    that pins nothing; no_datum = a location not taken from a datum) and a human
    `reason`. ok=True (empty violations) means the drawing is manufacturing-complete.

    Datum-origin discipline turns on automatically when the part has faces annotated
    role='datum' (annotate_face): a location dimension not measured from a datum face
    is then flagged no_datum. Set datums_declared=True to force the check on even
    without annotated datums."""
    return _call("drawing_gate", page=page, process=process,
                 datums_declared=datums_declared)


@mcp.tool()
def fit_page(page: str, margin: float = 8.0) -> dict:
    """Auto-fit a drawing to its sheet: recentre the views so the part AND its
    placed dimensions sit inside the printable border (clear of the title block).
    The projection group's Automatic scale already sizes the part; its dimensions
    extend a fixed margin beyond it which can run off an edge — call this after
    placing dimensions to slide everything inside. `margin` mm is the border inset.
    Returns {scale, fits, envelope, border}; fits=False means the part + dims are too
    large even when centred (use a larger sheet)."""
    return _call("fit_page", page=page, margin=margin)


@mcp.tool()
def drawing_legibility(page: str, min_gap: float = 0.5) -> dict:
    """Legibility gate for a drawing page: on the ACTUAL placed graphics, flag the
    ways the layout becomes unreadable — overlapping dimension labels, a dimension
    line crossing a view it does not reference, or anything past the sheet border.

    min_gap (mm) is the breathing room required between two labels. Returns {ok,
    violations, labels, segments, views}; each violation has a `code`
    (overlap/crosses_view/out_of_border) and a human `reason`. ok=True means the
    placed dimensions read cleanly on the sheet."""
    return _call("drawing_legibility", page=page, min_gap=min_gap)


@mcp.tool()
def add_thumbnail(page: str, name: str = "IsoThumb") -> dict:
    """Place a small isometric pictorial of the part in the top-right corner of the
    sheet — the "glance" reference a machinist uses to grok the 3-D shape before
    reading the orthographic views — IF it fits there without crowding the existing
    views and dimensions.

    It is a real TechDraw isometric projection rendered through the same path as the
    other views (a vector line drawing, not a raster), scaled to fit a reserved
    top-right box and pinned to that corner (fit_page leaves it put, and it is never
    dimensioned or counted by the manufacturability gate). Best-effort: when the
    top-right corner is already occupied it returns {placed: False, reason} rather
    than overlapping content. Call it AFTER placing the views and dimensions (and
    after fit_page) so "fits" is judged against the final layout.

    Returns {placed, box, scale?, view?, reason?}."""
    return _call("add_thumbnail", page=page, name=name)


@mcp.tool()
def add_section_view(page: str, auto: bool = True, process: str = "auto",
                     name: str = "Section", symbol: str = "A") -> dict:
    """Add a cross-section view when the part has internal features the outline /
    hidden-line views convey ambiguously — a counterbore, a blind hole/bore, or a
    pocket. The need is judged automatically from the real solid (the same feature
    enumeration the manufacturability gate uses); the cut runs lengthwise through
    such a feature so its bore profile and depth read directly, and the view is
    placed in clear space beside the existing views.

    auto (default True): add the section ONLY if the part actually has hidden
        internal geometry; otherwise return {added: False, recommended: False}. Set
        auto=False to force a section regardless.
    process: 'auto' (default) | 'prismatic' | 'turned' — how features are enumerated.

    Returns {added, recommended, reasons, feature_ids, view?, normal?, origin?}.
    (drawing_gate also reports `section_recommended` so you can decide in advance.)"""
    return _call("add_section_view", page=page, auto=auto, process=process,
                 name=name, symbol=symbol)


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
def solve_capabilities() -> dict:
    """Report which P2 external solvers (CFD/MBD/topology/transient-thermal/optics)
    are usable right now — so you can pick a working solver for a *_submit family
    instead of discovering availability by trial and error. The solver twin of
    render_capabilities.

    Takes no arguments. Resolves each solver side-effect-free: a binary by
    DRIFTPIN_<SOLVER>_PATH env -> PATH -> per-OS install dirs; a pip-wheel solver by
    importability. It executes nothing and installs nothing.

    Returns {platform, available (sorted ready solver names), solvers: {name:
    {available, kind ('binary'|'wheel'), family, extra, and either path/module (when
    available) or install_hint}}, families: {family: {solvers, available,
    any_available}}, extras: {extra: [solver names]} for `pip install
    driftpin[<extra>]`}."""
    return _call("solve_capabilities")


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
def contact_setup(
    analysis: str,
    face_pairs: list,
    friction: float = 0.0,
    slope: str | float | None = None,
    nonlinear: bool = True,
    name: str = "Contact",
) -> dict:
    """Set up surface-to-surface contact between face pairs for a CalculiX solve and
    flip the solver to nonlinear — no new solver (promotes the CCX contact/nonlinear
    flags the FEM path already exposes). `face_pairs` is a list of
    {a:{handle, tag|face}, b:{handle, tag|face}} (master, slave) pairs; `friction` is
    the Coulomb coefficient (0 = frictionless); `slope` optionally sets the penalty
    contact stiffness; `nonlinear` (default True) sets the solver's
    GeometricalNonlinearity.

    Run fem_run + fem_results after. Gate RELATIVE to a bonded reference on the same
    mesh: a bonded model is stiffer (less peak displacement) than frictional contact.
    Returns {contacts:[handles], n_pairs, friction, nonlinear}."""
    params = {"analysis": analysis, "face_pairs": face_pairs, "friction": friction,
              "nonlinear": nonlinear, "name": name}
    if slope is not None:
        params["slope"] = slope
    return _call("contact_setup", **params)


@mcp.tool()
def fem_set_nonlinear_material(
    analysis: str,
    base_material: str,
    yield_mpa: float | None = None,
    yield_points: list | None = None,
    tangent_modulus_mpa: float | None = None,
    max_plastic_strain: float = 0.2,
    hardening: str = "isotropic",
    geometric_nonlinearity: bool = False,
    ramp_increments: int = 10,
    name: str = "NonlinearMaterial",
) -> dict:
    """Attach an elastoplastic (`*PLASTIC`) hardening curve to a linear FEM material
    and switch the CalculiX solve to nonlinear — the material-nonlinearity half of
    the nonlinear FEM path (`contact_setup` is the geometric/contact half). No new
    solver: this promotes the CCX `MaterialNonlinearity` / `GeometricalNonlinearity`
    flags the FEM path already exposes. `base_material` is the handle from
    `fem_set_material` (its YoungsModulus/PoissonRatio stay the elastic branch).

    Give the post-yield curve either as `yield_points` ([[stress_MPa,
    plastic_strain], ...], first point at plastic_strain 0 = initial yield) or from
    `yield_mpa` (+ optional `tangent_modulus_mpa` linear-hardening slope and
    `max_plastic_strain`). With no tangent modulus the curve is
    elastic–perfectly-plastic and caps the stress at σ_y exactly. `hardening`:
    'isotropic' (monotonic) or 'kinematic' (cyclic/Bauschinger). Set
    `geometric_nonlinearity=true` to combine plasticity with large deflection
    (*NLGEOM). `ramp_increments` sub-divides the load step so ccx's plastic
    return-mapping converges. Run `fem_run` + `fem_results` after; gate against
    `plastic_collapse` (perfectly-plastic stress saturates at σ_y, collapse at M_p).

    Returns {handle, name, hardening, yield_points, n_points,
    solver_material_nonlinear, solver_geometric_nonlinear, ramp_increments}."""
    params = {"analysis": analysis, "base_material": base_material,
              "max_plastic_strain": max_plastic_strain, "hardening": hardening,
              "geometric_nonlinearity": geometric_nonlinearity,
              "ramp_increments": ramp_increments, "name": name}
    for k, v in (("yield_mpa", yield_mpa), ("yield_points", yield_points),
                 ("tangent_modulus_mpa", tangent_modulus_mpa)):
        if v is not None:
            params[k] = v
    return _call("fem_set_nonlinear_material", **params)


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
    element_order: str | None = None,
) -> dict:
    """Create a Gmsh mesh of `body`, attached to `analysis`.

    char_length: max characteristic element length in mm. 0 = let Gmsh pick.
    element_order: '1st' or '2nd' (quadratic). Use '2nd' for bending/modal accuracy —
    linear tets (C3D4) shear-lock and overstiffen thin sections (a cantilever's first
    natural frequency lands ~50% high with only 1-2 elements through the thickness;
    2nd-order tets bring it within ~1% of beam theory). Default lets Gmsh choose.
    Returns {handle, name, nodes, tets}.
    """
    params = {"analysis": analysis, "body": body, "char_length": char_length,
              "name": name}
    if element_order is not None:
        params["element_order"] = element_order
    return _call("fem_mesh", **params)


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
def h_estimate(
    geometry: str,
    characteristic_mm: float,
    t_surface_c: float,
    t_ambient_c: float = 25.0,
    velocity_m_s: float = 0.0,
    emissivity: float = 0.0,
    fluid: str = "air",
    k_w_mk: float | None = None,
    nu_m2_s: float | None = None,
    pr: float | None = None,
    beta_per_k: float | None = None,
) -> dict:
    """Screening convection coefficient h (NO solver) — the honest h_conv to feed
    thermal_lumped / thermal_transient_1d / a convection BC, instead of a guess.
    `geometry` picks the correlation: natural (velocity_m_s = 0)
    'vertical_plate' | 'horizontal_cylinder' (Churchill–Chu); forced (velocity_m_s > 0)
    'flat_plate' (averaged laminar/mixed Nu) | 'cylinder_crossflow' (Hilpert).
    `characteristic_mm` is the plate height/length or cylinder diameter. Film-temp
    air properties built in; another `fluid` needs explicit k_w_mk + nu_m2_s + pr
    (+ beta_per_k for natural). emissivity > 0 adds the linearized radiation screen
    into h_total_w_m2k.

    This is a focusing estimate, not a gate: fidelity='correlation' with band_pct
    the literature scatter (±15–20 %). Escalate to the conjugate solve
    `cht_channel_submit` (or a meshed convection BC via thermal_transient_submit)
    when the thermal margin is within ~2× band_pct. Returns {geometry, mode,
    correlation, h_conv_w_m2k, h_rad_w_m2k, h_total_w_m2k, nusselt, reynolds,
    rayleigh, prandtl, film_temp_c, fidelity, band_pct, valid_range_ok, warnings,
    escalate_to}."""
    params = {"geometry": geometry, "characteristic_mm": characteristic_mm,
              "t_surface_c": t_surface_c, "t_ambient_c": t_ambient_c,
              "velocity_m_s": velocity_m_s, "emissivity": emissivity,
              "fluid": fluid}
    for k, v in (("k_w_mk", k_w_mk), ("nu_m2_s", nu_m2_s), ("pr", pr),
                 ("beta_per_k", beta_per_k)):
        if v is not None:
            params[k] = v
    return _call("h_estimate", **params)


@mcp.tool()
def acoustic_screen(
    kind: str,
    lx_mm: float | None = None,
    ly_mm: float | None = None,
    lz_mm: float | None = None,
    n_modes: int = 10,
    neck_area_mm2: float | None = None,
    neck_length_mm: float | None = None,
    cavity_volume_mm3: float | None = None,
    frequency_hz: float | None = None,
    surface_density_kg_m2: float | None = None,
    duct_width_mm: float | None = None,
    duct_diameter_mm: float | None = None,
    t_ambient_c: float = 20.0,
    c_m_s: float | None = None,
) -> dict:
    """Closed-form acoustics screen (NO solver). `kind`: 'cavity_modes' (lx/ly/lz_mm
    -> the lowest n_modes rigid-cavity eigenfrequencies f=(c/2)·√(Σ(n/L)²) with
    [nx,ny,nz] indices — exact, and the future oracle for the planned Elmer
    HelmholtzSolve FEM) | 'helmholtz' (neck_area_mm2 + neck_length_mm +
    cavity_volume_mm3 -> resonance with flanged end correction — ±10 %) |
    'mass_law' (frequency_hz + surface_density_kg_m2 -> limp-wall TL =
    20·log₁₀(f·m″)−47 dB — ±3 dB) | 'duct_cutoff' (duct_width_mm or
    duct_diameter_mm -> first cross-mode; plane waves only below — exact). Sound
    speed from air at t_ambient_c unless c_m_s given. Fidelity is labeled per kind;
    escalate to the Elmer `acoustic_fem_submit` solve (Tier B1) when the margin is
    within ~2× the band.

    Returns {kind, c_m_s, fidelity, band_pct, band_db, valid_range_ok, warnings,
    escalate_to} plus per kind: {modes:[{f_hz,n}], f_fundamental_hz} |
    {f_resonance_hz, neck_radius_mm, l_eff_mm} | {tl_db, fm_product} |
    {f_cutoff_hz, geometry}."""
    params = {"kind": kind, "n_modes": n_modes, "t_ambient_c": t_ambient_c}
    for k, v in (("lx_mm", lx_mm), ("ly_mm", ly_mm), ("lz_mm", lz_mm),
                 ("neck_area_mm2", neck_area_mm2), ("neck_length_mm", neck_length_mm),
                 ("cavity_volume_mm3", cavity_volume_mm3),
                 ("frequency_hz", frequency_hz),
                 ("surface_density_kg_m2", surface_density_kg_m2),
                 ("duct_width_mm", duct_width_mm),
                 ("duct_diameter_mm", duct_diameter_mm), ("c_m_s", c_m_s)):
        if v is not None:
            params[k] = v
    return _call("acoustic_screen", **params)


@mcp.tool()
def plate_check(
    shape: str,
    thickness_mm: float,
    pressure_kpa: float,
    a_mm: float | None = None,
    b_mm: float | None = None,
    diameter_mm: float | None = None,
    support: str = "simply_supported",
    youngs_gpa: float | None = None,
    material: str | None = None,
    poisson: float = 0.3,
) -> dict:
    """Handbook bending of a uniformly loaded flat plate (NO solver) — the
    "do I need FEM at all?" screen. `shape`: 'rectangular' (a_mm × b_mm, short side
    drives; Roark/Timoshenko ν=0.3 coefficients σ=β·q·b²/t², δ=α·q·b⁴/(E·t³),
    interpolated in a/b) | 'circular' (diameter_mm; exact closed forms).
    `support`: 'simply_supported' | 'clamped' (all edges). E from youngs_gpa or a
    Materials-DB `material` (which also supplies yield for yield_safety_factor).
    Exact within thin-plate theory, and the limits are returned as flags
    (thin_plate_ok: span/t ≥ 10; small_deflection_ok: δ ≤ t/2) — a tripped flag
    means escalate to the CCX fem_* pipeline (escalate_to='fem_run').

    Returns {shape, support, aspect_ratio, beta, alpha, sigma_max_mpa,
    deflection_max_mm, yield_safety_factor, thin_plate_ok, small_deflection_ok,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"shape": shape, "thickness_mm": thickness_mm,
              "pressure_kpa": pressure_kpa, "support": support, "poisson": poisson}
    for k, v in (("a_mm", a_mm), ("b_mm", b_mm), ("diameter_mm", diameter_mm),
                 ("youngs_gpa", youngs_gpa), ("material", material)):
        if v is not None:
            params[k] = v
    return _call("plate_check", **params)


@mcp.tool()
def beam_buckling(
    length_mm: float,
    end_condition: str = "pinned_pinned",
    width_mm: float | None = None,
    height_mm: float | None = None,
    diameter_mm: float | None = None,
    area_mm2: float | None = None,
    i_min_mm4: float | None = None,
    youngs_gpa: float | None = None,
    yield_mpa: float | None = None,
    material: str | None = None,
    load_n: float | None = None,
) -> dict:
    """Exact column-buckling screen (Euler + Johnson, NO solver) — the closed-form
    twin the CalculiX `fem_buckling` eigen-solve is gated against (as `beam_modal`
    is to `fem_modal`). Section: width_mm+height_mm (solid rectangle, weak axis
    automatic), diameter_mm (round), or explicit area_mm2+i_min_mm4. E/σ_y from
    youngs_gpa/yield_mpa or a Materials-DB `material`. `end_condition`:
    'pinned_pinned' | 'fixed_free' | 'fixed_pinned' | 'fixed_fixed' (theoretical K).
    Euler σ_cr=π²E/λ² above the transition slenderness √(2π²E/σ_y), Johnson
    parabola below (both exactly σ_y/2 at it). With load_n the safety factor
    P_cr/P is returned. Escalate to `fem_buckling` for non-prismatic /
    eccentric / built-up cases.

    Returns {end_condition, k_factor, slenderness, transition_slenderness,
    governing, sigma_cr_mpa, p_cr_n, area_mm2, i_min_mm4, radius_gyration_mm,
    safety_factor, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"length_mm": length_mm, "end_condition": end_condition}
    for k, v in (("width_mm", width_mm), ("height_mm", height_mm),
                 ("diameter_mm", diameter_mm), ("area_mm2", area_mm2),
                 ("i_min_mm4", i_min_mm4), ("youngs_gpa", youngs_gpa),
                 ("yield_mpa", yield_mpa), ("material", material),
                 ("load_n", load_n)):
        if v is not None:
            params[k] = v
    return _call("beam_buckling", **params)


@mcp.tool()
def plastic_collapse(
    length_mm: float,
    width_mm: float,
    height_mm: float,
    yield_mpa: float | None = None,
    material: str | None = None,
    load_n: float | None = None,
    support: str = "cantilever",
) -> dict:
    """Exact plastic-hinge collapse of a solid rectangular beam (NO solver) — the
    closed-form twin the perfectly-plastic CalculiX solve
    (`fem_set_nonlinear_material`) is gated against. The beam bends about the
    `width_mm` axis (depth = `height_mm`). σ_y from `yield_mpa` or a Materials-DB
    `material`. Elastic modulus S = b·h²/6, plastic modulus Z = b·h²/4, shape
    factor Z/S = 1.5; yield moment M_y = σ_y·S, fully-plastic moment M_p = σ_y·Z.
    `support` maps the collapse moment to a point load: 'cantilever' (M = P·L) or
    'simply_supported' (central, M = P·L/4). With `load_n` the applied moment and
    its margins to M_y / M_p (and the regime: elastic / partially_plastic /
    collapsed) are returned. A perfectly-plastic FEM solve caps the surface stress
    at σ_y and loses equilibrium at M_p; linear theory climbs past both — that
    contrast is the gate. Escalate to `fem_set_nonlinear_material` for
    non-rectangular sections or partial-plasticity fields.

    Returns {support, S_elastic_mm3, Z_plastic_mm3, shape_factor, yield_mpa,
    yield_moment_nmm, plastic_moment_nmm, yield_load_n, collapse_load_n,
    applied_moment_nmm, margin_to_yield, margin_to_collapse, regime, fidelity,
    band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"length_mm": length_mm, "width_mm": width_mm, "height_mm": height_mm,
              "support": support}
    for k, v in (("yield_mpa", yield_mpa), ("material", material),
                 ("load_n", load_n)):
        if v is not None:
            params[k] = v
    return _call("plastic_collapse", **params)


@mcp.tool()
def elastica_deflection(
    load_n: float,
    length_mm: float,
    youngs_gpa: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
    i_mm4: float | None = None,
    material: str | None = None,
) -> dict:
    """Exact large-deflection cantilever tip — Bisshopp–Drucker elastica (NO
    solver) — the closed-form twin the *NLGEOM CalculiX solve is gated against.
    Section: width_mm+height_mm (solid rectangle, I = b·h³/12, load transverse to
    height_mm) or explicit i_mm4. E from youngs_gpa or a Materials-DB `material`.
    Load parameter α = P·L²/(E·I); the tip slope solves the elliptic-integral
    elastica. Linear theory δ/L = α/3 over-predicts the transverse tip and ignores
    the axial draw-in — the elastica captures both, and `nonlinear_over_linear` is
    the divergence the solve must reproduce. Valid for tip slope < ~80° (α ≲ 3.5);
    beyond that escalate to a follower-load `fem_set_nonlinear_material` solve.

    Returns {alpha, tip_slope_deg, tip_disp_mm (transverse), tip_x_mm (axial
    projection), axial_drawin_mm, linear_tip_mm, nonlinear_over_linear, youngs_mpa,
    I_mm4, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"load_n": load_n, "length_mm": length_mm}
    for k, v in (("youngs_gpa", youngs_gpa), ("width_mm", width_mm),
                 ("height_mm", height_mm), ("i_mm4", i_mm4), ("material", material)):
        if v is not None:
            params[k] = v
    return _call("elastica_deflection", **params)


@mcp.tool()
def hertz_contact(
    load_n: float,
    radius_mm: float,
    youngs1_gpa: float | None = None,
    poisson1: float | None = None,
    material1: str | None = None,
    radius2_mm: float | None = None,
    youngs2_gpa: float | None = None,
    poisson2: float | None = None,
    material2: str | None = None,
) -> dict:
    """Exact Hertzian point-contact peak pressure (NO solver) — the screening twin
    of a frictional *CONTACT PAIR solve. Sphere of `radius_mm` on a flat (default)
    or on a second sphere `radius2_mm` (negative for a conforming socket). Each
    body's elastics from youngs#_gpa+poisson# or a Materials-DB `material#`; body 2
    defaults to body 1. Reduced modulus 1/E* = (1−ν₁²)/E₁ + (1−ν₂²)/E₂, effective
    radius 1/R = 1/R₁ + 1/R₂; contact radius a = (3FR/4E*)^(1/3), peak pressure
    p₀ = 3F/(2πa²) = 1.5× mean, approach δ = a²/R. Half-space theory: valid while
    a ≪ R and p₀ below first sub-surface yield (~1.6·σ_y) — past that escalate to
    the nonlinear `fem_set_nonlinear_material` contact path.

    Returns {e_star_mpa, effective_radius_mm, contact_radius_mm, peak_pressure_mpa,
    mean_pressure_mpa, approach_mm, a_over_R, fidelity, band_pct, valid_range_ok,
    warnings, escalate_to}."""
    params = {"load_n": load_n, "radius_mm": radius_mm}
    for k, v in (("youngs1_gpa", youngs1_gpa), ("poisson1", poisson1),
                 ("material1", material1), ("radius2_mm", radius2_mm),
                 ("youngs2_gpa", youngs2_gpa), ("poisson2", poisson2),
                 ("material2", material2)):
        if v is not None:
            params[k] = v
    return _call("hertz_contact", **params)


@mcp.tool()
def waveguide_cutoff(
    a_mm: float,
    b_mm: float | None = None,
    mode: str = "TE10",
    eps_r: float = 1.0,
    freq_ghz: float | None = None,
) -> dict:
    """Exact rectangular-waveguide cutoff frequency (NO solver) — the closed-form
    twin the openEMS FDTD full-wave solve (`em_fullwave_submit`) is gated against.
    Broad wall `a_mm`, narrow wall `b_mm` (default a/2, WR convention). `mode` is
    'TE<m><n>'/'TM<m><n>'. f_c(m,n) = (c/2√εᵣ)·√((m/a)²+(n/b)²); dominant TE10
    reduces to the EXACT f_c = c/(2a√εᵣ). Below f_c the guide is evanescent
    (axial β imaginary, nothing transmits), above it propagates with guided
    wavelength λ_g = 2π/β. With a probe `freq_ghz` the regime (propagating /
    evanescent), k, β and λ_g are returned. An FDTD drive straddling f_c must
    collapse its transmission below the analytic cutoff and rise above it.

    Returns {mode, m, n, a_mm, b_mm, eps_r, cutoff_hz, cutoff_ghz, kc_per_m,
    next_mode_cutoff_ghz, single_mode_band_ghz, probe_freq_ghz, regime, k_per_m,
    beta_per_m, guided_wavelength_mm, fidelity, band_pct, valid_range_ok,
    warnings, escalate_to}."""
    params = {"a_mm": a_mm, "mode": mode, "eps_r": eps_r}
    for k, v in (("b_mm", b_mm), ("freq_ghz", freq_ghz)):
        if v is not None:
            params[k] = v
    return _call("waveguide_cutoff", **params)


@mcp.tool()
def dipole_resonance(
    length_mm: float | None = None,
    freq_ghz: float | None = None,
    shortening: float = 0.48,
) -> dict:
    """Thin centre-fed half-wave dipole first resonance (NO solver, banded) — the
    closed-form twin the openEMS FDTD S11 antenna sweep (`em_fullwave_submit`) is
    gated against. Give EXACTLY ONE of `length_mm` (→ resonant frequency) or
    `freq_ghz` (→ resonant length). End-effect shortening k = `shortening` makes
    the resonant length a little under λ/2: L = k·λ, f_r = k·c/L (k≈0.48 textbook;
    ≈0.475 typical wire). Because k tracks the length/diameter ratio this is a
    ±band correlation (fidelity='banded', ~±3% over k∈[0.46,0.49]); an FDTD S11
    sweep must put its first resonance inside [freq_lo, freq_hi] (or [length_lo,
    length_hi]).

    Returns {given, shortening, half_wavelength_mm, resonant_length_mm,
    resonant_freq_ghz, freq_lo_ghz, freq_hi_ghz, length_lo_mm, length_hi_mm,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"shortening": shortening}
    for k, v in (("length_mm", length_mm), ("freq_ghz", freq_ghz)):
        if v is not None:
            params[k] = v
    return _call("dipole_resonance", **params)


@mcp.tool()
def em_fullwave_submit(
    problem: str = "waveguide_sweep",
    a_mm: float = 22.86,
    b_mm: float | None = None,
    length_mm: float | None = None,
    f_start_ghz: float | None = None,
    f_stop_ghz: float | None = None,
    n_freq: int | None = None,
    nrts: int | None = None,
    cells_per_wl: float | None = None,
    eps_r: float = 1.0,
    gap_mm: float | None = None,
    radius_mm: float | None = None,
    timeout: int = 600,
) -> dict:
    """Full-wave FDTD EM solve on openEMS, asynchronous (OFF the MCP channel) — the
    real-field twin of the analytic `waveguide_cutoff` / `dipole_resonance`
    oracles. openEMS is GPL-3.0 and is run ONLY out-of-process via
    driftpin/em_fullwave_gpl_runner.py; degrades to {ok:false, reason, install}
    when no openEMS venv resolves.

    `problem`='waveguide_sweep' (default): hollow rectangular guide, broad wall
    `a_mm`/narrow wall `b_mm` (default a/2), length `length_mm` (default 60), TE10
    port at each end. Sweep `f_start_ghz`..`f_stop_ghz` (default 4..10 GHz —
    straddling the WR-90 cutoff 6.56 GHz) in `n_freq` points, `nrts` max timesteps,
    `cells_per_wl` mesh density, `eps_r` fill. The result's `fc_crossing_ghz`
    (half-power transmission) vs the analytic c/(2a) (`fc_ratio`≈1, evanescent_mean
    ≈0, propagating_mean≈1) IS the gate. `problem`='dipole_s11': centre-fed thin
    dipole (`length_mm`, `gap_mm`, `radius_mm`), sweep S11, report first resonance.

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result
    for {ok, fc_analytic_ghz, freq_ghz[], s21_db[], transmission_norm[],
    fc_crossing_ghz, fc_ratio, evanescent_mean, propagating_mean, n_cells, wall_s}
    (waveguide) or {freq_ghz[], s11_db[], resonance_ghz} (dipole)."""
    params = {"problem": problem, "a_mm": a_mm, "eps_r": eps_r, "timeout": timeout}
    for k, v in (("b_mm", b_mm), ("length_mm", length_mm),
                 ("f_start_ghz", f_start_ghz), ("f_stop_ghz", f_stop_ghz),
                 ("n_freq", n_freq), ("nrts", nrts),
                 ("cells_per_wl", cells_per_wl), ("gap_mm", gap_mm),
                 ("radius_mm", radius_mm)):
        if v is not None:
            params[k] = v
    return _call("em_fullwave_submit", **params)


@mcp.tool()
def molding_screen(
    wall_thickness_mm: float,
    material: str | None = None,
    flow_length_mm: float | None = None,
    t_melt_c: float | None = None,
    t_mold_c: float | None = None,
    t_eject_c: float | None = None,
    alpha_mm2_s: float | None = None,
    flow_ratio_limit: float | None = None,
) -> dict:
    """Injection-molding screen (NO solver): one-term cooling time (exact given α,
    t_cool = s²/(π²α)·ln(8·(T_melt−T_mold)/(π²·(T_eject−T_mold))) — the t ∝ s²
    design lever) + the spiral-flow fill check (fill_ok when flow_length ≤
    (L/t-limit)·wall — chart correlation, ±30 %). `material` picks per-polymer
    defaults (ABS | PP | PC | PA66 | POM | HDPE | PS), each individually
    overridable; with all temps + alpha_mm2_s explicit no material is needed.
    A cooling-only call returns fidelity='exact'; adding flow_length_mm makes the
    headline answer fidelity='correlation', band_pct=30 (cooling stays exact).
    No mold-filling solver is shipped (escalate_to=None).

    Returns {material, wall_thickness_mm, t_melt_c, t_mold_c, t_eject_c,
    alpha_mm2_s, cooling_time_s, flow_length_mm, flow_ratio, flow_ratio_limit,
    fill_ok, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"wall_thickness_mm": wall_thickness_mm}
    for k, v in (("material", material), ("flow_length_mm", flow_length_mm),
                 ("t_melt_c", t_melt_c), ("t_mold_c", t_mold_c),
                 ("t_eject_c", t_eject_c), ("alpha_mm2_s", alpha_mm2_s),
                 ("flow_ratio_limit", flow_ratio_limit)):
        if v is not None:
            params[k] = v
    return _call("molding_screen", **params)


@mcp.tool()
def drop_impact(
    drop_height_mm: float,
    crush_distance_mm: float | None = None,
    deceleration_limit_g: float | None = None,
    pulse: str = "linear_spring",
    mass_g: float | None = None,
) -> dict:
    """Drop/impact screen by exact energy balance (NO solver). Give
    crush_distance_mm (available cushion/crumple stroke) -> deceleration, OR
    deceleration_limit_g (fragility spec) -> required stroke — exactly one.
    G_avg = h/d exactly (mass cancels); g_peak = pulse_factor·G_avg with `pulse`
    bounding the shape: 'constant' (ideal crush, 1×) | 'linear_spring' (elastic,
    2×) | 'half_sine' (π/2×). v = √(2gh). mass_g only adds peak_force_n and
    energy_j. fidelity='exact'; no explicit impact-dynamics solve is shipped
    (escalate_to=None — horizon scope).

    Returns {drop_height_mm, impact_velocity_m_s, pulse, pulse_factor,
    crush_distance_mm, g_avg, g_peak, pulse_duration_ms, deceleration_limit_g,
    required_crush_mm, energy_j, peak_force_n, fidelity, band_pct,
    valid_range_ok, warnings, escalate_to}."""
    params = {"drop_height_mm": drop_height_mm, "pulse": pulse}
    for k, v in (("crush_distance_mm", crush_distance_mm),
                 ("deceleration_limit_g", deceleration_limit_g),
                 ("mass_g", mass_g)):
        if v is not None:
            params[k] = v
    return _call("drop_impact", **params)


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
    value/quantity-string ('900 J/kg/K') or read from `material`. Get h_conv from
    the `h_estimate` correlation screen rather than guessing. With duration_s
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


@mcp.tool()
def thermal_transient_1d(
    half_thickness_mm: float,
    h_conv: float,
    duration_s: float,
    k: str | float | None = None,
    rho: str | float | None = None,
    cp: str | float | None = None,
    alpha_m2_s: float | None = None,
    material: str | None = None,
    t_initial_c: float = 100.0,
    t_ambient_c: float = 25.0,
) -> dict:
    """Analytic 1-D plane-wall transient conduction (one-term Heisler series), valid
    for Fourier ≳ 0.2 — the closed-form transient the Elmer thermal_transient solve is
    gated against, and the *distributed* (spatial-gradient) answer the lumped screen
    only approximates. A wall of half-thickness L cools/heats toward ambient by surface
    convection: Bi = h·L/k, Fo = α·t/L², α = k/(ρ·cₚ). Pass `alpha_m2_s`, or k+rho+cp,
    or a `material` (Materials DB: thermal_conductivity/Density/specific_heat); get
    h_conv from the `h_estimate` correlation screen rather than guessing.

    As Bi→0 the body is isothermal and this collapses to the lumped exponential
    exp(−t/τ) (cross-checked via t_center_lumped_c / lumped_agrees). Returns {biot,
    fourier, eigenvalue_1, c1, t_center_c, t_surface_c, t_center_lumped_c,
    time_constant_s, one_term_valid, lumped_agrees}."""
    params = {"half_thickness_mm": half_thickness_mm, "h_conv": h_conv,
              "duration_s": duration_s, "t_initial_c": t_initial_c,
              "t_ambient_c": t_ambient_c}
    for key, v in (("k", k), ("rho", rho), ("cp", cp), ("alpha_m2_s", alpha_m2_s),
                   ("material", material)):
        if v is not None:
            params[key] = v
    return _call("thermal_transient_1d", **params)


@mcp.tool()
def thermal_transient_submit(
    case_dir: str | None = None,
    sif: str = "case.sif",
    half_thickness_mm: float | None = None,
    body: str | None = None,
    convection_faces: list | None = None,
    char_length_mm: float | None = None,
    h_conv: float | None = None,
    duration_s: float | None = None,
    k: float | None = None,
    rho: float | None = None,
    cp: float | None = None,
    material: str | None = None,
    t_initial_c: float = 100.0,
    t_ambient_c: float = 25.0,
    n_elements: int = 40,
    n_steps: int = 120,
    element_order: str | None = None,
) -> dict:
    """Transient thermal FEM via Elmer, asynchronous. Requires ElmerSolver (apt
    `elmerfem-csc` / conda); when absent this returns {ok:false, reason, install}
    rather than raising. Three modes:

    - **Build the analytic-slab case** (no solver case prep needed): pass the
      plane-wall transient — `half_thickness_mm`, `h_conv` (W/m²K), `duration_s`, and
      either `k`+`rho`+`cp` (SI) or a `material` name, with optional `t_initial_c` /
      `t_ambient_c` and mesh/step counts `n_elements` / `n_steps`. The handler writes
      the 1-D conduction case (symmetry at the centre, convection at the surface), runs
      ElmerSolver, and returns the centre/surface temperatures — the same plane-wall
      BVP `thermal_transient_1d` solves analytically, so the two are directly
      comparable (the kickoff's relative gate).
    - **Solve a real FreeCAD solid — the geometry bridge**: pass a `body` handle plus
      `convection_faces` (1-based indices into the solid's faces; those faces get the
      `h_conv`/`t_ambient_c` convective BC, every other face is adiabatic), the
      physics (`h_conv`, `duration_s`, `k`+`rho`+`cp` or `material`), and an optional
      `char_length_mm` Gmsh element size and `element_order` ('1st'|'2nd'). The solid
      is Gmsh-meshed and solved as a true 3-D body (ElmerGrid + ElmerSolver); the
      result's {t_max_c, t_min_c} are the interior/convective-surface temperatures (for
      a slab-like body, directly gateable against thermal_transient_1d). Prefer
      `element_order='2nd'` for a sharp transient — quadratic tets resolve the wall
      gradient accurately even on a coarse mesh.
    - **Run a prepared `case_dir`** containing its own `.sif` + mesh.

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result for
    {ok, returncode, solver, case_dir, stdout_tail} plus, for the slab case,
    {t_center_c, t_surface_c, n_steps_written}, for a body {t_max_c, t_min_c, nodes,
    tets} (or {scalars_final} for a prepared case)."""
    params = {"sif": sif, "t_initial_c": t_initial_c, "t_ambient_c": t_ambient_c,
              "n_elements": n_elements, "n_steps": n_steps}
    for key, v in (("case_dir", case_dir), ("half_thickness_mm", half_thickness_mm),
                   ("body", body), ("convection_faces", convection_faces),
                   ("char_length_mm", char_length_mm),
                   ("element_order", element_order),
                   ("h_conv", h_conv), ("duration_s", duration_s),
                   ("k", k), ("rho", rho), ("cp", cp), ("material", material)):
        if v is not None:
            params[key] = v
    return _call("thermal_transient_submit", **params)


@mcp.tool()
def thermal_radiation_submit(
    t1_c: float | None = None,
    t2_c: float | None = None,
    emissivity_1: float = 0.8,
    emissivity_2: float = 0.8,
    case_dir: str | None = None,
    sif: str = "case.sif",
    width_m: float = 1.0,
    gap_m: float = 0.01,
    plate_thickness_m: float = 0.01,
    n_x: int = 80,
    k_plate: float = 400.0,
) -> dict:
    """Diffuse-gray radiation FEM via Elmer, asynchronous — the radiation sibling of
    thermal_transient_submit. Requires ElmerSolver + the ViewFactors binary (apt
    `elmerfem-csc` / conda); when absent this returns {ok:false, reason, install}
    rather than raising. Two modes:

    - **Build the two-plate enclosure case** (no case prep): pass `t1_c`, `t2_c` (°C)
      and the two surface emissivities `emissivity_1`/`emissivity_2` (default 0.8). The
      handler writes a 2-D pair of parallel plates radiating across an unmeshed vacuum
      gap, runs ViewFactors then ElmerSolver, and extracts the net radiative exchange —
      directly gated against the exact two infinite parallel plates oracle
      q = σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1) (`oracle_ratio` ≈ 1). Mesh/geometry knobs: `width_m`,
      `gap_m`, `plate_thickness_m`, `n_x`, `k_plate`.
    - **Run a prepared `case_dir`** containing its `.sif` + mesh (ViewFactors is run
      first when no factor file is present).

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result for
    {ok, returncode, solver, case_dir, stdout_tail} plus, for the plate case,
    {flux_w_m2, q_net_w, two_plate_flux_w_m2, oracle_ratio, t1_c, t2_c, emissivity_1,
    emissivity_2} (or {scalars_final} for a prepared case)."""
    params = {"emissivity_1": emissivity_1, "emissivity_2": emissivity_2, "sif": sif,
              "width_m": width_m, "gap_m": gap_m,
              "plate_thickness_m": plate_thickness_m, "n_x": n_x, "k_plate": k_plate}
    for key, v in (("t1_c", t1_c), ("t2_c", t2_c), ("case_dir", case_dir)):
        if v is not None:
            params[key] = v
    return _call("thermal_radiation_submit", **params)


@mcp.tool()
def thermal_composite_wall(
    layers: list,
    t_in_c: float,
    t_out_c: float,
    h_in: float | None = None,
    h_out: float | None = None,
    area_m2: float = 1.0,
) -> dict:
    """Exact series thermal-resistance network of a plane composite wall (NO solver)
    — the classic overall-U calculation and the conjugate-heat-transfer family's
    closed-form oracle. `layers` is the in→out list of solid layers, each
    {thickness_mm, k | material} (k in W/m·K, or a Materials-DB name); `h_in`/`h_out`
    are optional convection film coefficients (W/m²K). Per unit area
    R = 1/h_in + Σ tᵢ/kᵢ + 1/h_out, U = 1/R, q = U·(t_in − t_out), and every
    surface/interface temperature follows exactly.

    Returns {u_w_m2k, r_total_m2k_w, q_w_m2, q_w, layer_resistances_m2k_w,
    interface_temps_c (inner surface → outer surface), t_in_c, t_out_c, area_m2}."""
    params = {"layers": layers, "t_in_c": t_in_c, "t_out_c": t_out_c,
              "area_m2": area_m2}
    for k, v in (("h_in", h_in), ("h_out", h_out)):
        if v is not None:
            params[k] = v
    return _call("thermal_composite_wall", **params)


@mcp.tool()
def cht_channel_submit(
    flux_w_m2: float = 10000.0,
    velocity_m_s: float = 0.001,
    t_in_c: float = 20.0,
    length_m: float = 0.1,
    fluid_height_m: float = 0.005,
    solid_thickness_m: float = 0.002,
    k_fluid: float = 0.6,
    rho_fluid: float = 1000.0,
    cp_fluid: float = 4180.0,
    k_solid: float = 1.0,
    nx: int = 80,
    ny_fluid: int = 10,
    ny_solid: int = 4,
    case_dir: str | None = None,
    sif: str = "case.sif",
) -> dict:
    """Conjugate heat transfer via Elmer (P3 M6 frontier), asynchronous — ONE solve
    spanning a plug-flow fluid channel AND a conducting solid wall coupled at their
    shared interface. Requires ElmerSolver; when absent this returns {ok:false,
    reason, install} rather than raising.

    Builds the two-body channel (constant outer `flux_w_m2`, inlet Dirichlet
    `t_in_c`, all else adiabatic) whose gates are exact WITHOUT a Nusselt
    correlation: the outlet bulk temperature follows the energy balance
    q″·L = ṁ·c_p·ΔT and the solid-layer drop is q″·t/k. Defaults are the
    live-validated water channel (cell Péclet ≈ 9 — the builder rejects > 25, where
    stabilized advection visibly leaks the energy balance). Also accepts a prepared
    `case_dir`.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, t_outlet_mean_c, t_out_exact_c, energy_balance_ratio (≈1, ±3%),
    dt_solid_k, dt_solid_exact_k, solid_drop_ratio (≈1), pe_cell, case_dir}."""
    params = {"flux_w_m2": flux_w_m2, "velocity_m_s": velocity_m_s,
              "t_in_c": t_in_c, "length_m": length_m,
              "fluid_height_m": fluid_height_m,
              "solid_thickness_m": solid_thickness_m, "k_fluid": k_fluid,
              "rho_fluid": rho_fluid, "cp_fluid": cp_fluid, "k_solid": k_solid,
              "nx": nx, "ny_fluid": ny_fluid, "ny_solid": ny_solid, "sif": sif}
    if case_dir is not None:
        params["case_dir"] = case_dir
    return _call("cht_channel_submit", **params)


@mcp.tool()
def cht_graetz_submit(
    velocity_m_s: float = 0.025,
    gap_m: float = 0.01,
    length_m: float = 0.12,
    rho_fluid: float = 1000.0,
    mu_fluid: float = 0.02,
    k_fluid: float = 80.0,
    cp_fluid: float = 4000.0,
    t_in_c: float = 20.0,
    t_wall_c: float = 80.0,
    nx: int = 120,
    ny: int = 20,
    case_dir: str | None = None,
    sif: str = "case.sif",
) -> dict:
    """Flow-coupled Graetz channel via Elmer (SIMULATION_NEXT B4), asynchronous —
    the TRUE Nusselt validation that upgrades cht_channel_submit's plug flow:
    FlowSolve computes the real laminar profile and HeatSolver rides on it
    (Convection = Computed) between two isothermal walls. Requires ElmerSolver;
    when absent this returns {ok:false, reason, install} rather than raising.

    Two gates: the solved parabola's u_max/u_mean ≡ 3/2 exactly, and the developed
    mixing-cup decay d ln(T_wall−T_bulk)/dx fitted over the second half of the
    channel yields Nu, gated against the Graetz eigenvalue Nu_T = 7.5407 (parallel
    plates, constant wall temperature) — a slug profile would give π² = 9.87, so
    the gate also proves the profile coupling is real. This closes the loop with
    the `h_estimate` correlation screen. The writer polices Re < 400, development
    lengths inside the first 45 %, and cell Péclet ≤ 25. Also accepts a prepared
    `case_dir`.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, u_max_over_mean (≈1.5), nu_fit, nu_exact, nu_ratio (≈1, ±10 %),
    nu_slug, reynolds, prandtl, pe_cell, case_dir}."""
    params = {}
    if case_dir is not None:
        params.update({"case_dir": case_dir, "sif": sif})
    else:
        params.update({"velocity_m_s": velocity_m_s, "gap_m": gap_m,
                       "length_m": length_m, "rho_fluid": rho_fluid,
                       "mu_fluid": mu_fluid, "k_fluid": k_fluid,
                       "cp_fluid": cp_fluid, "t_in_c": t_in_c,
                       "t_wall_c": t_wall_c, "nx": nx, "ny": ny})
    return _call("cht_graetz_submit", **params)


@mcp.tool()
def acoustic_fem_submit(
    kind: str = "duct",
    length_m: float = 1.0,
    kl: float = 2.0,
    n_elements: int = 200,
    lx_m: float = 0.5,
    ly_m: float = 0.4,
    nx: int = 50,
    ny: int = 40,
    mode_nx: int = 1,
    mode_ny: int = 0,
    span_pct: float = 8.0,
    n_steps: int = 13,
    c_m_s: float = 343.0,
    case_dir: str | None = None,
    sif: str = "case.sif",
) -> dict:
    """Acoustic FEM via Elmer HelmholtzSolve (SIMULATION_NEXT Tier B1),
    asynchronous — the higher-order twin of `acoustic_screen`, gated against its
    exact closed forms. Requires ElmerSolver; when absent this returns {ok:false,
    reason, install} rather than raising.

    kind='duct': a closed duct driven p=1 at x=0, rigid at x=L, at f = kL·c/(2πL)
    (keep `kl` off the quarter-wave resonances) — the rigid-end pressure has the
    exact oracle 1/cos(kL), so `p_end_ratio` ≈ 1 machine-tight. kind='cavity': a
    rigid lx_m × ly_m cavity excited by a corner Wave Flux source, swept ±span_pct%
    around the exact (mode_nx, mode_ny) eigenfrequency in n_steps Scanning steps;
    the in-phase corner-probe response flips sign through resonance, and the 1/A
    zero-crossing gives `f_solved_hz` with `mode_ratio` ≈ 1 (<0.1%). Also accepts
    a prepared `case_dir`.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for duct {ok, p_end_re, p_end_exact, p_end_ratio (≈1), p_mean_ratio,
    frequency_hz, case_dir} | cavity {ok, f_solved_hz, f_exact_hz, mode_ratio
    (≈1), mode, case_dir}."""
    params = {"kind": kind, "c_m_s": c_m_s}
    if case_dir is not None:
        params.update({"case_dir": case_dir, "sif": sif})
    elif kind == "duct":
        params.update({"length_m": length_m, "kl": kl, "n_elements": n_elements})
    else:
        params.update({"lx_m": lx_m, "ly_m": ly_m, "nx": nx, "ny": ny,
                       "mode_nx": mode_nx, "mode_ny": mode_ny,
                       "span_pct": span_pct, "n_steps": n_steps})
    return _call("acoustic_fem_submit", **params)


@mcp.tool()
def harmonic_response(
    natural_frequency_hz: float,
    damping_ratio: float,
    frequency_hz: float | None = None,
    static_deflection_mm: float | None = None,
) -> dict:
    """Exact SDOF harmonic frequency response (NO solver) — the FRF screen and the
    oracle the Elmer `harmonic_response_submit` sweep is gated against. Bridges
    `beam_modal` (f_n) and `random_vibration` (Q = 1/(2ζ)): with r = f/f_n,
    |H| = 1/√((1−r²)²+(2ζr)²), phase = atan2(2ζr, 1−r²), peak amplification
    Q = 1/(2ζ√(1−ζ²)) at f_peak = f_n·√(1−2ζ²), half-power bandwidth ≈ 2ζ·f_n.
    With `frequency_hz` the response at that drive is returned;
    `static_deflection_mm` scales it to an absolute amplitude_mm. fidelity='exact';
    ζ ≥ 1/√2 has no peak (flagged). Escalate to `harmonic_response_submit` for a
    real meshed FRF (multi-mode, geometry-true).

    Returns {natural_frequency_hz, damping_ratio, q_factor, f_peak_hz,
    half_power_bandwidth_hz, frequency_ratio, amplification, phase_deg,
    amplitude_mm, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    params = {"natural_frequency_hz": natural_frequency_hz,
              "damping_ratio": damping_ratio}
    for k, v in (("frequency_hz", frequency_hz),
                 ("static_deflection_mm", static_deflection_mm)):
        if v is not None:
            params[k] = v
    return _call("harmonic_response", **params)


@mcp.tool()
def harmonic_response_submit(
    length_m: float = 0.2,
    height_m: float = 0.01,
    nx: int = 80,
    ny: int = 4,
    youngs_pa: float = 200e9,
    density_kg_m3: float = 7850.0,
    poisson: float = 0.3,
    damping_ratio: float = 0.02,
    traction_pa: float = 1000.0,
    span_pct: float = 10.0,
    n_sweep: int = 21,
    case_dir: str | None = None,
    sif: str = "case.sif",
) -> dict:
    """Harmonic forced response (FRF) via Elmer StressSolve Harmonic Analysis
    (SIMULATION_NEXT Tier B2), asynchronous — a plane-stress cantilever driven by
    a harmonic tip traction, swept one quasi-static point + n_sweep points across
    ±span_pct% of its first resonance, with Rayleigh β tuned to `damping_ratio`
    at f₁. Requires ElmerSolver; when absent this returns {ok:false, reason,
    install} rather than raising.

    Three gates from the in-phase (real) response: `f1_ratio` — Re(H) = 0 exactly
    AT resonance, so the swept tip response's sign-flip locates f₁ vs the
    Euler-Bernoulli `beam_modal` closed form (within ~2%, plane-stress vs beam
    theory); `static_ratio` — the quasi-static point vs the exact tip compliance
    F·L³/(3EI) (within ~5%); `q_ratio` — max|Re|/static vs Q/2 = 1/(4ζ), the
    exact SDOF light-damping identity (within ~15%, sweep-sampled). Cross-links
    `harmonic_response` (the SDOF oracle) and `random_vibration` (same Q). Also
    accepts a prepared `case_dir`.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, f1_solved_hz, f1_eb_hz, f1_ratio, static_solved_m, static_exact_m,
    static_ratio, peak_over_static, q_factor, q_ratio, frf:[[f_hz, tip_re_m]],
    case_dir}."""
    params = {}
    if case_dir is not None:
        params.update({"case_dir": case_dir, "sif": sif})
    else:
        params.update({"length_m": length_m, "height_m": height_m, "nx": nx,
                       "ny": ny, "youngs_pa": youngs_pa,
                       "density_kg_m3": density_kg_m3, "poisson": poisson,
                       "damping_ratio": damping_ratio, "traction_pa": traction_pa,
                       "span_pct": span_pct, "n_sweep": n_sweep})
    return _call("harmonic_response_submit", **params)


@mcp.tool()
def em_skin_depth(
    frequency_hz: float,
    conductivity_s_m: float | None = None,
    conductor: str | None = None,
    mu_r: float = 1.0,
) -> dict:
    """Exact AC skin depth (NO solver) — δ = √(2/(ω·μ₀·μ_r·σ)) plus the per-square
    surface resistance R_s = 1/(σ·δ); fields/current decay e^(−x/δ) into the
    conductor (~95% of induction heating deposits within 1.5·δ). σ from an explicit
    `conductivity_s_m` or a `conductor` name (copper, aluminum, silver, gold, brass,
    steel-mild, stainless-304).

    Returns {skin_depth_m, skin_depth_mm, surface_resistance_ohm,
    angular_frequency_rad_s, conductivity_s_m, mu_r}."""
    params = {"frequency_hz": frequency_hz, "mu_r": mu_r}
    for k, v in (("conductivity_s_m", conductivity_s_m), ("conductor", conductor)):
        if v is not None:
            params[k] = v
    return _call("em_skin_depth", **params)


@mcp.tool()
def em_dc_resistance(
    length_mm: float,
    area_mm2: float,
    conductivity_s_m: float | None = None,
    conductor: str | None = None,
    voltage_v: float | None = None,
) -> dict:
    """Exact DC resistance of a uniform conductor (NO solver) — R = L/(σ·A), the
    closed-form anchor the Elmer em_conduction_submit gate reproduces to machine
    precision. With `voltage_v` the Ohm/Joule pair is included (I = V/R, P = V·I).
    σ from `conductivity_s_m` or a `conductor` name.

    Returns {resistance_ohm, conductivity_s_m, length_m, area_m2, current_a?,
    joule_w?}."""
    params = {"length_mm": length_mm, "area_mm2": area_mm2}
    for k, v in (("conductivity_s_m", conductivity_s_m), ("conductor", conductor),
                 ("voltage_v", voltage_v)):
        if v is not None:
            params[k] = v
    return _call("em_dc_resistance", **params)


@mcp.tool()
def em_field(
    kind: str = "wire",
    current_a: float = 1.0,
    distance_mm: float | None = None,
    turns_per_m: float | None = None,
    mu_r: float = 1.0,
) -> dict:
    """Exact magnetostatic field of the two canonical sources (NO solver):
    `kind='wire'` is the long straight wire B = μ₀·I/(2π·r) at `distance_mm`
    (Ampère's law); `kind='solenoid'` is the long-solenoid interior
    B = μ₀·μ_r·n·I with `turns_per_m`.

    Returns {b_t, b_mt, …} (the field in tesla and millitesla)."""
    params = {"kind": kind, "current_a": current_a, "mu_r": mu_r}
    for k, v in (("distance_mm", distance_mm), ("turns_per_m", turns_per_m)):
        if v is not None:
            params[k] = v
    return _call("em_field", **params)


@mcp.tool()
def em_conduction_submit(
    voltage_v: float = 0.001,
    length_m: float = 0.1,
    width_m: float = 0.02,
    conductivity_s_m: float | None = None,
    conductor: str = "copper",
    nx: int = 40,
    ny: int = 8,
) -> dict:
    """DC current conduction via Elmer's StatCurrentSolver (P3 M6 frontier),
    asynchronous. Requires ElmerSolver; when absent this returns {ok:false, reason,
    install} rather than raising. Builds a rectangular strip with `voltage_v`
    across its ends and reads the electrode current, total Joule heating and
    Elmer's effective resistance — all machine-exact against R = L/(σ·A)
    (`resistance_ratio` = 1.000000 live).

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, current_a, joule_w, effective_resistance_ohm, resistance_exact_ohm,
    current_exact_a, resistance_ratio, case_dir}."""
    params = {"voltage_v": voltage_v, "length_m": length_m, "width_m": width_m,
              "conductor": conductor, "nx": nx, "ny": ny}
    if conductivity_s_m is not None:
        params["conductivity_s_m"] = conductivity_s_m
    return _call("em_conduction_submit", **params)


@mcp.tool()
def em_induction_submit(
    frequency_hz: float = 50.0,
    conductivity_s_m: float | None = None,
    conductor: str = "copper",
    mu_r: float = 1.0,
    depths: float = 5.3,
    nx: int = 100,
    ny: int = 2,
) -> dict:
    """AC skin effect / induction via Elmer's harmonic 2-D magnetodynamics (P3 M6
    frontier), asynchronous. Requires ElmerSolver; when absent this returns
    {ok:false, reason, install} rather than raising. Builds a conductor slab
    `depths` skin depths deep driven by the surface vector potential at
    `frequency_hz`, solves the complex field, and fits the e-folding length of
    BOTH the magnitude and the phase of A(x) — each must equal the exact
    δ = √(2/(ω·μ₀·μ_r·σ)) (live: decay_ratio 0.999, phase_ratio 1.000). The Joule
    deposition profile |J|² ∝ e^(−2x/δ) is the induction-heating answer.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, skin_depth_exact_m, decay_length_m, phase_length_m, decay_ratio,
    phase_ratio, case_dir}."""
    params = {"frequency_hz": frequency_hz, "conductor": conductor, "mu_r": mu_r,
              "depths": depths, "nx": nx, "ny": ny}
    if conductivity_s_m is not None:
        params["conductivity_s_m"] = conductivity_s_m
    return _call("em_induction_submit", **params)


@mcp.tool()
def em_induction_heating_submit(
    frequency_hz: float = 1.0e4,
    conductivity_s_m: float | None = None,
    conductor: str = "copper",
    mu_r: float = 1.0,
    a_surface: float = 1.0e-3,
    density_kg_m3: float = 8960.0,
    cp_j_kgk: float = 385.0,
    k_thermal: float = 400.0,
    heat_duration_s: float = 0.01,
    n_steps: int = 20,
    depths: float = 5.3,
    nx: int = 100,
    ny: int = 2,
    case_dir: str | None = None,
    sif: str = "case.sif",
) -> dict:
    """Coupled induction heating via Elmer (SIMULATION_NEXT B5), asynchronous —
    completes `em_induction_submit` into a THERMAL answer: the harmonic
    MagnetoDynamics solve runs once, MagnetoDynamicsCalcFields turns it into the
    time-averaged Joule loss field, and a transient adiabatic HeatSolver
    integrates it for `heat_duration_s`. Requires ElmerSolver; when absent this
    returns {ok:false, reason, install} rather than raising.

    Two gates: joule_power_ratio — the solved eddy-current power vs the exact
    deep-slab dissipation P″ = R_s·|H₀|²/2 = ω²σA₀²δ/4 (from the shipped
    `em_skin_depth` chain; live 1.0003) — and energy_balance_ratio — the mean
    temperature rise vs P·t/(m·cₚ) (live 1.005). Conductor σ from a name or
    explicit `conductivity_s_m`; thermal ρ/cₚ/k explicit. Also accepts a prepared
    `case_dir`.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, eddy_power_w_m, p_total_exact_w_m, joule_power_ratio (≈1),
    t_mean_final_k, dt_mean_exact_k, energy_balance_ratio (≈1), skin_depth_m,
    case_dir}."""
    params = {}
    if case_dir is not None:
        params.update({"case_dir": case_dir, "sif": sif})
    else:
        params.update({"frequency_hz": frequency_hz, "conductor": conductor,
                       "mu_r": mu_r, "a_surface": a_surface,
                       "density_kg_m3": density_kg_m3, "cp_j_kgk": cp_j_kgk,
                       "k_thermal": k_thermal,
                       "heat_duration_s": heat_duration_s, "n_steps": n_steps,
                       "depths": depths, "nx": nx, "ny": ny})
        if conductivity_s_m is not None:
            params["conductivity_s_m"] = conductivity_s_m
    return _call("em_induction_heating_submit", **params)


@mcp.tool()
def cfd_pipe_flow(
    diameter_mm: float,
    length_mm: float,
    flow_rate_lpm: float | None = None,
    velocity_m_s: float | None = None,
    fluid: str = "water-20c",
    mu_pa_s: float | None = None,
    rho_kg_m3: float | None = None,
    roughness_mm: float = 0.0,
) -> dict:
    """Analytic straight-pipe pressure drop (NO solver) — the fast internal-flow screen
    and the exact gate the OpenFOAM cfd_internal_flow solve is checked against. Laminar
    (Re<2300) is Hagen–Poiseuille Δp = 128·μ·L·Q/(π·D⁴) with its D⁴ scaling — exact;
    turbulent uses smooth-pipe Blasius, or Colebrook–White when `roughness_mm` is given
    (the Colebrook value is always reported for turbulent flow) — a ±10 % Moody-band
    correlation (fidelity labeled). Give flow as `flow_rate_lpm` or `velocity_m_s`;
    fluid μ,ρ from a name ('water-20c','air-20c','oil-sae30-20c','glycerin-20c') or
    explicit `mu_pa_s`+`rho_kg_m3`. Escalate turbulent cases to
    cfd_internal_flow_submit(turbulence='kOmegaSST').

    Returns {reynolds, regime, velocity_m_s, flow_rate_m3_s, friction_factor,
    colebrook_friction_factor, relative_roughness, pressure_drop_pa, wall_shear_pa,
    hagen_poiseuille_pa, laminar, fidelity, band_pct, escalate_to}."""
    params = {"diameter_mm": diameter_mm, "length_mm": length_mm, "fluid": fluid,
              "roughness_mm": roughness_mm}
    for k, v in (("flow_rate_lpm", flow_rate_lpm), ("velocity_m_s", velocity_m_s),
                 ("mu_pa_s", mu_pa_s), ("rho_kg_m3", rho_kg_m3)):
        if v is not None:
            params[k] = v
    return _call("cfd_pipe_flow", **params)


@mcp.tool()
def cfd_internal_flow_submit(
    case_dir: str | None = None,
    application: str | None = None,
    diameter_mm: float | None = None,
    length_mm: float | None = None,
    body: str | None = None,
    inlet_face: int | None = None,
    outlet_face: int | None = None,
    velocity_m_s: float | None = None,
    flow_rate_lpm: float | None = None,
    fluid: str = "water-20c",
    mu_pa_s: float | None = None,
    rho_kg_m3: float | None = None,
    n_axial: int | None = None,
    n_radial: int | None = None,
    base_cell_mm: float | None = None,
    location_in_mesh_mm: list | None = None,
    stl_tolerance_mm: float = 0.2,
    end_time: int | None = None,
    turbulence: str = "laminar",
) -> dict:
    """Internal-flow CFD (pressure drop) via OpenFOAM or SU2, asynchronous. Requires an
    OpenFOAM (apt/conda) or SU2 binary; when none resolves this returns {ok:false,
    reason, install} rather than raising. `turbulence='kOmegaSST'` upgrades the pipe
    validation case to RANS (SIMULATION_NEXT B3): wall-function k/ω/ν_t with first-cell
    y+ targeted at ~30–100, developed dp/dx fitted over the second half of a ≥40·D pipe,
    gated BANDED against Colebrook (`colebrook_ratio` ≈ 1 ± 10 % — the Moody correlation
    is itself a band, never an exact gate). Three modes:

    - **Build the straight-pipe validation case** (no case prep): pass `diameter_mm`,
      `length_mm`, and `velocity_m_s` (or `flow_rate_lpm`), with a `fluid` name or
      explicit `mu_pa_s`+`rho_kg_m3` (mesh density via `n_axial`/`n_radial`, iterations
      via `end_time`). The handler builds the axisymmetric laminar pipe, runs
      blockMesh+simpleFoam, and returns the solved Δp next to the Hagen–Poiseuille
      analytic reference (`cfd_pipe_flow`) — the kickoff's exact CFD gate, `hp_ratio`≈1.
    - **Solve a real FreeCAD solid — the geometry bridge**: pass a `body` handle plus
      `inlet_face`/`outlet_face` (1-based indices into the solid's faces; every other
      face becomes a no-slip wall) and `velocity_m_s` (applied along the inlet face's
      inward normal). The solid tessellates into a multi-region STL and meshes with
      blockMesh + snappyHexMesh; `base_cell_mm` sets the background cell size,
      `location_in_mesh_mm` the kept-region seed point (default: bbox centre — set it
      for non-convex solids), `stl_tolerance_mm` the tessellation sag. Use the
      developed-profile `pressure_drop_pa`; also pass `diameter_mm`+`length_mm` to get
      an `hp_ratio` reference for pipe-like bodies.
    - **Run a prepared OpenFOAM `case_dir`** (optionally an `application`, e.g.
      'simpleFoam'/'foamRun'); the OpenFOAM environment is sourced before the run.

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result.
    Pipe/body cases: {ok, returncode, pressure_drop_pa (developed),
    pressure_drop_inlet_pa, hagen_poiseuille_pa?, hp_ratio?, n_cells, case_dir};
    RANS pipe adds {dpdx_pa_m, dpdx_colebrook_pa_m, colebrook_ratio, y_plus_estimate,
    band_pct}. Prepared case: {ok, returncode, solver, application, case_dir, kind,
    stdout_tail}."""
    params = {"fluid": fluid, "stl_tolerance_mm": stl_tolerance_mm,
              "turbulence": turbulence}
    for k, v in (("case_dir", case_dir), ("application", application),
                 ("diameter_mm", diameter_mm), ("length_mm", length_mm),
                 ("body", body), ("inlet_face", inlet_face),
                 ("outlet_face", outlet_face), ("base_cell_mm", base_cell_mm),
                 ("location_in_mesh_mm", location_in_mesh_mm),
                 ("velocity_m_s", velocity_m_s), ("flow_rate_lpm", flow_rate_lpm),
                 ("mu_pa_s", mu_pa_s), ("rho_kg_m3", rho_kg_m3),
                 ("n_axial", n_axial), ("n_radial", n_radial),
                 ("end_time", end_time)):
        if v is not None:
            params[k] = v
    return _call("cfd_internal_flow_submit", **params)


@mcp.tool()
def cfd_external_flow_submit(
    velocity_m_s: float | None = None,
    plate_length_mm: float | None = None,
    fluid: str = "air-20c",
    mu_pa_s: float | None = None,
    rho_kg_m3: float | None = None,
    case_dir: str | None = None,
    application: str | None = None,
    model: str | None = None,
    nx_plate: int | None = None,
    n_y: int | None = None,
    end_time: int | None = None,
    turbulence: str = "laminar",
) -> dict:
    """External-flow CFD (drag) via OpenFOAM or SU2, asynchronous. Requires an OpenFOAM
    (apt/conda) or SU2 binary; when none resolves this returns {ok:false, reason,
    install} rather than raising. `turbulence='kOmegaSST'` upgrades the plate to RANS
    (SIMULATION_NEXT B3, default plate_length 1000 mm so Re_L > transition): the
    headline drag is the trailing-edge momentum-thickness integral, gated BANDED
    against the mixed-transition Cf = 0.074·Re^(−1/5) − A/Re (`cf_mixed_ratio` ≈ 1
    ± 15 % — the 1/7-power family is itself a band); the (ν+ν_t)-corrected wall-shear
    sum is the cross-check. Two modes:

    - **Build the flat-plate validation case** (no case prep): pass `velocity_m_s`, with
      optional `plate_length_mm` (default 100), a `fluid` name ('air-20c','water-20c',…)
      or explicit `mu_pa_s`+`rho_kg_m3`, and mesh knobs `nx_plate`/`n_y`/`end_time`. The
      handler builds a 2-D laminar flat plate with a clean leading edge (slip→plate→slip,
      far-field top), runs blockMesh+simpleFoam, integrates the wall-shear drag straight
      from the converged U field (OpenFOAM's force function objects abort with a 'sha1'
      IOstream error in this build), and returns the solved Cd next to the Blasius
      reference Cf=1.328/√Re_L — the kickoff's external gate (`blasius_ratio`≈1, ~15%).
    - **Run a prepared OpenFOAM `case_dir`** (optionally an `application`).

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result. Flat
    plate: {ok, returncode, reynolds_l, cd, cf_solved, cf_blasius, blasius_ratio,
    drag_force_n, drag_momentum_n, drag_blasius_n, n_cells, case_dir}; RANS plate
    swaps the gate fields for {cf_solved (momentum), cf_mixed_ref, cf_mixed_ratio,
    cf_turbulent_ref, cf_wall_corrected, y_plus_estimate, band_pct}. Prepared case:
    {ok, returncode, solver, application, case_dir, kind, stdout_tail}."""
    params = {"fluid": fluid, "turbulence": turbulence}
    for k, v in (("velocity_m_s", velocity_m_s), ("plate_length_mm", plate_length_mm),
                 ("mu_pa_s", mu_pa_s), ("rho_kg_m3", rho_kg_m3), ("case_dir", case_dir),
                 ("application", application), ("model", model),
                 ("nx_plate", nx_plate), ("n_y", n_y), ("end_time", end_time)):
        if v is not None:
            params[k] = v
    return _call("cfd_external_flow_submit", **params)


@mcp.tool()
def random_vibration(
    psd_profile: list,
    analysis: str | None = None,
    frequencies_hz: list | None = None,
    q: float = 10.0,
    modal_stress_mpa_per_g: float | None = None,
    allowable_stress_mpa: float | None = None,
) -> dict:
    """Random-vibration response off a modal run (Miles' equation; closed-form, no
    external solver). Provide either `analysis` (a handle whose fem_modal + fem_run
    already produced natural frequencies) or an explicit `frequencies_hz` list, plus
    a base-acceleration PSD `psd_profile` ([{"hz":20,"g2_hz":0.01}, ...]; log-log
    interpolated, and zero outside its band so a mode stiffened above the band
    escapes drive). Each mode is an SDOF resonator with amplification `q` (default
    10; rule of thumb Q≈√f_n), combined by SRSS: rms_g = sqrt(Σ (π/2)·f·W(f)·Q).
    With `modal_stress_mpa_per_g` the g response converts to RMS / 3-σ stress; add
    `allowable_stress_mpa` for a pass/fail.

    Returns {rms_g, first_mode_hz, dominant_mode_hz, q, psd_band_hz, miles_grms_g,
    modes: [{mode, frequency_hz, psd_g2_hz, contribution_g, in_band}], rms_stress_mpa,
    three_sigma_stress_mpa, pass}."""
    params = {"psd_profile": psd_profile, "q": q}
    for k, v in (("analysis", analysis), ("frequencies_hz", frequencies_hz),
                 ("modal_stress_mpa_per_g", modal_stress_mpa_per_g),
                 ("allowable_stress_mpa", allowable_stress_mpa)):
        if v is not None:
            params[k] = v
    return _call("random_vibration", **params)


@mcp.tool()
def beam_modal(
    length_mm: float,
    width_mm: float,
    height_mm: float,
    boundary: str = "cantilever",
    n_modes: int = 3,
    youngs_gpa: float | None = None,
    density_kg_m3: float | None = None,
    material: str | None = None,
) -> dict:
    """Exact Euler-Bernoulli natural frequencies of a uniform rectangular beam — the
    closed-form modal oracle (no solver), and the band the CalculiX `fem_modal`
    eigen-solve is gated against. f_n = (βL)_n²/(2π)·sqrt(E·I/(ρ·A·L⁴)); the beam bends
    in `height_mm` (I = width·height³/12, so a slender beam's lowest mode is the
    thinnest-direction bend). `boundary`: 'cantilever' | 'simply_supported' |
    'clamped_clamped' | 'free_free' | 'clamped_pinned' (up to 5 modes each). Material
    via `youngs_gpa`+`density_kg_m3`, or a Materials-DB `material` name. Slender-beam
    theory — accurate while length ≫ height (thick beams need a Timoshenko correction).

    Returns {boundary, n_modes, frequencies_hz, beta_l, first_mode_hz, youngs_gpa,
    density_kg_m3, area_mm2, I_mm4, slenderness}."""
    params = {"length_mm": length_mm, "width_mm": width_mm, "height_mm": height_mm,
              "boundary": boundary, "n_modes": n_modes}
    for k, v in (("youngs_gpa", youngs_gpa), ("density_kg_m3", density_kg_m3),
                 ("material", material)):
        if v is not None:
            params[k] = v
    return _call("beam_modal", **params)


@mcp.tool()
def dfm_check(
    faces: list,
    pull_axis: str = "+z",
    process: str = "injection",
    min_wall_mm: float | None = None,
    min_draft_deg: float = 1.0,
) -> dict:
    """Screen a part for manufacturability against a pull/tool axis. `faces` is a
    list of {name, draft_deg, wall_mm?} — draft_deg relative to pull_axis (0 = a
    vertical wall needing draft; <0 = a re-entrant undercut). draft_violations are
    0≤draft<min_draft_deg, undercut_faces are draft<0, min_wall_violations are
    wall_mm<min_wall_mm (defaults by process: injection 1.0, cnc 0.5, sheet/fdm
    0.8). Returns {process, pull_axis, min_wall_mm, draft_violations, undercut_faces,
    min_wall_violations, score, pass}."""
    params = {"faces": faces, "pull_axis": pull_axis, "process": process,
              "min_draft_deg": min_draft_deg}
    if min_wall_mm is not None:
        params["min_wall_mm"] = min_wall_mm
    return _call("dfm_check", **params)


@mcp.tool()
def optics_raytrace(
    n_refractive: float = 1.49062,
    source_config: dict | None = None,
    n_rays: int = 64,
    model: dict | None = None,
) -> dict:
    """Ray-trace a bundle through a dielectric optical model with rayoptics
    (asynchronous-free; needs no FreeCAD geometry). Requires the rayoptics wheel
    (the `optics` extra); when it does not resolve this returns {ok:false, reason,
    install} rather than raising. The geometric refraction comes from rayoptics;
    the Fresnel/TIR energy split + the exit histogram come from the exact
    analysis/optics core, so the trace is gated against that oracle
    (`oracle_max_dev_deg` = max rayoptics−Snell exit-angle deviation, ~0).

    `n_refractive` is the medium index n2 (default PMMA 1.49062). `source_config`
    is {kind:'collimated'(angle_deg)|'cone'(half_angle_deg)|'lambertian'
    (max_angle_deg)} (default collimated at normal incidence). `model` may carry
    {n1 (incident index, default air 1.0), absorption (0..1 bulk loss),
    target_half_angle_deg (the acceptance cone counted as efficiency)}.

    Returns the degradation dict, or {ok, backend:'rayoptics', rayoptics_version,
    n_rays, n1, n2, critical_angle_deg, efficiency, leakage_fraction,
    absorbed_fraction, tir_fraction, energy_balance, oracle_max_dev_deg,
    exit_distribution:[{angle_deg,intensity}], hotspot_locations}."""
    params = {"n_refractive": n_refractive, "n_rays": n_rays}
    for k, v in (("source_config", source_config), ("model", model)):
        if v is not None:
            params[k] = v
    return _call("optics_raytrace", **params)


@mcp.tool()
def optics_lens_design(
    surfaces: list,
    epd: float | None = None,
    fno: float | None = None,
    wavelengths_um: list | None = None,
    field_angles_deg: list | None = None,
    image_solve: bool = True,
    want_spot: bool = True,
) -> dict:
    """First-order + spot analysis of a SEQUENTIAL optical system with optiland
    (MIT, in-process). Requires the `optics` extra; degrades to {ok:false, reason,
    install} otherwise. For a single lens optiland is gated against the analytic
    thick-lens oracle (`oracle_dev_pct`).

    `surfaces`: list (object->image) of {radius, thickness, material, stop?} — exactly
    one surface must set stop:true (the aperture stop). `epd` (entrance-pupil dia)
    OR `fno`. `wavelengths_um` (first is primary, default [0.5876]). `field_angles_deg`
    (default [0.0]). `image_solve` solves the last gap to paraxial focus.

    Returns the degradation dict, or {ok, backend:'optiland', optiland_version,
    efl_mm, bfl_mm, fno, n_surfaces, rms_spot_um:[per field], oracle_efl_mm,
    oracle_dev_pct}."""
    params: dict = {"surfaces": surfaces, "image_solve": image_solve,
                    "want_spot": want_spot}
    for k, v in (("epd", epd), ("fno", fno), ("wavelengths_um", wavelengths_um),
                 ("field_angles_deg", field_angles_deg)):
        if v is not None:
            params[k] = v
    return _call("optics_lens_design", **params)


@mcp.tool()
def optics_lens_optimize(
    surfaces: list,
    variables: list,
    targets: list,
    epd: float | None = None,
    wavelengths_um: list | None = None,
    field_angles_deg: list | None = None,
    maxiter: int = 200,
) -> dict:
    """Optimize a SEQUENTIAL optical system with optiland's optimizer (MIT,
    in-process) — the capability rayoptics lacks. Requires the `optics` extra;
    degrades to {ok:false, reason, install} otherwise.

    `surfaces`: as in optics_lens_design. `variables`: [{type:'radius'|'thickness',
    surface:<1-based int>}] — the degrees of freedom. `targets`: [{operand:'f2'|
    'rms_spot_size'|…, target, weight?, surface?}] — the merit function. `maxiter`
    caps iterations.

    Returns the degradation dict, or {ok, backend:'optiland', converged, n_fev,
    before:{efl_mm,rss}, after:{efl_mm,rss}, surfaces:[optimized]}."""
    params: dict = {"surfaces": surfaces, "variables": variables, "targets": targets,
                    "maxiter": maxiter}
    for k, v in (("epd", epd), ("wavelengths_um", wavelengths_um),
                 ("field_angles_deg", field_angles_deg)):
        if v is not None:
            params[k] = v
    return _call("optics_lens_optimize", **params)


@mcp.tool()
def optics_solid_trace(
    rays: list,
    model: str | None = None,
    stl_path: str | None = None,
    glass: str | None = None,
    n_refractive: float | None = None,
    wavelength_um: float = 0.55,
    solid: dict | None = None,
) -> dict:
    """NON-SEQUENTIAL ray trace through a real solid (STL mesh) with a refractive
    index — the lane for molded optical parts (light-pipes, prisms, lenses). Backed
    by KrakenOS, which is GPL-3.0 and is run ONLY in a subprocess (the parent never
    imports it — same arm's-length isolation as the GPL Elmer/OpenFOAM binaries).
    Requires the `optics_gpl` extra; degrades to {ok:false, reason, install} otherwise.

    Geometry: pass a `model` handle (exported to STL here) OR a ready `stl_path`.
    Material: `glass` (KrakenOS catalog name, e.g. 'BK7') or `n_refractive` (constant
    index). `rays`: [{origin:[x,y,z], dir:[l,m,n]}]; each ray's turn_deg is its
    input->exit bend (~90 for a TIR corner prism, ~0 for a straight pass). `solid`:
    {diameter, thickness, axis_move} placement. `wavelength_um` default 0.55.

    Returns the degradation dict, or {ok, backend:'KrakenOS (subprocess-isolated,
    GPL-3.0)', n_launched, n_valid, valid_fraction, mean_turn_deg, max_turn_deg,
    rays:[{valid, exit_dir, turn_deg}], stl_path}."""
    params: dict = {"rays": rays, "wavelength_um": wavelength_um}
    for k, v in (("model", model), ("stl_path", stl_path), ("glass", glass),
                 ("n_refractive", n_refractive), ("solid", solid)):
        if v is not None:
            params[k] = v
    return _call("optics_solid_trace", **params)


# --- granular / powder discrete-element mechanics (YADE DEM, GPL-3.0) ----------
# The closed-form oracle (granular_screen) gates the solver; dem_pack_submit /
# dem_flow_submit run the REAL YADE DEM solve off the MCP channel (background
# jobs.py jobs — poll with job_status / job_result). YADE is GPL-3.0 and is run
# ONLY out-of-process via the `yade` executable, so its copyleft never reaches
# DriftPin's permissive code; absent YADE, the *_submit tools degrade to
# {ok:false, reason, install, oracle} (the oracle band still comes back).

@mcp.tool()
def granular_screen(
    problem: str = "packing",
    regime: str = "random_close",
    coordination: float | None = None,
    outlet_m: float | None = None,
    particle_d_m: float | None = None,
    bulk_density_kg_m3: float | None = None,
    material: str | None = None,
    friction_coeff: float | None = None,
    saturation: float = 1.0,
    outlet1_m: float | None = None,
    flow1_kg_s: float | None = None,
    outlet2_m: float | None = None,
    flow2_kg_s: float | None = None,
    mu_low: float | None = None,
    repose_low_deg: float | None = None,
    mu_high: float | None = None,
    repose_high_deg: float | None = None,
) -> dict:
    """Closed-form granular/powder-mechanics oracles — banded correlations, NO
    external solver (the FreeCAD-free analytic twins the YADE DEM solve is gated
    against). These are correlations, not exact theory, so each returns
    fidelity='correlation' + an honest [low, high] band; the band IS the oracle.
    Dispatch on `problem`:

      'packing' (regime='random_close'|'random_loose'|'fcc'[, coordination]) —
          monodisperse sphere solid-volume fraction φ. RCP ≈ 0.637 (band
          0.60–0.66), the random pile a real settle must hit, well below the
          crystalline FCC/HCP 0.7405.
      'beverloo' (outlet_m, particle_d_m[, bulk_density_kg_m3 | material]) —
          flat-bottom hopper discharge W = C·ρ·√g·(D−k·d)^2.5 [kg/s]; flow ∝
          outlet to the 2.5 power, independent of fill height.
      'beverloo_exponent' (outlet1_m, flow1_kg_s, outlet2_m, flow2_kg_s
          [, particle_d_m]) — recover the log-log flow exponent from two
          (outlet, flow) points; granular 2.5 (band 2.2–2.8) vs Torricelli 2.0.
      'repose' (friction_coeff[, saturation]) — poured-pile repose angle
          θ ≈ atan(μ) + ±25% band.
      'repose_monotone' (mu_low, repose_low_deg, mu_high, repose_high_deg) —
          the steeper-with-friction monotonicity gate.

    SI units (m, kg/m³, kg/s, degrees). Escalate to dem_pack_submit /
    dem_flow_submit (the real YADE solve) for polydisperse mixes, non-spherical
    grains, cohesion, or geometry this monodisperse idealization can't see."""
    params: dict = {"problem": problem}
    for k, v in (("regime", regime), ("coordination", coordination),
                 ("outlet_m", outlet_m), ("particle_d_m", particle_d_m),
                 ("bulk_density_kg_m3", bulk_density_kg_m3), ("material", material),
                 ("friction_coeff", friction_coeff), ("saturation", saturation),
                 ("outlet1_m", outlet1_m), ("flow1_kg_s", flow1_kg_s),
                 ("outlet2_m", outlet2_m), ("flow2_kg_s", flow2_kg_s),
                 ("mu_low", mu_low), ("repose_low_deg", repose_low_deg),
                 ("mu_high", mu_high), ("repose_high_deg", repose_high_deg)):
        if v is not None:
            params[k] = v
    return _call("granular_oracle", **params)


@mcp.tool()
def dem_pack_submit(
    n_spheres: int = 800,
    radius_m: float = 0.004,
    box_m: list | None = None,
    friction_deg: float = 26.0,
    young_pa: float = 1e7,
    density: float = 2600.0,
    steps: int = 30000,
) -> dict:
    """Pour N monodisperse spheres into a box and settle them under gravity with
    the REAL YADE discrete-element engine, then measure the random close-packing
    fraction φ of the settled bed — gated against granular_screen('packing') (RCP
    band 0.60–0.66, well below the crystalline 0.7405). YADE is GPL-3.0 and is run
    ONLY in a subprocess (the parent never imports it — same arm's-length
    isolation as the GPL Elmer/OpenFOAM binaries). Runs OFF the MCP channel via a
    background job, so a multi-second settle never blocks the worker.

    `box_m` is the [Lx, Ly] floor footprint (m; default [0.06, 0.06]); the column
    height is sized to hold n_spheres. `friction_deg` is the inter-particle
    friction angle; `young_pa` the contact modulus; `density` the grain density.
    Returns {job_id, status, cache_hit, oracle} (poll job_status / job_result);
    the job result carries the oracle band PLUS the measured {packing_fraction,
    in_band, n_settled, settled_height_m, mean_coordination, positions:[[x,y,z,r]]}.
    Absent YADE: {ok:false, reason, install, oracle}."""
    params = {"n_spheres": n_spheres, "radius_m": radius_m,
              "box_m": box_m or [0.06, 0.06], "friction_deg": friction_deg,
              "young_pa": young_pa, "density": density, "steps": steps}
    return _call("dem_pack_submit", **params)


@mcp.tool()
def dem_flow_submit(
    n_spheres: int = 1500,
    radius_m: float = 0.003,
    box_m: list | None = None,
    outlet_m: float = 0.03,
    friction_deg: float = 26.0,
    young_pa: float = 1e7,
    density: float = 2600.0,
    settle_steps: int = 20000,
    flow_steps: int = 60000,
) -> dict:
    """Discharge spheres from a flat-bottomed hopper box through a central orifice
    with the REAL YADE discrete-element engine and measure the steady mass-flow
    rate — gated against granular_screen('beverloo') (flow ∝ outlet^2.5). Submit
    two `outlet_m` sizes and feed the (outlet, flow) pair to
    granular_screen('beverloo_exponent') to check the Beverloo 2.5 exponent (vs the
    Torricelli 2.0 of a draining fluid). YADE is GPL-3.0 and is run ONLY in a
    subprocess; runs OFF the MCP channel via a background job.

    `box_m` is the [Lx, Ly, Lz] hopper box (m; default [0.10, 0.10, 0.20]);
    `outlet_m` the central orifice diameter; `settle_steps`/`flow_steps` the DEM
    step budgets. Returns {job_id, status, cache_hit, oracle}; the job result
    carries the Beverloo oracle PLUS {mass_flow_kg_s, n_discharged,
    discharge_time_s, positions:[...]}. Absent YADE: {ok:false, reason, install,
    oracle}."""
    params = {"n_spheres": n_spheres, "radius_m": radius_m,
              "box_m": box_m or [0.10, 0.10, 0.20], "outlet_m": outlet_m,
              "friction_deg": friction_deg, "young_pa": young_pa,
              "density": density, "settle_steps": settle_steps,
              "flow_steps": flow_steps}
    return _call("dem_flow_submit", **params)


@mcp.tool()
def optics_moldability_check(
    model: str,
    pull_axis: str = "+z",
    process: str = "injection",
    min_draft_deg: float = 1.0,
    min_wall_mm: float | None = None,
) -> dict:
    """Moldability screen for a part against a single pull axis — geometric, no
    solver. Resolves the `model` handle's solid, then per face computes the draft
    relative to `pull_axis` from the outward normal (draft_deg = 90 − angle(normal,
    pull); 0 = a wall parallel to the pull that needs draft) and ray-casts the face
    centroid along ±pull: a face the straight pull frees in neither direction is a
    re-entrant UNDERCUT (reported with negative draft). Inward chords give a wall-
    thickness distribution. Scored through the same DfM machinery as dfm_check.

    `pull_axis`: '+z'/'-x'/… or an [x,y,z] vector. `process` ('injection'|'cnc'|
    'sheet'|'fdm') sets the default min wall; override with `min_wall_mm`.

    Returns {process, pull_axis, n_faces, undercut_faces, draft_violations,
    min_wall_violations, wall_thickness_stats:{min_mm,mean_mm,max_mm,n}, score,
    pass}."""
    params = {"model": model, "pull_axis": pull_axis, "process": process,
              "min_draft_deg": min_draft_deg}
    if min_wall_mm is not None:
        params["min_wall_mm"] = min_wall_mm
    return _call("optics_moldability_check", **params)


@mcp.tool()
def dfa_check(
    part_count: int,
    fastener_count: int = 0,
    unique_part_count: int | None = None,
    insertion_axes: int = 1,
    symmetric_fraction: float = 0.0,
) -> dict:
    """Grade an assembly (Boothroyd-Dewhurst-lite). assembly_efficiency =
    theoretical_min/(part_count+fastener_count) (theoretical_min = unique_part_count
    or 1); assembly_score scales that by a handling penalty from insertion_axes/
    symmetry and decreases monotonically as part/fastener count rises. The grade is
    an ordinal index for comparing variants (fidelity='correlation', band_pct=None)
    — rank with it, don't gate on the absolute value. Returns
    {part_count, fastener_count, insertion_axes, handling_difficulty,
    assembly_efficiency, assembly_score, symmetry_score, fidelity, band_pct}."""
    params = {"part_count": part_count, "fastener_count": fastener_count,
              "insertion_axes": insertion_axes, "symmetric_fraction": symmetric_fraction}
    if unique_part_count is not None:
        params["unique_part_count"] = unique_part_count
    return _call("dfa_check", **params)


@mcp.tool()
def pack_check(
    part_bbox_mm: list,
    carton_mm: list,
    mass_g: float,
    dim_factor: float = 5000.0,
) -> dict:
    """Check a part against a shipping carton + compute billable weight.
    part_bbox_mm/carton_mm are [l,w,h] mm; `fits` allows reorientation (sorted-dim
    compare). void_fraction = 1−vol(part)/vol(carton); dim_weight_kg =
    vol(carton cm³)/dim_factor (default 5000 metric DIM); billable_weight_kg =
    max(actual, dimensional). Returns {fits, void_fraction, dim_weight_kg,
    actual_mass_kg, billable_weight_kg, pass}."""
    return _call("pack_check", part_bbox_mm=part_bbox_mm, carton_mm=carton_mm,
                 mass_g=mass_g, dim_factor=dim_factor)


@mcp.tool()
def cost_estimate(
    volume_mm3: float,
    material: str,
    process: str = "cnc",
    quantity: int = 1,
    tooling_usd: float = 0.0,
    machine_rate_usd_hr: float = 60.0,
    setup_min: float = 10.0,
    scrap_fraction: float = 0.0,
) -> dict:
    """Per-unit cost rollup (Design for Cost). material_cost = volume·density·price
    ·(1+scrap) from the Materials DB (process: cnc | fdm | casting | injection).
    process_cost = amortized setup + per-process machine time; tooling amortized
    over quantity, so unit_cost falls as quantity rises. The machine-time table is
    order-of-magnitude (fidelity='correlation', band_pct=100) — trust the ratios
    between processes/quantities, not the absolute dollars; material_cost alone is
    exact given its inputs. Returns {material_cost, process_cost, tooling_amortized,
    unit_cost, mass_kg, fidelity, band_pct, breakdown}. Errors on an unknown
    material/process or non-positive volume/quantity."""
    return _call("cost_estimate", volume_mm3=volume_mm3, material=material,
                 process=process, quantity=quantity, tooling_usd=tooling_usd,
                 machine_rate_usd_hr=machine_rate_usd_hr, setup_min=setup_min,
                 scrap_fraction=scrap_fraction)


@mcp.tool()
def slice_estimate(
    volume_mm3: float,
    bbox_mm: list,
    material: str = "PLA",
    infill_fraction: float = 1.0,
    layer_height_mm: float = 0.2,
    wall_fraction: float = 0.35,
    print_speed_mm_s: float = 50.0,
    nozzle_mm: float = 0.4,
) -> dict:
    """First-order FDM slice estimate (analytic, NO slicer needed; see
    slice_gcode_submit for the real PrusaSlicer CLI). mass_g = volume·density
    (Materials DB); deposited = volume·(wall_fraction + infill·(1−wall_fraction))
    so at 100% infill filament_g == mass_g; layer_count = ceil(bbox
    height/layer_height); print_time from nozzle volumetric flow. Returns {mass_g,
    filament_g, deposited_volume_mm3, layer_count, print_time_min,
    infill_fraction}."""
    return _call("slice_estimate", volume_mm3=volume_mm3, bbox_mm=bbox_mm,
                 material=material, infill_fraction=infill_fraction,
                 layer_height_mm=layer_height_mm, wall_fraction=wall_fraction,
                 print_speed_mm_s=print_speed_mm_s, nozzle_mm=nozzle_mm)


@mcp.tool()
def slice_gcode_submit(
    body: str | None = None,
    stl_path: str | None = None,
    layer_height_mm: float = 0.2,
    infill_fraction: float = 0.2,
    supports: bool = False,
    material: str = "PLA",
    density_g_cc: float | None = None,
    extra_args: list | None = None,
) -> dict:
    """Slice a real part with the PrusaSlicer CLI, asynchronous — the external-CLI
    upgrade of the analytic slice_estimate: real perimeters, infill patterns,
    `supports`, travel/acceleration, and the slicer's own print-time model.
    Requires a PrusaSlicer install ('apt install prusa-slicer' / the AppImage);
    when absent this returns {ok:false, reason, install} rather than raising.

    Pass a `body` handle (exported to STL in the modeller) or a prepared
    `stl_path`. `infill_fraction` is 0..1 (full infill auto-switches the fill
    pattern — PrusaSlicer's default refuses 100%); `material`/`density_g_cc` set
    the filament density used to turn the sliced volume into grams. With a `body`
    the result also carries the analytic estimate and the `deposited_ratio`
    between them (a 20 mm cube at 100% lands ~1.008 — the skirt).

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, gcode_path, filament_mm, filament_cm3, filament_g, print_time_s,
    print_time_text, layer_count, config (the slicer's echoed settings),
    analytic?, deposited_ratio?}."""
    params = {"layer_height_mm": layer_height_mm,
              "infill_fraction": infill_fraction, "supports": supports,
              "material": material}
    for k, v in (("body", body), ("stl_path", stl_path),
                 ("density_g_cc", density_g_cc), ("extra_args", extra_args)):
        if v is not None:
            params[k] = v
    return _call("slice_gcode_submit", **params)


@mcp.tool()
def mechanism_kinematics(
    mechanism: str = "fourbar",
    crank: float | None = None,
    coupler: float | None = None,
    rocker: float | None = None,
    ground: float | None = None,
    crank_mm: float | None = None,
    conrod_mm: float | None = None,
    wrist_offset_mm: float = 0.0,
    n_links: int | None = None,
    joints: list | None = None,
    config: str = "open",
    n_steps: int | None = None,
    planar: bool = True,
) -> dict:
    """Closed-form planar mechanism kinematics — exact, NO external solver (the static
    pre-check / gate for mechanism_simulate_submit). Pick `mechanism`:

    - 'fourbar': link lengths crank/coupler/rocker/ground -> {mobility_dof (=1),
      grashof: {condition, type, input_crank_fully_rotates, shortest}, reachable,
      n_reached, coupler_path [[x,y]...], reachable_bbox_mm}. `config` 'open'|'crossed'.
    - 'slider_crank': crank_mm/conrod_mm (+ wrist_offset_mm) -> {stroke_mm (exactly 2·R
      in-line, independent of conrod), x_tdc_mm, x_bdc_mm, inline_stroke_exact}.
    - 'gruebler': n_links (incl. ground) + joints ([{type}...]) -> {mobility_dof}.

    Returns the per-mechanism dict above. Raises on an unknown mechanism or a link set
    that cannot close."""
    params = {"mechanism": mechanism, "config": config, "planar": planar,
              "wrist_offset_mm": wrist_offset_mm}
    for k, v in (("crank", crank), ("coupler", coupler), ("rocker", rocker),
                 ("ground", ground), ("crank_mm", crank_mm), ("conrod_mm", conrod_mm),
                 ("n_links", n_links), ("joints", joints), ("n_steps", n_steps)):
        if v is not None:
            params[k] = v
    return _call("mechanism_kinematics", **params)


@mcp.tool()
def mechanism_simulate_submit(
    links: list,
    drivers: list | None = None,
    duration_s: float = 1.0,
    dt_s: float = 1.0 / 240.0,
    gravity: list | None = None,
    obstacles: list | None = None,
    base: dict | None = None,
    loop_closures: list | None = None,
    gears: list | None = None,
) -> dict:
    """Simulate a rigid-link mechanism's DYNAMICS with PyBullet, asynchronously (the
    MBD family; requires the `mbd` extra — `pip install 'driftpin[mbd]'`). Use
    mechanism_kinematics first for the exact closed-form gates (DOF, Grashof, stroke).

    `links` is a tree: [{name, box_mm:[lx,ly,lz], mass_g, parent (link index, −1 =
    fixed base), joint_type ('revolute'|'prismatic'|'fixed'), joint_axis:[x,y,z],
    joint_at_mm:[x,y,z] (in the parent frame), com_mm:[x,y,z]}]. `drivers`:
    [{link, rate_dps}] (revolute) or [{link, rate_mm_s}] (prismatic). Optional
    `obstacles` ([{box_mm, at_mm}]) for through-motion contact, `base`, `gravity`
    (m/s², default [0,0,−9.81]), `dt_s`, `duration_s`. `gears`
    ([{link_a, link_b, ratio, axis?, max_force?}]) couples two revolute links by
    ω_b = −ω_a/ratio (ratio = Nb/Na for an Na/Nb external mesh) — the moving image of
    the gear-train ratio gate.

    Returns immediately. If PyBullet is absent: {ok:false, reason, install,
    mobility_dof, n_links}. Otherwise {job_id, status, cache_hit, mobility_dof}; poll
    job_result(job_id) for {trajectories, orientations (per-link world quaternion,
    sampled with trajectories), max_torques, collisions_through_motion (with the sim
    time of each contact), reachable_envelope {bbox_mm}, mobility_dof}."""
    params = {"links": links, "duration_s": duration_s, "dt_s": dt_s}
    for k, v in (("drivers", drivers), ("gravity", gravity), ("obstacles", obstacles),
                 ("base", base), ("loop_closures", loop_closures), ("gears", gears)):
        if v is not None:
            params[k] = v
    return _call("mechanism_simulate_submit", **params)


@mcp.tool()
def topology_optimize_submit(
    nelx: int = 60,
    nely: int = 20,
    nelz: int | None = None,
    keep_fraction: float = 0.4,
    penal: float = 3.0,
    rmin: float = 1.5,
    max_iter: int = 60,
    tol: float = 0.01,
    load: list | None = None,
    fixed_dofs: list | None = None,
    loads: list | None = None,
    fixed_nodes: list | None = None,
    keep_out: list | None = None,
    keep_in: list | None = None,
) -> dict:
    """Minimum-compliance topology optimization (in-house SIMP; NO external solver),
    asynchronous because each iteration solves an FE system. Optimizes a 2-D
    rectangular design domain (`nelx`×`nely` unit cells) — or, when `nelz` is set, a
    3-D `nelx`×`nely`×`nelz` grid of trilinear hexahedra — to the stiffest layout that
    holds Σdensity = `keep_fraction` (the Optimality-Criteria update holds it exactly);
    `penal` is the SIMP penalty (≈3), `rmin` the cone filter radius. Default BCs
    (both): the whole left face clamped + a unit downward load at the right-face
    centre. 2-D overrides: `fixed_dofs` / `load`=[dof_index, value]. 3-D overrides:
    `loads`=[[i,j,k,axis,value],...] point loads at node grid coords (axis 'x'|'y'|'z'),
    `fixed_nodes`=[[i,j,k],...] clamped nodes, and `keep_out`/`keep_in` lists of
    half-open element-index boxes [i0,i1,j0,j1,k0,k1] forced void / forced solid
    (keep-out regions and must-keep pads).

    Returns immediately {job_id, status, cache_hit}; poll job_result for {density
    (2-D: nely×nelx grid; 3-D: nelz×nely×nelx voxel field, density[k][j][i] with j=0
    at the bottom — this IS geometry), mass_fraction (==keep_fraction), compliance,
    compliance_initial, iterations, converged, gray_fraction, solver (3-D: which
    linear-solve backend ran)}. Threshold + voxel→solid back in the modeller with
    `topology_to_solid`, then gate with mass_properties (mass ≤
    keep_fraction·original) and interference_check vs keep-outs."""
    params = {"nelx": nelx, "nely": nely, "keep_fraction": keep_fraction,
              "penal": penal, "rmin": rmin, "max_iter": max_iter, "tol": tol}
    for k, v in (("nelz", nelz), ("load", load), ("fixed_dofs", fixed_dofs),
                 ("loads", loads), ("fixed_nodes", fixed_nodes),
                 ("keep_out", keep_out), ("keep_in", keep_in)):
        if v is not None:
            params[k] = v
    return _call("topology_optimize_submit", **params)


@mcp.tool()
def topology_to_solid(
    density: list,
    threshold: float = 0.5,
    cell_mm: float = 1.0,
    thickness_mm: float | None = None,
    name: str | None = None,
    placement: list | None = None,
) -> dict:
    """Reconstruct a FreeCAD solid from a topology-optimization density field — the
    modeller-side close of the loop opened by `topology_optimize_submit`, whose
    `density` this consumes. 2-D (nely×nelx grid): thresholds (a cell is solid when
    density ≥ `threshold`), run-length-merges each row into solid spans, tiles each
    span as a `cell_mm` box extruded `thickness_mm` in Z (row 0 at the top). 3-D (a
    nelz×nely×nelx voxel field from the `nelz` mode): greedy-merges solid voxels into
    maximal boxes at (i·cx, j·cy, k·cz) — j=0 at the bottom, `thickness_mm` ignored.
    Fuses into one static Part::Feature. `cell_mm` is a scalar or [cx, cy(, cz)] mm;
    `thickness_mm` defaults to the smaller cell edge; `placement` is an optional
    [x, y, z] mm origin offset; `name` names the object. Runs synchronously (it builds
    geometry — no jobs.py poll).

    Returns {handle, name, volume (mm³), solid_cells, total_cells, mass_fraction
    (== solid_cells/total_cells — must be ≤ keep_fraction within one cell), n_solids
    (disjoint bodies; >1 means a split load path), threshold, nelx, nely, nelz (None
    for 2-D), bbox_mm}. Gate it with mass_properties (mass ≤ keep_fraction·original)
    and interference_check against keep-out regions, per SIMULATION_EXAMPLES §5."""
    params = {"density": density, "threshold": threshold, "cell_mm": cell_mm}
    if thickness_mm is not None:
        params["thickness_mm"] = thickness_mm
    if name is not None:
        params["name"] = name
    if placement is not None:
        params["placement"] = placement
    return _call("topology_to_solid", **params)


@mcp.tool()
def async_demo_submit(duration_s: float = 0.5, value: float = 1.0) -> dict:
    """Reference async long-solve: launch a job that runs OFF the MCP channel and
    return immediately, so a multi-minute solve never blocks the worker. (This demo
    just computes for duration_s then returns a deterministic result; a real
    FEM/CFD solve plugs into the same facility — see driftpin/jobs.py.) Returns
    {job_id, status, cache_hit}; poll with job_status / job_result. A re-submit with
    identical (duration_s, value) is a content-hash cache hit (no recompute)."""
    return _call("async_demo_submit", duration_s=duration_s, value=value)


@mcp.tool()
def job_status(job_id: str) -> dict:
    """Lightweight poll of any async job (from an *_submit tool). Returns {job_id,
    kind, status: 'running'|'done'|'failed', elapsed_s, meta} (+ error when failed)
    WITHOUT the result payload — cheap to call in a loop."""
    return _call("job_status", job_id=job_id)


@mcp.tool()
def job_result(job_id: str, discard: bool = False) -> dict:
    """Fetch an async job's outcome. Returns {job_id, kind, status, elapsed_s,
    result (when done) | error (when failed)}; while running neither is set.
    discard=True frees a terminal job (and its cache entry) once you have it."""
    return _call("job_result", job_id=job_id, discard=discard)


@mcp.tool()
def job_list() -> dict:
    """List every async job this worker session. Returns {count, jobs:[{job_id,
    kind, status, elapsed_s}]} in submit order."""
    return _call("job_list")


def run():
    mcp.run()


if __name__ == "__main__":
    run()

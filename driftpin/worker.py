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


def _set_visibility(obj, visible):
    """Set an object's persistent Visibility (the App-level bool that's
    serialized into Document.xml). The GUI reads it on re-open to decide
    whether the object renders. Returns True if the flag was applied."""
    if hasattr(obj, "Visibility"):
        try:
            obj.Visibility = bool(visible)
            return True
        except Exception:
            return False
    return False


# Producer-input properties: if object A holds object B in one of these, B's
# shape has been consumed into A and B should be hidden so re-opening the doc
# doesn't double-render the input alongside the result.
_INPUT_SINGLE_PROPS = ("Base", "Tool", "BaseFeature", "Profile", "Spine", "AuxiliarySpine")
_INPUT_LIST_PROPS = ("Sections", "Originals")


def _collect_consumed(doc):
    """Walk doc.Objects and return the set of Names that have been subsumed
    as a producer-input by another object."""
    consumed = set()
    for obj in doc.Objects:
        for prop in _INPUT_SINGLE_PROPS:
            if not hasattr(obj, prop):
                continue
            ref = getattr(obj, prop, None)
            if ref is None:
                continue
            # PropertyLinkSub returns (object, [subnames]); plain PropertyLink
            # returns the object directly.
            if isinstance(ref, tuple) and ref and hasattr(ref[0], "Name"):
                consumed.add(ref[0].Name)
            elif hasattr(ref, "Name"):
                consumed.add(ref.Name)
        for prop in _INPUT_LIST_PROPS:
            if not hasattr(obj, prop):
                continue
            for item in getattr(obj, prop, None) or []:
                if isinstance(item, tuple) and item and hasattr(item[0], "Name"):
                    consumed.add(item[0].Name)
                elif hasattr(item, "Name"):
                    consumed.add(item.Name)
        # PartDesign Body: the Body's own shape mirrors its Tip feature's shape,
        # so every feature inside the Body's Group is already rendered by the
        # Body itself. Letting them stay visible double-renders.
        if obj.isDerivedFrom("PartDesign::Body"):
            for feat in obj.Group:
                if hasattr(feat, "Name"):
                    consumed.add(feat.Name)
    return consumed


def _apply_visibility_hygiene(doc):
    """Hide every object that has been consumed as a producer-input. Final-
    stage results (top-level booleans, the Body itself, standalone primitives
    that aren't input to anything) keep Visibility=True. Idempotent."""
    for name in _collect_consumed(doc):
        obj = doc.getObject(name)
        if obj is not None:
            _set_visibility(obj, False)


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


@handler("add_gear")
def _h_add_gear(p):
    """Involute spur gear via FreeCAD's core InvoluteGearFeature, extruded to a
    solid. teeth, module (mm), height (mm), pressure_angle (deg, default 20),
    external (bool). Returns the solid's handle plus pitch/tip/root radii so a
    caller can space meshing gears (axes pitch_a + pitch_b apart)."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")
    import InvoluteGearFeature as _IGF
    teeth = int(p["teeth"])
    if teeth < 3:
        raise ValueError("teeth must be >= 3")
    module = float(p["module"])
    height = float(p.get("height", p.get("h", 6.0)))
    pressure = float(p.get("pressure_angle", 20.0))
    external = bool(p.get("external", True))

    g = _IGF.makeInvoluteGear("_gear_profile_tmp")
    g.NumberOfTeeth = teeth
    g.Modules = "%g mm" % module
    g.PressureAngle = "%g deg" % pressure
    g.ExternalGear = external
    doc.recompute()
    # 2D involute profile -> extrude to a solid; the profile face normal is flipped,
    # so a raw extrude yields a negative-volume (inside-out) solid — reverse it.
    f = g.Shape.Faces[0] if g.Shape.Faces else Part.Face(g.Shape.Wires[0])
    prism = f.extrude(App.Vector(0, 0, height))
    sol = prism.Solids[0] if prism.Solids else Part.Solid(prism)
    if sol.Volume < 0:
        sol = sol.reversed()
        sol = sol.Solids[0] if sol.Solids else Part.Solid(sol)
    doc.removeObject(g.Name)  # drop the parametric helper; keep a static solid

    obj = doc.addObject("Part::Feature", p.get("name", "Gear"))
    obj.Shape = sol
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("gear", obj)
    rp = module * teeth / 2.0
    return {"handle": h, "name": obj.Name, "volume": obj.Shape.Volume,
            "pitch_radius": round(rp, 4), "tip_radius": round(rp + module, 4),
            "root_radius": round(rp - 1.25 * module, 4),
            "teeth": teeth, "module": module, "external": external}


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


@handler("add_rack")
def _h_add_rack(p):
    """Linear gear rack: a straight-flanked tooth rail (a gear of infinite
    radius). All lengths mm, angles deg. Standard full-depth tooth form:
    pitch p = pi*module, addendum = module, dedendum = 1.25*module, so tooth
    height = 2.25*module; flanks are straight at pressure_angle from vertical.
    The profile is drawn in the XZ plane (root line at z=0, base band of
    thickness `width` below it, teeth rising to z=2.25*module) and extruded
    along +Y by `height` to a solid (face normal is flipped, so a negative
    -volume solid is reversed, mirroring add_gear). Returns the solid's handle
    plus the mating numbers: `pitch` (mm/tooth, must equal a meshing gear's
    module*pi), `module`, `teeth`, `tooth_height`, and `length` (= teeth*pi
    *module) so a coordinator can size and place the rail."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")
    teeth = int(p["teeth"])
    if teeth < 1:
        raise ValueError("teeth must be >= 1")
    module = float(p["module"])
    if module <= 0:
        raise ValueError("module must be > 0")
    height = float(p.get("height", 6.0))
    width = float(p.get("width", 10.0))
    if height <= 0 or width <= 0:
        raise ValueError("height and width must be > 0")
    pressure = float(p.get("pressure_angle", 20.0))
    if not 0 < pressure < 45:
        raise ValueError("pressure_angle must be between 0 and 45 deg")

    import math
    pitch = math.pi * module            # circular pitch (mm/tooth)
    add = module                        # addendum (above root line + dedendum)
    ded = 1.25 * module                 # dedendum
    tooth_h = add + ded                 # full tooth height = 2.25*module
    pa = math.radians(pressure)
    # half-widths of a tooth measured from its centerline:
    half_tip = pitch / 4.0 - add * math.tan(pa)         # at the tip (z=tooth_h)
    half_root = pitch / 4.0 + ded * math.tan(pa)        # at the root line (z=0)
    if half_tip <= 0:
        raise ValueError(
            "tooth tip degenerate for this module/pressure_angle "
            "(pitch/4 <= module*tan(pressure_angle)); lower pressure_angle")
    length = teeth * pitch

    # Walk the profile CCW: up the left edge, along the root line with a
    # trapezoidal tooth per pitch period, down the right edge, close the base.
    pts = [App.Vector(0, 0, -width), App.Vector(0, 0, 0)]
    for i in range(teeth):
        center = i * pitch + pitch / 2.0
        pts.append(App.Vector(center - half_root, 0, 0))
        pts.append(App.Vector(center - half_tip, 0, tooth_h))
        pts.append(App.Vector(center + half_tip, 0, tooth_h))
        pts.append(App.Vector(center + half_root, 0, 0))
    pts.append(App.Vector(length, 0, 0))
    pts.append(App.Vector(length, 0, -width))
    pts.append(App.Vector(0, 0, -width))

    face = Part.Face(Part.makePolygon(pts))
    prism = face.extrude(App.Vector(0, height, 0))
    sol = prism.Solids[0] if prism.Solids else Part.Solid(prism)
    if sol.Volume < 0:                  # flipped profile normal -> inside-out solid
        sol = sol.reversed()
        sol = sol.Solids[0] if sol.Solids else Part.Solid(sol)

    obj = doc.addObject("Part::Feature", p.get("name", "Rack"))
    obj.Shape = sol
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("rack", obj)
    return {"handle": h, "name": obj.Name, "volume": obj.Shape.Volume,
            "pitch": round(pitch, 4), "module": module, "teeth": teeth,
            "tooth_height": round(tooth_h, 4), "length": round(length, 4)}


@handler("add_sprocket")
def _h_add_sprocket(p):
    """Roller-chain sprocket (ISO 606 / ANSI), built as a static solid. teeth
    (tooth count, >= 3), chain_pitch (mm, chain link pitch e.g. 12.7 for #40),
    roller_diameter (mm), height (mm, plate thickness). Pitch diameter
    PD = chain_pitch / sin(pi/teeth); tip/outer radius ~= PD/2 + chain_pitch*0.3.
    Roller seats are circular pockets (radius roller_diameter/2 * 1.05) spaced one
    per tooth on the pitch circle and cut clean through the plate (a fit/visual
    approximation of the true ISO tooth form). placement: optional [x,y,z] mm.
    Returns the solid handle plus the mating numbers: pitch_diameter, chain_pitch,
    teeth, tip_radius, bore (0). A chain of the same chain_pitch wraps it; the
    centre distance between two sprockets derives from their pitch_diameters."""
    import math
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")
    teeth = int(p["teeth"])
    if teeth < 3:
        raise ValueError(f"teeth must be >= 3, got {teeth}")
    chain_pitch = float(p["chain_pitch"])
    if chain_pitch <= 0:
        raise ValueError(f"chain_pitch must be > 0, got {chain_pitch}")
    roller_diameter = float(p["roller_diameter"])
    if roller_diameter <= 0:
        raise ValueError(f"roller_diameter must be > 0, got {roller_diameter}")
    height = float(p.get("height", p.get("h", 6.0)))
    if height <= 0:
        raise ValueError(f"height must be > 0, got {height}")

    # ISO 606 pitch diameter; pitch radius is where roller-seat centres sit.
    pd = chain_pitch / math.sin(math.pi / teeth)
    pitch_radius = pd / 2.0
    tip_radius = pitch_radius + chain_pitch * 0.3   # tip/outer radius approximation
    seat_radius = roller_diameter / 2.0 * 1.05      # 5% clearance for roller fit

    disc = Part.makeCylinder(tip_radius, height)
    body = disc
    for i in range(teeth):
        ang = 2.0 * math.pi * i / teeth
        cx = pitch_radius * math.cos(ang)
        cy = pitch_radius * math.sin(ang)
        seat = Part.makeCylinder(seat_radius, height, App.Vector(cx, cy, 0))
        body = body.cut(seat)
    sol = body.Solids[0] if body.Solids else Part.Solid(body)

    obj = doc.addObject("Part::Feature", p.get("name", "Sprocket"))
    obj.Shape = sol
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("sprocket", obj)
    return {"handle": h, "name": obj.Name, "volume": obj.Shape.Volume,
            "pitch_diameter": round(pd, 4), "chain_pitch": chain_pitch,
            "teeth": teeth, "tip_radius": round(tip_radius, 4), "bore": 0}


@handler("add_pulley")
def _h_add_pulley(p):
    """Timing-belt (or V) pulley as a static solid. Pitch diameter
    PD = belt_pitch * teeth / pi. Builds a belt-face cylinder of radius PD/2 and
    axial length = width (mm), cuts `teeth` axial tooth grooves on the pitch
    circle (groove ~= belt_pitch*0.5 wide, ~belt_pitch*0.4 deep), and — when
    flanged — fuses two thin guide discs (radius PD/2 + 2*belt_pitch) at each
    end. The pulley axis is +Z; the toothed face spans z in [0, width].

    Units: mm throughout. Params: teeth (int >= 6), belt_pitch (mm/tooth),
    width (mm belt face), flanged (bool, default True), height (mm; overrides
    width if given), placement ([x,y,z] mm), name (str). Returns the solid's
    handle plus pitch_diameter (the mating number: centre distance with a mating
    pulley + belt length derives from the two PDs), belt_pitch, teeth, width,
    flanged, and volume."""
    doc = _active_doc()
    teeth = int(p["teeth"])
    if teeth < 6:
        raise ValueError("teeth must be >= 6 for a timing pulley")
    belt_pitch = float(p["belt_pitch"])
    if belt_pitch <= 0:
        raise ValueError("belt_pitch must be > 0 (mm/tooth)")
    # height overrides width when both supplied; default height = width.
    height = p.get("height")
    width = float(height) if height is not None else float(p["width"])
    if width <= 0:
        raise ValueError("width (belt face length) must be > 0 mm")
    flanged = bool(p.get("flanged", True))

    import math
    PD = belt_pitch * teeth / math.pi
    R = PD / 2.0
    # Belt-face cylinder, axis +Z, base at origin.
    sol = Part.makeCylinder(R, width)
    # Tooth grooves: axial cylindrical pockets centred on the pitch circle, one
    # per tooth, full belt-face length. Cylinder cutters approximate the groove
    # form (same polar-cut technique as the sprocket) — adequate for fit/visual.
    groove_w = belt_pitch * 0.5
    cutters = []
    for i in range(teeth):
        ang = 2.0 * math.pi * i / teeth
        cx = R * math.cos(ang)
        cy = R * math.sin(ang)
        cutters.append(
            Part.makeCylinder(groove_w / 2.0, width,
                              App.Vector(cx, cy, 0), App.Vector(0, 0, 1)))
    allcut = cutters[0]
    for c in cutters[1:]:
        allcut = allcut.fuse(c)
    sol = sol.cut(allcut)
    if flanged:
        # Thin guide discs overhanging the belt face at each end.
        flange_r = R + 2.0 * belt_pitch
        flange_t = max(0.8, belt_pitch * 0.4)
        f1 = Part.makeCylinder(flange_r, flange_t,
                               App.Vector(0, 0, -flange_t), App.Vector(0, 0, 1))
        f2 = Part.makeCylinder(flange_r, flange_t,
                               App.Vector(0, 0, width), App.Vector(0, 0, 1))
        sol = sol.fuse(f1).fuse(f2)

    obj = doc.addObject("Part::Feature", p.get("name", "Pulley"))
    obj.Shape = sol
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("pulley", obj)
    return {"handle": h, "name": obj.Name, "volume": obj.Shape.Volume,
            "pitch_diameter": round(PD, 4), "belt_pitch": belt_pitch,
            "teeth": teeth, "width": width, "flanged": flanged}


@handler("add_spring")
def _h_add_spring(p):
    """Helical compression spring: a circular wire-section swept along a helix.
    Units: all lengths mm. wire_diameter (d), outer_diameter (OD), free_length,
    coils (turns, may be fractional). kind: 'compression' (only mode for v1).
    Spring rate computed for steel (G = 79.3 GPa = 79300 MPa) via
    k = G*d^4 / (8*D^3*Na), D = mean coil diameter, Na = active coils (= coils),
    yielding k in N/mm. Returns the solid's handle plus the mating/reference
    dimensions {mean_diameter, free_length, coils, solid_height, spring_rate_n_per_mm}.
    """
    doc = _active_doc()
    wire_diameter = float(p["wire_diameter"])
    outer_diameter = float(p["outer_diameter"])
    free_length = float(p["free_length"])
    coils = float(p["coils"])
    kind = str(p.get("kind", "compression"))
    if wire_diameter <= 0:
        raise ValueError("wire_diameter must be > 0")
    if outer_diameter <= 0:
        raise ValueError("outer_diameter must be > 0")
    if outer_diameter <= wire_diameter:
        raise ValueError("outer_diameter must be > wire_diameter (no room for a coil)")
    if free_length <= 0:
        raise ValueError("free_length must be > 0")
    if coils <= 0:
        raise ValueError("coils must be > 0")

    # Mean coil radius: centreline of the wire sits half a wire-diameter inside the OD.
    Rm = (outer_diameter - wire_diameter) / 2.0
    pitch = free_length / coils
    helix = Part.makeHelix(pitch, free_length, Rm)

    # Profile must lie in the plane normal to the helix's start tangent, else the
    # swept section is skewed; makePipeShell with is_frenet keeps it normal along.
    e0 = helix.Edges[0]
    p0 = e0.valueAt(e0.FirstParameter)
    t0 = e0.tangentAt(e0.FirstParameter)
    circ = Part.Circle(App.Vector(p0), App.Vector(t0), wire_diameter / 2.0)
    profile = Part.Wire(circ.toShape())
    # makePipeShell(profiles, make_solid=True, is_frenet=True) -> closed swept solid.
    sol = Part.Wire(helix.Edges).makePipeShell([profile], True, True)
    if not sol.isValid() or sol.Volume <= 0:
        raise RuntimeError("spring sweep produced an invalid/empty solid; check dimensions")
    # TODO: kind=="compression" could flatten/grind the end coils (squared ends);
    # v1 leaves open ends — solid_height below still uses the closed-coil estimate.

    obj = doc.addObject("Part::Feature", p.get("name", "Spring"))
    obj.Shape = sol
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("spring", obj)

    # Spring rate, steel: G in MPa, d/D in mm -> k in N/mm.
    G = 79300.0
    D = Rm * 2.0
    k = G * wire_diameter ** 4 / (8.0 * D ** 3 * coils)
    return {"handle": h, "name": obj.Name, "volume": round(obj.Shape.Volume, 4),
            "mean_diameter": round(D, 4), "free_length": free_length, "coils": coils,
            "kind": kind, "solid_height": round(coils * wire_diameter, 4),
            "spring_rate_n_per_mm": round(k, 4)}


@handler("add_fastener")
def _h_add_fastener(p):
    """Build a standard ISO metric fastener as a static Part solid from
    primitives. kind in {socket_head_cap_screw, hex_bolt, hex_nut, washer};
    size in {M3,M4,M5,M6,M8,M10,M12}; length = shank length (mm, screws/bolts
    only, required for them). All dims in mm. Threads are cosmetic (plain
    shank). Returns the solid's handle plus the mating numbers a coordinator
    needs: major_diameter (drill the through-hole this + clearance), pitch,
    head_diameter / head_height (counterbore size), and length."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")

    # ISO metric reference table (representative ISO 4762 socket-head /
    # ISO 4032 nut / ISO 7089 washer values):
    # {size: (major_dia, pitch, head_dia, head_height, nut_width_af, nut_height,
    #         washer_od, washer_thk)}
    _FASTENER = {
        "M3":  (3.0,  0.5,  5.5,  3.0,  5.5,  2.4,  7.0,  0.5),
        "M4":  (4.0,  0.7,  7.0,  4.0,  7.0,  3.2,  9.0,  0.8),
        "M5":  (5.0,  0.8,  8.5,  5.0,  8.0,  4.7,  10.0, 1.0),
        "M6":  (6.0,  1.0,  10.0, 6.0,  10.0, 5.2,  12.0, 1.6),
        "M8":  (8.0,  1.25, 13.0, 8.0,  13.0, 6.8,  16.0, 1.6),
        "M10": (10.0, 1.5,  16.0, 10.0, 16.0, 8.4,  20.0, 2.0),
        "M12": (12.0, 1.75, 18.0, 12.0, 18.0, 10.8, 24.0, 2.5),
    }
    _KINDS = ("socket_head_cap_screw", "hex_bolt", "hex_nut", "washer")

    kind = str(p["kind"])
    if kind not in _KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {list(_KINDS)}")
    size = str(p["size"]).upper()
    if size not in _FASTENER:
        raise ValueError(f"unknown size {size!r}; expected one of {list(_FASTENER)}")
    major, pitch, head_dia, head_h, nut_af, nut_h, washer_od, washer_thk = _FASTENER[size]

    length = None
    if kind in ("socket_head_cap_screw", "hex_bolt"):
        if p.get("length") is None:
            raise ValueError(f"length (mm) is required for kind={kind!r}")
        length = float(p["length"])
        if length <= 0:
            raise ValueError("length must be > 0")

    def _hex_prism(across_flats, height, z0=0.0):
        # Regular hexagon with the given across-flats dimension (flat-to-flat),
        # circumradius = across_flats / sqrt(3); oriented flats parallel to X.
        import math
        rr = across_flats / math.sqrt(3.0)
        pts = [App.Vector(rr * math.cos(math.radians(60 * i + 30)),
                          rr * math.sin(math.radians(60 * i + 30)), z0)
               for i in range(6)]
        pts.append(pts[0])
        return Part.Face(Part.makePolygon(pts)).extrude(App.Vector(0, 0, height))

    if kind == "socket_head_cap_screw":
        # Cylindrical head (z: 0..head_h) + shank below it (z: -length..0).
        head = Part.makeCylinder(head_dia / 2.0, head_h)
        shank = Part.makeCylinder(major / 2.0, length, App.Vector(0, 0, -length))
        body = head.fuse(shank)
        # Cosmetic hex socket sunk into the head top.
        sock_af = 0.6 * head_dia
        sock_depth = 0.6 * head_h
        socket = _hex_prism(sock_af, sock_depth + 1.0, z0=head_h - sock_depth)
        sol = body.cut(socket).removeSplitter()
    elif kind == "hex_bolt":
        # Hex head (across-flats = head_dia) + plain shank below.
        head = _hex_prism(head_dia, head_h)
        shank = Part.makeCylinder(major / 2.0, length, App.Vector(0, 0, -length))
        sol = head.fuse(shank)
    elif kind == "hex_nut":
        # Hex prism with an axial clearance hole of the major diameter.
        prism = _hex_prism(nut_af, nut_h)
        sol = prism.cut(Part.makeCylinder(major / 2.0, nut_h))
    else:  # washer
        sol = (Part.makeCylinder(washer_od / 2.0, washer_thk)
               .cut(Part.makeCylinder(major / 2.0, washer_thk)))

    if not sol.Solids:
        raise RuntimeError(f"failed to build a solid for {kind} {size}")
    sol = sol.Solids[0] if len(sol.Solids) == 1 else sol

    obj = doc.addObject("Part::Feature", p.get("name") or kind.replace("_", " ").title().replace(" ", ""))
    obj.Shape = sol
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()

    h = _register("fastener", obj)
    out = {"handle": h, "name": obj.Name, "kind": kind, "size": size,
           "major_diameter": major, "pitch": pitch, "volume": obj.Shape.Volume}
    if kind in ("socket_head_cap_screw", "hex_bolt"):
        out["length"] = length
        out["head_diameter"] = head_dia
        out["head_height"] = head_h
        out["model_thread"] = False
    elif kind == "hex_nut":
        out["head_diameter"] = nut_af  # across-flats wrench size
        out["head_height"] = nut_h
    else:  # washer
        out["head_diameter"] = washer_od  # outer diameter
        out["head_height"] = washer_thk
    return out


@handler("add_bearing")
def _h_add_bearing(p):
    """Deep-groove ball bearing as an assembly *envelope* solid: an annular ring
    (OD cylinder minus bore cylinder), `width` long, axis along +Z. No
    balls/races — the coordinator only needs the fit envelope and bore shoulder.

    Dimensions come from either `designation` (looked up in a small metric table)
    or explicit `bore`/`outer_diameter`/`width`. Explicit values override a
    designation when both are supplied. All lengths mm.

    Returns {handle, name, designation, bore, outer_diameter, width, volume}.
    `bore` sizes the shaft, `outer_diameter` sizes the housing, `width` sets the
    shoulder spacing."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")

    # common metric deep-groove series: {designation: (bore, OD, width)} mm
    _BEARING = {
        "608":  (8.0, 22.0, 7.0),    # skateboard
        "623":  (3.0, 10.0, 4.0),
        "624":  (4.0, 13.0, 5.0),
        "625":  (5.0, 16.0, 5.0),
        "626":  (6.0, 19.0, 6.0),
        "688":  (8.0, 16.0, 5.0),
        "6000": (10.0, 26.0, 8.0),
        "6200": (10.0, 30.0, 9.0),
        "6800": (10.0, 19.0, 5.0),
        "6900": (10.0, 22.0, 6.0),
    }

    designation = p.get("designation")
    bore = p.get("bore")
    outer_diameter = p.get("outer_diameter")
    width = p.get("width")

    if designation is not None:
        designation = str(designation)
        if designation not in _BEARING:
            known = ", ".join(sorted(_BEARING))
            raise ValueError(
                f"unknown bearing designation {designation!r}; known: {known}. "
                f"Alternatively pass explicit bore/outer_diameter/width."
            )
        d_bore, d_od, d_w = _BEARING[designation]
        # explicit dims override table values when supplied
        bore = d_bore if bore is None else bore
        outer_diameter = d_od if outer_diameter is None else outer_diameter
        width = d_w if width is None else width

    if bore is None or outer_diameter is None or width is None:
        known = ", ".join(sorted(_BEARING))
        raise ValueError(
            "specify either a known `designation` or all of "
            "bore/outer_diameter/width. Known designations: " + known
        )

    bore = float(bore)
    outer_diameter = float(outer_diameter)
    width = float(width)
    if bore <= 0:
        raise ValueError("bore must be > 0")
    if width <= 0:
        raise ValueError("width must be > 0")
    if outer_diameter <= bore:
        raise ValueError(
            f"outer_diameter ({outer_diameter}) must be > bore ({bore})"
        )

    # envelope = OD cylinder with the bore cylinder cut out, axis +Z
    outer = Part.makeCylinder(outer_diameter / 2.0, width)
    inner = Part.makeCylinder(bore / 2.0, width)
    ring = outer.cut(inner)
    if ring.isNull() or not ring.isValid() or ring.Volume <= 0:
        raise RuntimeError(
            "bearing envelope produced an invalid/empty solid; check dimensions"
        )

    obj = doc.addObject("Part::Feature", p.get("name", "Bearing"))
    obj.Shape = ring
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("bearing", obj)
    return {"handle": h, "name": obj.Name,
            "designation": designation,
            "bore": round(bore, 4),
            "outer_diameter": round(outer_diameter, 4),
            "width": round(width, 4),
            "volume": obj.Shape.Volume}


@handler("oring_groove")
def _h_oring_groove(p):
    """Compute a static O-ring gland (groove) from the ring's cross-section and,
    optionally, cut the annular groove into a named flat face of a host solid.

    Units: mm throughout. Gland rule-of-thumb for a static seal:
      groove_depth = cross_section * 0.75  (~25% squeeze; clamped to a 20-30%
                     squeeze band so depth stays within w*0.70 .. w*0.80),
      groove_width = cross_section * 1.30  (room for swell/extrusion),
      corner_radius <= 0.4 mm.
    The groove is centred so its INNER diameter == inner_diameter; the groove
    therefore spans radially outward by groove_width:
      groove_inner_diameter = inner_diameter,
      groove_outer_diameter = inner_diameter + 2 * groove_width.

    When cut=True, the annular groove is cut into the face named by `face` (a
    stable f_* tag, a 'FaceN' index, or an int) of the solid `handle`: the cut
    is a ring (outer cylinder minus inner cylinder) of depth=groove_depth driven
    into the solid along the inward face normal, axially centred on the face's
    centre of mass. Requires the face to be planar.

    Returns {groove_depth, groove_width, groove_inner_diameter,
    groove_outer_diameter, squeeze_pct, cross_section} plus, when cut=True,
    {handle, name, volume} for the resulting solid. The host input is hidden."""
    cross_section = float(p["cross_section"])
    if cross_section <= 0:
        raise ValueError("cross_section must be > 0 (O-ring wire diameter in mm)")
    inner_diameter = float(p.get("inner_diameter", 0.0))
    cut = bool(p.get("cut", True))
    gland_type = str(p.get("gland_type", "static_radial"))

    # Static-seal gland: 25% nominal squeeze, clamped to a 20-30% band.
    depth = cross_section * 0.75
    depth = max(cross_section * 0.70, min(cross_section * 0.80, depth))
    width = cross_section * 1.30
    squeeze_pct = (1.0 - depth / cross_section) * 100.0

    result = {
        "groove_depth": round(depth, 4),
        "groove_width": round(width, 4),
        "groove_inner_diameter": round(inner_diameter, 4),
        "groove_outer_diameter": round(inner_diameter + 2.0 * width, 4),
        "squeeze_pct": round(squeeze_pct, 2),
        "cross_section": cross_section,
        "gland_type": gland_type,
    }

    if not cut:
        return result

    if "handle" not in p or p.get("handle") is None:
        raise ValueError("cut=True requires a `handle` for the host solid")
    if inner_diameter <= 0:
        raise ValueError("cut=True requires inner_diameter > 0 (mm)")
    ref = p.get("face")
    if ref is None:
        raise ValueError("cut=True requires `face` (an f_* tag, 'FaceN', or int)")

    doc = _active_doc()
    obj, shape = _shape_of(p["handle"])

    # Resolve the face reference to a 1-based FaceN index (mirror _h_fillet_edges).
    if isinstance(ref, str) and ref.startswith("f_"):
        idx = int(_h_resolve_face({"handle": p["handle"], "tag": ref})["index"][len("Face"):])
    elif isinstance(ref, str) and ref.startswith("Face"):
        idx = int(ref[len("Face"):])
    else:
        idx = int(ref)
    if idx < 1 or idx > len(shape.Faces):
        raise ValueError(f"face index {idx} out of range (1..{len(shape.Faces)})")
    face = shape.Faces[idx - 1]
    if _surface_kind(face) != "planar":
        raise ValueError(f"face {ref!r} is not planar; O-ring groove needs a flat face")

    com = face.CenterOfMass
    normal = _outward_normal(face)
    # Fresh vector pointing INTO the solid; avoid in-place negate of `normal`.
    into = App.Vector(-normal.x, -normal.y, -normal.z)

    r_out = (inner_diameter + 2.0 * width) / 2.0
    r_in = inner_diameter / 2.0
    cyl_out = Part.makeCylinder(r_out, depth, com, into)
    cyl_in = Part.makeCylinder(r_in, depth, com, into)
    ring = cyl_out.cut(cyl_in)
    if ring.Volume <= 0 or not ring.isValid():
        raise RuntimeError("failed to build a valid annular groove tool")
    cut_shape = shape.cut(ring)
    if not cut_shape.isValid():
        raise RuntimeError("O-ring groove cut produced an invalid solid")

    out = doc.addObject("Part::Feature", p.get("name", "ORingGroove"))
    out.Shape = cut_shape
    doc.recompute()
    _set_visibility(obj, False)  # host solid consumed into the grooved result
    h = _register("oring_groove", out)
    result["handle"] = h
    result["name"] = out.Name
    result["volume"] = out.Shape.Volume
    return result


@handler("chamfer_edges")
def _h_chamfer_edges(p):
    """Chamfer specific edges of a shaped (Part) object. Edges are referenced by
    tag (preferred, e_* from list_edges), 'EdgeN' string, or bare 1-based int.
    `size` is the symmetric chamfer leg distance in mm (applied as both
    dist1=dist2). Mirrors fillet_edges, swapping Part::Fillet for Part::Chamfer.
    The base object is hidden (consumed into the chamfer feature). Returns the
    new chamfer feature's handle plus name, resulting Shape volume (mm^3), and
    the resolved 1-based edge indices."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document; call new_document first")
    obj, shape = _shape_of(p["handle"])
    size = float(p.get("size", 1.0))
    if size <= 0:
        raise ValueError(f"size must be > 0 mm, got {size}")

    edges = []
    for ref in p.get("edges", []):
        if isinstance(ref, str) and ref.startswith("e_"):
            r = _h_resolve_edge({"handle": p["handle"], "tag": ref})
            edges.append(int(r["index"][len("Edge"):]))
        elif isinstance(ref, str) and ref.startswith("Edge"):
            edges.append(int(ref[len("Edge"):]))
        else:
            edges.append(int(ref))
    if not edges:
        raise ValueError("edges must be a non-empty list of edge tags/indices")

    chamfer = doc.addObject("Part::Chamfer", p.get("name", "Chamfer"))
    chamfer.Base = obj
    # (edge_idx, dist1, dist2): symmetric chamfer -> both legs == size.
    chamfer.Edges = [(i, size, size) for i in edges]
    doc.recompute()
    _set_visibility(obj, False)
    h = _register("chamfer", chamfer)
    return {"handle": h, "name": chamfer.Name,
            "volume": chamfer.Shape.Volume, "edges": edges}


@handler("shell_solid")
def _h_shell_solid(p):
    """Hollow a raw Part solid into a shell of uniform wall thickness, removing
    the listed faces to leave them as openings. Inputs: handle (a shaped Part
    object), faces (list of face refs to REMOVE — f_* tags, 'FaceN' strings, or
    1-based ints), thickness (wall thickness in mm, positive). The shell is
    offset INWARD (outer dimensions preserved). Produces a new Part::Feature,
    hides the consumed input, and returns {handle, name, volume (mm^3),
    wall_thickness (mm), removed_faces (1-based indices)}."""
    doc = _active_doc()
    obj, shape = _shape_of(p["handle"])

    thickness = float(p.get("thickness", 1.0))
    if thickness <= 0:
        raise ValueError("thickness must be > 0")

    refs = p.get("faces") or []
    if not refs:
        raise ValueError("faces must be a non-empty list of face refs to remove")

    # Resolve each ref to a 1-based face index (mirror _h_fillet_edges' loop).
    indices = []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("f_"):
            idx = int(_h_resolve_face({"handle": p["handle"], "tag": ref})["index"][len("Face"):])
        elif isinstance(ref, str) and ref.startswith("Face"):
            idx = int(ref[len("Face"):])
        else:
            idx = int(ref)
        if idx < 1 or idx > len(shape.Faces):
            raise ValueError(f"face index {idx} out of range 1..{len(shape.Faces)}")
        indices.append(idx)

    faces_to_remove = [shape.Faces[i - 1] for i in indices]
    # Negative offset shells inward; tolerance 1e-3 matches FreeCAD's default.
    hollow = shape.makeThickness(faces_to_remove, -abs(thickness), 1e-3)
    if hollow.isNull() or not hollow.isValid():
        raise RuntimeError(
            "makeThickness produced an invalid shell; thickness may exceed the "
            "solid's local wall room or the removed faces are degenerate"
        )

    out = doc.addObject("Part::Feature", p.get("name", "Shell"))
    out.Shape = hollow
    doc.recompute()
    _set_visibility(obj, False)
    h = _register("shell", out)
    return {
        "handle": h,
        "name": out.Name,
        "volume": out.Shape.Volume,
        "wall_thickness": abs(thickness),
        "removed_faces": indices,
    }


@handler("add_thread")
def _h_add_thread(p):
    """Build a REAL helical ISO-style thread as a static Part solid.

    Units: all lengths mm, angles deg. Parameters:
      diameter: nominal major diameter (mm). For external this is the crest
                diameter; for internal (a tap/insert tool) it is the bore.
      pitch:    thread pitch (mm per turn).
      length:   threaded length along the axis (mm). The thread runs +Z from z=0.
      internal: if True the returned solid is a TAP/cutting-tool sized to the
                bore (fuse it into a bored hole, or cut it, to thread the bore);
                if False it is a finished externally-threaded stud.
      starts:   number of thread starts (>=1); multi-start repeats the helix
                rotated by 360/starts and uses lead = pitch*starts.
      placement: optional [x,y,z] mm translation of the solid's base.

    Geometry: a 60-deg ISO triangular rib (fundamental height H = pitch*0.866,
    truncated to 5H/8) is swept along a Part.makeHelix at the minor radius via
    makePipeShell, then fused onto a core cylinder. The core radius eats 35% into
    the rib root so the OCC fuse is robust (a bare-tangent overlap intermittently
    drops the core operand). If a fuse fails to add material it is retried on a
    splitter-cleaned rib. Returns the solid's handle plus the mating numbers a
    coordinator needs: major_diameter (= diameter), minor_diameter
    (= diameter - 1.0825*pitch, the ISO 60-deg minor), pitch, length, starts,
    internal, volume (mm^3), and modeled (True when a valid swept solid was
    produced; the cosmetic-only fallback is never taken here because the sweep
    validates). Raises ValueError on non-positive dims or a pitch too coarse for
    the diameter (minor radius <= 0)."""
    import math
    doc = _active_doc()
    diameter = float(p["diameter"])
    pitch = float(p["pitch"])
    length = float(p["length"])
    internal = bool(p.get("internal", False))
    starts = int(p.get("starts", 1))
    if diameter <= 0:
        raise ValueError("diameter must be > 0")
    if pitch <= 0:
        raise ValueError("pitch must be > 0")
    if length <= 0:
        raise ValueError("length must be > 0")
    if starts < 1:
        raise ValueError("starts must be >= 1")

    Rmaj = diameter / 2.0
    # ISO 60-deg fundamental triangle height, truncated to the 5H/8 engaged depth.
    H = pitch * math.sqrt(3.0) / 2.0
    depth = 5.0 * H / 8.0
    Rminor = Rmaj - depth
    if Rminor <= 0:
        raise ValueError("pitch too coarse for diameter (minor radius <= 0)")
    # Core radius bites 35% into the rib root: a bare root-tangent overlap makes
    # the OCC fuse intermittently drop the core operand and return only the rib.
    Rcore = Rminor + 0.35 * depth

    solid = Part.makeCylinder(Rcore, length)
    for s in range(starts):
        ang = 360.0 * s / starts
        # lead = pitch*starts so multi-start crests don't collide.
        helix = Part.makeHelix(pitch * starts, length, Rminor)
        if ang:
            helix.rotate(App.Vector(0, 0, 0), App.Vector(0, 0, 1), ang)
        w = Part.Wire(helix.Edges)
        e0 = w.Edges[0]
        p0 = e0.valueAt(e0.FirstParameter)
        # Triangular profile in the plane of the start point: base spans one
        # pitch axially, apex points radially outward by the engaged depth.
        radial = App.Vector(p0.x, p0.y, 0)
        radial.normalize()
        axial = App.Vector(0, 0, 1)
        half = pitch / 2.0
        a = App.Vector(p0) + axial * half
        b = App.Vector(p0) - axial * half
        apex = App.Vector(p0) + radial * (depth * 1.05)
        prof = Part.makePolygon([a, apex, b, a])
        # makePipeShell(profiles, make_solid=True, is_frenet=True): keeps the
        # section normal to the helix tangent so the rib doesn't skew/self-cross.
        rib = w.makePipeShell([prof], True, True)
        fused = solid.fuse(rib)
        if fused.isValid() and fused.Volume > solid.Volume:
            solid = fused
        else:
            solid = solid.fuse(rib.removeSplitter())
    try:
        solid = solid.removeSplitter()
    except Exception:
        pass

    modeled = bool(solid.isValid() and solid.Volume > 0)
    if not modeled:
        # Cosmetic fallback: the swept solid is unusable — emit the bare core
        # cylinder tagged with the thread spec rather than a broken shape.
        solid = Part.makeCylinder(Rminor if internal else Rmaj, length)

    obj = doc.addObject("Part::Feature", p.get("name", "Thread"))
    obj.Shape = solid
    placement = p.get("placement")
    if placement:
        obj.Placement.Base = App.Vector(*placement)
    doc.recompute()
    h = _register("thread", obj)

    minor_diameter = diameter - 1.0825 * pitch
    return {"handle": h, "name": obj.Name, "volume": round(obj.Shape.Volume, 4),
            "major_diameter": round(diameter, 4), "minor_diameter": round(minor_diameter, 4),
            "pitch": pitch, "length": length, "starts": starts,
            "internal": internal, "modeled": modeled}


@handler("engrave_text")
def _h_engrave_text(p):
    """Engrave (cut) or emboss (add) extruded text onto a planar face of a solid.

    Units: mm. text is rendered as a Draft ShapeString in a system TrueType font,
    extruded by `depth` mm, oriented so its plane lies on the resolved face and
    centred on the face centroid (with an optional in-face [u, v] mm offset), then
    booleaned into the host: mode='engrave' cuts the text below the surface,
    mode='emboss' fuses it standing proud of the surface. The host solid and the
    transient text tool are hidden; the Draft ShapeString helper is dropped after
    extrusion (a static solid is kept).

    Params: handle (host solid), face (f_* tag / 'FaceN' / int, must be planar),
    text (non-empty str), size (cap height mm, default 5.0), depth (mm, default
    0.5), mode ('engrave'|'emboss', default 'engrave'), position (optional [u, v]
    mm in-face offset), font (optional path to a .ttf/.ttc; auto-probed if omitted),
    name (label, default 'Text').

    Returns {handle, name, volume (mm^3 of the result solid), text, mode, depth}.
    Raises RuntimeError if no usable font is found and none was supplied."""
    import os
    import Draft
    doc = _active_doc()
    obj, shape = _shape_of(p["handle"])

    text = str(p.get("text", "")).strip()
    if not text:
        raise ValueError("text must be a non-empty string")
    size = float(p.get("size", 5.0))
    if size <= 0:
        raise ValueError("size must be > 0 (mm)")
    depth = float(p.get("depth", 0.5))
    if depth <= 0:
        raise ValueError("depth must be > 0 (mm)")
    mode = str(p.get("mode", "engrave")).lower()
    if mode not in ("engrave", "emboss"):
        raise ValueError("mode must be 'engrave' (cut) or 'emboss' (add)")

    # FontFile must exist on disk headless; probe common macOS locations.
    font = p.get("font")
    if not font:
        for cand in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                     "/Library/Fonts/Arial.ttf",
                     "/System/Library/Fonts/Helvetica.ttc"):
            if os.path.isfile(cand):
                font = cand
                break
    if not font or not os.path.isfile(font):
        raise RuntimeError(
            "no usable font file found; pass font=<path to a .ttf/.ttc> "
            "(probed Arial/Helvetica under /System/Library/Fonts and /Library/Fonts)"
        )

    # Resolve the face reference to a 1-based FaceN index (mirror _h_oring_groove).
    ref = p.get("face")
    if ref is None:
        raise ValueError("face required (an f_* tag, 'FaceN', or int)")
    if isinstance(ref, str) and ref.startswith("f_"):
        idx = int(_h_resolve_face({"handle": p["handle"], "tag": ref})["index"][len("Face"):])
    elif isinstance(ref, str) and ref.startswith("Face"):
        idx = int(ref[len("Face"):])
    else:
        idx = int(ref)
    if idx < 1 or idx > len(shape.Faces):
        raise ValueError(f"face index {idx} out of range (1..{len(shape.Faces)})")
    face = shape.Faces[idx - 1]
    if _surface_kind(face) != "planar":
        raise ValueError(f"face {ref!r} is not planar; engrave_text needs a flat face")

    com = face.CenterOfMass
    normal = _outward_normal(face)

    # Draft ShapeString lies flat in the local XY plane (text along +X). Extrude
    # along local +Z to a solid, then drop the parametric helper (keep a static solid).
    ss = Draft.make_shapestring(String=text, FontFile=font, Size=size)
    doc.recompute()
    ss_name = ss.Name
    tsolid = ss.Shape.extrude(App.Vector(0, 0, depth))
    doc.removeObject(ss_name)
    doc.recompute()
    if not tsolid.isValid() or tsolid.Volume <= 0:
        raise RuntimeError(f"text {text!r} produced no valid extruded solid")

    # Map local +Z onto the outward face normal; centre the text bbox on the face
    # centroid. Engrave recesses the body inward (text top flush with surface),
    # emboss stands it proud (text bottom flush with surface): shift by depth/2.
    rot = App.Rotation(App.Vector(0, 0, 1), normal)
    rotated_center = rot.multVec(tsolid.BoundBox.Center)
    offset = App.Vector(0, 0, 0)
    pos = p.get("position")
    if pos:
        u_dir = rot.multVec(App.Vector(1, 0, 0))
        v_dir = rot.multVec(App.Vector(0, 1, 0))
        offset = u_dir.multiply(float(pos[0])) + v_dir.multiply(float(pos[1]))
    half = normal.multiply(depth / 2.0)
    base = com - rotated_center + (offset - half if mode == "engrave" else offset + half)
    placement = App.Placement()
    placement.Rotation = rot
    placement.Base = base
    placed = tsolid.transformGeometry(placement.toMatrix())

    # Materialise the placed text as a tool object, then cut/fuse via boolean_op
    # so consumed-input visibility hygiene is applied for free.
    tool = doc.addObject("Part::Feature", "TextTool")
    tool.Shape = placed
    doc.recompute()
    tool_h = _register("texttool", tool)
    op = "cut" if mode == "engrave" else "fuse"
    r = HANDLERS["boolean_op"]({"op": op, "base": p["handle"], "tool": tool_h})
    result_obj = _resolve(r["handle"])
    result_obj.Label = p.get("name", "Text")
    doc.recompute()
    h = _register("text", result_obj)
    return {"handle": h, "name": result_obj.Name,
            "volume": result_obj.Shape.Volume, "text": text,
            "mode": mode, "depth": depth}


@handler("add_rib")
def _h_add_rib(p):
    """Add a reinforcing rib/web inside a PartDesign Body, thickening an OPEN
    sketch profile (a line/arc/polyline spine) into a wall that fuses with the
    surrounding material. Units: thickness in mm. midplane=True centers the
    wall on the spine (thickness/2 each side); reversed flips the extrusion
    sense. Returns {handle, name, volume (whole-body Shape.Volume in mm^3 after
    the rib, which is strictly larger than before), thickness}.

    Fallback note: FreeCAD's native PartDesign::Rib type is NOT registered in
    the headless freecadcmd runtime, so this command synthesizes the rib as a
    midplane PartDesign::Pad: it offsets the open spine by +/-thickness/2 into a
    closed footprint sketch and pads it across the body's bounding-box diagonal
    (so the wall reliably reaches the surrounding walls), centered on the spine.
    Geometrically equivalent to a Rib for the common straight/curved-spine
    case."""
    doc = _active_doc()
    body = _resolve_body(p["body"])
    profile = _resolve_sketch(p["sketch"])
    thickness = float(p.get("thickness", 2.0))
    if thickness <= 0:
        raise ValueError(f"thickness must be > 0 mm (got {thickness})")
    midplane = bool(p.get("midplane", True))
    reversed_ = bool(p.get("reversed", False))
    name = p.get("name", "Rib")

    # Read the spine in the sketch's LOCAL frame (sketch geometry is planar at
    # z=0 local); we offset it there, then re-attach the footprint to the same
    # plane so it inherits the profile's world placement.
    local_edges = [g.toShape() for g in profile.Geometry if hasattr(g, "toShape")]
    if not local_edges:
        raise ValueError("rib profile sketch has no geometry")
    wire = Part.Wire(local_edges)
    if wire.isClosed():
        raise ValueError(
            "rib profile must be an OPEN spine (line/arc/polyline), not a closed "
            "loop; it is thickened to `thickness` mm about that spine"
        )
    ovs = wire.OrderedVertexes if hasattr(wire, "OrderedVertexes") else wire.Vertexes
    poly = [App.Vector(v.X, v.Y, 0.0) for v in ovs]
    clean = [poly[0]]
    for v in poly[1:]:
        if (v - clean[-1]).Length > 1e-7:
            clean.append(v)
    poly = clean
    if len(poly) < 2:
        raise ValueError("rib profile degenerated to a single point")

    # Perpendicular offset of the polyline using per-vertex averaged tangents
    # (left normal = z x tangent). Robust where Part.makeOffset2D is flaky on a
    # lone open segment.
    def _offset(d):
        out = []
        n = len(poly)
        for i, pt in enumerate(poly):
            if i == 0:
                t = poly[1] - poly[0]
            elif i == n - 1:
                t = poly[-1] - poly[-2]
            else:
                t = poly[i + 1] - poly[i - 1]
            t.normalize()
            nrm = App.Vector(-t.y, t.x, 0.0)
            out.append(pt + nrm.multiply(d))
        return out

    half = thickness / 2.0
    loop = _offset(half) + list(reversed(_offset(-half)))

    def _bvol(b):
        s = getattr(b, "Shape", None)
        return 0.0 if (s is None or s.isNull()) else s.Volume
    pre_volume = _bvol(body)

    # Extrude the rib across the body so it always reaches surrounding walls;
    # Midplane keeps it centered on the spine plane.
    bb = body.Shape.BoundBox if not body.Shape.isNull() else None
    span = bb.DiagonalLength if (bb is not None and bb.DiagonalLength > 1e-6) else 100.0

    foot = doc.addObject("Sketcher::SketchObject", name + "Footprint")
    body.addObject(foot)
    foot.AttachmentSupport = profile.AttachmentSupport
    foot.MapMode = profile.MapMode
    foot.AttachmentOffset = profile.AttachmentOffset
    for i in range(len(loop)):
        a = loop[i]
        b2 = loop[(i + 1) % len(loop)]
        foot.addGeometry(
            Part.LineSegment(App.Vector(a.x, a.y, 0.0), App.Vector(b2.x, b2.y, 0.0)),
            False,
        )
    doc.recompute()

    rib = doc.addObject("PartDesign::Pad", name)
    rib.Profile = foot
    rib.Length = span
    rib.Midplane = midplane
    rib.Reversed = reversed_
    body.addObject(rib)
    doc.recompute()

    if rib.Shape.isNull() or not rib.Shape.isValid():
        raise RuntimeError(
            "rib feature produced a null/invalid shape; check that the open "
            "profile lies between the body walls it should connect"
        )
    post_volume = rib.Shape.Volume
    if post_volume <= pre_volume + 1e-6:
        raise RuntimeError(
            f"rib added no material (body volume {pre_volume:.1f} -> "
            f"{post_volume:.1f} mm^3); the spine may not reach surrounding walls"
        )
    # The spine sketch and the generated footprint are consumed inputs.
    _set_visibility(profile, False)
    _set_visibility(foot, False)
    h = _register("rib", rib)
    return {
        "handle": h,
        "name": rib.Name,
        "volume": post_volume,
        "thickness": thickness,
    }


@handler("transform")
def _h_transform(p):
    """Move and/or rotate an existing object in place by mutating its Placement.

    Units: translate in mm [x, y, z]; rotate_axis a direction vector (need not be
    unit length); angle in degrees about that axis. relative=True (default)
    composes the delta onto the object's CURRENT placement (incremental move);
    relative=False sets the delta as the object's ABSOLUTE placement (discarding
    prior placement). No new object/handle is created — the same object moves.

    Returns {handle, name, placement: {base:[x,y,z], axis:[x,y,z], angle_deg}}
    where base/axis/angle_deg describe the object's resulting Placement."""
    import math
    doc = _active_doc()
    obj = _resolve(p["handle"])
    if not hasattr(obj, "Placement"):
        raise TypeError(f"object {obj.Name!r} has no Placement to transform")

    translate = p.get("translate") or [0.0, 0.0, 0.0]
    if len(translate) != 3:
        raise ValueError(f"translate must be [x, y, z] mm, got {translate!r}")
    trans = App.Vector(*[float(c) for c in translate])

    axis = p.get("rotate_axis") or [0.0, 0.0, 1.0]
    if len(axis) != 3:
        raise ValueError(f"rotate_axis must be a 3-vector, got {axis!r}")
    ax = App.Vector(*[float(c) for c in axis])
    angle = float(p.get("angle", 0.0))
    if angle != 0.0 and ax.Length == 0.0:
        raise ValueError("rotate_axis must be non-zero when angle != 0")
    if ax.Length == 0.0:
        ax = App.Vector(0.0, 0.0, 1.0)  # harmless default for a 0-deg no-op

    # App.Rotation(axis, deg) takes the angle in DEGREES.
    delta = App.Placement(trans, App.Rotation(ax, angle))
    relative = bool(p.get("relative", True))
    obj.Placement = delta.multiply(obj.Placement) if relative else delta
    doc.recompute()

    pl = obj.Placement
    rax = pl.Rotation.Axis
    return {
        "handle": p["handle"],
        "name": obj.Name,
        "placement": {
            "base": [pl.Base.x, pl.Base.y, pl.Base.z],
            "axis": [rax.x, rax.y, rax.z],
            "angle_deg": math.degrees(pl.Rotation.Angle),
        },
    }


@handler("scale_shape")
def _h_scale_shape(p):
    """Scale a shape uniformly or per-axis, baking a fresh static Part::Feature
    (scaling breaks parametric history, so the result is a standalone solid, not
    a linked feature). All lengths mm. `factor` is a scalar (uniform) or a
    [sx, sy, sz] list. `center` is an optional [x, y, z] mm pivot; when omitted
    the scale is about the world origin. Geometry is transformed via
    shape.transformGeometry(matrix) so the actual geometry scales (transformShape
    would only move it). The source object is hidden, since its shape has been
    consumed into the scaled copy. Returns {handle, name, volume, factor} where
    volume scales by sx*sy*sz versus the source."""
    doc = _active_doc()
    obj, shape = _shape_of(p["handle"])
    factor = p["factor"]
    if isinstance(factor, (int, float)):
        sx = sy = sz = float(factor)
    else:
        if not (isinstance(factor, (list, tuple)) and len(factor) == 3):
            raise ValueError(
                f"factor must be a number or [sx, sy, sz], got {factor!r}")
        sx, sy, sz = (float(factor[0]), float(factor[1]), float(factor[2]))
    if sx <= 0 or sy <= 0 or sz <= 0:
        raise ValueError(f"scale factors must be > 0, got [{sx}, {sy}, {sz}]")

    center = p.get("center")
    m = App.Matrix()
    m.scale(sx, sy, sz)
    if center is not None:
        if not (isinstance(center, (list, tuple)) and len(center) == 3):
            raise ValueError(f"center must be [x, y, z], got {center!r}")
        cx, cy, cz = (float(center[0]), float(center[1]), float(center[2]))
        # Scale about `center`: translate pivot to origin, scale, translate back.
        # Compose right-to-left so the move-to-origin is applied first.
        to_origin = App.Matrix(); to_origin.move(App.Vector(-cx, -cy, -cz))
        back = App.Matrix(); back.move(App.Vector(cx, cy, cz))
        m = back.multiply(m.multiply(to_origin))

    # transformGeometry rebuilds the geometry (B-rep) under the matrix, unlike
    # transformShape which only changes placement — non-uniform scale needs this.
    scaled = shape.transformGeometry(m)
    out = doc.addObject("Part::Feature", p.get("name", "Scaled"))
    out.Shape = scaled
    doc.recompute()
    _set_visibility(obj, False)
    h = _register("scaled", out)
    return {
        "handle": h,
        "name": out.Name,
        "volume": out.Shape.Volume,
        "factor": [sx, sy, sz],
    }


@handler("copy_shape")
def _h_copy_shape(p):
    """Duplicate an existing shape as an independent static solid. Lengths mm.

    Resolves p["handle"] to its Shape and stamps a deep copy (shape.copy())
    into a fresh Part::Feature. The copy carries its own geometry — unlike
    add_part's App::Link, later edits to the source do NOT propagate to the
    copy. Optional p["placement"] is an absolute [x, y, z] translation in mm
    applied to the copy's base (the source is left untouched and still visible).
    Optional p["name"] names the new object (defaults to "<SourceName>_copy").
    Returns {handle, name, volume} where volume is mm^3 of the copied solid."""
    doc = _active_doc()
    obj, shape = _shape_of(p["handle"])
    out = doc.addObject("Part::Feature", p.get("name") or (obj.Name + "_copy"))
    out.Shape = shape.copy()  # deep copy → independent geometry, not an App::Link
    placement = p.get("placement")
    if placement is not None:
        if len(placement) != 3:
            raise ValueError("placement must be [x, y, z] in mm")
        out.Placement.Base = App.Vector(*(float(c) for c in placement))
    doc.recompute()
    h = _register("copy", out)
    return {"handle": h, "name": out.Name, "volume": out.Shape.Volume}


@handler("measure_distance")
def _h_measure_distance(p):
    """Minimum distance between two entities (mm). a/b are handles; a_ref/b_ref
    optionally narrow to a sub-shape on that handle: an f_*/e_* tag or a
    FaceN/EdgeN string (whole Shape used if the ref is omitted). FreeCAD's
    Shape.distToShape does the math. Returns:
      distance_mm  -- minimum gap (0.0 when shapes touch or interpenetrate)
      point_on_a   -- [x,y,z] closest point on a (mm)
      point_on_b   -- [x,y,z] closest point on b (mm)
      touching     -- True when distance_mm < 1e-7
    Does not mutate input geometry."""
    def _sub(handle, ref):
        # Resolve a handle (+ optional face/edge ref) to a measurable shape.
        _, shape = _shape_of(handle)
        if ref is None:
            return shape
        ref = str(ref)
        # Tags resolve to a 1-based FaceN/EdgeN index; bare FaceN/EdgeN accepted too.
        if ref.startswith("f_"):
            ref = _h_resolve_face({"handle": handle, "tag": ref})["index"]
        elif ref.startswith("e_"):
            ref = _h_resolve_edge({"handle": handle, "tag": ref})["index"]
        if ref.startswith("Face"):
            idx = int(ref[len("Face"):])
            faces = shape.Faces
            if not 1 <= idx <= len(faces):
                raise ValueError(
                    f"{handle!r} has no Face{idx} (has {len(faces)} faces)"
                )
            return faces[idx - 1]
        if ref.startswith("Edge"):
            idx = int(ref[len("Edge"):])
            edges = shape.Edges
            if not 1 <= idx <= len(edges):
                raise ValueError(
                    f"{handle!r} has no Edge{idx} (has {len(edges)} edges)"
                )
            return edges[idx - 1]
        raise ValueError(
            f"unrecognized ref {ref!r}: use an f_*/e_* tag or a FaceN/EdgeN string"
        )

    sa = _sub(p["a"], p.get("a_ref"))
    sb = _sub(p["b"], p.get("b_ref"))
    # distToShape -> (dist, [(pt_on_a, pt_on_b), ...], infos); points[0] is the
    # closest pair. dist == 0 means the shapes touch or intersect.
    dist, points, _infos = sa.distToShape(sb)
    pa, pb = points[0]
    return {
        "distance_mm": round(dist, 6),
        "point_on_a": [round(pa.x, 6), round(pa.y, 6), round(pa.z, 6)],
        "point_on_b": [round(pb.x, 6), round(pb.y, 6), round(pb.z, 6)],
        "touching": dist < 1e-7,
    }


@handler("measure_angle")
def _h_measure_angle(p):
    """Angle between two planar faces or two straight edges, in degrees.

    `a`/`b` are object handles; `a_ref`/`b_ref` are required references that
    each narrow to a sub-shape on their handle: an `f_*` face tag (planar faces
    only -> angle between outward normals) or an `e_*` edge tag (straight/line
    edges only -> angle between tangent directions). `FaceN`/`EdgeN` strings and
    bare 1-based ints are accepted fallbacks. Both refs must be the same kind
    (both faces or both edges). The reported `angle_deg` is the raw angle between
    the two direction vectors (0..180); `supplement_deg` = 180 - angle_deg is
    also returned because the agent often wants the acute complement (e.g. two
    opposite parallel faces give angle_deg 180, supplement 0). Returns
    {angle_deg, supplement_deg, kind: 'face'|'edge'}. Read-only; mutates nothing.
    """
    import math
    a = p["a"]
    b = p["b"]
    a_ref = p.get("a_ref")
    b_ref = p.get("b_ref")
    if a_ref is None or b_ref is None:
        raise ValueError(
            "measure_angle requires a_ref and b_ref (f_* face tags or e_* edge tags)"
        )

    def _kind_of(ref):
        if isinstance(ref, str) and (ref.startswith("f_") or ref.startswith("Face")):
            return "face"
        if isinstance(ref, str) and (ref.startswith("e_") or ref.startswith("Edge")):
            return "edge"
        raise ValueError(
            f"ref {ref!r} must be an f_*/FaceN face tag or an e_*/EdgeN edge tag"
        )

    ka, kb = _kind_of(a_ref), _kind_of(b_ref)
    if ka != kb:
        raise ValueError(
            f"a_ref ({ka}) and b_ref ({kb}) must be the same kind: both faces or both edges"
        )
    kind = ka

    def _face_index(handle, ref):
        if isinstance(ref, str) and ref.startswith("f_"):
            return int(_h_resolve_face({"handle": handle, "tag": ref})["index"][len("Face"):])
        return int(ref[len("Face"):])

    def _edge_index(handle, ref):
        if isinstance(ref, str) and ref.startswith("e_"):
            return int(_h_resolve_edge({"handle": handle, "tag": ref})["index"][len("Edge"):])
        return int(ref[len("Edge"):])

    _, sa = _shape_of(a)
    _, sb = _shape_of(b)

    if kind == "face":
        ia, ib = _face_index(a, a_ref), _face_index(b, b_ref)
        if ia < 1 or ia > len(sa.Faces):
            raise ValueError(f"face index {ia} out of range (1..{len(sa.Faces)}) on {a!r}")
        if ib < 1 or ib > len(sb.Faces):
            raise ValueError(f"face index {ib} out of range (1..{len(sb.Faces)}) on {b!r}")
        fa, fb = sa.Faces[ia - 1], sb.Faces[ib - 1]
        if _surface_kind(fa) != "planar":
            raise ValueError(f"a_ref {a_ref!r} is not a planar face; angle needs planar faces")
        if _surface_kind(fb) != "planar":
            raise ValueError(f"b_ref {b_ref!r} is not a planar face; angle needs planar faces")
        va, vb = _outward_normal(fa), _outward_normal(fb)
    else:
        ia, ib = _edge_index(a, a_ref), _edge_index(b, b_ref)
        if ia < 1 or ia > len(sa.Edges):
            raise ValueError(f"edge index {ia} out of range (1..{len(sa.Edges)}) on {a!r}")
        if ib < 1 or ib > len(sb.Edges):
            raise ValueError(f"edge index {ib} out of range (1..{len(sb.Edges)}) on {b!r}")
        ea, eb = sa.Edges[ia - 1], sb.Edges[ib - 1]
        # tangentAt(FirstParameter) gives a constant direction for a Line; curved
        # edges have a direction that varies along their length, so reject them.
        if type(ea.Curve).__name__ != "Line":
            raise ValueError(f"a_ref {a_ref!r} is not a straight edge; angle needs line edges")
        if type(eb.Curve).__name__ != "Line":
            raise ValueError(f"b_ref {b_ref!r} is not a straight edge; angle needs line edges")
        va, vb = ea.tangentAt(ea.FirstParameter), eb.tangentAt(eb.FirstParameter)

    ang = math.degrees(va.getAngle(vb))  # getAngle returns radians in 0..pi
    return {
        "angle_deg": _round(ang),
        "supplement_deg": _round(180.0 - ang),
        "kind": kind,
    }


@handler("bounding_box")
def _h_bounding_box(p):
    """Axis-aligned bounding box (AABB) of a shaped object — a focused, cheap
    query (the same numbers mass_properties buries in its payload). All lengths
    in mm, in world coordinates.

    Returns:
      min      [x,y,z]  lower corner of the AABB
      max      [x,y,z]  upper corner of the AABB
      size     [x,y,z]  extents = max - min  (XLength, YLength, ZLength)
      center   [x,y,z]  AABB center
      diagonal float    space-diagonal length of the AABB
      oriented null, or (when oriented=True and FreeCAD supports it)
               {size:[x,y,z], center:[x,y,z], diagonal:float} for the tightest
               box at any orientation (from shape.optimalBoundingBox()); null if
               that computation is unavailable/failed.

    Does not mutate the input. No handle is returned (this is a measurement)."""
    _, shape = _shape_of(p["handle"])
    bb = shape.BoundBox
    out = {
        "min": [_round(bb.XMin), _round(bb.YMin), _round(bb.ZMin)],
        "max": [_round(bb.XMax), _round(bb.YMax), _round(bb.ZMax)],
        "size": [_round(bb.XLength), _round(bb.YLength), _round(bb.ZLength)],
        "center": [_round(bb.Center.x), _round(bb.Center.y), _round(bb.Center.z)],
        "diagonal": _round(bb.DiagonalLength),
        "oriented": None,
    }
    if p.get("oriented"):
        # optimalBoundingBox() (FreeCAD >= 0.20) returns a Base.BoundBox aligned
        # to the shape's tightest orientation; wrap in try/except since older
        # builds or degenerate shapes may not support it.
        try:
            obb = shape.optimalBoundingBox()
            out["oriented"] = {
                "size": [_round(obb.XLength), _round(obb.YLength), _round(obb.ZLength)],
                "center": [_round(obb.Center.x), _round(obb.Center.y), _round(obb.Center.z)],
                "diagonal": _round(obb.DiagonalLength),
            }
        except Exception:
            out["oriented"] = None
    return out


@handler("min_clearance")
def _h_min_clearance(p):
    """Closest approach between two solids — the richer companion to
    interference_check (which only reports overlap volume). Takes two handles
    `a` and `b` (whole shapes). Classifies the relationship and measures the
    gap. All lengths mm, volumes mm³.

    Logic: if the boolean common() has volume > 1e-9 the solids interpenetrate
    ("interference"); otherwise distToShape gives the minimum gap — dist > 1e-7
    is "clear", dist ~ 0 is "contact" (touching faces/edges).

    Returns:
      status: "clear" | "contact" | "interference"
      clearance_mm: minimum gap (0.0 when interfering or touching)
      overlap_volume_mm3: present only when status == "interference"
      point_on_a / point_on_b: [x,y,z] closest points (present when not
        interfering; for "contact" they coincide)
    """
    _, sa = _shape_of(p["a"])
    _, sb = _shape_of(p["b"])
    # interpenetration first: a non-trivial boolean intersection means the
    # solids share material, so there is no positive clearance to report.
    try:
        overlap = sa.common(sb).Volume
    except Exception:
        overlap = 0.0
    if overlap > 1e-9:
        return {
            "status": "interference",
            "clearance_mm": 0.0,
            "overlap_volume_mm3": round(overlap, 6),
        }
    dist, pts, _ = sa.distToShape(sb)
    pa, pb = pts[0]
    return {
        "status": "clear" if dist > 1e-7 else "contact",
        "clearance_mm": round(dist, 6),
        "point_on_a": [round(pa.x, 6), round(pa.y, 6), round(pa.z, 6)],
        "point_on_b": [round(pb.x, 6), round(pb.y, 6), round(pb.z, 6)],
    }


@handler("check_shape")
def _h_check_shape(p):
    """Geometry validity / sanity check for a shaped object (a cheap guard so
    agents don't keep building on a broken solid). Reports OCC validity, the
    topology census, and a single watertight-solid verdict. Inspects only; never
    mutates the input and never auto-fixes (a fix_shape would be a later sibling).
    All volumes in mm3.

    Returns:
      valid            (bool)  shape.isValid() — OCC topology/geometry is sound
      watertight_solid (bool)  exactly one solid AND valid AND closed
      shape_type       (str)   'Solid'/'Shell'/'Compound'/... (shape.ShapeType)
      closed           (bool)  shape.isClosed() — no free boundary edges
      solids/shells/faces/edges (int) sub-shape counts
      volume_mm3       (float) shape.Volume (0 for open/2D shapes)
      is_null          (bool)  shape.isNull() — empty shape
      check            (str, only when invalid) note that shape.check(True)
                       printed diagnostics to the worker log
      check_error      (str, only when the diagnostic call itself raised)
    """
    _, shape = _shape_of(p["handle"])
    valid = bool(shape.isValid())
    closed = bool(shape.isClosed())
    out = {
        "valid": valid,
        "shape_type": shape.ShapeType,
        "closed": closed,
        "solids": len(shape.Solids),
        "shells": len(shape.Shells),
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
        "volume_mm3": round(shape.Volume, 6),
        "is_null": bool(shape.isNull()),
    }
    if not valid:
        # check(True) prints per-defect diagnostics to stderr/worker log; it has
        # no structured return, so we just flag that detail landed in the log.
        try:
            shape.check(True)
            out["check"] = "see worker log"
        except Exception as e:
            out["check_error"] = str(e)
    out["watertight_solid"] = bool(out["solids"] == 1 and valid and closed)
    return out


@handler("section_view")
def _h_section_view(p):
    """Cut a solid with a plane and report the cross-section. Lengths mm, areas
    mm^2. `plane` is "XY"/"XZ"/"YZ" (world datum planes, oriented per FreeCAD's
    own datum map: XY normal +Z, XZ normal -Y, YZ normal +X) or a datum-plane
    handle (or any object with a Placement -> its local +Z is the normal, its
    origin the base). `offset` (mm) shifts the cutting plane along that normal.
    Slices the shape with Part `shape.slice(normal, d)` where d is the signed
    distance of the plane from the world origin along `normal`
    (d = base.dot(normal) + offset). section_area_mm2 sums the areas of the
    closed section wires. When `emit_profile` is True a Part::Feature holding the
    section wires is added to the document and registered, and its handle is
    returned (raises if the plane misses the shape so there is nothing to emit).
    Does not mutate the input geometry. Returns keys: plane, offset_mm, normal
    [x,y,z], section_area_mm2, wire_count, closed_wire_count, bbox
    {min:[x,y,z], max:[x,y,z], size:[dx,dy,dz]} (None when the plane misses the
    shape), and handle + name (only when emit_profile=True)."""
    doc = _active_doc()
    obj, shape = _shape_of(p["handle"])
    offset = float(p.get("offset", 0.0))

    plane = p.get("plane", "XY")
    if plane in _DATUM_PLANES:
        # World datum: reuse the same rotation map FreeCAD uses for origin planes
        # so the section normal matches the plane the agent named (XY->+Z etc.).
        _base, rot = _DATUM_PLANES[plane]
        normal = rot.multVec(App.Vector(0, 0, 1))
        base = App.Vector(0, 0, 0)
    else:
        # A datum-plane handle (or any object with a Placement): the plane is its
        # local XY, the normal is local +Z, and base is its origin.
        ref = _resolve(plane)
        pl = getattr(ref, "Placement", None)
        if pl is None:
            raise ValueError(
                f"plane {plane!r} is neither 'XY'/'XZ'/'YZ' nor a handle with a "
                f"Placement (datum plane); cannot derive a cutting normal"
            )
        normal = pl.Rotation.multVec(App.Vector(0, 0, 1))
        base = pl.Base
    normal = App.Vector(normal).normalize()

    # signed distance of the cutting plane from the world origin along `normal`
    d = base.dot(normal) + offset

    wires = shape.slice(normal, d)  # list of section wires at signed distance d
    closed = [w for w in wires if w.isClosed()]
    area = 0.0
    for w in closed:
        try:
            area += Part.Face(w).Area
        except Exception:
            # a closed wire that doesn't bound a planar face (rare) adds 0 area
            pass

    out = {
        "plane": plane,
        "offset_mm": round(offset, 6),
        "normal": [round(normal.x, 6), round(normal.y, 6), round(normal.z, 6)],
        "section_area_mm2": round(area, 6),
        "wire_count": len(wires),
        "closed_wire_count": len(closed),
    }

    if wires:
        bb = Part.Compound(wires).BoundBox
        out["bbox"] = {
            "min": [round(bb.XMin, 6), round(bb.YMin, 6), round(bb.ZMin, 6)],
            "max": [round(bb.XMax, 6), round(bb.YMax, 6), round(bb.ZMax, 6)],
            "size": [round(bb.XLength, 6), round(bb.YLength, 6), round(bb.ZLength, 6)],
        }
    else:
        out["bbox"] = None

    if p.get("emit_profile"):
        if not wires:
            raise RuntimeError(
                "emit_profile=True but the plane does not intersect the shape "
                "(no section wires); adjust plane/offset"
            )
        prof = doc.addObject("Part::Feature", p.get("name", "Section"))
        prof.Shape = Part.Compound(wires)
        doc.recompute()
        out["handle"] = _register("section", prof)
        out["name"] = prof.Name

    return out


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
    _set_visibility(base, False)
    _set_visibility(tool, False)
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


_INTERCHANGE_EXT = {".step", ".stp", ".iges", ".igs", ".brep", ".stl"}


def _export_doc_shape(doc, path, ext):
    """Export the document's geometry to an interchange file. Compounds every
    top-level shaped object (skipping consumed inputs) so a multi-feature doc
    exports as one shape."""
    import Part
    shaped = [o for o in doc.Objects
              if hasattr(o, "Shape") and not o.Shape.isNull() and not o.InList]
    if not shaped:
        shaped = [o for o in doc.Objects
                  if hasattr(o, "Shape") and not o.Shape.isNull()]
    if not shaped:
        raise RuntimeError("no shaped object to export")
    shape = shaped[0].Shape if len(shaped) == 1 \
        else Part.makeCompound([o.Shape for o in shaped])
    if ext in (".step", ".stp"):
        shape.exportStep(path)
    elif ext in (".iges", ".igs"):
        shape.exportIges(path)
    elif ext == ".brep":
        shape.exportBrep(path)
    elif ext == ".stl":
        shape.exportStl(path)


@handler("save_document")
def _h_save_document(p):
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document")
    path = p["path"]
    ext = os.path.splitext(path)[1].lower()

    # Route by extension. A FreeCAD *document* (.FCStd) is saved natively; a
    # geometry-interchange extension exports the doc's shape instead. Anything
    # else fails LOUDLY here, naming the supported formats — rather than letting
    # doc.saveAs() raise a bare FileNotFoundError on an unsupported extension
    # (the trap that bit the multi-agent coordinator when a decomposer named a
    # component file ".step").
    if ext in _INTERCHANGE_EXT:
        if p.get("visibility_hygiene", True):
            _apply_visibility_hygiene(doc)
        _export_doc_shape(doc, path, ext)
        return {"path": path, "size": os.path.getsize(path),
                "format": ext, "camera_fit": False}
    if ext not in ("", ".fcstd"):
        raise ValueError(
            f"save_document: unsupported extension {ext!r}; use .FCStd for a "
            f"FreeCAD document, or one of {sorted(_INTERCHANGE_EXT)} to export "
            f"geometry."
        )

    if p.get("visibility_hygiene", True):
        _apply_visibility_hygiene(doc)
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


def _body_volume(body):
    shape = getattr(body, "Shape", None)
    if shape is None or shape.isNull():
        return 0.0
    return shape.Volume


def _orient_subtractive(feature, body, direction):
    """Pick `feature.Reversed` so the cut goes in the requested direction
    relative to the body's existing material:
      'into_body'      : Reversed value that actually removes material.
      'away_from_body' : Reversed value that removes none.

    Hole and Pocket interpret the Reversed flag differently internally (Pocket
    flips a Pad-style extrusion convention; Hole flips a cylindrical cutter
    axis). A volume-delta probe translates intent → whichever flag value the
    feature happens to need, without the caller having to reason about it.

    Pre-condition: `feature` has already been added to `body`'s Group but the
    document has not yet been recomputed for this feature."""
    if direction not in ("into_body", "away_from_body"):
        raise ValueError(
            f"direction must be 'into_body' or 'away_from_body' (got {direction!r})"
        )
    doc = body.Document
    pre = _body_volume(body)

    feature.Reversed = False
    doc.recompute()
    post = feature.Shape.Volume if not feature.Shape.isNull() else pre
    removed = pre - post

    wants_removal = direction == "into_body"
    # 1e-6 mm³ is the floor for "the cut actually removed material" — anything
    # smaller is OCCT numerical noise on a no-op subtract.
    matched = (removed > 1e-6) if wants_removal else (removed <= 1e-6)
    if not matched:
        feature.Reversed = True
        doc.recompute()


def _sketch_plane_world(sketch):
    """Return (origin, normal) of the sketch's plane in world coords."""
    pl = sketch.Placement
    return pl.Base, pl.Rotation.multVec(App.Vector(0, 0, 1))


def _sketch_world_centroid(sketch):
    """A representative point on the sketch profile in world coords. Uses the
    sketch shape's bounding-box center, which sits on the sketch plane and
    is close to the centroid for the common single-profile case (circle,
    rectangle, etc.)."""
    shape = getattr(sketch, "Shape", None)
    if shape is None or shape.isNull():
        return sketch.Placement.Base
    bb = shape.BoundBox
    if not bb.isValid():
        return sketch.Placement.Base
    return App.Vector(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )


def _sketch_sample_points(sketch):
    """Return a list of world-coord sample points on the sketch profile:
    bbox center plus quarter-points along each edge. Used by wall-depth to
    probe the profile rather than relying on a single centroid — robust to
    off-center sketches and curved sketches that span varying wall thickness."""
    pts = [_sketch_world_centroid(sketch)]
    shape = getattr(sketch, "Shape", None)
    if shape is None or shape.isNull():
        return pts
    for edge in shape.Edges:
        try:
            t0, t1 = edge.ParameterRange
            for frac in (0.0, 0.25, 0.5, 0.75):
                pts.append(edge.valueAt(t0 + frac * (t1 - t0)))
        except Exception:
            continue
    return pts


def _into_body_direction(sketch, body):
    """Unit vector in world coords pointing FROM the sketch plane INTO the
    body's existing material. Decides between +normal and -normal by which
    half-space contains the body's centroid. Used for wall-depth ray casting,
    independent of any feature's Reversed convention."""
    origin, normal = _sketch_plane_world(sketch)
    n = App.Vector(normal).normalize()
    shape = body.Shape
    com = shape.CenterOfMass
    to_body = com - origin
    return n if to_body.dot(n) > 0 else App.Vector(-n.x, -n.y, -n.z)


def _ray_hit_distances(shape, origin, direction, max_distance):
    """Sorted distances along `direction` where a ray from `origin` crosses
    the boundary of `shape`. Distances are in [0, max_distance]."""
    d = App.Vector(direction).normalize()
    end = App.Vector(origin) + d * max_distance
    edge = Part.LineSegment(App.Vector(origin), end).toShape()
    inter = shape.section(edge)
    dists = []
    for v in inter.Vertexes:
        delta = v.Point - App.Vector(origin)
        t = delta.dot(d)
        if -1e-6 <= t <= max_distance + 1e-6:
            dists.append(max(0.0, t))
    dists.sort()
    return dists


def _first_wall_depth(body_shape, origin, direction):
    """Distance from `origin` along `direction` to where the ray first exits
    the body's material — i.e. the thickness of the first wall the cut would
    encounter. Returns None if no exit is found.

    If origin sits inside the body, the first ray-shape intersection is the
    exit. Otherwise the first intersection is an entry and the second is the
    exit. A solid body has one wall (entry+exit on the far side) so the depth
    equals the body's full extent in that direction. A shelled body has the
    outer-wall exit at distance ≈ wall_thickness."""
    if body_shape.isNull():
        return None
    bb = body_shape.BoundBox
    max_d = (bb.DiagonalLength or 1.0) * 2.0
    d = App.Vector(direction).normalize()
    # Nudge the probe a hair along the ray so a sketch sitting exactly on a
    # face doesn't make isInside ambiguous at the boundary.
    nudge = 1e-3
    probe = App.Vector(origin) + d * nudge
    dists = _ray_hit_distances(body_shape, probe, d, max_d)
    if not dists:
        return None
    try:
        inside = body_shape.isInside(probe, 1e-6, True)
    except Exception:
        inside = False
    if inside:
        return dists[0] + nudge
    if len(dists) < 2:
        return None
    return dists[1] + nudge


def _apply_through(feature, body, sketch, through):
    """Configure a Pocket or Hole's depth from a semantic 'through' choice.
    'wall' computes wall depth via ray cast and sets a Dimension-typed depth
    just past the first wall. 'body' sets the feature's ThroughAll mode."""
    if through == "body":
        if feature.isDerivedFrom("PartDesign::Pocket"):
            feature.Type = 1  # ThroughAll
        elif feature.isDerivedFrom("PartDesign::Hole"):
            feature.DepthType = "ThroughAll"
        else:
            raise TypeError(f"through= not supported for {feature.TypeId!r}")
        return None
    if through == "wall":
        ray_dir = _into_body_direction(sketch, body)
        depths = []
        for p in _sketch_sample_points(sketch):
            d = _first_wall_depth(body.Shape, p, ray_dir)
            if d is not None and d > 1e-6:
                depths.append(d)
        if not depths:
            raise RuntimeError(
                "through='wall' couldn't measure a wall depth — no sampled "
                "point on the sketch profile produced an exit boundary along "
                "the cut direction. The sketch may be outside the body, aligned "
                "grazingly with a face, or attached to a face the body doesn't "
                "actually own."
            )
        # Max across samples = thickest part of the wall under the cut profile.
        # Cutting to this depth ensures the hole emerges through the wall
        # everywhere the profile overlaps material (curved walls vary in
        # thickness across a hole's footprint; min would leave a ceiling).
        # Samples that returned None (over empty space) don't constrain depth.
        depth = max(depths)
        # Tiny epsilon ensures the cut emerges cleanly through the wall
        # without floating-point edge cases at the exit surface.
        depth_with_eps = depth + 0.001
        if feature.isDerivedFrom("PartDesign::Pocket"):
            feature.Type = 0  # Dimension
            feature.Length = depth_with_eps
        elif feature.isDerivedFrom("PartDesign::Hole"):
            feature.DepthType = "Dimension"
            feature.Depth = depth_with_eps
        else:
            raise TypeError(f"through= not supported for {feature.TypeId!r}")
        return depth
    raise ValueError(
        f"through must be 'wall' or 'body' (got {through!r})"
    )


def _revolve_axis_in_world(body, axis_name):
    """Return (origin, direction) in world coords for one of a Body's origin
    axes ('X'/'Y'/'Z'). Returns None for unrecognized names."""
    dirs = {
        "X": App.Vector(1, 0, 0),
        "Y": App.Vector(0, 1, 0),
        "Z": App.Vector(0, 0, 1),
    }
    if axis_name not in dirs:
        return None
    pl = body.Placement
    return pl.Base, pl.Rotation.multVec(dirs[axis_name])


def _point_axis_distance(point, axis_origin, axis_dir):
    """Perpendicular distance from a 3D point to the infinite line defined by
    (origin, direction)."""
    p = App.Vector(point)
    o = App.Vector(axis_origin)
    d = App.Vector(axis_dir).normalize()
    return p.distanceToLine(o, d)


def _axis_coincident_edges(sketch, axis_origin, axis_dir, tol=1e-4):
    """Return [(edge_index, length)] for edges that lie ON the revolution axis
    (both endpoints AND midpoint within `tol` of the axis line). PartDesign
    Revolution errors with 'shape is invalid' on such profiles — they sweep
    to zero-thickness slivers that OCCT rejects."""
    shape = getattr(sketch, "Shape", None)
    if shape is None or shape.isNull():
        return []
    bad = []
    for i, edge in enumerate(shape.Edges):
        if len(edge.Vertexes) < 2:
            continue  # closed-loop edge (full circle/ellipse) — skip
        d0 = _point_axis_distance(edge.Vertexes[0].Point, axis_origin, axis_dir)
        d1 = _point_axis_distance(edge.Vertexes[1].Point, axis_origin, axis_dir)
        if d0 >= tol or d1 >= tol:
            continue
        # Both endpoints on the axis. Verify the rest of the edge is too —
        # a curved edge from axis-point to axis-point could still bulge off
        # the axis (and would be fine to revolve).
        try:
            t0, t1 = edge.ParameterRange
            mid = edge.valueAt(0.5 * (t0 + t1))
            if _point_axis_distance(mid, axis_origin, axis_dir) >= tol:
                continue
        except Exception:
            pass
        bad.append((i, edge.Length))
    return bad


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
    """Pocket (subtract) a sketch from the body. through_all=True ignores length.

    direction (preferred over `reversed`): 'into_body' picks the Reversed value
    that actually removes material from the body's existing shape;
    'away_from_body' picks the value that removes none. Solves the case where
    Pocket and Hole disagree on which Reversed value drills inward.

    through ('wall'|'body'): semantic depth override. 'wall' ray-casts to the
    first exit boundary so the cut emerges cleanly through one wall (correct
    for both solids and shelled bodies — on a solid, wall = full thickness).
    'body' is the legacy ThroughAll behavior, which on a shelled body destroys
    the cavity by cutting through every wall. Implies direction='into_body'."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    pocket = doc.addObject("PartDesign::Pocket", p.get("name", "Pocket"))
    pocket.Profile = sketch
    body = _add_to_body_of_sketch(sketch, pocket)

    through = p.get("through")
    if through is not None:
        if p.get("direction", "into_body") != "into_body":
            raise ValueError(
                "through= implies direction='into_body'; "
                "explicit direction='away_from_body' is incompatible"
            )
        wall_depth = _apply_through(pocket, body, sketch, through)
        _orient_subtractive(pocket, body, "into_body")
        h = _register("pocket", pocket)
        return {
            "handle": h, "name": pocket.Name, "volume": pocket.Shape.Volume,
            "through": through, "wall_depth_mm": wall_depth,
        }

    if p.get("through_all"):
        pocket.Type = 1  # ThroughAll
    else:
        pocket.Length = float(p.get("length", 10.0))
    direction = p.get("direction")
    if direction is not None:
        _orient_subtractive(pocket, body, direction)
    else:
        pocket.Reversed = bool(p.get("reversed", False))
        doc.recompute()
    h = _register("pocket", pocket)
    return {"handle": h, "name": pocket.Name, "volume": pocket.Shape.Volume}


@handler("revolve")
def _h_revolve(p):
    """Revolve a sketch around a body-axis ('X','Y','Z') by `angle` degrees.

    Pre-check: if the sketch profile has straight edges that lie ALONG the
    revolution axis (zero distance from axis at both endpoints AND parallel
    to it), Revolution fails with an opaque OCCT 'shape is invalid' error.
    The pre-check surfaces the actual cause and suggests the
    boolean-difference workaround."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    body = _body_of(sketch)
    axis = p.get("axis", "Y").upper()

    bad_edges = []
    if axis in ("X", "Y", "Z"):
        axis_geo = _revolve_axis_in_world(body, axis)
        if axis_geo is not None:
            bad_edges = _axis_coincident_edges(sketch, *axis_geo)
    if bad_edges:
        details = ", ".join(f"edge {i} (length {l:.2f} mm)" for i, l in bad_edges)
        raise RuntimeError(
            f"Revolution around {axis} axis would fail: the sketch has "
            f"{len(bad_edges)} edge(s) lying along the axis itself "
            f"({details}). PartDesign Revolution errors with 'shape is invalid' "
            f"on such profiles — the axis-coincident edges sweep to zero-"
            f"thickness slivers that OCCT rejects.\n"
            f"Workaround: revolve a profile WITHOUT axis-coincident edges "
            f"(e.g. revolve a full annulus to make a torus), then use "
            f"boolean_op to subtract the unwanted material. Alternatively, "
            f"trim the axis-coincident edges out of the sketch — they "
            f"represent zero-volume boundaries in the revolved solid anyway."
        )

    rev = doc.addObject("PartDesign::Revolution", p.get("name", "Revolution"))
    rev.Profile = sketch
    if axis in ("X", "Y", "Z"):
        rev.ReferenceAxis = (_origin_axis(body, axis), [""])
    rev.Angle = float(p.get("angle", 360.0))
    rev.Reversed = bool(p.get("reversed", False))
    body.addObject(rev)
    doc.recompute()
    if rev.Shape.isNull():
        raise RuntimeError(
            f"Revolution recompute produced a null shape — sketch may be "
            f"open, self-intersecting, or the axis/profile configuration is "
            f"unsupported."
        )
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


_INTENDED_FOR = ("print", "machine", "drawing")


@handler("hole")
def _h_hole(p):
    """Drill a parametric hole from a sketch containing one or more circles.
    sketch: sketch handle (must lie inside a body that already has material).
    diameter: mm.
    depth_type: 'Dimension' (uses depth) | 'ThroughAll'. Default 'ThroughAll'.
    depth: mm (used only when depth_type='Dimension').
    cut_type: 'None' | 'Counterbore' | 'Countersink' | 'Counterdrill'. Default 'None'.
    cut_diameter / cut_depth: mm (used when cut_type != 'None').
    threaded: bool to enable tap (sets Threaded=True).
    thread_type + thread_size: COUPLED enums — valid thread_size values DEPEND
    on thread_type ('M4' is valid for 'ISOMetricProfile' but not for 'UNC').
    Call list_thread_options() to discover thread_type values, then
    list_thread_options(thread_type=...) for that type's valid thread_sizes.
    intended_for ('print'|'machine'|'drawing'): drives ModelThread when threaded
    so the caller doesn't have to know what ModelThread means.
      'print'   → ModelThread=True. The screw thread geometry is emitted in
                  the model because a 3D printer can't tap a smooth pilot hole;
                  the print emerges with the thread cut into it.
      'machine' → ModelThread=False. The hole is a smooth pilot at the major
                  diameter; CAM software reads the thread metadata and drives
                  a physical tap. Modeling the thread bloats the file and
                  fights with patterns/fillets.
      'drawing' → ModelThread=False. Drawings annotate threads symbolically;
                  the modeled geometry is cosmetic.
    Explicit model_thread overrides intended_for.
    direction (preferred over `reversed`): 'into_body' picks the Reversed value
    that actually removes material; 'away_from_body' picks the value that
    removes none.
    through ('wall'|'body'): semantic depth override that takes precedence over
    depth_type/depth. 'wall' ray-casts to the first exit boundary so the hole
    emerges through exactly one wall — essential on shelled bodies where
    'body' (ThroughAll) would punch through every wall and destroy the cavity.
    Implies direction='into_body'."""
    doc = _active_doc()
    sketch = _resolve_sketch(p["sketch"])
    body = _body_of(sketch)

    intended_for = p.get("intended_for")
    if intended_for is not None and intended_for not in _INTENDED_FOR:
        raise ValueError(
            f"intended_for must be one of {_INTENDED_FOR!r} (got {intended_for!r})"
        )

    hole = doc.addObject("PartDesign::Hole", p.get("name", "Hole"))
    body.addObject(hole)
    hole.Profile = sketch
    hole.Diameter = float(p.get("diameter", 5.0))

    through = p.get("through")
    if through is None:
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
        # ModelThread: explicit overrides intended_for; intended_for overrides
        # the FreeCAD default of False.
        if p.get("model_thread") is not None:
            hole.ModelThread = bool(p["model_thread"])
        elif intended_for == "print":
            hole.ModelThread = True
        elif intended_for in ("machine", "drawing"):
            hole.ModelThread = False

    if through is not None:
        if p.get("direction", "into_body") != "into_body":
            raise ValueError(
                "through= implies direction='into_body'; "
                "explicit direction='away_from_body' is incompatible"
            )
        wall_depth = _apply_through(hole, body, sketch, through)
        _orient_subtractive(hole, body, "into_body")
        h = _register("hole", hole)
        return {
            "handle": h, "name": hole.Name, "volume": hole.Shape.Volume,
            "through": through, "wall_depth_mm": wall_depth,
        }

    direction = p.get("direction")
    if direction is not None:
        _orient_subtractive(hole, body, direction)
    else:
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


@handler("set_visibility")
def _h_set_visibility(p):
    """Explicit override of an object's persistent Visibility flag. Use to
    keep a reference part visible alongside a derived cut, or to manually hide
    something the hygiene heuristic missed. A subsequent save_document re-runs
    the hygiene pass by default — pass visibility_hygiene=False to save_document
    to preserve your override."""
    obj = _resolve(p["handle"])
    visible = bool(p["visible"])
    if not _set_visibility(obj, visible):
        raise RuntimeError(f"object {obj.Name!r} has no Visibility property")
    return {"handle": p["handle"], "name": obj.Name, "visible": visible}


_VERIFY_UNSUPPORTED = object()


def _previous_volume_of(obj):
    """Return the volume an additive/subtractive feature's delta is measured
    against. 0.0 if the feature is first in its Body (BaseFeature is None).
    Returns the _VERIFY_UNSUPPORTED sentinel if the object type lacks a
    well-defined 'before' (Part primitives, Part::Fuse, Part::Common, etc. —
    those combine multiple operands and the caller should diff against an
    explicit baseline)."""
    if hasattr(obj, "BaseFeature"):
        base = obj.BaseFeature
        if base is None:
            return 0.0
        if hasattr(base, "Shape") and not base.Shape.isNull():
            return base.Shape.Volume
        return 0.0
    if obj.isDerivedFrom("Part::Cut"):
        base = getattr(obj, "Base", None)
        if base is not None and hasattr(base, "Shape") and not base.Shape.isNull():
            return base.Shape.Volume
        return 0.0
    return _VERIFY_UNSUPPORTED


@handler("verify_feature")
def _h_verify_feature(p):
    """Compare a feature's actual volume change against an expected signed
    delta. Catches silent failures: a Pocket on a curved surface that only
    removes 17% of the expected material, a Hole that drilled outside the
    body, a Cut whose Tool didn't intersect the Base.

    handle: PartDesign feature (Pad/Pocket/Hole/Revolve/etc.) or Part::Cut.
    expected_delta_mm3: signed expected change. Subtractive features should
                        pass a NEGATIVE number; additive features POSITIVE.
                        Wrong sign is itself a useful error.
    tolerance: relative tolerance fraction (default 0.05 = 5%).
    abs_tolerance: absolute tolerance in mm³ for tiny expected magnitudes
                   (default 0.01). Pass if EITHER tolerance is satisfied.

    Returns {handle, name, expected_delta_mm3, actual_delta_mm3, ratio,
    passed, previous_volume_mm3, current_volume_mm3, message}. Does NOT
    raise on mismatch — caller inspects `passed` and decides."""
    obj = _resolve(p["handle"])
    expected = float(p["expected_delta_mm3"])
    rel_tol = float(p.get("tolerance", 0.05))
    abs_tol = float(p.get("abs_tolerance", 0.01))

    prev_vol = _previous_volume_of(obj)
    if prev_vol is _VERIFY_UNSUPPORTED:
        raise ValueError(
            f"verify_feature: no 'previous shape' available for {obj.TypeId!r}. "
            f"Supported: PartDesign features (via BaseFeature) and Part::Cut "
            f"(via Base). For other types, diff against an explicit baseline."
        )

    if not hasattr(obj, "Shape") or obj.Shape.isNull():
        raise RuntimeError(
            f"feature {obj.Name!r} has null Shape — likely a recompute failure"
        )
    curr_vol = obj.Shape.Volume

    actual = curr_vol - prev_vol
    diff = actual - expected
    abs_diff = abs(diff)
    rel = (abs_diff / abs(expected)) if abs(expected) > 1e-12 else None

    passed_rel = rel is not None and rel <= rel_tol
    passed_abs = abs_diff <= abs_tol
    passed = bool(passed_rel or passed_abs)

    if passed:
        if rel is not None:
            message = (
                f"OK: expected Δ={expected:+.3f} mm³, "
                f"actual Δ={actual:+.3f} mm³ ({rel * 100:.2f}% diff)"
            )
        else:
            message = (
                f"OK: expected Δ={expected:+.3f} mm³, actual Δ={actual:+.3f} mm³ "
                f"(within ±{abs_tol} mm³)"
            )
    else:
        if rel is not None:
            message = (
                f"MISMATCH: expected Δ={expected:+.3f} mm³, "
                f"actual Δ={actual:+.3f} mm³ "
                f"({rel * 100:.1f}% off, tolerance {rel_tol * 100:.1f}%)"
            )
        else:
            message = (
                f"MISMATCH: expected Δ={expected:+.3f} mm³, "
                f"actual Δ={actual:+.3f} mm³ "
                f"(|diff|={abs_diff:.3f} mm³ exceeds abs_tolerance {abs_tol} mm³)"
            )

    return {
        "handle": p["handle"],
        "name": obj.Name,
        "expected_delta_mm3": expected,
        "actual_delta_mm3": actual,
        "ratio": (actual / expected) if abs(expected) > 1e-12 else None,
        "passed": passed,
        "previous_volume_mm3": prev_vol,
        "current_volume_mm3": curr_vol,
        "message": message,
    }


@handler("list_thread_options")
def _h_list_thread_options(p):
    """Discover the COUPLED ThreadType / ThreadSize enums on PartDesign::Hole.
    Call with no args to get valid thread_type values; pass thread_type=... to
    get the valid thread_size values for that type (the coupling means
    thread_size='M4' is valid for 'ISOMetricProfile' but not for 'UNC').

    Implementation: spins up a hidden probe document, creates a temporary
    Hole feature, reads the enumerations, and tears down. Doesn't touch the
    user's active document."""
    prev_active = App.ActiveDocument
    probe = App.newDocument("_dp_thread_probe")
    try:
        hole = probe.addObject("PartDesign::Hole", "_probe")
        thread_type = p.get("thread_type")
        if thread_type is not None:
            try:
                hole.ThreadType = thread_type
            except Exception as e:
                types = list(hole.getEnumerationsOfProperty("ThreadType"))
                raise ValueError(
                    f"unknown thread_type {thread_type!r}; valid: {types}"
                ) from e
            sizes = list(hole.getEnumerationsOfProperty("ThreadSize"))
            return {"thread_type": thread_type, "thread_sizes": sizes}
        return {"thread_types": list(hole.getEnumerationsOfProperty("ThreadType"))}
    finally:
        App.closeDocument(probe.Name)
        if prev_active is not None:
            try:
                App.setActiveDocument(prev_active.Name)
            except Exception:
                pass


@handler("register_handle")
def _h_register_handle(p):
    """Register an existing FreeCAD object (by .Name) into the handle table.
    Returns a fresh handle that the rest of the tool surface (render_view,
    list_faces, fillet_edges, mass_properties, etc.) accepts.

    Use cases:
      - After run_script created objects in Python, bring them back into the
        tool ecosystem instead of being stuck calling .Name-accepting tools.
      - After open_document loaded a saved file, register objects whose handles
        from the previous session are gone.

    object: the FreeCAD object's .Name attribute.
    prefix: handle prefix (default 'manual'). Each call returns a new handle
            even if the same object was registered before — multiple aliases
            for one object are allowed."""
    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document")
    name = p["object"]
    obj = doc.getObject(name)
    if obj is None:
        raise KeyError(f"no object named {name!r} in active document")
    prefix = p.get("prefix", "manual")
    h = _register(prefix, obj)
    return {"handle": h, "name": obj.Name, "type": obj.TypeId, "label": obj.Label}


# --- script escape hatch ------------------------------------------------------

_SCRIPT_GLOBALS = None


@handler("run_script")
def _h_run_script(p):
    """Execute an arbitrary Python script inside the worker's live document context.

    The script has App, Part, ObjectsFem, and the handle registry helpers in scope.
    It may set __result__ to a JSON-serializable value which is returned to the host.

    Auto-registration: any new shape-bearing object created during execution is
    automatically added to the handle table when auto_register=True (default).
    The result includes a 'registered' list of {handle, name, type} entries so
    the caller can use them in subsequent tool calls without a separate
    register_handle round-trip.
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
    auto_register = bool(p.get("auto_register", True))

    pre_names = set()
    if auto_register and App.ActiveDocument is not None:
        pre_names = {o.Name for o in App.ActiveDocument.Objects}

    _SCRIPT_GLOBALS.pop("__result__", None)
    exec(compile(code, p.get("path", "<script>"), "exec"), _SCRIPT_GLOBALS)

    registered = []
    if auto_register and App.ActiveDocument is not None:
        for obj in App.ActiveDocument.Objects:
            if obj.Name in pre_names:
                continue
            if not hasattr(obj, "Shape"):
                continue
            try:
                if obj.Shape.isNull():
                    continue
            except Exception:
                continue
            h = _register("script", obj)
            registered.append(
                {"handle": h, "name": obj.Name, "type": obj.TypeId}
            )

    return {
        "result": _SCRIPT_GLOBALS.get("__result__"),
        "registered": registered,
    }


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


# --- interface frames (mate-by-named-frame) ----------------------------------
#
# A component publishes named "interface frames" — local coordinate systems at the
# places other parts mate to it (a mounting face, a bolt circle, a bore axis).
# Frames are content, not raw numbers: merge places a child by ALIGNING its frame
# to a parent's, so placement is derived from the contract and can't drift from it.
# The mate math is  Pc = Pp · Fp · Fc⁻¹  (child placement makes the child's frame
# coincide in world space with the parent's).
#
# Frames persist in the component file as a JSON property bag (DP_Interfaces) on
# the top-level shaped object. We deliberately do NOT add a separate datum object
# for them: a PartDesign::CoordinateSystem derives from Part::Feature, so it would
# pollute add_part's candidate set and get linked instead of the real solid.

_IFACE_PROP = "DP_Interfaces"


def _frame_to_placement(frame):
    """frame {origin:[x,y,z], z_axis:[...]?, x_axis:[...]?} -> App.Placement.
    z_axis defaults +Z, x_axis +X; both re-orthonormalized."""
    origin = App.Vector(*frame.get("origin", [0, 0, 0]))
    z = App.Vector(*frame.get("z_axis", [0, 0, 1]))
    x = App.Vector(*frame.get("x_axis", [1, 0, 0]))
    if z.Length < 1e-9:
        z = App.Vector(0, 0, 1)
    z.normalize()
    if x.Length < 1e-9 or abs(x.dot(z)) > 1 - 1e-9:
        x = App.Vector(1, 0, 0) if abs(z.x) < 0.9 else App.Vector(0, 1, 0)
    y = z.cross(x)
    y.normalize()
    x = y.cross(z)
    x.normalize()
    m = App.Matrix(x.x, y.x, z.x, origin.x,
                   x.y, y.y, z.y, origin.y,
                   x.z, y.z, z.z, origin.z,
                   0, 0, 0, 1)
    return App.Placement(m)


def _shaped_top(obj):
    """The shaped object that carries a component's interfaces: the linked target
    if obj is a link, else obj itself."""
    if obj.isDerivedFrom("App::Link") and obj.LinkedObject is not None:
        return obj.LinkedObject
    return obj


def _read_interfaces(obj):
    """Published interface dict for a component object ({} if none)."""
    import json as _json
    base = _shaped_top(obj)
    if _IFACE_PROP in base.PropertiesList:
        try:
            return _json.loads(getattr(base, _IFACE_PROP) or "{}")
        except Exception:
            return {}
    return {}


@handler("publish_interface")
def _h_publish_interface(p):
    """Record a named interface frame on a component so other parts can mate to it
    — the published "here is where you bolt to me, and how it's oriented".

    handle: the component's shaped object.
    name: interface name (e.g. "lid_seat", "bolt_circle", "bore_axis").
    frame: {origin:[x,y,z], z_axis:[...]?, x_axis:[...]?}. Extra keys (e.g.
        bolt-circle metadata) are stored verbatim alongside the frame.

    Persists in the component's .FCStd as a JSON property bag, so merge_assembly
    can mate against it later. Returns {handle, name, frame, interfaces}."""
    import json as _json
    obj = _resolve(p["handle"])
    base = _shaped_top(obj)
    name = p["name"]
    frame = dict(p["frame"])
    ifaces = _read_interfaces(obj)
    ifaces[name] = frame
    if _IFACE_PROP not in base.PropertiesList:
        base.addProperty("App::PropertyString", _IFACE_PROP, "DriftPin",
                         "published interface frames (JSON)")
    setattr(base, _IFACE_PROP, _json.dumps(ifaces))
    base.Document.recompute()
    return {"handle": p["handle"], "name": name, "frame": frame,
            "interfaces": sorted(ifaces.keys())}


def _apply_mate(link, parent_link, child_iface, parent_iface):
    """Place `link` so its child_iface frame coincides with parent_link's
    parent_iface frame in world space: LinkPlacement = Pp · Fp · Fc⁻¹."""
    cif = _read_interfaces(link)
    pif = _read_interfaces(parent_link)
    if child_iface not in cif:
        raise KeyError(f"child has no published interface {child_iface!r} "
                       f"(has {sorted(cif)})")
    if parent_iface not in pif:
        raise KeyError(f"parent has no published interface {parent_iface!r} "
                       f"(has {sorted(pif)})")
    Fc = _frame_to_placement(cif[child_iface])
    Fp = _frame_to_placement(pif[parent_iface])
    Pp = parent_link.LinkPlacement
    link.LinkPlacement = Pp.multiply(Fp).multiply(Fc.inverse())


@handler("interface_align_check")
def _h_interface_align_check(p):
    """Gate: verify declared interface pairs actually coincide in world space —
    the "do the OTHER interfaces line up?" check for multi-interface mates.

    pairs: [{child, child_iface, parent, parent_iface}, ...] where child/parent
    are link Names in the assembly. tol_mm (default 1e-3). Returns the list of
    misaligned pairs [{..., gap_mm}], empty if every pair coincides."""
    asm = _resolve(p["assembly"])
    tol = float(p.get("tol_mm", 1e-3))
    by_name = {o.Name: o for o in asm.Group}
    out = []
    for pr in p.get("pairs", []):
        c = by_name.get(pr["child"])
        pa = by_name.get(pr["parent"])
        if c is None or pa is None:
            out.append({**pr, "error": "link not found"})
            continue
        cif = _read_interfaces(c)
        pif = _read_interfaces(pa)
        if pr["child_iface"] not in cif or pr["parent_iface"] not in pif:
            out.append({**pr, "error": "interface not published"})
            continue
        cw = c.LinkPlacement.multiply(_frame_to_placement(cif[pr["child_iface"]]))
        pw = pa.LinkPlacement.multiply(_frame_to_placement(pif[pr["parent_iface"]]))
        gap = (cw.Base - pw.Base).Length
        if gap > tol:
            out.append({**pr, "gap_mm": gap})
    return out


# --- change propagation + lockfile (RFC §9) ----------------------------------
#
# A lockfile is the provenance record that lets a coordinator detect drift across
# a team without reading geometry: per component, a content hash of its file plus
# a hash of its published interface frames, and which other components it mates to
# (depends on). On re-check we distinguish:
#   internal change  — file changed, interfaces unchanged  -> just re-merge
#   interface change — published frames moved              -> every neighbor that
#                       mates to it is STALE and must be re-dispatched.
# This is exactly the "neighbor not re-dispatched after an interface move" failure
# toy #6 guards against.


def _hash_file_bytes(path):
    """blake2b digest of a file's bytes — detects any change to the component."""
    import hashlib
    h = hashlib.blake2b(digest_size=12)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _interfaces_of_file(path):
    """Published interface frames read FROM DISK. Closes any stale in-memory copy
    of the file first (and the freshly-opened one after) so the read reflects what
    is actually on disk. Run on quiescent files — not while a file is linked into
    a live assembly in this same worker."""
    import os as _os
    for d in list(App.listDocuments().values()):
        try:
            if d.FileName and _os.path.exists(d.FileName) \
                    and _os.path.samefile(d.FileName, path):
                App.closeDocument(d.Name)
        except Exception:
            pass
    doc = App.openDocument(path, True)
    try:
        for o in doc.Objects:
            if _IFACE_PROP in o.PropertiesList:
                import json as _json
                try:
                    return _json.loads(getattr(o, _IFACE_PROP) or "{}")
                except Exception:
                    return {}
        return {}
    finally:
        try:
            App.closeDocument(doc.Name)
        except Exception:
            pass


def _lock_state(manifest, base_dir):
    """Per-component lock state: {id: {file, file_hash, interfaces_hash,
    depends_on}}. depends_on lists the components this one mates to."""
    import os as _os
    import json as _json
    import hashlib
    comps = manifest.get("components", {})
    inst_comp = {inst.get("name", inst["component"]): inst["component"]
                 for inst in manifest.get("instances", [])}
    deps = {cid: set() for cid in comps}
    mates = list(manifest.get("mates", []))
    for inst in manifest.get("instances", []):
        if inst.get("mate"):
            m = dict(inst["mate"])
            m["child"] = inst.get("name", inst["component"])
            mates.append(m)
    for m in mates:
        cc = inst_comp.get(m["child"], m["child"])
        pc = inst_comp.get(m["parent"], m["parent"])
        if cc in deps and pc in comps:
            deps[cc].add(pc)
    state = {}
    for cid, spec in comps.items():
        cfile = spec["file"]
        cfile = cfile if _os.path.isabs(cfile) else _os.path.join(base_dir, cfile)
        ifaces = _interfaces_of_file(cfile)
        ih = hashlib.blake2b(
            _json.dumps(ifaces, sort_keys=True).encode(), digest_size=12).hexdigest()
        state[cid] = {"file": spec["file"], "file_hash": _hash_file_bytes(cfile),
                      "interfaces_hash": ih, "depends_on": sorted(deps[cid])}
    return state


@handler("assembly_lock")
def _h_assembly_lock(p):
    """Write a lockfile recording each component's content hash, interface hash,
    and mate dependencies — the provenance baseline for change detection. Call
    after a clean merge. lockfile defaults to <manifest>.lock.json. Returns
    {lockfile, components}."""
    import os as _os
    import json as _json
    manifest_path = p["manifest"]
    with open(manifest_path) as f:
        man = _json.load(f)
    base_dir = _os.path.dirname(_os.path.abspath(manifest_path))
    lockfile = p.get("lockfile") or (
        _os.path.splitext(manifest_path)[0] + ".lock.json")
    state = _lock_state(man, base_dir)
    lock = {"manifest": _os.path.basename(manifest_path), "components": state}
    with open(lockfile, "w") as f:
        _json.dump(lock, f, indent=2, sort_keys=True)
    return {"lockfile": lockfile, "components": state}


@handler("assembly_lock_check")
def _h_assembly_lock_check(p):
    """Compare current component files to a lockfile and classify drift (RFC §9):
      modified          — file changed since lock
      interface_changed — published interface frames moved (subset of modified)
      stale             — mates to an interface_changed component and was NOT
                          itself rebuilt -> a neighbor that needs re-dispatch
      new / removed     — components added to / dropped from the manifest
    ok = nothing stale and no new/removed: safe to re-merge without re-dispatch.
    An interface change with no un-rebuilt dependents is still ok (links reload)."""
    import os as _os
    import json as _json
    manifest_path = p["manifest"]
    with open(manifest_path) as f:
        man = _json.load(f)
    base_dir = _os.path.dirname(_os.path.abspath(manifest_path))
    lockfile = p.get("lockfile") or (
        _os.path.splitext(manifest_path)[0] + ".lock.json")
    with open(lockfile) as f:
        locked = _json.load(f).get("components", {})
    current = _lock_state(man, base_dir)

    modified, interface_changed = [], []
    for cid, cur in current.items():
        old = locked.get(cid)
        if old is None:
            continue
        if cur["file_hash"] != old["file_hash"]:
            modified.append(cid)
        if cur["interfaces_hash"] != old["interfaces_hash"]:
            interface_changed.append(cid)
    new = [cid for cid in current if cid not in locked]
    removed = [cid for cid in locked if cid not in current]

    iface_set = set(interface_changed)
    modified_set = set(modified)
    stale = [
        cid for cid, cur in current.items()
        if cid not in iface_set and cid not in modified_set
        and any(dep in iface_set for dep in cur["depends_on"])
    ]
    ok = not stale and not new and not removed
    return {"modified": sorted(modified),
            "interface_changed": sorted(interface_changed),
            "stale": sorted(stale), "new": sorted(new),
            "removed": sorted(removed), "ok": ok}


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
            candidates = [
                o for o in ext_doc.Objects
                if (o.isDerivedFrom("PartDesign::Body")
                    or o.isDerivedFrom("Part::Feature")
                    or o.isDerivedFrom("App::Part"))  # subassembly container
                and not o.isDerivedFrom("Part::Datum")  # skip datum planes/LCS
                and hasattr(o, "Shape") and not o.Shape.isNull()
            ]
            if not candidates:
                raise RuntimeError(f"no shaped object in {source['path']}")
            # Prefer a top-level result (nothing in the file consumes it) over a
            # consumed input: link the Cut, not the Box it was cut from; link the
            # subassembly App::Part, not the parts inside it. A boolean's inputs (and
            # a Part's members) carry the parent in their InList; the top's is empty.
            toplevel = [o for o in candidates if not o.InList]
            target = (toplevel or candidates)[-1]
        App.setActiveDocument(doc.Name)
    else:
        raise ValueError(f"source must have 'handle' or 'path': {source!r}")

    link = doc.addObject("App::Link", p.get("name", "Part"))
    link.LinkedObject = target
    assembly.addObject(link)

    placement = p.get("placement")
    if placement is not None:
        if isinstance(placement, list) and len(placement) == 3:
            pl = App.Placement(App.Vector(*placement), App.Rotation())
        elif isinstance(placement, dict):
            pos = App.Vector(*placement.get("position", [0, 0, 0]))
            axis = App.Vector(*placement.get("axis", [0, 0, 1]))
            angle = float(placement.get("angle_deg", 0))
            pl = App.Placement(pos, App.Rotation(axis, angle))
        else:
            pl = None
        if pl is not None:
            # Set LinkPlacement, not Placement: when a link sits in an App::Part
            # alongside a linked subassembly (App::Part), recompute resets a plain
            # .Placement back to the origin. LinkPlacement is the link's own frame
            # and survives recompute; for flat assemblies it's equivalent.
            link.LinkPlacement = pl

    # mate-by-frame: place this part by aligning its published interface frame to
    # an already-placed parent's, instead of (or after) a raw placement.
    mate = p.get("mate")
    if mate is not None:
        parent_ref = mate["parent"]
        try:
            parent_link = _resolve(parent_ref)
        except Exception:
            parent_link = doc.getObject(parent_ref)
        if parent_link is None:
            raise KeyError(f"mate parent {parent_ref!r} not found")
        _apply_mate(link, parent_link, mate["child_iface"], mate["parent_iface"])

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


def _leaf_world_shapes(group, parent_matrix, acc, prefix=""):
    """Flatten an assembly to leaf (name, world-shape) pairs, descending through
    linked subassemblies (App::Part) and composing the placement chain. So a
    subassembly is checked leaf-by-leaf — intra-subassembly clashes count too,
    not just the merged compound."""
    for o in group:
        m = parent_matrix.multiply(o.Placement.Matrix)
        base = (o.LinkedObject if (o.isDerivedFrom("App::Link")
                                   and o.LinkedObject is not None) else o)
        label = prefix + o.Name
        if base.isDerivedFrom("App::Part"):
            _leaf_world_shapes(base.Group, m.multiply(base.Placement.Matrix),
                               acc, prefix=label + "/")
        elif hasattr(base, "Shape") and not base.Shape.isNull():
            acc.append((label, base.Shape.transformed(m)))


@handler("interference_check")
def _h_interference_check(p):
    """Pairwise interference: compute volume of intersection between every pair of
    parts in the assembly (flattened to leaves through any subassemblies). Returns
    overlapping pairs ordered by descending interference volume."""
    asm = _resolve(p["assembly"])
    shapes = []
    _leaf_world_shapes(asm.Group, App.Matrix(), shapes)

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
    """Walk an assembly, group its parts, return a BOM:
    [{part, count, total_volume_mm3, total_mass_kg?}, ...]. density is
    optional kg/mm³; if given, each row's mass is computed.

    Identity is the source (component file + object), NOT the bare object Name —
    two distinct components both named "Box" (add_primitive's default) must not
    collapse into one row. The displayed `part` is the component file's stem when
    the part is an external link, else the object's Label/Name.

    recursive (default True): descend into linked subassemblies (App::Part) so the
    BOM flattens to leaf parts. False counts a subassembly as a single line."""
    import os as _os
    asm = _resolve(p["assembly"])
    density = float(p["density"]) if "density" in p else None
    recursive = p.get("recursive", True)
    counts = {}

    def _add(base):
        fname = getattr(getattr(base, "Document", None), "FileName", "") or ""
        if fname:
            key = (fname, base.Name)
            part = _os.path.splitext(_os.path.basename(fname))[0]
        else:
            key = (None, base.Name)
            part = base.Label or base.Name
        # volume is invariant under the placement chain, so the local shape is fine
        s = base.Shape if (hasattr(base, "Shape") and not base.Shape.isNull()) else None
        v = s.Volume if s is not None else 0.0
        row = counts.get(key)
        if row is None:
            counts[key] = {"part": part, "count": 1, "total_volume_mm3": v}
        else:
            row["count"] += 1
            row["total_volume_mm3"] += v

    def _walk(group):
        for o in group:
            base = (o.LinkedObject if (o.isDerivedFrom("App::Link")
                                       and o.LinkedObject is not None) else o)
            if recursive and base.isDerivedFrom("App::Part"):
                _walk(base.Group)
            else:
                _add(base)

    _walk(asm.Group)
    rows = list(counts.values())
    if density is not None:
        for r in rows:
            r["total_mass_kg"] = r["total_volume_mm3"] * density
    rows.sort(key=lambda r: -r["count"])
    return rows


@handler("envelope_check")
def _h_envelope_check(p):
    """Keep-out gate: assert each named part's world-space bounding box stays
    inside its declared envelope. envelopes maps a part's link Name (or Label) to
    {"min": [x,y,z], "max": [x,y,z]} in the assembly frame. Returns a list of
    violations [{part, axis, got: [min,max], allowed: [min,max]}, ...] — empty
    means every declared part is within its box."""
    asm = _resolve(p["assembly"])
    envelopes = p.get("envelopes") or {}
    eps = 1e-6
    out = []
    for o in asm.Group:
        env = envelopes.get(o.Name) or envelopes.get(getattr(o, "Label", None))
        if not env:
            continue
        s = _world_shape(o)
        if s is None:
            continue
        bb = s.BoundBox
        bmin = [bb.XMin, bb.YMin, bb.ZMin]
        bmax = [bb.XMax, bb.YMax, bb.ZMax]
        for i, ax in enumerate("xyz"):
            if bmin[i] < env["min"][i] - eps or bmax[i] > env["max"][i] + eps:
                out.append({"part": o.Name, "axis": ax,
                            "got": [bmin[i], bmax[i]],
                            "allowed": [env["min"][i], env["max"][i]]})
    return out


@handler("merge_assembly")
def _h_merge_assembly(p):
    """Construct-up an assembly from a manifest (the coordinator's one call).

    manifest (path to JSON) shape:
      { "name": "gearbox",
        "root": "gearbox.FCStd",                       # optional output path (rel)
        "components": { "<id>": { "file": "rel/part.FCStd",
                                  "object": "<name>",   # optional explicit target
                                  "envelope": {"min":[...],"max":[...]} } },  # optional
        "instances": [ { "component": "<id>",
                         "name": "<instance>",          # optional, defaults to id
                         "placement": [x,y,z] | {position,axis,angle_deg} } ] }

    Component files are resolved relative to the manifest's directory. Links
    auto-reload from those files, so re-running picks up updated components.
    Runs the gates (interference, recursive BOM, envelope) and returns a report.
    Deterministic and idempotent."""
    import json as _json
    import os as _os
    manifest_path = p["manifest"]
    with open(manifest_path) as f:
        man = _json.load(f)
    base_dir = _os.path.dirname(_os.path.abspath(manifest_path))
    comps = man.get("components", {})

    name = man.get("name", "merged")
    HANDLERS["new_document"]({"name": name})
    asm = HANDLERS["make_assembly"]({"name": man.get("assembly_name", "Assembly")})
    asm_h = asm["handle"]

    root = man.get("root") or (name + ".FCStd")
    root_path = root if _os.path.isabs(root) else _os.path.join(base_dir, root)
    HANDLERS["save_document"]({"path": root_path})  # owner needs a path to link

    placed = []
    envelopes = {}
    links_by_inst = {}
    for inst in man.get("instances", []):
        cid = inst["component"]
        spec = comps[cid]
        iname = inst.get("name", cid)
        cfile = spec["file"]
        cfile = cfile if _os.path.isabs(cfile) else _os.path.join(base_dir, cfile)
        src = {"path": cfile}
        if spec.get("object"):
            src["object"] = spec["object"]
        link = HANDLERS["add_part"]({
            "assembly": asm_h, "source": src,
            "placement": inst.get("placement"), "name": iname,
        })
        links_by_inst[iname] = link["name"]
        placed.append({"instance": iname, "component": cid,
                       "linked": link["linked"]})
        if spec.get("envelope"):
            envelopes[link["name"]] = spec["envelope"]

    # mate-by-frame: align each child instance's published frame to an already-
    # placed parent's. Mates come from a top-level "mates" list and/or per-instance
    # "mate" keys. Anchors get raw placements above; everything else is positioned
    # by contract here. A parent must be linked before its children (manifest order).
    asm_obj = _resolve(asm_h)
    by_name = {o.Name: o for o in asm_obj.Group}
    mates = list(man.get("mates", []))
    for inst in man.get("instances", []):
        if inst.get("mate"):
            m = dict(inst["mate"])
            m["child"] = inst.get("name", inst["component"])
            mates.append(m)
    for m in mates:
        child = by_name[links_by_inst[m["child"]]]
        parent = by_name[links_by_inst[m["parent"]]]
        _apply_mate(child, parent, m["child_iface"], m["parent_iface"])
    if mates:
        asm_obj.Document.recompute()

    align_pairs = [m for m in mates if m.get("verify_align")]
    gates = {
        "interference": HANDLERS["interference_check"]({"assembly": asm_h}),
        "bom": HANDLERS["bom_extract"]({"assembly": asm_h, "recursive": True}),
        "envelope": HANDLERS["envelope_check"]({"assembly": asm_h,
                                                "envelopes": envelopes}),
    }
    if align_pairs:
        gates["interface_align"] = HANDLERS["interface_align_check"]({
            "assembly": asm_h,
            "pairs": [{"child": links_by_inst[m["child"]],
                       "parent": links_by_inst[m["parent"]],
                       "child_iface": m["verify_align"]["child_iface"],
                       "parent_iface": m["verify_align"]["parent_iface"]}
                      for m in align_pairs],
        })
    HANDLERS["save_document"]({"path": root_path})
    ok = (not gates["interference"]) and (not gates["envelope"]) \
        and (not gates.get("interface_align"))
    return {"assembly": asm_h, "doc": name, "root": root_path,
            "placed": placed, "gates": gates, "ok": ok}


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

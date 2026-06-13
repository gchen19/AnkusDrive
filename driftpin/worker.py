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

# This file is launched as a bare script (`freecadcmd worker.py`), so the repo
# root isn't on sys.path. Add it so pure-Python `driftpin.analysis.*` modules
# (materials, tolerance, ...) are importable from handlers below.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


# --- airtight / enclosed-flow void analysis (issue #19) ----------------------
#
# An enclosed-flow part (a vacuum adapter, a manifold, a duct) is "correct" when
# a single connected void joins its declared inlet to its declared outlet and is
# bounded by solid everywhere else. check_shape's watertight verdict is necessary
# but NOT sufficient: a watertight solid can still have a blocked path (a near-
# zero "almond slit") or an unintended opening (an over-cut doorway) — the two
# failure modes in issue #19. This computes the functional invariant via a pure-
# BREP void analysis: build the void as padded_bbox.cut(part_with_ports_capped)
# and let OCCT's boolean engine separate enclosed cavities (each its own entry in
# .Solids) from ambient (the single large outside solid). A void open to ambient
# fuses INTO ambient; an enclosed cavity falls out as a distinct solid. The
# bottleneck is then an analytic slice-area sweep along the inlet->outlet axis.


def _resolve_port_face(handle, shape, ref, label):
    """A face reference -> (Face, 1-based index). Accepts an f_* tag, a 'FaceN'
    string, an int index, or — once roles are declared with annotate_face — a
    face-role NAME or ROLE string (resolved through DP_FaceRoles via the stored
    tag, so it survives edits). Mirrors the inline resolution in oring_groove."""
    if ref is None:
        raise ValueError(f"{label} face reference is required")
    if isinstance(ref, str) and ref.startswith("f_"):
        idx = int(_h_resolve_face({"handle": handle, "tag": ref})["index"][len("Face"):])
    elif isinstance(ref, str) and ref.startswith("Face"):
        idx = int(ref[len("Face"):])
    else:
        try:
            idx = int(ref)
        except (TypeError, ValueError):
            idx = _resolve_face_role(handle, ref, label)
    if idx < 1 or idx > len(shape.Faces):
        raise ValueError(f"{label} face index {idx} out of range (1..{len(shape.Faces)})")
    return shape.Faces[idx - 1], idx


def _port_cap(part, face, label):
    """A solid 'plug' that seals a port opening: the face's outer boundary filled
    and extruded along the outward normal, with a small inward overlap so it fuses
    into the part (abutting caps stay disjoint and break the boolean). Returns
    (cap_solid, outward_normal)."""
    n = _outward_normal(face)
    try:
        plate = Part.Face(face.OuterWire)
    except Exception as e:
        raise ValueError(
            f"{label} port face is not cappable (need a planar opening rim): {e}"
        )
    depth = max(2.0, 0.05 * part.BoundBox.DiagonalLength)
    overlap = 0.5  # inward overlap so fuse() merges the cap into the wall
    inward = App.Vector(-n.x, -n.y, -n.z)
    cap = plate.translated(inward * overlap).extrude(n * (overlap + depth))
    return cap, n


def _section_area_at(solid, axis, d):
    """Sum of closed section-wire areas where `solid` meets the plane (axis, d).
    Reuses the section_view slice pattern."""
    area = 0.0
    for w in solid.slice(axis, d):
        if w.isClosed():
            try:
                area += Part.Face(w).Area
            except Exception:
                pass
    return area


@handler("check_airtight_path")
def _h_check_airtight_path(p):
    """Functional check for an enclosed-flow part: is there a single connected
    void joining the declared inlet to the outlet, bounded by solid everywhere
    else? This is what 'mostly airtight' means operationally, and it is what
    check_shape's watertight verdict CANNOT tell you — a watertight solid can
    still have a blocked path or a hidden leak. Pure inspection: mutates nothing.

    inlet / outlet: a face reference on `handle` naming each port OPENING (the rim
        face around the hole) — an f_* tag, 'FaceN', an int index, or a role/name
        declared with annotate_face (e.g. "inlet"). The check seals both ports with
        cap solids, builds the negative-space void as padded_bbox.cut(capped), and
        classifies the result.
    min_aperture_mm2 (optional): minimum acceptable bottleneck cross-section. When
        given, a connected-but-pinched path (a near-zero 'almond slit') fails.
    pad_mm (optional): bounding-box margin for the void box (default
        max(2.0, 0.05*diagonal)).

    Returns (lengths mm, areas mm², volumes mm³):
      ok                   (bool)  connected AND not leaky AND aperture >= threshold
      status               (str)   'airtight' | 'bottleneck' | 'blocked' | 'leaky'
      connected            (bool)  one void joins inlet and outlet
      leaky                (bool)  with both ports capped the cavity still reaches
                                   ambient => an unintended opening exists
      min_aperture_mm2     (float|null) narrowest section of the flow void
      bottleneck_point     ([x,y,z]|null) a point on the narrowest section plane
      flow_void_volume_mm3 (float|null) volume of the connecting void
      void_components      (int)   number of void solids (ambient + enclosed)
      inlet / outlet       (str)   the resolved 'FaceN' references
      pad_mm               (float) the margin used
    """
    handle = p["handle"]
    _, shape = _shape_of(handle)
    if not shape.isValid():
        raise RuntimeError(
            "shape is not valid (run check_shape first); cannot build a reliable void"
        )
    fin, i_in = _resolve_port_face(handle, shape, p.get("inlet"), "inlet")
    fout, i_out = _resolve_port_face(handle, shape, p.get("outlet"), "outlet")
    if i_in == i_out:
        raise ValueError("inlet and outlet resolve to the same face")
    min_aperture = p.get("min_aperture_mm2")
    if min_aperture is not None:
        min_aperture = float(min_aperture)

    incap, n_in = _port_cap(shape, fin, "inlet")
    outcap, n_out = _port_cap(shape, fout, "outlet")
    capped = shape.fuse(incap).fuse(outcap)
    try:
        capped = capped.removeSplitter()
    except Exception:
        pass

    cb = capped.BoundBox
    pad = float(p["pad_mm"]) if p.get("pad_mm") else max(2.0, 0.05 * cb.DiagonalLength)
    big = Part.makeBox(
        cb.XLength + 2 * pad, cb.YLength + 2 * pad, cb.ZLength + 2 * pad,
        App.Vector(cb.XMin - pad, cb.YMin - pad, cb.ZMin - pad),
    )
    void = big.cut(capped)
    solids = list(void.Solids)
    if not solids:
        raise RuntimeError("void computation produced no solids (degenerate geometry)")

    # ambient = the single large outside void; tie-break by centroid so the pick
    # is deterministic when two void solids happen to share a volume.
    def _amb_key(i):
        s = solids[i]
        c = s.CenterOfMass
        return (round(s.Volume, 6), round(c.x, 6), round(c.y, 6), round(c.z, 6))
    amb_idx = max(range(len(solids)), key=_amb_key)
    ambient = solids[amb_idx]
    enclosed = [s for i, s in enumerate(solids) if i != amb_idx]

    # interior probe points: just inside each opening, past the cap overlap.
    def _interior(face, n):
        c = face.CenterOfMass
        return c - App.Vector(n.x, n.y, n.z) * 1.0
    pin = _interior(fin, n_in)
    pout = _interior(fout, n_out)
    if capped.isInside(pin, 1e-6, True):
        raise ValueError(
            "inlet does not open into a void (not an opening, or wall too thick "
            "behind the rim)"
        )
    if capped.isInside(pout, 1e-6, True):
        raise ValueError(
            "outlet does not open into a void (not an opening, or wall too thick "
            "behind the rim)"
        )

    def _host(pt):
        for i, s in enumerate(enclosed):
            if s.isInside(pt, 1e-6, True):
                return ("enclosed", i)
        if ambient.isInside(pt, 1e-6, True):
            return ("ambient", -1)
        return ("none", -2)
    hin, hout = _host(pin), _host(pout)

    same_enclosed = hin[0] == "enclosed" and hin == hout
    leaky = hin[0] == "ambient" or hout[0] == "ambient"
    connected = same_enclosed or (hin[0] == "ambient" and hout[0] == "ambient")

    min_ap = None
    bottleneck = None
    flow_vol = None
    if same_enclosed:
        fv = enclosed[hin[1]]
        flow_vol = fv.Volume
        axis = pout - pin
        if axis.Length > 1e-9:
            axis.normalize()
            d0, d1 = pin.dot(axis), pout.dot(axis)
            best = None
            n_stations = 40
            for k in range(1, n_stations):
                d = d0 + (d1 - d0) * k / n_stations
                a = _section_area_at(fv, axis, d)
                if a > 1e-9 and (best is None or a < best[0]):
                    best = (a, d)
            if best is not None:
                min_ap = best[0]
                t = best[1] - d0
                bottleneck = [
                    round(pin.x + axis.x * t, 6),
                    round(pin.y + axis.y * t, 6),
                    round(pin.z + axis.z * t, 6),
                ]

    if leaky:
        status = "leaky"
    elif not connected:
        status = "blocked"
    elif min_aperture is not None and min_ap is not None and min_ap < min_aperture:
        status = "bottleneck"
    else:
        status = "airtight"
    ok = bool(
        connected and not leaky
        and (min_aperture is None or (min_ap is not None and min_ap >= min_aperture))
    )

    return {
        "ok": ok,
        "status": status,
        "connected": bool(connected),
        "leaky": bool(leaky),
        "min_aperture_mm2": None if min_ap is None else round(min_ap, 6),
        "bottleneck_point": bottleneck,
        "flow_void_volume_mm3": None if flow_vol is None else round(flow_vol, 6),
        "void_components": len(solids),
        "inlet": f"Face{i_in}",
        "outlet": f"Face{i_out}",
        "pad_mm": round(pad, 6),
    }


@handler("classify_face_sides")
def _h_classify_face_sides(p):
    """Inside-vs-outside topology: for every face, which void does its outward
    side open into — an enclosed cavity (wetted) or ambient (exterior)? Answers
    the "which faces are inside the airflow path" question from issue #19 and
    auto-suggests a role per face. Pure inspection; mutates nothing.

    Method: build the negative-space void (padded_bbox.cut(part)) and split it
    into ambient (the one large outside solid) and any enclosed cavities; probe
    each face just off its outward normal and see which it lands in. With
    seal_ports=True (default) declared inlet/outlet roles (annotate_face) are
    capped first, so an OPEN duct's bore reads as the enclosed flow cavity rather
    than as ambient.

    handle: the part. seal_ports: cap declared inlet/outlet before classifying.

    Returns a list (one per face) of dicts:
      tag / index      (str)   stable f_* tag and 'FaceN'
      kind             (str)   surface kind (planar/cylindrical/…)
      side             (str)   'interior' (bounds an enclosed void) | 'ambient' |
                               'ambiguous' (probe inconclusive, e.g. a capped port)
      suggested_role   (str)   'wetted' for interior, 'ambient' for exterior, else null
      declared_role    (str)   the role already annotated on this face, if any
    """
    handle = p["handle"]
    obj, shape = _shape_of(handle)
    roles = _read_face_roles(obj)

    solid = shape
    if p.get("seal_ports", True):
        caps = []
        for name, e in roles.items():
            if e.get("role") in ("inlet", "outlet"):
                try:
                    f, _ = _resolve_port_face(handle, shape, e["tag"], name)
                    cap, _n = _port_cap(shape, f, name)
                    caps.append(cap)
                except Exception:
                    pass  # a drifted/uncappable port just isn't sealed
        for c in caps:
            solid = solid.fuse(c)
        if caps:
            try:
                solid = solid.removeSplitter()
            except Exception:
                pass

    bb = solid.BoundBox
    pad = max(2.0, 0.05 * bb.DiagonalLength)
    big = Part.makeBox(
        bb.XLength + 2 * pad, bb.YLength + 2 * pad, bb.ZLength + 2 * pad,
        App.Vector(bb.XMin - pad, bb.YMin - pad, bb.ZMin - pad),
    )
    solids = list(big.cut(solid).Solids)
    if not solids:
        raise RuntimeError("void computation produced no solids (degenerate geometry)")

    def _amb_key(i):
        s = solids[i]
        c = s.CenterOfMass
        return (round(s.Volume, 6), round(c.x, 6), round(c.y, 6), round(c.z, 6))
    amb_idx = max(range(len(solids)), key=_amb_key)
    ambient = solids[amb_idx]
    enclosed = [s for i, s in enumerate(solids) if i != amb_idx]

    tag2role = {e["tag"]: e["role"] for e in roles.values() if "tag" in e}
    eps = max(0.01, 1e-3 * bb.DiagonalLength)
    out = []
    for i, f in enumerate(shape.Faces):
        sig = _face_signature(f)
        tag = f"f_{_hash_sig(sig)}"
        n = _outward_normal(f)
        probe = f.CenterOfMass + App.Vector(n.x, n.y, n.z) * eps
        if any(s.isInside(probe, 1e-6, True) for s in enclosed):
            side, suggest = "interior", "wetted"
        elif ambient.isInside(probe, 1e-6, True):
            side, suggest = "ambient", "ambient"
        else:
            side, suggest = "ambiguous", None
        item = {"tag": tag, "index": f"Face{i + 1}", "kind": sig["kind"],
                "side": side, "suggested_role": suggest}
        if tag in tag2role:
            item["declared_role"] = tag2role[tag]
        out.append(item)
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
        import Mesh  # noqa: F401  (side-effect: registers the Mesh module MeshPart needs)
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
            "Revolution recompute produced a null shape — sketch may be "
            "open, self-intersecting, or the axis/profile configuration is "
            "unsupported."
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


# --- semantic face roles (issue #19) -----------------------------------------
#
# Declare WHAT a face is FOR — "inlet", "outlet", "sealing", ... — so later edits
# can be checked against intent instead of re-derived from raw geometry. Roles
# bind to the stable f_* face tag (which survives edits) and persist as a JSON
# property bag, mirroring publish_interface's DP_Interfaces exactly. The stored
# signature snapshot lets a later check (verify_intent, slice 4) detect a tagged
# face that has drifted or vanished. check_airtight_path resolves a role/name
# string back to the current face through the stored tag.

_FACEROLE_PROP = "DP_FaceRoles"
_FACE_ROLES = ("inlet", "outlet", "sealing", "wetted", "ambient", "mating")


def _read_face_roles(obj):
    """Declared face-role dict for an object ({} if none)."""
    import json as _json
    base = _shaped_top(obj)
    if _FACEROLE_PROP in base.PropertiesList:
        try:
            return _json.loads(getattr(base, _FACEROLE_PROP) or "{}")
        except Exception:
            return {}
    return {}


def _unique_role_name(roles, role):
    """A free key for a new annotation: the role itself, else role_2, role_3, …"""
    if role not in roles:
        return role
    i = 2
    while f"{role}_{i}" in roles:
        i += 1
    return f"{role}_{i}"


def _resolve_face_role(handle, ref, label):
    """Resolve a face-role NAME (preferred) or ROLE string to a current 1-based
    face index via the stored tag. Raises if unknown or (for a role) ambiguous."""
    roles = _read_face_roles(_resolve(handle))
    if not roles:
        raise ValueError(
            f"{label}={ref!r} is not a face tag/'FaceN'/index, and no face roles "
            f"are declared on {handle!r} (use annotate_face first)"
        )
    if ref in roles:
        tag = roles[ref]["tag"]
    else:
        matches = [n for n, e in roles.items() if e.get("role") == ref]
        if not matches:
            raise ValueError(
                f"{label}={ref!r}: no annotation with that name or role "
                f"(declared: {sorted(roles)})"
            )
        if len(matches) > 1:
            raise ValueError(
                f"{label} role {ref!r} is ambiguous across {sorted(matches)}; "
                f"pass a specific annotation name or a face tag"
            )
        tag = roles[matches[0]]["tag"]
    return int(_h_resolve_face({"handle": handle, "tag": tag})["index"][len("Face"):])


@handler("annotate_face")
def _h_annotate_face(p):
    """Declare the semantic ROLE of a face — what it is FOR — so edits can be
    checked against intent. Persists in the .FCStd as a JSON property bag keyed by
    a unique annotation name; survives save/reopen. The role binds to the face's
    stable f_* tag, and a signature snapshot is stored so a later check can flag a
    tagged face that has drifted or vanished.

    handle: the part.
    face: an f_* tag, 'FaceN', or int index of the face to annotate.
    role: one of inlet | outlet | sealing | wetted | ambient | mating.
    name: optional unique label for this annotation (default: the role, then
        role_2, role_3, …). Re-using a name updates that annotation.
    meta: optional dict stored verbatim (e.g. {"spec": "32mm hose", "od": 32}).

    Returns {handle, name, role, tag, index, roles} — roles is the sorted list of
    all annotation names now on the part."""
    import json as _json
    handle = p["handle"]
    obj, shape = _shape_of(handle)
    face, idx = _resolve_port_face(handle, shape, p.get("face"), "face")
    role = p.get("role")
    if role not in _FACE_ROLES:
        raise ValueError(f"role {role!r} not in {list(_FACE_ROLES)}")
    sig = _face_signature(face)
    tag = f"f_{_hash_sig(sig)}"
    roles = _read_face_roles(obj)
    name = p.get("name") or _unique_role_name(roles, role)
    entry = {"role": role, "tag": tag, "index": f"Face{idx}", "signature": sig}
    meta = p.get("meta")
    if meta:
        entry["meta"] = meta
    roles[name] = entry
    base = _shaped_top(obj)
    if _FACEROLE_PROP not in base.PropertiesList:
        base.addProperty("App::PropertyString", _FACEROLE_PROP, "DriftPin",
                         "semantic face roles (JSON)")
    setattr(base, _FACEROLE_PROP, _json.dumps(roles))
    base.Document.recompute()
    return {"handle": handle, "name": name, "role": role, "tag": tag,
            "index": f"Face{idx}", "roles": sorted(roles)}


@handler("list_face_roles")
def _h_list_face_roles(p):
    """Read back the semantic face roles declared on a part (see annotate_face).
    Each entry re-resolves its stored tag against the CURRENT geometry, so
    `present` is False when the tagged face has drifted or vanished since it was
    annotated — the cheap drift signal the regression gate builds on.

    Returns a list of {name, role, tag, present, index?, meta?}, sorted by name."""
    handle = p["handle"]
    obj, shape = _shape_of(handle)
    roles = _read_face_roles(obj)
    current = {f"f_{_hash_sig(_face_signature(f))}" for f in shape.Faces}
    out = []
    for name in sorted(roles):
        e = roles[name]
        tag = e.get("tag")
        item = {"name": name, "role": e.get("role"), "tag": tag,
                "present": tag in current}
        if item["present"]:
            try:
                item["index"] = _h_resolve_face({"handle": handle, "tag": tag})["index"]
            except Exception:
                item["present"] = False
        if "meta" in e:
            item["meta"] = e["meta"]
        out.append(item)
    return out


# --- declared intent + re-runnable regression gate (issue #19) ---------------
#
# Record the functional invariants of a part ONCE, then re-run them after every
# edit — the regression check the issue calls out as missing. The contract
# composes the slice 1-3 primitives (check_shape, check_airtight_path, face-role
# presence) and persists as a JSON property bag (DP_Intent), like DP_Interfaces /
# DP_FaceRoles. verify_intent never raises on a failing invariant: a failure
# becomes a {passed: False} row so the gate is safe to run in a loop.

_INTENT_PROP = "DP_Intent"


def _read_intent(obj):
    """Declared intent contract for an object ({} if none)."""
    import json as _json
    base = _shaped_top(obj)
    if _INTENT_PROP in base.PropertiesList:
        try:
            return _json.loads(getattr(base, _INTENT_PROP) or "{}")
        except Exception:
            return {}
    return {}


@handler("declare_intent")
def _h_declare_intent(p):
    """Record the functional invariants a part must keep satisfying, so they can
    be re-checked after every edit (see verify_intent). Persists in the .FCStd as
    a JSON property bag (DP_Intent); one contract per part, re-declaring replaces.

    contract keys (all optional, but declare at least one):
      watertight     (bool)  require check_shape's watertight_solid verdict.
      airtight_path  (dict)  {inlet, outlet, min_aperture_mm2?} — each port is a
                             face tag / 'FaceN' / int / declared role-or-name.
      required_faces (list)  face tags / 'FaceN' / declared role-or-names that
                             must still resolve (catches a deleted/drifted face).

    Returns {handle, contract} (the stored contract)."""
    import json as _json
    handle = p["handle"]
    obj, _ = _shape_of(handle)
    contract = dict(p.get("contract") or {})
    if not contract:
        raise ValueError("contract is empty; declare at least one invariant")
    ap = contract.get("airtight_path")
    if ap is not None and ("inlet" not in ap or "outlet" not in ap):
        raise ValueError("airtight_path requires both 'inlet' and 'outlet'")
    base = _shaped_top(obj)
    if _INTENT_PROP not in base.PropertiesList:
        base.addProperty("App::PropertyString", _INTENT_PROP, "DriftPin",
                         "declared functional intent (JSON)")
    setattr(base, _INTENT_PROP, _json.dumps(contract))
    base.Document.recompute()
    return {"handle": handle, "contract": contract}


@handler("verify_intent")
def _h_verify_intent(p):
    """Re-run every invariant declared with declare_intent — the regression gate
    to run after each edit. Composes check_shape / check_airtight_path / face-role
    resolution. Never raises on a failing invariant (a failure is a passed=False
    row), so it is safe to call in a loop. Pure inspection; mutates nothing.

    Returns {handle, ok, results} where results is a list of
      {invariant, passed, detail} (one per declared invariant) and ok is True iff
    every invariant passed."""
    handle = p["handle"]
    obj, shape = _shape_of(handle)
    contract = _read_intent(obj)
    if not contract:
        raise ValueError(f"no intent declared on {handle!r} (use declare_intent first)")

    results = []

    def _add(name, fn):
        try:
            passed, detail = fn()
        except Exception as e:
            passed, detail = False, f"{type(e).__name__}: {e}"
        results.append({"invariant": name, "passed": bool(passed), "detail": detail})

    if contract.get("watertight"):
        def _w():
            r = _h_check_shape({"handle": handle})
            return r["watertight_solid"], (
                f"solids={r['solids']}, closed={r['closed']}, valid={r['valid']}")
        _add("watertight", _w)

    ap = contract.get("airtight_path")
    if ap:
        def _a():
            r = _h_check_airtight_path({
                "handle": handle, "inlet": ap["inlet"], "outlet": ap["outlet"],
                "min_aperture_mm2": ap.get("min_aperture_mm2")})
            return r["ok"], (
                f"status={r['status']}, connected={r['connected']}, "
                f"leaky={r['leaky']}, min_aperture_mm2={r['min_aperture_mm2']}")
        _add("airtight_path", _a)

    req = contract.get("required_faces")
    if req:
        def _r():
            missing = []
            for ref in req:
                try:
                    _resolve_port_face(handle, shape, ref, "required_face")
                except Exception:
                    missing.append(ref)
            return (not missing), (
                "all present" if not missing else f"missing/drifted: {missing}")
        _add("required_faces", _r)

    ok = all(r["passed"] for r in results) if results else True
    return {"handle": handle, "ok": ok, "results": results}


# --- verify_contract: build-time self-check against a component's slice (§11.3) -
#
# A builder calls this on its OWN part before saving, checking it against its slice
# of the manifest — so a contract violation is caught locally and cheaply instead
# of after a fan-in merge (build → merge → gate-fail → rebuild becomes build →
# self-check → fix). It unifies pieces that exist separately at other altitudes:
# envelope_check (assembly-level) → a LOCAL bbox check here; the published-frame
# contract that interface_align checks post-merge → a "did I publish it, in the
# right place?" check here; plus per-feature self-checks (a gear's module, a bore's
# diameter, an overall extent) and a verify_intent passthrough. Like verify_intent
# it NEVER raises on a failing check (a failure is a passed=False row), so a builder
# can call it in a loop.

def _vc_feature_check(shape, feat):
    """One self-checkable feature contract -> (passed, detail). Kinds:
      gear   {module_mm, teeth, [internal], tol_mm?}  measured pitch radius
             (tip∓module) == module*teeth/2
      bore   {diameter_mm, tol_mm?}                    a cylindrical face of the
             nominal radius is present (the hole was actually cut)
      extent {axis: x|y|z, length_mm, tol_mm?}         bbox span along axis"""
    import math
    kind = feat.get("kind")
    tol = float(feat.get("tol_mm", 0.5))
    if kind == "gear":
        m = float(feat["module_mm"])
        rp = _gear_pitch_radius(shape, m, bool(feat.get("internal")))
        if rp is None:
            return False, "no vertices to measure a gear"
        want = m * float(feat["teeth"]) / 2.0
        return abs(rp - want) <= tol, (
            f"pitch radius {rp:.3f} vs module·teeth/2 = {want:.3f} (tol {tol})")
    if kind == "bore":
        d = float(feat["diameter_mm"])
        radii = [f.Surface.Radius for f in shape.Faces
                 if type(f.Surface).__name__ == "Cylinder"]
        hit = [r for r in radii if abs(2 * r - d) <= tol]
        return bool(hit), (
            f"Ø{d} bore present (cyl radii {[round(r,3) for r in radii]})"
            if hit else f"no cylindrical face at Ø{d} (found {[round(r,3) for r in radii]})")
    if kind == "extent":
        ax = feat.get("axis", "x")
        bb = shape.BoundBox
        span = {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength}[ax]
        want = float(feat["length_mm"])
        return abs(span - want) <= tol, (
            f"{ax}-extent {span:.3f} vs {want:.3f} (tol {tol})")
    return False, f"unknown feature kind {kind!r}"


@handler("verify_contract")
def _h_verify_contract(p):
    """Build-time self-check of a component against its manifest slice (§11.3).
    Returns {handle, ok, results:[{check, passed, detail}]} like verify_intent and
    never raises on a failing check, so a builder can call it before save and loop.

    handle:   the component's shaped object.
    contract: the component's slice (all keys optional, give at least one):
      envelope   {min:[x,y,z], max:[x,y,z]}   LOCAL bbox must fit inside it.
      interfaces {name: {origin:[...], z_axis?:[...], tol_mm?, angle_tol_deg?}}
                 each named frame must be PUBLISHED and within tolerance of the
                 contracted origin (and axis, if z_axis given) — catches "forgot to
                 publish" and "published in the wrong place", the usual merge/align
                 failures, locally.
      features   [ {kind:"gear"|"bore"|"extent", ...} ]  per-feature self-checks.
      intent     bool                          run verify_intent too.
    """
    import math
    handle = p["handle"]
    obj, shape = _shape_of(handle)
    contract = dict(p.get("contract") or {})
    results = []

    def _add(name, fn):
        try:
            passed, detail = fn()
        except Exception as e:
            passed, detail = False, f"{type(e).__name__}: {e}"
        results.append({"check": name, "passed": bool(passed), "detail": detail})

    env = contract.get("envelope")
    if env:
        def _env():
            bb = shape.BoundBox
            got = {"min": [bb.XMin, bb.YMin, bb.ZMin],
                   "max": [bb.XMax, bb.YMax, bb.ZMax]}
            eps = 1e-6
            bad = []
            for i, ax in enumerate("xyz"):
                if got["min"][i] < env["min"][i] - eps or got["max"][i] > env["max"][i] + eps:
                    bad.append(f"{ax}:[{got['min'][i]:.2f},{got['max'][i]:.2f}]"
                               f"⊄[{env['min'][i]},{env['max'][i]}]")
            return (not bad), ("local bbox fits" if not bad else "; ".join(bad))
        _add("envelope", _env)

    ifaces_contract = contract.get("interfaces") or {}
    if ifaces_contract:
        published = _read_interfaces(obj)
        for name, spec in ifaces_contract.items():
            def _iface(name=name, spec=spec):
                if name not in published:
                    return False, f"interface {name!r} not published"
                fr = published[name]
                tol = float(spec.get("tol_mm", 0.5))
                o_got = App.Vector(*fr.get("origin", [0, 0, 0]))
                o_want = App.Vector(*spec.get("origin", [0, 0, 0]))
                gap = (o_got - o_want).Length
                if gap > tol:
                    return False, f"{name} origin off by {gap:.3f} mm (tol {tol})"
                if spec.get("z_axis"):
                    za = App.Vector(*fr.get("z_axis", [0, 0, 1])); za.normalize()
                    zw = App.Vector(*spec["z_axis"]); zw.normalize()
                    ang = math.degrees(math.acos(max(-1.0, min(1.0, za.dot(zw)))))
                    lim = float(spec.get("angle_tol_deg", 1.0))
                    if ang > lim:
                        return False, f"{name} axis off by {ang:.2f}° (tol {lim})"
                return True, f"{name} published within tolerance"
            _add(f"interface:{name}", _iface)

    for feat in contract.get("features") or []:
        _add(f"feature:{feat.get('name', feat.get('kind'))}",
             lambda feat=feat: _vc_feature_check(shape, feat))

    if contract.get("intent"):
        def _intent():
            r = _h_verify_intent({"handle": handle})
            return r["ok"], f"{sum(x['passed'] for x in r['results'])}/" \
                            f"{len(r['results'])} invariants passed"
        _add("intent", _intent)

    ok = all(r["passed"] for r in results) if results else True
    return {"handle": handle, "ok": ok, "results": results}


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
        if "library" in spec:
            # §11.5: a generated standard part. Its lock identity is the spec hash
            # (it is computed, not designed), so it never reads as `modified` unless
            # the SPEC changes — the "one side of the contract can't drift" guarantee.
            lib = spec["library"]
            state[cid] = {"library": lib.get("tool"),
                          "file_hash": _library_spec_hash(lib["tool"],
                                                           lib.get("spec", {})),
                          "interfaces_hash": "", "depends_on": sorted(deps[cid])}
            continue
        if "manifest" in spec:
            # §11.4: a subassembly node. Its "file" is the child's merged root, and
            # its provenance is the child's lockfile — so a change anywhere in the
            # child tree (a re-merge updates the root bytes; a child interface move
            # updates the child lockfile) propagates UP to this parent entry, and
            # `depends_on` carries it to the parent's neighbors that mate the subasm.
            cm = spec["manifest"]
            cm = cm if _os.path.isabs(cm) else _os.path.join(base_dir, cm)
            try:
                with open(cm) as cf:
                    child_man = _json.load(cf)
            except Exception:
                child_man = {}
            child_base = _os.path.dirname(cm)
            child_root = child_man.get("root") or (child_man.get("name", "merged") + ".FCStd")
            child_root = child_root if _os.path.isabs(child_root) \
                else _os.path.join(child_base, child_root)
            child_lock = _os.path.splitext(cm)[0] + ".lock.json"
            fh = _hash_file_bytes(child_root) if _os.path.exists(child_root) else ""
            prov = child_lock if _os.path.exists(child_lock) else cm
            ih = _hash_file_bytes(prov) if _os.path.exists(prov) else ""
            state[cid] = {"file": _os.path.relpath(child_root, base_dir),
                          "file_hash": fh, "interfaces_hash": ih,
                          "depends_on": sorted(deps[cid]),
                          "child_manifest": _os.path.basename(cm)}
            continue
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


# --- typed-interface gates (RFC §11.2) ---------------------------------------
#
# An untyped interface is just a frame; a TYPED check carries its contract fields
# and a geometric gate dispatched by `kind`. The manifest gains a `checks` list
# of {kind, ...refs..., ...contract...}; merge_assembly resolves each ref to its
# placed link and runs the kind's gate against the assembled geometry. Each gate
# returns a list of VIOLATIONS (empty == pass), the same shape as the existing
# interference / envelope / interface_align gates, so a coordinator branches on
# them mechanically. The gate logic is promoted from the M2 eval helpers that
# proved it (gear-mesh pitch sums, min-clearance fits) plus the orientation check
# that interface_align (origins only) does not cover.

def _link_world_shape(link):
    """World-space shape of a top-level assembly link (LinkedObject geometry
    transformed by the link's placement). None if the link carries no shape."""
    base = (link.LinkedObject if (link.isDerivedFrom("App::Link")
                                  and link.LinkedObject is not None) else link)
    if not (hasattr(base, "Shape") and not base.Shape.isNull()):
        return None
    return base.Shape.transformed(link.Placement.Matrix)


def _link_local_shape(link):
    """Local (unplaced) shape of a link's target — for axisymmetric measurements
    (a gear's pitch radius) that are taken about the part's own axis."""
    base = (link.LinkedObject if (link.isDerivedFrom("App::Link")
                                  and link.LinkedObject is not None) else link)
    if not (hasattr(base, "Shape") and not base.Shape.isNull()):
        return None
    return base.Shape


def _gear_pitch_radius(shape, module, internal=False):
    """Pitch radius of an involute gear from its as-built teeth, about its own
    centroid axis: external rp = tip_radius − module; internal rp = inner_tip +
    module (mirrors the M2 gearbox oracle, which reads rp back from geometry so a
    wrong tooth count is caught by the measured radius, not trusted from input)."""
    import math
    com = shape.CenterOfMass
    radii = [math.hypot(v.X - com.x, v.Y - com.y) for v in shape.Vertexes]
    if not radii:
        return None
    return (min(radii) + module) if internal else (max(radii) - module)


def _axis_world(link):
    """(point, unit-direction) of a link's local +Z axis in world space — the
    rotation axis of a gear/shaft placed into the assembly."""
    pl = link.Placement
    base = pl.Base
    d = pl.Rotation.multVec(App.Vector(0, 0, 1))
    d.normalize()
    return base, d


def _parallel_axis_distance(shape_a, dir_a, shape_b):
    """Perpendicular distance between two (near-)parallel part axes, taken
    through their world centroids — the as-placed centre distance of a gear pair."""
    ca, cb = shape_a.CenterOfMass, shape_b.CenterOfMass
    dv = cb - ca
    return (dv - dir_a.multiply(dv.dot(dir_a))).Length


def _gate_bore_fit(by_name, links_by_inst, chk):
    """Clearance-fit gate: the pin must sit in the bore with clearance inside the
    contracted band. Closes the exact-touch blind spot (§6) — interference_check
    reads ZERO for tangent solids, so a slip fit MUST be gated on minimum
    clearance, not on non-interference. min_clearance_mm required; max optional."""
    pin = by_name.get(links_by_inst.get(chk["pin"], chk["pin"]))
    bore = by_name.get(links_by_inst.get(chk["bore"], chk["bore"]))
    if pin is None or bore is None:
        return [{**chk, "error": "pin/bore link not found"}]
    sp, sb = _link_world_shape(pin), _link_world_shape(bore)
    if sp is None or sb is None:
        return [{**chk, "error": "pin/bore has no shape"}]
    lo = float(chk["min_clearance_mm"])
    hi = chk.get("max_clearance_mm")
    try:
        overlap = sp.common(sb).Volume
    except Exception:
        overlap = 0.0
    if overlap > 1e-9:
        return [{**chk, "status": "interference", "clearance_mm": 0.0,
                 "overlap_mm3": round(overlap, 4),
                 "reason": f"{chk['pin']} interferes with {chk['bore']} "
                           f"(too tight; need ≥{lo} mm clearance)"}]
    gap = round(sp.distToShape(sb)[0], 6)
    if gap < lo - 1e-6:
        return [{**chk, "status": "clear" if gap > 1e-7 else "contact",
                 "clearance_mm": gap,
                 "reason": f"{chk['pin']}↔{chk['bore']} clearance {gap:.4f} mm "
                           f"< min {lo} mm" + (" (exact-touch)" if gap <= 1e-7 else "")}]
    if hi is not None and gap > float(hi) + 1e-6:
        return [{**chk, "status": "clear", "clearance_mm": gap,
                 "reason": f"{chk['pin']}↔{chk['bore']} clearance {gap:.4f} mm "
                           f"> max {hi} mm (too loose)"}]
    return []


def _gate_gear_mesh(by_name, links_by_inst, chk):
    """Gear-mesh gate (external pair): the two gears' pitch radii must sum to the
    contracted centre distance, the as-placed axes must actually sit at that
    distance, and (if given) the ratio must hit target. The canonical
    shared-constraint partition — each builder sizes its gear so the pair meshes
    at one shared C (the M2 gearbox oracle, promoted)."""
    a = by_name.get(links_by_inst.get(chk["a"], chk["a"]))
    b = by_name.get(links_by_inst.get(chk["b"], chk["b"]))
    if a is None or b is None:
        return [{**chk, "error": "gear link not found"}]
    if chk.get("a_internal") or chk.get("b_internal"):
        return [{**chk, "error": "internal-gear mesh not supported in v0 "
                                 "(external pair only)"}]
    la, lb = _link_local_shape(a), _link_local_shape(b)
    if la is None or lb is None:
        return [{**chk, "error": "gear has no shape"}]
    m = float(chk["module_mm"])
    C = float(chk["center_distance_mm"])
    tol = float(chk.get("tol_mm", 0.5))
    rpa = _gear_pitch_radius(la, m)
    rpb = _gear_pitch_radius(lb, m)
    out = []
    if abs((rpa + rpb) - C) > tol:
        out.append({**chk, "rp_a": round(rpa, 4), "rp_b": round(rpb, 4),
                    "reason": f"pitch radii sum {rpa+rpb:.3f} != centre distance "
                              f"{C:g} mm (pair will not mesh)"})
    _, da = _axis_world(a)
    measured_C = _parallel_axis_distance(_link_world_shape(a), da,
                                         _link_world_shape(b))
    if abs(measured_C - C) > tol:
        out.append({**chk, "measured_center_distance_mm": round(measured_C, 4),
                    "reason": f"as-placed centre distance {measured_C:.3f} != "
                              f"{C:g} mm"})
    if "ratio" in chk and rpa > 1e-9:
        r = rpb / rpa
        rt = float(chk["ratio"])
        if abs(r - rt) > 0.05 * rt + 0.02:
            out.append({**chk, "measured_ratio": round(r, 4),
                        "reason": f"ratio {r:.3f} != {rt:g}"})
    return out


def _gate_frame_orientation(by_name, links_by_inst, chk):
    """Orientation gate for a mated frame pair: the published child/parent frames
    must be ANGULARLY aligned, not merely coincident in origin. interface_align
    checks origins only — a frame positioned right but rotated passes it — so a
    keyed/clocked interface needs this. max_angle_deg (default 1.0)."""
    import math
    c = by_name.get(links_by_inst.get(chk["child"], chk["child"]))
    pa = by_name.get(links_by_inst.get(chk["parent"], chk["parent"]))
    if c is None or pa is None:
        return [{**chk, "error": "child/parent link not found"}]
    cif, pif = _read_interfaces(c), _read_interfaces(pa)
    ci, pi = chk["child_iface"], chk["parent_iface"]
    if ci not in cif or pi not in pif:
        return [{**chk, "error": "interface not published"}]
    cw = c.LinkPlacement.multiply(_frame_to_placement(cif[ci]))
    pw = pa.LinkPlacement.multiply(_frame_to_placement(pif[pi]))
    zc = cw.Rotation.multVec(App.Vector(0, 0, 1))
    zp = pw.Rotation.multVec(App.Vector(0, 0, 1))
    cosang = max(-1.0, min(1.0, zc.dot(zp)))
    ang = math.degrees(math.acos(cosang))
    lim = float(chk.get("max_angle_deg", 1.0))
    if ang > lim:
        return [{**chk, "angle_deg": round(ang, 4),
                 "reason": f"{chk['child']}.{ci} axis off {chk['parent']}.{pi} "
                           f"by {ang:.2f}° (> {lim}°)"}]
    return []


_TYPED_GATES = {
    "bore_fit": _gate_bore_fit,
    "gear_mesh": _gate_gear_mesh,
    "frame_orientation": _gate_frame_orientation,
}

# Typed kinds whose two parts are MEANT to be in contact / interpenetrating in a
# static pose, so the blunt interference gate must not also flag them: meshing
# involute teeth overlap at the pitch line (the eval's "posed interference isn't
# the right test for a gear ratio" finding). The typed gate is authoritative for
# that pair; any other pair still gets the normal interference check. A check of
# any kind can opt in with "expected_contact": true (e.g. a press_fit band).
_CONTACT_KINDS = {"gear_mesh"}


def _check_pair(chk):
    """The two instance refs a typed check relates, by kind ((a,b) / (pin,bore)
    / (child,parent))."""
    if "a" in chk and "b" in chk:
        return chk["a"], chk["b"]
    if "pin" in chk and "bore" in chk:
        return chk["pin"], chk["bore"]
    if "child" in chk and "parent" in chk:
        return chk["child"], chk["parent"]
    return None, None


def _contact_exclusions(checks, links_by_inst):
    """Link-name pairs the interference gate should skip because a typed check
    owns them as expected contact."""
    excl = set()
    for chk in checks:
        if chk.get("kind") in _CONTACT_KINDS or chk.get("expected_contact"):
            ra, rb = _check_pair(chk)
            if ra is not None and rb is not None:
                excl.add(frozenset((links_by_inst.get(ra, ra),
                                    links_by_inst.get(rb, rb))))
    return excl


def _run_typed_checks(checks, by_name, links_by_inst):
    """Dispatch each manifest `checks` entry to its kind's gate. Returns the flat
    list of violations across all checks (empty == every typed contract holds).
    An unknown kind is itself a violation — a typed contract that silently does
    not run is worse than one that fails loudly."""
    out = []
    for chk in checks:
        gate = _TYPED_GATES.get(chk.get("kind"))
        if gate is None:
            out.append({**chk, "error": f"unknown typed-interface kind "
                                        f"{chk.get('kind')!r}"})
            continue
        try:
            out.extend(gate(by_name, links_by_inst, chk))
        except Exception as e:
            out.append({**chk, "error": f"{type(e).__name__}: {e}"})
    return out


# --- standard / library parts (RFC §11.5) ------------------------------------
#
# A component can be GENERATED from a spec instead of built by an agent: the tool
# surface already makes standard parts deterministically (fasteners, bearings,
# gears, ...). merge_assembly generates these on the fly from the manifest — no
# builder, no owner, no file an agent has to produce — which shrinks the fan-out
# and anchors interfaces (one side of the contract is computed, not designed, so
# it can't drift). The allow-list keeps this to the deterministic generators; an
# arbitrary tool name is rejected rather than run.

_LIBRARY_TOOLS = {"add_fastener", "add_bearing", "add_gear", "add_spring",
                  "add_sprocket", "add_pulley", "add_rack", "add_thread"}


def _library_spec_hash(tool, spec):
    """Stable identity of a generated part: a hash of (tool, spec). This — not the
    saved bytes — is the part's lock identity, since it is computed, not designed."""
    import hashlib
    import json as _json
    blob = _json.dumps({"tool": tool, "spec": spec}, sort_keys=True)
    return hashlib.blake2b(blob.encode(), digest_size=8).hexdigest()


def _library_part_path(base_dir, tool, spec):
    """Deterministic cache path for a generated part (same spec → same file, so two
    components referencing the same bolt share one generated file). The filename
    carries a readable spec slug so the BOM (which keys on the file stem) is legible,
    plus a short hash so distinct specs never collide."""
    import os as _os
    d = _os.path.join(base_dir, ".dp_lib")
    _os.makedirs(d, exist_ok=True)
    slug = "-".join(str(v) for v in spec.values()
                    if isinstance(v, (str, int, float)) and not isinstance(v, bool))
    h = _library_spec_hash(tool, spec)[:6]
    stem = f"{tool}_{slug}_{h}" if slug else f"{tool}_{h}"
    return _os.path.join(d, stem + ".FCStd")


def _generate_library_part(tool, spec, path):
    """Generate a standard part into its own document and save it to `path`.
    Raises on a non-allow-listed tool or a bad spec — a library contract that
    can't be generated must fail loudly at merge, not silently vanish."""
    if tool not in _LIBRARY_TOOLS:
        raise ValueError(
            f"library tool {tool!r} is not an allowed standard-part generator "
            f"(expected one of {sorted(_LIBRARY_TOOLS)})")
    HANDLERS["new_document"]({"name": "_dp_lib"})
    HANDLERS[tool](dict(spec))
    HANDLERS["save_document"]({"path": path})


# --- requirements gates (RFC §11.6) ------------------------------------------
#
# "The pieces fit" is not "the product works." The manifest may carry a
# `requirements` block gated at merge. v0 ships the ALWAYS-ON, cheap tier built on
# shipped machinery — total mass against a budget and centre-of-mass inside a
# window (mass_properties + the recursive leaf walk). Returns {report, violations}:
# `report` is the measured numbers (so a coordinator sees them on a pass too),
# `violations` is the failing requirements (empty == all met). An unrecognised
# requirement key is reported as `skipped`, never silently dropped — the
# expensive physics tier (min_first_mode_hz via FEM, with its bonding / boundary-
# condition modelling) is deferred, so naming it here surfaces as skipped, not as
# a silent pass.

_TIER1_REQ_KEYS = {"max_mass_g", "cg_window", "density_kg_mm3"}


def _requirements_gate(assembly_handle, req):
    """Evaluate the manifest `requirements` block over the merged assembly's
    world-space leaves. Returns {report, violations, skipped}."""
    asm = _resolve(assembly_handle)
    shapes = []
    _leaf_world_shapes(asm.Group, App.Matrix(), shapes)
    total_vol = sum(s.Volume for _, s in shapes)
    report = {"total_volume_mm3": round(total_vol, 3), "leaf_count": len(shapes)}
    violations = []

    density = req.get("density_kg_mm3")
    if density is not None and total_vol > 0:
        report["mass_g"] = round(total_vol * float(density) * 1000.0, 3)
    if total_vol > 0:
        cg = [round(sum(s.Volume * getattr(s.CenterOfMass, ax) for _, s in shapes)
                    / total_vol, 4) for ax in ("x", "y", "z")]
        report["cg_mm"] = cg
    else:
        cg = None

    if "max_mass_g" in req:
        if density is None:
            violations.append({"requirement": "max_mass_g",
                               "error": "needs density_kg_mm3 to compute mass"})
        else:
            mass_g = total_vol * float(density) * 1000.0
            if mass_g > float(req["max_mass_g"]) + 1e-6:
                violations.append({"requirement": "max_mass_g",
                                   "got_g": round(mass_g, 3),
                                   "limit_g": req["max_mass_g"],
                                   "reason": f"mass {mass_g:.1f} g > budget "
                                             f"{req['max_mass_g']} g"})

    if "cg_window" in req:
        win = req["cg_window"]
        if cg is None:
            violations.append({"requirement": "cg_window", "error": "no volume"})
        else:
            bad = [ax for i, ax in enumerate("xyz")
                   if cg[i] < win["min"][i] - 1e-6 or cg[i] > win["max"][i] + 1e-6]
            if bad:
                violations.append({"requirement": "cg_window", "cg_mm": cg,
                                   "window": win, "axes": bad,
                                   "reason": f"CG {cg} outside window on {bad}"})

    skipped = sorted(set(req) - _TIER1_REQ_KEYS)
    return {"report": report, "violations": violations, "skipped": skipped}


@handler("merge_assembly")
def _h_merge_assembly(p):
    """Construct-up an assembly from a manifest (the coordinator's one call).

    manifest (path to JSON) shape:
      { "name": "gearbox",
        "root": "gearbox.FCStd",                       # optional output path (rel)
        "components": { "<id>": { "file": "rel/part.FCStd",
                                  "object": "<name>",   # optional explicit target
                                  "envelope": {"min":[...],"max":[...]} },  # optional
                        "<sub>":  { "manifest": "sub/manifest.json" },  # §11.4 nest
                        "<std>":  { "library": { "tool": "add_fastener",  # §11.5
                                    "spec": {"kind":"hex_bolt","size":"M6","length":20} } } },
        "instances": [ { "component": "<id>",
                         "name": "<instance>",          # optional, defaults to id
                         "placement": [x,y,z] | {position,axis,angle_deg} } ] }

    Component files are resolved relative to the manifest's directory. Links
    auto-reload from those files, so re-running picks up updated components.
    A component with a `manifest` key (instead of `file`) is a SUBASSEMBLY: it is
    merged + gated first (recursively, any depth) and the parent links its merged
    root; a failed child fails the parent, surfaced as gates["children"] and a
    `children` block in the report. A component with a `library` key is a STANDARD
    PART generated on the fly from {tool, spec} (§11.5) — no builder, no owner —
    reported under `library`. An optional top-level `requirements` block
    {density_kg_mm3?, max_mass_g?, cg_window?} (§11.6) gates mass / CG over the
    merged tree; the measured numbers ride in report["requirements"]. Runs the gates
    (interference, recursive BOM, envelope, typed, requirements) and returns a
    report. Deterministic and idempotent."""
    import json as _json
    import os as _os
    manifest_path = p["manifest"]
    with open(manifest_path) as f:
        man = _json.load(f)
    base_dir = _os.path.dirname(_os.path.abspath(manifest_path))
    comps = man.get("components", {})

    # §11.4 hierarchical manifests: a component referencing a child `manifest`
    # (instead of a `file`) is a SUBASSEMBLY node — merge and gate it FIRST, then
    # the parent links its merged root .FCStd like any component file. The recursion
    # runs before the parent's new_document because each merge switches the active
    # document. Build an effective file path per component (a child's merged root,
    # or the component's own file) and roll the children's ok up into the parent.
    eff_file = {}
    children = {}
    library = {}
    for cid, spec in comps.items():
        if "manifest" in spec:
            cm = spec["manifest"]
            cm = cm if _os.path.isabs(cm) else _os.path.join(base_dir, cm)
            child_rep = _h_merge_assembly({"manifest": cm})
            children[cid] = {"ok": child_rep["ok"], "root": child_rep["root"],
                             "manifest": cm, "gates": child_rep["gates"]}
            eff_file[cid] = child_rep["root"]
        elif "library" in spec:
            # §11.5 standard part: generate it from its spec into the cache and
            # link the generated file like any component — no builder, no owner.
            lib = spec["library"]
            tool, tspec = lib["tool"], lib.get("spec", {})
            lib_path = _library_part_path(base_dir, tool, tspec)
            _generate_library_part(tool, tspec, lib_path)
            eff_file[cid] = lib_path
            library[cid] = {"tool": tool, "file": lib_path,
                            "spec_hash": _library_spec_hash(tool, tspec)}
        else:
            f = spec["file"]
            eff_file[cid] = f if _os.path.isabs(f) else _os.path.join(base_dir, f)

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
        cfile = eff_file[cid]  # a component file, or a child subassembly's merged root
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
    checks = man.get("checks", [])
    if checks:
        gates["typed"] = _run_typed_checks(checks, by_name, links_by_inst)
        # A typed contact gate (gear mesh) owns its pair; drop it from the blunt
        # interference list so a valid mesh isn't double-failed for overlapping
        # teeth. Other pairs still get the normal interference check.
        excl = _contact_exclusions(checks, links_by_inst)
        if excl:
            gates["interference"] = [
                r for r in gates["interference"]
                if frozenset((r["a"], r["b"])) not in excl]
    # §11.4: a subassembly that failed its OWN gates fails the parent too — a
    # broken child can't be a sound part of the whole. Surfaced as gates["children"]
    # (id -> ok) so a coordinator can re-dispatch into the offending child manifest.
    child_fail = [cid for cid, c in children.items() if not c["ok"]]
    if children:
        gates["children"] = {cid: c["ok"] for cid, c in children.items()}
    # §11.6: requirements gates ("the product works", not just "fits") — mass / CG
    # over the merged tree. The measured numbers ride in report["requirements"]
    # (visible on a pass); only the violations fail the merge.
    req_result = None
    if man.get("requirements"):
        req_result = _requirements_gate(asm_h, man["requirements"])
        gates["requirements"] = req_result["violations"]
    HANDLERS["save_document"]({"path": root_path})
    ok = (not gates["interference"]) and (not gates["envelope"]) \
        and (not gates.get("interface_align")) and (not gates.get("typed")) \
        and (not child_fail) and (not gates.get("requirements"))
    report = {"assembly": asm_h, "doc": name, "root": root_path,
              "placed": placed, "gates": gates, "ok": ok}
    if req_result is not None:
        report["requirements"] = {"report": req_result["report"],
                                  "skipped": req_result["skipped"]}
    if children:
        report["children"] = children
    if library:
        report["library"] = library  # §11.5: generated standard parts (spec hash)
    return report


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


# --- photorealistic rendering (FreeCAD Render workbench) ----------------------
#
# Unlike render_view (the host-side NumPy rasterizer in driftpin/render.py),
# photoreal rendering must run inside the FreeCAD process: it needs the live Part
# shapes and the third-party `Render` workbench, which serializes the scene and
# shells out to an external renderer binary (POV-Ray by default). It is therefore
# a worker handler, not a change to render.py. See docs/RENDER_WORKBENCH.md.
#
# Install (cross-platform): clone https://github.com/FreeCAD/FreeCAD-render into
# <App.getUserAppDataDir()>/Mod/Render (or via the Addon Manager), plus a renderer
# binary. DriftPin locates the binary at call time and writes its path into the
# FreeCAD param the Render plugin reads, so no preferences UI is needed.

# View directions — (unit vector from bbox center toward the camera, up vector).
# Mirrors driftpin/render.py's _VIEWS so render_view and render_photoreal frame a
# part identically. Kept as a local copy because render.py is a host-side
# (NumPy/Pillow) module the freecadcmd worker does not import.
_RENDER_VIEWS = {
    "iso":    ((1.0, 1.0, 1.0),  (0.0, 0.0, 1.0)),
    "top":    ((0.0, 0.0, 1.0),  (0.0, 1.0, 0.0)),
    "bottom": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "front":  ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "back":   ((0.0, 1.0, 0.0),  (0.0, 0.0, 1.0)),
    "right":  ((1.0, 0.0, 0.0),  (0.0, 0.0, 1.0)),
    "left":   ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "side":   ((1.0, 0.0, 0.0),  (0.0, 0.0, 1.0)),  # alias for "right"
}

# Renderer registry. Adding a renderer is a single dict entry: the FreeCAD param
# key its plugin reads for the exec path, a default scene template shipped with the
# addon, candidate binary names, common install dirs per OS (platform.system()
# keys), and an optional `batch` flag (forces the project into batch mode so the
# plugin uses its headless console binary). POV-Ray is verified end-to-end on Linux;
# LuxCore's scene export + material translation are verified headless, with the
# render binary itself driven on a provisioned box (it is a hand-fetched build).
_RENDER_PARAM_GROUP = "User parameter:BaseApp/Preferences/Mod/Render"
_RENDERERS = {
    "Povray": {
        "param_key": "PovRayPath",
        "template": "povray_standard.pov",
        "binaries": ("povray", "pvengine64", "pvengine"),
        # Headless: batch makes the plugin pass `-D` (no display). Without it the
        # plugin passes `+D`, so POV-Ray tries to open a preview window with no X
        # display and, once orphaned (worker gone, scene dir cleaned), busy-loops
        # at 100% CPU instead of exiting. POV-Ray has no separate console binary,
        # so batch here only flips the display flag (PovRayPath is read either way).
        "batch": True,
        "dirs": {
            "Linux":   ("/usr/bin", "/usr/local/bin"),
            "Darwin":  ("/opt/homebrew/bin", "/usr/local/bin"),
            "Windows": (r"C:\Program Files\POV-Ray\v3.7\bin",
                        r"C:\Program Files (x86)\POV-Ray\v3.7\bin"),
        },
        "install_hint": "'apt install povray' (Linux), 'brew install povray' (macOS), "
                        "or the official Windows installer",
    },
    "Luxcore": {
        # Headless -> batch mode -> the plugin reads LuxCoreConsolePath and runs
        # the `luxcoreconsole` CLI (the non-batch path uses the GUI LuxCorePath).
        "param_key": "LuxCoreConsolePath",
        "template": "luxcore_standard.cfg",
        "binaries": ("luxcoreconsole",),
        "batch": True,
        "dirs": {
            "Linux":   ("/usr/local/bin", "/opt/LuxCore", "/opt/luxcorerender"),
            "Darwin":  ("/Applications/LuxCore.app/Contents/MacOS", "/usr/local/bin"),
            "Windows": (r"C:\Program Files\LuxCoreRender",),
        },
        "install_hint": "download a standalone build from "
                        "https://github.com/LuxCoreRender/LuxCore/releases (provides "
                        "luxcoreconsole), put it on PATH with its bundled libs reachable "
                        "(e.g. via LD_LIBRARY_PATH on Linux)",
    },
    "Appleseed": {
        # Headless -> batch -> the plugin reads AppleseedCliPath and runs the
        # `appleseed.cli` console renderer (non-batch uses GUI AppleseedStudioPath).
        "param_key": "AppleseedCliPath",
        "template": "appleseed_standard.appleseed",
        "binaries": ("appleseed.cli",),
        "batch": True,
        "dirs": {
            "Linux":   ("/usr/local/bin", "/usr/bin", "/opt/appleseed/bin"),
            "Darwin":  ("/usr/local/bin", "/Applications/appleseed/bin"),
            "Windows": (r"C:\Program Files\appleseed\bin",),
        },
        "install_hint": "download an appleseed build from "
                        "https://github.com/appleseedhq/appleseed/releases (provides "
                        "appleseed.cli) and put it on PATH",
    },
    "Cycles": {
        # Cycles uses one path (CyclesPath); batch mode adds `--background` so the
        # standalone `cycles` renderer runs headless (no GUI window).
        "param_key": "CyclesPath",
        "template": "cycles_standard.xml",
        "binaries": ("cycles",),
        "batch": True,
        "dirs": {
            "Linux":   ("/usr/local/bin", "/usr/bin", "/opt/cycles"),
            "Darwin":  ("/usr/local/bin",),
            "Windows": (r"C:\Program Files\Cycles",),
        },
        "install_hint": "build or download the standalone Cycles renderer (the `cycles` "
                        "CLI) and put it on PATH",
    },
    "Ospray": {
        # OSPRay Studio; batch mode adds a `batch` subcommand so ospStudio renders
        # headless to an image instead of opening its viewer.
        "param_key": "OspPath",
        "template": "ospray_standard.sg",
        "binaries": ("ospStudio",),
        "batch": True,
        "dirs": {
            "Linux":   ("/usr/local/bin", "/usr/bin", "/opt/ospray_studio/bin"),
            "Darwin":  ("/usr/local/bin", "/Applications/ospStudio.app/Contents/MacOS"),
            "Windows": (r"C:\Program Files\Intel\OSPRay Studio\bin",),
        },
        "install_hint": "download OSPRay Studio from "
                        "https://github.com/RenderKit/ospray_studio/releases (provides "
                        "ospStudio) and put it on PATH",
    },
    "Pbrt": {
        # pbrt-v4. batch is headless; non-batch streams frames to a 'tev' viewer.
        # NOTE: pbrt-v4 support is marked experimental upstream in the addon.
        "param_key": "PbrtPath",
        "template": "pbrt_standard.pbrt",
        "binaries": ("pbrt",),
        "batch": True,
        "dirs": {
            "Linux":   ("/usr/local/bin", "/usr/bin", "/opt/pbrt/bin"),
            "Darwin":  ("/usr/local/bin",),
            "Windows": (r"C:\Program Files\pbrt\bin",),
        },
        "install_hint": "build pbrt-v4 from https://github.com/mmp/pbrt-v4 (provides "
                        "pbrt) and put it on PATH — pbrt-v4 support is experimental upstream",
    },
}


def _placement_from_view(view, obj, fov_deg=45.0, margin=1.2):
    """App.Placement that frames obj's bounding box from the named view.

    Pure App.Vector math (no NumPy): builds the same orthonormal camera basis as
    render.py's _camera_basis — the camera looks down its local -Z toward the bbox
    center, local +Y is up, local +X is right — then steps back far enough that the
    bounding sphere fits the vertical field of view. Returns a camera->world
    App.Placement (App.Rotation(x, y, z) maps the local axes onto x/y/z).
    """
    import math
    if view not in _RENDER_VIEWS:
        raise ValueError(f"unknown view {view!r}; valid: {sorted(_RENDER_VIEWS)}")
    cam_dir, up = _RENDER_VIEWS[view]
    z = App.Vector(*cam_dir)
    z.normalize()                                    # bbox center -> camera
    x = App.Vector(*up).cross(z)
    if x.Length < 1e-8:                              # up parallel to view dir
        x = App.Vector(0.0, 1.0, 0.0).cross(z)
        if x.Length < 1e-8:
            x = App.Vector(1.0, 0.0, 0.0).cross(z)
    x.normalize()
    y = z.cross(x)
    y.normalize()
    bb = obj.Shape.BoundBox
    center = App.Vector(bb.Center.x, bb.Center.y, bb.Center.z)
    radius = (bb.DiagonalLength / 2.0) or 1.0
    dist = (radius * margin) / math.tan(math.radians(fov_deg) / 2.0)
    return App.Placement(center + z * dist, App.Rotation(x, y, z))


def _renderer_exec_candidates(renderer, spec):
    """Ordered candidate paths for a renderer's binary, most-preferred first:
    DRIFTPIN_<RENDERER>_PATH env override -> path already set in FreeCAD prefs ->
    PATH (shutil.which, which honors Windows PATHEXT) -> common per-OS install dirs.
    Pure lookup — no side effects, no existence check (the caller filters)."""
    import shutil
    import platform
    key = spec["param_key"]
    candidates = []
    if env_path := os.environ.get(f"DRIFTPIN_{renderer.upper()}_PATH"):
        candidates.append(env_path)                  # 1) explicit env override
    if existing := App.ParamGet(_RENDER_PARAM_GROUP).GetString(key, ""):
        candidates.append(existing)                  # 2) already set in prefs
    for name in spec["binaries"]:                    # 3) PATH
        if found := shutil.which(name):
            candidates.append(found)
    for d in spec["dirs"].get(platform.system(), ()):  # 4) common install dirs
        for name in spec["binaries"]:
            for exe in (name, name + ".exe"):
                candidates.append(os.path.join(d, exe))
    return candidates


def _find_renderer_exec(renderer):
    """First existing candidate path for `renderer`'s binary, or None. No side
    effects (does not touch FreeCAD prefs) — used by the capabilities probe to
    report availability without committing a path."""
    spec = _RENDERERS.get(renderer)
    if spec is None:
        return None
    for c in _renderer_exec_candidates(renderer, spec):
        if c and os.path.isfile(c):
            return c
    return None


def _resolve_renderer_exec(renderer):
    """Locate the external renderer binary cross-platform and write its path into
    the FreeCAD param the Render plugin reads. Returns the resolved path.

    Resolution order is _find_renderer_exec's (env override -> prefs -> PATH ->
    per-OS install dirs). Raises RuntimeError with install guidance if not found.
    """
    spec = _RENDERERS.get(renderer)
    if spec is None:
        raise RuntimeError(
            f"renderer {renderer!r} is not wired in DriftPin yet (Phase 1 supports "
            f"{sorted(_RENDERERS)}). Install it and set its path in FreeCAD's Render "
            "preferences, or use renderer='Povray'."
        )
    found = _find_renderer_exec(renderer)
    if found:
        App.ParamGet(_RENDER_PARAM_GROUP).SetString(spec["param_key"], found)
        return found
    raise RuntimeError(
        f"could not locate the {renderer} renderer binary (tried "
        f"{list(spec['binaries'])}). Install it — {spec['install_hint']} — or set "
        f"DRIFTPIN_{renderer.upper()}_PATH to its full path."
    )


def _available_render_materials():
    """Sorted names of the material library cards shipped with the Render addon
    (e.g. 'Gold', 'Glass', 'Aluminium', 'GlossyPlastic'). Empty if the addon's
    materials dir is missing. Assumes `import Render` has already succeeded."""
    from Render.constants import WBMATERIALDIR
    if not os.path.isdir(WBMATERIALDIR):
        return []
    suffix = ".FCMat"
    return sorted(
        f[: -len(suffix)] for f in os.listdir(WBMATERIALDIR) if f.endswith(suffix)
    )


def _apply_render_material(doc, view, material_name):
    """Load a Render material library card by name and link it to `view`.

    Parses the .FCMat card (case-sensitive INI, all sections flattened into one
    dict — the exact logic the addon's material chooser uses), creates a Render
    Material object, imports any image textures, and links it via the View's
    Material property. Raises ValueError listing the valid names if the card is
    unknown. Assumes `import Render` has already succeeded.
    """
    import configparser
    from Render.constants import WBMATERIALDIR
    from Render.material import make_material

    path = os.path.join(WBMATERIALDIR, material_name + ".FCMat")
    if not os.path.isfile(path):
        raise ValueError(
            f"unknown render material {material_name!r}; available: "
            f"{_available_render_materials()}"
        )
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = lambda s: s                 # material card keys are case-sensitive
    parser.read(path)
    card = {key: value for section in parser.values() for key, value in section.items()}

    mat = make_material(name=material_name, doc=doc)
    # import_textures is a no-op for solid cards (metals, glass, plastics) and
    # extracts image textures into child objects for textured cards (marble, etc.).
    mat.Material = mat.Proxy.import_textures(card, WBMATERIALDIR)
    view.Material = mat
    return mat


def _require_render():
    """Import the FreeCAD Render workbench, or raise with cross-platform install
    guidance. Returns the Render module."""
    try:
        import Render
        return Render
    except Exception as e:
        raise RuntimeError(
            "FreeCAD Render workbench not importable. Install it by cloning "
            "https://github.com/FreeCAD/FreeCAD-render into "
            f"{os.path.join(App.getUserAppDataDir(), 'Mod', 'Render')} "
            f"(or via the Addon Manager). Underlying error: {e!r}"
        )


def _parse_render_request(p):
    """Validate render_photoreal params and resolve the renderer binary (setting
    its FreeCAD param). Returns a dict of normalized parameters. Shared by the
    blocking and async handlers."""
    src = _resolve(p["handle"])
    if not hasattr(src, "Shape"):
        raise TypeError(f"handle {p['handle']!r} has no Shape to render")
    width = int(p.get("width", 800))
    height = int(p.get("height", 600))
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    renderer = p.get("renderer", "Povray")
    exec_path = _resolve_renderer_exec(renderer)     # validates renderer + sets param
    return {
        "src": src,
        "renderer": renderer,
        "view": p.get("view", "iso"),
        "width": width,
        "height": height,
        "material": p.get("material") or None,       # None -> default gray material
        "template": p.get("template") or _RENDERERS[renderer]["template"],
        "exec_path": exec_path,
    }


def _setup_render_project(tmp, req):
    """Build the Render Project/Camera/View graph for req['src'] in document tmp.
    Returns the project fpo (call proj.Proxy.render(...) on it). Shared by the
    blocking and async handlers; assumes tmp is the active document."""
    Render = _require_render()
    feat = tmp.addObject("Part::Feature", "RenderTarget")
    feat.Shape = req["src"].Shape.copy()
    tmp.recompute()

    proj_proxy, proj, _ = Render.Project.create(
        tmp, renderer=req["renderer"], template=req["template"]
    )
    proj.RenderWidth = req["width"]
    proj.RenderHeight = req["height"]
    if _RENDERERS[req["renderer"]].get("batch") and hasattr(proj, "BatchMode"):
        proj.BatchMode = True                        # headless console binary (e.g. LuxCore)

    _, cam, _ = Render.Camera.create(tmp)
    cam.Projection = "Perspective"
    cam.Placement = _placement_from_view(req["view"], feat)

    proj_proxy.add_views([cam, feat])
    if req["material"]:
        # add_views wraps feat in a View object; link the material to it.
        for v in proj_proxy.all_views():
            if getattr(v, "Source", None) is feat:
                _apply_render_material(tmp, v, req["material"])
    tmp.recompute()
    return proj


@handler("render_photoreal")
def _h_render_photoreal(p):
    """Photorealistic render of a shaped object via the FreeCAD Render workbench
    (external renderer; POV-Ray by default). Renders in an isolated temporary
    document so the live model is never mutated, then returns
    {png_base64, png_path, renderer, view, material, width, height}.

    Optional `material` names a Render material library card (e.g. 'Gold',
    'Glass', 'Aluminium', 'GlossyPlastic'); omitted -> default gray material. An
    unknown name raises ValueError listing the available cards.

    Blocks until the render finishes; for long renders use render_photoreal_submit
    + render_job. Presentation-only: photoreal output is not bit-reproducible
    (sampler noise, thread count), so this stays out of the reliability/golden tests.
    """
    import base64
    _require_render()
    req = _parse_render_request(p)
    # Render in an isolated temp document: build the scene there, render, then close
    # it. Keeps the user's live document untouched (no Project/Camera/View objects
    # leaking into their model or their saved .FCStd).
    prev_active = App.ActiveDocument.Name if App.ActiveDocument else None
    tmp = App.newDocument("driftpin_render")
    try:
        proj = _setup_render_project(tmp, req)
        out = proj.Proxy.render(wait_for_completion=True)
        if not out or not os.path.isfile(out):
            raise RuntimeError(
                f"renderer {req['renderer']!r} (exec {req['exec_path']!r}) produced "
                "no output image. Check that the renderer runs headless on this "
                "platform (see the FreeCAD report log)."
            )
        with open(out, "rb") as f:
            data = f.read()
        return {
            "png_base64": base64.b64encode(data).decode("ascii"),
            "png_path": out,
            "renderer": req["renderer"],
            "view": req["view"],
            "material": req["material"],
            "width": req["width"],
            "height": req["height"],
        }
    finally:
        try:
            App.closeDocument(tmp.Name)
        except Exception:
            pass
        if prev_active and App.getDocument(prev_active) is not None:
            App.setActiveDocument(prev_active)


# Async render jobs. render_photoreal_submit launches the external renderer via the
# Render workbench's headless executor (RendererExecutorCli — a plain threading.Thread
# that runs ONLY the renderer subprocess; the FreeCAD scene export already ran in the
# calling thread before launch, so there is no cross-thread FreeCAD access). The job
# keeps its temp document open until the result is collected, because the renderer
# reads exported scene files from the doc's TransientDir. Jobs persist for the worker
# session, like _handles.
_render_jobs = {}

# Cap on retained jobs so abandoned results don't accumulate base64 PNGs for the
# whole worker session. Only finished (done/failed) jobs are evicted — a running
# job holds an open temp document the renderer is still reading.
_MAX_RENDER_JOBS = 16


def _close_render_job_doc(job):
    name = job.pop("doc", None)
    if name and App.getDocument(name) is not None:
        try:
            App.closeDocument(name)
        except Exception:
            pass


def _evict_render_jobs():
    """Drop the oldest finished jobs while over the cap (insertion order = age).
    Running jobs are never evicted."""
    while len(_render_jobs) > _MAX_RENDER_JOBS:
        victim = next(
            (jid for jid, j in _render_jobs.items() if j["status"] != "running"),
            None,
        )
        if victim is None:
            break                                    # all running -> nothing to free
        _close_render_job_doc(_render_jobs.pop(victim))


def _refresh_render_job(job):
    """Advance a running job: poll its executor thread, and once finished cache the
    PNG (base64) and close the temp document. No-op for already-finished jobs."""
    import base64
    if job["status"] != "running":
        return
    thread = job.get("thread")
    if thread is not None and thread.is_alive():
        return                                       # renderer still running
    out = job["out_path"]
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        with open(out, "rb") as f:
            job["png_base64"] = base64.b64encode(f.read()).decode("ascii")
        job["status"] = "done"
    elif thread is None and not os.path.isfile(out):
        return                                       # untrackable + no output yet -> still running
    else:
        job["status"] = "failed"
        job["error"] = (
            f"renderer {job['renderer']!r} finished without producing an output image"
        )
    _close_render_job_doc(job)


@handler("render_photoreal_submit")
def _h_render_photoreal_submit(p):
    """Start a photoreal render asynchronously and return immediately, so a long
    external render does not block the worker. Same params as render_photoreal.
    Returns {job_id, status}; poll render_job(job_id) for the result."""
    import threading
    _require_render()
    req = _parse_render_request(p)
    prev_active = App.ActiveDocument.Name if App.ActiveDocument else None
    tmp = App.newDocument("driftpin_render")
    try:
        proj = _setup_render_project(tmp, req)
        before = set(threading.enumerate())
        out = proj.Proxy.render(wait_for_completion=False)   # launches executor thread
        new_threads = [t for t in threading.enumerate() if t not in before]
    except Exception:
        try:
            App.closeDocument(tmp.Name)
        except Exception:
            pass
        raise
    finally:
        if prev_active and App.getDocument(prev_active) is not None:
            App.setActiveDocument(prev_active)
    job_id = _new_handle("render_job")
    _render_jobs[job_id] = {
        "status": "running",
        "thread": new_threads[0] if new_threads else None,
        "doc": tmp.Name,
        "out_path": out,
        "renderer": req["renderer"],
        "view": req["view"],
        "material": req["material"],
        "width": req["width"],
        "height": req["height"],
    }
    _evict_render_jobs()
    return {"job_id": job_id, "status": "running"}


@handler("render_job")
def _h_render_job(p):
    """Poll an async render started by render_photoreal_submit. Returns
    {job_id, status} with status 'running' | 'done' | 'failed'. When 'done', also
    returns {png_base64, png_path, renderer, view, material, width, height}; when
    'failed', {error}. The result stays available for repeat polls.

    Pass discard=True to free the job once you have a terminal result (closes its
    temp document and drops the cached PNG); ignored while still running."""
    job_id = p["job_id"]
    job = _render_jobs.get(job_id)
    if job is None:
        raise KeyError(f"unknown render job: {job_id!r}")
    _refresh_render_job(job)
    out = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "done":
        out.update({
            "png_base64": job["png_base64"],
            "png_path": job["out_path"],
            "renderer": job["renderer"],
            "view": job["view"],
            "material": job["material"],
            "width": job["width"],
            "height": job["height"],
        })
    elif job["status"] == "failed":
        out["error"] = job.get("error", "render failed")
    if p.get("discard") and job["status"] != "running":
        _close_render_job_doc(job)                   # done/failed doc already closed; idempotent
        _render_jobs.pop(job_id, None)
    return out


@handler("render_capabilities")
def _h_render_capabilities(p):
    """Report which photoreal renderers are usable *right now* and whether the
    FreeCAD Render addon imports, so a caller can pick a working renderer instead of
    probing render_photoreal by trial and error.

    For each renderer in the registry it resolves the binary the same way
    render_photoreal does (DRIFTPIN_<R>_PATH env -> FreeCAD prefs -> PATH -> per-OS
    install dirs) but WITHOUT mutating prefs or rendering anything. The addon check
    is the lazy import render_photoreal performs on call (the worker boots without it).

    Returns {addon_importable (bool), default_renderer, platform, available (sorted
    names of ready renderers), renderers: {name: {available, param_key, batch,
    binaries, and either path (resolved binary) or install_hint}}, materials (library
    card names — only when the addon imports), addon_error (only when it does not)}.
    """
    import platform
    addon_importable = True
    addon_error = None
    try:
        _require_render()                            # lazy: same import render_photoreal does
    except Exception as e:
        addon_importable = False
        addon_error = str(e)

    renderers = {}
    for name, spec in _RENDERERS.items():
        path = _find_renderer_exec(name)             # side-effect-free probe
        info = {
            "available": path is not None,
            "param_key": spec["param_key"],
            "batch": bool(spec.get("batch", False)),
            "binaries": list(spec["binaries"]),
        }
        if path is not None:
            info["path"] = path
        else:
            info["install_hint"] = spec["install_hint"]
        renderers[name] = info

    out = {
        "addon_importable": addon_importable,
        "default_renderer": "Povray",
        "platform": platform.system(),
        "available": sorted(n for n, i in renderers.items() if i["available"]),
        "renderers": renderers,
    }
    if addon_error is not None:
        out["addon_error"] = addon_error
    if addon_importable:
        # Cheap listdir of the addon's material cards — discover materials too, not
        # just renderers. Best-effort: never let it sink the whole capability probe.
        try:
            out["materials"] = _available_render_materials()
        except Exception:
            pass
    return out


# --- external-solver provisioning (driftpin.solvers) --------------------------
# The P2 twin of the renderer provisioning glue: discover the heavy external
# solvers the P2 families ride on (CFD/MBD/topology/transient-thermal/optics) and
# degrade to a clean structured dict when one is absent. The registry + resolution
# live in driftpin/solvers.py (pure-Python, FreeCAD-free — so the degradation
# contract is testable on the no-FreeCAD CI lane). A family's *_submit calls
# _require_solver(name) first and returns its dict verbatim on a miss, the way the
# render path uses _require_render — so a missing solver is a clean result, never
# an import crash.

def _require_solver(name):
    """Resolve an external P2 solver, or return the structured-degradation dict —
    the graceful-degradation twin of _require_render. {ok:True, ...} when the
    solver resolves; {ok:False, solver, reason:'solver not installed', install}
    when it does not. See driftpin/solvers.py."""
    from driftpin import solvers
    return solvers.require_solver(name)


@handler("solve_capabilities")
def _h_solve_capabilities(p):
    """Report which P2 external solvers — and which families — are usable right
    now, so a caller can pick a working solver instead of probing a *_submit by
    trial and error. The solver twin of render_capabilities: it resolves each
    solver side-effect-free (DRIFTPIN_<SOLVER>_PATH env -> PATH -> per-OS install
    dirs for binaries; importability for pip-wheel solvers) and executes nothing.

    Returns {platform, available (sorted ready solver names), solvers: {name:
    {available, kind, family, extra, and either path/module or install_hint}},
    families: {family: {solvers, available, any_available}}, extras: {extra:
    [solver names]}} (see driftpin/solvers.capabilities)."""
    from driftpin import solvers
    return solvers.capabilities()


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
    `char_length` caps the element size (mm). `element_order` is '1st' or '2nd'
    (quadratic): use '2nd' for bending/modal accuracy — linear tets (C3D4) shear-lock
    and badly overstiffen thin sections (a cantilever's first mode comes out ~50% high
    with 1-2 elements through the thickness). Returns handle + node/element counts."""
    from femmesh.gmshtools import GmshTools
    doc = _active_doc()
    analysis = _resolve_analysis(p["analysis"])
    body_obj = _shape_handle_to_obj(p["body"])
    char_length = float(p.get("char_length", 0.0))

    mesh = ObjectsFem.makeMeshGmsh(doc, p.get("name", "Mesh"))
    mesh.Shape = body_obj
    if char_length > 0:
        mesh.CharacteristicLengthMax = char_length
    order = p.get("element_order")
    if order is not None:
        if order not in ("1st", "2nd"):
            raise ValueError("element_order must be '1st' or '2nd'")
        mesh.ElementOrder = order
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


@handler("random_vibration")
def _h_random_vibration(p):
    """Random-vibration response from a modal run + a base-acceleration PSD
    (Miles' equation; pure-Python, no external solver — modal already ran). Reads
    the natural frequencies from the analysis's completed modal results (same
    extraction as fem_modal_results) when given an `analysis` handle, or uses an
    explicit `frequencies_hz` list, then computes the SRSS GRMS response off the
    PSD. See driftpin.analysis.vibration. Returns {rms_g, first_mode_hz,
    dominant_mode_hz, q, psd_band_hz, miles_grms_g, modes, rms_stress_mpa,
    three_sigma_stress_mpa, pass}."""
    from driftpin.analysis import vibration
    freqs = p.get("frequencies_hz")
    if not freqs:
        if "analysis" not in p:
            raise ValueError("provide an `analysis` handle or `frequencies_hz`")
        freqs = _h_fem_modal_results({"analysis": p["analysis"]})["frequencies_hz"]
        if not freqs:
            raise RuntimeError(
                "no modal frequencies on the analysis — run fem_modal + fem_run first"
            )
    return vibration.random_vibration(
        frequencies_hz=freqs,
        psd_profile=p["psd_profile"],
        q=float(p.get("q", 10.0)),
        modal_stress_mpa_per_g=p.get("modal_stress_mpa_per_g"),
        allowable_stress_mpa=p.get("allowable_stress_mpa"),
    )


@handler("beam_modal")
def _h_beam_modal(p):
    """Exact Euler-Bernoulli natural frequencies of a uniform rectangular beam (no
    solver) — the closed-form oracle the CalculiX `fem_modal` eigen-solve is gated
    against, and a fast modal screen on its own. f_n = (βL)_n²/(2π)·sqrt(E·I/(ρ·A·L⁴));
    the beam bends in `height_mm` (I = width·height³/12). `boundary` is one of
    cantilever / simply_supported / clamped_clamped / free_free / clamped_pinned; E,ρ
    from youngs_gpa+density_kg_m3 or a Materials-DB `material`. See
    driftpin.analysis.vibration. Returns {boundary, n_modes, frequencies_hz, beta_l,
    first_mode_hz, youngs_gpa, density_kg_m3, area_mm2, I_mm4, slenderness}."""
    from driftpin.analysis import vibration
    return vibration.beam_natural_frequencies(**p)


@handler("contact_setup")
def _h_contact_setup(p):
    """Set up surface-to-surface contact between face pairs for a CalculiX solve and
    flip the solver to nonlinear — promoting the CCX contact/nonlinear flags the FEM
    path already exposes (no new solver). Each entry of `face_pairs` is
    {a:{handle, tag|face}, b:{handle, tag|face}} (master, slave). `friction` is the
    Coulomb coefficient (0 = frictionless); `slope` optionally sets the penalty
    contact stiffness. The two faces become one FemConstraintContact each.

    Returns {contacts:[handles], n_pairs, friction, nonlinear (whether the solver's
    GeometricalNonlinearity was set)}. Run fem_run + fem_results after; gate the
    result RELATIVE to a bonded reference under the same mesh (a bonded model is
    stiffer — less peak displacement — than the same parts in frictional contact)."""
    doc = _active_doc()
    analysis = _resolve_analysis(p["analysis"])
    pairs = p.get("face_pairs") or []
    if not pairs:
        raise ValueError("face_pairs must be a non-empty list of {a, b} face refs")
    friction = float(p.get("friction", 0.0))
    handles = []
    for i, pair in enumerate(pairs):
        if "a" not in pair or "b" not in pair:
            raise ValueError(f"face_pair {i} needs both 'a' and 'b' face refs: {pair!r}")
        refs = _build_references([pair["a"]]) + _build_references([pair["b"]])
        c = ObjectsFem.makeConstraintContact(doc, p.get("name", "Contact") + f"_{i + 1}")
        c.References = refs
        # FreeCAD versions differ: newer ones have Friction (bool toggle) +
        # FrictionCoefficient (float); older ones make Friction the float coefficient.
        if "Friction" in c.PropertiesList:
            if c.getTypeIdOfProperty("Friction") == "App::PropertyBool":
                c.Friction = friction > 0.0
                if "FrictionCoefficient" in c.PropertiesList:
                    c.FrictionCoefficient = friction
            else:
                c.Friction = friction
        if p.get("slope") is not None and "Slope" in c.PropertiesList:
            c.Slope = p["slope"]
        analysis.addObject(c)
        handles.append(_register("contact", c))

    # Contact is a nonlinear analysis in CCX — flip the solver flag unless told not to.
    nonlinear = False
    if p.get("nonlinear", True):
        try:
            solver = _solver_of(analysis)
            if "GeometricalNonlinearity" in solver.PropertiesList:
                solver.GeometricalNonlinearity = "nonlinear"
                nonlinear = True
        except RuntimeError:
            pass                                     # no solver yet; set one with fem_set_solver
    doc.recompute()
    return {"contacts": handles, "n_pairs": len(pairs), "friction": friction,
            "nonlinear": nonlinear}


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


@handler("material_get")
def _h_material_get(p):
    from driftpin.analysis import materials
    try:
        return materials.get(p["name"])
    except materials.MaterialNotFound as e:
        return {"ok": False, "reason": str(e)}


@handler("material_select")
def _h_material_select(p):
    from driftpin.analysis import materials
    return materials.select(p.get("criteria") or {},
                            p.get("rank_by", "specific_strength"))


@handler("material_list")
def _h_material_list(p):
    from driftpin.analysis import materials
    return materials.list_materials(p.get("category"))


@handler("bolted_joint_check")
def _h_bolted_joint_check(p):
    from driftpin.analysis import machine_elements as me
    return me.bolted_joint_check(**p)


@handler("bearing_life")
def _h_bearing_life(p):
    from driftpin.analysis import machine_elements as me
    return me.bearing_life(**p)


@handler("spring_check")
def _h_spring_check(p):
    from driftpin.analysis import machine_elements as me
    return me.spring_check(**p)


@handler("gear_rating")
def _h_gear_rating(p):
    from driftpin.analysis import machine_elements as me
    return me.gear_rating(**p)


@handler("belt_drive")
def _h_belt_drive(p):
    from driftpin.analysis import machine_elements as me
    return me.belt_drive(**p)


@handler("press_fit_stress")
def _h_press_fit_stress(p):
    from driftpin.analysis import machine_elements as me
    return me.press_fit_stress(**p)


@handler("seal_check")
def _h_seal_check(p):
    from driftpin.analysis import machine_elements as me
    return me.seal_check(**p)


@handler("tolerance_stackup")
def _h_tolerance_stackup(p):
    from driftpin.analysis import tolerance
    return tolerance.stackup(**p)


@handler("fit_check")
def _h_fit_check(p):
    from driftpin.analysis import tolerance
    return tolerance.fit_check(p["hole"], p["shaft"])


@handler("fit_class")
def _h_fit_class(p):
    from driftpin.analysis import tolerance
    return tolerance.fit_class(p["basic_size"], p.get("fit", "H7/g6"))


@handler("gdt_check")
def _h_gdt_check(p):
    from driftpin.analysis import tolerance
    return tolerance.gdt_check(**p)


@handler("fatigue_check")
def _h_fatigue_check(p):
    from driftpin.analysis import durability
    return durability.fatigue_check(**p)


@handler("fracture_check")
def _h_fracture_check(p):
    from driftpin.analysis import durability
    return durability.fracture_check(**p)


@handler("wear_estimate")
def _h_wear_estimate(p):
    from driftpin.analysis import durability
    return durability.wear_estimate(**p)


@handler("creep_flag")
def _h_creep_flag(p):
    from driftpin.analysis import durability
    return durability.creep_flag(**p)


@handler("thermal_lumped")
def _h_thermal_lumped(p):
    from driftpin.analysis import thermal
    return thermal.thermal_lumped(**p)


@handler("h_estimate")
def _h_h_estimate(p):
    """Screening convection coefficient from handbook correlations (no solver):
    Churchill–Chu natural (vertical plate / horizontal cylinder), averaged flat-plate
    and Hilpert crossflow forced, plus the linearized radiation screen. Carries the
    fidelity contract (fidelity='correlation', band_pct). See
    driftpin.analysis.convection. Returns {geometry, mode, correlation, h_conv_w_m2k,
    h_rad_w_m2k, h_total_w_m2k, nusselt, reynolds, rayleigh, prandtl, film_temp_c,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    from driftpin.analysis import convection
    return convection.h_estimate(**p)


@handler("acoustic_screen")
def _h_acoustic_screen(p):
    """Closed-form acoustics screen: rigid-cavity modes (exact), Helmholtz
    resonator (±10%), mass-law TL (±3 dB), duct cutoff (exact) — fidelity labeled
    per kind. See driftpin.analysis.acoustics. Returns {kind, c_m_s, fidelity,
    band_pct, band_db, valid_range_ok, warnings, escalate_to} + per-kind fields."""
    from driftpin.analysis import acoustics
    return acoustics.acoustic_screen(**p)


@handler("plate_check")
def _h_plate_check(p):
    """Handbook bending of a uniformly loaded flat plate (Roark/Timoshenko —
    exact within thin-plate theory, limits flagged). See driftpin.analysis.plates.
    Returns {shape, support, aspect_ratio, beta, alpha, sigma_max_mpa,
    deflection_max_mm, yield_safety_factor, thin_plate_ok, small_deflection_ok,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    from driftpin.analysis import plates
    return plates.plate_check(**p)


@handler("beam_buckling")
def _h_beam_buckling(p):
    """Exact Euler + Johnson column buckling — the closed-form twin the CalculiX
    fem_buckling eigen-solve is gated against. See driftpin.analysis.buckling.
    Returns {end_condition, k_factor, slenderness, transition_slenderness,
    governing, sigma_cr_mpa, p_cr_n, area_mm2, i_min_mm4, radius_gyration_mm,
    safety_factor, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    from driftpin.analysis import buckling
    return buckling.beam_buckling(**p)


@handler("molding_screen")
def _h_molding_screen(p):
    """Injection-molding screen: exact one-term cooling time + spiral-flow fill
    reach (±30% chart). See driftpin.analysis.molding. Returns {material,
    wall_thickness_mm, t_melt_c, t_mold_c, t_eject_c, alpha_mm2_s, cooling_time_s,
    flow_length_mm, flow_ratio, flow_ratio_limit, fill_ok, fidelity, band_pct,
    valid_range_ok, warnings, escalate_to}."""
    from driftpin.analysis import molding
    return molding.molding_screen(**p)


@handler("drop_impact")
def _h_drop_impact(p):
    """Drop/impact screen by exact energy balance: G_avg = h/d, pulse-shape peak
    factors, v = sqrt(2gh), fragility-to-crush inversion. See
    driftpin.analysis.impact. Returns {drop_height_mm, impact_velocity_m_s, pulse,
    pulse_factor, crush_distance_mm, g_avg, g_peak, pulse_duration_ms,
    deceleration_limit_g, required_crush_mm, energy_j, peak_force_n, fidelity,
    band_pct, valid_range_ok, warnings, escalate_to}."""
    from driftpin.analysis import impact
    return impact.drop_impact(**p)


@handler("thermal_transient_1d")
def _h_thermal_transient_1d(p):
    """Analytic 1-D plane-wall transient (one-term Heisler series) — the closed-form
    oracle the Elmer thermal_transient solve is gated against, and the distributed
    answer the lumped screen only approximates. See driftpin.analysis.thermal. Returns
    {biot, fourier, eigenvalue_1, c1, t_center_c, t_surface_c, t_center_lumped_c,
    time_constant_s, one_term_valid, lumped_agrees}."""
    from driftpin.analysis import thermal
    return thermal.thermal_transient_1d(**p)


@handler("cfd_pipe_flow")
def _h_cfd_pipe_flow(p):
    """Analytic straight-pipe pressure drop (no solver) — Hagen–Poiseuille in the
    laminar regime (the exact CFD gate) and Blasius for smooth turbulent. The fast
    internal-flow screen and the oracle the OpenFOAM cfd_internal_flow solve is gated
    against. See driftpin.analysis.cfd. Returns {reynolds, regime, velocity_m_s,
    flow_rate_m3_s, friction_factor, pressure_drop_pa, wall_shear_pa,
    hagen_poiseuille_pa, laminar}."""
    from driftpin.analysis import cfd
    return cfd.pipe_pressure_drop(**p)


@handler("dfm_check")
def _h_dfm_check(p):
    from driftpin.analysis import dfx
    return dfx.dfm_check(**p)


@handler("dfa_check")
def _h_dfa_check(p):
    from driftpin.analysis import dfx
    return dfx.dfa_check(**p)


@handler("pack_check")
def _h_pack_check(p):
    from driftpin.analysis import dfx
    return dfx.pack_check(**p)


@handler("cost_estimate")
def _h_cost_estimate(p):
    from driftpin.analysis import cost
    return cost.cost_estimate(**p)


@handler("slice_estimate")
def _h_slice_estimate(p):
    from driftpin.analysis import slicing
    return slicing.slice_estimate(**p)


@handler("slice_gcode_submit")
def _h_slice_gcode_submit(p):
    """Slice a real body with the PrusaSlicer CLI, OFF the MCP channel — the
    Sprint 4 external-CLI upgrade of the analytic slice_estimate (real perimeters,
    infill patterns, supports, travel/acceleration). Degrades to {ok:false,
    reason, install} when no slicer resolves.

    Pass a `body` handle (exported to STL on the MAIN thread — the jobs.py
    contract) or a prepared `stl_path`. Knobs: `layer_height_mm`,
    `infill_fraction` (0..1; full infill auto-switches the fill pattern —
    PrusaSlicer's default refuses 100%), `supports`, `material` (filament density
    for grams — PrusaSlicer reports 0 g without one). The result carries the
    analytic slice_estimate for the same body alongside, with the
    `deposited_ratio` between them (live: a 20 mm cube at 100% lands 1.008 — the
    skirt).

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, gcode_path, filament_mm, filament_cm3, filament_g, print_time_s,
    print_time_text, layer_count, config, analytic?, deposited_ratio?,
    stdout_tail}."""
    info = _require_solver("prusaslicer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import slicing as _slicing
    slicer_bin = info["path"]

    layer_height_mm = float(p.get("layer_height_mm", 0.2))
    infill_fraction = float(p.get("infill_fraction", 0.2))
    supports = bool(p.get("supports", False))
    material = p.get("material", "PLA")
    density = p.get("density_g_cc")

    body = p.get("body")
    stl_path = p.get("stl_path")
    analytic = None
    if body:                                         # export on the MAIN thread
        obj = _shape_handle_to_obj(body)
        work_dir = tempfile.mkdtemp(prefix="slice_")
        stl_path = os.path.join(work_dir, "body.stl")
        obj.Shape.exportStl(stl_path)
        bb = obj.Shape.BoundBox
        analytic = _slicing.slice_estimate(
            volume_mm3=obj.Shape.Volume,
            bbox_mm=[bb.XLength, bb.YLength, bb.ZLength],
            material=material, infill_fraction=infill_fraction,
            layer_height_mm=layer_height_mm,
            **({"density_g_cc": float(density)} if density is not None else {}))
    elif stl_path:
        if not os.path.isfile(stl_path):
            raise ValueError(f"stl_path {stl_path!r} is not a file")
        work_dir = tempfile.mkdtemp(prefix="slice_")
    else:
        raise ValueError("provide a `body` handle or a prepared `stl_path`")

    rho = (float(density) if density is not None
           else _slicing._mat_value(material, "density_g_cc"))
    gcode_path = os.path.join(work_dir, "out.gcode")
    argv = [slicer_bin] + _slicing.slicer_cmd(
        stl_path, gcode_path, layer_height_mm=layer_height_mm,
        infill_fraction=infill_fraction, supports=supports,
        extra_args=p.get("extra_args"))
    key = jobs.content_key("slice_gcode", {
        "stl": os.path.abspath(stl_path), "argv": argv[1:], "rho": rho})

    def _work():
        import subprocess
        proc = subprocess.run(argv, capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "prusaslicer",
            "gcode_path": gcode_path,
            "stdout_tail": ((proc.stdout or "") + (proc.stderr or ""))[-2000:],
        }
        if proc.returncode == 0 and os.path.isfile(gcode_path):
            try:
                stats = _slicing.parse_gcode_stats(
                    open(gcode_path, errors="replace").read(), density_g_cc=rho)
            except ValueError as exc:
                out["ok"] = False
                out["reason"] = f"unparseable G-code: {exc}"
                return out
            out.update(stats)
            if analytic:
                out["analytic"] = analytic
                if analytic["deposited_volume_mm3"] > 0 and stats["filament_cm3"]:
                    out["deposited_ratio"] = round(
                        stats["filament_cm3"] * 1000.0
                        / analytic["deposited_volume_mm3"], 4)
        return out

    return jobs.submit("slice_gcode", _work, key=key,
                       meta={"layer_height_mm": layer_height_mm,
                             "infill_fraction": infill_fraction})


# --- optics (family 7) --------------------------------------------------------
# Two surfaces, mirroring the rest of the heavy tier: the exact closed-form core
# (Snell / Fresnel / TIR + an energy-conserving bundle trace) lives in
# analysis/optics.py and is fast-lane gated; optics_raytrace backs the full
# diffuser/lens trace with the rayoptics wheel (the `optics` extra) behind
# _require_solver, degrading cleanly when it is absent. optics_moldability_check
# is purely geometric — per-face draft vs the pull axis + a ray-cast undercut test
# on the live FreeCAD solid, scored through the DfM machinery (analysis/dfx.py).

_PULL_AXES = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}


def _pull_vector(pull_axis):
    """Resolve a pull axis to a unit App.Vector. Accepts '+z'/'-x'/… or an
    explicit [x,y,z] (normalized)."""
    if isinstance(pull_axis, str):
        key = pull_axis.lower().strip()
        if key not in _PULL_AXES:
            raise ValueError(
                f"pull_axis must be one of {sorted(_PULL_AXES)} or [x,y,z]; got {pull_axis!r}")
        return App.Vector(*_PULL_AXES[key])
    v = App.Vector(*[float(c) for c in pull_axis])
    if v.Length == 0:
        raise ValueError("pull_axis vector must be non-zero")
    v.normalize()
    return v


def _ray_hits_solid(shape, start, direction, reach):
    """True if the segment from `start` along unit `direction` for `reach` mm
    passes through the solid's interior — the occlusion test behind the undercut
    check. Uses a boolean common of the solid with the probe edge; any surviving
    edge length above a small tolerance means the ray re-enters material."""
    import Part
    end = start + direction.multiply(reach)
    try:
        seg = Part.makeLine(start, end)
        common = shape.common(seg)
    except Exception:
        return False
    return common.Length > 1e-6


@handler("optics_moldability_check")
def _h_optics_moldability_check(p):
    """Moldability screen for an optical (or any) part against a single pull axis —
    geometric, no solver. Resolves the `model` handle to its solid, then for every
    face computes the draft relative to `pull_axis` from the outward normal
    (draft_deg = 90 − angle(normal, pull); 0 is a wall parallel to the pull that
    needs draft) and ray-casts the face centroid along ±pull to decide releasability
    — a face the straight pull cannot free in either direction is a re-entrant
    UNDERCUT (its draft_deg is reported negative so it falls out as an undercut).
    Inward chords give a wall-thickness distribution. The per-face descriptors are
    scored through analysis/dfx.dfm_check.

    Args: model (handle), pull_axis ('+z'/'-x'/… or [x,y,z]), process
    ('injection'|'cnc'|'sheet'|'fdm', default injection), min_draft_deg (default 1.0),
    min_wall_mm (optional, else the process default). Returns {process, pull_axis,
    n_faces, undercut_faces, draft_violations, min_wall_violations,
    wall_thickness_stats:{min_mm,mean_mm,max_mm,n}, score, pass}."""
    from driftpin.analysis import dfx
    handle = p.get("model") or p.get("handle")
    if not handle:
        raise ValueError("optics_moldability_check needs a `model` handle")
    _, shape = _shape_of(handle)
    pull = _pull_vector(p.get("pull_axis", "+z"))
    min_draft = float(p.get("min_draft_deg", 1.0))

    bbox = shape.BoundBox
    reach = bbox.DiagonalLength * 2.0 + 1.0          # comfortably exits the solid
    eps = max(bbox.DiagonalLength * 1e-4, 1e-4)      # step just off the surface

    faces = []
    walls = []
    import math as _math
    for i, face in enumerate(shape.Faces):
        idx = f"Face{i + 1}"
        n = _outward_normal(face)
        if n.Length == 0:
            continue
        n = App.Vector(n).normalize()
        cos = max(-1.0, min(1.0, n.dot(pull)))
        phi = _math.degrees(_math.acos(cos))         # angle of normal from +pull
        draft = 90.0 - phi                           # >0 toward pull, <0 against

        c = face.CenterOfMass
        out_pt = c + App.Vector(n).multiply(eps)
        releasable_plus = not _ray_hits_solid(shape, out_pt, App.Vector(pull), reach)
        releasable_minus = not _ray_hits_solid(
            shape, out_pt, App.Vector(pull).multiply(-1.0), reach)
        undercut = not (releasable_plus or releasable_minus)
        # An undercut face is reported with a negative draft so dfx.dfm_check
        # classifies it as re-entrant; otherwise carry the geometric draft (its
        # sign already encodes which mold half releases it).
        draft_deg = -abs(draft) if undercut else abs(draft)

        # inward chord ~ local wall thickness (face inward to the next boundary)
        in_pt = c - App.Vector(n).multiply(eps)
        wall_mm = None
        try:
            import Part
            chord = shape.common(Part.makeLine(in_pt, in_pt - App.Vector(n).multiply(reach)))
            if chord.Length > 1e-6:
                wall_mm = round(chord.Length, 4)
                walls.append(chord.Length)
        except Exception:
            pass

        fdesc = {"name": idx, "draft_deg": round(draft_deg, 4)}
        if wall_mm is not None:
            fdesc["wall_mm"] = wall_mm
        faces.append(fdesc)

    res = dfx.dfm_check(
        faces=faces, pull_axis=str(p.get("pull_axis", "+z")),
        process=p.get("process", "injection"),
        min_wall_mm=p.get("min_wall_mm"), min_draft_deg=min_draft)
    if walls:
        res["wall_thickness_stats"] = {
            "min_mm": round(min(walls), 4), "mean_mm": round(sum(walls) / len(walls), 4),
            "max_mm": round(max(walls), 4), "n": len(walls)}
    else:
        res["wall_thickness_stats"] = {"min_mm": None, "mean_mm": None, "max_mm": None, "n": 0}
    res["n_faces"] = len(shape.Faces)
    return res


def _rayoptics_flat_trace(n1, n2, angles_deg):
    """Trace each incidence angle (deg) through a single flat n1→n2 interface with
    rayoptics, returning the list of refraction angles (deg). n1 is air-side (the
    object space); rayoptics owns the geometry so this validates Snell against the
    analytic oracle. Imported lazily — only after _require_solver('rayoptics')."""
    import math as _math

    import numpy as np
    from rayoptics.environment import OpticalModel
    from rayoptics.raytr import raytrace
    from rayoptics.raytr.opticalspec import FieldSpec, PupilSpec, WvlSpec

    opm = OpticalModel(radius_mode=True)
    sm, osp = opm["seq_model"], opm["optical_spec"]
    osp["pupil"] = PupilSpec(osp, key=["object", "epd"], value=2.0)
    osp["fov"] = FieldSpec(osp, key=["object", "angle"], value=[0.0], is_relative=False)
    osp["wvls"] = WvlSpec([("d", 1.0)], ref_wl=0)
    sm.gaps[0].thi = 100.0
    sm.add_surface([1e10, 10.0, n2 / n1, 57.4])      # flat interface, relative index
    sm.add_surface([1e10, 0.0])
    sm.gaps[-1].thi = 10.0
    sm.set_stop()
    opm.update_model()
    wvl = sm.central_wavelength()
    path = list(sm.path(wl=wvl))

    out = []
    for th in angles_deg:
        t = _math.radians(th)
        dir0 = np.array([0.0, _math.sin(t), _math.cos(t)])
        pt0 = np.array([0.0, 0.0, 0.0])
        ray, _opd, _w = raytrace.trace_raw(iter(path), pt0, dir0, wvl)
        after = ray[1][1]
        nrm = float(np.linalg.norm(after))
        out.append(_math.degrees(_math.acos(min(1.0, abs(after[2] / nrm)))))
    return out


@handler("optics_raytrace")
def _h_optics_raytrace(p):
    """Ray-trace a bundle through an optical model with rayoptics, OFF no FreeCAD
    geometry — degrades to {ok:false, reason, install} when the rayoptics wheel
    (the `optics` extra) is absent, never raising. The geometric refraction comes
    from rayoptics; the Fresnel/TIR energy split and the histogram come from the
    exact analysis/optics core, so the result is gated against that oracle
    (`oracle_max_dev_deg` is the max rayoptics−Snell exit-angle deviation).

    Args: source_config ({kind:'collimated'|'cone'|'lambertian', …}, see
    analysis.optics.sample_source), n_refractive (the medium index, n2), n_rays
    (default 64), model (optional {n1, n_refractive/n2, absorption,
    target_half_angle_deg}). The incident medium n1 defaults to air (1.0).

    Returns the degradation dict, or {ok, backend:'rayoptics', rayoptics_version,
    n_rays, n1, n2, critical_angle_deg, efficiency, leakage_fraction,
    absorbed_fraction, tir_fraction, energy_balance, oracle_max_dev_deg,
    exit_distribution:[{angle_deg,intensity}], hotspot_locations:[…]}."""
    info = _require_solver("rayoptics")
    if not info["ok"]:
        return info
    from driftpin.analysis import optics

    model = p.get("model") or {}
    n2 = float(p.get("n_refractive", model.get("n_refractive", model.get("n2", 1.49062))))
    n1 = float(model.get("n1", 1.0))
    n_rays = int(p.get("n_rays", 64))
    absorption = float(model.get("absorption", p.get("absorption", 0.0)))
    target = model.get("target_half_angle_deg", p.get("target_half_angle_deg"))
    source = p.get("source_config") or {"kind": "collimated", "angle_deg": 0.0}

    angles, weights = optics.sample_source(source, n_rays)
    if n1 != 1.0:
        raise ValueError(
            "optics_raytrace's rayoptics flat-interface trace expects an air-incident "
            "model (n1=1.0); set model.n1=1.0 (use the analytic core for n1>1 TIR cases)")
    ro_exit = _rayoptics_flat_trace(n1, n2, angles)

    # Energy partition from the exact Fresnel/TIR core; geometry from rayoptics.
    efficiency = leakage = absorbed = tir = 0.0
    exit_samples = []
    max_dev = 0.0
    for theta_i, w, theta_ro in zip(angles, weights, ro_exit):
        absorbed += w * absorption
        remaining = w * (1.0 - absorption)
        fr = optics.fresnel_reflectance(theta_i, n1, n2)
        snell = optics.refract_angle(theta_i, n1, n2)
        if snell is not None:
            max_dev = max(max_dev, abs(theta_ro - snell))
        if fr["tir"]:
            leakage += remaining
            tir += remaining
            continue
        transmitted = remaining * fr["transmittance"]
        leakage += remaining * fr["reflectance"]
        exit_samples.append((theta_ro, transmitted))
        if target is None or theta_ro <= float(target):
            efficiency += transmitted
        else:
            leakage += transmitted

    dist = optics._histogram(exit_samples, 18)
    hot = sorted((b for b in dist if b["intensity"] > 0),
                 key=lambda b: b["intensity"], reverse=True)[:3]
    theta_c = optics.critical_angle(n1, n2)
    import rayoptics as _ro
    return {
        "ok": True,
        "backend": "rayoptics",
        "rayoptics_version": getattr(_ro, "__version__", "unknown"),
        "n_rays": n_rays,
        "n1": n1,
        "n2": n2,
        "critical_angle_deg": (round(theta_c, 4) if theta_c is not None else None),
        "efficiency": round(efficiency, 6),
        "leakage_fraction": round(leakage, 6),
        "absorbed_fraction": round(absorbed, 6),
        "tir_fraction": round(tir, 6),
        "energy_balance": round(efficiency + leakage + absorbed, 6),
        "oracle_max_dev_deg": round(max_dev, 6),
        "exit_distribution": dist,
        "hotspot_locations": hot,
    }


# --- multibody dynamics / kinematics (family 8) -------------------------------
# Two surfaces: mechanism_kinematics is the closed-form, solver-free gate (Grübler
# DOF, Grashof, slider-crank stroke = 2R, four-bar sweep) in analysis/kinematics.py;
# mechanism_simulate_submit runs the PyBullet dynamics (analysis/mbd.py) off the MCP
# channel via jobs.py, degrading through _require_solver when the wheel is absent.

@handler("mechanism_kinematics")
def _h_mechanism_kinematics(p):
    """Closed-form planar mechanism kinematics — exact, no external solver. Dispatches
    on `mechanism`: 'fourbar' (ground/crank/coupler/rocker) -> {mobility_dof, grashof,
    reachable, coupler_path, reachable_bbox_mm, n_reached}; 'slider_crank'
    (crank_mm/conrod_mm[/wrist_offset_mm]) -> {stroke_mm (=2R inline), x_tdc_mm,
    x_bdc_mm, inline_stroke_exact}; 'gruebler' (n_links + joints) -> {mobility_dof}.
    See driftpin.analysis.kinematics."""
    from driftpin.analysis import kinematics as kin
    mech = p.get("mechanism", "fourbar")
    if mech == "slider_crank":
        return kin.slider_crank(
            crank_mm=p["crank_mm"], conrod_mm=p["conrod_mm"],
            n_steps=int(p.get("n_steps", 360)),
            wrist_offset_mm=float(p.get("wrist_offset_mm", 0.0)))
    if mech == "fourbar":
        grashof = kin.grashof_classify(crank=p["crank"], coupler=p["coupler"],
                                       rocker=p["rocker"], ground=p["ground"])
        sweep = kin.fourbar_sweep(
            ground=p["ground"], crank=p["crank"], coupler=p["coupler"],
            rocker=p["rocker"], config=p.get("config", "open"),
            n_steps=int(p.get("n_steps", 72)),
            coupler_point=tuple(p.get("coupler_point", (0.5, 0.0))))
        return {"mobility_dof": kin.gruebler_dof(4, [{"type": "revolute"}] * 4),
                "grashof": grashof, **sweep}
    if mech == "gruebler":
        return {"mobility_dof": kin.gruebler_dof(
            int(p["n_links"]), p.get("joints", []), planar=bool(p.get("planar", True)))}
    raise ValueError(
        f"unknown mechanism {mech!r}; use 'fourbar', 'slider_crank', or 'gruebler'")


def _mbd_to_si_spec(links, drivers, obstacles=None, base=None):
    """Convert a physical-unit (mm / g / deg·s⁻¹) link/driver/obstacle description to
    the SI spec driftpin.analysis.mbd.run_mbd expects. Pure unit math, FreeCAD-free —
    runs on the main thread to build the background job's payload."""
    import math as _m

    def hx(box_mm):                                  # full box dims (mm) -> half extents (m)
        return [float(b) / 2.0 / 1000.0 for b in box_mm]

    def mm2m(v):
        return [float(x) / 1000.0 for x in v]

    si_links = [{
        "name": lk.get("name"),
        "half_extents_m": hx(lk["box_mm"]),
        "mass_kg": float(lk.get("mass_g", 0.0)) / 1000.0,
        "parent": int(lk.get("parent", -1)),
        "joint_type": lk.get("joint_type", "revolute"),
        "joint_axis": lk.get("joint_axis", [0, 0, 1]),
        "joint_pos_m": mm2m(lk.get("joint_at_mm", [0, 0, 0])),
        "com_m": mm2m(lk.get("com_mm", [0, 0, 0])),
    } for lk in links]

    si_drivers = []
    for d in drivers or []:
        if "rate_dps" in d:
            vel = float(d["rate_dps"]) * _m.pi / 180.0      # revolute -> rad/s
        elif "rate_mm_s" in d:
            vel = float(d["rate_mm_s"]) / 1000.0            # prismatic -> m/s
        else:
            vel = float(d.get("target_velocity", 0.0))
        si_drivers.append({"link": int(d["link"]), "target_velocity": vel,
                           "max_force": float(d.get("max_force", 1e3))})

    si_obstacles = [{"half_extents_m": hx(o["box_mm"]),
                     "pos_m": mm2m(o.get("at_mm", [0, 0, 0]))}
                    for o in (obstacles or [])]
    spec = {"links": si_links, "drivers": si_drivers, "obstacles": si_obstacles}
    if base is not None:
        spec["base"] = {"half_extents_m": hx(base.get("box_mm", [10, 10, 10])),
                        "mass_kg": float(base.get("mass_g", 0.0)) / 1000.0,
                        "pos_m": mm2m(base.get("at_mm", [0, 0, 0]))}
    return spec


@handler("mechanism_simulate_submit")
def _h_mechanism_simulate_submit(p):
    """Simulate a rigid-link mechanism's dynamics with PyBullet, OFF the MCP channel.

    `links` is a tree of {name, box_mm, mass_g, parent (index, −1 = fixed base),
    joint_type ('revolute'|'prismatic'|'fixed'), joint_axis, joint_at_mm (in the
    parent frame), com_mm}; `drivers` drive a link's joint (rate_dps for revolute,
    rate_mm_s for prismatic); optional `obstacles` and `base`. The closed-form
    `mobility_dof` (Grübler) is computed on the main thread and always returned.

    Degrades when PyBullet is absent: returns {ok:false, reason, install, mobility_dof,
    n_links} instead of submitting. Otherwise exports the SI spec on the main thread
    and runs the solve in a background job (FreeCAD-free), returning {job_id, status,
    cache_hit, mobility_dof}; poll job_result for {trajectories, max_torques,
    collisions_through_motion, reachable_envelope, mobility_dof}."""
    from driftpin import jobs
    from driftpin.analysis import kinematics as kin
    from driftpin.analysis import mbd

    links = p["links"]
    drivers = p.get("drivers", [])
    duration_s = float(p.get("duration_s", 1.0))
    dt_s = float(p.get("dt_s", 1.0 / 240.0))
    gravity = list(p.get("gravity", [0.0, 0.0, -9.81]))

    # closed-form mobility (Grübler) — main thread, no solver. Count each link's
    # parent joint plus any loop-closure joints.
    joints = [{"type": lk.get("joint_type", "revolute")} for lk in links]
    joints += [{"type": lc.get("type", "revolute")} for lc in (p.get("loop_closures") or [])]
    n_links = len(links) + 1                          # + ground/base
    mobility = kin.gruebler_dof(n_links, joints)

    info = _require_solver("pybullet")
    if not info["ok"]:                                # graceful degradation
        return {**info, "mobility_dof": mobility, "n_links": n_links}

    spec = _mbd_to_si_spec(links, drivers, p.get("obstacles"), p.get("base"))
    key = jobs.content_key("mechanism_simulate",
                           {"spec": spec, "duration_s": duration_s,
                            "dt_s": dt_s, "gravity": gravity})

    def _work():
        out = mbd.run_mbd(spec, duration_s=duration_s, dt_s=dt_s, gravity=tuple(gravity))
        out["mobility_dof"] = mobility
        return out

    res = jobs.submit("mechanism_simulate", _work, key=key,
                      meta={"n_links": n_links, "duration_s": duration_s})
    res["mobility_dof"] = mobility
    return res


# --- topology optimization (family 5; in-house SIMP, no external solver) ------

@handler("topology_optimize_submit")
def _h_topology_optimize_submit(p):
    """Minimum-compliance topology optimization (in-house NumPy SIMP — no external
    solver), run OFF the MCP channel because each iteration solves an FE system.
    Optimizes a 2-D rectangular design domain (nelx×nely unit cells) — or, when
    `nelz` >= 1, a 3-D nelx×nely×nelz grid of trilinear hexahedra — to the stiffest
    layout subject to Σdensity = keep_fraction (held exactly by the OC update).
    Default BCs (both): left face clamped + unit downward load at the right-face
    centre. 2-D overrides: `fixed_dofs` / `load`=[dof_index, value]. 3-D overrides:
    `loads`=[[i,j,k,axis,value],...] (node grid coords, axis 'x'|'y'|'z'),
    `fixed_nodes`=[[i,j,k],...], and `keep_out`/`keep_in` half-open element-index
    boxes [i0,i1,j0,j1,k0,k1] forced void / forced solid.

    Returns {job_id, status, cache_hit}; poll job_result for {density (2-D: nely×nelx
    grid; 3-D: nelz×nely×nelx voxel field — this is geometry), mass_fraction,
    compliance, compliance_initial, iterations, converged, gray_fraction}.
    Pure-Python background body (no FreeCAD)."""
    from driftpin import jobs
    from driftpin.analysis import topology as topo
    nelz = int(p.get("nelz") or 0)
    # 3-D DOFs grow as the product of three dims, so a 3-D run must NOT inherit the
    # 2-D grid defaults (nelx=60,nely=20 → ~40k DOFs in 3-D, a multi-minute solve).
    # Use the small 3-D defaults the optimizer itself ships with, unless overridden.
    if nelz >= 1:
        nelx = int(p.get("nelx", 16))
        nely = int(p.get("nely", 8))
        max_iter = int(p.get("max_iter", 40))
    else:
        nelx = int(p.get("nelx", 60))
        nely = int(p.get("nely", 20))
        max_iter = int(p.get("max_iter", 60))
    # Guard against a runaway grid (accidental or otherwise): cap total DOFs.
    ndof_est = (3 * (nelx + 1) * (nely + 1) * (nelz + 1) if nelz >= 1
                else 2 * (nelx + 1) * (nely + 1))
    if ndof_est > 250000:
        raise ValueError(
            f"requested grid is ~{ndof_est} DOFs (cap 250000) — reduce nelx/nely"
            + ("/nelz" if nelz >= 1 else "") + " to avoid a runaway solve")
    keep_fraction = float(p.get("keep_fraction", 0.4))
    penal = float(p.get("penal", 3.0))
    rmin = float(p.get("rmin", 1.5))
    tol = float(p.get("tol", 0.01))
    load = p.get("load")
    fixed_dofs = p.get("fixed_dofs")
    loads = p.get("loads")
    fixed_nodes = p.get("fixed_nodes")
    keep_out = p.get("keep_out")
    keep_in = p.get("keep_in")
    key = jobs.content_key("topology_optimize", {
        "nelx": nelx, "nely": nely, "nelz": nelz, "keep_fraction": keep_fraction,
        "penal": penal, "rmin": rmin, "max_iter": max_iter, "tol": tol, "load": load,
        "fixed_dofs": fixed_dofs, "loads": loads, "fixed_nodes": fixed_nodes,
        "keep_out": keep_out, "keep_in": keep_in})

    if nelz >= 1:
        def _work():
            return topo.simp_topology_3d(
                nelx=nelx, nely=nely, nelz=nelz, keep_fraction=keep_fraction,
                penal=penal, rmin=rmin, max_iter=max_iter, tol=tol, loads=loads,
                fixed_nodes=fixed_nodes, keep_out=keep_out, keep_in=keep_in)
    else:
        def _work():
            return topo.simp_topology_2d(
                nelx=nelx, nely=nely, keep_fraction=keep_fraction, penal=penal,
                rmin=rmin, max_iter=max_iter, tol=tol, load=load, fixed_dofs=fixed_dofs)

    return jobs.submit("topology_optimize", _work, key=key,
                       meta={"nelx": nelx, "nely": nely, "nelz": nelz or None,
                             "keep_fraction": keep_fraction})


@handler("topology_to_solid")
def _h_topology_to_solid(p):
    """Reconstruct a FreeCAD solid from a topology-optimization density field — the
    modeller-side follow-on that closes the loop opened by topology_optimize_submit
    (whose `density` this consumes). 2-D: thresholds the nely×nelx grid (a cell is
    solid when density >= `threshold`, default 0.5), run-length-merges each row into
    solid spans, tiles each span as a `cell_mm` box extruded `thickness_mm` in Z
    (grid row 0 at the top (+Y), matching the grid's reading order). 3-D (a
    nelz×nely×nelx voxel field from the `nelz` mode): greedy-merges voxels into
    maximal boxes tiled at (i·cx, j·cy, k·cz) — j=0 at the BOTTOM, no flip;
    `thickness_mm` is ignored. Fuses the boxes and bakes a static Part::Feature.
    `cell_mm` is a scalar or [cx, cy(, cz)] mm; `placement` an optional [x, y, z] mm
    origin offset. Runs synchronously on the main thread (it builds geometry —
    unlike the *_submit solves it does NOT use jobs.py). Returns {handle, name,
    volume (mm^3), solid_cells, total_cells, mass_fraction (==solid_cells/
    total_cells), n_solids (>1 = a split load path), threshold, nelx, nely,
    nelz (None for 2-D), bbox_mm}."""
    doc = _active_doc()
    from driftpin.analysis import topology as topo
    density = p.get("density")
    if not density:
        raise ValueError("density (nely×nelx grid or nelz×nely×nelx field, 0..1) is required")
    threshold = float(p.get("threshold", 0.5))
    is_3d = (isinstance(density[0], (list, tuple)) and density[0]
             and isinstance(density[0][0], (list, tuple)))
    cell = p.get("cell_mm", 1.0)
    if isinstance(cell, (list, tuple)):
        if len(cell) not in (2, 3):
            raise ValueError("cell_mm must be a number, [cx, cy] or [cx, cy, cz] in mm")
        cx, cy = float(cell[0]), float(cell[1])
        cz = float(cell[2]) if len(cell) == 3 else min(cx, cy)
    else:
        cx = cy = cz = float(cell)

    if is_3d:
        # 3-D voxel field from simp_topology_3d: density[k][j][i], j=0 at the
        # BOTTOM (no display flip) — boxes tile at (i·cx, j·cy, k·cz).
        if cx <= 0 or cy <= 0 or cz <= 0:
            raise ValueError("cell_mm must be > 0")
        dec = topo.density_to_boxes(density, threshold)
        if not dec["boxes"]:
            raise ValueError(
                f"no cells at or above threshold {threshold}; lower `threshold` or "
                f"check the density field (max cell < {threshold})")
        nelx, nely, nelz = dec["nelx"], dec["nely"], dec["nelz"]
        boxes = [
            Part.makeBox(w * cx, h * cy, d * cz,
                         App.Vector(i0 * cx, j0 * cy, k0 * cz))
            for (i0, j0, k0, w, h, d) in dec["boxes"]
        ]
        total = nelx * nely * nelz
    else:
        thickness = float(p.get("thickness_mm", p.get("thickness", min(cx, cy))))
        if cx <= 0 or cy <= 0 or thickness <= 0:
            raise ValueError("cell_mm and thickness_mm must be > 0")
        dec = topo.density_to_rects(density, threshold)
        if not dec["rects"]:
            raise ValueError(
                f"no cells at or above threshold {threshold}; lower `threshold` or "
                f"check the density field (max cell < {threshold})")
        nelx, nely, nelz = dec["nelx"], dec["nely"], None
        boxes = [
            Part.makeBox(w * cx, cy, thickness,
                         App.Vector(i0 * cx, (nely - 1 - j) * cy, 0.0))
            for (i0, j, w) in dec["rects"]
        ]
        total = nelx * nely

    solid = boxes[0] if len(boxes) == 1 else boxes[0].multiFuse(boxes[1:])
    # adjacent boxes leave coplanar seams; collapse them into single faces.
    solid = solid.removeSplitter()

    out = doc.addObject("Part::Feature", p.get("name", "TopologySolid"))
    out.Shape = solid
    placement = p.get("placement")
    if placement is not None:
        if len(placement) != 3:
            raise ValueError("placement must be [x, y, z] in mm")
        out.Placement.Base = App.Vector(*(float(c) for c in placement))
    doc.recompute()
    h = _register("toposolid", out)
    bb = solid.BoundBox
    return {
        "handle": h,
        "name": out.Name,
        "volume": solid.Volume,
        "solid_cells": dec["solid_cells"],
        "total_cells": total,
        "mass_fraction": round(dec["solid_cells"] / total, 6),
        "n_solids": len(solid.Solids),
        "threshold": threshold,
        "nelx": nelx, "nely": nely, "nelz": nelz,
        "bbox_mm": [round(bb.XLength, 4), round(bb.YLength, 4), round(bb.ZLength, 4)],
    }


# --- transient/radiation thermal (family 4 P2; Elmer-backed) ------------------

def _parse_elmer_scalars(case_dir):
    """Best-effort parse of an Elmer SaveScalars .dat (whitespace columns; the last
    row is the final timestep). Returns that row as floats, or None when absent."""
    import glob
    dats = sorted(glob.glob(os.path.join(case_dir, "*.dat")))
    if not dats:
        return None
    with open(dats[-1]) as f:
        rows = [r for r in f.read().splitlines() if r.strip()]
    if not rows:
        return None
    try:
        return [float(x) for x in rows[-1].split()]
    except ValueError:
        return None


def _resolve_thermal_props(p):
    """Resolve (k, rho, cp) in SI from explicit p['k'/'rho'/'cp'] or a p['material']
    card — the same resolution thermal_transient_1d uses, so the Elmer slab solve and
    its analytic oracle read identical properties. Raises ValueError if incomplete."""
    from driftpin.analysis import materials as _materials
    from driftpin.analysis import thermal as _thermal
    card = _materials.get(p["material"]) if p.get("material") else {}
    k = _thermal._thermal_property(p.get("k"), card, "thermal_conductivity")
    rho = _thermal._thermal_property(p.get("rho"), card, "Density", "density")
    cp = _thermal._thermal_property(p.get("cp"), card, "specific_heat",
                                    "specific_heat_j_kgk")
    if not (k and rho and cp):
        raise ValueError(
            "provide k+rho+cp, or a material with thermal_conductivity/Density/"
            "specific_heat, to build the Elmer slab case")
    return k, rho, cp


def _thermal_body_submit(p, info):
    """Geometry-driven transient thermal — the P3 M4 bridge. Gmsh-meshes a FreeCAD
    solid on the MAIN thread (FemMesh + UNV export; the jobs.py contract keeps all
    FreeCAD work out of the background fn), then ElmerGrid-converts and
    ElmerSolver-solves in the background. FreeCAD's UNV export preserves per-face
    groups, so `convection_faces` are the solid's 1-based face indices (boundary
    tag i == shape.Faces[i-1], verified live); every unlisted face is adiabatic.
    Degrades to {ok:false, reason, install} when ElmerGrid is missing."""
    import shutil
    import tempfile

    from femmesh.gmshtools import GmshTools

    from driftpin import jobs
    from driftpin.analysis import meshbridge as _mb

    elmer_bin = info["path"]
    elmergrid = (shutil.which("ElmerGrid")
                 or os.path.join(os.path.dirname(elmer_bin), "ElmerGrid"))
    if not os.path.isfile(elmergrid):
        return {"ok": False,
                "reason": "ElmerGrid not found (converts the Gmsh UNV mesh for Elmer)",
                "install": "ElmerGrid ships with Elmer — apt install elmerfem-csc, "
                           "or put ElmerGrid next to ElmerSolver on PATH"}

    doc = _active_doc()
    obj = _shape_handle_to_obj(p["body"])
    faces = obj.Shape.Faces
    conv = p.get("convection_faces")
    if not conv:
        raise ValueError("convection_faces (1-based face indices of `body`) is "
                         "required for the geometry bridge")
    conv = sorted({int(i) for i in conv})
    if any(i < 1 or i > len(faces) for i in conv):
        raise ValueError(f"convection_faces out of range 1..{len(faces)}")
    if p.get("h_conv") is None or p.get("duration_s") is None:
        raise ValueError("h_conv and duration_s are required")
    k, rho, cp = _resolve_thermal_props(p)
    h_conv = float(p["h_conv"])
    duration_s = float(p["duration_s"])
    t_initial_c = float(p.get("t_initial_c", 100.0))
    t_ambient_c = float(p.get("t_ambient_c", 25.0))
    n_steps = int(p.get("n_steps", 120))
    char_length = float(p.get("char_length_mm", 0.0))
    element_order = p.get("element_order")

    # mesh + export on the MAIN thread; the temp FemMesh never outlives this call
    mesh = ObjectsFem.makeMeshGmsh(doc, "BridgeMesh")
    mesh.Shape = obj
    if char_length > 0:
        mesh.CharacteristicLengthMax = char_length
    # 2nd-order (quadratic) tets resolve a sharp transient gradient far better than
    # linear C3D4 — a coarse linear box can under-resolve a high-Biot wall and report
    # too little cooling. Default keeps Gmsh's choice.
    if element_order is not None:
        if element_order not in ("1st", "2nd"):
            raise ValueError("element_order must be '1st' or '2nd'")
        mesh.ElementOrder = element_order
    doc.recompute()
    case_dir = tempfile.mkdtemp(prefix="elmer_body_")
    try:
        err = GmshTools(mesh).create_mesh()
        nodes, tets = mesh.FemMesh.NodeCount, mesh.FemMesh.TetraCount
        if not tets:
            raise RuntimeError(f"Gmsh produced no volume mesh ({err or 'no detail'})")
        mesh.FemMesh.write(os.path.join(case_dir, "body.unv"))
    finally:
        doc.removeObject(mesh.Name)
        doc.recompute()

    built = _mb.write_body_transient_case(
        case_dir, k=k, rho=rho, cp=cp, h_conv=h_conv, duration_s=duration_s,
        convection_tags=conv, t_initial_c=t_initial_c, t_ambient_c=t_ambient_c,
        n_steps=n_steps)
    grid_argv = [elmergrid] + built["elmergrid_argv"][1:]

    bb = obj.Shape.BoundBox
    key = jobs.content_key("thermal_transient", {"body": {
        "volume": round(obj.Shape.Volume, 6), "area": round(obj.Shape.Area, 6),
        "bbox": [round(v, 6) for v in (bb.XLength, bb.YLength, bb.ZLength)],
        "n_faces": len(faces), "conv": conv, "char": char_length,
        "k": k, "rho": rho, "cp": cp, "h": h_conv, "ti": t_initial_c,
        "ta": t_ambient_c, "t": duration_s, "ns": n_steps}})

    def _work():
        import subprocess
        grid = subprocess.run(grid_argv, cwd=case_dir, capture_output=True, text=True)
        if grid.returncode != 0:
            return {"ok": False, "returncode": grid.returncode, "solver": "elmergrid",
                    "mode": "body", "case_dir": case_dir,
                    "stdout_tail": ((grid.stdout or "") + (grid.stderr or ""))[-2000:]}
        # ElmerGrid can succeed (rc=0) yet drop the boundary groups during UNV import
        # (the silent-zero-boundary failure the .grd path hit). Then the convective BC
        # binds to nothing, the body stays adiabatic, and the solve returns ok:true
        # with physically wrong temperatures. Fail loudly instead.
        if not _mb.mesh_boundary_count(case_dir, built["mesh_name"]):
            return {"ok": False, "returncode": grid.returncode, "solver": "elmergrid",
                    "mode": "body", "case_dir": case_dir,
                    "reason": "ElmerGrid produced 0 boundary elements — the convective "
                              "faces would bind to nothing (adiabatic). The mesh's face "
                              "groups did not survive UNV import; check the solid/mesh.",
                    "stdout_tail": ((grid.stdout or "") + (grid.stderr or ""))[-2000:]}
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=case_dir,
                              capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "mode": "body",
            "case_dir": case_dir,
            "nodes": nodes,
            "tets": tets,
            "dt": built["dt"],
            "n_steps": built["n_steps"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        parsed = _mb.parse_minmax_scalars(case_dir, built["scalars"])
        if parsed:
            out.update(parsed)                       # t_max_c, t_min_c, ...
        return out

    return jobs.submit("thermal_transient", _work, key=key,
                       meta={"mode": "body", "duration_s": duration_s, "tets": tets})


@handler("thermal_transient_submit")
def _h_thermal_transient_submit(p):
    """Transient thermal FEM via Elmer, OFF the MCP channel. Degrades to
    {ok:false, reason, install} when ElmerSolver is absent (never raises on a miss).

    Three ways to drive it:
      * **Build the analytic-slab case** — pass the plane-wall transient params
        (`half_thickness_mm`, `h_conv`, `duration_s`, `t_initial_c`, `t_ambient_c`,
        and `k`+`rho`+`cp` or a `material`). The handler writes the 1-D conduction
        case (native Elmer mesh + .sif, symmetry at the centre, convection at the
        surface) and SaveScalars-extracts the centre/surface temperatures — directly
        gateable against thermal_transient_1d (the Heisler oracle).
      * **Solve a real FreeCAD solid (the P3 M4 geometry bridge)** — pass a `body`
        handle, `convection_faces` (1-based face indices that get the convective BC;
        the rest are adiabatic), `h_conv`, `duration_s`, the material props, and an
        optional `char_length_mm` mesh size. Gmsh meshes the solid on the main
        thread; ElmerGrid + ElmerSolver run in the background. Result carries
        {t_max_c, t_min_c} (max = interior, min = convective surface).
      * **Run a prepared `case_dir`** — pass a directory containing its `.sif` and
        mesh; the handler just runs ElmerSolver there and parses the scalar history.

    The background job runs ONLY solver subprocesses (it never touches FreeCAD).
    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result for
    {ok, returncode, solver, case_dir, stdout_tail} plus, for the slab case,
    {t_center_c, t_surface_c, n_steps_written}, for a body {t_max_c, t_min_c, nodes,
    tets}, or for a prepared case {scalars_final}."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    if p.get("body"):                                # --- geometry bridge (M4) ---
        return _thermal_body_submit(p, info)
    from driftpin import jobs
    elmer_bin = info["path"]
    case_dir = p.get("case_dir")

    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        key = jobs.content_key("thermal_transient",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work():
            import subprocess
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "scalars_final": _parse_elmer_scalars(case_dir),
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("thermal_transient", _work, key=key,
                           meta={"case_dir": case_dir, "duration_s": p.get("duration_s")})

    if p.get("half_thickness_mm") is None:
        raise ValueError(
            "provide a prepared `case_dir`, or the slab params "
            "(half_thickness_mm, h_conv, duration_s, and k+rho+cp or material) to "
            "build the analytic-slab case gated against thermal_transient_1d")

    # --- build the 1-D plane-wall transient case from physical params ------------
    k, rho, cp = _resolve_thermal_props(p)
    half_thickness_m = float(p["half_thickness_mm"]) / 1000.0
    slab = {
        "half_thickness_m": half_thickness_m,
        "k": k, "rho": rho, "cp": cp,
        "h_conv": float(p["h_conv"]),
        "t_initial_c": float(p.get("t_initial_c", 100.0)),
        "t_ambient_c": float(p.get("t_ambient_c", 25.0)),
        "duration_s": float(p["duration_s"]),
        "n_elements": int(p.get("n_elements", 40)),
        "n_steps": int(p.get("n_steps", 120)),
    }
    key = jobs.content_key("thermal_transient", {"slab": slab})

    def _work():
        import subprocess
        import tempfile
        from driftpin.analysis import elmer as _elmer
        cdir = tempfile.mkdtemp(prefix="elmer_slab_")
        built = _elmer.write_slab_transient_case(cdir, **slab)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "n_steps": built["n_steps"],
            "dt": built["dt"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        parsed = _elmer.parse_slab_scalars(cdir, built["scalars"])
        if parsed:
            out.update(parsed)                       # t_center_c, t_surface_c, ...
        return out

    return jobs.submit("thermal_transient", _work, key=key,
                       meta={"mode": "slab", "duration_s": slab["duration_s"],
                             "half_thickness_mm": p["half_thickness_mm"]})


@handler("thermal_radiation_submit")
def _h_thermal_radiation_submit(p):
    """Diffuse-gray radiation FEM via Elmer, OFF the MCP channel — the radiation
    sibling of thermal_transient_submit. Degrades to {ok:false, reason, install} when
    ElmerSolver is absent (never raises on a miss).

    Two ways to drive it:
      * **Build the two-plate enclosure case** — pass `t1_c`, `t2_c` and the two
        emissivities (`emissivity_1`, `emissivity_2`, default 0.8). The handler writes
        a 2-D two-parallel-plate case (each plate held isothermal, their facing faces
        radiating across an unmeshed vacuum gap), runs **ViewFactors then ElmerSolver**,
        and SaveScalars-extracts the net radiative exchange — directly gateable against
        thermal_radiation's two-plate oracle q = σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1). Geometry/
        mesh knobs: `width_m` (1.0), `gap_m` (0.01), `plate_thickness_m` (0.01), `n_x`
        (80), `k_plate` (400).
      * **Run a prepared `case_dir`** — a directory with its `.sif`+mesh+view factors;
        the handler runs ElmerSolver there (ViewFactors too if a *.dat is missing) and
        parses the scalar flux.

    The background job runs ONLY the ViewFactors/ElmerSolver subprocesses (never touches
    FreeCAD). Returns the degradation dict, or {job_id, status, cache_hit}; poll
    job_result for {ok, returncode, solver, case_dir, stdout_tail} plus, for the plate
    case, {flux_w_m2, q_net_w, two_plate_flux_w_m2, oracle_ratio, t1_c, t2_c,
    emissivity_1, emissivity_2}."""
    import shutil

    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    from driftpin import jobs
    elmer_bin = info["path"]
    vf_bin = shutil.which("ViewFactors") or os.path.join(
        os.path.dirname(elmer_bin), "ViewFactors")
    case_dir = p.get("case_dir")

    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        import glob as _glob
        key = jobs.content_key("thermal_radiation",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work():
            import subprocess
            if not _glob.glob(os.path.join(case_dir, "*ViewFactors*")):
                subprocess.run([vf_bin, sif], cwd=case_dir, capture_output=True, text=True)
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "scalars_final": _parse_elmer_scalars(case_dir),
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("thermal_radiation", _work, key=key,
                           meta={"case_dir": case_dir})

    if p.get("t1_c") is None or p.get("t2_c") is None:
        raise ValueError(
            "provide a prepared `case_dir`, or the two-plate params (t1_c, t2_c, and "
            "optionally emissivity_1/emissivity_2) to build the radiation enclosure "
            "case gated against the σ(T₁⁴−T₂⁴)/(1/ε₁+1/ε₂−1) oracle")

    # --- build the two-plate diffuse-gray enclosure case from physical params -----
    plates = {
        "t1_c": float(p["t1_c"]),
        "t2_c": float(p["t2_c"]),
        "emissivity_1": float(p.get("emissivity_1", 0.8)),
        "emissivity_2": float(p.get("emissivity_2", 0.8)),
        "width_m": float(p.get("width_m", 1.0)),
        "gap_m": float(p.get("gap_m", 0.01)),
        "plate_thickness_m": float(p.get("plate_thickness_m", 0.01)),
        "n_x": int(p.get("n_x", 80)),
        "k_plate": float(p.get("k_plate", 400.0)),
    }
    key = jobs.content_key("thermal_radiation", {"plates": plates})

    def _work():
        import subprocess
        import tempfile
        from driftpin.analysis import elmer as _elmer
        from driftpin.analysis import thermal as _thermal
        cdir = tempfile.mkdtemp(prefix="elmer_rad_")
        built = _elmer.write_radiation_plates_case(cdir, **plates)
        vf = subprocess.run([vf_bin, built["sif"]], cwd=cdir,
                            capture_output=True, text=True)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "viewfactors_returncode": vf.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "t1_c": plates["t1_c"], "t2_c": plates["t2_c"],
            "emissivity_1": plates["emissivity_1"],
            "emissivity_2": plates["emissivity_2"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        orc = _thermal.radiation_exchange(
            plates["t1_c"], plates["t2_c"], plates["emissivity_1"],
            plates["emissivity_2"], area_1_m2=built["area_1_m2"],
            area_2_m2=built["area_1_m2"])
        out["two_plate_flux_w_m2"] = orc["two_plate_flux_w_m2"]
        parsed = _elmer.parse_radiation_flux(cdir, built["scalars"], built["area_1_m2"])
        if parsed:
            out["q_net_w"] = round(parsed["q_net_w"], 6)
            out["flux_w_m2"] = round(parsed["flux_w_m2"], 6)
            if orc["two_plate_flux_w_m2"] != 0:
                out["oracle_ratio"] = round(
                    parsed["flux_w_m2"] / orc["two_plate_flux_w_m2"], 5)
        return out

    return jobs.submit("thermal_radiation", _work, key=key,
                       meta={"mode": "plates", "t1_c": plates["t1_c"],
                             "t2_c": plates["t2_c"]})


# --- conjugate heat transfer (P3 M6 frontier; Elmer-backed) --------------------

@handler("thermal_composite_wall")
def _h_thermal_composite_wall(p):
    """Exact series thermal-resistance network of a plane composite wall (the
    classic overall-U calculation and the CHT family's closed-form oracle):
    U = 1/(1/h_in + Σ tᵢ/kᵢ + 1/h_out), q = U·ΔT, every interface temperature
    exact. `layers` is the in→out list of {thickness_mm, k|material}; `h_in`/
    `h_out` optional film coefficients. Pure-Python, no solver. Returns
    {u_w_m2k, r_total_m2k_w, q_w_m2, q_w, layer_resistances_m2k_w,
    interface_temps_c, t_in_c, t_out_c, area_m2}."""
    from driftpin.analysis import cht as _cht
    return _cht.composite_wall(
        p["layers"], float(p["t_in_c"]), float(p["t_out_c"]),
        h_in=(float(p["h_in"]) if p.get("h_in") is not None else None),
        h_out=(float(p["h_out"]) if p.get("h_out") is not None else None),
        area_m2=float(p.get("area_m2", 1.0)))


@handler("cht_channel_submit")
def _h_cht_channel_submit(p):
    """Conjugate heat transfer via Elmer, OFF the MCP channel — one solve spanning
    a plug-flow fluid channel AND a conducting solid wall, coupled at their shared
    interface (the P3 M6 frontier family). Degrades to {ok:false, reason, install}
    when ElmerSolver is absent.

    Builds the two-body channel case (constant outer heat flux, inlet Dirichlet,
    everything else adiabatic) whose gates are exact WITHOUT a Nusselt correlation:
    the outlet bulk temperature follows the energy balance q″·L = ṁ·c_p·ΔT and the
    solid-layer drop is q″·t/k. Params: `flux_w_m2`, `velocity_m_s`, `t_in_c`,
    geometry (`length_m`, `fluid_height_m`, `solid_thickness_m`), fluid `k_fluid`/
    `rho_fluid`/`cp_fluid`, `k_solid`, mesh (`nx`, `ny_fluid`, `ny_solid`). The
    writer rejects cell Péclet > 25 (the stabilized-advection envelope). Also
    accepts a prepared `case_dir`.

    The background job runs ONLY the ElmerSolver subprocess. Returns the
    degradation dict or {job_id, status, cache_hit}; poll job_result for {ok,
    t_outlet_mean_c, t_out_exact_c, energy_balance_ratio (≈1), dt_solid_k,
    dt_solid_exact_k, solid_drop_ratio (≈1), pe_cell, case_dir, stdout_tail}."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import cht as _cht
    elmer_bin = info["path"]

    case_dir = p.get("case_dir")
    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        key = jobs.content_key("cht_channel",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work_prepared():
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "scalars": _cht.parse_cht_scalars(case_dir),
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("cht_channel", _work_prepared, key=key,
                           meta={"case_dir": case_dir})

    params = {k: float(p[k]) for k in (
        "flux_w_m2", "velocity_m_s", "t_in_c", "length_m", "fluid_height_m",
        "solid_thickness_m", "k_fluid", "rho_fluid", "cp_fluid", "k_solid")
        if p.get(k) is not None}
    for k in ("nx", "ny_fluid", "ny_solid"):
        if p.get(k) is not None:
            params[k] = int(p[k])
    key = jobs.content_key("cht_channel", {"channel": params})

    def _work():
        cdir = tempfile.mkdtemp(prefix="elmer_cht_")
        built = _cht.write_cht_channel_case(cdir, **params)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        orc = built["oracle"]
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "pe_cell": built["pe_cell"],
            "t_out_exact_c": orc["t_out_c"],
            "dt_solid_exact_k": orc["dt_solid_k"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        parsed = _cht.parse_cht_scalars(cdir, built["scalars"])
        if parsed:
            out.update(parsed)
            t_in = params.get("t_in_c", 20.0)
            if orc["dt_out_k"] > 0:
                out["energy_balance_ratio"] = round(
                    (parsed["t_outlet_mean_c"] - t_in) / orc["dt_out_k"], 5)
            if orc["dt_solid_k"] > 0:
                out["solid_drop_ratio"] = round(
                    parsed["dt_solid_k"] / orc["dt_solid_k"], 5)
        return out

    return jobs.submit("cht_channel", _work, key=key,
                       meta={"mode": "channel",
                             "flux_w_m2": params.get("flux_w_m2", 10000.0)})


@handler("cht_graetz_submit")
def _h_cht_graetz_submit(p):
    """Flow-coupled Graetz channel via Elmer (SIMULATION_NEXT B4), OFF the MCP
    channel — FlowSolve computes the real laminar profile and HeatSolver rides on
    it (Convection = Computed) between isothermal walls; the developed mixing-cup
    decay yields a TRUE Nusselt number gated against the Graetz eigenvalue
    Nu_T = 7.5407 (parallel plates; a slug profile would give pi^2 = 9.87 — the
    discriminator). Second gate: the solved parabola's u_max/u_mean = 3/2 exactly.
    Degrades to {ok:false, reason, install} when ElmerSolver is absent. Also
    accepts a prepared case_dir. Returns the degradation dict or {job_id, status,
    cache_hit}; poll job_result."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import cht as _cht
    elmer_bin = info["path"]

    case_dir = p.get("case_dir")
    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        key = jobs.content_key("cht_graetz",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work_prepared():
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("cht_graetz", _work_prepared, key=key,
                           meta={"case_dir": case_dir})

    params = {k: float(p[k]) for k in (
        "velocity_m_s", "gap_m", "length_m", "rho_fluid", "mu_fluid",
        "k_fluid", "cp_fluid", "t_in_c", "t_wall_c") if p.get(k) is not None}
    for k in ("nx", "ny", "max_iterations"):
        if p.get(k) is not None:
            params[k] = int(p[k])
    key = jobs.content_key("cht_graetz", {"channel": params})

    def _work():
        cdir = tempfile.mkdtemp(prefix="elmer_graetz_")
        built = _cht.write_graetz_channel_case(cdir, **params)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "reynolds": round(built["reynolds"], 2),
            "prandtl": round(built["prandtl"], 4),
            "pe_cell": round(built["pe_cell"], 2),
            "nu_exact": built["nu_exact"],
            "nu_slug": built["nu_slug"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        parsed = _cht.parse_graetz_channel(
            cdir, nx=built["nx"], ny=built["ny"], gap_m=built["gap_m"],
            length_m=built["length_m"], rho_fluid=built["rho_fluid"],
            cp_fluid=built["cp_fluid"], k_fluid=built["k_fluid"],
            t_wall_c=built["t_wall_c"])
        if parsed:
            out["u_max_over_mean"] = parsed["u_max_over_mean"]
            out["nu_fit"] = parsed["nu_fit"]
            if parsed["nu_fit"]:
                out["nu_ratio"] = round(parsed["nu_fit"] / built["nu_exact"], 4)
        return out

    return jobs.submit("cht_graetz", _work, key=key, meta={"mode": "graetz"})


# --- acoustics + harmonic response (SIMULATION_NEXT B1/B2; Elmer-backed) --------

@handler("acoustic_fem_submit")
def _h_acoustic_fem_submit(p):
    """Acoustic FEM via Elmer HelmholtzSolve, OFF the MCP channel (SIMULATION_NEXT
    Tier B1) — the higher-order twin of the acoustic_screen closed forms, gated
    against them. Degrades to {ok:false, reason, install} when ElmerSolver is
    absent. kind='duct': driven closed duct, gate = exact rigid-end standing-wave
    pressure 1/cos(kL) -> p_end_ratio ~ 1 (machine-tight). kind='cavity': rigid
    rectangular cavity swept around the exact (mode_nx, mode_ny) eigenfrequency
    by a Wave Flux corner source; the in-phase corner-probe response flips sign
    through resonance -> f_solved_hz / f_exact_hz ~ 1 (<0.1% in practice). Also
    accepts a prepared case_dir. Returns the degradation dict or {job_id, status,
    cache_hit}; poll job_result."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import acoustics as _ac
    elmer_bin = info["path"]

    case_dir = p.get("case_dir")
    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        key = jobs.content_key("acoustic_fem",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work_prepared():
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("acoustic_fem", _work_prepared, key=key,
                           meta={"case_dir": case_dir})

    kind = p.get("kind", "duct")
    if kind == "duct":
        params = {k: float(p[k]) for k in ("length_m", "kl", "c_m_s")
                  if p.get(k) is not None}
        if p.get("n_elements") is not None:
            params["n_elements"] = int(p["n_elements"])
        key = jobs.content_key("acoustic_fem", {"duct": params})

        def _work():
            cdir = tempfile.mkdtemp(prefix="elmer_ac_duct_")
            built = _ac.write_helmholtz_duct_case(cdir, **params)
            proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                                  capture_output=True, text=True)
            out = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "kind": "duct",
                "case_dir": cdir,
                "frequency_hz": round(built["frequency_hz"], 4),
                "kl": built["kl"],
                "p_end_exact": round(built["p_end_exact"], 6),
                "p_mean_exact": round(built["p_mean_exact"], 6),
                "stdout_tail": (proc.stdout or "")[-2000:],
            }
            parsed = _ac.parse_helmholtz_duct(cdir, built["scalars"])
            if parsed:
                out["p_end_re"] = round(parsed["p_end_re"], 6)
                out["p_mean_re"] = round(parsed["p_mean_re"], 6)
                out["p_end_ratio"] = round(parsed["p_end_re"] / built["p_end_exact"], 6)
                out["p_mean_ratio"] = round(parsed["p_mean_re"] / built["p_mean_exact"], 6)
            return out

        return jobs.submit("acoustic_fem", _work, key=key,
                           meta={"kind": "duct"})

    if kind == "cavity":
        params = {k: float(p[k]) for k in ("lx_m", "ly_m", "span_pct", "c_m_s")
                  if p.get(k) is not None}
        for k in ("nx", "ny", "mode_nx", "mode_ny", "n_steps"):
            if p.get(k) is not None:
                params[k] = int(p[k])
        key = jobs.content_key("acoustic_fem", {"cavity": params})

        def _work_cavity():
            cdir = tempfile.mkdtemp(prefix="elmer_ac_cav_")
            built = _ac.write_helmholtz_cavity_case(cdir, **params)
            proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                                  capture_output=True, text=True)
            out = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "kind": "cavity",
                "case_dir": cdir,
                "mode": built["mode"],
                "f_exact_hz": round(built["f_exact_hz"], 4),
                "stdout_tail": (proc.stdout or "")[-2000:],
            }
            rows = _ac.parse_helmholtz_cavity(cdir, built["scalars"])
            if rows:
                f_est = _ac.locate_resonance(rows)
                if f_est is not None:
                    out["f_solved_hz"] = round(f_est, 4)
                    out["mode_ratio"] = round(f_est / built["f_exact_hz"], 6)
                else:
                    out["ok"] = False
                    out["reason"] = ("no resonance sign-flip captured in the sweep "
                                     "window — widen span_pct or check the mode")
            return out

        return jobs.submit("acoustic_fem", _work_cavity, key=key,
                           meta={"kind": "cavity"})

    raise ValueError(f"unknown kind {kind!r}; choose 'duct' or 'cavity'")


@handler("harmonic_response")
def _h_harmonic_response(p):
    """Exact SDOF harmonic FRF (no solver): |H|, phase, Q = 1/(2ζ√(1−ζ²)), peak
    frequency, half-power bandwidth — the oracle the Elmer harmonic sweep is gated
    against. See driftpin.analysis.vibration. Returns {natural_frequency_hz,
    damping_ratio, q_factor, f_peak_hz, half_power_bandwidth_hz, frequency_ratio,
    amplification, phase_deg, amplitude_mm, fidelity, band_pct, valid_range_ok,
    warnings, escalate_to}."""
    from driftpin.analysis import vibration
    return vibration.harmonic_response(**p)


@handler("harmonic_response_submit")
def _h_harmonic_response_submit(p):
    """Harmonic forced response via Elmer StressSolve (Harmonic Analysis), OFF the
    MCP channel (SIMULATION_NEXT Tier B2). A plane-stress cantilever driven by a
    harmonic tip traction is swept through its first resonance; gates from the
    in-phase response: f1_ratio (Re(H)=0 exactly at f_n, vs the Euler-Bernoulli
    beam_modal closed form), static_ratio (quasi-static point vs F·L³/3EI), and
    q_ratio (max|Re|/static vs Q/2 = 1/(4ζ), the SDOF light-damping identity).
    Degrades to {ok:false, reason, install} when ElmerSolver is absent. Also
    accepts a prepared case_dir. Returns the degradation dict or {job_id, status,
    cache_hit}; poll job_result."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import vibration as _vib
    elmer_bin = info["path"]

    case_dir = p.get("case_dir")
    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        key = jobs.content_key("harmonic_response",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work_prepared():
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("harmonic_response", _work_prepared, key=key,
                           meta={"case_dir": case_dir})

    params = {k: float(p[k]) for k in (
        "length_m", "height_m", "youngs_pa", "density_kg_m3", "poisson",
        "damping_ratio", "traction_pa", "span_pct") if p.get(k) is not None}
    for k in ("nx", "ny", "n_sweep"):
        if p.get(k) is not None:
            params[k] = int(p[k])
    key = jobs.content_key("harmonic_response", {"beam": params})

    def _work():
        cdir = tempfile.mkdtemp(prefix="elmer_frf_")
        built = _vib.write_harmonic_beam_case(cdir, **params)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "f1_eb_hz": round(built["f1_eb_hz"], 4),
            "static_exact_m": built["static_exact_m"],
            "q_factor": built["q_factor"],
            "damping_ratio": built["damping_ratio"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        tips = _vib.parse_harmonic_beam(cdir, n_steps=built["n_steps"],
                                        tip_node=built["tip_node"])
        if tips:
            static = abs(tips[0])
            out["static_solved_m"] = static
            out["static_ratio"] = round(static / built["static_exact_m"], 5)
            f_est = _vib.locate_frf_resonance(built["freqs"][1:], tips[1:])
            if f_est is not None:
                out["f1_solved_hz"] = round(f_est, 4)
                out["f1_ratio"] = round(f_est / built["f1_eb_hz"], 5)
            peak = max(abs(t) for t in tips[1:])
            out["peak_over_static"] = round(peak / static, 4)
            out["q_ratio"] = round((peak / static) / (built["q_factor"] / 2.0), 5)
            out["frf"] = [[round(f, 3), t] for f, t in zip(built["freqs"], tips)]
        return out

    return jobs.submit("harmonic_response", _work, key=key,
                       meta={"mode": "beam"})


# --- low-frequency EM (P3 M6 frontier; Elmer-backed) ---------------------------

@handler("em_skin_depth")
def _h_em_skin_depth(p):
    """Exact AC skin depth δ = √(2/(ω·μ₀·μ_r·σ)) + the per-square surface
    resistance R_s = 1/(σ·δ) — the closed-form induction/skin oracle. σ from
    `conductivity_s_m` or a `conductor` name (copper, aluminum, …). Pure-Python,
    no solver. Returns {skin_depth_m, skin_depth_mm, surface_resistance_ohm,
    angular_frequency_rad_s, conductivity_s_m, mu_r}."""
    from driftpin.analysis import em as _em
    return _em.skin_depth(float(p["frequency_hz"]),
                          conductivity_s_m=p.get("conductivity_s_m"),
                          mu_r=float(p.get("mu_r", 1.0)),
                          conductor=p.get("conductor"))


@handler("em_dc_resistance")
def _h_em_dc_resistance(p):
    """Exact DC resistance of a uniform conductor, R = L/(σ·A), with the Ohm/Joule
    pair (I = V/R, P = V·I) when `voltage_v` is given. σ from `conductivity_s_m` or
    a `conductor` name. Pure-Python, no solver. Returns {resistance_ohm,
    conductivity_s_m, length_m, area_m2, current_a?, joule_w?}."""
    from driftpin.analysis import em as _em
    return _em.dc_resistance(
        float(p["length_mm"]), float(p["area_mm2"]),
        conductivity_s_m=p.get("conductivity_s_m"), conductor=p.get("conductor"),
        voltage_v=(float(p["voltage_v"]) if p.get("voltage_v") is not None else None))


@handler("em_field")
def _h_em_field(p):
    """Exact magnetostatic field of the two canonical sources: kind='wire' is the
    long straight wire B = μ₀·I/(2π·r) at `distance_mm`; kind='solenoid' is the
    long-solenoid interior B = μ₀·μ_r·n·I with `turns_per_m`. Pure-Python, no
    solver. Returns {b_t, b_mt, …}."""
    from driftpin.analysis import em as _em
    kind = p.get("kind", "wire")
    if kind == "wire":
        return _em.wire_field(float(p["current_a"]), float(p["distance_mm"]))
    if kind == "solenoid":
        return _em.solenoid_field(float(p["turns_per_m"]), float(p["current_a"]),
                                  mu_r=float(p.get("mu_r", 1.0)))
    raise ValueError(f"kind must be 'wire' or 'solenoid', got {kind!r}")


@handler("em_conduction_submit")
def _h_em_conduction_submit(p):
    """DC current conduction via Elmer's StatCurrentSolver, OFF the MCP channel —
    the first half of the P3 M6 EM family. Degrades to {ok:false, reason, install}
    when ElmerSolver is absent.

    Builds a rectangular strip (`length_m` × `width_m`, unit depth) with
    `voltage_v` across its ends, runs the solve, and reads the electrode current
    (the diffusive flux of Potential × conductivity), the total Joule heating and
    Elmer's own effective resistance — all three machine-exact against R = L/(σ·A)
    (verified live to 1e-6). σ from `conductivity_s_m` or a `conductor` name.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, current_a, joule_w, effective_resistance_ohm, resistance_exact_ohm,
    current_exact_a, resistance_ratio (≈1), case_dir, stdout_tail}."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import em as _em
    elmer_bin = info["path"]
    params = {k: float(p[k]) for k in ("voltage_v", "length_m", "width_m",
                                       "conductivity_s_m") if p.get(k) is not None}
    for k in ("nx", "ny"):
        if p.get(k) is not None:
            params[k] = int(p[k])
    if p.get("conductor") is not None:
        params["conductor"] = str(p["conductor"])
    key = jobs.content_key("em_conduction", {"strip": params})

    def _work():
        cdir = tempfile.mkdtemp(prefix="elmer_dc_")
        built = _em.write_dc_strip_case(cdir, **params)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        orc = built["oracle"]
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "resistance_exact_ohm": orc["resistance_ohm"],
            "current_exact_a": orc.get("current_a"),
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        parsed = _em.parse_dc_scalars(cdir, built["scalars"])
        if parsed:
            out.update(parsed)
            if parsed.get("effective_resistance_ohm") and orc["resistance_ohm"] > 0:
                out["resistance_ratio"] = round(
                    parsed["effective_resistance_ohm"] / orc["resistance_ohm"], 6)
        return out

    return jobs.submit("em_conduction", _work, key=key,
                       meta={"mode": "strip", "voltage_v": params.get("voltage_v", 0.001)})


@handler("em_induction_heating_submit")
def _h_em_induction_heating_submit(p):
    """Coupled induction heating via Elmer (SIMULATION_NEXT B5), OFF the MCP
    channel — completes em_induction_submit into a THERMAL answer: the harmonic
    MagnetoDynamics solve runs once, MagnetoDynamicsCalcFields turns it into the
    time-averaged Joule loss field, and a transient adiabatic HeatSolver
    integrates it for heat_duration_s. Two gates: joule_power_ratio (the solved
    eddy-current power vs the exact P'' = R_s*|H0|^2/2 = omega^2*sigma*A0^2*delta/4)
    and energy_balance_ratio (mean dT vs P*t/(m*cp)). Degrades to {ok:false,
    reason, install} when ElmerSolver is absent. Also accepts a prepared case_dir.
    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import em as _em
    elmer_bin = info["path"]

    case_dir = p.get("case_dir")
    if case_dir:                                     # --- prepared case directory ---
        if not os.path.isdir(case_dir):
            raise ValueError(f"case_dir {case_dir!r} is not a directory")
        sif = p.get("sif", "case.sif")
        key = jobs.content_key("em_induction_heating",
                               {"case_dir": os.path.abspath(case_dir), "sif": sif})

        def _work_prepared():
            proc = subprocess.run([elmer_bin, sif], cwd=case_dir,
                                  capture_output=True, text=True)
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "solver": "elmer",
                "case_dir": case_dir,
                "scalars": _em.parse_induction_scalars(case_dir),
                "stdout_tail": (proc.stdout or "")[-2000:],
            }

        return jobs.submit("em_induction_heating", _work_prepared, key=key,
                           meta={"case_dir": case_dir})

    params = {k: float(p[k]) for k in (
        "frequency_hz", "conductivity_s_m", "mu_r", "a_surface",
        "density_kg_m3", "cp_j_kgk", "k_thermal", "heat_duration_s",
        "depths") if p.get(k) is not None}
    if p.get("conductor") is not None:
        params["conductor"] = str(p["conductor"])
    for k in ("n_steps", "nx", "ny"):
        if p.get(k) is not None:
            params[k] = int(p[k])
    key = jobs.content_key("em_induction_heating", {"slab": params})

    def _work():
        cdir = tempfile.mkdtemp(prefix="elmer_indheat_")
        built = _em.write_induction_heating_case(cdir, **params)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "skin_depth_m": built["oracle"]["skin_depth_m"],
            "p_total_exact_w_m": round(built["p_total_w_m"], 4),
            "dt_mean_exact_k": round(built["dt_mean_exact_k"], 5),
            "heat_duration_s": built["heat_duration_s"],
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        parsed = _em.parse_induction_scalars(cdir, built["scalars"])
        if parsed:
            out["t_mean_final_k"] = round(parsed["t_mean_final_k"], 5)
            if parsed["eddy_power_w_m"] is not None:
                out["eddy_power_w_m"] = round(parsed["eddy_power_w_m"], 4)
                out["joule_power_ratio"] = round(
                    parsed["eddy_power_w_m"] / built["p_total_w_m"], 5)
            if built["dt_mean_exact_k"] > 0:
                out["energy_balance_ratio"] = round(
                    parsed["t_mean_final_k"] / built["dt_mean_exact_k"], 5)
        return out

    return jobs.submit("em_induction_heating", _work, key=key,
                       meta={"mode": "slab"})


@handler("em_induction_submit")
def _h_em_induction_submit(p):
    """AC skin effect via Elmer's harmonic 2-D magnetodynamics, OFF the MCP channel
    — the induction-heating half of the P3 M6 EM family. Degrades to {ok:false,
    reason, install} when ElmerSolver is absent.

    Builds a conductor slab `depths` skin depths deep driven by the surface vector
    potential at `frequency_hz`, runs MagnetoDynamics2DHarmonic, and least-squares
    fits the e-folding length of the solved complex A(x) in BOTH magnitude and
    phase — each must equal the exact δ = √(2/(ω·μ·σ)) (verified live to 0.1 %).
    σ from `conductivity_s_m` or a `conductor` name; optional `mu_r`.

    Returns the degradation dict or {job_id, status, cache_hit}; poll job_result
    for {ok, skin_depth_exact_m, decay_length_m, phase_length_m, decay_ratio (≈1),
    phase_ratio (≈1), case_dir, stdout_tail}."""
    info = _require_solver("elmer")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import subprocess
    import tempfile

    from driftpin import jobs
    from driftpin.analysis import em as _em
    elmer_bin = info["path"]
    params = {k: float(p[k]) for k in ("frequency_hz", "conductivity_s_m", "mu_r",
                                       "depths") if p.get(k) is not None}
    for k in ("nx", "ny"):
        if p.get(k) is not None:
            params[k] = int(p[k])
    if p.get("conductor") is not None:
        params["conductor"] = str(p["conductor"])
    key = jobs.content_key("em_induction", {"skin": params})

    def _work():
        cdir = tempfile.mkdtemp(prefix="elmer_skin_")
        built = _em.write_skin_effect_case(cdir, **params)
        proc = subprocess.run([elmer_bin, built["sif"]], cwd=cdir,
                              capture_output=True, text=True)
        delta = built["oracle"]["skin_depth_m"]
        out = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "solver": "elmer",
            "case_dir": cdir,
            "skin_depth_exact_m": delta,
            "frequency_hz": built["oracle"]["angular_frequency_rad_s"] / (2 * 3.141592653589793),
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        pts = _em.parse_line_profile(cdir, built["line_file"])
        if pts:
            try:
                fit = _em.fit_decay_length(pts, 0.5 * delta, 2.5 * delta)
            except ValueError as exc:
                out["ok"] = False
                out["reason"] = f"profile fit failed: {exc}"
                return out
            out["decay_length_m"] = fit["decay_length_m"]
            out["phase_length_m"] = fit["phase_length_m"]
            out["decay_ratio"] = round(fit["decay_length_m"] / delta, 5)
            out["phase_ratio"] = round(fit["phase_length_m"] / delta, 5)
        return out

    return jobs.submit("em_induction", _work, key=key,
                       meta={"mode": "skin", "frequency_hz": params.get("frequency_hz", 50.0)})


# --- CFD (family 6 P2; OpenFOAM/SU2-backed) -----------------------------------

def _run_foam(case_dir, argv_list, env_bashrc):
    """Run a sequence of OpenFOAM apps (each an argv list) in ``case_dir``, sourcing
    ``env_bashrc`` first so WM_PROJECT_DIR/FOAM_ETC are exported — without that the
    foam apps abort with "Could not find mandatory etc entry 'controlDict'". Apps run
    left-to-right, stopping at the first failure. Returns (returncode, combined_tail)."""
    import subprocess
    chain = " && ".join(" ".join(a) for a in argv_list)
    script = (f"source '{env_bashrc}' >/dev/null 2>&1\n" if env_bashrc else "") + chain
    proc = subprocess.run(["bash", "-c", script], cwd=case_dir,
                          capture_output=True, text=True)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-2000:]


def _openfoam_submit(p, kind):
    """Shared OpenFOAM/SU2 runner for the prepared-`case_dir` path of the
    cfd_*_flow_submit handlers. Degrades to the structured dict when no CFD solver
    resolves; otherwise runs the solver app in the prepared case (with the OpenFOAM
    environment sourced) as a background subprocess. ``kind`` ('internal' | 'external')
    labels the job/meta; force/pressure extraction lives in the case's setup."""
    info = _require_solver("openfoam")
    is_openfoam = info["ok"]
    if not info["ok"]:
        info_su2 = _require_solver("su2")             # SU2 is the documented alternative
        if not info_su2["ok"]:
            return info                               # report the primary solver's hint
        info = info_su2
    import shutil

    from driftpin import jobs, solvers
    case_dir = p.get("case_dir")
    if not case_dir or not os.path.isdir(case_dir):
        raise ValueError(
            f"cfd_{kind}_flow_submit needs a prepared CFD `case_dir` (or, for internal "
            "flow, pipe params to build the straight-pipe validation case).")
    app = p.get("application") or os.path.basename(info["path"])
    solver_bin = info["path"] if not p.get("application") else (shutil.which(app) or app)
    env_bashrc = solvers.openfoam_bashrc() if is_openfoam else None
    key = jobs.content_key(f"cfd_{kind}_flow",
                           {"case_dir": os.path.abspath(case_dir), "app": app})

    def _work():
        rc, tail = _run_foam(case_dir, [[solver_bin]], env_bashrc)
        return {
            "ok": rc == 0,
            "returncode": rc,
            "solver": info["name"],
            "application": app,
            "case_dir": case_dir,
            "kind": kind,
            "stdout_tail": tail,
        }

    return jobs.submit(f"cfd_{kind}_flow", _work, key=key,
                       meta={"case_dir": case_dir, "kind": kind, "application": app})


def _is_rans(p) -> bool:
    return str(p.get("turbulence", "laminar")).lower() in (
        "komegasst", "k-omega-sst", "rans", "turbulent")


def _cfd_pipe_rans_submit(p):
    """kOmegaSST upgrade of the pipe validation case (SIMULATION_NEXT B3): same
    wedge, wall-function k/omega/nut, developed dp/dx fitted over the second half
    of the pipe and gated BANDED against Colebrook (the Moody correlation is itself
    ±10%). Wall-function discipline: the mesh targets first-cell y+ ~ 30-100
    (reported as y_plus_estimate)."""
    import math

    info = _require_solver("openfoam")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    from driftpin import jobs, solvers
    from driftpin.analysis import cfd as _cfd

    diameter_mm = float(p.get("diameter_mm", 50.0))
    D = diameter_mm / 1000.0
    length_mm = float(p.get("length_mm", 48 * diameter_mm))
    L = length_mm / 1000.0
    mu, rho = _cfd._fluid_props(p.get("fluid", "water-20c"),
                                p.get("mu_pa_s"), p.get("rho_kg_m3"))
    nu = mu / rho
    area = math.pi * D * D / 4.0
    velocity = p.get("velocity_m_s")
    if velocity is None:
        if p.get("flow_rate_lpm") is None:
            raise ValueError("provide velocity_m_s or flow_rate_lpm")
        velocity = (float(p["flow_rate_lpm"]) / 1000.0 / 60.0) / area
    velocity = float(velocity)
    n_axial = int(p.get("n_axial", 100))
    n_radial = int(p.get("n_radial", 24))
    end_time = int(p.get("end_time", 2000))

    oracle = _cfd.pipe_pressure_drop(
        diameter_mm=diameter_mm, length_mm=length_mm, velocity_m_s=velocity,
        mu_pa_s=mu, rho_kg_m3=rho)
    env_bashrc = solvers.openfoam_bashrc()
    key = jobs.content_key("cfd_internal_flow", {"pipe_rans": {
        "D": D, "L": L, "U": velocity, "nu": nu, "rho": rho,
        "na": n_axial, "nr": n_radial, "et": end_time}})

    def _work():
        import tempfile
        from driftpin.analysis import openfoam as _of
        cdir = tempfile.mkdtemp(prefix="foam_pipe_rans_")
        built = _of.write_pipe_rans_case(
            cdir, diameter_m=D, length_m=L, velocity_m_s=velocity, nu_m2_s=nu,
            n_axial=n_axial, n_radial=n_radial, end_time=end_time)
        rc, tail = _run_foam(cdir, [["blockMesh"], ["simpleFoam"]], env_bashrc)
        dpdx_cole = built["friction_factor_colebrook"] / D * 0.5 * rho * velocity ** 2
        out = {
            "ok": rc == 0,
            "returncode": rc,
            "solver": "openfoam",
            "kind": "internal",
            "turbulence": "kOmegaSST",
            "case_dir": cdir,
            "reynolds": round(built["reynolds"], 3),
            "regime": oracle["regime"],
            "y_plus_estimate": built["y_plus_estimate"],
            "dpdx_colebrook_pa_m": round(dpdx_cole, 4),
            "band_pct": 10.0,
            "stdout_tail": tail,
        }
        parsed = _of.parse_pipe_rans_dpdx(
            cdir, n_axial=n_axial, n_radial=n_radial, length_m=L, rho_kg_m3=rho)
        if parsed:
            out["dpdx_pa_m"] = round(parsed["dpdx_pa_m"], 4)
            out["pressure_drop_pa"] = round(parsed["dpdx_pa_m"] * L, 4)
            out["n_cells"] = parsed["n_cells"]
            out["colebrook_ratio"] = round(parsed["dpdx_pa_m"] / dpdx_cole, 4)
        return out

    return jobs.submit("cfd_internal_flow", _work, key=key,
                       meta={"mode": "pipe_rans", "diameter_mm": diameter_mm,
                             "length_mm": length_mm})


def _cfd_pipe_submit(p):
    """Build the axisymmetric straight-pipe case, run blockMesh+simpleFoam (laminar)
    and parse the pressure drop — the kickoff's Hagen–Poiseuille gate, now that
    OpenFOAM is provisioned. Degrades cleanly when OpenFOAM is absent. With
    turbulence='kOmegaSST' the RANS variant runs instead (banded Colebrook gate —
    SIMULATION_NEXT B3). Returns the solved Δp next to the analytic `cfd_pipe_flow`
    reference so the two are directly comparable; the solve runs OFF the MCP
    channel and never touches FreeCAD."""
    import math

    if _is_rans(p):
        return _cfd_pipe_rans_submit(p)
    info = _require_solver("openfoam")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    from driftpin import jobs, solvers
    from driftpin.analysis import cfd as _cfd

    diameter_mm = float(p["diameter_mm"])
    length_mm = float(p["length_mm"])
    D = diameter_mm / 1000.0
    L = length_mm / 1000.0
    mu, rho = _cfd._fluid_props(p.get("fluid", "water-20c"),
                                p.get("mu_pa_s"), p.get("rho_kg_m3"))
    nu = mu / rho
    area = math.pi * D * D / 4.0
    velocity = p.get("velocity_m_s")
    if velocity is None:
        if p.get("flow_rate_lpm") is None:
            raise ValueError("provide velocity_m_s or flow_rate_lpm")
        velocity = (float(p["flow_rate_lpm"]) / 1000.0 / 60.0) / area
    velocity = float(velocity)
    n_axial = int(p.get("n_axial", 120))
    n_radial = int(p.get("n_radial", 15))
    end_time = int(p.get("end_time", 4000))

    hp = _cfd.pipe_pressure_drop(diameter_mm=diameter_mm, length_mm=length_mm,
                                 velocity_m_s=velocity, mu_pa_s=mu, rho_kg_m3=rho)
    env_bashrc = solvers.openfoam_bashrc()
    key = jobs.content_key("cfd_internal_flow", {"pipe": {
        "D": D, "L": L, "U": velocity, "nu": nu, "rho": rho,
        "na": n_axial, "nr": n_radial, "et": end_time}})

    def _work():
        import tempfile
        from driftpin.analysis import openfoam as _of
        cdir = tempfile.mkdtemp(prefix="foam_pipe_")
        built = _of.write_pipe_case(
            cdir, diameter_m=D, length_m=L, velocity_m_s=velocity, nu_m2_s=nu,
            n_axial=n_axial, n_radial=n_radial, end_time=end_time)
        rc, tail = _run_foam(cdir, [["blockMesh"], ["simpleFoam"]], env_bashrc)
        out = {
            "ok": rc == 0,
            "returncode": rc,
            "solver": "openfoam",
            "kind": "internal",
            "case_dir": cdir,
            "reynolds": round(built["reynolds"], 3),
            "regime": hp["regime"],
            "hagen_poiseuille_pa": hp["hagen_poiseuille_pa"],
            "stdout_tail": tail,
        }
        parsed = _of.parse_pressure_drop(cdir, rho_kg_m3=rho)
        if parsed:
            out["pressure_drop_pa"] = round(parsed["dp_developed_pa"], 6)
            out["pressure_drop_inlet_pa"] = round(parsed["dp_inlet_pa"], 6)
            out["n_cells"] = parsed["n_cells"]
            if hp["hagen_poiseuille_pa"] > 0:
                out["hp_ratio"] = round(parsed["dp_developed_pa"]
                                        / hp["hagen_poiseuille_pa"], 4)
        return out

    return jobs.submit("cfd_internal_flow", _work, key=key,
                       meta={"mode": "pipe", "diameter_mm": diameter_mm,
                             "length_mm": length_mm})


def _cfd_body_submit(p):
    """Geometry-driven internal flow — the P3 M4 bridge. Tessellates a FreeCAD
    solid's faces into a multi-region STL on the MAIN thread (`inlet_face` /
    `outlet_face` are 1-based face indices; every other face becomes the no-slip
    `walls` patch), then blockMesh + snappyHexMesh + simpleFoam in the background.
    The inlet velocity points along the inlet face's INWARD normal (probed against
    the solid, so face orientation can't flip it). Degrades cleanly when no CFD
    solver resolves."""
    info = _require_solver("openfoam")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    import tempfile

    from driftpin import jobs, solvers
    from driftpin.analysis import cfd as _cfd
    from driftpin.analysis import meshbridge as _mb

    obj = _shape_handle_to_obj(p["body"])
    faces = obj.Shape.Faces
    inlet_i = p.get("inlet_face")
    outlet_i = p.get("outlet_face")
    if inlet_i is None or outlet_i is None:
        raise ValueError("inlet_face and outlet_face (1-based face indices of "
                         "`body`) are required for the geometry bridge")
    inlet_i, outlet_i = int(inlet_i), int(outlet_i)
    if not (1 <= inlet_i <= len(faces) and 1 <= outlet_i <= len(faces)) \
            or inlet_i == outlet_i:
        raise ValueError(f"inlet/outlet must be distinct face indices in 1..{len(faces)}")
    if p.get("velocity_m_s") is None:
        raise ValueError("velocity_m_s is required")
    velocity = float(p["velocity_m_s"])
    mu, rho = _cfd._fluid_props(p.get("fluid", "water-20c"),
                                p.get("mu_pa_s"), p.get("rho_kg_m3"))
    nu = mu / rho
    stl_tol = float(p.get("stl_tolerance_mm", 0.2))
    end_time = int(p.get("end_time", 3000))

    def face_tris(face):
        pts, tris = face.tessellate(stl_tol)         # mm -> m below
        return [tuple((pts[i].x * 1e-3, pts[i].y * 1e-3, pts[i].z * 1e-3)
                      for i in tri) for tri in tris]

    regions = {
        "inlet": face_tris(faces[inlet_i - 1]),
        "outlet": face_tris(faces[outlet_i - 1]),
        "walls": [t for j, f in enumerate(faces, start=1)
                  if j not in (inlet_i, outlet_i) for t in face_tris(f)],
    }
    stl_text = _mb.ascii_stl_regions(regions)

    # inward inlet direction, orientation-proof: probe a point just off the face
    # centre along the surface normal — if it lies inside the solid the normal
    # already points inward, else flip it.
    fin = faces[inlet_i - 1]
    u0, u1, v0, v1 = fin.ParameterRange
    n = fin.normalAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
    c = fin.CenterOfMass
    eps = max(obj.Shape.BoundBox.DiagonalLength * 1e-4, 1e-3)
    probe = App.Vector(c.x + n.x * eps, c.y + n.y * eps, c.z + n.z * eps)
    sgn = 1.0 if obj.Shape.isInside(probe, 1e-7, True) else -1.0
    vel_vec = (sgn * n.x * velocity, sgn * n.y * velocity, sgn * n.z * velocity)

    bb = obj.Shape.BoundBox
    bbox_min = (bb.XMin * 1e-3, bb.YMin * 1e-3, bb.ZMin * 1e-3)
    bbox_max = (bb.XMax * 1e-3, bb.YMax * 1e-3, bb.ZMax * 1e-3)
    loc = p.get("location_in_mesh_mm")
    base_cell = p.get("base_cell_mm")
    case_dir = tempfile.mkdtemp(prefix="foam_body_")
    _mb.write_snappy_internal_case(
        case_dir, stl_text=stl_text, bbox_min_m=bbox_min, bbox_max_m=bbox_max,
        inlet_velocity_m_s=vel_vec, nu_m2_s=nu,
        location_in_mesh_m=(tuple(float(v) * 1e-3 for v in loc) if loc else None),
        base_cell_m=(float(base_cell) * 1e-3 if base_cell else None),
        end_time=end_time)
    env_bashrc = solvers.openfoam_bashrc()

    # optional Hagen–Poiseuille reference when the caller names the equivalent pipe
    hp = None
    if p.get("diameter_mm") is not None and p.get("length_mm") is not None:
        hp = _cfd.pipe_pressure_drop(
            diameter_mm=float(p["diameter_mm"]), length_mm=float(p["length_mm"]),
            velocity_m_s=velocity, mu_pa_s=mu, rho_kg_m3=rho)

    key = jobs.content_key("cfd_internal_flow", {"body": {
        "volume": round(obj.Shape.Volume, 6), "area": round(obj.Shape.Area, 6),
        "inlet": inlet_i, "outlet": outlet_i, "U": velocity, "nu": nu, "rho": rho,
        "stl_tol": stl_tol, "cell": base_cell or 0, "loc": loc, "et": end_time}})

    def _work():
        from driftpin.analysis import openfoam as _of
        rc, tail = _run_foam(case_dir, _mb.snappy_mesh_cmds(), env_bashrc)
        out = {
            "ok": rc == 0,
            "returncode": rc,
            "solver": "openfoam",
            "kind": "internal",
            "mode": "body",
            "case_dir": case_dir,
            "stdout_tail": tail,
        }
        parsed = _of.parse_pressure_drop(case_dir, rho_kg_m3=rho)
        if parsed:
            out["pressure_drop_pa"] = round(parsed["dp_developed_pa"], 6)
            out["pressure_drop_inlet_pa"] = round(parsed["dp_inlet_pa"], 6)
            out["n_cells"] = parsed["n_cells"]
            if hp and hp["hagen_poiseuille_pa"] > 0:
                out["hagen_poiseuille_pa"] = hp["hagen_poiseuille_pa"]
                out["hp_ratio"] = round(
                    parsed["dp_developed_pa"] / hp["hagen_poiseuille_pa"], 4)
        return out

    return jobs.submit("cfd_internal_flow", _work, key=key,
                       meta={"mode": "body", "inlet_face": inlet_i,
                             "outlet_face": outlet_i})


@handler("cfd_internal_flow_submit")
def _h_cfd_internal_flow_submit(p):
    """Internal-flow CFD (pressure drop) via OpenFOAM or SU2, OFF the MCP channel.
    Degrades to {ok:false, reason, install} when no CFD solver resolves (never raises).

    Three ways to drive it:
      * **Build the straight-pipe validation case** — pass `diameter_mm`, `length_mm`,
        and `velocity_m_s` (or `flow_rate_lpm`), plus a `fluid` name or `mu_pa_s`+
        `rho_kg_m3`. The handler builds the axisymmetric laminar pipe, runs
        blockMesh+simpleFoam, and returns the solved pressure drop next to the
        Hagen–Poiseuille reference — the kickoff's exact CFD gate (`hp_ratio` ~ 1).
      * **Solve a real FreeCAD solid (the P3 M4 geometry bridge)** — pass a `body`
        handle, `inlet_face`/`outlet_face` (1-based face indices; the rest become
        no-slip walls), `velocity_m_s` and the fluid. The solid's faces tessellate
        into a multi-region STL on the main thread; blockMesh + snappyHexMesh +
        simpleFoam run in the background. Use `pressure_drop_pa` (the developed-
        profile 2·mean(p)); pass `diameter_mm`+`length_mm` too for an `hp_ratio`
        reference. Optional: `base_cell_mm`, `location_in_mesh_mm` (for non-convex
        solids), `stl_tolerance_mm`.
      * **Run a prepared OpenFOAM `case_dir`** containing its own mesh + dictionaries.

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result. For
    the pipe/body cases: {ok, returncode, pressure_drop_pa (developed),
    pressure_drop_inlet_pa, hagen_poiseuille_pa?, hp_ratio?, n_cells, case_dir}. For a
    prepared case: {ok, returncode, solver, application, case_dir, kind, stdout_tail}."""
    if p.get("body"):                                # --- geometry bridge (M4) ---
        return _cfd_body_submit(p)
    if p.get("case_dir"):
        return _openfoam_submit(p, "internal")
    if p.get("diameter_mm") is not None:
        return _cfd_pipe_submit(p)
    raise ValueError(
        "provide a `body` handle (geometry bridge), a prepared `case_dir`, or the "
        "straight-pipe params (diameter_mm, length_mm, velocity_m_s or "
        "flow_rate_lpm) to build the Hagen–Poiseuille validation case")


def _cfd_flat_plate_rans_submit(p):
    """kOmegaSST upgrade of the flat-plate validation case (SIMULATION_NEXT B3):
    same 3-block mesh with wall-function grading, gated BANDED against the
    mixed-transition Cf = 0.074·Re^(-1/5) − A/Re (the 1/7-power family is itself
    ±10-15%). The headline drag is the trailing-edge momentum-thickness integral
    (valid for any turbulence treatment); the (nu+nut)-corrected wall-shear sum is
    the cross-check."""
    info = _require_solver("openfoam")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    from driftpin import jobs, solvers
    from driftpin.analysis import cfd as _cfd

    velocity = p.get("velocity_m_s")
    if velocity is None:
        raise ValueError("provide velocity_m_s to build the flat-plate RANS case")
    velocity = float(velocity)
    plate_length_mm = float(p.get("plate_length_mm", 1000.0))
    L = plate_length_mm / 1000.0
    mu, rho = _cfd._fluid_props(p.get("fluid", "air-20c"),
                                p.get("mu_pa_s"), p.get("rho_kg_m3"))
    nu = mu / rho
    thickness_m = 0.002
    nx_plate = int(p.get("nx_plate", 120))
    n_y = int(p.get("n_y", 50))
    grading_y = float(p.get("grading_y", 25.0))
    height_m = float(p.get("height_m", 0.5))
    end_time = int(p.get("end_time", 2000))

    oracle = _cfd.flat_plate_drag_turbulent(
        length_mm=plate_length_mm, velocity_m_s=velocity,
        width_mm=thickness_m * 1000.0, mu_pa_s=mu, rho_kg_m3=rho)
    env_bashrc = solvers.openfoam_bashrc()
    key = jobs.content_key("cfd_external_flow", {"plate_rans": {
        "L": L, "U": velocity, "nu": nu, "rho": rho, "nxp": nx_plate, "ny": n_y,
        "gy": grading_y, "H": height_m, "et": end_time}})

    def _work():
        import tempfile
        from driftpin.analysis import openfoam as _of
        cdir = tempfile.mkdtemp(prefix="foam_plate_rans_")
        built = _of.write_flat_plate_rans_case(
            cdir, velocity_m_s=velocity, nu_m2_s=nu, plate_length_m=L,
            thickness_m=thickness_m, nx_plate=nx_plate, n_y=n_y,
            grading_y=grading_y, height_m=height_m, end_time=end_time)
        rc, tail = _run_foam(cdir, [["blockMesh"], ["simpleFoam"]], env_bashrc)
        out = {
            "ok": rc == 0,
            "returncode": rc,
            "solver": "openfoam",
            "kind": "external",
            "body": "flat_plate",
            "turbulence": "kOmegaSST",
            "case_dir": cdir,
            "reynolds_l": round(built["reynolds_l"], 3),
            "y_plus_estimate": built["y_plus_estimate"],
            "cf_mixed_ref": oracle["cf_mixed"],
            "cf_turbulent_ref": oracle["cf_turbulent"],
            "cf_laminar_blasius": oracle["cf_laminar_blasius"],
            "band_pct": 15.0,
            "stdout_tail": tail,
        }
        parsed = _of.parse_flat_plate_rans_drag(
            cdir, rho_kg_m3=rho, nu_m2_s=nu, velocity_m_s=velocity,
            plate_length_m=L, thickness_m=thickness_m, nx_plate=nx_plate,
            nx_upstream=built["nx_upstream"], n_y=n_y, grading_y=grading_y,
            height_m=height_m)
        if parsed:
            out["drag_momentum_n"] = parsed["drag_momentum_n"]
            out["cf_solved"] = round(parsed["cf_momentum"], 6)
            out["cf_wall_corrected"] = (round(parsed["cf_wall_corrected"], 6)
                                        if parsed["cf_wall_corrected"] else None)
            out["n_cells"] = parsed["n_cells"]
            if oracle["cf_mixed"] > 0:
                out["cf_mixed_ratio"] = round(
                    parsed["cf_momentum"] / oracle["cf_mixed"], 4)
        return out

    return jobs.submit("cfd_external_flow", _work, key=key,
                       meta={"mode": "flat_plate_rans",
                             "plate_length_mm": plate_length_mm,
                             "velocity_m_s": velocity})


def _cfd_flat_plate_submit(p):
    """Build the 2-D laminar flat-plate case, run blockMesh+simpleFoam and integrate
    the wall-shear drag — the kickoff's external-flow Blasius gate. Degrades cleanly
    when OpenFOAM is absent. With turbulence='kOmegaSST' the RANS variant runs
    instead (banded mixed-transition Cf gate — SIMULATION_NEXT B3). Returns the
    solved drag/Cd next to the analytic `flat_plate_drag` (Blasius Cf=1.328/√Re_L)
    reference; the solve runs OFF the MCP channel and never touches FreeCAD. Drag is
    read straight from the converged U field (OpenFOAM force function objects abort
    with a 'sha1' IOstream error in this build)."""
    if _is_rans(p):
        return _cfd_flat_plate_rans_submit(p)
    info = _require_solver("openfoam")
    if not info["ok"]:                               # graceful degradation (verified)
        return info
    from driftpin import jobs, solvers
    from driftpin.analysis import cfd as _cfd

    velocity = p.get("velocity_m_s")
    if velocity is None:
        raise ValueError("provide velocity_m_s to build the flat-plate validation case")
    velocity = float(velocity)
    plate_length_mm = float(p.get("plate_length_mm", 100.0))
    L = plate_length_mm / 1000.0
    mu, rho = _cfd._fluid_props(p.get("fluid", "air-20c"),
                                p.get("mu_pa_s"), p.get("rho_kg_m3"))
    nu = mu / rho
    thickness_m = 0.002
    nx_plate = int(p.get("nx_plate", 160))
    n_y = int(p.get("n_y", 140))
    grading_y = float(p.get("grading_y", 3000.0))
    height_m = float(p.get("height_m", 1.5))
    end_time = int(p.get("end_time", 3000))

    bl = _cfd.flat_plate_drag(length_mm=plate_length_mm, velocity_m_s=velocity,
                              width_mm=thickness_m * 1000.0, mu_pa_s=mu, rho_kg_m3=rho)
    env_bashrc = solvers.openfoam_bashrc()
    key = jobs.content_key("cfd_external_flow", {"plate": {
        "L": L, "U": velocity, "nu": nu, "rho": rho, "nxp": nx_plate, "ny": n_y,
        "gy": grading_y, "H": height_m, "et": end_time}})

    def _work():
        import tempfile
        from driftpin.analysis import openfoam as _of
        cdir = tempfile.mkdtemp(prefix="foam_plate_")
        built = _of.write_flat_plate_case(
            cdir, velocity_m_s=velocity, nu_m2_s=nu, plate_length_m=L,
            thickness_m=thickness_m, nx_plate=nx_plate, n_y=n_y, grading_y=grading_y,
            height_m=height_m, end_time=end_time)
        rc, tail = _run_foam(cdir, [["blockMesh"], ["simpleFoam"]], env_bashrc)
        out = {
            "ok": rc == 0,
            "returncode": rc,
            "solver": "openfoam",
            "kind": "external",
            "body": "flat_plate",
            "case_dir": cdir,
            "reynolds_l": round(built["reynolds_l"], 3),
            "laminar": bl["laminar"],
            "cf_blasius": bl["cf_avg"],
            "drag_blasius_n": round(bl["drag_force_n"], 9),
            "stdout_tail": tail,
        }
        parsed = _of.parse_flat_plate_drag(
            cdir, rho_kg_m3=rho, nu_m2_s=nu, velocity_m_s=velocity, plate_length_m=L,
            thickness_m=thickness_m, nx_plate=nx_plate, nx_upstream=built["nx_upstream"],
            n_y=n_y, grading_y=grading_y, height_m=height_m)
        if parsed:
            out["drag_force_n"] = round(parsed["drag_force_n"], 9)
            out["drag_momentum_n"] = round(parsed["drag_momentum_n"], 9)
            out["cd"] = round(parsed["cd"], 6)
            out["cf_solved"] = round(parsed["cf_solved"], 6)
            out["n_cells"] = parsed["n_cells"]
            if bl["cf_avg"] > 0:
                out["blasius_ratio"] = round(parsed["cf_solved"] / bl["cf_avg"], 4)
        return out

    return jobs.submit("cfd_external_flow", _work, key=key,
                       meta={"mode": "flat_plate", "plate_length_mm": plate_length_mm,
                             "velocity_m_s": velocity})


@handler("cfd_external_flow_submit")
def _h_cfd_external_flow_submit(p):
    """External-flow CFD (drag) via OpenFOAM or SU2, OFF the MCP channel. Degrades to
    {ok:false, reason, install} when no CFD solver resolves (never raises).

    Two ways to drive it:
      * **Build the flat-plate validation case** — pass `velocity_m_s` (and optionally
        `plate_length_mm`, a `fluid` name or `mu_pa_s`+`rho_kg_m3`, mesh knobs). The
        handler builds a 2-D laminar flat plate (clean leading edge: slip→plate→slip),
        runs blockMesh+simpleFoam, integrates the wall-shear drag from the converged U
        field, and returns the solved Cd next to the Blasius reference Cf=1.328/√Re_L —
        the kickoff's external gate (`blasius_ratio` ~ 1, within ~15%).
      * **Run a prepared OpenFOAM `case_dir`** containing its own mesh + dictionaries.

    Returns the degradation dict, or {job_id, status, cache_hit}; poll job_result. For
    the flat-plate case: {ok, returncode, reynolds_l, cd, cf_solved, cf_blasius,
    blasius_ratio, drag_force_n, drag_momentum_n, drag_blasius_n, n_cells, case_dir}.
    For a prepared case: {ok, returncode, solver, application, case_dir, kind,
    stdout_tail}."""
    if p.get("case_dir"):
        return _openfoam_submit(p, "external")
    if p.get("velocity_m_s") is not None:
        return _cfd_flat_plate_submit(p)
    raise ValueError(
        "provide a prepared `case_dir`, or the flat-plate params (velocity_m_s, and "
        "optionally plate_length_mm/fluid) to build the Blasius validation case")


# --- generic async jobs (driftpin.jobs) ---------------------------------------
# A reusable submit/poll facility for long solves that must not block the MCP
# channel. async_demo_submit is the reference implementation; job_status /
# job_result / job_list are the shared poll surface every future *_submit reuses.
# The submitted callable runs on a background thread, so it must not touch FreeCAD
# (see driftpin/jobs.py) — for an FEM/CFD solve, background only the solver
# subprocess the way render_photoreal_submit does, then read results on the main
# thread once the job is done.

@handler("async_demo_submit")
def _h_async_demo_submit(p):
    """Reference long-solve: a FreeCAD-free background job that 'computes' for
    duration_s then returns a deterministic result. Returns {job_id, status,
    cache_hit}; identical (duration_s, value) is a content-hash cache hit."""
    import time as _time
    from driftpin import jobs
    duration = float(p.get("duration_s", 0.5))
    value = float(p.get("value", 1.0))
    key = jobs.content_key("async_demo", {"duration_s": duration, "value": value})

    def _work():
        _time.sleep(duration)
        return {"value": value, "squared": value * value, "duration_s": duration}

    return jobs.submit("async_demo", _work, key=key, meta={"duration_s": duration})


@handler("job_status")
def _h_job_status(p):
    from driftpin import jobs
    return jobs.status(p["job_id"])


@handler("job_result")
def _h_job_result(p):
    from driftpin import jobs
    return jobs.result(p["job_id"], discard=bool(p.get("discard", False)))


@handler("job_list")
def _h_job_list(p):
    from driftpin import jobs
    return jobs.list_jobs()


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

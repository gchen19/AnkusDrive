"""
Feature templates — declared-input feature recipes (PowerCopy / UDF / iFeature
analog, issue #139, RFC docs/DESIGN_HIERARCHY.md §2 Theme B / B2).

A *part recipe* (issue #136, :mod:`driftpin.recipes`) is a named build function
from scalar parameters to a whole part. A **feature template** is its sub-part
analog: a reusable feature with declared **reference-geometry inputs** — a
placement frame, a face, an axis — *plus* scalar parameters, stamped repeatedly
onto an existing host body in new contexts. It is DriftPin's PowerCopy/UDF: the
"mounting boss" / "bolt pattern" you author once and instantiate onto reference
geometry by name, no UI picking.

What makes it scriptable without a UI is the codebase's existing stable-reference
system: published interface FRAMES (``publish_interface``) and content-addressed
``f_`` / ``e_`` face/edge TAGS (``resolve_face`` / ``resolve_edge`` /
``query_faces``). An agent supplies a reference input *by name* — an interface
name, an ``f_`` tag, a literal frame — and the template resolves it against the
host's *current* geometry. A tag that does not resolve fails LOUDLY at the door,
exactly like a missing required parameter (mirrors ``recipes`` and
``worker._validate_manifest``).

------------------------------------------------------------------------------
Two declaration kinds make up a template's input surface:

  1. REFERENCE inputs (:class:`RefSpec`) — the geometry the feature attaches to.
     ``kind`` is one of:
       frame  a placement frame {origin, z_axis?, x_axis?}. The agent supplies
              EITHER a published-interface NAME on the host, an ``f_``/``e_`` tag
              (the face/edge is reduced to a frame at its centroid + normal/axis),
              OR a literal frame dict.
       face   an ``f_`` face tag (resolved via resolve_face — loud if absent).
       edge   an ``e_`` edge tag (resolved via resolve_edge — loud if absent).
       axis   a direction: a 3-vector, or an ``f_``/``e_`` tag whose normal/axis
              is taken.
     Resolution touches geometry, so it runs through the injected ``call``
     dispatch (the worker's HANDLERS) — this module never imports the worker.

  2. SCALAR inputs (reuse :class:`driftpin.recipes.InputSpec`) — the published
     parameters: typed, unit-bearing (``length`` rides the #102 units layer),
     range-checked, defaulted. Validated exactly the way a recipe validates its
     driving parameters, so ``"0.25 in"`` is accepted and ``"5 N"`` for a length
     is refused at the door.

------------------------------------------------------------------------------
The frozen contract (what a consumer pins to):

  * INSTANTIATION reference form — the call an agent makes:

        { "template": "mounting_boss",
          "host": "<handle>",
          "refs": { "seat": "top_seat_a" },          # name | f_ tag | frame dict
          "inputs": { "boss_dia_mm": 12.0, "height_mm": 8.0 } }

  * Per-template SCHEMA form (:func:`feature_schema`):

        { "schema": "driftpin.feature_template/1",
          "template": "mounting_boss", "doc": "...",
          "refs":  [ {"name": "seat", "kind": "frame", "required": true,
                      "doc": "..."}, ... ],
          "inputs":[ {"name": "boss_dia_mm", "type": "length", "unit": "mm",
                      "default": 12.0, "min": ..., "required": true, ...}, ... ],
          "emits": {...} }

Validation fails LOUDLY at the door: an unknown template, an unknown/missing
required reference, a malformed reference value, an unresolvable tag, and every
scalar-input failure recipes already catches (missing required, out of range,
wrong type, bad unit, unknown key).
"""
from __future__ import annotations

from driftpin import recipes
from driftpin.recipes import InputSpec  # re-export: templates declare scalars with it

__all__ = [
    "FeatureTemplateError",
    "RefSpec",
    "InputSpec",
    "FeatureTemplate",
    "register",
    "get",
    "names",
    "feature_schema",
    "list_templates",
    "validate_inputs",
    "validate_refs",
    "validate_instantiation",
    "resolve_refs",
    "instantiate",
    "FEATURES",
    "SCHEMA",
]

# Contract version stamp — bumped if the template-ref / schema SHAPE changes
# (parallel to recipes.SCHEMA / worker._MANIFEST_SCHEMA).
SCHEMA = "driftpin.feature_template/1"

# Reference-geometry kinds a declared reference input may take.
_REF_KINDS = {"frame", "face", "edge", "axis"}


class FeatureTemplateError(ValueError):
    """Raised when a template is unknown, or its references / inputs fail declared
    validation — the loud door failure (mirrors a recipe / manifest problem)."""


# =============================================================================
# Declarations
# =============================================================================

class RefSpec:
    """One declared reference-geometry input of a feature template.

    name      identifier used as the reference key.
    kind      one of frame | face | edge | axis (see module docstring).
    required  if True and omitted, validation fails loudly.
    doc       human/agent-facing description.
    """

    __slots__ = ("name", "kind", "required", "doc")

    def __init__(self, name, kind, required=True, doc=""):
        if kind not in _REF_KINDS:
            raise FeatureTemplateError(
                f"reference {name!r}: unknown kind {kind!r} (expected one of "
                f"{sorted(_REF_KINDS)})")
        self.name = name
        self.kind = kind
        self.required = required
        self.doc = doc

    def as_dict(self):
        d = {"name": self.name, "kind": self.kind, "required": self.required}
        if self.doc:
            d["doc"] = self.doc
        return d

    # --- structural validation (pure — no geometry) --------------------------

    def check_shape(self, value):
        """Structural (no-geometry) check of one supplied reference value. Returns
        a problem string, or None when the value is shaped correctly for the kind.
        The actual geometry resolution (and its loud failure) happens later in
        :func:`resolve_refs`."""
        if self.kind == "frame":
            if isinstance(value, str):
                if not value:
                    return f"reference {self.name!r}: empty frame name"
                return None
            if isinstance(value, dict):
                if "origin" not in value:
                    return (f"reference {self.name!r}: literal frame must have an "
                            f"'origin'")
                if not _is_vec3(value.get("origin")):
                    return (f"reference {self.name!r}: frame 'origin' must be "
                            f"[x, y, z]")
                return None
            return (f"reference {self.name!r}: frame must be an interface name, an "
                    f"f_/e_ tag, or a {{origin, z_axis?, x_axis?}} dict")
        if self.kind in ("face", "edge"):
            pfx = "f_" if self.kind == "face" else "e_"
            if not isinstance(value, str) or not value.startswith(pfx):
                return (f"reference {self.name!r}: {self.kind} must be an "
                        f"{pfx!r}-prefixed tag (got {value!r})")
            return None
        # axis
        if isinstance(value, str):
            if value.startswith("f_") or value.startswith("e_"):
                return None
            return (f"reference {self.name!r}: axis tag must be an f_/e_ tag "
                    f"(got {value!r})")
        if _is_vec3(value):
            return None
        return f"reference {self.name!r}: axis must be a 3-vector or an f_/e_ tag"


class FeatureTemplate:
    """A named, declared-input feature recipe: reference-geometry inputs + scalar
    inputs + a deterministic ``build_fn(host, refs, inputs, call)`` that stamps the
    feature onto the host and returns its result dict."""

    __slots__ = ("name", "refs", "inputs", "build_fn", "doc", "emits")

    def __init__(self, name, refs, inputs, build_fn, doc="", emits=None):
        self.name = name
        self.refs = list(refs)
        self.inputs = list(inputs)
        seen = set()
        for spec in self.refs + self.inputs:
            if spec.name in seen:
                raise FeatureTemplateError(
                    f"template {name!r}: duplicate input/reference {spec.name!r}")
            seen.add(spec.name)
        self.build_fn = build_fn
        self.doc = doc
        self.emits = emits or {}

    @property
    def refs_by_name(self):
        return {s.name: s for s in self.refs}

    @property
    def inputs_by_name(self):
        return {s.name: s for s in self.inputs}

    def schema(self):
        return {
            "schema": SCHEMA,
            "template": self.name,
            "doc": self.doc,
            "refs": [s.as_dict() for s in self.refs],
            "inputs": [s.as_dict() for s in self.inputs],
            "emits": self.emits,
        }


# =============================================================================
# Registry
# =============================================================================

FEATURES: dict[str, FeatureTemplate] = {}


def register(template: FeatureTemplate) -> FeatureTemplate:
    """Register (or replace) a feature template by name. Returns it."""
    FEATURES[template.name] = template
    return template


def get(name: str) -> FeatureTemplate:
    """The registered template, or a loud error naming the known set."""
    try:
        return FEATURES[name]
    except KeyError:
        raise FeatureTemplateError(
            f"unknown feature template {name!r} (registered: {sorted(FEATURES)})"
        ) from None


def names():
    """Sorted list of registered template names."""
    return sorted(FEATURES)


def feature_schema(name: str) -> dict:
    """The declared ref+input schema contract for one template."""
    return get(name).schema()


def list_templates() -> dict:
    """A directory of every registered template: name -> {doc, refs, required,
    optional, emits}. The cheap browse before picking a template."""
    out = {}
    for n, t in sorted(FEATURES.items()):
        out[n] = {
            "doc": t.doc,
            "refs": [{"name": s.name, "kind": s.kind, "required": s.required}
                     for s in t.refs],
            "required": [s.name for s in t.inputs if s.required],
            "optional": [s.name for s in t.inputs if not s.required],
            "emits": t.emits,
        }
    return {"schema": SCHEMA, "count": len(out), "templates": out}


# =============================================================================
# Validation (the door)
# =============================================================================

def validate_inputs(name: str, inputs: dict | None) -> dict:
    """Validate scalar ``inputs`` against template ``name``'s declared scalar
    schema and return the RESOLVED dict (canonical units, defaults filled). Same
    fail-at-the-door discipline as :func:`recipes.validate_inputs`."""
    template = get(name)
    inputs = dict(inputs or {})
    by_name = template.inputs_by_name

    extra = sorted(set(inputs) - set(by_name))
    if extra:
        raise FeatureTemplateError(
            f"template {name!r}: unknown input(s) {extra} "
            f"(declared: {sorted(by_name)})")

    resolved = {}
    for spec in template.inputs:
        if spec.name in inputs:
            resolved[spec.name] = spec.coerce(inputs[spec.name])
        elif spec.default is not None:
            resolved[spec.name] = spec.coerce(spec.default)
        elif spec.required:
            raise FeatureTemplateError(
                f"template {name!r}: missing required input {spec.name!r}")
    return resolved


def validate_refs(name: str, refs: dict | None) -> list:
    """Structural (no-geometry) validation of the reference inputs against the
    template's declared references. Returns a list of problems (empty == shaped
    correctly). Catches a non-object refs map, unknown/extra reference keys, a
    missing required reference, and a malformed reference value. The actual
    geometry resolution (an f_ tag that doesn't resolve) is the SECOND door,
    :func:`resolve_refs`."""
    template = get(name)
    problems = []
    if refs is None:
        refs = {}
    if not isinstance(refs, dict):
        return ["'refs' must be an object"]
    by_name = template.refs_by_name

    extra = sorted(set(refs) - set(by_name))
    if extra:
        problems.append(
            f"template {name!r}: unknown reference(s) {extra} "
            f"(declared: {sorted(by_name)})")

    for spec in template.refs:
        if spec.name not in refs:
            if spec.required:
                problems.append(
                    f"template {name!r}: missing required reference {spec.name!r}")
            continue
        problem = spec.check_shape(refs[spec.name])
        if problem:
            problems.append(problem)
    return problems


def validate_instantiation(spec) -> list:
    """Validate an instantiation reference ``{template, refs, inputs}`` WITHOUT
    building it — the cheap front door (mirrors ``recipes.validate_ref`` /
    ``validate_manifest``). Returns a list of human-readable problems (empty ==
    valid). Pure: structural ref-shape + full scalar-input validation; geometry
    resolution is left to :func:`instantiate`."""
    problems = []
    if not isinstance(spec, dict):
        return ["feature instantiation must be an object"]
    name = spec.get("template")
    if not name or not isinstance(name, str):
        return ["instantiation must name a 'template' (string)"]
    if name not in FEATURES:
        return [f"unknown feature template {name!r} "
                f"(registered: {sorted(FEATURES)})"]
    problems.extend(validate_refs(name, spec.get("refs")))
    inputs = spec.get("inputs", {})
    if not isinstance(inputs, dict):
        problems.append(f"template {name!r}: 'inputs' must be an object")
    else:
        try:
            validate_inputs(name, inputs)
        except (FeatureTemplateError, recipes.RecipeError) as e:
            problems.append(str(e))
    return problems


# =============================================================================
# Reference resolution (the geometry door — touches FreeCAD via `call`)
# =============================================================================

def _is_vec3(v):
    return (isinstance(v, (list, tuple)) and len(v) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                    for c in v))


def _canonical_frame(value):
    """A literal frame dict -> {origin, z_axis, x_axis} with defaults filled."""
    frame = {"origin": [float(c) for c in value["origin"]]}
    frame["z_axis"] = [float(c) for c in value.get("z_axis", [0.0, 0.0, 1.0])]
    frame["x_axis"] = [float(c) for c in value.get("x_axis", [1.0, 0.0, 0.0])]
    for k, v in value.items():
        if k not in frame:
            frame[k] = v
    return frame


def _face_descriptor_by_tag(host, tag, call):
    """Resolve an f_ tag to its face descriptor (centroid + normal/axis) via
    query_faces. resolve_face is the loud canonical resolver; query_faces carries
    the geometry we reduce to a frame. Raises FeatureTemplateError if absent."""
    # resolve_face raises a loud KeyError/RuntimeError on a missing/ambiguous tag.
    try:
        call("resolve_face", handle=host, tag=tag)
    except Exception as e:
        raise FeatureTemplateError(
            f"face tag {tag!r} does not resolve on host {host!r}: {e}") from None
    for d in call("query_faces", handle=host, predicate={}):
        if d.get("tag") == tag:
            return d
    raise FeatureTemplateError(
        f"face tag {tag!r} does not resolve on host {host!r}")


def _edge_descriptor_by_tag(host, tag, call):
    try:
        call("resolve_edge", handle=host, tag=tag)
    except Exception as e:
        raise FeatureTemplateError(
            f"edge tag {tag!r} does not resolve on host {host!r}: {e}") from None
    for d in call("list_edges", handle=host):
        if d.get("tag") == tag:
            return d
    raise FeatureTemplateError(
        f"edge tag {tag!r} does not resolve on host {host!r}")


def _frame_from_face(desc):
    origin = list(desc["centroid"])
    z = desc.get("normal") or desc.get("axis") or [0.0, 0.0, 1.0]
    return {"origin": origin, "z_axis": list(z), "x_axis": [1.0, 0.0, 0.0]}


def _frame_from_edge(desc):
    origin = list(desc["centroid"])
    z = desc.get("axis") or [0.0, 0.0, 1.0]
    return {"origin": origin, "z_axis": list(z), "x_axis": [1.0, 0.0, 0.0]}


def _resolve_one_ref(spec, host, value, call):
    """Resolve one supplied reference value against the host's current geometry.
    Returns a canonical reference (frame dict / face dict / edge dict / axis vec).
    Raises FeatureTemplateError loudly on an unresolvable tag or interface name."""
    if spec.kind == "frame":
        if isinstance(value, dict):
            return _canonical_frame(value)
        if isinstance(value, str) and value.startswith("f_"):
            return _frame_from_face(_face_descriptor_by_tag(host, value, call))
        if isinstance(value, str) and value.startswith("e_"):
            return _frame_from_edge(_edge_descriptor_by_tag(host, value, call))
        # a plain string -> a published interface name on the host
        try:
            res = call("get_interface", handle=host, name=value)
        except Exception as e:
            raise FeatureTemplateError(
                f"reference {spec.name!r}: interface {value!r} not published on "
                f"host {host!r}: {e}") from None
        return _canonical_frame(res["frame"])
    if spec.kind == "face":
        desc = _face_descriptor_by_tag(host, value, call)
        out = {"tag": value, "frame": _frame_from_face(desc)}
        out["face"] = desc
        return out
    if spec.kind == "edge":
        desc = _edge_descriptor_by_tag(host, value, call)
        return {"tag": value, "frame": _frame_from_edge(desc), "edge": desc}
    # axis
    if isinstance(value, str) and value.startswith("f_"):
        d = _face_descriptor_by_tag(host, value, call)
        return d.get("normal") or d.get("axis") or [0.0, 0.0, 1.0]
    if isinstance(value, str) and value.startswith("e_"):
        d = _edge_descriptor_by_tag(host, value, call)
        return d.get("axis") or [0.0, 0.0, 1.0]
    return [float(c) for c in value]


def resolve_refs(name: str, host, refs: dict | None, call) -> dict:
    """Resolve every declared reference of template ``name`` against ``host``'s
    current geometry, returning {ref_name -> canonical reference}. Runs structural
    validation first (loud), then the geometry door (loud on an unresolvable tag /
    interface name). ``call(tool, **params)`` is the worker handler dispatch."""
    problems = validate_refs(name, refs)
    if problems:
        raise FeatureTemplateError(f"invalid references: {problems}")
    template = get(name)
    refs = dict(refs or {})
    resolved = {}
    for spec in template.refs:
        if spec.name in refs:
            resolved[spec.name] = _resolve_one_ref(spec, host, refs[spec.name], call)
    return resolved


# =============================================================================
# Instantiation (the only FreeCAD-touching build path)
# =============================================================================

def instantiate(name: str, host, refs: dict | None, inputs: dict | None,
                call) -> dict:
    """Stamp feature template ``name`` onto ``host`` at the resolved reference
    geometry with ``inputs``. Validates scalars + references at the door (loud),
    resolves references against the host's CURRENT geometry (loud on a tag that
    doesn't resolve), then runs the deterministic build. ``call(tool, **params)``
    is the worker handler dispatch (injected so this module never imports the
    worker). Returns {template, schema, host, refs, inputs, handle, name,
    interfaces, intent} — deterministic for fixed host + inputs."""
    resolved_inputs = validate_inputs(name, inputs)
    resolved_refs = resolve_refs(name, host, refs, call)
    template = get(name)
    result = template.build_fn(host, resolved_refs, resolved_inputs, call)
    result.setdefault("template", name)
    result.setdefault("schema", SCHEMA)
    result["host"] = host
    result["refs"] = resolved_refs
    result["inputs"] = resolved_inputs
    return result


# =============================================================================
# Build helpers (geometry authored here; run through the run_script escape hatch)
# =============================================================================

def _v(seq):
    """Format a 3-seq as a literal App.Vector(...) call for an embedded script."""
    return "App.Vector(%.9g, %.9g, %.9g)" % (float(seq[0]), float(seq[1]),
                                             float(seq[2]))


def _stamp(host, code_body, label, call):
    """Run a build script that resolves the host, mutates `result` (a Part.Shape)
    from `host.Shape`, and leaves a single new `Part::Feature` named via
    __result__. Returns the registered handle for that feature."""
    code = (
        "import Part, math\n"
        "import FreeCAD as App\n"
        "doc = App.ActiveDocument\n"
        f"host = _resolve({host!r})\n"
        "result = host.Shape\n"
        f"{code_body}"
        f"feat = doc.addObject('Part::Feature', {label!r})\n"
        "feat.Shape = result.removeSplitter()\n"
        "try:\n"
        "    host.Visibility = False\n"
        "except Exception:\n"
        "    pass\n"
        "doc.recompute()\n"
        "__result__ = feat.Name\n"
    )
    r = call("run_script", code=code)
    target = r.get("result")
    for entry in r.get("registered", []):
        if entry.get("name") == target:
            return entry["handle"]
    reg = r.get("registered") or []
    if not reg:
        raise FeatureTemplateError(
            f"feature build produced no shaped object (label {label!r})")
    return reg[0]["handle"]


# =============================================================================
# Reference feature templates
# =============================================================================

def _build_mounting_boss(host, refs, p, call):
    """A cylindrical mounting boss with a co-axial pilot bore, stamped on a seat
    frame and fused to the host. The canonical PowerCopy reference (issue #139):
    a placement frame + a scalar diameter, instantiated onto new references."""
    frame = refs["seat"]
    origin, z = frame["origin"], frame["z_axis"]
    boss_r = p["boss_dia_mm"] / 2.0
    h = p["height_mm"]
    body = (
        f"base = {_v(origin)}\n"
        f"zdir = {_v(z)}\n"
        "zdir.normalize()\n"
        f"boss = Part.makeCylinder({boss_r:.9g}, {h:.9g}, base, zdir)\n"
        "result = result.fuse(boss)\n"
    )
    bore_d = p["bore_dia_mm"]
    if bore_d > 0.0:
        bore_r = bore_d / 2.0
        eps = 1.0
        body += (
            f"bore = Part.makeCylinder({bore_r:.9g}, {h + 2 * eps:.9g}, "
            f"base - zdir * {eps:.9g}, zdir)\n"
            "result = result.cut(bore)\n"
        )
    handle = _stamp(host, body, "MountingBoss", call)
    # The boss top is a fresh seat for whatever mounts ON the boss.
    top = [origin[i] + _unit(z)[i] * h for i in range(3)]
    iface = call("publish_interface", handle=handle, name="boss_top", frame={
        "origin": top, "z_axis": list(z), "x_axis": frame.get("x_axis", [1, 0, 0]),
        "boss_dia_mm": p["boss_dia_mm"], "bore_dia_mm": bore_d,
    })
    intent = call("declare_intent", handle=handle, contract={"watertight": True})
    return {"handle": handle, "name": "MountingBoss",
            "interfaces": iface["interfaces"], "intent": intent["contract"]}


def _build_bolt_pattern(host, refs, p, call):
    """A circular bolt pattern: `count` blind holes on a bolt circle in the frame
    plane, drilled into the host along -z. Inputs {frame, circle_dia, count,
    hole_dia, depth, thread} — thread is carried as metadata (machined, not
    modeled). The second reference template (issue #139)."""
    frame = refs["frame"]
    origin, z = frame["origin"], frame["z_axis"]
    x = frame.get("x_axis", [1.0, 0.0, 0.0])
    circle_r = p["circle_dia_mm"] / 2.0
    hole_r = p["hole_dia_mm"] / 2.0
    count = p["count"]
    depth = p["depth_mm"]
    eps = 1.0
    body = (
        f"base = {_v(origin)}\n"
        f"zdir = {_v(z)}\n"
        "zdir.normalize()\n"
        f"xdir = {_v(x)}\n"
        "ydir = zdir.cross(xdir)\n"
        "ydir.normalize()\n"
        "xdir = ydir.cross(zdir)\n"
        "xdir.normalize()\n"
        "ndir = App.Vector(-zdir.x, -zdir.y, -zdir.z)\n"
        f"for i in range({count}):\n"
        f"    ang = 2.0 * math.pi * i / {count}\n"
        f"    c = base + xdir * ({circle_r:.9g} * math.cos(ang)) "
        f"+ ydir * ({circle_r:.9g} * math.sin(ang))\n"
        f"    hole = Part.makeCylinder({hole_r:.9g}, {depth + eps:.9g}, "
        f"c + zdir * {eps:.9g}, ndir)\n"
        "    result = result.cut(hole)\n"
    )
    handle = _stamp(host, body, "BoltPattern", call)
    iface = call("publish_interface", handle=handle, name="bolt_circle", frame={
        "origin": list(origin), "z_axis": list(z), "x_axis": list(x),
        "circle_dia_mm": p["circle_dia_mm"], "count": count,
        "hole_dia_mm": p["hole_dia_mm"], "thread": p["thread"],
    })
    intent = call("declare_intent", handle=handle, contract={"watertight": True})
    return {"handle": handle, "name": "BoltPattern",
            "interfaces": iface["interfaces"], "intent": intent["contract"]}


def _unit(v):
    import math
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / n for c in v]


register(FeatureTemplate(
    name="mounting_boss",
    doc="A cylindrical mounting boss with a co-axial pilot bore, stamped onto a "
        "seat frame and fused to the host (PowerCopy/UDF reference, issue #139). "
        "Supply a placement frame by name (interface / f_ face tag / literal) plus "
        "a boss diameter; publishes a 'boss_top' seat for what mounts on it.",
    refs=[
        RefSpec("seat", "frame", required=True,
                doc="Placement frame the boss sits on: a published interface name, "
                    "an f_/e_ tag, or a literal {origin, z_axis?, x_axis?}."),
    ],
    inputs=[
        InputSpec("boss_dia_mm", "length", unit="mm", default=12.0,
                  min=1.0, max=500.0, required=True,
                  doc="Outside diameter of the boss."),
        InputSpec("height_mm", "length", unit="mm", default=8.0,
                  min=0.5, max=500.0, required=False,
                  doc="Boss height (extrusion along the seat +z)."),
        InputSpec("bore_dia_mm", "length", unit="mm", default=5.0,
                  min=0.0, max=490.0, required=False,
                  doc="Co-axial pilot bore diameter (0 => solid boss)."),
    ],
    build_fn=_build_mounting_boss,
    emits={"interfaces": ["boss_top"], "intent": ["watertight"]},
))


register(FeatureTemplate(
    name="bolt_pattern",
    doc="A circular bolt pattern: `count` blind holes on a bolt circle in a "
        "reference frame, drilled into the host (issue #139). Supply the frame by "
        "name plus the circle diameter, count, and thread; thread rides as "
        "metadata (machined, not modeled). Publishes a 'bolt_circle' interface.",
    refs=[
        RefSpec("frame", "frame", required=True,
                doc="Bolt-circle frame: a published interface name, an f_/e_ tag, "
                    "or a literal {origin, z_axis?, x_axis?}."),
    ],
    inputs=[
        InputSpec("circle_dia_mm", "length", unit="mm", default=40.0,
                  min=1.0, max=2000.0, required=True,
                  doc="Bolt-circle diameter (hole centers lie on it)."),
        InputSpec("count", "int", default=4, min=1, max=64, required=True,
                  doc="Number of equally-spaced holes."),
        InputSpec("hole_dia_mm", "length", unit="mm", default=5.5,
                  min=0.2, max=200.0, required=False,
                  doc="Through/blind hole diameter."),
        InputSpec("depth_mm", "length", unit="mm", default=8.0,
                  min=0.5, max=1000.0, required=False,
                  doc="Drill depth into the host along -z."),
        InputSpec("thread", "string", default="none",
                  choices=("none", "M3", "M4", "M5", "M6", "M8", "M10"),
                  required=False,
                  doc="Tapped thread spec carried as metadata (not modeled)."),
    ],
    build_fn=_build_bolt_pattern,
    emits={"interfaces": ["bolt_circle"], "intent": ["watertight"]},
))

"""
Part recipes — named, parameterized, declared-input build templates (issue #136).

THE KEYSTONE (RFC docs/DESIGN_HIERARCHY.md §1, §2.1). DriftPin deliberately does
NOT store an editable in-file feature tree, and it rejected live cross-file
FreeCAD expression links as fragile-headless (MULTI_AGENT.md §11.1, §13). So the
parametric model has to live somewhere else, and it is already latent in the
codebase: ``example/gearbox_manifest.py``'s ``build_manifest(params)`` is a pure
function from parameters to geometry, deterministic by construction (#123/#127
guarantee same-inputs -> same-bytes).

A **recipe** formalizes exactly that: a named build function with a declared
**input schema** (its driving parameters — type, unit, default, range) that
deterministically emits a part (geometry + ``publish_interface`` frames +
``declare_intent``). It is DriftPin's intra-part parametric model AND its
PowerCopy/UDF in one: *"regenerate with new parameters" = "re-run the recipe."*

This module is PURE PYTHON for everything except the actual geometry build:
schema declaration, input validation (typed via :mod:`driftpin.units`), and the
recipe-reference / manifest-lowering logic all import cleanly WITHOUT FreeCAD, so
the door-validation tests run free and fast. Only :func:`build` touches geometry,
and it does so through an injected ``call(tool, **params)`` callable (the worker's
``HANDLERS`` dispatch) rather than importing the worker — no circular import, and
the same function drives a live worker or a stub.

------------------------------------------------------------------------------
The frozen contract (what Wave-1 work — #138 design tables, #139 feature
templates, #146 registry — builds against):

  1. Recipe-reference COMPONENT form, a fourth manifest component source kind
     alongside ``file`` / ``manifest`` / ``library``:

         { "recipe": "spur_gear",
           "inputs": { "module_mm": 2.0, "teeth": 24, "width_mm": 6.0 } }

     ``recipe`` names a registered recipe; ``inputs`` supplies its declared
     driving parameters (omitted optional inputs fall back to their declared
     default). :func:`lower_component` rewrites this into the existing ``library``
     component form so ``merge_assembly`` builds it exactly the way it builds a
     standard part — through ``_generate_library_part`` -> the ``"recipe"``
     library tool (registered in worker.py).

  2. Per-recipe INPUT-SCHEMA form (returned by :func:`input_schema`): an ordered
     list of declared inputs, each:

         { "name": "module_mm", "type": "length", "unit": "mm",
           "default": 2.0, "min": 0.2, "max": 50.0,
           "required": true, "doc": "..." }

     ``type`` is one of length | number | int | bool | string. ``length`` inputs
     are validated and stored in the canonical unit (mm) via the typed-units
     layer, so a unit-bearing string (``"0.25 in"``) is accepted and a
     wrong-dimension input (``"5 N"`` for a length) is refused at the door rather
     than silently mis-scaled. ``min``/``max`` bound the (canonical) value;
     ``choices`` (optional, string inputs) is an allow-list.

Validation fails LOUDLY at the door (mirrors ``worker._validate_manifest``): an
unknown recipe, a missing required input, an out-of-range or wrong-typed input,
and an unknown/extra input key are each a hard error before any geometry (or
token) is spent.
"""
from __future__ import annotations

import copy

from driftpin import units

__all__ = [
    "RecipeError",
    "InputSpec",
    "Recipe",
    "register",
    "get",
    "names",
    "input_schema",
    "list_recipes",
    "validate_inputs",
    "validate_ref",
    "build",
    "lower_component",
    "lower_manifest",
    "RECIPES",
    "SCHEMA",
]

# Contract version stamp — bumped if the recipe-ref / input-schema SHAPE changes
# (parallel to worker._MANIFEST_SCHEMA). Carried in input_schema() output so a
# downstream consumer can pin the contract it was authored against.
SCHEMA = "driftpin.recipe/1"

# Input value types a declared input may take. "length" rides the typed-units
# layer (#102): canonical unit mm, dimension-validated, unit-bearing strings
# accepted. The rest are plain scalars validated for Python type.
_TYPES = {"length", "number", "int", "bool", "string"}


class RecipeError(ValueError):
    """Raised when a recipe is unknown, or its inputs fail declared validation —
    the loud door failure (mirrors a manifest validation problem)."""


class InputSpec:
    """One declared driving parameter of a recipe.

    name      identifier used as the input key.
    type      one of length | number | int | bool | string.
    unit      documentation/coercion unit (e.g. "mm" for length, "deg" for an
              angle expressed as a number). Length inputs always resolve to mm.
    default   value used when the input is omitted (None => required, no default).
    required  if True and omitted with no default, validation fails loudly.
    min/max   inclusive bounds on the resolved (canonical) numeric value.
    choices   optional allow-list (string inputs).
    doc       human/agent-facing description.
    """

    __slots__ = ("name", "type", "unit", "default", "required",
                 "min", "max", "choices", "doc")

    def __init__(self, name, type, unit=None, default=None, required=False,
                 min=None, max=None, choices=None, doc=""):
        if type not in _TYPES:
            raise RecipeError(
                f"input {name!r}: unknown type {type!r} (expected one of "
                f"{sorted(_TYPES)})")
        self.name = name
        self.type = type
        self.unit = unit if unit is not None else (
            "mm" if type == "length" else None)
        self.default = default
        # An input with no default is required by definition; an explicit
        # required=True with a default is contradictory but harmless (the default
        # is simply never reached). Treat "no default" as the source of truth.
        self.required = required or (default is None)
        self.min = min
        self.max = max
        self.choices = tuple(choices) if choices else None
        self.doc = doc

    def as_dict(self):
        """The frozen JSON form of this input declaration (the contract)."""
        d = {"name": self.name, "type": self.type, "required": self.required}
        if self.unit is not None:
            d["unit"] = self.unit
        if self.default is not None:
            d["default"] = self.default
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.choices is not None:
            d["choices"] = list(self.choices)
        if self.doc:
            d["doc"] = self.doc
        return d

    # --- coercion + validation (the door) ------------------------------------

    def coerce(self, value):
        """Validate + canonicalize one supplied value. Returns the resolved value
        (length -> float mm; number -> float; int -> int; bool -> bool; string ->
        str). Raises :class:`RecipeError` on a wrong type / out-of-range / bad
        unit / off-allow-list value."""
        if self.type == "length":
            try:
                q = units.quantity(value, "length", default_unit=self.unit or "mm")
            except units.UnitError as e:
                raise RecipeError(f"input {self.name!r}: {e}") from None
            resolved = q.canonical  # mm
            self._check_range(resolved, "mm")
            return resolved
        if self.type == "number":
            resolved = self._as_float(value)
            self._check_range(resolved, self.unit or "")
            return resolved
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                # accept an integral float / numeric string, reject 1.5 and bool
                f = self._as_float(value)
                if f != int(f):
                    raise RecipeError(
                        f"input {self.name!r}: expected an integer, got {value!r}")
                resolved = int(f)
            else:
                resolved = int(value)
            self._check_range(resolved, self.unit or "")
            return resolved
        if self.type == "bool":
            if not isinstance(value, bool):
                raise RecipeError(
                    f"input {self.name!r}: expected a bool, got {value!r}")
            return value
        # string
        if not isinstance(value, str):
            raise RecipeError(
                f"input {self.name!r}: expected a string, got {value!r}")
        if self.choices is not None and value not in self.choices:
            raise RecipeError(
                f"input {self.name!r}: {value!r} not in allowed "
                f"{list(self.choices)}")
        return value

    def _as_float(self, value):
        if isinstance(value, bool):
            raise RecipeError(
                f"input {self.name!r}: expected a number, got bool {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise RecipeError(
                f"input {self.name!r}: expected a number, got {value!r}") from None

    def _check_range(self, value, unit):
        u = f" {unit}" if unit else ""
        if self.min is not None and value < self.min - 1e-12:
            raise RecipeError(
                f"input {self.name!r}: {value}{u} is below the minimum "
                f"{self.min}{u}")
        if self.max is not None and value > self.max + 1e-12:
            raise RecipeError(
                f"input {self.name!r}: {value}{u} is above the maximum "
                f"{self.max}{u}")


class Recipe:
    """A named build template: an ordered input schema + a deterministic build
    function ``build_fn(resolved_inputs, call)`` that emits the part."""

    __slots__ = ("name", "inputs", "build_fn", "doc", "emits")

    def __init__(self, name, inputs, build_fn, doc="", emits=None):
        self.name = name
        self.inputs = list(inputs)
        names_seen = set()
        for spec in self.inputs:
            if spec.name in names_seen:
                raise RecipeError(
                    f"recipe {name!r}: duplicate input {spec.name!r}")
            names_seen.add(spec.name)
        self.build_fn = build_fn
        self.doc = doc
        self.emits = emits or {}

    @property
    def by_name(self):
        return {s.name: s for s in self.inputs}

    def schema(self):
        """The frozen input-schema contract for this recipe."""
        return {
            "schema": SCHEMA,
            "recipe": self.name,
            "doc": self.doc,
            "inputs": [s.as_dict() for s in self.inputs],
            "emits": self.emits,
        }


# --- registry ----------------------------------------------------------------

RECIPES: dict[str, Recipe] = {}


def register(recipe: Recipe) -> Recipe:
    """Register (or replace) a recipe by name. Returns it (decorator-friendly)."""
    RECIPES[recipe.name] = recipe
    return recipe


def get(name: str) -> Recipe:
    """The registered recipe, or a loud :class:`RecipeError` naming the known set."""
    try:
        return RECIPES[name]
    except KeyError:
        raise RecipeError(
            f"unknown recipe {name!r} (registered: {sorted(RECIPES)})") from None


def names():
    """Sorted list of registered recipe names."""
    return sorted(RECIPES)


def input_schema(name: str) -> dict:
    """The declared input-schema contract for one recipe (see module docstring)."""
    return get(name).schema()


def list_recipes() -> dict:
    """A directory of every registered recipe: name -> {doc, required, optional,
    emits}. The cheap browse an agent runs before picking a recipe."""
    out = {}
    for n, r in sorted(RECIPES.items()):
        out[n] = {
            "doc": r.doc,
            "required": [s.name for s in r.inputs if s.required],
            "optional": [s.name for s in r.inputs if not s.required],
            "emits": r.emits,
        }
    return {"schema": SCHEMA, "count": len(out), "recipes": out}


# --- input validation (the door) ---------------------------------------------

def validate_inputs(name: str, inputs: dict | None) -> dict:
    """Validate ``inputs`` against recipe ``name``'s declared schema and return the
    RESOLVED input dict (canonical units, defaults filled). Raises
    :class:`RecipeError` loudly on: an unknown recipe, an unknown/extra input key,
    a missing required input, or a wrong-typed / out-of-range / bad-unit value —
    the same fail-at-the-door discipline as ``_validate_manifest``."""
    recipe = get(name)
    inputs = dict(inputs or {})
    by_name = recipe.by_name

    extra = sorted(set(inputs) - set(by_name))
    if extra:
        raise RecipeError(
            f"recipe {name!r}: unknown input(s) {extra} "
            f"(declared: {sorted(by_name)})")

    resolved = {}
    for spec in recipe.inputs:
        if spec.name in inputs:
            resolved[spec.name] = spec.coerce(inputs[spec.name])
        elif spec.default is not None:
            # Re-run the default through coercion so a length default lands in mm.
            resolved[spec.name] = spec.coerce(spec.default)
        elif spec.required:
            raise RecipeError(
                f"recipe {name!r}: missing required input {spec.name!r}")
        # an optional input with no default is simply omitted
    return resolved


def validate_ref(spec) -> list:
    """Validate a recipe-reference COMPONENT ``{"recipe": name, "inputs": {...}}``
    and return a list of human-readable problems (empty == valid) — the
    ``_validate_manifest``-style front door for a recipe component. Catches a
    non-object ref, a missing/unknown ``recipe``, a non-object ``inputs``, and any
    per-input validation failure."""
    problems = []
    if not isinstance(spec, dict):
        return ["recipe component must be an object"]
    name = spec.get("recipe")
    if not name or not isinstance(name, str):
        problems.append("recipe component must name a 'recipe' (string)")
        return problems
    if name not in RECIPES:
        problems.append(
            f"unknown recipe {name!r} (registered: {sorted(RECIPES)})")
        return problems
    inputs = spec.get("inputs", {})
    if not isinstance(inputs, dict):
        problems.append(f"recipe {name!r}: 'inputs' must be an object")
        return problems
    try:
        validate_inputs(name, inputs)
    except RecipeError as e:
        problems.append(str(e))
    return problems


# --- build (the only FreeCAD-touching path) ----------------------------------

def build(name: str, inputs: dict | None, call) -> dict:
    """Run recipe ``name`` with ``inputs`` against the worker, emitting geometry +
    published interfaces + declared intent. ``call(tool, **params)`` is the
    worker's handler dispatch (injected so this module never imports the worker).
    Validates inputs at the door first (raises :class:`RecipeError`). Returns
    ``{recipe, schema, inputs, handle, name, interfaces, intent, part}`` —
    deterministic for fixed inputs (rides the determinism envelope)."""
    resolved = validate_inputs(name, inputs)
    recipe = get(name)
    result = recipe.build_fn(resolved, call)
    result.setdefault("recipe", name)
    result.setdefault("schema", SCHEMA)
    result["inputs"] = resolved
    return result


# --- manifest lowering (so merge_assembly builds a recipe component) ----------

def lower_component(spec) -> dict:
    """Rewrite one recipe-reference component into the existing ``library``
    component form so ``merge_assembly`` builds it through ``_generate_library_part``
    exactly the way it builds a standard part. Validates the ref first (loud on a
    bad recipe/inputs). Carries over an optional ``envelope``."""
    problems = validate_ref(spec)
    if problems:
        raise RecipeError(f"invalid recipe component: {problems}")
    lib = {"tool": "recipe",
           "spec": {"recipe": spec["recipe"], "inputs": spec.get("inputs", {})}}
    out = {"library": lib}
    if "envelope" in spec:
        out["envelope"] = spec["envelope"]
    return out


def lower_manifest(manifest: dict) -> dict:
    """Return a deep copy of ``manifest`` with every recipe-reference component
    (a component carrying a ``recipe`` key) lowered to the ``library`` form via
    :func:`lower_component`. The result is a standard manifest that
    ``merge_assembly`` / ``validate_manifest`` accept unchanged — the bridge that
    lets a recipe component flow through the existing assembly machinery without
    editing it. Components already in file/manifest/library form pass through."""
    out = copy.deepcopy(manifest)
    comps = out.get("components")
    if isinstance(comps, dict):
        for cid, spec in list(comps.items()):
            if isinstance(spec, dict) and "recipe" in spec:
                comps[cid] = lower_component(spec)
    return out


# =============================================================================
# Reference recipes
# =============================================================================

def _build_spur_gear(p, call):
    """Build an involute spur gear from resolved inputs, wrapping the existing
    ``add_gear`` generator, then publish a ``gear_mesh`` interface frame at the
    gear axis and declare the watertight-solid intent."""
    r = call("add_gear",
             teeth=p["teeth"],
             module=p["module_mm"],
             height=p["width_mm"],
             pressure_angle=p["pressure_angle_deg"],
             external=p["external"],
             name="Gear")
    handle = r["handle"]
    iface = call("publish_interface", handle=handle, name="gear_mesh", frame={
        "origin": [0.0, 0.0, 0.0],
        "z_axis": [0.0, 0.0, 1.0],
        "x_axis": [1.0, 0.0, 0.0],
        "module_mm": p["module_mm"],
        "teeth": p["teeth"],
        "pitch_radius_mm": r["pitch_radius"],
    })
    intent = call("declare_intent", handle=handle,
                  contract={"watertight": True})
    return {
        "handle": handle,
        "name": r["name"],
        "interfaces": iface["interfaces"],
        "intent": intent["contract"],
        "part": r,
    }


register(Recipe(
    name="spur_gear",
    doc="Involute spur gear (wraps add_gear): a solid disc of teeth on the XY "
        "plane about +Z, publishing a 'gear_mesh' frame at its axis and declaring "
        "a watertight-solid intent. The canonical reference recipe (issue #136).",
    inputs=[
        InputSpec("module_mm", "length", unit="mm", default=2.0,
                  min=0.2, max=50.0, required=True,
                  doc="Gear module (tooth size); pitch_dia = module * teeth."),
        InputSpec("teeth", "int", default=17, min=3, max=500, required=True,
                  doc="Number of teeth (>=3 for a valid involute)."),
        InputSpec("width_mm", "length", unit="mm", default=6.0,
                  min=0.5, max=1000.0, required=False,
                  doc="Face width (extrusion along the axis)."),
        InputSpec("pressure_angle_deg", "number", unit="deg", default=20.0,
                  min=14.5, max=30.0, required=False,
                  doc="Involute pressure angle (14.5/20/25 deg are standard)."),
        InputSpec("external", "bool", default=True, required=False,
                  doc="External (True) vs internal/ring (False) gear."),
    ],
    build_fn=_build_spur_gear,
    emits={"interfaces": ["gear_mesh"], "intent": ["watertight"]},
))

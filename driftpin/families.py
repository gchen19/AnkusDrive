"""
Design tables — a variant family from a row x column table (issue #138, the B1
work item of docs/DESIGN_HIERARCHY.md §2 Theme B).

"Make all variants of a gear" today means a hand-rolled Python loop emitting
independent static files (example/gearbox_manifest.py). This module replaces the
loop with the universal MCAD primitive for variant families — the *design table*
(SolidWorks design tables / Creo family tables / Inventor iParts):

  * a **row** is a *variant*, keyed by a size designator;
  * a **column** is a *parameter / feature-flag / material / metadata*.

Materialize the family deterministically: for each row, in table order, split its
columns into recipe inputs vs item metadata, run the recipe (A1, issue #136), and
emit one part + one item + one sequential part number (C1, issue #140). The table
is text (CSV or JSON), git-diffable; geometry is the derived artifact.

This module is PURE PYTHON apart from the geometry build, which it reaches only
through an injected ``call(tool, **kw)`` dispatch (the worker's HANDLERS) — exactly
as driftpin/recipes.py does — so table parsing, the column split, door validation,
and item/part-number allocation all run free, fast and FreeCAD-free. Pass
``call=None`` to materialize the *bookkeeping* (items + part numbers + the column
split) without building any geometry.

------------------------------------------------------------------------------
This SUBSUMES the standard-part catalogs (#101 ISO tables): a bearing catalog IS a
family table keyed by designation. ``add_bearing`` becomes the ``ball_bearing``
recipe (registered below) driven by a catalog table, and a catalog row's geometry
is sourced from the ISO standards corpus (driftpin/analysis/standards) so a built
catalog part matches the standard table value by construction.

------------------------------------------------------------------------------
The family-table contract (schema "driftpin.family/1"):

  JSON form
  ---------
      { "schema": "driftpin.family/1",
        "family": "spur_gear_family",   # the family's name (-> item-id prefix)
        "recipe": "spur_gear",          # the A1 recipe each row is built with
        "mode":   "instances",          # "instances" | "configurations"
        "key":    "size",               # the column naming the variant (the
                                        #   size designator); defaults to col 0
        "rows": [
          { "size": "M1_24T", "module_mm": 1.0, "teeth": 24, "material": "POM" },
          ...
        ] }

  CSV form (same family, text/git-diffable, directives in leading '#' lines)
  -------------------------------------------------------------------------
      # family: spur_gear_family
      # recipe: spur_gear
      # mode: instances
      # key: size
      size,module_mm,teeth,material
      M1_24T,1.0,24,POM
      ...

Column routing (per row): the **key** column is the variant identity. Every other
column whose name is a declared input of the recipe becomes a recipe **input**;
every remaining column becomes free-form item **metadata** (e.g. ``material``). A
bool recipe input is a **feature-flag** column (CSV ``true``/``false`` is parsed to
a real bool); a CSV numeric/length cell is coerced by the recipe's typed door.

Two materialization **modes**, both supported:

  * **instances** — each variant is its own released file with its own part number
    (needed for BOM / revision / where-used); ``files = [<family>/<key>.FCStd]``.
  * **configurations** — the variants share one artifact (cheap to manage); every
    row's ``files = [<family>.FCStd]`` and the item carries a ``configuration`` tag.

The built geometry is IDENTICAL across the two modes (same recipe, same inputs) —
the modes differ only in file allocation and item identity.

Validation fails LOUDLY and NAMES THE ROW+COLUMN (mirrors recipes' door): an
unknown recipe, a duplicate size key, a missing size key, an out-of-range / wrong-
typed / unknown input value are each a hard problem before any geometry is spent.
"""
from __future__ import annotations

import csv
import io
import json

from driftpin import items as _items
from driftpin import recipes as _recipes

__all__ = [
    "FamilyError",
    "SCHEMA",
    "MODES",
    "load_table",
    "parse_csv",
    "normalize",
    "validate_table",
    "resolve_row",
    "materialize",
    "register_catalog_recipes",
]

# Contract version stamp — bumped only if the family-table SHAPE changes (parallel
# to recipes.SCHEMA / items.SCHEMA). Carried in materialize() output so a consumer
# can pin the contract it was authored against.
SCHEMA = "driftpin.family/1"

MODES = ("instances", "configurations")
_DEFAULT_MODE = "instances"
_DEFAULT_OUT_DIR = "components"

# CSV cells that name a boolean (a feature-flag column), case-insensitive.
_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


class FamilyError(ValueError):
    """Raised when a family table is malformed or a row fails recipe validation —
    the loud door failure (mirrors recipes.RecipeError / a manifest problem)."""


# --- load + parse ------------------------------------------------------------

def load_table(path: str) -> dict:
    """Read a family table from disk into the canonical normalized dict. Dispatches
    on extension: ``.csv`` -> :func:`parse_csv`, otherwise JSON. Raises loudly on an
    unreadable / malformed file (a bad table must never resolve to silence)."""
    with open(path) as f:
        text = f.read()
    if path.lower().endswith(".csv"):
        return parse_csv(text)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise FamilyError(f"family table {path!r}: invalid JSON ({e})") from None
    return normalize(raw)


def parse_csv(text: str) -> dict:
    """Parse the CSV family-table form into the canonical normalized dict. Leading
    lines whose first non-space char is ``#`` are *directives* (``# key: value``)
    carrying ``family`` / ``recipe`` / ``mode`` / ``key``; the remainder is standard
    CSV with a header row. Preserves column order. Empty cells are omitted (the
    recipe default applies)."""
    directives: dict[str, str] = {}
    body_lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            payload = line.lstrip()[1:].strip()
            if ":" in payload:
                k, v = payload.split(":", 1)
                directives[k.strip().lower()] = v.strip()
            # a bare '#' comment with no ':' is ignored
        else:
            body_lines.append(line)

    reader = csv.DictReader(io.StringIO("\n".join(body_lines)))
    rows: list[dict] = []
    for raw in reader:
        # csv.DictReader fills missing trailing fields with None; drop empty cells
        # so an omitted optional column falls back to the recipe default.
        row = {k: v for k, v in raw.items()
               if k is not None and v is not None and v != ""}
        if row:
            rows.append(row)

    table = {
        "schema": SCHEMA,
        "family": directives.get("family"),
        "recipe": directives.get("recipe"),
        "mode": directives.get("mode"),
        "key": directives.get("key"),
        "rows": rows,
    }
    return normalize(table)


def normalize(table: dict) -> dict:
    """Return a canonical family dict (schema/family/recipe/mode/key/rows) with
    defaults filled: ``mode`` defaults to ``instances``; ``key`` defaults to the
    first column of the first row. Light structural coercion only — it does NOT
    validate the recipe or the rows (that is :func:`validate_table`)."""
    if not isinstance(table, dict):
        raise FamilyError("family table must be a JSON object")
    rows = table.get("rows") or []
    if not isinstance(rows, list):
        raise FamilyError("family table 'rows' must be a list")
    key = table.get("key")
    if not key and rows and isinstance(rows[0], dict) and rows[0]:
        key = next(iter(rows[0]))
    return {
        "schema": table.get("schema", SCHEMA),
        "family": table.get("family"),
        "recipe": table.get("recipe"),
        "mode": table.get("mode") or _DEFAULT_MODE,
        "key": key,
        "rows": rows,
    }


# --- column routing + per-cell coercion --------------------------------------

def _coerce_flag(spec, value):
    """Coerce a feature-flag (bool) cell. A real bool passes through; a recognized
    CSV truthy/falsey string is parsed; anything else is left for the recipe door to
    reject loudly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    return value  # recipe door will reject a non-bool


def _autoparse(value):
    """Best-effort typing of a free-form metadata cell so metadata is queryable:
    a bool-ish / int-ish / float-ish string becomes that type; everything else stays
    a string. (Metadata typing never affects recipe validation.)"""
    if not isinstance(value, str):
        return value
    s = value.strip()
    low = s.lower()
    if low in _TRUE and low not in {"1"}:
        return True
    if low in _FALSE and low not in {"0"}:
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return value


def _split_row(family, row):
    """Split a row's columns into (key_value, raw_inputs, metadata) using the
    recipe's declared input schema. The key column is identity; columns that name a
    recipe input route to inputs (a bool input is flag-parsed); everything else
    routes to metadata. Does NOT yet run the recipe door."""
    recipe = _recipes.get(family["recipe"])
    by_name = recipe.by_name
    key_col = family["key"]
    key_val = row.get(key_col)
    if key_val is not None:
        key_val = str(key_val)

    raw_inputs = {}
    metadata = {}
    for col, val in row.items():
        if col in by_name:
            spec = by_name[col]
            raw_inputs[col] = _coerce_flag(spec, val) if spec.type == "bool" else val
        elif col == key_col:
            continue  # pure identity, neither a parameter nor metadata
        else:
            if val is not None and val != "":
                metadata[col] = _autoparse(val)
    return key_val, raw_inputs, metadata


def resolve_row(family, row):
    """Resolve one row to ``(key, resolved_inputs, metadata)``: split its columns,
    then run the resolved inputs through the recipe's typed door (canonical units,
    defaults filled, ranges enforced). Raises :class:`FamilyError` naming the
    row+column on any bad value."""
    key_val, raw_inputs, metadata = _split_row(family, row)
    try:
        resolved = _recipes.validate_inputs(family["recipe"], raw_inputs)
    except _recipes.RecipeError as e:
        raise FamilyError(
            f"family {family['family']!r} row {key_val!r}: {e}") from None
    return key_val, resolved, metadata


# --- validation (the door) ---------------------------------------------------

def validate_table(table) -> list:
    """Validate a family table and return a list of human-readable problems (empty
    == valid) — the ``validate_manifest``-style front door. Catches: a non-object
    table, a missing/unknown recipe, a bad mode, a missing key column, a duplicate
    or missing size key, and every per-row recipe-door failure (named row+column).
    Accepts either a raw or already-normalized table."""
    try:
        fam = normalize(table)
    except FamilyError as e:
        return [str(e)]

    problems = []
    name = fam["family"]
    if not name or not isinstance(name, str):
        problems.append("family table must name a 'family' (string)")

    recipe_name = fam["recipe"]
    if not recipe_name or not isinstance(recipe_name, str):
        problems.append("family table must name a 'recipe' (string)")
        return problems
    if recipe_name not in _recipes.RECIPES:
        problems.append(
            f"family {name!r}: unknown recipe {recipe_name!r} "
            f"(registered: {_recipes.names()})")
        return problems

    if fam["mode"] not in MODES:
        problems.append(
            f"family {name!r}: unknown mode {fam['mode']!r} (expected one of "
            f"{list(MODES)})")

    key_col = fam["key"]
    if not key_col:
        problems.append(f"family {name!r}: no key column (size designator)")
        return problems

    seen = {}
    for i, row in enumerate(fam["rows"]):
        if not isinstance(row, dict):
            problems.append(f"family {name!r} row {i}: must be an object")
            continue
        key_val = row.get(key_col)
        if key_val is None or key_val == "":
            problems.append(
                f"family {name!r} row {i}: missing size key (column {key_col!r})")
            continue
        key_val = str(key_val)
        if key_val in seen:
            problems.append(
                f"family {name!r} column {key_col!r}: duplicate size key "
                f"{key_val!r} (rows {seen[key_val]} and {i}) — size keys must be "
                f"unique")
        seen[key_val] = i
        try:
            resolve_row(fam, row)
        except FamilyError as e:
            problems.append(str(e))
    return problems


# --- materialize -------------------------------------------------------------

def _file_for(fam_name, mode, key, out_dir):
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in key)
    if mode == "configurations":
        return [f"{out_dir}/{fam_name}.FCStd"]
    return [f"{out_dir}/{fam_name}/{safe}.FCStd"]


def materialize(table, call=None, registry=None, *, mode=None,
                out_dir=_DEFAULT_OUT_DIR) -> dict:
    """Materialize a whole variant family from one table — the headline. For each
    row, in table order (deterministic): resolve the row, build it with the recipe
    (when ``call`` is provided), and allocate one item + one sequential part number.

    Parameters
    ----------
    table : dict
        A raw or normalized family table (see :func:`load_table`).
    call : callable | None
        The worker handler dispatch ``call(tool, **kw)`` used to build geometry.
        Pass ``None`` to materialize the *bookkeeping* (items + part numbers + the
        column split + metadata) with NO geometry build — pure, FreeCAD-free.
    registry : dict | None
        An items.json registry to allocate into (mutated in place); a fresh empty
        registry is created when omitted.
    mode : str | None
        Override the table's mode ("instances" | "configurations"). Used to prove
        the two modes yield identical geometry from the same table.
    out_dir : str
        Directory prefix for the emitted file path(s).

    Returns ``{schema, family, recipe, mode, key, count, rows, registry}`` where
    each row carries ``{key, item, part_number, files, inputs, metadata, handle,
    name, build}``. Validates the whole table first and raises
    :class:`FamilyError` (naming row+column) on any problem — nothing is built or
    allocated if the table is bad."""
    fam = normalize(table)
    if mode is not None:
        fam = dict(fam, mode=mode)
    problems = validate_table(fam)
    if problems:
        raise FamilyError(f"invalid family table: {problems}")

    if registry is None:
        registry = _items.empty_registry()

    fam_name = fam["family"]
    recipe_name = fam["recipe"]
    use_mode = fam["mode"]

    out_rows = []
    for row in fam["rows"]:
        key, resolved, metadata = resolve_row(fam, row)
        item_id = f"{fam_name}:{key}"
        files = _file_for(fam_name, use_mode, key, out_dir)

        handle = name = None
        build = None
        if call is not None:
            build = _recipes.build(recipe_name, resolved, call)
            handle = build.get("handle")
            name = build.get("name")

        meta = dict(metadata)
        meta.update({
            "family": fam_name,
            "recipe": recipe_name,
            "size": key,
            "inputs": resolved,
            "mode": use_mode,
        })
        if use_mode == "configurations":
            meta["configuration"] = key

        _items.new_item(registry, item_id, files=files, metadata=meta)
        part_number = registry["items"][item_id]["part_number"]

        out_rows.append({
            "key": key,
            "item": item_id,
            "part_number": part_number,
            "files": files,
            "inputs": resolved,
            "metadata": metadata,
            "handle": handle,
            "name": name,
            "build": build,
        })

    return {
        "schema": SCHEMA,
        "family": fam_name,
        "recipe": recipe_name,
        "mode": use_mode,
        "key": fam["key"],
        "count": len(out_rows),
        "rows": out_rows,
        "registry": registry,
    }


# =============================================================================
# Catalog recipes — a standard-part catalog IS a family table keyed by designation
# (subsumes #101). Registered into the shared recipe registry on import, so a
# catalog table drives add_bearing the way a gear family drives add_gear.
# =============================================================================

def _build_ball_bearing(p, call):
    """Build a deep-groove ball-bearing envelope whose dimensions are SOURCED FROM
    the ISO standards corpus (driftpin/analysis/standards) by designation — so a
    catalog row matches the standard table value by construction. Publishes a
    'bore' interface frame and declares the watertight-solid intent."""
    from driftpin.analysis import standards as _std
    desig = p["designation"]
    b = _std.bearing(desig)
    bore = b["bore_mm"]
    od = b["od_mm"]
    width = b["width_mm"]
    r = call("add_bearing", bore=bore, outer_diameter=od, width=width,
             name="Bearing")
    handle = r["handle"]
    iface = call("publish_interface", handle=handle, name="bore", frame={
        "origin": [0.0, 0.0, 0.0],
        "z_axis": [0.0, 0.0, 1.0],
        "x_axis": [1.0, 0.0, 0.0],
        "bore_mm": bore,
        "od_mm": od,
        "width_mm": width,
        "designation": desig,
    })
    intent = call("declare_intent", handle=handle, contract={"watertight": True})
    return {
        "handle": handle,
        "name": r["name"],
        "interfaces": iface["interfaces"],
        "intent": intent["contract"],
        "part": r,
        "designation": desig,
        "bore_mm": bore,
        "od_mm": od,
        "width_mm": width,
    }


_CATALOG_REGISTERED = False


def register_catalog_recipes():
    """Register the standard-part catalog recipes (currently ``ball_bearing``) into
    the shared recipe registry. Idempotent — safe to call from a worker import and
    from a test. ``designation`` is an allow-list drawn from the live ISO corpus, so
    an unknown designation is refused at the door naming the row+column."""
    global _CATALOG_REGISTERED
    if _CATALOG_REGISTERED:
        return
    try:
        from driftpin.analysis import standards as _std
        choices = _std.list_bearings()
    except Exception:
        choices = None  # corpus unavailable; leave designation unconstrained

    _recipes.register(_recipes.Recipe(
        name="ball_bearing",
        doc="Single-row deep-groove ball-bearing envelope (wraps add_bearing) whose "
            "bore/OD/width are looked up from the ISO 15 standards corpus by "
            "designation. The canonical catalog recipe: a bearing catalog is a "
            "family table keyed by designation (issue #138, subsumes #101).",
        inputs=[
            _recipes.InputSpec(
                "designation", "string", default=None, required=True,
                choices=choices,
                doc="ISO deep-groove bearing designation (e.g. '6205'); dimensions "
                    "are sourced from the standards corpus."),
        ],
        build_fn=_build_ball_bearing,
        emits={"interfaces": ["bore"], "intent": ["watertight"]},
    ))
    _CATALOG_REGISTERED = True


register_catalog_recipes()

"""
Item model + part numbering — the PLM item/document/file split (issue #140, the
C1 keystone of docs/DESIGN_HIERARCHY.md §2 Theme C).

In DriftPin today a part *is* its file path. There is no logical identity for
"the thing," so there is nowhere to hang a revision, a lifecycle state, metadata,
or a stable cross-reference — and renaming/moving a file breaks everything that
points at it. This module introduces that identity layer as a lightweight,
git-friendly text sidecar (`items.json`); design control stays text-on-the-
filesystem (git is the vault — §5 non-goal: no DB, no server).

This is pure Python — no FreeCAD, no LLM, no key — so any host (the reference
coordinator, a CI script, another AI tool) can load/validate/resolve the
registry, exactly like driftpin/manifest.py's resolve step. The MCP handlers in
worker.py are thin append-only wrappers over the functions here.

--- The contract (frozen in the first commit so #141/#142/#143/#146 build on it) ---

items.json (schema "driftpin.items/1"):

    {
      "schema": "driftpin.items/1",
      "part_number_format": {            # allocation state for sequential PNs
        "prefix": "DP-",                 # non-significant fixed prefix
        "digits": 6,                     # zero-pad width
        "next": 1003                     # next integer to hand out (monotonic)
      },
      "items": {
        "<item_id>": {                   # stable logical key a manifest references
          "part_number": "DP-001001",    # non-significant, unique, allocated
          "rev": "-",                    # RESERVED for #141 — held, not interpreted
          "lifecycle": "in_work",        # RESERVED for #141 — held, not a state machine
          "files": ["components/x.FCStd", "components/x.step"],  # the artifact(s)
          "metadata": { ... }            # free-form, queryable; ALL "meaning" lives here
        }
      }
    }

The **item** (logical part: part_number + rev + lifecycle + metadata) is distinct
from the **file** (the .FCStd/STEP artifact). One item may map to several files.

**Part numbers are non-significant and sequential by default** (a counter in
`part_number_format`). This is the strong modern PLM best practice: intelligent/
significant numbers run out of digit space and "become wrong" when a part changes
category. All significance is pushed into the queryable `metadata` object.

**`rev` and `lifecycle` are held here but interpreted elsewhere.** C1 only holds
the fields (with cheap shape validation); the transition rules, the Form/Fit/
Function predicate, and immutability now live in `lifecycle.py` (shipped #141) and
the ECO/where-used change record in `change.py` (shipped #142). This module still
never interprets a transition — it just carries the fields those modules read.

--- The item-reference form (how a manifest points at an item, not a bare path) ---

A manifest component/instance references an item by id instead of a bare file:

    { "item": "<item_id>" }              # object form (preferred), or
    "<item_id>"                          # bare-string shorthand

`resolve_item_ref` turns that into the item's `files[]`, so renaming/moving a
file updates the item's `files[]` in one place without breaking any reference.
`validate_manifest_refs` walks a manifest and flags every item-ref that dangles
(points at an unknown item id) — the C1 reference-integrity guard.
"""
import copy
import json

SCHEMA = "driftpin.items/1"

# Reserved lifecycle vocabulary (docs/DESIGN_HIERARCHY.md §2 C2). C1 only checks a
# present value is a string drawn from this set — it does NOT implement the state
# machine, the guarded transitions, or immutability. That is #141's scope; we hold
# the field so #141 extends without a schema migration.
LIFECYCLE_STATES = ("in_work", "in_review", "released", "obsolete")
_DEFAULT_LIFECYCLE = "in_work"

# Reserved revision default (a deliberate released milestone in #141; "-" = the
# conventional "no released revision yet"). Held, never interpreted, in C1.
_DEFAULT_REV = "-"

_DEFAULT_FORMAT = {"prefix": "DP-", "digits": 6, "next": 1001}


# --- load -------------------------------------------------------------------

def load_registry(path):
    """Read an items.json sidecar into a dict. Raises on unreadable/!JSON — a bad
    registry must fail loudly, never resolve to silence (the house rule)."""
    with open(path) as f:
        return json.load(f)


def empty_registry(part_number_format=None):
    """A well-formed, empty registry — the initial state a fresh project scaffolds
    (the on-disk home for item identity lands in #143/D1)."""
    fmt = dict(_DEFAULT_FORMAT)
    if part_number_format:
        fmt.update(part_number_format)
    return {"schema": SCHEMA, "part_number_format": fmt, "items": {}}


# --- validate ---------------------------------------------------------------

def _validate_format(fmt, problems):
    if not isinstance(fmt, dict):
        problems.append("part_number_format must be an object")
        return
    prefix = fmt.get("prefix", "")
    if not isinstance(prefix, str):
        problems.append("part_number_format.prefix must be a string")
    digits = fmt.get("digits", _DEFAULT_FORMAT["digits"])
    if not isinstance(digits, int) or isinstance(digits, bool) or digits <= 0:
        problems.append("part_number_format.digits must be a positive integer")
    nxt = fmt.get("next", _DEFAULT_FORMAT["next"])
    if not isinstance(nxt, int) or isinstance(nxt, bool) or nxt < 0:
        problems.append("part_number_format.next must be a non-negative integer")


def validate_registry(registry):
    """Structural + cross-reference validation of an items.json registry. Returns a
    list of human-readable problems; empty == valid (mirrors manifest's
    _validate_manifest). Schema-agnostic for structure: an absent `schema` is
    accepted as unversioned for back-compat; a PRESENT schema must be the known
    version. Catches what a JSON shape can't: a duplicate part number, a malformed
    item record, a bad reserved field."""
    problems = []
    if not isinstance(registry, dict):
        return ["registry must be a JSON object"]

    schema = registry.get("schema")
    if schema is not None and schema != SCHEMA:
        problems.append(f"unknown schema {schema!r} (expected {SCHEMA!r} or none)")

    if "part_number_format" in registry:
        _validate_format(registry["part_number_format"], problems)

    items = registry.get("items")
    if not isinstance(items, dict):
        problems.append("items must be an object")
        return problems

    seen_pn = {}
    for item_id, rec in items.items():
        if not isinstance(item_id, str) or not item_id:
            problems.append(f"item id {item_id!r} must be a non-empty string")
        if not isinstance(rec, dict):
            problems.append(f"item {item_id!r} must be an object")
            continue

        pn = rec.get("part_number")
        if not isinstance(pn, str) or not pn:
            problems.append(f"item {item_id!r} part_number must be a non-empty string")
        else:
            if pn in seen_pn:
                problems.append(
                    f"duplicate part_number {pn!r} (items {seen_pn[pn]!r} and "
                    f"{item_id!r}) — part numbers must be unique")
            seen_pn[pn] = item_id

        rev = rec.get("rev", _DEFAULT_REV)
        if not isinstance(rev, str):
            problems.append(f"item {item_id!r} rev must be a string")

        lc = rec.get("lifecycle", _DEFAULT_LIFECYCLE)
        if not isinstance(lc, str):
            problems.append(f"item {item_id!r} lifecycle must be a string")
        elif lc not in LIFECYCLE_STATES:
            problems.append(
                f"item {item_id!r} lifecycle {lc!r} is not one of "
                f"{list(LIFECYCLE_STATES)}")

        files = rec.get("files", [])
        if not isinstance(files, list) or not all(
                isinstance(fp, str) and fp for fp in files):
            problems.append(f"item {item_id!r} files must be a list of path strings")

        meta = rec.get("metadata", {})
        if not isinstance(meta, dict):
            problems.append(f"item {item_id!r} metadata must be an object")

    return problems


# --- allocate ---------------------------------------------------------------

def _format_part_number(fmt, n):
    prefix = fmt.get("prefix", _DEFAULT_FORMAT["prefix"])
    digits = fmt.get("digits", _DEFAULT_FORMAT["digits"])
    return f"{prefix}{n:0{digits}d}"


def allocate_part_number(registry):
    """Hand out the next non-significant sequential part number, mutating the
    registry's `part_number_format.next` counter. Deterministic and monotonic:
    same registry state -> same number, and the counter only ever advances (a
    number is never reused, even if its item is later deleted). Returns the
    formatted part-number string."""
    fmt = registry.setdefault("part_number_format", dict(_DEFAULT_FORMAT))
    for k, v in _DEFAULT_FORMAT.items():
        fmt.setdefault(k, v)
    n = fmt["next"]
    fmt["next"] = n + 1
    return _format_part_number(fmt, n)


def new_item(registry, item_id, files=None, metadata=None,
             rev=None, lifecycle=None):
    """Create an item in the registry: allocate a sequential part number, attach
    the artifact file(s) and free-form metadata, and seed the RESERVED rev /
    lifecycle fields with their defaults (held, not interpreted — #141 drives
    transitions). Mutates and returns the registry. Raises on a duplicate item id."""
    items = registry.setdefault("items", {})
    if item_id in items:
        raise ValueError(f"item id {item_id!r} already exists")
    pn = allocate_part_number(registry)
    items[item_id] = {
        "part_number": pn,
        "rev": rev if rev is not None else _DEFAULT_REV,
        "lifecycle": lifecycle if lifecycle is not None else _DEFAULT_LIFECYCLE,
        "files": list(files) if files else [],
        "metadata": dict(metadata) if metadata else {},
    }
    return registry


# --- resolve ----------------------------------------------------------------

def _ref_item_id(ref):
    """Normalize an item-reference to its item id, or None if it is not one.
    Accepts the object form {"item": "<id>"} or the bare-string shorthand "<id>"."""
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict) and "item" in ref:
        return ref["item"]
    return None


def resolve_item_ref(registry, ref):
    """Resolve an item-reference to the item's artifact file(s). `ref` is either an
    item-id string or {"item": "<id>"}. Returns the item's files[] list. Raises
    KeyError on a dangling reference (an unknown item id) — the C1 reference-
    integrity guard: a broken cross-reference fails loudly, it is never a silent
    empty resolve."""
    item_id = _ref_item_id(ref)
    if item_id is None:
        raise ValueError(f"not an item-reference: {ref!r}")
    items = registry.get("items", {})
    if item_id not in items:
        raise KeyError(f"dangling item reference: no item {item_id!r} in registry")
    return list(items[item_id].get("files", []))


def get_item(registry, item_id):
    """Return a deep copy of an item record, or None if absent."""
    rec = registry.get("items", {}).get(item_id)
    return copy.deepcopy(rec) if rec is not None else None


# --- manifest cross-reference integrity -------------------------------------

def _iter_manifest_refs(manifest):
    """Yield (where, item_id) for every item-reference in a manifest: a component
    spec `{"item": "<id>"}` or an instance `{"item": "<id>"}`. The item-ref is an
    alternative to the manifest's bare file/manifest/library source — identity, not
    a path. (This walker owns the item-ref contract; it does not touch manifest.py's
    own validator.)"""
    for cid, spec in (manifest.get("components") or {}).items():
        if isinstance(spec, dict) and "item" in spec:
            yield (f"component {cid!r}", spec["item"])
    for i, inst in enumerate(manifest.get("instances") or []):
        if isinstance(inst, dict) and "item" in inst:
            yield (f"instance {i}", inst["item"])


def validate_manifest_refs(manifest, registry):
    """Check that every item-reference in a manifest resolves against the registry.
    Returns a list of problems; empty == every reference is live. This is the guard
    that makes "a part is its filename" fragility go away: a manifest names items,
    and a dangling name is caught before a merge."""
    problems = []
    items = registry.get("items", {})
    for where, item_id in _iter_manifest_refs(manifest):
        if not isinstance(item_id, str) or item_id not in items:
            problems.append(f"{where} references unknown item {item_id!r}")
    return problems

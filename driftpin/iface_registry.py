"""
Interface-type registry — named, versioned interface types a part *declares
conformance to* (issue #146, RFC docs/DESIGN_HIERARCHY.md §6.3).

THE PRIMITIVE. Today a typed interface's contract (RFC §11.2 — gear_mesh /
bore_fit / frame_orientation) is re-specified per manifest, ad hoc. This module
promotes it: a small registry of **named, versioned interface definitions**
(``nema17_face@1``, ``bore_h7@1``) that a part *implements* — the mechanical
analog of ``implements SomeInterface``. A **conformance check** gates a part
claiming a type against that type's contract, measured from the REAL geometry
(circular features about the published interface frame). The benefits mirror the
software case (RFC §6.1/§6.3):

  * a part swap is safe if both sides conform to the same interface VERSION
    (this feeds the #147 substitutability gate);
  * a **bus** is one registry entry many parts reference (Ulrich's bus modular
    type) — attach a second, a third part, all gated against the same contract;
  * reuse stops being copy-paste — the contract lives once, in the registry.

DISCIPLINE (RFC §7, the §6.3 row): an **unknown interface type is itself a
violation** — never a silent pass, exactly like an unknown typed-gate `kind`
(``worker._run_typed_checks``). A part that claims a type the registry does not
define fails loudly at merge.

This module is PURE PYTHON: the registry, the versioned definitions, and the
conformance predicate (:func:`check_conformance`) all import cleanly WITHOUT
FreeCAD, so the contract/door tests run free and fast. The only geometry-touching
step — extracting a part's circular features from its ``Part.Shape`` — lives in
the worker's thin gate (``worker._gate_interface_conformance``), which hands this
module a plain list of measured features. Same split as :mod:`driftpin.recipes`
(pure logic; geometry injected), so there is no circular import and the same
predicate drives a live worker or a scripted feature list.

------------------------------------------------------------------------------
The frozen contract (what #147 substitutability and §11.2 build against):

  1. A type id is ``"<name>@<version>"`` (``version`` a positive int) — a
     semantic-versioning handle (RFC §6.1): a MAJOR break mints a new version,
     a conformant part pins the version it was authored against.

  2. A part DECLARES conformance with an ``implements`` list on its manifest
     component or instance::

         "components": { "motor": { "file": "motor.FCStd",
                                    "implements": ["nema17_face@1"] } }

     :func:`lower_manifest` expands every ``implements`` declaration into a
     ``checks`` entry of kind ``interface_conformance`` —

         { "kind": "interface_conformance", "part": "<instance>",
           "type": "nema17_face@1", "iface": "nema17_face" }

     — so the conformance gate rides ``merge_assembly``'s EXISTING typed-check
     dispatch unchanged (the gate registers append-only into ``_TYPED_GATES``),
     exactly as a recipe component lowers to the ``library`` form. The caller
     lowers before merge (mirrors ``recipes.lower_manifest``).

  3. The gate measures the part's distinct circular features (deduped by
     position, so a through-hole's two rims count once) about the published
     interface frame and hands this module a list of::

         { "dia_mm": float, "x_mm": float, "y_mm": float, "center_r_mm": float }

     :func:`check_conformance` returns a list of violations (empty == conforms),
     the same shape every typed gate returns.
"""
from __future__ import annotations

import copy
import re

__all__ = [
    "IfaceError",
    "InterfaceType",
    "register",
    "get",
    "names",
    "list_types",
    "type_schema",
    "parse_ref",
    "check_conformance",
    "lower_manifest",
    "REGISTRY",
    "SCHEMA",
]

# Contract version stamp — bumped if the type-id / feature-list / conformance
# SHAPE changes (parallel to recipes.SCHEMA and worker._MANIFEST_SCHEMA). Carried
# in the schema() / list_types() output so a downstream consumer can pin it.
SCHEMA = "driftpin.iface/1"

_REF_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)@(\d+)$")


class IfaceError(ValueError):
    """Raised when a type id is malformed, or a registration is invalid — the loud
    door failure (mirrors :class:`recipes.RecipeError`). An *unknown but
    well-formed* type id is NOT raised here: it is a conformance VIOLATION
    returned by :func:`check_conformance`, never a silent pass (RFC §6.3)."""


def parse_ref(type_id):
    """Validate a ``"<name>@<version>"`` type id and return ``(name, version:int)``.
    Raises :class:`IfaceError` on a malformed id (missing ``@``, non-int version,
    empty name) — the syntactic door. Existence is a separate question answered by
    :func:`check_conformance` (an unknown-but-well-formed id is a violation, not a
    syntax error)."""
    if not isinstance(type_id, str):
        raise IfaceError(f"interface type id must be a string, got {type_id!r}")
    m = _REF_RE.match(type_id.strip())
    if not m:
        raise IfaceError(
            f"malformed interface type id {type_id!r} (expected '<name>@<version>', "
            f"e.g. 'nema17_face@1')")
    return m.group(1), int(m.group(2))


class InterfaceType:
    """One named, versioned interface definition.

    name          base identifier (e.g. ``"nema17_face"``).
    version       positive int; the id is ``f"{name}@{version}"``.
    doc           human/agent-facing description.
    iface         the published-interface frame name that LOCATES this interface
                  on a conforming part (origin + axis the features are measured
                  about); defaults to ``name``.
    contract      a dict of the declared geometric bands (documented in the
                  schema; consumed by ``conformance_fn``).
    conformance_fn  ``fn(features, contract) -> list[violation dict]`` — the pure
                  predicate over measured circular features (empty == conforms).
    """

    __slots__ = ("name", "version", "doc", "iface", "contract", "conformance_fn")

    def __init__(self, name, version, conformance_fn, contract=None, doc="",
                 iface=None):
        if not isinstance(version, int) or version < 1:
            raise IfaceError(
                f"interface {name!r}: version must be a positive int, got "
                f"{version!r}")
        # validate the resulting id is well-formed (catches a bad name early)
        parse_ref(f"{name}@{version}")
        self.name = name
        self.version = version
        self.doc = doc
        self.iface = iface if iface is not None else name
        self.contract = dict(contract or {})
        self.conformance_fn = conformance_fn

    @property
    def id(self):
        return f"{self.name}@{self.version}"

    def check(self, features):
        """Run the conformance predicate over measured ``features`` (a list of
        circular-feature dicts). Returns a list of violations (empty == conforms).
        Stamps every violation with this type id."""
        out = []
        for v in self.conformance_fn(list(features or []), self.contract):
            out.append({"type": self.id, **v})
        return out

    def schema(self):
        """The frozen JSON description of this interface type (the contract)."""
        return {
            "schema": SCHEMA,
            "type": self.id,
            "name": self.name,
            "version": self.version,
            "iface": self.iface,
            "doc": self.doc,
            "contract": self.contract,
        }


# --- registry ----------------------------------------------------------------

REGISTRY: dict[str, InterfaceType] = {}


def register(t: InterfaceType) -> InterfaceType:
    """Register (or replace) an interface type by id. Returns it."""
    REGISTRY[t.id] = t
    return t


def get(type_id: str) -> InterfaceType:
    """The registered interface type, or a loud :class:`IfaceError` naming the
    known set. (The conformance GATE does not call this — it tolerates an unknown
    id as a violation; this is the strict lookup for the introspection handlers.)"""
    parse_ref(type_id)  # syntactic check first → a precise error
    try:
        return REGISTRY[type_id]
    except KeyError:
        raise IfaceError(
            f"unknown interface type {type_id!r} (registered: "
            f"{sorted(REGISTRY)})") from None


def names():
    """Sorted list of registered interface-type ids."""
    return sorted(REGISTRY)


def list_types() -> dict:
    """A directory of every registered interface type: id -> {name, version, doc,
    iface, contract}. The cheap browse before declaring ``implements``."""
    out = {tid: {"name": t.name, "version": t.version, "doc": t.doc,
                 "iface": t.iface, "contract": t.contract}
           for tid, t in sorted(REGISTRY.items())}
    return {"schema": SCHEMA, "count": len(out), "types": out}


def type_schema(type_id: str) -> dict:
    """The declared schema/contract for one interface type (loud on unknown)."""
    return get(type_id).schema()


# --- conformance (the gate's pure entry point) -------------------------------

def check_conformance(type_id, features) -> list:
    """Check measured ``features`` (a list of circular-feature dicts) against the
    interface type ``type_id``. Returns a list of violations (empty == conforms),
    the same shape every typed gate returns.

    An **unknown** (or malformed) type id is itself a single violation — never a
    silent pass (RFC §6.3, the same discipline as an unknown typed-gate kind in
    ``worker._run_typed_checks``). A part claiming an interface the registry does
    not define fails loudly at merge."""
    try:
        name, version = parse_ref(type_id)
    except IfaceError as e:
        return [{"type": type_id, "error": str(e)}]
    t = REGISTRY.get(type_id)
    if t is None:
        return [{"type": type_id,
                 "error": f"unknown interface type {type_id!r} "
                          f"(registered: {sorted(REGISTRY)})"}]
    return t.check(features)


# --- manifest lowering (the seam: implements -> interface_conformance checks) --

def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def lower_manifest(manifest: dict) -> dict:
    """Return a deep copy of ``manifest`` with every ``implements`` declaration
    (on a component or an instance) expanded into an ``interface_conformance``
    entry in the ``checks`` list — so ``merge_assembly`` gates conformance through
    its EXISTING typed-check dispatch, the core untouched (mirrors
    ``recipes.lower_manifest``). The caller lowers before merge.

    Each declared type id is syntactically validated (loud :class:`IfaceError` on
    a malformed ``name@version``); its EXISTENCE is deliberately NOT checked here —
    an unknown-but-well-formed type surfaces as a conformance VIOLATION at the gate
    (RFC §6.3), so it is reported in the merge, not hidden behind a pre-merge
    raise. Idempotent: re-lowering adds no duplicate checks."""
    out = copy.deepcopy(manifest)
    comps = out.get("components") or {}
    insts = out.get("instances") or []
    checks = out.setdefault("checks", [])
    have = {(c.get("part"), c.get("type"))
            for c in checks if isinstance(c, dict)
            and c.get("kind") == "interface_conformance"}
    for inst in insts:
        if not isinstance(inst, dict):
            continue
        cid = inst.get("component")
        iname = inst.get("name", cid)
        comp = comps.get(cid, {}) if isinstance(comps, dict) else {}
        declared = _as_list(inst.get("implements")) + _as_list(
            comp.get("implements") if isinstance(comp, dict) else None)
        seen = set()
        for tid in declared:
            if tid in seen:
                continue
            seen.add(tid)
            parse_ref(tid)  # loud on a malformed id (the syntactic door)
            if (iname, tid) in have:
                continue
            chk = {"kind": "interface_conformance", "part": iname, "type": tid}
            # carry the locating frame name so the gate measures about the right
            # axis even when several interfaces are published on one part.
            base = tid.split("@", 1)[0]
            t = REGISTRY.get(tid)
            chk["iface"] = t.iface if t is not None else base
            checks.append(chk)
            have.add((iname, tid))
    return out


# =============================================================================
# Reference interface types
# =============================================================================
#
# Conformance is measured from a part's distinct circular features (the worker
# gate extracts them from the REAL shape about the published interface frame):
#   feature = {dia_mm, x_mm, y_mm, center_r_mm}  (a through-hole counts once).
# A "central" feature is one on the interface axis (center_r ~ 0). The bands below
# are the type's visible design rules (RFC §6.1) — the public contract a part must
# satisfy; how it is otherwise shaped is the part's hidden, free implementation.

_AXIS_TOL_MM = 0.3   # how close to the axis a "central" feature must sit


def _central(features, tol=_AXIS_TOL_MM):
    return [f for f in features if f.get("center_r_mm", 9e9) <= tol]


def _conform_bore_h7(features, contract):
    """A bore on the interface axis whose diameter sits in the ISO H7 band of the
    type's nominal (a slip/locational-clearance fit). The canonical *bus* / *slot*
    interface (RFC §6.3): a shaft and any number of bored parts all conform to one
    ``bore_h7@1`` entry. Off-spec (oversize/undersize) bore => violation; no central
    bore => violation."""
    nom = contract["nominal_dia_mm"]
    lo, hi = contract["dia_min_mm"], contract["dia_max_mm"]
    central = _central(features)
    if any(lo - 1e-6 <= f["dia_mm"] <= hi + 1e-6 for f in central):
        return []
    if not central:
        return [{"feature": "bore",
                 "reason": f"no central bore found on the interface axis; "
                           f"needs a Ø{nom} mm H7 bore [{lo}, {hi}] mm"}]
    near = min(central, key=lambda f: abs(f["dia_mm"] - nom))
    return [{"feature": "bore", "measured_dia_mm": near["dia_mm"],
             "nominal_dia_mm": nom, "band_mm": [lo, hi],
             "reason": f"central bore Ø{near['dia_mm']} mm is outside the H7 "
                       f"band [{lo}, {hi}] mm for Ø{nom}"}]


def _conform_nema17_face(features, contract):
    """A NEMA 17 motor mounting face: a central pilot bore plus four mounting
    holes on the 31 mm square bolt pattern. The canonical *slot* interface — a
    motor and its bracket conform to one ``nema17_face@1`` entry, so any NEMA17
    motor drops into any NEMA17 bracket. A wrong pilot or a missing/mis-patterned
    hole set => violation(s)."""
    viol = []
    plo, phi = contract["pilot_min_mm"], contract["pilot_max_mm"]
    central = _central(features)
    if not any(plo - 1e-6 <= f["dia_mm"] <= phi + 1e-6 for f in central):
        if central:
            near = min(central, key=lambda f: abs(f["dia_mm"] - contract["pilot_nom_mm"]))
            viol.append({"feature": "pilot", "measured_dia_mm": near["dia_mm"],
                         "band_mm": [plo, phi],
                         "reason": f"pilot bore Ø{near['dia_mm']} mm outside the "
                                   f"NEMA17 band [{plo}, {phi}] mm"})
        else:
            viol.append({"feature": "pilot",
                         "reason": f"no central pilot bore (need Ø"
                                   f"{contract['pilot_nom_mm']} mm [{plo}, {phi}])"})
    rlo, rhi = contract["bolt_r_min_mm"], contract["bolt_r_max_mm"]
    hlo, hhi = contract["hole_min_mm"], contract["hole_max_mm"]
    holes = [f for f in features
             if rlo <= f["center_r_mm"] <= rhi and hlo - 1e-6 <= f["dia_mm"] <= hhi + 1e-6]
    if len(holes) < 4:
        viol.append({"feature": "mounting_holes", "found": len(holes), "need": 4,
                     "pattern_mm": contract["bolt_pattern_mm"],
                     "reason": f"found {len(holes)} mounting hole(s) on the "
                               f"{contract['bolt_pattern_mm']} mm pattern (r≈"
                               f"{contract['bolt_r_nom_mm']} mm, Ø"
                               f"{contract['hole_nom_mm']} mm); NEMA17 needs 4"})
    return viol


# bore_h7@1 — an 8 mm H7 bore (ISO 286 H7 for D=8: +0 / +0.015 mm).
register(InterfaceType(
    name="bore_h7", version=1, conformance_fn=_conform_bore_h7,
    doc="A locational-clearance (H7) bore of nominal Ø8 mm on the interface "
        "axis. The canonical bus/slot bore interface — a shaft and every bored "
        "part conform to this one entry, so a part swap is safe at the fit (RFC "
        "§6.3). H7 for Ø8 is +0 / +0.015 mm.",
    contract={"nominal_dia_mm": 8.0, "dia_min_mm": 8.0, "dia_max_mm": 8.015}))

# nema17_face@1 — a NEMA 17 mounting face (Ø22 H7 pilot + 4×Ø3 holes
# on a 31 mm square pattern; bolt-circle radius 31/√2 ≈ 21.92 mm).
register(InterfaceType(
    name="nema17_face", version=1, conformance_fn=_conform_nema17_face,
    doc="A NEMA 17 motor mounting face: a Ø22 mm H7 pilot bore and four Ø3 "
        "mm mounting holes on the 31 mm square bolt pattern. The slot interface "
        "any NEMA17 motor/bracket conforms to (RFC §6.3).",
    contract={
        "pilot_nom_mm": 22.0, "pilot_min_mm": 22.0, "pilot_max_mm": 22.06,
        "hole_nom_mm": 3.0, "hole_min_mm": 2.8, "hole_max_mm": 3.3,
        "bolt_pattern_mm": 31.0, "bolt_r_nom_mm": 21.92,
        "bolt_r_min_mm": 21.4, "bolt_r_max_mm": 22.4,
    }))

"""Drawing-is-manufacturable gates (issue #85, the "Next layer" of the TechDraw
export work) — does the *dimension set* reconstruct the part, and is the sheet
*legible*?

The renderer (``worker._compose_page_svg`` / ``_h_add_dimension``) is a deliberate
primitive: it places exactly the dimensions it is told and reads each one off the
real solid (``DP_TrueValue``). What it cannot do is judge the *set*. A drawing that
under-dimensions (a hole with a Ø but no location) or over-dimensions (the same span
called out twice with disagreeing values) exports just as happily as a correct one —
the same failure mode the geometry-realizes-declaration gate (:mod:`driftpin.realize`)
closed for assemblies: *a green render is not a manufacturable drawing.* So this is
that gate, one layer up: validate the drawing, not just that it rendered.

Two independent checks, both returning :mod:`realize`-style violation lists (a list
of dicts each carrying a human ``reason``; empty list == pass):

* **completeness** (``check_completeness``) — degrees-of-freedom accounting. A part
  is manufacturing-complete iff every feature is both **located** (positioned from a
  datum) and **sized**, with no degree of freedom pinned twice by *disagreeing*
  numbers. Process-aware, because the scheme differs: a *prismatic* (milled / plate)
  part locates each hole by X/Y from a datum corner and sizes the block W×H×T; a
  *turned* part is concentric by construction, so a step needs only Ø + axial length
  from the face — no radial location. Feeding a turned part a prismatic scheme (or
  vice-versa) is itself a finding.

* **legibility** (``check_legibility``) — pure 2-D geometry on the placed graphics:
  no two dimension labels overlap, no dimension line crosses a view outline it does
  not reference, and everything sits inside the printable border. Turns "the demo
  looks clean" into a regression assertion.

Design note: this module imports NO FreeCAD. It operates entirely on plain
descriptor dicts so the accounting and the 2-D geometry are unit-testable on the host
interpreter in milliseconds. The worker (``_h_drawing_gate`` / ``_h_drawing_legibility``)
is the thin FreeCAD-bound shim that reads the descriptors off the real ``Part.Shape``
and the composed page, then calls in here. Same split as ``realize`` (geometry there
is FreeCAD-bound by necessity; here it need not be).
"""
from __future__ import annotations

import math

# Matching tolerances. A dimension is said to "cover" a slot when its measured value
# lands within _SIZE_TOL of the slot's nominal (read off the solid) and, for a
# location, its endpoints land within _POS_TOL of the feature/datum. Two dimensions
# pinning one slot AGREE within _AGREE_TOL; beyond that they CONFLICT.
_SIZE_TOL = 0.05      # mm — a dim value matches a feature size within this
_POS_TOL = 0.20       # mm — a dim endpoint sits on a feature/datum within this
_AGREE_TOL = 0.05     # mm — two dims on one slot agree within this (else conflict)
_ANGLE_TOL = 0.5      # deg — an angle dim matches a cone half/included angle within this

# A turned part declared with prismatic location dims (or vice-versa) is a
# process-scheme error, surfaced rather than silently mapped.
PRISMATIC = "prismatic"   # milled block / plate: locate holes X/Y from a datum
TURNED = "turned"         # lathe part: concentric, locate by Ø + axial length


# --------------------------------------------------------------------------- #
# small vector helpers (lists of 3 floats — no numpy, no FreeCAD)
# --------------------------------------------------------------------------- #
def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _dist(a, b):
    return math.sqrt(sum(d * d for d in _sub(a, b)))


def _axis_index(axis):
    """Index (0/1/2) of the dominant component of a unit-ish axis vector."""
    return max(range(3), key=lambda i: abs(axis[i]))


# --------------------------------------------------------------------------- #
# Part B — manufacturing completeness (degrees-of-freedom accounting)
# --------------------------------------------------------------------------- #
#
# Feature descriptors (built by the worker off the real solid; see module docstring):
#   {"id":"BBOX","kind":"bbox","size":[w,h,t]}
#   {"id":"H1","kind":"hole","dia":6.0,"through":True,"depth":None,
#    "center":[x,y,z],"axis":[0,0,1]}
#   {"id":"H1.cb","kind":"counterbore","parent":"H1","dia":11.0,"depth":4.0}
#   {"id":"S1","kind":"cyl_step","dia":20.0,"length":30.0,"z0":0.0,"z1":30.0}   (turned)
#   {"id":"B1","kind":"bore","dia":8.0,"depth":12.0}                            (turned)
#   {"id":"CONE1","kind":"cone","semi_angle":45.0,"radius":6.0}   (conical face — issue #108)
#   {"id":"FREEFORM1","kind":"freeform","faces":20}              (BSpline/Bezier walls — #108)
#   {"id":"PAT1","kind":"pattern","count":80,"pitch":2.0,"depth":1.0,
#    "facet_angle":30.0,"draft_angle":3.0}                       (periodic micro-features — #108)
#
# Curved/periodic features (#108) are SURFACE-based, not edge-based, so a part can
# look fully dimensioned to the prismatic enumeration above while the defining
# geometry — a cone's half-angle, a freeform wall's profile, a tooth pattern's pitch
# — is never required. Their slots may be satisfied by a dimension OR an explicit
# NOTE (a "per CAD model / profile table" callout); a freeform/detail slot has no
# numeric value, so only a note (or a controlling section) can close it.
#
# Dimension descriptors (built by the worker off each DrawViewDimension):
#   {"name":"Dim1","type":"Diameter","value":6.0,
#    "circle":{"center":[x,y,z],"radius":3.0},"span":None,"from_datum":True}
#   {"name":"Dim2","type":"DistanceX","value":50.0,"circle":None,
#    "span":{"p1":[0,0,0],"p2":[50,0,0]},"from_datum":True}
#
# A "slot" is one required degree of freedom: a dict
#   {"id","feature","kind":"size"|"location","axis","nominal","desc"}


def required_slots(features, process):
    """The degrees of freedom a complete drawing must pin for ``features`` under
    ``process``. ``nominal`` is the true value read off the solid, so a covering
    dimension can be checked for agreement (conflict detection) — not just presence."""
    slots = []

    def add(fid, kind, axis, nominal, desc, *, note_ok=False, note_only=False):
        # note_ok: an explicit feature note may satisfy this slot (curved/periodic
        # geometry a number alone can't capture); note_only: ONLY a note (or a
        # controlling section) closes it — it carries no dimensionable numeric value.
        slots.append({"id": f"{fid}.{axis}", "feature": fid, "kind": kind,
                      "axis": axis, "nominal": float(nominal), "desc": desc,
                      "note_ok": bool(note_ok or note_only),
                      "note_only": bool(note_only)})

    for f in features:
        k = f["kind"]
        fid = f["id"]
        if k == "bbox":
            w, h, t = f["size"]
            if process == TURNED:
                # a turned part's profile is captured by its steps; the only
                # block-level size that isn't redundant with the steps is overall
                # length along the axis (the longest dim of the stock).
                add(fid, "size", "length", max(w, h, t), "overall length")
            else:
                add(fid, "size", "x", w, "overall width (X)")
                add(fid, "size", "y", h, "overall height (Y)")
                add(fid, "size", "z", t, "overall thickness (Z)")
        elif k == "hole":
            add(fid, "size", "dia", f["dia"], f"{fid} diameter")
            if process == TURNED:
                # concentric bore: no radial location, only depth if blind
                if not f.get("through", True) and f.get("depth"):
                    add(fid, "size", "depth", f["depth"], f"{fid} depth")
            else:
                c = f["center"]
                add(fid, "location", "x", c[0], f"{fid} X location from datum")
                add(fid, "location", "y", c[1], f"{fid} Y location from datum")
                if not f.get("through", True) and f.get("depth"):
                    add(fid, "size", "depth", f["depth"], f"{fid} depth")
        elif k in ("counterbore", "countersink"):
            add(fid, "size", "dia", f["dia"], f"{fid} {k} diameter")
            if f.get("depth"):
                add(fid, "size", "depth", f["depth"], f"{fid} {k} depth")
        elif k == "cyl_step":
            add(fid, "size", "dia", f["dia"], f"{fid} diameter")
            add(fid, "size", "length", f["length"], f"{fid} length")
        elif k == "bore":
            add(fid, "size", "dia", f["dia"], f"{fid} bore diameter")
            if f.get("depth"):
                add(fid, "size", "depth", f["depth"], f"{fid} bore depth")
        elif k == "fillet":
            # a fillet is called out by its radius — matched by an R dimension
            add(fid, "size", "radius", f.get("radius", 0.0), f"{fid} fillet radius")
        elif k == "chamfer":
            # a chamfer leg — matched by a linear dimension (the "L×45°" convention
            # is future work; v1 dimensions the leg)
            add(fid, "size", "size", f.get("size", 0.0), f"{fid} chamfer size")
        elif k == "cone":
            # a conical face (chamfer cone, countersink, taper): the curved angle a
            # machinist sets is the half-angle (or its 2× included angle) — matched by
            # an angle dimension, or documented by a note.
            add(fid, "size", "angle", f.get("semi_angle", 0.0),
                f"{fid} cone half-angle", note_ok=True)
        elif k == "freeform":
            # a BSpline/Bezier/freeform wall: no single number defines it. Require a
            # controlling section/profile or an explicit "per CAD model / profile
            # table" note — never silently ignored.
            nf = int(f.get("faces", 1))
            add(fid, "size", "profile", 0.0,
                f"{fid} freeform/BSpline surface ({nf} face{'s' if nf != 1 else ''}) "
                f"needs a controlling section/profile or a 'per CAD model' note",
                note_only=True)
        elif k == "pattern":
            # ONE periodic pattern (not N per-instance callouts): a complete drawing
            # gives pitch + count + depth + facet angle + draft angle and a detail
            # view — each satisfiable by a dim or by a single pattern-table note.
            cnt = int(f.get("count", 0))
            add(fid, "size", "pitch", f.get("pitch", 0.0),
                f"{fid} pattern pitch ({cnt}×)", note_ok=True)
            add(fid, "size", "count", cnt, f"{fid} pattern instance count",
                note_ok=True)
            add(fid, "size", "depth", f.get("depth", 0.0),
                f"{fid} pattern feature depth", note_ok=True)
            add(fid, "size", "facet_angle", f.get("facet_angle", 0.0),
                f"{fid} pattern facet angle", note_ok=True)
            add(fid, "size", "draft_angle", f.get("draft_angle", 0.0),
                f"{fid} pattern draft angle", note_ok=True)
            add(fid, "detail", "view", 0.0, f"{fid} pattern detail/section view",
                note_only=True)
    return slots


def _dim_diameter_value(dim):
    """The diameter a Ø/R dim implies (R doubles), else its raw value."""
    t = dim.get("type", "")
    v = float(dim.get("value", 0.0))
    return 2.0 * v if t == "Radius" else v


def _covers_size(slot, dim, features_by_id):
    """Does ``dim`` size the ``slot``? Matches a Ø/R/linear value to the slot's
    nominal, and (for circular features) requires the dim's circle to sit on the
    feature so a Ø6 on hole A is not credited to a different Ø6 hole B."""
    axis = slot["axis"]
    dt = dim.get("type", "")
    if axis == "dia":
        if dt not in ("Diameter", "Radius"):
            return False
        if abs(_dim_diameter_value(dim) - slot["nominal"]) > _SIZE_TOL:
            return False
        circ = dim.get("circle")
        feat = features_by_id.get(slot["feature"])
        if circ and feat and feat.get("center"):
            # compare in the plane perpendicular to the hole axis: a cylinder
            # surface's centre sits at an ARBITRARY point along the axis, while the
            # dim's circle is on a face, so their axial coords need not agree —
            # only the radial position identifies the hole.
            d = _sub(circ["center"], feat["center"])
            axis = feat.get("axis")
            if axis:
                dot = sum(d[i] * axis[i] for i in range(3))
                d = [d[i] - dot * axis[i] for i in range(3)]
            return math.sqrt(sum(c * c for c in d)) <= _POS_TOL
        # no circle to localise it: a Ø dimension sizes a hole/bore, but a bare R
        # dimension is a fillet callout (R3 == Ø6 numerically) — don't credit it to
        # a hole diameter, or a fillet would satisfy a hole's size slot.
        return dt == "Diameter"
    if axis == "angle":
        # a cone half-angle is sized by an Angle dimension, in degrees; accept either
        # the half-angle or its 2× included angle (both are valid conventions).
        if dt != "Angle":
            return False
        v = abs(float(dim.get("value", 0.0)))
        nom = slot["nominal"]
        return abs(v - nom) <= _ANGLE_TOL or abs(v - 2.0 * nom) <= _ANGLE_TOL
    if axis == "radius":
        # a fillet radius: an R dimension whose value is the radius (or a linear
        # callout of the same value)
        if dt == "Diameter":
            return False
        return abs(abs(float(dim.get("value", 0.0))) - slot["nominal"]) <= _SIZE_TOL
    # linear sizes: overall extents, lengths, depths — match value to nominal
    if dt in ("Diameter", "Radius", "Angle"):
        return False
    return abs(abs(float(dim.get("value", 0.0))) - slot["nominal"]) <= _SIZE_TOL


def _covers_location(slot, dim, features_by_id):
    """Does ``dim`` locate the ``slot``? A location dim runs from a datum to the
    feature along the slot axis: one endpoint on the feature centre, the dim aligned
    with the axis, and its value equal to that coordinate."""
    if dim.get("type", "") in ("Diameter", "Radius"):
        return False
    span = dim.get("span")
    feat = features_by_id.get(slot["feature"])
    if not span or not feat or not feat.get("center"):
        return False
    ai = {"x": 0, "y": 1, "z": 2}[slot["axis"]]
    p1, p2 = span["p1"], span["p2"]
    # the dim must run along the slot axis (its endpoints differ mainly in that comp)
    delta = _sub(p2, p1)
    if abs(delta[ai]) < max(abs(delta[(ai + 1) % 3]), abs(delta[(ai + 2) % 3])):
        return False
    # one endpoint sits on the feature centre's axis coordinate
    c = feat["center"][ai]
    near_feat = abs(p1[ai] - c) <= _POS_TOL or abs(p2[ai] - c) <= _POS_TOL
    return near_feat


def assign_dimensions(features, slots, dims, notes=None):
    """Map each dimension onto the slot(s) it satisfies. Returns
    ``(coverage, leftover, note_covered)`` where ``coverage[slot_id] = [dim,...]``,
    ``leftover`` is the dims that pinned nothing (candidate over-dimensioning), and
    ``note_covered`` is the set of slot ids closed by an explicit feature ``note``
    rather than a dimension.

    A dim binds to a slot one of two ways. When the worker has resolved the dim's
    references to a feature degree of freedom it stamps an explicit ``slot`` hint
    (``"BBOX.x"``, ``"H1.dia"``): that binding is *value-independent*, so a dim that
    targets the width but reads a wrong number still lands on the width slot — which
    is exactly what makes CONFLICT detectable (two dims, one slot, disagreeing
    values). Without a hint (hand-authored dims) we fall back to geometric matching:
    a value/reference match against the feature.

    ``notes`` is a list of ``{"feature": fid, ...}``; a note targeting feature *F*
    closes every ``note_ok`` slot of *F* (a cone half-angle, a freeform profile, a
    whole pattern's pitch/count/.../detail-view) — the "per CAD model / profile
    table" escape hatch the curved/periodic gate (#108) allows."""
    by_id = {f["id"]: f for f in features}
    valid = {s["id"] for s in slots}
    coverage = {s["id"]: [] for s in slots}
    noted_feats = {nt.get("feature") for nt in (notes or []) if nt.get("feature")}
    note_covered = {s["id"] for s in slots
                    if s.get("note_ok") and s["feature"] in noted_feats}
    used = set()
    for dim in dims:
        hint = dim.get("slot")
        if hint in valid:
            coverage[hint].append(dim)
            used.add(id(dim))
            continue
        matched = False
        for s in slots:
            if s.get("note_only"):
                continue   # a freeform profile / detail view is never a dim match
            ok = (_covers_size(s, dim, by_id) if s["kind"] == "size"
                  else _covers_location(s, dim, by_id))
            if ok:
                coverage[s["id"]].append(dim)
                matched = True
        if matched:
            used.add(id(dim))
    leftover = [d for d in dims if id(d) not in used]
    return coverage, leftover, note_covered


# violation code for an uncovered slot, by the feature class it belongs to: the
# curved/periodic classes (#108) get their own codes so they are never lumped in
# with — nor hidden by — the prismatic "under".
_UNDER_CODE = {"cone": "angle_undimensioned",
               "freeform": "freeform_undimensioned",
               "pattern": "pattern_undimensioned"}


def check_completeness(features, dims, process, *, datums_declared=False, notes=None):
    """Decide whether ``dims`` fully and non-redundantly reconstruct ``features``
    under ``process``. Returns a realize-style violation list; each violation has a
    ``code`` in {under, redundant, conflict, no_datum, extra, scheme,
    angle_undimensioned, freeform_undimensioned, pattern_undimensioned} and a
    ``reason``. An empty list means the drawing is manufacturing-complete.

    ``datums_declared`` — whether the part carries annotated datum faces
    (``DP_FaceRoles``); when true, a location dimension that is not measured *from*
    a datum is flagged (``no_datum``). ``notes`` — explicit feature notes that close
    curved/periodic slots (see :func:`assign_dimensions`)."""
    slots = required_slots(features, process)
    coverage, leftover, note_covered = assign_dimensions(features, slots, dims, notes)
    out = []

    feat_kind = {f["id"]: f.get("kind") for f in features}
    by_id = {s["id"]: s for s in slots}
    pattern_flagged = set()   # one pattern_undimensioned per pattern feature, not N
    for sid, covering in coverage.items():
        s = by_id[sid]
        if not covering and sid not in note_covered:
            kind = feat_kind.get(s["feature"])
            code = _UNDER_CODE.get(kind, "under")
            if code == "pattern_undimensioned":
                # collapse a pattern's many param slots into ONE finding for the
                # whole pattern (the wrong abstraction the gate used to explode into
                # N per-instance chamfer callouts).
                if s["feature"] in pattern_flagged:
                    continue
                pattern_flagged.add(s["feature"])
                out.append({
                    "code": code, "feature": s["feature"], "kind": "pattern",
                    "reason": f"periodic pattern {s['feature']} is undimensioned — "
                              f"call out pitch + count + depth + facet angle + draft "
                              f"angle and add a detail view (or a pattern-table note), "
                              f"not per-instance dimensions"})
            else:
                out.append({
                    "code": code, "slot": sid, "feature": s["feature"],
                    "kind": s["kind"], "axis": s["axis"], "nominal": s["nominal"],
                    "reason": f"{s['desc']} is not dimensioned — the part is "
                              f"under-constrained ({'size' if s['kind']=='size' else 'location'} "
                              f"missing; nominal {s['nominal']:.2f} mm)"})
            continue
        if not covering:
            continue   # closed by an explicit note — satisfied
        # redundancy / conflict among multiple dims on one slot
        if len(covering) > 1:
            vals = [_dim_diameter_value(d) if s["axis"] == "dia"
                    else abs(float(d.get("value", 0.0))) for d in covering]
            spread = max(vals) - min(vals)
            names = [d.get("name", "?") for d in covering]
            if spread > _AGREE_TOL:
                out.append({
                    "code": "conflict", "slot": sid, "feature": s["feature"],
                    "dims": names, "values": [round(v, 3) for v in vals],
                    "reason": f"{s['desc']} is dimensioned {len(covering)}× with "
                              f"DISAGREEING values {[round(v,2) for v in vals]} mm "
                              f"({', '.join(names)}) — the drawing contradicts itself"})
            else:
                out.append({
                    "code": "redundant", "slot": sid, "feature": s["feature"],
                    "dims": names,
                    "reason": f"{s['desc']} is dimensioned {len(covering)}× "
                              f"({', '.join(names)}) — redundant; remove all but one "
                              f"(over-dimensioning invites tolerance conflicts)"})
        # datum discipline: located dims should originate at a datum
        if s["kind"] == "location" and datums_declared:
            if any(not d.get("from_datum", False) for d in covering):
                out.append({
                    "code": "no_datum", "slot": sid, "feature": s["feature"],
                    "reason": f"{s['desc']} is measured from a non-datum reference — "
                              f"locate it from an annotated datum face for repeatable setup"})

    for d in leftover:
        out.append({
            "code": "extra", "dim": d.get("name", "?"),
            "type": d.get("type"), "value": d.get("value"),
            "reason": f"dimension {d.get('name','?')} ({d.get('type')} "
                      f"{d.get('value')}) does not pin any feature degree of freedom "
                      f"— redundant or mis-referenced (over-dimensioning)"})
    return out


def completeness_report(features, dims, process, *, datums_declared=False, notes=None):
    """``check_completeness`` plus a positive summary, so a PASS still reports the
    numbers (a green gate that shows its work, like the realize gate). The coverage
    accounting spans ALL feature classes — prismatic holes AND curved/periodic
    surface features (#108) — so a cone half-angle or a pattern left undimensioned
    drops the ratio. Returns ``{ok, violations, slots_total, slots_covered,
    slots_noted, coverage, process, features, ...}``."""
    slots = required_slots(features, process)
    coverage, _leftover, note_covered = assign_dimensions(features, slots, dims, notes)
    by_dim = sum(1 for v in coverage.values() if v)
    noted = sum(1 for sid in note_covered if not coverage[sid])
    covered = by_dim + noted
    violations = check_completeness(features, dims, process,
                                    datums_declared=datums_declared, notes=notes)
    return {
        "ok": not violations,
        "violations": violations,
        "process": process,
        "slots_total": len(slots),
        "slots_covered": covered,
        "slots_noted": noted,
        "coverage": round(covered / len(slots), 4) if slots else 1.0,
        "features": len(features),
        "dimensions": len(dims),
        "notes": len(notes or []),
    }


# --------------------------------------------------------------------------- #
# Part B2 — tolerance necessity (issue #173): is every toleranced dimension
# EARNING its tolerance, and is every functional feature toleranced at all?
# --------------------------------------------------------------------------- #
#
# Advisory tier (like legibility). The completeness gate above decides the drawing
# PASS/FAIL — does the dimension set reconstruct the part. This layer asks a softer,
# orthogonal question about each dimension's TOLERANCE, and returns warnings only:
# the gate never fails on necessity unless the caller opts into strict mode.
#
# A feature is FUNCTIONAL when something already on the model says it earns
# precision — an annotated sealing/mating/datum face role (``annotate_face``), a
# published interface frame, a declared fit (``fit_check`` inputs), or a thread
# spec. Everything else is FREE: its size is held only to general tolerances. The
# two v1 rules:
#
#   * a FUNCTIONAL feature dimensioned with NO explicit tolerance -> under_toleranced
#     (the fit / seal / interface is silently left to general tolerances).
#   * a FREE feature toleranced TIGHTER than the IT-grade threshold for its size
#     band (default IT7) -> over_toleranced (precision no function asks for is
#     needless cost), reported with the ISO 286 band it could relax to.
#
# ``functional`` maps feature id -> a human backing string ("sealing face role",
# "declared fit H7/g6", "published interface bore", "thread M6"); the worker builds
# it from the roles/interfaces/fits/threads already on the part, the pure core just
# consults it. A dimension carries a tolerance as signed deviations ``plus``/
# ``minus`` (or the symmetric shorthand ``tol``), matching :mod:`analysis.tolerance`;
# absent both -> "no explicit tolerance". The ISO 286 IT tables are reused from
# :mod:`driftpin.analysis.tolerance` so there is one source of truth for the bands.


def _it_band_mm(size, grade):
    """The ISO 286 IT-grade band width in mm for a nominal ``size`` (mm), reusing
    the tabulated grades in :mod:`analysis.tolerance`. ``None`` when the size is
    off-table (>500 mm or <=0) or the grade is not tabulated (v1: IT4–IT11)."""
    from driftpin.analysis import tolerance
    try:
        idx = tolerance._band_index(abs(float(size)))
        return tolerance._it(int(grade), idx) / 1000.0
    except Exception:
        return None


def _it_grade_of(size, tol_width):
    """The finest ISO 286 IT grade whose band still contains ``tol_width`` (mm) at
    ``size`` — an approximate read of "how tight is this tolerance". Returns an int
    grade, ``None`` off-table, or the finest tabulated grade when the tolerance is
    even tighter than that (reported as "finer than ITn" by callers)."""
    from driftpin.analysis import tolerance
    try:
        idx = tolerance._band_index(abs(float(size)))
    except Exception:
        return None
    for g in sorted(tolerance._IT):
        if tolerance._IT[g][idx] / 1000.0 >= tol_width - 1e-9:
            return g
    return min(tolerance._IT)   # tighter than the finest tabulated grade


def _dim_tol_width(dim):
    """A dimension's total tolerance band (mm) from signed ``plus``/``minus`` or a
    symmetric ``tol``; ``None`` when the dimension carries no explicit tolerance."""
    if dim.get("plus") is not None or dim.get("minus") is not None:
        return abs(float(dim.get("plus", 0.0)) - float(dim.get("minus", 0.0)))
    if dim.get("tol") is not None:
        return abs(2.0 * float(dim["tol"]))
    return None


def _dim_feature_map(features, dims, process):
    """Best-effort map ``id(dim) -> {"feats": {fid,...}, "kinds": {"size"|"location"}}``:
    which feature(s) each dimension references and whether it pins a SIZE or a
    LOCATION degree of freedom. Reuses the completeness gate's slot assignment (value
    + geometric reference matching), so it needs no new heuristics — a dim binds to a
    feature exactly when it would help pin one of that feature's degrees of freedom.
    The size/location split matters because a fit/seal/thread backs a feature's SIZE,
    not where it sits (positional tolerancing is a separate GD&T concern)."""
    slots = required_slots(features, process)
    coverage, _leftover, _noted = assign_dimensions(features, slots, dims)
    by_slot = {s["id"]: s for s in slots}
    out = {}
    for sid, covering in coverage.items():
        s = by_slot[sid]
        kind = "size" if s["kind"] == "size" else "location"
        for d in covering:
            e = out.setdefault(id(d), {"feats": set(), "kinds": set()})
            e["feats"].add(s["feature"])
            e["kinds"].add(kind)
    return out


def check_tolerance_necessity(features, dims, functional=None, process=PRISMATIC,
                              *, threshold_grade=7):
    """Flag dimensions whose tolerance is not matched to the part's declared
    function. Advisory: returns a realize-style finding list (empty == quiet), each
    with ``code`` in {under_toleranced, over_toleranced}, the ``dim`` name, the
    ``feature`` it references, a ``verdict``, and a ``suggestion``.

    ``functional`` — ``{feature_id: backing_str}`` for features some existing model
    fact makes functional (face role / interface / fit / thread). ``process`` picks
    the completeness slot scheme used to attribute each dim to a feature.
    ``threshold_grade`` — the IT grade a FREE feature may be held to before it reads
    as over-toleranced (default 7)."""
    functional = functional or {}
    fmap = _dim_feature_map(features, dims, process)
    out = []
    for d in dims:
        if d.get("feature"):
            feats = {str(d["feature"])}
            kinds = set(d.get("kinds") or ["size"])
        else:
            entry = fmap.get(id(d))
            if not entry:
                continue   # pins no feature DOF — the completeness gate owns 'extra'
            feats, kinds = entry["feats"], entry["kinds"]
        func_fid, backing = None, None
        for fid in sorted(feats):
            if fid in functional:
                func_fid, backing = fid, functional[fid]
                break
        name = d.get("name", "?")
        nominal = abs(float(d.get("value", 0.0)))
        tol_width = _dim_tol_width(d)

        # a fit/seal/thread governs a feature's SIZE, so only an untoleranced SIZE
        # dimension of a functional feature is under-toleranced — its position is a
        # separate (GD&T) concern the necessity gate does not pin in v1.
        if backing is not None and tol_width is None and "size" in kinds:
            out.append({
                "code": "under_toleranced", "verdict": "under_toleranced",
                "dim": name, "feature": func_fid, "backing": backing,
                "nominal": round(nominal, 4),
                "suggestion": f"add an explicit tolerance sized to its function "
                              f"({backing})",
                "reason": f"{func_fid} is functional ({backing}) but dimension "
                          f"{name} (nominal {nominal:.3f} mm) carries no explicit "
                          f"tolerance — under-toleranced; the fit/interface is left "
                          f"to general tolerances"})
            continue

        if backing is None and tol_width is not None:
            thr = _it_band_mm(nominal, threshold_grade)
            if thr is None or tol_width >= thr - 1e-9:
                continue
            fid = sorted(feats)[0]
            grade = _it_grade_of(nominal, tol_width)
            grade_txt = f"~IT{grade}" if grade is not None else "very tight"
            out.append({
                "code": "over_toleranced", "verdict": "over_toleranced",
                "dim": name, "feature": fid,
                "nominal": round(nominal, 4), "tol_width": round(tol_width, 4),
                "grade": grade,
                "relax_to": {"grade": threshold_grade, "band_mm": round(thr, 4)},
                "suggestion": f"open to IT{threshold_grade} (±{thr / 2.0:.3f} mm / "
                              f"{thr * 1000.0:.0f} µm total) or a general ISO 2768 "
                              f"tolerance",
                "reason": f"dimension {name} on free feature {fid} is held to "
                          f"{tol_width * 1000.0:.0f} µm ({grade_txt}) but no fit, "
                          f"face role, interface, or thread requires it — "
                          f"over-toleranced; open to IT{threshold_grade} "
                          f"({thr * 1000.0:.0f} µm)"})
    return out


def tolerance_necessity_report(features, dims, functional=None, process=PRISMATIC,
                               *, threshold_grade=7, strict=False):
    """:func:`check_tolerance_necessity` plus counts, mirroring the other reports.
    ``ok`` is always True in the default advisory mode (necessity is a warning, not
    a manufacturability failure); ``strict=True`` makes ``ok`` reflect the findings
    for callers who want to fail the gate on them."""
    findings = check_tolerance_necessity(features, dims, functional, process,
                                         threshold_grade=threshold_grade)
    under = sum(1 for f in findings if f["code"] == "under_toleranced")
    over = sum(1 for f in findings if f["code"] == "over_toleranced")
    return {
        "ok": (not findings) if strict else True,
        "advisory": True,
        "strict": strict,
        "violations": findings,
        "under_toleranced": under,
        "over_toleranced": over,
        "threshold_grade": threshold_grade,
        "functional_features": sorted(functional or {}),
        "dimensions": len(dims),
    }


# --------------------------------------------------------------------------- #
# drawings-next — does this part need a cross-section to be understood?
# --------------------------------------------------------------------------- #
def needs_section(features):
    """Decide whether a drawing needs a cross-section view, from the same feature
    descriptors the completeness gate enumerates.

    A part needs a section when it has INTERNAL geometry the outline / hidden-line
    views convey ambiguously: a **counterbore** (a stepped bore — two diameters on
    one axis, whose step depth reads as a tangle of hidden lines), a **blind**
    hole/bore (it terminates inside the metal, so its depth is a hidden-line guess),
    or a **pocket**. A plain *through*-hole does NOT trigger one — it reads
    unambiguously as a circle plus two hidden edge lines, the case every machinist
    expects.

    Advisory, not a hard gate: a drawing without a recommended section is "could be
    clearer," not "wrong" — so this returns a recommendation, never a violation list.
    Returns ``{recommended: bool, reasons: [str], feature_ids: [str]}`` (same
    show-your-work shape as the other reports)."""
    reasons, ids = [], []
    for f in features:
        kind = f.get("kind")
        fid = f.get("id")
        if kind == "counterbore":
            reasons.append(f"counterbore {fid}: a stepped bore — the recess depth "
                           f"and shoulder read ambiguously as hidden lines")
            ids.append(fid)
        elif kind in ("hole", "bore"):
            # blind iff it does not pass through: prismatic holes carry through=False,
            # turned bores carry a finite depth (both leave a non-None `depth`).
            blind = (f.get("through") is False) or (f.get("depth") is not None)
            if blind:
                reasons.append(f"blind {kind} {fid}: terminates inside the metal — "
                               f"its depth is a hidden-line guess in the outline views")
                ids.append(fid)
        elif kind == "pocket":
            reasons.append(f"pocket {fid}: an internal cavity the outline cannot show")
            ids.append(fid)
    return {"recommended": bool(reasons), "reasons": reasons, "feature_ids": ids}


# --------------------------------------------------------------------------- #
# Part A2 — placement: collision-driven lane packing
# --------------------------------------------------------------------------- #
def pack_lanes(intervals, *, gap=1.0):
    """Assign each outward-stacked dimension a lane (0 = innermost) by greedy
    interval packing, replacing a blind one-lane-per-dimension stack.

    ``intervals`` is a list of (lo, hi) extents along the dimension-line axis, in
    DRAW ORDER — smallest span first, so feature dims are considered before the
    overall extents that nest them. Each interval takes the lowest lane whose
    current occupants it does not overlap (within ``gap`` mm). Two dimensions whose
    labels/lines don't overlap along the axis therefore SHARE a lane (compact, fewer
    lanes pushed off the sheet or across other views), while an overall extent that
    spans several nested feature dims overlaps them all and is pushed outward — so
    the familiar smallest-inside / overall-outside nesting falls out for free.
    Returns one lane index per interval."""
    lanes = []   # lane -> list of occupied (lo, hi)
    out = []
    for (lo, hi) in intervals:
        placed = None
        for li, occ in enumerate(lanes):
            if all(hi + gap <= o_lo or o_hi + gap <= lo for (o_lo, o_hi) in occ):
                occ.append((lo, hi))
                placed = li
                break
        if placed is None:
            lanes.append([(lo, hi)])
            placed = len(lanes) - 1
        out.append(placed)
    return out


# --------------------------------------------------------------------------- #
# Part A1 — legibility (2-D geometry on the placed graphics)
# --------------------------------------------------------------------------- #
#
# Inputs are page-mm descriptors (origin top-left, +Y down — the SVG page frame the
# composer emits):
#   labels:   [{"id","box":[x0,y0,x1,y1],"text":"50.00"}]      text bounding boxes
#   segments: [{"id","p1":[x,y],"p2":[x,y],"refs":["Front"]}]  dim/extension lines
#   views:    [{"id":"Front","box":[x0,y0,x1,y1]}]             view outline boxes
#   border:   [x0,y0,x1,y1]                                    printable area


def _boxes_overlap(a, b, gap=0.0):
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0]
                or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _box_contains(outer, inner):
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def _seg_intersects_box(p1, p2, box):
    """True if segment p1->p2 touches rectangle ``box`` (endpoint inside or an
    edge crossing). Liang–Barsky clip against the box."""
    x0, y0, x1, y1 = box
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, p1[0] - x0), (dx, x1 - p1[0]),
                 (-dy, p1[1] - y0), (dy, y1 - p1[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return False          # parallel and outside this edge
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return t0 <= t1


def check_legibility(labels, segments, views, border, *, min_gap=0.5):
    """Flag the ways a placed dimension layout becomes unreadable. Returns a
    realize-style violation list; ``code`` in {overlap, out_of_border, crosses_view}.

    * **overlap** — two dimension-text boxes intersect (with ``min_gap`` mm of
      breathing room): the numbers run together.
    * **out_of_border** — a label box, or a dimension-line endpoint, falls outside
      the printable border: it is clipped off the sheet.
    * **crosses_view** — a dimension line passes through a view outline it does not
      reference: the leader cuts across unrelated geometry."""
    out = []
    # 1. text-vs-text overlap
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            if _boxes_overlap(a["box"], b["box"], gap=min_gap):
                out.append({
                    "code": "overlap", "a": a.get("id", i), "b": b.get("id", j),
                    "reason": f"dimension labels {a.get('text', a.get('id', i))!r} and "
                              f"{b.get('text', b.get('id', j))!r} overlap — illegible"})
    # 2. everything inside the border
    if border is not None:
        for lab in labels:
            if not _box_contains(border, lab["box"]):
                out.append({
                    "code": "out_of_border", "a": lab.get("id"),
                    "reason": f"dimension label {lab.get('text', lab.get('id'))!r} "
                              f"extends past the sheet border — clipped on print"})
        for seg in segments:
            for pt in (seg["p1"], seg["p2"]):
                if not (border[0] <= pt[0] <= border[2]
                        and border[1] <= pt[1] <= border[3]):
                    out.append({
                        "code": "out_of_border", "a": seg.get("id"),
                        "reason": f"dimension line {seg.get('id')} runs off the sheet "
                                  f"border at ({pt[0]:.1f}, {pt[1]:.1f})"})
                    break
    # 3. dimension line crossing a view it does not reference
    for seg in segments:
        refs = set(seg.get("refs", []))
        for v in views:
            if v["id"] in refs:
                continue
            if _seg_intersects_box(seg["p1"], seg["p2"], v["box"]):
                out.append({
                    "code": "crosses_view", "a": seg.get("id"), "view": v["id"],
                    "reason": f"dimension line {seg.get('id')} crosses view "
                              f"{v['id']!r} which it does not dimension — reroute or "
                              f"use a leader into open space"})
    return out


def legibility_report(labels, segments, views, border, *, min_gap=0.5):
    """``check_legibility`` plus counts, mirroring ``completeness_report``."""
    violations = check_legibility(labels, segments, views, border, min_gap=min_gap)
    return {
        "ok": not violations,
        "violations": violations,
        "labels": len(labels),
        "segments": len(segments),
        "views": len(views),
    }

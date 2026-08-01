"""
Sheet metal (issue #230): bends and flanges, K-factor unfolding, the flat pattern,
and the layered DXF a press-brake shop actually quotes from.

FreeCAD's SheetMetal workbench is an unbundled addon that pulls in ``FreeCADGui`` on
import, so it cannot be relied on inside ``freecadcmd``. That is fine: a bend on a
constant-thickness shell is closed-form geometry, and owning the arithmetic is what
lets the numbers be *shown* rather than trusted. Everything here is stdlib-only
vector math over a feature model, so it unit-tests on the host interpreter in
milliseconds; the worker shim (``sheet_base`` / ``sheet_flange`` / ``sheet_tab`` /
``sheet_hem`` / ``sheet_unfold`` / ``sheet_flat_export`` / ``sheet_check``) only
turns the build recipes this module emits into OCC solids and hands real geometry
back.

Four ideas carry the module:

* **The flat pattern is derived from the feature model, not reverse-engineered from
  the solid.** A part is a base profile plus a tree of bends; :func:`unfold` replays
  that tree in a single global *flat space*, so the flat outline, the bend lines,
  and the folded solid all come from one description. Recovering bends by
  interrogating faces of a fused solid is the fragile way round.

* **BA given K is exact; K itself is convention.** The bend allowance
  ``BA = θ·(R + K·t)`` is the arc length of the neutral fibre — arithmetic, nothing
  more. *Where the neutral fibre sits* is a press-brake fact that varies with tool,
  material lot and grain direction. So a supplied K (or a shop bend table) yields
  ``fidelity="exact"``, and a K taken from the corpus below yields
  ``fidelity="correlation"`` with a band — and the K in force is echoed into every
  result so the number is never invisible.

* **Volume is not conserved by a bend, and that is correct.** Unfolding preserves
  neutral-fibre *length*, not material volume: a bend sector's true volume is
  ``θ·t·(R + t/2)·w`` while its flat footprint is ``θ·(R + K·t)·t·w``. Those agree
  only at ``K = 0.5``. A round-trip gate must therefore compare a *refold* against
  the original folded solid (:func:`refold`), never the flat against the folded.

* **The deliverable is the DXF, so the DXF is a first-class artifact.**
  :func:`to_dxf` writes R12 ASCII with a real LAYER table — ``CUT`` / ``BEND_UP`` /
  ``BEND_DOWN`` — because a flat pattern without bend lines on their own layers is
  not something a brake operator can set up from.

Scope, stated plainly: profiles are straight-sided polygons (a curved base profile
is rejected rather than silently faceted); flanges attach along a straight boundary
segment of an existing flat region and may nest to any depth; bend angles run
0 < θ ≤ 180 (a teardrop hem, which wraps past 180, is out of scope). Lengths mm,
angles degrees, matching the rest of ``driftpin``.
"""

import math

# --- 1. the K-factor corpus ---------------------------------------------------
#
# K locates the neutral fibre as a fraction of thickness from the INSIDE surface.
# It rises with the bend radius (a gentle bend strains the section less, so the
# fibre sits nearer mid-thickness) and with material softness. The table below is
# the shape every press-brake K chart takes — banded on r/t, split by how readily
# the material yields — with values in the middle of the published spread.
#
# THIS IS SHOP CONVENTION, NOT A STANDARD. Two brakes running the same part on
# different tooling will disagree at the ±15% level, which is why every consumer
# labels a corpus-sourced K as `correlation` and carries K_BAND_PCT.

SOFT = "soft"        # annealed aluminium, copper, brass
MEDIUM = "medium"    # mild / low-carbon steel
HARD = "hard"        # stainless, high-strength and alloy steel, titanium

# (r/t upper bound, K) — the FIRST band whose bound the ratio falls under wins.
_K_TABLE = {
    SOFT:   [(1.0, 0.33), (3.0, 0.40), (float("inf"), 0.45)],
    MEDIUM: [(1.0, 0.38), (3.0, 0.43), (float("inf"), 0.46)],
    HARD:   [(1.0, 0.40), (3.0, 0.45), (float("inf"), 0.50)],
}

K_BAND_PCT = 15.0
DEFAULT_BEND_CLASS = MEDIUM

# Material -> bend class. Names are the Materials-DB names (driftpin.analysis.
# materials) so a part that already declared a material needs no second vocabulary;
# `_CLASS_HINTS` catches the long tail by substring so an unlisted EN steel grade
# still lands in the right band instead of falling back to the global default.
_BEND_CLASS_BY_MATERIAL = {
    "AL1100-O": SOFT, "AL2024-T3": SOFT, "AL6061-T6": SOFT, "AL7075-T6": SOFT,
    "Aluminum Generic": SOFT, "AlMg3F24": SOFT, "AlMgSi1F31": SOFT,
    "Brass": SOFT, "Brass-C36000": SOFT, "Bronze-C93200": SOFT,
    "Copper Generic": SOFT, "Mg-AZ31B": SOFT,
    "Steel-A36": MEDIUM, "Steel-Generic": MEDIUM, "Steel-1045": MEDIUM,
    "CalculiX-Steel": MEDIUM,
    "SS304": HARD, "X5CrNi18-10": HARD, "X5CrNiMo17-12-2": HARD,
    "X2CrNiMoN17-13-3": HARD, "X6CrNiTi18-10": HARD,
    "Steel-4140-QT": HARD, "Ti-6Al-4V": HARD,
}
_CLASS_HINTS = (
    # the wrought aluminium series by number. 3xxx and 5xxx especially: they are THE
    # everyday sheet alloys and are absent from the FreeCAD Materials DB, so without
    # a hint they would fall through to the steel default.
    ("alumin", SOFT), ("al1", SOFT), ("al2", SOFT), ("al3", SOFT), ("al5", SOFT),
    ("al6", SOFT), ("al7", SOFT),
    ("almg", SOFT), ("brass", SOFT), ("bronze", SOFT), ("copper", SOFT),
    ("stainless", HARD), ("x5crni", HARD), ("x2crni", HARD), ("ss3", HARD),
    ("ti-", HARD), ("titan", HARD),
    ("steel", MEDIUM), ("s235", MEDIUM), ("s275", MEDIUM), ("s355", MEDIUM),
)

# Minimum inside bend radius as a multiple of thickness. Unlike K this does NOT
# track the bend class — 6061-T6 is soft to form but famously cracks under 3t
# because it is precipitation-hardened, while annealed 1100 takes 0.5t. So it gets
# its own corpus keyed by material, with a per-class fallback.
MIN_BEND_RADIUS_T = {
    "AL1100-O": 0.5, "AL2024-T3": 3.0, "AL6061-T6": 3.0, "AL7075-T6": 4.0,
    # not Materials-DB names, but the two alloys most sheet parts are actually cut
    # from — a sheet-metal corpus that omitted them would be missing its centre
    "AL3003-H14": 0.5, "AL5052-H32": 1.0,
    "Aluminum Generic": 1.0, "AlMg3F24": 1.0, "AlMgSi1F31": 1.5,
    "Brass": 1.0, "Brass-C36000": 1.5, "Bronze-C93200": 1.5,
    "Copper Generic": 0.5, "Mg-AZ31B": 5.0,
    "Steel-A36": 1.0, "Steel-Generic": 1.0, "Steel-1045": 2.0,
    "CalculiX-Steel": 1.0, "Steel-4140-QT": 3.0,
    "SS304": 1.0, "X5CrNi18-10": 1.0, "X5CrNiMo17-12-2": 1.0,
    "X2CrNiMoN17-13-3": 1.0, "X6CrNiTi18-10": 1.0, "Ti-6Al-4V": 4.5,
}
_MIN_RADIUS_T_BY_CLASS = {SOFT: 1.0, MEDIUM: 1.0, HARD: 2.0}

# DFM rule constants — press-brake rules of thumb, all in multiples of thickness.
MIN_FLANGE_T = 4.0        # outer leg >= 4t + R, else the leg falls into the die
HOLE_TO_BEND_T = 2.0      # hole edge >= 2t + R from the bend tangent, else it pulls
COLLISION_VOLUME_MM3 = 1e-3   # OCC common() noise floor for the refold check


def bend_class(material=None):
    """Which K-table band a material forms in, or ``None`` when it is unrecognised.

    Exact Materials-DB names win; otherwise a substring hint places the long tail of
    EN/DIN grades. An unknown material returns None rather than guessing — callers
    fall back to :data:`DEFAULT_BEND_CLASS` *and say so in the source string*."""
    if not material:
        return None
    if material in _BEND_CLASS_BY_MATERIAL:
        return _BEND_CLASS_BY_MATERIAL[material]
    low = str(material).lower()
    for hint, cls in _CLASS_HINTS:
        if hint in low:
            return cls
    return None


def k_factor(thickness_mm, inner_radius_mm, material=None, k=None, bend_class_=None):
    """Resolve the K-factor for one bend, and say where the number came from.

    ``k`` supplied -> ``fidelity="exact"``: the caller owns the number, so the bend
    allowance derived from it is pure arithmetic. Otherwise K is read from the
    corpus by (bend class, r/t) and comes back ``fidelity="correlation"`` with
    :data:`K_BAND_PCT`, because the neutral-fibre position is a property of the
    press and the tooling, not of the drawing.

    Returns ``{k, source, bend_class, r_over_t, fidelity, band_pct}``. ``source`` is
    a sentence, not a token — it is echoed into the unfold report so an agent
    reading the flat length can see the assumption that produced it."""
    if k is not None:
        return {"k": float(k), "source": "supplied by caller",
                "bend_class": bend_class_ or bend_class(material),
                "r_over_t": (round(inner_radius_mm / thickness_mm, 4)
                             if thickness_mm else None),
                "fidelity": "exact", "band_pct": 0.0}
    if thickness_mm <= 0:
        raise ValueError("thickness_mm must be > 0 to resolve a K-factor")
    cls = bend_class_ or bend_class(material)
    if cls is None:
        cls = DEFAULT_BEND_CLASS
        why = (f"material {material!r} not in the bend corpus; defaulted to the "
               f"{cls} band")
    else:
        why = f"{material or cls} forms in the {cls} band"
    ratio = inner_radius_mm / thickness_mm
    for bound, value in _K_TABLE[cls]:
        if ratio < bound:
            break
    return {"k": value, "source": f"{why}, r/t={ratio:.3g} -> K={value:g} "
                                  "(press-brake chart, shop convention)",
            "bend_class": cls, "r_over_t": round(ratio, 4),
            "fidelity": "correlation", "band_pct": K_BAND_PCT}


def min_bend_radius(thickness_mm, material=None):
    """Smallest inside radius the material will take without cracking, in mm.

    Returns ``{radius_mm, multiple_of_t, source, known}``. ``known=False`` means the
    material was not in the corpus and a class fallback was used — the screen keeps
    running (a missing corpus row must never abort a DFM check) but says so."""
    if material in MIN_BEND_RADIUS_T:
        mult = MIN_BEND_RADIUS_T[material]
        return {"radius_mm": round(mult * thickness_mm, 6), "multiple_of_t": mult,
                "source": f"{material}: min inside radius {mult:g}t", "known": True}
    cls = bend_class(material) or DEFAULT_BEND_CLASS
    mult = _MIN_RADIUS_T_BY_CLASS[cls]
    return {"radius_mm": round(mult * thickness_mm, 6), "multiple_of_t": mult,
            "source": f"material {material!r} not in the min-radius corpus; "
                      f"{cls}-class fallback of {mult:g}t",
            "known": False}


# --- 2. exact bend arithmetic -------------------------------------------------

def bend_allowance(angle_deg, inner_radius_mm, thickness_mm, k):
    """``BA = θ·(R + K·t)`` — the developed length of the neutral fibre through the
    bend, θ in radians. Exact given K; this is the whole of the unfold arithmetic."""
    return math.radians(float(angle_deg)) * (float(inner_radius_mm)
                                             + float(k) * float(thickness_mm))


def outside_setback(angle_deg, inner_radius_mm, thickness_mm):
    """``OSSB = (R + t)·tan(θ/2)`` — tangent line to the outside virtual apex.

    Undefined at θ = 180 (the apex runs to infinity: the outside surfaces of a hem
    are parallel and never meet), so a hem returns None and must be dimensioned
    tangent-to-free-end instead of to a sharp corner that does not exist."""
    half = math.radians(float(angle_deg)) / 2.0
    if abs(math.cos(half)) < 1e-9:
        return None
    return (float(inner_radius_mm) + float(thickness_mm)) * math.tan(half)


def inside_setback(angle_deg, inner_radius_mm):
    """``ISSB = R·tan(θ/2)`` — tangent line to the inside virtual apex. None at 180
    for the same reason as :func:`outside_setback`."""
    half = math.radians(float(angle_deg)) / 2.0
    if abs(math.cos(half)) < 1e-9:
        return None
    return float(inner_radius_mm) * math.tan(half)


def bend_deduction(angle_deg, inner_radius_mm, thickness_mm, k):
    """``BD = 2·OSSB − BA`` — what to subtract from the summed OUTSIDE dimensions to
    get the flat length. The same physics as the bend allowance, expressed the way a
    brake operator dimensions a part; None at 180 where OSSB is undefined."""
    ossb = outside_setback(angle_deg, inner_radius_mm, thickness_mm)
    if ossb is None:
        return None
    return 2.0 * ossb - bend_allowance(angle_deg, inner_radius_mm, thickness_mm, k)


def flat_length(lengths_mm, bends, reference="tangent"):
    """Developed (flat) length of a strip, by either of the two shop routes.

    ``bends`` is a list of ``{angle_deg, inner_radius_mm, thickness_mm, k}``.

    ``reference="tangent"`` — ``lengths_mm`` are the straight tangent-to-tangent
    segments; the flat length is ``Σ segment + Σ BA``.
    ``reference="outer"`` — ``lengths_mm`` are the OUTSIDE dimensions measured to
    the virtual apexes (what a drawing carries); the flat length is
    ``Σ outside − Σ BD``.

    The two are algebraically identical, which is exactly why both are offered and
    why the test suite asserts they agree to the last bit: an implementation where
    they diverge has a sign error in the setback.

    Returns ``{length_mm, reference, bend_allowance_mm, bend_deduction_mm,
    per_bend}``."""
    bas, bds = [], []
    for b in bends:
        ba = bend_allowance(b["angle_deg"], b["inner_radius_mm"],
                            b["thickness_mm"], b["k"])
        bd = bend_deduction(b["angle_deg"], b["inner_radius_mm"],
                            b["thickness_mm"], b["k"])
        bas.append(ba)
        bds.append(bd)
    straight = sum(float(x) for x in lengths_mm)
    if reference == "tangent":
        total = straight + sum(bas)
    elif reference == "outer":
        if any(bd is None for bd in bds):
            raise ValueError(
                "a 180-degree bend has no outside virtual apex, so its outside "
                "dimension is undefined; use reference='tangent'")
        total = straight - sum(bds)
    else:
        raise ValueError(f"reference must be 'tangent' or 'outer', got {reference!r}")
    return {"length_mm": total, "reference": reference,
            "bend_allowance_mm": sum(bas),
            "bend_deduction_mm": None if any(b is None for b in bds) else sum(bds),
            "per_bend": [{"bend_allowance_mm": ba, "bend_deduction_mm": bd}
                         for ba, bd in zip(bas, bds)]}


# --- 3. small vector helpers --------------------------------------------------
#
# Tuples, not a class: the model is serialised to JSON across the worker pipe on
# every call, so anything richer than a tuple would need a codec.

def _a2(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _s2(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _m2(a, s):
    return (a[0] * s, a[1] * s)


def _d2(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _n2(a):
    L = math.hypot(a[0], a[1])
    if L < 1e-12:
        raise ValueError("cannot normalise a zero-length 2-D vector")
    return (a[0] / L, a[1] / L)


def _right2(a):
    """Rotate a 2-D vector by -90 degrees. For a counter-clockwise polygon this
    turns an edge direction into the OUTWARD normal of that edge."""
    return (a[1], -a[0])


def _a3(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _s3(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _m3(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _d3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _x3(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _n3(a):
    L = math.sqrt(_d3(a, a))
    if L < 1e-12:
        raise ValueError("cannot normalise a zero-length 3-D vector")
    return (a[0] / L, a[1] / L, a[2] / L)


def _signed_area(poly):
    s = 0.0
    for i, p in enumerate(poly):
        q = poly[(i + 1) % len(poly)]
        s += p[0] * q[1] - q[0] * p[1]
    return s / 2.0


def _ccw(poly):
    """Force counter-clockwise winding so :func:`_right2` always yields the outward
    normal. Every splice and every outward direction downstream depends on it."""
    return list(poly) if _signed_area(poly) >= 0 else list(reversed(poly))


def _dedupe(poly, tol=1e-9):
    out = []
    for p in poly:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > tol:
            out.append(tuple(p))
    while len(out) > 1 and math.hypot(out[0][0] - out[-1][0],
                                      out[0][1] - out[-1][1]) <= tol:
        out.pop()
    return out


# --- 4. the feature model -----------------------------------------------------
#
# A part is: a base profile polygon, plus an ordered list of features. Each feature
# names its parent REGION and the segment of that region's LOCAL polygon it hangs
# off. Local, not global-flat, is the load-bearing choice: a child's position in the
# flat pattern moves when K moves, so anything stored in global flat coordinates
# would silently freeze the K that was in force when the feature was created.
# Everything K-dependent is recomputed by unfold().

FLANGE = "flange"
TAB = "tab"
HEM = "hem"


def new_part(thickness_mm, profile, material=None, origin=(0.0, 0.0, 0.0),
             e1=(1.0, 0.0, 0.0), e2=(0.0, 1.0, 0.0)):
    """Start a sheet-metal part from a closed straight-sided profile.

    ``profile`` is the base flange outline as ``[[x, y], ...]`` in the sketch plane,
    implicitly closed. ``origin``/``e1``/``e2`` place that plane in 3-D; the sheet
    grows from it along ``n = e1 × e2`` by ``thickness_mm``, so the profile plane is
    the sheet's *datum surface* and a flat part's flat pattern is congruent to its
    own profile.

    Returns the model dict — plain JSON, since it crosses the worker pipe."""
    if thickness_mm <= 0:
        raise ValueError("thickness_mm must be > 0")
    poly = _ccw(_dedupe([(float(x), float(y)) for x, y in profile]))
    if len(poly) < 3:
        raise ValueError("profile must have at least 3 distinct points")
    if abs(_signed_area(poly)) < 1e-9:
        raise ValueError("profile encloses no area")
    u1, u2 = _n3(e1), _n3(e2)
    normal = _n3(_x3(u1, u2))
    return {
        "thickness": float(thickness_mm),
        "material": material,
        "profile": [list(p) for p in poly],
        "regions": [{
            "id": 0, "parent": None, "kind": "base", "feature": None,
            "polygon": [list(p) for p in poly],
            "origin3": list(origin), "e1_3": list(u1), "e2_3": list(u2),
            "n_3": list(normal),
        }],
        "features": [],
        "holes": [],
    }


def region_point(region, q):
    """Map a point from a region's LOCAL 2-D frame onto its datum surface in 3-D."""
    o, e1, e2 = region["origin3"], region["e1_3"], region["e2_3"]
    return _a3(tuple(o), _a3(_m3(tuple(e1), q[0]), _m3(tuple(e2), q[1])))


def _region_edges(region):
    poly = [tuple(p) for p in region["polygon"]]
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def locate_edge(model, p_a, p_b, tol=1e-5):
    """Find which flat region a picked 3-D edge belongs to.

    The worker hands over the two endpoints of an edge the caller selected by stable
    tag; this walks every region's boundary, on both the datum and the opposite
    surface, and returns the matching LOCAL segment. A picked edge that is only a
    *piece* of a boundary segment is accepted and returned as that sub-segment —
    which is how a partial-width flange gets picked without a second API.

    Returns ``{region, a, b, side}`` (``side`` is "datum" or "offset", i.e. which
    face of the sheet the edge lies on) or ``None`` when nothing matches."""
    t = model["thickness"]
    for region in model["regions"]:
        n = tuple(region["n_3"])
        for side, lift in (("datum", 0.0), ("offset", t)):
            shift = _m3(n, lift)
            for (la, lb) in _region_edges(region):
                A = _a3(region_point(region, la), shift)
                B = _a3(region_point(region, lb), shift)
                seg = _s3(B, A)
                L = math.sqrt(_d3(seg, seg))
                if L < 1e-9:
                    continue
                u = _m3(seg, 1.0 / L)
                params = []
                for P in (p_a, p_b):
                    d = _s3(tuple(P), A)
                    s = _d3(d, u)
                    off = _s3(d, _m3(u, s))
                    if math.sqrt(_d3(off, off)) > tol:
                        params = None
                        break
                    if s < -tol or s > L + tol:
                        params = None
                        break
                    params.append(max(0.0, min(L, s)))
                if not params or abs(params[0] - params[1]) < 1e-9:
                    continue
                s0, s1 = sorted(params)
                dir2 = _n2(_s2(lb, la))
                a_local = _a2(la, _m2(dir2, s0))
                b_local = _a2(la, _m2(dir2, s1))
                return {"region": region["id"], "a": list(a_local),
                        "b": list(b_local), "side": side}
    return None


def _edge_outward(region, a, b):
    """Outward normal, in the region's local frame, of the boundary segment a->b.

    Derived from the polygon winding rather than from a point-in-polygon probe: the
    polygon is forced CCW at construction, so the right-hand normal of any boundary
    edge points out of the material by construction."""
    for (la, lb) in _region_edges(region):
        d = _s2(lb, la)
        L = math.hypot(*d)
        if L < 1e-9:
            continue
        u = (d[0] / L, d[1] / L)
        ok = True
        for P in (a, b):
            w = _s2(P, la)
            s = _d2(w, u)
            perp = math.hypot(w[0] - u[0] * s, w[1] - u[1] * s)
            if perp > 1e-6 or s < -1e-6 or s > L + 1e-6:
                ok = False
                break
        if ok:
            return _right2(u), u
    raise ValueError("segment is not on the region's boundary")


def attach(model, region_id, a, b, *, kind=FLANGE, length_mm=10.0, angle_deg=90.0,
           inner_radius_mm=None, direction="up", length_from="outer", k=None,
           name=None):
    """Add a flange, tab or hem along a boundary segment of an existing region.

    ``a``/``b`` are the segment endpoints in the parent region's local frame (from
    :func:`locate_edge`). ``direction`` is "up" (toward the region's +n, i.e. the
    side away from the datum surface) or "down".

    ``length_from`` says how ``length_mm`` is measured, which is the single most
    misread number in sheet metal: "outer" (default) is to the outside virtual
    apex — what a drawing dimension normally means; "inner" is to the inside apex;
    "tangent" is the straight leg from the end of the bend. A 180-degree hem has no
    apex at all, so it is forced to "tangent" rather than quietly producing an
    infinite leg.

    Mutates ``model`` (appends a feature and a region) and returns the feature dict.
    No K-factor is consumed here: the folded geometry depends on R, θ and the leg,
    never on where the neutral fibre sits."""
    region = next(r for r in model["regions"] if r["id"] == region_id)
    t = model["thickness"]
    a, b = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    m_local, along = _edge_outward(region, a, b)
    span = math.hypot(*_s2(b, a))
    if span < 1e-9:
        raise ValueError("attachment segment has zero length")
    # keep a->b running with the boundary direction so the child's local +x axis and
    # the parent's edge agree; otherwise the flange would be built mirrored.
    if _d2(_s2(b, a), along) < 0:
        a, b = b, a

    if kind == TAB:
        angle = 0.0
        radius = 0.0
        leg = float(length_mm)
        if leg <= 0:
            raise ValueError("tab length_mm must be > 0")
    else:
        angle = float(angle_deg)
        if not (0.0 < angle <= 180.0):
            raise ValueError("angle_deg must be in (0, 180]; a teardrop hem that "
                             "wraps past 180 degrees is out of scope")
        radius = float(inner_radius_mm) if inner_radius_mm is not None else t
        if radius < 0:
            raise ValueError("inner_radius_mm must be >= 0")
        ref = length_from
        if angle > 179.999 and ref != "tangent":
            raise ValueError(
                "a 180-degree bend has no virtual apex, so length_from='outer'/"
                "'inner' is undefined — dimension the return leg with "
                "length_from='tangent'")
        if ref == "outer":
            setback = outside_setback(angle, radius, t)
        elif ref == "inner":
            setback = inside_setback(angle, radius)
        elif ref == "tangent":
            setback = 0.0
        else:
            raise ValueError("length_from must be 'outer', 'inner' or 'tangent'")
        leg = float(length_mm) - setback
        if leg <= 0:
            raise ValueError(
                f"length_mm={length_mm:g} leaves no straight leg: the {ref} setback "
                f"for a {angle:g} deg bend at R={radius:g}, t={t:g} is "
                f"{setback:.4g} mm")
    if direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")

    fid = len(model["features"])
    rid = len(model["regions"])
    feature = {
        "id": fid, "region": rid, "parent": region_id, "kind": kind,
        "name": name or f"{kind}{fid + 1}",
        "a": list(a), "b": list(b), "m": list(m_local), "span": span,
        "angle_deg": angle, "inner_radius": radius, "direction": direction,
        "leg_tangent": leg, "length_mm": float(length_mm),
        "length_from": (length_from if kind != TAB else "tangent"),
        "k": None if k is None else float(k),
    }
    # The child's local +x runs from b to a, NOT from a to b. Reversing it is what
    # keeps every region's local frame right-handed against its own CCW polygon:
    # (edge direction, outward normal, surface normal) is a LEFT-handed triple, so a
    # child that inherited the edge direction unchanged would develop into the flat
    # pattern mirrored, and every outward normal on it — and therefore every splice
    # of a flange-on-a-flange — would point into the material instead of out of it.
    frame = _fold_frame(region, b, a, m_local, angle, radius, t, direction)
    model["regions"].append({
        "id": rid, "parent": region_id, "kind": kind, "feature": fid,
        "polygon": [[0.0, 0.0], [span, 0.0], [span, leg], [0.0, leg]],
        "origin3": list(frame["origin3"]), "e1_3": list(frame["e1_3"]),
        "e2_3": list(frame["e2_3"]), "n_3": list(frame["n_3"]),
    })
    model["features"].append(feature)
    return feature


def _fold_frame(region, origin_local, far_local, m_local, angle_deg, radius, t,
                direction):
    """3-D frame of the region a bend produces, plus the bend's revolve axis.

    ``origin_local`` is the attachment corner that becomes the child's local origin
    and ``far_local`` the other one; the child's local +x runs origin -> far.

    Working in the parent's local (w, n) plane — w the outward direction at the
    attachment, n the parent's normal — with the material at w <= 0 and the sheet
    occupying n in [0, t]:

      up:   the n=t face becomes concave, so the arc centre sits at (0, t + R) and
            the datum surface rides the OUTER radius R + t;
      down: the n=0 (datum) face becomes concave, the centre sits at (0, -R) and the
            datum surface rides the INNER radius R.

    Either way the tangent lines at both ends of the bend are perpendicular to the
    sheet, so every surface — inner, outer, neutral — leaves the bend at the same
    station. That is what lets the flat pattern be spliced at the attachment segment
    with no correction term."""
    w3 = _n3(_a3(_m3(tuple(region["e1_3"]), m_local[0]),
                 _m3(tuple(region["e2_3"]), m_local[1])))
    n3 = tuple(region["n_3"])
    e1_3 = _n3(_s3(region_point(region, far_local), region_point(region, origin_local)))
    base3 = region_point(region, origin_local)
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    if direction == "up":
        e2_3 = _a3(_m3(w3, c), _m3(n3, s))
        n_3 = _a3(_m3(w3, -s), _m3(n3, c))
        centre = _a3(base3, _m3(n3, t + radius))
        origin3 = _a3(base3, _a3(_m3(w3, (radius + t) * s),
                                 _m3(n3, (radius + t) * (1.0 - c))))
        axis = _x3(w3, n3)
    else:
        e2_3 = _a3(_m3(w3, c), _m3(n3, -s))
        n_3 = _a3(_m3(w3, s), _m3(n3, c))
        centre = _a3(base3, _m3(n3, -radius))
        origin3 = _a3(base3, _a3(_m3(w3, radius * s),
                                 _m3(n3, -radius * (1.0 - c))))
        axis = _x3(n3, w3)
    return {"origin3": origin3, "e1_3": e1_3, "e2_3": e2_3, "n_3": n_3,
            "axis_point": centre, "axis_dir": axis, "w3": w3}


def build_recipes(model, feature_id=None):
    """Emit the OCC build steps for the folded solid — the worker's whole job.

    Each step is a small dict the shim turns into a shape:
      ``{"op": "extrude", "profile": [3-D points], "vector": [dx, dy, dz]}``
      ``{"op": "revolve", "profile": [...], "axis_point": [...], "axis_dir": [...],
         "angle_deg": θ}``

    A bend is a revolve of the parent's end cross-section about the bend axis, which
    is exact for a constant-thickness sheet — no lofting, no approximation. A tab
    (θ = 0) degenerates to a single extrude with no revolve step, which is why tabs
    and flanges share one code path throughout the module.

    ``feature_id=None`` emits the base too; otherwise only that feature's steps, so
    the worker can fuse incrementally."""
    t = model["thickness"]
    steps = []
    if feature_id is None:
        base = model["regions"][0]
        steps.append({"op": "extrude", "name": "base",
                      "profile": [list(region_point(base, p))
                                  for p in base["polygon"]],
                      "vector": list(_m3(tuple(base["n_3"]), t))})
    for feature in model["features"]:
        if feature_id is not None and feature["id"] != feature_id:
            continue
        parent = next(r for r in model["regions"] if r["id"] == feature["parent"])
        child = next(r for r in model["regions"] if r["id"] == feature["region"])
        a, b = tuple(feature["a"]), tuple(feature["b"])
        n3 = tuple(parent["n_3"])
        A, B = region_point(parent, a), region_point(parent, b)
        cross = [A, B, _a3(B, _m3(n3, t)), _a3(A, _m3(n3, t))]
        if feature["angle_deg"] > 1e-9:
            frame = _fold_frame(parent, b, a, tuple(feature["m"]),
                                feature["angle_deg"], feature["inner_radius"], t,
                                feature["direction"])
            steps.append({"op": "revolve", "name": f"{feature['name']}_bend",
                          "profile": [list(p) for p in cross],
                          "axis_point": list(frame["axis_point"]),
                          "axis_dir": list(frame["axis_dir"]),
                          "angle_deg": feature["angle_deg"]})
        if feature["leg_tangent"] > 1e-9:
            o3 = tuple(child["origin3"])
            e1, cn = tuple(child["e1_3"]), tuple(child["n_3"])
            leg_cross = [o3, _a3(o3, _m3(e1, feature["span"])),
                         _a3(_a3(o3, _m3(e1, feature["span"])), _m3(cn, t)),
                         _a3(o3, _m3(cn, t))]
            steps.append({"op": "extrude", "name": f"{feature['name']}_leg",
                          "profile": [list(p) for p in leg_cross],
                          "vector": list(_m3(tuple(child["e2_3"]),
                                             feature["leg_tangent"]))})
    return steps


# --- 5. unfolding -------------------------------------------------------------

def _splice(outline, a, b, depth, tol=1e-6):
    """Push the boundary of ``outline`` outward between ``a`` and ``b`` by ``depth``.

    The flat footprint of a flange is a rectangle hanging off the segment it is bent
    from, so the flat pattern is the running union of the base profile and those
    rectangles. Because each rectangle sits outside the current boundary and spans a
    segment OF that boundary, the union reduces to a boundary detour — no general
    polygon-boolean needed, and the result is exact rather than tessellated.

    Returns the new outline, or None when the segment is no longer on the boundary,
    which means an earlier feature already consumed it (reported as an overlap)."""
    for i in range(len(outline)):
        P, Q = outline[i], outline[(i + 1) % len(outline)]
        d = _s2(Q, P)
        L = math.hypot(*d)
        if L < 1e-12:
            continue
        u = (d[0] / L, d[1] / L)
        params = []
        for T in (a, b):
            w = _s2(T, P)
            s = _d2(w, u)
            if math.hypot(w[0] - u[0] * s, w[1] - u[1] * s) > tol:
                params = None
                break
            if s < -tol or s > L + tol:
                params = None
                break
            params.append(s)
        if not params:
            continue
        s0, s1 = params
        if s0 > s1:
            # the caller's a->b runs against this boundary edge, so this is the
            # opposite side of a zero-width sliver rather than a real match
            continue
        m = _right2(u)
        p0 = _a2(P, _m2(u, s0))
        p1 = _a2(P, _m2(u, s1))
        detour = [p0, _a2(p0, _m2(m, depth)), _a2(p1, _m2(m, depth)), p1]
        return _dedupe(outline[:i + 1] + detour + outline[i + 1:])
    return None


def _proper_crossings(poly, tol=1e-7):
    """Non-adjacent boundary segments that actually cross (not merely touch).

    A crossing in the flat pattern means two features' footprints overlap — the
    part cannot be cut as one blank and wants a corner relief. Shared vertices
    between adjacent segments are normal and never reported."""
    n = len(poly)
    hits = []

    def _cross(o, p, q):
        return ((p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0]))

    for i in range(n):
        a1, a2 = poly[i], poly[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            b1, b2 = poly[j], poly[(j + 1) % n]
            d1 = _cross(a1, a2, b1)
            d2 = _cross(a1, a2, b2)
            d3 = _cross(b1, b2, a1)
            d4 = _cross(b1, b2, a2)
            if ((d1 > tol and d2 < -tol) or (d1 < -tol and d2 > tol)) and \
               ((d3 > tol and d4 < -tol) or (d3 < -tol and d4 > tol)):
                hits.append((i, j))
    return hits


def _bend_table_lookup(bend_table, thickness, radius, angle, tol=1e-3):
    """Find a shop bend-table row for this bend, if the caller supplied one.

    A real bend table is measured on a specific press with specific tooling, so when
    a row matches it OUTRANKS the K corpus and the result is labelled exact — the
    shop's number is the ground truth for that shop."""
    for row in bend_table or []:
        if abs(float(row.get("thickness_mm", thickness)) - thickness) > tol:
            continue
        if abs(float(row.get("inner_radius_mm", radius)) - radius) > tol:
            continue
        if abs(float(row.get("angle_deg", angle)) - angle) > 1e-6:
            continue
        return row
    return None


def unfold(model, k=None, bend_table=None):
    """Develop the part into its flat pattern and report every bend.

    Replays the feature tree in one global flat space: each region is placed
    relative to its parent, offset outward by that bend's allowance, and its
    footprint is spliced into the running outline. Nothing K-dependent is cached in
    the model, so calling this with a different K (or a shop bend table) genuinely
    re-develops the part rather than rescaling a stale answer.

    ``k`` pins the K-factor for every bend; ``bend_table`` is a list of measured rows
    ``{thickness_mm, inner_radius_mm, angle_deg, allowance_mm | deduction_mm}`` that
    wins over the corpus where it matches.

    Returns ``{ok, outline, holes, bend_lines, bends, regions, flat_bbox,
    flat_size, flat_area, blank_area, thickness, k_factor, fidelity, band_pct,
    developed_band_mm, warnings}``. ``fidelity`` is "exact" only when EVERY bend's K
    was supplied or table-derived; one corpus-defaulted bend makes the whole flat
    pattern a correlation, with ``developed_band_mm`` giving the actual millimetre
    spread of the flat extent over the K band — the honest way to say "your blank is
    this long, plus or minus this much"."""
    t = model["thickness"]
    material = model.get("material")
    placements = {0: {"origin": (0.0, 0.0), "e1": (1.0, 0.0), "e2": (0.0, 1.0)}}
    outline = _ccw(_dedupe([tuple(p) for p in model["regions"][0]["polygon"]]))
    bends, bend_lines, warnings = [], [], []
    region_flat = {0: [list(p) for p in outline]}
    exact = True
    band_lo = band_hi = 0.0

    def to_global(rid, q):
        pl = placements[rid]
        return _a2(pl["origin"], _a2(_m2(pl["e1"], q[0]), _m2(pl["e2"], q[1])))

    for feature in model["features"]:
        pid = feature["parent"]
        if pid not in placements:
            warnings.append(f"{feature['name']}: parent region {pid} was never "
                            "placed in the flat pattern")
            continue
        a_g = to_global(pid, tuple(feature["a"]))
        b_g = to_global(pid, tuple(feature["b"]))
        m_local = tuple(feature["m"])
        pl = placements[pid]
        m_g = _n2(_a2(_m2(pl["e1"], m_local[0]), _m2(pl["e2"], m_local[1])))

        angle = feature["angle_deg"]
        radius = feature["inner_radius"]
        if angle <= 1e-9:
            ba = 0.0
            kinfo = {"k": None, "source": "coplanar tab — no bend, no allowance",
                     "bend_class": None, "r_over_t": None,
                     "fidelity": "exact", "band_pct": 0.0}
            k_lo = k_hi = None
        else:
            row = _bend_table_lookup(bend_table, t, radius, angle)
            if row is not None:
                if row.get("allowance_mm") is not None:
                    ba = float(row["allowance_mm"])
                else:
                    ossb = outside_setback(angle, radius, t)
                    if ossb is None:
                        raise ValueError(
                            "a bend-table row for a 180-degree bend must give "
                            "allowance_mm; a deduction is undefined there")
                    ba = 2.0 * ossb - float(row["deduction_mm"])
                kinfo = {"k": None, "source": "measured shop bend table row",
                         "bend_class": None, "r_over_t": round(radius / t, 4),
                         "fidelity": "exact", "band_pct": 0.0}
                k_lo = k_hi = None
            else:
                kinfo = k_factor(t, radius, material=material,
                                 k=feature.get("k") if feature.get("k") is not None
                                 else k)
                ba = bend_allowance(angle, radius, t, kinfo["k"])
                if kinfo["fidelity"] != "exact":
                    exact = False
                    f = kinfo["band_pct"] / 100.0
                    k_lo = kinfo["k"] * (1.0 - f)
                    k_hi = kinfo["k"] * (1.0 + f)
                else:
                    k_lo = k_hi = None
        if k_lo is not None:
            band_lo += bend_allowance(angle, radius, t, k_lo) - ba
            band_hi += bend_allowance(angle, radius, t, k_hi) - ba

        leg = feature["leg_tangent"]
        depth = ba + leg
        new_outline = _splice(outline, a_g, b_g, depth)
        if new_outline is None:
            warnings.append(
                f"{feature['name']}: its footprint is no longer on the flat "
                "boundary — an earlier feature already occupies that segment, so "
                "the blank cannot be cut as drawn (corner relief needed)")
        else:
            outline = new_outline

        # mirrors attach(): the child's local origin is the b corner and its local
        # +x runs b -> a, which is what keeps the flat placement orientation-
        # preserving so a flange-on-a-flange splices outward rather than inward.
        child_origin = _a2(b_g, _m2(m_g, ba))
        child_e1 = _n2(_s2(a_g, b_g))
        placements[feature["region"]] = {
            "origin": child_origin, "e1": child_e1, "e2": m_g}
        child_poly = [list(_a2(child_origin, _a2(_m2(child_e1, q[0]),
                                                 _m2(m_g, q[1]))))
                      for q in [(0.0, 0.0), (feature["span"], 0.0),
                                (feature["span"], leg), (0.0, leg)]]
        region_flat[feature["region"]] = child_poly

        ossb = outside_setback(angle, radius, t) if angle > 1e-9 else 0.0
        bd = (bend_deduction(angle, radius, t, kinfo["k"])
              if (angle > 1e-9 and kinfo["k"] is not None) else None)
        record = {
            "id": feature["id"], "name": feature["name"], "kind": feature["kind"],
            "region": feature["region"], "parent": pid,
            "angle_deg": angle, "inner_radius_mm": radius,
            "direction": feature["direction"], "span_mm": feature["span"],
            "leg_tangent_mm": leg,
            "outer_length_mm": (leg + ossb) if ossb is not None else None,
            "k_factor": kinfo["k"], "k_source": kinfo["source"],
            "bend_class": kinfo["bend_class"], "r_over_t": kinfo["r_over_t"],
            "bend_allowance_mm": round(ba, 6),
            "bend_deduction_mm": None if bd is None else round(bd, 6),
            "outside_setback_mm": None if ossb is None else round(ossb, 6),
            "fidelity": kinfo["fidelity"], "band_pct": kinfo["band_pct"],
            # the tangent lines bracket the bend region; the bend LINE is the
            # centreline between them, which is where a brake operator sets the
            # backgauge and therefore what the DXF must carry.
            "tangent_start": [list(a_g), list(b_g)],
            "tangent_end": [list(_a2(a_g, _m2(m_g, ba))),
                            list(_a2(b_g, _m2(m_g, ba)))],
            "bend_line": [list(_a2(a_g, _m2(m_g, ba / 2.0))),
                          list(_a2(b_g, _m2(m_g, ba / 2.0)))],
            "flat_outward": list(m_g),
        }
        bends.append(record)
        if angle > 1e-9:
            bend_lines.append({"name": feature["name"],
                               "layer": (LAYER_BEND_UP if feature["direction"] == "up"
                                         else LAYER_BEND_DOWN),
                               "direction": feature["direction"],
                               "angle_deg": angle,
                               "points": record["bend_line"]})

    crossings = _proper_crossings(outline)
    if crossings:
        warnings.append(
            f"the flat outline self-intersects at {len(crossings)} place(s): two "
            "feature footprints overlap, so this blank cannot be cut flat without "
            "a corner relief")

    holes = []
    for h in model.get("holes", []):
        rid = h.get("region", 0)
        if rid not in placements:
            continue
        c = to_global(rid, (h["x"], h["y"]))
        holes.append({"x": c[0], "y": c[1], "diameter_mm": h["diameter_mm"],
                      "region": rid, "id": h.get("id")})

    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    area = abs(_signed_area(outline))
    hole_area = sum(math.pi * (h["diameter_mm"] / 2.0) ** 2 for h in holes)
    return {
        "ok": not warnings,
        "outline": [list(p) for p in outline],
        "holes": holes,
        "bend_lines": bend_lines,
        "bends": bends,
        "regions": {str(rid): poly for rid, poly in region_flat.items()},
        "flat_bbox": [min(xs), min(ys), max(xs), max(ys)],
        "flat_size": [max(xs) - min(xs), max(ys) - min(ys)],
        "flat_area_mm2": round(area, 6),
        "blank_area_mm2": round(area - hole_area, 6),
        "blank_volume_mm3": round((area - hole_area) * t, 6),
        "thickness": t,
        "material": material,
        "fidelity": "exact" if exact else "correlation",
        "band_pct": 0.0 if exact else K_BAND_PCT,
        "developed_band_mm": [round(band_lo, 6), round(band_hi, 6)],
        "warnings": warnings,
    }


def refold(flat, base_placement=None):
    """Rebuild the folded part FROM the flat pattern — the other half of the gate.

    This is deliberately not the inverse of :func:`build_recipes`. That function
    folds from the feature model, where each leg length is a stored input; this one
    derives every leg from the flat geometry, walking outward from each bend's
    attachment segment past its recorded bend allowance to the far edge of that
    region's flat polygon. So a wrong allowance, a wrong angle or a wrong direction
    lands the refolded solid somewhere the original is not, and the round-trip test
    catches it — whereas replaying the same stored numbers twice would prove
    nothing.

    ``base_placement`` is ``{origin3, e1_3, e2_3}`` for the base region, defaulting
    to the flat plane itself. Returns build steps in the same form as
    :func:`build_recipes`."""
    t = flat["thickness"]
    if base_placement is None:
        base_placement = {"origin3": (0.0, 0.0, 0.0), "e1_3": (1.0, 0.0, 0.0),
                          "e2_3": (0.0, 1.0, 0.0)}
    e1 = _n3(tuple(base_placement["e1_3"]))
    e2 = _n3(tuple(base_placement["e2_3"]))
    frames = {0: {"origin3": tuple(base_placement["origin3"]), "e1_3": e1,
                  "e2_3": e2, "n_3": _n3(_x3(e1, e2)),
                  "flat_origin": (0.0, 0.0), "flat_e1": (1.0, 0.0),
                  "flat_e2": (0.0, 1.0)}}
    steps = []
    base_poly = [tuple(p) for p in flat["regions"]["0"]]
    steps.append({"op": "extrude", "name": "base",
                  "profile": [list(_flat_to_3d(frames[0], p)) for p in base_poly],
                  "vector": list(_m3(frames[0]["n_3"], t))})

    for bend in flat["bends"]:
        pid = bend["parent"]
        if pid not in frames:
            continue
        parent = frames[pid]
        a_g, b_g = tuple(bend["tangent_start"][0]), tuple(bend["tangent_start"][1])
        m_g = _n2(tuple(bend["flat_outward"]))
        ba = bend["bend_allowance_mm"]
        angle = bend["angle_deg"]
        radius = bend["inner_radius_mm"]

        # leg length read back OFF the flat pattern, not off the model: the far edge
        # of the child's flat polygon, minus the bend allowance that precedes it.
        child_poly = [tuple(p) for p in flat["regions"][str(bend["region"])]]
        far = max(_d2(_s2(p, a_g), m_g) for p in child_poly)
        leg = far - ba

        A3 = _flat_to_3d(parent, a_g)
        B3 = _flat_to_3d(parent, b_g)
        w3 = _n3(_a3(_m3(parent["e1_3"], _d2(m_g, parent["flat_e1"])),
                     _m3(parent["e2_3"], _d2(m_g, parent["flat_e2"]))))
        n3 = parent["n_3"]
        span = math.hypot(*_s2(b_g, a_g))
        # same b -> a convention attach() uses, so a refolded flange-on-a-flange
        # lands on the same side of its parent as the modelled one
        pseudo = {"origin3": B3, "e1_3": _n3(_s3(A3, B3)), "e2_3": w3, "n_3": n3}
        frame = _fold_frame(pseudo, (0.0, 0.0), (span, 0.0),
                            (0.0, 1.0), angle, radius, t, bend["direction"])
        if angle > 1e-9:
            cross = [A3, B3, _a3(B3, _m3(n3, t)), _a3(A3, _m3(n3, t))]
            steps.append({"op": "revolve", "name": f"{bend['name']}_bend",
                          "profile": [list(p) for p in cross],
                          "axis_point": list(frame["axis_point"]),
                          "axis_dir": list(frame["axis_dir"]),
                          "angle_deg": angle})
        o3, ce1, cn, ce2 = (frame["origin3"], frame["e1_3"], frame["n_3"],
                            frame["e2_3"])
        if leg > 1e-9:
            leg_cross = [o3, _a3(o3, _m3(ce1, span)),
                         _a3(_a3(o3, _m3(ce1, span)), _m3(cn, t)),
                         _a3(o3, _m3(cn, t))]
            steps.append({"op": "extrude", "name": f"{bend['name']}_leg",
                          "profile": [list(p) for p in leg_cross],
                          "vector": list(_m3(ce2, leg))})
        frames[bend["region"]] = {
            "origin3": o3, "e1_3": ce1, "e2_3": ce2, "n_3": cn,
            "flat_origin": _a2(b_g, _m2(m_g, ba)),
            "flat_e1": _n2(_s2(a_g, b_g)), "flat_e2": m_g}
    return steps


def _flat_to_3d(frame, q):
    d = _s2(tuple(q), frame["flat_origin"])
    return _a3(frame["origin3"],
               _a3(_m3(frame["e1_3"], _d2(d, frame["flat_e1"])),
                   _m3(frame["e2_3"], _d2(d, frame["flat_e2"]))))


# --- 6. the layered DXF -------------------------------------------------------

LAYER_CUT = "CUT"
LAYER_BEND_UP = "BEND_UP"
LAYER_BEND_DOWN = "BEND_DOWN"
DXF_LAYERS = (LAYER_CUT, LAYER_BEND_UP, LAYER_BEND_DOWN)

# AutoCAD colour indices: white/black for the cut profile, red for up-bends, cyan
# for down-bends — the convention most nesting software and brake CAM expects.
_LAYER_COLOURS = {LAYER_CUT: 7, LAYER_BEND_UP: 1, LAYER_BEND_DOWN: 4}


def _g(code, value):
    return f"{code}\n{value}\n"


def to_dxf(flat, layers=None):
    """Serialise a flat pattern as layered DXF R12 ASCII text.

    R12 rather than a modern release on purpose: it is the dialect every laser,
    punch and waterjet front end reads without complaint, and the entity set
    (POLYLINE/VERTEX/SEQEND, LINE, CIRCLE) is small enough to be written and
    re-parsed without a library.

    Three layers, because a flat pattern without them is not a shop deliverable:
    ``CUT`` carries the closed outer profile and every hole; ``BEND_UP`` and
    ``BEND_DOWN`` carry one centreline per bend, so an operator can read the fold
    direction off the print instead of inferring it. The outline is emitted with the
    R12 closed-polyline flag set, so an importer closes the profile itself rather
    than relying on a duplicated last vertex."""
    layers = layers or DXF_LAYERS
    out = []
    out.append(_g(0, "SECTION") + _g(2, "HEADER"))
    out.append(_g(9, "$ACADVER") + _g(1, "AC1009"))
    out.append(_g(9, "$INSUNITS") + _g(70, 4))          # 4 == millimetres
    out.append(_g(0, "ENDSEC"))

    out.append(_g(0, "SECTION") + _g(2, "TABLES"))
    out.append(_g(0, "TABLE") + _g(2, "LAYER") + _g(70, len(layers)))
    for name in layers:
        out.append(_g(0, "LAYER") + _g(2, name) + _g(70, 0)
                   + _g(62, _LAYER_COLOURS.get(name, 7)) + _g(6, "CONTINUOUS"))
    out.append(_g(0, "ENDTAB") + _g(0, "ENDSEC"))

    out.append(_g(0, "SECTION") + _g(2, "ENTITIES"))
    out.append(_polyline(flat["outline"], LAYER_CUT))
    for hole in flat.get("holes", []):
        out.append(_g(0, "CIRCLE") + _g(8, LAYER_CUT)
                   + _g(10, f"{hole['x']:.6f}") + _g(20, f"{hole['y']:.6f}")
                   + _g(30, "0.0")
                   + _g(40, f"{hole['diameter_mm'] / 2.0:.6f}"))
    for line in flat.get("bend_lines", []):
        (x0, y0), (x1, y1) = line["points"]
        out.append(_g(0, "LINE") + _g(8, line["layer"])
                   + _g(10, f"{x0:.6f}") + _g(20, f"{y0:.6f}") + _g(30, "0.0")
                   + _g(11, f"{x1:.6f}") + _g(21, f"{y1:.6f}") + _g(31, "0.0"))
    out.append(_g(0, "ENDSEC"))
    out.append(_g(0, "EOF"))
    return "".join(out)


def _polyline(points, layer):
    body = (_g(0, "POLYLINE") + _g(8, layer) + _g(66, 1) + _g(70, 1)
            + _g(10, "0.0") + _g(20, "0.0") + _g(30, "0.0"))
    for x, y in points:
        body += (_g(0, "VERTEX") + _g(8, layer) + _g(10, f"{x:.6f}")
                 + _g(20, f"{y:.6f}") + _g(30, "0.0"))
    return body + _g(0, "SEQEND") + _g(8, layer)


def parse_dxf(text):
    """Read a DXF back into ``{layers, polylines, lines, circles}``.

    Deliberately part of the module rather than the test file: "the file was
    written" is not the claim worth making about a shop deliverable — "the file
    re-imports as a closed profile on the right layer" is, and a claim worth testing
    is worth shipping the reader for."""
    codes = []
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        try:
            codes.append((int(lines[i].strip()), lines[i + 1].strip()))
        except ValueError:
            continue
    out = {"layers": [], "polylines": [], "lines": [], "circles": []}
    i = 0
    while i < len(codes):
        code, val = codes[i]
        if code == 0 and val == "LAYER":
            j, name, colour = i + 1, None, None
            while j < len(codes) and codes[j][0] != 0:
                if codes[j][0] == 2:
                    name = codes[j][1]
                elif codes[j][0] == 62:
                    colour = int(codes[j][1])
                j += 1
            if name:
                out["layers"].append({"name": name, "color": colour})
            i = j
            continue
        if code == 0 and val == "POLYLINE":
            j, layer, closed = i + 1, None, False
            while j < len(codes) and codes[j][0] != 0:
                if codes[j][0] == 8:
                    layer = codes[j][1]
                elif codes[j][0] == 70:
                    closed = bool(int(codes[j][1]) & 1)
                j += 1
            verts = []
            while j < len(codes) and codes[j] != (0, "SEQEND"):
                if codes[j] == (0, "VERTEX"):
                    k, x, y = j + 1, None, None
                    while k < len(codes) and codes[k][0] != 0:
                        if codes[k][0] == 10:
                            x = float(codes[k][1])
                        elif codes[k][0] == 20:
                            y = float(codes[k][1])
                        k += 1
                    if x is not None and y is not None:
                        verts.append((x, y))
                    j = k
                    continue
                j += 1
            out["polylines"].append({"layer": layer, "closed": closed,
                                     "points": verts})
            i = j
            continue
        if code == 0 and val in ("LINE", "CIRCLE"):
            j, rec = i + 1, {"layer": None}
            while j < len(codes) and codes[j][0] != 0:
                c, v = codes[j]
                if c == 8:
                    rec["layer"] = v
                elif c in (10, 20, 11, 21, 40):
                    rec[c] = float(v)
                j += 1
            if val == "LINE":
                out["lines"].append({"layer": rec["layer"],
                                     "points": [(rec.get(10, 0.0), rec.get(20, 0.0)),
                                                (rec.get(11, 0.0), rec.get(21, 0.0))]})
            else:
                out["circles"].append({"layer": rec["layer"],
                                       "center": (rec.get(10, 0.0), rec.get(20, 0.0)),
                                       "radius": rec.get(40, 0.0)})
            i = j
            continue
        i += 1
    return out


# --- 7. the DFM screen --------------------------------------------------------
#
# One implementation, two front doors: `sheet_check` in the worker builds bend/hole
# descriptors off a real model and calls check_bends(); dfx.dfm_check(process=
# 'sheet') passes descriptors straight through to the same function. There is no
# second copy of these rules anywhere.

def check_bends(bends, thickness_mm, material=None, holes=None, interferences=None,
                min_flange_t=MIN_FLANGE_T, hole_to_bend_t=HOLE_TO_BEND_T,
                extra_findings=None):
    """Screen a set of bends for press-brake manufacturability.

    ``bends`` are ``{id, angle_deg, inner_radius_mm, outer_length_mm |
    leg_tangent_mm, direction}``; ``holes`` are ``{id, diameter_mm, bend,
    distance_to_bend_mm}`` (edge-of-hole to the nearest bend TANGENT line, not to
    the bend centreline — the tangent is where the material starts to move);
    ``interferences`` are ``{a, b, volume_mm3}`` pairs the caller found by
    intersecting the refolded solids.

    The four rules, and where each number comes from:

    * ``min_bend_radius`` — R below the material's minimum cracks the outer fibre.
      Corpus value, per material; an unknown material degrades to a class fallback
      and says so rather than skipping the rule silently.
    * ``min_flange_length`` — an outer leg shorter than ``4t + R`` has nothing left
      to sit on the die shoulder and dives into the vee.
    * ``hole_to_bend`` — a hole whose edge is nearer the tangent than ``2t + R``
      distorts into an egg as the material draws through the bend.
    * ``refold_collision`` — two features that occupy the same space once folded.
      This one is geometric fact, not a rule of thumb, so it is always a hard fail.

    The first three are shop rules of thumb: ``fidelity="correlation"``. They are
    thresholds, not measurements, so no scatter band applies and ``band_pct`` is
    None — rank and gate with them, do not read them as predictions. Returns
    ``{ok, findings, rules, thickness_mm, material, min_bend_radius_mm, fidelity,
    band_pct}``; never raises on a thin corpus."""
    t = float(thickness_mm)
    findings = list(extra_findings or [])
    mbr = min_bend_radius(t, material)
    if not mbr["known"] and material:
        findings.append({
            "code": "material_not_in_corpus", "severity": "info",
            "message": f"{mbr['source']} — supply a bend table or a measured "
                       "minimum radius to make this rule authoritative"})

    for b in bends:
        bid = b.get("id", b.get("name"))
        radius = float(b.get("inner_radius_mm", 0.0))
        angle = float(b.get("angle_deg", 0.0))
        if angle <= 1e-9:
            continue      # a coplanar tab has no bend to screen
        # A hem is not air-bent. It is bent to ~30 degrees and then FLATTENED in a
        # hemming die, so neither the air-bend minimum radius (a closed hem's radius
        # is deliberately below it) nor the die-shoulder flange minimum describes
        # it. Screening it with the air-bend rules would fail every hem ever drawn,
        # which is worse than not screening it — so it gets its own rule and says
        # which process it assumed.
        is_hem = angle >= 179.0
        if is_hem:
            if float(b.get("leg_tangent_mm") or 0.0) < 4.0 * t - 1e-9:
                findings.append({
                    "code": "min_hem_length", "severity": "fail", "bend": bid,
                    "value_mm": round(float(b.get("leg_tangent_mm") or 0.0), 4),
                    "limit_mm": round(4.0 * t, 4),
                    "message": f"hem return {float(b.get('leg_tangent_mm') or 0):g} "
                               f"mm is under the {4.0 * t:g} mm minimum (4t) — a "
                               "shorter return cannot be gripped by the hemming die"})
            findings.append({
                "code": "hem_process_assumed", "severity": "info", "bend": bid,
                "message": "screened as a two-hit hem (bend, then flatten), so the "
                           "air-bend minimum radius and flange rules do not apply"})
            continue
        if radius < mbr["radius_mm"] - 1e-9:
            findings.append({
                "code": "min_bend_radius", "severity": "fail", "bend": bid,
                "value_mm": round(radius, 4), "limit_mm": mbr["radius_mm"],
                "message": f"inside radius {radius:g} mm is under the "
                           f"{mbr['radius_mm']:g} mm minimum for "
                           f"{material or 'this material'} at t={t:g} "
                           f"({mbr['multiple_of_t']:g}t) — the outer fibre cracks"})
        leg = b.get("outer_length_mm")
        if leg is None:
            leg = b.get("leg_tangent_mm")
        if leg is not None:
            limit = min_flange_t * t + radius
            if float(leg) < limit - 1e-9:
                findings.append({
                    "code": "min_flange_length", "severity": "fail", "bend": bid,
                    "value_mm": round(float(leg), 4), "limit_mm": round(limit, 4),
                    "message": f"outer flange {float(leg):g} mm is under the "
                               f"{limit:g} mm minimum ({min_flange_t:g}t + R) — the "
                               "leg has no die shoulder to sit on and will dive "
                               "into the vee"})

    radius_by_bend = {str(b.get("id", b.get("name"))): float(
        b.get("inner_radius_mm", 0.0)) for b in bends}
    for h in holes or []:
        dist = h.get("distance_to_bend_mm")
        if dist is None:
            continue
        radius = radius_by_bend.get(str(h.get("bend")), 0.0)
        limit = hole_to_bend_t * t + radius
        if float(dist) < limit - 1e-9:
            findings.append({
                "code": "hole_to_bend", "severity": "fail",
                "hole": h.get("id"), "bend": h.get("bend"),
                "value_mm": round(float(dist), 4), "limit_mm": round(limit, 4),
                "message": f"hole edge is {float(dist):g} mm from the bend tangent, "
                           f"under the {limit:g} mm minimum ({hole_to_bend_t:g}t + "
                           "R) — it will draw into an oval"})

    for pair in interferences or []:
        findings.append({
            "code": "refold_collision", "severity": "fail",
            "features": [pair.get("a"), pair.get("b")],
            "value_mm3": round(float(pair.get("volume_mm3", 0.0)), 4),
            "message": f"{pair.get('a')} and {pair.get('b')} overlap by "
                       f"{float(pair.get('volume_mm3', 0.0)):.4g} mm^3 once folded "
                       "— the part cannot be formed as modelled"})

    fails = [f for f in findings if f.get("severity") == "fail"]
    return {
        "ok": not fails,
        "findings": findings,
        "fail_count": len(fails),
        "thickness_mm": t,
        "material": material,
        "min_bend_radius_mm": mbr["radius_mm"],
        "min_bend_radius_source": mbr["source"],
        "rules": {
            "min_bend_radius_mm": mbr["radius_mm"],
            "min_flange_outer_mm": f"{min_flange_t:g}*t + R",
            "hole_to_bend_mm": f"{hole_to_bend_t:g}*t + R",
        },
        # press-brake rules of thumb: thresholds for ranking and gating, not
        # measured quantities, so no scatter band applies (same contract dfa_check
        # carries for its Boothroyd index).
        "fidelity": "correlation",
        "band_pct": None,
    }


def check(model, flat=None, interferences=None, **kw):
    """DFM screen for a whole sheet-metal model — the front door ``sheet_check`` uses.

    Develops the part (or reuses a supplied ``flat``) so the screen sees the same
    bend allowances and the same flat pattern the DXF will carry, converts the
    features and holes into the descriptors :func:`check_bends` grades, and folds in
    anything the unfold itself complained about (a footprint overlap is a
    manufacturability finding, not a warning to be dropped on the floor).

    Returns :func:`check_bends`' dict plus ``flat_size``, ``blank_area_mm2`` and the
    K-factor actually used, so a single call answers both "can this be made" and
    "how much material does it take"."""
    flat = flat if flat is not None else unfold(model)
    extra = [{"code": "flat_pattern_warning", "severity": "fail", "message": w}
             for w in flat.get("warnings", [])]
    holes = []
    for h in flat.get("holes", []):
        nearest, best = None, None
        for bend in flat["bends"]:
            if bend["angle_deg"] <= 1e-9:
                continue
            d = _point_to_segment((h["x"], h["y"]), bend["tangent_start"])
            d = min(d, _point_to_segment((h["x"], h["y"]), bend["tangent_end"]))
            d -= h["diameter_mm"] / 2.0     # edge of hole, not its centre
            if best is None or d < best:
                best, nearest = d, bend["name"]
        if nearest is not None:
            holes.append({"id": h.get("id"), "diameter_mm": h["diameter_mm"],
                          "bend": nearest, "distance_to_bend_mm": best})
    result = check_bends(
        [dict(b, id=b["name"]) for b in flat["bends"]], model["thickness"],
        material=model.get("material"), holes=holes, interferences=interferences,
        extra_findings=extra, **kw)
    result["flat_size"] = flat["flat_size"]
    result["blank_area_mm2"] = flat["blank_area_mm2"]
    result["k_factors"] = [{"bend": b["name"], "k": b["k_factor"],
                            "source": b["k_source"]} for b in flat["bends"]]
    return result


def _point_to_segment(p, seg):
    a, b = tuple(seg[0]), tuple(seg[1])
    d = _s2(b, a)
    L2 = _d2(d, d)
    if L2 < 1e-18:
        return math.hypot(*_s2(p, a))
    s = max(0.0, min(1.0, _d2(_s2(p, a), d) / L2))
    return math.hypot(*_s2(p, _a2(a, _m2(d, s))))

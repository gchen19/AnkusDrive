"""
Inspection artifacts (issue #232): ballooned characteristics, the inspection plan,
and an AS9102-Form-3-*shaped* first-article report.

A drawing that passes ``drawing_gate`` (complete) and ``drawing_legibility``
(readable) still isn't an inspectable one. Production parts get *measured*, and the
quality engineer's inputs — a ballooned print, a numbered characteristic list, a
measurement method per characteristic, and an FAI form — are what this module
produces from the dimension set that already exists on the page.

FreeCAD-free on purpose, exactly like :mod:`driftpin.drawing_gate`: everything here
is descriptor arithmetic over the same dim descriptors the completeness gate
consumes, so it unit-tests on the host interpreter in milliseconds. The worker shim
(``balloon_drawing`` / ``inspection_plan`` / ``fai_report``) only reads descriptors
off the real page and hands them here.

Three ideas carry the module:

* **Balloon numbers are an identity, not an ordinal.** Once a characteristic owns
  balloon 4, it keeps balloon 4 for the life of the drawing — a revision that adds a
  dimension appends balloon N+1 rather than renumbering the print an inspector has
  already written measurements against. :func:`assign_balloons` takes the previously
  assigned map and only allocates for what is new.
* **Method follows resolution, not vibes.** The instrument for a characteristic is
  chosen by the gauge-maker's 10:1 rule — the gauge must resolve a tenth of the
  tolerance band — walked down a per-family instrument ladder. The required
  resolution is exact arithmetic; the ladder is a documented shop convention, so the
  plan labels itself ``fidelity="correlation"``.
* **An unmeasurable characteristic is a finding, not a blank.** A dimension with no
  tolerance can't pass or fail, so it reports ``status="not_evaluated"`` with a
  reason instead of silently reading as OK.

Nothing here certifies anything: :func:`fai_rows` produces the *shape* of an AS9102
Form 3, with AS9102 field names, so a real form can be filled from it. It is not a
qualified AS9102 submission and every export says so.
"""

import math

# --- characteristic kinds -----------------------------------------------------

DIMENSIONAL = "dimension"     # a placed drawing dimension (size or location)
GEOMETRIC = "gdt"             # a feature control frame (GD&T)
NOTE = "note"                 # a manufacturing note credited as coverage

_KIND_RANK = {DIMENSIONAL: 0, GEOMETRIC: 1, NOTE: 2}

# Dimension types, in the order they read on a print (sizes before locations).
_TYPE_RANK = {
    "Diameter": 0, "Radius": 1, "Angle": 2,
    "Distance": 3, "DistanceX": 4, "DistanceY": 5,
}

# GD&T controls that need a datum reference frame to mean anything — those are CMM
# work. The rest are form controls, measurable on a surface plate with an indicator.
_DATUM_CONTROLS = {
    "position", "perpendicularity", "parallelism", "angularity",
    "concentricity", "symmetry", "runout", "total_runout",
    "profile_line", "profile_surface",
}
_FORM_CONTROLS = {"flatness", "straightness", "circularity", "cylindricity"}

ANGULAR_TYPES = {"Angle"}


# --- instrument selection -----------------------------------------------------
#
# The gauge-maker's rule: an instrument should resolve ~1/10 of the tolerance band
# it is asked to verify (10:1; some shops run 4:1 or 5:1 — `ratio` is settable).
# Each family is a ladder ordered coarse -> fine; we take the FIRST rung that
# resolves the requirement, so a loose feature isn't sent to the CMM and a tight one
# can't be signed off with a caliper.
#
# Resolutions are the everyday shop instrument's resolution, mm (deg for angles).
# They are conventions, not physics — hence fidelity="correlation" on the plan.

_LADDERS = {
    # linear sizes and locations
    "length": [
        ("steel rule", 0.5),
        ("caliper", 0.02),        # vernier graduation; a digital reads 0.01
        ("micrometer", 0.001),    # vernier/digital outside mic
        ("CMM", 0.0005),
    ],
    # an external diameter or radius — a mic closes on it directly
    "shaft": [
        ("caliper", 0.02),
        ("micrometer", 0.001),
        ("CMM", 0.0005),
    ],
    # an internal diameter — a mic can't reach inside; pin/bore gauges can
    "bore": [
        ("caliper", 0.02),
        ("pin gauge", 0.005),     # a pin set steps ~0.01; ~0.005 read as a variable
        ("bore gauge", 0.001),    # dial bore gauge — the shop answer for an H7 hole
        ("CMM", 0.0005),
    ],
    # angles, in degrees
    "angle": [
        ("protractor", 0.5),
        ("sine bar", 0.02),
        ("CMM", 0.005),
    ],
    # GD&T form controls — datum-free, so a surface plate does it
    "gdt_form": [
        ("surface plate + dial indicator", 0.005),
        ("CMM", 0.0005),
    ],
    # GD&T controls referencing a datum frame — establish the frame, then measure
    "gdt_datum": [
        ("CMM", 0.0005),
    ],
}

GAUGE_RATIO = 10.0   # gauge-maker's rule: resolve 1/10 of the band


def _family(char):
    """Which instrument ladder a characteristic is measured on."""
    if char.get("kind") == GEOMETRIC:
        control = str(char.get("gdt", {}).get("control", ""))
        if control in _FORM_CONTROLS and not char.get("gdt", {}).get("datums"):
            return "gdt_form"
        return "gdt_datum"
    ctype = str(char.get("type", ""))
    if ctype in ANGULAR_TYPES:
        return "angle"
    if ctype in ("Diameter", "Radius"):
        # `internal` is supplied by the worker from the enumerated features (a hole/
        # bore/counterbore is internal). Unknown -> assume external, which is the
        # conservative default: it never recommends a bore gauge for a shaft.
        return "bore" if char.get("internal") else "shaft"
    return "length"


def tol_width(char):
    """A characteristic's total tolerance band, or ``None`` when it carries none.

    Dimensional: ``tol_plus - tol_minus`` (signed deviations, plus >= minus).
    Geometric: the tolerance zone itself — the whole zone is the band the
    instrument must resolve."""
    if char.get("kind") == GEOMETRIC:
        zone = char.get("gdt", {}).get("zone")
        return abs(float(zone)) if zone is not None else None
    plus, minus = char.get("tol_plus"), char.get("tol_minus")
    if plus is None and minus is None:
        return None
    return abs(float(plus or 0.0) - float(minus or 0.0))


def suggest_method(char, *, ratio=GAUGE_RATIO):
    """Pick a measurement instrument for one characteristic.

    Returns ``{method, family, required_resolution, resolution, reason}``.
    ``required_resolution`` is ``band / ratio`` — exact arithmetic; the mapping onto
    a named instrument is the shop convention the plan labels ``correlation``.

    An untoleranced characteristic gets the family's coarse rung with a reason
    saying so: it is inspected against the drawing's general tolerance note, and no
    finer instrument is justified by anything on the print."""
    fam = _family(char)
    ladder = _LADDERS[fam]
    band = tol_width(char)
    unit = "deg" if fam == "angle" else "mm"
    if band is None:
        method, res = ladder[0]
        return {"method": method, "family": fam, "required_resolution": None,
                "resolution": res, "unit": unit,
                "reason": "no explicit tolerance — inspect to the general tolerance "
                          f"note; {method} suffices"}
    if band <= 0.0:
        # A basic/exact dimension (zero band) can only be verified against a zone,
        # not a band — that is what its controlling GD&T frame is for.
        method, res = ladder[-1]
        return {"method": method, "family": fam, "required_resolution": 0.0,
                "resolution": res, "unit": unit,
                "reason": "zero tolerance band (basic dimension) — verify through "
                          "its controlling geometric tolerance"}
    required = band / float(ratio)
    for method, res in ladder:
        if res <= required + 1e-12:
            return {"method": method, "family": fam,
                    "required_resolution": round(required, 6),
                    "resolution": res, "unit": unit,
                    "reason": f"band {band:g} {unit} needs {required:.4g} {unit} "
                              f"resolution at {ratio:g}:1; {method} resolves {res:g}"}
    method, res = ladder[-1]
    return {"method": method, "family": fam,
            "required_resolution": round(required, 6),
            "resolution": res, "unit": unit,
            "reason": f"band {band:g} {unit} needs {required:.4g} {unit} resolution "
                      f"at {ratio:g}:1 — finer than {method} ({res:g}); the finest "
                      "available instrument is recommended, gauge R&R will be poor"}


# --- characteristic construction ---------------------------------------------

def _label(char):
    """The human characteristic designator printed next to the balloon."""
    if char.get("kind") == GEOMETRIC:
        g = char.get("gdt", {})
        refs = "|".join(str(d) for d in (g.get("datums") or []))
        body = f"{g.get('control')} {float(g.get('zone', 0.0)):g}"
        return f"{body}|{refs}" if refs else body
    if char.get("kind") == NOTE:
        return str(char.get("text", "note"))
    prefix = {"Diameter": "Ø", "Radius": "R"}.get(str(char.get("type", "")), "")
    suffix = "°" if str(char.get("type", "")) in ANGULAR_TYPES else ""
    return f"{prefix}{float(char.get('nominal', 0.0)):.2f}{suffix}"


def _limits(char):
    """(lower, upper) limits for a dimensional characteristic, or (None, None)."""
    if char.get("kind") != DIMENSIONAL:
        return (None, None)
    plus, minus = char.get("tol_plus"), char.get("tol_minus")
    if plus is None and minus is None:
        return (None, None)
    nom = float(char.get("nominal", 0.0))
    return (nom + float(minus or 0.0), nom + float(plus or 0.0))


def characteristics(dims=None, gdt=None, notes=None, *, ratio=GAUGE_RATIO):
    """Build the characteristic list from a page's descriptors.

    ``dims`` are drawing-gate dim descriptors (``{name, type, value, plus, minus,
    view, internal}``); ``gdt`` are feature-control-frame descriptors
    (``{name, control, zone, datums, feature, view}``); ``notes`` are feature notes
    (``{name, feature, text}``) — a note is a characteristic too when it is the only
    thing documenting a feature, but it is inspected by attribute, not by gauge.

    Returns the list unballooned and unsorted-by-balloon; run it through
    :func:`assign_balloons` to number it."""
    out = []
    for d in (dims or []):
        char = {
            "id": str(d.get("name")),
            "kind": DIMENSIONAL,
            "type": str(d.get("type", "Distance")),
            "nominal": round(float(d.get("value", 0.0)), 6),
            "view": str(d.get("view", "") or ""),
            "internal": bool(d.get("internal")) if d.get("internal") is not None
            else None,
            "unit": "deg" if str(d.get("type", "")) in ANGULAR_TYPES else "mm",
        }
        for src, dst in (("plus", "tol_plus"), ("minus", "tol_minus")):
            if d.get(src) is not None:
                char[dst] = round(float(d[src]), 6)
        lo, hi = _limits(char)
        char["lower"] = None if lo is None else round(lo, 6)
        char["upper"] = None if hi is None else round(hi, 6)
        char["characteristic"] = _label(char)
        char["method"] = suggest_method(char, ratio=ratio)
        out.append(char)
    for g in (gdt or []):
        char = {
            "id": str(g.get("name")),
            "kind": GEOMETRIC,
            "type": str(g.get("control", "")),
            "nominal": 0.0,
            "unit": "mm",
            "view": str(g.get("view", "") or ""),
            "feature": g.get("feature"),
            "gdt": {"control": str(g.get("control", "")),
                    "zone": round(float(g.get("zone", 0.0)), 6),
                    "datums": [str(x) for x in (g.get("datums") or [])],
                    "mmc_bonus": round(float(g.get("mmc_bonus", 0.0) or 0.0), 6)},
            "lower": 0.0,
            "upper": round(float(g.get("zone", 0.0)), 6),
        }
        char["characteristic"] = _label(char)
        char["method"] = suggest_method(char, ratio=ratio)
        out.append(char)
    for n in (notes or []):
        char = {
            "id": str(n.get("name", n.get("feature"))),
            "kind": NOTE,
            "type": "note",
            "feature": n.get("feature"),
            "text": str(n.get("text", "")),
            "unit": "",
            "view": "",
            "nominal": None, "lower": None, "upper": None,
        }
        char["characteristic"] = _label(char)
        char["method"] = {"method": "attribute / visual", "family": "note",
                          "required_resolution": None, "resolution": None,
                          "unit": "",
                          "reason": "documented by note — verified by attribute "
                                    "against the CAD model / profile table"}
        out.append(char)
    return out


# --- balloon numbering --------------------------------------------------------

def _fresh_sort_key(char):
    """Deterministic reading order for characteristics being numbered for the first
    time: grouped by view, sizes before locations before GD&T, then by value, then by
    source id so the order never depends on dict/set iteration.

    Characteristics not anchored to a view (a page-level control frame, a general
    note) sort LAST rather than first — a ballooned print reads through the views
    before it reaches the page notes."""
    view = str(char.get("view", ""))
    return (
        (view == "", view),
        _KIND_RANK.get(char.get("kind"), 9),
        _TYPE_RANK.get(str(char.get("type", "")), 8),
        float(char.get("nominal") if char.get("nominal") is not None else 0.0),
        str(char.get("id", "")),
    )


def assign_balloons(chars, existing=None):
    """Number the characteristics, preserving any numbers already issued.

    ``existing`` maps source id -> balloon number from a previous run (the worker
    reads it back off the page). Characteristics already in it keep their number;
    the rest are numbered above the high-water mark of every number the drawing has
    EVER issued, in :func:`_fresh_sort_key` order. That is what makes the operation
    idempotent — re-running on an unchanged page reassigns nothing — and what keeps
    a revision from renumbering balloons an inspector has already recorded
    measurements against.

    The high-water mark is taken over all of ``existing``, not just the
    characteristics still on the page: when a dimension is deleted its balloon
    RETIRES rather than being recycled, so an inspection record written against
    balloon 7 of rev A can never be misread as the different feature that would
    otherwise inherit balloon 7 in rev B.

    Mutates each char in place (setting ``balloon``) and returns
    ``{characteristics, assigned, kept, retired, next_balloon}`` with
    ``characteristics`` sorted by balloon."""
    existing = {str(k): int(v) for k, v in (existing or {}).items()}
    present = {str(c.get("id")) for c in chars}
    taken = set()
    kept = []
    fresh = []
    for c in chars:
        num = existing.get(str(c.get("id")))
        if num is not None and num not in taken:
            c["balloon"] = num
            taken.add(num)
            kept.append(c)
        else:
            fresh.append(c)
    # never reissue a number, including one belonging to a since-deleted dimension
    nxt = max([*taken, *existing.values(), 0]) + 1
    assigned = []
    for c in sorted(fresh, key=_fresh_sort_key):
        while nxt in taken:
            nxt += 1
        c["balloon"] = nxt
        taken.add(nxt)
        assigned.append({"balloon": nxt, "id": str(c.get("id")),
                         "characteristic": c.get("characteristic")})
        nxt += 1
    retired = sorted(n for k, n in existing.items() if k not in present)
    ordered = sorted(chars, key=lambda c: c["balloon"])
    return {"characteristics": ordered, "assigned": assigned, "kept": len(kept),
            "retired": retired,
            "next_balloon": max([*taken, *existing.values(), 0]) + 1}


def balloon_map(chars):
    """``{source id: balloon}`` — the stamp the worker persists on the page."""
    return {str(c["id"]): int(c["balloon"]) for c in chars if c.get("balloon")}


# --- the plan -----------------------------------------------------------------

def inspection_plan(dims=None, gdt=None, notes=None, existing=None, *,
                    ratio=GAUGE_RATIO):
    """The full characteristic list, ballooned and with a method per row.

    Returns ``{ok, characteristics, count, by_method, fidelity, band_pct, basis,
    unmeasurable, retired, next_balloon}``. ``ok`` is False when any characteristic
    can't be inspected as drawn (``unmeasurable`` lists them with a reason) — an
    untoleranced size the inspector has no limits for, or a tolerance finer than any
    instrument on its ladder. ``retired`` lists balloon numbers from a previous
    revision whose characteristic is gone; they are never reissued."""
    chars = characteristics(dims, gdt, notes, ratio=ratio)
    numbered = assign_balloons(chars, existing)
    chars = numbered["characteristics"]

    unmeasurable = []
    by_method = {}
    for c in chars:
        m = c["method"]["method"]
        by_method[m] = by_method.get(m, 0) + 1
        if c["kind"] == DIMENSIONAL and tol_width(c) is None:
            unmeasurable.append({
                "balloon": c["balloon"], "id": c["id"], "code": "no_tolerance",
                "reason": f"{c['characteristic']} carries no tolerance — the "
                          "inspector has no limits to accept or reject against",
            })
        elif (c["method"]["required_resolution"] is not None
              and c["method"]["resolution"] is not None
              and c["method"]["resolution"] > c["method"]["required_resolution"]
                                              + 1e-12):
            unmeasurable.append({
                "balloon": c["balloon"], "id": c["id"], "code": "no_instrument",
                "reason": c["method"]["reason"],
            })
    return {
        "ok": not unmeasurable,
        "characteristics": chars,
        "count": len(chars),
        "by_method": dict(sorted(by_method.items())),
        "unmeasurable": unmeasurable,
        "retired": numbered["retired"],
        "next_balloon": numbered["next_balloon"],
        # the required resolution is exact; the instrument mapping is convention
        "fidelity": "correlation",
        "band_pct": 0.0,
        "basis": f"gauge-maker's {ratio:g}:1 resolution rule over a per-family "
                 "instrument ladder (shop convention, not a standard)",
    }


# --- evaluation ---------------------------------------------------------------

def evaluate(chars, results=None):
    """Judge supplied measurements against each characteristic's limits.

    ``results`` maps balloon number (int or its string form) -> measured value. For
    a dimensional characteristic that is the measured size, accepted when
    ``lower <= measured <= upper``. For a geometric one it is the measured deviation
    within the zone (for ``position`` you may pass ``{"x":…, "y":…}`` and the
    diametral deviation 2·√(x²+y²) is used, matching ``gdt_check``), accepted when
    it is within ``zone + mmc_bonus``.

    Returns a row per characteristic with ``measured``, ``status``
    (``pass`` | ``fail`` | ``not_evaluated``), ``margin``, and ``reason``.
    A row with no supplied measurement, or no limits to judge against, is
    ``not_evaluated`` — never a silent pass."""
    lookup = {}
    for k, v in (results or {}).items():
        try:
            lookup[int(k)] = v
        except (TypeError, ValueError):
            lookup[str(k)] = v

    rows = []
    for c in chars:
        b = c.get("balloon")
        measured = lookup.get(b, lookup.get(str(c.get("id"))))
        row = {"balloon": b, "id": c.get("id"),
               "characteristic": c.get("characteristic"),
               "measured": None, "margin": None,
               "status": "not_evaluated", "reason": ""}
        if measured is None:
            row["reason"] = "no measurement supplied"
            rows.append(row)
            continue

        if c.get("kind") == GEOMETRIC:
            g = c.get("gdt", {})
            if isinstance(measured, dict):
                dev = 2.0 * math.hypot(float(measured.get("x", 0.0)),
                                       float(measured.get("y", 0.0)))
            else:
                dev = abs(float(measured))
            effective = float(g.get("zone", 0.0)) + max(0.0, float(
                g.get("mmc_bonus", 0.0) or 0.0))
            margin = effective - dev
            row.update({"measured": round(dev, 6), "margin": round(margin, 6),
                        "status": "pass" if margin >= 0 else "fail",
                        "reason": f"deviation {dev:g} vs zone {effective:g}"})
            rows.append(row)
            continue

        lo, hi = c.get("lower"), c.get("upper")
        if lo is None or hi is None:
            row["measured"] = float(measured)
            row["reason"] = "characteristic has no limits — cannot accept or reject"
            rows.append(row)
            continue
        m = float(measured)
        margin = min(m - lo, hi - m)
        row.update({"measured": round(m, 6), "margin": round(margin, 6),
                    "status": "pass" if margin >= 0 else "fail",
                    "reason": f"{m:g} vs [{lo:g}, {hi:g}]"})
        rows.append(row)
    return rows


# --- AS9102-shaped first-article report ---------------------------------------

# The header carried on every export. This module produces the SHAPE of an AS9102
# Form 3, not a certified submission — say so on the artifact itself, every time.
FAI_DISCLAIMER = (
    "AS9102-SHAPED first-article characteristic list generated by DriftPin. "
    "This is NOT a certified AS9102 submission and has not been qualified by any "
    "accreditation body; it reproduces the Form 3 field layout so a real form can "
    "be filled from it."
)

# AS9102 Rev B Form 3 fields 5-12, verbatim, followed by the DriftPin columns that
# make the row self-contained (limits, method, computed status).
FAI_COLUMNS = [
    "Char No.",                       # 5
    "Reference Location",             # 6
    "Characteristic Designator",      # 7
    "Requirement",                    # 8
    "Results",                        # 9
    "Designed / Qualified Tooling",   # 10
    "Nonconformance Number",          # 11
    "Notes",                          # 12
    "Lower Limit",                    # DriftPin
    "Upper Limit",                    # DriftPin
    "Measurement Method",             # DriftPin
    "Status",                         # DriftPin
]


def _requirement(char):
    """AS9102 field 8 — the requirement exactly as the print states it."""
    if char.get("kind") == GEOMETRIC:
        return char["characteristic"]
    if char.get("kind") == NOTE:
        return char.get("text", "")
    lo, hi = char.get("lower"), char.get("upper")
    base = char["characteristic"]
    if lo is None or hi is None:
        return f"{base} (general tolerance)"
    plus = char.get("tol_plus", 0.0)
    minus = char.get("tol_minus", 0.0)
    if abs(float(plus) + float(minus)) < 5e-7:
        return f"{base} ±{abs(float(plus)):g}"
    return f"{base} +{float(plus):g}/{float(minus):g}"


def fai_rows(chars, results=None, *, reference="", sheet=""):
    """AS9102-Form-3-shaped rows for the characteristic list.

    ``reference`` overrides field 6 (Reference Location) — the drawing sheet/zone
    the characteristic is called out on — for every row; left empty, each row
    references the view it is dimensioned on, and ``sheet`` covers the ones anchored
    to no view (a page-level control frame or general note). With ``results``
    supplied the Results and Status columns are filled and computed; without, they
    are blank for an inspector to write into. Returns
    ``{columns, rows, summary, disclaimer, ok}`` where ``summary`` counts
    pass/fail/not_evaluated."""
    verdicts = {r["balloon"]: r for r in evaluate(chars, results)}
    rows = []
    summary = {"pass": 0, "fail": 0, "not_evaluated": 0}
    for c in chars:
        v = verdicts.get(c.get("balloon"), {})
        status = v.get("status", "not_evaluated")
        summary[status] = summary.get(status, 0) + 1
        measured = v.get("measured")
        rows.append({
            "Char No.": c.get("balloon"),
            "Reference Location": reference or c.get("view", "") or sheet,
            "Characteristic Designator": c.get("characteristic"),
            "Requirement": _requirement(c),
            "Results": "" if measured is None else f"{measured:g}",
            "Designed / Qualified Tooling": "",
            "Nonconformance Number": "",
            "Notes": v.get("reason", ""),
            "Lower Limit": "" if c.get("lower") is None else f"{c['lower']:g}",
            "Upper Limit": "" if c.get("upper") is None else f"{c['upper']:g}",
            "Measurement Method": c.get("method", {}).get("method", ""),
            "Status": status,
        })
    return {"columns": list(FAI_COLUMNS), "rows": rows, "summary": summary,
            "disclaimer": FAI_DISCLAIMER,
            "ok": summary.get("fail", 0) == 0}


def _csv_cell(value):
    s = "" if value is None else str(value)
    if any(ch in s for ch in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


def fai_csv(report, *, part=None, rev=None):
    """Serialise :func:`fai_rows` output to CSV text.

    A comment preamble carries the disclaimer and the part/revision identity, so a
    file that escapes its directory still says what it is and what it isn't."""
    lines = [f"# {FAI_DISCLAIMER}"]
    if part:
        lines.append(f"# Part: {part}")
    if rev:
        lines.append(f"# Revision: {rev}")
    s = report["summary"]
    lines.append(f"# Characteristics: {len(report['rows'])}  "
                 f"pass={s.get('pass', 0)} fail={s.get('fail', 0)} "
                 f"not_evaluated={s.get('not_evaluated', 0)}")
    lines.append(",".join(_csv_cell(c) for c in report["columns"]))
    for row in report["rows"]:
        lines.append(",".join(_csv_cell(row.get(c)) for c in report["columns"]))
    return "\n".join(lines) + "\n"

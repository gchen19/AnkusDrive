"""
Orderable standard parts (issue #234): canonical designations for purchased parts,
and the off-the-shelf catalog that says whether the part exists at all.

A BOM row that reads ``SocketHeadCapScrew`` is not a buyable line — somebody has to
work out that it means an M4×12 socket-head cap screw to ISO 4762 in A2 stainless,
and they have to do it again on every revision. Worse, a design can specify an M4×13
screw: dimensionally reasonable, geometrically fine, and impossible to buy. The
existing standards corpora describe what a part's dimensions ARE; nothing said which
parts EXIST. This module closes both gaps, in two independent layers:

* **Designation (offline, deterministic).** Turn what the generator tools already
  know — kind, size, length, seal type, cross-section — into the canonical string a
  buyer reads: ``ISO 4762 M4×12 A2``, ``608-2RS``, ``AS568-214 NBR70``. It is
  stamped on the object at creation time, so it survives save/reopen and travels
  with the assembly.
* **Catalog (offline, deterministic).** Resolve a designation against the curated
  off-the-shelf corpus in :mod:`ankusdrive.analysis.standards`: is this a stocked
  item, and if not, what IS? :func:`catalog_nearest` is the one that changes how an
  agent designs — asked for a 13 mm screw it answers "12 and 16 exist, 13 does not",
  which turns a dead end into a stack-up adjustment.

Neither layer touches the network, needs a credential, or knows what anything costs.
Availability is a design-time fact about a market; price is a purchasing-time
negotiation, and they are deliberately not conflated here.

FreeCAD-free on purpose, like :mod:`ankusdrive.inspection` — everything here is string
and table arithmetic over plain dicts, so it unit-tests on the host interpreter in
milliseconds. The worker shims (``standard_part_designate`` / ``catalog_search`` /
``catalog_nearest`` / ``designation_check``, and ``bom_extract(orderable=True)``)
only read stamps off real objects and hand them here.

Three ideas carry the module:

* **A designation is derived, never guessed.** Every constructor takes facts the
  generator already had. Nothing infers a designation from geometry, so a hand-
  modelled bracket can never acquire a false one. Where a fact is genuinely absent
  (nobody said what grade of steel), the designation is emitted WITHOUT it and
  marked ``complete=False`` with a reason, rather than defaulting to a plausible lie.
* **The ladder is discrete and nothing interpolates.** Stocked lengths are rungs,
  not a range. :func:`catalog_nearest` returns the rungs either side of a request
  and the exact deltas; it never invents a size between them, and it never rounds
  silently on the caller's behalf.
* **A part that isn't stocked is a finding, not a blank.** ``not_stocked`` drags a
  BOM's ``ok`` to False and names the alternatives. A parts list that reads as
  buildable while containing a screw nobody sells is the failure this exists to
  prevent.
"""

from __future__ import annotations

import re

from ankusdrive.analysis import standards as _std

# --- families -----------------------------------------------------------------

FASTENER = "fastener"
BEARING = "bearing"
ORING = "oring"
THREADED_ROD = "threaded_rod"

# The multiplication sign is the one ISO actually prints; every parser here accepts
# the ASCII 'x'/'X'/'*' a human types, and every emitter writes the real character
# so two spellings of the same part can never become two BOM lines.
TIMES = "×"

# Availability is a curated snapshot of a market, not physics. Every result that
# makes an availability claim carries this.
CATALOG_FIDELITY = "curated snapshot"


class DesignationError(ValueError):
    """Raised when a designation is asked for with facts that cannot produce one —
    an unknown fastener kind, an invalid grade, a negative length. NOT raised when
    a fact is merely missing: that yields an incomplete designation with a reason,
    because "nobody said which grade" is an answer a buyer can act on and a crash
    is not."""


# --- fasteners ----------------------------------------------------------------
#
# The standard number IS the part identity for hardware — "M4×12 cap screw" is
# ambiguous (ISO 4762 socket head? ISO 7380 button head? which head height?),
# "ISO 4762 M4×12 A2" is not. Each add_fastener kind maps to the standard whose
# geometry it actually builds: a plain-shank hex bolt is ISO 4014 (partially
# threaded), NOT ISO 4017 (fully threaded), and getting that backwards buys the
# wrong screw.

_FASTENER_STANDARDS = {
    "socket_head_cap_screw": ("ISO 4762", True),   # (standard, takes a length)
    "hex_bolt": ("ISO 4014", True),                # partially threaded — plain shank
    "hex_nut": ("ISO 4032", False),
    "washer": ("ISO 7089", False),
}

# ISO 898-1 property classes for steel bolts/screws, and ISO 3506-1 grades for
# stainless. A nut is classified by ISO 898-2 / ISO 3506-2 on a DIFFERENT scale —
# "8" not "8.8" — so the two vocabularies are kept apart and validated per kind.
# Writing "ISO 4032 M8 8.8" is a real, common, and expensive mistake.
_SCREW_GRADES = {"4.6", "4.8", "5.6", "5.8", "6.8", "8.8", "9.8", "10.9", "12.9"}
_NUT_GRADES = {"04", "05", "5", "6", "8", "10", "12"}
_STAINLESS_GRADES = {"A1", "A2", "A4", "C1", "C3", "C4", "F1",
                     "A2-50", "A2-70", "A2-80", "A4-50", "A4-70", "A4-80"}
# ISO 7089 washers are classified by hardness, not by a bolt property class.
_WASHER_GRADES = {"200HV", "300HV", "140HV"} | _STAINLESS_GRADES

_GRADES_BY_KIND = {
    "socket_head_cap_screw": _SCREW_GRADES | _STAINLESS_GRADES,
    "hex_bolt": _SCREW_GRADES | _STAINLESS_GRADES,
    "hex_nut": _NUT_GRADES | _STAINLESS_GRADES,
    "washer": _WASHER_GRADES,
}

_SIZE_RE = re.compile(r"^M(\d+(?:\.\d+)?)$")


def _norm_size(size):
    """``'m4'`` / ``'M4'`` / ``4`` -> ``'M4'``. Raises on anything that isn't an ISO
    metric nominal."""
    s = str(size).strip().upper().replace(" ", "")
    if not s.startswith("M"):
        s = "M" + s
    if not _SIZE_RE.match(s):
        raise DesignationError(f"{size!r} is not an ISO metric size (expected e.g. 'M4')")
    # M4.0 and M4 are the same thread; canonicalise so they can't split a BOM line
    body = s[1:]
    if body.endswith(".0"):
        body = body[:-2]
    return "M" + body


def _norm_grade(kind, grade):
    """Validate and canonicalise a material/property grade for a fastener kind.

    ``None`` passes through — an unstated grade is a documented gap, not an error.
    An unrecognised one RAISES: a typo'd grade silently baked into a designation is
    a part you order and can't use."""
    if grade is None or str(grade).strip() == "":
        return None
    g = str(grade).strip().upper().replace(" ", "")
    allowed = _GRADES_BY_KIND.get(kind, set())
    if g not in allowed:
        raise DesignationError(
            f"grade {grade!r} is not valid for {kind}; expected one of "
            f"{sorted(allowed)}"
        )
    return g


def _fmt_len(value):
    """Lengths print as integers when they are integral — 'M4×12', not 'M4×12.0'."""
    f = float(value)
    return str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:g}"


def designate_fastener(kind, size, length=None, grade=None):
    """Canonical designation for a standard fastener.

    ``kind`` is an ``add_fastener`` kind; ``size`` an ISO metric nominal ('M4');
    ``length`` the shank length in mm (screws and bolts only); ``grade`` the ISO
    898-1 property class ('8.8'), ISO 898-2 nut class ('8'), or ISO 3506 stainless
    grade ('A2') — omit it and the designation says so rather than inventing one.

    Returns a designation card: ``{ok, family, standard, designation, size, length,
    grade, complete, reason, purchased}``. ``complete`` is False when the string is
    not yet enough to order against (today: no grade), with ``reason`` naming the
    gap. Raises :class:`DesignationError` on an unknown kind/size/grade."""
    k = str(kind)
    if k not in _FASTENER_STANDARDS:
        raise DesignationError(
            f"unknown fastener kind {kind!r}; expected one of "
            f"{sorted(_FASTENER_STANDARDS)}")
    standard, needs_length = _FASTENER_STANDARDS[k]
    sz = _norm_size(size)
    g = _norm_grade(k, grade)

    if needs_length:
        if length is None:
            raise DesignationError(f"{standard} ({k}) is designated by size AND "
                                   "length; length (mm) is required")
        if float(length) <= 0:
            raise DesignationError("length must be > 0 mm")
        body = f"{sz}{TIMES}{_fmt_len(length)}"
    else:
        if k == "washer":
            # ISO 7089 designates a washer by the BARE nominal size of the bolt it
            # fits ("ISO 7089 - 8 - 200HV"), not by a thread designation; the M is
            # not part of the standard's designation. Parsing accepts both.
            body = sz[1:]
        else:
            body = sz

    text = f"{standard} {body}" + (f" {g}" if g else "")
    card = {
        "ok": True, "family": FASTENER, "purchased": True,
        "standard": standard, "kind": k, "designation": text,
        "size": sz, "length": None if not needs_length else float(length),
        "grade": g,
    }
    return _finish(card, None if g else
                   "no material grade specified — a buyer cannot choose between "
                   "class 8.8 steel and A2 stainless from this designation; pass "
                   "grade= to complete it")


# --- bearings -----------------------------------------------------------------
#
# A rolling bearing's designation IS its catalog number, and the seal suffix is part
# of it: 608 (open), 608-2Z (shielded), 608-2RS (contact-sealed) are three different
# purchases with different drag and speed limits, built on the same rings.

_SEAL_SUFFIX = {
    "open": "", "": "",
    "rs": "-RS", "2rs": "-2RS", "rz": "-RZ", "2rz": "-2RZ",
    "z": "-Z", "2z": "-2Z", "zz": "-2Z",
}
_SEAL_LABEL = {
    "": "open (no seal or shield)",
    "-RS": "contact seal, one side", "-2RS": "contact seals, both sides",
    "-RZ": "low-friction seal, one side", "-2RZ": "low-friction seals, both sides",
    "-Z": "shield, one side", "-2Z": "shields, both sides",
}


def designate_bearing(designation, seals="open"):
    """Canonical designation for a deep-groove ball bearing.

    ``designation`` is the catalog number ('608', '6205'); ``seals`` one of
    open/RS/2RS/RZ/2RZ/Z/2Z (case-insensitive, and '2rs' == '-2RS'). A catalog
    number that already carries a suffix ('608-2RS') is accepted and its suffix
    wins unless ``seals`` contradicts it.

    Returns a designation card ``{ok, family, standard, designation, series, seals,
    seal_description, complete, purchased}``. Raises :class:`DesignationError` on an
    unknown seal code or an empty catalog number — a bearing built from raw
    bore/OD/width with no catalog number is NOT designatable and callers should not
    ask (there is no such thing as a generic orderable bearing)."""
    base = str(designation or "").strip().upper().replace(" ", "")
    if not base:
        raise DesignationError(
            "a bearing designation requires a catalog number (e.g. '608'); a "
            "bearing given only bore/OD/width is a geometric envelope, not an "
            "orderable part")
    suffix = ""
    if "-" in base:
        base, _, raw = base.partition("-")
        suffix = _SEAL_SUFFIX.get(raw.lower())
        if suffix is None:
            raise DesignationError(f"unknown bearing seal suffix {raw!r}")
    if seals is not None and str(seals).strip() != "":
        key = str(seals).strip().lower().lstrip("-")
        if key not in _SEAL_SUFFIX:
            raise DesignationError(
                f"unknown seal code {seals!r}; expected one of "
                f"{sorted(k for k in _SEAL_SUFFIX if k)}")
        want = _SEAL_SUFFIX[key]
        # an explicit non-open `seals` overrides a suffix already on the number;
        # `seals='open'` defers to the suffix so designate_bearing('608-2RS') works
        if want or not suffix:
            suffix = want
    if not base.isalnum():
        raise DesignationError(f"{designation!r} is not a bearing catalog number")
    card = {
        "ok": True, "family": BEARING, "purchased": True,
        "standard": "ISO 15",      # boundary dimensions; the number is the identity
        "designation": base + suffix,
        "series": base, "seals": suffix.lstrip("-") or "open",
        "seal_description": _SEAL_LABEL.get(suffix, suffix),
    }
    return _finish(card, None)


# --- O-rings ------------------------------------------------------------------

def designate_oring(inner_diameter_mm, cross_section_mm, compound=None):
    """Canonical designation for an O-ring from the gland it seals.

    ``inner_diameter_mm`` is the ring's free ID (for a static face/radial gland that
    is the groove's inner diameter, which is how ``oring_groove`` defines it);
    ``cross_section_mm`` the wire diameter; ``compound`` the elastomer and durometer
    as ordered ('NBR70', 'FKM75', 'EPDM70').

    Sizes come from :func:`ankusdrive.analysis.standards.as568_lookup`. When the size
    is not an AS568 standard size, ``ok`` is False with a reason and the nearest
    sizes — an off-table ring is a custom tooled part, not a catalog line.

    Returns a designation card ``{ok, family, standard, designation, dash, id_mm,
    cs_mm, compound, complete, reason}``."""
    hit = _std.as568_lookup(inner_diameter_mm, cross_section_mm)
    if not hit["ok"]:
        return {"ok": False, "family": ORING, "purchased": True,
                "designation": None, "complete": False,
                "reason": hit["reason"], "nearest": hit.get("nearest", [])}
    comp = str(compound).strip().upper().replace(" ", "") if compound else None
    text = f"AS568-{hit['dash']}" + (f" {comp}" if comp else "")
    card = {
        "ok": True, "family": ORING, "purchased": True, "standard": "SAE AS568",
        "designation": text, "dash": hit["dash"], "id_mm": hit["id_mm"],
        "cs_mm": hit["cs_mm"], "compound": comp,
    }
    return _finish(card, None if comp else
                   "no elastomer/durometer specified — NBR70 and FKM75 are the same "
                   "AS568 size and different parts; pass compound= to complete it")


# --- threaded rod -------------------------------------------------------------

def designate_threaded_rod(diameter_mm, pitch_mm, length_mm, grade=None):
    """Canonical designation for a length of threaded rod (studding).

    An externally threaded bar IS a purchased item; the internal form ``add_thread``
    can also build is a tap-shaped cutting tool, not a BOM line, and callers should
    not designate it.

    Coarse pitch is implied by the size, so a coarse rod designates as
    ``DIN 976-1 M8×1000`` and a fine one carries its pitch: ``DIN 976-1 M8×1×1000``.
    Returns a designation card ``{ok, family, standard, designation, size, pitch,
    length, grade, complete, reason}``."""
    d = float(diameter_mm)
    if d <= 0:
        raise DesignationError("diameter must be > 0 mm")
    length = float(length_mm)
    if length <= 0:
        raise DesignationError("length must be > 0 mm")
    pitch = float(pitch_mm)
    size = _norm_size(f"M{_fmt_len(d)}" if abs(d - round(d)) < 1e-9 else f"M{d:g}")
    g = _norm_grade("hex_bolt", grade)
    coarse = _coarse_pitch(size)
    fine = coarse is not None and abs(pitch - coarse) > 1e-6
    body = (f"{size}{TIMES}{_fmt_len(pitch)}{TIMES}{_fmt_len(length)}" if fine
            else f"{size}{TIMES}{_fmt_len(length)}")
    text = f"DIN 976-1 {body}" + (f" {g}" if g else "")
    card = {
        "ok": True, "family": THREADED_ROD, "purchased": True,
        "standard": "DIN 976-1", "designation": text, "size": size,
        "pitch": pitch, "length": length, "grade": g, "fine_pitch": fine,
    }
    return _finish(card, None if g else
                   "no material grade specified — pass grade= to complete it")


# ISO 261 coarse pitches, the sizes add_fastener/add_thread can produce. Used only
# to decide whether a pitch needs to appear in the designation at all.
_COARSE_PITCH = {
    "M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M4": 0.7, "M5": 0.8, "M6": 1.0,
    "M8": 1.25, "M10": 1.5, "M12": 1.75, "M16": 2.0, "M20": 2.5, "M24": 3.0,
}


def _coarse_pitch(size):
    return _COARSE_PITCH.get(size)


def _finish(card, incomplete_reason):
    """Attach the completeness verdict every card carries."""
    card["complete"] = incomplete_reason is None
    card["reason"] = incomplete_reason or ""
    return card


# --- parsing / normalisation --------------------------------------------------

_STD_ALIASES = {
    "ISO4762": "ISO 4762", "ISO4014": "ISO 4014", "ISO4017": "ISO 4017",
    "ISO4032": "ISO 4032", "ISO7089": "ISO 7089", "DIN976": "DIN 976-1",
    "DIN976-1": "DIN 976-1", "DIN912": "ISO 4762", "DIN931": "ISO 4014",
    "DIN934": "ISO 4032", "DIN125A": "ISO 7089", "AS568": "AS568",
}
_KIND_BY_STANDARD = {std: kind for kind, (std, _) in _FASTENER_STANDARDS.items()}
_BEARING_RE = re.compile(r"^(\d{3,5})(?:-(2RS|RS|2RZ|RZ|2Z|Z|ZZ))?$")


def _split_tokens(text):
    """Split a designation into its whitespace-separated tokens, having glued the
    standard number back onto its family ('ISO 4762' is one token, not two)."""
    parts = str(text).strip().split()
    if len(parts) >= 2 and parts[0].upper() in ("ISO", "DIN", "ANSI", "SAE", "JIS"):
        parts = [f"{parts[0].upper()} {parts[1]}"] + parts[2:]
    return parts


def parse_designation(text):
    """Parse a canonical designation string back into its facts.

    The inverse of the ``designate_*`` constructors: every designation they emit
    round-trips through this and back to the same string, which is what makes a
    designation stamped on an object usable as an identity rather than a label.

    Accepts the spellings a human types — 'iso4762 m4x12 a2', 'M4 X 12', 'DIN 912
    M4×12 A2' — and returns the same card shape the constructors return. A string
    that is not a designation returns ``{ok: False, reason: ...}`` rather than
    raising: "this isn't a designation" is a legitimate answer about arbitrary
    text."""
    raw = str(text or "").strip()
    if not raw:
        return {"ok": False, "designation": None, "complete": False,
                "reason": "empty designation"}
    norm = raw.replace(TIMES, "x").replace("*", "x")
    # glue a size×length back together when a human typed it with spaces
    # ("M4 x 12" -> "M4x12"); anchored on digits so it can only ever fire between
    # a size and a number, never inside a word
    norm = re.sub(r"([0-9M])\s*[xX]\s*(?=[0-9])", r"\1x", norm)
    tokens = _split_tokens(norm)
    head = tokens[0].upper().replace(" ", "")
    std = _STD_ALIASES.get(head, tokens[0].upper() if " " in tokens[0] else None)

    try:
        if std in _KIND_BY_STANDARD:
            return _parse_fastener(std, tokens[1:])
        if std == "DIN 976-1":
            return _parse_rod(tokens[1:])
        if head.startswith("AS568"):
            return _parse_oring(tokens, head)
        m = _BEARING_RE.match(head)
        if m and len(tokens) == 1:
            return designate_bearing(m.group(1), m.group(2) or "open")
    except DesignationError as exc:
        return {"ok": False, "designation": None, "complete": False,
                "reason": str(exc)}
    return {"ok": False, "designation": None, "complete": False,
            "reason": f"{raw!r} is not a designation this module knows how to read "
                      "(expected an ISO/DIN fastener, a bearing catalog number, or "
                      "an AS568 O-ring size)"}


def _parse_fastener(std, rest):
    kind = _KIND_BY_STANDARD[std]
    if not rest:
        raise DesignationError(f"{std} needs at least a size")
    size_field = rest[0]
    grade = rest[1] if len(rest) > 1 else None
    length = None
    if "x" in size_field.lower():
        size, _, length_s = size_field.lower().partition("x")
        length = float(length_s)
    else:
        size = size_field
    return designate_fastener(kind, size, length, grade)


def _parse_rod(rest):
    if not rest:
        raise DesignationError("DIN 976-1 needs a size and a length")
    parts = rest[0].lower().split("x")
    grade = rest[1] if len(rest) > 1 else None
    size = _norm_size(parts[0])
    if len(parts) == 3:
        pitch, length = float(parts[1]), float(parts[2])
    elif len(parts) == 2:
        length = float(parts[1])
        pitch = _coarse_pitch(size)
        if pitch is None:
            raise DesignationError(f"no ISO 261 coarse pitch tabulated for {size}; "
                                   "spell the pitch out (M8×1×1000)")
    else:
        raise DesignationError("DIN 976-1 is designated size×length (M8×1000)")
    return designate_threaded_rod(float(size[1:]), pitch, length, grade)


def _parse_oring(tokens, head):
    dash_s = head[len("AS568"):].lstrip("-")
    if not dash_s.isdigit():
        raise DesignationError(f"{tokens[0]!r} carries no AS568 dash number")
    dash = int(dash_s)
    row = next((r for r in _std.as568_sizes() if r["dash"] == dash), None)
    if row is None:
        raise DesignationError(
            f"AS568-{dash} is outside the tabulated runs (-110..-149, -201..-246)")
    compound = tokens[1] if len(tokens) > 1 else None
    return designate_oring(row["id_mm"], row["cs_mm"], compound)


def normalize(text):
    """Canonical spelling of a designation, or ``None`` if it isn't one.

    'iso4762 m4x12 a2' -> 'ISO 4762 M4×12 A2'. Use it before comparing two
    designations or keying a table: two spellings of one part must never become two
    BOM lines."""
    card = parse_designation(text)
    return card.get("designation") if card.get("ok") else None


def designation_key(text):
    """Case/spelling-insensitive identity key for a designation.

    Falls back to a squashed form of the raw string when the text doesn't parse, so
    a hand-entered designation this module has no constructor for (a proprietary
    part number) still groups with itself."""
    canon = normalize(text)
    base = canon if canon is not None else str(text or "")
    return re.sub(r"\s+", " ", base.upper().replace(TIMES, "X")).strip()


# --- purchased-part detection -------------------------------------------------
#
# Whether a BOM row is a purchased standard part is EXACT for anything AnkusDrive
# generated (the object is stamped) and a documented heuristic for everything else.
# The heuristic exists for exactly one job: catching a row whose name screams
# "purchased" and which carries no designation, so it is reported rather than
# quietly shipped as if someone had already sourced it.

_PURCHASED_WORDS = {
    "screw", "capscrew", "setscrew", "bolt", "nut", "locknut", "washer",
    "bearing", "oring", "orings", "dowel", "rivet", "circlip", "snapring",
    "retainingring", "seal", "gasket", "spring", "stud", "fastener", "shcs",
    "helicoil", "insert", "spacer", "standoff",
}

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=\d)")


def name_tokens(name):
    """Lower-cased words in an object name, splitting snake_case, kebab-case AND
    CamelCase ('SocketHeadCapScrew' -> socket/head/cap/screw) plus the digit
    boundary ('M6Screw' -> m/6/screw)."""
    return [t.lower() for t in _TOKEN_SPLIT.split(str(name or "")) if t]


def looks_purchased(name):
    """Heuristic: does this part NAME read like a purchased standard part?

    Returns ``{purchased, matched, basis}``. It is a name heuristic and labels
    itself as one — it exists to raise a question ("Bearing002 has no designation"),
    never to assert an identity. A hand-modelled ``Bracket`` is not purchased-
    looking, which is the half of the test that keeps false designations out."""
    toks = name_tokens(name)
    joined = "".join(toks)
    matched = [t for t in toks if t in _PURCHASED_WORDS]
    # also catch names that ran the words together past the CamelCase split
    if not matched:
        matched = [w for w in sorted(_PURCHASED_WORDS)
                   if len(w) >= 5 and w in joined]
    return {"purchased": bool(matched), "matched": matched,
            "basis": "name heuristic over a standard-hardware word list — a "
                     "question to answer, not an identity"}


# --- the off-the-shelf catalog ------------------------------------------------
#
# The corpus lives in ankusdrive.analysis.standards (catalog.json) next to the other
# reference tables; this is the query layer over it. Everything below is exact
# lookup and comparison on a DISCRETE ladder — there is no interpolation anywhere,
# because a length between two rungs is not a part.

_SIZE_KIND_LABEL = {
    "metric_thread": "ISO metric thread designation",
    "metric_nominal": "nominal size, mm",
    "shaft_mm": "shaft diameter, mm",
    "bore_mm": "bore diameter, mm",
    "bearing_designation": "bearing catalog number",
    "as568_dash": "AS568 dash size",
}


def _grade_matches(want, offered):
    """Does a designation's grade match a catalog grade?

    Exact, or the un-suffixed stem of it: a designation that says ``A2`` is
    satisfied by a catalog listing ``A2-70``, because ``A2`` names the alloy and
    ``-70`` the strength class. It does NOT match the other way round — asking for
    ``A2-80`` is not satisfied by ``A2-70``."""
    w = str(want).strip().upper()
    o = str(offered).strip().upper()
    return w == o or (w == o.split("-")[0] and "-" not in w)


def _size_key_for(card, sizes):
    """The catalog size key a designation card refers to, or None.

    Handles the places a designation and a catalog index legitimately disagree on
    spelling: a washer designates by bare nominal ('4') while a thread designates
    'M4', and a bearing's size key is its series without the seal suffix."""
    if card.get("family") == BEARING:
        return card.get("series") if card.get("series") in sizes else None
    if card.get("family") == ORING:
        key = f"-{card.get('dash')}"
        return key if key in sizes else None
    for cand in (card.get("size"), str(card.get("size") or "").lstrip("M"),
                 "M" + str(card.get("size") or "").lstrip("M")):
        if cand in sizes:
            return cand
    return None


def catalog_check(designation):
    """Is this designation a part you can actually buy off the shelf?

    Takes a designation string or a designation card and resolves it against the
    curated catalog. Returns
    ``{ok, code, standard, size, length, stocked, grade_ok, reason, nearest,
    lengths, grades, fidelity, captured, market}`` where ``code`` is one of:

    ``stocked``            the size/length exists and the grade is listed. For a
                           cut-to-length product (threaded rod) any length up to the
                           longest stock bar counts, with a ``note`` saying it is a
                           cut rather than a part number;
    ``not_stocked``        the size exists but the LENGTH is not a stocked rung —
                           ``nearest`` names the rungs either side;
    ``size_not_stocked``   the standard does not cover this size at all;
    ``grade_not_listed``   the size/length exists, the material does not;
    ``not_catalogued``     the product standard is outside the corpus's coverage —
                           an absence of evidence, explicitly NOT a claim that the
                           part doesn't exist;
    ``undesignated``       there was no designation to check.

    ``ok`` is True only for ``stocked``. Availability is a curated snapshot of a
    market (see ``captured`` / ``market``), not a physical fact."""
    card = (designation if isinstance(designation, dict)
            else parse_designation(designation))
    if isinstance(card, dict) and card.get("designation"):
        # A BOM row carries a designation STRING and only some of the facts behind
        # it, so re-derive them from the string: a row and a bare designation must
        # behave identically, and a designation round-trips by construction so this
        # is a no-op for a card that was already complete. A designation this module
        # cannot parse (a standard outside its constructors) keeps what it declared.
        reparsed = parse_designation(card["designation"])
        if reparsed.get("ok"):
            card = reparsed
    meta = _std.catalog_meta()
    base = {"fidelity": CATALOG_FIDELITY, "captured": meta.get("captured"),
            "market": meta.get("market"), "nearest": [], "lengths": [],
            "grades": []}
    if not card.get("designation"):
        return {**base, "ok": False, "code": "undesignated", "stocked": False,
                "grade_ok": False, "standard": None, "size": None, "length": None,
                "reason": card.get("reason") or "no designation to check"}

    std_id = card.get("standard")
    try:
        product = _std.catalog_product(std_id)
    except _std.StandardNotFound:
        return {**base, "ok": False, "code": "not_catalogued", "stocked": False,
                "grade_ok": False, "standard": std_id, "size": card.get("size"),
                "length": card.get("length"),
                "reason": f"{std_id} is outside this catalog's coverage, so nothing "
                          "here can say whether it is stocked — that is an absence "
                          "of evidence, not evidence of absence. Covered standards: "
                          f"{_std.list_catalog_products()}"}

    sizes = product["sizes"]
    key = _size_key_for(card, sizes)
    out = {**base, "standard": product["id"], "size": key or card.get("size"),
           "length": card.get("length"), "grades": list(product.get("grades") or [])}
    if key is None:
        return {**out, "ok": False, "code": "size_not_stocked", "stocked": False,
                "grade_ok": False, "nearest": list(sizes),
                "reason": f"{product['id']} is not stocked in size "
                          f"{card.get('size') or card.get('designation')}; stocked "
                          f"sizes are {list(sizes)}"}

    ladder = list(sizes[key])
    out["lengths"] = ladder
    out["cut_to_length"] = bool(product.get("cut_to_length"))
    want_len = card.get("length")
    if product.get("cut_to_length") and want_len is not None and ladder:
        # Studding is bought by the bar and cut, so a length off the ladder is
        # normal practice rather than a finding — but only up to the longest bar.
        longest = max(ladder)
        if want_len > longest:
            return {**out, "ok": False, "code": "not_stocked", "stocked": False,
                    "grade_ok": False, "nearest": [{"length": longest,
                                                    "delta": longest - want_len}],
                    "reason": f"{product['id']} {key} is supplied in bar lengths "
                              f"{ladder} mm and cut to size; {_fmt_len(want_len)} mm "
                              "exceeds the longest bar, so it would have to be "
                              "joined"}
        out["note"] = (f"supplied in {ladder} mm bar and cut to length — "
                       f"{_fmt_len(want_len)} mm is a cut, not a stocked part number")
    elif ladder and want_len is not None:
        snap = nearest_on_ladder(ladder, want_len)
        if not snap["exact"]:
            return {**out, "ok": False, "code": "not_stocked", "stocked": False,
                    "grade_ok": False, "nearest": snap["nearest"],
                    "reason": f"{product['id']} {key} is not stocked at "
                              f"{_fmt_len(want_len)} mm — the stocked lengths are "
                              f"{ladder}; nearest are "
                              f"{[n['length'] for n in snap['nearest']]} mm"}

    grade = card.get("grade") or card.get("compound")
    offered = product.get("grades") or []
    if grade and offered and not any(_grade_matches(grade, o) for o in offered):
        return {**out, "ok": False, "code": "grade_not_listed", "stocked": True,
                "grade_ok": False, "nearest": list(offered),
                "reason": f"{product['id']} {key} is a stocked size, but the "
                          f"catalog does not list it in {grade}; listed materials "
                          f"are {offered}"}
    return {**out, "ok": True, "code": "stocked", "stocked": True,
            "grade_ok": True,
            "reason": f"{card['designation']} is a commonly stocked "
                      f"{product['name'].lower()}"}


def nearest_on_ladder(ladder, want):
    """Snap a wanted value to a DISCRETE ladder — the whole point of the catalog.

    Returns ``{exact, requested, value, below, above, nearest}``. ``below``/``above``
    are the bracketing rungs (``None`` past either end) and ``nearest`` lists them
    with signed deltas, closest first. Nothing is interpolated and nothing is
    silently rounded: a value between two rungs is not a part, and the caller has to
    decide which way to move.

    Ties (a request exactly between two rungs) resolve to the LARGER rung first —
    for a screw that is the safe direction, since a too-short screw doesn't engage
    while a too-long one is usually a spacer or a counterbore away from working."""
    rungs = sorted(float(x) for x in ladder)
    w = float(want)
    exact = any(abs(r - w) < 1e-9 for r in rungs)
    below = max((r for r in rungs if r < w - 1e-9), default=None)
    above = min((r for r in rungs if r > w + 1e-9), default=None)
    cands = [r for r in (below, above) if r is not None]
    if exact:
        cands = [next(r for r in rungs if abs(r - w) < 1e-9)]
    nearest = [{"length": r, "delta": round(r - w, 6)}
               for r in sorted(cands, key=lambda r: (abs(r - w), -r))]
    return {"exact": exact, "requested": w,
            "value": nearest[0]["length"] if nearest else None,
            "below": below, "above": above, "nearest": nearest}


def catalog_nearest(standard, size, length=None, grade=None):
    """Snap a desired standard part to the nearest thing that actually exists.

    ``standard`` is a product standard or alias ('ISO 4762', 'DIN 912'); ``size`` a
    thread/nominal/shaft size; ``length`` the wanted length in mm for a product that
    has one. This is the call that changes how an agent designs: asked for an
    ISO 4762 M4×13 it answers that 12 and 16 exist and 13 does not, turning a dead
    end into a stack-up decision.

    Returns ``{ok, standard, name, size, requested_length, exact, stocked, below,
    above, nearest, lengths, grades, designation, reason, fidelity, captured}``.
    ``designation`` is the canonical designation of the RECOMMENDED part (the
    nearest rung, or the exact one), so the answer is directly usable. ``ok`` is
    True when the request is already stocked. Raises
    :class:`ankusdrive.analysis.standards.StandardNotFound` for a standard the catalog
    does not cover — that is a coverage question, not a design finding."""
    product = _std.catalog_product(standard)
    meta = _std.catalog_meta()
    sizes = product["sizes"]
    out = {"standard": product["id"], "name": product["name"],
           "family": product.get("family"), "size_kind": product.get("size_kind"),
           "grades": list(product.get("grades") or []),
           "fidelity": CATALOG_FIDELITY, "captured": meta.get("captured"),
           "market": meta.get("market")}

    key = str(size).strip()
    if key not in sizes:
        for alt in (key.lstrip("Mm"), "M" + key.lstrip("Mm")):
            if alt in sizes:
                key = alt
                break
    if key not in sizes:
        # a size off the end of the range is the same KIND of answer as a length off
        # the ladder, so it comes back the same shape rather than as an exception
        return {**out, "ok": False, "stocked": False, "exact": False,
                "size": str(size), "requested_length": length,
                "below": None, "above": None, "nearest": [], "lengths": [],
                "designation": None,
                "reason": f"{product['id']} is not stocked in size {size!r}; "
                          f"stocked sizes are {list(sizes)}"}

    ladder = list(sizes[key])
    out.update({"size": key, "lengths": ladder, "requested_length": length})
    if not ladder:
        # no length dimension (a nut, a washer, a circlip): the size IS the answer
        return {**out, "ok": True, "stocked": True, "exact": True,
                "below": None, "above": None, "nearest": [],
                "designation": _designation_for(product, key, None, grade),
                "reason": f"{product['id']} {key} is a stocked size "
                          f"({product['name'].lower()} has no length dimension)"}
    if length is None:
        return {**out, "ok": True, "stocked": True, "exact": True,
                "below": None, "above": None,
                "nearest": [{"length": r, "delta": None} for r in ladder],
                "designation": None,
                "reason": f"{product['id']} {key} is stocked in {len(ladder)} "
                          f"lengths: {ladder} mm"}

    snap = nearest_on_ladder(ladder, length)
    pick = snap["value"]
    deltas = ", ".join("{:+g}".format(n["delta"]) for n in snap["nearest"])
    return {**out, "ok": snap["exact"], "stocked": snap["exact"],
            "exact": snap["exact"], "below": snap["below"], "above": snap["above"],
            "nearest": snap["nearest"],
            "designation": _designation_for(product, key, pick, grade),
            "reason": (f"{product['id']} {key}{TIMES}{_fmt_len(length)} is stocked"
                       if snap["exact"] else
                       f"{product['id']} {key}{TIMES}{_fmt_len(length)} is NOT "
                       f"stocked; the ladder runs {ladder} mm — nearest are "
                       f"{[n['length'] for n in snap['nearest']]} mm ({deltas})")}


def _designation_for(product, size, length, grade):
    """Canonical designation of a catalog item, when the module can build one.

    Only the families with a designation constructor get one; the rest (a button
    head screw, a circlip) return None rather than a hand-assembled string that
    would not round-trip through :func:`parse_designation`."""
    std = product["id"]
    kind = _KIND_BY_STANDARD.get(std)
    try:
        if kind:
            return designate_fastener(kind, size, length, grade)["designation"]
        if product.get("family") == BEARING:
            return designate_bearing(size, grade or "open")["designation"]
    except DesignationError:
        return None
    return None


def catalog_search(family=None, standard=None, kind=None, size=None, length=None,
                   min_length=None, max_length=None, grade=None, drive=None,
                   limit=50):
    """Browse what off-the-shelf standard parts exist — the "what can I actually
    buy" call an agent makes WHILE designing, before committing geometry to a size.

    Every argument is an optional filter: ``family`` (screw / set_screw / nut /
    washer / retaining_ring / pin / bearing / oring), ``standard`` (a product
    standard or alias), ``kind`` (the product's kind tag), ``size``, an exact
    ``length`` or a ``min_length``/``max_length`` window, ``grade``, ``drive``
    (hex_socket / hex). Results are one row per (standard, size).

    Returns ``{ok, count, truncated, items, standards, fidelity, captured, market,
    not_covered}``. Each item is ``{standard, name, family, kind, drive, size,
    size_kind, lengths, length_count, grades, length_measured, designation?}`` where
    ``lengths`` is the stocked ladder narrowed to any length filter. ``not_covered``
    is the catalog's own declaration of what it deliberately omits — read it before
    concluding a part does not exist."""
    meta = _std.catalog_meta()
    want_len = None if length is None else float(length)
    lo = None if min_length is None else float(min_length)
    hi = None if max_length is None else float(max_length)

    items = []
    for pid in _std.list_catalog_products(family):
        product = _std.catalog_product(pid)
        if standard is not None:
            names = [product["id"], *(product.get("aliases") or [])]
            k = str(standard).strip().upper().replace(" ", "")
            if not any(k == n.upper().replace(" ", "") for n in names):
                continue
        if kind is not None and product.get("kind") != str(kind):
            continue
        if drive is not None and product.get("drive") != str(drive):
            continue
        offered = product.get("grades") or []
        if grade is not None and offered and not any(
                _grade_matches(grade, o) for o in offered):
            continue
        for key, ladder in product["sizes"].items():
            if size is not None and not _size_matches(size, key):
                continue
            keep = list(ladder)
            if ladder:
                if want_len is not None:
                    keep = [x for x in keep if abs(x - want_len) < 1e-9]
                if lo is not None:
                    keep = [x for x in keep if x >= lo]
                if hi is not None:
                    keep = [x for x in keep if x <= hi]
                if not keep:
                    continue
            elif want_len is not None or lo is not None or hi is not None:
                # a nut has no length; a length filter cannot be satisfied by it
                continue
            item = {
                "standard": product["id"], "name": product["name"],
                "family": product.get("family"), "kind": product.get("kind"),
                "drive": product.get("drive"), "size": key,
                "size_kind": product.get("size_kind"),
                "length_measured": product.get("length_measured"),
                "lengths": keep, "length_count": len(keep),
                "grades": list(offered),
            }
            pinned = keep[0] if (want_len is not None and keep) else None
            desig = _designation_for(product, key, pinned, grade)
            if desig:
                item["designation"] = desig
            items.append(item)

    truncated = len(items) > int(limit)
    return {
        "ok": bool(items), "count": len(items), "truncated": truncated,
        "items": items[:int(limit)],
        "standards": sorted({i["standard"] for i in items}),
        "fidelity": CATALOG_FIDELITY, "captured": meta.get("captured"),
        "market": meta.get("market"), "not_covered": meta.get("not_covered", []),
    }


def _size_matches(want, key):
    """Compare a requested size to a catalog size key across the spellings that
    legitimately differ ('M4' vs '4' for a washer, '-214' vs '214' for an O-ring)."""
    w = str(want).strip().upper().lstrip("-")
    k = str(key).strip().upper().lstrip("-")
    return w == k or w.lstrip("M") == k.lstrip("M")


# --- BOM integration ----------------------------------------------------------

def designation_check(rows):
    """Flag BOM rows that need a designation and don't have one.

    ``rows`` are BOM-shaped dicts: ``{part, count, designation?, part_class?,
    complete?}``. ``part_class == 'purchased'`` is the EXACT signal (a AnkusDrive
    generator stamped it); everything else falls back to :func:`looks_purchased` on
    the name.

    Returns ``{ok, findings, purchased, designated, undesignated, incomplete,
    basis}``. ``ok`` is False when any purchased row is undesignated or carries a
    designation that isn't yet orderable. Findings carry a ``code``
    (``no_designation`` | ``incomplete_designation``), the row, and a reason."""
    findings = []
    purchased = designated = incomplete = 0
    for row in rows or []:
        part = str(row.get("part", ""))
        stamped = str(row.get("part_class", "") or "").lower() == "purchased"
        hint = looks_purchased(part)
        if not (stamped or hint["purchased"]):
            continue
        purchased += 1
        desig = row.get("designation")
        if not desig:
            findings.append({
                "part": part, "count": row.get("count"), "code": "no_designation",
                "certainty": "stamped" if stamped else "name_heuristic",
                "reason": (
                    f"{part} is a purchased standard part with no canonical "
                    "designation — a buyer cannot order from this row"
                    if stamped else
                    f"{part} reads like a purchased standard part "
                    f"(matched {hint['matched']}) but carries no designation; "
                    "designate it or rename it if it is actually machined"),
            })
            continue
        designated += 1
        if row.get("complete") is False:
            incomplete += 1
            findings.append({
                "part": part, "count": row.get("count"),
                "code": "incomplete_designation", "certainty": "stamped",
                "designation": desig,
                "reason": row.get("reason") or
                f"{desig} is not yet orderable — a required attribute is unstated",
            })
    return {
        "ok": not findings, "findings": findings, "purchased": purchased,
        "designated": designated, "undesignated": purchased - designated,
        "incomplete": incomplete,
        "basis": "exact for AnkusDrive-generated parts (stamped part_class); a name "
                 "heuristic for everything else",
    }


def orderable_bom(rows, check_stock=True):
    """Turn BOM rows into an orderable BOM: designated, and checked against the
    catalog of what you can actually buy.

    ``rows`` are ``bom_extract``-shaped dicts already carrying whatever designation
    stamp the worker read off each part (``designation``, ``part_class``,
    ``complete``). Rows are annotated in place, never dropped: a BOM that quietly
    omits its unbuyable lines is the exact artifact this module exists to prevent.

    Returns ``{ok, rows, undesignated, not_stocked, designation, stocked_count,
    fidelity, captured, market}``. Each purchased row gains ``catalog`` (the
    :func:`catalog_check` verdict), ``stocked``, and ``catalog_code``. ``ok`` is
    False when anything purchased is undesignated OR not stocked — a design built
    from parts nobody sells is not a design you can build."""
    rows = [dict(r) for r in (rows or [])]
    check = designation_check(rows)
    meta = _std.catalog_meta()

    not_stocked = []
    stocked_count = 0
    if check_stock:
        for r in rows:
            if not r.get("designation"):
                continue
            verdict = catalog_check(r)
            r["catalog"] = verdict
            r["stocked"] = verdict["stocked"]
            r["catalog_code"] = verdict["code"]
            if verdict["ok"]:
                stocked_count += 1
            elif verdict["code"] != "not_catalogued":
                # `not_catalogued` is an absence of evidence — it must not read as
                # "this part doesn't exist", so it never becomes a finding
                not_stocked.append({
                    "part": r.get("part"), "count": r.get("count"),
                    "designation": r.get("designation"), "code": verdict["code"],
                    "reason": verdict["reason"], "nearest": verdict["nearest"]})

    return {
        "ok": bool(check["ok"] and not not_stocked),
        "rows": rows,
        "designation": check,
        "undesignated": [f for f in check["findings"]
                         if f["code"] == "no_designation"],
        "not_stocked": not_stocked,
        "stocked_count": stocked_count,
        "fidelity": CATALOG_FIDELITY, "captured": meta.get("captured"),
        "market": meta.get("market"),
    }

"""Standard reference tables — machine-readable dimensional/rating corpora.

Pure-Python, FreeCAD-free. Small JSON corpora of *standard facts* (dimensional
values and published load ratings) plus accessors, mirroring the materials-DB
pattern (``ankusdrive.analysis.materials``). These back the component tools so they
return catalog-accurate numbers instead of guessing.

Five corpora (issues #101, #234):

    threads.json   ISO 261/262/724 metric threads (pitch, tensile stress area,
                   standard sizes) + ISO 898-1 property classes (proof/tensile).
    bearings.json  single-row deep-groove ball bearings: designation -> bore/OD/
                   width + dynamic/static ratings C / C0 (ISO 15 + ISO 281).
    stock.json     ASME B36.10M pipe schedules, steel sheet gauges, round/hex bar.
    catalog.json   which off-the-shelf standard components you can actually BUY:
                   product standards (ISO 4762, ISO 7380-1, ISO 4032, DIN 471, ...),
                   the sizes each covers, and — for screws and pins — the discrete
                   stocked LENGTH LADDER. Issue #234.
    (ISO 286 limits & fits already live as computed tables in
     ``ankusdrive.analysis.tolerance.fit_class`` — see that module.)

The first three answer "what are this part's dimensions"; the catalog answers a
different and, at design time, more decisive question: "does this part exist?"
Nothing in a dimensional table says that an ISO 4762 M4×12 is a stocked item and an
M4×13 is not, and a design built out of M4×13 screws is a design nobody can build.
The catalog is a curated snapshot of a MARKET rather than a statement of physics, so
it is dated and its coverage limits are declared in ``catalog.json``'s ``_meta``.

AS568 O-ring sizes live here too, as a rule plus published anchors rather than a
transcribed block — see :func:`as568_sizes`.

Public API:

    thread(designation)                  -> thread card
    tensile_stress_area(designation)     -> At, mm^2 (tabulated)
    thread_property_class(cls)           -> {proof/yield/tensile} MPa
    recommended_preload(designation, property_class, fraction=0.75) -> N
    recommended_torque(designation, property_class, ...) -> {torque_nm, preload_n}
    list_threads()                       -> [designations]

    bearing(designation)                 -> bearing card (C/C0 in N too)
    list_bearings(series=None)           -> [designations]

    pipe(nps, schedule="sch40")          -> {od_mm, wall_mm, ...}
    sheet_gauge(gauge)                   -> {thickness_mm}
    round_bar(diameter_mm) / hex_bar(af_mm) nearest-stock helpers
    list_pipes()                         -> [nps]

    catalog_meta()                       -> provenance + coverage limits
    list_catalog_products(family=None)   -> [product ids]
    catalog_product(standard)            -> product card (aliases resolved)
    catalog_sizes(standard)              -> [size keys]
    catalog_lengths(standard, size)      -> stocked length ladder (mm)

    as568_sizes()                        -> [{dash, series, id_mm, cs_mm}]
    as568_lookup(id_mm, cs_mm)           -> dash number, or the near misses

Bad designations raise StandardNotFound; out-of-table numeric lookups degrade by
nearest-stock helpers that report the gap rather than raising.

License: dimensional/standard *values* are facts (not copyrightable); the ISO/
ASME standard *documents* are not reproduced. See each JSON ``_meta.source``.
"""
from __future__ import annotations

import json
from pathlib import Path

_THREADS_PATH = Path(__file__).with_name("threads.json")
_BEARINGS_PATH = Path(__file__).with_name("bearings.json")
_STOCK_PATH = Path(__file__).with_name("stock.json")
_CATALOG_PATH = Path(__file__).with_name("catalog.json")

_CACHE: dict | None = None


class StandardNotFound(KeyError):
    """Raised when a designation / size is not in a standard table."""


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = {
            "threads": json.loads(_THREADS_PATH.read_text(encoding="utf-8")),
            "bearings": json.loads(_BEARINGS_PATH.read_text(encoding="utf-8")),
            "stock": json.loads(_STOCK_PATH.read_text(encoding="utf-8")),
            "catalog": json.loads(_CATALOG_PATH.read_text(encoding="utf-8")),
        }
    return _CACHE


def reload_corpus() -> dict:
    """Drop the in-memory cache and reload. Returns {table: count}."""
    global _CACHE
    _CACHE = None
    c = _load()
    return {
        "threads": len(c["threads"]["threads"]),
        "bearings": len(c["bearings"]["bearings"]),
        "pipes": len(c["stock"]["pipe_schedules"]["pipes"]),
        "catalog_products": len(c["catalog"]["products"]),
    }


def _norm_thread(designation: str) -> str:
    """Normalize a thread designation: 'm8' / 'M8x1.25' / 'M8 x 1.25' -> 'M8'
    (nominal only; pitch handled separately)."""
    s = str(designation).strip().upper().replace(" ", "")
    # split off any pitch suffix ('M8X1.25' -> 'M8')
    for sep in ("X", "*"):
        if sep in s:
            s = s.split(sep, 1)[0]
    if not s.startswith("M"):
        s = "M" + s
    return s


# --- threads / fasteners ------------------------------------------------------

def thread(designation: str) -> dict:
    """Return the thread card for an ISO metric designation (e.g. ``'M8'`` or
    ``'M8x1.25'``). Returns a dict copy. Raises StandardNotFound otherwise."""
    key = _norm_thread(designation)
    for t in _load()["threads"]["threads"]:
        if t["designation"].upper() == key:
            return dict(t)
    avail = [t["designation"] for t in _load()["threads"]["threads"]]
    raise StandardNotFound(f"thread {designation!r} not in ISO table; available={avail}")


def list_threads(preferred_only: bool = False) -> list[str]:
    """List thread designations (optionally only ISO 262 preferred sizes)."""
    return [t["designation"] for t in _load()["threads"]["threads"]
            if not preferred_only or t.get("preferred")]


def tensile_stress_area(designation: str) -> float:
    """Tabulated tensile stress area At (mm^2) for an ISO metric thread.
    Raises StandardNotFound for an unknown size."""
    return float(thread(designation)["tensile_stress_area_mm2"])


def thread_pitch(designation: str) -> float:
    """Coarse pitch (mm) for an ISO metric thread (the pitch named in the
    designation if given, e.g. 'M8x1.0', else the coarse pitch)."""
    s = str(designation).strip().upper().replace(" ", "")
    card = thread(designation)
    for sep in ("X", "*"):
        if sep in s:
            try:
                return float(s.split(sep, 1)[1])
            except ValueError:
                break
    return float(card["coarse_pitch_mm"])


def thread_property_class(cls: str) -> dict:
    """Return ISO 898-1 strengths for a steel property class (e.g. ``'8.8'``):
    {proof_strength_mpa, yield_strength_mpa, tensile_strength_mpa, ...}.
    Raises StandardNotFound for an unknown class."""
    pcs = _load()["threads"]["property_classes"]
    key = str(cls)
    if key not in pcs or key == "_meta":
        avail = [k for k in pcs if k != "_meta"]
        raise StandardNotFound(f"property class {cls!r} unknown; available={avail}")
    return dict(pcs[key])


def proof_strength_mpa(designation: str, property_class: str = "8.8") -> float:
    """Proof strength Sp (MPa) for a bolt, honoring the ISO 898-1 8.8 split
    (>M16 uses the higher proof value)."""
    pc = thread_property_class(property_class)
    sp = pc["proof_strength_mpa"]
    if "use_above_m16_proof_mpa" in pc:
        if thread(designation)["nominal_dia_mm"] > 16:
            sp = pc["use_above_m16_proof_mpa"]
    return float(sp)


def recommended_preload(designation: str, property_class: str = "8.8",
                        fraction: float = 0.75) -> dict:
    """Recommended bolt preload Fi = fraction * Sp * At (N).

    fraction defaults to 0.75 (Shigley's reusable-connection guidance; 0.90 for
    permanent). Returns {preload_n, proof_load_n, fraction, tensile_stress_area_mm2,
    proof_strength_mpa}. Raises StandardNotFound on a bad size/class."""
    at = tensile_stress_area(designation)
    sp = proof_strength_mpa(designation, property_class)
    proof_load = sp * at
    return {
        "preload_n": round(fraction * proof_load, 1),
        "proof_load_n": round(proof_load, 1),
        "fraction": fraction,
        "tensile_stress_area_mm2": at,
        "proof_strength_mpa": sp,
        "property_class": property_class,
    }


def recommended_torque(designation: str, property_class: str = "8.8",
                       fraction: float = 0.75, condition: str = "dry",
                       k_factor: float | None = None) -> dict:
    """Recommended tightening torque T = K * Fi * d (N*m).

    Fi is recommended_preload(); K is the nut factor for `condition` (dry,
    lubricated, zinc_plated, cadmium_plated, waxed) unless k_factor overrides.
    d is the nominal diameter in metres. Returns {torque_nm, preload_n, k_factor,
    condition, nominal_dia_mm}. Raises StandardNotFound / ValueError on bad input."""
    pre = recommended_preload(designation, property_class, fraction)
    fi = pre["preload_n"]
    d_mm = thread(designation)["nominal_dia_mm"]
    if k_factor is None:
        kf = _load()["threads"]["torque_k_factors"]
        if condition not in kf or condition == "_meta":
            avail = [k for k in kf if k != "_meta"]
            raise ValueError(f"unknown torque condition {condition!r}; available={avail}")
        k_factor = kf[condition]
    torque_nm = k_factor * fi * (d_mm / 1000.0)
    return {
        "torque_nm": round(torque_nm, 2),
        "preload_n": fi,
        "k_factor": k_factor,
        "condition": condition,
        "nominal_dia_mm": d_mm,
        "property_class": property_class,
    }


# --- bearings -----------------------------------------------------------------

def bearing(designation: str) -> dict:
    """Return the deep-groove ball-bearing card for a designation (e.g. ``'6205'``).
    Adds ``dynamic_c_n`` / ``static_c0_n`` (N) alongside the kN catalog values.
    Raises StandardNotFound for an unknown designation."""
    key = str(designation).strip().upper()
    for b in _load()["bearings"]["bearings"]:
        if b["designation"].upper() == key:
            out = dict(b)
            out["dynamic_c_n"] = out["dynamic_c_kn"] * 1000.0
            out["static_c0_n"] = out["static_c0_kn"] * 1000.0
            return out
    avail = [b["designation"] for b in _load()["bearings"]["bearings"]]
    raise StandardNotFound(f"bearing {designation!r} not in catalog; available={avail}")


def list_bearings(series: str | None = None) -> list[str]:
    """List bearing designations, optionally filtered to one series (6000/6200/6300)."""
    return [b["designation"] for b in _load()["bearings"]["bearings"]
            if series is None or b.get("series") == str(series)]


# --- stock & profiles ---------------------------------------------------------

def pipe(nps: str, schedule: str = "sch40") -> dict:
    """ASME B36.10M pipe dimensions for a nominal pipe size and schedule.

    nps accepts the inch label ('1', '1/2', '1-1/4') or 'DN25'/'25' (metric DN).
    schedule in {sch10, sch40, sch80}. Returns {nps, dn, od_mm, schedule, wall_mm,
    id_mm}. Raises StandardNotFound on an unknown size, ValueError on a schedule
    the size does not tabulate."""
    want = str(nps).strip().upper().replace("DN", "")
    rows = _load()["stock"]["pipe_schedules"]["pipes"]
    row = None
    for r in rows:
        if r["nps"].upper() == str(nps).strip().upper() or str(r["dn"]) == want:
            row = r
            break
    if row is None:
        avail = [r["nps"] for r in rows]
        raise StandardNotFound(f"pipe NPS {nps!r} not tabulated; available={avail}")
    sch = str(schedule).strip().lower()
    if sch not in row["walls_mm"]:
        raise ValueError(
            f"schedule {schedule!r} not tabulated for NPS {row['nps']}; "
            f"available={sorted(row['walls_mm'])}"
        )
    wall = row["walls_mm"][sch]
    return {
        "nps": row["nps"], "dn": row["dn"], "od_mm": row["od_mm"],
        "schedule": sch, "wall_mm": wall,
        "id_mm": round(row["od_mm"] - 2 * wall, 3),
    }


def list_pipes() -> list[str]:
    """List tabulated pipe NPS labels."""
    return [r["nps"] for r in _load()["stock"]["pipe_schedules"]["pipes"]]


def sheet_gauge(gauge: int) -> dict:
    """Steel sheet thickness for a Manufacturers' Standard Gauge number.
    Returns {gauge, thickness_mm}. Raises StandardNotFound for an untabulated gauge."""
    for g in _load()["stock"]["sheet_gauge_steel"]["gauges"]:
        if int(g["gauge"]) == int(gauge):
            return {"gauge": int(gauge), "thickness_mm": g["thickness_mm"]}
    avail = [g["gauge"] for g in _load()["stock"]["sheet_gauge_steel"]["gauges"]]
    raise StandardNotFound(f"sheet gauge {gauge!r} not tabulated; available={avail}")


def _nearest(value: float, options: list[float]) -> dict:
    """Nearest-stock helper: degrade cleanly rather than raise. Returns the closest
    tabulated size, the gap, and whether it was an exact hit."""
    nearest = min(options, key=lambda x: abs(x - value))
    return {
        "requested": value, "nearest_stock": nearest,
        "delta": round(nearest - value, 4), "exact": abs(nearest - value) < 1e-9,
    }


def round_bar(diameter_mm: float) -> dict:
    """Nearest stocked round-bar diameter to a requested value (mm). Degrades
    cleanly: returns the closest size and the gap (never raises for in-range)."""
    return _nearest(float(diameter_mm),
                    _load()["stock"]["round_bar_metric_mm"]["diameters_mm"])


def hex_bar(across_flats_mm: float) -> dict:
    """Nearest stocked hex-bar across-flats (A/F) size to a requested value (mm)."""
    return _nearest(float(across_flats_mm),
                    _load()["stock"]["hex_bar_metric_af_mm"]["across_flats_mm"])


# --- AS568 O-ring sizes -------------------------------------------------------
#
# Dash numbers within a series are a regular inch progression, so this is the RULE
# plus published anchors rather than a transcribed block of numbers: a rule with two
# independently checkable anchors per run is far harder to get subtly wrong than 90
# hand-typed rows, and it makes the coverage limits explicit instead of implied.
# Runs outside these ranges are NOT extrapolated — an ID that falls off the end
# returns no dash number, with the nearest tabulated sizes named.
#
#   (series, first dash, last dash, ID of the first dash (in), step (in), CS (in))
# Anchors: AS568-110 ID 0.362 in = 9.19 mm and AS568-149 ID 2.800 in = 71.12 mm;
#          AS568-201 ID 0.171 in and AS568-214 ID 0.984 in = 24.99 mm.

_AS568_RUNS = [
    ("1xx", 110, 149, 0.362, 0.0625, 0.103),
    ("2xx", 201, 246, 0.171, 0.0625, 0.139),
]
_IN_MM = 25.4

# How far a requested size may sit from a tabulated one and still BE that size. The
# CS window is the AS568 manufacturing tolerance band (~±0.08 mm); the ID window is
# half a dash step, so a request can only ever resolve to one dash.
AS568_CS_TOL_MM = 0.10
AS568_ID_TOL_MM = 0.35


def as568_sizes() -> list[dict]:
    """Every tabulated AS568 size: ``[{dash, series, id_mm, cs_mm}, ...]``.

    Coverage is the two regular runs -110..-149 and -201..-246. Sizes outside them
    are deliberately absent rather than extrapolated."""
    out = []
    for series, first, last, id0_in, step_in, cs_in in _AS568_RUNS:
        for n in range(first, last + 1):
            # round in INCHES first: the published table is an inch table, and
            # rounding after the conversion drifts off it by ~0.01 mm
            id_in = round(id0_in + (n - first) * step_in, 3)
            out.append({"dash": n, "series": series,
                        "id_mm": round(id_in * _IN_MM, 2),
                        "cs_mm": round(cs_in * _IN_MM, 2)})
    return sorted(out, key=lambda r: (r["cs_mm"], r["id_mm"]))


def as568_lookup(inner_diameter_mm: float, cross_section_mm: float) -> dict:
    """The AS568 dash number for an ID/CS pair, or ``None`` with the near misses.

    Returns ``{ok, dash, id_mm, cs_mm, delta_id_mm}`` on a hit, or
    ``{ok: False, reason, nearest}``. Nothing is extrapolated: an O-ring that isn't
    a standard size is a custom tooled part, and saying so beats naming a dash
    number that won't seal."""
    idm = float(inner_diameter_mm)
    csm = float(cross_section_mm)
    table = as568_sizes()
    same_cs = [r for r in table if abs(r["cs_mm"] - csm) <= AS568_CS_TOL_MM]
    if not same_cs:
        offered = sorted({r["cs_mm"] for r in table})
        return {"ok": False, "nearest": [],
                "reason": f"cross-section {csm:g} mm is not an AS568 series section; "
                          f"tabulated sections are {offered} mm"}
    best = min(same_cs, key=lambda r: abs(r["id_mm"] - idm))
    delta = round(best["id_mm"] - idm, 3)
    if abs(delta) > AS568_ID_TOL_MM:
        near = sorted(same_cs, key=lambda r: abs(r["id_mm"] - idm))[:3]
        return {"ok": False,
                "nearest": [{"dash": r["dash"], "id_mm": r["id_mm"],
                             "cs_mm": r["cs_mm"]} for r in near],
                "reason": f"ID {idm:g} mm is not a tabulated AS568 size for a "
                          f"{csm:g} mm section (nearest is -{best['dash']} at "
                          f"{best['id_mm']:g} mm, {delta:+.2f} mm off)"}
    return {"ok": True, "dash": best["dash"], "id_mm": best["id_mm"],
            "cs_mm": best["cs_mm"], "delta_id_mm": delta}


# --- the off-the-shelf catalog ------------------------------------------------
#
# Two products deliberately carry no size block of their own: bearings resolve from
# bearings.json and O-rings from the AS568 rule above. Duplicating either would give
# the corpus two sources of truth that can drift apart, which is exactly the failure
# a catalog is supposed to prevent.

def catalog_meta() -> dict:
    """Provenance for the off-the-shelf catalog: what market it describes, when it
    was captured, and — importantly — what it deliberately does NOT cover."""
    return dict(_load()["catalog"]["_meta"])


def catalog_length_series(name: str) -> list[float]:
    """A named nominal-length series (``screw_iso888`` / ``pin_iso2338``)."""
    series = _load()["catalog"]["length_series"]
    if name not in series:
        raise StandardNotFound(f"no length series {name!r}; have {sorted(series)}")
    return list(series[name])


def _catalog_products() -> list[dict]:
    return _load()["catalog"]["products"]


def _resolve_sizes(product: dict) -> dict:
    """Fill in a product whose sizes live in another corpus.

    ``also_stocked`` tops the borrowed list up with sizes that are stocked but carry
    no entry in the source corpus — the miniature bearing series have no C/C0
    rating in bearings.json, and availability is a claim this catalog can make while
    a load rating is not."""
    src = product.get("sizes_from")
    if src is None:
        return product.get("sizes", {})
    if src == "bearings":
        keys = list(list_bearings()) + list(product.get("also_stocked") or [])
    elif src == "as568":
        keys = [f"-{r['dash']}" for r in as568_sizes()]
    else:
        raise StandardNotFound(f"catalog product {product['id']!r} references "
                               f"unknown size source {src!r}")
    return {k: [] for k in sorted(set(keys))}


def list_catalog_products(family: str | None = None) -> list[str]:
    """Product-standard ids in the catalog, optionally filtered to one family
    (screw / set_screw / nut / washer / retaining_ring / pin / bearing / oring)."""
    return [p["id"] for p in _catalog_products()
            if family is None or p.get("family") == family]


def catalog_product(standard: str) -> dict:
    """The catalog card for a product standard, by id or by alias.

    'ISO 4762', 'iso4762' and 'DIN 912' all resolve to the same card. Returns a copy
    with ``sizes`` materialised (bearings and O-rings included). Raises
    StandardNotFound for an unknown standard."""
    key = str(standard).strip().upper().replace(" ", "")
    for p in _catalog_products():
        names = [p["id"], *(p.get("aliases") or [])]
        if any(key == n.upper().replace(" ", "") for n in names):
            out = dict(p)
            out["sizes"] = _resolve_sizes(p)
            return out
    raise StandardNotFound(
        f"{standard!r} is not a catalog product standard; have "
        f"{list_catalog_products()}")


def catalog_sizes(standard: str) -> list[str]:
    """The size keys a product standard is stocked in (thread designations for
    fasteners, shaft/bore diameters for retaining rings, dash sizes for O-rings)."""
    return list(catalog_product(standard)["sizes"])


def catalog_lengths(standard: str, size: str) -> list[float]:
    """The stocked length ladder (mm) for one size of a product standard.

    Empty for products with no length dimension (a nut, a washer, a circlip).
    Raises StandardNotFound when the standard doesn't cover that size at all —
    which is itself the answer to "can I buy an M22 socket cap screw"."""
    card = catalog_product(standard)
    sizes = card["sizes"]
    key = str(size).strip()
    if key not in sizes:
        # accept 'M6' for a bare-nominal product (a washer) and vice versa
        for alt in (key.lstrip("Mm"), "M" + key):
            if alt in sizes:
                key = alt
                break
        else:
            raise StandardNotFound(
                f"{card['id']} is not stocked in size {size!r}; stocked sizes are "
                f"{list(sizes)}")
    return list(sizes[key])

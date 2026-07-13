"""Standard reference tables — machine-readable dimensional/rating corpora.

Pure-Python, FreeCAD-free. Small JSON corpora of *standard facts* (dimensional
values and published load ratings) plus accessors, mirroring the materials-DB
pattern (``driftpin.analysis.materials``). These back the component tools so they
return catalog-accurate numbers instead of guessing.

Four corpora (issue #101):

    threads.json   ISO 261/262/724 metric threads (pitch, tensile stress area,
                   standard sizes) + ISO 898-1 property classes (proof/tensile).
    bearings.json  single-row deep-groove ball bearings: designation -> bore/OD/
                   width + dynamic/static ratings C / C0 (ISO 15 + ISO 281).
    stock.json     ASME B36.10M pipe schedules, steel sheet gauges, round/hex bar.
    (ISO 286 limits & fits already live as computed tables in
     ``driftpin.analysis.tolerance.fit_class`` — see that module.)

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

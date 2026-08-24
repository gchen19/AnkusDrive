"""Column-buckling screen — Euler + Johnson, the closed-form twin of fem_buckling.

Pure-Python, FreeCAD-free. A Tier-A screening estimator from
``docs/SIMULATION_NEXT.md``: fills the same gap for ``fem_buckling`` that
``beam_modal`` fills for ``fem_modal`` — an exact handbook answer to gate the
eigenvalue solve against, and a 10 ms screen before any mesh.

Slender columns buckle elastically (Euler):  σ_cr = π²·E/λ²,  λ = K·L/r.
Stocky columns yield first; the Johnson parabola blends the two:
σ_cr = σ_y·[1 − σ_y·λ²/(4π²E)] for λ below the transition slenderness
λ_t = √(2π²E/σ_y) — at λ_t both formulas give exactly σ_y/2 (the built-in
continuity identity). K from the end condition: pinned-pinned 1.0,
fixed-free 2.0, fixed-pinned 0.699, fixed-fixed 0.5 (theoretical values —
real connections justify the conservative AISC-recommended K, which is on the
caller). Both formulas are exact closed forms; ``fidelity="exact"``.
Lengths mm, stresses MPa, loads N.
"""
from __future__ import annotations

import math

from . import materials

# theoretical effective-length factors
_K_FACTORS = {
    "pinned_pinned": 1.0,
    "fixed_free": 2.0,
    "fixed_pinned": 0.699,
    "fixed_fixed": 0.5,
}


def _section(width_mm, height_mm, diameter_mm, area_mm2, i_min_mm4):
    """(area, I_min) for a solid rectangle / circle, or explicit values. Buckling
    always picks the WEAK axis, so a rectangle uses I = w·h³/12 with h the
    thinner dimension."""
    if area_mm2 is not None or i_min_mm4 is not None:
        if not (area_mm2 and i_min_mm4) or min(area_mm2, i_min_mm4) <= 0:
            raise ValueError("explicit section needs positive area_mm2 AND i_min_mm4")
        return float(area_mm2), float(i_min_mm4)
    if diameter_mm is not None:
        if diameter_mm <= 0:
            raise ValueError("diameter_mm must be > 0")
        a = math.pi * diameter_mm ** 2 / 4.0
        return a, math.pi * diameter_mm ** 4 / 64.0
    if width_mm and height_mm:
        if min(width_mm, height_mm) <= 0:
            raise ValueError("width_mm and height_mm must be > 0")
        thick, thin = max(width_mm, height_mm), min(width_mm, height_mm)
        return thick * thin, thick * thin ** 3 / 12.0
    raise ValueError(
        "give a section: width_mm+height_mm, diameter_mm, or area_mm2+i_min_mm4")


def _resolve(explicit, material, accessor, what):
    if explicit is not None:
        if explicit <= 0:
            raise ValueError(f"{what} must be > 0")
        return float(explicit)
    if material:
        try:
            val = materials.numeric(materials.get(material), accessor)
        except (materials.MaterialNotFound, KeyError):
            val = None
        if val:
            return val
    raise ValueError(f"provide {what} or a material that carries it")


def beam_buckling(
    length_mm: float,
    end_condition: str = "pinned_pinned",
    width_mm: float | None = None,
    height_mm: float | None = None,
    diameter_mm: float | None = None,
    area_mm2: float | None = None,
    i_min_mm4: float | None = None,
    youngs_gpa: float | None = None,
    yield_mpa: float | None = None,
    material: str | None = None,
    load_n: float | None = None,
) -> dict:
    """Exact column-buckling screen (Euler + Johnson, no solver) — the closed-form
    twin the CalculiX ``fem_buckling`` eigen-solve is gated against (the pairing
    ``beam_modal`` ↔ ``fem_modal`` already has). Section: ``width_mm``+``height_mm``
    (solid rectangle, weak axis taken automatically), ``diameter_mm`` (solid round),
    or explicit ``area_mm2``+``i_min_mm4``. E / σ_y from ``youngs_gpa`` /
    ``yield_mpa`` or a Materials-DB ``material``. ``end_condition``: 'pinned_pinned'
    | 'fixed_free' | 'fixed_pinned' | 'fixed_fixed' (theoretical K). With ``load_n``
    the safety factor P_cr/P is returned.

    Slenderness λ = K·L/r against the transition λ_t = √(2π²E/σ_y): Euler
    σ_cr = π²E/λ² above (``governing="euler"``), Johnson parabola
    σ_y·[1 − σ_y·λ²/(4π²E)] below (``governing="johnson"``); both equal σ_y/2 at
    λ_t exactly. Long-column theory — a very low λ (< ~10) is plain compression
    and is flagged. fidelity="exact"; escalate to ``fem_buckling`` for any
    non-prismatic / non-concentric / built-up case this idealization can't see.

    Returns {end_condition, k_factor, slenderness, transition_slenderness,
    governing, sigma_cr_mpa, p_cr_n, area_mm2, i_min_mm4, radius_gyration_mm,
    safety_factor, fidelity, band_pct, valid_range_ok, warnings, escalate_to}.
    Raises ValueError on a bad end condition, section, or material."""
    if length_mm <= 0:
        raise ValueError("length_mm must be > 0")
    if end_condition not in _K_FACTORS:
        raise ValueError(
            f"unknown end_condition {end_condition!r}; choose from "
            f"{sorted(_K_FACTORS)}")
    k = _K_FACTORS[end_condition]
    area, i_min = _section(width_mm, height_mm, diameter_mm, area_mm2, i_min_mm4)
    e_mpa = _resolve(youngs_gpa, material, "youngs_gpa", "youngs_gpa") * 1e3
    sy = _resolve(yield_mpa, material, "yield_mpa", "yield_mpa")

    r_gyr = math.sqrt(i_min / area)
    lam = k * length_mm / r_gyr
    lam_t = math.sqrt(2.0 * math.pi ** 2 * e_mpa / sy)

    warnings: list[str] = []
    if lam >= lam_t:
        governing = "euler"
        sigma_cr = math.pi ** 2 * e_mpa / lam ** 2
    else:
        governing = "johnson"
        sigma_cr = sy * (1.0 - sy * lam ** 2 / (4.0 * math.pi ** 2 * e_mpa))
        if lam < 10.0:
            warnings.append(
                f"slenderness {lam:.1f} < 10 — this is plain compression, not a "
                "column; sigma_cr ~ yield")
    p_cr = sigma_cr * area

    sf = None
    if load_n is not None:
        if load_n <= 0:
            raise ValueError("load_n must be > 0")
        sf = p_cr / load_n

    return {
        "end_condition": end_condition,
        "k_factor": k,
        "slenderness": round(lam, 3),
        "transition_slenderness": round(lam_t, 3),
        "governing": governing,
        "sigma_cr_mpa": round(sigma_cr, 4),
        "p_cr_n": round(p_cr, 2),
        "area_mm2": round(area, 4),
        "i_min_mm4": round(i_min, 4),
        "radius_gyration_mm": round(r_gyr, 4),
        "safety_factor": (round(sf, 3) if sf is not None else None),
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "fem_buckling",
    }

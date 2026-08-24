"""Wear, fatigue & fracture — turn a stress number into a durability verdict.

Pure-Python, FreeCAD-free. Simulation family 3 (``docs/SIMULATION_TOOLS.md``):
the closed-form counterpart to ``fem_results`` — feed it a stress (from FEM or a
hand number) plus a material name and it returns life / safety-factor / a `pass`
bool that drops into a merge gate. Strengths, endurance, toughness and service
temperature are read from the Materials DB (``ankusdrive.analysis.materials``) by
name, with explicit overrides and documented fallbacks so every call works
standalone.

Methods (textbook closed-form, documented per function):
    fatigue_check   — S-N (Basquin) life + Goodman mean-stress correction
    fracture_check  — LEFM stress-intensity K vs K_IC, with critical crack size
    wear_estimate   — Archard sliding-wear volume
    creep_flag      — service-temperature screen

Stresses are MPa, lengths mm, forces N, temperatures °C — matching the rest of
``analysis/``. See ``docs/SIMULATION_EXAMPLES.md`` §3 for the worked toys.
"""
from __future__ import annotations

import math

from . import materials


# --- shared helper ------------------------------------------------------------

def _mat_value(material: str | None, accessor: str):
    """Pull a canonical numeric (e.g. 'uts_mpa') from the Materials DB, or None if
    the material/property is unknown. Never raises on a missing material."""
    if not material:
        return None
    try:
        card = materials.get(material)
    except materials.MaterialNotFound:
        return None
    try:
        return materials.numeric(card, accessor)
    except KeyError:
        return None


# --- 1. fatigue ---------------------------------------------------------------

def fatigue_check(
    stress_range_mpa: float,
    mean_stress_mpa: float = 0.0,
    cycles: float = 1_000_000.0,
    material: str = "Steel-1045",
    endurance_mpa: float | None = None,
    uts_mpa: float | None = None,
    s1000_fraction: float = 0.9,
    endurance_cycles: float = 1_000_000.0,
) -> dict:
    """Rate fatigue life (S-N Basquin + Goodman mean-stress correction).

    Amplitude σ_a = stress_range/2, mean σ_m = mean_stress. The infinite-life
    safety factor is Goodman: 1/n = σ_a/σ_e + σ_m/σ_uts. For finite life the mean
    is folded into an equivalent fully-reversed amplitude σ_ar = σ_a/(1−σ_m/σ_uts),
    then read off a log-log S-N line from (1e3, s1000_fraction·σ_uts) to
    (endurance_cycles, σ_e). σ_e defaults to the Materials DB fatigue endurance,
    else 0.4·σ_uts (flagged in `endurance_basis`).

    `pass` = the part survives the required `cycles` (σ_ar ≤ σ_e ⇒ infinite life).
    A tensile mean ≥ σ_uts is a static overload (`pass` false at any cycle count).

    Returns {stress_amplitude_mpa, mean_stress_mpa, endurance_mpa, uts_mpa,
    equiv_reversed_mpa, safety_factor, life_cycles, required_cycles, pass,
    governing_mode, endurance_basis}. Raises ValueError without a usable σ_uts."""
    sa = stress_range_mpa / 2.0
    sm = mean_stress_mpa

    uts = uts_mpa if uts_mpa is not None else _mat_value(material, "uts_mpa")
    if not uts:
        raise ValueError(f"no ultimate strength for {material!r}; pass uts_mpa")

    if endurance_mpa is not None:
        se, basis = endurance_mpa, "explicit"
    else:
        se = _mat_value(material, "fatigue_mpa")
        if se:
            basis = "material"
        else:
            se, basis = 0.4 * uts, "estimated_0.4_uts"

    goodman_denom = sa / se + max(sm, 0.0) / uts
    sf = (1.0 / goodman_denom) if goodman_denom > 0 else float("inf")

    # static-overload screen: a tensile mean (or peak) at/above UTS fails outright.
    if sm >= uts or (sm + sa) >= uts:
        return _fatigue_result(sa, sm, se, uts, None, min(sf, 0.99), 0.0, cycles,
                               False, "static_overload", basis)

    # equivalent fully-reversed amplitude (Goodman) -> S-N line.
    s_ar = sa / (1.0 - sm / uts) if sm < uts else float("inf")
    if s_ar <= se:
        # below the endurance limit: infinite life.
        return _fatigue_result(sa, sm, se, uts, s_ar, sf, None, cycles,
                               True, "infinite_life", basis)

    s1000 = s1000_fraction * uts
    b = math.log10(se / s1000) / math.log10(endurance_cycles / 1e3)
    log_n = 3.0 + (math.log10(s_ar) - math.log10(s1000)) / b
    life = 10.0 ** log_n

    ok = life >= cycles
    mode = "goodman_mean_stress" if sm > 0 else "fully_reversed_fatigue"
    return _fatigue_result(sa, sm, se, uts, s_ar, sf, life, cycles, ok, mode, basis)


def _fatigue_result(sa, sm, se, uts, s_ar, sf, life, cycles, ok, mode, basis):
    return {
        "stress_amplitude_mpa": round(sa, 2),
        "mean_stress_mpa": round(sm, 2),
        "endurance_mpa": round(se, 1),
        "uts_mpa": round(uts, 1),
        "equiv_reversed_mpa": (round(s_ar, 2) if s_ar not in (None,) and math.isfinite(s_ar) else None),
        "safety_factor": (round(sf, 3) if math.isfinite(sf) else None),
        "life_cycles": (round(life) if life is not None else None),
        "required_cycles": round(cycles),
        "pass": ok,
        "governing_mode": mode,
        "endurance_basis": basis,
    }


# --- 2. fracture --------------------------------------------------------------

def fracture_check(
    stress_mpa: float,
    crack_len_mm: float,
    material: str = "Steel-1045",
    geometry_factor: float = 1.12,
    fracture_toughness_mpa_sqrt_m: float | None = None,
) -> dict:
    """Rate brittle fracture (LEFM): K = Y·σ·√(π·a) vs K_IC.

    a is the crack length in mm (converted to m). Y (geometry_factor) defaults to
    1.12 — a single-edge through-crack; use 1.0 for a centre crack. K_IC defaults
    to the Materials DB fracture toughness. The critical crack length inverts the
    same relation: a_c = (K_IC/(Y·σ))²/π.

    Returns {k_applied_mpa_sqrt_m, k_ic_mpa_sqrt_m, geometry_factor,
    safety_factor, margin, critical_crack_mm, pass}. `pass` = K < K_IC. A crack
    past a_c yields safety_factor < 1 and a negative margin (never a false-positive
    SF). Raises ValueError without a usable K_IC or for a non-positive crack."""
    if crack_len_mm <= 0:
        raise ValueError("crack_len_mm must be > 0")
    kic = (fracture_toughness_mpa_sqrt_m if fracture_toughness_mpa_sqrt_m is not None
           else _mat_value(material, "fracture_mpa_sqrt_m"))
    if not kic:
        raise ValueError(
            f"no fracture toughness for {material!r}; pass fracture_toughness_mpa_sqrt_m"
        )
    a_m = crack_len_mm / 1000.0
    k = geometry_factor * stress_mpa * math.sqrt(math.pi * a_m)
    sf = kic / k if k > 0 else float("inf")
    a_c_m = (kic / (geometry_factor * stress_mpa)) ** 2 / math.pi if stress_mpa > 0 else float("inf")
    return {
        "k_applied_mpa_sqrt_m": round(k, 3),
        "k_ic_mpa_sqrt_m": round(kic, 2),
        "geometry_factor": geometry_factor,
        "safety_factor": (round(sf, 3) if math.isfinite(sf) else None),
        "margin": (round(sf - 1.0, 3) if math.isfinite(sf) else None),
        "critical_crack_mm": (round(a_c_m * 1000.0, 3) if math.isfinite(a_c_m) else None),
        "pass": k < kic,
    }


# --- 3. wear ------------------------------------------------------------------

# Order-of-magnitude Archard dimensionless wear coefficients k by category pair
# (unlubricated sliding). Calibrate to the real tribo-pair before trusting; these
# only seed a screen. Keyed by a sorted (category, category) tuple.
_WEAR_COEF = {
    ("steel", "steel"): 5e-4,
    ("aluminum", "steel"): 2e-4,
    ("polymer", "steel"): 1e-5,
    ("polymer", "polymer"): 1e-6,
}
_WEAR_COEF_DEFAULT = 1e-4


def _category(material: str | None):
    if not material:
        return None
    try:
        return materials.get(material).get("category")
    except materials.MaterialNotFound:
        return None


def wear_estimate(
    load_n: float,
    sliding_dist_m: float,
    material_pair: list | None = None,
    wear_coef: float | None = None,
    hardness_mpa: float | None = None,
    apparent_area_mm2: float | None = None,
    max_depth_mm: float | None = None,
) -> dict:
    """Estimate sliding wear (Archard): V = k·F·s/H.

    k (wear_coef) is empirical — pass it explicitly when you have it, else it is
    looked up by the material_pair's category pair (order-of-magnitude only). H
    (hardness_mpa) defaults to the Tabor estimate 3·σ_y of the softer pair member,
    else 1000 MPa. Volume V is in mm³; with apparent_area_mm2 a mean depth_loss is
    reported, gated against max_depth_mm.

    Returns {wear_coef, hardness_mpa, volume_loss_mm3, depth_loss_mm,
    coef_basis, hardness_basis, pass}. Raises ValueError on non-positive H."""
    cats = [(_category(m) or "?") for m in (material_pair or [])]

    if wear_coef is not None:
        k, coef_basis = wear_coef, "explicit"
    elif len(cats) == 2:
        k = _WEAR_COEF.get(tuple(sorted(cats)), _WEAR_COEF_DEFAULT)
        coef_basis = f"category_pair:{'+'.join(sorted(cats))}"
    else:
        k, coef_basis = _WEAR_COEF_DEFAULT, "default"

    if hardness_mpa is not None:
        h, h_basis = hardness_mpa, "explicit"
    elif material_pair:
        yields = [_mat_value(m, "yield_mpa") for m in material_pair]
        yields = [y for y in yields if y]
        if yields:
            h, h_basis = 3.0 * min(yields), "tabor_3x_yield"
        else:
            h, h_basis = 1000.0, "default"
    else:
        h, h_basis = 1000.0, "default"
    if h <= 0:
        raise ValueError("hardness_mpa must be > 0")

    h_pa = h * 1e6
    vol_m3 = k * load_n * sliding_dist_m / h_pa
    vol_mm3 = vol_m3 * 1e9
    depth_mm = (vol_mm3 / apparent_area_mm2) if apparent_area_mm2 else None
    ok = True if (max_depth_mm is None or depth_mm is None) else depth_mm <= max_depth_mm
    return {
        "wear_coef": k,
        "hardness_mpa": round(h, 1),
        "volume_loss_mm3": round(vol_mm3, 4),
        "depth_loss_mm": (round(depth_mm, 5) if depth_mm is not None else None),
        "coef_basis": coef_basis,
        "hardness_basis": h_basis,
        "pass": ok,
    }


# --- 4. creep screen ----------------------------------------------------------

def creep_flag(
    stress_mpa: float,
    temp_c: float,
    material: str = "Steel-1045",
    max_service_temp_c: float | None = None,
) -> dict:
    """Screen for creep risk by comparing operating temperature to the material's
    max service temperature (Materials DB `service_temp_c`, or an override).

    This is a *screen*, not a Larson-Miller life model: it flags when sustained
    load at temperature warrants a real creep analysis. `pass` = operating temp is
    below the service limit. Returns {operating_temp_c, service_temp_c, margin_c,
    stress_mpa, creep_risk, pass, reason}. Raises ValueError without a service
    temperature."""
    t_svc = (max_service_temp_c if max_service_temp_c is not None
             else _mat_value(material, "service_temp_c"))
    if t_svc is None:
        raise ValueError(
            f"no service temperature for {material!r}; pass max_service_temp_c"
        )
    margin = t_svc - temp_c
    risk = temp_c >= t_svc
    return {
        "operating_temp_c": round(temp_c, 1),
        "service_temp_c": round(t_svc, 1),
        "margin_c": round(margin, 1),
        "stress_mpa": round(stress_mpa, 2),
        "creep_risk": risk,
        "pass": not risk,
        "reason": ("operating temp at/above service limit — run a creep analysis"
                   if risk else "below service temperature"),
    }

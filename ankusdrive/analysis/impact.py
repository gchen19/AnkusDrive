"""Drop / impact screen — peak G and crush from drop height, by energy balance.

Pure-Python, FreeCAD-free. A Tier-A screening estimator from
``docs/SIMULATION_NEXT.md``: the first question of every drop spec ("1 m onto
concrete — what does the board see?") answered without a dynamics run.

Energy method, exact: a drop from height h arrested over crush distance d
means the average deceleration satisfies m·g·h = F̄·d, so

    G_avg  = h / d                      (in g's — mass cancels)
    G_peak = pulse_factor · G_avg       (constant-force crush 1.0,
                                         linear spring 2.0, half-sine π/2)
    v_impact = √(2·g·h)                 (exact)

Inverted, a deceleration limit G_lim needs crush d ≥ pulse_factor·h/G_lim —
the cushion-sizing form. The pulse factor IS the design choice (an ideal
crumple is twice as gentle as a spring for the same stroke); beyond these
bounding shapes, real cushion-curve data or the transient contact solve
(``impact_dynamics_submit`` — ``analysis/impact_case.py``, issue #311) takes over.
Lengths mm, mass g.

``bar_impact`` is that solve's closed-form twin: St-Venant's elastic bar on a rigid
wall, the one impact problem with an exact answer, plus its bilinear plastic-wave
extension.
"""
from __future__ import annotations

import math

from . import materials

_G = 9.80665  # m/s^2

_PULSE_FACTORS = {
    "constant": 1.0,        # ideal crush / crumple: flat force over the stroke
    "linear_spring": 2.0,   # elastic cushion: force ramps, peak = 2x average
    "half_sine": math.pi / 2.0,
}


def drop_impact(
    drop_height_mm: float,
    crush_distance_mm: float | None = None,
    deceleration_limit_g: float | None = None,
    pulse: str = "linear_spring",
    mass_g: float | None = None,
) -> dict:
    """Drop/impact screen by exact energy balance (no solver). Give
    ``crush_distance_mm`` (the available cushion/crumple stroke) to get the
    deceleration, OR ``deceleration_limit_g`` (the fragility spec) to get the
    required stroke — exactly one of the two. ``pulse`` bounds the pulse shape:
    'constant' (ideal crush) | 'linear_spring' (elastic cushion) | 'half_sine'.
    Mass cancels from every G; ``mass_g`` only adds peak_force_n and energy_j.

    G_avg = h/d and v = √(2gh) are exact (fidelity="exact"); the pulse factor is
    a stated idealization, not scatter, so band_pct is None. The stroke-mode
    pulse duration is the constant-force value t = 2d/v (other pulses are
    within ~25 % of it). Where the part is stressed, and what an edge or corner
    strike changes, is ``escalate_to='impact_dynamics_submit'``.

    Returns {drop_height_mm, impact_velocity_m_s, pulse, pulse_factor,
    crush_distance_mm, g_avg, g_peak, pulse_duration_ms, deceleration_limit_g,
    required_crush_mm, energy_j, peak_force_n, fidelity, band_pct,
    valid_range_ok, warnings, escalate_to}. Raises ValueError unless exactly one
    of crush_distance_mm / deceleration_limit_g is given (positive), or on an
    unknown pulse."""
    if drop_height_mm <= 0:
        raise ValueError("drop_height_mm must be > 0")
    if pulse not in _PULSE_FACTORS:
        raise ValueError(
            f"unknown pulse {pulse!r}; choose from {sorted(_PULSE_FACTORS)}")
    if (crush_distance_mm is None) == (deceleration_limit_g is None):
        raise ValueError(
            "give exactly one of crush_distance_mm (-> deceleration) or "
            "deceleration_limit_g (-> required crush)")
    factor = _PULSE_FACTORS[pulse]
    h = drop_height_mm
    v = math.sqrt(2.0 * _G * h / 1e3)

    warnings: list[str] = []
    g_avg = g_peak = duration_ms = required_crush = None
    if crush_distance_mm is not None:
        if crush_distance_mm <= 0:
            raise ValueError("crush_distance_mm must be > 0")
        g_avg = h / crush_distance_mm
        g_peak = factor * g_avg
        duration_ms = 2.0 * (crush_distance_mm / 1e3) / v * 1e3  # t = 2d/v
        if crush_distance_mm > h:
            warnings.append(
                "crush stroke exceeds the drop height — sub-1g deceleration; "
                "check the inputs")
    else:
        if deceleration_limit_g <= 0:
            raise ValueError("deceleration_limit_g must be > 0")
        required_crush = factor * h / deceleration_limit_g
        g_avg = deceleration_limit_g / factor
        g_peak = deceleration_limit_g

    mass_kg = mass_g / 1e3 if mass_g is not None else None
    if mass_kg is not None and mass_kg <= 0:
        raise ValueError("mass_g must be > 0")
    energy = mass_kg * _G * h / 1e3 if mass_kg is not None else None
    peak_force = mass_kg * _G * g_peak if mass_kg is not None else None

    return {
        "drop_height_mm": h,
        "impact_velocity_m_s": round(v, 4),
        "pulse": pulse,
        "pulse_factor": round(factor, 4),
        "crush_distance_mm": crush_distance_mm,
        "g_avg": round(g_avg, 3),
        "g_peak": round(g_peak, 3),
        "pulse_duration_ms": (round(duration_ms, 3) if duration_ms is not None else None),
        "deceleration_limit_g": deceleration_limit_g,
        "required_crush_mm": (round(required_crush, 3) if required_crush is not None else None),
        "energy_j": (round(energy, 4) if energy is not None else None),
        "peak_force_n": (round(peak_force, 2) if peak_force is not None else None),
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "impact_dynamics_submit",
    }


def _prop(explicit, material, accessor, what):
    """An explicit value, else ``accessor`` off a Materials-DB card."""
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


def bar_impact(
    velocity_m_s: float,
    length_mm: float,
    youngs_gpa: float | None = None,
    density_kg_m3: float | None = None,
    material: str | None = None,
    area_mm2: float | None = None,
    yield_mpa: float | None = None,
    tangent_mpa: float | None = None,
) -> dict:
    """St-Venant bar impact (no solver) — a uniform bar striking a rigid wall end-on
    at ``velocity_m_s``; the exact twin ``impact_dynamics_submit`` is gated against.

    A compression wave leaves the struck face at the bar speed c₀ = √(E/ρ), bringing
    the material behind it to rest at

        σ = ρ·c₀·v₀                       (exact, 1-D elastic)

    It reflects off the free end as a release wave, and when that returns — after
    T = 2L/c₀ — the bar leaves stress-free at −v₀ (restitution 1). Mass, area and
    length cancel from σ: the only way to lower it is a slower strike or a softer,
    lighter material.

    Past v_y = σ_y/(ρ·c₀) the face yields. For a bilinear material (tangent modulus
    ``tangent_mpa``) an elastic precursor carries σ_y and a slower plastic wave
    (c_p = √(E_t/ρ)) carries the rest, so σ = σ_y + ρ·c_p·(v₀ − v_y); perfectly
    plastic (E_t = 0) caps at σ_y. Give ``yield_mpa`` (or a ``material`` that has it)
    to get that branch; rebound and the 2L/c₀ duration are then no longer exact and
    are returned as None.

    Exact for uniaxial stress (a slender bar; ν drops out) — a squat body is 3-D and
    its wave speed rises toward the dilatational one. Returns {wave_speed_m_s,
    stress_mpa, elastic_stress_mpa, force_n, contact_duration_ms,
    rebound_velocity_m_s, yield_velocity_m_s, plastic, plastic_wave_speed_m_s,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    if velocity_m_s <= 0 or length_mm <= 0:
        raise ValueError("velocity_m_s and length_mm must be > 0")
    if area_mm2 is not None and area_mm2 <= 0:
        raise ValueError("area_mm2 must be > 0")
    e_mpa = _prop(youngs_gpa, material, "youngs_gpa", "youngs_gpa") * 1e3
    rho = _prop(density_kg_m3, material, "density_kg_m3", "density_kg_m3")
    if yield_mpa is None and material:
        try:
            yield_mpa = materials.numeric(materials.get(material), "yield_mpa")
        except (materials.MaterialNotFound, KeyError):
            yield_mpa = None
    if yield_mpa is not None and yield_mpa <= 0:
        raise ValueError("yield_mpa must be > 0")
    et = 0.0 if tangent_mpa is None else float(tangent_mpa)
    if not (0.0 <= et < e_mpa):
        raise ValueError("tangent_mpa must satisfy 0 <= tangent_mpa < E")

    c0 = math.sqrt(e_mpa * 1e6 / rho)                       # m/s
    elastic = rho * c0 * velocity_m_s / 1e6                 # MPa
    v_y = yield_mpa * 1e6 / (rho * c0) if yield_mpa else None
    plastic = v_y is not None and velocity_m_s > v_y
    warnings: list[str] = []
    cp = None
    if plastic:
        cp = math.sqrt(et * 1e6 / rho)
        stress = yield_mpa + rho * cp * (velocity_m_s - v_y) / 1e6
        warnings.append(
            f"strike speed exceeds the yield velocity {v_y:.2f} m/s — the face "
            "yields; stress is the bilinear plastic-wave value, not ρ·c₀·v₀")
    else:
        stress = elastic
    return {
        "wave_speed_m_s": round(c0, 3),
        "stress_mpa": round(stress, 4),
        "elastic_stress_mpa": round(elastic, 4),
        "force_n": round(stress * area_mm2, 4) if area_mm2 is not None else None,
        "contact_duration_ms": (None if plastic
                                else round(2.0 * length_mm / 1e3 / c0 * 1e3, 6)),
        "rebound_velocity_m_s": None if plastic else velocity_m_s,
        "yield_velocity_m_s": round(v_y, 4) if v_y is not None else None,
        "plastic": plastic,
        "plastic_wave_speed_m_s": round(cp, 3) if cp is not None else None,
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not plastic,
        "warnings": warnings,
        "escalate_to": "impact_dynamics_submit",
    }

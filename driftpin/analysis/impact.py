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
bounding shapes, real cushion-curve data or an explicit dynamics run (horizon
scope — nothing shipped to escalate to) takes over. Lengths mm, mass g.
"""
from __future__ import annotations

import math

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
    within ~25 % of it). No explicit impact-dynamics solve is shipped
    (``escalate_to=None`` — horizon scope in SIMULATION_NEXT.md).

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
        "escalate_to": None,
    }

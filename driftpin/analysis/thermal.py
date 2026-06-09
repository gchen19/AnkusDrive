"""Lumped-mass transient thermal — first-order warm-up without a mesh.

Pure-Python, FreeCAD-free. The pure-Python member of simulation family 4
(``docs/SIMULATION_TOOLS.md``): a lumped (single-node RC) estimate that fills the
*time* gap CalculiX steady-state conduction leaves open — "how hot after 5 min,
and does radiation matter?" — without standing up a transient FEM. The
transient/radiation-FEM members stay P2.

Model: a body of mass m and specific heat c_p loses heat convectively from area A
with coefficient h. Steady rise ΔT_ss = P/(h·A); time constant τ = m·c_p/(h·A);
response T(t) = T_amb + ΔT_ss·(1−e^(−t/τ)). A radiation screen compares the
radiative HTC h_rad = ε·σ·(T_s²+T_amb²)(T_s+T_amb) to h — when h_rad exceeds h,
radiation is no longer negligible and a real (nonlinear) analysis is warranted.

Specific heat may be read from the Materials DB by name. Mass is grams, area mm²,
power W, temperatures °C — matching the rest of ``analysis/``. See
``docs/SIMULATION_EXAMPLES.md`` §4 for the worked toy.
"""
from __future__ import annotations

import math

from . import materials

_STEFAN_BOLTZMANN = 5.670374419e-8  # W/m^2/K^4


def _specific_heat(c_p, material: str | None) -> float:
    """Resolve c_p (J/kg/K) from an explicit value/quantity-string, else the
    Materials DB. Raises ValueError when neither yields a number."""
    if c_p is not None:
        return materials.parse_quantity(c_p)[0]
    if material:
        try:
            val = materials.numeric(materials.get(material), "specific_heat_j_kgk")
        except materials.MaterialNotFound:
            val = None
        if val:
            return val
    raise ValueError("provide c_p (J/kg/K) or a material with specific_heat")


def thermal_lumped(
    mass_g: float,
    power_w: float,
    h_conv: float,
    area_mm2: float,
    c_p=None,
    material: str | None = None,
    t_ambient_c: float = 25.0,
    duration_s: float | None = None,
    emissivity: float = 0.8,
) -> dict:
    """Lumped first-order transient warm-up of a convectively-cooled body.

    ΔT_ss = P/(h·A), τ = m·c_p/(h·A), T(t) = T_amb + ΔT_ss·(1−e^(−t/τ)). c_p is an
    explicit value/quantity-string or read from `material`. With `duration_s` the
    temperature and fraction-of-steady reached at that time are returned; without
    it only the steady state and τ. The radiation screen flags when the radiative
    HTC at steady state exceeds h_conv (a linear lumped model then understates
    cooling — escalate to a nonlinear/FEM run).

    Returns {t_ambient_c, delta_t_steady_k, t_steady_c, time_constant_s, t_final_c,
    reached_steady_pct, h_rad_w_m2k, radiation_significant}. Raises ValueError on a
    non-positive h·A or missing c_p."""
    m = mass_g / 1000.0
    cp = _specific_heat(c_p, material)
    area_m2 = area_mm2 * 1e-6
    ha = h_conv * area_m2
    if ha <= 0:
        raise ValueError("h_conv and area_mm2 must be > 0")

    dt_ss = power_w / ha
    t_steady = t_ambient_c + dt_ss
    tau = m * cp / ha

    if duration_s is not None:
        frac = 1.0 - math.exp(-duration_s / tau)
        t_final = t_ambient_c + dt_ss * frac
        reached_pct = 100.0 * frac
    else:
        t_final, reached_pct = None, None

    ts_k = t_steady + 273.15
    ta_k = t_ambient_c + 273.15
    h_rad = emissivity * _STEFAN_BOLTZMANN * (ts_k * ts_k + ta_k * ta_k) * (ts_k + ta_k)

    return {
        "t_ambient_c": round(t_ambient_c, 2),
        "delta_t_steady_k": round(dt_ss, 2),
        "t_steady_c": round(t_steady, 2),
        "time_constant_s": round(tau, 2),
        "t_final_c": (round(t_final, 2) if t_final is not None else None),
        "reached_steady_pct": (round(reached_pct, 1) if reached_pct is not None else None),
        "h_rad_w_m2k": round(h_rad, 3),
        "radiation_significant": h_rad > h_conv,
    }

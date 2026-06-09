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


def _thermal_property(explicit, card, *keys):
    """Resolve a thermal property (SI) from an explicit value/quantity-string, else
    the named Materials-DB card keys. Returns the number or None."""
    if explicit is not None:
        return materials.parse_quantity(explicit)[0]
    for key in keys:
        if key in card:
            return materials.parse_quantity(card[key])[0]
    return None


def _first_eigenvalue(bi: float) -> float:
    """First root ζ₁ of the plane-wall transcendental ζ·tan(ζ) = Bi on (0, π/2).
    f(ζ)=ζ·tanζ−Bi rises monotonically from −Bi to +∞ there, so bisection is exact."""
    lo, hi = 1e-9, math.pi / 2.0 - 1e-9
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if mid * math.tan(mid) - bi > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def thermal_transient_1d(
    half_thickness_mm: float,
    h_conv: float,
    duration_s: float,
    k=None,
    rho=None,
    cp=None,
    alpha_m2_s=None,
    material: str | None = None,
    t_initial_c: float = 100.0,
    t_ambient_c: float = 25.0,
) -> dict:
    """1-D transient conduction in a plane wall of half-thickness L, cooling (or
    heating) toward ambient through surface convection — the one-term series
    (Heisler) solution, the analytic oracle the Elmer ``thermal_transient`` solve is
    gated against (and the *distributed* answer the lumped model only screens).

    θ*(x,t) = C₁·exp(−ζ₁²·Fo)·cos(ζ₁·x/L), with Bi = h·L/k, Fo = α·t/L², ζ₁ the first
    root of ζ·tanζ = Bi, C₁ = 4·sinζ₁/(2ζ₁+sin2ζ₁); the center is x=0, the surface
    x=L. Diffusivity α = k/(ρ·cₚ) from explicit values/quantity-strings or `material`
    (Materials DB), or pass `alpha_m2_s` directly. The one-term form is accurate for
    Fo ≳ 0.2 (``one_term_valid``).

    As Bi→0 the body is isothermal and this collapses to the lumped exponential
    exp(−Bi·Fo)=exp(−t/τ); ``t_center_lumped_c`` and ``lumped_agrees`` expose that
    cross-check. Returns {biot, fourier, eigenvalue_1, c1, t_center_c, t_surface_c,
    t_center_lumped_c, time_constant_s, one_term_valid, lumped_agrees}. Raises
    ValueError if the properties can't be resolved or inputs are non-positive."""
    card = materials.get(material) if material else {}
    L = half_thickness_mm / 1000.0
    if L <= 0 or h_conv <= 0 or duration_s <= 0:
        raise ValueError("half_thickness_mm, h_conv, duration_s must be > 0")

    if alpha_m2_s is not None:
        alpha = float(alpha_m2_s)
        kk = _thermal_property(k, card, "thermal_conductivity")
    else:
        kk = _thermal_property(k, card, "thermal_conductivity")
        rr = _thermal_property(rho, card, "Density", "density")
        cc = _thermal_property(cp, card, "specific_heat", "specific_heat_j_kgk")
        if not (kk and rr and cc):
            raise ValueError(
                "provide alpha_m2_s, or k+rho+cp, or a material with "
                "thermal_conductivity/Density/specific_heat")
        alpha = kk / (rr * cc)
    if not kk:
        raise ValueError("thermal conductivity k is required (explicit or material)")

    bi = h_conv * L / kk
    fo = alpha * duration_s / (L * L)
    zeta1 = _first_eigenvalue(bi)
    c1 = 4.0 * math.sin(zeta1) / (2.0 * zeta1 + math.sin(2.0 * zeta1))

    theta_center = c1 * math.exp(-zeta1 * zeta1 * fo)         # x=0
    theta_surface = theta_center * math.cos(zeta1)            # x=L
    dT = t_initial_c - t_ambient_c
    t_center = t_ambient_c + theta_center * dT
    t_surface = t_ambient_c + theta_surface * dT

    # lumped limit: τ = ρ·cₚ·Lc/h with Lc = L (slab cooled both faces); θ = exp(−Bi·Fo)
    theta_lumped = math.exp(-bi * fo)
    t_center_lumped = t_ambient_c + theta_lumped * dT
    tau = (bi * fo / duration_s) ** -1 if (bi * fo) > 0 else float("inf")  # = ρcpL/h

    return {
        "biot": round(bi, 5),
        "fourier": round(fo, 5),
        "eigenvalue_1": round(zeta1, 5),
        "c1": round(c1, 5),
        "t_center_c": round(t_center, 3),
        "t_surface_c": round(t_surface, 3),
        "t_center_lumped_c": round(t_center_lumped, 3),
        "time_constant_s": round(tau, 3),
        "one_term_valid": fo >= 0.2,
        "lumped_agrees": abs(theta_center - theta_lumped) < 0.05,
    }

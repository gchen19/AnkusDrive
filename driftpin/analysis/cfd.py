"""Internal-flow hydraulics — the exact closed-form gate for the CFD family (§6).

Pure-Python, FreeCAD-free. The unambiguous oracle the kickoff
(docs/SIMULATION_P2_KICKOFF.md) names for CFD: a straight circular pipe. Laminar
flow has the exact Hagen–Poiseuille pressure drop

    Δp = 128·μ·L·Q / (π·D⁴),

with its sharp D⁴ scaling (halving the bore → ~16× Δp) — a mis-scaled CFD solver
fails it immediately. This module is also a genuinely useful first-order internal-
flow screen on its own (no solver): Reynolds number + regime, friction factor
(f = 64/Re laminar; Blasius f = 0.316·Re^−0.25 for smooth turbulent), and wall
shear. The full OpenFOAM/SU2 solve (recirculation, separation, 3-D losses) rides on
the async path and is gated against this band where the physics is exact.

Units: diameter/length mm, flow L/min or velocity m/s, pressure Pa. Fluid μ,ρ from a
small built-in table or explicit values.
"""
from __future__ import annotations

import math

# (dynamic viscosity μ [Pa·s], density ρ [kg/m³]) at ~20 °C, 1 atm.
_FLUIDS = {
    "water-20c": (1.002e-3, 998.2),
    "air-20c": (1.81e-5, 1.204),
    "oil-sae30-20c": (0.29, 891.0),
    "glycerin-20c": (1.41, 1261.0),
}


def _fluid_props(fluid, mu_pa_s, rho_kg_m3):
    """Resolve (μ, ρ) from explicit values, else the named fluid table."""
    m = r = None
    if fluid in _FLUIDS:
        m, r = _FLUIDS[fluid]
    if mu_pa_s is not None:
        m = float(mu_pa_s)
    if rho_kg_m3 is not None:
        r = float(rho_kg_m3)
    if m is None or r is None:
        raise ValueError(
            f"unknown fluid {fluid!r}; known: {sorted(_FLUIDS)} "
            "(or pass mu_pa_s + rho_kg_m3)")
    return m, r


def pipe_pressure_drop(
    diameter_mm: float,
    length_mm: float,
    flow_rate_lpm: float | None = None,
    velocity_m_s: float | None = None,
    fluid: str = "water-20c",
    mu_pa_s=None,
    rho_kg_m3=None,
) -> dict:
    """Steady incompressible pressure drop in a straight circular pipe.

    Give the flow as ``flow_rate_lpm`` (L/min) or ``velocity_m_s``. Re = ρ·V·D/μ sets
    the regime: laminar (Re<2300) uses f = 64/Re — which makes the Darcy drop
    f·(L/D)·(ρV²/2) identical to Hagen–Poiseuille Δp = 128·μ·L·Q/(π·D⁴); turbulent
    (Re>4000) uses the smooth-pipe Blasius f = 0.316·Re^−0.25; the transitional band
    is flagged and screened with the laminar f.

    Returns {reynolds, regime ('laminar'|'transitional'|'turbulent'), velocity_m_s,
    flow_rate_m3_s, friction_factor, pressure_drop_pa, wall_shear_pa,
    hagen_poiseuille_pa (the exact laminar reference, always reported), laminar}.
    Raises ValueError on non-positive geometry or no flow given."""
    mu, rho = _fluid_props(fluid, mu_pa_s, rho_kg_m3)
    D = diameter_mm / 1000.0
    L = length_mm / 1000.0
    if D <= 0 or L <= 0:
        raise ValueError("diameter_mm and length_mm must be > 0")
    area = math.pi * D * D / 4.0

    if flow_rate_lpm is not None:
        Q = float(flow_rate_lpm) / 1000.0 / 60.0      # L/min -> m³/s
        V = Q / area
    elif velocity_m_s is not None:
        V = float(velocity_m_s)
        Q = V * area
    else:
        raise ValueError("provide flow_rate_lpm or velocity_m_s")

    Re = rho * V * D / mu if mu > 0 else float("inf")
    if Re < 2300:
        regime, f = "laminar", (64.0 / Re if Re > 0 else float("inf"))
    elif Re < 4000:
        regime, f = "transitional", (64.0 / Re)       # screen with laminar f
    else:
        regime, f = "turbulent", 0.316 * Re ** -0.25  # Blasius, smooth pipe

    dp = f * (L / D) * (rho * V * V / 2.0)
    tau_w = dp * D / (4.0 * L)                         # τ_w = Δp·D/(4L)
    dp_hp = 128.0 * mu * L * Q / (math.pi * D ** 4)    # exact laminar reference

    return {
        "reynolds": round(Re, 3),
        "regime": regime,
        "velocity_m_s": round(V, 6),
        "flow_rate_m3_s": round(Q, 10),
        "friction_factor": round(f, 6),
        "pressure_drop_pa": round(dp, 4),
        "wall_shear_pa": round(tau_w, 5),
        "hagen_poiseuille_pa": round(dp_hp, 4),
        "laminar": regime == "laminar",
    }


def stokes_sphere_drag(
    diameter_mm: float,
    velocity_m_s: float,
    fluid: str = "water-20c",
    mu_pa_s=None,
    rho_kg_m3=None,
) -> dict:
    """Creeping-flow (Stokes) drag on a sphere — the exact external-flow oracle for the
    CFD family at Re ≪ 1.

    The Stokes drag force is F = 3·π·μ·U·D = 6·π·μ·U·R (exact as Re→0), giving the drag
    coefficient Cd = F/(½·ρ·U²·A) = **24/Re** on the frontal area A = π·R². The form is
    only valid for creeping flow (``stokes_valid`` flags Re < 1; by Re ≈ 1 the true Cd
    already runs ~10 % above 24/Re). Re = ρ·U·D/μ.

    Diameter mm, velocity m/s; fluid μ,ρ from a name or explicit ``mu_pa_s``+``rho_kg_m3``.
    Returns {reynolds, cd, cd_stokes (=24/Re), drag_force_n, frontal_area_m2,
    velocity_m_s, stokes_valid}. Raises ValueError on non-positive geometry/velocity."""
    mu, rho = _fluid_props(fluid, mu_pa_s, rho_kg_m3)
    D = diameter_mm / 1000.0
    if D <= 0 or velocity_m_s <= 0:
        raise ValueError("diameter_mm and velocity_m_s must be > 0")
    R = D / 2.0
    U = float(velocity_m_s)
    Re = rho * U * D / mu if mu > 0 else float("inf")
    drag = 6.0 * math.pi * mu * U * R                 # = 3·π·μ·U·D, exact Stokes
    area = math.pi * R * R
    cd = drag / (0.5 * rho * U * U * area)            # identically 24/Re
    return {
        "reynolds": round(Re, 6),
        "cd": round(cd, 6),
        "cd_stokes": round(24.0 / Re, 6) if Re > 0 else float("inf"),
        "drag_force_n": drag,
        "frontal_area_m2": area,
        "velocity_m_s": U,
        "stokes_valid": Re < 1.0,
    }


def flat_plate_drag(
    length_mm: float,
    velocity_m_s: float,
    width_mm: float | None = None,
    fluid: str = "water-20c",
    mu_pa_s=None,
    rho_kg_m3=None,
) -> dict:
    """Laminar (Blasius) friction drag on one side of a flat plate aligned with the flow.

    The Blasius boundary layer gives the local skin-friction Cf(x) = 0.664/√Re_x and the
    length-averaged **Cf = 1.328/√Re_L** (Re_L = ρ·U·L/μ); the friction drag on one wetted
    side is F = Cf·(½·ρ·U²)·(L·b) for plate length L and width b (default 1 m, i.e. drag
    per unit width). Laminar until transition near Re_L ≈ 5·10⁵ (``laminar``).

    Length/width mm, velocity m/s; fluid μ,ρ from a name or explicit overrides. Returns
    {reynolds_l, cf_avg, drag_force_n (one side), drag_per_width_n_m, dynamic_pressure_pa,
    wetted_area_m2, velocity_m_s, laminar}. Raises ValueError on non-positive
    geometry/velocity."""
    mu, rho = _fluid_props(fluid, mu_pa_s, rho_kg_m3)
    L = length_mm / 1000.0
    if L <= 0 or velocity_m_s <= 0:
        raise ValueError("length_mm and velocity_m_s must be > 0")
    b = (width_mm / 1000.0) if width_mm else 1.0
    U = float(velocity_m_s)
    Re_L = rho * U * L / mu if mu > 0 else float("inf")
    cf_avg = 1.328 / math.sqrt(Re_L) if Re_L > 0 else float("inf")
    q = 0.5 * rho * U * U
    drag_per_width = cf_avg * q * L                   # N per metre of span
    drag = drag_per_width * b
    return {
        "reynolds_l": round(Re_L, 3),
        "cf_avg": round(cf_avg, 8),
        "drag_force_n": drag,
        "drag_per_width_n_m": drag_per_width,
        "dynamic_pressure_pa": round(q, 6),
        "wetted_area_m2": L * b,
        "velocity_m_s": U,
        "laminar": Re_L < 5e5,
    }

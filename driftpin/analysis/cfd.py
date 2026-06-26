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

# (dynamic viscosity μ [Pa·s], density ρ [kg/m³]) at ~20 °C, 1 atm. Constant
# fallback used when CoolProp (the DEFAULT source, issue #100) is unavailable.
_FLUIDS = {
    "water-20c": (1.002e-3, 998.2),
    "air-20c": (1.81e-5, 1.204),
    "oil-sae30-20c": (0.29, 891.0),
    "glycerin-20c": (1.41, 1261.0),
}

# Named fluids CoolProp resolves f(T,P) for → (CoolProp name, T_K) at 1 atm. The
# others (oil/glycerin) stay table-only — CoolProp ships no model for them.
_FLUID_COOLPROP = {
    "water-20c": ("water", 293.15),
    "air-20c": ("air", 293.15),
}


def _fluid_props(fluid, mu_pa_s, rho_kg_m3):
    """Resolve (μ, ρ): CoolProp's EOS by default for the named fluid, the constant
    table when CoolProp is absent, and caller-supplied ``mu_pa_s``/``rho_kg_m3``
    always win (explicit overrides applied last)."""
    m = r = None
    # DEFAULT: CoolProp EOS, but only when the caller gave no explicit overrides
    # (an override means the caller wants its own number, not a looked-up one).
    if fluid in _FLUID_COOLPROP and mu_pa_s is None and rho_kg_m3 is None:
        try:
            from driftpin.analysis import fluids
            cp_name, t_k = _FLUID_COOLPROP[fluid]
            fp = fluids.fluid_props(cp_name, t_k, 101325.0)
            if (fp.get("ok") and fp.get("coolprop_available")
                    and fp.get("valid_range_ok")):
                m, r = fp["viscosity"], fp["density"]
        except Exception:
            pass  # fall through to the constant table
    if (m is None or r is None) and fluid in _FLUIDS:
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


def colebrook_friction_factor(reynolds: float, relative_roughness: float = 0.0) -> float:
    """Darcy friction factor from the Colebrook–White equation,
    1/√f = −2·log₁₀(ε/(3.7·D) + 2.51/(Re·√f)), solved by fixed-point iteration
    (converges in a handful of steps for any turbulent Re). The standard Moody-chart
    correlation (±10 % literature band): smooth pipes track Blasius within ~2 % below
    Re≈10⁵, and the fully-rough limit f = (2·log₁₀(3.7/(ε/D)))⁻² is recovered as
    Re → ∞. Valid for turbulent flow (Re ≳ 4000). Raises ValueError otherwise."""
    if reynolds < 4000:
        raise ValueError("Colebrook is a turbulent correlation — needs Re >= 4000")
    if relative_roughness < 0:
        raise ValueError("relative_roughness must be >= 0")
    f = 0.02
    for _ in range(60):
        f = (-2.0 * math.log10(relative_roughness / 3.7
                               + 2.51 / (reynolds * math.sqrt(f)))) ** -2
    return f


def pipe_pressure_drop(
    diameter_mm: float,
    length_mm: float,
    flow_rate_lpm: float | None = None,
    velocity_m_s: float | None = None,
    fluid: str = "water-20c",
    mu_pa_s=None,
    rho_kg_m3=None,
    roughness_mm: float = 0.0,
) -> dict:
    """Steady incompressible pressure drop in a straight circular pipe.

    Give the flow as ``flow_rate_lpm`` (L/min) or ``velocity_m_s``. Re = ρ·V·D/μ sets
    the regime: laminar (Re<2300) uses f = 64/Re — which makes the Darcy drop
    f·(L/D)·(ρV²/2) identical to Hagen–Poiseuille Δp = 128·μ·L·Q/(π·D⁴); turbulent
    (Re>4000) uses the smooth-pipe Blasius f = 0.316·Re^−0.25, or Colebrook–White
    when a wall ``roughness_mm`` is given (the Colebrook value is always reported
    as ``colebrook_friction_factor`` for turbulent flow); the transitional band is
    flagged and screened with the laminar f.

    Fidelity contract: laminar is exact; the turbulent branch is a correlation
    (fidelity='correlation', band_pct=10 — the Moody-chart scatter). Escalate to
    the OpenFOAM ``cfd_internal_flow_submit`` solve (turbulence='kOmegaSST' past
    Re≈4000).

    Returns {reynolds, regime ('laminar'|'transitional'|'turbulent'), velocity_m_s,
    flow_rate_m3_s, friction_factor, colebrook_friction_factor, relative_roughness,
    pressure_drop_pa, wall_shear_pa, hagen_poiseuille_pa (the exact laminar
    reference, always reported), laminar, fidelity, band_pct, escalate_to}.
    Raises ValueError on non-positive geometry, negative roughness, or no flow."""
    mu, rho = _fluid_props(fluid, mu_pa_s, rho_kg_m3)
    D = diameter_mm / 1000.0
    L = length_mm / 1000.0
    if D <= 0 or L <= 0:
        raise ValueError("diameter_mm and length_mm must be > 0")
    if roughness_mm < 0:
        raise ValueError("roughness_mm must be >= 0")
    rel_rough = (roughness_mm / 1000.0) / D
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
    f_colebrook = None
    if Re < 2300:
        regime, f = "laminar", (64.0 / Re if Re > 0 else float("inf"))
        fidelity, band = "exact", None
    elif Re < 4000:
        regime, f = "transitional", (64.0 / Re)       # screen with laminar f
        fidelity, band = "correlation", None          # indeterminate regime
    else:
        regime = "turbulent"
        f_colebrook = colebrook_friction_factor(Re, rel_rough)
        # smooth default stays Blasius (the historical gate); roughness switches
        # the headline factor to Colebrook, which is the rough-wall correlation
        f = f_colebrook if roughness_mm > 0 else 0.316 * Re ** -0.25
        fidelity, band = "correlation", 10.0

    dp = f * (L / D) * (rho * V * V / 2.0)
    tau_w = dp * D / (4.0 * L)                         # τ_w = Δp·D/(4L)
    dp_hp = 128.0 * mu * L * Q / (math.pi * D ** 4)    # exact laminar reference

    return {
        "reynolds": round(Re, 3),
        "regime": regime,
        "velocity_m_s": round(V, 6),
        "flow_rate_m3_s": round(Q, 10),
        "friction_factor": round(f, 6),
        "colebrook_friction_factor": (round(f_colebrook, 6)
                                      if f_colebrook is not None else None),
        "relative_roughness": round(rel_rough, 8),
        "pressure_drop_pa": round(dp, 4),
        "wall_shear_pa": round(tau_w, 5),
        "hagen_poiseuille_pa": round(dp_hp, 4),
        "laminar": regime == "laminar",
        "fidelity": fidelity,
        "band_pct": band,
        "escalate_to": "cfd_internal_flow_submit",
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


def flat_plate_drag_turbulent(
    length_mm: float,
    velocity_m_s: float,
    width_mm: float | None = None,
    fluid: str = "water-20c",
    mu_pa_s=None,
    rho_kg_m3=None,
    transition_re: float = 5e5,
) -> dict:
    """Turbulent friction drag on one side of a flat plate — the banded oracle the
    kOmegaSST ``cfd_external_flow_submit`` solve is gated against past the laminar
    envelope (SIMULATION_NEXT B3). Two standard correlations, both reported:

        Cf_turbulent = 0.074·Re_L^(−1/5)            (turbulent from the leading edge)
        Cf_mixed     = 0.074·Re_L^(−1/5) − A/Re_L   (laminar run to Re_x = transition_re;
                                                     A = 1742 for the 5·10⁵ default)

    The mixed form is the realistic plate (and what a transition-resolving RANS
    lands on); the fully-turbulent form bounds a tripped plate. The headline drag
    uses Cf_mixed. fidelity='correlation', band_pct=15 (the ±10–15 % scatter of
    the 1/7-power family — gates against this MUST stay banded). Valid for
    5·10⁵ < Re_L < 10⁷ (the 1/7-power envelope); outside it ``valid_range_ok``
    is False with the reason in ``warnings``.

    Returns {reynolds_l, cf_turbulent, cf_mixed, transition_re, drag_force_n
    (one side, mixed), drag_per_width_n_m, cf_laminar_blasius (the screen this
    extends), dynamic_pressure_pa, wetted_area_m2, velocity_m_s, fidelity,
    band_pct, valid_range_ok, warnings, escalate_to}. Raises ValueError on
    non-positive geometry/velocity or a transition_re below 1e5."""
    mu, rho = _fluid_props(fluid, mu_pa_s, rho_kg_m3)
    L = length_mm / 1000.0
    if L <= 0 or velocity_m_s <= 0:
        raise ValueError("length_mm and velocity_m_s must be > 0")
    if transition_re < 1e5:
        raise ValueError("transition_re below 1e5 is not a flat-plate transition")
    b = (width_mm / 1000.0) if width_mm else 1.0
    U = float(velocity_m_s)
    Re_L = rho * U * L / mu if mu > 0 else float("inf")

    warnings: list[str] = []
    if Re_L <= transition_re:
        warnings.append(
            f"Re_L = {Re_L:.3g} is below transition ({transition_re:.3g}) — the "
            "plate is laminar; use the exact Blasius flat_plate_drag instead")
    elif Re_L > 1e7:
        warnings.append(
            f"Re_L = {Re_L:.3g} above the 1e7 1/7-power envelope — use the "
            "Schultz-Grunow/ITTC family beyond it")

    cf_turb = 0.074 * Re_L ** -0.2
    # laminar-run correction: A = Re_xc*(Cf_turb(Re_xc) - Cf_lam(Re_xc))
    a_corr = transition_re * (0.074 * transition_re ** -0.2
                              - 1.328 / math.sqrt(transition_re))
    cf_mixed = cf_turb - a_corr / Re_L
    q = 0.5 * rho * U * U
    drag_per_width = cf_mixed * q * L
    return {
        "reynolds_l": round(Re_L, 3),
        "cf_turbulent": round(cf_turb, 8),
        "cf_mixed": round(cf_mixed, 8),
        "transition_re": transition_re,
        "drag_force_n": drag_per_width * b,
        "drag_per_width_n_m": drag_per_width,
        "cf_laminar_blasius": round(1.328 / math.sqrt(Re_L), 8),
        "dynamic_pressure_pa": round(q, 6),
        "wetted_area_m2": L * b,
        "velocity_m_s": U,
        "fidelity": "correlation",
        "band_pct": 15.0,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "cfd_external_flow_submit",
    }

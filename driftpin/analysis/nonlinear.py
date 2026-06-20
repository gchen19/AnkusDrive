"""Nonlinear-structural closed-form oracles — the analytic anchors the CalculiX
nonlinear FEM path (``fem_set_nonlinear_material`` + ``contact_setup`` + the
``GeometricalNonlinearity='nonlinear'`` solver flag) is gated against.

Pure-Python, FreeCAD-free. Three exact handbook results, each the closed-form
twin of a nonlinear ``ccx`` solve (the pairing ``beam_modal`` ↔ ``fem_modal``
already has for the linear path):

* **Plastic collapse** (``plastic_collapse``) — a rectangular beam/cantilever
  yields at the surface when the bending moment reaches the *yield moment*
  M_y = σ_y·S (S = elastic section modulus b·h²/6) and forms a full plastic
  hinge at the *fully-plastic moment* M_p = σ_y·Z (Z = plastic section modulus
  b·h²/4). The shape factor Z/S = 1.5 for a rectangle is exact. A
  perfectly-plastic ``*PLASTIC`` solve must cap the surface stress at σ_y and
  collapse at M_p — linear theory keeps climbing past it.

* **Large deflection** (``elastica_deflection``) — the end-loaded cantilever
  *elastica* (Bisshopp–Drucker, 1945). With the load parameter
  α = P·L²/(E·I), linear theory δ/L = α/3 over-predicts the tip deflection and
  diverges; the exact elliptic-integral elastica tracks the real (shorter,
  rotated) tip. An ``*NLGEOM`` solve must follow the elastica, not the line.

* **Hertz contact** (``hertz_contact``) — sphere-on-flat (or sphere-on-sphere)
  peak pressure p₀ = 3F/(2πa²), contact radius a = (3FR/4E*)^(1/3), from the
  reduced modulus 1/E* = (1−ν₁²)/E₁ + (1−ν₂²)/E₂. The screening twin of a
  frictional ``*CONTACT PAIR`` solve.

Lengths mm, stresses MPa, forces N, moments N·mm. fidelity="exact" throughout
(elastica/Hertz are exact theory; the quadrature is converged well past test
tolerance). Theory limits (thin-beam, small-strain, half-space) are stated in
each return's ``warnings`` / ``escalate_to``.
"""
from __future__ import annotations

import math

from . import materials


def _resolve(explicit, material, accessor, what):
    """Take an explicit value, else pull `accessor` off a Materials-DB card."""
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


# --- plastic collapse ---------------------------------------------------------

def plastic_collapse(
    length_mm: float,
    width_mm: float,
    height_mm: float,
    yield_mpa: float | None = None,
    material: str | None = None,
    load_n: float | None = None,
    support: str = "cantilever",
) -> dict:
    """Exact plastic-hinge collapse of a solid rectangular beam (no solver) — the
    closed-form twin the perfectly-plastic ``ccx`` solve is gated against. The
    beam bends about the ``width_mm`` axis (depth = ``height_mm``). σ_y from
    ``yield_mpa`` or a Materials-DB ``material``.

    Section moduli (exact): elastic S = b·h²/6, plastic Z = b·h²/4, shape factor
    Z/S = 1.5. Yield moment M_y = σ_y·S (first surface yield), fully-plastic
    moment M_p = σ_y·Z (hinge). ``support`` maps the collapse moment to a tip
    point load: 'cantilever' (M = P·L, hinge at the root) or
    'simply_supported' (central load, M = P·L/4). With ``load_n`` the applied
    root/mid moment and its margin to M_y / M_p are returned.

    A real perfectly-plastic FEM solve caps the surface von Mises at σ_y and
    loses equilibrium as M → M_p; a linear-elastic solve climbs straight past
    both — that contrast is the gate. Escalate to the nonlinear ``ccx`` path
    (``fem_set_nonlinear_material``) for any non-rectangular section, partial
    plasticity field, or combined load this idealization can't see.

    Returns {support, S_elastic_mm3, Z_plastic_mm3, shape_factor, yield_mpa,
    yield_moment_nmm, plastic_moment_nmm, yield_load_n, collapse_load_n,
    applied_moment_nmm, margin_to_yield, margin_to_collapse, regime, fidelity,
    band_pct, valid_range_ok, warnings, escalate_to}."""
    if min(length_mm, width_mm, height_mm) <= 0:
        raise ValueError("length_mm, width_mm, height_mm must all be > 0")
    if support not in ("cantilever", "simply_supported"):
        raise ValueError("support must be 'cantilever' or 'simply_supported'")
    sy = _resolve(yield_mpa, material, "yield_mpa", "yield_mpa")
    b, h, L = float(width_mm), float(height_mm), float(length_mm)

    s_elastic = b * h ** 2 / 6.0          # elastic section modulus
    z_plastic = b * h ** 2 / 4.0          # plastic section modulus
    m_yield = sy * s_elastic
    m_plastic = sy * z_plastic

    # moment arm: M = load * lever for the chosen support
    lever = L if support == "cantilever" else L / 4.0
    p_yield = m_yield / lever
    p_collapse = m_plastic / lever

    warnings: list[str] = []
    if h > L / 4.0:
        warnings.append(
            f"height {h:g} > L/4 — stocky beam, transverse shear is not "
            "negligible; plastic-hinge bending theory under-predicts capacity")

    applied_moment = None
    margin_yield = margin_collapse = None
    regime = None
    if load_n is not None:
        if load_n <= 0:
            raise ValueError("load_n must be > 0")
        applied_moment = load_n * lever
        margin_yield = m_yield / applied_moment
        margin_collapse = m_plastic / applied_moment
        if applied_moment < m_yield:
            regime = "elastic"
        elif applied_moment < m_plastic:
            regime = "partially_plastic"
        else:
            regime = "collapsed"

    return {
        "support": support,
        "S_elastic_mm3": round(s_elastic, 4),
        "Z_plastic_mm3": round(z_plastic, 4),
        "shape_factor": round(z_plastic / s_elastic, 6),
        "yield_mpa": round(sy, 4),
        "yield_moment_nmm": round(m_yield, 4),
        "plastic_moment_nmm": round(m_plastic, 4),
        "yield_load_n": round(p_yield, 4),
        "collapse_load_n": round(p_collapse, 4),
        "applied_moment_nmm": (round(applied_moment, 4)
                               if applied_moment is not None else None),
        "margin_to_yield": (round(margin_yield, 4)
                            if margin_yield is not None else None),
        "margin_to_collapse": (round(margin_collapse, 4)
                               if margin_collapse is not None else None),
        "regime": regime,
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "fem_set_nonlinear_material",
    }


# --- large-deflection elastica ------------------------------------------------

def _elastica_quad(theta0: float, fn, n: int = 2000) -> float:
    """∫₀^θ₀ fn(θ)/√(sinθ₀ − sinθ) dθ via the w = √(sinθ₀ − sinθ) substitution
    that removes the inverse-square-root endpoint singularity:
    dθ = −2w/cosθ · dw, so the integral becomes ∫₀^√sinθ₀ 2·fn(θ)/cosθ dw with a
    smooth integrand for θ₀ < π/2. Composite Simpson over w."""
    s0 = math.sin(theta0)
    wmax = math.sqrt(s0)
    if n % 2:
        n += 1
    dw = wmax / n
    total = 0.0
    for i in range(n + 1):
        w = i * dw
        sin_t = s0 - w * w
        sin_t = max(-1.0, min(1.0, sin_t))
        theta = math.asin(sin_t)
        cos_t = math.sqrt(max(1.0 - sin_t * sin_t, 1e-300))
        val = 2.0 * fn(theta) / cos_t
        weight = 1 if (i == 0 or i == n) else (4 if i % 2 else 2)
        total += weight * val
    return total * dw / 3.0


def _theta0_from_alpha(alpha: float) -> float:
    """Solve √(2α) = ∫₀^θ₀ dθ/√(sinθ₀ − sinθ) for the free-end slope θ₀
    (monotone increasing in α). Bisection on (0, π/2)."""
    target = math.sqrt(2.0 * alpha)
    lo, hi = 1e-7, math.pi / 2.0 - 1e-7
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        if _elastica_quad(mid, lambda _t: 1.0) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def elastica_deflection(
    load_n: float,
    length_mm: float,
    youngs_gpa: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
    i_mm4: float | None = None,
    material: str | None = None,
) -> dict:
    """Exact large-deflection tip of an end-loaded cantilever (Bisshopp–Drucker
    elastica, no solver) — the closed-form twin the ``*NLGEOM`` ``ccx`` solve is
    gated against. Section: ``width_mm``+``height_mm`` (solid rectangle, I =
    b·h³/12, load transverse to ``height_mm``) or an explicit ``i_mm4``. E from
    ``youngs_gpa`` or a Materials-DB ``material``.

    The load parameter is α = P·L²/(E·I). The tip slope θ₀ solves
    √(2α) = ∫₀^θ₀ dθ/√(sinθ₀ − sinθ); the tip then sits at
    x/L = ∫cosθ·/∫, y/L = ∫sinθ·/∫ (same kernel). Linear cantilever theory gives
    δ/L = α/3 and a straight, un-shortened beam — it over-predicts the
    transverse tip and ignores the axial draw-in, both of which the elastica
    captures. The ratio (linear δ)/(elastica δ) is the divergence the nonlinear
    solve must reproduce.

    Valid for θ₀ < ~80° (α up to ~3.5); beyond that the tip rotates past the
    formulation's half-plane and a follower-load / arc-length ``ccx`` solve is
    needed. Returns {alpha, tip_slope_deg, tip_disp_mm (transverse, load
    direction), tip_x_mm (along undeformed axis), axial_drawin_mm,
    linear_tip_mm, nonlinear_over_linear, youngs_mpa, I_mm4, fidelity,
    band_pct, valid_range_ok, warnings, escalate_to}."""
    if load_n <= 0:
        raise ValueError("load_n must be > 0")
    if length_mm <= 0:
        raise ValueError("length_mm must be > 0")
    e_mpa = _resolve(youngs_gpa, material, "youngs_gpa", "youngs_gpa") * 1e3

    if i_mm4 is not None:
        if i_mm4 <= 0:
            raise ValueError("i_mm4 must be > 0")
        inertia = float(i_mm4)
    elif width_mm and height_mm:
        if min(width_mm, height_mm) <= 0:
            raise ValueError("width_mm and height_mm must be > 0")
        inertia = width_mm * height_mm ** 3 / 12.0
    else:
        raise ValueError("give a section: width_mm+height_mm or i_mm4")

    L = float(length_mm)
    alpha = load_n * L ** 2 / (e_mpa * inertia)

    warnings: list[str] = []
    theta0 = _theta0_from_alpha(alpha)
    if theta0 >= math.radians(80.0):
        warnings.append(
            f"tip slope {math.degrees(theta0):.0f}° ≥ 80° — beyond the "
            "Bisshopp–Drucker half-plane; use a follower-load ccx solve")

    denom = _elastica_quad(theta0, lambda _t: 1.0)
    x_over_l = _elastica_quad(theta0, math.cos) / denom
    y_over_l = _elastica_quad(theta0, math.sin) / denom

    tip_disp = y_over_l * L                       # transverse, in load direction
    tip_x = x_over_l * L                          # projection on undeformed axis
    axial_drawin = L - tip_x                       # how much the tip pulls back
    linear_tip = alpha / 3.0 * L                   # PL³/3EI

    return {
        "alpha": round(alpha, 6),
        "tip_slope_deg": round(math.degrees(theta0), 4),
        "tip_disp_mm": round(tip_disp, 6),
        "tip_x_mm": round(tip_x, 6),
        "axial_drawin_mm": round(axial_drawin, 6),
        "linear_tip_mm": round(linear_tip, 6),
        "nonlinear_over_linear": round(tip_disp / linear_tip, 6),
        "youngs_mpa": round(e_mpa, 4),
        "I_mm4": round(inertia, 6),
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "fem_set_nonlinear_material",
    }


# --- Hertz contact ------------------------------------------------------------

def hertz_contact(
    load_n: float,
    radius_mm: float,
    youngs1_gpa: float | None = None,
    poisson1: float | None = None,
    material1: str | None = None,
    radius2_mm: float | None = None,
    youngs2_gpa: float | None = None,
    poisson2: float | None = None,
    material2: str | None = None,
) -> dict:
    """Exact Hertzian point-contact peak pressure (no solver) — the screening twin
    of a frictional ``*CONTACT PAIR`` solve. Sphere of ``radius_mm`` on a flat
    (default) or on a second sphere ``radius2_mm`` (use a negative radius for an
    internal/conforming socket). Each body's elastic constants from
    ``youngs#_gpa``+``poisson#`` or a Materials-DB ``material#``; body 2 defaults
    to the same material as body 1.

    Reduced modulus 1/E* = (1−ν₁²)/E₁ + (1−ν₂²)/E₂; effective radius
    1/R = 1/R₁ + 1/R₂ (R₂ → ∞ for a flat). Contact radius a = (3FR/4E*)^(1/3),
    peak pressure p₀ = 3F/(2πa²) = (1.5×) the mean, mutual approach δ = a²/R.

    Half-space theory: valid while a ≪ R (small contact patch) and the peak
    stays below ~1.6·σ_y (first sub-surface yield) — past that the patch goes
    elastic-plastic and you must escalate to the nonlinear ``ccx`` contact path.
    Returns {e_star_mpa, effective_radius_mm, contact_radius_mm,
    peak_pressure_mpa, mean_pressure_mpa, approach_mm, a_over_R, fidelity,
    band_pct, valid_range_ok, warnings, escalate_to}."""
    if load_n <= 0:
        raise ValueError("load_n must be > 0")
    if radius_mm <= 0:
        raise ValueError("radius_mm must be > 0")
    e1 = _resolve(youngs1_gpa, material1, "youngs_gpa", "youngs1_gpa") * 1e3
    nu1 = _resolve(poisson1, material1, "poisson", "poisson1")
    # body 2 defaults to body 1 (e.g. a steel ball on a steel flat) when neither
    # its own modulus nor a material is given.
    if youngs2_gpa is None and material2 is None:
        e2 = e1
    else:
        e2 = _resolve(youngs2_gpa, material2 or material1, "youngs_gpa", "youngs2_gpa") * 1e3
    if poisson2 is None and material2 is None:
        nu2 = nu1
    else:
        nu2 = _resolve(poisson2, material2 or material1, "poisson", "poisson2")

    inv_estar = (1.0 - nu1 ** 2) / e1 + (1.0 - nu2 ** 2) / e2
    e_star = 1.0 / inv_estar

    inv_r = 1.0 / radius_mm
    if radius2_mm is not None:
        if radius2_mm == 0:
            raise ValueError("radius2_mm must be non-zero (omit for a flat)")
        inv_r += 1.0 / radius2_mm
    if inv_r <= 0:
        raise ValueError("effective radius is non-positive — concave pair too "
                         "conforming for point-contact Hertz theory")
    r_eff = 1.0 / inv_r

    a = (3.0 * load_n * r_eff / (4.0 * e_star)) ** (1.0 / 3.0)
    p0 = 3.0 * load_n / (2.0 * math.pi * a ** 2)
    approach = a ** 2 / r_eff
    a_over_r = a / r_eff

    warnings: list[str] = []
    if a_over_r > 0.1:
        warnings.append(
            f"a/R = {a_over_r:.2f} > 0.1 — contact patch is not small vs the "
            "radius; half-space Hertz theory loses accuracy")

    return {
        "e_star_mpa": round(e_star, 4),
        "effective_radius_mm": round(r_eff, 6),
        "contact_radius_mm": round(a, 6),
        "peak_pressure_mpa": round(p0, 4),
        "mean_pressure_mpa": round(p0 * 2.0 / 3.0, 4),
        "approach_mm": round(approach, 8),
        "a_over_R": round(a_over_r, 6),
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "fem_set_nonlinear_material",
    }

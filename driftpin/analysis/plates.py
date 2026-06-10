"""Plate-bending handbook screen — "do I need FEM at all?".

Pure-Python, FreeCAD-free. A Tier-A screening estimator from
``docs/SIMULATION_NEXT.md``: maximum bending stress and deflection of uniformly
loaded flat plates from the standard Roark/Timoshenko cases, exact within
thin-plate (Kirchhoff) theory:

- **rectangular** (simply supported / clamped, all four edges):
  σ_max = β·q·b²/t², δ_max = α·q·b⁴/(E·t³) with α, β from the ν=0.3 handbook
  table vs aspect ratio a/b (b the SHORT side), linearly interpolated. The
  a/b → ∞ rows are exactly the 1-D beam-strip closed forms (σ = q·b²·6/(8 or
  12)/t², δ = (5 or 1)·q·b⁴/(384·D)) — the built-in cross-check.
- **circular** (simply supported / clamped edge): the exact closed forms
  σ = 3(3+ν)qR²/(8t²) center / 3qR²/(4t²) edge, δ = [(5+ν)/(1+ν) or 1]·qR⁴/(64·D),
  D = E·t³/12(1−ν²).

``fidelity`` is "exact" — but exact *within theory limits*, which the return
polices: ``thin_plate_ok`` (span/thickness ≥ 10; shear deformation grows below)
and ``small_deflection_ok`` (δ ≤ t/2; beyond it membrane stretching stiffens the
real plate and the linear number overestimates δ). Escalation twin: the CCX
``fem_*`` shell/solid pipeline (``fem_run``) — escalate when a validity flag
trips or the margin is thin. Lengths mm, pressure kPa, stresses MPa.
"""
from __future__ import annotations

from . import materials

# (a/b, beta, alpha) for a uniformly loaded rectangular plate, nu = 0.3,
# b = short side: sigma_max = beta*q*b^2/t^2, delta_max = alpha*q*b^4/(E*t^3).
# Last row is a/b -> infinity == the 1-D beam strip (the exact limit).
_RECT_SS = (  # simply supported on all four edges; sigma at the center
    (1.0, 0.2874, 0.0444), (1.2, 0.3762, 0.0616), (1.4, 0.4530, 0.0770),
    (1.6, 0.5172, 0.0906), (1.8, 0.5688, 0.1017), (2.0, 0.6102, 0.1110),
    (3.0, 0.7134, 0.1335), (4.0, 0.7410, 0.1400), (5.0, 0.7476, 0.1417),
    (float("inf"), 0.7500, 0.1421),
)
_RECT_CLAMPED = (  # clamped on all four edges; sigma at mid-long-edge
    (1.0, 0.3078, 0.0138), (1.2, 0.3834, 0.0188), (1.4, 0.4356, 0.0226),
    (1.6, 0.4680, 0.0251), (1.8, 0.4872, 0.0267), (2.0, 0.4974, 0.0277),
    (float("inf"), 0.5000, 0.0284),
)


def _interp_coeffs(table, ratio):
    """(beta, alpha) linearly interpolated in a/b; past the last finite row the
    infinity row applies (the coefficients have converged)."""
    last_finite = table[-2]
    if ratio >= last_finite[0]:
        return table[-1][1], table[-1][2]
    lo = table[0]
    for hi in table[1:]:
        if ratio <= hi[0]:
            t = (ratio - lo[0]) / (hi[0] - lo[0])
            return lo[1] + t * (hi[1] - lo[1]), lo[2] + t * (hi[2] - lo[2])
        lo = hi
    return table[-1][1], table[-1][2]


def _youngs_mpa(youngs_gpa, material):
    if youngs_gpa is not None:
        return youngs_gpa * 1e3
    if material:
        try:
            card = materials.get(material)
        except materials.MaterialNotFound:
            card = None
        if card:
            try:
                return materials.numeric(card, "youngs_mpa")
            except KeyError:
                pass
    raise ValueError("provide youngs_gpa or a material with a Young's modulus")


def _yield_mpa(material):
    if not material:
        return None
    try:
        return materials.numeric(materials.get(material), "yield_mpa")
    except (materials.MaterialNotFound, KeyError):
        return None


def plate_check(
    shape: str,
    thickness_mm: float,
    pressure_kpa: float,
    a_mm: float | None = None,
    b_mm: float | None = None,
    diameter_mm: float | None = None,
    support: str = "simply_supported",
    youngs_gpa: float | None = None,
    material: str | None = None,
    poisson: float = 0.3,
) -> dict:
    """Handbook bending of a uniformly loaded flat plate (no solver) — the
    "do I need FEM at all?" screen. ``shape``: 'rectangular' (a_mm × b_mm, any
    order — the short side drives) | 'circular' (diameter_mm). ``support``:
    'simply_supported' | 'clamped' (all edges). E from ``youngs_gpa`` or a
    Materials-DB ``material`` (which also supplies the yield for
    ``yield_safety_factor`` when present). Rectangular coefficients are the
    ν=0.3 handbook table (interpolated in a/b); circular uses the exact ν-aware
    closed forms.

    Fidelity contract: ``fidelity="exact"`` within thin-plate theory, and the
    theory limits are returned as flags — ``thin_plate_ok`` (span/t ≥ 10) and
    ``small_deflection_ok`` (δ ≤ t/2); a tripped flag means escalate to the CCX
    ``fem_*`` pipeline (``escalate_to="fem_run"``).

    Returns {shape, support, aspect_ratio, beta, alpha, sigma_max_mpa,
    deflection_max_mm, yield_safety_factor, thin_plate_ok, small_deflection_ok,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}. Raises
    ValueError on missing/non-positive dimensions or an unknown shape/support."""
    if thickness_mm <= 0 or pressure_kpa <= 0:
        raise ValueError("thickness_mm and pressure_kpa must be > 0")
    if support not in ("simply_supported", "clamped"):
        raise ValueError("support must be 'simply_supported' or 'clamped'")
    e_mpa = _youngs_mpa(youngs_gpa, material)
    q_mpa = pressure_kpa * 1e-3
    t = thickness_mm
    warnings: list[str] = []

    if shape == "rectangular":
        if not (a_mm and b_mm) or min(a_mm, b_mm) <= 0:
            raise ValueError("rectangular needs positive a_mm and b_mm")
        short, long_ = min(a_mm, b_mm), max(a_mm, b_mm)
        ratio = long_ / short
        table = _RECT_SS if support == "simply_supported" else _RECT_CLAMPED
        beta, alpha = _interp_coeffs(table, ratio)
        sigma = beta * q_mpa * short ** 2 / t ** 2
        delta = alpha * q_mpa * short ** 4 / (e_mpa * t ** 3)
        span = short
        if abs(poisson - 0.3) > 1e-9:
            warnings.append(
                "rectangular coefficients are the ν=0.3 handbook table — "
                f"requested ν={poisson:g} is not applied to them")
    elif shape == "circular":
        if not diameter_mm or diameter_mm <= 0:
            raise ValueError("circular needs positive diameter_mm")
        if not 0.0 <= poisson < 0.5:
            raise ValueError("poisson must be in [0, 0.5)")
        r = diameter_mm / 2.0
        d_flex = e_mpa * t ** 3 / (12.0 * (1.0 - poisson ** 2))
        if support == "simply_supported":
            sigma = 3.0 * (3.0 + poisson) * q_mpa * r ** 2 / (8.0 * t ** 2)
            delta = (5.0 + poisson) / (1.0 + poisson) * q_mpa * r ** 4 / (64.0 * d_flex)
        else:
            sigma = 3.0 * q_mpa * r ** 2 / (4.0 * t ** 2)
            delta = q_mpa * r ** 4 / (64.0 * d_flex)
        ratio, beta, alpha = 1.0, None, None
        span = diameter_mm
    else:
        raise ValueError("shape must be 'rectangular' or 'circular'")

    thin_ok = span / t >= 10.0
    if not thin_ok:
        warnings.append(
            f"span/thickness = {span / t:.1f} < 10 — thick plate, Kirchhoff "
            "theory understates shear deflection")
    small_ok = delta <= t / 2.0
    if not small_ok:
        warnings.append(
            f"deflection {delta:.3g} mm exceeds t/2 — membrane stiffening makes "
            "the real plate stiffer than this linear number")

    y = _yield_mpa(material)
    sf = (y / sigma) if (y and sigma > 0) else None

    return {
        "shape": shape,
        "support": support,
        "aspect_ratio": round(ratio, 4),
        "beta": (round(beta, 4) if beta is not None else None),
        "alpha": (round(alpha, 4) if alpha is not None else None),
        "sigma_max_mpa": round(sigma, 4),
        "deflection_max_mm": round(delta, 5),
        "yield_safety_factor": (round(sf, 3) if sf is not None else None),
        "thin_plate_ok": thin_ok,
        "small_deflection_ok": small_ok,
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "fem_run",
    }

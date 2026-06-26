"""Convection-coefficient screening — handbook correlations, no solver.

Pure-Python, FreeCAD-free. The first Tier-A screening estimator from
``docs/SIMULATION_NEXT.md``: it answers "what h do I feed ``thermal_lumped`` /
``thermal_transient_1d`` / a convection BC?" with a literature correlation
instead of a guess. Natural convection uses Churchill–Chu (vertical plate,
horizontal cylinder); forced convection uses the averaged flat-plate Nusselt
laws and the Hilpert cylinder-in-crossflow table. Air properties are evaluated
at the film temperature via Sutherland-law fits (ideal-gas density at 1 atm);
any other fluid takes explicit film properties.

These are correlations, not exact physics, so every return carries the
fidelity contract from ``SIMULATION_NEXT.md``: ``fidelity = "correlation"``
plus ``band_pct`` (the literature scatter, e.g. Churchill–Chu ±20 %). The
higher-order twin is the Elmer conjugate-heat-transfer solve
(``cht_channel_submit``): escalate when the design margin is within ~2× the
band. Lengths mm, temperatures °C, velocity m/s — matching ``analysis/``.
"""
from __future__ import annotations

import math

from .thermal import _STEFAN_BOLTZMANN

_GRAVITY = 9.80665  # m/s^2

# geometry -> convection mode. Natural geometries require velocity_m_s == 0,
# forced ones require velocity_m_s > 0 — the pairing is part of each
# correlation's identity, so a mismatch raises instead of silently switching.
_NATURAL = ("vertical_plate", "horizontal_cylinder")
_FORCED = ("flat_plate", "cylinder_crossflow")

# Literature scatter per correlation (the ± band an agent should design
# against). Churchill–Chu and Hilpert are usually quoted at ±20 %; the laminar
# flat-plate average is similarity theory and tighter in practice.
_BAND_PCT = {
    "churchill_chu_vertical_plate": 20.0,
    "churchill_chu_horizontal_cylinder": 20.0,
    "flat_plate_laminar": 15.0,
    "flat_plate_mixed": 20.0,
    "hilpert_crossflow": 20.0,
}

# Hilpert (1933) Nu = C·Re^m·Pr^(1/3) for a cylinder in crossflow, by Re band.
_HILPERT_ROWS = (
    (0.4, 4.0, 0.989, 0.330),
    (4.0, 40.0, 0.911, 0.385),
    (40.0, 4000.0, 0.683, 0.466),
    (4000.0, 40000.0, 0.193, 0.618),
    (40000.0, 400000.0, 0.027, 0.805),
)


def _air_film_properties(t_film_k: float) -> dict:
    """Dry air at 1 atm and the film temperature.

    The DEFAULT source is CoolProp's air EOS (``analysis/fluids``, issue #100) when
    the ``fluids`` extra is installed; it falls back transparently to the Sutherland
    viscosity/conductivity fits + ideal-gas density when CoolProp is absent (within
    ~2 % of table air over 250–600 K — well inside every correlation band here).
    β = 1/T (ideal gas) regardless of source."""
    try:
        from driftpin.analysis import fluids
        fp = fluids.fluid_props("air", t_film_k, 101325.0)
        if (fp.get("ok") and fp.get("coolprop_available")
                and fp.get("valid_range_ok")):
            return {
                "k_w_mk": fp["conductivity"],
                "nu_m2_s": fp["kinematic_viscosity"],
                "pr": fp["prandtl"],
                "beta_per_k": 1.0 / t_film_k,  # ideal gas
            }
    except Exception:
        pass  # any CoolProp hiccup → deterministic Sutherland fallback below
    rho = 101325.0 / (287.05 * t_film_k)
    mu = 1.716e-5 * (t_film_k / 273.15) ** 1.5 * (273.15 + 110.4) / (t_film_k + 110.4)
    k = 0.0241 * (t_film_k / 273.15) ** 1.5 * (273.15 + 194.0) / (t_film_k + 194.0)
    cp = 1006.0
    return {
        "k_w_mk": k,
        "nu_m2_s": mu / rho,
        "pr": mu * cp / k,
        "beta_per_k": 1.0 / t_film_k,  # ideal gas
    }


def _churchill_chu(ra: float, pr: float, lead: float, pr_const: float) -> float:
    """The Churchill–Chu all-Ra form Nu = (lead + 0.387·Ra^(1/6)/f(Pr))² with
    f(Pr) = (1+(pr_const/Pr)^(9/16))^(8/27); lead 0.825 (plate) / 0.60 (cylinder)."""
    f_pr = (1.0 + (pr_const / pr) ** (9.0 / 16.0)) ** (8.0 / 27.0)
    root = lead + 0.387 * ra ** (1.0 / 6.0) / f_pr
    return root * root


def h_estimate(
    geometry: str,
    characteristic_mm: float,
    t_surface_c: float,
    t_ambient_c: float = 25.0,
    velocity_m_s: float = 0.0,
    emissivity: float = 0.0,
    fluid: str = "air",
    k_w_mk: float | None = None,
    nu_m2_s: float | None = None,
    pr: float | None = None,
    beta_per_k: float | None = None,
) -> dict:
    """Screening convection coefficient h from handbook correlations (no solver).

    ``geometry`` picks the correlation; the characteristic length is the plate
    height (vertical_plate), the plate length along the flow (flat_plate), or
    the cylinder diameter:

    - natural (``velocity_m_s == 0``): 'vertical_plate' | 'horizontal_cylinder'
      — Churchill–Chu, Nu = (lead + 0.387·Ra^(1/6)/f(Pr))², Ra = g·β·|ΔT|·L³·Pr/ν²
    - forced (``velocity_m_s > 0``): 'flat_plate' — averaged Nu = 0.664·Re^(1/2)·Pr^(1/3)
      laminar, (0.037·Re^(4/5) − 871)·Pr^(1/3) mixed past Re_c = 5·10⁵;
      'cylinder_crossflow' — Hilpert Nu = C·Re^m·Pr^(1/3) by Re band

    Properties are film-temperature air (Sutherland fits, 1 atm) unless
    overridden; a non-air ``fluid`` requires explicit ``k_w_mk`` + ``nu_m2_s`` +
    ``pr`` (+ ``beta_per_k`` for natural). With ``emissivity`` > 0 the linearized
    radiation screen h_rad = ε·σ·(T_s²+T_a²)(T_s+T_a) is added, mirroring
    ``thermal_lumped``: h_total = h_conv + h_rad is the number to feed an
    effective-h model. Outside a correlation's published Re/Ra/Pr range the
    nearest band is still evaluated but ``valid_range_ok`` is False with the
    reason in ``warnings``.

    Fidelity contract: ``fidelity`` is always "correlation" and ``band_pct`` the
    literature scatter (±15–20 %). ``escalate_to`` names the higher-order twin —
    the Elmer CHT solve ``cht_channel_submit`` (or a meshed convection BC via
    ``thermal_transient_submit``); escalate when the thermal margin is within
    ~2× band_pct.

    Returns {geometry, mode, correlation, h_conv_w_m2k, h_rad_w_m2k,
    h_total_w_m2k, nusselt, reynolds, rayleigh, prandtl, film_temp_c,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}. Raises
    ValueError on an unknown geometry, a geometry/velocity mismatch, a
    non-positive length, or a non-air fluid without explicit properties."""
    if geometry not in _NATURAL + _FORCED:
        raise ValueError(
            f"unknown geometry {geometry!r}; choose from {sorted(_NATURAL + _FORCED)}")
    if characteristic_mm <= 0:
        raise ValueError("characteristic_mm must be > 0")
    if velocity_m_s < 0:
        raise ValueError("velocity_m_s must be >= 0")
    if not 0.0 <= emissivity <= 1.0:
        raise ValueError("emissivity must be in [0, 1]")
    natural = geometry in _NATURAL
    if natural and velocity_m_s > 0:
        raise ValueError(
            f"{geometry!r} is a natural-convection correlation — use velocity_m_s=0, "
            "or geometry 'flat_plate'/'cylinder_crossflow' for forced flow")
    if not natural and velocity_m_s == 0:
        raise ValueError(
            f"{geometry!r} is a forced-convection correlation — give velocity_m_s > 0, "
            "or geometry 'vertical_plate'/'horizontal_cylinder' for buoyancy-driven")

    L = characteristic_mm / 1000.0
    t_film_k = 0.5 * (t_surface_c + t_ambient_c) + 273.15
    props = _air_film_properties(t_film_k) if fluid == "air" else {}
    for key, override in (("k_w_mk", k_w_mk), ("nu_m2_s", nu_m2_s),
                          ("pr", pr), ("beta_per_k", beta_per_k)):
        if override is not None:
            props[key] = float(override)
    needed = ("k_w_mk", "nu_m2_s", "pr") + (("beta_per_k",) if natural else ())
    missing = [key for key in needed if not props.get(key)]
    if missing:
        raise ValueError(
            f"fluid {fluid!r} needs explicit film properties: {', '.join(missing)}")
    kf, nu_f, pr_f = props["k_w_mk"], props["nu_m2_s"], props["pr"]

    warnings: list[str] = []
    reynolds = rayleigh = None
    if natural:
        dt = abs(t_surface_c - t_ambient_c)
        rayleigh = _GRAVITY * props["beta_per_k"] * dt * L ** 3 * pr_f / (nu_f * nu_f)
        if dt == 0:
            warnings.append("ΔT = 0 — Ra = 0, conduction-limit Nusselt")
        if geometry == "vertical_plate":
            correlation = "churchill_chu_vertical_plate"
            nusselt = _churchill_chu(rayleigh, pr_f, 0.825, 0.492)
        else:
            correlation = "churchill_chu_horizontal_cylinder"
            nusselt = _churchill_chu(rayleigh, pr_f, 0.60, 0.559)
        if rayleigh > 1e12:
            warnings.append(f"Ra = {rayleigh:.3g} above the 1e12 correlation limit")
    else:
        reynolds = velocity_m_s * L / nu_f
        if geometry == "flat_plate":
            if reynolds <= 5e5:
                correlation = "flat_plate_laminar"
                nusselt = 0.664 * math.sqrt(reynolds) * pr_f ** (1.0 / 3.0)
            else:
                correlation = "flat_plate_mixed"
                nusselt = (0.037 * reynolds ** 0.8 - 871.0) * pr_f ** (1.0 / 3.0)
                if reynolds > 1e8:
                    warnings.append(f"Re = {reynolds:.3g} above the 1e8 correlation limit")
            if not 0.6 <= pr_f <= 60.0:
                warnings.append(f"Pr = {pr_f:.3g} outside the flat-plate 0.6–60 range")
        else:
            correlation = "hilpert_crossflow"
            row = next((r for r in _HILPERT_ROWS if r[0] <= reynolds <= r[1]), None)
            if row is None:
                # evaluate the nearest band anyway — flagged, never silent
                row = _HILPERT_ROWS[0] if reynolds < 0.4 else _HILPERT_ROWS[-1]
                warnings.append(
                    f"Re = {reynolds:.3g} outside the Hilpert 0.4–4e5 table; "
                    "nearest band used")
            _, _, c_coef, m_exp = row
            nusselt = c_coef * reynolds ** m_exp * pr_f ** (1.0 / 3.0)
            if pr_f < 0.7:
                warnings.append(f"Pr = {pr_f:.3g} below the Hilpert Pr ≥ 0.7 range")

    h_conv = nusselt * kf / L

    ts_k = t_surface_c + 273.15
    ta_k = t_ambient_c + 273.15
    h_rad = emissivity * _STEFAN_BOLTZMANN * (ts_k * ts_k + ta_k * ta_k) * (ts_k + ta_k)

    return {
        "geometry": geometry,
        "mode": "natural" if natural else "forced",
        "correlation": correlation,
        "h_conv_w_m2k": round(h_conv, 4),
        "h_rad_w_m2k": round(h_rad, 4),
        "h_total_w_m2k": round(h_conv + h_rad, 4),
        "nusselt": round(nusselt, 4),
        "reynolds": (round(reynolds, 2) if reynolds is not None else None),
        "rayleigh": (round(rayleigh, 2) if rayleigh is not None else None),
        "prandtl": round(pr_f, 4),
        "film_temp_c": round(t_film_k - 273.15, 2),
        "fidelity": "correlation",
        "band_pct": _BAND_PCT[correlation],
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "cht_channel_submit",
    }

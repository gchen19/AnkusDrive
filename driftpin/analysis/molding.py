"""Injection-molding screen — cooling time + fill reach, before any mold flow.

Pure-Python, FreeCAD-free. A Tier-A screening estimator from
``docs/SIMULATION_NEXT.md`` feeding the DfM/cost cluster:

- **Cooling time** — the one-term transient-conduction solution for a plate of
  wall thickness s ejected when its centerline reaches T_eject:
      t_cool = s²/(π²·α) · ln( 8·(T_melt − T_mold) / (π²·(T_eject − T_mold)) )
  Exact (one-term) given the melt diffusivity α — and the dominant share of the
  cycle, which is why thick walls are expensive: t ∝ s².
- **Fill reach** — the spiral-flow chart screen: a runner-to-end flow path
  longer than (L/t)·s won't fill at standard pressures. The per-polymer L/t
  limits are chart midpoints (correlation, ±30 % — actual reach moves with
  injection pressure, melt temperature, and gate design).

Per-polymer defaults (melt/mold/eject temperatures, α, L/t) are typical
datasheet midpoints; every one is overridable. No mold-filling solver is
shipped, so there is no higher-order twin to escalate to yet (a fill solve is
horizon scope). Lengths mm, temperatures °C, diffusivity mm²/s.
"""
from __future__ import annotations

import math

# (t_melt_c, t_mold_c, t_eject_c, alpha_mm2_s, flow_ratio_limit) — typical
# datasheet midpoints for unfilled grades; rough by design (a screen, not a
# datasheet), and each is individually overridable.
#
# SOURCE OF TRUTH: the melt/mold/eject temperatures here mirror the
# melt_temp_c/mold_temp_c/eject_temp_c fields now carried (cited) on the polymer
# cards in driftpin/analysis/materials/seed.json (issue #106), which are the
# authoritative process-data layer. This table is kept in place for now so the
# existing molding_screen API/tests are unchanged; α and flow_ratio_limit are
# screen-only chart values that don't live on the cards. PA66 == the Nylon-6/6
# card. When this estimator is rewired to read materials.numeric(card,
# "melt_temp_c"/...) the cards win; keep the two consistent until then.
_POLYMERS = {
    "ABS":  (240.0, 60.0,  95.0, 0.09, 175.0),
    "PP":   (230.0, 40.0,  90.0, 0.07, 280.0),
    "PC":   (300.0, 90.0, 130.0, 0.11, 130.0),
    "PA66": (280.0, 80.0, 170.0, 0.10, 200.0),
    "POM":  (205.0, 90.0, 120.0, 0.08, 190.0),
    "HDPE": (220.0, 30.0,  80.0, 0.10, 250.0),
    "PS":   (220.0, 40.0,  80.0, 0.09, 220.0),
}


def molding_screen(
    wall_thickness_mm: float,
    material: str | None = None,
    flow_length_mm: float | None = None,
    t_melt_c: float | None = None,
    t_mold_c: float | None = None,
    t_eject_c: float | None = None,
    alpha_mm2_s: float | None = None,
    flow_ratio_limit: float | None = None,
) -> dict:
    """Injection-molding screen (no solver): one-term cooling time + spiral-flow
    fill reach. ``material`` picks per-polymer defaults (ABS | PP | PC | PA66 |
    POM | HDPE | PS); any of ``t_melt_c``/``t_mold_c``/``t_eject_c``/
    ``alpha_mm2_s``/``flow_ratio_limit`` overrides them, and with ALL of the
    first four given no material is needed. ``flow_length_mm`` (the longest
    gate-to-end fill path) enables the fill check: fill_ok when
    flow_length ≤ flow_ratio_limit · wall_thickness.

    Fidelity contract: ``cooling_time_s`` is the exact one-term solution given
    α, so a cooling-only call returns ``fidelity="exact"``; adding the fill
    check makes the headline answer chart-based — ``fidelity="correlation"``,
    ``band_pct=30`` (the cooling number stays exact either way). t ∝ s² is the
    design lever: halve the wall, quarter the cooling. No higher-order
    mold-filling solve is shipped (``escalate_to=None``).

    Returns {material, wall_thickness_mm, t_melt_c, t_mold_c, t_eject_c,
    alpha_mm2_s, cooling_time_s, flow_length_mm, flow_ratio, flow_ratio_limit,
    fill_ok, fidelity, band_pct, valid_range_ok, warnings, escalate_to}. Raises
    ValueError on a non-positive thickness/length, an unknown material, missing
    properties, or temperatures out of order (need mold < eject < melt)."""
    if wall_thickness_mm <= 0:
        raise ValueError("wall_thickness_mm must be > 0")
    if material is not None and material not in _POLYMERS:
        raise ValueError(
            f"unknown material {material!r}; choose from {sorted(_POLYMERS)} "
            "or pass explicit temperatures + alpha_mm2_s")
    defaults = _POLYMERS.get(material, (None,) * 5)
    t_melt = t_melt_c if t_melt_c is not None else defaults[0]
    t_mold = t_mold_c if t_mold_c is not None else defaults[1]
    t_eject = t_eject_c if t_eject_c is not None else defaults[2]
    alpha = alpha_mm2_s if alpha_mm2_s is not None else defaults[3]
    lt_limit = flow_ratio_limit if flow_ratio_limit is not None else defaults[4]
    if None in (t_melt, t_mold, t_eject) or alpha is None:
        raise ValueError(
            "provide a material, or all of t_melt_c/t_mold_c/t_eject_c + alpha_mm2_s")
    if alpha <= 0:
        raise ValueError("alpha_mm2_s must be > 0")
    if not t_mold < t_eject < t_melt:
        raise ValueError(
            f"need t_mold_c < t_eject_c < t_melt_c, got {t_mold} / {t_eject} / {t_melt}")

    s = wall_thickness_mm
    t_cool = (s * s / (math.pi ** 2 * alpha)) * math.log(
        8.0 * (t_melt - t_mold) / (math.pi ** 2 * (t_eject - t_mold)))
    t_cool = max(t_cool, 0.0)  # an eject temp very near melt can drive ln below 0

    warnings: list[str] = []
    fidelity, band = "exact", None
    flow_ratio = fill_ok = None
    if flow_length_mm is not None:
        if flow_length_mm <= 0:
            raise ValueError("flow_length_mm must be > 0")
        if lt_limit is None:
            raise ValueError(
                "fill check needs a material or an explicit flow_ratio_limit")
        flow_ratio = flow_length_mm / s
        fill_ok = flow_ratio <= lt_limit
        fidelity, band = "correlation", 30.0
        if not fill_ok:
            warnings.append(
                f"flow ratio {flow_ratio:.0f} exceeds the ~{lt_limit:.0f}:1 chart "
                "limit — expect a short shot; add gates, thicken the wall, or "
                "raise melt/pressure")
    if s > 4.0:
        warnings.append(
            f"wall {s:g} mm is thick for injection molding — cooling t ∝ s² "
            f"({t_cool:.1f} s) dominates the cycle; consider coring it out")

    return {
        "material": material,
        "wall_thickness_mm": s,
        "t_melt_c": t_melt,
        "t_mold_c": t_mold,
        "t_eject_c": t_eject,
        "alpha_mm2_s": alpha,
        "cooling_time_s": round(t_cool, 3),
        "flow_length_mm": flow_length_mm,
        "flow_ratio": (round(flow_ratio, 2) if flow_ratio is not None else None),
        "flow_ratio_limit": lt_limit,
        "fill_ok": fill_ok,
        "fidelity": fidelity,
        "band_pct": band,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": None,
    }

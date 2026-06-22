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

from . import materials

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


# ============================================================================
# Moldability DFx screen (issue #104) — wall-thickness quality + shrinkage.
#
# Two pure-Python sub-screens (thickness_screen, shrinkage_estimate) and their
# combiner (moldability_screen), in the house verdict shape: pass/score/
# fidelity/band_pct/warnings/escalate_to. No FreeCAD; the geometry-aware path
# (worker.py @handler("moldability_check")) samples walls from a solid and feeds
# thickness_screen.
#
# GRACEFUL DEGRADATION (issue #106 not yet landed): the materials corpus does
# not yet carry per-resin ``recommended_wall_mm``, ``mold_shrinkage_pct``, or a
# ``crystallinity`` flag. Every function reads those fields *if present* on the
# card and otherwise falls back to the built-in tables below, so this ships
# without blocking on the corpus work. When #106 lands, the card fields win.
# ============================================================================

# Generic moldable wall band (mm) by resin — handbook midpoints for a *screen*,
# keyed by both the molding _POLYMERS short names and the materials-DB card
# names (lower-cased lookup tolerates either). Used only when the card lacks a
# ``recommended_wall_mm`` field (issue #106). Global default when neither hits.
_GENERIC_WALL_MM_DEFAULT = (0.8, 4.0)
_GENERIC_WALL_MM = {
    "abs":            (1.0, 3.5),
    "pp":             (0.8, 3.8),
    "polypropylene":  (0.8, 3.8),
    "pc":             (1.0, 4.0),
    "polycarbonate":  (1.0, 4.0),
    "pa66":           (0.8, 3.0),
    "pa6":            (0.8, 3.0),
    "nylon-6/6":      (0.8, 3.0),
    "nylon":          (0.8, 3.0),
    "pom":            (0.8, 3.0),
    "hdpe":           (0.9, 4.0),
    "ldpe":           (0.9, 4.0),
    "pe":             (0.9, 4.0),
    "ps":             (1.0, 3.5),
    "polystyrene":    (1.0, 3.5),
    "pmma":           (1.0, 4.0),
    "pla":            (1.0, 3.5),
}

# Fallback linear CTE (1/K) by resin when the card carries no ``cte``/the
# material is unknown — typical unfilled-grade midpoints. The card's value wins.
_GENERIC_CTE_PER_K = {
    "abs":            90e-6,
    "pp":             100e-6,
    "polypropylene":  100e-6,
    "pc":             68e-6,
    "polycarbonate":  68e-6,
    "pa66":           80e-6,
    "pa6":            80e-6,
    "nylon-6/6":      80e-6,
    "nylon":          80e-6,
    "pom":            110e-6,
    "hdpe":           150e-6,
    "ldpe":           180e-6,
    "pe":             150e-6,
    "ps":             80e-6,
    "polystyrene":    80e-6,
    "pmma":           70e-6,
    "pla":            68e-6,
}

# Published linear mold-shrinkage (%) midpoints — what a molder hands back. Used
# only when the card lacks ``mold_shrinkage_pct`` (issue #106). For amorphous
# resins this is close to the CTE estimate; for semicrystalline ones it is much
# larger (the crystallization-shrink the first-order CTE model underpredicts).
_GENERIC_MOLD_SHRINK_PCT = {
    "abs":            0.6,
    "pp":             1.7,
    "polypropylene":  1.7,
    "pc":             0.6,
    "polycarbonate":  0.6,
    "pa66":           1.5,
    "pa6":            1.4,
    "nylon-6/6":      1.5,
    "nylon":          1.5,
    "pom":            2.0,
    "hdpe":           2.5,
    "ldpe":           2.5,
    "pe":             2.5,
    "ps":             0.5,
    "polystyrene":    0.5,
    "pmma":           0.4,
    "pla":            0.4,
}

# Semicrystalline resins — their crystallization shrink ≫ the first-order CTE
# prediction, so shrinkage_estimate flags model_underpredicts and escalates.
# Issue #106 will add a per-card ``crystallinity`` field; until then this set is
# the oracle (and the card field, if present, overrides per-resin).
_SEMICRYSTALLINE = {
    "pp", "polypropylene", "pe", "hdpe", "ldpe", "pa", "pa6", "pa66",
    "nylon", "nylon-6/6", "pom", "pla",
}


def _resin_key(material) -> str:
    """Normalize a resin/material name for the fallback tables: lower-cased,
    stripped. '' for a missing name."""
    return str(material).strip().lower() if material else ""


def _card_for(material):
    """Return the materials-DB card for ``material`` or None (never raises on a
    miss) — the source of the #106 fields when they exist."""
    if not material:
        return None
    try:
        return materials.get(material)
    except materials.MaterialNotFound:
        return None


def _recommended_wall_band(material, card) -> tuple[float, float, str]:
    """Resolve the recommended wall band (mm) for ``material``: the card's
    ``recommended_wall_mm`` if present (issue #106), else the per-resin generic
    band, else the global default. Returns (lo, hi, source)."""
    if card is not None and card.get("recommended_wall_mm") is not None:
        raw = card["recommended_wall_mm"]
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            lo, hi = float(raw[0]), float(raw[1])
        else:  # a single quantity string/number → treat as the band midpoint ±40%
            v = materials.parse_quantity(raw)[0]
            lo, hi = 0.6 * v, 1.4 * v
        return lo, hi, "card"
    band = _GENERIC_WALL_MM.get(_resin_key(material))
    if band is not None:
        return band[0], band[1], "generic"
    return _GENERIC_WALL_MM_DEFAULT[0], _GENERIC_WALL_MM_DEFAULT[1], "default"


def thickness_screen(
    wall_samples=None,
    nominal_mm: float | None = None,
    material: str | None = None,
    sink_factor: float = 1.5,
    warn_ratio: float = 2.0,
    fail_ratio: float = 3.0,
) -> dict:
    """Wall-thickness quality screen — is the section moldable *and* uniform?

    Pass either ``wall_samples`` (a list of local wall thicknesses, mm — e.g.
    the inward-chord samples the geometry path collects) or a single
    ``nominal_mm`` (or both; samples drive uniformity/sink, nominal anchors the
    range check). The nominal defaults to the sample mean when omitted.

    - **in_range** — nominal within the resin's recommended wall band. The band
      is read from the material card's ``recommended_wall_mm`` when present
      (issue #106), else a per-resin generic band, else a global 0.8–4.0 mm
      default — so this degrades gracefully before the corpus carries the field.
    - **uniformity_ratio** = ``max/min`` across samples; warns above
      ``warn_ratio`` (2), fails above ``fail_ratio`` (3) — non-uniform walls
      drive differential shrink → warp.
    - **sink_risk_samples** — samples exceeding ``sink_factor·nominal`` (k≈1.5):
      thick lobes/bosses sink and need coring.
    - Cooling is tied to the *thickest* wall by calling :func:`molding_screen`
      on ``t_max`` (cooling t ∝ s²) when the resin resolves to a known polymer.

    ``score`` is 1.0 minus penalties for out-of-range, poor uniformity, and the
    sink-flagged fraction; ``pass`` is true only when in-range, uniformity ≤
    fail, and no sink flag. Returns {material, nominal_mm, min_mm, mean_mm,
    max_mm, uniformity_ratio, recommended_wall_mm, wall_band_source, in_range,
    sink_risk_samples, cooling_time_s, pass, score, fidelity, band_pct,
    warnings, escalate_to}. Raises ValueError when neither samples nor nominal
    are given, or a sample/nominal is non-positive."""
    samples: list[float] = []
    if wall_samples is not None:
        samples = [float(s) for s in wall_samples]
    if not samples and nominal_mm is None:
        raise ValueError("thickness_screen needs wall_samples or nominal_mm")
    if any(s <= 0 for s in samples):
        raise ValueError("wall_samples must all be > 0")

    if samples:
        t_min, t_max = min(samples), max(samples)
        t_mean = sum(samples) / len(samples)
    else:
        t_min = t_max = t_mean = float(nominal_mm)

    nominal = float(nominal_mm) if nominal_mm is not None else t_mean
    if nominal <= 0:
        raise ValueError("nominal_mm must be > 0")

    warnings: list[str] = []
    card = _card_for(material)
    lo, hi, band_source = _recommended_wall_band(material, card)
    in_range = lo <= nominal <= hi
    if not in_range:
        side = "thin" if nominal < lo else "thick"
        warnings.append(
            f"nominal wall {nominal:g} mm is out of the recommended "
            f"{lo:g}–{hi:g} mm band for {material or 'this resin'} (too {side})")

    uniformity_ratio = (t_max / t_min) if t_min > 0 else 1.0
    if uniformity_ratio > fail_ratio:
        warnings.append(
            f"wall uniformity {uniformity_ratio:.1f}:1 exceeds {fail_ratio:g}:1 — "
            "differential cooling will warp the part; even out the section")
    elif uniformity_ratio > warn_ratio:
        warnings.append(
            f"wall uniformity {uniformity_ratio:.1f}:1 exceeds {warn_ratio:g}:1 — "
            "watch for differential shrink / warp")

    sink_threshold = sink_factor * nominal
    sink_risk_samples = [round(s, 4) for s in samples if s > sink_threshold]
    if sink_risk_samples:
        warnings.append(
            f"{len(sink_risk_samples)} wall sample(s) exceed {sink_factor:g}×nominal "
            f"({sink_threshold:g} mm) — sink marks / voids; core out the thick sections")

    # Tie cooling to the thickest wall (t ∝ s²). Only when the resin resolves to
    # a known molding polymer; otherwise leave it None (still a valid screen).
    cooling_time_s = None
    if material and _resin_key_to_polymer(material) is not None:
        try:
            cool = molding_screen(t_max, material=_resin_key_to_polymer(material))
            cooling_time_s = cool["cooling_time_s"]
        except ValueError:
            cooling_time_s = None

    # score: start at 1, dock for each failure mode.
    score = 1.0
    if not in_range:
        score -= 0.4
    if uniformity_ratio > fail_ratio:
        score -= 0.4
    elif uniformity_ratio > warn_ratio:
        score -= 0.15
    if samples:
        score -= 0.4 * (len(sink_risk_samples) / len(samples))
    score = max(0.0, score)

    passed = in_range and uniformity_ratio <= fail_ratio and not sink_risk_samples
    return {
        "material": material,
        "nominal_mm": round(nominal, 4),
        "min_mm": round(t_min, 4),
        "mean_mm": round(t_mean, 4),
        "max_mm": round(t_max, 4),
        "uniformity_ratio": round(uniformity_ratio, 4),
        "recommended_wall_mm": [round(lo, 4), round(hi, 4)],
        "wall_band_source": band_source,
        "in_range": in_range,
        "sink_risk_samples": sink_risk_samples,
        "cooling_time_s": cooling_time_s,
        "pass": passed,
        "score": round(score, 4),
        "fidelity": "correlation",
        "band_pct": 30.0,
        "warnings": warnings,
        "escalate_to": ("molding_solve" if not passed else None),
    }


def _resin_key_to_polymer(material) -> str | None:
    """Map a resin/material name to a ``_POLYMERS`` key (for cooling), or None
    if it isn't one of the known molding polymers. Tolerates the DB card names
    ('Polycarbonate' → 'PC', 'Nylon-6/6' → 'PA66', 'Polystyrene' → 'PS')."""
    if not material:
        return None
    if material in _POLYMERS:
        return material
    alias = {
        "polypropylene": "PP", "pp": "PP",
        "polycarbonate": "PC", "pc": "PC",
        "nylon-6/6": "PA66", "nylon": "PA66", "pa66": "PA66", "pa6": "PA66",
        "polystyrene": "PS", "ps": "PS",
        "pom": "POM", "hdpe": "HDPE", "abs": "ABS",
    }
    return alias.get(_resin_key(material))


def _is_semicrystalline(material, card) -> bool:
    """Decide whether a resin is semicrystalline — the card's ``crystallinity``
    field if present (issue #106; a 'semicrystalline'/'crystalline' value or a
    truthy flag), else the built-in ``_SEMICRYSTALLINE`` set."""
    if card is not None and card.get("crystallinity") is not None:
        cz = card["crystallinity"]
        if isinstance(cz, str):
            return cz.strip().lower() in ("semicrystalline", "crystalline", "semi-crystalline")
        return bool(cz)
    return _resin_key(material) in _SEMICRYSTALLINE


def _solidification_temp(material, card, override):
    """Resolve the no-flow/solidification temperature (°C): explicit override,
    else the molding ``_POLYMERS`` eject temp for the resin, else the card's
    ``max_service_temp`` as a rough proxy. None if nothing resolves."""
    if override is not None:
        return float(override)
    pk = _resin_key_to_polymer(material)
    if pk is not None:
        return _POLYMERS[pk][2]  # t_eject_c
    if card is not None:
        st = materials.numeric(card, "service_temp_c")
        if st is not None:
            return st
    return None


def shrinkage_estimate(
    material: str | None = None,
    alpha_per_k: float | None = None,
    t_solidify_c: float | None = None,
    t_ambient_c: float = 23.0,
    nominal_mm=None,
    uniformity_ratio: float | None = None,
    warpage_risk: bool | None = None,
) -> dict:
    """First-order mold-shrinkage estimate from the resin CTE (no flow solver).

    ``S_linear = alpha · ΔT``, ``ΔT = T_solidify − T_ambient``. ``alpha`` (linear
    CTE, 1/K) comes from the explicit ``alpha_per_k`` arg, else the material
    card's ``cte`` (via the ``cte_per_k`` accessor), else a per-resin generic
    fallback. ``T_solidify`` defaults to the resin's eject/no-flow temperature
    (the molding ``_POLYMERS`` table, else the card's service temp); ``T_ambient``
    defaults to 23 °C. Both are overridable.

    - ``S_volumetric ≈ 3·S_linear``; ``cavity_scale_factor = 1/(1−S_linear)``
      (the tool-maker cavity upscale). Given a nominal dim or list ``nominal_mm``
      = L, ``predicted_final_mm`` = ``L·(1−S_linear)``.
    - **Semicrystalline caveat** — for PP/PE/PA/POM/PLA/HDPE/LDPE (or a card
      ``crystallinity`` flag) the crystallization shrink ≫ this CTE estimate, so
      ``model_underpredicts=True`` and ``escalate_to='molding_solve'``. When the
      card (or the generic table) carries a published linear mold-shrinkage, it
      is returned as ``published_shrinkage_pct`` alongside the CTE estimate.
    - **warpage_risk** — passed in directly, or inferred True when
      ``uniformity_ratio`` is poor (>2). Non-uniform walls warp.

    Returns {material, alpha_per_k, t_solidify_c, t_ambient_c, delta_t_c,
    linear_shrinkage_pct, volumetric_shrinkage_pct, cavity_scale_factor,
    predicted_final_mm, published_shrinkage_pct, model_underpredicts,
    warpage_risk, fidelity, band_pct, warnings, escalate_to}. Raises ValueError
    when alpha cannot be resolved or ΔT ≤ 0."""
    card = _card_for(material)

    # alpha: explicit > card cte > generic fallback.
    alpha = alpha_per_k
    if alpha is None and card is not None:
        alpha = materials.numeric(card, "cte_per_k")
    if alpha is None:
        alpha = _GENERIC_CTE_PER_K.get(_resin_key(material))
    if alpha is None:
        raise ValueError(
            "shrinkage_estimate could not resolve a CTE; pass alpha_per_k or a "
            "material with a known cte")
    alpha = float(alpha)

    t_solidify = _solidification_temp(material, card, t_solidify_c)
    if t_solidify is None:
        raise ValueError(
            "shrinkage_estimate could not resolve a solidification temperature; "
            "pass t_solidify_c or a known molding resin")
    delta_t = t_solidify - t_ambient_c
    if delta_t <= 0:
        raise ValueError(
            f"need t_solidify_c > t_ambient_c; got ΔT = {delta_t} °C")

    s_linear = alpha * delta_t                    # fraction
    s_volumetric = 3.0 * s_linear
    cavity_scale_factor = 1.0 / (1.0 - s_linear)

    predicted_final_mm = None
    if nominal_mm is not None:
        if isinstance(nominal_mm, (list, tuple)):
            predicted_final_mm = [round(float(L) * (1.0 - s_linear), 4) for L in nominal_mm]
        else:
            predicted_final_mm = round(float(nominal_mm) * (1.0 - s_linear), 4)

    warnings: list[str] = []
    semicrystalline = _is_semicrystalline(material, card)
    model_underpredicts = semicrystalline
    if semicrystalline:
        warnings.append(
            f"{material or 'this resin'} is semicrystalline — crystallization "
            "shrink exceeds the CTE first-order estimate; treat the number as a "
            "lower bound and escalate to a flow/warp solve")

    # published linear mold-shrinkage: card field (issue #106) > generic table.
    published = None
    if card is not None and card.get("mold_shrinkage_pct") is not None:
        published = float(materials.parse_quantity(card["mold_shrinkage_pct"])[0])
    if published is None:
        published = _GENERIC_MOLD_SHRINK_PCT.get(_resin_key(material))

    if warpage_risk is None:
        warpage_risk = bool(uniformity_ratio is not None and uniformity_ratio > 2.0)
    if warpage_risk:
        warnings.append(
            "non-uniform wall section → differential shrink → warp risk")

    return {
        "material": material,
        "alpha_per_k": alpha,
        "t_solidify_c": round(t_solidify, 3),
        "t_ambient_c": t_ambient_c,
        "delta_t_c": round(delta_t, 3),
        "linear_shrinkage_pct": round(s_linear * 100.0, 4),
        "volumetric_shrinkage_pct": round(s_volumetric * 100.0, 4),
        "cavity_scale_factor": round(cavity_scale_factor, 6),
        "predicted_final_mm": predicted_final_mm,
        "published_shrinkage_pct": (round(published, 4) if published is not None else None),
        "model_underpredicts": model_underpredicts,
        "warpage_risk": warpage_risk,
        "fidelity": "correlation",
        "band_pct": 50.0,
        "warnings": warnings,
        "escalate_to": ("molding_solve" if model_underpredicts else None),
    }


def moldability_screen(
    wall_samples=None,
    nominal_mm: float | None = None,
    material: str | None = None,
    alpha_per_k: float | None = None,
    t_solidify_c: float | None = None,
    t_ambient_c: float = 23.0,
    sink_factor: float = 1.5,
    warn_ratio: float = 2.0,
    fail_ratio: float = 3.0,
) -> dict:
    """Moldability DFx screen — combines the wall-thickness quality screen
    (:func:`thickness_screen`) and the CTE shrinkage estimate
    (:func:`shrinkage_estimate`) into one house verdict.

    Runs ``thickness_screen`` on the wall samples / nominal, then feeds its
    ``uniformity_ratio`` into ``shrinkage_estimate`` so the warp flag is wired.
    The combined ``pass`` is true only when the thickness sub-screen passes *and*
    the shrinkage model does not flag a warp risk or underprediction; ``score``
    is the mean of the sub-scores (thickness ``score`` and a shrinkage proxy).
    This is the low-fidelity gate — ``escalate_to='molding_solve'`` always points
    at the higher-fidelity fill/warp solve.

    Returns {thickness:{…}, shrinkage:{…}, material, pass, score, fidelity,
    band_pct, warnings, escalate_to}."""
    th = thickness_screen(
        wall_samples=wall_samples, nominal_mm=nominal_mm, material=material,
        sink_factor=sink_factor, warn_ratio=warn_ratio, fail_ratio=fail_ratio)
    sh = shrinkage_estimate(
        material=material, alpha_per_k=alpha_per_k, t_solidify_c=t_solidify_c,
        t_ambient_c=t_ambient_c, nominal_mm=nominal_mm,
        uniformity_ratio=th["uniformity_ratio"])

    warnings = list(th["warnings"]) + list(sh["warnings"])
    # shrinkage sub-score proxy: full marks unless it warps or underpredicts.
    sh_score = 1.0
    if sh["warpage_risk"]:
        sh_score -= 0.4
    if sh["model_underpredicts"]:
        sh_score -= 0.2
    sh_score = max(0.0, sh_score)
    score = 0.5 * (th["score"] + sh_score)

    passed = th["pass"] and not sh["warpage_risk"] and not sh["model_underpredicts"]
    return {
        "material": material,
        "thickness": th,
        "shrinkage": sh,
        "pass": passed,
        "score": round(score, 4),
        "fidelity": "correlation",
        "band_pct": 50.0,
        "warnings": warnings,
        "escalate_to": "molding_solve",
    }

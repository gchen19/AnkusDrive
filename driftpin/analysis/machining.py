"""CNC machinability screen + machining-time model (issue #231).

For additive DriftPin has a real manufacturing path (``slice_estimate`` /
``slice_gcode_submit`` via PrusaSlicer). For CNC — ``cost_estimate``'s default
process — there was nothing: no setup reasoning, and a machine-time table that is
explicitly order-of-magnitude (``band_pct=100``). An agent could design a machinable
part and then say nothing true about what it takes to machine it.

This module is the two screening tiers, pure-Python and FreeCAD-free like the rest
of ``analysis/``. **There is no CAM engine here and there is not meant to be** — no
toolpath is computed, no gouge check is run. Real 2.5D toolpaths through FreeCAD's
Path/CAM workbench are a separate, later decision; the value of these two tiers does
not depend on it.

HORIZON, probed and recorded so the next person does not have to: under FreeCAD
1.0.2's ``freecadcmd`` the CAM workbench DOES import headless — ``Path``,
``Path.Base``, ``Path.Op.Profile`` and ``CAMSimulator`` all load. The legacy
``PathScripts.PathJob`` / ``PathScripts.PathProfile`` module names do NOT; they were
renamed to the ``Path.Op.*`` namespace in 1.0, so any recipe written against a
pre-1.0 tutorial will fail on import. The toolpath tier is therefore technically
reachable; it is simply not built here.

    machinability_screen — setups from the tool-approach census, undercuts, tool
                           reach (L/D), internal corner radii, thin walls
    machining_time       — material-removal-rate model: removed volume / MRR for
                           roughing, machined area / area-rate for finishing,
                           per-setup overhead, tolerance scaling

Two decisions shape the screen and are worth stating up front:

* **Stock faces are not machined faces.** A face lying on the stock envelope is a
  sawn/extruded billet surface. Counting it would say a plain rectangular block
  needs six setups, when in practice it needs one (plus facing). Excluding it is
  what makes "a prismatic block is a one-setup part" come out right, and it is what
  makes an undercut stand out instead of drowning in six trivially-blocked faces.
* **Reachability, not visibility.** A face is machinable from a direction when the
  tool can both address it (the outward normal leans toward the tool) and get to it
  (nothing else in the solid is in the way). The occlusion half is a ray cast — the
  SAME ray machinery the moldability tools use for undercut release, deliberately,
  because "no straight pull frees this face" and "no 3-axis approach reaches this
  face" are the same geometric question asked of different direction sets.

The time model's honesty is the point of the second tier: it replaces a flat
volume table with removed volume / MRR, and tightens the declared band from ±100 %
to ±50 % — derived, not asserted, in :func:`machining_time`.

Units: lengths mm, areas mm², volumes mm³, MRR cm³/min, time min (hours where a
field says ``_hr``).
"""
from __future__ import annotations

import math

from . import materials, tolerance_cost

# --- material corpus ----------------------------------------------------------
#
# Roughing MRR and finishing area-rate for a mid-size 40-taper 3-axis VMC with solid
# carbide tooling, at the rates a general job shop actually runs — not catalogue
# maximums and not a high-power HSM cell. Anchor: a 12 mm 3-flute in 6061 at
# ap=10 mm, ae=5 mm, vf=1200 mm/min is 60 cm³/min, which is a completely ordinary
# aluminium roughing cut.
#
# The RATIOS are what this table is for. They track the standard machinability
# ratings (AISI B1112 = 100 %: 6061 ~190 %, 1018 ~78 %, 304 ~45 %, Ti-6Al-4V ~22 %)
# AMPLIFIED by depth-of-cut headroom — a rating compares tool life at matched
# cutting speed, whereas MRR also gets to take a deeper, wider cut in a soft alloy
# on the same spindle. That is why aluminium/steel here is ~5x rather than the
# rating's ~2.4x, and it is the number a shop would recognise.
#
# `finish_cm2_min` is the swept area a finishing pass covers per minute (stepover x
# feed). It falls with material for the same reasons but less steeply: a finish pass
# is limited by surface finish and deflection, not by spindle power.

MATERIAL_CLASSES = {
    "magnesium":      {"mrr_cm3_min": 90.0, "finish_cm2_min": 35.0},
    "plastic":        {"mrr_cm3_min": 100.0, "finish_cm2_min": 40.0},
    "aluminium":      {"mrr_cm3_min": 60.0, "finish_cm2_min": 25.0},
    "brass":          {"mrr_cm3_min": 70.0, "finish_cm2_min": 28.0},
    "copper":         {"mrr_cm3_min": 30.0, "finish_cm2_min": 14.0},
    "composite":      {"mrr_cm3_min": 20.0, "finish_cm2_min": 12.0},
    "cast_iron":      {"mrr_cm3_min": 15.0, "finish_cm2_min": 8.0},
    "steel":          {"mrr_cm3_min": 12.0, "finish_cm2_min": 6.0},
    "alloy_steel":    {"mrr_cm3_min": 8.0,  "finish_cm2_min": 4.0},
    "stainless":      {"mrr_cm3_min": 6.0,  "finish_cm2_min": 3.5},
    "titanium":       {"mrr_cm3_min": 2.5,  "finish_cm2_min": 1.5},
    "hardened_steel": {"mrr_cm3_min": 1.2,  "finish_cm2_min": 0.8},
}

# Materials DB `category` -> machining class. Category is a chemistry bucket, not a
# machinability one, so it only gets us most of the way.
_CATEGORY_CLASS = {
    "aluminum": "aluminium",
    "magnesium": "magnesium",
    "copper": "brass",
    "cast_iron": "cast_iron",
    "steel": "steel",
    "titanium": "titanium",
    "polymer": "plastic",
    "composite": "composite",
    "metal": "brass",          # the Ag/Au entries: soft, free-cutting
}

# Per-NAME overrides, for the materials whose category lies about how they cut.
# Austenitic stainless work-hardens and its category is "steel"; a quenched-and-
# tempered 4140 is ~30 HRC and cuts nothing like a mild steel of the same category.
_NAME_CLASS = {
    "ss304": "stainless",
    "steel-4140-qt": "alloy_steel",
    "copper generic": "copper",
}

# Categories with no milling answer at all. A silent default here would be a
# fabricated number for a process that does not exist, so they raise.
_UNMACHINABLE_CATEGORIES = {"glass"}


def material_class(material: str | None) -> dict:
    """Resolve a material name to a machining class.

    Looks at an explicit per-name override first, then the Materials DB category.
    Returns ``{class, mrr_cm3_min, finish_cm2_min, basis}``. Raises ValueError for
    an unknown material, or one whose category has no milling answer (glass) —
    there is no silent default; pass ``mrr_cm3_min`` explicitly instead."""
    if not material:
        raise ValueError("material is required (or pass mrr_cm3_min explicitly)")
    key = str(material).strip().lower()
    if key in MATERIAL_CLASSES:
        cls, basis = key, "explicit class"
    elif key in _NAME_CLASS:
        cls, basis = _NAME_CLASS[key], f"name override for {material}"
    else:
        try:
            card = materials.get(material)
        except materials.MaterialNotFound:
            raise ValueError(
                f"unknown material {material!r}; pass mrr_cm3_min explicitly or "
                f"name a machining class from {sorted(MATERIAL_CLASSES)}")
        cat = str(card.get("category") or "").lower()
        if cat in _UNMACHINABLE_CATEGORIES:
            raise ValueError(
                f"{material!r} is {cat} — not a milling material; this model has "
                "no MRR for it (grinding/ultrasonic is a different process)")
        if cat not in _CATEGORY_CLASS:
            raise ValueError(
                f"{material!r} has category {cat!r} with no machining class; pass "
                "mrr_cm3_min explicitly")
        cls, basis = _CATEGORY_CLASS[cat], f"Materials DB category {cat!r}"
    row = MATERIAL_CLASSES[cls]
    return {"class": cls, "mrr_cm3_min": row["mrr_cm3_min"],
            "finish_cm2_min": row["finish_cm2_min"], "basis": basis}


# --- the tool-approach census -------------------------------------------------

#: The six principal tool approaches — a 3-axis machine plus re-fixturing, which is
#: what the overwhelming majority of prismatic parts are actually made on. A face
#: reachable from NONE of them needs a 5-axis machine, a special tool, or a redesign,
#: and that is exactly the finding the screen exists to surface.
PRINCIPAL_DIRECTIONS = ["+z", "-z", "+x", "-x", "+y", "-y"]


def setup_cover(faces: list) -> dict:
    """Minimum set of tool-approach directions covering every machined face.

    ``faces`` is a list of ``{name, reachable: [direction names], on_stock: bool}``
    — the worker builds it off the live solid, the screen just counts. Faces marked
    ``on_stock`` are billet surfaces and are excluded (see the module docstring).

    Greedy set cover, iterating :data:`PRINCIPAL_DIRECTIONS` in a FIXED order and
    breaking ties on that order: an optimal cover is NP-hard and pointless at six
    candidate directions, whereas a reproducible answer is load-bearing — the same
    part must never quote two setups on one run and three on the next.

    Returns ``{setups, directions, coverage, unreachable, machined, stock}``."""
    machined = [f for f in faces if not f.get("on_stock")]
    stock = [f for f in faces if f.get("on_stock")]
    unreachable = [str(f.get("name")) for f in machined if not f.get("reachable")]

    remaining = {str(f.get("name")): set(f.get("reachable") or [])
                 for f in machined if f.get("reachable")}
    directions: list = []
    coverage: dict = {}
    while remaining:
        best_dir, best_hit = None, []
        for d in PRINCIPAL_DIRECTIONS:
            hit = sorted(n for n, dirs in remaining.items() if d in dirs)
            if len(hit) > len(best_hit):
                best_dir, best_hit = d, hit
        if best_dir is None:
            break                       # cannot happen: every entry has >=1 direction
        directions.append(best_dir)
        coverage[best_dir] = best_hit
        for n in best_hit:
            remaining.pop(n, None)

    return {
        # A part with no machined faces is still one setup: the billet has to be
        # held and faced. Never quote zero.
        "setups": max(1, len(directions)),
        "directions": directions,
        "coverage": coverage,
        "unreachable": unreachable,
        "machined": len(machined),
        "stock": len(stock),
    }


# --- tier 1: the machinability screen ----------------------------------------

# Standard-stub carbide runs about 3xD of flute, an extended-reach holder 5xD, and
# past ~8xD you are into necked/anti-vibration tooling, reduced feeds and chatter.
# 8 is therefore the ratio at which a pocket stops being ordinary work.
MAX_L_OVER_D = 8.0
# A Ø1 mm cutter is the smallest most job shops will quote without a micro-machining
# conversation, so 0.5 mm is the smallest ordinary internal corner radius.
MIN_TOOL_RADIUS_MM = 0.5
# dfx.dfm_check's CNC min wall is 0.5 mm — the absolute floor. 0.8 is where a wall
# stops chattering and needing support, which is the useful screening line.
MIN_WALL_MM = 0.8
# Beyond three setups the fixturing and datum-transfer cost stops being incidental.
MAX_SETUPS = 3


def machinability_screen(
    faces: list,
    internal_radii: list | None = None,
    wall_samples: list | None = None,
    max_l_over_d: float = MAX_L_OVER_D,
    min_tool_radius_mm: float = MIN_TOOL_RADIUS_MM,
    min_wall_mm: float = MIN_WALL_MM,
    max_setups: int = MAX_SETUPS,
) -> dict:
    """Screen a part for 3-axis machinability from pure geometry — no CAM engine.

    ``faces``: ``{name, reachable: [dirs], on_stock: bool, area_mm2}`` per face.
    ``internal_radii``: ``{name, radius_mm, depth_mm}`` per concave internal corner
    or pocket — the corner radius caps the tool diameter and the depth sets the
    reach it needs, which together are the classic ``L/D`` machinability number.
    ``wall_samples``: ``{name, wall_mm}`` (or plain floats) from thickness probes.

    Four findings, each with a code an agent can branch on:

    * ``undercut`` — a machined face no principal approach reaches. 5-axis, a
      special tool, or a redesign; this is what fails a part.
    * ``deep_pocket`` — ``depth / (2·radius) > max_l_over_d``: the corner radius
      forces a cutter too small to reach the bottom without special tooling.
    * ``small_radius`` — an internal corner tighter than ``min_tool_radius_mm``,
      i.e. below the smallest cutter the shop will quote. Almost always cheaper to
      open the radius than to buy the tool. A radius of exactly 0 is reported
      separately as ``sharp_internal_corner``: that is not a small radius a smaller
      cutter would reach, it is geometry no rotating tool can produce.
    * ``thin_wall`` — below ``min_wall_mm``: it will deflect and chatter.

    Plus ``many_setups`` as a warning (not a failure) past ``max_setups``.

    ``pass`` is false when any of the four findings fires. ``score`` is
    ``1 − findings/machined_faces``, clamped to [0, 1], for ranking variants.

    Fidelity ``correlation``, ``band_pct`` None: this is an ordinal screen, not a
    measurement — there is no physical quantity for a band to be a band OF. It
    escalates to ``cnc_time_estimate`` for the quantitative tier.

    Returns ``{setups, setup_directions, coverage, machined_faces, stock_faces,
    machined_area_mm2, min_internal_radius_mm, max_l_over_d_seen, findings,
    undercut_faces, score, pass, fidelity, band_pct, basis, escalate_to,
    limitations}``."""
    cover = setup_cover(faces)
    machined = [f for f in faces if not f.get("on_stock")]
    machined_area = sum(float(f.get("area_mm2") or 0.0) for f in machined)

    findings: list = []
    for name in cover["unreachable"]:
        findings.append({
            "code": "undercut", "severity": "fail", "feature": name,
            "detail": f"{name} is reachable from none of "
                      f"{'/'.join(PRINCIPAL_DIRECTIONS)} — no 3-axis approach "
                      "addresses it; 5-axis, a T-slot/undercut cutter, or a "
                      "redesign",
        })

    radii = list(internal_radii or [])
    min_radius = None
    max_ld = None
    for r in radii:
        name = str(r.get("name", "corner"))
        radius = float(r.get("radius_mm") or 0.0)
        depth = float(r.get("depth_mm") or 0.0)
        if radius < 0:
            continue
        min_radius = radius if min_radius is None else min(min_radius, radius)
        if radius == 0.0:
            # A square internal corner. This is not "a very small radius" that a
            # smaller cutter would reach — a rotating tool cannot produce it AT ALL,
            # so it gets its own code and short-circuits the L/D arithmetic (which
            # would divide by zero anyway).
            findings.append({
                "code": "sharp_internal_corner", "severity": "fail",
                "feature": name,
                "detail": "square internal corner — no rotating cutter can produce "
                          f"one; specify a fillet of at least R{min_tool_radius_mm:g} "
                          "(or relieve the corner) unless it is EDM'd",
            })
            continue
        if radius < min_tool_radius_mm:
            findings.append({
                "code": "small_radius", "severity": "fail", "feature": name,
                "detail": f"internal corner R{radius:g} needs a "
                          f"Ø{2 * radius:g} cutter, below the Ø"
                          f"{2 * min_tool_radius_mm:g} screening floor — open the "
                          "radius if the function allows",
            })
        ld = depth / (2.0 * radius)
        max_ld = ld if max_ld is None else max(max_ld, ld)
        if ld > max_l_over_d:
            findings.append({
                "code": "deep_pocket", "severity": "fail", "feature": name,
                "detail": f"{depth:g} mm deep on a Ø{2 * radius:g} cutter is "
                          f"L/D {ld:.1f} (> {max_l_over_d:g}) — special tooling, "
                          "reduced feeds, chatter risk",
            })

    for w in (wall_samples or []):
        if isinstance(w, dict):
            name, wall = str(w.get("name", "wall")), float(w.get("wall_mm") or 0.0)
        else:
            name, wall = "wall", float(w)
        if 0.0 < wall < min_wall_mm:
            findings.append({
                "code": "thin_wall", "severity": "fail", "feature": name,
                "detail": f"{wall:g} mm wall is below the {min_wall_mm:g} mm "
                          "screening floor — it will deflect under the cutter",
            })

    warnings: list = []
    if cover["setups"] > max_setups:
        warnings.append({
            "code": "many_setups", "severity": "warn", "feature": None,
            "detail": f"{cover['setups']} tool-approach directions (> "
                      f"{max_setups}) — each is a fixture, a datum transfer and a "
                      "position-tolerance stack; consider consolidating features "
                      "onto fewer faces",
        })

    n = max(len(machined), 1)
    score = max(0.0, min(1.0, 1.0 - len(findings) / n))
    return {
        "setups": cover["setups"],
        "setup_directions": cover["directions"],
        "coverage": cover["coverage"],
        "machined_faces": len(machined),
        "stock_faces": cover["stock"],
        "machined_area_mm2": round(machined_area, 3),
        "min_internal_radius_mm": (round(min_radius, 4)
                                   if min_radius is not None else None),
        "max_l_over_d_seen": round(max_ld, 3) if max_ld is not None else None,
        "undercut_faces": cover["unreachable"],
        "findings": findings,
        "warnings": warnings,
        "score": round(score, 4),
        "pass": not findings,
        "fidelity": "correlation",
        # An ordinal screen — no measured quantity, so no scatter band applies.
        "band_pct": None,
        "basis": ("3-axis reachability over the six principal approaches + tool "
                  "L/D from internal corner radii; no toolpath is computed"),
        "escalate_to": "cnc_time_estimate",
        "limitations": (
            "reachability is sampled at each face centroid, so a face that is "
            "partly shadowed can read as reachable; pocket depth comes from the "
            "corner-radius records, not from a swept toolpath; and nothing here "
            "checks holder collision"),
    }


# --- tier 2: the machining-time model -----------------------------------------

# Fraction of the clock a spindle is actually cutting: the rest is rapids, tool
# changes, probing, chip clearing and lid-open time. 0.65 is the ordinary
# lights-on job-shop figure for prismatic work.
UTILISATION = 0.65
# Per-setup human + machine overhead: tear down, re-fixture, indicate, re-probe,
# first-part check.
SETUP_MIN = 15.0
# Stock is bought oversize; 2 mm per side off a sawn billet is the normal allowance
# for a part whose faces get machined.
STOCK_ALLOWANCE_MM = 2.0

# --- where the declared band comes from (issue #231 asks for a justified number) ---
#
# The model has four inputs. Two are EXACT off the geometry — the removed volume
# (stock box minus part volume) and the machined area — and contribute no scatter of
# their own beyond the stock-allowance assumption, which the caller controls.
# The scatter lives in the other two:
#
#   * MRR: the dominant term. The same material on a 30-taper mill, a rigid 40-taper
#     VMC, and an HSM cell spans roughly x0.6 to x1.6 about the corpus value
#     (spindle power, rigidity, tool choice, conventional vs. trochoidal). Call it
#     +/-40 %.
#   * utilisation: shops run 0.5 to 0.8 cut/air about our 0.65 — +/-25 %.
#
# They are independent, so RSS: sqrt(0.40^2 + 0.25^2) = 0.47 -> 50 % rounded up
# rather than down, because rounding an uncertainty DOWN is how a screen starts
# lying. That is a 2x tightening on the flat table's +/-100 %, and it sits at the
# top of the +/-30-50 % the issue asked for — claimed honestly, not wished into
# the tighter end.
#
# When the caller supplies a MEASURED shop MRR the dominant term disappears and only
# utilisation and setup scatter remain: sqrt(0.25^2 + small) -> 30 %.
BAND_PCT = 50.0
BAND_PCT_MEASURED_MRR = 30.0


def machining_time(
    part_volume_mm3: float,
    bbox_mm: list,
    machined_area_mm2: float | None = None,
    material: str | None = None,
    setups: int = 1,
    stock_allowance_mm: float = STOCK_ALLOWANCE_MM,
    setup_min: float = SETUP_MIN,
    utilisation: float = UTILISATION,
    tolerance_class=None,
    mrr_cm3_min: float | None = None,
    finish_cm2_min: float | None = None,
) -> dict:
    """Machining time for one part, from a material-removal-rate model.

        stock            = (L + 2a)(W + 2a)(H + 2a)          a = stock_allowance_mm
        removed          = stock − part_volume               (the chips)
        roughing_min     = removed_cm3 / MRR
        finishing_min    = machined_area_cm2 / finish_area_rate
        cutting_min      = (roughing + finishing) / utilisation · tolerance_factor
        total_min        = cutting_min + setups · setup_min

    MRR and the finishing area-rate come from :data:`MATERIAL_CLASSES` via
    :func:`material_class`, or from the explicit overrides — pass a measured shop
    rate and the declared band tightens (see :data:`BAND_PCT_MEASURED_MRR`).

    ``tolerance_class`` (``'IT7'``, ``7``, …) scales the CUTTING time through the
    shared tolerance–cost corpus (:func:`tolerance_cost.time_factor`, process
    ``cnc``): holding a grade tighter than the cell's natural IT9 means slower
    finish passes, spring passes and in-process gauging. It is the same curve
    ``cost_estimate`` uses, so a tolerance costs the same thing in both places.
    ``None`` leaves the factor at exactly 1.0.

    Setup time is NOT scaled by utilisation or tolerance — it is wall-clock work
    that happens once per orientation regardless of how the part cuts.

    Fidelity ``correlation`` at ``band_pct`` :data:`BAND_PCT` — derived in this
    module's source from the MRR and utilisation scatter, not asserted. Feed
    ``machine_time_hr`` to ``cost_estimate`` to replace its flat volume table.

    Returns ``{machine_time_min, machine_time_hr, roughing_min, finishing_min,
    cutting_min, setup_min_total, removed_volume_mm3, stock_volume_mm3,
    removal_fraction, machined_area_mm2, setups, material_class, mrr_cm3_min,
    finish_cm2_min, utilisation, tolerance, fidelity, band_pct, basis, warnings}``.
    Raises ValueError on a non-positive volume/bbox, a part larger than its own
    stock envelope, a utilisation outside (0, 1], or an unusable material."""
    if part_volume_mm3 <= 0:
        raise ValueError("part_volume_mm3 must be > 0")
    if len(bbox_mm) != 3 or any(float(d) <= 0 for d in bbox_mm):
        raise ValueError("bbox_mm must be [l, w, h] with every dimension > 0")
    if not (0.0 < utilisation <= 1.0):
        raise ValueError("utilisation must be in (0, 1]")
    if setups < 1:
        raise ValueError("setups must be >= 1")
    if stock_allowance_mm < 0:
        raise ValueError("stock_allowance_mm must be >= 0")

    a = float(stock_allowance_mm)
    l, w, h = (float(d) for d in bbox_mm)
    stock_volume = (l + 2 * a) * (w + 2 * a) * (h + 2 * a)
    removed = stock_volume - float(part_volume_mm3)
    if removed < 0:
        raise ValueError(
            f"part_volume_mm3 ({part_volume_mm3:g}) exceeds its stock envelope "
            f"({stock_volume:g} mm³) — the volume and bbox do not describe the "
            "same solid")

    warnings: list = []
    if machined_area_mm2 is None:
        # No area supplied: fall back to the stock envelope's surface area. It is a
        # LOWER bound on the finishing work for anything with a pocket in it, so
        # say so rather than letting the number pass as measured.
        machined_area_mm2 = 2.0 * (l * w + l * h + w * h)
        warnings.append(
            "machined_area_mm2 not supplied — using the stock envelope's surface "
            "area, which under-counts finishing on any part with internal features")

    if mrr_cm3_min is not None and finish_cm2_min is not None:
        cls = {"class": "explicit", "mrr_cm3_min": float(mrr_cm3_min),
               "finish_cm2_min": float(finish_cm2_min), "basis": "explicit rates"}
    else:
        cls = material_class(material)
        if mrr_cm3_min is not None:
            cls = dict(cls, mrr_cm3_min=float(mrr_cm3_min),
                       basis=cls["basis"] + " + explicit MRR")
        if finish_cm2_min is not None:
            cls = dict(cls, finish_cm2_min=float(finish_cm2_min))
    if cls["mrr_cm3_min"] <= 0 or cls["finish_cm2_min"] <= 0:
        raise ValueError("mrr_cm3_min and finish_cm2_min must be > 0")

    tol = tolerance_cost.time_factor(tolerance_class, process="cnc")

    roughing_min = (removed / 1000.0) / cls["mrr_cm3_min"]
    finishing_min = (float(machined_area_mm2) / 100.0) / cls["finish_cm2_min"]
    cutting_min = (roughing_min + finishing_min) / utilisation * tol["factor"]
    setup_total = float(setups) * float(setup_min)
    total_min = cutting_min + setup_total

    band = (BAND_PCT_MEASURED_MRR if mrr_cm3_min is not None else BAND_PCT)
    return {
        "machine_time_min": round(total_min, 4),
        "machine_time_hr": round(total_min / 60.0, 6),
        "roughing_min": round(roughing_min, 4),
        "finishing_min": round(finishing_min, 4),
        "cutting_min": round(cutting_min, 4),
        "setup_min_total": round(setup_total, 4),
        "removed_volume_mm3": round(removed, 3),
        "stock_volume_mm3": round(stock_volume, 3),
        "removal_fraction": round(removed / stock_volume, 6),
        "machined_area_mm2": round(float(machined_area_mm2), 3),
        "setups": int(setups),
        "stock_allowance_mm": a,
        "material": material,
        "material_class": cls["class"],
        "material_basis": cls["basis"],
        "mrr_cm3_min": cls["mrr_cm3_min"],
        "finish_cm2_min": cls["finish_cm2_min"],
        "utilisation": utilisation,
        "tolerance": tol,
        "fidelity": "correlation",
        "band_pct": band,
        "basis": (
            f"removed volume / MRR + machined area / finish rate, at "
            f"{utilisation:g} cut-time utilisation and {setup_min:g} min per "
            f"setup. Band is the RSS of MRR scatter (±40 %) and utilisation "
            f"scatter (±25 %)"
            + (" — MRR supplied, so only the utilisation term remains"
               if mrr_cm3_min is not None else "")),
        "warnings": warnings,
        "escalate_to": None,
    }


def hole_time(diameter_mm: float, depth_mm: float, material: str | None = None,
              mrr_cm3_min: float | None = None) -> float:
    """Drilling time (min) for one hole, as a volume through the same MRR model.

    Drilling is a much lower MRR process than milling (one point of contact, chip
    evacuation up a flute), so it runs at a quarter of the milling rate — the
    ordinary shop ratio. Small helper, used to sanity-check the bulk model against
    a feature nobody has to guess about."""
    if diameter_mm <= 0 or depth_mm <= 0:
        raise ValueError("diameter_mm and depth_mm must be > 0")
    rate = mrr_cm3_min if mrr_cm3_min is not None else material_class(
        material)["mrr_cm3_min"]
    volume_cm3 = math.pi * (diameter_mm / 2.0) ** 2 * depth_mm / 1000.0
    return round(volume_cm3 / (rate * 0.25), 6)

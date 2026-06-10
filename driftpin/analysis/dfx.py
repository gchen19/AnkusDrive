"""Design-for-X heuristics — grade a design against a downstream process.

Pure-Python, FreeCAD-free. Simulation family 9 (``docs/SIMULATION_TOOLS.md``):
the closed-form, geometry-free counterpart to the face-tagging DfX tools. Each
check takes **explicit** inputs (face descriptors, part/fastener counts, a
bounding box) — exactly like ``tolerance.py`` takes an explicit dimension chain —
and returns a small dict of rounded numbers with a `pass`/`score`, so it drops
straight into a merge gate. Where a property is wanted it is read from the
Materials DB (``driftpin.analysis.materials``) by name with explicit-override
params and documented fallbacks; an unknown material with no override raises
ValueError (no silent default).

Methods (handbook heuristics, documented per function):
    dfm_check   — manufacturability screen: draft / undercut / min-wall (DfM)
    dfa_check   — Boothroyd-Dewhurst-lite assembly efficiency / score (DfA)
    pack_check  — carton fit + dimensional (volumetric) weight (packaging)

Lengths are mm, masses grams, temperatures °C — matching the rest of
``analysis/``. See ``docs/SIMULATION_EXAMPLES.md`` §9 for the worked toys.
"""
from __future__ import annotations

from . import materials


# --- shared helper ------------------------------------------------------------

def _mat_value(material: str | None, accessor: str):
    """Pull a canonical numeric (e.g. 'density_g_cc') from the Materials DB, or
    None if the material/property is unknown. Never raises on a missing
    material — callers turn a missing override+lookup into a ValueError."""
    if not material:
        return None
    try:
        card = materials.get(material)
    except materials.MaterialNotFound:
        return None
    try:
        return materials.numeric(card, accessor)
    except KeyError:
        return None


# --- 1. DfM: manufacturability ------------------------------------------------

# Default minimum wall thickness (mm) by process — order-of-magnitude handbook
# values for a manufacturability *screen*, not a process spec. Injection moulding
# wants the thickest wall (flow/sink), CNC the thinnest (rigidity-limited).
_MIN_WALL_MM = {
    "injection": 1.0,
    "cnc": 0.5,
    "sheet": 0.8,
    "fdm": 0.8,
}


def dfm_check(
    faces: list,
    pull_axis: str = "+z",
    process: str = "injection",
    min_wall_mm: float | None = None,
    min_draft_deg: float = 1.0,
) -> dict:
    """Screen a part for manufacturability against a pull/tool axis.

    ``faces`` is a list of ``{name, draft_deg, wall_mm (optional)}``. ``draft_deg``
    is the face's draft relative to ``pull_axis``: 0 is a vertical wall that needs
    draft to release; a negative value is a re-entrant / undercut face (a side
    hole, a snap hook) that no straight pull can free.

    - ``draft_violations``  — faces with ``0 <= draft_deg < min_draft_deg``.
    - ``undercut_faces``    — faces with ``draft_deg < 0``.
    - ``min_wall_violations`` — faces carrying a ``wall_mm`` below ``min_wall_mm``.

    ``min_wall_mm`` defaults by ``process`` (injection 1.0, cnc 0.5, sheet 0.8,
    fdm 0.8); an unknown process with no override raises ValueError. ``score`` is
    ``1 - (#violations / max(#faces, 1))`` and ``pass`` is true only when no face
    is flagged.

    Returns {process, pull_axis, min_wall_mm, draft_violations, undercut_faces,
    min_wall_violations, score, pass}."""
    if min_wall_mm is None:
        if process not in _MIN_WALL_MM:
            raise ValueError(
                f"unknown process {process!r}; pass min_wall_mm "
                f"(known: {', '.join(sorted(_MIN_WALL_MM))})"
            )
        min_wall_mm = _MIN_WALL_MM[process]

    draft_violations: list = []
    undercut_faces: list = []
    min_wall_violations: list = []
    for f in faces:
        name = f.get("name")
        draft = f.get("draft_deg")
        wall = f.get("wall_mm")
        if draft is not None:
            if draft < 0:
                undercut_faces.append(name)
            elif draft < min_draft_deg:
                draft_violations.append(name)
        if wall is not None and wall < min_wall_mm:
            min_wall_violations.append(name)

    n_viol = len(draft_violations) + len(undercut_faces) + len(min_wall_violations)
    n_faces = max(len(faces), 1)
    score = 1.0 - n_viol / n_faces
    return {
        "process": process,
        "pull_axis": pull_axis,
        "min_wall_mm": round(min_wall_mm, 3),
        "draft_violations": draft_violations,
        "undercut_faces": undercut_faces,
        "min_wall_violations": min_wall_violations,
        "score": round(score, 3),
        "pass": n_viol == 0,
    }


# --- 2. DfA: assembly efficiency ----------------------------------------------

def dfa_check(
    part_count: int,
    fastener_count: int = 0,
    unique_part_count: int | None = None,
    insertion_axes: int = 1,
    symmetric_fraction: float = 0.0,
) -> dict:
    """Grade an assembly's design-for-assembly (Boothroyd-Dewhurst-lite).

    ``theoretical_min_parts`` is ``unique_part_count`` if given, else 1 (the
    Boothroyd ideal — one monolithic part). ``assembly_efficiency`` is the classic
    ratio ``theoretical_min / (part_count + fastener_count)``: fewer parts and
    fasteners for the same function score higher. ``handling_difficulty`` follows
    ``insertion_axes`` (1 -> 'low', 2-3 -> 'medium', >=4 -> 'high'), nudged up one
    band when fewer than half the parts are symmetric. ``symmetry_score`` is just
    the symmetric fraction clamped to [0, 1].

    ``assembly_score`` is in [0, 1] and **decreases monotonically** as part_count
    or fastener_count rises: it is the efficiency ratio scaled by a handling
    penalty, so adding any part or fastener can only lower it.

    Fidelity (``SIMULATION_NEXT.md`` contract): the grade is a Boothroyd-style
    ordinal index for *comparing variants*, not a measured quantity, so it carries
    ``fidelity = "correlation"`` with ``band_pct = None`` (no physical scatter band
    exists — rank with it, don't gate on the absolute value).

    Returns {part_count, fastener_count, insertion_axes, handling_difficulty,
    assembly_efficiency, assembly_score, symmetry_score, fidelity, band_pct}.
    Raises ValueError on a non-positive part_count or negative counts."""
    if part_count <= 0:
        raise ValueError("part_count must be > 0")
    if fastener_count < 0:
        raise ValueError("fastener_count must be >= 0")
    if insertion_axes < 1:
        raise ValueError("insertion_axes must be >= 1")

    theoretical_min = unique_part_count if unique_part_count is not None else 1
    total_ops = part_count + fastener_count
    assembly_efficiency = theoretical_min / total_ops

    sym = max(0.0, min(symmetric_fraction, 1.0))

    if insertion_axes >= 4:
        band = "high"
    elif insertion_axes >= 2:
        band = "medium"
    else:
        band = "low"
    # asymmetric parts (less than half symmetric) make handling harder: bump up
    # one band.
    if sym < 0.5:
        band = {"low": "medium", "medium": "high", "high": "high"}[band]

    # handling penalty in (0, 1]: 'low' -> 1.0, 'medium' -> 0.8, 'high' -> 0.6,
    # eased back up by the symmetric fraction (symmetric parts feed/orient easily).
    penalty = {"low": 1.0, "medium": 0.8, "high": 0.6}[band]
    handling = penalty + (1.0 - penalty) * sym

    # assembly_score = efficiency * handling. efficiency strictly decreases as
    # total_ops (part_count + fastener_count) rises, and handling is independent
    # of the counts, so the product is monotone non-increasing in each count.
    assembly_score = assembly_efficiency * handling
    return {
        "part_count": part_count,
        "fastener_count": fastener_count,
        "insertion_axes": insertion_axes,
        "handling_difficulty": band,
        "assembly_efficiency": round(assembly_efficiency, 4),
        "assembly_score": round(assembly_score, 4),
        "symmetry_score": round(sym, 4),
        # SIMULATION_NEXT.md fidelity contract: an ordinal ranking index — no
        # literature scatter band applies, hence band_pct None.
        "fidelity": "correlation",
        "band_pct": None,
    }


# --- 3. packaging: carton fit + dimensional weight ----------------------------

def pack_check(
    part_bbox_mm: list,
    carton_mm: list,
    mass_g: float,
    dim_factor: float = 5000.0,
) -> dict:
    """Check a part against a shipping carton and compute billable weight.

    ``part_bbox_mm`` and ``carton_mm`` are ``[l, w, h]`` in mm. ``fits`` is true
    when each sorted part dimension is <= the matching sorted carton dimension
    (so the part may be re-oriented to fit). ``void_fraction`` is
    ``1 - vol(part)/vol(carton)`` when it fits, else None.

    ``dim_weight_kg`` is the carrier volumetric weight: ``vol(carton in cm) /
    dim_factor`` (default divisor 5000, the common metric DIM factor).
    ``actual_mass_kg`` is ``mass_g / 1000``. ``billable_weight_kg`` is the larger
    of the two — what a carrier actually charges.

    Returns {fits, void_fraction, dim_weight_kg, actual_mass_kg,
    billable_weight_kg, pass}. Raises ValueError on a non-positive carton
    dimension or negative mass."""
    if len(part_bbox_mm) != 3 or len(carton_mm) != 3:
        raise ValueError("part_bbox_mm and carton_mm must each be [l, w, h]")
    if any(c <= 0 for c in carton_mm):
        raise ValueError("carton_mm dimensions must be > 0")
    if mass_g < 0:
        raise ValueError("mass_g must be >= 0")

    part_sorted = sorted(part_bbox_mm)
    carton_sorted = sorted(carton_mm)
    fits = all(p <= c for p, c in zip(part_sorted, carton_sorted))

    part_vol = part_bbox_mm[0] * part_bbox_mm[1] * part_bbox_mm[2]
    carton_vol = carton_mm[0] * carton_mm[1] * carton_mm[2]
    void_fraction = (1.0 - part_vol / carton_vol) if fits else None

    # dimensional (volumetric) weight: carrier carton volume in cm^3 / DIM factor.
    carton_vol_cm3 = carton_vol / 1000.0  # mm^3 -> cm^3
    dim_weight_kg = carton_vol_cm3 / dim_factor
    actual_mass_kg = mass_g / 1000.0
    billable_weight_kg = max(actual_mass_kg, dim_weight_kg)
    return {
        "fits": fits,
        "void_fraction": (round(void_fraction, 4) if void_fraction is not None else None),
        "dim_weight_kg": round(dim_weight_kg, 4),
        "actual_mass_kg": round(actual_mass_kg, 4),
        "billable_weight_kg": round(billable_weight_kg, 4),
        "pass": fits,
    }

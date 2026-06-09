"""FDM slice estimate — first-order print time / filament / mass from geometry.

Pure-Python, FreeCAD-free. Simulation family 9 (Design for X / slicing,
``docs/SIMULATION_TOOLS.md``): a standalone *analytic* estimator for an FDM
print. Feed it an explicit volume + bounding box (from ``mass_properties`` or a
hand number) plus a filament material and it returns deposited mass, filament
weight, layer count and a print-time estimate that drops into a DfM/cost gate.

This is the closed-form first-order estimate; shelling out to a PrusaSlicer /
OrcaSlicer / CuraEngine CLI on an exported STL — which adds supports, real
travel/acceleration and per-feature speeds — is the P1 upgrade. This module is
the FreeCAD-free standalone that needs no slicer installed.

Filament density is read from the Materials DB
(``driftpin.analysis.materials``) by name, with an explicit ``density_g_cc``
override and a documented fallback; an unknown material with no override raises
ValueError (no silent default).

Volumes are mm³, lengths mm, masses grams, speeds mm/s — matching the rest of
``analysis/``. See ``docs/SIMULATION_EXAMPLES.md`` §9 for the worked toy.
"""
from __future__ import annotations

import math

from . import materials


# --- shared helper ------------------------------------------------------------

def _mat_value(material: str | None, accessor: str):
    """Pull a canonical numeric (e.g. 'density_g_cc') from the Materials DB, or
    None if the material/property is unknown. Never raises on a missing material."""
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


# --- FDM slice estimate -------------------------------------------------------

def slice_estimate(
    volume_mm3: float,
    bbox_mm: list,
    material: str = "PLA",
    infill_fraction: float = 1.0,
    layer_height_mm: float = 0.2,
    wall_fraction: float = 0.35,
    print_speed_mm_s: float = 50.0,
    nozzle_mm: float = 0.4,
    filament_dia_mm: float = 1.75,
    density_g_cc: float | None = None,
) -> dict:
    """First-order FDM slice estimate: deposited mass, filament, layers, time.

    `bbox_mm` is the part's axis-aligned bounding box [x, y, z] in mm; build
    height is bbox_mm[2]. Filament density is read from the Materials DB
    (`density_g_cc` canonical) by name, or taken from the explicit `density_g_cc`
    override; an unknown material with no override raises ValueError.

    Solid mass is the part filled 100%::

        mass_g = volume_mm3 * 1e-3 * density_g_cc          (mm^3 -> cm^3 is *1e-3)

    The deposited volume scales the solid volume by the perimeter walls (always
    solid) plus the infilled interior::

        deposited_volume_mm3 = volume_mm3 * (wall_fraction
                                             + infill_fraction*(1 - wall_fraction))
        filament_g           = deposited_volume_mm3 * 1e-3 * density_g_cc

    At infill_fraction=1.0 the bracket is 1.0, so deposited == solid and
    filament_g == mass_g (a solid print weighs its density·volume). Lower infill
    deposits strictly less filament.

    Layers and time::

        layer_count      = ceil(bbox_mm[2] / layer_height_mm)
        volumetric_flow  = nozzle_mm * layer_height_mm * print_speed_mm_s  (mm^3/s)
        print_time_min   = deposited_volume_mm3 / volumetric_flow / 60

    A finer layer_height raises layer_count and (with flow ∝ layer_height) the
    print time, monotonically.

    Returns {mass_g, filament_g, deposited_volume_mm3, layer_count,
    print_time_min, infill_fraction}."""
    if volume_mm3 < 0:
        raise ValueError("volume_mm3 must be >= 0")
    if not bbox_mm or len(bbox_mm) < 3:
        raise ValueError("bbox_mm must be [x, y, z] in mm")
    if layer_height_mm <= 0:
        raise ValueError("layer_height_mm must be > 0")
    if not (0.0 <= wall_fraction <= 1.0):
        raise ValueError("wall_fraction must be in [0, 1]")
    if not (0.0 <= infill_fraction <= 1.0):
        raise ValueError("infill_fraction must be in [0, 1]")

    rho = density_g_cc if density_g_cc is not None else _mat_value(material, "density_g_cc")
    if not rho:
        raise ValueError(
            f"no density for {material!r}; pass density_g_cc"
        )

    # solid (100%-fill) mass: density * volume.
    mass_g = volume_mm3 * 1e-3 * rho

    # deposited material: solid perimeter walls + infilled interior.
    fill_factor = wall_fraction + infill_fraction * (1.0 - wall_fraction)
    deposited_volume_mm3 = volume_mm3 * fill_factor
    filament_g = deposited_volume_mm3 * 1e-3 * rho

    layer_count = math.ceil(bbox_mm[2] / layer_height_mm)

    # volumetric throughput at the nozzle -> deposition time.
    volumetric_flow = nozzle_mm * layer_height_mm * print_speed_mm_s
    print_time_min = (deposited_volume_mm3 / volumetric_flow / 60.0
                      if volumetric_flow > 0 else float("inf"))

    return {
        "mass_g": round(mass_g, 3),
        "filament_g": round(filament_g, 3),
        "deposited_volume_mm3": round(deposited_volume_mm3, 2),
        "layer_count": layer_count,
        "print_time_min": (round(print_time_min, 2)
                           if math.isfinite(print_time_min) else None),
        "infill_fraction": infill_fraction,
    }

"""FDM slice estimate — first-order print time / filament / mass from geometry.

Pure-Python, FreeCAD-free. Simulation family 9 (Design for X / slicing,
``docs/SIMULATION_TOOLS.md``): a standalone *analytic* estimator for an FDM
print. Feed it an explicit volume + bounding box (from ``mass_properties`` or a
hand number) plus a filament material and it returns deposited mass, filament
weight, layer count and a print-time estimate that drops into a DfM/cost gate.

Two tiers live here:

* the closed-form first-order estimate (:func:`slice_estimate`) — the FreeCAD-free
  standalone that needs no slicer installed; and
* the **external-CLI upgrade** (the Sprint 4 follow-on): :func:`slicer_cmd` builds
  a headless PrusaSlicer invocation on an exported STL — real perimeters, infill
  patterns, supports, travel/acceleration — and :func:`parse_gcode_stats` reads the
  sliced G-code's footer (filament used, estimated print time, the echoed config)
  plus the layer-change markers. The CLI runs behind the ``prusaslicer`` solver
  registration with the standard graceful degradation; the parser is pure-Python
  and testable on synthetic G-code text. CLI quirk handled here: PrusaSlicer's
  default fill pattern REJECTS 100 % density ("not supposed to work at 100%"), so
  full infill switches to ``rectilinear``. Validated live: a 20 mm cube at 100 %
  infill slices to 8.06 cm³ vs the exact 8.00 cm³ (the excess is the skirt).

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
        # Both exits, or the instruction is unfollowable (#264, the sibling of #238):
        # the override, AND a real card — 'polymer' is a CATEGORY and 'nylon' is one
        # hop from a card, and neither is something the caller can guess from
        # "pass density_g_cc".
        raise ValueError(
            f"no density for {material!r} — pass density_g_cc, "
            + materials.exit_hint(material, "density_g_cc")
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


# --- external-CLI upgrade (Sprint 4 follow-on): PrusaSlicer on a real STL ------

def slicer_cmd(
    stl_path: str,
    gcode_path: str,
    *,
    layer_height_mm: float = 0.2,
    infill_fraction: float = 0.2,
    supports: bool = False,
    extra_args: list | None = None,
) -> list:
    """The headless PrusaSlicer argv slicing ``stl_path`` into ``gcode_path``.

    ``--export-gcode`` with ``--layer-height`` and ``--fill-density`` (percent);
    ``supports`` adds ``--support-material``. PrusaSlicer's default fill pattern
    refuses 100 % density, so ``infill_fraction >= 0.99`` switches the pattern to
    ``rectilinear`` (which supports it). ``extra_args`` append verbatim (e.g.
    ``--filament-diameter``). The binary name is NOT included — the worker prepends
    the resolved ``prusaslicer`` solver path. Raises ValueError on bad inputs."""
    if not stl_path or not gcode_path:
        raise ValueError("stl_path and gcode_path are required")
    if layer_height_mm <= 0:
        raise ValueError("layer_height_mm must be > 0")
    if not 0.0 < infill_fraction <= 1.0:
        raise ValueError("infill_fraction must be in (0, 1]")
    argv = ["--export-gcode", "--output", gcode_path,
            "--layer-height", f"{layer_height_mm:g}",
            "--fill-density", f"{round(infill_fraction * 100)}%"]
    if infill_fraction >= 0.99:
        argv += ["--fill-pattern", "rectilinear"]
    if supports:
        argv.append("--support-material")
    argv += [str(a) for a in (extra_args or [])]
    argv.append(stl_path)
    return argv


def parse_gcode_time(text: str) -> int:
    """PrusaSlicer's footer duration ('19m 21s', '1h 2m 3s', '2d 1h …') in
    seconds. Raises ValueError when no d/h/m/s token parses."""
    import re
    total, found = 0, False
    for value, unit in re.findall(r"(\d+)\s*([dhms])", text):
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        found = True
    if not found:
        raise ValueError(f"unparseable duration {text!r}")
    return total


def parse_gcode_stats(gcode_text: str, density_g_cc: float | None = None) -> dict:
    """Statistics of a PrusaSlicer G-code file (pass the TEXT — read the file
    first): the footer's filament length/volume, the estimated print time, the
    layer count (``;LAYER_CHANGE``/``AFTER_LAYER_CHANGE`` markers), and the echoed
    slicing config. PrusaSlicer reports 0 g unless a filament density was
    configured, so ``filament_g`` is recomputed from the volume when
    ``density_g_cc`` is given.

    Returns {filament_mm, filament_cm3, filament_g, print_time_s, print_time_text,
    layer_count, config: {layer_height_mm, first_layer_height_mm,
    fill_density_pct, perimeters, nozzle_mm, filament_dia_mm}} (missing footer
    entries are None). Raises ValueError when the text has no filament footer at
    all (not a PrusaSlicer G-code)."""
    import re

    def footer(pattern, cast=float):
        m = re.search(pattern, gcode_text, re.M)
        return cast(m.group(1)) if m else None

    filament_mm = footer(r"^; filament used \[mm\]\s*=\s*([\d.]+)")
    filament_cm3 = footer(r"^; filament used \[cm3\]\s*=\s*([\d.]+)")
    if filament_mm is None and filament_cm3 is None:
        raise ValueError("no '; filament used' footer — not a PrusaSlicer G-code")

    time_m = re.search(r"^; estimated printing time \(normal mode\)\s*=\s*(.+)$",
                       gcode_text, re.M)
    print_time_text = time_m.group(1).strip() if time_m else None
    print_time_s = parse_gcode_time(print_time_text) if print_time_text else None

    layer_count = gcode_text.count(";LAYER_CHANGE")
    if not layer_count:
        layer_count = gcode_text.count("AFTER_LAYER_CHANGE")

    filament_g = footer(r"^; total filament used \[g\]\s*=\s*([\d.]+)")
    if density_g_cc is not None and filament_cm3 is not None:
        filament_g = round(filament_cm3 * density_g_cc, 3)

    config = {
        "layer_height_mm": footer(r"^; layer_height\s*=\s*([\d.]+)"),
        "first_layer_height_mm": footer(r"^; first_layer_height\s*=\s*([\d.]+)"),
        "fill_density_pct": footer(r"^; fill_density\s*=\s*([\d.]+)%"),
        "perimeters": footer(r"^; perimeters\s*=\s*(\d+)", int),
        "nozzle_mm": footer(r"^; nozzle_diameter\s*=\s*([\d.]+)"),
        "filament_dia_mm": footer(r"^; filament_diameter\s*=\s*([\d.]+)"),
    }
    return {
        "filament_mm": filament_mm,
        "filament_cm3": filament_cm3,
        "filament_g": filament_g,
        "print_time_s": print_time_s,
        "print_time_text": print_time_text,
        "layer_count": layer_count,
        "config": config,
    }

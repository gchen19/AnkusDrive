"""Design-for-Cost (DfC) — turn a part volume + material into a unit-cost rollup.

Pure-Python, FreeCAD-free. Simulation family 9 (``docs/SIMULATION_TOOLS.md``):
the cost counterpart to the DfX cluster. Feed it an explicit material volume (mm³,
e.g. from ``mass_properties``) plus a material name and a process, and it returns a
per-unit cost with a material / process / tooling breakdown that drops into a BOM
or a merge gate. Density and price are read from the Materials DB
(``driftpin.analysis.materials``) by name, with explicit overrides and documented
fallbacks so every call works standalone — an unknown material with no override
raises (no silent default).

Method (heuristic, documented in the function):
    cost_estimate — material cost (volume·density·price) + a per-process machine-
                    time model + amortized tooling/setup over the lot quantity

Units: volume mm³, density kg/m³, price USD/kg, machine rate USD/hr, time min/hr,
mass kg, cost USD — matching the rest of ``analysis/``. See
``docs/SIMULATION_EXAMPLES.md`` §9 for the worked toys.
"""
from __future__ import annotations

from . import materials


# --- shared helper ------------------------------------------------------------

def _mat_value(material: str | None, accessor: str):
    """Pull a canonical numeric (e.g. 'density_kg_m3') from the Materials DB, or
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


# --- per-process machine-time model -------------------------------------------

# Order-of-magnitude machine-time factors in hours of cutting/forming per cm³ of
# part volume (i.e. per 1000 mm³). Subtractive CNC removes stock slowly; net-shape
# injection/casting fills a cavity in seconds; FDM extrudes a moderate bead. These
# only seed a screen — calibrate to a real shop rate before trusting an absolute
# number. The *ratios* between processes are what the rollup leans on.
_PROCESS_HR_PER_CM3 = {
    "cnc":       3.0e-3,   # slow: chip-by-chip material removal
    "fdm":       8.0e-4,   # extrusion / 3d print
    "casting":   2.0e-4,   # near-net-shape pour
    "injection": 5.0e-5,   # fast: cavity fill + cycle time
}
_PROCESS_DEFAULT = "cnc"


# --- cost rollup --------------------------------------------------------------

def cost_estimate(
    volume_mm3: float,
    material: str,
    process: str = "cnc",
    quantity: int = 1,
    tooling_usd: float = 0.0,
    machine_rate_usd_hr: float = 60.0,
    setup_min: float = 10.0,
    scrap_fraction: float = 0.0,
    density_kg_m3: float | None = None,
    price_usd_kg: float | None = None,
) -> dict:
    """Roll up the per-unit cost of one machined/molded part (Design for Cost).

    Material cost is the exact anchor:
        mass_kg       = volume_mm3 · 1e-9 · density        (density kg/m³)
        material_cost = mass_kg · price · (1 + scrap_fraction)
    Density and price come from the Materials DB (``density_kg_m3`` / ``cost_usd_kg``
    accessors) unless overridden by the ``density_kg_m3`` / ``price_usd_kg`` params.
    A material that is unknown *or* lacks density/price, with no override, raises
    ValueError — there is no silent default.

    Process cost uses a per-process machine-time heuristic (``_PROCESS_HR_PER_CM3``,
    cnc slow → injection fast): machine_time_hr scales with part volume, and one-
    time setup is amortized over the lot:
        machine_time_hr = volume_cm3 · factor(process)
        process_cost    = (setup_min/60)/quantity · rate + machine_time_hr · rate
    Tooling is amortized the same way: tooling_amortized = tooling_usd / quantity.
    Hence ``unit_cost`` falls monotonically as quantity rises whenever there is any
    fixed cost (tooling or setup) to spread.

        unit_cost = material_cost + process_cost + tooling_amortized

    Returns {material_cost, process_cost, tooling_amortized, unit_cost, mass_kg,
    breakdown:{volume_mm3, mass_kg, density_kg_m3, price_usd_kg, scrap_fraction,
    process, machine_time_hr, machine_rate_usd_hr, setup_min, quantity,
    setup_amortized, machining_cost, tooling_usd, density_basis, price_basis}}.
    Raises ValueError on a non-positive volume/quantity, an unknown process, or a
    material with no usable density/price."""
    if volume_mm3 <= 0:
        raise ValueError("volume_mm3 must be > 0")
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    proc = process or _PROCESS_DEFAULT
    if proc not in _PROCESS_HR_PER_CM3:
        raise ValueError(
            f"unknown process {process!r}; choose from {sorted(_PROCESS_HR_PER_CM3)}"
        )

    if density_kg_m3 is not None:
        density, density_basis = density_kg_m3, "explicit"
    else:
        density = _mat_value(material, "density_kg_m3")
        density_basis = "material"
    if not density or density <= 0:
        raise ValueError(
            f"no density for {material!r}; pass density_kg_m3"
        )

    if price_usd_kg is not None:
        price, price_basis = price_usd_kg, "explicit"
    else:
        price = _mat_value(material, "cost_usd_kg")
        price_basis = "material"
    if price is None or price < 0:
        raise ValueError(
            f"no price for {material!r}; pass price_usd_kg"
        )

    # --- material cost (the exact anchor) ---
    mass_kg = volume_mm3 * 1e-9 * density
    material_cost = mass_kg * price * (1.0 + scrap_fraction)

    # --- process cost: machining time + amortized setup ---
    volume_cm3 = volume_mm3 / 1000.0
    machine_time_hr = volume_cm3 * _PROCESS_HR_PER_CM3[proc]
    machining_cost = machine_time_hr * machine_rate_usd_hr
    setup_amortized = (setup_min / 60.0) / quantity * machine_rate_usd_hr
    process_cost = setup_amortized + machining_cost

    # --- tooling amortized over the lot ---
    tooling_amortized = tooling_usd / quantity

    unit_cost = material_cost + process_cost + tooling_amortized

    return {
        "material_cost": round(material_cost, 4),
        "process_cost": round(process_cost, 4),
        "tooling_amortized": round(tooling_amortized, 4),
        "unit_cost": round(unit_cost, 4),
        "mass_kg": round(mass_kg, 6),
        "breakdown": {
            "volume_mm3": round(volume_mm3, 3),
            "mass_kg": round(mass_kg, 6),
            "density_kg_m3": round(density, 2),
            "price_usd_kg": round(price, 4),
            "scrap_fraction": scrap_fraction,
            "process": proc,
            "machine_time_hr": round(machine_time_hr, 6),
            "machine_rate_usd_hr": machine_rate_usd_hr,
            "setup_min": setup_min,
            "quantity": quantity,
            "setup_amortized": round(setup_amortized, 4),
            "machining_cost": round(machining_cost, 4),
            "tooling_usd": tooling_usd,
            "density_basis": density_basis,
            "price_basis": price_basis,
        },
    }

"""Design-for-Cost (DfC) — turn a part volume + material into a unit-cost rollup.

Pure-Python, FreeCAD-free. Simulation family 9 (``docs/SIMULATION_TOOLS.md``):
the cost counterpart to the DfX cluster. Feed it an explicit material volume (mm³,
e.g. from ``mass_properties``) plus a material name and a process, and it returns a
per-unit cost with a material / process / tooling breakdown that drops into a BOM
or a merge gate. Density and price are read from the Materials DB
(``ankusdrive.analysis.materials``) by name, with explicit overrides and documented
fallbacks so every call works standalone — an unknown material with no override
raises (no silent default).

Method (heuristic, documented in the function):
    cost_estimate — material cost (volume·density·price) + a per-process machine-
                    time model + amortized tooling/setup over the lot quantity

Two optional inputs added by the production-readiness epic (#229), both defaulting
to today's behaviour byte-for-byte:

* ``tolerance_class`` (#235) scales the TABLE machine time by the shared
  tolerance–cost curve — the gradient that makes a design pay for precision it
  didn't need.
* ``machine_time_hr`` (#231) replaces the order-of-magnitude table outright with a
  time a real model computed (``cnc_time_estimate``), and tightens the rollup's
  declared band to that model's.

Units: volume mm³, density kg/m³, price USD/kg, machine rate USD/hr, time min/hr,
mass kg, cost USD — matching the rest of ``analysis/``. See
``docs/SIMULATION_EXAMPLES.md`` §9 for the worked toys.
"""
from __future__ import annotations

from . import materials, tolerance_cost


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


# --- the missing-density/price error message (issue #238) ---------------------
#
# Both exits out of "I have no number for this material" must be named, because an
# agent can only take the exit the message tells it about: pass the override, or
# name a real card. The dead end #238 filed was a message that named one exit and
# a wrapper that didn't expose it.
#
# The corpus-derived half now lives in materials.exit_hint — #264 found
# slice_estimate raising the identical unfollowable error, so the hint belongs next
# to the corpus it reads rather than in whichever analysis module happened to need
# it first.

_material_exit_hint = materials.exit_hint


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

# The declared band on the rollup. The default table is order-of-magnitude; a
# machine time supplied by a real model (cnc_time_estimate) carries its own, much
# tighter one, and the rollup must not keep claiming ±100 % once the dominant
# unknown has been measured.
_TABLE_BAND_PCT = 100.0
_SUPPLIED_TIME_BAND_PCT = 50.0


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
    tolerance_class=None,
    machine_time_hr: float | None = None,
    machine_time_band_pct: float | None = None,
) -> dict:
    """Roll up the per-unit cost of one machined/molded part (Design for Cost).

    Material cost is the exact anchor:
        mass_kg       = volume_mm3 · 1e-9 · density        (density kg/m³)
        material_cost = mass_kg · price · (1 + scrap_fraction)
    Density and price come from the Materials DB (``density_kg_m3`` / ``cost_usd_kg``
    accessors) unless overridden by the ``density_kg_m3`` / ``price_usd_kg`` params.
    A material that is unknown *or* lacks density/price, with no override, raises
    ValueError — there is no silent default. That error names BOTH exits (#238):
    the override, and the real Materials-DB cards in the category the caller typed
    ('aluminum' is a category, not a card → AL6061-T6, AL7075-T6, …).

    Process cost uses a per-process machine-time heuristic (``_PROCESS_HR_PER_CM3``,
    cnc slow → injection fast): machine_time_hr scales with part volume, and one-
    time setup is amortized over the lot:
        machine_time_hr = volume_cm3 · factor(process)
        process_cost    = (setup_min/60)/quantity · rate + machine_time_hr · rate
    Tooling is amortized the same way: tooling_amortized = tooling_usd / quantity.
    Hence ``unit_cost`` falls monotonically as quantity rises whenever there is any
    fixed cost (tooling or setup) to spread.

        unit_cost = material_cost + process_cost + tooling_amortized

    **Tolerance (issue #235, optional).** ``tolerance_class`` — an IT grade, e.g.
    ``'IT7'`` or ``7`` — scales the table machine time by the shared tolerance–cost
    curve (:func:`ankusdrive.analysis.tolerance_cost.time_factor`): holding a grade
    tighter than the process's natural capability buys slower finish passes, spring
    passes, in-process gauging and sometimes a whole secondary operation, and
    roughly doubles cost every 1.5 grades. ``None`` (the default) leaves the factor
    at exactly 1.0, so every pre-#235 call returns exactly what it always did.

    **A real machine time (issue #231, optional).** ``machine_time_hr`` replaces
    the ``_PROCESS_HR_PER_CM3`` table with a time an actual model computed —
    ``cnc_time_estimate`` / :func:`ankusdrive.analysis.machining.machining_time` — and
    the rollup's ``band_pct`` drops from 100 to that model's band (``50`` by
    default, or ``machine_time_band_pct``). The tolerance factor is then NOT applied
    on top: ``cnc_time_estimate`` takes its own ``tolerance_class`` and applies the
    same curve, so scaling again here would charge for the same precision twice.
    ``breakdown.tolerance_applied`` records which of the two happened.

    Fidelity (``SIMULATION_NEXT.md`` contract): ``material_cost`` is exact given
    its density/price inputs. With the default table the machine time is
    order-of-magnitude, so the rollup carries ``fidelity = "correlation"`` with
    ``band_pct = 100`` — trust the *ratios* between processes and quantities, not
    the absolute dollars. Supply ``machine_time_hr`` and the band tightens as above.

    Returns {material_cost, process_cost, tooling_amortized, unit_cost, mass_kg,
    fidelity, band_pct, breakdown:{volume_mm3, mass_kg, density_kg_m3,
    price_usd_kg, scrap_fraction, process, machine_time_hr, machine_time_basis,
    base_machine_time_hr, tolerance_class, tolerance_factor, tolerance_applied,
    tolerance_basis, machine_rate_usd_hr, setup_min, quantity, setup_amortized,
    machining_cost, tooling_usd, density_basis, price_basis}}. Raises ValueError on
    a non-positive volume/quantity/machine time, an unknown process or tolerance
    class, or a material with no usable density/price."""
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
            f"no density for {material!r} — pass density_kg_m3 (with price_usd_kg "
            f"if the material is also unknown), "
            + _material_exit_hint(material, "density_kg_m3")
        )

    if price_usd_kg is not None:
        price, price_basis = price_usd_kg, "explicit"
    else:
        price = _mat_value(material, "cost_usd_kg")
        price_basis = "material"
    if price is None or price < 0:
        raise ValueError(
            f"no price for {material!r} — pass price_usd_kg (with density_kg_m3 "
            f"if the material is also unknown), "
            + _material_exit_hint(material, "cost_usd_kg")
        )

    # --- material cost (the exact anchor) ---
    mass_kg = volume_mm3 * 1e-9 * density
    material_cost = mass_kg * price * (1.0 + scrap_fraction)

    # --- process cost: machining time + amortized setup ---
    # The tolerance factor is computed either way so the breakdown always says what
    # the declared class would have cost, even when it is not the thing applied.
    tol = tolerance_cost.time_factor(tolerance_class, process=proc)
    volume_cm3 = volume_mm3 / 1000.0
    base_time_hr = volume_cm3 * _PROCESS_HR_PER_CM3[proc]
    if machine_time_hr is not None:
        if machine_time_hr <= 0:
            raise ValueError("machine_time_hr must be > 0 when supplied")
        # A supplied time already reflects its own tolerance class (see docstring):
        # applying the factor again would double-charge for the same precision.
        machine_time_hr = float(machine_time_hr)
        time_basis = "supplied"
        tolerance_applied = False
        band_pct = float(machine_time_band_pct
                         if machine_time_band_pct is not None
                         else _SUPPLIED_TIME_BAND_PCT)
    else:
        machine_time_hr = base_time_hr * tol["factor"]
        time_basis = "table"
        tolerance_applied = tolerance_class is not None
        band_pct = _TABLE_BAND_PCT
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
        # SIMULATION_NEXT.md fidelity contract: with the default table the
        # process-time model is an order-of-magnitude screen, so the rollup is a
        # focusing estimate, not a gate. material_cost alone is exact given its
        # inputs. A supplied machine_time_hr tightens the band (see docstring).
        "fidelity": "correlation",
        "band_pct": band_pct,
        "breakdown": {
            "volume_mm3": round(volume_mm3, 3),
            "mass_kg": round(mass_kg, 6),
            "density_kg_m3": round(density, 2),
            "price_usd_kg": round(price, 4),
            "scrap_fraction": scrap_fraction,
            "process": proc,
            "machine_time_hr": round(machine_time_hr, 6),
            "machine_time_basis": time_basis,
            "base_machine_time_hr": round(base_time_hr, 6),
            "tolerance_class": tolerance_class,
            "tolerance_factor": tol["factor"],
            "tolerance_applied": tolerance_applied,
            "tolerance_basis": tol["basis"],
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

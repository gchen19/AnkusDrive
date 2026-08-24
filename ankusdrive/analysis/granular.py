"""Granular / powder-mechanics closed-form oracles — the analytic anchors the
YADE discrete-element (DEM) path (``dem_pack_submit`` / ``dem_flow_submit``) is
gated against.

Pure-Python, FreeCAD-free (same property ``nonlinear.py`` and ``jobs.py`` have),
so the band contract is testable on the no-solver CI lane. Three handbook
results for poured / flowing granular media, each the correlation twin of a real
YADE solve. Unlike the structural oracles (elastica / Hertz are *exact* theory),
granular packing and discharge are empirical **correlations** — so every result
here carries ``fidelity="correlation"`` and an honest BAND, never a fake "exact".
The band IS the oracle: a DEM solve passes by landing inside it.

* **Random close packing** (``packing_fraction``) — monodisperse hard spheres
  poured and tapped settle near the *random-close-packing* (RCP) limit
  φ ≈ 0.64. The ordered crystalline limits FCC/HCP φ = π/√18 ≈ 0.7405 bracket it
  from above; *random-loose packing* (RLP, frictional, gently deposited)
  φ ≈ 0.55–0.60 brackets it from below. A poured DEM pile must land in the
  random band 0.60–0.66, well short of the crystalline 0.74.

* **Beverloo's law** (``beverloo_discharge``) — gravity discharge of a granular
  solid from a flat-bottomed hopper through an orifice of size D:
  W = C·ρ_bulk·√g·(D − k·d)^2.5 (mass flow per unit time). The flow scales as
  the (empty-annulus-corrected) outlet to the **2.5 power** and is essentially
  *independent of fill height* — the defining non-Torricelli signature of a
  granular column (a "Janssen" stress screen, not a fluid head). The exponent
  2.5 (3-D) is the gate.

* **Angle of repose** (``angle_of_repose``) — the free-surface slope of a poured
  pile rises monotonically with the inter-particle friction coefficient μ. A
  bounded correlation θ ≈ atan(c·μ) (saturating below the μ→∞ limit) gives a
  bracketed band; the monotone trend (more friction → steeper pile) is the gate.

SI units throughout: lengths m, density kg/m³, g m/s², mass-flow kg/s, angles
returned in degrees. ``fidelity="correlation"`` everywhere; ``band`` gives the
[low, high] the real solve must fall inside, and ``valid_range_ok`` flags when
the inputs leave the correlation's regime. Escalate to ``dem_pack_submit`` /
``dem_flow_submit`` (the real YADE solve) for polydisperse mixes, non-spherical
grains, cohesion, or any geometry this idealization can't see.
"""
from __future__ import annotations

import math

from . import materials

# Ordered (crystalline) and random packing limits for monodisperse hard spheres.
PHI_FCC_HCP = math.pi / math.sqrt(18.0)   # 0.74048… — densest lattice (Kepler)
PHI_RCP = 0.6366                           # random close packing (Scott & Kilgour)
PHI_RLP = 0.555                            # random loose packing (frictional)
# A poured monodisperse pile lands in the random band, never the crystal:
RANDOM_BAND = (0.60, 0.66)


def _resolve(explicit, material, accessor, what):
    """Take an explicit value, else pull ``accessor`` off a Materials-DB card."""
    if explicit is not None:
        if explicit <= 0:
            raise ValueError(f"{what} must be > 0")
        return float(explicit)
    if material:
        try:
            val = materials.numeric(materials.get(material), accessor)
        except (materials.MaterialNotFound, KeyError):
            val = None
        if val:
            return val
    raise ValueError(f"provide {what} or a material that carries it")


# --- random close packing -----------------------------------------------------

def packing_fraction(
    regime: str = "random_close",
    coordination: float | None = None,
) -> dict:
    """Reference solid-volume fraction φ for monodisperse hard spheres (no solver)
    — the correlation band a poured/tapped DEM pack is gated against.

    ``regime`` selects the anchor:
      * ``'random_close'`` (RCP, default) — poured-and-tapped, φ ≈ 0.637, the
        target band 0.60–0.66 a real settle must hit.
      * ``'random_loose'`` (RLP) — gently deposited frictional grains, φ ≈ 0.555.
      * ``'fcc'`` / ``'hcp'`` / ``'crystalline'`` — the ordered Kepler limit
        φ = π/√18 ≈ 0.7405 (the ceiling a *random* pile must stay below).

    The packing fraction is a structural correlation, not an exact theorem (RCP
    has no closed form), so ``fidelity='correlation'`` and ``band`` give the
    honest [low, high]. The void fraction is 1 − φ; the mean coordination number
    for RCP is ≈ 6 (isostatic for frictional spheres) and ≈ 12 for FCC/HCP — pass
    ``coordination`` to report its margin to the regime's expected contact count.

    Escalate to ``dem_pack_submit`` (a real YADE settle) for polydisperse size
    distributions, friction/cohesion effects on φ, non-spherical grains, or wall
    confinement this monodisperse idealization can't see. Returns {regime,
    packing_fraction, void_fraction, band, expected_coordination, coordination,
    coordination_ok, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    r = regime.lower()
    if r in ("random_close", "rcp", "close"):
        phi, band, z_exp, label = PHI_RCP, RANDOM_BAND, 6.0, "random_close"
    elif r in ("random_loose", "rlp", "loose"):
        phi, band, z_exp, label = PHI_RLP, (0.54, 0.60), 4.0, "random_loose"
    elif r in ("fcc", "hcp", "crystalline", "ordered"):
        phi, band, z_exp, label = PHI_FCC_HCP, (0.72, 0.7405), 12.0, "crystalline"
    else:
        raise ValueError(
            "regime must be 'random_close', 'random_loose', or 'fcc'/'hcp'")

    warnings: list[str] = []
    coord_ok = None
    if coordination is not None:
        if coordination < 0:
            raise ValueError("coordination must be >= 0")
        # within ±35% of the regime's expected mean contact count
        coord_ok = abs(coordination - z_exp) <= 0.35 * z_exp
        if not coord_ok:
            warnings.append(
                f"coordination {coordination:g} far from the {label} expectation "
                f"~{z_exp:g} — pack may not be in this regime")

    return {
        "regime": label,
        "packing_fraction": round(phi, 6),
        "void_fraction": round(1.0 - phi, 6),
        "band": [round(band[0], 4), round(band[1], 4)],
        "expected_coordination": z_exp,
        "coordination": coordination,
        "coordination_ok": coord_ok,
        "fidelity": "correlation",
        "band_pct": round(100.0 * (band[1] - band[0]) / phi, 2),
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "dem_pack_submit",
    }


# --- Beverloo hopper discharge ------------------------------------------------

def beverloo_discharge(
    outlet_m: float,
    particle_d_m: float,
    bulk_density_kg_m3: float | None = None,
    material: str | None = None,
    discharge_coeff: float = 0.58,
    shape_factor: float = 1.4,
    g_m_s2: float = 9.81,
) -> dict:
    """Beverloo gravity-discharge mass flow from a flat-bottomed hopper (no solver)
    — the correlation twin a real YADE silo solve is gated against.

    W = C·ρ_bulk·√g·(D − k·d)^2.5  [kg/s], with outlet D (``outlet_m``), grain
    diameter d (``particle_d_m``), bulk density ρ_bulk (``bulk_density_kg_m3`` or a
    Materials-DB ``material`` times an assumed φ≈0.64 if the card carries solid
    density), Beverloo coefficient C (``discharge_coeff`` ≈ 0.55–0.65) and the
    empty-annulus factor k (``shape_factor`` ≈ 1.4 for spheres). The flow goes as
    the corrected outlet to the **2.5 power** (3-D) and is independent of fill
    height — the granular (non-Torricelli) signature. ``mass_flow`` is that W;
    ``flow_exponent`` = 2.5 is the gate a two-outlet DEM sweep must reproduce
    (log-log slope of W vs D).

    Valid while the outlet clears several grains (D ≳ 6·d) and is well below the
    hopper width (no Janssen wall jamming); outside that the empty-annulus
    correction breaks and the flow can arch/jam — escalate to ``dem_flow_submit``.
    Returns {outlet_m, particle_d_m, effective_outlet_m, bulk_density_kg_m3,
    mass_flow_kg_s, flow_exponent, discharge_coeff, shape_factor, fidelity,
    band, band_pct, valid_range_ok, warnings, escalate_to}."""
    if outlet_m <= 0 or particle_d_m <= 0:
        raise ValueError("outlet_m and particle_d_m must be > 0")
    if discharge_coeff <= 0 or shape_factor < 0:
        raise ValueError("discharge_coeff must be > 0 and shape_factor >= 0")
    rho = _resolve(bulk_density_kg_m3, material, "density_kg_m3",
                   "bulk_density_kg_m3")
    # if pulled from a card it is the SOLID density; bulk ≈ φ·solid (RCP ~0.64)
    if bulk_density_kg_m3 is None and material:
        rho *= PHI_RCP

    eff = outlet_m - shape_factor * particle_d_m
    warnings: list[str] = []
    d_over_d = outlet_m / particle_d_m
    if d_over_d < 6.0:
        warnings.append(
            f"outlet/grain = {d_over_d:.1f} < 6 — orifice too narrow; flow can "
            "arch/jam and Beverloo over-predicts (use dem_flow_submit)")
    if eff <= 0:
        warnings.append(
            "effective outlet (D − k·d) <= 0 — the empty annulus closes the "
            "orifice; no steady flow")
        mass_flow = 0.0
    else:
        mass_flow = discharge_coeff * rho * math.sqrt(g_m_s2) * eff ** 2.5

    return {
        "outlet_m": round(float(outlet_m), 6),
        "particle_d_m": round(float(particle_d_m), 6),
        "effective_outlet_m": round(eff, 6),
        "bulk_density_kg_m3": round(rho, 4),
        "mass_flow_kg_s": round(mass_flow, 8),
        "flow_exponent": 2.5,
        "discharge_coeff": discharge_coeff,
        "shape_factor": shape_factor,
        "fidelity": "correlation",
        # the gate is the EXPONENT, not the absolute flow: 2.5 ± 0.3
        "band": [2.2, 2.8],
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "dem_flow_submit",
    }


def beverloo_exponent(
    outlet1_m: float,
    flow1_kg_s: float,
    outlet2_m: float,
    flow2_kg_s: float,
    particle_d_m: float = 0.0,
    shape_factor: float = 1.4,
) -> dict:
    """Recover the Beverloo flow exponent from two measured (outlet, mass-flow)
    points — the reduction a two-outlet DEM discharge sweep is gated against.

    The log-log slope n = ln(W₂/W₁)/ln(D'₂/D'₁) over the empty-annulus-corrected
    outlets D' = D − k·d should land at the granular 3-D value **2.5**, NOT the
    Torricelli/fluid 2.0 of a draining tank — that contrast (2.5 vs 2.0) is the
    physics gate. Set ``particle_d_m`` to apply the (D − k·d) correction; leave 0
    to use the raw outlets. Returns {exponent, band, in_band, torricelli_exponent,
    fidelity, valid_range_ok, warnings}."""
    if min(outlet1_m, outlet2_m, flow1_kg_s, flow2_kg_s) <= 0:
        raise ValueError("outlets and flows must be > 0")
    if outlet1_m == outlet2_m:
        raise ValueError("need two distinct outlet sizes")
    d1 = outlet1_m - shape_factor * particle_d_m
    d2 = outlet2_m - shape_factor * particle_d_m
    if d1 <= 0 or d2 <= 0:
        raise ValueError("effective outlet (D − k·d) <= 0 for one point")
    n = math.log(flow2_kg_s / flow1_kg_s) / math.log(d2 / d1)
    band = (2.2, 2.8)
    return {
        "exponent": round(n, 4),
        "band": list(band),
        "in_band": band[0] <= n <= band[1],
        "torricelli_exponent": 2.0,
        "fidelity": "correlation",
        "valid_range_ok": True,
        "warnings": [],
    }


# --- angle of repose ----------------------------------------------------------

def angle_of_repose(
    friction_coeff: float,
    saturation: float = 1.0,
) -> dict:
    """Free-surface repose angle of a poured monodisperse pile vs inter-particle
    friction μ (no solver) — the monotone correlation a DEM pile is gated against.

    θ_repose ≈ atan(``saturation``·μ): the pile steepens with friction and
    saturates toward a bulk limit (sliding never fully reaches the μ→∞ vertical
    because rolling/rearrangement relaxes the slope). ``saturation`` (≈0.8–1.0)
    rolls the micro-friction down to the bulk angle. The defining gate is the
    **trend**: a higher-μ DEM pile must repose at a STEEPER angle than a lower-μ
    one — so this returns a band around the correlation and a partner helper
    (``repose_increases_with_friction``) for the two-μ monotonicity check.

    Frictionless spheres (μ=0) give a flat-ish pile (θ→0, they avalanche to the
    container slope); real powders sit at 25–40°. Beyond μ≈1 the angle plateaus
    near 40–45°. Cohesion (wet/fine powders) can exceed this — escalate to
    ``dem_pack_submit``. Returns {friction_coeff, repose_deg, band,
    fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    if friction_coeff < 0:
        raise ValueError("friction_coeff must be >= 0")
    if not (0.0 < saturation <= 1.5):
        raise ValueError("saturation must be in (0, 1.5]")
    theta = math.degrees(math.atan(saturation * friction_coeff))
    # ±25% band (correlation, scatter is large in real piles)
    lo, hi = 0.75 * theta, min(1.25 * theta + 3.0, 60.0)
    warnings: list[str] = []
    if friction_coeff > 1.2:
        warnings.append(
            f"μ = {friction_coeff:g} > 1.2 — repose plateaus (~40–45°); the "
            "atan correlation over-steepens (use dem_pack_submit)")
    return {
        "friction_coeff": round(float(friction_coeff), 6),
        "repose_deg": round(theta, 4),
        "band": [round(lo, 4), round(hi, 4)],
        "fidelity": "correlation",
        "band_pct": 25.0,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "dem_pack_submit",
    }


def repose_increases_with_friction(
    mu_low: float,
    repose_low_deg: float,
    mu_high: float,
    repose_high_deg: float,
) -> dict:
    """Monotonicity gate for a two-μ repose sweep — the higher-friction pile must
    repose STEEPER. Returns {monotone, delta_deg, mu_low, mu_high, fidelity}.

    This is the strongest, assumption-free granular gate: regardless of the exact
    correlation, raising inter-particle friction can only steepen (never flatten)
    a poured pile. A DEM run that violates it is wrong."""
    if mu_high <= mu_low:
        raise ValueError("need mu_high > mu_low")
    delta = repose_high_deg - repose_low_deg
    return {
        "monotone": delta > 0,
        "delta_deg": round(delta, 4),
        "mu_low": round(float(mu_low), 6),
        "mu_high": round(float(mu_high), 6),
        "fidelity": "correlation",
        "valid_range_ok": True,
        "warnings": [],
    }

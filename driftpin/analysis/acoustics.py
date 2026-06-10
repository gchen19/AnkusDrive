"""Acoustics screening — cavity modes, resonator tuning, wall attenuation.

Pure-Python, FreeCAD-free. A Tier-A screening estimator from
``docs/SIMULATION_NEXT.md``: the closed-form acoustics an enclosure / duct /
resonator design needs *before* any FEM. Four kinds under one tool:

- ``cavity_modes`` — rigid rectangular cavity eigenfrequencies
  f = (c/2)·√((n_x/L_x)² + (n_y/L_y)² + (n_z/L_z)²)            (exact)
- ``helmholtz`` — neck-on-volume resonator f = (c/2π)·√(A/(V·L_eff)),
  L_eff = L + 1.7·r (two flanged-end corrections)              (correlation ±10 %)
- ``mass_law`` — normal-incidence transmission loss of a limp wall
  TL = 20·log₁₀(f·m″) − 47 dB                                  (correlation ±3 dB)
- ``duct_cutoff`` — first cross-mode cutoff: rectangular f_c = c/(2·a),
  circular f_c = 1.8412·c/(π·d); below it only plane waves     (exact)

Fidelity is labeled per kind (``SIMULATION_NEXT.md`` contract): the exact rows
carry ``fidelity="exact"``; the correlations carry ``band_pct`` / ``band_db``.
The rigid-cavity modes double as the oracle the planned Elmer ``HelmholtzSolve``
acoustic FEM (Tier B1) will be gated against — until that ships there is no
higher-order twin to escalate to. Lengths mm, temperatures °C.
"""
from __future__ import annotations

import math

# speed of sound in dry air: c = sqrt(gamma*R*T)
_GAMMA_AIR = 1.400
_R_AIR = 287.05  # J/kg/K


def speed_of_sound(t_ambient_c: float = 20.0) -> float:
    """c = √(γ·R·T) for dry air — 343.2 m/s at 20 °C (exact ideal-gas form)."""
    return math.sqrt(_GAMMA_AIR * _R_AIR * (t_ambient_c + 273.15))


def _cavity_modes(lx_m, ly_m, lz_m, c, n_modes):
    """All (n_x,n_y,n_z) rigid-wall eigenfrequencies up to n_modes, ascending."""
    modes = []
    n_max = 8  # indices beyond this are far past any screening interest
    for nx in range(n_max + 1):
        for ny in range(n_max + 1):
            for nz in range(n_max + 1):
                if nx == ny == nz == 0:
                    continue
                f = (c / 2.0) * math.sqrt(
                    (nx / lx_m) ** 2 + (ny / ly_m) ** 2 + (nz / lz_m) ** 2)
                modes.append((f, [nx, ny, nz]))
    modes.sort(key=lambda m: m[0])
    return modes[:n_modes]


def acoustic_screen(
    kind: str,
    # cavity_modes
    lx_mm: float | None = None,
    ly_mm: float | None = None,
    lz_mm: float | None = None,
    n_modes: int = 10,
    # helmholtz
    neck_area_mm2: float | None = None,
    neck_length_mm: float | None = None,
    cavity_volume_mm3: float | None = None,
    # mass_law
    frequency_hz: float | None = None,
    surface_density_kg_m2: float | None = None,
    # duct_cutoff
    duct_width_mm: float | None = None,
    duct_diameter_mm: float | None = None,
    # shared
    t_ambient_c: float = 20.0,
    c_m_s: float | None = None,
) -> dict:
    """Closed-form acoustics screen (no solver) — see the module docstring for the
    four kinds and their formulas.

    ``kind``: 'cavity_modes' (needs lx/ly/lz_mm; returns the lowest ``n_modes``
    rigid-cavity eigenfrequencies with their [n_x,n_y,n_z] indices — exact, and
    the oracle for the planned Tier-B1 Helmholtz FEM) | 'helmholtz' (neck_area_mm2,
    neck_length_mm, cavity_volume_mm3; flanged end correction L_eff = L + 1.7·r —
    ±10 %) | 'mass_law' (frequency_hz, surface_density_kg_m2; limp-wall
    normal-incidence TL — ±3 dB, and +6 dB per doubling of f or m″) |
    'duct_cutoff' (duct_width_mm or duct_diameter_mm; below f_c only plane waves
    propagate — exact). Sound speed from dry air at ``t_ambient_c`` unless
    ``c_m_s`` is given.

    Returns {kind, c_m_s, fidelity, band_pct, band_db, valid_range_ok, warnings,
    escalate_to} plus per kind: cavity_modes → {modes:[{f_hz, n}], f_fundamental_hz};
    helmholtz → {f_resonance_hz, neck_radius_mm, l_eff_mm}; mass_law → {tl_db,
    fm_product}; duct_cutoff → {f_cutoff_hz, geometry}. Raises ValueError on an
    unknown kind, missing inputs for the kind, or non-positive dimensions."""
    c = float(c_m_s) if c_m_s is not None else speed_of_sound(t_ambient_c)
    if c <= 0:
        raise ValueError("c_m_s must be > 0")
    warnings: list[str] = []
    out = {
        "kind": kind,
        "c_m_s": round(c, 2),
        "band_pct": None,
        "band_db": None,
        # no higher-order acoustic solve is shipped yet — the Elmer HelmholtzSolve
        # FEM is Tier B1 in SIMULATION_NEXT.md; these screens are its future oracle.
        "escalate_to": None,
    }

    if kind == "cavity_modes":
        if not (lx_mm and ly_mm and lz_mm) or min(lx_mm, ly_mm, lz_mm) <= 0:
            raise ValueError("cavity_modes needs positive lx_mm, ly_mm, lz_mm")
        if n_modes < 1:
            raise ValueError("n_modes must be >= 1")
        modes = _cavity_modes(lx_mm / 1e3, ly_mm / 1e3, lz_mm / 1e3, c, n_modes)
        out["fidelity"] = "exact"
        out["modes"] = [{"f_hz": round(f, 3), "n": n} for f, n in modes]
        out["f_fundamental_hz"] = round(modes[0][0], 3)

    elif kind == "helmholtz":
        if not (neck_area_mm2 and neck_length_mm and cavity_volume_mm3) or \
                min(neck_area_mm2, neck_length_mm, cavity_volume_mm3) <= 0:
            raise ValueError(
                "helmholtz needs positive neck_area_mm2, neck_length_mm, "
                "cavity_volume_mm3")
        a_m2 = neck_area_mm2 * 1e-6
        v_m3 = cavity_volume_mm3 * 1e-9
        r_m = math.sqrt(a_m2 / math.pi)          # equivalent circular neck
        l_eff = neck_length_mm / 1e3 + 1.7 * r_m  # two flanged ends (0.85·r each)
        f = (c / (2.0 * math.pi)) * math.sqrt(a_m2 / (v_m3 * l_eff))
        out["fidelity"] = "correlation"
        out["band_pct"] = 10.0
        out["f_resonance_hz"] = round(f, 3)
        out["neck_radius_mm"] = round(r_m * 1e3, 4)
        out["l_eff_mm"] = round(l_eff * 1e3, 4)
        # lumped model needs the resonator small vs wavelength
        wavelength_m = c / f
        if v_m3 ** (1.0 / 3.0) > wavelength_m / 4.0:
            warnings.append(
                "cavity dimension is not small vs wavelength/4 — the lumped "
                "Helmholtz model degrades toward a cavity-mode problem")

    elif kind == "mass_law":
        if not (frequency_hz and surface_density_kg_m2) or \
                min(frequency_hz, surface_density_kg_m2) <= 0:
            raise ValueError(
                "mass_law needs positive frequency_hz, surface_density_kg_m2")
        fm = frequency_hz * surface_density_kg_m2
        tl = 20.0 * math.log10(fm) - 47.0
        out["fidelity"] = "correlation"
        out["band_db"] = 3.0
        out["tl_db"] = round(tl, 2)
        out["fm_product"] = round(fm, 3)
        if tl < 0:
            warnings.append(
                "f·m″ below the mass-law floor (TL < 0 dB) — the wall is "
                "acoustically transparent at this frequency; the law does not apply")
        else:
            warnings.extend(
                [] if tl < 60 else
                ["TL > 60 dB — flanking/coincidence limits real walls below "
                 "the mass-law line"])

    elif kind == "duct_cutoff":
        if duct_width_mm is not None and duct_diameter_mm is not None:
            raise ValueError("give duct_width_mm OR duct_diameter_mm, not both")
        if duct_width_mm is not None:
            if duct_width_mm <= 0:
                raise ValueError("duct_width_mm must be > 0")
            f_c = c / (2.0 * duct_width_mm / 1e3)
            out["geometry"] = "rectangular"
        elif duct_diameter_mm is not None:
            if duct_diameter_mm <= 0:
                raise ValueError("duct_diameter_mm must be > 0")
            # first asymmetric mode of a rigid circular duct: ka = 1.8412
            f_c = 1.8412 * c / (math.pi * duct_diameter_mm / 1e3)
            out["geometry"] = "circular"
        else:
            raise ValueError("duct_cutoff needs duct_width_mm or duct_diameter_mm")
        out["fidelity"] = "exact"
        out["f_cutoff_hz"] = round(f_c, 3)

    else:
        raise ValueError(
            f"unknown kind {kind!r}; choose from ['cavity_modes', 'duct_cutoff', "
            "'helmholtz', 'mass_law']")

    out["warnings"] = warnings
    out["valid_range_ok"] = not warnings
    return out

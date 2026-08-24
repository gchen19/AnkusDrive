"""Fluid–structure-interaction closed-form oracles — the analytic anchors the
partitioned preCICE solve (OpenFOAM ↔ CalculiX, coupled through the
``fsi_pressure_plate_submit`` worker path) is gated against.

Pure-Python, FreeCAD-free, the same degradation/units contract as
``ankusdrive/analysis/nonlinear.py``. The coupled CFD↔FEM solve is the unsteady
Turek–Hron limit cycle in the general case, but that is an expensive, finicky
gate. The *tractable, exact* anchor used here is the **steady pressure-loaded
flexible plate**: a thin elastic plate/beam clamped at one edge and loaded by a
known, uniform fluid pressure ``q`` deflects with a tip deflection that small-
deflection beam theory pins exactly,

    δ_tip = q · b · L⁴ / (8 · E · I)         (uniformly-loaded cantilever)

with the second moment of a solid rectangular section I = b·t³/12. Written in
terms of the *traction* q (Pa) on a plate of wetted width ``b`` (the strip the
flow presses on) and length ``L``, this is the partitioned-FSI steady state: the
fluid hands the solid a uniform pressure, the solid returns a deflection, and at
the fixed (small-deflection) operating point the two close on the handbook value.
A converged OpenFOAM(pressure)→CalculiX(displacement) solve must land on it.

Two gates, mirroring the optics energy-balance pattern:

* **plate_deflection** — the exact small-deflection cantilever-plate tip (and
  the centre deflection of a both-ends-clamped strip, δ = q·b·L⁴/(384·E·I)).
  This is the *primary* gate: the coupled tip displacement vs δ_tip.

* **interface_balance** — the partitioned force/energy balance at the wet
  interface closes: Σ(fluid traction · area) ≈ Σ(solid reaction). For the
  uniform-pressure plate the total wet load is F = q·b·L and the clamped-edge
  reaction must carry exactly that (a free body), with the bending moment at the
  root M = F·L/2 = q·b·L²/2. The relative residual |F_fluid − R_solid|/F_fluid
  is the partitioned analogue of the energy-balance gate — a real coupled solve
  conserves the interface load to mapping/quadrature tolerance.

A helper ``channel_pressure_load`` supplies the *fluid* side of the anchor: the
fully-developed laminar pressure drop a flow of mean velocity U over a channel
of gap ``h`` feeds, Δp = 12·μ·U·L/h² (plane-Poiseuille), so the plate can be
driven by a physically-sourced traction rather than an invented number.

Lengths mm, pressures/stresses MPa unless named ``_pa`` (then Pa), forces N,
moments N·mm. fidelity="exact" for the small-deflection plate (Euler–Bernoulli
thin-beam theory; the gate is the linearised operating point of the partitioned
fixed point). Theory limits (thin plate, small deflection δ≲t, fully-developed
laminar flow) are stated in each return's ``warnings`` / ``escalate_to``.
"""
from __future__ import annotations

from . import materials


def _resolve(explicit, material, accessor, what):
    """Take an explicit value, else pull `accessor` off a Materials-DB card
    (mirrors nonlinear._resolve)."""
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


# --- the fluid side: a physically-sourced uniform traction --------------------

def channel_pressure_load(
    velocity_m_s: float,
    length_mm: float,
    gap_mm: float,
    mu_pa_s: float = 1.0e-3,
    rho_kg_m3: float = 1000.0,
) -> dict:
    """Fully-developed plane-channel pressure drop — the *fluid* load that drives
    the plate. For laminar flow between parallel plates a gap ``h`` apart with
    mean velocity ``U`` over a length ``L``, the wall pressure drop is the exact
    plane-Poiseuille result Δp = 12·μ·U·L / h². The Reynolds number Re = ρ·U·h/μ
    flags when the laminar (and hence exact) assumption holds (Re ≲ 1400 for
    plane channels). Default fluid is water at 20 °C (μ=1e-3 Pa·s, ρ=1000).

    Returns {pressure_pa, pressure_drop_pa, reynolds, regime, velocity_m_s,
    wall_shear_pa, fidelity, valid_range_ok, warnings, escalate_to}. The
    ``pressure_pa`` is the uniform traction to feed ``plate_deflection`` /
    ``interface_balance`` as ``pressure_pa``."""
    if velocity_m_s <= 0 or length_mm <= 0 or gap_mm <= 0:
        raise ValueError("velocity_m_s, length_mm, gap_mm must be > 0")
    if mu_pa_s <= 0 or rho_kg_m3 <= 0:
        raise ValueError("mu_pa_s and rho_kg_m3 must be > 0")
    L = length_mm * 1e-3
    h = gap_mm * 1e-3
    re = rho_kg_m3 * velocity_m_s * h / mu_pa_s
    dp = 12.0 * mu_pa_s * velocity_m_s * L / (h * h)
    # plane-Poiseuille wall shear τ = 6μU/h
    tau = 6.0 * mu_pa_s * velocity_m_s / h
    warnings: list[str] = []
    laminar = re < 1400.0
    if not laminar:
        warnings.append(
            f"Re={re:.0f} ≥ 1400 — plane channel likely transitional/turbulent; "
            "the laminar Δp=12μUL/h² is no longer exact, escalate to a RANS solve")
    return {
        "pressure_pa": round(dp, 6),
        "pressure_drop_pa": round(dp, 6),
        "reynolds": round(re, 3),
        "regime": "laminar" if laminar else "transitional/turbulent",
        "velocity_m_s": float(velocity_m_s),
        "wall_shear_pa": round(tau, 6),
        "fidelity": "exact" if laminar else "approximate",
        "valid_range_ok": laminar,
        "warnings": warnings,
        "escalate_to": "fsi_pressure_plate_submit",
    }


# --- the solid side: the exact small-deflection plate tip ----------------------

def plate_deflection(
    pressure_pa: float,
    length_mm: float,
    width_mm: float,
    thickness_mm: float,
    youngs_gpa: float | None = None,
    material: str | None = None,
    support: str = "cantilever",
    i_mm4: float | None = None,
) -> dict:
    """Exact small-deflection tip/centre deflection of a uniform-pressure-loaded
    thin plate strip — the closed-form twin the coupled OpenFOAM→CalculiX FSI
    solve is gated against. The wetted strip is ``width_mm`` × ``length_mm``, the
    pressure ``pressure_pa`` (Pa) acts normal to it, giving a line load
    q = pressure · width (N per mm of length). Section is the solid rectangle
    I = width·thickness³/12 (mm⁴) unless an explicit ``i_mm4`` is given. E from
    ``youngs_gpa`` or a Materials-DB ``material``.

    * ``support="cantilever"`` (clamped one edge, free tip): the uniformly-loaded
      cantilever tip δ = q·L⁴/(8·E·I), root moment M = q·L²/2, root reaction
      R = q·L.
    * ``support="clamped-clamped"`` (both edges clamped): the centre deflection
      δ = q·L⁴/(384·E·I), end reactions R = q·L/2 each.

    Valid while δ ≲ thickness (small-deflection / linear-bending regime); past
    that membrane stiffening sets in and an ``*NLGEOM`` follower-pressure solve is
    needed (``warnings``). Returns {support, pressure_pa, line_load_n_per_mm,
    total_load_n, I_mm4, tip_disp_mm, root_moment_nmm, reaction_n, max_stress_mpa,
    youngs_mpa, slenderness, fidelity, band_pct, valid_range_ok, warnings,
    escalate_to}."""
    if pressure_pa <= 0:
        raise ValueError("pressure_pa must be > 0")
    for nm, v in (("length_mm", length_mm), ("width_mm", width_mm),
                  ("thickness_mm", thickness_mm)):
        if v <= 0:
            raise ValueError(f"{nm} must be > 0")
    if support not in ("cantilever", "clamped-clamped"):
        raise ValueError("support must be 'cantilever' or 'clamped-clamped'")

    e_mpa = _resolve(youngs_gpa, material, "youngs_gpa", "youngs_gpa") * 1e3
    if i_mm4 is not None:
        if i_mm4 <= 0:
            raise ValueError("i_mm4 must be > 0")
        inertia = float(i_mm4)
    else:
        inertia = width_mm * thickness_mm ** 3 / 12.0

    L = float(length_mm)
    # pressure_pa (N/m²) -> N/mm² = MPa; q line load = pressure(MPa) * width(mm) = N/mm
    p_mpa = pressure_pa * 1e-6
    q = p_mpa * width_mm                         # N per mm of length
    total_load = q * L                           # total wet force, N

    warnings: list[str] = []
    if support == "cantilever":
        tip = q * L ** 4 / (8.0 * e_mpa * inertia)
        root_moment = q * L ** 2 / 2.0            # N·mm
        reaction = q * L                          # N (free-body: carries all of F)
    else:  # clamped-clamped strip
        tip = q * L ** 4 / (384.0 * e_mpa * inertia)
        root_moment = q * L ** 2 / 12.0           # fixed-end moment
        reaction = q * L / 2.0                    # each end

    # bending stress at the most-loaded section, σ = M·c/I, c = t/2
    c = thickness_mm / 2.0
    max_stress = root_moment * c / inertia        # MPa
    slenderness = L / thickness_mm
    small_defl = tip <= thickness_mm
    if not small_defl:
        warnings.append(
            f"tip δ={tip:.3g} mm > thickness {thickness_mm} mm — past the small-"
            "deflection regime; membrane stiffening matters, escalate to an "
            "NLGEOM follower-pressure ccx solve")
    if slenderness < 8.0:
        warnings.append(
            f"L/t={slenderness:.1f} < 8 — stocky strip, shear deflection not "
            "captured by Euler–Bernoulli")

    return {
        "support": support,
        "pressure_pa": float(pressure_pa),
        "line_load_n_per_mm": round(q, 8),
        "total_load_n": round(total_load, 6),
        "I_mm4": round(inertia, 6),
        "tip_disp_mm": round(tip, 8),
        "root_moment_nmm": round(root_moment, 6),
        "reaction_n": round(reaction, 6),
        "max_stress_mpa": round(max_stress, 6),
        "youngs_mpa": round(e_mpa, 3),
        "slenderness": round(slenderness, 3),
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": small_defl and slenderness >= 8.0,
        "warnings": warnings,
        "escalate_to": "fsi_pressure_plate_submit",
    }


# --- the partitioned interface conservation gate -------------------------------

def interface_balance(
    pressure_pa: float,
    length_mm: float,
    width_mm: float,
    solid_reaction_n: float | None = None,
    fluid_force_n: float | None = None,
) -> dict:
    """Partitioned wet-interface force balance — the FSI analogue of the optics
    energy-balance gate. The fluid presses a uniform ``pressure_pa`` over the
    wetted ``length_mm`` × ``width_mm`` strip, so the total fluid traction is
    F_fluid = pressure · area. A converged partitioned solve must hand the solid
    exactly this load and the solid's support reactions must carry it (Newton's
    third law across the coupling surface). Pass the measured ``fluid_force_n``
    (∮ p·dA over the OpenFOAM wet patch) and/or ``solid_reaction_n`` (Σ CalculiX
    reaction forces at the clamp); the relative residual |F_fluid − R_solid| /
    F_fluid is the conservation error.

    With neither measured value supplied this returns the *reference* analytic
    load (both sides = F = p·A, residual 0) — the target a real solve closes on.
    Returns {area_mm2, reference_load_n, fluid_force_n, solid_reaction_n,
    residual_n, relative_residual, balanced (bool, < 1%), fidelity,
    escalate_to}."""
    if pressure_pa <= 0:
        raise ValueError("pressure_pa must be > 0")
    if length_mm <= 0 or width_mm <= 0:
        raise ValueError("length_mm and width_mm must be > 0")
    area_mm2 = length_mm * width_mm
    p_mpa = pressure_pa * 1e-6
    f_ref = p_mpa * area_mm2                       # N, the analytic wet load

    f_fluid = float(fluid_force_n) if fluid_force_n is not None else f_ref
    r_solid = float(solid_reaction_n) if solid_reaction_n is not None else f_ref

    residual = abs(f_fluid - r_solid)
    base = abs(f_fluid) if f_fluid else 1.0
    rel = residual / base
    return {
        "area_mm2": round(area_mm2, 6),
        "reference_load_n": round(f_ref, 6),
        "fluid_force_n": round(f_fluid, 6),
        "solid_reaction_n": round(r_solid, 6),
        "residual_n": round(residual, 8),
        "relative_residual": round(rel, 8),
        "balanced": rel < 0.01,
        "fidelity": "exact",
        "escalate_to": "fsi_pressure_plate_submit",
    }

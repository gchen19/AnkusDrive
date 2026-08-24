"""Machine-element rating — closed-form life / safety-factor for standard parts.

Pure-Python, FreeCAD-free. Each function is the durability counterpart to one of
AnkusDrive's component generators (add_fastener/add_thread, add_bearing, add_spring,
add_gear, ...): the generator encodes the geometry, this rates it. Strengths and
moduli are read from the Materials DB (ankusdrive.analysis.materials) by name, with
sensible defaults so every call works standalone.

Methods (textbook closed-form, documented per function):
    bolted_joint_check  — VDI 2230-lite preload / separation
    bearing_life        — ISO 281 basic rating life (L10)
    spring_check        — helical compression spring (Wahl)
    gear_rating         — spur-gear tooth bending (Lewis)
    chain_drive         — ANSI roller-chain power rating (ASME B29.1)
    weld_group          — fillet weld group, treat-weld-as-a-line (Blodgett)

Units are explicit engineering scalars (mm, N, N*mm or N*m where noted, rpm, MPa),
matching how the FEM tools take forces as plain floats. Results return a numeric
safety-factor / life plus a `pass` bool so they drop into multi-agent merge gates.

See docs/SIMULATION_TOOLS.md (family 10) and docs/SIMULATION_EXAMPLES.md.
"""
from __future__ import annotations

import math

from . import materials

# ISO metric coarse-thread pitch (mm) for common nominal diameters.
_COARSE_PITCH = {
    3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5,
    12: 1.75, 14: 2.0, 16: 2.0, 20: 2.5, 24: 3.0,
}


# --- shared helpers -----------------------------------------------------------

def _mat_value(material: str | None, accessor: str):
    """Pull a canonical numeric (e.g. 'yield_mpa') from the Materials DB, or None
    if the material/property is unknown. Never raises on a missing material."""
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


def tensile_stress_area_mm2(dia_mm: float, pitch_mm: float | None = None) -> float:
    """ISO metric tensile stress area At = (pi/4)(d - 0.9382 p)^2, mm^2.
    Falls back to coarse-thread pitch for the nominal diameter when pitch is None.
    Returns At (float)."""
    if pitch_mm is None:
        pitch_mm = _COARSE_PITCH.get(round(dia_mm), 0.15 * dia_mm)
    return (math.pi / 4.0) * (dia_mm - 0.9382 * pitch_mm) ** 2


# --- ratings ------------------------------------------------------------------

def bolted_joint_check(
    bolt_dia_mm: float | None = None,
    pitch_mm: float | None = None,
    torque_nm: float | None = None,
    preload_n: float | None = None,
    k_factor: float = 0.2,
    external_load_n: float = 0.0,
    joint_stiffness_ratio: float = 0.3,
    material: str = "Steel-4140-QT",
    proof_strength_mpa: float | None = None,
    preload_target_pct: float = 0.75,
    bolt_size: str | None = None,
    property_class: str | None = None,
) -> dict:
    """Rate a bolted joint (VDI 2230-lite).

    Geometry: give bolt_dia_mm (+ optional pitch_mm), OR name a standard thread
    via bolt_size (e.g. "M8"/"M8x1.0") to pull nominal diameter, pitch and the
    standards-table tensile stress area. preload from torque via T = K*F*d (give
    torque_nm OR preload_n directly). The external tensile load splits by
    joint_stiffness_ratio C: the bolt sees C*P, and the joint separates at
    P_sep = F_preload/(1-C). proof_strength_mpa is taken from the ISO 898-1
    property_class (e.g. "8.8") when given, else ~0.9*yield from the Materials DB,
    else 640 MPa (~class 8.8).

    Returns {preload_n, tensile_stress_area_mm2, bolt_stress_mpa, proof_load_n,
    preload_pct_proof, bolt_stress_with_load_mpa, separation_load_n,
    separation_margin, pass, governing}."""
    at = None
    if bolt_size is not None:
        from . import standards
        card = standards.thread(bolt_size)
        if bolt_dia_mm is None:
            bolt_dia_mm = card["nominal_dia_mm"]
        if pitch_mm is None:
            pitch_mm = standards.thread_pitch(bolt_size)
        at = standards.tensile_stress_area(bolt_size)  # tabulated At
        if property_class is not None and proof_strength_mpa is None:
            proof_strength_mpa = standards.proof_strength_mpa(bolt_size, property_class)
    if bolt_dia_mm is None:
        raise ValueError("provide bolt_dia_mm or bolt_size")
    if property_class is not None and proof_strength_mpa is None:
        from . import standards
        # property class without a named size: derive Sp ignoring the >M16 split
        proof_strength_mpa = standards.thread_property_class(property_class)["proof_strength_mpa"]
    if at is None:
        at = tensile_stress_area_mm2(bolt_dia_mm, pitch_mm)
    if preload_n is None:
        if torque_nm is None:
            raise ValueError("provide torque_nm or preload_n")
        preload_n = torque_nm / (k_factor * (bolt_dia_mm / 1000.0))  # N

    if proof_strength_mpa is None:
        y = _mat_value(material, "yield_mpa")
        proof_strength_mpa = 0.9 * y if y else 640.0

    bolt_stress = preload_n / at
    proof_load = proof_strength_mpa * at
    pct_proof = preload_n / proof_load

    d_bolt = joint_stiffness_ratio * external_load_n
    bolt_stress_with_load = (preload_n + d_bolt) / at
    sep_load = preload_n / (1.0 - joint_stiffness_ratio)
    sep_margin = (sep_load / external_load_n) if external_load_n > 0 else float("inf")

    reasons = []
    if pct_proof > 0.92:
        reasons.append("preload exceeds 92% of proof")
    if bolt_stress_with_load > proof_strength_mpa:
        reasons.append("bolt yields under external load")
    if external_load_n > 0 and sep_margin < 1.0:
        reasons.append("joint separates")
    ok = not reasons
    return {
        "preload_n": round(preload_n, 1),
        "tensile_stress_area_mm2": round(at, 2),
        "bolt_stress_mpa": round(bolt_stress, 1),
        "proof_load_n": round(proof_load, 1),
        "preload_pct_proof": round(pct_proof, 3),
        "bolt_stress_with_load_mpa": round(bolt_stress_with_load, 1),
        "separation_load_n": round(sep_load, 1),
        "separation_margin": (round(sep_margin, 2) if math.isfinite(sep_margin) else None),
        "pass": ok,
        "governing": reasons[0] if reasons else f"preload {pct_proof:.0%} of proof",
        "target_preload_pct": preload_target_pct,
    }


def bearing_life(
    dynamic_load_c_n: float | None = None,
    equivalent_load_p_n: float | None = None,
    speed_rpm: float | None = None,
    designation: str | None = None,
    kind: str = "ball",
    target_hours: float | None = None,
) -> dict:
    """Basic rating life L10 (ISO 281): L10 = (C/P)^p revolutions, p=3 for ball,
    10/3 for roller. L10h = L10*1e6 / (60*n).

    Supply the dynamic load rating C directly (dynamic_load_c_n) or pull it from
    the deep-groove ball-bearing catalog by `designation` (e.g. "6205" -> C=14.0 kN).
    An explicit dynamic_load_c_n overrides the catalog value. A catalog lookup also
    reports the bearing's bore/OD/width and static rating C0 plus the static safety
    factor s0 = C0/P.

    Returns {l10_million_rev, l10_hours, load_ratio, dynamic_load_c_n, exponent,
    pass} plus {designation, bore_mm, od_mm, width_mm, static_load_c0_n,
    static_safety_factor} when a designation is used. `pass` is True when
    target_hours is None, else L10h >= target_hours. Raises ValueError if neither C
    nor a designation is given (or P/speed missing), standards.StandardNotFound for
    an unknown designation."""
    if equivalent_load_p_n is None or equivalent_load_p_n <= 0:
        raise ValueError("equivalent_load_p_n must be > 0")
    if speed_rpm is None or speed_rpm <= 0:
        raise ValueError("speed_rpm must be > 0")
    card = None
    if designation is not None:
        from . import standards
        card = standards.bearing(designation)
        if dynamic_load_c_n is None:
            dynamic_load_c_n = card["dynamic_c_n"]
    if dynamic_load_c_n is None:
        raise ValueError("provide dynamic_load_c_n or designation")
    p = 3.0 if kind == "ball" else 10.0 / 3.0
    l10_mrev = (dynamic_load_c_n / equivalent_load_p_n) ** p
    l10_hours = l10_mrev * 1e6 / (60.0 * speed_rpm)
    ok = True if target_hours is None else l10_hours >= target_hours
    out = {
        "l10_million_rev": round(l10_mrev, 2),
        "l10_hours": round(l10_hours, 1),
        "load_ratio": round(dynamic_load_c_n / equivalent_load_p_n, 3),
        "dynamic_load_c_n": round(dynamic_load_c_n, 1),
        "exponent": p,
        "pass": ok,
    }
    if card is not None:
        out.update({
            "designation": card["designation"],
            "bore_mm": card["bore_mm"], "od_mm": card["od_mm"],
            "width_mm": card["width_mm"],
            "static_load_c0_n": round(card["static_c0_n"], 1),
            "static_safety_factor": round(card["static_c0_n"] / equivalent_load_p_n, 2),
        })
    return out


def spring_check(
    wire_dia_mm: float,
    coil_mean_dia_mm: float,
    active_coils: float,
    force_n: float | None = None,
    deflection_mm: float | None = None,
    material: str = "Steel-1045",
    shear_modulus_mpa: float | None = None,
    free_length_mm: float | None = None,
    allowable_shear_mpa: float | None = None,
) -> dict:
    """Rate a helical compression spring (Wahl correction).

    Index C = D/d; Wahl Kw = (4C-1)/(4C-4) + 0.615/C; rate k = G d^4/(8 D^3 Na);
    corrected shear stress tau = Kw * 8 F D / (pi d^3). Provide force_n OR
    deflection_mm. G defaults from the Materials DB (E, nu -> G = E/2(1+nu)),
    else 79300 MPa. Buckling flagged when free_length/D exceeds ~2.6.

    Returns {spring_index, wahl_factor, rate_n_mm, force_n, deflection_mm,
    shear_stress_mpa, slenderness, buckling_flag, allowable_shear_mpa,
    shear_sf, pass}."""
    c = coil_mean_dia_mm / wire_dia_mm
    kw = (4 * c - 1) / (4 * c - 4) + 0.615 / c

    if shear_modulus_mpa is None:
        e = _mat_value(material, "youngs_mpa")
        nu = _mat_value(material, "poisson")
        shear_modulus_mpa = e / (2 * (1 + nu)) if (e and nu) else 79300.0

    rate = shear_modulus_mpa * wire_dia_mm ** 4 / (
        8 * coil_mean_dia_mm ** 3 * active_coils
    )
    if force_n is None and deflection_mm is None:
        raise ValueError("provide force_n or deflection_mm")
    if force_n is None:
        force_n = rate * deflection_mm
    if deflection_mm is None:
        deflection_mm = force_n / rate

    tau = kw * 8 * force_n * coil_mean_dia_mm / (math.pi * wire_dia_mm ** 3)

    slenderness = (free_length_mm / coil_mean_dia_mm) if free_length_mm else None
    buckling = (slenderness > 2.6) if slenderness is not None else None

    if allowable_shear_mpa is None:
        uts = _mat_value(material, "uts_mpa")
        allowable_shear_mpa = 0.45 * uts if uts else 700.0
    shear_sf = allowable_shear_mpa / tau

    ok = shear_sf >= 1.0 and not (buckling is True)
    return {
        "spring_index": round(c, 3),
        "wahl_factor": round(kw, 4),
        "rate_n_mm": round(rate, 4),
        "force_n": round(force_n, 2),
        "deflection_mm": round(deflection_mm, 3),
        "shear_stress_mpa": round(tau, 1),
        "slenderness": (round(slenderness, 2) if slenderness is not None else None),
        "buckling_flag": buckling,
        "allowable_shear_mpa": round(allowable_shear_mpa, 1),
        "shear_sf": round(shear_sf, 2),
        "pass": ok,
    }


def _lewis_form_factor(teeth: int) -> float:
    """Approximate Lewis form factor Y for a 20-deg full-depth spur tooth."""
    return 0.484 - 2.87 / teeth


def gear_rating(
    module_mm: float,
    teeth: int,
    face_width_mm: float,
    tangential_force_n: float | None = None,
    power_w: float | None = None,
    pinion_speed_rpm: float | None = None,
    material: str = "Steel-4140-QT",
    lewis_form_factor: float | None = None,
    allowable_bending_mpa: float | None = None,
) -> dict:
    """Rate spur-gear tooth bending (Lewis): sigma = Ft / (b * m * Y).

    Give tangential_force_n directly, or power_w + pinion_speed_rpm (Ft is then
    derived from pitch-line velocity V = pi * d * n, with pitch dia d = m*z). Y is
    a 20-deg full-depth Lewis estimate from tooth count unless overridden.
    Allowable bending defaults to the material's fatigue endurance (else 0.3*UTS).

    Returns {tangential_force_n, pitch_dia_mm, pitch_line_velocity_m_s,
    lewis_form_factor, bending_stress_mpa, allowable_bending_mpa, bending_sf,
    pass}. This is a first-order screen, NOT a full AGMA rating."""
    pitch_dia_mm = module_mm * teeth
    v = None
    if tangential_force_n is None:
        if power_w is None or pinion_speed_rpm is None:
            raise ValueError("provide tangential_force_n, or power_w + pinion_speed_rpm")
        v = math.pi * (pitch_dia_mm / 1000.0) * (pinion_speed_rpm / 60.0)  # m/s
        tangential_force_n = power_w / v
    elif pinion_speed_rpm is not None:
        v = math.pi * (pitch_dia_mm / 1000.0) * (pinion_speed_rpm / 60.0)

    y = lewis_form_factor if lewis_form_factor is not None else _lewis_form_factor(teeth)
    bending_stress = tangential_force_n / (face_width_mm * module_mm * y)

    if allowable_bending_mpa is None:
        fe = _mat_value(material, "fatigue_mpa")
        uts = _mat_value(material, "uts_mpa")
        allowable_bending_mpa = fe if fe else (0.3 * uts if uts else 300.0)
    sf = allowable_bending_mpa / bending_stress
    return {
        "tangential_force_n": round(tangential_force_n, 1),
        "pitch_dia_mm": round(pitch_dia_mm, 2),
        "pitch_line_velocity_m_s": (round(v, 3) if v is not None else None),
        "lewis_form_factor": round(y, 4),
        "bending_stress_mpa": round(bending_stress, 1),
        "allowable_bending_mpa": round(allowable_bending_mpa, 1),
        "bending_sf": round(sf, 2),
        "pass": sf >= 1.0,
    }


def belt_drive(
    power_w: float,
    small_pulley_dia_mm: float,
    large_pulley_dia_mm: float,
    center_distance_mm: float,
    small_pulley_rpm: float,
    friction_coef: float = 0.3,
    vbelt_groove_deg: float | None = None,
    tight_side_limit_n: float | None = None,
) -> dict:
    """Rate a belt drive (Eytelwein / capstan).

    Wrap on the small pulley theta = pi - 2*asin((D-d)/2C); belt speed
    V = pi*d*n. Effective force Fe = P/V. Tension ratio T1/T2 = e^(mu*theta)
    (flat) or e^(mu*theta/sin(beta/2)) for a V-belt of groove angle beta. Required
    tensions to transmit Fe without slip: T1 = Fe*R/(R-1), T2 = Fe/(R-1).

    Returns {wrap_angle_deg, belt_speed_m_s, effective_force_n, tension_ratio,
    tight_side_n, slack_side_n, transmissible_power_w, pass}. If tight_side_limit_n
    is given, pass = required T1 <= limit (else pass True)."""
    d, dd, c = small_pulley_dia_mm, large_pulley_dia_mm, center_distance_mm
    theta = math.pi - 2.0 * math.asin((dd - d) / (2.0 * c))
    v = math.pi * (d / 1000.0) * (small_pulley_rpm / 60.0)  # m/s
    fe = power_w / v
    mu_eff = (friction_coef / math.sin(math.radians(vbelt_groove_deg) / 2.0)
              if vbelt_groove_deg else friction_coef)
    r = math.exp(mu_eff * theta)
    t1 = fe * r / (r - 1.0)
    t2 = fe / (r - 1.0)
    if tight_side_limit_n:
        fe_max = tight_side_limit_n * (r - 1.0) / r
        transmissible = fe_max * v
        ok = t1 <= tight_side_limit_n
    else:
        transmissible = power_w
        ok = True
    return {
        "wrap_angle_deg": round(math.degrees(theta), 1),
        "belt_speed_m_s": round(v, 3),
        "effective_force_n": round(fe, 1),
        "tension_ratio": round(r, 4),
        "tight_side_n": round(t1, 1),
        "slack_side_n": round(t2, 1),
        "transmissible_power_w": round(transmissible, 1),
        "pass": ok,
    }


def press_fit_stress(
    shaft_dia_mm: float,
    hub_outer_dia_mm: float,
    interference_mm: float,
    engagement_length_mm: float,
    material: str = "Steel-A36",
    friction_coef: float = 0.15,
    youngs_modulus_mpa: float | None = None,
) -> dict:
    """Rate an interference (press/shrink) fit via Lamé thick-wall theory.

    Solid shaft + hub of the same material. With contact radius rc, hub outer
    radius ro, radial interference delta_r = interference/2:
      p = delta_r*E*(ro^2-rc^2)/(2*rc*ro^2)            contact pressure
      sigma_theta_hub = p*(ro^2+rc^2)/(ro^2-rc^2)      hub bore hoop stress (max)
      torque = 2*pi*mu*p*rc^2*L ; axial_force = mu*p*2*pi*rc*L

    interference_mm is diametral. Every output is LINEAR in E, so E must come from
    the part's own material or from `youngs_modulus_mpa` — never from an assumption
    (#269). A material the corpus has no `youngs_mpa` for raises, naming both exits.

    `pass` is THREE-state. The hub yield check needs `yield_mpa`; a corpus card that
    lacks it (Fused-Silica, Gold, Concrete, ...) leaves the check unperformed, and an
    unperformed check is not a passed one — `pass` is then None with the reason in
    `warnings`, never True. Callers that need a decision must test `is True` /
    `is False` rather than truthiness.

    Returns {contact_pressure_mpa, hub_hoop_stress_mpa, torque_capacity_nm,
    axial_force_n, hub_yield_sf, youngs_modulus_mpa, youngs_basis
    ('explicit'|'material'), hub_yield_basis ('material'|'unavailable'), pass
    (True|False|None), warnings}."""
    rc = shaft_dia_mm / 2.0
    ro = hub_outer_dia_mm / 2.0
    if ro <= rc:
        raise ValueError("hub_outer_dia_mm must exceed shaft_dia_mm")
    delta_r = interference_mm / 2.0

    # E: explicit override, else the card, else REFUSE. The old code ended this
    # chain with `or 200000.0` — steel — so an aluminium or glass hub was rated
    # with steel's modulus and nothing in the result said so, overstating contact
    # pressure, torque and axial force ~2.9x in the unsafe direction (#269).
    if youngs_modulus_mpa is not None:
        e, youngs_basis = float(youngs_modulus_mpa), "explicit"
    else:
        e, youngs_basis = _mat_value(material, "youngs_mpa"), "material"
    if not e or e <= 0:
        raise ValueError(
            f"no Young's modulus for {material!r} — pass youngs_modulus_mpa, "
            + materials.exit_hint(material, "youngs_mpa")
        )

    p = delta_r * e * (ro**2 - rc**2) / (2.0 * rc * ro**2)
    hoop = p * (ro**2 + rc**2) / (ro**2 - rc**2)
    torque_nmm = 2.0 * math.pi * friction_coef * p * rc**2 * engagement_length_mm
    axial = friction_coef * p * 2.0 * math.pi * rc * engagement_length_mm

    warnings = []
    y = _mat_value(material, "yield_mpa")
    hub_sf = (y / hoop) if y else None
    if hub_sf is None:
        # The check could not be RUN. Reporting that as pass:True is the silent
        # pass #248 and #261 were fixed for, one layer down.
        ok = None
        warnings.append(
            f"no yield strength for {material!r}, so the hub yield check was not "
            "performed — `pass` is null (NOT a pass); pass a material the corpus "
            "carries yield_mpa for, or check the hub stress against your own limit"
        )
    else:
        ok = hub_sf >= 1.0
    return {
        "contact_pressure_mpa": round(p, 2),
        "hub_hoop_stress_mpa": round(hoop, 1),
        "torque_capacity_nm": round(torque_nmm / 1000.0, 1),
        "axial_force_n": round(axial, 1),
        "hub_yield_sf": (round(hub_sf, 2) if hub_sf is not None else None),
        "youngs_modulus_mpa": round(e, 1),
        "youngs_basis": youngs_basis,
        "hub_yield_basis": ("material" if hub_sf is not None else "unavailable"),
        "pass": ok,
        "warnings": warnings,
    }


# --- chain / sprocket rating (ASME B29.1) -------------------------------------

# ANSI standard roller-chain numbers: pitch (in), roller diameter (in) and the
# roller-impact constant K_r used in the Type-II rating. K_r = 29 for the full
# 25–240 series, 17 for the lightweight #41 (ANSI/ASME B29.1, Machinery's
# Handbook 30th, "Roller Chain" horsepower ratings). Pitch is the defining
# geometry the add_sprocket generator consumes (chain_pitch_mm = pitch·25.4).
_ANSI_CHAIN = {
    # number: (pitch_in, roller_dia_in, k_r)
    "25":  (0.250, 0.130, 29.0),
    "35":  (0.375, 0.200, 29.0),
    "40":  (0.500, 0.312, 29.0),
    "41":  (0.500, 0.306, 17.0),   # lightweight
    "50":  (0.625, 0.400, 29.0),
    "60":  (0.750, 0.469, 29.0),
    "80":  (1.000, 0.625, 29.0),
    "100": (1.250, 0.750, 29.0),
    "120": (1.500, 0.875, 29.0),
    "140": (1.750, 1.000, 29.0),
    "160": (2.000, 1.125, 29.0),
    "200": (2.500, 1.562, 29.0),
    "240": (3.000, 1.875, 29.0),
}

# Multiple-strand power factors (ASME B29.1): a chain of N strands transmits less
# than N× a single strand (uneven load sharing).
_STRAND_FACTOR = {1: 1.0, 2: 1.7, 3: 2.5, 4: 3.3, 5: 3.9, 6: 4.6}

_HP_TO_W = 745.699872


def chain_drive(
    teeth_small: int,
    speed_rpm: float,
    chain_pitch_mm: float | None = None,
    chain_number: str | None = None,
    strands: int = 1,
    power_w: float | None = None,
    k_r: float | None = None,
) -> dict:
    """Rate an ANSI roller-chain drive (ASME B29.1 single-strand horsepower).

    Two failure envelopes bound the rated power of one strand; the drive is rated
    at the LOWER of the two at the operating speed:

      * **Type I — link-plate fatigue** (governs at low/moderate speed):
        ``HP1 = 0.004 · N1^1.08 · n1^0.9 · P^(3 − 0.07·P)``
      * **Type II — roller/bushing impact** (governs at high speed):
        ``HP2 = 1000 · K_r · N1^1.5 · P^0.8 / n1^1.5``

    with N1 the small-sprocket tooth count, n1 the small-sprocket rpm and P the
    chain pitch in **inches**. Give the pitch metrically (``chain_pitch_mm``, to
    match ``add_sprocket``) OR name a standard ``chain_number`` ("40", "60", …)
    to pull pitch and K_r from the ANSI table. Multiple ``strands`` scale by the
    B29.1 factor (2→1.7, 3→2.5, 4→3.3). Powers are returned in watts.

    Returns {rated_power_w, type1_power_w, type2_power_w, governing, strands,
    strand_factor, teeth_small, speed_rpm, chain_pitch_mm, required_power_w?,
    power_sf?, pass}. `pass` is True when power_w is None, else rated >= power_w.
    Raises ValueError on bad geometry / an unknown chain_number."""
    if teeth_small < 3:
        raise ValueError("teeth_small must be >= 3")
    if speed_rpm <= 0:
        raise ValueError("speed_rpm must be > 0")
    if strands not in _STRAND_FACTOR:
        raise ValueError(f"strands must be one of {sorted(_STRAND_FACTOR)}")
    if chain_number is not None:
        key = str(chain_number).strip().upper().lstrip("#")
        if key not in _ANSI_CHAIN:
            raise ValueError(
                f"unknown chain_number {chain_number!r}; known: {sorted(_ANSI_CHAIN)}")
        pitch_in, _roller, tbl_kr = _ANSI_CHAIN[key]
        if chain_pitch_mm is None:
            chain_pitch_mm = pitch_in * 25.4
        if k_r is None:
            k_r = tbl_kr
    if chain_pitch_mm is None or chain_pitch_mm <= 0:
        raise ValueError("provide chain_pitch_mm (or a chain_number) > 0")
    if k_r is None:
        k_r = 29.0

    n1 = float(teeth_small)
    p_in = chain_pitch_mm / 25.4
    hp1 = 0.004 * n1 ** 1.08 * speed_rpm ** 0.9 * p_in ** (3.0 - 0.07 * p_in)
    hp2 = 1000.0 * k_r * n1 ** 1.5 * p_in ** 0.8 / speed_rpm ** 1.5

    factor = _STRAND_FACTOR[strands]
    p1 = hp1 * _HP_TO_W * factor
    p2 = hp2 * _HP_TO_W * factor
    rated = min(p1, p2)
    governing = "link_plate_fatigue" if p1 <= p2 else "roller_impact"

    out = {
        "rated_power_w": round(rated, 2),
        "type1_power_w": round(p1, 2),
        "type2_power_w": round(p2, 2),
        "governing": governing,
        "strands": strands,
        "strand_factor": factor,
        "teeth_small": teeth_small,
        "speed_rpm": speed_rpm,
        "chain_pitch_mm": round(chain_pitch_mm, 4),
    }
    if power_w is not None:
        out["required_power_w"] = round(power_w, 2)
        out["power_sf"] = round(rated / power_w, 3) if power_w > 0 else None
        out["pass"] = rated >= power_w
    else:
        out["pass"] = True
    return out


# --- fillet weld group rating (Blodgett treat-weld-as-a-line) -----------------

def _weld_line_props(segments):
    """Unit-throat (treat-weld-as-a-line) section properties of a set of straight
    weld segments. Each segment is ((x1,y1),(x2,y2)) in mm. Returns
    (length, cx, cy, Ix, Iy, J) — line length (mm), centroid (mm) and the
    centroidal second moments of the LINE (mm^3, unit throat), J = Ix + Iy."""
    total_l = 0.0
    sx = sy = 0.0
    for (x1, y1), (x2, y2) in segments:
        ell = math.hypot(x2 - x1, y2 - y1)
        total_l += ell
        sx += ell * (x1 + x2) / 2.0
        sy += ell * (y1 + y2) / 2.0
    if total_l <= 0:
        raise ValueError("weld segments have zero total length")
    cx, cy = sx / total_l, sy / total_l

    ix = iy = 0.0
    for (x1, y1), (x2, y2) in segments:
        ell = math.hypot(x2 - x1, y2 - y1)
        # second moment of a line segment about the global axes:
        #   ∫ y^2 ds = ell·(y1^2 + y1·y2 + y2^2)/3  (Simpson on a linear y(s))
        ix += ell * (y1 * y1 + y1 * y2 + y2 * y2) / 3.0
        iy += ell * (x1 * x1 + x1 * x2 + x2 * x2) / 3.0
    # parallel-axis shift to the centroid
    ix -= total_l * cy * cy
    iy -= total_l * cx * cx
    return total_l, cx, cy, ix, iy, ix + iy


def weld_group(
    segments: list,
    force_n: list | tuple,
    load_point_mm: list | tuple,
    leg_mm: float | None = None,
    allowable_shear_mpa: float = 96.0,
    weld_type: str = "fillet",
) -> dict:
    """Rate a planar fillet-weld group by Blodgett's treat-the-weld-as-a-line method.

    ``segments`` is a list of straight welds ``[((x1,y1),(x2,y2)), …]`` (mm), all
    in the load plane. An in-plane force ``force_n = [Fx, Fy]`` applied at
    ``load_point_mm = [px, py]`` produces, per unit weld length:

      * **direct shear** f_d = F / L (uniform over the group),
      * **torsional shear** f_t = T·r / J from the eccentric moment
        T = (px−x̄)·Fy − (py−ȳ)·Fx about the weld centroid, directed ⟂ to the
        radius (f_tx = −T·(y−ȳ)/J, f_ty = +T·(x−x̄)/J).

    The two are added vectorially at every segment end; the **worst point** sets
    the resultant force per unit length f_r (N/mm). The required fillet leg is
    ``ω = f_r / (0.707 · allowable_shear)``; given ``leg_mm`` the actual throat
    stress ``f_r / (0.707·ω)`` is compared to ``allowable_shear_mpa`` for a
    safety factor. All properties use the unit-throat line idealisation, so J and
    the section moduli scale linearly with the real throat.

    Returns {weld_length_mm, centroid_mm, Ix_mm3, Iy_mm3, J_mm3,
    direct_shear_n_per_mm, max_shear_n_per_mm, worst_point_mm, required_leg_mm,
    leg_mm?, throat_stress_mpa?, shear_sf?, pass}. Raises ValueError on empty /
    zero-length geometry or a non-positive allowable."""
    if not segments:
        raise ValueError("provide at least one weld segment")
    if allowable_shear_mpa <= 0:
        raise ValueError("allowable_shear_mpa must be > 0")
    fx, fy = float(force_n[0]), float(force_n[1])
    px, py = float(load_point_mm[0]), float(load_point_mm[1])

    length, cx, cy, ix, iy, j = _weld_line_props(segments)
    torque = (px - cx) * fy - (py - cy) * fx        # about centroid, +z

    fdx, fdy = fx / length, fy / length
    direct = math.hypot(fdx, fdy)

    # evaluate the resultant per-length force at every segment endpoint
    pts = []
    for (x1, y1), (x2, y2) in segments:
        pts.append((x1, y1))
        pts.append((x2, y2))
    worst = 0.0
    worst_pt = (cx, cy)
    for x, y in pts:
        ftx = -torque * (y - cy) / j if j > 0 else 0.0
        fty = torque * (x - cx) / j if j > 0 else 0.0
        fr = math.hypot(fdx + ftx, fdy + fty)
        if fr > worst:
            worst = fr
            worst_pt = (x, y)

    required_leg = worst / (0.707 * allowable_shear_mpa)
    out = {
        "weld_length_mm": round(length, 4),
        "centroid_mm": [round(cx, 4), round(cy, 4)],
        "Ix_mm3": round(ix, 4),
        "Iy_mm3": round(iy, 4),
        "J_mm3": round(j, 4),
        "direct_shear_n_per_mm": round(direct, 4),
        "max_shear_n_per_mm": round(worst, 4),
        "worst_point_mm": [round(worst_pt[0], 4), round(worst_pt[1], 4)],
        "required_leg_mm": round(required_leg, 4),
        "weld_type": weld_type,
    }
    if leg_mm is not None:
        if leg_mm <= 0:
            raise ValueError("leg_mm must be > 0")
        throat_stress = worst / (0.707 * leg_mm)
        out["leg_mm"] = leg_mm
        out["throat_stress_mpa"] = round(throat_stress, 3)
        out["shear_sf"] = round(allowable_shear_mpa / throat_stress, 3)
        out["pass"] = throat_stress <= allowable_shear_mpa
    else:
        out["pass"] = True
    return out


# Acceptable O-ring squeeze bands (% of cross-section) by application.
_SQUEEZE_RANGE = {
    "static_radial": (0.15, 0.30),
    "static_axial": (0.15, 0.30),
    "dynamic": (0.10, 0.20),
}


def seal_check(
    cross_section_dia_mm: float,
    groove_depth_mm: float,
    groove_width_mm: float,
    application: str = "static_radial",
    max_gland_fill_pct: float = 90.0,
) -> dict:
    """Rate an O-ring gland (squeeze + gland-fill), pairing with oring_groove.

    squeeze = W - depth; squeeze_pct = squeeze/W. gland_fill = O-ring CS area
    (pi/4 W^2) / groove area (width*depth). Squeeze must fall in the application
    band (static 15-30%, dynamic 10-20%); fill must stay below max_gland_fill_pct
    (room for thermal expansion).

    Returns {squeeze_mm, squeeze_pct, gland_fill_pct, squeeze_range_pct,
    within_squeeze, within_fill, pass}."""
    w = cross_section_dia_mm
    squeeze = w - groove_depth_mm
    squeeze_pct = squeeze / w * 100.0
    ring_area = math.pi / 4.0 * w**2
    groove_area = groove_width_mm * groove_depth_mm
    fill_pct = ring_area / groove_area * 100.0

    lo, hi = _SQUEEZE_RANGE.get(application, (0.15, 0.30))
    within_sq = (lo * 100.0) <= squeeze_pct <= (hi * 100.0)
    within_fill = fill_pct <= max_gland_fill_pct
    return {
        "squeeze_mm": round(squeeze, 3),
        "squeeze_pct": round(squeeze_pct, 1),
        "gland_fill_pct": round(fill_pct, 1),
        "squeeze_range_pct": [lo * 100.0, hi * 100.0],
        "within_squeeze": within_sq,
        "within_fill": within_fill,
        "pass": within_sq and within_fill,
    }

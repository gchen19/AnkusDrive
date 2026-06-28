"""Laminate / composite-stack mechanical oracle — effective stiffness, thermal
warp, and first-ply failure of a bonded multi-layer stack (issue #103).

Pure-Python, FreeCAD-free. The closed-form screening twin a layered
multi-material FEM static solve (the ``beam_modal`` ↔ ``fem_modal`` pairing the
linear path already has) is gated against: from just an ordered stack
``[(material, thickness), …]`` it returns

* **In-plane modulus** — Voigt / rule-of-mixtures (iso-strain) parallel to the
  layers; Reuss (series) through the thickness. A single-material stack reduces
  to that material's ``E`` exactly.
* **Bending via transformed section** — the modulus-weighted neutral axis, the
  effective ``EI_eff`` about it, and the flexural modulus
  E_flex = 12·EI/(b·h³). Equal-modulus layers put the neutral axis at
  mid-thickness; a single material gives E_flex = E.
* **CLT ``ABD`` matrix** — A (extensional), B (extension–bending coupling), D
  (bending), per unit width, from each isotropic layer's plane-stress reduced
  stiffness Q. A stack symmetric about its midplane has B = 0; an asymmetric
  one (e.g. metal + plastic) has B ≠ 0 — the bending–extension coupling that
  makes the part warp, flagged with a ``coupling`` warning.
* **Effective ρ, CTE, conductivity** — mass-averaged density; the
  stiffness-thickness-weighted in-plane CTE; series-through-thickness /
  parallel-in-plane thermal conductivity.
* **Bimetal thermal curvature** — the Euler–Bernoulli transformed-section warp
  of the bonded stack under ``delta_T`` (general N-layer); for a two-layer strip
  this is exactly Timoshenko's 1925 bimetal formula, reported alongside as a
  cross-check. ``delta_T = 0`` or zero CTE-mismatch → zero curl.
* **First-ply failure** — under an applied moment / in-plane force the strain
  field ε(y) = ε₀ + κ·y gives each layer's extreme-fibre stress
  σ_i = E_i·(ε₀ + κ·y_max,i); the layer with the smallest margin to its yield
  governs, and the load is scaled linearly to first yield.

Lengths mm, stresses/moduli MPa, forces N, moments N·mm, temperatures K (ΔT),
CTE 1/K. fidelity="exact" throughout (closed-form linear elasticity / CLT;
the transformed-section warp equals Timoshenko exactly for two layers). Theory
limits (thin-stack Euler–Bernoulli, perfect bond, linear elastic, isotropic
layers, no edge effects) are stated in each return's ``warnings`` /
``escalate_to``.
"""
from __future__ import annotations

from . import materials


def _pick(d: dict, *keys):
    """First present, non-None value among `keys` in mapping `d`, else None."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _from_card(card, accessor):
    if card is None:
        return None
    try:
        return materials.numeric(card, accessor)
    except (materials.MaterialNotFound, KeyError):
        return None


def _resolve_layer(layer: dict, index: int) -> dict:
    """Resolve one stack entry into explicit floats. A layer is a mapping with a
    `thickness` (or `thickness_mm`) and either a `material` name (looked up in the
    corpus) or explicit `E`/`youngs_mpa`/`youngs_gpa`, `nu`/`poisson`, and
    optionally `yield_mpa`, `cte`/`cte_per_k`, `density_kg_m3`/`rho`,
    `thermal_conductivity_w_mk`/`k`. Explicit values always override the card."""
    if not isinstance(layer, dict):
        raise ValueError(f"layer {index} must be a mapping, got {type(layer).__name__}")

    name = _pick(layer, "material")
    card = None
    if name:
        card = materials.get(name)  # raises MaterialNotFound with suggestions

    t = _pick(layer, "thickness", "thickness_mm")
    if t is None:
        raise ValueError(f"layer {index} needs a thickness")
    t = float(t)
    if t <= 0:
        raise ValueError(f"layer {index} thickness must be > 0")

    e = _pick(layer, "E", "youngs_mpa")
    e_gpa = _pick(layer, "youngs_gpa")
    if e is None and e_gpa is not None:
        e = float(e_gpa) * 1e3
    if e is None:
        e = _from_card(card, "youngs_mpa")
    if e is None:
        raise ValueError(f"layer {index} needs a modulus (E/youngs_mpa/youngs_gpa or a material)")
    e = float(e)
    if e <= 0:
        raise ValueError(f"layer {index} modulus must be > 0")

    nu = _pick(layer, "nu", "poisson")
    if nu is None:
        nu = _from_card(card, "poisson")
    nu_assumed = nu is None
    nu = 0.3 if nu_assumed else float(nu)

    yld = _pick(layer, "yield_mpa", "yield")
    if yld is None:
        yld = _from_card(card, "yield_mpa")
    cte = _pick(layer, "cte", "cte_per_k")
    if cte is None:
        cte = _from_card(card, "cte_per_k")
    rho = _pick(layer, "density_kg_m3", "rho")
    if rho is None:
        rho = _from_card(card, "density_kg_m3")
    kth = _pick(layer, "thermal_conductivity_w_mk", "k")
    if kth is None:
        kth = _from_card(card, "thermal_conductivity_w_mk")

    return {
        "index": index,
        "material": (card["name"] if card else None),
        "thickness_mm": t,
        "E_mpa": e,
        "nu": nu,
        "nu_assumed": nu_assumed,
        "yield_mpa": (float(yld) if yld is not None else None),
        "cte_per_k": (float(cte) if cte is not None else None),
        "density_kg_m3": (float(rho) if rho is not None else None),
        "thermal_conductivity_w_mk": (float(kth) if kth is not None else None),
    }


def _q_matrix(e: float, nu: float):
    """Isotropic plane-stress reduced stiffness Q (3×3, 1-2-6 ordering)."""
    denom = 1.0 - nu * nu
    q11 = e / denom
    q12 = nu * e / denom
    q66 = e / (2.0 * (1.0 + nu))
    return [[q11, q12, 0.0], [q12, q11, 0.0], [0.0, 0.0, q66]]


def _timoshenko_curvature(lo: dict, hi: dict, delta_t: float) -> float:
    """Timoshenko (1925) two-layer bimetal curvature 1/ρ. `lo`/`hi` are the
    bottom/top layers (each {E_mpa, thickness_mm, cte_per_k}); curls toward the
    lower-CTE side. Returns curvature 1/mm (sign: positive = concave toward `hi`)."""
    a1, a2 = lo["thickness_mm"], hi["thickness_mm"]      # bottom, top thickness
    e1, e2 = lo["E_mpa"], hi["E_mpa"]
    h = a1 + a2
    m = a1 / a2
    n = e1 / e2
    dalpha = (hi["cte_per_k"] - lo["cte_per_k"])
    num = 6.0 * dalpha * delta_t * (1.0 + m) ** 2
    den = h * (3.0 * (1.0 + m) ** 2 + (1.0 + m * n) * (m * m + 1.0 / (m * n)))
    return num / den


def laminate_properties(
    layers: list,
    width_mm: float = 1.0,
    delta_T: float | None = None,
    force_n: float | None = None,
    moment_nmm: float | None = None,
) -> dict:
    """Effective stiffness, thermal warp, and first-ply failure of a bonded
    multi-layer stack (NO solver) — the closed-form twin a layered multi-material
    FEM static solve is gated against.

    `layers` is the stack bottom→top, each a mapping with a `thickness` and either
    a `material` name or explicit `E`/`youngs_mpa`/`youngs_gpa` (+ optional
    `nu`/`poisson`, `yield_mpa`, `cte`/`cte_per_k`, `density_kg_m3`,
    `thermal_conductivity_w_mk`). `width_mm` scales EI / first-ply (per-unit-width
    ABD is width-independent). With `delta_T` (K) the bimetal thermal curvature is
    computed; with `force_n` (in-plane, total across the width) and/or `moment_nmm`
    (about the neutral axis) the first-ply failure margin is returned.

    Computes: in-plane modulus (Voigt rule-of-mixtures parallel, Reuss series
    through-thickness); transformed-section neutral axis, EI_eff, flexural modulus
    E_flex = 12·EI/(b·h³); the CLT A/B/D matrices per unit width (B ≠ 0 ⇒
    bending–extension coupling / warp warning); mass-averaged ρ, stiffness-weighted
    in-plane CTE, series/parallel thermal conductivity; the transformed-section
    bimetal curvature (= Timoshenko's two-layer formula exactly, also reported);
    and per-layer extreme-fibre stress → margin to yield → governing layer + load
    to first yield. A single-material stack collapses to that material's E / EI; a
    symmetric stack gives B = 0; ΔT = 0 or zero CTE-mismatch gives zero curl.

    Escalate to a layered FEM solve (`fem_run`) for thick stacks, anticlastic
    curvature, free-edge interlaminar stress, or non-isotropic plies this
    Euler–Bernoulli / CLT idealization cannot see.

    Returns {n_layers, width_mm, total_thickness_mm, layers, E_inplane_mpa,
    E_through_mpa, neutral_axis_mm, EI_eff_nmm2, E_flex_mpa, A_matrix, B_matrix,
    D_matrix, coupling_ratio, asymmetric, rho_eff_kg_m3, cte_eff_per_k,
    k_through_w_mk, k_inplane_w_mk, delta_T, thermal_curvature_per_mm,
    radius_of_curvature_mm, timoshenko_curvature_per_mm, applied_force_n,
    applied_moment_nmm, kappa_applied_per_mm, axial_strain, layer_stresses,
    first_ply, fidelity, band_pct, valid_range_ok, warnings, escalate_to}."""
    if not layers:
        raise ValueError("layers must be a non-empty list")
    if width_mm <= 0:
        raise ValueError("width_mm must be > 0")
    b = float(width_mm)
    res = [_resolve_layer(layer, i) for i, layer in enumerate(layers)]
    warnings: list[str] = []

    h = sum(lyr["thickness_mm"] for lyr in res)            # total thickness

    # --- per-layer z-edges measured from the bottom (z=0) and the midplane ----
    z_bot = 0.0
    for lyr in res:
        lyr["z0"] = z_bot                                  # bottom edge (from base)
        lyr["z1"] = z_bot + lyr["thickness_mm"]            # top edge
        lyr["yc"] = z_bot + 0.5 * lyr["thickness_mm"]      # centroid (from base)
        z_bot = lyr["z1"]

    # --- in-plane effective modulus -------------------------------------------
    e_inplane = sum(lyr["E_mpa"] * lyr["thickness_mm"] for lyr in res) / h   # Voigt
    e_through = h / sum(lyr["thickness_mm"] / lyr["E_mpa"] for lyr in res)   # Reuss

    # --- transformed-section bending (modulus-weighted neutral axis) ----------
    EA = sum(lyr["E_mpa"] * b * lyr["thickness_mm"] for lyr in res)          # ΣE·A
    EAy = sum(lyr["E_mpa"] * b * lyr["thickness_mm"] * lyr["yc"] for lyr in res)
    na = EAy / EA                                          # neutral axis from base
    ei_eff = 0.0
    for lyr in res:
        a_i = b * lyr["thickness_mm"]
        i_i = b * lyr["thickness_mm"] ** 3 / 12.0
        d_i = lyr["yc"] - na
        ei_eff += lyr["E_mpa"] * (i_i + a_i * d_i * d_i)
    e_flex = 12.0 * ei_eff / (b * h ** 3)

    # --- CLT ABD (per unit width), z from the geometric midplane --------------
    mid = h / 2.0
    A = [[0.0] * 3 for _ in range(3)]
    Bm = [[0.0] * 3 for _ in range(3)]
    D = [[0.0] * 3 for _ in range(3)]
    for lyr in res:
        zk0 = lyr["z0"] - mid
        zk1 = lyr["z1"] - mid
        q = _q_matrix(lyr["E_mpa"], lyr["nu"])
        d1 = zk1 - zk0
        d2 = (zk1 ** 2 - zk0 ** 2) / 2.0
        d3 = (zk1 ** 3 - zk0 ** 3) / 3.0
        for r in range(3):
            for c in range(3):
                A[r][c] += q[r][c] * d1
                Bm[r][c] += q[r][c] * d2
                D[r][c] += q[r][c] * d3

    # coupling: |B11| normalized by sqrt(A11·D11) (dimensionless). Symmetric → ~0.
    b11 = abs(Bm[0][0])
    scale = (A[0][0] * D[0][0]) ** 0.5 or 1.0
    coupling_ratio = b11 / scale
    asymmetric = coupling_ratio > 1e-6
    if asymmetric:
        warnings.append(
            f"asymmetric stack: B11 ≠ 0 (coupling ratio {coupling_ratio:.3g}) — "
            "bending–extension coupling; the part warps under in-plane load or ΔT")

    # --- effective ρ / CTE / conductivity -------------------------------------
    rho_eff = None
    if all(lyr["density_kg_m3"] is not None for lyr in res):
        rho_eff = sum(lyr["density_kg_m3"] * lyr["thickness_mm"] for lyr in res) / h
    cte_eff = None
    if all(lyr["cte_per_k"] is not None for lyr in res):
        # stiffness-thickness-weighted (force-balanced uniform in-plane growth)
        cte_eff = (sum(lyr["E_mpa"] * lyr["thickness_mm"] * lyr["cte_per_k"] for lyr in res)
                   / sum(lyr["E_mpa"] * lyr["thickness_mm"] for lyr in res))
    k_through = k_inplane = None
    if all(lyr["thermal_conductivity_w_mk"] is not None for lyr in res):
        k_through = h / sum(lyr["thickness_mm"] / lyr["thermal_conductivity_w_mk"] for lyr in res)
        k_inplane = sum(lyr["thermal_conductivity_w_mk"] * lyr["thickness_mm"] for lyr in res) / h

    # --- bimetal thermal curvature (transformed-section, general N-layer) -----
    thermal_kappa = radius = timoshenko_kappa = None
    if delta_T is not None:
        dT = float(delta_T)
        if any(lyr["cte_per_k"] is None for lyr in res):
            warnings.append("delta_T given but a layer lacks a CTE — thermal warp skipped")
        else:
            # Euler-Bernoulli: [A_ax B_ax; B_ax D_ax][ε0;κ] = [N_th; M_th], z from base.
            A_ax = sum(lyr["E_mpa"] * lyr["thickness_mm"] for lyr in res)
            B_ax = sum(lyr["E_mpa"] * (lyr["z1"] ** 2 - lyr["z0"] ** 2) / 2.0 for lyr in res)
            D_ax = sum(lyr["E_mpa"] * (lyr["z1"] ** 3 - lyr["z0"] ** 3) / 3.0 for lyr in res)
            N_th = dT * sum(lyr["E_mpa"] * lyr["cte_per_k"] * lyr["thickness_mm"] for lyr in res)
            M_th = dT * sum(lyr["E_mpa"] * lyr["cte_per_k"]
                            * (lyr["z1"] ** 2 - lyr["z0"] ** 2) / 2.0 for lyr in res)
            det = A_ax * D_ax - B_ax * B_ax
            thermal_kappa = (A_ax * M_th - B_ax * N_th) / det
            radius = (1.0 / thermal_kappa) if abs(thermal_kappa) > 1e-300 else None
            if len(res) == 2:
                timoshenko_kappa = _timoshenko_curvature(res[0], res[1], dT)

    # --- first-ply failure under applied force/moment -------------------------
    applied_f = float(force_n) if force_n is not None else None
    applied_m = float(moment_nmm) if moment_nmm is not None else None
    layer_stresses = None
    first_ply = None
    kappa_applied = eps0 = None
    if applied_f is not None or applied_m is not None:
        f = applied_f or 0.0
        m = applied_m or 0.0
        # About the modulus-weighted neutral axis the coupling vanishes:
        # ε0 = F/ΣEA, κ = M/EI_eff; σ_i(y') = E_i(ε0 + κ·y'), y' from the NA.
        eps0 = f / EA
        kappa_applied = m / ei_eff
        layer_stresses = []
        governing = None
        for lyr in res:
            edges = [lyr["z0"] - na, lyr["z1"] - na]      # fibre offsets from NA
            sig_lo = lyr["E_mpa"] * (eps0 + kappa_applied * edges[0])
            sig_hi = lyr["E_mpa"] * (eps0 + kappa_applied * edges[1])
            sig_ext = sig_lo if abs(sig_lo) >= abs(sig_hi) else sig_hi
            entry = {
                "index": lyr["index"],
                "material": lyr["material"],
                "sigma_top_mpa": round(sig_hi, 4),
                "sigma_bottom_mpa": round(sig_lo, 4),
                "sigma_extreme_mpa": round(sig_ext, 4),
                "yield_mpa": lyr["yield_mpa"],
                "margin": None,
            }
            if lyr["yield_mpa"] is not None and abs(sig_ext) > 0:
                margin = lyr["yield_mpa"] / abs(sig_ext)   # load multiple to yield
                entry["margin"] = round(margin, 6)
                if governing is None or margin < governing["margin_raw"]:
                    governing = {"layer": lyr, "sigma": sig_ext, "margin_raw": margin}
            layer_stresses.append(entry)

        if governing is None:
            warnings.append("no layer carries a yield strength — first-ply margin skipped")
        else:
            lf = governing["margin_raw"]
            first_ply = {
                "governing_layer": governing["layer"]["index"],
                "governing_material": governing["layer"]["material"],
                "sigma_mpa": round(governing["sigma"], 4),
                "yield_mpa": governing["layer"]["yield_mpa"],
                "margin": round(lf, 6),
                "load_factor_to_yield": round(lf, 6),
                "force_to_first_yield_n": (round(applied_f * lf, 4)
                                           if applied_f else None),
                "moment_to_first_yield_nmm": (round(applied_m * lf, 4)
                                              if applied_m else None),
                "yields": lf <= 1.0,
            }
            if lf <= 1.0:
                warnings.append(
                    f"first-ply yield: layer {first_ply['governing_layer']} "
                    f"({first_ply['governing_material'] or 'custom'}) at σ="
                    f"{first_ply['sigma_mpa']:.1f} MPa exceeds σ_y="
                    f"{first_ply['yield_mpa']} MPa (load factor {lf:.3f})")

    if any(lyr["nu_assumed"] for lyr in res):
        warnings.append("a layer's Poisson ratio defaulted to 0.30 (none given)")

    def _mat(m):
        return [[round(v, 6) for v in row] for row in m]

    return {
        "n_layers": len(res),
        "width_mm": round(b, 6),
        "total_thickness_mm": round(h, 6),
        "layers": [{
            "index": lyr["index"], "material": lyr["material"],
            "thickness_mm": round(lyr["thickness_mm"], 6),
            "E_mpa": round(lyr["E_mpa"], 4), "nu": round(lyr["nu"], 4),
            "yield_mpa": lyr["yield_mpa"], "cte_per_k": lyr["cte_per_k"],
            "density_kg_m3": lyr["density_kg_m3"],
            "thermal_conductivity_w_mk": lyr["thermal_conductivity_w_mk"],
        } for lyr in res],
        "E_inplane_mpa": round(e_inplane, 4),
        "E_through_mpa": round(e_through, 4),
        "neutral_axis_mm": round(na, 6),
        "EI_eff_nmm2": round(ei_eff, 4),
        "E_flex_mpa": round(e_flex, 4),
        "A_matrix": _mat(A),
        "B_matrix": _mat(Bm),
        "D_matrix": _mat(D),
        "coupling_ratio": round(coupling_ratio, 9),
        "asymmetric": asymmetric,
        "rho_eff_kg_m3": (round(rho_eff, 4) if rho_eff is not None else None),
        "cte_eff_per_k": (cte_eff if cte_eff is None else float(f"{cte_eff:.6g}")),
        "k_through_w_mk": (round(k_through, 6) if k_through is not None else None),
        "k_inplane_w_mk": (round(k_inplane, 6) if k_inplane is not None else None),
        "delta_T": (float(delta_T) if delta_T is not None else None),
        "thermal_curvature_per_mm": (float(f"{thermal_kappa:.6g}")
                                     if thermal_kappa is not None else None),
        "radius_of_curvature_mm": (round(radius, 4) if radius is not None else None),
        "timoshenko_curvature_per_mm": (float(f"{timoshenko_kappa:.6g}")
                                        if timoshenko_kappa is not None else None),
        "applied_force_n": applied_f,
        "applied_moment_nmm": applied_m,
        "kappa_applied_per_mm": (float(f"{kappa_applied:.6g}")
                                 if kappa_applied is not None else None),
        "axial_strain": (float(f"{eps0:.6g}") if eps0 is not None else None),
        "layer_stresses": layer_stresses,
        "first_ply": first_ply,
        "fidelity": "exact",
        "band_pct": None,
        "valid_range_ok": not warnings,
        "warnings": warnings,
        "escalate_to": "fem_run",
    }

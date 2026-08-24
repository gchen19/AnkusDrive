"""Solution verification — how much of a solved number is the discretization (issue #225).

Every other gate in AnkusDrive is a *validation* gate: compare the solve to a closed form
or a correlation and report the ratio. That works exactly where an oracle exists — the
pipe, the plate, the sphere. On arbitrary geometry there is no oracle, and the honest
question becomes the other one: **is this number converged with respect to the mesh, or
is it a property of the mesh?** That is verification, it needs no reference value, and
it is the only band available for a shape nobody has an analytic answer for.

The procedure here is the standard one (Roache's Grid Convergence Index, as codified in
ASME V&V 20-2009): solve the same case at 2–3 systematically refined meshes, fit the
observed order of convergence from how the answer moves, Richardson-extrapolate to zero
cell size, and report the extrapolation's uncertainty as a percentage band. With three
meshes the order is *measured*; with two it must be *assumed*, and the safety factor
triples to say so.

This module is deliberately family-agnostic — it takes numbers and mesh sizes, never a
case. The same three lines gate a CFD drag coefficient, an FEM peak stress, and a modal
frequency; only the caller knows what h means.

Pure-Python, FreeCAD-free, no solver. See ``tests/test_verification.py``.
"""
from __future__ import annotations

import math

# Roache's factor of safety on the extrapolated error. 1.25 is the ASME V&V 20 value for
# a three-mesh study where the order is OBSERVED; 3.0 is the two-mesh value, where the
# order had to be assumed and the band must cover being wrong about it.
_FS_THREE_MESH = 1.25
_FS_TWO_MESH = 3.0

# An observed order outside this range means the sequence is not in the asymptotic
# range (round-off, an unconverged solve, a mesh that changed topology, oscillation).
# We report the fitted value AND the clamped one used for the band, never silently swap.
_ORDER_MIN = 0.5
_ORDER_MAX = 4.0


def _observed_order(f1: float, f2: float, f3: float, r21: float, r32: float) -> dict:
    """Fit the observed order of convergence p from three solutions on meshes with
    refinement ratios ``r21`` (medium/fine) and ``r32`` (coarse/medium).

    Solves the ASME V&V 20 fixed point

        p = |ln|ε32/ε21| + q(p)| / ln(r21),   q(p) = ln((r21^p − s)/(r32^p − s))

    with s = sign(ε32/ε21). For equal ratios q ≡ 0 and it closes in one step. Returns
    ``{order, converged_fit, sign, eps21, eps32}``; ``order`` is None when the sequence
    carries no information (two identical solutions)."""
    eps21 = f2 - f1
    eps32 = f3 - f2
    if eps21 == 0 or eps32 == 0:
        return {"order": None, "converged_fit": False, "sign": 0.0,
                "eps21": eps21, "eps32": eps32}
    ratio = eps32 / eps21
    s = 1.0 if ratio > 0 else -1.0
    p = abs(math.log(abs(ratio))) / math.log(r21)      # q = 0 first guess
    converged_fit = False
    for _ in range(60):
        try:
            q = math.log((r21 ** p - s) / (r32 ** p - s))
        except (ValueError, ZeroDivisionError):
            break
        p_new = abs(math.log(abs(ratio)) + q) / math.log(r21)
        if abs(p_new - p) < 1e-10:
            p, converged_fit = p_new, True
            break
        p = p_new
    return {"order": p, "converged_fit": converged_fit, "sign": s,
            "eps21": eps21, "eps32": eps32}


def grid_convergence(
    values,
    cell_sizes=None,
    cell_counts=None,
    *,
    assumed_order: float = 2.0,
    dimensions: int = 3,
) -> dict:
    """Grid Convergence Index for the same quantity solved on 2–3 refined meshes.

    ``values`` are the solved quantity **finest first**; the mesh is described either by
    ``cell_sizes`` (representative cell length h, same order as ``values``) or by
    ``cell_counts`` (total cells — h is then taken as N^(−1/``dimensions``), the standard
    representative size for an unstructured mesh). Refinement ratios come from those,
    so the meshes need not be refined by a constant factor; ASME asks for r ≳ 1.3 and
    anything below that lands in ``warnings``.

    With **three** values the order of convergence is OBSERVED from the sequence and the
    band uses Fs = 1.25. With **two** it cannot be, so ``assumed_order`` is used (default
    2.0, the formal order of a second-order scheme) and Fs = 3.0 — a materially wider
    band, which is the honest price of the missing mesh.

    The headline output is ``gci_pct``: the percentage band around the FINEST value
    inside which the mesh-independent answer is expected to lie. Treat it as this
    solve's ``band_pct`` when no analytic oracle exists. ``extrapolated_value`` is the
    Richardson estimate at h→0.

    Things that make the number untrustworthy are reported, never smoothed over:
    ``monotonic`` False means the solutions oscillate (the extrapolation is not
    meaningful — the usual cause is an unconverged solve, not a mesh effect);
    ``asymptotic_ratio`` far from 1 means the meshes are not yet in the asymptotic range
    where the theory applies; ``order_clamped`` True means the fitted order was outside
    0.5–4 and the band was computed with the clamped value.

    Returns {n_levels, values, cell_sizes, refinement_ratios, observed_order,
    order_used, order_clamped, extrapolated_value, gci_pct, gci_coarse_pct, band_pct,
    relative_error_pct, monotonic, asymptotic_ratio, safety_factor, converged_fit,
    warnings}. Raises ValueError on fewer than 2 values, a values/mesh length mismatch,
    non-monotone mesh sizes, or non-positive sizes/counts."""
    vals = [float(v) for v in values]
    if len(vals) < 2:
        raise ValueError("grid convergence needs at least 2 mesh levels "
                         "(3 to observe the order rather than assume it)")
    if len(vals) > 3:
        raise ValueError("pass the 3 finest levels; the GCI procedure is defined on "
                         "2 or 3 meshes")
    if (cell_sizes is None) == (cell_counts is None):
        raise ValueError("give exactly one of cell_sizes or cell_counts")
    if cell_sizes is not None:
        h = [float(v) for v in cell_sizes]
    else:
        counts = [float(v) for v in cell_counts]
        if any(c <= 0 for c in counts):
            raise ValueError("cell_counts must all be > 0")
        if dimensions not in (1, 2, 3):
            raise ValueError("dimensions must be 1, 2 or 3")
        h = [c ** (-1.0 / dimensions) for c in counts]
    if len(h) != len(vals):
        raise ValueError("values and the mesh description must be the same length")
    if any(v <= 0 for v in h):
        raise ValueError("cell sizes must be > 0")
    if any(a >= b for a, b in zip(h, h[1:])):
        raise ValueError("meshes must be ordered FINEST FIRST (strictly increasing "
                         "cell size)")

    warnings: list[str] = []
    ratios = [h[i + 1] / h[i] for i in range(len(h) - 1)]
    for i, r in enumerate(ratios):
        if r < 1.3:
            warnings.append(
                f"refinement ratio r{i + 2}{i + 1} = {r:.3g} is below the 1.3 ASME "
                "recommends — the solutions are too close for the fit to separate "
                "discretization error from noise")

    if len(vals) == 3:
        fit = _observed_order(vals[0], vals[1], vals[2], ratios[0], ratios[1])
        observed = fit["order"]
        monotonic = fit["sign"] > 0
        if not monotonic and fit["sign"] != 0:
            warnings.append(
                "the three solutions are NOT monotone (they oscillate): Richardson "
                "extrapolation assumes monotone convergence, so the band below is not "
                "meaningful — check that every level actually converged before "
                "blaming the mesh")
        fs = _FS_THREE_MESH
        converged_fit = fit["converged_fit"]
    else:
        observed = None
        monotonic = True
        fs = _FS_TWO_MESH
        converged_fit = False
        warnings.append(
            f"only 2 meshes: the order of convergence is ASSUMED ({assumed_order:g}), "
            "not observed, and the safety factor is 3.0 instead of 1.25 — add a third "
            "level to measure it")

    order_used = observed if observed is not None else float(assumed_order)
    order_clamped = False
    if order_used is None or not math.isfinite(order_used):
        order_used, order_clamped = float(assumed_order), True
    elif not _ORDER_MIN <= order_used <= _ORDER_MAX:
        warnings.append(
            f"observed order {order_used:.3g} is outside {_ORDER_MIN}-{_ORDER_MAX}: the "
            "meshes are not in the asymptotic range (round-off, an unconverged level, "
            "or a mesh whose topology changed between levels). The band uses the "
            "clamped value")
        order_used = min(max(order_used, _ORDER_MIN), _ORDER_MAX)
        order_clamped = True

    f1, f2 = vals[0], vals[1]
    r21 = ratios[0]
    denom = r21 ** order_used - 1.0
    if denom <= 0:
        raise ValueError("refinement ratio and order give a degenerate extrapolation")
    extrapolated = (r21 ** order_used * f1 - f2) / denom
    e_a21 = abs((f1 - f2) / f1) if f1 != 0 else float("inf")
    gci21 = fs * e_a21 / denom

    gci32 = asym = None
    if len(vals) == 3:
        f3, r32 = vals[2], ratios[1]
        e_a32 = abs((f2 - f3) / f2) if f2 != 0 else float("inf")
        gci32 = fs * e_a32 / (r32 ** order_used - 1.0)
        if gci21 > 0:
            asym = gci32 / (r21 ** order_used * gci21)
            if not 0.85 < asym < 1.15:
                warnings.append(
                    f"asymptotic ratio {asym:.3g} is far from 1 — the mesh sequence has "
                    "not reached the range where the extrapolation theory holds; treat "
                    "gci_pct as a lower bound on the real uncertainty")

    return {
        "n_levels": len(vals),
        "values": vals,
        "cell_sizes": h,
        "refinement_ratios": ratios,
        "observed_order": (round(observed, 6) if observed is not None else None),
        "order_used": round(order_used, 6),
        "order_clamped": order_clamped,
        "extrapolated_value": extrapolated,
        "gci_pct": gci21 * 100.0,
        "gci_coarse_pct": (gci32 * 100.0 if gci32 is not None else None),
        "band_pct": gci21 * 100.0,
        "relative_error_pct": e_a21 * 100.0,
        "monotonic": monotonic,
        "asymptotic_ratio": (round(asym, 6) if asym is not None else None),
        "safety_factor": fs,
        "converged_fit": converged_fit,
        "fidelity": "verification",
        "warnings": warnings,
    }

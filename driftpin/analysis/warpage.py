"""Injection-molding *warpage* — the thermo-elastic post-step of the cooling solve.

This is **Part B of GitHub issue #113** (Part A — packing/cooling — landed in PR
#114). There is no purpose-built open-source injection-molding warpage solver, so
the realistic path mirrors the established coupling pattern (preCICE
OpenFOAM↔CalculiX, FSI): take the **frozen-in temperature / differential-shrinkage
field** from the Part-A cooling solve and hand it to **CalculiX** as a
thermo-elastic post-step that solves the part's free distortion.

The physics (why a part warps). A moulding is ejected hot in the core and cool at
the surface. On the way to room temperature the resin shrinks (CTE α). If that
shrinkage is **uniform** the part just gets smaller — no warp. Warp comes from
*differential* shrinkage:

* an **asymmetric through-thickness** temperature field at ejection (one mould half
  hotter than the other, an unbalanced cooling layout, or a rib on one face) — the
  classic bow, and
* **uneven wall thickness** — thick sections shrink more in-plane than thin ones.

Either way the free thermal strain ``ε_th = α·(T − T_ref)`` is non-uniform across
the part; solving the *free* (rigid-body-constrained only) linear-elastic problem
with that eigenstrain gives the out-of-plane distortion. A balanced part (uniform
or symmetric field) returns ~zero warp; that contrast is the gate.

Fidelity & honesty. This is a **one-way, linear-elastic, loose coupling**: it
ignores viscoelastic stress relaxation during cooling, flow-induced anisotropy /
fibre orientation, the packing-pressure residual stress, and any
solidification-history path dependence. It captures the *dominant* differential-
shrinkage warp and its direction, not a calibrated absolute number — hence a
conservative ``band_pct`` on the gate. ``fidelity="solve"`` (a real FEM
distortion, not a correlation), but read the band.

Solver. **CalculiX** (``ccx``), driven by a deck this module writes directly (the
GPL/▸free solver stays at the subprocess boundary, never imported). CalculiX is
the right fit here: a per-node ``*TEMPERATURE`` field maps the frozen-in cooling
field straight onto the structural mesh, and node-level ``*BOUNDARY`` lets us pin
exactly three nodes in a statically-determinate **3-2-1** scheme that removes the
six rigid-body modes while leaving the part free to expand and warp.

Units. CalculiX is unit-agnostic; this module is consistent in **mm / MPa / 1/K /
°C**, matching FreeCAD's mm mesh export — so displacements come back in **mm**. The
warp is invariant to the reference temperature (a uniform shift is pure isotropic
shrink); ``ref_temp_c`` only affects the reported residual stress.

Pure-Python (FreeCAD-free): the deck text, the analytic twin, the ``.frd`` parser
and the gate are all testable on the no-solver CI lane; only the *solve* needs
``ccx``. See ``tests/test_warpage.py``.
"""

from __future__ import annotations

import os
import re

# Mid-span sagitta of a free strip bent to uniform curvature κ over a chord L is
# κ·L²/8 — the analytic oracle the ccx warp solve is gated against.
_SAGITTA_COEFF = 8.0


# --- the analytic twin -------------------------------------------------------

def free_plate_thermal_bow(
    *, span_mm: float, thickness_mm: float, dT_through_k: float, cte_per_k: float,
) -> dict:
    """Closed-form free-plate thermal bow — the oracle the ccx solve is gated against.

    A flat plate with a **linear through-thickness** temperature difference
    ``dT_through_k`` (``T_bottom − T_top``, K) bends, with no external restraint, to a
    uniform curvature ``κ = α·ΔT / h`` (the thin-plate / bimetallic-strip result; α =
    ``cte_per_k``, h = ``thickness_mm``). Over a span ``L`` the mid-to-end out-of-plane
    deflection (sagitta of the circular arc) is ``δ = κ·L² / 8``.

    This is a thin-beam idealisation (no Poisson anticlastic curvature, no finite-
    thickness shear), so a 3-D ccx solve runs a few percent higher — that few-percent
    agreement is exactly what makes it a useful gate, not an exact equality.

    Returns ``{curvature_per_mm, bow_mm, span_mm, thickness_mm, dT_through_k,
    cte_per_k}``. ``bow_mm`` carries the sign of ``dT_through_k`` (hotter bottom →
    positive → the part bows concave-up toward the hot face)."""
    if thickness_mm <= 0 or span_mm <= 0:
        raise ValueError("span_mm and thickness_mm must be > 0")
    kappa = cte_per_k * dT_through_k / thickness_mm          # 1/mm
    bow = kappa * span_mm * span_mm / _SAGITTA_COEFF          # mm
    return {
        "curvature_per_mm": kappa,
        "bow_mm": bow,
        "span_mm": float(span_mm),
        "thickness_mm": float(thickness_mm),
        "dT_through_k": float(dT_through_k),
        "cte_per_k": float(cte_per_k),
    }


# --- mesh geometry: thickness axis + the 3-2-1 anchor nodes ------------------

_AXES = ("x", "y", "z")


def _bbox(nodes: dict) -> tuple:
    xs = [p[0] for p in nodes.values()]
    ys = [p[1] for p in nodes.values()]
    zs = [p[2] for p in nodes.values()]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def thickness_axis(nodes: dict) -> int:
    """Index (0=x, 1=y, 2=z) of the part's **thinnest** bounding-box axis — the
    out-of-plane / through-thickness direction for a plate-like moulding."""
    xmin, xmax, ymin, ymax, zmin, zmax = _bbox(nodes)
    extents = (xmax - xmin, ymax - ymin, zmax - zmin)
    return min(range(3), key=lambda k: extents[k])


def pick_321_nodes(nodes: dict, *, axis: int | None = None) -> dict:
    """Choose three non-collinear nodes for a statically-determinate **3-2-1**
    constraint that removes all six rigid-body modes while leaving the part free to
    expand in-plane and warp out-of-plane.

    ``axis`` is the through-thickness (out-of-plane) axis (default: the thinnest
    bbox axis). With in-plane axes ``i, j`` and thickness ``k``:

    * **A** (in-plane origin corner): pin ``U_i = U_j = U_k = 0``
    * **B** (offset along ``i``): pin ``U_j = U_k = 0`` (free to slide along ``i``)
    * **C** (offset along ``j``): pin ``U_k = 0``

    Returns ``{a, b, c, axis, in_plane:(i,j), span_mm, thickness_mm}`` where a/b/c are
    node ids and ``span_mm`` is the larger in-plane extent (the warp lever arm)."""
    if not nodes:
        raise ValueError("no nodes")
    k = thickness_axis(nodes) if axis is None else int(axis)
    i, j = [a for a in range(3) if a != k]
    bb = _bbox(nodes)
    lo = (bb[0], bb[2], bb[4])
    hi = (bb[1], bb[3], bb[5])
    mid_k = 0.5 * (lo[k] + hi[k])

    def score(nid, want_i, want_j):
        p = nodes[nid]
        # prefer the requested in-plane corner, on the part's mid-plane in k (so the
        # anchors sit on the neutral axis and don't themselves load the bending)
        ti = lo[i] if want_i == "lo" else hi[i]
        tj = lo[j] if want_j == "lo" else hi[j]
        return (p[i] - ti) ** 2 + (p[j] - tj) ** 2 + (p[k] - mid_k) ** 2

    a = min(nodes, key=lambda n: score(n, "lo", "lo"))
    b = min(nodes, key=lambda n: score(n, "hi", "lo"))
    c = min(nodes, key=lambda n: score(n, "lo", "hi"))
    span = max(hi[i] - lo[i], hi[j] - lo[j])
    return {
        "a": a, "b": b, "c": c, "axis": k, "in_plane": (i, j),
        "span_mm": span, "thickness_mm": hi[k] - lo[k],
    }


def _boundary_cards(anchors: dict) -> list:
    """The three ``*BOUNDARY`` rows (1-based dof) for the 3-2-1 anchors."""
    k = anchors["axis"]
    i, j = anchors["in_plane"]
    di, dj, dk = i + 1, j + 1, k + 1   # CalculiX dofs are 1-based (1=x,2=y,3=z)
    return [
        f"{anchors['a']}, {min(di,dj,dk)}, {max(di,dj,dk)}, 0.0",  # A: all three
        f"{anchors['b']}, {dj}, {dj}, 0.0",                        # B: in-plane j
        f"{anchors['b']}, {dk}, {dk}, 0.0",                        # B: thickness k
        f"{anchors['c']}, {dk}, {dk}, 0.0",                        # C: thickness k
    ]


# --- through-thickness temperature field -------------------------------------

def linear_through_thickness_temps(
    nodes: dict, *, axis: int, dT_through_k: float, ref_temp_c: float = 0.0,
) -> dict:
    """A linear through-thickness nodal temperature field that reproduces a
    ``dT_through_k`` differential (``T at min-thickness-face − T at max-face``),
    centred on ``ref_temp_c``.

    ``T(node) = ref + dT·(0.5 − (k − k_min)/thickness)`` — the min-``k`` face sits at
    ``ref + dT/2`` and the max-``k`` face at ``ref − dT/2``. Pure antisymmetric about
    the mid-plane, so it is exactly the bending eigenstrain the analytic twin assumes
    (and the warp is invariant to ``ref_temp_c``). Returns ``{node_id: T_c}``."""
    bb = _bbox(nodes)
    k_min = (bb[0], bb[2], bb[4])[axis]
    k_max = (bb[1], bb[3], bb[5])[axis]
    h = k_max - k_min
    if h <= 0:
        raise ValueError("degenerate thickness")
    return {n: ref_temp_c + dT_through_k * (0.5 - (p[axis] - k_min) / h)
            for n, p in nodes.items()}


# --- the CalculiX thermo-elastic deck ----------------------------------------

def warpage_inp_text(
    *, mesh_include: str, node_temps: dict, anchors: dict,
    youngs_mpa: float, poisson: float, cte_per_k: float, ref_temp_c: float,
    elset: str = "Evolumes", nset: str = "Nall",
) -> str:
    """The CalculiX ``.inp`` deck for a one-shot thermo-elastic free-distortion solve.

    ``*INCLUDE`` pulls in the mesh (``*Node`` + ``*Element,TYPE=C3D10`` from FreeCAD's
    ABAQUS export). Linear ``*ELASTIC`` (``youngs_mpa``, ``poisson``) + ``*EXPANSION``
    with ``ZERO=ref_temp_c`` so the thermal strain is ``α·(T − ref)``; the stress-free
    reference is also the ``*INITIAL CONDITIONS`` temperature, and the per-node
    ``*TEMPERATURE`` field (``node_temps``, °C) is ramped on in a single ``*STATIC``
    step. The 3-2-1 ``*BOUNDARY`` rows (from ``anchors``) remove rigid-body motion;
    ``*NODE FILE U`` writes displacements to the ``.frd``.

    No ``#calc``-style dynamic code, no functionObjects — a flat, fully-resolved deck
    (same discipline as the molding fill case). Returns the deck text."""
    if youngs_mpa <= 0 or not (0.0 <= poisson < 0.5):
        raise ValueError("youngs_mpa must be > 0 and 0 <= poisson < 0.5")
    lines = [
        f"*INCLUDE, INPUT={mesh_include}",
        "*MATERIAL, NAME=resin",
        "*ELASTIC",
        f"{youngs_mpa:.6g}, {poisson:.6g}",
        f"*EXPANSION, ZERO={ref_temp_c:.6g}",
        f"{cte_per_k:.6g}",
        f"*SOLID SECTION, ELSET={elset}, MATERIAL=resin",
        "*INITIAL CONDITIONS, TYPE=TEMPERATURE",
        f"{nset}, {ref_temp_c:.6g}",
        "*STEP",
        "*STATIC",
        "*BOUNDARY",
        *_boundary_cards(anchors),
        "*TEMPERATURE",
    ]
    for n in sorted(node_temps):
        lines.append(f"{n}, {node_temps[n]:.6f}")
    lines += ["*NODE FILE", "U", "*EL FILE", "S", "*END STEP"]
    return "\n".join(lines) + "\n"


def write_warpage_case(
    case_dir: str, *, nodes: dict, mesh_filename: str = "mesh.inp",
    youngs_mpa: float, poisson: float, cte_per_k: float,
    dT_through_k: float | None = None, node_temps: dict | None = None,
    ref_temp_c: float | None = None, axis: int | None = None,
    job_name: str = "case",
) -> dict:
    """Write the ccx warpage deck (``<job_name>.inp``) into ``case_dir``.

    The caller has already written the mesh (``mesh_filename``) and supplies the node
    coordinates (``nodes`` = ``{id: (x, y, z)}`` in mm). Provide **either**
    ``dT_through_k`` (a linear through-thickness differential — the asymmetric frozen-in
    cooling signal) **or** an explicit ``node_temps`` field (°C, e.g. mapped from the
    Part-A cooling solve). ``ref_temp_c`` defaults to the field mean (warp is invariant
    to it; set the solidification temperature for honest residual stress). ``axis``
    overrides the auto-detected through-thickness axis.

    Returns ``{case_dir, inp, job_name, anchors, axis, span_mm, thickness_mm,
    ref_temp_c, n_temp_nodes, argv}`` where ``argv`` runs ccx (``ccx -i <job>``)."""
    if (dT_through_k is None) == (node_temps is None):
        raise ValueError("supply exactly one of dT_through_k or node_temps")
    anchors = pick_321_nodes(nodes, axis=axis)
    ax = anchors["axis"]
    if node_temps is None:
        ref = 0.0 if ref_temp_c is None else ref_temp_c
        node_temps = linear_through_thickness_temps(
            nodes, axis=ax, dT_through_k=dT_through_k, ref_temp_c=ref)
    if ref_temp_c is None:
        vals = list(node_temps.values())
        ref_temp_c = sum(vals) / len(vals) if vals else 0.0
    text = warpage_inp_text(
        mesh_include=mesh_filename, node_temps=node_temps, anchors=anchors,
        youngs_mpa=youngs_mpa, poisson=poisson, cte_per_k=cte_per_k,
        ref_temp_c=ref_temp_c)
    os.makedirs(case_dir, exist_ok=True)
    inp = f"{job_name}.inp"
    with open(os.path.join(case_dir, inp), "w") as f:
        f.write(text)
    return {
        "case_dir": case_dir, "inp": inp, "job_name": job_name,
        "anchors": {k: anchors[k] for k in ("a", "b", "c")},
        "axis": _AXES[ax], "axis_index": ax,
        "span_mm": round(anchors["span_mm"], 6),
        "thickness_mm": round(anchors["thickness_mm"], 6),
        "ref_temp_c": round(ref_temp_c, 6),
        "n_temp_nodes": len(node_temps),
        "argv": ["ccx", "-i", job_name],
    }


# --- .frd displacement parser ------------------------------------------------

def parse_warp_frd(frd_path: str, *, axis_index: int = 2) -> dict | None:
    """Read the displacement (``DISP``) block of a CalculiX ``.frd`` and reduce it to
    the warp result.

    CalculiX writes each nodal-result block in fixed-width columns: a ``-4`` header
    line names the block (``DISP``), ``-1`` rows carry ``node`` then three 12-char
    float fields (Ux, Uy, Uz), and a ``-3`` line ends the block. ``axis_index`` is the
    out-of-plane (thickness) axis whose displacement IS the warp.

    Returns ``{max_warp_mm, max_disp_mm, warp_vector, n_nodes}`` — ``max_warp_mm`` is
    the peak |U along the thickness axis| (the out-of-plane bow, relative to the pinned
    3-2-1 corner), ``max_disp_mm`` the peak total displacement magnitude. None if the
    file is absent or has no DISP block (the solve failed)."""
    if not os.path.isfile(frd_path):
        return None
    in_disp = False
    in_block = False
    max_warp = 0.0
    max_mag = 0.0
    warp_vec = None
    n = 0
    with open(frd_path) as f:
        for ln in f:
            if not in_disp:
                # a -4 header naming the DISP block opens it
                if ln.startswith(" -4") and "DISP" in ln:
                    in_disp = True
                    in_block = True
                continue
            if ln.startswith(" -3"):
                break
            if in_block and ln.startswith(" -1"):
                try:
                    ux = float(ln[13:25]); uy = float(ln[25:37]); uz = float(ln[37:49])
                except ValueError:
                    continue
                comps = (ux, uy, uz)
                w = abs(comps[axis_index])
                mag = (ux * ux + uy * uy + uz * uz) ** 0.5
                if w > max_warp:
                    max_warp = w
                    warp_vec = [ux, uy, uz]
                max_mag = max(max_mag, mag)
                n += 1
    if n == 0:
        return None
    return {
        "max_warp_mm": round(max_warp, 6),
        "max_disp_mm": round(max_mag, 6),
        "warp_vector": [round(v, 6) for v in warp_vec] if warp_vec else None,
        "n_nodes": n,
    }


# --- the warpage gate --------------------------------------------------------

def warpage_gate(
    parsed: dict, *, span_mm: float, flatness_tol_mm: float | None = None,
    flatness_tol_frac: float = 0.002, analytic_bow_mm: float | None = None,
    faithfulness_tol: float = 1.5, band_pct: float = 40.0,
) -> dict:
    """Turn a :func:`parse_warp_frd` result into the house verdict shape.

    **pass/fail = flatness**: the part passes when the out-of-plane warp
    ``max_warp_mm`` is within the flatness tolerance — ``flatness_tol_mm`` if given,
    else ``flatness_tol_frac · span_mm`` (default 0.2 % of the span, a typical plastic-
    part flatness call-out). ``score`` is ``1 − warp/tol`` clamped to ``[0, 1]`` (1.0 =
    dead flat).

    When ``analytic_bow_mm`` (the :func:`free_plate_thermal_bow` twin) is supplied the
    solved warp is also checked for **faithfulness** — a solved/analytic ratio outside
    ``[1/faithfulness_tol, faithfulness_tol]`` raises a warning (it does NOT fail the
    gate; the twin is a thin-plate idealisation, so a few-percent-to-~50 % spread is
    expected, but an order-of-magnitude miss means the mesh or field is wrong).

    ``fidelity="solve"`` with a conservative ``band_pct`` reflecting the one-way
    linear-elastic loose coupling (no viscoelastic relaxation / flow anisotropy /
    packing residual). Returns ``{pass, score, fidelity, band_pct, max_warp_mm,
    flatness_tol_mm, warp_per_span, max_disp_mm, warp_faithful, analytic_bow_mm,
    warnings}``."""
    warnings: list[str] = []
    warp = parsed.get("max_warp_mm")
    tol = flatness_tol_mm if flatness_tol_mm is not None else flatness_tol_frac * span_mm
    passed = warp is not None and tol > 0 and warp <= tol
    if warp is not None and tol > 0 and warp > tol:
        warnings.append(
            f"warp {warp:.3f} mm exceeds the flatness tolerance {tol:.3f} mm over a "
            f"{span_mm:.0f} mm span ({warp/span_mm*100:.2f} % of span) — expect the "
            "part to bow out of flat; balance the cooling (equalise mould-half "
            "temperatures), even out the wall thickness, or relax the flatness call-out")

    faithful = None
    if analytic_bow_mm is not None and warp is not None and abs(analytic_bow_mm) > 1e-9:
        ratio = warp / abs(analytic_bow_mm)
        faithful = (1.0 / faithfulness_tol) <= ratio <= faithfulness_tol
        if not faithful:
            warnings.append(
                f"solved warp {warp:.3f} mm is {ratio:.2f}x the thin-plate analytic "
                f"bow {abs(analytic_bow_mm):.3f} mm — beyond the expected idealisation "
                "spread; check the mesh resolution and the imposed temperature field")

    score = 0.0
    if warp is not None and tol > 0:
        score = max(0.0, min(1.0, 1.0 - warp / tol))
    return {
        "pass": bool(passed),
        "score": round(score, 4),
        "fidelity": "solve",
        "band_pct": band_pct,
        "max_warp_mm": warp,
        "flatness_tol_mm": round(tol, 6),
        "warp_per_span": round(warp / span_mm, 6) if (warp is not None and span_mm) else None,
        "max_disp_mm": parsed.get("max_disp_mm"),
        "warp_faithful": faithful,
        "analytic_bow_mm": (round(analytic_bow_mm, 6)
                            if analytic_bow_mm is not None else None),
        "warnings": warnings,
    }

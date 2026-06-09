"""Topology optimization — in-house SIMP, no external solver.

Pure-Python + NumPy (a base dependency), FreeCAD-free. The structural member of P2
family 5/§2 that the kickoff (docs/SIMULATION_P2_KICKOFF.md) earmarked for a pip
wheel (`solidspy`/`topopt`/FEniCS) — but the classic density-based SIMP optimizer
(Sigmund's "99-line" / Andreassen's "88-line") is a self-contained NumPy routine, so
this ships with no new dependency (the FEniCS/solidspy upgrade stays an option, and
the `topology` solver still registers for it).

Method — minimum-compliance SIMP on a regular 2-D grid of bilinear quad elements:
each element carries a density x∈[0,1]; its stiffness is penalized E = Emin +
xᵖ·(E0−Emin) (p≈3) so intermediate densities are uneconomical and the design drives
toward black/white. Each iteration solves K(x)·U = F, takes the compliance c = UᵀKU
and its sensitivity, applies a cone (radius rmin) sensitivity filter to kill
checkerboarding, and updates x by the Optimality-Criteria rule under a hard volume
constraint Σx = volfrac·N. It converges to a truss-like load path whose volume
fraction equals ``keep_fraction`` by construction.

The result is a density field (geometry) — the one P2 family that closes the loop
back into the modeller; turning it into a FreeCAD solid (threshold → voxels →
boolean) is the worker-side follow-on. See ``docs/SIMULATION_EXAMPLES.md`` §5.
"""
from __future__ import annotations

import math

import numpy as np


def _element_stiffness(nu: float = 0.3) -> np.ndarray:
    """8×8 stiffness of a unit bilinear quad (plane stress, E=1) — the standard
    top88 element matrix."""
    k = np.array([
        0.5 - nu / 6.0, 0.125 + nu / 8.0, -0.25 - nu / 12.0, -0.125 + 3.0 * nu / 8.0,
        -0.25 + nu / 12.0, -0.125 - nu / 8.0, nu / 6.0, 0.125 - 3.0 * nu / 8.0,
    ])
    KE = (1.0 / (1.0 - nu * nu)) * np.array([
        [k[0], k[1], k[2], k[3], k[4], k[5], k[6], k[7]],
        [k[1], k[0], k[7], k[6], k[5], k[4], k[3], k[2]],
        [k[2], k[7], k[0], k[5], k[6], k[3], k[4], k[1]],
        [k[3], k[6], k[5], k[0], k[7], k[2], k[1], k[4]],
        [k[4], k[5], k[6], k[7], k[0], k[1], k[2], k[3]],
        [k[5], k[4], k[3], k[2], k[1], k[0], k[7], k[6]],
        [k[6], k[3], k[4], k[1], k[2], k[7], k[0], k[5]],
        [k[7], k[2], k[1], k[4], k[3], k[6], k[5], k[0]],
    ])
    return KE


def _edof_matrix(nelx: int, nely: int) -> np.ndarray:
    """Element→DOF map (nelx·nely × 8); node n has dofs (2n, 2n+1). Column-major
    node numbering, matching _element_stiffness."""
    edof = np.zeros((nelx * nely, 8), dtype=int)
    for elx in range(nelx):
        for ely in range(nely):
            el = ely + elx * nely
            n1 = (nely + 1) * elx + ely
            n2 = (nely + 1) * (elx + 1) + ely
            edof[el] = [2 * n1 + 2, 2 * n1 + 3, 2 * n2 + 2, 2 * n2 + 3,
                        2 * n2, 2 * n2 + 1, 2 * n1, 2 * n1 + 1]
    return edof


def _filter_weights(nelx: int, nely: int, rmin: float):
    """Cone (linear-decay, radius rmin) sensitivity-filter weight matrix H and its
    row sums Hs. Dense — fine for the modest grids this runs at."""
    n = nelx * nely
    H = np.zeros((n, n))
    r = int(math.ceil(rmin)) - 1
    for i in range(nelx):
        for j in range(nely):
            e1 = j + i * nely
            for k in range(max(i - r, 0), min(i + r + 1, nelx)):
                for l in range(max(j - r, 0), min(j + r + 1, nely)):
                    fac = rmin - math.hypot(i - k, j - l)
                    if fac > 0:
                        H[e1, l + k * nely] = fac
    return H, H.sum(axis=1)


def simp_topology_2d(
    nelx: int = 60,
    nely: int = 20,
    keep_fraction: float = 0.4,
    penal: float = 3.0,
    rmin: float = 1.5,
    max_iter: int = 60,
    tol: float = 0.01,
    load=None,
    fixed_dofs=None,
) -> dict:
    """Minimum-compliance SIMP on a 2-D cantilever grid.

    Default boundary conditions: the whole left edge is clamped and a unit downward
    load is applied at the mid-height of the right edge (override with ``fixed_dofs``
    / ``load`` = (dof_index, value)). Optimizes element densities to minimize
    compliance subject to Σx = ``keep_fraction``·N (the OC update enforces the volume
    exactly each step), stopping at ``max_iter`` or when the max density change < ``tol``.

    Returns {nelx, nely, keep_fraction, iterations, converged, compliance,
    compliance_initial, mass_fraction (mean density — must equal keep_fraction),
    change, density (nely×nelx row-major grid, 0=void..1=solid),
    gray_fraction (share of cells in 0.1..0.9 — discreteness check)}. Raises ValueError
    on degenerate sizes or an out-of-range keep_fraction."""
    if nelx < 2 or nely < 2:
        raise ValueError("nelx and nely must each be >= 2")
    if not 0.0 < keep_fraction < 1.0:
        raise ValueError("keep_fraction must be in (0, 1)")

    E0, Emin, nu = 1.0, 1e-9, 0.3
    ndof = 2 * (nelx + 1) * (nely + 1)
    n = nelx * nely
    KE = _element_stiffness(nu)
    edof = _edof_matrix(nelx, nely)
    H, Hs = _filter_weights(nelx, nely, rmin)

    # Vectorized scatter indices for dense assembly: iK[el,a,b]=edof[el,a],
    # jK[el,a,b]=edof[el,b], so np.add.at(K,(iK,jK), Evec⊗KE) == the per-element
    # K[edof][:,edof] += Evec*KE, but without a Python element loop.
    iK = np.repeat(edof, 8, axis=1).reshape(-1)
    jK = np.tile(edof, (1, 8)).reshape(-1)

    # boundary conditions
    if fixed_dofs is None:
        fixed = np.arange(2 * (nely + 1))            # entire left edge clamped
    else:
        fixed = np.asarray(fixed_dofs, dtype=int)
    free = np.setdiff1d(np.arange(ndof), fixed)
    F = np.zeros(ndof)
    if load is None:
        node = (nely + 1) * nelx + nely // 2          # mid-height, right edge
        F[2 * node + 1] = -1.0                         # downward
    else:
        F[int(load[0])] = float(load[1])

    x = np.full(n, keep_fraction)
    compliance_initial = None
    change = 1.0
    it = 0
    c = 0.0
    while it < max_iter and change > tol:
        it += 1
        # assemble global stiffness (dense; small grids) via vectorized scatter
        Evec = Emin + x ** penal * (E0 - Emin)
        K = np.zeros((ndof, ndof))
        sK = (Evec[:, None, None] * KE[None, :, :]).reshape(-1)
        np.add.at(K, (iK, jK), sK)
        # solve on free dofs
        U = np.zeros(ndof)
        U[free] = np.linalg.solve(K[np.ix_(free, free)], F[free])
        # compliance + sensitivity
        Ue = U[edof]                                  # (n, 8)
        ce = np.einsum("ij,jk,ik->i", Ue, KE, Ue)     # uᵀ KE u per element
        c = float((Evec * ce).sum())
        if compliance_initial is None:
            compliance_initial = c
        dc = -penal * x ** (penal - 1) * (E0 - Emin) * ce
        # sensitivity filter
        dc = (H @ (x * dc)) / (Hs * np.maximum(1e-3, x))
        # Optimality-Criteria update (bisection on the volume Lagrange multiplier)
        l1, l2, move = 0.0, 1e9, 0.2
        xnew = x
        while (l2 - l1) / (l1 + l2 + 1e-30) > 1e-4:
            lmid = 0.5 * (l1 + l2)
            xnew = np.maximum(0.0, np.maximum(
                x - move, np.minimum(1.0, np.minimum(
                    x + move, x * np.sqrt(-dc / lmid)))))
            if xnew.sum() - keep_fraction * n > 0:
                l1 = lmid
            else:
                l2 = lmid
        change = float(np.max(np.abs(xnew - x)))
        x = xnew

    grid = x.reshape(nelx, nely).T                    # -> (nely, nelx) row-major
    gray = float(np.mean((x > 0.1) & (x < 0.9)))
    return {
        "nelx": nelx, "nely": nely, "keep_fraction": keep_fraction,
        "iterations": it, "converged": change <= tol,
        "compliance": round(c, 6),
        "compliance_initial": round(compliance_initial, 6) if compliance_initial else None,
        "mass_fraction": round(float(x.mean()), 6),
        "change": round(change, 6),
        "gray_fraction": round(gray, 4),
        "density": [[round(v, 4) for v in row] for row in grid.tolist()],
    }


def density_to_rects(density, threshold: float = 0.5) -> dict:
    """Decompose a thresholded density field into axis-aligned solid rectangles.

    The modeller-side half of the topology follow-on: ``simp_topology_2d`` returns a
    ``density`` grid (geometry, not a solid); to hand it to the FreeCAD kernel as a
    real body each above-threshold cell must become a box. Tiling one box *per cell*
    would fuse thousands of coincident faces; instead this run-length-merges each row
    into maximal horizontal spans of solid cells, so the worker fuses O(runs) boxes
    rather than O(cells). Vertically-stacked runs still share faces, which a single
    ``removeSplitter`` cleans up after the fuse.

    ``density`` is the nely×nelx row-major grid from ``simp_topology_2d`` (values
    0..1, ``density[j][i]`` = column i of row j). A cell is solid when its value is
    ``>= threshold``. Returns ``{nelx, nely, threshold, solid_cells, rects}`` where
    each rect is ``(col0, row, width)`` — row ``row`` is solid across columns
    ``col0 .. col0+width-1``. ``solid_cells`` (= Σ width) over ``nelx*nely`` is the
    realized mass fraction at this threshold. Raises ValueError on an empty or ragged
    grid."""
    if not density or not density[0]:
        raise ValueError("density must be a non-empty 2-D grid")
    nely = len(density)
    nelx = len(density[0])
    rects = []
    for j, row in enumerate(density):
        if len(row) != nelx:
            raise ValueError(
                f"density rows must all be length {nelx}; row {j} has {len(row)}")
        i = 0
        while i < nelx:
            if row[i] >= threshold:
                i0 = i
                while i < nelx and row[i] >= threshold:
                    i += 1
                rects.append((i0, j, i - i0))
            else:
                i += 1
    return {
        "nelx": nelx, "nely": nely, "threshold": threshold,
        "solid_cells": sum(w for _, _, w in rects),
        "rects": rects,
    }

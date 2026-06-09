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


# --- 3-D SIMP (P3 M5) ----------------------------------------------------------
# The 2-D routine promoted to 8-node trilinear hexahedra (the top3d formulation),
# same SIMP penalization + cone sensitivity filter + Optimality-Criteria update.
# Still NumPy-only at heart so it runs inside freecadcmd's bundled interpreter; the
# linear solve picks the best backend available: scipy.sparse (venv), dense NumPy
# (small systems), or a matrix-free Jacobi-preconditioned CG (no scipy, big grid).


def _hex8_stiffness(nu: float = 0.3) -> np.ndarray:
    """24×24 stiffness of a unit trilinear hexahedron (E=1, isotropic ``nu``),
    integrated with 2×2×2 Gauss points — exact for the trilinear element (the
    integrand is at most quadratic per direction). Node order: the z=0 quad
    counter-clockwise from the origin (000, 100, 110, 010), then the z=1 quad in
    the same order; dofs (ux, uy, uz) per node."""
    c = 1.0 / ((1.0 + nu) * (1.0 - 2.0 * nu))
    C = np.zeros((6, 6))
    C[:3, :3] = nu * c
    np.fill_diagonal(C[:3, :3], (1.0 - nu) * c)
    C[3:, 3:] = np.eye(3) * ((1.0 - 2.0 * nu) / 2.0 * c)
    signs = np.array([
        [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
        [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)
    g = 1.0 / math.sqrt(3.0)
    KE = np.zeros((24, 24))
    for gx in (-g, g):
        for gy in (-g, g):
            for gz in (-g, g):
                # dN/dx = 2·dN/dξ on the unit cube (J = I/2, detJ = 1/8)
                B = np.zeros((6, 24))
                for a in range(8):
                    sx, sy, sz = signs[a]
                    bx = 0.25 * sx * (1 + sy * gy) * (1 + sz * gz)
                    by = 0.25 * sy * (1 + sx * gx) * (1 + sz * gz)
                    bz = 0.25 * sz * (1 + sx * gx) * (1 + sy * gy)
                    B[0, 3 * a] = bx
                    B[1, 3 * a + 1] = by
                    B[2, 3 * a + 2] = bz
                    B[3, 3 * a] = by
                    B[3, 3 * a + 1] = bx
                    B[4, 3 * a + 1] = bz
                    B[4, 3 * a + 2] = by
                    B[5, 3 * a] = bz
                    B[5, 3 * a + 2] = bx
                KE += (B.T @ C @ B) * 0.125
    return KE


def _edof_matrix_3d(nelx: int, nely: int, nelz: int) -> np.ndarray:
    """Element→DOF map (nelx·nely·nelz × 24). Node (i,j,k) has id
    k·(nelx+1)(nely+1) + i·(nely+1) + j (the 2-D column-major numbering per
    z-layer) and dofs (3n, 3n+1, 3n+2); element (i,j,k) index is
    k·nelx·nely + i·nely + j. Node order matches :func:`_hex8_stiffness`."""
    npx, npy = nelx + 1, nely + 1

    def nid(i, j, k):
        return k * npx * npy + i * npy + j

    edof = np.zeros((nelx * nely * nelz, 24), dtype=int)
    for ek in range(nelz):
        for ei in range(nelx):
            for ej in range(nely):
                el = ek * nelx * nely + ei * nely + ej
                ns = (nid(ei, ej, ek), nid(ei + 1, ej, ek),
                      nid(ei + 1, ej + 1, ek), nid(ei, ej + 1, ek),
                      nid(ei, ej, ek + 1), nid(ei + 1, ej, ek + 1),
                      nid(ei + 1, ej + 1, ek + 1), nid(ei, ej + 1, ek + 1))
                edof[el] = [3 * n + d for n in ns for d in range(3)]
    return edof


def _filter_offsets_3d(rmin: float):
    """Cone-filter neighbor offsets [(dz, dx, dy, weight)] with weight
    rmin − distance > 0 — the 3-D analogue of the dense H of `_filter_weights`,
    applied by array shifts instead of an n×n matrix."""
    r = int(math.ceil(rmin)) - 1
    offs = []
    for dz in range(-r, r + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                fac = rmin - math.sqrt(dx * dx + dy * dy + dz * dz)
                if fac > 0:
                    offs.append((dz, dx, dy, fac))
    return offs


def _shift_slices(shape, off):
    """(src, dst) slice tuples that add a field shifted by ``off`` onto itself,
    truncated at the boundaries (the cone filter's boundary handling)."""
    src, dst = [], []
    for d, s in zip(off, shape):
        src.append(slice(max(0, -d), s - max(0, d)))
        dst.append(slice(max(0, d), s - max(0, -d)))
    return tuple(src), tuple(dst)


_DENSE_DOF_CAP = 4500  # biggest system the dense no-scipy fallback will assemble


def _solve_elastic(Evec, KE, edof, ndof, free, F):
    """Solve K(x)·U = F on the free dofs. Backend order: scipy.sparse (fast,
    present in the venv), dense NumPy (small systems — the freecadcmd worker has
    no scipy), matrix-free Jacobi-PCG (no scipy AND too big for dense). Returns
    (U, backend_name)."""
    U = np.zeros(ndof)
    try:
        import scipy.sparse as sp
        import scipy.sparse.linalg as spla
    except ImportError:
        sp = spla = None
    if sp is not None:
        iK = np.repeat(edof, edof.shape[1], axis=1).reshape(-1)
        jK = np.tile(edof, (1, edof.shape[1])).reshape(-1)
        sK = (Evec[:, None, None] * KE[None, :, :]).reshape(-1)
        K = sp.coo_matrix((sK, (iK, jK)), shape=(ndof, ndof)).tocsc()
        U[free] = spla.spsolve(K[free][:, free], F[free])
        return U, "scipy"
    if ndof <= _DENSE_DOF_CAP:                         # dense: ≤ ~160 MB
        iK = np.repeat(edof, edof.shape[1], axis=1).reshape(-1)
        jK = np.tile(edof, (1, edof.shape[1])).reshape(-1)
        sK = (Evec[:, None, None] * KE[None, :, :]).reshape(-1)
        K = np.zeros((ndof, ndof))
        np.add.at(K, (iK, jK), sK)
        U[free] = np.linalg.solve(K[np.ix_(free, free)], F[free])
        return U, "dense"
    # matrix-free Jacobi-preconditioned conjugate gradients (pure NumPy)
    fixed_mask = np.ones(ndof, dtype=bool)
    fixed_mask[free] = False
    diag = np.zeros(ndof)
    np.add.at(diag, edof.reshape(-1),
              (Evec[:, None] * np.diag(KE)[None, :]).reshape(-1))

    def matvec(u):
        y = np.zeros(ndof)
        np.add.at(y, edof.reshape(-1),
                  (Evec[:, None] * (u[edof] @ KE)).reshape(-1))
        y[fixed_mask] = 0.0
        return y

    b = F.copy()
    b[fixed_mask] = 0.0
    minv = np.where(diag > 0, 1.0 / np.maximum(diag, 1e-300), 0.0)
    minv[fixed_mask] = 0.0
    u = np.zeros(ndof)
    r = b.copy()
    z = minv * r
    p = z.copy()
    rz = float(r @ z)
    bnorm = math.sqrt(float(b @ b)) or 1.0
    for _ in range(min(20000, 30 * ndof)):
        Ap = matvec(p)
        alpha = rz / float(p @ Ap)
        u += alpha * p
        r -= alpha * Ap
        if math.sqrt(float(r @ r)) <= 1e-9 * bnorm:
            break
        z = minv * r
        rz_new = float(r @ z)
        p = z + (rz_new / rz) * p
        rz = rz_new
    return u, "cg"


def _cells_in_boxes(boxes, nelx, nely, nelz) -> np.ndarray:
    """Flat element indices covered by half-open index boxes
    [i0, i1, j0, j1, k0, k1] (i along x, j along y, k along z)."""
    mask = np.zeros((nelz, nelx, nely), dtype=bool)
    for box in boxes:
        if len(box) != 6:
            raise ValueError(
                "keep_out/keep_in boxes must be [i0, i1, j0, j1, k0, k1]")
        i0, i1, j0, j1, k0, k1 = (int(v) for v in box)
        mask[max(k0, 0):min(k1, nelz),
             max(i0, 0):min(i1, nelx),
             max(j0, 0):min(j1, nely)] = True
    return np.flatnonzero(mask.reshape(-1))


def simp_topology_3d(
    nelx: int = 16,
    nely: int = 8,
    nelz: int = 4,
    keep_fraction: float = 0.3,
    penal: float = 3.0,
    rmin: float = 1.5,
    max_iter: int = 40,
    tol: float = 0.01,
    loads=None,
    fixed_nodes=None,
    keep_out=None,
    keep_in=None,
) -> dict:
    """Minimum-compliance SIMP on a 3-D grid of trilinear hexahedra (top3d).

    Default boundary conditions are the 3-D cantilever: the whole x=0 face is
    clamped and a unit −y load is applied at the right-face centre node
    (nelx, nely//2, nelz//2). Override with:

    * ``loads`` — list of [i, j, k, axis, value] point loads at node (i, j, k)
      along axis 'x'|'y'|'z' (or 0|1|2).
    * ``fixed_nodes`` — list of [i, j, k] nodes clamped in all three dofs
      (default: every node of the x=0 face).
    * ``keep_out`` / ``keep_in`` — lists of half-open element-index boxes
      [i0, i1, j0, j1, k0, k1] forced void / forced solid (passive cells, the
      top88 extension) — the hook the kickoff's keep-out gate drives.

    The volume constraint Σx = keep_fraction·N is over the whole domain
    (keep-in cells count toward it; ValueError if keep_in alone exceeds it).
    Returns {nelx, nely, nelz, keep_fraction, iterations, converged, compliance,
    compliance_initial, mass_fraction, change, gray_fraction, solver,
    density (nelz×nely×nelx nested list — density[k][j][i], j=0 at the BOTTOM,
    no display flip unlike the 2-D grid)}. Raises ValueError on degenerate
    sizes, an out-of-range keep_fraction, or an infeasible keep_in."""
    if nelx < 2 or nely < 2 or nelz < 1:
        raise ValueError("need nelx >= 2, nely >= 2, nelz >= 1")
    if not 0.0 < keep_fraction < 1.0:
        raise ValueError("keep_fraction must be in (0, 1)")

    E0, Emin, nu = 1.0, 1e-9, 0.3
    npx, npy, npz = nelx + 1, nely + 1, nelz + 1
    ndof = 3 * npx * npy * npz
    n = nelx * nely * nelz
    KE = _hex8_stiffness(nu)
    edof = _edof_matrix_3d(nelx, nely, nelz)
    offs = _filter_offsets_3d(rmin)
    shape3 = (nelz, nelx, nely)

    def nid(i, j, k):
        if not (0 <= i <= nelx and 0 <= j <= nely and 0 <= k <= nelz):
            raise ValueError(f"node ({i},{j},{k}) outside the grid")
        return k * npx * npy + i * npy + j

    # boundary conditions
    if fixed_nodes is None:
        fixed = np.array([3 * nid(0, j, k) + d
                          for k in range(npz) for j in range(npy)
                          for d in range(3)], dtype=int)
    else:
        fixed = np.array(sorted({3 * nid(int(i), int(j), int(k)) + d
                                 for i, j, k in fixed_nodes for d in range(3)}),
                         dtype=int)
    free = np.setdiff1d(np.arange(ndof), fixed)
    F = np.zeros(ndof)
    axis_map = {"x": 0, "y": 1, "z": 2, 0: 0, 1: 1, 2: 2}
    if loads is None:
        loads = [[nelx, nely // 2, nelz // 2, "y", -1.0]]
    for i, j, k, axis, value in loads:
        if axis not in axis_map:
            raise ValueError(f"load axis must be x|y|z or 0|1|2, got {axis!r}")
        F[3 * nid(int(i), int(j), int(k)) + axis_map[axis]] += float(value)

    passive = _cells_in_boxes(keep_out, nelx, nely, nelz) if keep_out else None
    active = _cells_in_boxes(keep_in, nelx, nely, nelz) if keep_in else None
    if active is not None and len(active) > keep_fraction * n:
        raise ValueError(
            f"keep_in covers {len(active)} cells > keep_fraction·N = "
            f"{keep_fraction * n:.1f}; raise keep_fraction or shrink keep_in")

    def clamp_passive(arr):
        if passive is not None:
            arr[passive] = 0.0
        if active is not None:
            arr[active] = 1.0
        return arr

    x = clamp_passive(np.full(n, keep_fraction))
    compliance_initial = None
    backend = None
    change, it, c = 1.0, 0, 0.0
    while it < max_iter and change > tol:
        it += 1
        Evec = Emin + x ** penal * (E0 - Emin)
        U, backend = _solve_elastic(Evec, KE, edof, ndof, free, F)
        Ue = U[edof]                                   # (n, 24)
        ce = np.einsum("ij,jk,ik->i", Ue, KE, Ue)
        c = float((Evec * ce).sum())
        if compliance_initial is None:
            compliance_initial = c
        dc = -penal * x ** (penal - 1) * (E0 - Emin) * ce
        # cone sensitivity filter by boundary-truncated array shifts
        x3 = x.reshape(shape3)
        xd = (x * dc).reshape(shape3)
        num = np.zeros(shape3)
        den = np.zeros(shape3)
        for dz, dx_, dy, fac in offs:
            src, dst = _shift_slices(shape3, (dz, dx_, dy))
            num[dst] += fac * xd[src]
            den[dst] += fac
        dc = (num / (den * np.maximum(1e-3, x3))).reshape(-1)
        # Optimality-Criteria update (bisection on the volume multiplier)
        l1, l2, move = 0.0, 1e9, 0.2
        xnew = x
        while (l2 - l1) / (l1 + l2 + 1e-30) > 1e-4:
            lmid = 0.5 * (l1 + l2)
            xnew = np.maximum(0.0, np.maximum(
                x - move, np.minimum(1.0, np.minimum(
                    x + move, x * np.sqrt(np.maximum(-dc, 0.0) / lmid)))))
            clamp_passive(xnew)
            if xnew.sum() - keep_fraction * n > 0:
                l1 = lmid
            else:
                l2 = lmid
        change = float(np.max(np.abs(xnew - x)))
        x = xnew

    grid = x.reshape(shape3).transpose(0, 2, 1)        # -> (nelz, nely, nelx)
    gray = float(np.mean((x > 0.1) & (x < 0.9)))
    return {
        "nelx": nelx, "nely": nely, "nelz": nelz,
        "keep_fraction": keep_fraction,
        "iterations": it, "converged": change <= tol,
        "compliance": round(c, 6),
        "compliance_initial": round(compliance_initial, 6) if compliance_initial else None,
        "mass_fraction": round(float(x.mean()), 6),
        "change": round(change, 6),
        "gray_fraction": round(gray, 4),
        "solver": backend,
        "density": [[[round(v, 4) for v in row] for row in layer]
                    for layer in grid.tolist()],
    }


def density_to_boxes(density, threshold: float = 0.5) -> dict:
    """Decompose a thresholded 3-D density field into maximal axis-aligned boxes.

    The 3-D analogue of :func:`density_to_rects` for ``simp_topology_3d``'s
    ``density[k][j][i]`` voxel field: greedy run-length merge along x within each
    (k, j) row, then identical x-runs on consecutive rows merge into y-rects, and
    identical rects on consecutive layers merge into z-boxes — so the worker
    fuses O(boxes), not O(voxels). A voxel is solid when value >= ``threshold``.

    Returns {nelx, nely, nelz, threshold, solid_cells, boxes} where each box is
    (i0, j0, k0, w, h, d): solid over x ∈ [i0, i0+w), y ∈ [j0, j0+h),
    z ∈ [k0, k0+d). ``solid_cells`` = Σ w·h·d. Raises ValueError on an empty or
    ragged grid."""
    if not density or not density[0] or not density[0][0]:
        raise ValueError("density must be a non-empty 3-D grid")
    nelz = len(density)
    nely = len(density[0])
    nelx = len(density[0][0])

    def row_runs(row):
        runs, i = [], 0
        while i < nelx:
            if row[i] >= threshold:
                i0 = i
                while i < nelx and row[i] >= threshold:
                    i += 1
                runs.append((i0, i - i0))
            else:
                i += 1
        return runs

    layer_rects = []                                   # per k: [(i0, j0, w, h)]
    for k, layer in enumerate(density):
        if len(layer) != nely:
            raise ValueError(
                f"density layers must all be {nely} rows; layer {k} has {len(layer)}")
        open_rects: dict = {}                          # (i0, w) -> j_start
        rects = []
        for j, row in enumerate(layer):
            if len(row) != nelx:
                raise ValueError(
                    f"density rows must all be length {nelx}; "
                    f"layer {k} row {j} has {len(row)}")
            runs = set(row_runs(row))
            for key in list(open_rects):
                if key not in runs:                    # x-run ended -> emit rect
                    i0, w = key
                    j0 = open_rects.pop(key)
                    rects.append((i0, j0, w, j - j0))
            for key in runs:                           # new x-runs open here
                open_rects.setdefault(key, j)
        for (i0, w), j0 in open_rects.items():
            rects.append((i0, j0, w, nely - j0))
        layer_rects.append(rects)

    boxes = []                                         # (i0, j0, k0, w, h, d)
    open_boxes: dict = {}                              # (i0, j0, w, h) -> k_start
    for k, rects in enumerate(layer_rects):
        rect_set = set(rects)
        for key in list(open_boxes):
            if key not in rect_set:                    # rect ended -> emit box
                i0, j0, w, h = key
                k0 = open_boxes.pop(key)
                boxes.append((i0, j0, k0, w, h, k - k0))
        for key in rect_set:
            open_boxes.setdefault(key, k)
    for (i0, j0, w, h), k0 in open_boxes.items():
        boxes.append((i0, j0, k0, w, h, nelz - k0))
    return {
        "nelx": nelx, "nely": nely, "nelz": nelz, "threshold": threshold,
        "solid_cells": sum(w * h * d for _, _, _, w, h, d in boxes),
        "boxes": sorted(boxes, key=lambda b: (b[2], b[1], b[0])),
    }

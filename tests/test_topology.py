"""Topology-optimization toys — oracles for ankusdrive.analysis.topology (SIMP).

Needs NumPy (a base dependency), so this runs under the venv lane like the render
tests. The grids are kept small because a dense FE solve runs every iteration (the
reason topology_optimize is async); the invariants checked are grid-independent:
the Optimality-Criteria update holds the volume fraction at keep_fraction exactly,
compliance falls from the uniform-density start, and more material yields a stiffer
(lower-compliance) result — the geometric gate from docs/SIMULATION_EXAMPLES.md §5.

Run:  .venv/bin/python3 tests/test_topology.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import topology as topo  # noqa: E402


def test_holds_volume_and_reduces_compliance():
    # small grid: a dense FE solve runs every iteration (why it's async); the
    # invariants are grid-independent.
    r = topo.simp_topology_2d(nelx=10, nely=5, keep_fraction=0.4, max_iter=12)
    # OC enforces the volume constraint exactly each iteration
    assert abs(r["mass_fraction"] - 0.4) < 1e-2, r["mass_fraction"]
    # optimization makes the structure much stiffer than uniform gray
    assert r["compliance"] < 0.7 * r["compliance_initial"], r
    # well-formed density field in [0,1], shape (nely, nelx)
    assert len(r["density"]) == 5 and len(r["density"][0]) == 10, "grid shape"
    flat = [v for row in r["density"] for v in row]
    assert all(0.0 <= v <= 1.0 for v in flat), "densities out of [0,1]"


def test_keep_fraction_is_respected():
    for kf in (0.3, 0.5):
        r = topo.simp_topology_2d(nelx=8, nely=4, keep_fraction=kf, max_iter=10)
        assert abs(r["mass_fraction"] - kf) < 1e-2, (kf, r["mass_fraction"])


def test_more_material_is_stiffer():
    lean = topo.simp_topology_2d(nelx=10, nely=5, keep_fraction=0.3, max_iter=12)
    rich = topo.simp_topology_2d(nelx=10, nely=5, keep_fraction=0.5, max_iter=12)
    # more material -> lower compliance (stiffer) for the same load/domain
    assert rich["compliance"] < lean["compliance"], (lean["compliance"], rich["compliance"])


def test_input_validation():
    for bad in (
        lambda: topo.simp_topology_2d(nelx=1, nely=8, keep_fraction=0.4),
        lambda: topo.simp_topology_2d(nelx=8, nely=8, keep_fraction=0.0),
        lambda: topo.simp_topology_2d(nelx=8, nely=8, keep_fraction=1.0),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- density -> solid reconstruction (the modeller-side follow-on) ------------

def test_density_to_rects_run_length_merges_rows():
    grid = [
        [0.9, 0.9, 0.1, 0.8],   # cols 0-1 solid (run w2), col 2 void, col 3 solid (w1)
        [0.2, 0.6, 0.6, 0.6],   # col 0 void, cols 1-3 solid (run w3)
    ]
    dec = topo.density_to_rects(grid, threshold=0.5)
    assert dec["nelx"] == 4 and dec["nely"] == 2, dec
    # rects emitted row-major, left-to-right; gaps split runs
    assert dec["rects"] == [(0, 0, 2), (3, 0, 1), (1, 1, 3)], dec["rects"]
    # solid_cells counts thresholded cells == realized mass fraction numerator
    assert dec["solid_cells"] == 6, dec["solid_cells"]


def test_density_to_rects_full_grid_is_one_run_per_row():
    grid = [[1.0, 1.0, 1.0]] * 4          # 4 rows x 3 cols, all solid
    dec = topo.density_to_rects(grid, threshold=0.5)
    assert dec["rects"] == [(0, j, 3) for j in range(4)], dec["rects"]
    assert dec["solid_cells"] == 12, dec["solid_cells"]


def test_density_to_rects_threshold_excludes_gray():
    grid = [[0.49, 0.5, 0.51]]            # >= threshold is solid (cols 1,2)
    dec = topo.density_to_rects(grid, threshold=0.5)
    assert dec["rects"] == [(1, 0, 2)], dec["rects"]
    # a higher threshold drops the 0.5 cell, leaving only col 2
    assert topo.density_to_rects(grid, threshold=0.51)["rects"] == [(2, 0, 1)]


def test_density_to_rects_validation():
    for bad in (
        lambda: topo.density_to_rects([], threshold=0.5),
        lambda: topo.density_to_rects([[]], threshold=0.5),
        lambda: topo.density_to_rects([[1.0, 1.0], [1.0]], threshold=0.5),  # ragged
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- 3-D SIMP (P3 M5) -----------------------------------------------------------

def test_hex8_element_is_sound():
    import numpy as np
    KE = topo._hex8_stiffness(0.3)
    # symmetric, positive semi-definite, exactly 6 rigid-body zero modes
    assert np.allclose(KE, KE.T), "KE not symmetric"
    w = np.linalg.eigvalsh(KE)
    assert np.sum(np.abs(w) < 1e-9) == 6, f"expected 6 rigid-body modes, eigs {w[:8]}"
    assert w[0] > -1e-9, "KE not positive semi-definite"


def test_3d_holds_volume_and_reduces_compliance():
    r = topo.simp_topology_3d(nelx=8, nely=4, nelz=2, keep_fraction=0.35, max_iter=8)
    assert abs(r["mass_fraction"] - 0.35) < 1e-2, r["mass_fraction"]
    assert r["compliance"] < 0.7 * r["compliance_initial"], r
    # density[k][j][i] voxel field, values in [0,1]
    assert len(r["density"]) == 2 and len(r["density"][0]) == 4 \
        and len(r["density"][0][0]) == 8, "voxel field shape"
    flat = [v for layer in r["density"] for row in layer for v in row]
    assert all(0.0 <= v <= 1.0 for v in flat), "densities out of [0,1]"


def test_3d_more_material_is_stiffer():
    lean = topo.simp_topology_3d(nelx=8, nely=4, nelz=2, keep_fraction=0.25, max_iter=8)
    rich = topo.simp_topology_3d(nelx=8, nely=4, nelz=2, keep_fraction=0.5, max_iter=8)
    assert rich["compliance"] < lean["compliance"], (lean["compliance"], rich["compliance"])


def test_3d_mid_plane_load_gives_z_symmetric_design():
    import numpy as np
    # default load sits on the z mid-plane when nelz is even -> the optimized
    # density field must mirror about it (assembly, filter and OC are all
    # deterministic, so the symmetry is exact)
    r = topo.simp_topology_3d(nelx=8, nely=4, nelz=4, keep_fraction=0.3, max_iter=8)
    d = np.array(r["density"])
    assert float(np.abs(d - d[::-1]).max()) < 1e-6, "design not z-symmetric"


def test_3d_keep_out_stays_void_and_keep_in_stays_solid():
    import numpy as np
    ko = [2, 4, 1, 3, 0, 2]          # x 2..4, y 1..3, z 0..2 forced void
    ki = [6, 8, 0, 2, 0, 1]          # forced solid
    r = topo.simp_topology_3d(nelx=8, nely=4, nelz=2, keep_fraction=0.4,
                              max_iter=6, keep_out=[ko], keep_in=[ki])
    d = np.array(r["density"])       # (nelz, nely, nelx) = d[k][j][i]
    assert float(d[0:2, 1:3, 2:4].max()) == 0.0, "keep_out cell got material"
    assert float(d[0:1, 0:2, 6:8].min()) == 1.0, "keep_in cell lost material"
    # an infeasible keep_in (more cells than the volume budget) is rejected
    try:
        topo.simp_topology_3d(nelx=4, nely=4, nelz=2, keep_fraction=0.1,
                              max_iter=2, keep_in=[[0, 4, 0, 4, 0, 2]])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for infeasible keep_in")


def test_3d_infeasible_keep_out_and_overlap_rejected():
    # keep_out that voids more than (1-keep_fraction)·N can't meet the volume target
    # — reject it (don't silently report converged below keep_fraction).
    try:
        topo.simp_topology_3d(nelx=10, nely=10, nelz=2, keep_fraction=0.6,
                              max_iter=2, keep_out=[[0, 7, 0, 10, 0, 2]])  # voids 70%
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for infeasible keep_out")
    # keep_out and keep_in cannot claim the same cell (forced void vs forced solid)
    try:
        topo.simp_topology_3d(nelx=10, nely=10, nelz=2, keep_fraction=0.5, max_iter=2,
                              keep_out=[[0, 3, 0, 3, 0, 2]], keep_in=[[0, 3, 0, 3, 0, 2]])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for keep_out/keep_in overlap")
    # a feasible keep_out still hits the volume target
    r = topo.simp_topology_3d(nelx=10, nely=10, nelz=2, keep_fraction=0.4, max_iter=5,
                              keep_out=[[0, 2, 0, 2, 0, 1]])
    assert abs(r["mass_fraction"] - 0.4) < 0.02, r["mass_fraction"]


def test_3d_uniform_cantilever_matches_beam_theory():
    # The relative physics gate: a (near-)uniform-density cantilever's FE
    # compliance vs Euler-Bernoulli + Timoshenko shear, F·δ = F²L³/3EI + κF²L/GA.
    # Coarse trilinear hexes + a point load put the ratio near 1.08 on this grid;
    # the band documents the H8 discretization envelope (locking vs point-load
    # softening), not an exact identity.
    nelx, nely, nelz = 12, 4, 4
    r = topo.simp_topology_3d(nelx=nelx, nely=nely, nelz=nelz,
                              keep_fraction=0.95, penal=1.0, max_iter=1)
    c_fe = r["compliance_initial"] / 0.95          # E scales linearly at penal=1
    L, h, b, E, nu, F = float(nelx), float(nely), float(nelz), 1.0, 0.3, 1.0
    inertia = b * h ** 3 / 12.0
    shear = 1.2 * F * F * L / ((E / (2 * (1 + nu))) * b * h)
    c_beam = F * F * L ** 3 / (3 * E * inertia) + shear
    ratio = c_fe / c_beam
    assert 0.85 < ratio < 1.35, (c_fe, c_beam, ratio)


def test_3d_solver_backends_agree():
    import sys
    import importlib
    ref = topo.simp_topology_3d(nelx=6, nely=3, nelz=2, keep_fraction=0.4, max_iter=4)
    # hide scipy -> dense; then cap dense -> matrix-free Jacobi-PCG
    saved = {m: sys.modules.pop(m) for m in list(sys.modules)
             if m == "scipy" or m.startswith("scipy.")}
    sys.modules["scipy"] = None
    cap = topo._DENSE_DOF_CAP
    try:
        dense = topo.simp_topology_3d(nelx=6, nely=3, nelz=2, keep_fraction=0.4, max_iter=4)
        topo._DENSE_DOF_CAP = 0
        cg = topo.simp_topology_3d(nelx=6, nely=3, nelz=2, keep_fraction=0.4, max_iter=4)
    finally:
        topo._DENSE_DOF_CAP = cap
        del sys.modules["scipy"]
        sys.modules.update(saved)
        importlib.invalidate_caches()
    assert dense["solver"] == "dense" and cg["solver"] == "cg", (dense["solver"], cg["solver"])
    assert ref["solver"] in ("scipy", "dense"), ref["solver"]
    for other in (dense, cg):
        assert abs(other["compliance"] - ref["compliance"]) < 1e-4 * ref["compliance"], \
            (ref["compliance"], other["compliance"], other["solver"])


def test_3d_input_validation():
    for bad in (
        lambda: topo.simp_topology_3d(nelx=1, nely=4, nelz=2),
        lambda: topo.simp_topology_3d(nelx=4, nely=4, nelz=0),
        lambda: topo.simp_topology_3d(nelx=4, nely=4, nelz=2, keep_fraction=1.0),
        lambda: topo.simp_topology_3d(nelx=4, nely=4, nelz=2, max_iter=2,
                                      loads=[[99, 0, 0, "y", -1.0]]),
        lambda: topo.simp_topology_3d(nelx=4, nely=4, nelz=2, max_iter=2,
                                      loads=[[4, 2, 1, "q", -1.0]]),
        lambda: topo.simp_topology_3d(nelx=4, nely=4, nelz=2, max_iter=2,
                                      keep_out=[[0, 1, 0, 1]]),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- 3-D density -> boxes (the voxel analogue of density_to_rects) -------------

def test_density_to_boxes_merges_full_grid_to_one_box():
    full = [[[1.0] * 3] * 2] * 2                     # nelz=2, nely=2, nelx=3
    dec = topo.density_to_boxes(full, threshold=0.5)
    assert dec["boxes"] == [(0, 0, 0, 3, 2, 2)], dec["boxes"]
    assert dec["solid_cells"] == 12, dec["solid_cells"]


def test_density_to_boxes_l_shape_exact_cover():
    g = [[[1, 1, 0], [1, 1, 0]],                     # layer 0: 2x2 block
         [[1, 1, 0], [1, 1, 1]]]                     # layer 1: block + 1 extra run
    dec = topo.density_to_boxes(g, threshold=0.5)
    assert dec["solid_cells"] == 9, dec
    # boxes are disjoint and cover exactly the solid voxels
    covered = set()
    for (i0, j0, k0, w, h, d) in dec["boxes"]:
        for k in range(k0, k0 + d):
            for j in range(j0, j0 + h):
                for i in range(i0, i0 + w):
                    assert (i, j, k) not in covered, "boxes overlap"
                    covered.add((i, j, k))
    solid = {(i, j, k) for k in range(2) for j in range(2) for i in range(3)
             if g[k][j][i] >= 0.5}
    assert covered == solid, (covered, solid)


def test_density_to_boxes_validation():
    for bad in (
        lambda: topo.density_to_boxes([], threshold=0.5),
        lambda: topo.density_to_boxes([[]], threshold=0.5),
        lambda: topo.density_to_boxes([[[]]], threshold=0.5),
        lambda: topo.density_to_boxes([[[1, 1], [1, 1]], [[1, 1]]]),       # ragged layer
        lambda: topo.density_to_boxes([[[1, 1], [1]]]),                    # ragged row
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

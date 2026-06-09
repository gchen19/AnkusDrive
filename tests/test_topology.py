"""Topology-optimization toys — oracles for driftpin.analysis.topology (SIMP).

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

from driftpin.analysis import topology as topo  # noqa: E402


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

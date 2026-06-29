"""Shared gate for CPU-intensive *live external-solver* test cases.

A handful of test files pair a cheap analytic-oracle half (always run) with a
heavy half that shells out to a real external solver — OpenFOAM, Elmer,
openEMS, Bempp, preCICE/CalculiX FSI, the openInjMoldSim molding build. On the
self-hosted CI box every one of those solvers IS installed, so the heavy half
runs on every push. Several take minutes, a few are flaky (multi-process
coupling under load), and run_all.sh uses `set -e` — so one slow/flaky live
solve can blow the 20-minute cap or abort the rest of the suite.

These live solves are a regression guard for the *solvers*; they add nothing to
an unrelated change. So they run only when RUN_HEAVY_SOLVES=1 — set by the
dedicated heavy-solves workflow (on-demand / nightly / when solver code
changes). The standard per-push suite leaves it unset, and every heavy case
skips (even where the solver is installed) while the cheap oracle/structure
halves still run and catch most regressions.

Usage — first line of a live-solve test, before its solver-presence check:

    from heavy_solve import skip_heavy
    def test_some_live_solve():
        if skip_heavy():
            return
        if not solvers.is_available("openfoam"):
            ...
"""
import os

RUN_HEAVY_SOLVES = os.environ.get("RUN_HEAVY_SOLVES") == "1"


def skip_heavy(label: str = "") -> bool:
    """True (and prints a reason) when live external-solver solves are gated off.

    Default in the per-push suite. Set RUN_HEAVY_SOLVES=1 to run them.
    """
    if not RUN_HEAVY_SOLVES:
        tag = f" ({label})" if label else ""
        print(f"    SKIP — live solver solve gated off{tag}; set RUN_HEAVY_SOLVES=1 to run")
        return True
    return False

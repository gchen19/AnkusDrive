"""Solver graceful-degradation contract — the gate that needs no solver.

Pure-Python, no FreeCAD (driftpin.solvers is FreeCAD-free), so this runs on the
fast/no-FreeCAD CI lane and gates every PR. It pins the M0 degradation contract
from docs/archive/SIMULATION_P2_KICKOFF.md: with a solver ABSENT, the resolution path must
return the clean {ok:false, reason, install} dict and never raise — that is what
lets each P2 family's *_submit degrade instead of crashing on a missing binary.

The two low-level probes (_module_available / _binary_path) are monkeypatched to
simulate absent/present, so the contract is deterministic regardless of what's
actually installed on the test box. As each external family lands (M2+), its own
*_submit adds a degradation assertion; this file guards the shared primitive every
one of them is built on.

Run:  python3 tests/test_solve_degradation.py
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402

# The families the P2 milestones (M2–M5 + optics) must each be able to degrade for.
_EXPECTED_FAMILIES = {"mbd", "topology", "cfd", "thermal_transient", "optics"}


class _force:
    """Context manager: force every solver absent (or present) by patching the two
    resolution primitives find_solver() relies on. Restores them on exit."""

    def __init__(self, available: bool):
        self.available = available

    def __enter__(self):
        self._mod = solvers._module_available
        self._bin = solvers._binary_path
        if self.available:
            solvers._module_available = lambda m: True
            solvers._binary_path = lambda name, spec: "/fake/bin/" + name
        else:
            solvers._module_available = lambda m: False
            solvers._binary_path = lambda name, spec: None
        return self

    def __exit__(self, *exc):
        solvers._module_available = self._mod
        solvers._binary_path = self._bin
        return False


def test_registry_covers_every_planned_family():
    """Every P2 family in the kickoff has at least one solver in the registry, so
    its *_submit has something to degrade against when it lands."""
    fams = {spec["family"] for spec in solvers._SOLVERS.values()}
    missing = _EXPECTED_FAMILIES - fams
    assert not missing, f"families with no registered solver: {sorted(missing)}"


def test_absent_solver_returns_clean_dict():
    """The core contract: solver absent -> require_solver returns the structured
    miss dict (ok False, the documented reason, an install hint) and NEVER raises,
    for every solver in the registry."""
    with _force(available=False):
        for name in solvers.known_solvers():
            r = solvers.require_solver(name)
            assert r["ok"] is False, (name, r)
            assert r["solver"] == name, (name, r)
            assert r["reason"] == "solver not installed", (name, r)
            assert r["install"] and isinstance(r["install"], str), (name, r)
            # find_solver agrees and carries the hint, not a path/module
            info = solvers.find_solver(name)
            assert info["available"] is False, (name, info)
            assert "install_hint" in info and "path" not in info and "module" not in info


def test_present_solver_resolves():
    """Solver present -> require_solver returns ok True with the resolved
    path (binary) or module (wheel), and find_solver marks it available."""
    with _force(available=True):
        for name in solvers.known_solvers():
            r = solvers.require_solver(name)
            assert r["ok"] is True, (name, r)
            assert r["name"] == name and "reason" not in r, (name, r)
            assert ("path" in r) or ("module" in r), (name, r)
            assert solvers.is_available(name) is True, name


def test_capabilities_never_raises_either_way():
    """solve_capabilities' payload is well-formed whether solvers are absent or
    present, and the family roll-up is consistent with per-solver availability."""
    with _force(available=False):
        caps = solvers.capabilities()
        assert caps["available"] == [], caps["available"]
        assert set(caps["solvers"]) == set(solvers.known_solvers())
        for fam, fi in caps["families"].items():
            assert fi["any_available"] is False, (fam, fi)
            assert fi["available"] == [], (fam, fi)
        # extras map names a pip extra to its wheel solvers
        assert "mbd" in caps["extras"] and "pybullet" in caps["extras"]["mbd"]

    with _force(available=True):
        caps = solvers.capabilities()
        assert sorted(caps["available"]) == solvers.known_solvers()
        assert _EXPECTED_FAMILIES <= set(caps["families"])
        for fam, fi in caps["families"].items():
            assert fi["any_available"] is True, (fam, fi)


def test_unknown_solver_raises_valueerror():
    """An unknown solver name is a wiring bug, not a missing install -> ValueError
    (so a typo'd name can't masquerade as 'not installed')."""
    for fn in (solvers.require_solver, solvers.find_solver, solvers.is_available):
        try:
            fn("definitely-not-a-solver")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{fn.__name__} should raise ValueError on unknown solver")


def test_module_probe_uses_find_spec():
    """The wheel probe reports importability via find_spec (no heavy import): a real
    dependency resolves, a nonsense name does not."""
    # use a stdlib module — the no-FreeCAD lane runs under system python3, which is
    # not guaranteed to carry driftpin's wheel deps (numpy etc.)
    assert solvers._module_available("json") is True
    assert solvers._module_available("nope_not_a_real_module_xyz") is False
    # a dotted name whose parent is missing must be absent, not an exception
    assert solvers._module_available("nope_xyz.sub") is False


def test_binary_env_override_resolves_real_path():
    """The binary resolution order honors DRIFTPIN_<SOLVER>_PATH first (the agent's
    escape hatch), resolving to a real file without any solver installed."""
    with tempfile.NamedTemporaryFile(prefix="fake-elmer-", delete=False) as tf:
        fake = tf.name
    prev = os.environ.get("DRIFTPIN_ELMER_PATH")
    try:
        os.environ["DRIFTPIN_ELMER_PATH"] = fake
        info = solvers.find_solver("elmer")
        assert info["available"] is True, info
        assert info["path"] == fake, info
    finally:
        if prev is None:
            os.environ.pop("DRIFTPIN_ELMER_PATH", None)
        else:
            os.environ["DRIFTPIN_ELMER_PATH"] = prev
        os.unlink(fake)


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

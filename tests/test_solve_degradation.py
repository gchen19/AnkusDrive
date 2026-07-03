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
        self._unwired = solvers._unwired_found
        # neutralize the installed-but-unwired probe (#177): "truly absent" means no
        # standard-location bashrc / dedicated venv on the box counts as evidence, so
        # the two-state contract stays deterministic regardless of the test host.
        solvers._unwired_found = lambda name, spec: None
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
        solvers._unwired_found = self._unwired
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
            assert r["status"] == "absent", (name, r)
            assert r["reason"] == "solver not installed", (name, r)
            assert r["install"] and isinstance(r["install"], str), (name, r)
            assert "found_at" not in r, (name, r)
            # find_solver agrees and carries the hint, not a path/module
            info = solvers.find_solver(name)
            assert info["available"] is False, (name, info)
            assert info["status"] == "absent", (name, info)
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
            assert solvers.find_solver(name)["status"] == "ok", name


def test_capabilities_never_raises_either_way():
    """solve_capabilities' payload is well-formed whether solvers are absent or
    present, and the family roll-up is consistent with per-solver availability."""
    with _force(available=False):
        caps = solvers.capabilities()
        assert caps["available"] == [], caps["available"]
        assert caps["unwired"] == [], caps["unwired"]
        assert set(caps["solvers"]) == set(solvers.known_solvers())
        for fam, fi in caps["families"].items():
            assert fi["any_available"] is False, (fam, fi)
            assert fi["available"] == [], (fam, fi)
            assert fi["unwired"] == [], (fam, fi)
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


class _absent_binaries_and_wheels:
    """Force the primitive resolvers absent WITHOUT touching the installed-but-unwired
    probe — so the #177 third state can be exercised deterministically on any host."""

    def __enter__(self):
        self._mod = solvers._module_available
        self._bin = solvers._binary_path
        solvers._module_available = lambda m: False
        solvers._binary_path = lambda name, spec: None
        return self

    def __exit__(self, *exc):
        solvers._module_available = self._mod
        solvers._binary_path = self._bin
        return False


def _clear_env(*names):
    """Temporarily clear env vars; returns a restorer callable."""
    saved = {n: os.environ.pop(n, None) for n in names}

    def restore():
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
    return restore


def test_openfoam_unwired_from_standard_bashrc():
    """#177: OpenFOAM's binary is only on PATH after sourcing etc/bashrc, so a bare
    shell can't resolve it — but a standard-location bashrc proves it's installed.
    Fake one in a tmp dir via the DRIFTPIN_OPENFOAM_DIRS discovery override and assert
    the third state `unwired` + the specific 'set DRIFTPIN_OPENFOAM_BASHRC=' hint."""
    tmp = tempfile.mkdtemp(prefix="fake-openfoam-")
    os.makedirs(os.path.join(tmp, "etc"))
    bashrc = os.path.join(tmp, "etc", "bashrc")
    with open(bashrc, "w") as fh:
        fh.write("# fake OpenFOAM bashrc\n")
    restore = _clear_env("DRIFTPIN_OPENFOAM_PATH", "DRIFTPIN_OPENFOAM_BASHRC")
    os.environ["DRIFTPIN_OPENFOAM_DIRS"] = tmp
    try:
        with _absent_binaries_and_wheels():
            info = solvers.find_solver("openfoam")
            assert info["available"] is False, info
            assert info["status"] == "unwired", info
            assert info["found_at"] == bashrc, info
            assert info["wire_hint"] == f"set DRIFTPIN_OPENFOAM_BASHRC={bashrc} (or source it)", info

            r = solvers.require_solver("openfoam")
            assert r["ok"] is False and r["status"] == "unwired", r
            assert r["reason"] == "solver installed but env not wired in this shell", r
            assert r["found_at"] == bashrc, r
            assert r["install"] == info["wire_hint"], r

            caps = solvers.capabilities()
            assert "openfoam" in caps["unwired"], caps["unwired"]
            assert "openfoam" not in caps["available"], caps["available"]
            assert "openfoam" in caps["families"]["cfd"]["unwired"], caps["families"]["cfd"]
    finally:
        os.environ.pop("DRIFTPIN_OPENFOAM_DIRS", None)
        restore()
        os.unlink(bashrc)
        os.rmdir(os.path.join(tmp, "etc"))
        os.rmdir(tmp)


def test_openems_unwired_from_dedicated_venv():
    """#177: openEMS lives in a dedicated .venv-openems, not this interpreter, so
    find_spec reports it absent. Fake the venv beside a tmp repo root (DRIFTPIN_REPO_ROOT
    discovery override) and assert `unwired` + the 'set DRIFTPIN_OPENEMS_PYTHON=' hint."""
    tmp = tempfile.mkdtemp(prefix="fake-repo-")
    venv = os.path.join(tmp, ".venv-openems")
    os.makedirs(os.path.join(venv, "bin"))
    with open(os.path.join(venv, "bin", "python3"), "w") as fh:
        fh.write("#!/bin/sh\n")
    restore = _clear_env("DRIFTPIN_OPENEMS_PYTHON")
    os.environ["DRIFTPIN_REPO_ROOT"] = tmp
    try:
        with _absent_binaries_and_wheels():
            info = solvers.find_solver("openems")
            assert info["status"] == "unwired", info
            assert info["found_at"] == venv, info
            assert info["wire_hint"] == f"set DRIFTPIN_OPENEMS_PYTHON={venv}/bin/python3", info
            # bempp (also a dedicated-venv solver) has no .venv-bempp here -> stays absent
            bempp = solvers.find_solver("bempp")
            assert bempp["status"] == "absent", bempp
            assert "found_at" not in bempp, bempp
    finally:
        os.environ.pop("DRIFTPIN_REPO_ROOT", None)
        restore()
        os.unlink(os.path.join(venv, "bin", "python3"))
        os.rmdir(os.path.join(venv, "bin"))
        os.rmdir(venv)
        os.rmdir(tmp)


def test_truly_absent_solver_still_reports_absent_with_install_hint():
    """#177 guard: with NO unwired evidence on the box, an env-scoped solver still
    reports plain `absent` and hands back the full install hint (not a wire hint)."""
    restore = _clear_env("DRIFTPIN_OPENFOAM_DIRS", "DRIFTPIN_REPO_ROOT",
                         "DRIFTPIN_OPENFOAM_BASHRC", "DRIFTPIN_OPENFOAM_PATH")
    # point the repo-root probe at an empty tmp dir so no real .venv-* is discovered
    empty = tempfile.mkdtemp(prefix="empty-repo-")
    os.environ["DRIFTPIN_REPO_ROOT"] = empty
    try:
        with _absent_binaries_and_wheels():
            for name in ("openems", "bempp"):
                info = solvers.find_solver(name)
                assert info["status"] == "absent", (name, info)
                assert "found_at" not in info and "wire_hint" not in info, (name, info)
                r = solvers.require_solver(name)
                assert r["status"] == "absent" and r["reason"] == "solver not installed", (name, r)
                assert r["install"] == solvers._SOLVERS[name]["install_hint"], (name, r)
    finally:
        os.environ.pop("DRIFTPIN_REPO_ROOT", None)
        restore()
        os.rmdir(empty)


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

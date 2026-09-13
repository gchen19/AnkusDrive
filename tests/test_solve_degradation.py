"""Solver graceful-degradation contract — the gate that needs no solver.

Pure-Python, no FreeCAD (ankusdrive.solvers is FreeCAD-free), so this runs on the
fast/no-FreeCAD CI lane and gates every PR. It pins the M0 degradation contract
from docs/archive/SIMULATION_P2_KICKOFF.md: with a solver ABSENT, the resolution path must
return the clean {ok:false, reason, install} dict and never raise — that is what
lets each P2 family's *_submit degrade instead of crashing on a missing binary.

The low-level resolution probes (see _RESOLUTION_PRIMITIVES) are
monkeypatched to simulate absent/present, so the contract is deterministic
regardless of what's actually installed on the test box. As each external family lands (M2+), its own
*_submit adds a degradation assertion; this file guards the shared primitive every
one of them is built on.

Run:  python3 tests/test_solve_degradation.py
"""
import ast
import inspect
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import config as adconfig  # noqa: E402
from ankusdrive import solvers  # noqa: E402

# The families the P2 milestones (M2–M5 + optics) must each be able to degrade for.
_EXPECTED_FAMILIES = {"mbd", "topology", "cfd", "thermal_transient", "optics"}


# Every module-level probe find_solver() consults to decide whether a solver
# resolves. A helper that forces a resolution state must patch ALL of them: one left
# live answers from whatever the test host has declared. _vm_binary_path (the macOS
# Multipass branch, #193) was added after these helpers and was missed, so on a Mac
# with a declared in-VM OpenFOAM the "everything absent" tests saw it resolve (#349).
# test_helpers_patch_every_resolution_primitive derives the real set from
# find_solver's AST and fails when this tuple falls behind.
_RESOLUTION_PRIMITIVES = ("_module_available", "_binary_path", "_vm_binary_path",
                          "_interpreter_python", "_unwired_found",
                          "_host_discovery_applies")


# Env vars that select WHERE substrate solvers run (#361). _clear_declared always
# clears them: a host that exports ANKUSDRIVE_SUBSTRATE=container would otherwise
# route every macOS/Linux resolution test through the container branch.
_SUBSTRATE_SELECTION = ("ANKUSDRIVE_SUBSTRATE", "ANKUSDRIVE_CONTAINER",
                        "ANKUSDRIVE_CONTAINER_ENGINE")


class _patched_resolution:
    """Base for the forcing helpers: saves and restores every resolution primitive,
    so a subclass only says what each one returns."""

    def _patches(self) -> dict:
        raise NotImplementedError

    def __enter__(self):
        patches = self._patches()
        missing = set(_RESOLUTION_PRIMITIVES) - set(patches)
        assert not missing, f"{type(self).__name__} leaves {sorted(missing)} live"
        self._saved = {n: getattr(solvers, n) for n in _RESOLUTION_PRIMITIVES}
        for n, fn in patches.items():
            setattr(solvers, n, fn)
        return self

    def __exit__(self, *exc):
        for n, fn in self._saved.items():
            setattr(solvers, n, fn)
        return False


class _force(_patched_resolution):
    """Context manager: force every solver absent (or present) by patching every
    resolution primitive find_solver() relies on. Restores them on exit."""

    def __init__(self, available: bool):
        self.available = available

    def _patches(self) -> dict:
        return {
            # neutralize the installed-but-unwired probe (#177): "truly absent" means
            # no standard-location bashrc / dedicated venv on the box counts as
            # evidence, so the two-state contract stays deterministic on any host.
            "_unwired_found": lambda name, spec: None,
            "_module_available": lambda m: self.available,
            "_binary_path": (lambda name, spec: "/fake/bin/" + name) if self.available
                            else (lambda name, spec: None),
            # the host path answers when forcing present; nothing answers from a VM
            "_vm_binary_path": lambda name, spec: None,
            # a host with ANKUSDRIVE_SUBSTRATE=container would otherwise skip host
            # discovery and ignore the forced _binary_path answer (#361)
            "_host_discovery_applies": lambda: True,
            # dedicated-interpreter solvers (#351) resolve in their own interpreter
            "_interpreter_python": (lambda name, spec: "/fake/venv/bin/python3")
                                   if self.available else (lambda name, spec: None),
        }


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
    # not guaranteed to carry ankusdrive's wheel deps (numpy etc.)
    assert solvers._module_available("json") is True
    assert solvers._module_available("nope_not_a_real_module_xyz") is False


def test_doctor_ready_via_names_the_resolved_package():
    """doctor's "ready via …" line must name the package that actually resolved, not
    the registry key. A wheel family can be satisfied by an alternative module (the
    "topopt" entry lists ("topopt", "solidspy")); if only solidspy is installed,
    reporting "ready via topopt" while topopt is absent is exactly the stale hint the
    doctor exists to avoid. Regression guard for the honest-label fix (issue #189)."""
    from ankusdrive import doctor

    _mod, _bin, _unwired, _interp = (
        solvers._module_available, solvers._binary_path, solvers._unwired_found,
        solvers._interpreter_python)
    solvers._unwired_found = lambda name, spec: None
    solvers._binary_path = lambda name, spec: None          # no binaries resolve
    solvers._interpreter_python = lambda name, spec: None
    # only solidspy present; topopt (the entry's own name) is absent
    solvers._module_available = lambda m: m == "solidspy"
    try:
        caps = solvers.capabilities()
        topo = caps["families"]["topology"]
        assert topo["any_available"] is True, topo
        # find_solver carried the resolved module through, and it is NOT the key name
        assert caps["solvers"]["topopt"]["module"] == "solidspy", caps["solvers"]["topopt"]
        line = next(l for l in doctor._fmt_solvers(caps) if l.strip().startswith(
            f"{doctor._MARK['ok']}") and "topology" in l)
        assert "solidspy" in line, line
        assert "topopt" not in line, line
    finally:
        solvers._module_available = _mod
        solvers._binary_path = _bin
        solvers._unwired_found = _unwired
        solvers._interpreter_python = _interp
    # a dotted name whose parent is missing must be absent, not an exception
    assert solvers._module_available("nope_xyz.sub") is False


def test_binary_env_override_resolves_real_path():
    """The binary resolution order honors ANKUSDRIVE_<SOLVER>_PATH first (the agent's
    escape hatch), resolving to a real file without any solver installed."""
    with tempfile.NamedTemporaryFile(prefix="fake-elmer-", delete=False) as tf:
        fake = tf.name
    prev = os.environ.get("ANKUSDRIVE_ELMER_PATH")
    try:
        os.environ["ANKUSDRIVE_ELMER_PATH"] = fake
        info = solvers.find_solver("elmer")
        assert info["available"] is True, info
        assert info["path"] == fake, info
    finally:
        if prev is None:
            os.environ.pop("ANKUSDRIVE_ELMER_PATH", None)
        else:
            os.environ["ANKUSDRIVE_ELMER_PATH"] = prev
        os.unlink(fake)


def test_windows_provisioner_dir_discovered_without_env():
    """On Windows, a solver the PowerShell provisioner extracted under
    %LOCALAPPDATA%\\AnkusDrive\\solvers resolves with NO env var — the minimal-env
    way an MCP host launches `ankusdrive mcp` (issues #205/#199). Skipped elsewhere
    (the provisioner layout is Windows-only)."""
    if os.name != "nt":
        print("    SKIP — Windows provisioner layout only exists on Windows")
        return
    prev_lad = os.environ.get("LOCALAPPDATA")
    prev_env = os.environ.pop("ANKUSDRIVE_ELMER_PATH", None)
    tmp = tempfile.mkdtemp(prefix="fake_lad_")
    try:
        bin_dir = os.path.join(tmp, "AnkusDrive", "solvers", "ElmerFake", "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "ElmerSolver.exe")
        open(fake, "w", encoding="utf-8").close()
        os.environ["LOCALAPPDATA"] = tmp
        # guard against a PATH-installed ElmerSolver stealing the resolution
        info = solvers.find_solver("elmer")
        assert info["available"] is True, info
        assert info["path"].startswith(tmp) or os.path.isfile(info["path"]), info
        if not info["path"].startswith(tmp):
            print("    NOTE — a real ElmerSolver resolved first; glob step untested here")
    finally:
        if prev_lad is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = prev_lad
        if prev_env is not None:
            os.environ["ANKUSDRIVE_ELMER_PATH"] = prev_env


def test_darwin_provisioner_dir_discovered_without_env():
    """On macOS, a solver install-solvers.sh extracted under
    ~/Library/Application Support/AnkusDrive/solvers resolves with NO env var —
    the minimal-env way an MCP host launches `ankusdrive mcp` (issue #194, the
    Darwin analog of the %LOCALAPPDATA% glob above). Skipped elsewhere."""
    if sys.platform != "darwin":
        print("    SKIP — Darwin provisioner layout only exists on macOS")
        return
    prev_home = os.environ.get("HOME")
    prev_env = os.environ.pop("ANKUSDRIVE_SU2_PATH", None)
    tmp = tempfile.mkdtemp(prefix="fake_home_")
    try:
        bin_dir = os.path.join(tmp, "Library", "Application Support", "AnkusDrive",
                               "solvers", "SU2-8.5.0", "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "SU2_CFD")
        open(fake, "w", encoding="utf-8").close()
        os.environ["HOME"] = tmp
        # guard against a PATH-installed SU2_CFD stealing the resolution
        info = solvers.find_solver("su2")
        assert info["available"] is True, info
        assert info["path"].startswith(tmp) or os.path.isfile(info["path"]), info
        if not info["path"].startswith(tmp):
            print("    NOTE — a real SU2_CFD resolved first; glob step untested here")
    finally:
        if prev_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev_home
        if prev_env is not None:
            os.environ["ANKUSDRIVE_SU2_PATH"] = prev_env


def test_sibling_bin_appends_exe_on_windows():
    """sibling_bin resolves a companion executable next to the main binary,
    adding the .exe suffix Windows needs (the bare join silently skipped the
    ViewFactors/ElmerGrid legs of a complete native Elmer install, issue #205)."""
    d = tempfile.mkdtemp(prefix="sibling_")
    main = os.path.join(d, "ElmerSolver.exe" if os.name == "nt" else "ElmerSolver")
    open(main, "w", encoding="utf-8").close()
    grid = os.path.join(d, "ElmerGrid.exe" if os.name == "nt" else "ElmerGrid")
    open(grid, "w", encoding="utf-8").close()
    got = solvers.sibling_bin(main, "ElmerGrid")
    # PATH may shadow with a real install; both outcomes must be a real file
    assert os.path.isfile(got), got
    if got.startswith(d):
        assert got == grid, (got, grid)


class _absent_binaries_and_wheels:
    """Force the primitive resolvers absent WITHOUT touching the installed-but-unwired
    probe — so the #177 third state can be exercised deterministically on any host."""

    def __enter__(self):
        self._mod = solvers._module_available
        self._bin = solvers._binary_path
        self._imports = solvers._interpreter_imports
        solvers._module_available = lambda m: False
        solvers._binary_path = lambda name, spec: None
        # no interpreter carries a dedicated-interpreter solver (#351) — the
        # candidates are still enumerated, so the unwired venv probe is exercised
        solvers._interpreter_imports = lambda exe, modules: False
        return self

    def __exit__(self, *exc):
        solvers._module_available = self._mod
        solvers._binary_path = self._bin
        solvers._interpreter_imports = self._imports
        return False


def _clear_declared(*names):
    """Temporarily clear a declaration from BOTH layers; returns a restorer callable.

    A solver path can be declared two ways, and `config.lookup` reads them in order:
    the environment variable first, then the on-disk config file that `ankusdrive setup`
    writes (`~/.config/ankusdrive/config.toml`). Clearing only the env vars therefore
    leaves the FILE still answering, so a test asserting the *undeclared* state reads
    whatever the machine it happens to run on has declared.

    That is not hypothetical: it turned this suite red on the Linux self-hosted runner
    the moment that box grew a config.toml naming an OpenFOAM path (#313). The suite
    looked hermetic because it cleared env vars, and was hermetic against exactly one
    of the two layers.

    Blinding `config.load` — rather than deleting keys — keeps the env layer's real
    behaviour intact, so a test that WANTS to assert env precedence still can.
    """
    # The substrate selection (#361) decides which resolution branch runs at all, so
    # an "undeclared" state must never inherit the host's choice of it either.
    names = (*names, *(n for n in _SUBSTRATE_SELECTION if n not in names))
    saved = {n: os.environ.pop(n, None) for n in names}
    real_load = adconfig.load
    adconfig.load = lambda: {}

    def restore():
        adconfig.load = real_load
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
    return restore


def test_openfoam_unwired_from_standard_bashrc():
    """#177: OpenFOAM's binary is only on PATH after sourcing etc/bashrc, so a bare
    shell can't resolve it — but a standard-location bashrc proves it's installed.
    Fake one in a tmp dir via the ANKUSDRIVE_OPENFOAM_DIRS discovery override and assert
    the third state `unwired` + the specific 'set ANKUSDRIVE_OPENFOAM_BASHRC=' hint."""
    tmp = tempfile.mkdtemp(prefix="fake-openfoam-")
    os.makedirs(os.path.join(tmp, "etc"))
    bashrc = os.path.join(tmp, "etc", "bashrc")
    with open(bashrc, "w", encoding="utf-8") as fh:
        fh.write("# fake OpenFOAM bashrc\n")
    restore = _clear_declared("ANKUSDRIVE_OPENFOAM_PATH", "ANKUSDRIVE_OPENFOAM_BASHRC")
    os.environ["ANKUSDRIVE_OPENFOAM_DIRS"] = tmp
    try:
        with _absent_binaries_and_wheels():
            info = solvers.find_solver("openfoam")
            assert info["available"] is False, info
            assert info["status"] == "unwired", info
            assert info["found_at"] == bashrc, info
            assert info["wire_hint"] == f"set ANKUSDRIVE_OPENFOAM_BASHRC={bashrc} (or source it)", info

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
        os.environ.pop("ANKUSDRIVE_OPENFOAM_DIRS", None)
        restore()
        os.unlink(bashrc)
        os.rmdir(os.path.join(tmp, "etc"))
        os.rmdir(tmp)


def test_openems_unwired_from_dedicated_venv():
    """#177: openEMS lives in a dedicated .venv-openems, not this interpreter, so
    find_spec reports it absent. Fake the venv beside a tmp repo root (ANKUSDRIVE_REPO_ROOT
    discovery override) and assert `unwired` + the 'set ANKUSDRIVE_OPENEMS_PYTHON=' hint."""
    tmp = tempfile.mkdtemp(prefix="fake-repo-")
    venv = os.path.join(tmp, ".venv-openems")
    os.makedirs(os.path.join(venv, "bin"))
    with open(os.path.join(venv, "bin", "python3"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    restore = _clear_declared("ANKUSDRIVE_OPENEMS_PYTHON")
    os.environ["ANKUSDRIVE_REPO_ROOT"] = tmp
    try:
        with _absent_binaries_and_wheels():
            info = solvers.find_solver("openems")
            assert info["status"] == "unwired", info
            assert info["found_at"] == venv, info
            assert info["wire_hint"] == f"set ANKUSDRIVE_OPENEMS_PYTHON={venv}/bin/python3", info
            # bempp (also a dedicated-venv solver) has no .venv-bempp here -> stays absent
            bempp = solvers.find_solver("bempp")
            assert bempp["status"] == "absent", bempp
            assert "found_at" not in bempp, bempp
    finally:
        os.environ.pop("ANKUSDRIVE_REPO_ROOT", None)
        restore()
        os.unlink(os.path.join(venv, "bin", "python3"))
        os.rmdir(os.path.join(venv, "bin"))
        os.rmdir(venv)
        os.rmdir(tmp)


def test_truly_absent_solver_still_reports_absent_with_install_hint():
    """#177 guard: with NO unwired evidence on the box, an env-scoped solver still
    reports plain `absent` and hands back the full install hint (not a wire hint)."""
    restore = _clear_declared("ANKUSDRIVE_OPENFOAM_DIRS", "ANKUSDRIVE_REPO_ROOT",
                         "ANKUSDRIVE_OPENFOAM_BASHRC", "ANKUSDRIVE_OPENFOAM_PATH")
    # point the repo-root probe at an empty tmp dir so no real .venv-* is discovered
    empty = tempfile.mkdtemp(prefix="empty-repo-")
    os.environ["ANKUSDRIVE_REPO_ROOT"] = empty
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
        os.environ.pop("ANKUSDRIVE_REPO_ROOT", None)
        restore()
        os.rmdir(empty)


# --- dedicated-interpreter solvers resolve in THEIR interpreter (issue #351) -------

_DEDICATED = ("openems", "bempp", "kraken")


def _make_venv(root):
    """A real venv under ``root`` (no pip, so it is quick) and its python path."""
    import subprocess
    import venv as _venv
    _venv.EnvBuilder(with_pip=False).create(root)
    py = next(p for p in solvers._venv_pythons(root) if os.path.isfile(p))
    purelib = subprocess.run(
        [py, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True).stdout.strip()
    return py, purelib


class _only_the_dedicated_interpreter:
    """Run find_solver as a process that CANNOT import the dedicated solvers, whatever
    the test box has installed: nothing in-process, no find_spec-origin venv, this
    interpreter's child probe blinded, the beside-the-repo venvs moved out of reach
    (ANKUSDRIVE_REPO_ROOT -> an empty dir), and every *_PYTHON declaration cleared —
    so only the override the test sets can answer."""

    def __init__(self, empty_root):
        self.root = empty_root

    def __enter__(self):
        self._mod, self._origin, self._imports = (
            solvers._module_available, solvers._module_origin, solvers._interpreter_imports)
        real = solvers._interpreter_imports_exec
        solvers._module_available = lambda m: False
        solvers._module_origin = lambda m: None
        solvers._interpreter_imports = (
            lambda exe, modules: exe != sys.executable and real(exe, modules, 30.0))
        self._restore = _clear_declared(
            "ANKUSDRIVE_REPO_ROOT",
            *(solvers._SOLVERS[n]["interpreter"]["env"] for n in _DEDICATED))
        os.environ["ANKUSDRIVE_REPO_ROOT"] = self.root
        return self

    def __exit__(self, *exc):
        for n in _DEDICATED:
            os.environ.pop(solvers._SOLVERS[n]["interpreter"]["env"], None)
        os.environ.pop("ANKUSDRIVE_REPO_ROOT", None)
        self._restore()
        solvers._module_available = self._mod
        solvers._module_origin = self._origin
        solvers._interpreter_imports = self._imports
        return False


def test_dedicated_interpreter_override_resolves_ok():
    """#351: openEMS / Bempp / KrakenOS run under a dedicated interpreter named by
    their *_PYTHON override. Discovery must ask THAT interpreter: with each override
    pointing at a venv that imports the solver's modules, run from a process that
    can't, find_solver reports ok (path = the interpreter) and each family is
    available. The reverse — override set, modules missing there — is absent, not ok."""
    import shutil
    tmp = tempfile.mkdtemp(prefix="dedicated-py-")
    try:
        empty = os.path.join(tmp, "repo")
        os.makedirs(empty)
        py, purelib = _make_venv(os.path.join(tmp, "solver-venv"))
        with _only_the_dedicated_interpreter(empty):
            for n in _DEDICATED:
                os.environ[solvers._SOLVERS[n]["interpreter"]["env"]] = py

            # reverse first: the interpreter exists but carries none of the modules
            for n in _DEDICATED:
                info = solvers.find_solver(n)
                assert info["status"] == "absent", (n, info)
                assert info["available"] is False and "path" not in info, (n, info)
            assert solvers.solver_python("bempp") is None

            # now give that interpreter the modules (stub packages via a .pth)
            stubs = os.path.join(tmp, "stubs")
            for n in _DEDICATED:
                for m in solvers._SOLVERS[n]["interpreter"]["requires"]:
                    os.makedirs(os.path.join(stubs, m))
                    open(os.path.join(stubs, m, "__init__.py"), "w", encoding="utf-8").close()
            with open(os.path.join(purelib, "stubs.pth"), "w", encoding="utf-8") as fh:
                fh.write(stubs + "\n")

            caps = solvers.capabilities()
            for n in _DEDICATED:
                info = caps["solvers"][n]
                assert info["status"] == "ok", (n, info)
                assert info["path"] == py, (n, info)
                assert info["module"] == solvers._SOLVERS[n]["interpreter"]["requires"][0]
                assert caps["families"][info["family"]]["any_available"] is True, (n, caps)
                assert solvers.solver_python(n) == py, n
                r = solvers.require_solver(n)
                assert r["ok"] is True and r["path"] == py, (n, r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dedicated_interpreter_needs_every_required_module():
    """openEMS's runner imports BOTH openEMS and CSXCAD — an interpreter carrying only
    one of them must not read as ok (the worker would launch it and fail)."""
    _imports = solvers._interpreter_imports
    _cands = solvers._interpreter_candidates
    solvers._interpreter_candidates = lambda spec: [sys.executable]
    solvers._interpreter_imports = lambda exe, modules: set(modules) <= {"openEMS"}
    try:
        assert solvers.solver_python("openems") is None
        solvers._interpreter_imports = lambda exe, modules: set(modules) <= {"openEMS", "CSXCAD"}
        assert solvers.solver_python("openems") == sys.executable
    finally:
        solvers._interpreter_imports = _imports
        solvers._interpreter_candidates = _cands


def test_solver_python_rejects_a_non_dedicated_solver():
    try:
        solvers.solver_python("openfoam")
    except ValueError:
        pass
    else:
        raise AssertionError("solver_python should refuse a binary solver")


# --- honest capabilities: drivability + the three Multipass states (issue #237) ---

class _only_available(_patched_resolution):
    """Force exactly ``names`` to resolve as binaries and nothing else, with the
    installed-but-unwired probe neutralized — so a family roll-up can be asserted
    independently of what the test box has installed."""

    def __init__(self, *names):
        self.names = set(names)

    def _patches(self) -> dict:
        return {
            "_module_available": lambda m: False,
            "_unwired_found": lambda name, spec: None,
            "_binary_path": lambda name, spec: ("/fake/bin/" + name) if name in self.names else None,
            "_vm_binary_path": lambda name, spec: None,
            # a host with ANKUSDRIVE_SUBSTRATE=container would otherwise skip host
            # discovery and ignore the forced _binary_path answer (#361)
            "_host_discovery_applies": lambda: True,
            "_interpreter_python": lambda name, spec: None,
        }


class _declares_prepared_case_only:
    """Temporarily mark a registry entry ``prepared_case_only``.

    Since #237 item 3 (analysis/su2_case.py builds the plane-Poiseuille case, so SU2
    is drivable and counts again) NO shipped solver declares this. The MECHANISM still
    has to hold — it is the general invariant "a solver counts toward a family only if
    some tool can actually drive it", and the next undrivable solver must not quietly
    make its family read available. So the tests inject the flag rather than leaning on
    a particular solver being the example."""

    def __init__(self, name, reason="no AnkusDrive tool builds a case for it"):
        self.name, self.reason = name, reason

    def __enter__(self):
        self._spec = solvers._SOLVERS[self.name]
        solvers._SOLVERS[self.name] = {**self._spec, "prepared_case_only": self.reason}
        return self

    def __exit__(self, *exc):
        solvers._SOLVERS[self.name] = self._spec
        return False


def test_prepared_case_only_solver_does_not_make_its_family_available():
    """The general invariant from #237 item 2: a solver counts toward a family only if
    some tool can actually DRIVE it. Counting a resolved-but-undrivable binary told an
    agent to escalate per cfd_pipe_flow's escalate_to hint, and it dead-ended.

    The solver must still be listed as resolved, and require_solver must still say ok
    — a hand-prepared case_dir remains a legitimate way to run it."""
    with _only_available("su2"), _declares_prepared_case_only("su2"):
        caps = solvers.capabilities()
        cfd = caps["families"]["cfd"]
        # honest both ways: the binary resolved, and the family is still not drivable
        assert cfd["available"] == ["su2"], cfd
        assert cfd["prepared_case_only"] == ["su2"], cfd
        assert cfd["any_available"] is False, cfd
        assert "su2" in caps["available"], caps["available"]
        assert caps["prepared_case_only"] == ["su2"], caps["prepared_case_only"]
        # the qualifier is a reason string the caller can show, not a bare flag
        info = solvers.find_solver("su2")
        assert isinstance(info.get("prepared_case_only"), str), info
        # a prepared case_dir still runs: require_solver is deliberately unaffected
        assert solvers.require_solver("su2")["ok"] is True, solvers.require_solver("su2")

    # ... and a solver that DOES have a case builder restores the family
    with _only_available("su2", "openfoam"), _declares_prepared_case_only("su2"):
        cfd = solvers.capabilities()["families"]["cfd"]
        assert cfd["any_available"] is True, cfd
        assert cfd["prepared_case_only"] == ["su2"], cfd


def test_su2_alone_now_makes_the_cfd_family_available():
    """#237 item 3, the other side of the invariant: SU2 has a case builder now
    (analysis/su2_case.py writes the plane-Poiseuille case, run by
    cfd_internal_flow_submit's `channel_height_mm` mode), so it must NOT be marked
    prepared_case_only and the cfd family must read available on SU2 alone. That is
    what makes docs/MACOS.md's "CFD degrades to SU2" true rather than aspirational —
    on Apple Silicon it is the difference between needing a Multipass VM and not."""
    assert "prepared_case_only" not in solvers._SOLVERS["su2"], \
        "su2 has a case builder since #237 item 3; it must count toward its family"
    with _only_available("su2"):
        cfd = solvers.capabilities()["families"]["cfd"]
        assert cfd["available"] == ["su2"], cfd
        assert cfd["prepared_case_only"] == [], cfd
        assert cfd["any_available"] is True, cfd


def test_doctor_names_a_prepared_case_only_solver_it_did_not_count():
    """A family reported unwired next to a solver the user just installed is baffling
    unless the doctor says why it doesn't count. #237 item 2 consumer check."""
    from ankusdrive import doctor

    with _only_available("su2"), _declares_prepared_case_only("su2"):
        caps = solvers.capabilities()
        lines = doctor._fmt_solvers(caps)
        idx = next(i for i, ln in enumerate(lines) if ln.split()[1:2] == ["cfd"])
        assert doctor._MARK["ok"] not in lines[idx], lines[idx]
        note = "\n".join(lines[idx:idx + 4])
        assert "su2 resolves" in note, note
        assert "no AnkusDrive tool builds a case for it" in note, note

    # and when the family IS ready, the "ready via" line names only what made it so —
    # "ready via su2, openfoam" would point back at the solver that cannot be driven
    with _only_available("su2", "openfoam"), _declares_prepared_case_only("su2"):
        lines = doctor._fmt_solvers(solvers.capabilities())
        line = next(ln for ln in lines if ln.split()[1:2] == ["cfd"])
        assert doctor._MARK["ok"] in line, line
        assert "openfoam" in line and "su2" not in line, line


# Captured verbatim from a real `multipass info openfoam --format json` on this
# Apple-Silicon box (Ubuntu 24.04 VM), trimmed to the keys the probe reads. Only
# `state` differs between the fixtures — the shape is the contract being parsed.
def _fake_vm_info(instance: str, state: str) -> str:
    return json.dumps({
        "errors": [],
        "info": {instance: {
            "cpu_count": "8", "image_release": "24.04 LTS",
            "ipv4": ["192.168.252.2"], "release": "Ubuntu 24.04.4 LTS",
            "snapshot_count": "0", "state": state,
        }},
    })


class _fake_multipass:
    """Patch the injectable `multipass info` seam (and the bashrc glob that would
    otherwise short-circuit the macOS branch) so all three VM states can be exercised
    on any platform — including Linux CI, where multipass does not exist. Reads a
    fixture string; never launches, starts or stops a VM.

    ``state=None`` fakes the read failing (no such instance / timeout / hung daemon),
    which must degrade to the absent hint."""

    def __init__(self, state: str | None):
        self.state = state

    def __enter__(self):
        self._avail = solvers.multipass_available
        self._info = solvers._multipass_info
        self._bashrc = solvers._standard_bashrc
        solvers.multipass_available = lambda: True
        solvers._standard_bashrc = lambda cfg: None
        solvers._multipass_info = (
            (lambda inst: None) if self.state is None
            else (lambda inst: _fake_vm_info(inst, self.state)))
        return self

    def __exit__(self, *exc):
        solvers.multipass_available = self._avail
        solvers._multipass_info = self._info
        solvers._standard_bashrc = self._bashrc
        return False


def test_multipass_vm_state_reads_three_states_and_degrades():
    """#237(b): the read-only state probe distinguishes running / stopped / absent
    from the same `multipass info` payload, and degrades to `absent` — the
    conservative provision hint — on anything it cannot parse."""
    with _fake_multipass("Running"):
        assert solvers.multipass_vm_state() == "running"
    for stopped in ("Stopped", "Suspended", "Starting"):
        with _fake_multipass(stopped):
            assert solvers.multipass_vm_state() == "stopped", stopped
    for gone in (None, "Deleted", "Unknown", ""):
        with _fake_multipass(gone):
            assert solvers.multipass_vm_state() == "absent", gone
    # garbage payload -> absent, never an exception
    saved_avail, saved_info = solvers.multipass_available, solvers._multipass_info
    try:
        solvers.multipass_available = lambda: True
        solvers._multipass_info = lambda inst: "not json at all {{"
        assert solvers.multipass_vm_state() == "absent"
        solvers._multipass_info = lambda inst: json.dumps(["not", "a", "dict"])
        assert solvers.multipass_vm_state() == "absent"
    finally:
        solvers.multipass_available, solvers._multipass_info = saved_avail, saved_info
    # no multipass CLI at all (every non-macOS box) -> absent, with no subprocess
    saved_avail = solvers.multipass_available
    try:
        solvers.multipass_available = lambda: False
        assert solvers.multipass_vm_state() == "absent"
    finally:
        solvers.multipass_available = saved_avail


def test_multipass_info_read_degrades_instead_of_raising():
    """The subprocess seam itself: a missing multipass (Linux), a nonexistent
    instance (rc 2) and a read that outruns its timeout all return None rather than
    raising or blocking discovery. Read-only — `multipass info` on a name that does
    not exist touches nothing."""
    assert solvers._multipass_info_exec("ankusdrive-no-such-vm-237", 10.0) is None
    # a timeout that no real read can beat must also come back None, not raise
    assert solvers._multipass_info_exec("ankusdrive-no-such-vm-237", 0.001) is None


def test_openfoam_unwired_hint_tracks_multipass_vm_state():
    """#237(b), the issue's acceptance gate: VM absent vs stopped vs running produce
    three DISTINGUISHABLE, correctly-hinted states. Before this, all three said
    "provision the VM" — the one instruction that is wrong for the most common
    post-setup state, a provisioned VM that is up with an unwired shell."""
    restore = _clear_declared("ANKUSDRIVE_OPENFOAM_PATH", "ANKUSDRIVE_OPENFOAM_BASHRC",
                         "ANKUSDRIVE_OPENFOAM_DIRS")
    hints, founds = {}, {}
    try:
        for label, state in (("absent", None), ("stopped", "Stopped"),
                             ("running", "Running")):
            with _absent_binaries_and_wheels(), _fake_multipass(state):
                info = solvers.find_solver("openfoam")
                assert info["status"] == "unwired", (label, info)
                hints[label] = info["wire_hint"]
                founds[label] = info["found_at"]
                assert solvers.require_solver("openfoam")["install"] == hints[label]
    finally:
        restore()

    # three states, three messages, three evidence strings — nothing collapses
    assert len(set(hints.values())) == 3, hints
    assert len(set(founds.values())) == 3, founds

    # absent: provision it (the original message, now scoped to the state it fits)
    assert hints["absent"].startswith("provision OpenFOAM in the Multipass VM"), hints
    assert founds["absent"] == "multipass", founds

    # stopped: start it — NOT provision it again
    assert "multipass start openfoam" in hints["stopped"], hints["stopped"]
    assert not hints["stopped"].startswith("provision"), hints["stopped"]
    assert "(stopped)" in founds["stopped"], founds

    # running: the env-wiring exports from docs/MACOS.md, and no provisioning lead
    run = hints["running"]
    assert not run.startswith("provision"), run
    assert "multipass start" not in run, run
    for export in ("ANKUSDRIVE_OPENFOAM_BASHRC", "ANKUSDRIVE_OPENFOAM_PATH", "TMPDIR"):
        assert export in run, (export, run)
    assert "docs/MACOS.md" in run, run
    assert "(running)" in founds["running"], founds


def test_multipass_running_state_live_on_macos():
    """The running state verified against the REAL machine, not a fixture — the half
    of the #237 gate a fake cannot prove. Strictly read-only: `multipass info` only,
    never `multipass start`/`stop` (a live solve may be running in that VM)."""
    if sys.platform != "darwin":
        print("    SKIP — the Multipass substrate is macOS-only")
        return
    if not solvers.multipass_available():
        print("    SKIP — no multipass CLI on PATH")
        return
    state = solvers.multipass_vm_state()
    assert state in ("absent", "stopped", "running"), state
    if state != "running":
        print(f"    NOTE — VM is {state!r} here; live running-state check skipped")
        return
    restore = _clear_declared("ANKUSDRIVE_OPENFOAM_PATH", "ANKUSDRIVE_OPENFOAM_BASHRC",
                         "ANKUSDRIVE_OPENFOAM_DIRS")
    try:
        with _absent_binaries_and_wheels():
            info = solvers.find_solver("openfoam")
            assert info["status"] == "unwired", info
            assert "(running)" in info["found_at"], info
            assert not info["wire_hint"].startswith("provision"), info["wire_hint"]
            assert "ANKUSDRIVE_OPENFOAM_BASHRC" in info["wire_hint"], info["wire_hint"]
    finally:
        restore()


# --- runner -------------------------------------------------------------------

def test_helpers_patch_every_resolution_primitive():
    """#349, the gate that keeps the forcing helpers hermetic as discovery grows.

    Derive the probes find_solver() really calls from its AST (module-level `_name(`
    calls, minus the registry lookup `_spec`), and require _RESOLUTION_PRIMITIVES to
    match exactly. Then prove each helper replaces every one of them while active and
    restores them all afterwards. Adding a third resolution branch — a container
    probe, a second VM — fails here on every lane, instead of only on the one machine
    whose config happens to exercise it."""
    tree = ast.parse(inspect.getsource(solvers.find_solver))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id.startswith("_") and callable(getattr(solvers, n.func.id, None))}
    probes = called - {"_spec"}
    assert probes == set(_RESOLUTION_PRIMITIVES), (
        f"find_solver's resolution probes {sorted(probes)} != "
        f"_RESOLUTION_PRIMITIVES {sorted(_RESOLUTION_PRIMITIVES)} — patch the new one in "
        f"_force/_only_available and add it to the tuple")

    originals = {n: getattr(solvers, n) for n in _RESOLUTION_PRIMITIVES}
    for helper in (_force(available=False), _force(available=True), _only_available("su2")):
        with helper:
            live = [n for n in _RESOLUTION_PRIMITIVES if getattr(solvers, n) is originals[n]]
            assert not live, f"{type(helper).__name__} leaves {live} live"
        leaked = [n for n in _RESOLUTION_PRIMITIVES if getattr(solvers, n) is not originals[n]]
        assert not leaked, f"{type(helper).__name__} did not restore {leaked}"


def test_force_absent_holds_against_a_declared_multipass_solver():
    """#349's failure, made reachable on every lane: a macOS box with `multipass` and
    an in-VM OpenFOAM override declared. Multipass is faked present and the override
    is declared through the env layer (config file blinded), so this runs on Linux and
    Windows CI too — where the real bug never showed.

    Control first: unforced, the declaration resolves via the VM branch. Otherwise the
    forced assertion below could pass on a box where that branch is simply dead."""
    declared = "/usr/lib/openfoam/openfoam2512/platforms/fake/bin/simpleFoam"
    restore = _clear_declared("ANKUSDRIVE_OPENFOAM_PATH", "ANKUSDRIVE_OPENFOAM_BASHRC",
                              "ANKUSDRIVE_OPENFOAM_DIRS")
    saved_avail, saved_bin = solvers.multipass_available, solvers._binary_path
    os.environ["ANKUSDRIVE_OPENFOAM_PATH"] = declared
    try:
        solvers.multipass_available = lambda: True
        solvers._binary_path = lambda name, spec: None      # nothing on the host
        control = solvers.find_solver("openfoam")
        assert control["available"] and control.get("via") == "multipass", \
            ("control: the VM branch did not resolve the declared override", control)

        with _force(available=False):
            r = solvers.require_solver("openfoam")
            assert r["ok"] is False and r["status"] == "absent", r
            caps = solvers.capabilities()
            assert "openfoam" not in caps["available"], caps["available"]
        with _only_available("su2"):
            assert solvers.capabilities()["families"]["cfd"]["available"] == ["su2"]
    finally:
        solvers.multipass_available, solvers._binary_path = saved_avail, saved_bin
        os.environ.pop("ANKUSDRIVE_OPENFOAM_PATH", None)
        restore()


def test_clear_declared_blinds_the_config_file_not_just_the_env():
    """#313, this suite's own isolation gate: the "solver is not declared" state must
    be reachable on a machine that HAS declared one.

    Every test here that asserts an undeclared state does it through _clear_declared.
    That helper used to clear only the environment variable, while `config.lookup`
    resolves env *then* the on-disk config file — so on a box whose config.toml names
    an OpenFOAM path, the "absent" case silently read that path and the suite went red
    on unmodified main. A helper that isolates one of two layers is worse than none,
    because it looks hermetic.

    So assert the isolation itself, both ways: with a config that declares a path,
    the harness must not see it — AND the control must prove the fake config would
    otherwise be seen, or this test could pass while asserting nothing.
    """
    declared = "/nonexistent/openfoam/platforms/bin/simpleFoam"
    real_load = adconfig.load
    adconfig.load = lambda: {"solvers": {"openfoam_path": declared}}
    # The control reads the FILE layer, but env outranks it: on a machine that exports
    # ANKUSDRIVE_OPENFOAM_PATH (the heavy-solver image does, #340) the control saw the
    # env value instead. Take the env var out of the way for this test's duration —
    # the mirror image of the #313 defect, on the other layer.
    saved_env = os.environ.pop("ANKUSDRIVE_OPENFOAM_PATH", None)
    try:
        # Control: unblinded, the declaration IS honoured — the fake has real teeth.
        seen = adconfig.get("ANKUSDRIVE_OPENFOAM_PATH")
        assert seen == declared, ("control: config layer not consulted", seen)

        # The gate: under the helper, neither layer answers.
        restore = _clear_declared("ANKUSDRIVE_OPENFOAM_PATH")
        try:
            blind = adconfig.get("ANKUSDRIVE_OPENFOAM_PATH")
            assert blind is None, ("config file still answering through the helper", blind)
        finally:
            restore()

        # And the helper restores what it borrowed — a leaked patch would silently
        # disarm every later test in the file.
        assert adconfig.get("ANKUSDRIVE_OPENFOAM_PATH") == declared, "helper did not restore"
    finally:
        adconfig.load = real_load
        if saved_env is not None:
            os.environ["ANKUSDRIVE_OPENFOAM_PATH"] = saved_env


def test_clear_declared_leaves_the_env_layer_working():
    """The fix must blind the FILE, not flatten both layers — a test that wants to
    assert env precedence still has to be able to."""
    real_load = adconfig.load
    adconfig.load = lambda: {"solvers": {"openfoam_path": "/from/config/simpleFoam"}}
    saved = os.environ.get("ANKUSDRIVE_OPENFOAM_PATH")
    os.environ["ANKUSDRIVE_OPENFOAM_PATH"] = "/from/env/simpleFoam"
    try:
        restore = _clear_declared("ANKUSDRIVE_SOME_OTHER_KEY")   # a DIFFERENT key
        try:
            # config blinded, but this key's env value is untouched and still wins
            assert adconfig.get("ANKUSDRIVE_OPENFOAM_PATH") == "/from/env/simpleFoam"
        finally:
            restore()
    finally:
        adconfig.load = real_load
        if saved is None:
            os.environ.pop("ANKUSDRIVE_OPENFOAM_PATH", None)
        else:
            os.environ["ANKUSDRIVE_OPENFOAM_PATH"] = saved


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

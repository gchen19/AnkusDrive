"""The container substrate and substrate selection (issue #361).

Pure-Python, no container engine and no solver needed: like test_wsl_routing.py it
monkeypatches ``platform.system`` / ``shutil.which`` / the filesystem probes / the
``<engine> container inspect`` read, so every branch runs on any host OS. Pins:

  * ``substrate()`` — unset keeps the per-OS defaults exactly (Linux native, Windows
    wsl, macOS multipass); an explicit value is honored; a bad value, ``wsl`` off
    Windows and ``container`` on Windows raise a NAMED error instead of silently
    defaulting. Same for ``ANKUSDRIVE_CONTAINER_ENGINE``.
  * ``bash_argv`` under ``container`` — ``<engine> exec -w <case> <name> bash -c``,
    per engine; never ``-i`` (the caller's stdin must not reach the container);
    ``runs_in_substrate()`` true, so FSI sweeps participants by pidfile.
  * Opaque-filesystem trust — absolute in-container overrides resolve (``via:
    "container"``) when the engine is on PATH, never when it is missing, never
    relative; and a HOST install is ignored under ``container`` (it is not what
    ``<engine> exec`` would run) — for find_solver and every OpenFOAM / FSI /
    openInjMoldSim resolver.
  * ``container_state`` — running / stopped / absent from real inspect payload
    shapes, degrading to absent on garbage; the inspect read itself never takes stdin.
  * The unwired hint tracks the container's state, the capabilities payload names
    the substrate, and doctor says "(in container)".
  * Beyond OpenFOAM (#419): YADE and Elmer resolve by PROBING the running container
    (or from a trusted absolute override), openEMS and Bempp by a find_spec asked of
    the in-container interpreter; host installs of all four are ignored, an image
    lacking one reports it unresolved with a container-state hint, and the unset
    substrate never probes. ``solver_argv`` wraps direct launches (``-i`` only for an
    owned pipe), ``stage_runner`` puts the runner where the container can read it,
    Elmer's siblings are in-container paths, and the worker has no direct launch left.

Run:  python3 tests/test_container_substrate.py
"""
import fnmatch
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import solvers  # noqa: E402

# in-container paths as the heavy image publishes them (docker/heavy-solvers/Dockerfile)
_C_BIN = "/usr/lib/openfoam/openfoam2512/platforms/linux64GccDPInt32Opt/bin/simpleFoam"
_C_BASHRC = "/usr/lib/openfoam/openfoam2512/etc/bashrc"
_C_CCX = "/opt/fsi/calculix-adapter/bin/ccx_preCICE"
_C_PRECICE_LIB = "/opt/fsi/precice/lib"
_C_ADAPTER_LIB = "/opt/fsi/openfoam-adapter/lib"
_C_OIMS = "/opt/of7/OpenFOAM/site/7/platforms/linux64GccDPInt32Opt/bin/openInjMoldSim"
_C_OIMS_BASHRC = "/opt/of7/OpenFOAM/OpenFOAM-7/etc/bashrc"

_ISOLATE = ("ANKUSDRIVE_SUBSTRATE", "ANKUSDRIVE_CONTAINER", "ANKUSDRIVE_CONTAINER_ENGINE",
            "ANKUSDRIVE_OPENFOAM_PATH", "ANKUSDRIVE_OPENFOAM_BASHRC",
            "ANKUSDRIVE_OPENFOAM_DIRS", "ANKUSDRIVE_OPENFOAM_INSTANCE",
            "ANKUSDRIVE_FSI_OPENFOAM_BASHRC", "ANKUSDRIVE_CCX_PRECICE",
            "ANKUSDRIVE_PRECICE_PATH", "ANKUSDRIVE_PRECICE_LIB",
            "ANKUSDRIVE_OPENFOAM_ADAPTER_LIB", "ANKUSDRIVE_OPENINJMOLDSIM",
            "ANKUSDRIVE_OPENINJMOLDSIM_PATH", "ANKUSDRIVE_OPENINJMOLDSIM_BASHRC",
            "ANKUSDRIVE_WSL_DISTRO", "WM_PROJECT_DIR", "ANKUSDRIVE_CONFIG",
            "ANKUSDRIVE_ELMER_PATH", "ANKUSDRIVE_YADE", "ANKUSDRIVE_YADE_PATH",
            "ANKUSDRIVE_OPENEMS_PYTHON", "ANKUSDRIVE_BEMPP_PYTHON",
            "ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE")


class _patch:
    """Patch module attributes and isolate the ANKUSDRIVE_* env + config.toml layer
    (ANKUSDRIVE_CONFIG -> a nonexistent file, which config.load() treats as {})."""

    def __init__(self):
        self._saved = {}
        self._env = {}

    def set(self, obj, attr, value):
        key = (id(obj), attr)
        if key not in self._saved:
            self._saved[key] = (obj, attr, getattr(obj, attr))
        setattr(obj, attr, value)

    def env(self, **kv):
        for k, v in kv.items():
            os.environ[k] = v

    def __enter__(self):
        self._env = {n: os.environ.pop(n, None) for n in _ISOLATE}
        os.environ["ANKUSDRIVE_CONFIG"] = os.path.join(
            os.path.dirname(__file__), "no-such-config.toml")
        solvers._ctr_info_cache.clear()
        solvers._ctr_probe_cache.clear()
        return self

    def __exit__(self, *exc):
        for obj, attr, val in self._saved.values():
            setattr(obj, attr, val)
        for n in _ISOLATE:
            os.environ.pop(n, None)
        for n, v in self._env.items():
            if v is not None:
                os.environ[n] = v
        solvers._ctr_info_cache.clear()
        solvers._ctr_probe_cache.clear()
        return False


def _fake_platform(system):
    class _P:
        @staticmethod
        def system():
            return system

        @staticmethod
        def machine():
            return "x86_64"
    return _P


def _container_host(p, system="Linux", engine_on_path=("docker",), host_files=()):
    """A host with ANKUSDRIVE_SUBSTRATE=container, the given engine CLIs on PATH, and a
    host filesystem holding only ``host_files`` — so anything resolved from the
    container was resolved by opaque trust, never by a host hit."""
    p.set(solvers, "platform", _fake_platform(system))
    p.env(ANKUSDRIVE_SUBSTRATE="container")
    p.set(solvers.shutil, "which",
          lambda n: f"/usr/bin/{n}" if n in engine_on_path else None)
    # The fake host is POSIX-shaped, but the resolvers build candidates with the REAL
    # os.path — on a Windows runner "/usr/bin" + "simpleFoam" joins as
    # "/usr/bin\\simpleFoam" and "~" expands to "C:\\Users\\...". Compare with separators
    # normalized so the fake filesystem answers identically on every host OS.
    def _norm(x):
        return str(x).replace("\\", "/")
    files = {_norm(f) for f in host_files}
    p.set(solvers.os.path, "isfile", lambda x: _norm(x) in files)
    p.set(solvers.os.path, "isdir", lambda x: False)
    p.set(glob, "glob", lambda pat, recursive=False: sorted(
        f for f in files if fnmatch.fnmatchcase(f, _norm(os.path.expanduser(pat)))))


# --- selection ------------------------------------------------------------------

def test_unset_substrate_keeps_every_platform_default():
    """No ANKUSDRIVE_SUBSTRATE: the substrate is exactly what the host OS implied
    before #361, so no existing install changes behavior."""
    for system, expect in (("Linux", "native"), ("Windows", "wsl"),
                           ("Darwin", "multipass"), ("FreeBSD", "native")):
        with _patch() as p:
            p.set(solvers, "platform", _fake_platform(system))
            assert solvers.substrate() == expect, (system, solvers.substrate())


def test_explicit_substrate_is_honored_case_insensitively():
    with _patch() as p:
        p.set(solvers, "platform", _fake_platform("Darwin"))
        for raw, expect in (("container", "container"), (" Native ", "native"),
                            ("MULTIPASS", "multipass")):
            p.env(ANKUSDRIVE_SUBSTRATE=raw)
            assert solvers.substrate() == expect, raw
        p.set(solvers, "platform", _fake_platform("Windows"))
        p.env(ANKUSDRIVE_SUBSTRATE="wsl")
        assert solvers.substrate() == "wsl"


def test_bad_selection_raises_a_named_error_never_a_silent_default():
    """A typo must not quietly fall back to native — the solvers would run (or not be
    found) somewhere the user never chose."""
    cases = (("Linux", "ANKUSDRIVE_SUBSTRATE", "docker", "not a substrate"),
             ("Linux", "ANKUSDRIVE_SUBSTRATE", "wsl", "only applies on Windows"),
             ("Darwin", "ANKUSDRIVE_SUBSTRATE", "wsl", "only applies on Windows"),
             ("Windows", "ANKUSDRIVE_SUBSTRATE", "container", "not supported on Windows"))
    for system, var, value, needle in cases:
        with _patch() as p:
            p.set(solvers, "platform", _fake_platform(system))
            p.env(**{var: value})
            try:
                solvers.substrate()
            except ValueError as e:
                assert needle in str(e) and var in str(e), (system, value, e)
            else:
                raise AssertionError(f"{system} {var}={value!r} did not raise")
    with _patch() as p:
        p.env(ANKUSDRIVE_CONTAINER_ENGINE="lxc")
        try:
            solvers.container_engine()
        except ValueError as e:
            assert "ANKUSDRIVE_CONTAINER_ENGINE" in str(e) and "lxc" in str(e), e
        else:
            raise AssertionError("an unsupported engine did not raise")


def test_engine_and_container_name_defaults_and_overrides():
    with _patch() as p:
        assert solvers.container_engine() == "docker"
        assert solvers.container_name() == "ankusdrive-solvers"
        p.env(ANKUSDRIVE_CONTAINER_ENGINE="Podman", ANKUSDRIVE_CONTAINER="foam")
        assert solvers.container_engine() == "podman"
        assert solvers.container_name() == "foam"


# --- launch ---------------------------------------------------------------------

def test_bash_argv_execs_into_the_container_with_the_case_as_workdir():
    """`<engine> exec -w <case> <name> bash -c <script>`: the case dir is the same
    absolute path inside (bind-mounted), so -w names it directly. No -i/-t: the
    caller's stdin (the worker's JSON-RPC pipe) must never reach the container."""
    with _patch() as p:
        _container_host(p)
        assert solvers.bash_argv("blockMesh", "/tmp/my case") == [
            "docker", "exec", "-w", "/tmp/my case", "ankusdrive-solvers",
            "bash", "-c", "blockMesh"]
        assert solvers.bash_argv("echo hi") == [
            "docker", "exec", "ankusdrive-solvers", "bash", "-c", "echo hi"]
        for engine in ("podman", "nerdctl"):
            p.env(ANKUSDRIVE_CONTAINER_ENGINE=engine, ANKUSDRIVE_CONTAINER="foam")
            argv = solvers.bash_argv("x", "/c")
            assert argv[:2] == [engine, "exec"] and argv[4] == "foam", argv
        assert not {"-i", "-t", "-it", "--interactive", "--tty"} & set(
            solvers.bash_argv("x", "/c")), "the container must not get stdin"


def test_runs_in_substrate_follows_the_selection_not_the_os():
    with _patch() as p:
        _container_host(p)
        assert solvers.runs_in_substrate() is True        # a relay: pidfile sweep
        p.set(solvers, "platform", _fake_platform("Darwin"))
        p.env(ANKUSDRIVE_SUBSTRATE="native")
        assert solvers.runs_in_substrate() is False       # explicit native on a Mac
        assert solvers.bash_argv("x", "/c") == ["bash", "-c", "x"]


def test_container_run_command_mounts_the_scratch_at_the_same_path():
    with _patch() as p:
        p.env(ANKUSDRIVE_CONTAINER_ENGINE="podman", ANKUSDRIVE_CONTAINER="foam")
        cmd = solvers.container_run_command()
        assert cmd.startswith("podman run -d --name foam "), cmd
        assert '-v "$TMPDIR:$TMPDIR"' in cmd and "ghcr.io/gchen19/ankusdrive-solvers" in cmd
        # solvers write heavily to /tmp (OpenMPI, openEMS, numba); a full Docker disk
        # otherwise fails a solve in a way that looks like a solver bug (#422)
        assert "--tmpfs /tmp" in cmd, cmd
        # as the host user, or case files come out root-owned on a native Linux engine
        assert '--user "$(id -u):$(id -g)"' in cmd and "-e HOME=/tmp" in cmd, cmd


# --- opaque trust + host isolation ----------------------------------------------

def test_substrate_override_trusts_in_container_paths_only_when_reachable():
    with _patch() as p:
        _container_host(p)
        assert solvers._substrate_override(_C_CCX, is_dir=False) == _C_CCX
        assert solvers._substrate_override(_C_PRECICE_LIB, is_dir=True) == _C_PRECICE_LIB
        assert solvers._substrate_override("opt/fsi/bin/ccx_preCICE", is_dir=False) is None
        _container_host(p, engine_on_path=())              # engine missing
        assert solvers._substrate_override(_C_CCX, is_dir=False) is None


def test_openfoam_resolves_via_container_from_the_override():
    with _patch() as p:
        _container_host(p)
        p.env(ANKUSDRIVE_OPENFOAM_PATH=_C_BIN)
        info = solvers.find_solver("openfoam")
        assert info["available"] is True and info["status"] == "ok", info
        assert info["path"] == _C_BIN and info["via"] == "container", info
        r = solvers.require_solver("openfoam")
        assert r["ok"] is True and r["via"] == "container", r


def test_a_host_install_is_ignored_under_container():
    """The host has a real OpenFOAM, FSI stack and openInjMoldSim at their default
    locations — none of which `<engine> exec` would run. Under `container` every
    substrate resolver must ignore them; with the substrate unset (native Linux) the
    same filesystem resolves as before. The control proves the fake host is real."""
    home = os.path.expanduser("~")
    host = {
        "/usr/bin/simpleFoam",
        "/usr/lib/openfoam/openfoam2512/etc/bashrc",
        f"{home}/precice-serial/lib/libprecice.so.3",
        f"{home}/OpenFOAM/u-v2512/platforms/linux64GccDPInt32Opt/lib/"
        "libpreciceAdapterFunctionObject.so",
        f"{home}/calculix-adapter/bin/ccx_preCICE",
        f"{home}/opt/openInjMoldSim/x/bin/openInjMoldSim",
        f"{home}/OpenFOAM/OpenFOAM-7/etc/bashrc",
    }
    resolvers = ("openfoam_bashrc", "precice_lib_dir", "openfoam_adapter_lib_dir",
                 "fsi_openfoam_bashrc", "openinjmoldsim_bashrc", "ccx_precice_bin")
    with _patch() as p:
        _container_host(p, host_files=host, engine_on_path=("docker", "openInjMoldSim"))
        os.environ.pop("ANKUSDRIVE_SUBSTRATE")               # control: native Linux
        assert solvers.find_solver("openfoam")["available"] is True
        for fn in resolvers:
            assert getattr(solvers, fn)() is not None, f"control: {fn} found nothing"
        assert solvers.openinjmoldsim_bin() is not None

        p.env(ANKUSDRIVE_SUBSTRATE="container")
        solvers._ctr_info_cache.clear()
        p.set(solvers, "_container_inspect", lambda eng, name: None)
        info = solvers.find_solver("openfoam")
        assert info["available"] is False, ("host openfoam leaked into container", info)
        assert solvers.find_solver("precice")["available"] is False
        for fn in resolvers:
            assert getattr(solvers, fn)() is None, f"{fn} resolved a HOST path under container"
        assert solvers.openinjmoldsim_bin() is None


def test_fsi_and_molding_resolvers_take_in_container_overrides():
    with _patch() as p:
        _container_host(p)
        p.env(ANKUSDRIVE_CCX_PRECICE=_C_CCX, ANKUSDRIVE_PRECICE_LIB=_C_PRECICE_LIB,
              ANKUSDRIVE_OPENFOAM_ADAPTER_LIB=_C_ADAPTER_LIB,
              ANKUSDRIVE_FSI_OPENFOAM_BASHRC=_C_BASHRC,
              ANKUSDRIVE_OPENINJMOLDSIM=_C_OIMS,
              ANKUSDRIVE_OPENINJMOLDSIM_BASHRC=_C_OIMS_BASHRC)
        st = solvers.fsi_stack_status()
        assert st["ok"] is True, st
        assert (st["ccx_precice"], st["precice_lib"], st["openfoam_adapter_lib"],
                st["openfoam_bashrc"]) == (_C_CCX, _C_PRECICE_LIB, _C_ADAPTER_LIB, _C_BASHRC)
        assert solvers.find_solver("precice")["via"] == "container"
        assert solvers.openinjmoldsim_bin() == _C_OIMS
        assert solvers.openinjmoldsim_bashrc() == _C_OIMS_BASHRC
        # FSI's bashrc falls back to the general override, never a host glob
        os.environ.pop("ANKUSDRIVE_FSI_OPENFOAM_BASHRC")
        p.env(ANKUSDRIVE_OPENFOAM_BASHRC=_C_BASHRC)
        assert solvers.fsi_openfoam_bashrc() == _C_BASHRC


# --- state + hints --------------------------------------------------------------

def test_container_state_reads_real_inspect_shapes_and_degrades():
    running = json.dumps({"Status": "running", "Running": True, "Pid": 42})
    exited = json.dumps({"Status": "exited", "Running": False, "ExitCode": 0})
    created = json.dumps({"Status": "created", "Running": False})
    for payload, expect in ((running, "running"), (exited, "stopped"),
                            (created, "stopped"), (None, "absent"), ("", "absent"),
                            ("not json", "absent"), ("[1, 2]", "absent"),
                            (json.dumps({"Status": "dead"}), "absent")):
        with _patch() as p:
            _container_host(p)
            p.set(solvers, "_container_inspect", lambda eng, name, _p=payload: _p)
            assert solvers.container_state() == expect, (payload, expect)
    with _patch() as p:                                   # engine missing: never reads
        _container_host(p, engine_on_path=())
        p.set(solvers, "_container_inspect",
              lambda *a: (_ for _ in ()).throw(AssertionError("read without an engine")))
        assert solvers.container_state() == "absent"


def test_inspect_read_is_bounded_and_never_takes_stdin():
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw

        class R:
            returncode, stdout = 0, '{"Running": true}'
        return R()
    with _patch() as p:
        p.set(subprocess, "run", fake_run)
        out = solvers._container_inspect_exec("podman", "foam", 3.0)
    assert out == '{"Running": true}'
    assert seen["argv"] == ["podman", "container", "inspect", "--format",
                            "{{json .State}}", "foam"], seen["argv"]
    assert seen["kw"].get("stdin") is subprocess.DEVNULL, seen["kw"]
    assert seen["kw"].get("timeout") == 3.0, seen["kw"]


def test_openfoam_unwired_hint_tracks_container_state():
    """Absent -> the `run` line; stopped -> `<engine> start`; running -> export the
    in-container paths. A host bashrc never decides the hint under container."""
    payloads = {"absent": None,
                "stopped": json.dumps({"Status": "exited", "Running": False}),
                "running": json.dumps({"Status": "running", "Running": True})}
    needles = {"absent": "docker run -d --name ankusdrive-solvers",
               "stopped": "docker start ankusdrive-solvers",
               "running": "only this shell's env is missing"}
    for state, payload in payloads.items():
        with _patch() as p:
            _container_host(p, host_files={"/usr/lib/openfoam/openfoam2512/etc/bashrc"})
            p.set(solvers, "_container_inspect", lambda eng, name, _p=payload: _p)
            info = solvers.find_solver("openfoam")
            assert info["status"] == "unwired", (state, info)
            assert needles[state] in info["wire_hint"], (state, info["wire_hint"])
            assert "ANKUSDRIVE_OPENFOAM_BASHRC=/usr/lib" not in info["wire_hint"], \
                ("a host bashrc decided the container hint", info["wire_hint"])
    with _patch() as p:
        _container_host(p, engine_on_path=())
        info = solvers.find_solver("openfoam")
        assert info["found_at"] == "container substrate (no `docker` on PATH)", info


def test_capabilities_and_doctor_name_the_container_substrate():
    from ankusdrive import doctor
    with _patch() as p:
        _container_host(p)
        p.set(solvers, "_container_inspect", lambda eng, name: None)
        assert solvers.capabilities()["substrate"] == "container"
    caps = {"solvers": {"openfoam": {"name": "openfoam", "available": True,
                                     "status": "ok", "kind": "binary", "family": "cfd",
                                     "path": _C_BIN, "via": "container"}},
            "families": {"cfd": {"solvers": ["openfoam"], "available": ["openfoam"],
                                 "unwired": [], "any_available": True}}}
    assert "ready via openfoam (in container)" in "\n".join(doctor._fmt_solvers(caps))


def test_fsi_sweeps_participants_through_the_container():
    """runs_in_substrate() is true under container, so FSI's _stop_participant kills
    each participant by pidfile THROUGH `<engine> exec -w <workdir>` — killing the
    `docker exec` client alone would leave the solver running in the container."""
    from ankusdrive.analysis import fsi_case

    calls = []

    class _FakePopen:
        def __init__(self, argv, **kw):
            calls.append(("popen", argv, kw))

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    class _FakeRun:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kw):
        calls.append(("run", argv, kw))
        return _FakeRun()

    with _patch() as p:
        _container_host(p)
        p.env(ANKUSDRIVE_CCX_PRECICE=_C_CCX, ANKUSDRIVE_PRECICE_LIB=_C_PRECICE_LIB,
              ANKUSDRIVE_OPENFOAM_ADAPTER_LIB=_C_ADAPTER_LIB,
              ANKUSDRIVE_FSI_OPENFOAM_BASHRC=_C_BASHRC)
        p.set(fsi_case.subprocess, "run", fake_run)
        p.set(fsi_case.subprocess, "Popen", _FakePopen)
        p.set(fsi_case.time, "sleep", lambda *_: None)
        with tempfile.TemporaryDirectory() as case:
            fluid = os.path.join(case, "fluid-openfoam")
            solid = os.path.join(case, "solid-calculix")
            os.makedirs(fluid)
            os.makedirs(solid)
            fsi_case.run_coupled_fsi(case, timeout_s=5)

    launches = [c for c in calls if c[0] == "popen"]
    assert len(launches) == 2, launches
    for _, argv, kw in launches:
        assert argv[:3] == ["docker", "exec", "-w"], argv
        assert argv[3] in (fluid, solid) and argv[4] == "ankusdrive-solvers", argv
    sweeps = [c for c in calls if c[0] == "run" and "kill -TERM" in c[1][-1]]
    assert {c[1][3] for c in sweeps} == {fluid, solid}, sweeps
    assert all(c[1][:2] == ["docker", "exec"] for c in sweeps), sweeps


# --- beyond OpenFOAM: YADE / Elmer / openEMS / Bempp (#419) -----------------------

_RUNNING = json.dumps({"Status": "running", "Running": True})
_C_ELMER = "/opt/elmer/bin/ElmerSolver"
_C_YADE = "/opt/yade/bin/yade"
_C_OPENEMS_PY = "/opt/venv-openems/bin/python"
_C_BEMPP_PY = "/opt/venv-bempp/bin/python"


def _manifest(solvers=(), excluded=()):
    """The JSON an image writes at /etc/ankusdrive/solvers.json (#422 part C)."""
    return json.dumps({
        "schema": 1, "architecture": "amd64",
        "solvers": {s: {"path": f"/opt/{s}", "description": s} for s in solvers},
        "excluded": {s: {"reason": "not selected when this image was built"}
                     for s in excluded},
    })


def _fake_container(p, *, holds=(), pythons=(), state=_RUNNING, manifest=None):
    """A container whose filesystem holds the executables ``holds``, whose interpreters
    ``pythons`` import their solver's modules, and which publishes ``manifest`` (None =
    an older image with no manifest). Records every probe."""
    probes = []

    def probe(engine, name, argv):
        probes.append(argv)
        if argv[0] == "cat":                         # the image's own manifest
            return (0, manifest) if manifest else (1, "")
        if argv[0] == "sh":                          # _container_binary's command -v scan
            script = argv[2]
            for path in holds:
                if f" {os.path.basename(path)}" in script.split(";")[0]:
                    return 0, path + "\n"
            return 1, ""
        if argv[0] == "test":
            return (0, "") if argv[2] in holds else (1, "")
        if len(argv) == 3 and argv[1] == "-c":       # find_spec in an interpreter
            return (0, "") if argv[0] in pythons else (1, "")
        raise AssertionError(f"unexpected probe {argv}")
    p.set(solvers, "_container_inspect", lambda eng, name, _s=state: _s)
    p.set(solvers, "_container_probe", probe)
    return probes


def test_yade_and_elmer_resolve_by_probing_the_container():
    with _patch() as p:
        _container_host(p)
        probes = _fake_container(p, holds=(_C_ELMER, _C_YADE))
        for name, path in (("elmer", _C_ELMER), ("yade", _C_YADE)):
            info = solvers.find_solver(name)
            assert info["available"] and info["path"] == path, info
            assert info["via"] == "container", info
            assert solvers.routes_through_container(name) is True
        assert all(a[:2] == ("sh", "-c") or a[0] == "cat" for a in probes), probes


def test_an_override_is_honoured_when_the_container_really_has_it():
    """An override wins over the PATH scan — it is how a user points at a solver the
    image put somewhere else. Since #422 it is checked inside the container first, so
    only a path that exists there wins; the scan is skipped either way."""
    with _patch() as p:
        _container_host(p)
        probes = _fake_container(p, holds=("/usr/bin/ElmerSolver", _C_YADE))
        p.env(ANKUSDRIVE_ELMER_PATH="/usr/bin/ElmerSolver", ANKUSDRIVE_YADE=_C_YADE)
        assert solvers.find_solver("elmer")["path"] == "/usr/bin/ElmerSolver"
        assert solvers.find_solver("yade")["path"] == _C_YADE     # the image's own env name
        assert not [a for a in probes if a[0] == "sh"], \
            ("an honoured override must not fall through to the PATH scan", probes)


def test_host_elmer_and_yade_are_ignored_under_container_but_not_natively():
    host = {"/usr/bin/ElmerSolver", "/usr/bin/yade"}
    with _patch() as p:
        _container_host(p, host_files=host)
        os.environ.pop("ANKUSDRIVE_SUBSTRATE")                  # control: native Linux
        p.set(solvers, "_container_probe",
              lambda *a: (_ for _ in ()).throw(AssertionError("probed natively")))
        for name in ("elmer", "yade"):
            info = solvers.find_solver(name)
            assert info["available"] and "via" not in info, info
            assert solvers.routes_through_container(name) is False
        p.env(ANKUSDRIVE_SUBSTRATE="container")
        _fake_container(p)                                      # running, holds nothing
        for name in ("elmer", "yade"):
            info = solvers.find_solver(name)
            assert info["available"] is False, ("host install leaked", info)


def test_an_image_without_the_solver_says_so_per_container_state():
    """Absent / stopped / running-but-missing each get their own fix; a host install
    hint (apt, source build) never answers a container-substrate miss."""
    needles = {None: "docker run -d --name ankusdrive-solvers",
               json.dumps({"Status": "exited", "Running": False}): "docker start",
               _RUNNING: "did not resolve inside it"}
    for state, needle in needles.items():
        for name, env in (("elmer", "ANKUSDRIVE_ELMER_PATH"),
                          ("openems", "ANKUSDRIVE_OPENEMS_PYTHON")):
            with _patch() as p:
                _container_host(p)
                _fake_container(p, state=state)
                info = solvers.find_solver(name)
                assert info["status"] == "unwired", (state, info)
                assert needle in info["wire_hint"], (state, info["wire_hint"])
                if state == _RUNNING:
                    assert env in info["wire_hint"], info["wire_hint"]


def test_openems_and_bempp_are_asked_inside_the_container():
    with _patch() as p:
        _container_host(p)
        probes = _fake_container(p, pythons=(_C_OPENEMS_PY, _C_BEMPP_PY))
        for name, py in (("openems", _C_OPENEMS_PY), ("bempp", _C_BEMPP_PY)):
            info = solvers.find_solver(name)
            assert info["available"] and info["path"] == py, info
            assert info["via"] == "container", info
        interp = [a for a in probes if a[0] not in ("cat", "sh", "test")]
        assert {a[0] for a in interp} == {_C_OPENEMS_PY, _C_BEMPP_PY}, probes
        assert all("find_spec" in a[2] for a in interp), probes
        # a relative override is never trusted — and never stepped over either (#443):
        # it is reported, and the container is not even asked about it
        probes.clear()
        p.env(ANKUSDRIVE_OPENEMS_PYTHON="venv/bin/python")
        assert solvers.solver_python("openems") is None
        assert [a for a in probes if a[0] not in ("cat", "test")] == [], probes
        info = solvers.find_solver("openems")
        assert "ANKUSDRIVE_OPENEMS_PYTHON=venv/bin/python is not an absolute path" \
            in info["wire_hint"], info["wire_hint"]
        # kraken is not container-routed: the host decides, the container is never asked
        probes.clear()
        solvers.find_solver("kraken")
        assert probes == [], probes


def test_solver_argv_wraps_only_routed_solvers_and_takes_stdin_only_when_asked():
    with _patch() as p:
        _container_host(p)
        assert solvers.solver_argv("elmer", ["/opt/elmer/bin/ElmerSolver", "case.sif"],
                                   "/tmp/c") == [
            "docker", "exec", "-w", "/tmp/c", "ankusdrive-solvers",
            "/opt/elmer/bin/ElmerSolver", "case.sif"]
        argv = solvers.solver_argv("bempp", [_C_BEMPP_PY, "/tmp/r.py"], stdin=True)
        assert argv == ["docker", "exec", "-i",
                        "-e", "NUMBA_CACHE_DIR=/tmp/ankusdrive-numba-cache",
                        "ankusdrive-solvers", _C_BEMPP_PY, "/tmp/r.py"], argv
        assert "-t" not in argv
        assert solvers.solver_argv("kraken", ["py", "r.py"]) == ["py", "r.py"]
        os.environ.pop("ANKUSDRIVE_SUBSTRATE")                  # native: identity
        assert solvers.solver_argv("elmer", ["ElmerSolver", 1], "/c", stdin=True) == [
            "ElmerSolver", "1"]


def test_stage_runner_puts_the_runner_under_the_mounted_scratch():
    runner = Path(__file__).resolve().parent.parent / "ankusdrive" / "bempp_runner.py"
    with tempfile.TemporaryDirectory() as scratch, _patch() as p:
        p.set(solvers, "platform", _fake_platform("Linux"))
        p.set(solvers.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None)
        p.set(tempfile, "tempdir", scratch)
        assert solvers.stage_runner("bempp", str(runner)) == str(runner)   # native
        p.env(ANKUSDRIVE_SUBSTRATE="container")
        staged = solvers.stage_runner("bempp", str(runner))
        assert staged.startswith(os.path.join(scratch, "ankusdrive-runners")), staged
        assert Path(staged).read_bytes() == runner.read_bytes()
        assert solvers.stage_runner("bempp", str(runner)) == staged        # idempotent
        # a planted/tampered copy at the predictable path is never executed
        Path(staged).write_text("import os; os.system('evil')\n", encoding="utf-8")
        restaged = solvers.stage_runner("bempp", str(runner))
        assert Path(restaged).read_bytes() == runner.read_bytes(), restaged
        if os.name != "posix" or os.getuid() == 0:
            return                           # dir modes don't bind on Windows or for root
        os.chmod(os.path.dirname(staged), 0o500)                 # can't rewrite in place
        try:
            Path(staged).chmod(0o600)
            os.chmod(os.path.dirname(staged), 0o700)
            Path(staged).write_text("tampered\n", encoding="utf-8")
            os.chmod(os.path.dirname(staged), 0o500)
            fallback = solvers.stage_runner("bempp", str(runner))
            assert fallback != staged and "ankusdrive-runner-" in fallback, fallback
            assert Path(fallback).read_bytes() == runner.read_bytes()
        finally:
            os.chmod(os.path.dirname(staged), 0o700)
        assert solvers.stage_runner("kraken", str(runner)) == str(runner)  # not routed


def test_elmer_siblings_are_in_container_paths():
    with _patch() as p:
        _container_host(p, engine_on_path=("docker", "ElmerGrid"))   # a HOST ElmerGrid
        probes = _fake_container(p, holds=(_C_ELMER, "/opt/elmer/bin/ViewFactors"))
        vf = solvers.sibling_bin(_C_ELMER, "ViewFactors", "elmer")
        assert vf == "/opt/elmer/bin/ViewFactors"
        assert solvers.sibling_bin(_C_ELMER, "ElmerGrid", "elmer") == "/opt/elmer/bin/ElmerGrid"
        assert solvers.container_file_exists(vf) is True
        assert solvers.container_file_exists("/opt/elmer/bin/ElmerGrid") is False
        assert ("test", "-x", vf) in probes, probes


def test_container_probe_is_bounded_and_never_takes_stdin():
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw

        class R:
            returncode, stdout = 0, "/opt/yade/bin/yade\n"
        return R()
    with _patch() as p:
        p.set(subprocess, "run", fake_run)
        out = solvers._container_probe_exec("podman", "foam", ("sh", "-c", "x"), 7.0)
    assert out == (0, "/opt/yade/bin/yade\n")
    assert seen["argv"] == ["podman", "exec", "foam", "sh", "-c", "x"], seen["argv"]
    assert seen["kw"].get("stdin") is subprocess.DEVNULL and seen["kw"]["timeout"] == 7.0


def test_worker_launches_routed_solvers_only_through_solver_argv():
    """worker.py needs FreeCAD to import, so this pins the wiring at the source: no
    ElmerSolver / ElmerGrid / ViewFactors / yade / runner launch bypasses solver_argv,
    and every runner is staged where the container can read it."""
    src = (Path(__file__).resolve().parent.parent / "ankusdrive" / "worker.py").read_text(encoding="utf-8")
    for bypass in ("subprocess.run([elmer_bin", "subprocess.run([vf_bin",
                   "subprocess.run(grid_argv", "[yade_exe, \"-x\"",
                   "os.path.isfile(elmergrid)"):
        hits = [ln for ln in src.splitlines()
                if bypass in ln and "solver_argv" not in ln]
        assert not hits, (bypass, hits)
    for name, runner in (("yade", "dem_gpl_runner.py"), ("openems", "em_fullwave_gpl_runner.py"),
                         ("bempp", "bempp_runner.py")):
        assert f'solvers.stage_runner("{name}"' in src, name
        assert f'solvers.solver_argv("{name}"' in src, name
    assert src.count('_run_solver("elmer"') >= 20, src.count('_run_solver("elmer"')
    # A gate that stats a sibling on the HOST answers False for every in-container
    # binary, so the leg it guards SKIPs while the solver is right there (the
    # meshbridge Elmer leg did exactly that in Lane B). solver_file_exists asks
    # wherever the solver runs.
    tests = Path(__file__).resolve().parent
    for f in sorted(tests.glob("test_*.py")):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines()):
            if "sibling_bin" not in line:
                continue
            window = "\n".join(text.splitlines()[max(0, i - 2):i + 1])
            assert "os.path.isfile" not in window, (
                f"{f.name}:{i + 1} stats a sibling on the host — use "
                "solvers.solver_file_exists(<solver>, …)")


# --- the image states what it contains (#422 part C) ------------------------------

def test_a_solver_the_image_excludes_is_never_reported_ready():
    """With a partial image, trusting an in-container path means `doctor` says "ready
    via yade (in container)" and the solve dies minutes later. The manifest is read
    FIRST, so an omitted solver is a named miss — even when an override names it and
    even when a probe would have found something."""
    with _patch() as p:
        _container_host(p)
        _fake_container(p, holds=(_C_ELMER, _C_YADE),
                        pythons=(_C_OPENEMS_PY,),
                        manifest=_manifest(solvers=("openfoam", "elmer"),
                                           excluded=("yade", "openems", "bempp")))
        p.env(ANKUSDRIVE_YADE=_C_YADE)              # an override cannot override absence
        for name in ("yade", "openems"):
            info = solvers.find_solver(name)
            assert info["available"] is False, (name, info)
            assert info["status"] == "unwired", info
            assert "does not include" in info["wire_hint"], info["wire_hint"]
            assert "build_solver_image.sh" in info["wire_hint"], info["wire_hint"]
        # what the image DOES carry still resolves
        assert solvers.find_solver("elmer")["available"] is True


def test_an_image_without_a_manifest_still_probes():
    """Every image built before #422 has no manifest. Discovery must fall back to
    probing rather than treating a missing file as "carries nothing"."""
    with _patch() as p:
        _container_host(p)
        _fake_container(p, holds=(_C_ELMER, _C_YADE), manifest=None)
        assert solvers.container_manifest() is None
        assert solvers.container_excludes("yade") is None
        assert solvers.find_solver("yade")["available"] is True
    with _patch() as p:                              # garbage parses to "no manifest"
        _container_host(p)
        _fake_container(p, holds=(_C_YADE,), manifest="{not json")
        assert solvers.container_manifest() is None
        assert solvers.find_solver("yade")["available"] is True


def test_the_fsi_stack_maps_onto_its_manifest_name():
    """The registry calls the stack `precice` (its linchpin binary); the image records
    it as `fsi`. A mismatch here would report the stack excluded on every image."""
    with _patch() as p:
        _container_host(p)
        _fake_container(p, manifest=_manifest(solvers=("openfoam", "fsi")))
        assert solvers.container_excludes("precice") is None
        _fake_container(p, manifest=_manifest(solvers=("openfoam",), excluded=("fsi",)))
        assert solvers.container_excludes("precice")


def test_a_host_path_override_is_caught_instead_of_trusted():
    """The config.toml trap: a native install's `openfoam_bashrc` is an absolute path,
    so it is trusted as an in-container path and sourced in a container that has no
    such file — the solve then dies with `blockMesh: command not found`, naming
    nothing useful. A RUNNING container is asked instead."""
    host_bashrc = "/usr/lib/openfoam/openfoam2606/etc/bashrc"   # a HOST install's path
    with _patch() as p:
        _container_host(p)
        _fake_container(p, holds=(_C_BASHRC, _C_BIN))
        p.env(ANKUSDRIVE_OPENFOAM_BASHRC=host_bashrc, ANKUSDRIVE_OPENFOAM_PATH=_C_BIN)
        assert solvers.openfoam_bashrc() is None, "a host path was trusted as in-container"
        p.env(ANKUSDRIVE_OPENFOAM_BASHRC=_C_BASHRC)              # what the image publishes
        assert solvers.openfoam_bashrc() == _C_BASHRC
        # and the hint names the trap rather than sending the reader hunting
        p.env(ANKUSDRIVE_YADE="/usr/local/bin/yade")             # host path, not in image
        _fake_container(p, holds=())
        info = solvers.find_solver("yade")
        assert info["status"] == "unwired", info
        assert "does not exist inside container" in info["wire_hint"], info["wire_hint"]


def test_an_unreachable_container_still_trusts_an_override():
    """Only a RUNNING container can be asked. Stopped or absent, the override is
    trusted exactly as before — otherwise every path would go unresolvable the moment
    the container stops, which is not what the user needs to be told."""
    with _patch() as p:
        _container_host(p)
        p.set(solvers, "_container_inspect", lambda eng, name: None)   # absent
        p.env(ANKUSDRIVE_OPENFOAM_BASHRC=_C_BASHRC)
        assert solvers.openfoam_bashrc() == _C_BASHRC

# --- an override the container cannot use is reported, never stepped over (#443) ---

def test_a_bogus_binary_override_is_reported_not_trusted_or_discarded():
    """#443: under `container`, ANKUSDRIVE_ELMER_PATH=/bogus once came back "ready via
    elmer (in container)" and the solve failed — with the ElmerGrid/ViewFactors legs
    SKIPping beside a path that does not exist. The override is now asked about, and
    one that names nothing in the image is a named miss. It is NOT quietly replaced
    by the image's own copy either: an override that is set is what the user meant,
    so the hint names it, says why, and says what clearing it would give them."""
    for name, var, own in (("elmer", "ANKUSDRIVE_ELMER_PATH", _C_ELMER),
                           ("yade", "ANKUSDRIVE_YADE", _C_YADE)):       # the alias too
        with _patch() as p:
            _container_host(p)
            _fake_container(p, holds=(_C_ELMER, _C_YADE))
            p.env(**{var: "/bogus/" + name})
            info = solvers.find_solver(name)
            assert info["available"] is False, ("a bogus override was trusted", info)
            assert info["status"] == "unwired", info
            hint = info["wire_hint"]
            assert f"{var}=/bogus/{name} does not exist inside container" in hint, hint
            assert f"Clear it and the image's own {own} is used" in hint, hint
            assert solvers.routes_through_container(name) is True


def test_a_bogus_interpreter_override_is_reported_the_same_way():
    """The interpreter solvers used to probe the override, fail, and fall back to the
    image's venv without a word — `doctor` said ready with no hint the setting was
    ignored. Same decision as the binaries now: reported, never discarded."""
    for name, var, own in (("bempp", "ANKUSDRIVE_BEMPP_PYTHON", _C_BEMPP_PY),
                           ("openems", "ANKUSDRIVE_OPENEMS_PYTHON", _C_OPENEMS_PY)):
        with _patch() as p:
            _container_host(p)
            _fake_container(p, pythons=(_C_OPENEMS_PY, _C_BEMPP_PY))
            p.env(**{var: "/home/me/.venv-host/bin/python"})     # a HOST venv
            assert solvers.solver_python(name) is None
            info = solvers.find_solver(name)
            assert info["available"] is False, info
            hint = info["wire_hint"]
            assert f"{var}=/home/me/.venv-host/bin/python does not exist inside" in hint, hint
            assert f"the image's own {own} is used" in hint, hint
        with _patch() as p:                  # present in the image, but the wrong python
            _container_host(p)
            _fake_container(p, holds=("/usr/bin/python3",), pythons=(own,))
            p.env(**{var: "/usr/bin/python3"})
            info = solvers.find_solver(name)
            assert info["available"] is False, info
            assert "exists inside container" in info["wire_hint"], info["wire_hint"]
            assert "does not import" in info["wire_hint"], info["wire_hint"]


def test_a_good_override_still_wins_and_an_unreachable_container_still_trusts():
    """The two cases the report must not disturb: an override the image really has is
    used as-is (no PATH scan), and with the container stopped there is nothing to ask,
    so the override is trusted exactly as before (#422)."""
    with _patch() as p:
        _container_host(p)
        _fake_container(p, holds=("/srv/elmer/ElmerSolver", _C_ELMER),
                        pythons=("/srv/bempp/python", _C_BEMPP_PY))
        p.env(ANKUSDRIVE_ELMER_PATH="/srv/elmer/ElmerSolver",
              ANKUSDRIVE_BEMPP_PYTHON="/srv/bempp/python")
        assert solvers.find_solver("elmer")["path"] == "/srv/elmer/ElmerSolver"
        assert solvers.solver_python("bempp") == "/srv/bempp/python"
    with _patch() as p:
        _container_host(p)
        _fake_container(p, state=json.dumps({"Status": "exited", "Running": False}))
        p.env(ANKUSDRIVE_ELMER_PATH="/bogus/elmer")
        assert solvers._vm_binary_path("elmer", solvers._spec("elmer")) == "/bogus/elmer"


# --- is this image ours? (#423) ---------------------------------------------------

def _image(p, *, ref="ghcr.io/gchen19/ankusdrive-solvers:latest",
           digest="sha256:" + "a" * 64, verify=None, manifest=None):
    """A running container created from ``ref``@``digest``, with ``verify`` standing in
    for what `gh attestation verify` returns: (returncode, output)."""
    _container_host(p)
    _fake_container(p, manifest=manifest)
    p.set(solvers, "_container_inspect_config",
          lambda eng, name: json.dumps(
              {"Image": ref,
               # docker records "<repo>@<digest>" — build it the way docker does, or
               # the fake teaches the test a shape the real thing never produces
               "RepoDigests": [f"{solvers.image_base(ref)}@{digest}"] if digest else []}))
    p.set(solvers, "_gh_verify_exec", lambda r, t: verify)


def test_an_image_ref_reduces_to_its_repository():
    """Both shapes that matter break naive ':' splitting: a digest-pinned ref (what
    users are told to pin) and a registry with a port. Getting this wrong asked the
    verifier about `repo@sha256@sha256:…`, which it rejects as malformed — found by
    running the check against a real digest-pinned container."""
    cases = {
        "ghcr.io/o/i:latest": "ghcr.io/o/i",
        "ghcr.io/o/i@sha256:" + "a" * 64: "ghcr.io/o/i",
        "ghcr.io/o/i": "ghcr.io/o/i",
        "localhost:5000/o/i:tag": "localhost:5000/o/i",
        "localhost:5000/o/i@sha256:" + "b" * 64: "localhost:5000/o/i",
    }
    for ref, want in cases.items():
        assert solvers.image_base(ref) == want, (ref, solvers.image_base(ref))


def test_a_digest_pinned_container_is_verified_by_its_repository():
    """The container a careful user runs names a digest, not a tag."""
    asked = {}
    with _patch() as p:
        _image(p, ref="ghcr.io/gchen19/ankusdrive-solvers@sha256:" + "c" * 64,
               digest="sha256:" + "c" * 64)
        p.set(solvers, "_gh_verify_exec",
              lambda r, t: (asked.setdefault("ref", r), (0, "ok"))[1])
        assert solvers.verify_container_image()["status"] == "verified"
    assert asked["ref"] == "ghcr.io/gchen19/ankusdrive-solvers@sha256:" + "c" * 64, asked


def test_a_signed_image_verifies():
    with _patch() as p:
        _image(p, verify=(0, "Verification succeeded!"))
        v = solvers.verify_container_image()
        assert v["status"] == "verified", v
        assert v["repo"] == "gchen19/AnkusDrive"
        assert v["workflow"].endswith("heavy-image.yml")


def test_a_custom_image_is_unsigned_not_failed_and_can_be_allowed():
    """A user building their own image is the normal case, not an attack. It warns
    once, says why, and ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE=1 accepts it for good."""
    built = _manifest(solvers=("openfoam",))
    built = json.dumps({**json.loads(built),
                        "built": {"source": "local", "commit": "abc1234"}})
    with _patch() as p:
        _image(p, verify=(1, "no attestations found for subject"), manifest=built)
        v = solvers.verify_container_image()
        assert v["status"] == "unsigned", v
        assert v["allowed_by_config"] is False
        assert v["self_declared"] == {"source": "local", "commit": "abc1234"}, v
        p.env(ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE="1")
        assert solvers.verify_container_image()["allowed_by_config"] is True
    # an image built with no registry digest at all cannot be verified either
    with _patch() as p:
        _image(p, digest=None, verify=(0, "should not be consulted"))
        v = solvers.verify_container_image()
        assert v["status"] == "unsigned" and "built locally" in v["reason"], v


def test_an_attestation_from_another_repo_is_a_mismatch_and_is_never_silenced():
    """The case signing exists to catch: an image that carries provenance, just not
    ours. The override is for YOUR images, not for one impersonating this repo."""
    from ankusdrive import doctor
    with _patch() as p:
        _image(p, verify=(1, "verification failed: certificate identity does not match"))
        p.env(ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE="1")
        v = solvers.verify_container_image()
        assert v["status"] == "mismatch", v
        out = "\n".join(doctor._fmt_container_image(
            {"ref": "x:latest", "digest": "sha256:" + "a" * 64, "verification": v}))
        assert "NOT signed" in out and "[warn]" in out, out
        assert "allowed by" not in out, ("an override silenced a mismatch", out)


def test_an_unrunnable_check_is_never_reported_as_authentic():
    """No gh, no auth, no network: the honest answer is "not checked", never a pass."""
    with _patch() as p:
        _image(p, verify=None)                       # gh missing
        v = solvers.verify_container_image()
        assert v["status"] == "unavailable" and "gh" in v["reason"], v
    with _patch() as p:
        _image(p, verify=(1, "error: gh auth login required (HTTP 401)"))
        v = solvers.verify_container_image()
        assert v["status"] == "unavailable" and "authenticated" in v["reason"], v
    with _patch() as p:                              # an unfamiliar failure
        _image(p, verify=(1, "some future gh message"))
        assert solvers.verify_container_image()["status"] == "unavailable"
    with _patch() as p:                              # gh too old: it prints its usage
        _image(p, verify=(1, 'unknown command "attestation" for "gh"\n\nUsage: gh '
                             "<command>\n\nAvailable commands:\n  alias\n  api\n"))
        v = solvers.verify_container_image()
        assert v["status"] == "unavailable", v
        assert "2.49" in v["reason"] and "Available commands" not in v["reason"], \
            ("gh's whole usage text was quoted back at the reader", v["reason"])
        assert len(v["reason"]) < 200, v["reason"]


def test_verification_never_runs_unless_asked():
    """It is the one solver call that reaches the network, so no default path — and
    no MCP status call — may trigger it."""
    from ankusdrive import doctor
    with _patch() as p:
        _image(p, verify=(0, "ok"))
        p.set(solvers, "_gh_verify_exec",
              lambda *a: (_ for _ in ()).throw(AssertionError("verified without asking")))
        assert "verification" not in (doctor.build_report(
            probe_version=False, mcp_serve=False)["container_image"] or {})


def test_doctor_phrases_each_state_for_the_person_reading_it():
    from ankusdrive import doctor
    base = {"ref": "ghcr.io/gchen19/ankusdrive-solvers:latest", "digest": "sha256:" + "b" * 64}
    def render(v):
        return "\n".join(doctor._fmt_container_image({**base, "verification": v}))
    verified = render({"status": "verified", "repo": "gchen19/AnkusDrive",
                       "workflow": ".github/workflows/heavy-image.yml", "reason": "",
                       "allowed_by_config": False, "self_declared": None})
    assert "[ok]" in verified and "signed by gchen19/AnkusDrive" in verified
    unsigned = render({"status": "unsigned", "reason": "no attestation was published",
                       "repo": "gchen19/AnkusDrive", "workflow": "w",
                       "allowed_by_config": False,
                       "self_declared": {"source": "local", "commit": "abc1234"}})
    assert "built here" in unsigned and "abc1234" in unsigned, unsigned
    assert "ANKUSDRIVE_ALLOW_UNVERIFIED_IMAGE=1" in unsigned
    allowed = render({"status": "unsigned", "reason": "x", "repo": "r", "workflow": "w",
                      "allowed_by_config": True, "self_declared": None})
    assert "[ok]" in allowed and "allowed by" in allowed, allowed

# --- runner ---------------------------------------------------------------------

def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")
    print()
    total = len(_discover())
    if failures:
        print(f"== {len(failures)}/{total} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        return 1
    print(f"== {total}/{total} passed  ({time.time() - t_suite:.1f}s) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())

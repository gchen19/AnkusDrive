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
            "ANKUSDRIVE_OPENEMS_PYTHON", "ANKUSDRIVE_BEMPP_PYTHON")


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
        assert '-v "$TMPDIR:$TMPDIR"' in cmd and "ghcr.io/gchen19/ankusdrive-heavy" in cmd
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


def _fake_container(p, *, holds=(), pythons=(), state=_RUNNING):
    """A container whose filesystem holds the executables ``holds`` and whose
    interpreters ``pythons`` import their solver's modules. Records every probe."""
    probes = []

    def probe(engine, name, argv):
        probes.append(argv)
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
        assert all(a[:2] == ("sh", "-c") for a in probes), probes


def test_a_trusted_override_skips_the_probe():
    with _patch() as p:
        _container_host(p)
        probes = _fake_container(p)
        p.env(ANKUSDRIVE_ELMER_PATH="/usr/bin/ElmerSolver", ANKUSDRIVE_YADE=_C_YADE)
        assert solvers.find_solver("elmer")["path"] == "/usr/bin/ElmerSolver"
        assert solvers.find_solver("yade")["path"] == _C_YADE     # the image's own env name
        assert probes == [], probes


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
        assert {a[0] for a in probes} == {_C_OPENEMS_PY, _C_BEMPP_PY}, probes
        assert all("find_spec" in a[2] for a in probes), probes
        # the override is tried first, and a relative one is never trusted
        probes.clear()
        p.env(ANKUSDRIVE_OPENEMS_PYTHON="venv/bin/python")
        assert solvers.solver_python("openems") == _C_OPENEMS_PY
        assert [a[0] for a in probes] == [_C_OPENEMS_PY], probes
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

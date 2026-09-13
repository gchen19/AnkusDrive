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
            "ANKUSDRIVE_WSL_DISTRO", "WM_PROJECT_DIR", "ANKUSDRIVE_CONFIG")


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
    files = set(host_files)
    p.set(solvers.os.path, "isfile", lambda x: x in files)
    p.set(solvers.os.path, "isdir", lambda x: False)
    p.set(glob, "glob", lambda pat, recursive=False: sorted(
        f for f in files if fnmatch.fnmatch(f, os.path.expanduser(pat))))


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

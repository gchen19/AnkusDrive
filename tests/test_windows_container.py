"""The container substrate on Windows: Docker inside WSL.

`ankusdrive container setup` provisions it (engine -> image -> container -> config),
case/runner scratch must be enterable by a WSL-hosted container (Python's Windows
0o700 ACL is not), and doctor offers the route for a Linux-only family. Every
subprocess is faked, so this runs on any host; the live half was run on a Windows 11
box (WSL2 Ubuntu 26.04, Docker Engine 29.1.3): OpenFOAM, YADE, Elmer, openEMS and
Bempp solves through `wsl -d Ubuntu -e docker exec`.

Run:  python3 tests/test_windows_container.py
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import cases, config, container_setup, doctor, solvers  # noqa: E402

_ENV = ("ANKUSDRIVE_SUBSTRATE", "ANKUSDRIVE_CONFIG", "ANKUSDRIVE_WSL_DISTRO",
        "ANKUSDRIVE_CONTAINER", "ANKUSDRIVE_CONTAINER_ENGINE", "ANKUSDRIVE_YADE",
        "ANKUSDRIVE_ELMER_PATH", "ANKUSDRIVE_CASE_ROOT")


class _Patch:
    def __init__(self):
        self._saved, self._env = [], {}

    def set(self, obj, attr, value):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def __enter__(self):
        self._env = {k: os.environ.pop(k, None) for k in _ENV}
        self.tmp = tempfile.mkdtemp(prefix="ad-wincontainer-")
        os.environ["ANKUSDRIVE_CONFIG"] = os.path.join(self.tmp, "config.toml")
        solvers._ctr_info_cache.clear()
        solvers._ctr_probe_cache.clear()
        return self

    def __exit__(self, *exc):
        for obj, attr, val in reversed(self._saved):
            setattr(obj, attr, val)
        for k in _ENV:
            os.environ.pop(k, None)
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
        solvers._ctr_info_cache.clear()
        solvers._ctr_probe_cache.clear()
        config._cache.clear()
        return False


class _Plat:
    def __init__(self, system):
        self._s = system

    def system(self):
        return self._s

    def machine(self):
        return "AMD64"


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _windows(p, distro="Ubuntu"):
    plat = _Plat("Windows")
    for mod in (solvers, container_setup, doctor):
        p.set(mod, "platform", plat)
    p.set(solvers.shutil, "which", lambda n: "wsl.exe" if n == "wsl" else None)
    p.set(solvers, "_wsl_registry_distro", lambda: distro)


def _fake_distro(docker=True, running=None):
    """A fake `subprocess.run` for a WSL distro: records every argv."""
    calls = []
    state = {"running": running}

    def run(argv, **kw):
        calls.append(list(argv))
        a = " ".join(argv)
        if "command -v docker" in a:
            return _Proc(0 if docker else 1, "/usr/bin/docker\n" if docker else "")
        if a.endswith("info --format {{.ServerVersion}}"):
            return _Proc(0, "29.1.3\n")
        if " pull " in a:
            return _Proc(0, "ok\n")
        if "container inspect --format {{.State.Running}}" in a:
            if state["running"] is None:
                return _Proc(1, "", "no such container")
            return _Proc(0, "true\n" if state["running"] else "false\n")
        if " run -d " in a:
            state["running"] = True
            return _Proc(0, "abc123\n")
        if 'echo "$(id -u):$(id -g)"' in a:
            return _Proc(0, "1000:1000\n")
        if a.endswith("exec ankusdrive-solvers env"):
            return _Proc(0, "PATH=/usr/bin\nANKUSDRIVE_YADE=/opt/yade/bin/yade\n"
                            "ANKUSDRIVE_ELMER_PATH=/opt/elmer/bin/ElmerSolver\n")
        return _Proc(0, "")
    return run, calls


def test_setup_creates_the_container_and_records_the_config():
    with _Patch() as p:
        _windows(p)
        run, calls = _fake_distro()
        p.set(container_setup.subprocess, "run", run)
        p.set(container_setup.tempfile, "gettempdir",
              lambda: r"C:\Users\you\AppData\Local\Temp")
        p.set(solvers, "capabilities", lambda: {"families": {"dem": {"available": True}}})
        rep = container_setup.setup()
        assert rep["ok"], rep
        made = next(c for c in calls if "run" in c and "-d" in c)
        assert made[:5] == ["wsl", "-d", "Ubuntu", "-e", "docker"], made
        scratch = "/mnt/c/Users/you/AppData/Local/Temp"
        assert f"{scratch}:{scratch}" in made and "1000:1000" in made, made
        text = Path(rep["config"]).read_text()
        assert 'substrate = "container"' in text and "/opt/yade/bin/yade" in text, text
        assert rep["families"] == {"dem": True}
        assert "wsl -d Ubuntu -e docker" in container_setup.render(rep)


def test_setup_without_an_engine_asks_before_installing_one():
    with _Patch() as p:
        _windows(p)
        run, calls = _fake_distro(docker=False)
        p.set(container_setup.subprocess, "run", run)
        rep = container_setup.setup()
        assert not rep["ok"] and "--install-engine" in rep["error"], rep
        assert not any("-u" in c and "root" in c for c in calls), \
            "installed an engine without --install-engine"


def test_setup_with_install_engine_installs_as_root_in_the_distro():
    with _Patch() as p:
        _windows(p)
        run, calls = _fake_distro(docker=False)
        p.set(container_setup.subprocess, "run", run)
        p.set(solvers, "capabilities", lambda: {"families": {}})
        rep = container_setup.setup(install_engine=True)
        assert rep["ok"], rep
        root = [c for c in calls if c[:5] == ["wsl", "-d", "Ubuntu", "-u", "root"]]
        assert root and "docker.io" in root[0][-1], calls


def test_setup_without_wsl_names_the_install_command():
    with _Patch() as p:
        _windows(p, distro=None)
        rep = container_setup.setup()
        assert not rep["ok"] and "wsl --install" in rep["error"], rep


def test_setup_starts_a_stopped_container_and_flags_a_shadowing_env_var():
    with _Patch() as p:
        _windows(p)
        run, calls = _fake_distro(running=False)
        p.set(container_setup.subprocess, "run", run)
        p.set(solvers, "capabilities", lambda: {"families": {}})
        os.environ["ANKUSDRIVE_ELMER_PATH"] = r"C:\solvers\Elmer\bin\ElmerSolver.exe"
        rep = container_setup.setup()
        assert rep["ok"], rep
        assert any(c[-2:] == ["start", "ankusdrive-solvers"] for c in calls), calls
        assert not any("-d" in c and "run" in c for c in calls)
        assert rep["shadowed"] == ["ANKUSDRIVE_ELMER_PATH"], rep
        assert "SetEnvironmentVariable('ANKUSDRIVE_ELMER_PATH'" in container_setup.render(rep)


def test_setup_refuses_an_explicit_other_substrate():
    with _Patch() as p:
        _windows(p)
        os.environ["ANKUSDRIVE_SUBSTRATE"] = "wsl"
        rep = container_setup.setup()
        assert not rep["ok"] and "'wsl'" in rep["error"], rep


def test_private_dirs_keep_0700_off_the_windows_container_path():
    with _Patch() as p:
        root = os.path.join(p.tmp, "cases")
        os.environ["ANKUSDRIVE_CASE_ROOT"] = root
        assert cases.root() == root
        d = cases.new("foam_pipe")
        if os.name != "nt":
            assert os.stat(root).st_mode & 0o777 == 0o700
            assert os.stat(d).st_mode & 0o777 == 0o700


def test_private_dirs_inherit_the_acl_under_the_windows_container_substrate():
    made, reset = [], []
    with _Patch() as p:
        p.set(cases, "_container_scratch", lambda: True)
        p.set(cases.os, "mkdir", lambda path, *a, **k: made.append((path, a, k)))
        p.set(cases, "_inherit_acl", lambda path: reset.append(path))
        p.set(cases.os, "makedirs", lambda path, *a, **k: made.append((path, a, k)))
        cases.private_dir(os.path.join(p.tmp, "r"))
        d = cases.private_mkdtemp("foam_pipe-", p.tmp)
    assert all(not a and "mode" not in k for _, a, k in made), made   # default mode
    assert reset == [os.path.join(p.tmp, "r")]
    assert cases._OURS.match(os.path.basename(d)), d                   # still reapable


def test_relayed_engine_calls_wait_out_a_wsl_cold_start():
    """The first call after WSL idles the distro boots it, dockerd and the container
    (17 s measured); a 5 s inspect would report a healthy container as absent."""
    with _Patch() as p:
        _windows(p)
        assert solvers._engine_timeout("ANKUSDRIVE_MULTIPASS_TIMEOUT_S", 5.0) >= 45
        os.environ["ANKUSDRIVE_MULTIPASS_TIMEOUT_S"] = "7"
        try:
            assert solvers._engine_timeout("ANKUSDRIVE_MULTIPASS_TIMEOUT_S", 5.0) == 7.0
        finally:
            os.environ.pop("ANKUSDRIVE_MULTIPASS_TIMEOUT_S")
    with _Patch() as p:
        p.set(solvers, "platform", _Plat("Linux"))
        assert solvers._engine_timeout("ANKUSDRIVE_MULTIPASS_TIMEOUT_S", 5.0) == 5.0


def test_doctor_offers_the_container_route_on_windows_only():
    with _Patch() as p:
        _windows(p)
        assert doctor._container_route_hint(["yade"]) == [
            f"        or:    {doctor.CONTAINER_SETUP_HINT}"]
        assert doctor._container_route_hint(["pybullet"]) == []    # not in the image
        os.environ["ANKUSDRIVE_SUBSTRATE"] = "container"
        assert doctor._container_route_hint(["yade"]) == []        # already chosen
    with _Patch() as p:
        assert doctor._container_route_hint(["yade"], system="Linux") == []


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    tests = _discover()
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()

"""WSL routing for the OpenFOAM-backed families on Windows (issue #193).

Pure-Python, no solver and no WSL needed: it monkeypatches ``platform.system`` /
``glob.glob`` / ``os.path.isfile`` / ``shutil.which`` / the registry read, so the
Windows \\wsl$ discovery branches and the ``wsl -e bash`` launcher selection are
exercised on any host OS. Pins:

  * ``bash_argv`` — POSIX boxes launch ``["bash","-c",…]``; Windows launches
    ``["wsl","-d",<distro>,"-e","bash","-c",…]`` pinned to the discovered distro.
  * Distro resolution — ``DRIFTPIN_WSL_DISTRO`` beats the registry default; both
    are None off-Windows; no wsl.exe / no distro degrades cleanly.
  * \\wsl$ path mapping — UNC<->POSIX normalization (incl. \\wsl.localhost),
    ``_posix_glob`` maps UNC hits back to POSIX and expands ``~`` to the LINUX
    home, and resolvers return POSIX paths (the form embedded into the in-distro
    bash scripts).
  * ``find_solver("openfoam")`` — an in-distro binary resolves ``available`` with
    ``via: "wsl"`` (propagated by ``require_solver``); a bashrc-only hit reports
    ``unwired`` with the runs-via-WSL wire hint; no WSL at all stays ``absent``
    with a Windows install hint. The Linux unwired hint stays byte-identical
    (pinned separately by test_solve_degradation.py).

Run:  python3 tests/test_wsl_routing.py
"""
import glob
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import solvers  # noqa: E402

_UNC_BIN = (r"\\wsl$\Ubuntu\usr\lib\openfoam\openfoam2512\platforms"
            r"\linux64GccDPInt32Opt\bin\simpleFoam")
_POSIX_BIN = ("/usr/lib/openfoam/openfoam2512/platforms"
              "/linux64GccDPInt32Opt/bin/simpleFoam")
_UNC_BASHRC = r"\\wsl$\Ubuntu\usr\lib\openfoam\openfoam2512\etc\bashrc"
_POSIX_BASHRC = "/usr/lib/openfoam/openfoam2512/etc/bashrc"

# env vars that would leak the real box's wiring into these tests
_ISOLATE = ("DRIFTPIN_WSL_DISTRO", "DRIFTPIN_OPENFOAM_BASHRC",
            "DRIFTPIN_OPENFOAM_PATH", "DRIFTPIN_OPENFOAM_DIRS",
            "DRIFTPIN_CONFIG")


class _patch:
    """Patch attributes on modules, restoring them on exit; isolates the
    DRIFTPIN_* env vars above AND the config.toml layer (DRIFTPIN_CONFIG is
    pointed at a nonexistent file, which config.load() treats as {})."""

    def __init__(self):
        self._saved = {}
        self._env = {}

    def set(self, obj, attr, value):
        key = (id(obj), attr)
        if key not in self._saved:
            self._saved[key] = (obj, attr, getattr(obj, attr))
        setattr(obj, attr, value)

    def __enter__(self):
        self._env = {n: os.environ.pop(n, None) for n in _ISOLATE}
        os.environ["DRIFTPIN_CONFIG"] = os.path.join(
            os.path.dirname(__file__), "no-such-config.toml")
        return self

    def __exit__(self, *exc):
        for obj, attr, val in self._saved.values():
            setattr(obj, attr, val)
        for n, v in self._env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        return False


def _fake_platform(system):
    class _P:
        @staticmethod
        def system():
            return system

        @staticmethod
        def machine():
            return "AMD64" if system == "Windows" else "x86_64"
    return _P


def _fake_windows(p, distro="Ubuntu", wsl_exe=r"C:\Windows\System32\wsl.exe"):
    """The standard fake: Windows host, wsl.exe on PATH, one registered distro."""
    p.set(solvers, "platform", _fake_platform("Windows"))
    p.set(solvers.shutil, "which",
          lambda name: wsl_exe if name == "wsl" and wsl_exe else None)
    p.set(solvers, "_wsl_registry_distro", lambda: distro)


def test_bash_argv_posix_and_windows():
    """POSIX: plain bash -c. Windows: wsl -d <distro> -e bash -c, pinned to the
    same distro discovery probes."""
    with _patch() as p:
        p.set(solvers, "platform", _fake_platform("Linux"))
        assert solvers.bash_argv("echo hi") == ["bash", "-c", "echo hi"]
    with _patch() as p:
        _fake_windows(p)
        assert solvers.bash_argv("echo hi") == \
            ["wsl", "-d", "Ubuntu", "-e", "bash", "-c", "echo hi"]


def test_wsl_distro_override_beats_registry():
    with _patch() as p:
        _fake_windows(p, distro="Debian")
        assert solvers.wsl_distro() == "Debian"          # registry default
        os.environ["DRIFTPIN_WSL_DISTRO"] = "Ubuntu-24.04"
        try:
            assert solvers.wsl_distro() == "Ubuntu-24.04"
        finally:
            os.environ.pop("DRIFTPIN_WSL_DISTRO", None)
    with _patch() as p:
        p.set(solvers, "platform", _fake_platform("Linux"))
        assert solvers.wsl_distro() is None
        assert solvers.wsl_available() is False


def test_wsl_unc_posix_roundtrip():
    with _patch() as p:
        _fake_windows(p)
        assert solvers.wsl_unc(_POSIX_BASHRC) == _UNC_BASHRC
        assert solvers.wsl_posix(_UNC_BASHRC) == _POSIX_BASHRC
        # wsl.localhost (the newer UNC spelling) and forward slashes normalize too
        assert solvers.wsl_posix(
            "//wsl.localhost/Ubuntu/usr/lib/openfoam") == "/usr/lib/openfoam"
        # non-WSL paths pass through untouched
        assert solvers.wsl_posix(r"C:\Users\x\bashrc") == r"C:\Users\x\bashrc"
        assert solvers.wsl_posix(_POSIX_BASHRC) == _POSIX_BASHRC


def test_posix_glob_maps_unc_hits_and_linux_home():
    """Windows _posix_glob probes the UNC mirror and returns POSIX; ``~`` expands
    to the LINUX home (/home/*, /root), never C:\\Users."""
    seen = []

    def fake_glob(pat, recursive=False):
        seen.append(pat)
        if pat.endswith(r"\etc\bashrc") and pat.startswith("\\\\wsl$\\Ubuntu"):
            return [_UNC_BASHRC]
        if "\\home\\*\\" in pat:
            return [r"\\wsl$\Ubuntu\home\ci\calculix-adapter\bin"]
        return []

    with _patch() as p:
        _fake_windows(p)
        p.set(glob, "glob", fake_glob)
        hits = solvers._posix_glob(("/usr/lib/openfoam/openfoam*/etc/bashrc",))
        assert hits == [_POSIX_BASHRC], hits
        homes = solvers._posix_glob(("~/calculix-adapter/bin",))
        assert homes == ["/home/ci/calculix-adapter/bin"], homes
        assert any("\\root\\" in s for s in seen), seen   # /root probed too
        assert not any("C:" in s for s in seen), seen     # never the Windows home
    # off-Windows with no WSL: plain glob semantics are untouched
    with _patch() as p:
        _fake_windows(p, wsl_exe=None)                    # Windows but no wsl.exe
        p.set(glob, "glob", lambda pat, recursive=False: 1 / 0)  # must not be hit
        assert solvers._posix_glob(("/usr/lib/*",)) == []


def test_find_solver_openfoam_via_wsl():
    """An in-distro binary (probed through \\wsl$) resolves available with the
    POSIX path + via:'wsl', and require_solver propagates the flag."""
    def fake_glob(pat, recursive=False):
        return [_UNC_BIN] if pat.endswith("simpleFoam") and \
            pat.startswith("\\\\wsl$\\Ubuntu") else []

    with _patch() as p:
        _fake_windows(p)
        p.set(glob, "glob", fake_glob)
        p.set(solvers.os.path, "isfile", lambda x: x == _UNC_BIN)
        info = solvers.find_solver("openfoam")
        assert info["available"] is True and info["status"] == "ok", info
        assert info["path"] == _POSIX_BIN, info
        assert info["via"] == "wsl", info
        r = solvers.require_solver("openfoam")
        assert r["ok"] is True and r["via"] == "wsl" and r["path"] == _POSIX_BIN, r


def test_no_wsl_stays_absent_with_windows_install_hint():
    """Windows without wsl.exe: no \\wsl$ probing happens, openfoam is plain
    absent, and the install hint names the WSL provisioning path."""
    with _patch() as p:
        _fake_windows(p, wsl_exe=None)
        p.set(glob, "glob", lambda pat, recursive=False: [])
        p.set(solvers.os.path, "isfile", lambda x: False)
        info = solvers.find_solver("openfoam")
        assert info["available"] is False and info["status"] == "absent", info
        assert "wsl" in info["install_hint"].lower(), info
        r = solvers.require_solver("openfoam")
        assert r["status"] == "absent" and "wsl" in r["install"].lower(), r


def test_bashrc_only_reports_unwired_with_wsl_hint():
    """OpenFOAM installed in the distro but bin unresolvable (e.g. probe raced an
    install): the \\wsl$ bashrc hit reports unwired with the runs-via-WSL hint."""
    def fake_glob(pat, recursive=False):
        return [_UNC_BASHRC] if pat.endswith(r"\etc\bashrc") and \
            pat.startswith("\\\\wsl$\\Ubuntu") else []

    with _patch() as p:
        _fake_windows(p)
        p.set(glob, "glob", fake_glob)
        p.set(solvers.os.path, "isfile", lambda x: x == _UNC_BASHRC)
        info = solvers.find_solver("openfoam")
        assert info["status"] == "unwired", info
        assert info["found_at"] == _POSIX_BASHRC, info
        assert info["wire_hint"] == (
            f"set DRIFTPIN_OPENFOAM_BASHRC={_POSIX_BASHRC} "
            "(runs via WSL distro 'Ubuntu')"), info


def test_openfoam_bashrc_accepts_unc_override():
    """A DRIFTPIN_OPENFOAM_BASHRC override in \\wsl$ form normalizes to the POSIX
    form the in-distro scripts source."""
    with _patch() as p:
        _fake_windows(p)
        p.set(solvers.os.path, "isfile", lambda x: x == _UNC_BASHRC)
        p.set(glob, "glob", lambda pat, recursive=False: [])
        os.environ["DRIFTPIN_OPENFOAM_BASHRC"] = _UNC_BASHRC
        try:
            assert solvers.openfoam_bashrc() == _POSIX_BASHRC
        finally:
            os.environ.pop("DRIFTPIN_OPENFOAM_BASHRC", None)


def test_clean_wsl_text_strips_nuls():
    garbled = "t\x00h\x00e\x00r\x00e\x00 is no distribution\x00"
    assert solvers.clean_wsl_text(garbled) == "there is no distribution"
    assert solvers.clean_wsl_text("plain solver output") == "plain solver output"
    assert solvers.clean_wsl_text("") == ""


# --- macOS: Multipass is the OpenFOAM substrate (issue #193) -------------------

def test_bash_argv_macos_multipass():
    """macOS launches into the Multipass VM; `multipass exec` starts in the VM home,
    so the case dir is cd'd into inside the script (host scratch mounted at the same
    path). Instance name defaults to "openfoam" and honors the env override; a case
    dir with a space is shell-quoted."""
    with _patch() as p:
        p.set(solvers, "platform", _fake_platform("Darwin"))
        assert solvers.bash_argv("blockMesh", "/private/tmp/case") == [
            "multipass", "exec", "openfoam", "--",
            "bash", "-c", "cd /private/tmp/case && blockMesh"]
        # no case dir -> no cd wrapper (the script runs in the VM home)
        assert solvers.bash_argv("echo hi") == [
            "multipass", "exec", "openfoam", "--", "bash", "-c", "echo hi"]
        # spaced case dir is quoted
        argv = solvers.bash_argv("blockMesh", "/tmp/my case")
        assert argv[-1] == "cd '/tmp/my case' && blockMesh", argv
        # instance name is overridable (openfoam.org's `multipass launch -n <name>`)
        os.environ["DRIFTPIN_OPENFOAM_INSTANCE"] = "foam-dev"
        try:
            assert solvers.foam_instance() == "foam-dev"
            assert solvers.bash_argv("x", "/c")[2] == "foam-dev"
        finally:
            os.environ.pop("DRIFTPIN_OPENFOAM_INSTANCE", None)


def test_macos_openfoam_unwired_when_multipass_present():
    """macOS has no host-visible OpenFOAM bashrc (it's inside the VM), so the honest
    signal is the `multipass` CLI on PATH: openfoam reports `unwired` with the
    provisioning hint — never ready (the VM's contents can't be probed without
    executing it). With multipass absent it stays plain absent."""
    with _patch() as p:
        p.set(solvers, "platform", _fake_platform("Darwin"))
        p.set(solvers, "_binary_path", lambda name, spec: None)   # no host binary
        p.set(solvers, "_standard_bashrc", lambda cfg: None)      # no host bashrc
        p.set(solvers.shutil, "which",
              lambda n: "/opt/homebrew/bin/multipass" if n == "multipass" else None)
        info = solvers.find_solver("openfoam")
        assert info["available"] is False and info["status"] == "unwired", info
        assert info["found_at"] == "multipass", info
        assert "multipass shell" in info["wire_hint"], info

        p.set(solvers.shutil, "which", lambda n: None)            # multipass absent
        info = solvers.find_solver("openfoam")
        assert info["status"] == "absent", info


# --- runner -------------------------------------------------------------------

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
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()

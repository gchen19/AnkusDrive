"""Cross-platform FreeCAD resolution + `driftpin doctor` contract (issues #191, #198).

Pure-Python, no FreeCAD boot (probe_version=False), so this runs on any CI lane and
on any host OS: it monkeypatches ``platform.system`` / ``glob.glob`` / ``os.path.isfile``
so the Windows, macOS, and Linux discovery branches are all exercised regardless of the
box the test runs on. Pins:

  * #191 — the Windows ``freecadcmd.exe`` discovery candidates (Program Files version
    glob, ``.exe`` name variants, env/PATH precedence, overlap dedup).
  * #198 — ``driftpin doctor`` builds a well-formed report (platform + FreeCAD + the
    solver capabilities dict) and renders it without crashing, whether FreeCAD resolves
    or not.

Run:  python3 tests/test_windows_support.py
"""
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin import client, doctor  # noqa: E402


class _patch:
    """Patch attributes on a module, restoring them on exit. Also clears
    DRIFTPIN_FREECADCMD so the env override never leaks in from the real shell."""

    def __init__(self, **targets):
        # targets: {"client.platform.system": fn, ...} — but simpler: pass explicit
        self.targets = targets
        self._saved = {}
        self._env = None

    def set(self, obj, attr, value):
        # Record the ORIGINAL only on first patch of this (obj, attr). Several modules
        # share the same singletons (client.os.path IS doctor.os.path IS os.path), so a
        # naive save-every-time would capture an already-patched value on the 2nd set and
        # leak that patch into later tests on restore.
        key = (id(obj), attr)
        if key not in self._saved:
            self._saved[key] = (obj, attr, getattr(obj, attr))
        setattr(obj, attr, value)

    def __enter__(self):
        self._env = os.environ.pop("DRIFTPIN_FREECADCMD", None)
        return self

    def __exit__(self, *exc):
        for obj, attr, val in self._saved.values():
            setattr(obj, attr, val)
        if self._env is not None:
            os.environ["DRIFTPIN_FREECADCMD"] = self._env
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


def test_windows_candidates_glob_program_files():
    """On Windows the resolver globs the versioned Program Files dir and returns the
    freecadcmd.exe that exists — without any env var or PATH entry."""
    win_exe = r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"
    with _patch() as p:
        p.set(client, "platform", _fake_platform("Windows"))
        p.set(client.glob, "glob", lambda pat: [win_exe] if "FreeCAD" in pat else [])
        p.set(client.shutil, "which", lambda name: None)          # nothing on PATH
        p.set(client.os.path, "isfile", lambda x: x == win_exe)
        cands = client._freecadcmd_candidates()
        assert win_exe in cands, cands
        # overlapping globs (FreeCAD * and FreeCAD*) must not duplicate the same path
        assert cands.count(win_exe) == 1, cands
        assert client._resolve_freecadcmd() == win_exe


def test_windows_picks_highest_version():
    """Two installs present -> the highest version (last after sort) wins."""
    v10 = r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe"
    v11 = r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"
    with _patch() as p:
        p.set(client, "platform", _fake_platform("Windows"))
        p.set(client.glob, "glob", lambda pat: [v10, v11] if "FreeCAD *" in pat else [])
        p.set(client.shutil, "which", lambda name: None)
        p.set(client.os.path, "isfile", lambda x: x in (v10, v11))
        assert client._resolve_freecadcmd() == v11


def test_env_override_wins_on_every_os():
    """DRIFTPIN_FREECADCMD is the top precedence layer, returned verbatim and
    unconditionally (even if it doesn't exist) so CI/one-off runs are honored."""
    for system in ("Windows", "Darwin", "Linux"):
        with _patch() as p:
            p.set(client, "platform", _fake_platform(system))
            os.environ["DRIFTPIN_FREECADCMD"] = r"X:\custom\freecadcmd.exe"
            try:
                assert client._resolve_freecadcmd() == r"X:\custom\freecadcmd.exe"
            finally:
                os.environ.pop("DRIFTPIN_FREECADCMD", None)


def test_path_lookup_tries_both_names():
    """shutil.which is tried for freecadcmd AND FreeCADCmd (Windows ships the latter
    casing); a PATH hit beats the default install locations."""
    hit = r"C:\tools\FreeCADCmd.exe"
    seen = []

    def which(name):
        seen.append(name)
        return hit if name == "FreeCADCmd" else None

    with _patch() as p:
        p.set(client, "platform", _fake_platform("Windows"))
        p.set(client.shutil, "which", which)
        assert client._resolve_freecadcmd() == hit
        assert "FreeCADCmd" in seen and "freecadcmd" in seen, seen


def test_unresolved_returns_plausible_placeholder():
    """Nothing installed -> a placeholder is returned (never a crash) so the eventual
    Popen surfaces a clean 'not found' rather than a wrong-OS path. The key guarantee:
    on Windows it is NEVER the mac .app path — it's the bare `freecadcmd` name (which
    Popen resolves via PATH/PATHEXT and fails cleanly on) since every Windows default
    is a glob that expanded to nothing."""
    with _patch() as p:
        p.set(client, "platform", _fake_platform("Windows"))
        p.set(client.glob, "glob", lambda pat: [])       # no install found
        p.set(client.shutil, "which", lambda name: None)
        p.set(client.os.path, "isfile", lambda x: False)
        placeholder = client._resolve_freecadcmd()
        assert ".app" not in placeholder, placeholder
        assert "freecadcmd" in placeholder.lower(), placeholder


def test_doctor_report_freecad_missing_carries_fix():
    """doctor.freecad_report with nothing installed reports unavailable + an
    OS-specific fix hint, and never boots FreeCAD."""
    with _patch() as p:
        p.set(doctor, "platform", _fake_platform("Windows"))
        p.set(client, "platform", _fake_platform("Windows"))
        p.set(client.glob, "glob", lambda pat: [])
        p.set(client.shutil, "which", lambda name: None)
        p.set(client.os.path, "isfile", lambda x: False)
        fc = doctor.freecad_report(probe_version=False)
        assert fc["available"] is False, fc
        assert "version" not in fc, fc
        assert "fix" in fc and "freecad.org" in fc["fix"].lower(), fc


def test_doctor_report_freecad_found_no_boot():
    """When the binary exists on disk, freecad_report(probe_version=False) marks it
    available and records the resolution source, without booting."""
    win_exe = r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe"
    with _patch() as p:
        p.set(doctor, "platform", _fake_platform("Windows"))
        p.set(client, "platform", _fake_platform("Windows"))
        p.set(client.glob, "glob", lambda pat: [win_exe])
        p.set(client.shutil, "which", lambda name: None)
        p.set(client.os.path, "isfile", lambda x: x == win_exe)
        p.set(doctor.os.path, "isfile", lambda x: x == win_exe)
        fc = doctor.freecad_report(probe_version=False)
        assert fc["available"] is True and fc["path"] == win_exe, fc
        assert "version" not in fc, fc            # probe_version=False -> no boot
        assert fc["source"], fc


def test_doctor_build_and_render_never_crash():
    """The full report is well-formed and renders to a string on every OS, with the
    solver half needing no FreeCAD and no server."""
    for system in ("Windows", "Darwin", "Linux"):
        with _patch() as p:
            p.set(doctor, "platform", _fake_platform(system))
            report = doctor.build_report(probe_version=False)
            assert report["platform"]["system"] == system, report
            assert "freecad" in report and "solvers" in report, report
            caps = report["solvers"]
            assert set(caps) >= {"platform", "available", "families", "solvers"}, caps
            text = doctor.render(report)
            assert isinstance(text, str) and "Solver families:" in text, text
            assert "families ready" in text, text


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

"""Cross-platform FreeCAD resolution + `ankusdrive doctor` contract (issues #191, #198).

Pure-Python, no FreeCAD boot (probe_version=False), so this runs on any CI lane and
on any host OS: it monkeypatches ``platform.system`` / ``glob.glob`` / ``os.path.isfile``
so the Windows, macOS, and Linux discovery branches are all exercised regardless of the
box the test runs on. Pins:

  * #191 — the Windows ``freecadcmd.exe`` discovery candidates (Program Files version
    glob, ``.exe`` name variants, env/PATH precedence, overlap dedup).
  * #198 — ``ankusdrive doctor`` builds a well-formed report (platform + FreeCAD + the
    solver capabilities dict) and renders it without crashing, whether FreeCAD resolves
    or not.
  * #279 — the scripted Windows core install (``scripts/install-core.ps1``): it exists,
    it stays PowerShell-5.1/cp1252 parseable, it carries the MCP registration step the
    reporter got stuck on, and its declared Python range agrees with pyproject. Those
    are STATIC checks on purpose, so the Linux fast lane catches the drift; the script
    itself is exercised on the self-hosted Windows box.

Run:  python3 tests/test_windows_support.py
"""
import os
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import client, doctor  # noqa: E402


class _patch:
    """Patch attributes on a module, restoring them on exit. Also clears
    ANKUSDRIVE_FREECADCMD so the env override never leaks in from the real shell."""

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
        self._env = os.environ.pop("ANKUSDRIVE_FREECADCMD", None)
        return self

    def __exit__(self, *exc):
        for obj, attr, val in self._saved.values():
            setattr(obj, attr, val)
        if self._env is not None:
            os.environ["ANKUSDRIVE_FREECADCMD"] = self._env
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
    """ANKUSDRIVE_FREECADCMD is the top precedence layer, returned verbatim and
    unconditionally (even if it doesn't exist) so CI/one-off runs are honored."""
    for system in ("Windows", "Darwin", "Linux"):
        with _patch() as p:
            p.set(client, "platform", _fake_platform(system))
            os.environ["ANKUSDRIVE_FREECADCMD"] = r"X:\custom\freecadcmd.exe"
            try:
                assert client._resolve_freecadcmd() == r"X:\custom\freecadcmd.exe"
            finally:
                os.environ.pop("ANKUSDRIVE_FREECADCMD", None)


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


# --- the scripted Windows core install (#279) ---------------------------------

INSTALL_CORE = REPO / "scripts" / "install-core.ps1"
INSTALL_SOLVERS = REPO / "scripts" / "install-solvers.ps1"
PYPROJECT = REPO / "pyproject.toml"


def _pyproject_text():
    return PYPROJECT.read_text(encoding="utf-8")


def _classifier_pythons():
    """The `Programming Language :: Python :: 3.x` versions pyproject claims."""
    return sorted(
        tuple(int(p) for p in m.split("."))
        for m in re.findall(r'"Programming Language :: Python :: (3\.\d+)"', _pyproject_text())
    )


def test_install_core_script_exists():
    """The core install (venv + pip + doctor + MCP wiring) is scripted on Windows, not
    left as manual README steps — that gap is what #279's reporter hit."""
    assert INSTALL_CORE.is_file(), f"{INSTALL_CORE} is missing"


def test_install_core_script_is_powershell_51_safe():
    """The self-hosted runner and a stock Windows box only guarantee Windows PowerShell
    5.1 with a cp1252 console, so the script must be ASCII-only (a smart quote or an
    em dash there is a parse error, not a cosmetic issue) and must not use PS 6+ syntax."""
    raw = INSTALL_CORE.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not bad, f"non-ASCII byte(s) in install-core.ps1 at offsets {[i for i, _ in bad[:5]]}"
    text = raw.decode("ascii")
    for token in ("??", "&&", "||"):          # null-coalescing / PS7-only chaining
        assert token not in text, f"install-core.ps1 uses PS 6+ operator {token!r}"


def test_install_core_covers_the_mcp_registration_step():
    """The step the reporter actually got stuck on. The block must come from
    `ankusdrive setup --print-mcp-config` (ankusdrive/setup_cmd.py — the single source of
    truth that resolves the launcher to an ABSOLUTE path, because a GUI MCP host does
    not inherit the shell PATH) and must name the Windows config file location."""
    text = INSTALL_CORE.read_text(encoding="ascii")
    assert "setup --print-mcp-config" in text, "install-core.ps1 never prints the MCP block"
    assert "claude_desktop_config.json" in text, "no Claude Desktop config path"
    assert "APPDATA" in text, "the Claude Desktop config path must be DERIVED from %APPDATA%"
    assert "ankusdrive.exe" in text, "no resolved Windows launcher path"


def test_install_core_python_range_matches_pyproject():
    """The script gates on a Python range; pyproject declares one. #279 is exactly what
    happens when those two disagree and the user learns the answer from pip's resolver,
    so pin the agreement: the floor is `requires-python`, the ceiling is the highest
    `Programming Language :: Python :: 3.x` classifier (= newest version verified)."""
    text = INSTALL_CORE.read_text(encoding="ascii")
    py_min = re.search(r"\$PY_MIN\s*=\s*\[version\]'(\d+\.\d+)'", text)
    py_max = re.search(r"\$PY_MAX_VERIFIED\s*=\s*\[version\]'(\d+\.\d+)'", text)
    assert py_min and py_max, "install-core.ps1 must declare $PY_MIN and $PY_MAX_VERIFIED"

    req = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', _pyproject_text())
    assert req, "pyproject requires-python must be a simple '>=X.Y' floor"
    assert py_min.group(1) == req.group(1), (
        f"install-core.ps1 floor {py_min.group(1)} != pyproject requires-python "
        f">={req.group(1)}"
    )

    classifiers = _classifier_pythons()
    assert classifiers, "pyproject declares no Python version classifiers"
    newest = "%d.%d" % classifiers[-1]
    assert py_max.group(1) == newest, (
        f"install-core.ps1 verified ceiling {py_max.group(1)} != newest classifier {newest} "
        "— bump both together, and only after actually installing on that interpreter"
    )
    # The floor must be claimed too, and the run must be contiguous (a gap would mean a
    # version was dropped silently rather than deliberately).
    floor = tuple(int(p) for p in req.group(1).split("."))
    assert classifiers[0] == floor, f"classifiers start at {classifiers[0]}, floor is {floor}"
    assert classifiers == [(3, m) for m in range(floor[1], classifiers[-1][1] + 1)], classifiers


def test_doctor_python_window_matches_pyproject():
    """`ankusdrive doctor` warns above its own verified ceiling (#278) and the installer
    gates on the same range (#279) — two hardcoded copies of one fact. They drifted
    the moment 3.14 was verified, so pin all three to pyproject: floor =
    requires-python, ceiling = the newest Programming Language classifier."""
    from ankusdrive import doctor

    req = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', _pyproject_text())
    assert req, "pyproject requires-python must be a simple '>=X.Y' floor"
    floor = tuple(int(p) for p in req.group(1).split("."))
    assert doctor._PY_MIN == floor, (
        f"doctor._PY_MIN {doctor._PY_MIN} != pyproject requires-python >={req.group(1)}"
    )

    newest = _classifier_pythons()[-1]
    assert doctor._PY_MAX_TESTED == newest, (
        f"doctor._PY_MAX_TESTED {doctor._PY_MAX_TESTED} != newest classifier {newest} "
        "— bump doctor, install-core.ps1 and pyproject together, and only after "
        "actually installing on that interpreter"
    )


def test_pyproject_declares_windows_and_no_python_ceiling():
    """Honest metadata (#279): Windows is a supported OS — it has a docs page, an
    install script and a CI lane — and `requires-python` deliberately carries NO upper
    bound, because 3.14 was verified to work rather than guessed at."""
    text = _pyproject_text()
    assert '"Operating System :: Microsoft :: Windows"' in text, (
        "Windows is supported but missing from the OS classifiers, so PyPI would "
        "advertise AnkusDrive as Unix-only"
    )
    req = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    assert req and "<" not in req.group(1), (
        f"requires-python {req.group(1)!r} carries an upper bound — a hard ceiling "
        "breaks the next CPython for no reason; the verified range lives in the "
        "classifiers and install-core.ps1 instead"
    )


def test_install_solvers_ps1_forwards_a_core_target():
    """`install-solvers.ps1` is the script Windows users find first; it must be able to
    hand off to the core install rather than leaving them at 'No .venv'."""
    text = INSTALL_SOLVERS.read_text(encoding="utf-8")
    assert "install-core.ps1" in text, "install-solvers.ps1 never mentions the core install"
    assert re.search(r"'core'\s*\{", text), "install-solvers.ps1 has no 'core' target"


def test_readme_has_a_windows_quickstart_pointing_at_the_script():
    """#279 fix 3: Windows is its own quickstart with PowerShell-native commands,
    not a note block inside the Unix flow."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^#+ .*Windows.*\(PowerShell\)", readme, re.M), (
        "README has no Windows quickstart heading"
    )
    assert "scripts\\install-core.ps1" in readme, "README's Windows section doesn't run install-core.ps1"
    assert "claude mcp add ankusdrive" in readme, "README never shows the MCP registration one-liner"


def test_bundled_ccx_found_beside_a_root_level_freecadcmd():
    """#316: the Windows portable 7z has a FreeCADCmd.exe at the archive root AND the real
    bin\\ (where ccx.exe lives). When freecadcmd resolves to the root one, FreeCAD-bundled
    solver discovery must still probe <root>\\bin, or calculix goes absent and its tests
    SKIP green. Real temp dirs, so no path-API patching."""
    import tempfile
    from ankusdrive import solvers
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "bin"))
        fc = os.path.join(root, "FreeCADCmd.exe")
        ccx = os.path.join(root, "bin", "ccx.exe")
        for f in (fc, ccx):
            open(f, "w").close()
        with _patch() as p:
            p.set(client, "_resolve_freecadcmd", lambda: fc)
            p.set(client, "_freecadcmd_candidates", lambda: [])
            dirs = solvers._freecad_bundled_bin_dirs()
        assert os.path.join(root, "bin") in dirs, dirs
        assert any(os.path.isfile(os.path.join(d, "ccx.exe")) for d in dirs), dirs


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

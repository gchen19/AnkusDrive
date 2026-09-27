"""Issue #468: the FreeCAD worker must not let the host venv's compiled wheels
shadow FreeCAD's own ABI-sensitive packages (PySide6/shiboken6/numpy).

In a wheel install the worker's package parent IS the venv's site-packages. With it
at sys.path[0], rayoptics pulled the venv's PySide6 over FreeCAD's and died on the
Qt DLLs FreeCAD had already loaded. These tests run the worker's real path-setup
lines (no FreeCAD needed) and the doctor check for a Python-version mismatch.
Pure Python, any platform.

Run:  python3 tests/test_worker_venv_shadowing.py
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import doctor  # noqa: E402
from ankusdrive.analysis import optics_design  # noqa: E402

WORKER = REPO / "ankusdrive" / "worker.py"


def _path_setup_source() -> str:
    """The worker's sys.path block, verbatim: `_PKG_PARENT = ...` through the append."""
    src = WORKER.read_text(encoding="utf-8")
    start = src.index("_PKG_PARENT = ")
    end = src.index("sys.path.append(_PKG_PARENT)") + len("sys.path.append(_PKG_PARENT)")
    return src[start:end]


def test_worker_path_setup_puts_host_packages_last():
    with tempfile.TemporaryDirectory() as d:
        _path_setup_case(Path(d))


def _path_setup_case(tmp_path):
    # A stand-in for FreeCAD's bundled site-packages, holding its own `shadowme`;
    # the package parent carries a different one (the venv's). FreeCAD's must win.
    fc_sp = tmp_path / "fc_site"
    fc_sp.mkdir()
    (fc_sp / "shadowme.py").write_text("WHO = 'freecad'\n")
    parent = WORKER.parent.parent
    probe = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(fc_sp)!r})
        __file__ = {str(WORKER)!r}
    """) + _path_setup_source() + textwrap.dedent(f"""
        import ankusdrive, shadowme
        print(os.path.normcase(sys.path[-1]) == os.path.normcase({str(parent)!r}))
        print(sys.path.count(_PKG_PARENT))
        print(os.path.normcase(os.path.dirname(os.path.dirname(ankusdrive.__file__)))
              == os.path.normcase({str(parent)!r}))
        print(shadowme.WHO)
    """)
    (parent / "shadowme.py").write_text("WHO = 'venv'\n")
    try:
        # -I: no PYTHONPATH/user site, so only the setup under test orders sys.path.
        out = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path,
                             capture_output=True, text=True, check=True).stdout.split()
    finally:
        (parent / "shadowme.py").unlink()
    assert out == ["True", "1", "True", "freecad"], out


def test_worker_python_report_flags_a_mismatch_only_when_wheels_are_shared():
    same = doctor.worker_python_report([3, 11, 14], (3, 11, 9), reads_host_packages=True)
    assert same["match"] and "warning" not in same

    bad = doctor.worker_python_report([3, 11, 14], (3, 12, 3), reads_host_packages=True)
    assert not bad["match"]
    assert "3.11" in bad["warning"] and "3.12" in bad["warning"]
    assert "3.11" in bad["fix"]

    # A source checkout: the worker never sees the host's wheels, so no warning.
    ckout = doctor.worker_python_report([3, 11, 14], (3, 12, 3), reads_host_packages=False)
    assert "warning" not in ckout

    assert doctor.worker_python_report(None) is None  # older worker: says nothing


def test_worker_python_fix_is_written_for_pipx():
    old = os.environ.get("ANKUSDRIVE_INSTALL_KIND")
    os.environ["ANKUSDRIVE_INSTALL_KIND"] = "pipx"
    try:
        rep = doctor.worker_python_report([3, 11, 14], (3, 12, 3), reads_host_packages=True)
    finally:
        if old is None:
            os.environ.pop("ANKUSDRIVE_INSTALL_KIND")
        else:
            os.environ["ANKUSDRIVE_INSTALL_KIND"] = old
    assert rep["fix"] == "pipx reinstall --python 3.11 ankusdrive"


def test_render_shows_the_mismatch():
    fc = {"version": "1.1.1", "path": os.sep + "fc", "source": "default", "exists": True,
          "worker_python": doctor.worker_python_report([3, 11, 14], (3, 12, 3),
                                                       reads_host_packages=True)}
    text = "\n".join(doctor._fmt_freecad(fc))
    assert "python: 3.11.14 (host venv 3.12)" in text and "fix:" in text


def test_dist_version_prefers_metadata_over_dunder():
    class Mod:
        __version__ = "0.0.bogus"
    # pip ships dist metadata in every interpreter here; the stale attribute must lose.
    import importlib.metadata as md
    assert optics_design.dist_version("pip", Mod) == md.version("pip")
    assert optics_design.dist_version("no-such-dist-468", Mod) == "0.0.bogus"
    assert optics_design.dist_version("no-such-dist-468") == "unknown"


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

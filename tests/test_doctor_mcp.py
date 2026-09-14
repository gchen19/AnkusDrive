"""``ankusdrive doctor``'s MCP preflight (issue #278).

A field install had `ankusdrive ping` and `ankusdrive doctor` both passing while
`ankusdrive mcp` was dead on arrival — mcp 2.0.0 dropped ``mcp.server.fastmcp``
(issue #277) and no CLI path ever imported the `mcp` package. These tests pin the
section that closes that hole:

  * the interpreter window (pyproject's requires-python has no ceiling, so a host
    newer than the verified range installs cleanly and then breaks on a dependency
    with no wheels),
  * the server import check, and that a broken `mcp` prints the pin VERBATIM,
  * the serve round-trip (spawn `ankusdrive mcp` over stdio, initialize + ping),
  * that the default report never spawns a server — the MCP server's own
    setup_status tool calls build_report().

The mcp-2.0.0 failure path is exercised by INJECTION (a fake ``mcp`` package in
sys.modules, restored on exit), never by touching the real environment: no test
here installs, uninstalls, or upgrades anything.

Run:  python3 tests/test_doctor_mcp.py
"""
import json
import os
import subprocess
import sys
import time
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import doctor  # noqa: E402

# Is a usable mcp SDK present in THIS interpreter? The live-round-trip tests below
# assert the healthy-install acceptance, which only means anything when it is.
try:
    import mcp  # noqa: F401
    from mcp.server.fastmcp import FastMCP  # noqa: F401
    HAVE_MCP = True
except Exception:
    HAVE_MCP = False

_MCP_KEYS = lambda: [k for k in sys.modules  # noqa: E731
                     if k == "mcp" or k.startswith("mcp.") or k == "ankusdrive.mcp_server"]


@contextmanager
def _fake_mcp_2_0_0():
    """Stand in for a venv holding mcp 2.0.0: the package imports, but the
    ``mcp.server.fastmcp`` module that 2.0.0 removed is gone.

    sys.modules is snapshotted and restored, so the real SDK is back afterwards —
    the alternative (pip-installing mcp 2.0.0) would wreck the developer's env."""
    saved_modules = {k: sys.modules[k] for k in _MCP_KEYS()}
    saved_version = doctor._installed_mcp_version
    try:
        for k in _MCP_KEYS():
            del sys.modules[k]
        pkg = types.ModuleType("mcp")
        pkg.__path__ = []                  # a package, but with nothing to find in it
        pkg.__version__ = "2.0.0"
        server = types.ModuleType("mcp.server")
        server.__path__ = []
        sys.modules["mcp"] = pkg
        sys.modules["mcp.server"] = server
        doctor._installed_mcp_version = lambda: "2.0.0"
        yield
    finally:
        doctor._installed_mcp_version = saved_version
        for k in _MCP_KEYS():
            del sys.modules[k]
        sys.modules.update(saved_modules)


def test_pin_fix_is_the_pyproject_pin_verbatim():
    """The remediation string is what the user must type, character for character."""
    assert doctor.MCP_PIN_FIX == 'pip install "mcp>=1.10,<2"', doctor.MCP_PIN_FIX


def test_python_report_flags_an_untested_interpreter():
    """requires-python (>=3.10) has no ceiling, so an interpreter above the verified
    range installs and then finds no wheels — the field reporter's box. Doctor must
    say so rather than stay silent. The out-of-window version is derived from the
    window itself: 3.14 IS verified (#279 installed on it), and hardcoding a
    then-untested version here is how this test would quietly stop testing anything."""
    ok = doctor.python_report((3, 12, 3), "3.12.3 (main) [GCC]")
    assert ok["supported"] is True, ok
    assert "warning" not in ok and "fix" not in ok, ok
    assert ok["version_short"] == "3.12.3", ok

    window = "%d.%d-%d.%d" % (doctor._PY_MIN + doctor._PY_MAX_TESTED)
    above = (doctor._PY_MAX_TESTED[0], doctor._PY_MAX_TESTED[1] + 1)
    new = doctor.python_report(above + (0,), "%d.%d.0 (main) [MSC v.1900 64 bit]" % above)
    assert new["supported"] is False, new
    assert ("%d.%d.0" % above) in new["warning"] and window in new["warning"], new
    assert "wheels" in new["warning"], new
    assert new["fix"], new

    old = doctor.python_report((3, 9, 7), "3.9.7 (default)")
    assert old["supported"] is False, old
    assert "floor" in old["warning"], old

    live = doctor.python_report()
    assert live["version"] == " ".join(sys.version.split()), live


def test_mcp_2_0_0_fails_the_section_with_the_pin_printed_verbatim():
    """THE acceptance case: on a venv with mcp 2.0.0, the MCP section fails and the
    exact pin command is printed. Injected, not installed."""
    with _fake_mcp_2_0_0():
        imp = doctor.mcp_import_report()
    assert imp["available"] is False, imp
    assert imp["package_version"] == "2.0.0", imp
    assert "fastmcp" in imp["error"], imp
    assert imp["fix"] == doctor.MCP_PIN_FIX, imp

    # …and it survives rendering: the user reads lines, not dicts.
    lines = doctor._fmt_mcp({
        "python": doctor.python_report(),
        "import": imp,
        "serve": {"checked": False, "ok": False, "reason": "x"},
    })
    text = "\n".join(lines)
    assert doctor._MARK["missing"] in text, text
    assert "mcp 2.0.0 installed" in text, text
    assert 'pip install "mcp>=1.10,<2"' in text, text


def test_serve_probe_is_skipped_when_the_import_is_broken():
    """No point spawning a server that cannot import: the skip is reported with its
    reason, and the serve probe is never called."""
    def _explode(*a, **kw):
        raise AssertionError("must not spawn a server whose module does not import")

    saved = doctor.mcp_serve_report
    doctor.mcp_serve_report = _explode
    try:
        with _fake_mcp_2_0_0():
            rep = doctor.mcp_report(serve=True)
    finally:
        doctor.mcp_serve_report = saved
    assert rep["import"]["available"] is False, rep
    assert rep["serve"]["checked"] is False and rep["serve"]["ok"] is False, rep
    assert "does not import" in rep["serve"]["reason"], rep


def test_mcp_import_report_on_this_interpreter():
    """Healthy install: the server module imports and the mcp version is reported.
    Where mcp is absent, the same probe must still hand back the pin."""
    rep = doctor.mcp_import_report()
    if HAVE_MCP:
        assert rep["available"] is True, rep
        assert rep["package_version"], rep
        assert "error" not in rep, rep
    else:
        assert rep["available"] is False, rep
        assert doctor.MCP_PIN_FIX in rep["fix"], rep


def test_build_report_defaults_to_no_serve_probe():
    """build_report() is what the MCP server's own setup_status tool calls — a
    default-on serve probe would have the server spawn a copy of itself per call."""
    def _explode(*a, **kw):
        raise AssertionError("build_report() must not spawn a server by default")

    saved = doctor.mcp_serve_report
    doctor.mcp_serve_report = _explode
    try:
        rep = doctor.build_report(probe_version=False)
    finally:
        doctor.mcp_serve_report = saved
    mcp_rep = rep["mcp"]
    assert mcp_rep["serve"] == {"checked": False, "ok": False,
                                "reason": "not requested"}, mcp_rep
    assert "python" in mcp_rep and "import" in mcp_rep, mcp_rep
    text = doctor.render(rep)
    assert "MCP server:" in text and "Solver families:" in text, text


def test_serve_round_trip_against_the_real_server():
    """Healthy install: spawning `ankusdrive mcp` over stdio answers initialize and a
    protocol ping, and the boot time is reported. Skipped without an mcp SDK."""
    if not HAVE_MCP:
        print("    (skipped: no mcp SDK in this interpreter)")
        return
    srv = doctor.mcp_serve_report(timeout=120.0)
    assert srv["checked"] is True, srv
    assert srv["ok"] is True, srv
    assert srv["boot_s"] >= 0.0, srv
    assert srv["tools"] > 100, srv          # the whole point: the tools are served
    assert srv["server"] == "ankusdrive", srv
    line = "\n".join(doctor._fmt_mcp({
        "python": doctor.python_report(), "import": doctor.mcp_import_report(),
        "serve": srv,
    }))
    assert "served" in line and f"{srv['tools']} tools" in line, line
    print(f"    served: initialize {srv['boot_s']:.2f}s, {srv['tools']} tools")


def test_serve_probe_reports_a_server_that_dies_instead_of_raising():
    """Doctor never crashes: a server that exits immediately becomes a reported
    state carrying the child's own stderr, not an exception."""
    if not HAVE_MCP:
        print("    (skipped: no mcp SDK in this interpreter)")
        return
    # A stub with the shape of a `ankusdrive mcp` that cannot import its own server
    # module: it complains on stderr and exits without ever speaking the protocol.
    stub = ("import sys\n"
            "sys.stderr.write('ImportError: cannot import name FastMCP\\n')\n"
            "sys.exit(4)\n")
    srv = doctor.mcp_serve_report(timeout=60.0, argv=[sys.executable, "-c", stub])
    assert srv["checked"] is True and srv["ok"] is False, srv
    assert srv["error"], srv
    assert "FastMCP" in srv.get("stderr", ""), srv
    assert doctor.MCP_PIN_FIX in srv["fix"], srv
    lines = "\n".join(doctor._fmt_mcp({
        "python": doctor.python_report(), "import": doctor.mcp_import_report(),
        "serve": srv,
    }))
    assert "did not serve" in lines and doctor._MARK["missing"] in lines, lines
    assert "stderr: ImportError" in lines, lines      # the child's own last words


def test_cli_doctor_renders_the_mcp_section():
    """End to end through the CLI: the checklist carries an MCP section, and --json
    carries an additive top-level "mcp" key. Exit code is not asserted — this box
    may legitimately lack FreeCAD."""
    env = dict(os.environ)
    r = subprocess.run(
        [sys.executable, "-m", "ankusdrive", "doctor", "--no-boot", "--no-mcp-serve"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env,
    )
    assert "MCP server:" in r.stdout, r.stdout + r.stderr
    assert "python " in r.stdout, r.stdout
    assert "not checked" in r.stdout, r.stdout          # --no-mcp-serve honoured
    rj = subprocess.run(
        [sys.executable, "-m", "ankusdrive", "doctor", "--no-boot", "--json"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env,
    )
    payload = json.loads(rj.stdout)
    assert set(payload) >= {"platform", "freecad", "config", "mcp", "solvers"}, list(payload)
    assert set(payload["mcp"]) == {"python", "import", "serve"}, payload["mcp"]


# --- runner -------------------------------------------------------------------

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

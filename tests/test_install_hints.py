"""Install-hint contract (issue #347) — a fix command must reach the install it is for.

``pip install 'ankusdrive[X]'`` is right for a venv and wrong under pipx, ``uv tool``,
``uvx`` and the Claude Desktop extension: there it installs into some other Python,
the family stays missing, and nothing says why. ``ankusdrive/install_kind.py``
detects the install and rewrites hints; this file holds the codebase to routing
every hint through it.

Three kinds of assertion:

  * **routing** — no module builds a bare ``pip install …ankusdrive[`` string outside
    the allowlisted registries, and each registry's strings reach the user only
    through ``install_kind.adapt``. Swept with ``ast`` over string constants, so
    docstrings and comments (which describe the extras) are not hints.
  * **behaviour** — per kind, the rewritten hint is the right command and keeps the
    registry's notes; non-pip hints (apt, source builds, the dedicated Bempp venv)
    pass through untouched; detection reads the env first, then ``sys.prefix``
    shapes for macOS/Linux and Windows, and reports an unrecognised env value.
  * **propagation** — the FreeCAD worker is spawned with the host's install kind,
    and the MCPB launcher announces ``mcpb`` before importing the package.

Imports NEITHER the package nor any third-party module: ``install_kind.py`` is
stdlib-only and is loaded by path. Pure stdlib, milliseconds, no FreeCAD.

Run:  python3 tests/test_install_hints.py
"""
import ast
import importlib.util
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
BARE_EXTRA = re.compile(r"(?<![\w./\\-])pip install\s+['\"]?ankusdrive\[")


def _ik():
    spec = importlib.util.spec_from_file_location("install_kind_under_test", PKG / "install_kind.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _hint_constants():
    """{relpath: [(lineno, text)]} — every non-docstring string constant that is a
    bare `pip install …ankusdrive[` command (implicitly concatenated literals are one
    Constant, so a hint split over lines is seen whole)."""
    found = {}
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and BARE_EXTRA.search(node.value):
                found.setdefault(str(path.relative_to(REPO)), []).append((node.lineno, node.value))
    return found


# Where a bare hint string may live, and the one function that must hand it out.
ALLOWED = {
    "ankusdrive/solvers.py": "the _SOLVERS registry; find_solver/require_solver go through install_hint()",
    "ankusdrive/analysis/fluids.py": "_INSTALL_HINT; handed out only via _install_hint()",
    "ankusdrive/install_kind.py": "the rewrites themselves (pipx/uv/uvx/mcpb commands)",
}


def test_no_unrouted_hint_strings():
    found = _hint_constants()
    stray = {p: v for p, v in found.items() if p not in ALLOWED}
    assert not stray, f"bare `pip install ankusdrive[…]` hints outside install_kind routing: {stray}"


def test_the_sweep_actually_swept():
    """Guard the guard: the registries must still be seen, or every check above is vacuous."""
    found = _hint_constants()
    assert len(found.get("ankusdrive/solvers.py", [])) >= 6, found.get("ankusdrive/solvers.py")
    assert found.get("ankusdrive/analysis/fluids.py"), "fluids hint not seen"
    assert BARE_EXTRA.search("pip install 'ankusdrive[mbd]'")
    assert not BARE_EXTRA.search(".venv-bempp/bin/pip install bempp-cl"), "dedicated-venv pip is not this interpreter"


def test_registries_hand_out_only_adapted_hints():
    solvers = (PKG / "solvers.py").read_text(encoding="utf-8")
    assert 'info["install_hint"] = install_hint(name)' in solvers, "find_solver no longer routes its hint"
    assert "return _install_kind.adapt(_spec(name)[\"install_hint\"])" in solvers
    # Reading the REGISTRY's string anywhere but install_hint() bypasses the rewrite
    # (reading info["install_hint"], which find_solver already adapted, is fine).
    raw_reads = re.findall(r'(?:spec|_spec\(\w+\)|_SOLVERS\[[^\]]+\])\["install_hint"\]', solvers)
    assert raw_reads == ['_spec(name)["install_hint"]'], \
        f"registry install_hint read outside install_hint(): {raw_reads}"
    fluids = (PKG / "analysis" / "fluids.py").read_text(encoding="utf-8")
    assert fluids.count("_INSTALL_HINT") == 2, "fluids reads _INSTALL_HINT outside _install_hint()"
    worker = (PKG / "worker.py").read_text(encoding="utf-8")
    assert not re.search(r'info\.get\("install_hint",', worker), \
        "worker.py has a raw install_hint fallback — use `or solvers.install_hint(name)`"
    tree = ast.parse((PKG / "doctor.py").read_text(encoding="utf-8"))
    loads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "MCP_PIN_FIX" and isinstance(n.ctx, ast.Load)]
    pin_fix = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_pin_fix")
    inside = {id(n) for n in ast.walk(pin_fix)}
    assert loads and all(id(n) in inside for n in loads), \
        f"doctor reads MCP_PIN_FIX outside _pin_fix() at lines {[n.lineno for n in loads if id(n) not in inside]}"


def test_rewrites_per_kind():
    ik = _ik()
    mbd = ("pip install 'ankusdrive[mbd]'  (pulls mujoco on macOS), or: pip install mujoco")
    env = lambda k: {"ANKUSDRIVE_INSTALL_KIND": k}  # noqa: E731
    assert ik.adapt(mbd, env("venv")) == mbd
    assert ik.adapt(mbd, env("pipx")) == \
        "pipx runpip ankusdrive install 'ankusdrive[mbd]'  (pulls mujoco on macOS), or: pipx runpip ankusdrive install mujoco"
    for k, must in (("uv_tool", "uv tool install --reinstall 'ankusdrive[mbd]'"),
                    ("uvx", "uvx --from 'ankusdrive[mbd]' ankusdrive mcp"),
                    ("mcpb", "pipx install 'ankusdrive[mbd]'")):
        out = ik.adapt(mbd, env(k))
        assert must in out, (k, out)
        assert "(pulls mujoco on macOS)" in out, f"{k} dropped the registry note: {out}"
        assert not BARE_EXTRA.sub("", out).count("pip install 'ankusdrive["), (k, out)
        assert not re.search(r"(?<![\w./\\-])pip install ", out), f"{k} still says a bare pip install: {out}"


def test_non_extra_pip_hints_become_reinstalls():
    ik = _ik()
    pin = 'pip install "mcp>=1.2,<2"'
    assert ik.adapt(pin, {"ANKUSDRIVE_INSTALL_KIND": "pipx"}) == 'pipx runpip ankusdrive install "mcp>=1.2,<2"'
    assert "reinstall" in ik.adapt(pin, {"ANKUSDRIVE_INSTALL_KIND": "mcpb"})
    assert "--refresh" in ik.adapt(pin, {"ANKUSDRIVE_INSTALL_KIND": "uvx"})
    assert ik.adapt(pin, {"ANKUSDRIVE_INSTALL_KIND": "uv_tool"}) == "uv tool install --reinstall ankusdrive"


def test_non_pip_hints_pass_through_every_kind():
    ik = _ik()
    for hint in ("'apt install elmerfem-csc' (Linux)",
                 "source-build openEMS (GPL-3.0): scripts/install-solvers.sh em_gpl",
                 "python3 -m venv .venv-bempp && .venv-bempp/bin/pip install bempp-cl gmsh 'meshio>=5'",
                 ""):
        for k in ik.KINDS:
            assert ik.adapt(hint, {"ANKUSDRIVE_INSTALL_KIND": k}) == hint, (k, hint)


def test_detection():
    ik = _ik()
    cases = {
        "/Users/a/Library/Application Support/Claude/Claude Extensions/local.mcpb.x.ankusdrive/.venv": "mcpb",
        r"C:\Users\a\AppData\Roaming\Claude\Claude Extensions\local.mcpb.x.ankusdrive\.venv": "mcpb",
        "/Users/a/.cache/uv/archive-v0/tywjsHsDtgLGqKFzXPq5J": "uvx",
        r"C:\Users\a\AppData\Local\uv\cache\archive-v0\abc": "uvx",
        "/Users/a/.local/share/uv/tools/ankusdrive": "uv_tool",
        r"C:\Users\a\AppData\Roaming\uv\tools\ankusdrive": "uv_tool",
        "/Users/a/.local/pipx/venvs/ankusdrive": "pipx",
        r"C:\Users\a\pipx\venvs\ankusdrive": "pipx",
        "/Users/a/AnkusDrive/.venv": "venv",
        "/usr": "venv",
    }
    for prefix, want in cases.items():
        got = ik.detect({}, prefix)
        assert got["kind"] == want, (prefix, got)
    assert ik.detect({"ANKUSDRIVE_INSTALL_KIND": "pipx"}, "/usr") == {"kind": "pipx", "source": "env"}
    bogus = ik.detect({"ANKUSDRIVE_INSTALL_KIND": "conda"}, "/Users/a/.local/pipx/venvs/ankusdrive")
    assert bogus == {"kind": "pipx", "source": "prefix", "ignored_env": "conda"}, bogus


def test_worker_gets_the_host_kind():
    ik = _ik()
    assert ik.worker_env({"PATH": "/bin"})["ANKUSDRIVE_INSTALL_KIND"] in ik.KINDS
    assert ik.worker_env({"ANKUSDRIVE_INSTALL_KIND": "mcpb"})["ANKUSDRIVE_INSTALL_KIND"] == "mcpb"
    client = (PKG / "client.py").read_text(encoding="utf-8")
    popen = client[client.index("self.proc = subprocess.Popen("):]
    popen = popen[:popen.index("\n        )") + 10]
    assert "env=_install_kind.worker_env()" in popen, "the worker is spawned without the host's install kind"


def test_mcpb_launcher_announces_before_import():
    src = (REPO / "mcpb" / "src" / "server.py").read_text(encoding="utf-8")
    main = src[src.index("def main()"):]
    announce = main.find('os.environ["ANKUSDRIVE_INSTALL_KIND"] = "mcpb"')
    first_import = main.find("from ankusdrive")
    assert announce != -1, "the MCPB launcher does not set ANKUSDRIVE_INSTALL_KIND=mcpb"
    assert first_import != -1 and announce < first_import, "the install kind is set after the package import"


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

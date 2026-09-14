"""Toolset contract (#377) — every tool is in one family, and the switches mean what they say.

``ankusdrive/toolsets.py`` groups the MCP tools into families an install can switch
off, so a disabled family costs no context. What must hold, none of it visible until
a user misses a tool or pays for tokens they switched off:

  * **coverage** — every ``@mcp.tool()`` is in exactly one family (``apply()`` raises
    at import otherwise; this says so without importing the server).
  * **selection** — unset / ``all`` keeps every family (existing installs lose
    nothing), a list always includes ``core``, and an unknown name raises, never
    silently dropping a family.
  * **reporting** — every disabled family carries a label, a tool count and an
    ``enable`` instruction; the server attaches the report to ``solve_capabilities``
    and doctor to ``setup_status``.
  * **the bundle** — the ``.mcpb`` has one boolean toggle per non-core family, mapped
    to ``ANKUSDRIVE_TOOLSET_<FAMILY>``, defaulting exactly to ``BUNDLE_DEFAULT``; the
    launcher folds them into ``ANKUSDRIVE_TOOLSETS``, and a host that passes no
    toggles still gets the bundle default, not all 281 tools.

Stdlib only (the package itself imports only the stdlib); loads ``toolsets.py`` and the launcher by path. The real token cost of
the bundle default is measured against a budget in ``tests/test_toolsets_budget.py``.

Run:  python3 tests/test_toolsets.py
"""
import ast
import importlib.util
import json
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
# report()/enable_hint() import ankusdrive.install_kind; the package imports only the
# stdlib at top level (test_compat_rename relies on the same), so this stays dep-free.
sys.path.insert(0, str(REPO))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ts():
    return _load("toolsets_under_test", PKG / "toolsets.py")


def _tools():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    return {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
        for d in n.decorator_list)}


def _manifest():
    return json.loads((REPO / "mcpb" / "manifest.json").read_text(encoding="utf-8"))


def test_every_tool_is_in_exactly_one_family():
    ts, tools = _ts(), _tools()
    seen = {}
    for fam, members in ts.FAMILIES.items():
        for t in members:
            assert t not in seen, f"{t} is in both {seen[t]} and {fam}"
            seen[t] = fam
    assert set(seen) == tools, (f"unassigned: {sorted(tools - set(seen))}; "
                                f"not tools: {sorted(set(seen) - tools)}")
    assert len(tools) > 250, "AST sweep saw too few tools"
    assert set(ts.LABELS) == set(ts.FAMILIES), "every family needs a label"
    for must in ("setup_status", "solve_capabilities", "job_result", "new_document"):
        assert must in ts.FAMILIES["core"], f"{must} must stay in core — it is how a user finds the rest"


def test_selection():
    ts = _ts()
    every = tuple(ts.FAMILIES)
    assert ts.selected(None) == every and ts.selected("") == every and ts.selected(" ALL ") == every
    assert ts.selected("fem, simulation") == ("core", "fem", "simulation")
    assert ts.selected("drawings,all") == every
    for bad in ("cfd", "fem,thermal"):
        try:
            ts.selected(bad)
        except ValueError as e:
            assert "valid:" in str(e), e
            continue
        raise AssertionError(f"selected({bad!r}) did not raise")


def test_disabled_families_are_reported_with_an_enable_path():
    ts = _ts()
    rep = ts.report(("core", "fem"))
    assert rep["enabled"] == ["core", "fem"]
    assert set(rep["disabled"]) == set(ts.FAMILIES) - {"core", "fem"}
    for fam, d in rep["disabled"].items():
        assert d["label"] == ts.LABELS[fam] and d["tools"] == len(ts.FAMILIES[fam]), d
        assert d["enable"].strip(), f"{fam} disabled with no way to enable it"
    assert ts.report(tuple(ts.FAMILIES))["disabled"] == {}


def test_server_and_doctor_wire_it_in():
    server = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    at = server.find("TOOLSETS = _toolsets.apply(mcp)")
    assert at != -1, "mcp_server.py never applies toolsets"
    assert server.find("_tool_annotations.apply(mcp)") < at, "annotations must run before toolsets drop tools"
    assert server.rfind("@mcp.tool(") < at, "a tool is registered after toolsets are applied"
    assert 'out["toolsets"] = TOOLSETS' in server, "solve_capabilities does not report toolsets"
    doctor = (PKG / "doctor.py").read_text(encoding="utf-8")
    assert '"toolsets": _toolsets_report()' in doctor, "setup_status/doctor does not report toolsets"


def test_bundle_toggles_match_the_families_and_defaults():
    ts, m = _ts(), _manifest()
    optional = [f for f in ts.FAMILIES if f != "core"]
    uc, env = m["user_config"], m["server"]["mcp_config"]["env"]
    toggles = {k[len("toolset_"):]: v for k, v in uc.items() if k.startswith("toolset_")}
    assert set(toggles) == set(optional), f"toggles {sorted(toggles)} != optional families {sorted(optional)}"
    for fam, opt in toggles.items():
        assert opt["type"] == "boolean" and opt.get("required") is False, (fam, opt)
        assert opt["default"] is (fam in ts.BUNDLE_DEFAULT), f"toolset_{fam} default {opt['default']} disagrees with BUNDLE_DEFAULT"
        assert env.get(f"ANKUSDRIVE_TOOLSET_{fam.upper()}") == f"${{user_config.toolset_{fam}}}", fam
    assert "core" in ts.BUNDLE_DEFAULT and set(ts.BUNDLE_DEFAULT) <= set(ts.FAMILIES)


def test_launcher_composes_toggles():
    ts = _ts()
    shim = _load("shim_under_test", REPO / "mcpb" / "src" / "server.py")
    fams, dflt = tuple(ts.FAMILIES), ts.BUNDLE_DEFAULT
    env = {"ANKUSDRIVE_TOOLSET_SIMULATION": "true", "ANKUSDRIVE_TOOLSET_FEM": "false", "OTHER": "x"}
    got = shim.compose_toolsets(env, fams, dflt)
    want = ",".join(["core", *[f for f in fams if f != "core" and ((f in dflt and f != "fem") or f == "simulation")]])
    assert got == want and env["ANKUSDRIVE_TOOLSETS"] == want, (got, want)
    assert not any(k.startswith("ANKUSDRIVE_TOOLSET_") for k in env), "per-family vars left behind"
    assert ts.selected(got) == tuple(f for f in fams if f in want.split(","))
    bare = {}
    assert shim.compose_toolsets(bare, fams, dflt) == ",".join(dflt), "no toggles passed must mean the bundle default"
    main = (REPO / "mcpb" / "src" / "server.py").read_text(encoding="utf-8")
    body = main[main.index("def main()"):]
    assert body.index("compose_toolsets(") < body.index("from ankusdrive.mcp_server"), \
        "toolsets must be composed before the server registers tools"


def test_config_key_is_top_level():
    cfg = (PKG / "config.py").read_text(encoding="utf-8")
    assert re.search(r'name in \("FREECADCMD", "TOOLSETS"\)', cfg), "toolsets should be a top-level config.toml key"


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
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
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

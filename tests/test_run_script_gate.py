"""run_script gate (#378) — agent-written code runs only where the install allows it.

``ankusdrive/script_policy.py`` turns the MCP ``run_script`` tool into a switch,
``ANKUSDRIVE_ALLOW_RUN_SCRIPT``, off by default in the Claude Desktop extension. Three
tiers, each skipping (and saying so) when its prerequisite is missing:

  * **static** (stdlib, fast lane) — switch parsing (unset -> allowed, junk raises);
    the refusal is structured and names the switch; the server applies the gate after
    toolsets; the MCP wrapper tags ``origin="mcp"``; the worker checks that origin
    BEFORE any ``exec``; package-internal callers pass no origin; the bundle's toggle
    defaults off and the launcher makes a missing toggle explicit-off.
  * **server** (needs ``mcp``) — over real stdio, ``false`` leaves ``run_script`` off
    the tool list and ``setup_status`` reports how to enable it; ``true`` serves it.
  * **worker** (needs FreeCAD) — with the switch off, an ``origin="mcp"`` call is
    refused without executing, while an origin-less call (feature templates, the CLI)
    still runs.

Run:  .venv/bin/python3 tests/test_run_script_gate.py
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- static ---------------------------------------------------------------------

def test_switch_parsing():
    sp = _load("script_policy_under_test", PKG / "script_policy.py")
    assert sp.allowed(None) is True and sp.allowed("") is True, "unset must keep existing installs working"
    for v in ("true", "1", "YES", " on "):
        assert sp.allowed(v) is True, v
    for v in ("false", "0", "No", "off"):
        assert sp.allowed(v) is False, v
    for v in ("maybe", "disabled", "2"):
        try:
            sp.allowed(v)
        except ValueError:
            continue
        raise AssertionError(f"allowed({v!r}) did not raise — a typo must not decide exec")


def test_refusal_is_structured():
    sp = _load("script_policy_under_test2", PKG / "script_policy.py")
    r = sp.refusal()
    assert r["ok"] is False and r["status"] == "disabled" and r["reason"], r
    assert "ANKUSDRIVE_ALLOW_RUN_SCRIPT" in r["enable"] or "Allow run_script" in r["enable"], r


def test_server_wiring():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    ts_at, gate_at = src.find("TOOLSETS = _toolsets.apply(mcp)"), src.find("RUN_SCRIPT = _script_policy.apply(mcp)")
    assert gate_at != -1 and ts_at != -1 and ts_at < gate_at, "gate must apply after toolsets"
    assert src.rfind("@mcp.tool(") < gate_at, "a tool is registered after the gate is applied"
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_script")
    tagged = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_call"
              and any(k.arg == "origin" and getattr(k.value, "value", None) == "mcp" for k in c.keywords)]
    assert tagged, "the MCP run_script wrapper's _call does not pass origin='mcp' (a comment does not count)"


def test_worker_checks_origin_before_exec():
    src = (PKG / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    h = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and any(
        isinstance(d, ast.Call) and getattr(d.func, "id", None) == "handler" and d.args
        and getattr(d.args[0], "value", None) == "run_script" for d in n.decorator_list))
    body = ast.get_source_segment(src, h)
    gate, exe = body.find('p.get("origin") == "mcp"'), body.find("exec(")
    assert gate != -1 and exe != -1 and gate < exe, "worker run_script must check the mcp origin before exec"
    assert "script_policy.refusal()" in body[gate:exe], "the worker gate does not return the structured refusal"
    assert "permitted = False" in body[gate:exe], "an unparseable switch must never enable exec in the worker"


def test_internal_callers_pass_no_origin():
    for rel in ("feature_templates.py", "cli.py"):
        text = (PKG / rel).read_text(encoding="utf-8")
        calls = [ln for ln in text.splitlines() if '"run_script"' in ln and "call(" in ln]
        assert calls, f"{rel}: expected a run_script call site"
        assert all("origin" not in ln for ln in calls), f"{rel} tags origin — package code would be gated"


def test_bundle_defaults_off():
    m = json.loads((REPO / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    opt = m["user_config"]["allow_run_script"]
    assert opt["type"] == "boolean" and opt["default"] is False and opt.get("required") is False, opt
    assert m["server"]["mcp_config"]["env"]["ANKUSDRIVE_ALLOW_RUN_SCRIPT"] == "${user_config.allow_run_script}"
    shim = _load("shim_gate_under_test", REPO / "mcpb" / "src" / "server.py")
    env = {}
    assert shim.default_run_script_off(env) is True and env["ANKUSDRIVE_ALLOW_RUN_SCRIPT"] == "false"
    env = {"ANKUSDRIVE_ALLOW_RUN_SCRIPT": "true"}
    assert shim.default_run_script_off(env) is False and env["ANKUSDRIVE_ALLOW_RUN_SCRIPT"] == "true"
    main = (REPO / "mcpb" / "src" / "server.py").read_text(encoding="utf-8")
    body = main[main.index("def main()"):]
    assert body.index("default_run_script_off(") < body.index("from ankusdrive"), \
        "the default must be set before the package reads the environment"


def test_annotations_still_classify_it_destructive():
    ta = _load("ta_gate_under_test", PKG / "tool_annotations.py")
    assert "run_script" in ta.DESTRUCTIVE and "feature_instantiate" in ta.DESTRUCTIVE


# --- server (needs mcp) -------------------------------------------------------------

def _serve(allow: str):
    env = {**os.environ, "ANKUSDRIVE_ALLOW_RUN_SCRIPT": allow, "ANKUSDRIVE_TOOLSETS": "all"}
    p = subprocess.Popen([sys.executable, "-m", "ankusdrive", "mcp"], cwd=REPO, env=env, text=True, bufsize=1,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def rpc(i, method, params):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited before {method}")
            msg = json.loads(line)
            if msg.get("id") == i:
                return msg["result"]
    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        names = {t["name"] for t in rpc(2, "tools/list", {})["tools"]}
        status = json.loads(rpc(3, "tools/call", {"name": "setup_status", "arguments": {}})["content"][0]["text"])
        return names, status
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def server_test_off_means_unregistered_and_reported():
    names, status = _serve("false")
    assert "run_script" not in names, "run_script still served with ANKUSDRIVE_ALLOW_RUN_SCRIPT=false"
    assert status["run_script"]["allowed"] is False and status["run_script"]["enable"], status["run_script"]
    names, status = _serve("true")
    assert "run_script" in names and status["run_script"] == {"allowed": True}, status.get("run_script")


# --- worker (needs FreeCAD) ---------------------------------------------------------

def worker_test_mcp_origin_refused_internal_runs():
    from ankusdrive import Worker
    saved = os.environ.get("ANKUSDRIVE_ALLOW_RUN_SCRIPT")
    os.environ["ANKUSDRIVE_ALLOW_RUN_SCRIPT"] = "false"
    try:
        with Worker() as w:
            refused = w.call("run_script", code="__result__ = 'EXECUTED'", origin="mcp")
            assert refused.get("ok") is False and refused.get("status") == "disabled", refused
            assert refused.get("result") != "EXECUTED", "gated code ran"
            internal = w.call("run_script", code="__result__ = 'EXECUTED'")
            assert internal.get("result") == "EXECUTED", f"package-internal run_script was blocked: {internal}"
    finally:
        if saved is None:
            os.environ.pop("ANKUSDRIVE_ALLOW_RUN_SCRIPT", None)
        else:
            os.environ["ANKUSDRIVE_ALLOW_RUN_SCRIPT"] = saved


def _freecad_available() -> bool:
    try:
        from ankusdrive import client
        path = client._resolve_freecadcmd()
    except Exception:
        return False
    return bool(path and Path(path).exists())


def _tests():
    g = globals()
    out = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if importlib.util.find_spec("mcp") is None:
        print(f"  SKIP server tier — `mcp` not importable in {sys.executable}")
    else:
        out.append(("server_test_off_means_unregistered_and_reported", server_test_off_means_unregistered_and_reported))
    if not _freecad_available():
        print("  SKIP worker tier — freecadcmd not resolvable")
    else:
        out.append(("worker_test_mcp_origin_refused_internal_runs", worker_test_mcp_origin_refused_internal_runs))
    return out


def main():
    failures = []
    t_suite = time.time()
    tests = _tests()
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

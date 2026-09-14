"""session_transcript tool (#409, epic #309) — the export, within the directory policy.

The tool that returns a session as a regenerating script. Anthropic's Software
Directory Policy (#368) constrains it, and each constraint is a test here:

  * **static** (stdlib, fast lane)
      - registered, classified ``READ_ONLY``, in the ``core`` family, and listed as a
        host-side tool (no worker handler);
      - **writes nothing:** no path / file / directory parameter (a tool that writes to a
        caller path is DESTRUCTIVE by the annotation registry's rule), and the function
        body contains no file-writing call;
      - **no replay-as-tool:** no MCP tool executes a transcript or script other than the
        gated ``run_script`` (a transcript runner would reopen #380);
      - the docstring documents its return keys.
  * **server** (needs ``mcp``) — over real stdio with the Claude Desktop bundle's
    default families, the tool is listed with ``readOnlyHint: true``; an empty workspace
    returns a valid script and a note; host-side calls it records are skipped, not
    replayed.
  * **worker** (needs FreeCAD) — after a real build, the returned script compiles,
    links handles, checks the volume, and carries no recording path.

Run:  .venv/bin/python3 tests/test_session_transcript.py
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))
TOOL = "session_transcript"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _server_tree():
    return ast.parse((PKG / "mcp_server.py").read_text(encoding="utf-8"))


def _tool_fns():
    return {n.name: n for n in _server_tree().body if isinstance(n, ast.FunctionDef) and any(
        isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool" for d in n.decorator_list)}


# --- static -------------------------------------------------------------------------

def test_registered_read_only_core_host_side():
    fns = _tool_fns()
    assert TOOL in fns, "session_transcript is not an @mcp.tool()"
    ta = _load("ta_for_transcript", PKG / "tool_annotations.py")
    assert TOOL in ta.READ_ONLY and TOOL not in ta.ADDITIVE and TOOL not in ta.DESTRUCTIVE
    assert ta.hints_for(TOOL)["readOnlyHint"] is True
    ts = _load("ts_for_transcript", PKG / "toolsets.py")
    assert TOOL in ts.FAMILIES["core"], "the transcript must be available in every install"
    calls = [c for c in ast.walk(fns[TOOL]) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_call"]
    assert not calls, "session_transcript should read the host-side journal, not call the worker"


def test_writes_nothing():
    fn = _tool_fns()[TOOL]
    params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    for p in params:
        assert not any(w in p for w in ("path", "file", "dir", "out")), \
            f"parameter {p!r} suggests a write target; writing makes the tool DESTRUCTIVE (#368)"
    for c in ast.walk(fn):
        if isinstance(c, ast.Call):
            name = getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            assert name not in {"open", "write_text", "write_bytes", "mkdir", "dump"}, name
    doc = ast.get_docstring(fn)
    assert "Returns {" in doc and "script" in doc and "writes nothing" in doc, doc


def test_no_tool_executes_a_transcript():
    runners = []
    for name, fn in _tool_fns().items():
        if name == "run_script":
            continue
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        if params & {"code", "script", "transcript", "journal"}:
            runners.append(name)
        if any(k in name for k in ("replay", "transcript")) and name != TOOL:
            runners.append(name)
    assert not runners, (f"{runners} look like transcript/script runners — replay-as-tool would "
                         "reopen the run_script guardrail (#380); run transcripts outside MCP")


# --- server (needs mcp) -------------------------------------------------------------

def _rpc_session(env_extra):
    env = {**os.environ, **env_extra}
    p = subprocess.Popen([sys.executable, "-m", "ankusdrive", "mcp"], cwd=REPO, env=env, text=True,
                         bufsize=1, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    n = {"i": 0}

    def rpc(method, params):
        n["i"] += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n["i"], "method": method, "params": params}) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited before {method}")
            msg = json.loads(line)
            if msg.get("id") == n["i"]:
                return msg.get("result") or msg
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test_session_transcript", "version": "1"}})
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    p.stdin.flush()
    return p, rpc


def server_test_listed_read_only_and_empty_session_is_valid():
    ts = _load("ts_for_transcript2", PKG / "toolsets.py")
    p, rpc = _rpc_session({"ANKUSDRIVE_TOOLSETS": ",".join(ts.BUNDLE_DEFAULT)})
    try:
        tools = {t["name"]: t for t in rpc("tools/list", {})["tools"]}
        assert TOOL in tools, "not served with the bundle's default families"
        ann = tools[TOOL].get("annotations") or {}
        assert ann.get("readOnlyHint") is True and ann.get("title"), ann

        def call(name, args):
            r = rpc("tools/call", {"name": name, "arguments": args})
            return json.loads(r["content"][0]["text"])
        call("list_workspaces", {})
        out = call(TOOL, {})
        assert out["workspace"] == "default" and out["exported"] == 0, out
        assert out["skipped"] == {"workspace management": 1}, out["skipped"]
        compile(out["script"], "<transcript>", "exec")
        other = call(TOOL, {"workspace": "nobody"})
        assert other["calls"] == 0 and "no tool calls recorded" in other["note"], other
        again = call(TOOL, {})
        # both earlier transcript calls ran in the current workspace, whatever they exported
        assert again["skipped"].get("the transcript tool itself") == 2, again["skipped"]
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


# --- worker (needs FreeCAD) ---------------------------------------------------------

def worker_test_real_session_exports():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    call = mcp_server.mcp._tool_manager.call_tool

    def run(_tool, **args):
        return asyncio.run(call(_tool, args))
    rec = tempfile.mkdtemp()
    journal.clear()
    try:
        run("new_document", name="transcript_test")
        box = run("add_primitive", kind="box", w=30, d=10, h=4)
        cyl = run("add_primitive", kind="cylinder", r=2, h=4)
        cut = run("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])
        run("mass_properties", handle=cut["handle"])
        run("list_faces", handle=cut["handle"])
        run("save_document", path=str(Path(rec) / "part.FCStd"))
        out = run(TOOL)
    finally:
        mcp_server._cleanup()
    script = out["script"]
    compile(script, "<transcript>", "exec")
    assert "base=box_1, tool=cylinder_1" in script, script
    assert "'volume_mm3'" in script and rec not in script, script
    assert out["skipped"] == {"read-only": 1}, out["skipped"]
    assert out["exported"] == 6 and not out["truncated"], out


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
        print(f"  SKIP server + worker tiers — `mcp` not importable in {sys.executable}")
        return out
    out.append(("server_test_listed_read_only_and_empty_session_is_valid",
                server_test_listed_read_only_and_empty_session_is_valid))
    if not _freecad_available():
        print("  SKIP worker tier — freecadcmd not resolvable")
    else:
        out.append(("worker_test_real_session_exports", worker_test_real_session_exports))
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
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")
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

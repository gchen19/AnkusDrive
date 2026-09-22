"""Toolset token budget (#377) — what the bundle default actually costs a client.

Anthropic's directory policy: "MCP servers must be frugal with their use of tokens."
Before #377 every session paid for all 281 tool definitions — 456,735 characters of
``tools/list`` (~114k tokens). The Claude Desktop bundle now registers only
``toolsets.BUNDLE_DEFAULT`` (core, drawings, fem) unless the user turns more on:
measured at 112 tools / 124,921 characters (~31k tokens) when this test was written.

This starts the real server over stdio in a child process, once with the bundle
default and once with ``all``, and asserts:

  * the bundle default's ``tools/list`` stays under BUDGET_CHARS — a family quietly
    growing, or a tool landing in core that belongs elsewhere, shows up here;
  * ``all`` still serves every tool, so opting in loses nothing;
  * a disabled family's tools are really absent from the wire, not just unreported.

Measured in JSON characters of the tool list, the same unit the budget was set in;
~4 characters per token is the working conversion.

Needs the ``mcp`` package (not FreeCAD); SKIPs, saying so, when it is not importable
in this interpreter.

Run:  .venv/bin/python3 tests/test_toolsets_budget.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))   # the shared _mcp_python helper
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402

BUDGET_CHARS = 140_000          # bundle default measured at 124,921 (2026-09-13)


def _tools_list(toolsets: str) -> list:
    env = {**os.environ, "ANKUSDRIVE_TOOLSETS": toolsets}
    p = subprocess.Popen([sys.executable, "-m", "ankusdrive", "mcp"], cwd=REPO, env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, bufsize=1)

    def rpc(i, method, params):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        deadline = time.time() + 120
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited before answering {method}")
            msg = json.loads(line)
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]
        raise TimeoutError(method)

    try:
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "test_toolsets_budget", "version": "1"}})
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        return rpc(2, "tools/list", {})["tools"]
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def test_bundle_default_fits_the_budget():
    from ankusdrive import toolsets
    tools = _tools_list(",".join(toolsets.BUNDLE_DEFAULT))
    chars = len(json.dumps(tools))
    names = {t["name"] for t in tools}
    want = frozenset().union(*(toolsets.FAMILIES[f] for f in toolsets.BUNDLE_DEFAULT))
    print(f"    bundle default {toolsets.BUNDLE_DEFAULT}: {len(tools)} tools, {chars} chars (~{chars // 4} tokens), budget {BUDGET_CHARS}")
    assert names == want, f"served {sorted(names ^ want)[:8]} differ from the bundle default's families"
    assert chars <= BUDGET_CHARS, (
        f"bundle default tools/list is {chars} chars, over the {BUDGET_CHARS} budget — move tools out of "
        f"{toolsets.BUNDLE_DEFAULT} or trim the longest descriptions (#377)")


def test_all_serves_everything_and_off_means_absent():
    from ankusdrive import toolsets
    every = frozenset().union(*toolsets.FAMILIES.values())
    all_tools = {t["name"] for t in _tools_list("all")}
    assert all_tools == every, f"'all' is missing {sorted(every - all_tools)[:8]}"
    lean = {t["name"] for t in _tools_list("fem")}
    leaked = lean & toolsets.FAMILIES["simulation"]
    assert not leaked, f"disabled simulation tools still served: {sorted(leaked)[:5]}"
    assert "setup_status" in lean, "core must always be served"


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    # An interpreter with `mcp` (and the rest of the host deps), found by probing —
    # not assumed to be the one run_all.sh started (#449). Re-execs, or returns.
    ensure_mcp_interpreter(__file__, needs=HOST_DEPS)
    if importlib.util.find_spec("mcp") is None:
        print(f"  SKIP test_toolsets_budget — {mcp_skip_reason()}")
        return
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

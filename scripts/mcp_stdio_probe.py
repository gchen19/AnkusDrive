#!/usr/bin/env python3
"""Start an MCP server command over stdio and prove it serves: initialize, list tools,
call one tool. Stdlib only — no `mcp` client — so it runs anywhere the command does.

Used by CI against the introspection image (`docker run -i --rm ankusdrive-glama`),
which is what Glama does with it (#201). Exits non-zero, naming the step, when the
server does not start, lists fewer tools than --min-tools, or the probe tool errors
at the protocol level (a tool-level `isError` result is fine: without FreeCAD most
tools are expected to degrade, and that is the contract being shown).

Usage:  scripts/mcp_stdio_probe.py [--min-tools N] [--call TOOL] -- <command> [args...]
"""
import argparse
import json
import subprocess
import sys
import threading


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tools", type=int, default=1)
    ap.add_argument("--call", default="setup_status")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        ap.error("no server command given")

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    responses: dict = {}
    ready = threading.Condition()

    def reader():
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue          # a server must not write non-JSON to stdout; ignore, the ids will time out
            if "id" in msg:
                with ready:
                    responses[msg["id"]] = msg
                    ready.notify_all()

    threading.Thread(target=reader, daemon=True).start()

    def request(i, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                     **({"params": params} if params is not None else {})}) + "\n")
        proc.stdin.flush()
        with ready:
            if not ready.wait_for(lambda: i in responses, timeout=a.timeout):
                raise SystemExit(f"FAIL {method}: no response within {a.timeout}s")
        msg = responses[i]
        if "error" in msg:
            raise SystemExit(f"FAIL {method}: {msg['error']}")
        return msg["result"]

    try:
        init = request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "mcp_stdio_probe", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        print(f"initialize: {init.get('serverInfo')}")
        tools = request(2, "tools/list", {})["tools"]
        described = sum(1 for t in tools if (t.get("description") or "").strip())
        print(f"tools/list: {len(tools)} tools, {described} described")
        if len(tools) < a.min_tools:
            raise SystemExit(f"FAIL tools/list: {len(tools)} < --min-tools {a.min_tools}")
        if described != len(tools):
            raise SystemExit(f"FAIL tools/list: {len(tools) - described} tools have no description")
        if a.call:
            res = request(3, "tools/call", {"name": a.call, "arguments": {}})
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            print(f"tools/call {a.call}: isError={res.get('isError', False)} {' '.join(text.split())[:160]}")
        print("PASS")
        return 0
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())

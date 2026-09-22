"""Session journal (#407, epic #309) — every MCP tool call recorded, nothing changed.

``ankusdrive/journal.py`` wraps each registered tool so a session can later be exported
as a script that regenerates it (#408). Three tiers, each skipping (and saying so)
when its prerequisite is missing:

  * **static** (stdlib, fast lane) — the wrapper against stub tools: entry fields,
    results returned by identity and errors re-raised as the same object, compaction
    of bulky results, the entry cap keeps the oldest calls, workspace / worker-pid /
    transaction-depth tracking, idempotent wrapping, a failing journal never breaks a
    tool, thread-safe ordering; and the server applies it after every tool filter.
  * **server** (needs ``mcp``) — every registered tool is journaled, and a call through
    FastMCP returns what the unwrapped function returns and records it; a raising tool
    raises the same error and records it.
  * **worker** (needs FreeCAD) — a real build: handles in results, transaction depth,
    and a new ``worker_pid`` after ``restart_worker``.

Run:  .venv/bin/python3 tests/test_session_journal.py
"""
import ast
import importlib.util
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))   # the shared _mcp_python helper
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402


def _journal():
    spec = importlib.util.spec_from_file_location("journal_under_test", PKG / "journal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stub:
    """A FastMCP stand-in: ``_tool_manager._tools`` of objects with ``fn``."""

    def __init__(self, **fns):
        self._tool_manager = SimpleNamespace(
            _tools={n: SimpleNamespace(fn=f) for n, f in fns.items()})
        self.ws, self.pid = "default", 100

    def apply(self, j):
        return j.apply(self, workspace_of=lambda: self.ws, worker_pid_of=lambda _ws: self.pid)

    def call(self, _tool, **kw):
        return self._tool_manager._tools[_tool].fn(**kw)


# --- static -------------------------------------------------------------------------

def test_entry_fields_and_identity():
    j = _journal()
    marker = {"handle": "box_1", "volume": 1000.0}
    s = _Stub(add_primitive=lambda **kw: marker)
    info = s.apply(j)
    assert info["recorded_tools"] == 1, info
    got = s.call("add_primitive", kind="box", w=10, d=10, h=10)
    assert got is marker, "the client must get the tool's own object, not a copy"
    (e,) = j.snapshot()["entries"]
    assert e["seq"] == 1 and e["tool"] == "add_primitive" and e["ok"] is True, e
    assert e["args"] == {"kind": "box", "w": 10, "d": 10, "h": 10}, e["args"]
    assert e["result"] == marker and e["workspace"] == "default" and e["worker_pid"] == 100, e
    assert e["txn_depth"] == 0 and e["elapsed_s"] >= 0 and "error" not in e, e


def test_error_reraised_unchanged_and_recorded():
    j = _journal()
    err = ValueError("no such face")

    def raising(**_):
        raise err
    s = _Stub(resolve_face=raising)
    s.apply(j)
    try:
        s.call("resolve_face", handle="box_1", face="Face99")
    except ValueError as e:
        assert e is err, "the journal must re-raise the tool's own exception"
    else:
        raise AssertionError("error swallowed")
    (e,) = j.snapshot()["entries"]
    assert e["ok"] is False and e["error"] == "ValueError: no such face" and "result" not in e, e


def test_results_compacted_args_kept_whole():
    j = _journal()
    png = "iVBOR" + "A" * 50_000
    code = "x = 1\n" * 1000
    big = list(range(1000))
    s = _Stub(render_view=lambda **_: {"png_base64": png, "points": big, "handle": "view_1"})
    s.apply(j)
    out = s.call("render_view", code=code)
    assert out["png_base64"] is png, "compaction touched the returned value"
    (e,) = j.snapshot()["entries"]
    assert e["args"]["code"] == code, "arguments must be kept whole for replay"
    r = e["result"]
    assert r["handle"] == "view_1", r
    assert r["png_base64"]["__elided__"] == "str" and r["png_base64"]["len"] == len(png), r["png_base64"]
    assert len(r["points"]) == j.MAX_LIST + 1 and r["points"][-1] == {"__elided__": "items", "count": 1000 - j.MAX_LIST}
    assert j.compact(object()).startswith("<object"), "non-JSON values become str()"


def test_cap_keeps_the_oldest():
    j = _journal()
    j.MAX_ENTRIES = 3
    s = _Stub(ping=lambda **_: "pong")
    s.apply(j)
    for _ in range(5):
        s.call("ping")
    snap = j.snapshot()
    assert [e["seq"] for e in snap["entries"]] == [1, 2, 3], snap["entries"]
    assert snap["dropped"] == 2 and snap["truncated"] is True, snap


def test_workspace_filter_and_pid_change():
    j = _journal()
    s = _Stub(new_document=lambda **_: {"doc": "a"}, restart_worker=lambda **_: {"restarted": True})
    s.apply(j)
    s.call("new_document", name="a")
    s.ws = "agent2"
    s.call("new_document", name="b")
    s.ws, s.pid = "default", 101          # a fresh worker in the default workspace
    s.call("new_document", name="c")
    default = j.snapshot("default")["entries"]
    assert [e["args"]["name"] for e in default] == ["a", "c"], default
    assert [e["worker_pid"] for e in default] == [100, 101], default
    assert [e["args"]["name"] for e in j.snapshot("agent2")["entries"]] == ["b"]


def test_transaction_depth_tracks_and_resets_with_worker():
    j = _journal()
    s = _Stub(transaction_open=lambda **_: {}, transaction_abort=lambda **_: {},
              transaction_commit=lambda **_: {}, pad=lambda **_: {})
    s.apply(j)
    s.call("transaction_open")
    s.call("pad")                         # depth 1
    s.call("transaction_open")
    s.call("pad")                         # depth 2
    s.call("transaction_abort")
    s.call("pad")                         # depth 1
    s.call("transaction_commit")
    s.call("pad")                         # depth 0
    s.call("transaction_open")
    s.pid = 200                           # worker restarted: its transaction died with it
    s.call("pad")
    pads = [e["txn_depth"] for e in j.snapshot()["entries"] if e["tool"] == "pad"]
    assert pads == [1, 2, 1, 0, 0], pads


def test_failed_open_does_not_deepen():
    j = _journal()

    def bad_open(**_):
        raise RuntimeError("already open")
    s = _Stub(transaction_open=bad_open, pad=lambda **_: {})
    s.apply(j)
    try:
        s.call("transaction_open")
    except RuntimeError:
        pass
    s.call("pad")
    assert j.snapshot()["entries"][-1]["txn_depth"] == 0


def test_wrap_is_idempotent_and_keeps_metadata():
    j = _journal()

    def list_faces(handle: str) -> list:
        """List faces."""
        return []
    s = _Stub(list_faces=list_faces)
    s.apply(j)
    s.apply(j)
    fn = s._tool_manager._tools["list_faces"].fn
    assert fn.__wrapped__ is list_faces, "wrapped twice"
    assert fn.__name__ == "list_faces" and fn.__doc__ == "List faces."
    s.call("list_faces", handle="h")
    assert j.snapshot()["count"] == 1


def test_broken_journal_never_breaks_a_tool():
    j = _journal()
    s = _Stub(ping=lambda **_: "pong")
    j.apply(s, workspace_of=lambda: 1 / 0, worker_pid_of=lambda _ws: None)
    assert s.call("ping") == "pong"
    j2 = _journal()
    s2 = _Stub(ping=lambda **_: "pong")
    calls = {"n": 0}

    def pid_of(_ws):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("worker gone")
        return 1
    j2.apply(s2, workspace_of=lambda: "default", worker_pid_of=pid_of)
    assert s2.call("ping") == "pong", "a failure while recording must not surface"


def test_concurrent_calls_get_unique_ordered_seq():
    j = _journal()
    s = _Stub(ping=lambda **_: time.sleep(0.001) or "pong")
    s.apply(j)
    threads = [threading.Thread(target=lambda: [s.call("ping") for _ in range(50)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e["seq"] for e in j.snapshot()["entries"]]
    assert seqs == list(range(1, 401)), (len(seqs), seqs[:5])


def test_snapshot_is_a_copy_and_clear_resets():
    j = _journal()
    s = _Stub(ping=lambda **_: {"a": 1})
    s.apply(j)
    s.call("ping")
    j.snapshot()["entries"][0]["tool"] = "tampered"
    assert j.snapshot()["entries"][0]["tool"] == "ping"
    j.clear()
    s.call("ping")
    snap = j.snapshot()
    assert snap["count"] == 1 and snap["entries"][0]["seq"] == 1 and snap["dropped"] == 0, snap


def test_server_applies_journal_last():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    at = src.find("JOURNAL = _journal.apply(mcp")
    assert at != -1, "mcp_server.py does not apply the journal"
    for earlier in ("_tool_annotations.apply(mcp)", "TOOLSETS = _toolsets.apply(mcp)",
                    "RUN_SCRIPT = _script_policy.apply(mcp)"):
        pos = src.find(earlier)
        assert pos != -1 and pos < at, f"journal must apply after {earlier}"
    assert src.rfind("@mcp.tool(") < at, "a tool is registered after the journal is applied"


def test_journal_writes_nothing_to_disk():
    tree = ast.parse((PKG / "journal.py").read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            assert name not in {"open", "write_text", "write_bytes", "dump", "mkdir"}, \
                f"journal.py calls {name}() — the journal is memory-only (PRIVACY.md)"


# --- server (needs mcp) -------------------------------------------------------------

def server_test_every_tool_journaled_and_results_unchanged():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    tools = mcp_server.mcp._tool_manager._tools
    bare = [n for n, t in tools.items() if not getattr(t.fn, "__ankusdrive_journaled__", False)]
    assert not bare, f"tools not journaled: {bare}"
    assert mcp_server.JOURNAL["recorded_tools"] == len(tools), mcp_server.JOURNAL

    journal.clear()
    direct = tools["list_workspaces"].fn.__wrapped__()
    via = asyncio.run(mcp_server.mcp._tool_manager.call_tool("list_workspaces", {}))
    assert via == direct, (via, direct)
    try:
        asyncio.run(mcp_server.mcp._tool_manager.call_tool("use_workspace", {"name": ""}))
    except Exception as e:
        assert "workspace name must be a non-empty string" in str(e), e
    else:
        raise AssertionError("use_workspace('') did not raise")
    entries = journal.snapshot()["entries"]
    assert [e["tool"] for e in entries] == ["list_workspaces", "use_workspace"], entries
    assert entries[0]["ok"] and entries[0]["result"] == direct and entries[0]["worker_pid"] is None
    assert not entries[1]["ok"] and entries[1]["args"] == {"name": ""} and "non-empty" in entries[1]["error"]


# --- worker (needs FreeCAD) ---------------------------------------------------------

def worker_test_real_build_records_handles_txn_and_restart():
    import asyncio
    os.environ.setdefault("ANKUSDRIVE_TOOLSETS", "all")
    from ankusdrive import journal, mcp_server
    call = mcp_server.mcp._tool_manager.call_tool

    def run(_tool, **args):
        return asyncio.run(call(_tool, args))
    journal.clear()
    try:
        run("new_document", name="journal_test")
        box = run("add_primitive", kind="box", w=10, d=20, h=5)
        run("transaction_open", label="try")
        run("add_primitive", kind="cylinder", r=2, h=5)
        run("transaction_abort")
        run("restart_worker")
        run("ping")
    finally:
        mcp_server._cleanup()
    e = {x["seq"]: x for x in journal.snapshot("default")["entries"]}
    assert all(x["ok"] for x in e.values()), [x for x in e.values() if not x["ok"]]
    assert e[2]["result"]["handle"] == box["handle"], e[2]
    assert [e[i]["txn_depth"] for i in (4, 5)] == [1, 1], "the cylinder and abort ran inside the bracket"
    assert e[6]["txn_depth"] == 0
    assert e[1]["worker_pid"] == e[5]["worker_pid"] != e[7]["worker_pid"], \
        "restart_worker must show up as a new worker_pid"


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
        print(f"  SKIP server tier — {mcp_skip_reason()}")
        return out
    out.append(("server_test_every_tool_journaled_and_results_unchanged",
                server_test_every_tool_journaled_and_results_unchanged))
    if not _freecad_available():
        print("  SKIP worker tier — freecadcmd not resolvable")
    else:
        out.append(("worker_test_real_build_records_handles_txn_and_restart",
                    worker_test_real_build_records_handles_txn_and_restart))
    return out


def main():
    # An interpreter with `mcp` (and the rest of the host deps), found by probing —
    # not assumed to be the one run_all.sh started (#449). Re-execs, or returns.
    ensure_mcp_interpreter(__file__, needs=HOST_DEPS)
    failures = []
    t_suite = time.time()
    tests = _tests()
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
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

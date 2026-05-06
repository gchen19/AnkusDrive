"""
Toy problems for the DriftPin MCP server. Each test spins up the server as a
subprocess via the MCP client SDK, exercises one tool, verifies the response.

Must run with the venv python (where `mcp` is installed):
    .venv/bin/python3 tests/test_mcp.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

VENV_PY = str(REPO / ".venv" / "bin" / "python3")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


SERVER_PARAMS = StdioServerParameters(
    command=VENV_PY,
    args=["-m", "driftpin", "mcp"],
    cwd=str(REPO),
)


def _text_payload(result):
    """Pull the JSON / text out of a CallToolResult."""
    assert not result.isError, f"tool error: {result}"
    parts = []
    for c in result.content:
        if hasattr(c, "text"):
            parts.append(c.text)
    return "".join(parts)


async def _with_session(fn):
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


# --- tests --------------------------------------------------------------------

async def _test_list_tools(session):
    tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    expected = {
        "ping", "version", "new_document", "open_document", "save_document",
        "list_objects", "add_primitive", "boolean_op", "export_shape",
        "run_script", "fem_cantilever_demo",
    }
    assert expected <= names, f"missing tools: {expected - names}"


async def _test_ping(session):
    r = await session.call_tool("ping", {})
    assert "pong" in _text_payload(r)


async def _test_version(session):
    r = await session.call_tool("version", {})
    payload = _text_payload(r)
    assert "1.1" in payload, payload
    assert "3.11" in payload, payload


async def _test_session_state_persists(session):
    """Two tool calls in one session must see the same ActiveDocument."""
    await session.call_tool("new_document", {"name": "mcpstate"})
    await session.call_tool("add_primitive", {"kind": "box", "w": 10, "d": 10, "h": 10})
    r = await session.call_tool("list_objects", {})
    payload = _text_payload(r)
    assert "Part::Box" in payload, payload


async def _test_cad_chain(session):
    """Make a box, make a cylinder, cut — all through MCP."""
    await session.call_tool("new_document", {"name": "mcpchain"})
    box_r = await session.call_tool(
        "add_primitive",
        {"kind": "box", "w": 20, "d": 20, "h": 20},
    )
    box = json.loads(_text_payload(box_r))
    cyl_r = await session.call_tool(
        "add_primitive",
        {"kind": "cylinder", "r": 5, "h": 20, "placement": [10, 10, 0]},
    )
    cyl = json.loads(_text_payload(cyl_r))
    cut_r = await session.call_tool(
        "boolean_op",
        {"op": "cut", "base": box["handle"], "tool": cyl["handle"]},
    )
    cut = json.loads(_text_payload(cut_r))
    expected = 20 * 20 * 20 - 3.14159 * 25 * 20
    assert abs(cut["volume"] - expected) / expected < 0.01


async def _test_export_roundtrip(session):
    with tempfile.TemporaryDirectory() as tmp:
        fcstd = os.path.join(tmp, "r.FCStd")
        step = os.path.join(tmp, "r.step")
        await session.call_tool("new_document", {"name": "roundtrip"})
        await session.call_tool("add_primitive", {"kind": "box", "w": 10, "d": 10, "h": 10})
        await session.call_tool("save_document", {"path": fcstd})
        assert os.path.isfile(fcstd)
        await session.call_tool("export_shape", {"path": step})
        assert os.path.getsize(step) > 0
        with open(step) as f:
            assert "ISO-10303" in f.read(100)


async def _test_run_script_tool(session):
    r = await session.call_tool("run_script", {
        "code": (
            "doc = App.newDocument('mcpscript')\n"
            "b = doc.addObject('Part::Box', 'B')\n"
            "b.Length = 3; b.Width = 4; b.Height = 5\n"
            "doc.recompute()\n"
            "__result__ = {'volume': b.Shape.Volume}\n"
        ),
    })
    payload = _text_payload(r)
    assert "60" in payload, payload


async def _test_tool_error_returns_cleanly(session):
    """A failing tool call must not crash the session; subsequent calls still work."""
    r = await session.call_tool("boolean_op", {"op": "cut", "base": "nope_1", "tool": "nope_2"})
    assert r.isError, "expected error result, got success"
    r2 = await session.call_tool("ping", {})
    assert "pong" in _text_payload(r2)


async def _test_restart_worker_clears_state(session):
    """restart_worker must drop all in-process state: an open document and
    handle from before the restart should be gone after."""
    await session.call_tool("new_document", {"name": "before_restart"})
    box_r = await session.call_tool(
        "add_primitive", {"kind": "box", "w": 5, "d": 5, "h": 5},
    )
    box = json.loads(_text_payload(box_r))
    assert box["handle"], box

    rr = await session.call_tool("restart_worker", {})
    payload = json.loads(_text_payload(rr))
    assert payload["restarted"] is True, payload
    assert payload["freecad"], payload

    docs_r = await session.call_tool("list_documents", {})
    docs_text = _text_payload(docs_r)
    docs = json.loads(docs_text) if docs_text else []
    assert all(d["name"] != "before_restart" for d in docs), docs

    # Stale handle should now be unknown.
    bad = await session.call_tool("get_object", {"handle": box["handle"]})
    assert bad.isError, f"expected stale handle to fail, got: {bad}"

    # Worker is healthy: ping responds, can create a fresh doc.
    pong = await session.call_tool("ping", {})
    assert "pong" in _text_payload(pong)


async def _test_fem_cantilever_tool(session):
    with tempfile.TemporaryDirectory() as tmp:
        r = await session.call_tool(
            "fem_cantilever_demo",
            {"mesh_size": 500.0, "workdir": tmp},
        )
        payload = _text_payload(r)
        data = json.loads(payload)
        assert data["nodes"] > 0
        assert data["max_displacement_mm"] > 0
        assert data["max_vonmises_mpa"] > 0
        print(f"    FEM via MCP: nodes={data['nodes']} "
              f"|u|max={data['max_displacement_mm']:.4f}mm "
              f"vM_max={data['max_vonmises_mpa']:.2f}MPa")


# --- runner -------------------------------------------------------------------

ASYNC_TESTS = [
    ("test_list_tools", _test_list_tools),
    ("test_ping", _test_ping),
    ("test_version", _test_version),
    ("test_session_state_persists", _test_session_state_persists),
    ("test_cad_chain", _test_cad_chain),
    ("test_export_roundtrip", _test_export_roundtrip),
    ("test_run_script_tool", _test_run_script_tool),
    ("test_tool_error_returns_cleanly", _test_tool_error_returns_cleanly),
    ("test_restart_worker_clears_state", _test_restart_worker_clears_state),
    ("test_fem_cantilever_tool", _test_fem_cantilever_tool),
]


async def _run_one(name, fn):
    t0 = time.time()
    try:
        await _with_session(fn)
    except Exception as e:
        return (name, time.time() - t0, e, traceback.format_exc())
    return (name, time.time() - t0, None, None)


async def _amain():
    results = []
    for name, fn in ASYNC_TESTS:
        results.append(await _run_one(name, fn))
    return results


def main():
    t_suite = time.time()
    results = asyncio.run(_amain())
    failures = [(n, e, tb) for n, _, e, tb in results if e]
    for name, dt, err, _ in results:
        status = "FAIL" if err else "PASS"
        msg = f"  {type(err).__name__}: {err}" if err else ""
        print(f"  {status} {name:40s} ({dt:.2f}s){msg}")

    print()
    if failures:
        print(f"== {len(failures)}/{len(results)} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(results)}/{len(results)} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()

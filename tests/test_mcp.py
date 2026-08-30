"""
Toy problems for the AnkusDrive MCP server. Each test spins up the server as a
subprocess via the MCP client SDK, exercises one tool, verifies the response.

Needs an interpreter carrying the `mcp` client SDK AND a working FreeCAD: these
tools drive the real worker — documents, primitives, a restart that must clear
state, a CalculiX cantilever solve. The FreeCAD-free "can this interpreter serve
MCP at all" check is a different file, tests/test_mcp_boot.py, which is what the
hosted Windows core-install lane runs on a machine with no FreeCAD. Two files, two
questions; neither replaces the other.

INTERPRETER RESOLUTION (#288). This used to hardcode ``.venv/bin/python3``, which
was wrong three ways: POSIX-only, so Windows could not run the file at all even
though the MCP surface is fully supported there; dependent on a ``.venv`` existing
inside the repo, which is false for a pip/pipx install and for any git worktree;
and free to disagree with the interpreter actually running the test. Now it
derives instead of hardcoding, the same rule ``ankusdrive/client.py`` follows for
freecadcmd:

  1. if THIS interpreter has ``mcp``, the server runs under it — true whether that
     is a venv, a conda env, a worktree, or a plain ``pip install -e .``;
  2. else, if the repo venv has it, re-exec there (per-platform layout, so
     ``Scripts\\python.exe`` on Windows and ``bin/python3`` elsewhere) — a bare
     ``python3 tests/test_mcp.py`` should not silently drop the suite when the deps
     are one directory away;
  3. else SKIP, naming both interpreters it tried.

Run:  python3 tests/test_mcp.py        (any interpreter with `mcp` importable)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Guards the step-2 re-exec against looping if the venv python also lacks `mcp`
# in some way the probe did not catch.
_REEXEC_FLAG = "ANKUSDRIVE_TEST_MCP_REEXEC"


def _venv_python(root: Path) -> Path:
    """The interpreter a venv rooted at *root* exposes, per platform."""
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")


def _imports_mcp(python: Path) -> bool:
    """Ask an interpreter, rather than assuming from its path."""
    try:
        return subprocess.run(
            [str(python), "-c", "import mcp"],
            capture_output=True, timeout=120,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


try:
    from mcp import ClientSession, StdioServerParameters  # noqa: E402
    from mcp.client.stdio import stdio_client  # noqa: E402
except ImportError:
    _venv_py = _venv_python(REPO / ".venv")
    if not os.environ.get(_REEXEC_FLAG) and _venv_py.is_file() and _imports_mcp(_venv_py):
        os.environ[_REEXEC_FLAG] = "1"
        os.execv(str(_venv_py), [str(_venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])
    _where = f"{sys.executable}"
    _where += f" nor {_venv_py}" if _venv_py.is_file() else " and no repo venv was found"
    print(f"SKIP tests/test_mcp.py — the `mcp` client SDK is not importable in {_where}. "
          "Install the host deps (pip install -e .) to run this suite.")
    raise SystemExit(0)


# Whatever interpreter got here has `mcp`, so it can serve as well as call.
SERVER_PYTHON = sys.executable

SERVER_PARAMS = StdioServerParameters(
    command=SERVER_PYTHON,
    args=["-m", "ankusdrive", "mcp"],
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
        with open(step, encoding="utf-8") as f:
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


async def _test_setup_surface(session):
    # issue #202: the agent-guided setup surface — a status tool, a canned prompt,
    # and a resource, all read-only.
    status = json.loads(_text_payload(
        await session.call_tool("setup_status", {})))
    assert status["platform"]["system"], status
    assert "freecad" in status and "path" in status["freecad"], status
    fams = status["solvers"]["families"]
    assert fams, status
    for fam, info in fams.items():
        assert "any_available" in info, (fam, info)
        if not info["any_available"]:
            # every unavailable family must carry an actionable fix on its solvers
            states = [status["solvers"]["solvers"][s] for s in info["solvers"]]
            assert any(st.get("install_hint") or st.get("wire_hint")
                       for st in states), (fam, states)
    prompts = await session.list_prompts()
    assert "diagnose_setup" in {p.name for p in prompts.prompts}, prompts
    got = await session.get_prompt("diagnose_setup", {})
    text = "".join(m.content.text for m in got.messages
                   if hasattr(m.content, "text"))
    assert "setup_status" in text, text
    resources = await session.list_resources()
    assert any(str(r.uri) == "ankusdrive://setup" for r in resources.resources), resources
    read = await session.read_resource("ankusdrive://setup")
    body = "".join(c.text for c in read.contents if hasattr(c, "text"))
    assert "Solver families:" in body and "platform:" in body, body[:400]


# --- runner -------------------------------------------------------------------

ASYNC_TESTS = [
    ("test_list_tools", _test_list_tools),
    ("test_setup_surface", _test_setup_surface),
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

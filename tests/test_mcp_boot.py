"""The MCP server boots over stdio and advertises its tools — WITHOUT FreeCAD (#279).

This is the step where #279's reporter got stuck: `pip install` succeeded, but
"the MCP step" never came up, and nothing in the install path proved the server
could speak stdio at all. That failure has two shapes, and both pass `ankusdrive
ping`/`doctor`:

  * a bad dependency resolve — mcp 2.0.0 drops ``mcp.server.fastmcp`` (#277), so
    the import dies at server start while ping/doctor keep working;
  * a Python the resolved dep set doesn't actually support, which is only visible
    once something imports it.

So this test spawns the REAL server (``<this python> -m ankusdrive mcp``) with
FreeCAD deliberately pointed at a nonexistent path, completes the MCP
``initialize`` handshake, and lists tools. Everything up to and including
``tools/list`` is host-side only — the worker boots lazily on the first tool
CALL — so a green result here means "this interpreter can serve MCP", which is
exactly what the Windows core-install lane needs to assert on a runner with no
FreeCAD installed.

SKIPs when the ``mcp`` package isn't importable in this interpreter (the Linux
suite's system python3 has no venv deps).

Run:  .venv/bin/python3 tests/test_mcp_boot.py
"""
import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:                                     # noqa: BLE001 - reported below
    ClientSession = None

# A path that cannot exist, so the server has to boot with FreeCAD unresolved. The
# whole point is that stdio serving does NOT depend on the CAD backend.
_NO_FREECAD = str(REPO / "_no_such_freecadcmd_for_test")

# Tools every build advertises; a subset check keeps this from being a churn magnet
# while still failing if the registry didn't load.
_EXPECT = {"ping", "version", "new_document", "add_primitive", "export_shape",
           "fem_cantilever_demo"}


def _server_params():
    # sys.executable, not a hardcoded .venv/bin/python3: this must run from the venv
    # on Linux/macOS AND from .venv\Scripts\python.exe on Windows, and in CI from
    # whatever interpreter the matrix picked.
    env = dict(os.environ)
    env["ANKUSDRIVE_FREECADCMD"] = _NO_FREECAD
    return StdioServerParameters(
        command=sys.executable, args=["-m", "ankusdrive", "mcp"],
        cwd=str(REPO), env=env,
    )


async def _handshake():
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            return init, {t.name for t in tools.tools}


def test_stdio_initialize_and_list_tools_without_freecad():
    """`python -m ankusdrive mcp` completes the MCP handshake and lists its tools on a
    box with no FreeCAD — the check the Windows core-install CI lane runs."""
    assert not os.path.exists(_NO_FREECAD), "fixture path must not exist"
    init, names = asyncio.run(asyncio.wait_for(_handshake(), timeout=120))
    assert init.serverInfo.name, init
    missing = _EXPECT - names
    assert not missing, f"MCP server advertised no {sorted(missing)} (got {len(names)} tools)"
    # The registry is large; a handful of tools would mean a half-loaded module.
    assert len(names) >= 200, f"only {len(names)} tools advertised — registry did not load"


def test_fastmcp_symbol_is_importable():
    """ankusdrive/mcp_server.py imports `mcp.server.fastmcp.FastMCP`; mcp 2.x dropped it
    (#277). Fail here with the resolved version rather than at a user's MCP host."""
    from mcp.server.fastmcp import FastMCP           # noqa: F401
    from importlib.metadata import version
    assert int(version("mcp").split(".")[0]) < 2, (
        f"mcp {version('mcp')} is 2.x — it has no mcp.server.fastmcp; "
        "pyproject pins mcp<2 (#277), so this resolve is wrong"
    )


# --- runner -------------------------------------------------------------------

def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    if ClientSession is None:
        print("  SKIP test_mcp_boot — the `mcp` package is not installed in "
              f"{sys.executable} (run from the venv: pip install -e .)")
        return
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:60s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:60s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()

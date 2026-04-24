"""
DriftPin MCP server — exposes the worker handlers as MCP tools over stdio.

One Worker is spawned at server startup and reused across tool calls, so
ActiveDocument state persists across an MCP session (the whole point).

Run:   .venv/bin/python3 -m driftpin mcp
"""
import atexit
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import Worker, WorkerError


mcp = FastMCP("driftpin")

_worker: Worker | None = None


def _ensure_worker() -> Worker:
    global _worker
    if _worker is None or _worker.proc.poll() is not None:
        _worker = Worker()
    return _worker


def _call(method: str, **params: Any) -> Any:
    try:
        return _ensure_worker().call(method, **params)
    except WorkerError as e:
        raise RuntimeError(f"{e.type}: {e.remote_message}") from e


@atexit.register
def _cleanup():
    global _worker
    if _worker is not None:
        try:
            _worker.shutdown(timeout=3.0)
        except Exception:
            pass
        _worker = None


# --- tools --------------------------------------------------------------------

@mcp.tool()
def ping() -> str:
    """Check that the FreeCAD worker is alive. Returns 'pong' on success."""
    return _call("ping")


@mcp.tool()
def version() -> dict:
    """Return FreeCAD and bundled Python versions from the worker."""
    return _call("version")


@mcp.tool()
def new_document(name: str = "part") -> dict:
    """Create a new FreeCAD document and make it active. Returns {doc: <name>}."""
    return _call("new_document", name=name)


@mcp.tool()
def open_document(path: str) -> dict:
    """Open an existing .FCStd file, make it active. Returns doc name and object list."""
    return _call("open_document", path=path)


@mcp.tool()
def save_document(path: str) -> dict:
    """Save the active document to the given .FCStd path."""
    return _call("save_document", path=path)


@mcp.tool()
def list_objects() -> list:
    """List objects in the active document. Returns [{name, type, label}, ...]."""
    return _call("list_objects")


@mcp.tool()
def add_primitive(
    kind: str,
    w: float = 10.0,
    d: float = 10.0,
    h: float = 10.0,
    r: float = 5.0,
    placement: list | None = None,
) -> dict:
    """Add a primitive to the active document.

    kind: 'box' (uses w, d, h), 'cylinder' (uses r, h), or 'sphere' (uses r).
    placement: optional [x, y, z] mm translation.
    Returns {handle, name, volume}. The handle (e.g. 'box_1') is how you
    reference this object in subsequent boolean_op calls.
    """
    params = {"kind": kind, "w": w, "d": d, "h": h, "r": r}
    if placement is not None:
        params["placement"] = placement
    return _call("add_primitive", **params)


@mcp.tool()
def boolean_op(op: str, base: str, tool: str) -> dict:
    """Boolean operation on two existing objects, referenced by their handles.

    op: 'cut' (base minus tool), 'fuse' (union), or 'common' (intersection).
    base, tool: handles returned from add_primitive (e.g. 'box_1', 'cylinder_1').
    Returns {handle, volume}.
    """
    return _call("boolean_op", op=op, base=base, tool=tool)


@mcp.tool()
def export_shape(
    path: str, object: str | None = None
) -> dict:
    """Export a shape to STEP/IGES/BREP/STL. Format inferred from path extension.

    object: FreeCAD object name (NOT a DriftPin handle). If omitted, exports
    the first shaped object in the active document.
    """
    return _call("export_shape", path=path, object=object)


@mcp.tool()
def run_script(code: str) -> Any:
    """Escape hatch: execute Python in the worker with App/Part/ObjectsFem in scope.

    Set `__result__` in the script to return a JSON-serializable value.
    Useful when a needed FreeCAD operation doesn't have a dedicated tool yet.
    """
    return _call("run_script", code=code)


@mcp.tool()
def fem_cantilever_demo(
    length: float = 8000.0,
    width: float = 1000.0,
    height: float = 1000.0,
    force: float = 9_000_000.0,
    mesh_size: float = 500.0,
    workdir: str = "/tmp/driftpin_fem",
) -> dict:
    """Run the built-in cantilever FEM demo end-to-end (geometry → mesh → CalculiX).

    Dimensions in mm, force in N. Returns {nodes, tets, max_displacement_mm,
    max_vonmises_mpa, workdir}. A fresh document is created; existing state
    in the session is NOT overwritten but a new document becomes active.
    """
    return _call(
        "fem_cantilever_demo",
        _timeout=300.0,
        length=length,
        width=width,
        height=height,
        force=force,
        mesh_size=mesh_size,
        workdir=workdir,
    )


def run():
    mcp.run()


if __name__ == "__main__":
    run()

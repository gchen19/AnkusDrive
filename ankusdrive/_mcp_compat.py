"""The one place that knows which line of the ``mcp`` SDK is installed (#516).

AnkusDrive serves on both lines of the SDK, ``mcp>=1.10,<3``:

  * mcp 1.x ships the high-level server as ``mcp.server.fastmcp.FastMCP``.
  * mcp 2.0.0 renamed it ``mcp.server.mcpserver.MCPServer`` and left
    ``mcp.server.fastmcp`` as a module that only raises ``ModuleNotFoundError``
    (the #277 break), so a 1.x-only import is dead on arrival there.

The registration surface AnkusDrive uses (``.tool()`` / ``.prompt()`` /
``.resource()`` / ``.run()``, and ``_tool_manager._tools`` of ``Tool`` objects
with ``fn`` / ``title`` / ``annotations``) is the same on both lines, so the rest
of the server imports ``Server`` from here and does not branch. The differences
that do matter are handled here, once:

  * **Crash text.** mcp 1.x reports any exception a tool raises to the client as
    ``Error executing tool <name>: <message>``. mcp 2.x reports only an
    anticipated ``ToolError`` that way; anything else becomes an
    ``UnexpectedToolError`` whose client-facing text is just
    ``Error executing tool <name>``. AnkusDrive's tools fail by raising (a
    worker's ``RuntimeError("TypeError: ...")``, ``workspace pool full ...``), and
    that message is what tells the agent what to fix, so
    :func:`as_tool_error` turns a 2.x crash back into a ``ToolError`` with the
    1.x text.

Client-side code is not handled here, because importing this module imports the
SDK's server half: mcp 2.x result models use snake_case attributes
(``is_error``, ``server_info``) where 1.x used the wire's camelCase, and the two
readers (doctor's serve probe, tests/_mcp_python.is_error) try both names.
"""
from __future__ import annotations


try:                                         # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as Server
    from mcp.server.mcpserver.exceptions import ToolError
    MAJOR = 2
    try:
        # Added after 2.0.0. 2.0.x still wraps every tool exception as a ToolError
        # with the original text, so there is nothing to undo on those releases.
        from mcp.server.mcpserver.exceptions import UnexpectedToolError
    except ImportError:
        UnexpectedToolError = None
except ModuleNotFoundError:                  # mcp 1.10 .. 1.x
    from mcp.server.fastmcp import FastMCP as Server
    from mcp.server.fastmcp.exceptions import ToolError
    UnexpectedToolError = None
    MAJOR = 1

__all__ = ["MAJOR", "Server", "ToolError", "as_tool_error"]


def as_tool_error(exc: BaseException) -> BaseException:
    """The exception to raise to the client for a failed tool call.

    On mcp 2.x an ``UnexpectedToolError`` (a tool body that raised anything but
    ``ToolError``/``MCPError``) is rebuilt as a ``ToolError`` carrying the original
    message, which is what mcp 1.x sent. Everything else, and every exception on
    mcp 1.x, is returned unchanged."""
    if UnexpectedToolError is None or not isinstance(exc, UnexpectedToolError):
        return exc
    cause = exc.__cause__
    if cause is None or isinstance(cause, ToolError):
        return exc
    rebuilt = ToolError(f"{exc}: {cause}")
    rebuilt.__cause__ = cause
    return rebuilt


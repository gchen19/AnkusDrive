"""The session journal — every MCP tool call, in order, for export (#407, epic #309).

The ``.FCStd`` records where a session ended, not how it got there, and the agent's
reasoning is in no file at all. The journal is the missing record: tool name, the
arguments as FastMCP validated them, whether the call succeeded, and what it returned.
The exporter (#408) turns it into a Python script that regenerates the model and its
simulations through the same tools.

Recorded at the **MCP tool layer**, not in ``mcp_server._call``. Tool names and
arguments are the public surface a script can replay; ``_call`` sees worker methods,
and some tools call the worker several times (``render_view``) or never
(``use_workspace``). :func:`apply` wraps every registered tool's ``fn`` once, after
the server has registered and filtered them, so a new ``@mcp.tool()`` is recorded
without anyone remembering to add it.

Each entry:

* ``seq`` — call-start order across the whole server, from 1.
* ``tool``, ``args`` — the name and the full validated keyword arguments, defaults
  included, as a JSON-safe copy. Arguments are kept whole: replay needs them exactly
  (a sketch's geometry, ``run_script``'s code).
* ``ok``, and ``result`` or ``error`` (``"Type: message"``). The result is a
  **compacted** copy (:func:`compact`): long strings such as base64 renders and long
  lists are replaced by a marker with their size, since the exporter needs the handles
  and scalars a result carries, not its payloads. The client still gets the real value.
* ``workspace`` — the workspace current when the call started.
* ``worker_pid`` — that workspace's worker process after the call, or ``None``. A
  change of pid within one workspace means a fresh worker (restart, close and reuse,
  idle reap, a crash): handles and documents from before it are gone, and the exporter
  starts a new section there.
* ``txn_depth`` — open ``transaction_open`` brackets in this workspace when the call
  started, so the exporter can tell which calls an abort undid. Resets with the worker.
* ``elapsed_s``.

Memory only. The journal lives in the server process and ends with it: nothing is
written to disk (PRIVACY.md). It is capped at :data:`MAX_ENTRIES`; past the cap new
calls are counted but not stored, and :func:`snapshot` says so. The start of a session
is what replay needs, so the oldest entries are never the ones dropped.

Journaling never changes what a tool returns or raises. A failure inside the journal
itself is swallowed, never the tool's.
"""
from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from typing import Any, Callable

MAX_ENTRIES = 20_000
MAX_STR = 512          # longer result strings are elided
MAX_LIST = 256         # longer result lists keep this many items
MAX_DEPTH = 16

_OPEN, _CLOSE = "transaction_open", ("transaction_commit", "transaction_abort")

_lock = threading.Lock()
_entries: list[dict] = []
_state = {"seq": 0, "dropped": 0}
_txn: dict[str, list] = {}         # workspace -> [worker_pid, depth]


def compact(value: Any, _depth: int = 0) -> Any:
    """A JSON-safe copy of ``value`` with bulky payloads replaced by size markers.

    Strings over :data:`MAX_STR` become ``{"__elided__": "str", "len", "sha1"}``;
    lists and tuples over :data:`MAX_LIST` keep their first :data:`MAX_LIST` items
    followed by ``{"__elided__": "items", "count": <how many were dropped>}``.
    Anything that is not JSON becomes its ``str()``."""
    if _depth > MAX_DEPTH:
        return {"__elided__": "depth"}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_STR:
            return value
        return {"__elided__": "str", "len": len(value),
                "sha1": hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()}
    if isinstance(value, (bytes, bytearray)):
        return {"__elided__": "bytes", "len": len(value)}
    if isinstance(value, dict):
        return {str(k): compact(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out = [compact(v, _depth + 1) for v in value[:MAX_LIST]]
        if len(value) > MAX_LIST:
            out.append({"__elided__": "items", "count": len(value) - MAX_LIST})
        return out
    return str(value)


def _copy_args(kwargs: dict) -> dict:
    """A detached JSON-safe copy of the arguments, kept whole."""
    return json.loads(json.dumps(kwargs, default=str))


def _txn_depth_locked(workspace: str, pid) -> int:
    rec = _txn.get(workspace)
    if rec is None or rec[0] != pid:
        return 0
    return rec[1]


def _after_locked(entry: dict) -> None:
    """Advance the per-workspace transaction depth once a call has finished."""
    ws, pid = entry["workspace"], entry["worker_pid"]
    rec = _txn.get(ws)
    if rec is None or rec[0] != pid:
        rec = _txn[ws] = [pid, 0]
    if not entry["ok"]:
        return
    if entry["tool"] == _OPEN:
        rec[1] += 1
    elif entry["tool"] in _CLOSE:
        rec[1] = max(0, rec[1] - 1)


def record(tool: str, args: dict, started: float, *, ok: bool, result: Any = None,
           error: BaseException | None = None, workspace: str, worker_pid,
           seq: int, txn_depth: int) -> None:
    """Store one finished call. Called by the wrapper; public for tests."""
    entry = {"seq": seq, "tool": tool, "args": args, "ok": ok,
             "workspace": workspace, "worker_pid": worker_pid, "txn_depth": txn_depth,
             "elapsed_s": round(time.monotonic() - started, 3)}
    if ok:
        entry["result"] = compact(result)
    else:
        entry["error"] = f"{type(error).__name__}: {error}"
    with _lock:
        _after_locked(entry)
        if len(_entries) >= MAX_ENTRIES:
            _state["dropped"] += 1
            return
        _entries.append(entry)


def wrap(name: str, fn: Callable, workspace_of: Callable[[], str],
         worker_pid_of: Callable[[str], Any]) -> Callable:
    """Return ``fn`` wrapped to journal each call. Idempotent."""
    if getattr(fn, "__ankusdrive_journaled__", False):
        return fn

    @functools.wraps(fn)
    def journaled(*a, **kwargs):
        try:
            started = time.monotonic()
            workspace = workspace_of()
            args = _copy_args(kwargs)
            with _lock:
                _state["seq"] += 1
                seq = _state["seq"]
                depth = _txn_depth_locked(workspace, worker_pid_of(workspace))
        except Exception:                      # the journal must never break a tool
            return fn(*a, **kwargs)
        try:
            result = fn(*a, **kwargs)
        except BaseException as e:
            _safe_record(name, args, started, ok=False, error=e, workspace=workspace,
                         worker_pid_of=worker_pid_of, seq=seq, txn_depth=depth)
            raise
        _safe_record(name, args, started, ok=True, result=result, workspace=workspace,
                     worker_pid_of=worker_pid_of, seq=seq, txn_depth=depth)
        return result

    journaled.__ankusdrive_journaled__ = True
    return journaled


def _safe_record(name, args, started, *, worker_pid_of, workspace, **kw) -> None:
    try:
        record(name, args, started, workspace=workspace,
               worker_pid=worker_pid_of(workspace), **kw)
    except Exception:
        pass


def apply(mcp, workspace_of: Callable[[], str],
          worker_pid_of: Callable[[str], Any]) -> dict:
    """Wrap every tool registered on ``mcp`` (a FastMCP). Run after the server has
    registered and filtered its tools. Returns ``{recorded_tools, max_entries}``."""
    tools = mcp._tool_manager._tools
    for name, tool in tools.items():
        tool.fn = wrap(name, tool.fn, workspace_of, worker_pid_of)
    return {"recorded_tools": len(tools), "max_entries": MAX_ENTRIES}


def snapshot(workspace: str | None = None) -> dict:
    """The journal so far, oldest first: ``{entries, count, dropped, truncated}``.
    ``workspace`` keeps only the calls started in that workspace. Entries are copies."""
    with _lock:
        entries = [e for e in _entries if workspace is None or e["workspace"] == workspace]
        entries = json.loads(json.dumps(sorted(entries, key=lambda e: e["seq"])))
        dropped = _state["dropped"]
    return {"entries": entries, "count": len(entries), "dropped": dropped,
            "truncated": dropped > 0}


def clear() -> None:
    """Forget every entry (tests, and a future explicit reset)."""
    with _lock:
        _entries.clear()
        _txn.clear()
        _state["seq"] = 0
        _state["dropped"] = 0

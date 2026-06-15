"""
Static contract tests for the two parallel command registries.

DriftPin exposes every capability twice: a worker-side handler (`@handler("x")`
in driftpin/worker.py) and an MCP tool (`@mcp.tool()` in driftpin/mcp_server.py)
that marshals to it via `_call("x")`. These two registries are authored by hand
and MUST stay in lockstep — a forgotten tool wrapper, a copy-pasted `_call`
target, or a double-pasted handler is a silent break.

These tests guard that contract by STATIC PARSING (ast + regex). They import
NEITHER module, so they need no FreeCAD, no mcp, no numpy — pure stdlib. That's
deliberate: they run in seconds on a stock runner, giving fast feedback even
when the FreeCAD test box is busy or offline.

Run:  python3 tests/test_contracts.py
"""
import ast
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "driftpin" / "worker.py"
MCP = REPO / "driftpin" / "mcp_server.py"


# --- known, intentional exceptions (keep these honest; the tests verify that
#     every allowlisted name still exists, so stale entries get flagged) -------

# Tools that legitimately do NOT dispatch to a worker handler via _call(...).
_NO_DISPATCH = {"restart_worker"}  # tears down / respawns the worker process itself

# Tools whose function name intentionally differs from the handler they call.
_NAME_DIFFERS_OK = {"render_view", "render_views"}  # both call "tessellate", rasterize host-side

# Handlers intentionally NOT surfaced as an MCP tool (worker-internal only).
_INTERNAL_HANDLERS = {"list_handles", "recompute_stress"}

# Docstring ratchet: tools that predate the "document your return value" rule.
# New tools are held to the standard (test_tools_document_return_value); these
# legacy ones are exempted until their docstrings are improved. BURN THIS DOWN —
# do not add to it. (Seeded 2026-06-01 from the then-current corpus.)
_RETURNS_GRANDFATHERED = {
    'add_part', 'add_projection_group', 'add_sketch_constraint', 'add_sketch_external', 'draft',
    'export_shape', 'fem_add_constraint', 'fem_buckling',
    'fem_mesh_refinement', 'fem_modal', 'fem_set_material', 'fem_set_solver', 'get_object',
    'hole', 'linear_pattern', 'list_assembly_parts', 'list_documents', 'loft',
    'make_datum_plane', 'make_drawing_page', 'mirrored', 'pad', 'partdesign_chamfer',
    'partdesign_fillet', 'pocket', 'polar_pattern', 'resolve_edge', 'resolve_face', 'revolve',
    'save_document', 'set_property', 'set_visibility', 'sweep', 'thickness',
    'transaction_abort', 'transaction_commit', 'transaction_open',
}

_MIN_DOCSTRING_LEN = 40  # floor; the current shortest real docstring is 51 chars


# --- static introspection (no imports of the target modules) ------------------

def _handler_names():
    """All names registered via @handler("name") in worker.py, in file order
    (a list, so duplicates are detectable)."""
    return re.findall(r'@handler\(\s*["\']([^"\']+)["\']\s*\)', WORKER.read_text())


def _is_tool_decorator(d):
    # matches @mcp.tool() (a Call on an attribute named "tool") or bare @mcp.tool
    if isinstance(d, ast.Call):
        return getattr(d.func, "attr", None) == "tool"
    return getattr(d, "attr", None) == "tool"


def _call_target(func):
    """The string literal X in the first `_call("X", ...)` inside func, or None."""
    for sub in ast.walk(func):
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "_call":
            if sub.args and isinstance(sub.args[0], ast.Constant):
                return sub.args[0].value
    return None


def _tool_defs():
    """One dict per @mcp.tool function: {name, target, doc, params, lineno}."""
    tree = ast.parse(MCP.read_text())
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(
            _is_tool_decorator(d) for d in node.decorator_list
        ):
            out.append({
                "name": node.name,
                "target": _call_target(node),
                "doc": ast.get_docstring(node) or "",
                "params": [a.arg for a in node.args.args],
                "lineno": node.lineno,
            })
    return out


# parsed once at import; the tests below read these
_HANDLER_LIST = _handler_names()
_HANDLERS = set(_HANDLER_LIST)
_TOOLS = _tool_defs()
_TOOL_NAMES = {t["name"] for t in _TOOLS}
_DISPATCH_TARGETS = {t["target"] for t in _TOOLS if t["target"]}


# --- parity tests -------------------------------------------------------------

def test_handlers_and_tools_both_found():
    """Sanity: the parsers actually found the registries (guards against a
    refactor that moves/renames the decorators and silently empties these)."""
    assert len(_HANDLER_LIST) > 50, f"only {len(_HANDLER_LIST)} handlers parsed"
    assert len(_TOOLS) > 50, f"only {len(_TOOLS)} tools parsed"


def test_no_duplicate_handlers():
    """Each @handler name is registered exactly once. A duplicate means a
    second def silently shadowed the first in the HANDLERS dict (a real risk
    when handlers are assembled/pasted in bulk)."""
    seen, dupes = set(), []
    for n in _HANDLER_LIST:
        (dupes.append(n) if n in seen else seen.add(n))
    assert not dupes, f"duplicate @handler registrations: {sorted(set(dupes))}"


def test_no_duplicate_tools():
    """Each @mcp.tool function name is unique (a duplicate def would overwrite
    the earlier tool registration)."""
    names = [t["name"] for t in _TOOLS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate @mcp.tool functions: {dupes}"


def test_every_tool_dispatches_to_a_real_handler():
    """Every tool either marshals to an existing handler via _call("X"), or is
    explicitly listed as a non-dispatching tool. Catches a tool wired to a
    handler name that doesn't exist (typo / renamed handler)."""
    offenders = []
    for t in _TOOLS:
        if t["name"] in _NO_DISPATCH:
            continue
        if t["target"] is None:
            offenders.append(f"{t['name']} (no _call) @ L{t['lineno']}")
        elif t["target"] not in _HANDLERS:
            offenders.append(f"{t['name']} -> _call({t['target']!r}) has no handler")
    assert not offenders, "tools not dispatching to a real handler:\n  " + "\n  ".join(offenders)


def test_no_dispatch_allowlist_is_honest():
    """Every tool on the _NO_DISPATCH allowlist really has no _call (so the
    allowlist can't hide a genuinely-broken tool)."""
    by_name = {t["name"]: t for t in _TOOLS}
    for name in _NO_DISPATCH:
        assert name in by_name, f"_NO_DISPATCH lists {name!r}, which is not a tool"
        assert by_name[name]["target"] is None, (
            f"_NO_DISPATCH lists {name!r}, but it DOES dispatch to "
            f"{by_name[name]['target']!r} — drop it from the allowlist"
        )


def test_tool_name_matches_its_handler():
    """A dispatching tool is named the same as the handler it calls, unless it's
    an explicit exception. Catches a copy-pasted tool left calling the wrong
    handler."""
    offenders = []
    for t in _TOOLS:
        tgt = t["target"]
        if tgt is None or t["name"] in _NAME_DIFFERS_OK:
            continue
        if tgt != t["name"]:
            offenders.append(f"{t['name']} -> _call({tgt!r})")
    assert not offenders, (
        "tool name != handler it calls (add to _NAME_DIFFERS_OK if intended):\n  "
        + "\n  ".join(offenders)
    )
    # honesty: every _NAME_DIFFERS_OK tool actually differs from its target
    by_name = {t["name"]: t for t in _TOOLS}
    for name in _NAME_DIFFERS_OK:
        t = by_name.get(name)
        assert t is not None, f"_NAME_DIFFERS_OK lists {name!r}, which is not a tool"
        assert t["target"] and t["target"] != name, (
            f"_NAME_DIFFERS_OK lists {name!r}, but its name already matches its "
            f"target {t['target']!r} — drop it"
        )


def test_no_orphan_handlers():
    """Every handler is reachable from at least one MCP tool, unless it's an
    explicit worker-internal handler. Catches the exact failure of this codebase:
    a handler added without its tool wrapper."""
    orphans = sorted(_HANDLERS - _DISPATCH_TARGETS - _INTERNAL_HANDLERS)
    assert not orphans, (
        "handlers with no MCP tool (add the tool, or list as internal):\n  "
        + "\n  ".join(orphans)
    )
    # honesty: every _INTERNAL_HANDLERS name is a real, genuinely-unexposed handler
    for name in _INTERNAL_HANDLERS:
        assert name in _HANDLERS, f"_INTERNAL_HANDLERS lists {name!r}, not a handler"
        assert name not in _DISPATCH_TARGETS, (
            f"_INTERNAL_HANDLERS lists {name!r}, but a tool DOES expose it — drop it"
        )


# --- docstring contract -------------------------------------------------------
#
# The MCP tool docstring is the ONLY spec the calling LLM sees, so a missing or
# stub docstring silently degrades every agent. These gate the universal minimum
# for all tools, plus a forward-only "document your return value" ratchet.

def test_every_tool_has_a_real_docstring():
    """Every tool has a non-trivial docstring with a one-line summary and no
    leftover placeholder text."""
    offenders = []
    for t in _TOOLS:
        doc = t["doc"].strip()
        first = doc.splitlines()[0].strip() if doc else ""
        if not doc:
            offenders.append(f"{t['name']}: empty docstring")
        elif len(doc) < _MIN_DOCSTRING_LEN:
            offenders.append(f"{t['name']}: docstring only {len(doc)} chars (< {_MIN_DOCSTRING_LEN})")
        elif not first:
            offenders.append(f"{t['name']}: no summary line")
        elif doc.upper().startswith(("TODO", "FIXME", "XXX")):
            offenders.append(f"{t['name']}: placeholder docstring ({first!r})")
    assert not offenders, "tool docstring problems:\n  " + "\n  ".join(offenders)


def test_tools_document_return_value():
    """Ratchet: a tool's docstring must describe what it returns (so the agent
    knows the shape of the result), unless it's a grandfathered legacy tool.
    New tools have no excuse — document the return keys."""
    offenders = [
        t["name"] for t in _TOOLS
        if "eturn" not in t["doc"] and t["name"] not in _RETURNS_GRANDFATHERED
    ]
    assert not offenders, (
        "tools whose docstring never mentions what they return:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n(document the return value; do NOT add to _RETURNS_GRANDFATHERED)"
    )


def test_returns_grandfather_list_has_no_stale_entries():
    """Every grandfathered tool still exists AND still lacks a return mention.
    When someone improves a legacy docstring, this fails until they remove it
    from the allowlist — that's how the ratchet tightens."""
    by_name = {t["name"]: t for t in _TOOLS}
    stale = []
    for name in _RETURNS_GRANDFATHERED:
        t = by_name.get(name)
        if t is None:
            stale.append(f"{name}: no longer a tool")
        elif "eturn" in t["doc"]:
            stale.append(f"{name}: now documents its return — remove from grandfather list")
    assert not stale, "stale _RETURNS_GRANDFATHERED entries:\n  " + "\n  ".join(sorted(stale))


# --- runner (mirrors tests/test_worker.py) ------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

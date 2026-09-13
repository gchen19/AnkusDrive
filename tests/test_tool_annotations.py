"""Tool-annotation contract (#368) — every tool is classified, and "read-only" is true.

``ankusdrive/tool_annotations.py`` gives each MCP tool a title and a behaviour class
(read-only / additive / destructive) that clients use to decide what may run without
asking. Two ways that goes wrong, neither visible until a user is bitten:

  * **coverage** — a new ``@mcp.tool()`` nobody classified. ``apply()`` raises at
    import for that, but the fast lane should say so without importing the server.
  * **honesty** — a tool marked READ_ONLY whose worker handler plainly mutates. The
    audit that produced the classes used these same static signals; this test keeps
    them binding. A match is not proof of mutation (temp documents and temp files
    created and removed before returning are fine), so each such tool needs an
    entry in ``READ_ONLY_DESPITE_SIGNAL`` with the reason it is still read-only.

Also: tool names within the 64-character limit, a title for every tool, a reason
for every destructive mark, and the shipped ``mcp`` floor new enough to carry titles.

Imports NEITHER the package nor any third-party module: it parses ``mcp_server.py``
and ``worker.py`` with ``ast`` and loads ``tool_annotations.py`` by path.

Run:  python3 tests/test_tool_annotations.py
"""
import ast
import functools
import importlib.util
import re
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "ankusdrive"

# Static signs that a handler changes user-owned state.
MUTATION = re.compile(
    r"removeObject\(|closeDocument\(|\.saveAs\(|\bexec\(|shutil\.rmtree\(|"
    r"open\([^)\n]*['\"][wa]b?['\"]|\.write_text\(|\.write_bytes\(|"
    r"\.Placement\s*=|\.Visibility\s*=|purge_results\(")

# READ_ONLY tools whose handler matches MUTATION, verified by reading the code.
READ_ONLY_DESPITE_SIGNAL = {
    "assembly_lock_check": "opens component files in temp documents to hash them, closes them before returning",
}


def _ta():
    spec = importlib.util.spec_from_file_location("tool_annotations_under_test", PKG / "tool_annotations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tools():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    out = {}
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
                for d in n.decorator_list):
            out[n.name] = ast.get_source_segment(src, n)
    return out


@functools.lru_cache(maxsize=1)
def _handler_closure():
    """{handler name: source of the handler plus worker functions it calls, 2 deep}."""
    src = (PKG / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    handlers = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "id", None) == "handler" and d.args:
                    handlers[d.args[0].value] = ast.get_source_segment(src, n)

    def closure(text, depth=2):
        out, frontier, seen = [text], [text], set()
        for _ in range(depth):
            nxt = []
            for t in frontier:
                for name in set(re.findall(r"\b(_?[a-z][a-z0-9_]*)\(", t)):
                    if name not in seen and name in funcs:
                        seen.add(name)
                        out.append(funcs[name])
                        nxt.append(funcs[name])
            frontier = nxt
        return "\n".join(out)

    return {k: closure(v) for k, v in handlers.items()}


def test_every_tool_is_classified_exactly_once():
    ta, tools = _ta(), set(_tools())
    classes = {"READ_ONLY": set(ta.READ_ONLY), "ADDITIVE": set(ta.ADDITIVE), "DESTRUCTIVE": set(ta.DESTRUCTIVE)}
    everything = set().union(*classes.values())
    assert not tools - everything, f"unclassified tools: {sorted(tools - everything)}"
    assert not everything - tools, f"classified names that are not tools: {sorted(everything - tools)}"
    names = list(classes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = classes[a] & classes[b]
            assert not both, f"in both {a} and {b}: {sorted(both)}"
    assert len(tools) > 250, f"only {len(tools)} tools seen — the AST sweep is broken"


def test_read_only_tools_do_not_visibly_mutate():
    ta, tools, handlers = _ta(), _tools(), _handler_closure()
    suspicious = {}
    for name in sorted(ta.READ_ONLY):
        body = tools[name] + "\n".join(handlers.get(h, "") for h in re.findall(r'_call\(\s*"([a-z0-9_]+)"', tools[name]))
        m = MUTATION.search(body)
        if m and name not in READ_ONLY_DESPITE_SIGNAL:
            suspicious[name] = m.group(0)
    assert not suspicious, (
        "READ_ONLY tools whose handler shows a mutation signal — reclassify them, or add a "
        f"verified reason to READ_ONLY_DESPITE_SIGNAL: {suspicious}")
    stale = [n for n in READ_ONLY_DESPITE_SIGNAL if n not in ta.READ_ONLY]
    assert not stale, f"READ_ONLY_DESPITE_SIGNAL entries that are no longer READ_ONLY: {stale}"
    for name in READ_ONLY_DESPITE_SIGNAL:
        body = tools[name] + "\n".join(handlers.get(h, "") for h in re.findall(r'_call\(\s*"([a-z0-9_]+)"', tools[name]))
        assert MUTATION.search(body), f"{name} no longer shows a mutation signal — drop its exemption"


def test_known_mutators_are_not_read_only():
    """Guard the guard: the signal must fire on tools that really mutate, or the
    honesty check above passes vacuously."""
    tools, handlers = _tools(), _handler_closure()
    for name in ("close_document", "transform", "fem_run", "run_script", "save_document"):
        body = tools[name] + "\n".join(handlers.get(h, "") for h in re.findall(r'_call\(\s*"([a-z0-9_]+)"', tools[name]))
        assert MUTATION.search(body), f"mutation signal does not fire on {name}"


def test_titles_names_and_reasons():
    ta, tools = _ta(), _tools()
    for name in tools:
        assert len(name) <= 64, f"{name} exceeds the 64-character tool-name limit"
        title = ta.title_for(name)
        assert title and title.strip() == title and len(title) <= 60, (name, title)
        hints = ta.hints_for(name)
        assert "readOnlyHint" in hints and hints["openWorldHint"] is False, (name, hints)
        if not hints["readOnlyHint"]:
            assert "destructiveHint" in hints, name
    for name, why in ta.DESTRUCTIVE.items():
        assert isinstance(why, str) and len(why) > 15, f"destructive {name} has no real reason"
    assert ta.title_for("fem_run") == "FEM Run" and ta.title_for("gdt_check") == "GD&T Check"


def test_server_applies_the_registry_after_the_last_tool():
    src = (PKG / "mcp_server.py").read_text(encoding="utf-8")
    apply_at = src.find("_tool_annotations.apply(mcp)")
    assert apply_at != -1, "mcp_server.py never applies the tool annotations"
    assert src.rfind("@mcp.tool(") < apply_at, "a tool is registered after the annotations are applied"


def test_mcp_floor_supports_titles():
    toml = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"mcp>=(\d+)\.(\d+)[^"]*"', toml)
    assert m and (int(m.group(1)), int(m.group(2))) >= (1, 10), \
        f"mcp floor {m and m.group(0)} predates FastMCP tool titles (1.10) — annotations would be silently dropped"


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
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

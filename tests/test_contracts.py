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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import determinism_registry as detreg  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "driftpin" / "worker.py"
MCP = REPO / "driftpin" / "mcp_server.py"
ANALYSIS_DIR = REPO / "driftpin" / "analysis"


# --- known, intentional exceptions (keep these honest; the tests verify that
#     every allowlisted name still exists, so stale entries get flagged) -------

# Tools that legitimately do NOT dispatch to a worker handler via _call(...).
_NO_DISPATCH = {
    "restart_worker",   # tears down / respawns the worker process itself
    # host-side workspace-pool management (issue #167) — no worker handler
    "use_workspace", "list_workspaces", "close_workspace",
    # host-side doctor report (issue #202) — reads discovery state, needs no worker
    "setup_status",
}

# Tools whose function name intentionally differs from the handler they call.
_NAME_DIFFERS_OK = {
    "render_view", "render_views",   # both call "tessellate", rasterize host-side
    "render_fem_results",            # calls "fem_field_surface" (#174), rasterizes host-side
    "granular_screen",               # public DFx screen → closed-form "granular_oracle" handler
}

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
    return re.findall(r'@handler\(\s*["\']([^"\']+)["\']\s*\)', WORKER.read_text(encoding="utf-8"))


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
    tree = ast.parse(MCP.read_text(encoding="utf-8"))
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


# --- escalate_to referential integrity (issue #127) ---------------------------
#
# A screen's `escalate_to` names the higher-fidelity tool an agent should run
# next. If that name is a typo or a renamed/removed handler, the escalation is a
# dead end the LLM can't act on. We harvest every `escalate_to` VALUE any tool can
# emit by static-parsing the dict literals across driftpin/ (no imports), and
# require each to be either None or a real tool/handler name.

def _str_constants(node):
    """All str literals reachable as a dict-value expression: a bare Constant, or
    either arm of a conditional `X if c else Y` (the molding.py escalation form)."""
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else [None]
    if isinstance(node, ast.IfExp):
        return _str_constants(node.body) + _str_constants(node.orelse)
    return []  # dynamic expression we can't (and needn't) resolve statically


def _escalate_targets():
    """{source: set(targets)} — every string an `escalate_to` dict key can hold,
    parsed from driftpin/analysis/*.py + worker.py. None (escalate to nothing) is
    represented by the literal None in the set."""
    out = {}
    files = sorted(ANALYSIS_DIR.glob("*.py")) + [WORKER]
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "escalate_to":
                    out.setdefault(f.name, set()).update(_str_constants(val))
    return out


_ESCALATE_TARGETS = _escalate_targets()
_ALL_ESCALATE_NAMES = {
    name for targets in _ESCALATE_TARGETS.values()
    for name in targets if name is not None
}


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


# --- escalate_to contract (issue #127) ----------------------------------------

def test_escalate_to_targets_are_registered():
    """Every `escalate_to` a tool can emit resolves to a real MCP tool or worker
    handler (or is None). Catches a typo'd / renamed escalation target — the
    higher-fidelity solver an agent is told to run next must actually exist."""
    known = _TOOL_NAMES | _HANDLERS
    offenders = []
    for source, targets in sorted(_ESCALATE_TARGETS.items()):
        for name in sorted(t for t in targets if t is not None):
            if name not in known:
                offenders.append(f"{source}: escalate_to={name!r} is no tool/handler")
    assert not offenders, (
        "escalate_to targets with no registered tool/handler:\n  "
        + "\n  ".join(offenders)
    )
    assert _ALL_ESCALATE_NAMES, "no escalate_to targets parsed — collector is broken"


def test_molding_screen_docstring_names_its_escalation():
    """The molding_screen tool used to say 'No mold-filling solver is shipped
    (escalate_to=None)' — stale since #105 shipped molding_fill_submit. Guard the
    fix so the MCP-facing docstring keeps naming the real escalation target."""
    doc = next(t["doc"] for t in _TOOLS if t["name"] == "molding_screen")
    assert "molding_fill_submit" in doc, (
        "molding_screen docstring must name its escalation target molding_fill_submit"
    )
    assert "No mold-filling solver is shipped" not in doc, (
        "molding_screen docstring still carries the stale 'no solver' claim"
    )


# --- determinism-class registry (issue #123) ----------------------------------

def test_every_tool_has_a_determinism_class():
    """Every MCP tool is either given a determinism class (exact / bounded /
    nondeterministic-by-design) in tests/determinism_registry.py, or sits on the
    explicit NOT_YET_CLASSIFIED allowlist. A newly-added tool that is on NEITHER
    trips this test — that's the ratchet that stops the determinism-coverage gap
    re-opening with every new tool. Fix: classify it + add a sweep entry."""
    classified = (detreg.EXACT_TOOLS | detreg.BOUNDED_TOOLS
                  | detreg.NONDETERMINISTIC_TOOLS)
    unclassified = sorted(
        _TOOL_NAMES - classified - detreg.NOT_YET_CLASSIFIED)
    assert not unclassified, (
        "MCP tools with no determinism class (classify in determinism_registry.py "
        "and add a sweep entry, or — last resort — list in NOT_YET_CLASSIFIED):\n  "
        + "\n  ".join(unclassified)
    )


def test_determinism_registry_names_are_real_tools():
    """Honesty: every name the registry classifies (or parks on the allowlist) is
    a real MCP tool, so a renamed/removed tool can't leave a stale ghost entry."""
    classified = (detreg.EXACT_TOOLS | detreg.BOUNDED_TOOLS
                  | detreg.NONDETERMINISTIC_TOOLS)
    stale = sorted((classified | detreg.NOT_YET_CLASSIFIED) - _TOOL_NAMES)
    assert not stale, "determinism registry names that are not MCP tools:\n  " + \
        "\n  ".join(stale)


def test_determinism_classes_are_disjoint():
    """A tool has exactly one class: the three class sets and the allowlist must
    not overlap (an overlap makes determinism_class() ambiguous / a stale entry)."""
    sets = {
        "EXACT_TOOLS": detreg.EXACT_TOOLS,
        "BOUNDED_TOOLS": detreg.BOUNDED_TOOLS,
        "NONDETERMINISTIC_TOOLS": detreg.NONDETERMINISTIC_TOOLS,
        "NOT_YET_CLASSIFIED": detreg.NOT_YET_CLASSIFIED,
    }
    names = list(sets)
    overlaps = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            common = sets[names[i]] & sets[names[j]]
            if common:
                overlaps.append(f"{names[i]} ∩ {names[j]} = {sorted(common)}")
    assert not overlaps, "determinism-class sets overlap:\n  " + "\n  ".join(overlaps)


def test_swept_analysis_tools_are_classified_exact():
    """Every tool the bitwise analysis sweep asserts (determinism_registry
    ANALYSIS_SWEEP) is declared "exact" — the table and the class registry can't
    drift apart. (Sweep entries are handler-callable; those that are also MCP
    tools must carry the exact class.)"""
    swept = {name for name, _ in detreg.ANALYSIS_SWEEP}
    misclassed = sorted(
        n for n in swept if n in _TOOL_NAMES and n not in detreg.EXACT_TOOLS)
    assert not misclassed, (
        "tools in ANALYSIS_SWEEP not classified 'exact':\n  " + "\n  ".join(misclassed)
    )


def test_bounded_submits_are_classified_bounded():
    """Every BOUNDED_SUBMITS entry is a real tool classified "bounded"."""
    offenders = []
    for spec in detreg.BOUNDED_SUBMITS:
        t = spec["tool"]
        if t not in _TOOL_NAMES:
            offenders.append(f"{t}: not an MCP tool")
        elif t not in detreg.BOUNDED_TOOLS:
            offenders.append(f"{t}: not classified bounded")
    assert not offenders, "BOUNDED_SUBMITS problems:\n  " + "\n  ".join(offenders)


# --- encoding hygiene (issue #204) --------------------------------------------
#
# On Windows a text-mode open()/read_text()/write_text() with no explicit
# encoding= uses the locale codec (cp1252), not UTF-8. DriftPin's sources,
# generated solver decks and result files carry non-ASCII (em-dashes, µ, °, ×),
# so an encoding-less text open corrupts data or raises UnicodeDecodeError on a
# Windows MCP host — which is spawned with a minimal env (no PYTHONUTF8). This
# guard fails if any text-mode open in driftpin/ or tests/ omits encoding, so a
# future regression is caught here instead of on a user's machine.

_ENC_ROOTS = [REPO / "driftpin", REPO / "tests"]


def _binary_mode(call):
    """True if this open()/Path.open() call is binary mode ('b' in the mode)."""
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and "b" in mode


def _is_pil_open(call):
    """True for PIL Image.open(...) — an image decoder that takes no encoding=.
    Recognised by an `Image.open(` receiver or a BytesIO(...) first argument."""
    recv = call.func.value
    if isinstance(recv, ast.Name) and recv.id == "Image":
        return True
    if call.args and isinstance(call.args[0], ast.Call):
        inner = call.args[0].func
        name = getattr(inner, "id", None) or getattr(inner, "attr", None)
        if name == "BytesIO":
            return True
    return False


def _encodingless_text_opens():
    """(file:line, snippet) for every text-mode open/read_text/write_text in
    driftpin/ and tests/ that omits encoding=. PIL Image.open and binary-mode
    opens are exempt."""
    offenders = []
    for root in _ENC_ROOTS:
        for f in sorted(root.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            src = f.read_text(encoding="utf-8")
            lines = src.splitlines()
            tree = ast.parse(src, filename=str(f))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                has_enc = any(kw.arg == "encoding" for kw in node.keywords)
                hit = False
                if isinstance(fn, ast.Name) and fn.id == "open":
                    hit = not has_enc and not _binary_mode(node)
                elif isinstance(fn, ast.Attribute):
                    if fn.attr in ("read_text", "write_text"):
                        hit = not has_enc
                    elif fn.attr == "open":
                        hit = (not has_enc and not _binary_mode(node)
                               and not _is_pil_open(node))
                if hit:
                    rel = f.relative_to(REPO).as_posix()
                    snippet = lines[node.lineno - 1].strip()
                    offenders.append(f"{rel}:{node.lineno}  {snippet}")
    return offenders


def test_no_encodingless_text_opens():
    """Every text-mode file open specifies encoding= (issue #204)."""
    offenders = _encodingless_text_opens()
    assert not offenders, (
        "text-mode open/read_text/write_text without encoding=\"utf-8\" "
        "(cp1252 on Windows → UnicodeDecodeError / deck corruption):\n  "
        + "\n  ".join(offenders)
    )


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

"""Tool-selection eval (#508): does a tool's description make a model pick it?

The server exposes 280+ tools, and a client model chooses among them from the
``tools/list`` names and descriptions alone. ``test_tool_annotations.py`` checks
the annotations exist and ``test_toolsets_budget.py`` checks the list's size.
Neither checks whether a description tells ``fem_modal`` apart from ``beam_modal``.
This does. The corpus is in ``tests/tool_selection_cases.py``. How to run it and
read the report is in ``tests/TOOL_SELECTION.md``.

FREE (default, in fast-checks, no FreeCAD, no API key):

  * the corpus is well formed: ids unique, every ``accept`` tool inside its cluster;
  * every tool the corpus names is a registered ``@mcp.tool()`` in exactly one
    toolset family (static: ``mcp_server.py`` is parsed with ``ast``, not imported);
  * no tool description is empty or shorter than DESC_FLOOR characters;
  * every case's accepted tools are in the toolset the paid run would expose;
  * the paid path itself, offline, against a fake client: request shape (tools
    present, ``cache_control`` on the last tool only, ``tool_choice`` auto),
    grading, confusion counting, cost accounting and response-cache keying;
  * when ``mcp`` is importable: the same checks against the LIVE ``tools/list``
    served over stdio, so the paid run's input is the real thing.

PAID (``RUN_RELIABILITY=1``; needs ``anthropic``, ``mcp`` and ANTHROPIC_API_KEY):
one ``messages.create`` per case, graded on the first ``tool_use`` block's name.

  RUN_RELIABILITY=1 .venv/bin/python3 tests/test_tool_selection.py

  TOOL_SELECTION_MODEL     alias or full ID (default: sonnet)
  TOOL_SELECTION_TOOLSETS  'bundle' (default: bundle default + the families of the
                           case's cluster) or 'all' (every family, the pip default)
  TOOL_SELECTION_CASES     comma list of case ids or cluster names to run
  TOOL_SELECTION_LOADING   'eager' (default: every schema up front), 'search_bm25' /
                           'search_regex' (every tool deferred behind Anthropic's
                           server-side tool search) or 'client_search' (deferred
                           names listed, a Claude-Code-shaped client search; #514)
  TOOL_SELECTION_MIN       fail (exit 1) if overall accuracy is below this, e.g. 0.8
  TOOL_SELECTION_REFRESH=1 ignore cached responses and re-query

Responses are cached under tests/tool_selection_cache/ (gitignored), keyed by
model, case id and a hash of the exact request, which includes every tool schema.
Editing a description therefore re-queries the clusters that expose that tool.
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
from _mcp_python import HOST_DEPS, ensure_mcp_interpreter, mcp_skip_reason  # noqa: E402
from orchestration.claude_models import (  # noqa: E402
    PRICING, cost_of, resolve_model, response_cache_name, usage_dict)
from tool_selection_cases import CASES, CLUSTERS  # noqa: E402

# The shortest description registered when this was written was 58 characters
# ("Commit the most recent open transaction; changes are kept."). 40 leaves room for
# terse-but-real descriptions and still catches a stub or an empty docstring.
DESC_FLOOR = 40
MIN_CASES = 40

CACHE_DIR = REPO / "tests" / "tool_selection_cache"
MAX_TOKENS = 4096

# How the tools reach the model (#514). 'eager' sends every schema up front (#508).
# The others defer every MCP tool behind a search, the way an agent hosting 300+
# tools loads them: Anthropic's server-side tool search (bm25 / regex), or a
# Claude-Code-shaped client search that lists deferred NAMES in the system prompt
# and answers `tool_search` calls with tool_reference blocks.
LOADING_MODES = ("eager", "search_bm25", "search_regex", "client_search")
SERVER_SEARCH = {"search_bm25": {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"},
                 "search_regex": {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}}
CLIENT_SEARCH_NAME = "tool_search"
CLIENT_SEARCH_LIMIT = 5
MAX_TURNS = 6          # search -> (search ->) call; a cap so a looping model ends
CLIENT_SEARCH_TOOL = {
    "name": CLIENT_SEARCH_NAME,
    "description": (
        "Load deferred tools so they can be called. Only the tools this returns become "
        "callable. Query forms: 'select:name1,name2' loads those exact tools; any other "
        "text is a keyword search over tool names and descriptions, returning the best "
        f"matches (up to max_results, default {CLIENT_SEARCH_LIMIT})."),
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "'select:a,b' or keywords"},
        "max_results": {"type": "integer", "description": "maximum tools to return"}},
        "required": ["query"]},
}
SEARCH_NOTE = ("Tools are loaded on demand. Search the AnkusDrive tool catalog for the tool "
               "that performs the request, then call it.")

SYSTEM_PROMPT = (
    "You are a CAD and engineering assistant working through the AnkusDrive FreeCAD "
    "tools. The documents, objects, drawings and analyses the user names already exist "
    "under exactly those names, and each name is a valid handle. Answer the request by "
    "calling the one tool that performs it. Call it directly: do not list or inspect "
    "objects first, and do not ask a clarifying question. If an argument is not given, "
    "choose a reasonable value.")


def _toolsets():
    spec = importlib.util.spec_from_file_location("toolsets_under_test", REPO / "ankusdrive" / "toolsets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TS = _toolsets()


# --- the registry, two ways ---------------------------------------------------

def static_registry() -> dict:
    """{tool name: docstring} for every ``@mcp.tool()`` in mcp_server.py, by ``ast``.

    FastMCP's description is the function docstring, so this is what tools/list
    serves, without importing the server (fast-checks has no ``mcp``)."""
    src = (REPO / "ankusdrive" / "mcp_server.py").read_text(encoding="utf-8")
    out = {}
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
                for d in n.decorator_list):
            out[n.name] = ast.get_docstring(n) or ""
    return out


def live_tools_list() -> list:
    """The real ``tools/list`` with every family enabled, from the server over stdio."""
    from test_toolsets_budget import _tools_list
    return _tools_list("all")


# --- what the paid run exposes ------------------------------------------------

def exposed_families(cluster: str, mode: str = "bundle") -> tuple:
    """The families switched on for a case in ``cluster``, in FAMILIES order.

    'bundle': the Claude Desktop default (``BUNDLE_DEFAULT``) plus the family of
    every tool in the cluster, so all of a cluster's confusable members are on
    screen together. 'all': every family, which is what a pip/uvx install serves."""
    if mode == "all":
        return tuple(TS.FAMILIES)
    if mode != "bundle":
        raise ValueError(f"TOOL_SELECTION_TOOLSETS must be 'bundle' or 'all', not {mode!r}")
    want = set(TS.BUNDLE_DEFAULT) | {TS.family_of(t) for t in CLUSTERS[cluster]["members"]}
    return tuple(f for f in TS.FAMILIES if f in want)


def exposed_names(families: tuple) -> frozenset:
    return frozenset().union(*(TS.FAMILIES[f] for f in families))


def check_loading(loading: str) -> str:
    if loading not in LOADING_MODES:
        raise ValueError(f"TOOL_SELECTION_LOADING must be one of {LOADING_MODES}, not {loading!r}")
    return loading


def to_anthropic(tools: list, families: tuple, loading: str = "eager") -> list:
    """tools/list entries -> Anthropic tool definitions, for the enabled families.

    Keeps the server's order (deterministic, and what a client sends), copies
    ``name`` / ``description`` / ``inputSchema`` only, and marks the LAST tool with
    ``cache_control`` so the whole tool block is a cached prefix across cases.
    Filtering the full list by family equals what the server serves with only those
    families on; test_toolsets_budget asserts that for the bundle default."""
    keep = exposed_names(families)
    out = [{"name": t["name"], "description": t.get("description") or "",
            "input_schema": t["inputSchema"]} for t in tools if t["name"] in keep]
    if check_loading(loading) == "eager":
        if out:
            out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
        return out
    # Deferred: every MCP tool is sent (the API needs the definitions to search and
    # expand them) but none is in context. A deferred tool may not carry
    # cache_control, so the breakpoint moves to the system prompt (build_request).
    out = [{**t, "defer_loading": True} for t in out]
    search = SERVER_SEARCH.get(loading) or CLIENT_SEARCH_TOOL
    return out + [dict(search)]


def deferred_names(tools: list) -> list:
    return [t["name"] for t in tools if t.get("defer_loading")]


def system_for(tools: list, loading: str):
    if loading == "eager":
        return SYSTEM_PROMPT
    text = f"{SYSTEM_PROMPT}\n\n{SEARCH_NOTE}"
    if loading == "client_search":
        # Claude Code's shape: the model sees every deferred tool's NAME, not its
        # description, and loads definitions through the search tool.
        text += (f"\n\nDeferred tools, callable only after `{CLIENT_SEARCH_NAME}` loads them:\n"
                 + "\n".join(deferred_names(tools)))
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_request(case: dict, tools: list, model: str, loading: str = "eager") -> dict:
    return {"model": model, "max_tokens": MAX_TOKENS, "system": system_for(tools, loading), "tools": tools,
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": case["request"]}]}


# --- the client-side search (client_search mode) -------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def client_search(query: str, catalog: list, limit: int | None = None) -> list:
    """Names of the tools a ``tool_search`` call returns, best first.

    ``select:a,b`` returns those exact names that exist. Anything else is a keyword
    search: a query word scores 3 when it is a word of the tool's name, 2 when it
    is a substring of the name, and 1 when it appears in the description. Ties keep
    the catalog (tools/list) order. This approximates an agent's search; it is not
    Claude Code's ranking."""
    names = [t["name"] for t in catalog]
    q = (query or "").strip()
    if q.lower().startswith("select:"):
        want = [w.strip() for w in q[7:].split(",") if w.strip()]
        return [w for w in dict.fromkeys(want) if w in names]
    limit = max(1, min(int(limit or CLIENT_SEARCH_LIMIT), len(catalog) or 1))
    words = set(_WORD.findall(q.lower()))
    scored = []
    for i, t in enumerate(catalog):
        name = t["name"].lower()
        name_words = set(_WORD.findall(name.replace("_", " ")))
        desc = set(_WORD.findall((t.get("description") or "").lower()))
        score = sum(3 if w in name_words else 2 if w in name else 1 if w in desc else 0 for w in words)
        if score:
            scored.append((-score, i, t["name"]))
    return [n for _, _, n in sorted(scored)[:limit]]


def request_key(req: dict) -> str:
    """Hash of everything that decides the answer: model, prompt, request, every schema."""
    return hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()


def cache_path(case: dict, req: dict, cache_dir: Path | None = None) -> Path:
    return (cache_dir or CACHE_DIR) / response_cache_name(f"{case['id']}.{request_key(req)[:16]}", req["model"], "json")


# --- one response -> a grade ---------------------------------------------------

def serialize(resp) -> dict:
    """The parts of an SDK response the grade needs, as plain JSON (for the cache)."""
    blocks = []
    for b in getattr(resp, "content", None) or []:
        kind = getattr(b, "type", None)
        if kind == "tool_use":
            blocks.append({"type": kind, "name": b.name, "input": getattr(b, "input", {})})
        elif kind == "text":
            blocks.append({"type": kind, "text": b.text})
        elif kind == "server_tool_use":
            blocks.append({"type": kind, "name": b.name, "input": getattr(b, "input", {})})
        elif kind == "tool_search_tool_result":
            body = getattr(b, "content", None)
            refs = [r.tool_name for r in getattr(body, "tool_references", None) or []]
            blocks.append({"type": kind, "refs": refs, "error": getattr(body, "error_code", None)})
        else:
            blocks.append({"type": kind})       # thinking etc.: presence only
    return {"content": blocks, "stop_reason": getattr(resp, "stop_reason", None),
            "usage": usage_dict(resp)}


def searches_in(content: list) -> list:
    """[{query, refs}] for every search the model ran, server-side or client-side."""
    out = []
    for b in content:
        if b["type"] == "server_tool_use" or (b["type"] == "tool_use" and b["name"] == CLIENT_SEARCH_NAME):
            inp = b.get("input") or {}
            out.append({"query": inp.get("query", inp.get("pattern")), "refs": []})
        elif b["type"] in ("tool_search_tool_result", "client_search_result") and out:
            out[-1]["refs"] = list(b.get("refs") or [])
    return out


def grade(case: dict, rec: dict, loading: str = "eager") -> dict:
    """pass / wrong / no_tool / refusal / max_tokens, the picked tool, and, under a
    deferred loading mode, whether search ever surfaced an accepted tool."""
    picked = next((b["name"] for b in rec["content"]
                   if b["type"] == "tool_use" and b["name"] != CLIENT_SEARCH_NAME), None)
    stop = rec.get("stop_reason")
    if picked is not None:
        outcome = "pass" if picked in case["accept"] else "wrong"
    elif stop in ("refusal", "max_tokens"):
        outcome = stop
    else:
        outcome = "no_tool"
    text = " ".join(b.get("text", "") for b in rec["content"] if b["type"] == "text").strip()
    searches = searches_in(rec["content"])
    found = {n for s in searches for n in s["refs"]}
    return {"id": case["id"], "cluster": case["cluster"], "accept": list(case["accept"]),
            "picked": picked, "outcome": outcome, "stop_reason": stop,
            "text": text[:300], "usage": rec["usage"],
            "searches": searches, "turns": rec.get("turns", 1),
            "surfaced": None if loading == "eager" else bool(found & set(case["accept"]))}


# --- one case, possibly several turns -------------------------------------------

def converse(client, req: dict, loading: str) -> dict:
    """Run one case to its first non-search tool call (or a stop) and return the
    cache record: every turn's blocks flattened, the last stop_reason, summed usage.

    Eager and server-side search usually take one call. A ``pause_turn`` (a long
    server-side turn) is resumed by sending the assistant content back. Under
    client_search, each ``tool_search`` call is answered with a tool_result of
    tool_reference blocks, which the API expands into the definitions."""
    catalog = [t for t in req["tools"] if t.get("defer_loading")]
    msgs, content, usage, stop, turns = list(req["messages"]), [], Counter(), None, 0
    while turns < MAX_TURNS:
        turns += 1
        resp = client.messages.create(**{**req, "messages": list(msgs)})
        rec = serialize(resp)
        content += rec["content"]
        usage.update(rec["usage"])
        stop = rec["stop_reason"]
        uses = [b for b in rec["content"] if b["type"] == "tool_use"]
        if any(b["name"] != CLIENT_SEARCH_NAME for b in uses):
            break                                   # the graded call
        if stop == "pause_turn":
            msgs.append({"role": "assistant", "content": resp.content})
            continue
        if loading == "client_search" and uses:
            results = []
            for b in resp.content:
                if getattr(b, "type", None) == "tool_use" and b.name == CLIENT_SEARCH_NAME:
                    inp = getattr(b, "input", {}) or {}
                    refs = client_search(inp.get("query", ""), catalog, inp.get("max_results"))
                    content.append({"type": "client_search_result", "refs": refs})
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": [{"type": "tool_reference", "tool_name": n} for n in refs]
                                    or "No tools matched. Try other keywords."})
            msgs += [{"role": "assistant", "content": resp.content}, {"role": "user", "content": results}]
            continue
        break
    return {"content": content, "stop_reason": stop, "turns": turns,
            "usage": {k: usage.get(k, 0) for k in ("in_tokens", "out_tokens", "cache_read", "cache_write")}}


# --- the run ------------------------------------------------------------------

def run_cases(client_factory, tools_list: list, model: str, cases: list, mode: str = "bundle",
              cache_dir: Path | None = None, refresh: bool = False, log=print,
              loading: str = "eager") -> list:
    """Query (or read from cache) and grade every case. ``client_factory`` is called
    at most once, and only if some case is not cached, so a fully cached rerun needs
    no API key. Cases are ordered so those sharing a tool block run back to back,
    inside the 5-minute cache window."""
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    order = {c: i for i, c in enumerate(CLUSTERS)}
    plan = sorted(cases, key=lambda c: (exposed_families(c["cluster"], mode), order[c["cluster"]]))
    client, results, tool_blocks = None, [], {}
    for case in plan:
        fams = exposed_families(case["cluster"], mode)
        if fams not in tool_blocks:
            tool_blocks[fams] = to_anthropic(tools_list, fams, loading)
        req = build_request(case, tool_blocks[fams], model, loading)
        path = cache_path(case, req, cache_dir)
        if path.exists() and not refresh:
            rec, cached = json.loads(path.read_text(encoding="utf-8")), True
        else:
            if client is None:
                client = client_factory()
            rec, cached = converse(client, req, loading), False
            path.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        g = grade(case, rec, loading)
        g["from_cache"] = cached
        g["n_tools"] = len(req["tools"])
        results.append(g)
        mark = {"pass": "ok  "}.get(g["outcome"], "MISS")
        seen = "" if g["surfaced"] is None else f"  surfaced={g['surfaced']} searches={len(g['searches'])}"
        log(f"  {mark} {case['id']:20s} {g['outcome']:10s} picked={g['picked']}{seen}"
            f"{'' if cached else '  (fresh)'}")
    return results


def _sum_usage(rows) -> dict:
    tot = Counter()
    for r in rows:
        tot.update(r["usage"])
    return {k: tot.get(k, 0) for k in ("in_tokens", "cache_write", "cache_read", "out_tokens")}


def discovery(results: list) -> dict | None:
    """Deferred modes only: how often search surfaced an accepted tool, and the misses
    split into a disambiguation problem (surfaced, still picked wrong) vs a
    discoverability problem (never surfaced)."""
    rows = [r for r in results if r.get("surfaced") is not None]
    if not rows:
        return None
    miss = [r for r in rows if r["outcome"] != "pass"]
    per = defaultdict(lambda: [0, 0])
    for r in rows:
        per[r["cluster"]][0] += 1
        per[r["cluster"]][1] += r["surfaced"]
    return {"surfaced": sum(r["surfaced"] for r in rows), "n": len(rows),
            "recall": round(sum(r["surfaced"] for r in rows) / len(rows), 4),
            "miss_surfaced": sum(r["surfaced"] for r in miss),
            "miss_never_surfaced": sum(not r["surfaced"] for r in miss),
            "mean_searches": round(sum(len(r["searches"]) for r in rows) / len(rows), 2),
            "per_cluster_recall": {c: round(s / n, 3) for c, (n, s) in
                                   sorted(per.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[0]))}}


def summarize(results: list, model: str, mode: str, loading: str = "eager") -> dict:
    n = len(results)
    outcomes = Counter(r["outcome"] for r in results)
    per = defaultdict(lambda: {"n": 0, "pass": 0})
    for r in results:
        per[r["cluster"]]["n"] += 1
        per[r["cluster"]]["pass"] += r["outcome"] == "pass"
    per_cluster = {c: {**v, "accuracy": round(v["pass"] / v["n"], 3)}
                   for c, v in sorted(per.items(), key=lambda kv: (kv[1]["pass"] / kv[1]["n"], kv[0]))}
    conf = defaultdict(list)
    for r in results:
        if r["outcome"] != "pass":
            picked = r["picked"] or f"<{r['outcome']}>"
            conf[("/".join(sorted(r["accept"])), picked)].append(r["id"])
    hidden = {r["id"] for r in results if r.get("surfaced") is False}
    confusion = [{"expected": e, "picked": p, "count": len(ids), "cases": ids,
                  "never_surfaced": sum(i in hidden for i in ids)}
                 for (e, p), ids in sorted(conf.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    usage = _sum_usage(results)
    fresh = _sum_usage(r for r in results if not r["from_cache"])
    cost = cost_of(usage, model)
    # What the same tokens would cost with no prompt caching: every cached or
    # cache-written input token billed as plain input.
    uncached = {"in_tokens": usage["in_tokens"] + usage["cache_write"] + usage["cache_read"],
                "out_tokens": usage["out_tokens"]}
    no_cache_cost = cost_of(uncached, model)
    return {"model": model, "toolsets": mode, "loading": loading, "n": n,
            "discovery": discovery(results),
            "accuracy": round(outcomes["pass"] / n, 4) if n else 0.0,
            "outcomes": dict(outcomes), "per_cluster": per_cluster, "confusion": confusion,
            "usage": usage, "cost_usd": round(cost, 4),
            "cost_without_cache_usd": round(no_cache_cost, 4),
            "cache_savings_usd": round(no_cache_cost - cost, 4),
            "billed_this_run_usd": round(cost_of(fresh, model), 4),
            "fresh_calls": sum(not r["from_cache"] for r in results),
            "cases": results}


def print_report(rep: dict) -> None:
    print(f"\n== tool selection: {rep['model']}, toolsets={rep['toolsets']}, "
          f"loading={rep.get('loading', 'eager')} ==")
    print(f"  accuracy {rep['accuracy']:.1%}  ({rep['outcomes'].get('pass', 0)}/{rep['n']})   "
          f"outcomes {rep['outcomes']}")
    print("  per cluster (worst first):")
    for c, v in rep["per_cluster"].items():
        print(f"    {c:18s} {v['pass']:2d}/{v['n']:<2d}  {v['accuracy']:.0%}")
    d = rep.get("discovery")
    if d:
        print(f"  discovery: an accepted tool surfaced in {d['surfaced']}/{d['n']} ({d['recall']:.0%}); "
              f"{d['mean_searches']} searches/case")
        print(f"  misses: {d['miss_surfaced']} surfaced but picked wrong (description), "
              f"{d['miss_never_surfaced']} never surfaced (discoverability)")
        low = {c: v for c, v in d["per_cluster_recall"].items() if v < 1}
        if low:
            print("  clusters where search missed: " + ", ".join(f"{c} {v:.0%}" for c, v in low.items()))
    if rep["confusion"]:
        print("  confusions (asked for -> picked, count[, never surfaced]):")
        for row in rep["confusion"]:
            ns = f" ({row['never_surfaced']} never surfaced)" if row.get("never_surfaced") else ""
            print(f"    {row['expected']:34s} -> {row['picked']:28s} x{row['count']}{ns}  {row['cases']}")
    u = rep["usage"]
    print(f"  tokens: input {u['in_tokens']}, cache write {u['cache_write']}, "
          f"cache read {u['cache_read']}, output {u['out_tokens']}")
    print(f"  cost ${rep['cost_usd']:.4f} (without prompt caching ${rep['cost_without_cache_usd']:.4f}, "
          f"saved ${rep['cache_savings_usd']:.4f}); billed this run ${rep['billed_this_run_usd']:.4f} "
          f"over {rep['fresh_calls']} fresh call(s)")


def select_cases(spec: str | None) -> list:
    if not spec:
        return list(CASES)
    want = {s.strip() for s in spec.split(",") if s.strip()}
    unknown = want - {c["id"] for c in CASES} - set(CLUSTERS)
    if unknown:
        raise SystemExit(f"TOOL_SELECTION_CASES: unknown case/cluster {sorted(unknown)}")
    return [c for c in CASES if c["id"] in want or c["cluster"] in want]


# --- FREE tier: corpus + registry ---------------------------------------------

def _corpus_tools() -> set:
    return {t for c in CLUSTERS.values() for t in c["members"]} | {t for c in CASES for t in c["accept"]}


def test_corpus_is_well_formed():
    ids = [c["id"] for c in CASES]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    assert not dup, f"duplicate case ids {dup}"
    assert len(CASES) >= MIN_CASES, f"only {len(CASES)} cases; the eval needs >= {MIN_CASES}"
    per = Counter(c["cluster"] for c in CASES)
    for c in CASES:
        assert c["cluster"] in CLUSTERS, f"{c['id']}: unknown cluster {c['cluster']!r}"
        assert c["accept"] and c["request"].strip(), f"{c['id']}: empty accept or request"
        outside = set(c["accept"]) - set(CLUSTERS[c["cluster"]]["members"])
        assert not outside, f"{c['id']}: accepted {sorted(outside)} not in cluster {c['cluster']!r}"
        named = [t for t in c["accept"] if re.search(rf"\b{re.escape(t)}\b", c["request"])]
        assert not named, f"{c['id']}: the request names the tool {named}; describe the task instead"
    thin = [k for k in CLUSTERS if per[k] < 2]
    assert not thin, f"clusters with fewer than 2 cases measure nothing per cluster: {thin}"


def test_every_corpus_tool_is_registered_in_one_family():
    reg = static_registry()
    missing = sorted(_corpus_tools() - set(reg))
    assert not missing, f"corpus names tools the server does not register: {missing}"
    for t in _corpus_tools():
        TS.family_of(t)        # raises KeyError naming the tool


def test_no_description_is_empty_or_stub():
    reg = static_registry()
    short = sorted((len(d.strip()), n) for n, d in reg.items() if len(d.strip()) < DESC_FLOOR)
    assert not short, f"descriptions under {DESC_FLOOR} chars (a model cannot choose by these): {short}"


def test_accepted_tools_are_exposed_in_paid_mode():
    for mode in ("bundle", "all"):
        for c in CASES:
            fams = exposed_families(c["cluster"], mode)
            names = exposed_names(fams)
            gone = (set(c["accept"]) | set(CLUSTERS[c["cluster"]]["members"])) - names
            assert not gone, f"{c['id']} ({mode}): {sorted(gone)} not exposed by {fams}"
    assert set(TS.BUNDLE_DEFAULT) <= set(exposed_families("controls")), "bundle default must always be on"


# --- FREE tier: the paid path against a fake client ---------------------------

def _fake_tools(names):
    return [{"name": n, "title": n, "description": f"{n} does a thing, described at length here.",
             "inputSchema": {"type": "object", "properties": {"x": {"type": "number"}}},
             "outputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}
            for n in names]


class _FakeClient:
    """Answers each case from a script: {case request substring: (kind, value)}."""

    def __init__(self, script):
        self.script, self.calls = script, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        req = kw["messages"][0]["content"]
        kind, value = next(v for k, v in self.script.items() if k in req)
        content = [SimpleNamespace(type="thinking", thinking="hmm")]
        stop = "end_turn"
        if kind == "tool":
            content.append(SimpleNamespace(type="tool_use", id="t1", name=value, input={"x": 1}))
            stop = "tool_use"
        elif kind == "text":
            content.append(SimpleNamespace(type="text", text=value))
        else:
            stop = kind
        first = len(self.calls) == 1
        usage = SimpleNamespace(input_tokens=50, output_tokens=200,
                                cache_creation_input_tokens=10_000 if first else 0,
                                cache_read_input_tokens=0 if first else 10_000)
        return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


def _selftest_fixture(tmp: Path):
    cases = [c for c in CASES if c["cluster"] == "modal"][:4]
    names = sorted(TS.FAMILIES["core"] | TS.FAMILIES["drawings"] | TS.FAMILIES["fem"]
                   | TS.FAMILIES["hand_calcs"] | TS.FAMILIES["simulation"] | TS.FAMILIES["rendering"])
    tools = _fake_tools(names)
    a, b, c, d = cases
    script = {a["request"]: ("tool", a["accept"][0]),          # pass
              b["request"]: ("tool", "beam_modal"),            # wrong
              c["request"]: ("text", "Which analysis?"),       # no_tool
              d["request"]: ("refusal", None)}                 # refusal
    return cases, tools, script


def test_selftest_request_shape():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cases, tools, script = _selftest_fixture(Path(td))
        fake = _FakeClient(script)
        run_cases(lambda: fake, tools, "claude-sonnet-5-5", cases, cache_dir=Path(td), log=lambda *_: None)
    assert len(fake.calls) == len(cases)
    want = exposed_names(exposed_families("modal"))
    for kw in fake.calls:
        assert kw["model"] == "claude-sonnet-5-5" and kw["max_tokens"] == MAX_TOKENS
        assert kw["tool_choice"] == {"type": "auto"}
        assert kw["system"] == SYSTEM_PROMPT
        assert kw["messages"][0]["role"] == "user" and len(kw["messages"]) == 1
        ts = kw["tools"]
        assert {t["name"] for t in ts} == want & {t["name"] for t in tools}
        assert all(set(t) <= {"name", "description", "input_schema", "cache_control"} for t in ts), \
            "only name/description/input_schema (+cache_control) may reach the API"
        assert "cache_control" in ts[-1] and ts[-1]["cache_control"] == {"type": "ephemeral"}
        assert not any("cache_control" in t for t in ts[:-1]), "cache_control on the last tool only"
        assert [t["name"] for t in ts] == [t["name"] for t in tools if t["name"] in want], \
            "tool order must follow tools/list, or the prompt cache misses"
    assert fake.calls[0]["tools"] == fake.calls[1]["tools"], "same cluster -> byte-identical tool block"


def test_selftest_grading_and_confusion():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cases, tools, script = _selftest_fixture(Path(td))
        res = run_cases(lambda: _FakeClient(script), tools, "claude-sonnet-5-5", cases,
                        cache_dir=Path(td), log=lambda *_: None)
    by = {r["id"]: r for r in res}
    a, b, c, d = cases
    assert by[a["id"]]["outcome"] == "pass" and by[a["id"]]["picked"] == a["accept"][0]
    assert by[b["id"]]["outcome"] == "wrong" and by[b["id"]]["picked"] == "beam_modal"
    assert by[c["id"]]["outcome"] == "no_tool" and by[c["id"]]["text"] == "Which analysis?"
    assert by[d["id"]]["outcome"] == "refusal"
    rep = summarize(res, "claude-sonnet-5-5", "bundle")
    assert rep["accuracy"] == 0.25 and rep["outcomes"] == {"pass": 1, "wrong": 1, "no_tool": 1, "refusal": 1}
    assert rep["per_cluster"]["modal"] == {"n": 4, "pass": 1, "accuracy": 0.25}
    conf = {(r["expected"], r["picked"]): r["count"] for r in rep["confusion"]}
    assert conf == {("/".join(b["accept"]), "beam_modal"): 1, ("/".join(c["accept"]), "<no_tool>"): 1,
                    ("/".join(d["accept"]), "<refusal>"): 1}, conf
    # max_tokens with no tool call is its own outcome, not a no_tool
    mt = grade(a, {"content": [{"type": "thinking"}], "stop_reason": "max_tokens", "usage": {}})
    assert mt["outcome"] == "max_tokens"
    # cost: 1 cache write of 10k, 3 reads of 10k, 4x50 input, 4x200 output
    u = rep["usage"]
    assert u == {"in_tokens": 200, "cache_write": 10_000, "cache_read": 30_000, "out_tokens": 800}, u
    pi, pcw, pcr, po = PRICING["claude-sonnet-5-5"]
    assert abs(rep["cost_usd"] - round((200 * pi + 10_000 * pcw + 30_000 * pcr + 800 * po) / 1e6, 4)) < 1e-9
    assert rep["cache_savings_usd"] > 0 and rep["cost_without_cache_usd"] > rep["cost_usd"]
    assert rep["billed_this_run_usd"] == rep["cost_usd"] and rep["fresh_calls"] == 4


def test_selftest_response_cache_keying():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cases, tools, script = _selftest_fixture(tmp)

        def run(tl, model="claude-sonnet-5-5", factory=None):
            fake = _FakeClient(script)
            res = run_cases(factory or (lambda: fake), tl, model, cases, cache_dir=tmp, log=lambda *_: None)
            return fake, res

        run(tools)
        # a rerun is served wholly from disk and never builds a client (no API key needed)
        def boom():
            raise AssertionError("a fully cached rerun must not construct a client")
        _, res = run(tools, factory=boom)
        assert all(r["from_cache"] for r in res)
        rep = summarize(res, "claude-sonnet-5-5", "bundle")
        assert rep["billed_this_run_usd"] == 0 and rep["fresh_calls"] == 0 and rep["cost_usd"] > 0
        # editing ONE exposed description invalidates every case that sees it
        edited = [dict(t, description=t["description"] + " Edited.") if t["name"] == "beam_modal" else t
                  for t in tools]
        fake, res = run(edited)
        assert len(fake.calls) == len(cases) and not any(r["from_cache"] for r in res)
        # a tool the cluster does not expose does not invalidate it
        hidden = sorted(TS.FAMILIES["plm"])[0]
        fake, _ = run(edited + _fake_tools([hidden]))
        assert not fake.calls, "a tool outside the exposed families must not change the cache key"
        # another model is another cache entry, and the model is in the file name
        fake, _ = run(edited, model="claude-haiku-5-5")
        assert len(fake.calls) == len(cases)
        names = {p.name for p in tmp.iterdir()}
        assert any(".claude-haiku-5-5." in n for n in names) and any(".claude-sonnet-5-5." in n for n in names)


def test_selftest_case_selection():
    assert len(select_cases(None)) == len(CASES)
    sel = select_cases("modal,ctl_gear")
    assert {c["cluster"] for c in sel} == {"modal", "controls"} and len(sel) == \
        sum(c["cluster"] == "modal" for c in CASES) + 1
    try:
        select_cases("no_such_case")
    except SystemExit:
        pass
    else:
        raise AssertionError("an unknown case name must stop the run")


# --- FREE tier: deferred loading (#514) ----------------------------------------

def _ns_tool_use(name, inp, i="t1"):
    return SimpleNamespace(type="tool_use", id=i, name=name, input=inp)


def _ns_server_search(query, refs):
    return [SimpleNamespace(type="server_tool_use", id="srvtoolu_1", name="tool_search_tool_bm25",
                            input={"query": query}),
            SimpleNamespace(type="tool_search_tool_result", tool_use_id="srvtoolu_1",
                            content=SimpleNamespace(type="tool_search_tool_search_result",
                                                    tool_references=[SimpleNamespace(type="tool_reference",
                                                                                     tool_name=n) for n in refs]))]


class _TurnClient:
    """Answers each case turn by turn: {request substring: [turn0 blocks+stop, turn1 ...]}."""

    def __init__(self, script):
        self.script, self.calls = script, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        req = kw["messages"][0]["content"]
        turns = next(v for k, v in self.script.items() if k in req)
        turn = sum(m["role"] == "assistant" for m in kw["messages"])
        blocks, stop = turns[min(turn, len(turns) - 1)]
        usage = SimpleNamespace(input_tokens=10, output_tokens=20,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="")] + blocks,
                               stop_reason=stop, usage=usage)


def test_deferred_request_shape():
    import tempfile
    cases, tools, _ = _selftest_fixture(None)
    case = cases[0]
    want = exposed_names(exposed_families("modal")) & {t["name"] for t in tools}
    for loading in ("search_bm25", "search_regex", "client_search"):
        fake = _TurnClient({case["request"]: [([_ns_tool_use(case["accept"][0], {})], "tool_use")]})
        with tempfile.TemporaryDirectory() as td:
            run_cases(lambda: fake, tools, "claude-sonnet-5-5", [case], cache_dir=Path(td),
                      log=lambda *_: None, loading=loading)
        kw = fake.calls[0]
        mcp_tools = [t for t in kw["tools"] if t["name"] in want]
        assert {t["name"] for t in mcp_tools} == want
        assert all(t.get("defer_loading") is True for t in mcp_tools), "every MCP tool must be deferred"
        assert not any("cache_control" in t for t in kw["tools"]), "a deferred tool may not carry cache_control"
        search = kw["tools"][-1]
        assert not search.get("defer_loading"), "the search tool itself must stay loaded"
        assert len(kw["tools"]) == len(want) + 1
        if loading in SERVER_SEARCH:
            assert search == SERVER_SEARCH[loading]
        else:
            assert search["name"] == CLIENT_SEARCH_NAME and "input_schema" in search
        sys_block = kw["system"]
        assert isinstance(sys_block, list) and sys_block[-1]["cache_control"] == {"type": "ephemeral"}
        text = sys_block[0]["text"]
        assert text.startswith(SYSTEM_PROMPT) and SEARCH_NOTE in text
        listed = loading == "client_search"
        assert all((f"\n{n}" in text) == listed for n in want), \
            "client_search lists every deferred name; the server modes list none"
        assert kw["tool_choice"] == {"type": "auto"}
    # eager is untouched: the #508 request, and so its cached responses, stay valid
    assert build_request(case, to_anthropic(tools, exposed_families("modal")), "m")["system"] == SYSTEM_PROMPT
    try:
        to_anthropic(tools, exposed_families("modal"), "lazy")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown loading mode must be rejected")


def test_client_search_engine():
    cat = [{"name": "fem_modal", "description": "Set up a CalculiX eigenfrequency analysis on a meshed body."},
           {"name": "beam_modal", "description": "Closed-form natural frequencies of a beam, no mesh."},
           {"name": "fem_modal_results", "description": "Read mode shapes from a solved analysis."},
           {"name": "render_view", "description": "Fast preview image."}]
    assert client_search("select:beam_modal,nope,fem_modal,beam_modal", cat) == ["beam_modal", "fem_modal"]
    assert client_search("SELECT:render_view", cat) == ["render_view"]
    got = client_search("modal frequencies beam", cat)
    assert got[0] == "beam_modal", got                      # name word x2 + description
    assert "render_view" not in got
    assert client_search("modal", cat, 2) == ["fem_modal", "beam_modal"], "ties keep tools/list order"
    assert client_search("zzz", cat) == []
    assert len(client_search("a", [{"name": f"t{i}", "description": "a"} for i in range(9)])) == CLIENT_SEARCH_LIMIT


def test_deferred_loop_and_grading():
    import tempfile
    cases, tools, _ = _selftest_fixture(None)
    a, b, c, d = cases
    right, wrong = a["accept"][0], "harmonic_response"
    assert wrong not in a["accept"] + b["accept"]
    # client_search: search -> loaded -> call; the tool_result carries tool_reference blocks
    script = {
        a["request"]: [([_ns_tool_use(CLIENT_SEARCH_NAME, {"query": f"select:{right}"}, "s1")], "tool_use"),
                       ([_ns_tool_use(right, {})], "tool_use")],
        b["request"]: [([_ns_tool_use(CLIENT_SEARCH_NAME, {"query": f"select:{wrong}"}, "s1")], "tool_use"),
                       ([_ns_tool_use(wrong, {})], "tool_use")],
        # never stops searching -> capped at MAX_TURNS, graded no_tool
        c["request"]: [([_ns_tool_use(CLIENT_SEARCH_NAME, {"query": "zzz"}, "s1")], "tool_use")],
        d["request"]: [([SimpleNamespace(type="text", text="Which one?")], "end_turn")],
    }
    fake = _TurnClient(script)
    with tempfile.TemporaryDirectory() as td:
        res = run_cases(lambda: fake, tools, "claude-sonnet-5-5", cases, cache_dir=Path(td),
                        log=lambda *_: None, loading="client_search")
    by = {r["id"]: r for r in res}
    assert by[a["id"]]["outcome"] == "pass" and by[a["id"]]["surfaced"] is True and by[a["id"]]["turns"] == 2
    assert by[a["id"]]["searches"] == [{"query": f"select:{right}", "refs": [right]}]
    assert by[b["id"]]["outcome"] == "wrong" and by[b["id"]]["surfaced"] is False
    assert by[c["id"]]["outcome"] == "no_tool" and by[c["id"]]["turns"] == MAX_TURNS
    assert by[d["id"]]["outcome"] == "no_tool" and by[d["id"]]["searches"] == []
    second = next(kw for kw in fake.calls if a["request"] in kw["messages"][0]["content"]
                  and len(kw["messages"]) == 3)
    tr = second["messages"][2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "s1"
    assert tr["content"] == [{"type": "tool_reference", "tool_name": right}]
    assert second["tools"] == fake.calls[0]["tools"], "every turn resends the same tools array"
    empty = next(kw for kw in fake.calls if c["request"] in kw["messages"][0]["content"] and len(kw["messages"]) == 3)
    assert isinstance(empty["messages"][2]["content"][0]["content"], str), "no matches -> a text result, not []"
    rep = summarize(res, "claude-sonnet-5-5", "bundle", "client_search")
    dsc = rep["discovery"]
    assert dsc["surfaced"] == 1 and dsc["n"] == 4 and dsc["miss_surfaced"] == 0 and dsc["miss_never_surfaced"] == 3
    row = next(r for r in rep["confusion"] if r["picked"] == wrong)
    assert row["never_surfaced"] == 1
    assert rep["usage"]["in_tokens"] == 10 * sum(r["turns"] for r in res), "usage sums every turn"
    # server-side search: one call, blocks in the same response; pause_turn is resumed
    script = {
        a["request"]: [(_ns_server_search("eigenfrequency mesh", [right, wrong]), "pause_turn"),
                       ([_ns_tool_use(wrong, {})], "tool_use")],
        b["request"]: [(_ns_server_search("modes", [b["accept"][0]]) + [_ns_tool_use(b["accept"][0], {})],
                        "tool_use")],
    }
    fake = _TurnClient(script)
    with tempfile.TemporaryDirectory() as td:
        res = run_cases(lambda: fake, tools, "claude-sonnet-5-5", [a, b], cache_dir=Path(td),
                        log=lambda *_: None, loading="search_bm25")
    by = {r["id"]: r for r in res}
    assert by[a["id"]]["outcome"] == "wrong" and by[a["id"]]["surfaced"] is True and by[a["id"]]["turns"] == 2
    assert by[a["id"]]["searches"] == [{"query": "eigenfrequency mesh", "refs": [right, wrong]}]
    assert by[b["id"]]["outcome"] == "pass" and by[b["id"]]["turns"] == 1
    assert len(fake.calls) == 3
    rep = summarize(res, "claude-sonnet-5-5", "bundle", "search_bm25")
    assert rep["discovery"]["miss_surfaced"] == 1 and rep["discovery"]["recall"] == 1.0
    # eager rows carry no discovery block
    assert summarize([dict(r, surfaced=None) for r in res], "claude-sonnet-5-5", "bundle")["discovery"] is None


def test_gitignore_covers_the_response_cache():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "tests/tool_selection_cache/" in lines, "add tests/tool_selection_cache/ to .gitignore"


# --- FREE tier, when mcp is importable: the live tools/list --------------------

def live_test_registry_matches_and_converts():
    tools = live_tools_list()
    reg = static_registry()
    names = {t["name"] for t in tools}
    assert names == set(reg), f"live vs static registry differ: {sorted(names ^ set(reg))[:8]}"
    missing = sorted(_corpus_tools() - names)
    assert not missing, f"corpus tools absent from live tools/list: {missing}"
    short = sorted((len((t.get("description") or "").strip()), t["name"]) for t in tools
                   if len((t.get("description") or "").strip()) < DESC_FLOOR)
    assert not short, f"live descriptions under {DESC_FLOOR} chars: {short}"
    blocks = {}
    for c in CASES:
        fams = exposed_families(c["cluster"])
        if fams not in blocks:
            blocks[fams] = to_anthropic(tools, fams)
        got = {t["name"] for t in blocks[fams]}
        assert set(c["accept"]) <= got, f"{c['id']}: {sorted(set(c['accept']) - got)} not in the tool block"
    for fams, block in sorted(blocks.items(), key=lambda kv: len(json.dumps(kv[1]))):
        chars = len(json.dumps(block))
        print(f"    {len(block):3d} tools, {chars:7d} chars (~{chars // 4} tokens): {','.join(fams)}")


# --- runners ------------------------------------------------------------------

def _run(tests) -> int:
    failures = []
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:52s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:52s} ({time.time() - t0:.2f}s)")
    for name, tb in failures:
        print(f"\n--- {name} ---\n{tb}")
    return len(failures)


def free_main():
    ensure_mcp_interpreter(__file__, needs=HOST_DEPS)
    g = globals()
    tests = [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]
    if importlib.util.find_spec("mcp") is not None:
        tests.append(("live_test_registry_matches_and_converts", live_test_registry_matches_and_converts))
    else:
        print(f"  SKIP live tools/list tier — {mcp_skip_reason()}")
    t0 = time.time()
    failed = _run(tests)
    print(f"\n== {len(tests) - failed}/{len(tests)} passed  ({time.time() - t0:.1f}s) ==")
    if failed:
        sys.exit(1)


def paid_main():
    ensure_mcp_interpreter(__file__, needs=HOST_DEPS + ("anthropic",))
    for mod in ("mcp", "anthropic"):
        if importlib.util.find_spec(mod) is None:
            sys.exit(f"RUN_RELIABILITY=1 needs `{mod}` importable ({mcp_skip_reason(HOST_DEPS + ('anthropic',))})")
    model = resolve_model(os.environ.get("TOOL_SELECTION_MODEL"), default="sonnet")
    mode = os.environ.get("TOOL_SELECTION_TOOLSETS", "bundle").strip() or "bundle"
    loading = check_loading(os.environ.get("TOOL_SELECTION_LOADING", "eager").strip() or "eager")
    cases = select_cases(os.environ.get("TOOL_SELECTION_CASES"))
    print(f"tool selection: {len(cases)} cases, model {model}, toolsets {mode}, loading {loading}")

    def client():
        import anthropic
        return anthropic.Anthropic(max_retries=4)

    results = run_cases(client, live_tools_list(), model, cases, mode=mode, loading=loading,
                        refresh=os.environ.get("TOOL_SELECTION_REFRESH") == "1")
    rep = summarize(results, model, mode, loading)
    print_report(rep)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"report.{mode}" if loading == "eager" else f"report.{mode}.{loading}"
    out = CACHE_DIR / response_cache_name(stem, model, "json")
    out.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(f"  report: {out}")
    floor = os.environ.get("TOOL_SELECTION_MIN")
    if floor and rep["accuracy"] < float(floor):
        print(f"FAIL accuracy {rep['accuracy']:.1%} < TOOL_SELECTION_MIN {float(floor):.1%}")
        sys.exit(1)


if __name__ == "__main__":
    if os.environ.get("RUN_RELIABILITY") == "1":
        paid_main()
    else:
        free_main()

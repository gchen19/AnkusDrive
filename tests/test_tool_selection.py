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


def to_anthropic(tools: list, families: tuple) -> list:
    """tools/list entries -> Anthropic tool definitions, for the enabled families.

    Keeps the server's order (deterministic, and what a client sends), copies
    ``name`` / ``description`` / ``inputSchema`` only, and marks the LAST tool with
    ``cache_control`` so the whole tool block is a cached prefix across cases.
    Filtering the full list by family equals what the server serves with only those
    families on; test_toolsets_budget asserts that for the bundle default."""
    keep = exposed_names(families)
    out = [{"name": t["name"], "description": t.get("description") or "",
            "input_schema": t["inputSchema"]} for t in tools if t["name"] in keep]
    if out:
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def build_request(case: dict, tools: list, model: str) -> dict:
    return {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT, "tools": tools,
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": case["request"]}]}


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
        else:
            blocks.append({"type": kind})       # thinking etc.: presence only
    return {"content": blocks, "stop_reason": getattr(resp, "stop_reason", None),
            "usage": usage_dict(resp)}


def grade(case: dict, rec: dict) -> dict:
    """pass / wrong / no_tool / refusal / max_tokens, and the picked tool."""
    picked = next((b["name"] for b in rec["content"] if b["type"] == "tool_use"), None)
    stop = rec.get("stop_reason")
    if picked is not None:
        outcome = "pass" if picked in case["accept"] else "wrong"
    elif stop in ("refusal", "max_tokens"):
        outcome = stop
    else:
        outcome = "no_tool"
    text = " ".join(b.get("text", "") for b in rec["content"] if b["type"] == "text").strip()
    return {"id": case["id"], "cluster": case["cluster"], "accept": list(case["accept"]),
            "picked": picked, "outcome": outcome, "stop_reason": stop,
            "text": text[:300], "usage": rec["usage"]}


# --- the run ------------------------------------------------------------------

def run_cases(client_factory, tools_list: list, model: str, cases: list, mode: str = "bundle",
              cache_dir: Path | None = None, refresh: bool = False, log=print) -> list:
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
            tool_blocks[fams] = to_anthropic(tools_list, fams)
        req = build_request(case, tool_blocks[fams], model)
        path = cache_path(case, req, cache_dir)
        if path.exists() and not refresh:
            rec, cached = json.loads(path.read_text(encoding="utf-8")), True
        else:
            if client is None:
                client = client_factory()
            rec, cached = serialize(client.messages.create(**req)), False
            path.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        g = grade(case, rec)
        g["from_cache"] = cached
        g["n_tools"] = len(req["tools"])
        results.append(g)
        mark = {"pass": "ok  "}.get(g["outcome"], "MISS")
        log(f"  {mark} {case['id']:20s} {g['outcome']:10s} picked={g['picked']}"
            f"{'' if cached else '  (fresh)'}")
    return results


def _sum_usage(rows) -> dict:
    tot = Counter()
    for r in rows:
        tot.update(r["usage"])
    return {k: tot.get(k, 0) for k in ("in_tokens", "cache_write", "cache_read", "out_tokens")}


def summarize(results: list, model: str, mode: str) -> dict:
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
    confusion = [{"expected": e, "picked": p, "count": len(ids), "cases": ids}
                 for (e, p), ids in sorted(conf.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    usage = _sum_usage(results)
    fresh = _sum_usage(r for r in results if not r["from_cache"])
    cost = cost_of(usage, model)
    # What the same tokens would cost with no prompt caching: every cached or
    # cache-written input token billed as plain input.
    uncached = {"in_tokens": usage["in_tokens"] + usage["cache_write"] + usage["cache_read"],
                "out_tokens": usage["out_tokens"]}
    no_cache_cost = cost_of(uncached, model)
    return {"model": model, "toolsets": mode, "n": n,
            "accuracy": round(outcomes["pass"] / n, 4) if n else 0.0,
            "outcomes": dict(outcomes), "per_cluster": per_cluster, "confusion": confusion,
            "usage": usage, "cost_usd": round(cost, 4),
            "cost_without_cache_usd": round(no_cache_cost, 4),
            "cache_savings_usd": round(no_cache_cost - cost, 4),
            "billed_this_run_usd": round(cost_of(fresh, model), 4),
            "fresh_calls": sum(not r["from_cache"] for r in results),
            "cases": results}


def print_report(rep: dict) -> None:
    print(f"\n== tool selection: {rep['model']}, toolsets={rep['toolsets']} ==")
    print(f"  accuracy {rep['accuracy']:.1%}  ({rep['outcomes'].get('pass', 0)}/{rep['n']})   "
          f"outcomes {rep['outcomes']}")
    print("  per cluster (worst first):")
    for c, v in rep["per_cluster"].items():
        print(f"    {c:18s} {v['pass']:2d}/{v['n']:<2d}  {v['accuracy']:.0%}")
    if rep["confusion"]:
        print("  confusions (asked for -> picked, count):")
        for row in rep["confusion"]:
            print(f"    {row['expected']:34s} -> {row['picked']:28s} x{row['count']}  {row['cases']}")
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
    cases = select_cases(os.environ.get("TOOL_SELECTION_CASES"))
    print(f"tool selection: {len(cases)} cases, model {model}, toolsets {mode}")

    def client():
        import anthropic
        return anthropic.Anthropic(max_retries=4)

    results = run_cases(client, live_tools_list(), model, cases, mode=mode,
                        refresh=os.environ.get("TOOL_SELECTION_REFRESH") == "1")
    rep = summarize(results, model, mode)
    print_report(rep)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / response_cache_name(f"report.{mode}", model, "json")
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

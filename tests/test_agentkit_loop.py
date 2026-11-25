"""
The shared Claude tool-use loop (orchestration/agentkit.py, issue #518) — free,
no LLM, no key, no FreeCAD.

agentkit.tool_loop is the one copy of the loop that the coordinator, the M2
benchmark (tests/test_multiagent_m2.py) and the Layer D reliability tasks
(tests/test_reliability_tasks.py) all run. This drives it with a ScriptedClient
against a fake in-process worker, so the loop's contract is checked on any
machine: stop conditions, error handling, the trace, token accounting, cache
breakpoints, and that M2 and Layer D really go through it with their own tool
surface, dispatch and turn budget.

Run: .venv/bin/python3 tests/test_agentkit_loop.py
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from orchestration import agentkit  # noqa: E402
from orchestration.agentkit import ScriptedClient, text_block, tool_use  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


class FakeWorker:
    """Answers worker calls in-process. 'explode' raises; save_document records."""
    def __init__(self):
        self.calls = []
        self.saved = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, name, **kw):
        self.calls.append((name, kw))
        if name == "explode":
            raise RuntimeError("kaboom")
        if name == "save_document":
            self.saved = kw["path"]
            return {"saved": kw["path"]}
        return {"handle": f"{name}_{len(self.calls)}"}


class RecordingClient(ScriptedClient):
    """ScriptedClient that keeps every request's kwargs for inspection."""
    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def create(self, **kw):
        # Snapshot now: the loop mutates the message list in place afterwards.
        self.requests.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        return super().create(**kw)


def _steps(*turns):
    def script(task, turn):
        return turns[turn] if turn < len(turns) else [text_block("done")]
    return script


BUILD_AND_SAVE = _steps(
    [tool_use("a", "new_document", {"name": "d"})],
    [tool_use("b", "add_primitive", {"kind": "box", "w": 1, "d": 1, "h": 1})],
    [tool_use("c", "save_component", {})],
)


def test_builds_saves_and_stops():
    w = FakeWorker()
    trace = []
    r = agentkit.tool_loop(w, ScriptedClient(BUILD_AND_SAVE), "m", "task", "/tmp/x.FCStd",
                           trace=trace)
    _check("ok_built after save_component", r["ok_built"], True)
    _check("stops on the save turn", r["turns"], 3)
    _check("save_component maps to save_document with the harness path",
           w.saved, "/tmp/x.FCStd")
    _check("trace records every call in order", [t["name"] for t in trace],
           ["new_document", "add_primitive", "save_component"])
    # ScriptedClient bills 5 in / 20 out / 100 cache-read / 50 cache-write a turn.
    _check("usage summed over turns",
           {k: r[k] for k in ("in_tokens", "out_tokens", "cache_read", "cache_write")},
           {"in_tokens": 15, "out_tokens": 60, "cache_read": 300, "cache_write": 150})


def test_tool_error_is_returned_not_raised():
    w = FakeWorker()
    trace = []
    client = RecordingClient(_steps([tool_use("a", "explode", {})]))
    r = agentkit.tool_loop(w, client, "m", "task", "/tmp/x", trace=trace)
    _check("an erroring tool does not mark the part built", r["ok_built"], False)
    _check("trace marks the error", (trace[0]["error"], trace[0]["result"]),
           (True, "RuntimeError: kaboom"))
    res = client.requests[1]["messages"][-1]["content"][0]
    _check("model sees an is_error tool_result",
           (res["type"], res["is_error"], res["content"]),
           ("tool_result", True, "ERROR: RuntimeError: kaboom"))
    _check("loop ends when the model stops calling tools", r["turns"], 2)


def test_turn_budget():
    forever = lambda task, turn: [tool_use(f"t{turn}", "new_document", {"name": "d"})]  # noqa: E731
    r = agentkit.tool_loop(FakeWorker(), ScriptedClient(forever), "m", "t", "/tmp/x",
                           max_turns=4)
    _check("max_turns caps the conversation", (r["turns"], r["ok_built"]), (4, False))


def test_request_shape_and_caching():
    tools = agentkit.TOOLS + [{"name": "extra", "description": "x",
                               "input_schema": {"type": "object", "properties": {}}}]
    client = RecordingClient(BUILD_AND_SAVE)
    agentkit.tool_loop(FakeWorker(), client, "claude-x", "task", "/tmp/x",
                       system="SYS", tools=tools)
    req = client.requests[0]
    _check("custom tools are sent", [t["name"] for t in req["tools"]],
           [t["name"] for t in tools])
    _check("cache breakpoint on the last tool only",
           [("cache_control" in t) for t in req["tools"]],
           [False] * (len(tools) - 1) + [True])
    _check("caller's tool list is not mutated", "cache_control" in tools[-1], False)
    _check("system is cached", req["system"],
           [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])
    later = client.requests[2]["messages"]
    marks = [i for i, m in enumerate(later) if m["role"] == "user"
             and isinstance(m["content"], list)
             and any("cache_control" in b for b in m["content"] if isinstance(b, dict))]
    _check("one conversation breakpoint, on the latest user turn", marks, [len(later) - 1])


def test_custom_dispatch():
    seen = []

    def dispatch(w, save_path, name, args):
        seen.append(name)
        return agentkit._dispatch(w, save_path, name, args)

    agentkit.tool_loop(FakeWorker(), ScriptedClient(BUILD_AND_SAVE), "m", "t", "/tmp/x",
                       dispatch=dispatch)
    _check("custom dispatch handles every call", seen,
           ["new_document", "add_primitive", "save_component"])


def test_run_builder_opens_its_own_worker():
    made = []

    def fake_worker():
        made.append(FakeWorker())
        return made[-1]

    real, agentkit.Worker = agentkit.Worker, fake_worker
    try:
        r = agentkit.run_builder(ScriptedClient(BUILD_AND_SAVE), "m", "t", "/tmp/y")
    finally:
        agentkit.Worker = real
    _check("run_builder keeps its return keys", sorted(r),
           sorted(["ok_built", "turns", "in_tokens", "out_tokens", "cache_read", "cache_write"]))
    _check("run_builder used one fresh worker", (len(made), made[0].saved), (1, "/tmp/y"))


def test_m2_runs_the_shared_loop():
    import test_multiagent_m2 as m2
    w = FakeWorker()
    client = RecordingClient(_steps(
        [tool_use("a", "rotate", {"handle": "nope", "angle_deg": 90})],
        [tool_use("b", "save_component", {})]))
    real_w, real_turns = m2.Worker, m2.MAX_TURNS
    m2.Worker = lambda: w
    m2.MAX_TURNS = 7
    w.call = (lambda orig: lambda name, **kw: {} if name == "list_handles" else orig(name, **kw))(w.call)
    try:
        r = m2.run_agent(client, "m", m2.SYSTEM, "build", "/tmp/m2.FCStd")
    finally:
        m2.Worker, m2.MAX_TURNS = real_w, real_turns
    _check("M2 has no private copy of the loop",
           [n for n in ("_cached_tools", "_cached_system", "_mark_conversation_cache")
            if hasattr(m2, n)], [])
    _check("M2 sends its own tool surface (with rotate)",
           [t["name"] for t in client.requests[0]["tools"]], [t["name"] for t in m2.TOOLS])
    res = client.requests[1]["messages"][-1]["content"][0]
    _check("M2's dispatch handles rotate (unknown handle -> tool error)",
           (res["is_error"], res["content"].startswith("ERROR: KeyError")), (True, True))
    _check("M2 builds and saves through the shared loop", (r["ok_built"], w.saved),
           (True, "/tmp/m2.FCStd"))

    forever = lambda task, turn: [tool_use(f"t{turn}", "new_document", {"name": "d"})]  # noqa: E731
    m2.Worker = lambda: FakeWorker()
    m2.MAX_TURNS = 3
    try:
        r = m2.run_agent(ScriptedClient(forever), "m", m2.SYSTEM, "build", "/tmp/m2")
    finally:
        m2.Worker, m2.MAX_TURNS = real_w, real_turns
    _check("M2_MAX_TURNS budget still applies", r["turns"], 3)
    _check("M2 dry run uses agentkit's ScriptedClient script",
           m2._dry_script("Now build the PEG component", 2)[0].name, "save_component")


def test_reliability_runs_the_shared_loop():
    import test_reliability_tasks as rel
    w = FakeWorker()
    real = rel.Worker
    rel.Worker = lambda: w
    task = rel.Task("fake", 1, "Build a cube", lambda wk, trace: (wk is w, f"{len(trace)} calls"))
    client = RecordingClient(rel._dry_script)
    try:
        r = rel.run_trial(client, "claude-scripted", task, "/tmp/rel.FCStd")
    finally:
        rel.Worker = real
    _check("Layer D sends agentkit's tools plus its extras",
           [t["name"] for t in client.requests[0]["tools"]], [t["name"] for t in rel.TOOLS])
    _check("checker gets the live worker and the trace", (r["passed"], r["detail"]),
           (True, "3 calls"))
    _check("Layer D result shape kept",
           (r["built"], r["turns"], [t["name"] for t in r["trace"]]),
           (True, 3, ["new_document", "add_primitive", "save_component"]))


def main():
    print("== agentkit shared tool-use loop (#518, no API, no FreeCAD) ==")
    for t in (test_builds_saves_and_stops, test_tool_error_is_returned_not_raised,
              test_turn_budget, test_request_shape_and_caching, test_custom_dispatch,
              test_run_builder_opens_its_own_worker, test_m2_runs_the_shared_loop,
              test_reliability_runs_the_shared_loop):
        try:
            t()
        except Exception as e:
            global _FAIL
            _FAIL += 1
            import traceback
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

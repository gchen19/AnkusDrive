"""
Host-side agent toolkit for AnkusDrive orchestration.

The reusable parts of an LLM-driven component builder: the AnkusDrive tool surface
exposed to a model, a tool-use loop with prompt caching, cost accounting, and a
scripted stub client so the whole orchestration can be exercised with no API.

This is the one copy of the loop (issue #518). The coordinator, the M2 benchmark
(tests/test_multiagent_m2.py) and the Layer D reliability tasks
(tests/test_reliability_tasks.py) all run it. A harness that needs more tools, a
different dispatch, its own turn budget, or the live worker after the loop passes
them to :func:`tool_loop` instead of keeping a copy, so an API change lands once.
"""
import json
import types

from ankusdrive import Worker


from orchestration.claude_models import (  # noqa: E402,F401  (re-exported)
    MODELS, PRICING, cost_of, resolve_model, tool_choice_for, usage_dict,
)
MAX_TURNS = 14

BUILDER_SYSTEM = (
    "You are a mechanical design agent driving a CAD kernel through tools. Build "
    "exactly the component described, in millimetres. Reason about the geometry, "
    "call the tools, optionally verify with mass_properties, then call "
    "save_component LAST. Do not ask questions — build it."
)

# The build tool surface. Small on purpose: enough for primitive-or-boolean
# components plus publishing named interface frames for mate-by-frame assembly.
TOOLS = [
    {"name": "new_document",
     "description": "Create a new FreeCAD document and make it active. Call once before building.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "add_primitive",
     "description": ("Add a primitive solid. kind='box' uses w,d,h; kind='cylinder' uses "
                     "r,h; kind='sphere' uses r. Optional placement [x,y,z] translates it. "
                     "Returns {handle, name, volume}; use the handle in boolean_op."),
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["box", "cylinder", "sphere"]},
         "w": {"type": "number"}, "d": {"type": "number"}, "h": {"type": "number"},
         "r": {"type": "number"},
         "placement": {"type": "array", "items": {"type": "number"}},
         "name": {"type": "string"}}, "required": ["kind"]}},
    {"name": "boolean_op",
     "description": ("Boolean of two existing solids by handle. op='cut' (base minus tool), "
                     "'fuse', or 'common'. Returns {handle, volume}."),
     "input_schema": {"type": "object", "properties": {
         "op": {"type": "string", "enum": ["cut", "fuse", "common"]},
         "base": {"type": "string"}, "tool": {"type": "string"}},
         "required": ["op", "base", "tool"]}},
    {"name": "mass_properties",
     "description": "Volume, area, centroid, bounding_box_mm [xmin,ymin,zmin,xmax,ymax,zmax] of a handle.",
     "input_schema": {"type": "object", "properties": {"handle": {"type": "string"}},
                      "required": ["handle"]}},
    {"name": "publish_interface",
     "description": ("Record a named interface frame on the component so other parts mate to "
                     "it. frame = {origin:[x,y,z], z_axis:[...]?, x_axis:[...]?}. Call for "
                     "each mating feature (a seat, a bolt circle, a bore axis)."),
     "input_schema": {"type": "object", "properties": {
         "handle": {"type": "string"}, "name": {"type": "string"},
         "frame": {"type": "object"}}, "required": ["handle", "name", "frame"]}},
    {"name": "save_component",
     "description": ("Save the finished component to its file. Call this LAST. Takes no path "
                     "— the harness supplies it."),
     "input_schema": {"type": "object", "properties": {}}},
]




def _dispatch(w, save_path, name, args):
    if name == "save_component":
        return w.call("save_document", path=str(save_path))
    return w.call(name, **args)


# --- prompt caching ----------------------------------------------------------

_EPHEMERAL = {"type": "ephemeral"}


def _cached_tools(tools=None):
    """A copy of ``tools`` (default :data:`TOOLS`) with a cache breakpoint on the last."""
    cached = [dict(t) for t in (TOOLS if tools is None else tools)]
    cached[-1] = {**cached[-1], "cache_control": _EPHEMERAL}
    return cached


def _cached_system(system):
    return [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]


def _mark_conversation_cache(messages):
    """Move a cache breakpoint to the last block of the latest user message."""
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for blk in m["content"]:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
    last = messages[-1]
    if last["role"] != "user":
        return
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"], "cache_control": _EPHEMERAL}]
    elif isinstance(last["content"], list) and last["content"]:
        tail = last["content"][-1]
        if isinstance(tail, dict):
            tail["cache_control"] = _EPHEMERAL


def tool_loop(w, client, model, task, save_path, *, system=BUILDER_SYSTEM, tools=None,
              dispatch=None, max_turns=MAX_TURNS, trace=None):
    """Run one builder conversation against an already-open Worker ``w``.

    The loop ends when the model stops calling tools, when ``save_component``
    succeeds, or after ``max_turns`` model calls. The caller owns the worker, so it
    can inspect the live geometry afterwards (the reliability checkers do).

    tools    : tool schemas sent to the model (default :data:`TOOLS`).
    dispatch : ``(w, save_path, name, args) -> result`` (default :func:`_dispatch`).
    trace    : optional list; each tool call appends
               ``{name, args, result, error}`` (result is the error text on failure).

    Returns ``{ok_built, turns, in_tokens, out_tokens, cache_read, cache_write}``.
    """
    dispatch = dispatch or _dispatch
    cached_tools = _cached_tools(tools)
    sys_blocks = _cached_system(system)
    usage = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0}
    saved = False
    turn = 0
    messages = [{"role": "user", "content": task}]
    for turn in range(max_turns):
        _mark_conversation_cache(messages)
        resp = client.messages.create(
            model=model, max_tokens=4096, system=sys_blocks,
            tools=cached_tools, messages=messages)
        for k, v in usage_dict(resp).items():
            usage[k] += v
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            args = dict(tu.input)
            try:
                out = dispatch(w, save_path, tu.name, args)
                if trace is not None:
                    trace.append({"name": tu.name, "args": args, "result": out, "error": False})
                if tu.name == "save_component":
                    saved = True
                payload, is_err = json.dumps(out), False
            except Exception as e:
                if trace is not None:
                    trace.append({"name": tu.name, "args": args,
                                  "result": f"{type(e).__name__}: {e}", "error": True})
                payload, is_err = f"ERROR: {type(e).__name__}: {e}", True
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": payload, "is_error": is_err})
        messages.append({"role": "user", "content": results})
        if saved:
            break
    return {"ok_built": saved, "turns": turn + 1, **usage}


def run_builder(client, model, task, save_path, system=BUILDER_SYSTEM, *, tools=None,
                dispatch=None, max_turns=MAX_TURNS):
    """Drive one component-builder agent through :func:`tool_loop` in its own Worker.
    Returns {ok_built, turns, in_tokens, out_tokens, cache_read, cache_write}."""
    with Worker() as w:
        return tool_loop(w, client, model, task, save_path, system=system, tools=tools,
                         dispatch=dispatch, max_turns=max_turns)


# --- round 0 contract review (RFC §8 / §11.8) --------------------------------
#
# A cheap pre-build pass: each prospective builder reads ONLY its slice and returns
# accept | amend, before a single primitive is built. It catches an infeasible
# contract ("this envelope can't hold a gear of this module") for the price of one
# short completion instead of a full build → merge → gate-fail → rebuild cycle. The
# review is a forced tool call so the verdict is structured, never prose to parse.

REVIEWER_SYSTEM = (
    "You are a component builder doing a FEASIBILITY review of your build slice "
    "BEFORE building. You see only your own component's task. Decide whether it is "
    "buildable as written. Accept if it is self-contained and feasible. Amend if a "
    "dimension is missing/contradictory or a declared keep-out envelope is too "
    "small to hold the part the task describes — and propose the minimal fix. Do "
    "not build anything; call emit_review exactly once."
)

_REVIEW_TOOL = {
    "name": "emit_review",
    "description": "Emit your accept/amend verdict on this build slice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["accept", "amend"]},
            "reason": {"type": "string"},
            # optional structured fix folded back into the slice before fan-out:
            "patch": {
                "type": "object",
                "properties": {
                    "envelope": {"type": "object"},   # {min:[...], max:[...]}
                    "task_note": {"type": "string"},  # clarification appended to task
                },
            },
        },
        "required": ["verdict", "reason"],
    },
}


def run_reviewer(client, model, slice_text):
    """One builder's round-0 review of its slice. Returns
    {verdict, reason, patch, in_tokens, out_tokens, cache_read, cache_write}."""
    resp = client.messages.create(
        model=model, max_tokens=2048, system=_cached_system(REVIEWER_SYSTEM),
        tools=[_REVIEW_TOOL], tool_choice=tool_choice_for(model, "emit_review"),
        messages=[{"role": "user", "content": slice_text}])
    usage = usage_dict(resp)
    calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not calls:
        return {"verdict": "accept", "reason": "no review emitted", "patch": {}, **usage}
    inp = dict(calls[0].input)
    return {"verdict": inp.get("verdict", "accept"), "reason": inp.get("reason", ""),
            "patch": inp.get("patch") or {}, **usage}


# --- scripted stub client (free dry runs) ------------------------------------

class _Usage:
    input_tokens = 5
    output_tokens = 20
    cache_read_input_tokens = 100
    cache_creation_input_tokens = 50


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def tool_use(tid, name, inp):
    """Build a tool_use content block for a scripted response."""
    return _Block(type="tool_use", id=tid, name=name, input=inp)


def text_block(s):
    return _Block(type="text", text=s)


class ScriptedClient:
    """Stand-in for anthropic.Anthropic whose responses come from a script:
    script(task_text, turn_index) -> list of content blocks (use tool_use/text_block).
    Lets the full orchestration run with zero API. The FreeCAD worker stays real,
    so dry runs build genuine geometry."""
    def __init__(self, script):
        self._script = script
        self.messages = self

    def create(self, model, max_tokens, system, tools, messages, **kwargs):
        # tolerate forced-tool-choice (tool_choice=) used by decompose / round-0
        # review — the script decides the response, so the hint is ignored here.
        first = messages[0]["content"]
        task = first if isinstance(first, str) else str(first)
        turn = sum(1 for m in messages if m["role"] == "assistant")
        content = self._script(task, turn)
        return types.SimpleNamespace(content=content, usage=_Usage())

"""
Host-side agent toolkit for AnkusDrive orchestration.

The reusable parts of an LLM-driven component builder: the AnkusDrive tool surface
exposed to a model, a tool-use loop with prompt caching, cost accounting, and a
scripted stub client so the whole orchestration can be exercised with no API.

Mirrors the proven loop in tests/test_multiagent_m2.py. (The benchmark keeps its
own copy so it stays self-contained; this is the canonical host-side version that
the coordinator builds on. A future cleanup could dedupe M2 onto this.)
"""
import json
import types

from ankusdrive import Worker


MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5",
    "opus": "claude-opus-4-8",
}
# per-million-token pricing (USD): input, cache-write (1.25x), cache-read (0.1x), output
PRICING = {
    "claude-haiku-4-5-20251001": (1.0, 1.25, 0.10, 5.0),
    "claude-sonnet-4-5": (3.0, 3.75, 0.30, 15.0),
    "claude-opus-4-8": (15.0, 18.75, 1.50, 75.0),
}
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


def cost_of(usage, model):
    """USD for a {in_tokens, cache_write, cache_read, out_tokens} dict."""
    pi, pcw, pcr, po = PRICING[model]
    return (usage.get("in_tokens", 0) * pi + usage.get("cache_write", 0) * pcw
            + usage.get("cache_read", 0) * pcr + usage.get("out_tokens", 0) * po) / 1e6


def _dispatch(w, save_path, name, args):
    if name == "save_component":
        return w.call("save_document", path=str(save_path))
    return w.call(name, **args)


# --- prompt caching ----------------------------------------------------------

_EPHEMERAL = {"type": "ephemeral"}


def _cached_tools():
    cached = [dict(t) for t in TOOLS]
    cached[-1] = {**cached[-1], "cache_control": _EPHEMERAL}
    return cached


def _cached_system(system):
    return [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]


def _mark_conversation_cache(messages):
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


def run_builder(client, model, task, save_path, system=BUILDER_SYSTEM):
    """Drive one component-builder agent through a tool-use loop in its own Worker.
    Returns {ok_built, turns, in_tokens, out_tokens, cache_read, cache_write}."""
    in_tok = out_tok = cache_read = cache_write = 0
    tools = _cached_tools()
    sys_blocks = _cached_system(system)
    saved = False
    turn = 0
    with Worker() as w:
        messages = [{"role": "user", "content": task}]
        for turn in range(MAX_TURNS):
            _mark_conversation_cache(messages)
            resp = client.messages.create(
                model=model, max_tokens=1024, system=sys_blocks,
                tools=tools, messages=messages)
            in_tok += resp.usage.input_tokens
            out_tok += resp.usage.output_tokens
            cache_read += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                try:
                    out = _dispatch(w, save_path, tu.name, dict(tu.input))
                    if tu.name == "save_component":
                        saved = True
                    payload, is_err = json.dumps(out), False
                except Exception as e:
                    payload, is_err = f"ERROR: {type(e).__name__}: {e}", True
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": payload, "is_error": is_err})
            messages.append({"role": "user", "content": results})
            if saved:
                break
    return {"ok_built": saved, "turns": turn + 1, "in_tokens": in_tok,
            "out_tokens": out_tok, "cache_read": cache_read, "cache_write": cache_write}


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
        model=model, max_tokens=512, system=_cached_system(REVIEWER_SYSTEM),
        tools=[_REVIEW_TOOL], tool_choice={"type": "tool", "name": "emit_review"},
        messages=[{"role": "user", "content": slice_text}])
    usage = {"in_tokens": resp.usage.input_tokens,
             "out_tokens": resp.usage.output_tokens,
             "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
             "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0}
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

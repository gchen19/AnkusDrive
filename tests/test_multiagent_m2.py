"""
Layer M2 — agents in the loop (gated, needs ANTHROPIC_API_KEY).

See tests/MULTI_AGENT_EVAL.md. Layer M1 proved the merge/gate oracle discriminates
correct from broken. M2 asks the real question: can an LLM, given only a contract
slice, drive DriftPin's tools to build a component that PASSES the gates when merged
with another agent's work — and is partitioning better than one agent doing it all?

Each agent is a fresh Anthropic API call (NOT a Claude Code subagent): a cold model
with only its manifest slice, a small DriftPin tool surface, and no shared state.
That isolation is the point — it's a controlled measurement, reproducible in CI,
billed to a metered key, not a hand-driven demo.

Gated behind RUN_RELIABILITY=1 (costs credits). Load the key first:
    anthropic-key            # the ~/.bashrc helper -> ~/.secrets/anthropic_key
    RUN_RELIABILITY=1 .venv/bin/python3 tests/test_multiagent_m2.py

This first cut runs ONE toy (peg-in-hole) with a partition condition (2 agents) and
a single-agent baseline, 1 trial each. Scope is deliberately tiny to bound cost on
the first live run; widen TRIALS / TOYS once it's proven out.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

MODEL = "claude-sonnet-4-5"
MAX_TURNS = 12               # tool-use turns per agent before we give up
CACHE_DIR = REPO / "tests" / "multiagent_cache"


# --- the DriftPin tool surface exposed to the agent --------------------------
# A deliberately small subset: enough to build a primitive-or-boolean component
# and save it. The agent reads handles back out of each tool result.

TOOLS = [
    {
        "name": "new_document",
        "description": "Create a new FreeCAD document and make it active. Call once before building.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "add_primitive",
        "description": ("Add a primitive solid. kind='box' uses w,d,h; kind='cylinder' "
                        "uses r,h; kind='sphere' uses r. Optional placement [x,y,z] "
                        "translates it. Returns {handle, name, volume}; use the handle "
                        "in boolean_op."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["box", "cylinder", "sphere"]},
                "w": {"type": "number"}, "d": {"type": "number"},
                "h": {"type": "number"}, "r": {"type": "number"},
                "placement": {"type": "array", "items": {"type": "number"}},
                "name": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "boolean_op",
        "description": ("Boolean of two existing solids by handle. op='cut' (base minus "
                        "tool), 'fuse', or 'common'. Returns {handle, volume}."),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["cut", "fuse", "common"]},
                "base": {"type": "string"}, "tool": {"type": "string"},
            },
            "required": ["op", "base", "tool"],
        },
    },
    {
        "name": "mass_properties",
        "description": "Volume, area, centroid, bounding_box_mm [xmin,ymin,zmin,xmax,ymax,zmax] of a handle.",
        "input_schema": {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    },
    {
        "name": "save_component",
        "description": ("Save the finished component to its file. Call this LAST, once the "
                        "geometry is complete. Takes no path — the harness supplies it."),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _dispatch(w, save_path, name, args):
    """Run one agent tool call against the worker. Returns a JSON-able result."""
    if name == "save_component":
        return w.call("save_document", path=str(save_path))
    # pass through to the worker handler of the same name
    return w.call(name, **args)


_EPHEMERAL = {"type": "ephemeral"}


def _cached_tools():
    """TOOLS with a cache_control breakpoint on the last tool. Tools come first in
    the canonical prompt, so this caches the whole (static) tool block — re-read at
    ~0.1x input cost on every turn after the first instead of re-billed in full."""
    cached = [dict(t) for t in TOOLS]
    cached[-1] = {**cached[-1], "cache_control": _EPHEMERAL}
    return cached


def _cached_system(system):
    """System prompt as a cached text block (static per condition)."""
    return [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]


def _mark_conversation_cache(messages):
    """Move a cache breakpoint to the last block of the most recent USER message,
    clearing any earlier one — caches the growing conversation prefix turn over
    turn. Only user messages are annotated (assistant content is SDK objects we
    don't mutate); at create() time the last message is always a user turn."""
    # strip prior conversation breakpoints
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for blk in m["content"]:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
    last = messages[-1]
    if last["role"] != "user":
        return
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"],
                            "cache_control": _EPHEMERAL}]
    elif isinstance(last["content"], list) and last["content"]:
        tail = last["content"][-1]
        if isinstance(tail, dict):
            tail["cache_control"] = _EPHEMERAL


def run_agent(client, system, task, save_path):
    """Drive one component-builder agent through a tool-use loop in its own Worker.
    Returns {ok_built, turns, in_tokens, out_tokens, cache_read, cache_write}."""
    import anthropic  # noqa: F401
    in_tok = out_tok = cache_read = cache_write = 0
    tools = _cached_tools()
    sys_blocks = _cached_system(system)
    saved = False
    with Worker() as w:
        messages = [{"role": "user", "content": task}]
        for turn in range(MAX_TURNS):
            _mark_conversation_cache(messages)
            resp = client.messages.create(
                model=MODEL, max_tokens=1024, system=sys_blocks,
                tools=tools, messages=messages,
            )
            in_tok += resp.usage.input_tokens
            out_tok += resp.usage.output_tokens
            cache_read += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break  # model stopped calling tools

            results = []
            for tu in tool_uses:
                try:
                    out = _dispatch(w, save_path, tu.name, dict(tu.input))
                    if tu.name == "save_component":
                        saved = True
                    payload = json.dumps(out)
                    is_err = False
                except Exception as e:
                    payload = f"ERROR: {type(e).__name__}: {e}"
                    is_err = True
                results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": payload, "is_error": is_err,
                })
            messages.append({"role": "user", "content": results})
            if saved:
                break
    return {"ok_built": saved, "turns": turn + 1,
            "in_tokens": in_tok, "out_tokens": out_tok,
            "cache_read": cache_read, "cache_write": cache_write}


# --- toy #1 peg-in-hole: contract, agent tasks, and the deterministic gate ----

BORE_DIA = 16.0
CLEARANCE = 0.4
PLATE = (60.0, 60.0, 10.0)   # w, d, h
BORE_CENTER = (30.0, 30.0)
PEG_LEN = 20.0

SYSTEM = (
    "You are a mechanical design agent driving a CAD kernel through tools. Build "
    "exactly the component described, using millimetres. Think about the geometry, "
    "call the tools, verify with mass_properties if useful, then call "
    "save_component LAST. Do not ask questions — build it."
)

PLATE_TASK = (
    f"Build a rectangular PLATE: {PLATE[0]}x{PLATE[1]}x{PLATE[2]} mm. It must have a "
    f"single vertical THROUGH-HOLE (a bore) of diameter {BORE_DIA} mm, centered at "
    f"x={BORE_CENTER[0]}, y={BORE_CENTER[1]}. Make the box, make a cylinder for the "
    f"bore (cut it all the way through), and cut it out. Then save_component."
)

PEG_TASK = (
    f"Build a cylindrical PEG that will slip-fit into a mating bore. Shared contract: "
    f"the bore diameter is {BORE_DIA} mm and the required diametral clearance is "
    f"{CLEARANCE} mm. Size the peg diameter so it fits with that clearance (compute it). "
    f"The peg length is {PEG_LEN} mm. Build the cylinder, then save_component."
)

SINGLE_TASK = (
    "Build TWO components for a peg-and-plate fit, each in its own file when prompted.\n"
    f"1) A PLATE {PLATE[0]}x{PLATE[1]}x{PLATE[2]} mm with a through-hole of diameter "
    f"{BORE_DIA} mm centered at ({BORE_CENTER[0]},{BORE_CENTER[1]}).\n"
    f"2) A PEG: a cylinder that slip-fits the bore with {CLEARANCE} mm diametral "
    f"clearance (compute its diameter), length {PEG_LEN} mm.\n"
    "You will be asked to build them one at a time; build the current one and "
    "save_component."
)


def gate_peg_in_hole(tmp, plate_file, peg_file):
    """Deterministic oracle: merge the two built files and check the peg fits the
    bore. Returns {ok, interference, reason}."""
    with Worker() as w:
        w.call("new_document", name="m2_check")
        asm = w.call("make_assembly", name="A")
        w.call("save_document", path=str(tmp / "m2_check.FCStd"))
        w.call("add_part", assembly=asm["handle"],
               source={"path": str(plate_file)}, placement=[0, 0, 0], name="plate")
        w.call("add_part", assembly=asm["handle"],
               source={"path": str(peg_file)},
               placement=[BORE_CENTER[0], BORE_CENTER[1], -5], name="peg")
        clash = w.call("interference_check", assembly=asm["handle"])
    worst = max((c["interference_mm3"] for c in clash), default=0.0)
    ok = worst < 1.0
    reason = "peg fits the bore" if ok else f"interference {worst:.0f}mm3 (peg too fat / off)"
    return {"ok": ok, "interference": clash, "reason": reason}


# --- conditions --------------------------------------------------------------

def run_partition(client, tmp):
    """Two independent agents, one per component (the multi-agent condition)."""
    plate_f = tmp / "part_plate.FCStd"
    peg_f = tmp / "part_peg.FCStd"
    a = run_agent(client, SYSTEM, PLATE_TASK, plate_f)
    b = run_agent(client, SYSTEM, PEG_TASK, peg_f)
    built = a["ok_built"] and b["ok_built"] and plate_f.exists() and peg_f.exists()
    gate = gate_peg_in_hole(tmp, plate_f, peg_f) if built else {
        "ok": False, "reason": "an agent did not save its component"}
    return {
        "condition": "partition", "built": built, "passed": gate["ok"],
        "reason": gate["reason"],
        "in_tokens": a["in_tokens"] + b["in_tokens"],
        "out_tokens": a["out_tokens"] + b["out_tokens"],
        "cache_read": a["cache_read"] + b["cache_read"],
        "cache_write": a["cache_write"] + b["cache_write"],
        "agents": {"plate": a, "peg": b},
    }


def run_single(client, tmp):
    """One agent builds both components in a single session (the baseline)."""
    import anthropic  # noqa: F401
    plate_f = tmp / "single_plate.FCStd"
    peg_f = tmp / "single_peg.FCStd"
    in_tok = out_tok = cache_read = cache_write = 0
    saved = {"plate": False, "peg": False}
    # Build sequentially in ONE conversation, each in its own Worker/file.
    for comp, fpath, prompt in (
        ("plate", plate_f, "Now build the PLATE component and save_component."),
        ("peg", peg_f, "Now build the PEG component and save_component."),
    ):
        r = run_agent(client, SYSTEM, SINGLE_TASK + "\n\n" + prompt, fpath)
        in_tok += r["in_tokens"]
        out_tok += r["out_tokens"]
        cache_read += r["cache_read"]
        cache_write += r["cache_write"]
        saved[comp] = r["ok_built"]
    built = all(saved.values()) and plate_f.exists() and peg_f.exists()
    gate = gate_peg_in_hole(tmp, plate_f, peg_f) if built else {
        "ok": False, "reason": "baseline did not save a component"}
    return {
        "condition": "single", "built": built, "passed": gate["ok"],
        "reason": gate["reason"], "in_tokens": in_tok, "out_tokens": out_tok,
        "cache_read": cache_read, "cache_write": cache_write,
    }


def main():
    if not os.environ.get("RUN_RELIABILITY"):
        print("Layer M2 is gated behind RUN_RELIABILITY=1 (it calls the Anthropic "
              "API and costs credits). Set RUN_RELIABILITY=1 to run.", file=sys.stderr)
        sys.exit(0)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Run `anthropic-key` first.",
              file=sys.stderr)
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic()
    CACHE_DIR.mkdir(exist_ok=True)

    import tempfile
    results = []
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print(f"== Layer M2 — peg-in-hole, model={MODEL} ==")
        for cond_name, fn in (("partition", run_partition), ("single", run_single)):
            ts = time.time()
            try:
                r = fn(client, tmp)
            except Exception:
                r = {"condition": cond_name, "built": False, "passed": False,
                     "reason": "harness error", "error": traceback.format_exc()}
            r["seconds"] = round(time.time() - ts, 1)
            results.append(r)
            tok = r.get("in_tokens", 0) + r.get("out_tokens", 0)
            cr = r.get("cache_read", 0)
            verdict = "PASS" if r.get("passed") else ("BUILT-but-FAILED-gate"
                                                       if r.get("built") else "DID-NOT-BUILD")
            print(f"  {cond_name:9s}  {verdict:22s}  {r['reason']:40s}  "
                  f"{tok} tok ({cr} cached)  {r['seconds']}s")
            if r.get("error"):
                print(r["error"])

    report = {"model": MODEL, "results": results,
              "total_seconds": round(time.time() - t0, 1)}
    (CACHE_DIR / "report_m2.json").write_text(json.dumps(report, indent=2))
    print(f"\n  report -> {CACHE_DIR / 'report_m2.json'}")
    print("  NOTE: first cut — 1 toy, 1 trial/condition, no response cache yet. "
          "Widen TRIALS/TOYS once validated.")


if __name__ == "__main__":
    main()

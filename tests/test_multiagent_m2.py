"""
Layer M2 — agents in the loop (gated, needs ANTHROPIC_API_KEY).

See tests/MULTI_AGENT_EVAL.md. Layer M1 proved the merge/gate oracle discriminates
correct from broken. M2 asks the real question: can an LLM, given only a contract
slice, drive DriftPin's tools to build a component that PASSES the gates when merged
with another agent's work — and is partitioning better than one agent doing it all?

Each agent is a fresh Anthropic API call (NOT a Claude Code subagent): a cold model
with only its contract slice, a small DriftPin tool surface, and no shared state.
That isolation is the point — a controlled measurement, reproducible, billed to a
metered key, not a hand-driven demo.

Scope: the agent-buildable toys #1–3 (each agent builds a leaf component; the gate
merges and judges deterministically). Toys #4–6 are coordinator mechanics —
subassembly nesting, mate-by-frame, lockfiles — which M1 already covers as
deterministic checks; there is no "did the LLM build it" question there.

Usage:
    # free: validate every toy's gate against scripted reference builders, no API
    M2_SELFTEST=1 .venv/bin/python3 tests/test_multiagent_m2.py

    # billed: run the agents. Load the key first (anthropic-key ~/.bashrc helper).
    anthropic-key
    RUN_RELIABILITY=1 [M2_MODEL=haiku|sonnet] [M2_TRIALS=5] [M2_TOYS=peg,flange] \\
        .venv/bin/python3 tests/test_multiagent_m2.py

M2_MODEL defaults to haiku (cheap screening tier); M2_TRIALS to 1; M2_TOYS to all.
"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

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
MAX_TURNS = 12
CACHE_DIR = REPO / "tests" / "multiagent_cache"

SYSTEM = (
    "You are a mechanical design agent driving a CAD kernel through tools. Build "
    "exactly the component described, in millimetres. Reason about the geometry, "
    "call the tools, optionally verify with mass_properties, then call "
    "save_component LAST. Do not ask questions — build it."
)


def cost_of(result, model):
    """USD for one result dict's token counts under a model's pricing."""
    pi, pcw, pcr, po = PRICING[model]
    return (result.get("in_tokens", 0) * pi
            + result.get("cache_write", 0) * pcw
            + result.get("cache_read", 0) * pcr
            + result.get("out_tokens", 0) * po) / 1e6


# --- the DriftPin tool surface exposed to the agent --------------------------

TOOLS = [
    {
        "name": "new_document",
        "description": "Create a new FreeCAD document and make it active. Call once before building.",
        "input_schema": {"type": "object",
                         "properties": {"name": {"type": "string"}},
                         "required": ["name"]},
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
        "input_schema": {"type": "object",
                         "properties": {"handle": {"type": "string"}},
                         "required": ["handle"]},
    },
    {
        "name": "save_component",
        "description": ("Save the finished component to its file. Call this LAST, once the "
                        "geometry is complete. Takes no path — the harness supplies it."),
        "input_schema": {"type": "object", "properties": {}},
    },
]


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
        last["content"] = [{"type": "text", "text": last["content"],
                            "cache_control": _EPHEMERAL}]
    elif isinstance(last["content"], list) and last["content"]:
        tail = last["content"][-1]
        if isinstance(tail, dict):
            tail["cache_control"] = _EPHEMERAL


def run_agent(client, model, system, task, save_path):
    """Drive one component-builder agent through a tool-use loop in its own Worker."""
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
                tools=tools, messages=messages,
            )
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
            "out_tokens": out_tok, "cache_read": cache_read,
            "cache_write": cache_write}


# --- shared gate helpers -----------------------------------------------------

def _merge_check(tmp, placed, envelopes=None):
    """Link (path, placement, name) parts into an assembly and run gates.
    Returns {ok, reason, interference, envelope}."""
    with Worker() as w:
        w.call("new_document", name="m2_check")
        asm = w.call("make_assembly", name="A")
        w.call("save_document", path=str(tmp / "m2_check.FCStd"))
        for path, placement, name in placed:
            w.call("add_part", assembly=asm["handle"],
                   source={"path": str(path)}, placement=placement, name=name)
        clash = w.call("interference_check", assembly=asm["handle"])
        env = (w.call("envelope_check", assembly=asm["handle"], envelopes=envelopes)
               if envelopes else [])
    worst = max((c["interference_mm3"] for c in clash), default=0.0)
    if worst >= 1.0:
        return {"ok": False, "reason": f"interference {worst:.0f}mm3",
                "interference": clash, "envelope": env}
    if env:
        return {"ok": False, "reason": f"{len(env)} envelope violation(s)",
                "interference": clash, "envelope": env}
    return {"ok": True, "reason": "all gates clean",
            "interference": clash, "envelope": env}


def _ref_box(w, path, name, sx, sy, sz):
    w.call("new_document", name=name)
    w.call("add_primitive", kind="box", w=sx, d=sy, h=sz, name=name)
    w.call("save_document", path=str(path))


def _ref_cyl(w, path, name, r, h):
    w.call("new_document", name=name)
    w.call("add_primitive", kind="cylinder", r=r, h=h, name=name)
    w.call("save_document", path=str(path))


def _ref_box_holes(w, path, name, sx, sy, sz, hole_r, centers):
    w.call("new_document", name=name)
    cur = w.call("add_primitive", kind="box", w=sx, d=sy, h=sz, name=name)
    for cx, cy in centers:
        tool = w.call("add_primitive", kind="cylinder", r=hole_r, h=sz * 3,
                      placement=[cx, cy, -sz], name="hole")
        cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=tool["handle"])
    w.call("save_document", path=str(path))


# =============================================================================
# Toy registry. Each toy:
#   components : {name: build_task}        — one agent per component (partition)
#   single_task: overview for the baseline (one agent builds all, prompted per comp)
#   gate(tmp, files)  -> {ok, reason}      — deterministic merge + check
#   reference(w, name, path)               — scripted correct build (for selftest)
# =============================================================================

class Toy:
    def __init__(self, key, title, components, single_task, gate, reference):
        self.key = key
        self.title = title
        self.components = components      # dict name -> task text
        self.single_task = single_task
        self.gate = gate                 # (tmp, {name: path}) -> {ok, reason}
        self.reference = reference        # (w, name, path) -> builds file


# --- toy 1: peg-in-hole ------------------------------------------------------
T1_BORE_D, T1_CLEAR, T1_PLATE, T1_CTR, T1_PEGLEN = 16.0, 0.4, (60, 60, 10), (30, 30), 20.0

TOY1 = Toy(
    "peg", "Peg-in-hole (clearance fit)",
    components={
        "plate": (f"Build a rectangular PLATE {T1_PLATE[0]}x{T1_PLATE[1]}x{T1_PLATE[2]} mm "
                  f"with a single vertical THROUGH-HOLE of diameter {T1_BORE_D} mm centered "
                  f"at x={T1_CTR[0]}, y={T1_CTR[1]} (make the box, make a cylinder for the "
                  f"bore, cut it through). Then save_component."),
        "peg": (f"Build a cylindrical PEG that slip-fits a mating bore. Contract: bore "
                f"diameter {T1_BORE_D} mm, required diametral clearance {T1_CLEAR} mm — size "
                f"the peg diameter accordingly (compute it). Length {T1_PEGLEN} mm. Build the "
                f"cylinder, then save_component."),
    },
    single_task=(f"You will build two components for a peg-and-plate fit, one at a time.\n"
                 f"PLATE: {T1_PLATE[0]}x{T1_PLATE[1]}x{T1_PLATE[2]} mm, through-hole Ø{T1_BORE_D} "
                 f"mm at ({T1_CTR[0]},{T1_CTR[1]}).\nPEG: cylinder slip-fitting the bore with "
                 f"{T1_CLEAR} mm diametral clearance (compute Ø), length {T1_PEGLEN} mm."),
    gate=lambda tmp, f: _merge_check(tmp, [
        (f["plate"], [0, 0, 0], "plate"),
        (f["peg"], [T1_CTR[0], T1_CTR[1], -5], "peg")]),
    reference=lambda w, name, path: (
        _ref_box_holes(w, path, "plate", *T1_PLATE, T1_BORE_D / 2, [T1_CTR])
        if name == "plate" else
        _ref_cyl(w, path, "peg", (T1_BORE_D - T1_CLEAR) / 2, T1_PEGLEN)),
)


# --- toy 2: bolted flange ----------------------------------------------------
import math  # noqa: E402

T2_SIZE, T2_T, T2_N, T2_R, T2_HOLE_R = 60.0, 8.0, 4, 20.0, 2.6


def _t2_circle(ang0=0.0, n=T2_N, R=T2_R):
    c = T2_SIZE / 2.0
    return [(c + R * math.cos(math.radians(ang0 + i * 360.0 / n)),
             c + R * math.sin(math.radians(ang0 + i * 360.0 / n))) for i in range(n)]


def _t2_gate(tmp, files):
    # link both plates stacked, then nominal bolts spanning both; bolts must clear.
    bolt = tmp / "t2_bolt_ref.FCStd"
    with Worker() as w:
        _ref_cyl(w, bolt, "bolt", 2.4, 2 * T2_T)
    placed = [(files["plateA"], [0, 0, 0], "plateA"),
              (files["plateB"], [0, 0, T2_T], "plateB")]
    for i, (bx, by) in enumerate(_t2_circle()):
        placed.append((bolt, [bx, by, 0], f"bolt{i}"))
    return _merge_check(tmp, placed)


_T2_DESC = (f"a {T2_SIZE}x{T2_SIZE}x{T2_T} mm square plate with a bolt circle of "
            f"{T2_N} through-holes of diameter {2*T2_HOLE_R} mm, evenly spaced on a "
            f"circle of radius {T2_R} mm centered on the plate "
            f"({T2_SIZE/2},{T2_SIZE/2}), first hole at angle 0°")

TOY2 = Toy(
    "flange", "Bolted flange (shared bolt circle)",
    components={
        "plateA": f"Build {_T2_DESC}. Cut each hole through. Then save_component.",
        "plateB": (f"Build a MATING plate to the SAME shared bolt pattern: {_T2_DESC}. "
                   f"It must match so bolts pass through both. Then save_component."),
    },
    single_task=(f"You will build two mating flange plates, one at a time. Both share one "
                 f"bolt pattern: {_T2_DESC}. They must match so bolts pass through both."),
    gate=_t2_gate,
    reference=lambda w, name, path: _ref_box_holes(
        w, path, name, T2_SIZE, T2_SIZE, T2_T, T2_HOLE_R, _t2_circle()),
)


# --- toy 3: bracket on housing (envelope) ------------------------------------
T3_HOUSE = (80, 80, 40)
T3_BRACKET_MAX = (30, 30, 12)              # contract: stay within this
T3_SEAT = [25, 25, 40]                     # where the gate seats the bracket
T3_ENV = {"min": [25, 25, 40], "max": [55, 55, 52]}


def _t3_gate(tmp, files):
    return _merge_check(
        tmp,
        [(files["housing"], [0, 0, 0], "housing"),
         (files["bracket"], T3_SEAT, "bracket")],
        envelopes={"bracket": T3_ENV})

TOY3 = Toy(
    "bracket", "Bracket on housing (keep-out envelope)",
    components={
        "housing": (f"Build a HOUSING: a solid box {T3_HOUSE[0]}x{T3_HOUSE[1]}x{T3_HOUSE[2]} "
                    f"mm. Then save_component."),
        "bracket": (f"Build a BRACKET that mounts on the housing's top face and must stay "
                    f"within a keep-out envelope: it may be NO larger than "
                    f"{T3_BRACKET_MAX[0]}x{T3_BRACKET_MAX[1]}x{T3_BRACKET_MAX[2]} mm. Build a "
                    f"box at or under that size. Then save_component."),
    },
    single_task=(f"You will build a housing and a bracket, one at a time. HOUSING: box "
                 f"{T3_HOUSE[0]}x{T3_HOUSE[1]}x{T3_HOUSE[2]} mm. BRACKET: a box no larger "
                 f"than {T3_BRACKET_MAX[0]}x{T3_BRACKET_MAX[1]}x{T3_BRACKET_MAX[2]} mm "
                 f"(mounts on the housing top, within a keep-out envelope)."),
    gate=_t3_gate,
    reference=lambda w, name, path: (
        _ref_box(w, path, "housing", *T3_HOUSE) if name == "housing"
        else _ref_box(w, path, "bracket", 30, 30, 10)),
)


TOYS = {t.key: t for t in (TOY1, TOY2, TOY3)}


# --- conditions --------------------------------------------------------------

def _agg(*results):
    out = {k: 0 for k in ("in_tokens", "out_tokens", "cache_read", "cache_write")}
    for r in results:
        for k in out:
            out[k] += r.get(k, 0)
    return out


def run_partition(client, model, toy, tmp):
    """One independent agent per component."""
    files, agents = {}, {}
    for name in toy.components:
        files[name] = tmp / f"part_{toy.key}_{name}.FCStd"
        agents[name] = run_agent(client, model, SYSTEM, toy.components[name], files[name])
    built = all(a["ok_built"] for a in agents.values()) and all(p.exists() for p in files.values())
    gate = toy.gate(tmp, files) if built else {"ok": False, "reason": "an agent did not save"}
    return {"condition": "partition", "built": built, "passed": gate["ok"],
            "reason": gate["reason"], "agents": agents, **_agg(*agents.values())}


def run_single(client, model, toy, tmp):
    """One agent builds every component in a single conversation, file per component."""
    files, agents = {}, {}
    for name in toy.components:
        files[name] = tmp / f"single_{toy.key}_{name}.FCStd"
        task = (toy.single_task + f"\n\nNow build the {name.upper()} component and "
                f"save_component.")
        agents[name] = run_agent(client, model, SYSTEM, task, files[name])
    built = all(a["ok_built"] for a in agents.values()) and all(p.exists() for p in files.values())
    gate = toy.gate(tmp, files) if built else {"ok": False, "reason": "baseline did not save"}
    return {"condition": "single", "built": built, "passed": gate["ok"],
            "reason": gate["reason"], **_agg(*agents.values())}


# --- selftest (no API): run each toy's gate against scripted reference builds --

def selftest(toys):
    """Prove every toy's gate passes a correct (scripted) build — the M1 discipline
    applied to M2's gates, so the widened plumbing is validated before any spend."""
    print("== M2 selftest — gates vs. scripted reference builds (no API) ==")
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for toy in toys:
            files = {}
            for name in toy.components:
                files[name] = tmp / f"ref_{toy.key}_{name}.FCStd"
                with Worker() as w:
                    toy.reference(w, name, files[name])
            gate = toy.gate(tmp, files)
            ok = gate["ok"]
            failures += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  {toy.title:42s}  {gate['reason']}")
    if failures:
        print(f"\n== {failures} toy gate(s) FAILED on a correct build — fix before agents ==")
        sys.exit(1)
    print("\n== all toy gates pass a correct build; ready for agent runs ==")


# --- main --------------------------------------------------------------------

def _selected_toys():
    sel = os.environ.get("M2_TOYS")
    if not sel:
        return list(TOYS.values())
    keys = [k.strip() for k in sel.split(",")]
    return [TOYS[k] for k in keys]


def main():
    toys = _selected_toys()

    if os.environ.get("M2_SELFTEST"):
        selftest(toys)
        return

    if not os.environ.get("RUN_RELIABILITY"):
        print("Layer M2 is gated behind RUN_RELIABILITY=1 (it calls the Anthropic API "
              "and costs credits). Set RUN_RELIABILITY=1 to run, or M2_SELFTEST=1 to "
              "validate the gates for free.", file=sys.stderr)
        sys.exit(0)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Run `anthropic-key` first.", file=sys.stderr)
        sys.exit(1)

    model = MODELS.get(os.environ.get("M2_MODEL", "haiku"))
    if model is None:
        print(f"ERROR: M2_MODEL must be one of {sorted(MODELS)}", file=sys.stderr)
        sys.exit(1)
    trials = int(os.environ.get("M2_TRIALS", "1"))

    import anthropic
    client = anthropic.Anthropic()
    CACHE_DIR.mkdir(exist_ok=True)

    print(f"== Layer M2 — model={model}  trials={trials}  toys={[t.key for t in toys]} ==")
    rows = []
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for toy in toys:
            for cond_name, fn in (("partition", run_partition), ("single", run_single)):
                trial_results = []
                for i in range(trials):
                    try:
                        r = fn(client, model, toy, tmp)
                    except Exception:
                        r = {"condition": cond_name, "built": False, "passed": False,
                             "reason": "harness error", "error": traceback.format_exc()}
                    r["cost_usd"] = round(cost_of(r, model), 4)
                    trial_results.append(r)
                n = len(trial_results)
                passed = sum(1 for r in trial_results if r.get("passed"))
                built = sum(1 for r in trial_results if r.get("built"))
                mean_cost = sum(r["cost_usd"] for r in trial_results) / n
                rows.append({"toy": toy.key, "condition": cond_name, "trials": n,
                             "passed": passed, "built": built,
                             "pass_rate": passed / n, "mean_cost_usd": round(mean_cost, 4),
                             "results": trial_results})
                print(f"  {toy.key:8s} {cond_name:9s}  pass {passed}/{n}  built {built}/{n}  "
                      f"${mean_cost:.4f}/trial")
                err = next((r.get("error") for r in trial_results if r.get("error")), None)
                if err:
                    print(err)

    total_cost = sum(row["mean_cost_usd"] * row["trials"] for row in rows)
    report = {"model": model, "trials": trials, "rows": rows,
              "total_cost_usd": round(total_cost, 4),
              "total_seconds": round(time.time() - t0, 1)}
    (CACHE_DIR / "report_m2.json").write_text(json.dumps(report, indent=2))
    print(f"\n  total ~${total_cost:.2f}   report -> {CACHE_DIR / 'report_m2.json'}")


if __name__ == "__main__":
    main()

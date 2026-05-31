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
    # free: exercise the full agent loop with a stubbed model, no API
    M2_DRYRUN=1 .venv/bin/python3 tests/test_multiagent_m2.py

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
import types
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


def _ref_pinned_plate(w, path, name, sx, sy, sz, pin_r, pin_h, centers):
    """A plate with cylindrical pins standing UP from its top face, fused into one
    solid (the harder build: place + fuse N pins at exact positions)."""
    w.call("new_document", name=name)
    cur = w.call("add_primitive", kind="box", w=sx, d=sy, h=sz, name=name)
    for cx, cy in centers:
        pin = w.call("add_primitive", kind="cylinder", r=pin_r, h=pin_h,
                     placement=[cx, cy, sz], name="pin")
        cur = w.call("boolean_op", op="fuse", base=cur["handle"], tool=pin["handle"])
    w.call("save_document", path=str(path))


# =============================================================================
# Toy registry. Each toy:
#   components : {name: build_task}        — one agent per component (partition)
#   single_task: overview for the baseline (one agent builds all, prompted per comp)
#   gate(tmp, files)  -> {ok, reason}      — deterministic merge + check
#   reference(w, name, path)               — scripted correct build (for selftest)
#   negatives : [Neg, ...]                 — deliberately-wrong variants the gate
#                                            MUST catch (proves the gate measures
#                                            the build, isn't always-passing)
# =============================================================================

class Neg:
    """A negative control: a wrong build the gate must reject.

    expect       : which gate should fire ("interference" | "envelope")
    agent        : {component: wrong_task}  — overrides for the billed agent run;
                   unlisted components use the toy's normal (correct) task.
    ref          : {component: fn(w, path)} — scripted wrong build for the FREE
                   selftest; unlisted components use the toy's reference.
    """
    def __init__(self, name, expect, agent, ref):
        self.name = name
        self.expect = expect
        self.agent = agent
        self.ref = ref


class Toy:
    def __init__(self, key, title, components, single_task, gate, reference,
                 negatives=()):
        self.key = key
        self.title = title
        self.components = components      # dict name -> task text
        self.single_task = single_task
        self.gate = gate                 # (tmp, {name: path}) -> {ok, reason}
        self.reference = reference        # (w, name, path) -> builds file
        self.negatives = list(negatives)


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
    negatives=[
        # Peg deliberately too fat (Ø20 into Ø16 bore) -> must interfere.
        Neg("peg_too_fat", "interference",
            agent={"peg": ("Build a solid cylinder EXACTLY 20 mm in diameter "
                           "(radius 10 mm), 20 mm long. Use these exact dimensions; "
                           "do not adjust. Then save_component.")},
            ref={"peg": lambda w, p: _ref_cyl(w, p, "peg", 10.0, T1_PEGLEN)}),
    ],
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
    negatives=[
        # plateB on the wrong bolt circle (R28, not R20): bolts placed at the
        # nominal R20 hit plateB's solid material -> interference.
        Neg("wrong_circle", "interference",
            agent={"plateB": (f"Build a {T2_SIZE}x{T2_SIZE}x{T2_T} mm square plate with "
                              f"{T2_N} through-holes of diameter {2*T2_HOLE_R} mm on a bolt "
                              f"circle of radius 28 mm (use 28, not 20) centered on the "
                              f"plate ({T2_SIZE/2},{T2_SIZE/2}). Then save_component.")},
            ref={"plateB": lambda w, p: _ref_box_holes(
                w, p, "plateB", T2_SIZE, T2_SIZE, T2_T, T2_HOLE_R, _t2_circle(R=28.0))}),
    ],
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
    negatives=[
        # Bracket 50 mm tall: seated at z=40 it tops out at z=90, far past the
        # envelope's z-max of 52 -> envelope keep-out violation.
        Neg("bracket_too_tall", "envelope",
            agent={"bracket": ("Build a box EXACTLY 30 x 30 x 50 mm. Use these exact "
                               "dimensions; do not shrink it. Then save_component.")},
            ref={"bracket": lambda w, p: _ref_box(w, p, "bracket", 30, 30, 50)}),
    ],
)


# --- toy 4: two-pin link (HARDER) --------------------------------------------
# Two pins must seat into two holes SIMULTANEOUSLY. Unlike a single peg (which
# tolerates small error because one round pin in a round hole has rotational
# slack), two-point alignment pins down both spacing AND orientation — every pin
# must clear. Stresses the shared contract three ways the easy toys don't:
#   (a) two-point constraint (no rotational slack to hide a spacing error),
#   (b) a DERIVED shared value — pins/holes are symmetric about center, so both
#       agents must compute the same x = center ± spacing/2 independently,
#       which is exactly where partition (slice-only view) can diverge from single,
#   (c) more build steps (plate + 2 fused pins; bar + 2 cuts) = more failure surface.
T4_PLATE = (60.0, 40.0, 10.0)       # base plate w,d,h
T4_PIN_R, T4_PIN_H = 4.0, 12.0      # pins Ø8, 12 tall
T4_SPACING = 30.0                   # pin spacing along x, symmetric about center
T4_BAR_T = 6.0                      # link bar thickness
T4_HOLE_R = 4.2                     # holes Ø8.4 -> 0.4 mm diametral clearance
_T4_CX = T4_PLATE[0] / 2.0          # 30
_T4_CY = T4_PLATE[1] / 2.0          # 20


def _t4_centers(spacing):
    return [(_T4_CX - spacing / 2.0, _T4_CY), (_T4_CX + spacing / 2.0, _T4_CY)]


def _t4_gate(tmp, files):
    # base at origin (pins point up from z=10); link laid on top at z=10. Each pin
    # must pass through its hole — a spacing/position mismatch clips bar material.
    return _merge_check(tmp, [
        (files["base"], [0, 0, 0], "base"),
        (files["link"], [0, 0, T4_PLATE[2]], "link")])


_T4_GEOM = (f"on a {T4_PLATE[0]}x{T4_PLATE[1]} mm plate, positioned symmetrically "
            f"about the plate center, {T4_SPACING} mm apart along the long (X) axis, "
            f"both on the Y centerline")

TOY4 = Toy(
    "twopin", "Two-pin link (dual-point alignment, derived spacing)",
    components={
        "base": (f"Build a BASE: a {T4_PLATE[0]}x{T4_PLATE[1]}x{T4_PLATE[2]} mm plate "
                 f"with TWO cylindrical pins Ø{2*T4_PIN_R} mm, {T4_PIN_H} mm tall, "
                 f"standing up from the top face, {_T4_GEOM}. Fuse the pins to the "
                 f"plate so it is one solid. Then save_component."),
        "link": (f"Build a LINK bar: {T4_PLATE[0]}x{T4_PLATE[1]}x{T4_BAR_T} mm, with "
                 f"TWO vertical through-holes Ø{2*T4_HOLE_R} mm, {_T4_GEOM}. The holes "
                 f"must match a mating part's two pins. Then save_component."),
    },
    single_task=(f"You will build two mating parts, one at a time. They share a "
                 f"two-pin pattern: two locations {_T4_GEOM}.\n"
                 f"BASE: {T4_PLATE[0]}x{T4_PLATE[1]}x{T4_PLATE[2]} mm plate with two "
                 f"Ø{2*T4_PIN_R} mm pins {T4_PIN_H} mm tall fused on top at those "
                 f"locations.\nLINK: {T4_PLATE[0]}x{T4_PLATE[1]}x{T4_BAR_T} mm bar with "
                 f"two Ø{2*T4_HOLE_R} mm through-holes at the SAME two locations."),
    gate=_t4_gate,
    reference=lambda w, name, path: (
        _ref_pinned_plate(w, path, "base", *T4_PLATE, T4_PIN_R, T4_PIN_H,
                          _t4_centers(T4_SPACING)) if name == "base"
        else _ref_box_holes(w, path, "link", T4_PLATE[0], T4_PLATE[1], T4_BAR_T,
                            T4_HOLE_R, _t4_centers(T4_SPACING))),
    negatives=[
        # Link holes at the WRONG spacing (20 mm not 30): seat the bar over the
        # pins and the pins clip bar material -> interference.
        Neg("wrong_spacing", "interference",
            agent={"link": (f"Build a LINK bar {T4_PLATE[0]}x{T4_PLATE[1]}x{T4_BAR_T} mm "
                            f"with two vertical through-holes Ø{2*T4_HOLE_R} mm, "
                            f"symmetric about the plate center but only 20 mm apart "
                            f"along X (use 20, not any other value). Then save_component.")},
            ref={"link": lambda w, p: _ref_box_holes(
                w, p, "link", T4_PLATE[0], T4_PLATE[1], T4_BAR_T, T4_HOLE_R,
                _t4_centers(20.0))}),
    ],
)


TOYS = {t.key: t for t in (TOY1, TOY2, TOY3, TOY4)}


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


def run_negative(client, model, toy, neg, tmp):
    """Partition build where the named components get neg.agent's WRONG task; the
    rest stay correct. A good run: every agent builds (faithfully wrong) AND the
    gate REJECTS it. caught=True means the gate did its job on real agent output."""
    files, agents = {}, {}
    for name in toy.components:
        files[name] = tmp / f"neg_{toy.key}_{neg.name}_{name}.FCStd"
        task = neg.agent.get(name, toy.components[name])
        agents[name] = run_agent(client, model, SYSTEM, task, files[name])
    built = all(a["ok_built"] for a in agents.values()) and all(p.exists() for p in files.values())
    gate = toy.gate(tmp, files) if built else {"ok": True, "reason": "an agent did not save"}
    caught = built and not gate["ok"]   # built the wrong thing AND gate flagged it
    return {"condition": f"neg/{neg.name}", "built": built, "caught": caught,
            "expect": neg.expect, "reason": gate["reason"], **_agg(*agents.values())}


# --- selftest (no API): scripted builds prove the gate DISCRIMINATES -----------

def _build_scripted(tmp, toy, prefix, ref_overrides=None):
    """Build every component with scripted builders (ref_overrides[name] wins,
    else toy.reference). Returns {name: path}."""
    ref_overrides = ref_overrides or {}
    files = {}
    for name in toy.components:
        files[name] = tmp / f"{prefix}_{toy.key}_{name}.FCStd"
        with Worker() as w:
            if name in ref_overrides:
                ref_overrides[name](w, files[name])
            else:
                toy.reference(w, name, files[name])
    return files


def selftest(toys):
    """Two-sided gate validation, no API: every toy's gate must PASS a correct
    scripted build AND FAIL each scripted negative control. A gate that can't do
    both isn't measuring anything — this is the prerequisite for trusting agent
    pass-rates (the M1 discipline, applied to M2's gates)."""
    print("== M2 selftest — gate discrimination on scripted builds (no API) ==")
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for toy in toys:
            # positive: correct build must pass
            files = _build_scripted(tmp, toy, "pos")
            g = toy.gate(tmp, files)
            ok = g["ok"]
            failures += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  {toy.title:42s} correct build -> "
                  f"{'clean' if ok else g['reason']}")
            # negatives: each wrong build must be caught
            for neg in toy.negatives:
                nf = _build_scripted(tmp, toy, f"neg_{neg.name}", neg.ref)
                ng = toy.gate(tmp, nf)
                caught = not ng["ok"]
                failures += 0 if caught else 1
                print(f"  {'PASS' if caught else 'FAIL'}  {toy.title:42s} "
                      f"neg/{neg.name} -> "
                      f"{'caught: ' + ng['reason'] if caught else 'SLIPPED THROUGH'}")
    if failures:
        print(f"\n== {failures} gate check(s) FAILED — fix before agents ==")
        sys.exit(1)
    print("\n== gates pass correct builds AND catch every negative; "
          "ready for agent runs ==")


# --- dry run (no API): stub the LLM, exercise the full agent loop ------------
#
# Proves the run_agent loop + run_partition/run_single + gate + aggregation wiring
# with canned tool-use turns instead of API calls. The FreeCAD worker is real —
# only the model is stubbed — so it builds a genuine correct peg+plate and the gate
# must PASS. Catches loop/dispatch/save-detection regressions for free.

class _DryUsage:
    input_tokens = 5
    output_tokens = 20
    cache_read_input_tokens = 100
    cache_creation_input_tokens = 50


class _DryBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _du(tid, name, inp):
    return _DryBlock(type="tool_use", id=tid, name=name, input=inp)


class _DryMessages:
    """Scripted responses for the peg toy, keyed by turn count. Routes by the
    explicit single-agent suffix FIRST (single_task names both parts, so keyword
    sniffing alone is ambiguous)."""
    def create(self, model, max_tokens, system, tools, messages):
        first = messages[0]["content"]
        t = (first if isinstance(first, str) else str(first)).lower()
        turns = sum(1 for m in messages if m["role"] == "assistant")
        steps = self._script(t)
        content = steps[turns] if turns < len(steps) else [_DryBlock(type="text", text="done")]
        return types.SimpleNamespace(content=content, usage=_DryUsage())

    def _script(self, t):
        if "build the plate" in t:
            return self._plate()
        if "build the peg" in t:
            return self._peg()
        if "peg" in t and "plate" not in t:
            return self._peg()
        if "plate" in t and "through-hole" in t and "mating" not in t:
            return self._plate()
        return [[_du("a", "new_document", {"name": "x"})], [_du("z", "save_component", {})]]

    def _peg(self):
        return [[_du("a", "new_document", {"name": "peg"})],
                [_du("b", "add_primitive", {"kind": "cylinder", "r": 7.8, "h": 20, "name": "peg"})],
                [_du("c", "save_component", {})]]

    def _plate(self):
        return [[_du("a", "new_document", {"name": "plate"})],
                [_du("b", "add_primitive", {"kind": "box", "w": 60, "d": 60, "h": 10, "name": "plate"})],
                [_du("c", "add_primitive", {"kind": "cylinder", "r": 8, "h": 30,
                                            "placement": [30, 30, -10], "name": "bore"})],
                [_du("d", "boolean_op", {"op": "cut", "base": "box_1", "tool": "cylinder_1"})],
                [_du("e", "save_component", {})]]


class _DryClient:
    def __init__(self):
        self.messages = _DryMessages()


def dryrun():
    client = _DryClient()
    toy = TOYS["peg"]
    model = MODELS["haiku"]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        part = run_partition(client, model, toy, tmp)
        sing = run_single(client, model, toy, tmp)
    print("== M2 dry run — full agent loop with a stubbed model (no API) ==")
    for label, r in (("partition", part), ("single", sing)):
        print(f"  {label:9s} built={r['built']} passed={r['passed']} "
              f"reason={r['reason']!r} cache_read={r['cache_read']} out_tok={r['out_tokens']}")
    ok = all(r["built"] and r["passed"] for r in (part, sing))
    print("\n== WIRING OK ==" if ok else "\n== WIRING BROKEN ==")
    sys.exit(0 if ok else 1)


# --- main --------------------------------------------------------------------

def _selected_toys():
    sel = os.environ.get("M2_TOYS")
    if not sel:
        return list(TOYS.values())
    keys = [k.strip() for k in sel.split(",")]
    return [TOYS[k] for k in keys]


def main():
    toys = _selected_toys()

    if os.environ.get("M2_DRYRUN"):
        dryrun()
        return

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

    negatives_only = bool(os.environ.get("M2_NEGATIVES"))
    print(f"== Layer M2 — model={model}  trials={trials}  toys={[t.key for t in toys]}"
          f"{'  [NEGATIVES]' if negatives_only else ''} ==")
    rows = []
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for toy in toys:
            # Per-trial dir: no cross-trial file reuse, so FreeCAD's path-keyed
            # open-document cache can't serve stale geometry to a later trial's
            # gate. Each trial is fully independent.
            if negatives_only:
                # agent-driven negative controls: build WRONG, gate must catch.
                for neg in toy.negatives:
                    trial_results = []
                    for i in range(trials):
                        tmp = root / f"{toy.key}_{neg.name}_{i}"
                        tmp.mkdir(parents=True, exist_ok=True)
                        try:
                            r = run_negative(client, model, toy, neg, tmp)
                        except Exception:
                            r = {"condition": f"neg/{neg.name}", "built": False,
                                 "caught": False, "reason": "harness error",
                                 "error": traceback.format_exc()}
                        r["cost_usd"] = round(cost_of(r, model), 4)
                        trial_results.append(r)
                    n = len(trial_results)
                    built = sum(1 for r in trial_results if r.get("built"))
                    caught = sum(1 for r in trial_results if r.get("caught"))
                    mean_cost = sum(r["cost_usd"] for r in trial_results) / n
                    rows.append({"toy": toy.key, "condition": f"neg/{neg.name}",
                                 "trials": n, "built": built, "caught": caught,
                                 "caught_rate": caught / n, "expect": neg.expect,
                                 "mean_cost_usd": round(mean_cost, 4),
                                 "results": trial_results})
                    print(f"  {toy.key:8s} neg/{neg.name:14s}  built {built}/{n}  "
                          f"caught {caught}/{n}  (expect {neg.expect})  "
                          f"${mean_cost:.4f}/trial")
                    err = next((r.get("error") for r in trial_results if r.get("error")), None)
                    if err:
                        print(err)
                continue

            for cond_name, fn in (("partition", run_partition), ("single", run_single)):
                trial_results = []
                for i in range(trials):
                    tmp = root / f"{toy.key}_{cond_name}_{i}"
                    tmp.mkdir(parents=True, exist_ok=True)
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

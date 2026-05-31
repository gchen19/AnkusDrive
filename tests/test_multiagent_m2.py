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
        "name": "rotate",
        "description": ("Rotate an existing solid (by handle) about an axis through its "
                        "own centroid by angle_deg degrees. axis is [x,y,z] — e.g. "
                        "[0,1,0] is the Y axis. Use to TILT a feature to a required "
                        "orientation. Returns {rotated, angle_deg}."),
        "input_schema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "axis": {"type": "array", "items": {"type": "number"}},
                "angle_deg": {"type": "number"},
            },
            "required": ["handle", "axis", "angle_deg"],
        },
    },
    {
        "name": "save_component",
        "description": ("Save the finished component to its file. Call this LAST, once the "
                        "geometry is complete. Takes no path — the harness supplies it."),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _apply_rotation(w, objname, axis, angle_deg, center=None):
    """Rotate a doc object in place about `center` (default its centroid) by angle_deg
    about `axis`. Worker add_primitive only translates, so rotation goes through
    run_script — shared by the agent `rotate` tool and the reference builders."""
    ax = list(axis)
    cexpr = (f"App.Vector({center[0]},{center[1]},{center[2]})"
             if center is not None else "o.Shape.CenterOfMass")
    w.call("run_script", code=f"""
import FreeCAD as F
o = App.ActiveDocument.getObject({objname!r})
c = {cexpr}
o.Placement = F.Placement(F.Vector(0,0,0),
                          F.Rotation(F.Vector({ax[0]},{ax[1]},{ax[2]}), {float(angle_deg)}),
                          c).multiply(o.Placement)
App.ActiveDocument.recompute()
__result__ = "ok"
""")


def _dispatch(w, save_path, name, args):
    if name == "save_component":
        return w.call("save_document", path=str(save_path))
    if name == "rotate":
        handles = w.call("list_handles")
        h = args["handle"]
        if h not in handles:
            raise KeyError(f"unknown handle: {h!r}")
        _apply_rotation(w, handles[h]["name"], args.get("axis", [0, 1, 0]),
                        args["angle_deg"], args.get("center"))
        return {"rotated": h, "angle_deg": args["angle_deg"]}
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


# --- measurement helper (for gates that read a saved part's real geometry) ----

def _part_x_length(path):
    """X-extent (mm) of the first top-level shaped object in a saved component."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code="""
objs = [o for o in App.ActiveDocument.Objects
        if hasattr(o, "Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs = [o for o in App.ActiveDocument.Objects
            if hasattr(o, "Shape") and not o.Shape.isNull()]
bb = objs[0].Shape.BoundBox
__result__ = bb.XMax - bb.XMin
""")
    return r["result"]


# =============================================================================
# toy 5: N-slot board (CONTEXT-LOAD sweep — designed, see MULTI_AGENT_EVAL.md)
# A plate with k holes of DISTINCT diameters, and k pegs each sized to one slot.
# The point: in partition each peg agent is told ONE diameter; in single, one
# agent must keep all k distinct (diameter -> slot) pairs straight in a single
# growing conversation. Hypothesis: single's pass-rate decays as k grows while
# partition's holds. Registered at k=4 and k=8 (shared builder) to sweep the axis.
# Gate is direction-complete: interference catches a peg too BIG, a per-peg
# diameter check catches one too SMALL / assigned to the wrong slot.
# =============================================================================

NSLOT_HOLE_D = [10.0, 14.0, 18.0, 22.0, 12.0, 16.0, 20.0, 24.0]  # distinct, shuffled
NSLOT_CLEAR = 0.4
NSLOT_PITCH = 30.0
NSLOT_Y = 15.0
NSLOT_PLATE_D = 30.0
NSLOT_PLATE_H = 10.0
NSLOT_PEG_H = 20.0


def _nslot_centers(k):
    return [(15.0 + i * NSLOT_PITCH, NSLOT_Y) for i in range(k)]


def _nslot_plate_w(k):
    return k * NSLOT_PITCH


def _nslot_gate(k):
    def gate(tmp, files):
        centers = _nslot_centers(k)
        placed = [(files["plate"], [0, 0, 0], "plate")]
        for i in range(k):
            cx, cy = centers[i]
            placed.append((files[f"peg{i}"], [cx, cy, -5], f"peg{i}"))
        res = _merge_check(tmp, placed)            # interference => a peg too big
        if not res["ok"]:
            return res
        for i in range(k):                          # diameter check => too small / wrong slot
            d = _part_x_length(files[f"peg{i}"])
            target = NSLOT_HOLE_D[i] - NSLOT_CLEAR
            if abs(d - target) > 0.6:               # slots differ by >=2mm; 0.6 = clearance slop
                return {"ok": False,
                        "reason": f"peg{i} Ø{d:.1f} != slot target Ø{target:.1f}"}
        return {"ok": True, "reason": f"all {k} pegs fit their slots"}
    return gate


def _nslot_plate_task(k):
    holes = "; ".join(f"slot {i} at x={15.0 + i*NSLOT_PITCH:.0f} y={NSLOT_Y:.0f} "
                      f"diameter {NSLOT_HOLE_D[i]:.0f} mm" for i in range(k))
    return (f"Build a BASEPLATE {_nslot_plate_w(k):.0f} x {NSLOT_PLATE_D:.0f} x "
            f"{NSLOT_PLATE_H:.0f} mm with {k} vertical through-holes, each a "
            f"DIFFERENT diameter: {holes}. Cut each hole through. Then save_component.")


def _nslot_peg_task(i):
    target = NSLOT_HOLE_D[i] - NSLOT_CLEAR
    return (f"Build a cylindrical PEG of diameter {target:.1f} mm "
            f"(it slip-fits a {NSLOT_HOLE_D[i]:.0f} mm hole with {NSLOT_CLEAR} mm "
            f"clearance), {NSLOT_PEG_H:.0f} mm long. Then save_component.")


def _nslot_single_task(k):
    pegs = "; ".join(f"peg {i}: Ø{NSLOT_HOLE_D[i]-NSLOT_CLEAR:.1f} mm" for i in range(k))
    return (f"You will build a baseplate and {k} pegs, one at a time. The plate has "
            f"{k} holes of distinct diameters and each peg fits one specific hole "
            f"with {NSLOT_CLEAR} mm clearance. Peg diameters: {pegs}. "
            f"Keep each peg matched to its hole.")


def _nslot_ref(k):
    centers = _nslot_centers(k)
    def ref(w, name, path):
        if name == "plate":
            radii = [(centers[i][0], centers[i][1]) for i in range(k)]
            # holes of differing radius — build directly (helper assumes one radius)
            w.call("new_document", name="plate")
            cur = w.call("add_primitive", kind="box", w=_nslot_plate_w(k),
                         d=NSLOT_PLATE_D, h=NSLOT_PLATE_H, name="plate")
            for i in range(k):
                cx, cy = centers[i]
                tool = w.call("add_primitive", kind="cylinder",
                              r=NSLOT_HOLE_D[i] / 2.0, h=NSLOT_PLATE_H * 3,
                              placement=[cx, cy, -NSLOT_PLATE_H], name="hole")
                cur = w.call("boolean_op", op="cut", base=cur["handle"],
                             tool=tool["handle"])
            w.call("save_document", path=str(path))
        else:
            i = int(name[3:])  # "pegN"
            _ref_cyl(w, path, name, (NSLOT_HOLE_D[i] - NSLOT_CLEAR) / 2.0, NSLOT_PEG_H)
    return ref


def _make_nslot(k):
    comps = {"plate": _nslot_plate_task(k)}
    comps.update({f"peg{i}": _nslot_peg_task(i) for i in range(k)})
    return Toy(
        f"nslot{k}", f"N-slot board, k={k} (context load: {k} distinct slot↔peg pairs)",
        components=comps,
        single_task=_nslot_single_task(k),
        gate=_nslot_gate(k),
        reference=_nslot_ref(k),
        negatives=[
            # peg0 built to slot-1's diameter (wrong-slot swap) -> diameter check fires.
            Neg("peg0_wrong_slot", "interference",   # expect is informational here
                agent={"peg0": (f"Build a cylindrical peg of diameter "
                                f"{NSLOT_HOLE_D[1]-NSLOT_CLEAR:.1f} mm, "
                                f"{NSLOT_PEG_H:.0f} mm long. Then save_component.")},
                ref={"peg0": lambda w, p: _ref_cyl(
                    w, p, "peg0", (NSLOT_HOLE_D[1] - NSLOT_CLEAR) / 2.0, NSLOT_PEG_H)}),
        ],
    )


TOY5_NSLOT4 = _make_nslot(4)
TOY5_NSLOT8 = _make_nslot(8)


# =============================================================================
# toy 6: tolerance-stack chain (ERROR PROPAGATION — the case partition should LOSE)
# n equal segments butted end to end must total exactly T mm. Each agent must
# DERIVE T/n (non-integer) and any rounding accumulates. In single, one agent
# sees the whole chain and can make the segments sum to T (e.g. the last absorbs
# the remainder); in partition each agent rounds T/n blind to the others, so the
# sum drifts. Gate: measure each segment's actual X-length, sum, require
# |sum - T| <= tol. Pure cumulative-drift test (segments are butted at their
# ACTUAL ends, so there's no interference to conflate it with).
# =============================================================================

TCHAIN_TOTAL = 100.0
TCHAIN_TOL = 0.8
TCHAIN_SEG_D = 20.0
TCHAIN_SEG_H = 10.0


def _tchain_gate(n):
    def gate(tmp, files):
        lengths = [_part_x_length(files[f"seg{i}"]) for i in range(n)]
        total = sum(lengths)
        drift = abs(total - TCHAIN_TOTAL)
        if drift > TCHAIN_TOL:
            return {"ok": False,
                    "reason": f"chain {total:.1f}mm vs {TCHAIN_TOTAL:.0f} "
                              f"(drift {drift:.1f} > {TCHAIN_TOL})"}
        return {"ok": True, "reason": f"chain {total:.1f}mm within {TCHAIN_TOL}mm"}
    return gate


def _tchain_seg_task(n):
    return (f"A chain of {n} EQUAL segments butted end to end must total exactly "
            f"{TCHAIN_TOTAL:.0f} mm. Build ONE segment: a block "
            f"{TCHAIN_TOTAL:.0f}/{n} mm long along X, {TCHAIN_SEG_D:.0f} mm wide, "
            f"{TCHAIN_SEG_H:.0f} mm tall. Compute the length precisely. "
            f"Then save_component.")


def _tchain_single_task(n):
    return (f"You will build {n} segments of a chain, one at a time. Butted end to "
            f"end they must total EXACTLY {TCHAIN_TOTAL:.0f} mm. Each is "
            f"{TCHAIN_SEG_D:.0f} mm wide and {TCHAIN_SEG_H:.0f} mm tall; choose each "
            f"segment's length so the {n} lengths sum to exactly {TCHAIN_TOTAL:.0f} mm "
            f"(they should be equal, but make the total exact).")


def _tchain_ref(n):
    # reference distributes the remainder so the exact sum is T (what single can do)
    base = round(TCHAIN_TOTAL / n, 1)
    lengths = [base] * n
    lengths[-1] = round(TCHAIN_TOTAL - base * (n - 1), 4)
    def ref(w, name, path):
        i = int(name[3:])  # "segN"
        _ref_box(w, path, name, lengths[i], TCHAIN_SEG_D, TCHAIN_SEG_H)
    return ref


def _make_tchain(n):
    comps = {f"seg{i}": _tchain_seg_task(n) for i in range(n)}
    return Toy(
        f"tchain{n}", f"Tolerance chain, n={n} (cumulative drift; partition should lose)",
        components=comps,
        single_task=_tchain_single_task(n),
        gate=_tchain_gate(n),
        reference=_tchain_ref(n),
        negatives=[
            # one segment a full 2mm short -> total drifts past tol.
            Neg("seg0_short", "interference",  # expect informational; gate is length
                agent={"seg0": (f"Build a block {TCHAIN_TOTAL/n - 2.0:.2f} mm long "
                                f"along X, {TCHAIN_SEG_D:.0f} wide, {TCHAIN_SEG_H:.0f} "
                                f"tall. Then save_component.")},
                ref={"seg0": lambda w, p: _ref_box(
                    w, p, "seg0", TCHAIN_TOTAL / n - 2.0, TCHAIN_SEG_D, TCHAIN_SEG_H)}),
        ],
    )


TOY6_TCHAIN3 = _make_tchain(3)
TOY6_TCHAIN6 = _make_tchain(6)


# =============================================================================
# toy 7: UNEQUAL tolerance chain on a manufacturing grid — the REAL partition-LOSES
# case (equal segments dodged it: identical rounding cancels, so tchain6 partition
# passed 20/20). The lever is a coarse grid: with no grid each agent builds an exact
# float and the chain sums to T; force whole-mm stock and local rounding can no longer
# reconcile a GLOBAL total. The nominals are chosen so every segment rounds UP, so the
# errors ACCUMULATE instead of cancel:
#   nominals  [12.6,14.6,16.6,18.6,18.7,18.9] sum 100.0  -> each rounds to whole mm
#   partition each agent rounds its own -> [13,15,17,19,19,19] = 102 (drift 2.0, FAIL)
#   single    sees the whole chain -> picks 6 whole-mm lengths summing to 100 (PASS)
# Probe C (free, MCP-verified 2026-05-31): partition drift 2.0mm vs single 0.0, tol 0.8
# -> separates with 2.5x margin; FreeCAD reproduces mandated lengths exactly so the
# gate measures the real choice. The result hinges on SINGLE actually reconciling
# (in the no-grid equal case single failed to and drifted long) — the single_task
# makes that explicit. Gate is the same length-sum oracle as tchain (reused).
# =============================================================================

TCHAINU_NOMINALS = [12.6, 14.6, 16.6, 18.6, 18.7, 18.9]  # sum 100.0, each rounds UP
TCHAINU_N = len(TCHAINU_NOMINALS)
TCHAINU_GRID = 1.0  # whole-millimetre manufacturing stock


def _tchainu_rounded():
    """What each partition agent independently produces (round nominal to grid)."""
    return [round(x) for x in TCHAINU_NOMINALS]  # [13,15,17,19,19,19] = 102


def _tchainu_reconciled():
    """What a correct GLOBAL build (single / reference) achieves: whole-mm lengths
    summing to exactly T, by trimming the excess off the trailing segments."""
    ints = _tchainu_rounded()
    excess = int(round(sum(ints) - TCHAIN_TOTAL))  # 2
    i = len(ints) - 1
    while excess > 0:
        ints[i] -= 1
        excess -= 1
        i -= 1
    return ints  # [13,15,17,19,18,18] = 100


def _tchainu_seg_task(i):
    return (f"You are building ONE segment of a chain of {TCHAINU_N} segments that, "
            f"butted end to end, must total exactly {TCHAIN_TOTAL:.0f} mm. Your "
            f"segment's nominal length is {TCHAINU_NOMINALS[i]:.1f} mm along X. "
            f"MANUFACTURING CONSTRAINT: segments are cut from whole-millimetre stock, "
            f"so the finished length MUST be a whole number of millimetres — round "
            f"your nominal to the nearest whole mm. Width {TCHAIN_SEG_D:.0f} mm, "
            f"height {TCHAIN_SEG_H:.0f} mm. Build the block at the rounded length, "
            f"then save_component.")


def _tchainu_single_task():
    noms = ", ".join(f"{x:.1f}" for x in TCHAINU_NOMINALS)
    return (f"You will build all {TCHAINU_N} segments of a chain, one at a time. "
            f"Butted end to end they must total EXACTLY {TCHAIN_TOTAL:.0f} mm. "
            f"MANUFACTURING CONSTRAINT: each finished segment must be a whole number "
            f"of millimetres. The nominal lengths are {noms} mm — but you MAY adjust "
            f"each to a nearby whole mm; what matters is that the {TCHAINU_N} whole-mm "
            f"lengths SUM TO EXACTLY {TCHAIN_TOTAL:.0f} mm. Each is {TCHAIN_SEG_D:.0f} "
            f"mm wide and {TCHAIN_SEG_H:.0f} mm tall. Choose the {TCHAINU_N} integer "
            f"lengths now so they total {TCHAIN_TOTAL:.0f}, then build each.")


def _tchainu_ref(name):
    """Reference = the reconciled whole-mm chain that sums to T (what single can do)."""
    lengths = _tchainu_reconciled()
    i = int(name[3:])  # "segN"
    return lambda w, path: _ref_box(w, path, name, lengths[i],
                                    TCHAIN_SEG_D, TCHAIN_SEG_H)


def _make_tchain_unequal():
    comps = {f"seg{i}": _tchainu_seg_task(i) for i in range(TCHAINU_N)}
    rounded = _tchainu_rounded()
    return Toy(
        "tchainu", f"Tolerance chain, UNEQUAL on whole-mm grid (partition LOSES, n={TCHAINU_N})",
        components=comps,
        single_task=_tchainu_single_task(),
        gate=_tchain_gate(TCHAINU_N),  # same length-sum oracle, tol TCHAIN_TOL
        reference=lambda w, name, path: _tchainu_ref(name)(w, path),
        negatives=[
            # The un-reconciled round-up — exactly what partition produces. Sums to
            # 102, must be caught (drift 2.0 > 0.8). Overrides every segment.
            Neg("unreconciled_roundup", "interference",  # expect informational; gate is length
                agent={f"seg{i}": (f"Build a block EXACTLY {rounded[i]} mm long along "
                                   f"X, {TCHAIN_SEG_D:.0f} wide, {TCHAIN_SEG_H:.0f} "
                                   f"tall. Use this exact length. Then save_component.")
                       for i in range(TCHAINU_N)},
                ref={f"seg{i}": (lambda w, p, L=rounded[i], nm=f"seg{i}":
                                 _ref_box(w, p, nm, L, TCHAIN_SEG_D, TCHAIN_SEG_H))
                     for i in range(TCHAINU_N)}),
        ],
    )


TOY7_TCHAINU = _make_tchain_unequal()


# =============================================================================
# toy 8: pin-and-SLOT exact constraint (lock the in-plane DOF correctly)
# Locating one part on another in a plane removes 3 DOF: translation x, translation
# y, and rotation θ. The textbook EXACT-CONSTRAINT scheme uses a round hole + a slot,
# NOT two round holes (that's what twopin does — it's over-constrained and jams on any
# pin-spacing error). Here:
#   - a ROUND hole on pin 1 locks x and y (the locating datum),
#   - a SLOT on pin 2, long axis ALONG the pin1->pin2 line, locks rotation θ while
#     FREEING the spacing direction — so a small pin-spacing error still assembles.
# The contract the two agents must share: pin Ø, both positions, AND the slot's
# orientation (derived from the line between the two pins) + its length/width. Richer
# derived state than twopin. Gate is FUNCTIONAL, in two assemblies:
#   (1) agent carrier + agent fixture at NOMINAL must seat (both pins clear), and
#   (2) a reference carrier with pin 2 shifted along the slot axis must STILL seat —
#       a round hole at P2 (the over-constrained mistake) or a perpendicular slot
#       JAMS here, so this is the check that distinguishes a real slot.
# =============================================================================

PS_PLATE = (60.0, 40.0, 10.0)       # carrier plate w,d,h
PS_FIX_T = 6.0                      # fixture thickness
PS_PIN_R, PS_PIN_H = 4.0, 12.0      # pins Ø8, 12 tall
PS_SPACING = 30.0                   # pin spacing along X, symmetric about center
PS_HOLE_R = 4.2                     # round hole Ø8.4 -> 0.4 clearance; locks x,y
PS_SLOT_W = 8.4                     # slot narrow dim (Y) = pin Ø + clearance; locks θ
PS_SLOT_L = 14.0                    # slot long dim (X) along pin axis; frees spacing
PS_PROBE_DX = 2.5                   # perturbed pin-2 shift: < slot slack 2.8, > hole slack 0.2
_PS_CX, _PS_CY = PS_PLATE[0] / 2.0, PS_PLATE[1] / 2.0


def _ps_centers(spacing):
    return [(_PS_CX - spacing / 2.0, _PS_CY), (_PS_CX + spacing / 2.0, _PS_CY)]


def _ref_hole_slot(w, path, name, sx, sy, sz, hole_r, slot_w, slot_l, centers):
    """Fixture: a round hole at centers[0] (locates x,y) + a rectangular slot at
    centers[1] with long axis along X (frees spacing, locks rotation)."""
    w.call("new_document", name=name)
    cur = w.call("add_primitive", kind="box", w=sx, d=sy, h=sz, name=name)
    c0x, c0y = centers[0]
    h0 = w.call("add_primitive", kind="cylinder", r=hole_r, h=sz * 3,
                placement=[c0x, c0y, -sz], name="hole")
    cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=h0["handle"])
    c1x, c1y = centers[1]
    slot = w.call("add_primitive", kind="box", w=slot_l, d=slot_w, h=sz * 3,
                  placement=[c1x - slot_l / 2.0, c1y - slot_w / 2.0, -sz], name="slot")
    cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=slot["handle"])
    w.call("save_document", path=str(path))


def _ps_gate(tmp, files):
    centers = _ps_centers(PS_SPACING)
    # (1) nominal: both agent parts seat together.
    a1 = _merge_check(tmp, [
        (files["carrier"], [0, 0, 0], "carrier"),
        (files["fixture"], [0, 0, PS_PLATE[2]], "fixture")])
    if not a1["ok"]:
        return {"ok": False, "reason": f"nominal seat failed ({a1['reason']})",
                "interference": a1.get("interference", []), "envelope": []}
    # (2) probe the slot: reference carrier with pin 2 shifted +PROBE_DX along the
    # slot axis must STILL seat in the agent fixture. Round-hole/perpendicular jams.
    probe = tmp / "ps_probe_carrier.FCStd"
    pcent = [centers[0], (centers[1][0] + PS_PROBE_DX, centers[1][1])]
    with Worker() as w:
        _ref_pinned_plate(w, probe, "carrier", *PS_PLATE, PS_PIN_R, PS_PIN_H, pcent)
    a2 = _merge_check(tmp, [
        (probe, [0, 0, 0], "carrier"),
        (files["fixture"], [0, 0, PS_PLATE[2]], "fixture")])
    if not a2["ok"]:
        return {"ok": False,
                "reason": f"slot did not free the spacing axis — over-constrained "
                          f"(round hole) or mis-oriented slot ({a2['reason']})",
                "interference": a2.get("interference", []), "envelope": []}
    return {"ok": True, "reason": "round hole locates x,y; slot frees spacing & locks θ",
            "interference": [], "envelope": []}


_PS_POS = (f"on the Y centerline of a {PS_PLATE[0]:.0f}x{PS_PLATE[1]:.0f} mm plate, "
           f"{PS_SPACING:.0f} mm apart along X, symmetric about the plate center")

TOY8_PINSLOT = Toy(
    "pinslot", "Pin-and-slot exact constraint (round hole + oriented slot)",
    components={
        "carrier": (f"Build a CARRIER: a {PS_PLATE[0]:.0f}x{PS_PLATE[1]:.0f}x"
                    f"{PS_PLATE[2]:.0f} mm plate with TWO cylindrical pins Ø"
                    f"{2*PS_PIN_R:.0f} mm, {PS_PIN_H:.0f} mm tall, standing up from the "
                    f"top face, {_PS_POS}. Fuse the pins to the plate so it is one "
                    f"solid. Then save_component."),
        "fixture": (f"Build a FIXTURE: a {PS_PLATE[0]:.0f}x{PS_PLATE[1]:.0f}x"
                    f"{PS_FIX_T:.0f} mm plate that locates onto a mating part's two "
                    f"pins by the EXACT-CONSTRAINT scheme — a ROUND through-hole Ø"
                    f"{2*PS_HOLE_R:.1f} mm at the FIRST pin position (this locates X "
                    f"and Y), and a SLOT at the SECOND pin position whose LONG axis "
                    f"runs along the line joining the two pins (the X direction), "
                    f"{PS_SLOT_L:.0f} mm long by {PS_SLOT_W:.1f} mm wide (so it locks "
                    f"rotation but allows for pin-spacing tolerance along X). Both "
                    f"features {_PS_POS}. Cut both through. Then save_component."),
    },
    single_task=(f"You will build two mating parts that locate by an EXACT-CONSTRAINT "
                 f"pin-and-slot scheme, one at a time. Two pin positions {_PS_POS}.\n"
                 f"CARRIER: {PS_PLATE[0]:.0f}x{PS_PLATE[1]:.0f}x{PS_PLATE[2]:.0f} mm "
                 f"plate with two Ø{2*PS_PIN_R:.0f} mm pins {PS_PIN_H:.0f} mm tall "
                 f"fused on top at both positions.\nFIXTURE: {PS_PLATE[0]:.0f}x"
                 f"{PS_PLATE[1]:.0f}x{PS_FIX_T:.0f} mm plate with a ROUND hole Ø"
                 f"{2*PS_HOLE_R:.1f} mm at the FIRST position (locates x,y) and a SLOT "
                 f"{PS_SLOT_L:.0f}x{PS_SLOT_W:.1f} mm, long axis along X, at the SECOND "
                 f"(locks rotation, frees spacing). A round hole at BOTH positions "
                 f"would over-constrain and jam on any spacing error — use a slot."),
    gate=_ps_gate,
    reference=lambda w, name, path: (
        _ref_pinned_plate(w, path, "carrier", *PS_PLATE, PS_PIN_R, PS_PIN_H,
                          _ps_centers(PS_SPACING)) if name == "carrier"
        else _ref_hole_slot(w, path, "fixture", PS_PLATE[0], PS_PLATE[1], PS_FIX_T,
                            PS_HOLE_R, PS_SLOT_W, PS_SLOT_L, _ps_centers(PS_SPACING))),
    negatives=[
        # Over-constrained: a ROUND hole at BOTH positions (the twopin mistake). Seats
        # at nominal but JAMS when pin 2 shifts -> only assembly (2) catches it.
        Neg("round_hole_at_p2", "interference",
            agent={"fixture": (f"Build a FIXTURE plate {PS_PLATE[0]:.0f}x"
                               f"{PS_PLATE[1]:.0f}x{PS_FIX_T:.0f} mm with TWO ROUND "
                               f"through-holes Ø{2*PS_HOLE_R:.1f} mm (no slot), "
                               f"{_PS_POS}. Then save_component.")},
            ref={"fixture": lambda w, p: _ref_box_holes(
                w, p, "fixture", PS_PLATE[0], PS_PLATE[1], PS_FIX_T, PS_HOLE_R,
                _ps_centers(PS_SPACING))}),
        # Carrier pins at the WRONG spacing (20 not 30): misses both fixture features
        # at nominal -> assembly (1) catches it.
        Neg("wrong_spacing", "interference",
            agent={"carrier": (f"Build a CARRIER plate {PS_PLATE[0]:.0f}x"
                               f"{PS_PLATE[1]:.0f}x{PS_PLATE[2]:.0f} mm with two Ø"
                               f"{2*PS_PIN_R:.0f} mm pins {PS_PIN_H:.0f} mm tall fused "
                               f"on top, symmetric about center but only 20 mm apart "
                               f"along X. Then save_component.")},
            ref={"carrier": lambda w, p: _ref_pinned_plate(
                w, p, "carrier", *PS_PLATE, PS_PIN_R, PS_PIN_H, _ps_centers(20.0))}),
    ],
)


# =============================================================================
# toys 9-11: GD&T LOCATION family (position, concentricity, symmetry)
# The first toys whose gate MEASURES a feature against a tolerance ZONE rather than
# testing fit by interference. The thin builder surface (axis-aligned primitives,
# translate-only) can't introduce form/orientation error, but the agent fully
# controls feature PLACEMENT — so the location controls are a clean fit: the agent
# fails by mis-placing, and a run_script gate reads the as-built feature axis and
# checks deviation from true position. Two independent parts per toy with DIFFERENT
# nominals (context load for single); each must satisfy its callout.
# =============================================================================

def _cyl_faces(path):
    """Unique cylindrical faces of the first shaped object in a saved part, as
    [{cx, cy, r}] sorted by descending radius. Used to read as-built hole / boss /
    bore axes for the GD&T location gates."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code="""
objs = [o for o in App.ActiveDocument.Objects
        if hasattr(o, "Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs = [o for o in App.ActiveDocument.Objects
            if hasattr(o, "Shape") and not o.Shape.isNull()]
sh = objs[0].Shape
seen = []; uniq = []
for f in sh.Faces:
    s = f.Surface
    if s.__class__.__name__ == "Cylinder":
        c = s.Center
        k = (round(c.x, 3), round(c.y, 3), round(s.Radius, 3))
        if k not in seen:
            seen.append(k)
            uniq.append({"cx": round(c.x, 4), "cy": round(c.y, 4),
                         "r": round(s.Radius, 4)})
uniq.sort(key=lambda d: -d["r"])
__result__ = uniq
""")
    return r["result"]


GDT_ZONE_R = 0.2  # all callouts are Ø0.4 tolerance zones -> 0.2 mm allowed deviation


# --- toy 9: true position ----------------------------------------------------
POS_PLATE = (50.0, 50.0, 8.0)
POS_HOLE_R = 6.0  # Ø12 hole
POS_NOM = {"plateA": (18.0, 32.0), "plateB": (34.0, 14.0)}  # true position per part


def _pos_gate(tmp, files):
    for name, (nx, ny) in POS_NOM.items():
        cy = _cyl_faces(files[name])
        if len(cy) != 1:
            return {"ok": False, "reason": f"{name}: expected 1 hole, found {len(cy)}",
                    "interference": [], "envelope": []}
        dev = ((cy[0]["cx"] - nx) ** 2 + (cy[0]["cy"] - ny) ** 2) ** 0.5
        if dev > GDT_ZONE_R:
            return {"ok": False,
                    "reason": f"{name}: true-position dev {dev:.3f} > {GDT_ZONE_R} mm",
                    "interference": [], "envelope": []}
    return {"ok": True, "reason": "all holes within their Ø0.4 position zones",
            "interference": [], "envelope": []}


def _pos_task(name):
    nx, ny = POS_NOM[name]
    return (f"Build a {POS_PLATE[0]:.0f}x{POS_PLATE[1]:.0f}x{POS_PLATE[2]:.0f} mm PLATE "
            f"with one vertical THROUGH-HOLE Ø{2*POS_HOLE_R:.0f} mm. GD&T callout: the "
            f"hole's TRUE POSITION is X={nx:.0f} mm from datum B (the x=0 edge) and "
            f"Y={ny:.0f} mm from datum A (the y=0 edge), within a Ø0.4 mm tolerance "
            f"zone (the axis must land within 0.2 mm of true position). Place the bore "
            f"accordingly, cut it through, then save_component.")


TOY9_POSITION = Toy(
    "gdt_position", "GD&T true position (hole vs Ø0.4 zone off datums)",
    components={"plateA": _pos_task("plateA"), "plateB": _pos_task("plateB")},
    single_task=("You will build two plates, one at a time. Each is "
                 f"{POS_PLATE[0]:.0f}x{POS_PLATE[1]:.0f}x{POS_PLATE[2]:.0f} mm with one "
                 f"Ø{2*POS_HOLE_R:.0f} mm through-hole at a TRUE POSITION (within a Ø0.4 "
                 f"mm zone, i.e. axis within 0.2 mm) measured from datum B (x=0 edge) "
                 f"and datum A (y=0 edge):\nplateA: X={POS_NOM['plateA'][0]:.0f}, "
                 f"Y={POS_NOM['plateA'][1]:.0f}\nplateB: X={POS_NOM['plateB'][0]:.0f}, "
                 f"Y={POS_NOM['plateB'][1]:.0f}"),
    gate=_pos_gate,
    reference=lambda w, name, path: _ref_box_holes(
        w, path, name, *POS_PLATE, POS_HOLE_R, [POS_NOM[name]]),
    negatives=[
        # plateA hole displaced 0.5 mm (out of the 0.2 mm zone).
        Neg("plateA_off_position", "interference",
            agent={"plateA": (f"Build a {POS_PLATE[0]:.0f}x{POS_PLATE[1]:.0f}x"
                              f"{POS_PLATE[2]:.0f} mm plate with a Ø{2*POS_HOLE_R:.0f} "
                              f"mm through-hole whose axis is at X="
                              f"{POS_NOM['plateA'][0]+0.5:.1f}, "
                              f"Y={POS_NOM['plateA'][1]:.0f} (use these exact "
                              f"coordinates). Then save_component.")},
            ref={"plateA": lambda w, p: _ref_box_holes(
                w, p, "plateA", *POS_PLATE, POS_HOLE_R,
                [(POS_NOM["plateA"][0] + 0.5, POS_NOM["plateA"][1])])}),
    ],
)


# --- toy 10: concentricity / coaxiality --------------------------------------
def _ref_boss_bore(w, path, name, outer_r, bore_r, h, bore_center):
    """A cylindrical boss (Ø outer) with a through-bore (Ø bore) at bore_center."""
    w.call("new_document", name=name)
    boss = w.call("add_primitive", kind="cylinder", r=outer_r, h=h, name=name)
    bx, by = bore_center
    bore = w.call("add_primitive", kind="cylinder", r=bore_r, h=h * 3,
                  placement=[bx, by, -h], name="bore")
    w.call("boolean_op", op="cut", base=boss["handle"], tool=bore["handle"])
    w.call("save_document", path=str(path))


CONC = {"bossA": {"outer_r": 15.0, "bore_r": 6.0, "h": 12.0},
        "bossB": {"outer_r": 20.0, "bore_r": 8.0, "h": 12.0}}


def _conc_gate(tmp, files):
    for name in CONC:
        cy = _cyl_faces(files[name])
        if len(cy) < 2:
            return {"ok": False, "reason": f"{name}: need boss+bore faces, found {len(cy)}",
                    "interference": [], "envelope": []}
        outer, inner = cy[0], cy[-1]
        off = ((outer["cx"] - inner["cx"]) ** 2 + (outer["cy"] - inner["cy"]) ** 2) ** 0.5
        if off > GDT_ZONE_R:
            return {"ok": False,
                    "reason": f"{name}: bore axis off boss axis {off:.3f} > {GDT_ZONE_R} mm",
                    "interference": [], "envelope": []}
    return {"ok": True, "reason": "all bores coaxial within Ø0.4",
            "interference": [], "envelope": []}


def _conc_task(name):
    c = CONC[name]
    return (f"Build a cylindrical BOSS Ø{2*c['outer_r']:.0f} mm, {c['h']:.0f} mm tall, "
            f"with a concentric THROUGH-BORE Ø{2*c['bore_r']:.0f} mm. GD&T callout: the "
            f"bore axis must be COAXIAL with the boss axis within Ø0.4 mm (axes within "
            f"0.2 mm). Center the bore on the boss axis, then save_component.")


TOY10_CONCENTRIC = Toy(
    "gdt_concentric", "GD&T concentricity (bore coaxial with boss)",
    components={"bossA": _conc_task("bossA"), "bossB": _conc_task("bossB")},
    single_task=("You will build two cylindrical bosses, one at a time, each with a "
                 "concentric through-bore COAXIAL to the boss axis within Ø0.4 mm "
                 "(axes within 0.2 mm):\n"
                 f"bossA: boss Ø{2*CONC['bossA']['outer_r']:.0f}, bore "
                 f"Ø{2*CONC['bossA']['bore_r']:.0f}, {CONC['bossA']['h']:.0f} tall\n"
                 f"bossB: boss Ø{2*CONC['bossB']['outer_r']:.0f}, bore "
                 f"Ø{2*CONC['bossB']['bore_r']:.0f}, {CONC['bossB']['h']:.0f} tall"),
    gate=_conc_gate,
    reference=lambda w, name, path: _ref_boss_bore(
        w, path, name, CONC[name]["outer_r"], CONC[name]["bore_r"], CONC[name]["h"],
        (0.0, 0.0)),
    negatives=[
        # bossA bore offset 0.5 mm from the boss axis.
        Neg("bossA_eccentric", "interference",
            agent={"bossA": (f"Build a Ø{2*CONC['bossA']['outer_r']:.0f} mm boss "
                             f"{CONC['bossA']['h']:.0f} mm tall with a "
                             f"Ø{2*CONC['bossA']['bore_r']:.0f} mm through-bore whose "
                             f"axis is offset 0.5 mm from the boss axis. Then "
                             f"save_component.")},
            ref={"bossA": lambda w, p: _ref_boss_bore(
                w, p, "bossA", CONC["bossA"]["outer_r"], CONC["bossA"]["bore_r"],
                CONC["bossA"]["h"], (0.5, 0.0))}),
    ],
)


# --- toy 11: symmetry --------------------------------------------------------
SYM_PLATE = (60.0, 40.0, 8.0)
SYM_HOLE_R = 5.0
SYM_MEDIAN_Y = SYM_PLATE[1] / 2.0  # datum median plane (y = 20)
# two holes that must straddle the median plane symmetrically; X & spread differ/part
SYM_NOM = {"plateA": {"x": 20.0, "ys": (8.0, 32.0)},   # spread 24, midpoint 20
           "plateB": {"x": 38.0, "ys": (12.0, 28.0)}}  # spread 16, midpoint 20


def _sym_centers(name):
    s = SYM_NOM[name]
    return [(s["x"], s["ys"][0]), (s["x"], s["ys"][1])]


def _sym_gate(tmp, files):
    for name in SYM_NOM:
        cy = _cyl_faces(files[name])
        if len(cy) != 2:
            return {"ok": False, "reason": f"{name}: expected 2 holes, found {len(cy)}",
                    "interference": [], "envelope": []}
        midy = (cy[0]["cy"] + cy[1]["cy"]) / 2.0
        dev = abs(midy - SYM_MEDIAN_Y)
        if dev > GDT_ZONE_R:
            return {"ok": False,
                    "reason": f"{name}: holes' midplane off datum by {dev:.3f} > {GDT_ZONE_R} mm",
                    "interference": [], "envelope": []}
    return {"ok": True, "reason": "hole pairs symmetric about the median plane within Ø0.4",
            "interference": [], "envelope": []}


def _sym_task(name):
    s = SYM_NOM[name]
    return (f"Build a {SYM_PLATE[0]:.0f}x{SYM_PLATE[1]:.0f}x{SYM_PLATE[2]:.0f} mm PLATE "
            f"with TWO vertical through-holes Ø{2*SYM_HOLE_R:.0f} mm, both at x="
            f"{s['x']:.0f} mm, at y={s['ys'][0]:.0f} and y={s['ys'][1]:.0f} mm. GD&T "
            f"callout: the two holes must be SYMMETRIC about the plate's median plane "
            f"(y={SYM_MEDIAN_Y:.0f} mm, the datum) within Ø0.4 mm — their midpoint must "
            f"lie within 0.2 mm of y={SYM_MEDIAN_Y:.0f}. Cut both through, then "
            f"save_component.")


TOY11_SYMMETRY = Toy(
    "gdt_symmetry", "GD&T symmetry (hole pair about median plane)",
    components={"plateA": _sym_task("plateA"), "plateB": _sym_task("plateB")},
    single_task=("You will build two plates, one at a time, each "
                 f"{SYM_PLATE[0]:.0f}x{SYM_PLATE[1]:.0f}x{SYM_PLATE[2]:.0f} mm with two "
                 f"Ø{2*SYM_HOLE_R:.0f} mm through-holes that must be SYMMETRIC about the "
                 f"median plane y={SYM_MEDIAN_Y:.0f} (midpoint within 0.2 mm):\n"
                 f"plateA: x={SYM_NOM['plateA']['x']:.0f}, y="
                 f"{SYM_NOM['plateA']['ys'][0]:.0f} & {SYM_NOM['plateA']['ys'][1]:.0f}\n"
                 f"plateB: x={SYM_NOM['plateB']['x']:.0f}, y="
                 f"{SYM_NOM['plateB']['ys'][0]:.0f} & {SYM_NOM['plateB']['ys'][1]:.0f}"),
    gate=_sym_gate,
    reference=lambda w, name, path: _ref_box_holes(
        w, path, name, *SYM_PLATE, SYM_HOLE_R, _sym_centers(name)),
    negatives=[
        # plateA: one hole shifted so the pair's midplane is 0.5 mm off the datum.
        Neg("plateA_asymmetric", "interference",
            agent={"plateA": (f"Build a {SYM_PLATE[0]:.0f}x{SYM_PLATE[1]:.0f}x"
                              f"{SYM_PLATE[2]:.0f} mm plate with two "
                              f"Ø{2*SYM_HOLE_R:.0f} mm through-holes at x="
                              f"{SYM_NOM['plateA']['x']:.0f}, y="
                              f"{SYM_NOM['plateA']['ys'][0]+1.0:.0f} and y="
                              f"{SYM_NOM['plateA']['ys'][1]:.0f}. Then save_component.")},
            ref={"plateA": lambda w, p: _ref_box_holes(
                w, p, "plateA", *SYM_PLATE, SYM_HOLE_R,
                [(SYM_NOM["plateA"]["x"], SYM_NOM["plateA"]["ys"][0] + 1.0),
                 (SYM_NOM["plateA"]["x"], SYM_NOM["plateA"]["ys"][1])])}),
    ],
)


# =============================================================================
# toy 12: GD&T ORIENTATION — angularity of an axis (uses the rotate tool)
# Orientation tolerances need a real tilt, which the translate-only surface couldn't
# do — so this toy exercises the added `rotate` tool. A slender post must stand at a
# nominal angle from vertical (the Z datum) within ±1°. The agent builds the post and
# tilts it; the gate reads the as-built long axis (least-inertia principal axis) and
# checks its angle to Z. Agent fails by tilting the wrong amount (or not at all).
# =============================================================================

ANG_POST = (8.0, 8.0, 40.0)            # slender post w,d,h (long axis = h)
ANG_NOM = {"postA": 30.0, "postB": 20.0}  # tilt from vertical (Z), degrees
ANG_TOL = 1.0


def _post_axis_angle(path):
    """Angle (deg) between a slender part's long axis (least-inertia principal axis)
    and global Z — the angularity measurement."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code="""
import math
objs = [o for o in App.ActiveDocument.Objects
        if hasattr(o, "Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs = [o for o in App.ActiveDocument.Objects
            if hasattr(o, "Shape") and not o.Shape.isNull()]
pp = objs[0].Shape.PrincipalProperties
moms = list(pp["Moments"])
axes = [pp["FirstAxisOfInertia"], pp["SecondAxisOfInertia"], pp["ThirdAxisOfInertia"]]
v = App.Vector(axes[moms.index(min(moms))]); v.normalize()
__result__ = round(math.degrees(math.acos(min(1.0, abs(v.z)))), 4)
""")
    return r["result"]


def _ref_tilted_post(w, path, name, tilt_deg):
    w.call("new_document", name=name)
    b = w.call("add_primitive", kind="box", w=ANG_POST[0], d=ANG_POST[1],
               h=ANG_POST[2], name=name)
    _apply_rotation(w, b["name"], [0, 1, 0], tilt_deg)  # tilt from vertical about Y
    w.call("save_document", path=str(path))


def _ang_gate(tmp, files):
    for name, nom in ANG_NOM.items():
        a = _post_axis_angle(files[name])
        if abs(a - nom) > ANG_TOL:
            return {"ok": False,
                    "reason": f"{name}: axis at {a:.2f}° vs {nom:.0f}° (> ±{ANG_TOL}°)",
                    "interference": [], "envelope": []}
    return {"ok": True, "reason": "posts within ±1° of nominal angularity",
            "interference": [], "envelope": []}


def _ang_task(name):
    nom = ANG_NOM[name]
    return (f"Build a slender POST {ANG_POST[0]:.0f}x{ANG_POST[1]:.0f}x{ANG_POST[2]:.0f} "
            f"mm (long axis starts vertical, along Z). GD&T callout: ANGULARITY — its "
            f"long axis must sit at {nom:.0f}° from vertical (from the Z axis) within "
            f"±{ANG_TOL:.0f}°. Use the rotate tool to tilt it {nom:.0f}° about the Y "
            f"axis, then save_component.")


TOY12_ANGULARITY = Toy(
    "gdt_angularity", "GD&T angularity (post axis vs Z datum, ±1°)",
    components={"postA": _ang_task("postA"), "postB": _ang_task("postB")},
    single_task=("You will build two slender posts, one at a time, each "
                 f"{ANG_POST[0]:.0f}x{ANG_POST[1]:.0f}x{ANG_POST[2]:.0f} mm, then TILT "
                 f"each (rotate about Y) so its long axis is at the called-out angle "
                 f"from vertical (Z) within ±{ANG_TOL:.0f}°:\n"
                 f"postA: {ANG_NOM['postA']:.0f}°\npostB: {ANG_NOM['postB']:.0f}°"),
    gate=_ang_gate,
    reference=lambda w, name, path: _ref_tilted_post(w, path, name, ANG_NOM[name]),
    negatives=[
        # postA tilted 35° (5° past the ±1° band).
        Neg("postA_wrong_angle", "interference",
            agent={"postA": (f"Build an {ANG_POST[0]:.0f}x{ANG_POST[1]:.0f}x"
                             f"{ANG_POST[2]:.0f} mm post and tilt it 35° from vertical "
                             f"about the Y axis (use 35). Then save_component.")},
            ref={"postA": lambda w, p: _ref_tilted_post(w, p, "postA", 35.0)}),
    ],
)


TOYS = {t.key: t for t in (TOY1, TOY2, TOY3, TOY4,
                           TOY5_NSLOT4, TOY5_NSLOT8,
                           TOY6_TCHAIN3, TOY6_TCHAIN6,
                           TOY7_TCHAINU, TOY8_PINSLOT,
                           TOY9_POSITION, TOY10_CONCENTRIC, TOY11_SYMMETRY,
                           TOY12_ANGULARITY)}


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

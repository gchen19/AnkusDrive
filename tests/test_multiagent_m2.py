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
        "name": "add_gear",
        "description": ("Add an involute spur gear (real teeth) to the active document. "
                        "teeth (>=3), module (mm; pitch diameter = module*teeth), height "
                        "(mm), pressure_angle (deg, default 20), external (false for an "
                        "internal/ring gear), optional placement [x,y,z]. Two external "
                        "gears MESH when their axes are (module*(teeth_a+teeth_b)/2) "
                        "apart. Returns {handle, pitch_radius, tip_radius, teeth, ...}."),
        "input_schema": {
            "type": "object",
            "properties": {
                "teeth": {"type": "integer"},
                "module": {"type": "number"},
                "height": {"type": "number"},
                "pressure_angle": {"type": "number"},
                "external": {"type": "boolean"},
                "placement": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["teeth", "module"],
        },
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
    # Must carry the SAME full contract the partition agents get between them
    # (plate dims + hole positions/diameters + peg diameters) — the original
    # wording omitted the plate spec entirely, so single's plate put holes where
    # the gate doesn't look and 0/20 was an artifact, not a context-load result.
    holes = "; ".join(f"slot {i} at x={15.0 + i*NSLOT_PITCH:.0f} y={NSLOT_Y:.0f} "
                      f"diameter {NSLOT_HOLE_D[i]:.0f} mm" for i in range(k))
    pegs = "; ".join(f"peg {i}: Ø{NSLOT_HOLE_D[i]-NSLOT_CLEAR:.1f} mm" for i in range(k))
    return (f"You will build a baseplate and {k} pegs, one at a time. The BASEPLATE "
            f"is {_nslot_plate_w(k):.0f} x {NSLOT_PLATE_D:.0f} x {NSLOT_PLATE_H:.0f} mm "
            f"with {k} vertical through-holes, each a DIFFERENT diameter: {holes}. "
            f"Each peg is {NSLOT_PEG_H:.0f} mm long and slip-fits one specific hole "
            f"with {NSLOT_CLEAR} mm clearance: {pegs}. "
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
TOY5_NSLOT6 = _make_nslot(6)   # k-sweep midpoint (Probe D validated the k=6 oracle)
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
    [{cx, cy, r, axis:[x,y,z]}] sorted by descending radius. Reads as-built hole /
    boss / bore axes for the GD&T-location and kinematic gates."""
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
        c = s.Center; a = s.Axis
        k = (round(c.x, 3), round(c.y, 3), round(s.Radius, 3))
        if k not in seen:
            seen.append(k)
            uniq.append({"cx": round(c.x, 4), "cy": round(c.y, 4),
                         "r": round(s.Radius, 4),
                         "axis": [round(a.x, 4), round(a.y, 4), round(a.z, 4)]})
uniq.sort(key=lambda d: -d["r"])
__result__ = uniq
""")
    return r["result"]


GDT_ZONE_R = 0.2  # all callouts are Ø0.4 tolerance zones -> 0.2 mm allowed deviation


# --- structural FEM gate helper (CalculiX via the FreeCAD FEM stack) ----------
# The reusable physics oracle for the strength/stiffness toys (and the structural
# half of the thermo-structural capstone). Opens a saved part, fixes the support
# face and presses on the load face (a PRESSURE constraint acts along the face
# normal — no edge-picking, so it generalises to arbitrary geometry), meshes,
# solves with CalculiX, and returns max von Mises (MPa) + max displacement (mm).
#
# IMPORTANT — gate RELATIVE, not absolute. CalculiX-through-the-worker magnitudes
# depend on mesh + setup and are not certified stress; they ARE monotonic and
# discriminating (thinner/weaker part -> higher von Mises + displacement, verified).
# So the FEM toys compare the agent's part against a scripted reference build under
# the SAME setup (agent must be within a margin of, or stiffer than, the reference)
# — any consistent solver offset cancels. Built on the existing FreeCAD FEM tools;
# heavier per call (mesh + solve) but free. Future dedicated tooling: docs/SIMULATION_TOOLS.md.

_FEM_STEEL = {"Name": "Steel-Generic", "YoungsModulus": "210000 MPa",
              "PoissonRatio": "0.30", "Density": "7900 kg/m^3"}


def _fem_stress(path, fix_normal, load_normal, force_n,
                material=None, char_length=5.0):
    """Cantilever-style structural FEM on a saved part. Fixes the largest planar
    face whose normal ~ `fix_normal`, applies `force_n` (N) as pressure over the
    largest planar face whose normal ~ `load_normal`, solves. Returns
    {vm_mpa, disp_mm, load_area_mm2}. Face selection is deterministic (area_desc)
    so notched/multi-face parts pick the main support/load face."""
    material = material or _FEM_STEEL
    with Worker() as w:
        w.call("open_document", path=str(path))
        nm = w.call("run_script", code='''
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]
__result__ = objs[0].Name
''')["result"]
        body = w.call("register_handle", object=nm)["handle"]
        an = w.call("fem_new_analysis", name="A")["handle"]
        w.call("fem_set_solver", analysis=an, kind="ccx")
        w.call("fem_set_material", analysis=an, body=body, material=material)
        fix = w.call("query_faces", handle=body, predicate={
            "type": "planar", "normal_dir": fix_normal, "order": "area_desc"})
        load = w.call("query_faces", handle=body, predicate={
            "type": "planar", "normal_dir": load_normal, "order": "area_desc"})
        if not fix or not load:
            raise RuntimeError(
                f"FEM gate: faces not found (fix={len(fix)}, load={len(load)})")
        w.call("fem_add_constraint", analysis=an, kind="fixed",
               refs=[{"handle": body, "tag": fix[0]["tag"]}])
        area = load[0]["area"]
        w.call("fem_add_constraint", analysis=an, kind="pressure",
               pressure=force_n / area,
               refs=[{"handle": body, "tag": load[0]["tag"]}])
        w.call("fem_mesh", analysis=an, body=body, char_length=char_length)
        w.call("fem_run", analysis=an)
        res = w.call("fem_results", analysis=an)
    return {"vm_mpa": res["max_vonmises_mpa"],
            "disp_mm": res["max_displacement_mm"], "load_area_mm2": area}


_FEM_DENSITY = 7.9e-6   # steel, kg/mm^3 (for mass budgets)
_FEM_REF_CACHE = {}     # toy:component -> reference {vm_mpa, disp_mm}, computed once


def _part_volume(path):
    """Total solid volume (mm^3) of the top-level shaped objects in a saved part."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code='''
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]
__result__ = sum(o.Shape.Volume for o in objs)
''')
    return r["result"]


def _fem_ref(key, build_ref, fem_kwargs, fn=None):
    """Reference yardstick for a relative FEM gate: build the scripted-correct part
    once, run the analysis (fn defaults to _fem_stress), cache. build_ref(path)
    writes the reference part."""
    fn = fn or _fem_stress
    if key not in _FEM_REF_CACHE:
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            refp = Path(td) / "ref.FCStd"
            build_ref(refp)
            _FEM_REF_CACHE[key] = fn(refp, **fem_kwargs)
    return _FEM_REF_CACHE[key]


# --- steady-state thermal FEM gate helper (heat flux in + convection out) -----
_FEM_STEEL_THERMAL = dict(_FEM_STEEL, Name="Steel-Thermal",
                          ThermalConductivity="43 W/m/K", SpecificHeat="500 J/kg/K",
                          ThermalExpansionCoefficient="12 um/m/K")


def _fem_thermal(path, heat_normal, flux_w_m2, ambient_c=20.0, film=30.0,
                 char_length=6.0):
    """Steady-state thermal FEM: a fixed heat flux into the `heat_normal` face,
    convection (ambient_c, film) on every other planar face. Returns
    {max_temp_c, mean_temp_c}. Used by the thermo-structural capstone; gated
    RELATIVE to a reference (same caveat as _fem_stress)."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        nm = w.call("run_script", code='''
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]
__result__ = objs[0].Name''')["result"]
        body = w.call("register_handle", object=nm)["handle"]
        an = w.call("fem_new_analysis", name="T")["handle"]
        w.call("fem_set_solver", analysis=an, kind="ccx",
               tunables={"ThermoMechSteadyState": True, "AnalysisType": "thermomech"})
        w.call("fem_set_material", analysis=an, body=body, material=_FEM_STEEL_THERMAL)
        heat = w.call("query_faces", handle=body, predicate={
            "type": "planar", "normal_dir": heat_normal, "order": "area_desc"})
        cool = []
        for nd in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]):
            if nd == list(heat_normal):
                continue
            cool += w.call("query_faces", handle=body, predicate={
                "type": "planar", "normal_dir": nd, "order": "area_desc"})
        w.call("fem_add_constraint", analysis=an, kind="heatflux", flux_type="DFlux",
               flux=flux_w_m2, refs=[{"handle": body, "tag": heat[0]["tag"]}])
        w.call("fem_add_constraint", analysis=an, kind="heatflux", flux_type="Convection",
               ambient_temp=ambient_c, film_coef=film,
               refs=[{"handle": body, "tag": c["tag"]} for c in cool])
        w.call("fem_add_constraint", analysis=an, kind="initial_temperature",
               temperature=ambient_c, refs=[{"handle": body, "tag": heat[0]["tag"]}])
        w.call("fem_mesh", analysis=an, body=body, char_length=char_length)
        w.call("fem_run", analysis=an)
        res = w.call("fem_thermal_results", analysis=an)
    return {"max_temp_c": res["temperatures_c"]["max"],
            "mean_temp_c": res["temperatures_c"]["mean"]}


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


# =============================================================================
# toys 13-19: KINEMATIC mechanisms (gear trains, linkages, moving assemblies)
# A different class from the static-fit toys: these check ratios, mesh/center
# distances, linkage aiming, and — for the moving ones — that parts move through
# their range without colliding. Two new gate capabilities back them:
#   * gear geometry via the add_gear primitive (real involute teeth), measured from
#     the as-built tip radius (rp = tip - module);
#   * a swept-motion interference gate (_sweep_clear): pose every part at each motion
#     step via forward kinematics the gate encodes, then interference-check.
# Gears can't be built with box/cylinder, so the agent surface gains add_gear (the
# same DriftPin primitive). NOTE: add_gear (and rotate) change the tool surface — not
# a clean baseline against the pre-kinematic Haiku numbers.
# =============================================================================

GEAR_MODULE = 2.0
GEAR_H = 6.0


def _ref_gear(w, path, name, teeth, module=GEAR_MODULE, height=GEAR_H, external=True,
              placement=None):
    w.call("new_document", name=name)
    kw = {"teeth": int(teeth), "module": module, "height": height, "external": external}
    if placement is not None:
        kw["placement"] = placement
    w.call("add_gear", **kw)
    w.call("save_document", path=str(path))


def _gear_tip_radius(path):
    """Max vertex radial distance from the Z axis of the single gear in a saved part
    = tip radius. External pitch radius = tip - module."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code="""
import math
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]
vs=objs[0].Shape.Vertexes
__result__ = round(max(math.hypot(v.X, v.Y) for v in vs), 4)
""")
    return r["result"]


# --- toy 13/14: multi-speed gearbox (pairs sharing ONE center distance) ------
GBOX_C = 48.0                                   # shaft center distance (mm)
GBOX_RATIOS = [3.0, 2.0, 1.4, 1.0, 5.0 / 7.0, 0.5]   # 6 speeds, all integer-tooth


def _gbox_teeth(ratio):
    S = int(round(2 * GBOX_C / GEAR_MODULE))    # tooth sum per pair (48)
    nin = int(round(S / (1.0 + ratio)))
    return nin, S - nin


def _make_gearbox(n):
    ratios = GBOX_RATIOS[:n]
    S = int(round(2 * GBOX_C / GEAR_MODULE))
    teeth, comps = {}, {}
    for s, ratio in enumerate(ratios):
        nin, nout = _gbox_teeth(ratio)
        teeth[f"in{s}"], teeth[f"out{s}"] = nin, nout
        comps[f"in{s}"] = (
            f"Build the INPUT gear of gearbox speed {s+1}. Every gear uses module "
            f"{GEAR_MODULE:g} mm and every input+output pair meshes across a shaft "
            f"center distance of {GBOX_C:g} mm — so each pair's tooth counts SUM to "
            f"2*{GBOX_C:g}/{GEAR_MODULE:g} = {S}. This speed's ratio (output:input "
            f"teeth) is {ratio:.4g}. Compute your tooth count = round({S}/(1+{ratio:.4g})) "
            f"and build it: add_gear(teeth=<that>, module={GEAR_MODULE:g}, "
            f"height={GEAR_H:g}). Then save_component.")
        comps[f"out{s}"] = (
            f"Build the OUTPUT gear of gearbox speed {s+1}. Module {GEAR_MODULE:g} mm; "
            f"each pair's teeth sum to {S} (center distance {GBOX_C:g} mm). This speed's "
            f"ratio (output:input teeth) is {ratio:.4g}. Compute your tooth count = "
            f"{S} - round({S}/(1+{ratio:.4g})) and build it: add_gear(teeth=<that>, "
            f"module={GEAR_MODULE:g}, height={GEAR_H:g}). Then save_component.")

    def gate(tmp, files):
        m = GEAR_MODULE
        for s, ratio in enumerate(ratios):
            rin = _gear_tip_radius(files[f"in{s}"]) - m
            rout = _gear_tip_radius(files[f"out{s}"]) - m
            if abs(rin + rout - GBOX_C) > 0.5:
                return {"ok": False, "interference": [], "envelope": [],
                        "reason": f"speed {s+1}: pitch sum {rin+rout:.1f} != "
                                  f"C {GBOX_C:g} mm (pair will not mesh)"}
            if abs(rout / rin - ratio) > 0.05 * ratio + 0.02:
                return {"ok": False, "interference": [], "envelope": [],
                        "reason": f"speed {s+1}: ratio {rout/rin:.3f} != {ratio:.3f}"}
        return {"ok": True, "interference": [], "envelope": [],
                "reason": f"all {n} pairs mesh at C={GBOX_C:g} with correct ratios"}

    lines = "\n".join(f"speed {s+1}: ratio {r:.4g} -> teeth "
                      f"{_gbox_teeth(r)[0]}/{_gbox_teeth(r)[1]}"
                      for s, r in enumerate(ratios))
    single_task = (
        f"You will build {2*n} gears (input + output for {n} speeds), one at a time. "
        f"Module {GEAR_MODULE:g} mm; every pair meshes at shaft center distance "
        f"{GBOX_C:g} mm (teeth sum {S}). Build each with add_gear.\n{lines}")

    return Toy(
        f"kin_gearbox{n}",
        f"{n}-speed gearbox (gear pairs sharing one center distance)",
        components=comps, single_task=single_task, gate=gate,
        reference=lambda w, name, path: _ref_gear(w, path, name, teeth[name]),
        negatives=[
            # one input gear with 2 extra teeth -> its pair no longer sums to C.
            Neg("in0_wrong_teeth", "interference",
                agent={"in0": (f"Build a gear add_gear(teeth={teeth['in0']+2}, "
                               f"module={GEAR_MODULE:g}, height={GEAR_H:g}) — use "
                               f"{teeth['in0']+2} teeth exactly. Then save_component.")},
                ref={"in0": lambda w, p: _ref_gear(w, p, "in0", teeth["in0"] + 2)}),
        ],
    )


TOY13_GEARBOX6 = _make_gearbox(6)
TOY14_GEARBOX3 = _make_gearbox(3)


# --- toy 15: planetary gear set ----------------------------------------------
# sun + planets + internal ring + carrier. The defining relationships:
#   Nring = Nsun + 2*Nplanet          (concentric meshing — the core constraint)
#   carrier planet-circle radius = module*(Nsun+Nplanet)/2  (sun-planet mesh)
#   (Nsun + Nring) % n_planets == 0   (equal-spacing assembly condition)
# Sun/planet are external (pitch r = tip - m); the ring is internal (pitch r =
# inner-tip + m). The carrier carries n_planets axle holes, equally spaced.
PLAN_M = 2.0
PLAN_NSUN, PLAN_NPLANET, PLAN_NP = 18, 18, 3
PLAN_NRING = PLAN_NSUN + 2 * PLAN_NPLANET           # 54
PLAN_CARRIER_R = PLAN_M * (PLAN_NSUN + PLAN_NPLANET) / 2.0   # 36
PLAN_AXLE_R = 3.0


def _gear_radii(path):
    """(min, max) vertex radial distance from the part's Z axis."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code="""
import math
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]
rad=[math.hypot(v.X, v.Y) for v in objs[0].Shape.Vertexes]
__result__ = [round(min(rad),4), round(max(rad),4)]
""")
    return r["result"]


def _ref_carrier(w, path, name, n, radius, axle_r=PLAN_AXLE_R, disk_h=GEAR_H):
    import math as _m
    w.call("new_document", name=name)
    cur = w.call("add_primitive", kind="cylinder", r=radius + 8.0, h=disk_h, name=name)
    for k in range(n):
        a = 2 * _m.pi * k / n
        hole = w.call("add_primitive", kind="cylinder", r=axle_r, h=disk_h * 3,
                      placement=[radius * _m.cos(a), radius * _m.sin(a), -disk_h],
                      name="axle")
        cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=hole["handle"])
    w.call("save_document", path=str(path))


def _planetary_gate(tmp, files):
    import math as _m
    m = PLAN_M
    sun_rp = _gear_radii(files["sun"])[1] - m
    planet_rp = _gear_radii(files["planet"])[1] - m
    ring_rp = _gear_radii(files["ring"])[0] + m       # internal: inner tip + module
    # core relationship Nring = Nsun + 2 Nplanet  <=>  ring_rp = sun_rp + 2 planet_rp
    if abs(ring_rp - (sun_rp + 2 * planet_rp)) > 0.6:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"ring rp {ring_rp:.1f} != sun+2*planet "
                          f"{sun_rp + 2*planet_rp:.1f} (meshing relation broken)"}
    # carrier: n_planets holes equally spaced at the sun-planet center distance
    holes = [f for f in _cyl_faces(files["carrier"]) if f["r"] < PLAN_CARRIER_R * 0.5]
    if len(holes) != PLAN_NP:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"carrier has {len(holes)} axle holes, expected {PLAN_NP}"}
    want_r = sun_rp + planet_rp
    angs = []
    for h in holes:
        rr = _m.hypot(h["cx"], h["cy"])
        if abs(rr - want_r) > 0.6:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"carrier axle at r={rr:.1f}, expected {want_r:.1f}"}
        angs.append(_m.degrees(_m.atan2(h["cy"], h["cx"])) % 360)
    angs.sort()
    gaps = [(angs[(i + 1) % len(angs)] - angs[i]) % 360 for i in range(len(angs))]
    if max(gaps) - min(gaps) > 2.0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"carrier axles not equally spaced: gaps {[round(g,1) for g in gaps]}"}
    # equal-spacing assembly condition
    nsun, nring = round(2 * sun_rp / m), round(2 * ring_rp / m)
    if (nsun + nring) % PLAN_NP != 0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"(Nsun+Nring)={nsun+nring} not divisible by {PLAN_NP} planets"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"Nring={nring}=Nsun+2Nplanet, {PLAN_NP} planets equally spaced, assembles"}


# --- swept-motion interference gate (shared by the moving mechanisms) --------

def _sweep_clear(tmp, pose_fn, params, label="sweep", tol=1.0):
    """pose_fn(param) -> [(path, placement, name), ...]. Pose the parts at each motion
    step and interference-check. Returns {ok, reason}; fails at the first step whose
    worst interference >= tol mm^3 (a mechanism that collides through its range)."""
    with Worker() as w:
        for p in params:
            placed = pose_fn(p)
            w.call("new_document", name="swp")
            asm = w.call("make_assembly", name="A")
            w.call("save_document", path=str(tmp / "swp.FCStd"))
            for path, placement, nm in placed:
                w.call("add_part", assembly=asm["handle"], source={"path": str(path)},
                       placement=placement, name=nm)
            clash = w.call("interference_check", assembly=asm["handle"])
            worst = max((c["interference_mm3"] for c in clash), default=0.0)
            w.call("close_document", name="active")
            if worst >= tol:
                return {"ok": False,
                        "reason": f"{label}: interference {worst:.0f}mm3 at step {p:g}"}
    return {"ok": True, "reason": f"{label}: clear through {len(params)} steps"}


# --- toy 17: slider-crank (piston-crankshaft) --------------------------------
# crank (throw R) + connecting rod (length L) + piston sliding in a guide. Closure:
# x_piston(θ) = R cosθ + sqrt(L² - R² sin²θ) — real for all θ iff L > R (else the
# mechanism binds); stroke = 2R; rod swing = asin(R/L). The gate measures R (crankpin
# offset) and L (rod hole spacing), checks the analytic motion over a full crank
# revolution, and runs a posed-interference sweep of the piston through its stroke
# inside the guide (a too-wide piston jams the bore).
SC_R, SC_L, SC_PINR = 15.0, 50.0, 4.0
SC_DISKR = 23.0
SC_DISK_H, SC_PIN_H = 8.0, 10.0
SC_PW, SC_PL = 24.0, 20.0          # piston cross-section / length
SC_SLOT = 25.0                      # guide bore (square) -> 0.5 mm clearance
SC_MID = SC_L                       # mid-stroke piston centre (x_p at θ=90 ~ sqrt(L²-R²))


def _ref_crank(w, path, name, R=SC_R, pinr=SC_PINR, diskr=SC_DISKR):
    w.call("new_document", name=name)
    disk = w.call("add_primitive", kind="cylinder", r=diskr, h=SC_DISK_H, name=name)
    pin = w.call("add_primitive", kind="cylinder", r=pinr, h=SC_PIN_H,
                 placement=[R, 0, SC_DISK_H], name="pin")
    w.call("boolean_op", op="fuse", base=disk["handle"], tool=pin["handle"])
    w.call("save_document", path=str(path))


def _ref_conrod(w, path, name, L=SC_L, holer=SC_PINR + 0.3):
    w.call("new_document", name=name)
    bar = w.call("add_primitive", kind="box", w=L + 16, d=12, h=6,
                 placement=[-8, -6, 0], name=name)
    cur = bar
    for cx in (0.0, L):
        hole = w.call("add_primitive", kind="cylinder", r=holer, h=18,
                      placement=[cx, 0, -6], name="hole")
        cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=hole["handle"])
    w.call("save_document", path=str(path))


def _ref_piston(w, path, name, pw=SC_PW, pl=SC_PL):
    w.call("new_document", name=name)
    w.call("add_primitive", kind="box", w=pl, d=pw, h=pw,
           placement=[-pl / 2, -pw / 2, -pw / 2], name=name)
    w.call("save_document", path=str(path))


def _ref_guide(w, path, name, slot=SC_SLOT):
    w.call("new_document", name=name)
    span = 2 * SC_R + SC_PL + 10
    x0 = SC_MID - span / 2
    outer = w.call("add_primitive", kind="box", w=span, d=slot + 16, h=slot + 16,
                   placement=[x0, -(slot + 16) / 2, -(slot + 16) / 2], name=name)
    chan = w.call("add_primitive", kind="box", w=span * 1.2, d=slot, h=slot,
                  placement=[x0 - span * 0.1, -slot / 2, -slot / 2], name="chan")
    w.call("boolean_op", op="cut", base=outer["handle"], tool=chan["handle"])
    w.call("save_document", path=str(path))


def _slidercrank_gate(tmp, files):
    import math as _m
    # R = crankpin offset (the small cylinder off the disk axis)
    cf = _cyl_faces(files["crank"])
    pin = min(cf, key=lambda c: c["r"])
    R = _m.hypot(pin["cx"], pin["cy"])
    # L = spacing between the conrod's two end holes
    rf = _cyl_faces(files["conrod"])
    if len(rf) < 2:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"conrod has {len(rf)} holes, need 2"}
    a, b = rf[0], rf[1]
    L = _m.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"])
    if L <= R + 1.0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"rod L={L:.1f} <= crank throw R={R:.1f}: mechanism BINDS"}
    swing = _m.degrees(_m.asin(R / L))
    if swing > 20.0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"rod swing {swing:.1f}° > 20° (excessive side thrust)"}
    # analytic motion: piston position over a full crank revolution must stay real
    xs = []
    for deg in range(0, 360, 15):
        th = _m.radians(deg)
        disc = L * L - (R * _m.sin(th)) ** 2
        if disc < 0:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"closure fails at θ={deg}° (binds)"}
        xs.append(R * _m.cos(th) + _m.sqrt(disc))
    stroke = max(xs) - min(xs)
    if abs(stroke - 2 * R) > 0.5:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"stroke {stroke:.1f} != 2R {2*R:.1f}"}
    # posed-interference sweep: piston through its stroke inside the fixed guide
    def pose(x_p):
        return [(files["guide"], [0, 0, 0], "guide"),
                (files["piston"], [x_p, 0, 0], "piston")]
    sweep = _sweep_clear(tmp, pose, [SC_MID - SC_R, SC_MID, SC_MID + SC_R],
                         label="piston-in-guide")
    if not sweep["ok"]:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": sweep["reason"] + " (piston jams the bore)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"closes (L>R), stroke {stroke:.0f}mm, swing {swing:.0f}°, "
                      f"piston clears bore through stroke"}


TOY17_SLIDERCRANK = Toy(
    "kin_slidercrank", "Slider-crank piston-crankshaft (closure + stroke + bore clearance)",
    components={
        "crank": (f"Build a CRANK: a disk (radius {SC_DISKR:g} mm, {SC_DISK_H:g} mm "
                  f"thick) with a CRANKPIN (cylinder radius {SC_PINR:g} mm, {SC_PIN_H:g} "
                  f"mm tall) standing up from the disk face at radius (throw) {SC_R:g} mm "
                  f"from the disk axis. Fuse them. Then save_component."),
        "conrod": (f"Build a CONNECTING ROD: a flat bar with two pin holes radius "
                   f"{SC_PINR+0.3:g} mm whose centres are {SC_L:g} mm apart (the rod "
                   f"length must exceed the crank throw {SC_R:g} mm or the mechanism "
                   f"binds). Then save_component."),
        "piston": (f"Build a PISTON: a square block {SC_PW:g}x{SC_PW:g} mm in cross "
                   f"section and {SC_PL:g} mm long (it slides along its length). Then "
                   f"save_component."),
        "guide": (f"Build a GUIDE/cylinder block with a square bore {SC_SLOT:g}x"
                  f"{SC_SLOT:g} mm running through it (the piston slides in this bore "
                  f"with a little clearance). Then save_component."),
    },
    single_task=(
        f"You will build a slider-crank, one part at a time: CRANK (disk radius "
        f"{SC_DISKR:g}, crankpin radius {SC_PINR:g} at throw {SC_R:g} mm), CONROD (bar, "
        f"two Ø{2*(SC_PINR+0.3):g} holes {SC_L:g} mm apart — must exceed the throw), "
        f"PISTON ({SC_PW:g}x{SC_PW:g}x{SC_PL:g} block), GUIDE (block with a {SC_SLOT:g}x"
        f"{SC_SLOT:g} bore). Stroke = 2*throw; the piston must slide in the bore."),
    gate=_slidercrank_gate,
    reference=lambda w, name, path: (
        _ref_crank(w, path, "crank") if name == "crank" else
        _ref_conrod(w, path, "conrod") if name == "conrod" else
        _ref_piston(w, path, "piston") if name == "piston" else
        _ref_guide(w, path, "guide")),
    negatives=[
        # rod shorter than the crank throw -> the slider-crank cannot close (binds).
        Neg("rod_too_short", "interference",
            agent={"conrod": (f"Build a flat bar with two holes radius {SC_PINR+0.3:g} "
                              f"mm only {SC_R-3:g} mm apart. Then save_component.")},
            ref={"conrod": lambda w, p: _ref_conrod(w, p, "conrod", L=SC_R - 3)}),
        # piston wider than the bore -> jams in the guide through the stroke.
        Neg("piston_too_wide", "interference",
            agent={"piston": (f"Build a square block {SC_SLOT+3:g}x{SC_SLOT+3:g} mm and "
                              f"{SC_PL:g} mm long. Then save_component.")},
            ref={"piston": lambda w, p: _ref_piston(w, p, "piston", pw=SC_SLOT + 3)}),
    ],
)


# --- toy 16: Ackermann steering knuckles (linkage aiming, static) ------------
# The Ackermann condition (design-intent form): each steering arm aims at the centre
# of the rear axle, so the line from the kingpin through the tie-rod ball joint passes
# through the rear-axle midpoint. Two mirror knuckles share track T and wheelbase L;
# each agent derives its arm direction. The classic wrong answer — parallel steering
# arms (tie-rod straight inboard) — fails the aim. Holes differ in size so the gate can
# tell the kingpin (Ø12) from the tie-rod ball joint (Ø8).
import math as _math  # noqa: E402

ACK_TRACK, ACK_WHEELBASE, ACK_ARM = 120.0, 200.0, 30.0
ACK_PLATE = (50.0, 60.0, 8.0)
ACK_KINGPIN = (20.0, 45.0)          # kingpin hole position in the part frame
ACK_KP_R, ACK_TR_R = 6.0, 4.0       # Ø12 kingpin, Ø8 tie-rod


def _ack_aim_dir(side):
    """Unit vector from a front kingpin toward the rear-axle midpoint.
    Front kingpins at (±T/2, 0); rear-axle midpoint at (0, -L)."""
    kx = -ACK_TRACK / 2.0 if side == "left" else ACK_TRACK / 2.0
    v = (0.0 - kx, -ACK_WHEELBASE - 0.0)
    n = _math.hypot(*v)
    return (v[0] / n, v[1] / n)


def _ack_tierod_xy(side):
    d = _ack_aim_dir(side)
    return (ACK_KINGPIN[0] + ACK_ARM * d[0], ACK_KINGPIN[1] + ACK_ARM * d[1])


def _ref_two_holes(w, path, name, sx, sy, sz, h1, h2):
    """Plate with two through-holes h=(cx, cy, r)."""
    w.call("new_document", name=name)
    cur = w.call("add_primitive", kind="box", w=sx, d=sy, h=sz, name=name)
    for cx, cy, r in (h1, h2):
        tool = w.call("add_primitive", kind="cylinder", r=r, h=sz * 3,
                      placement=[cx, cy, -sz], name="hole")
        cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=tool["handle"])
    w.call("save_document", path=str(path))


def _ack_gate(tmp, files):
    for side, name in (("left", "knuckle_L"), ("right", "knuckle_R")):
        cyl = _cyl_faces(files[name])
        if len(cyl) != 2:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: expected 2 holes, found {len(cyl)}"}
        kingpin = max(cyl, key=lambda c: c["r"])   # Ø12 kingpin
        tierod = min(cyl, key=lambda c: c["r"])    # Ø8 ball joint
        vx, vy = tierod["cx"] - kingpin["cx"], tierod["cy"] - kingpin["cy"]
        n = _math.hypot(vx, vy)
        if n < 1e-6:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: holes coincide"}
        aim = _ack_aim_dir(side)
        cosang = max(-1.0, min(1.0, (vx / n) * aim[0] + (vy / n) * aim[1]))
        err = _math.degrees(_math.acos(cosang))
        if err > 2.0:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: steering arm off rear-axle aim by {err:.1f}° "
                              f"(parallel-arm / wrong Ackermann)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": "both steering arms aim at the rear-axle midpoint"}


def _ack_task(side, name):
    tr = _ack_tierod_xy(side)
    return (f"Build the {side.upper()} steering KNUCKLE: a {ACK_PLATE[0]:g}x"
            f"{ACK_PLATE[1]:g}x{ACK_PLATE[2]:g} mm plate with a KINGPIN hole Ø"
            f"{2*ACK_KP_R:g} mm at ({ACK_KINGPIN[0]:g}, {ACK_KINGPIN[1]:g}) and a "
            f"TIE-ROD ball-joint hole Ø{2*ACK_TR_R:g} mm. Ackermann condition: with the "
            f"{side} kingpin mounted at ({-ACK_TRACK/2 if side=='left' else ACK_TRACK/2:g}"
            f", 0) and the rear-axle midpoint at (0, {-ACK_WHEELBASE:g}), the steering "
            f"arm (length {ACK_ARM:g} mm from the kingpin to the tie-rod hole) must AIM "
            f"at the rear-axle midpoint. Compute the tie-rod hole position = kingpin + "
            f"{ACK_ARM:g}*unit(rear_axle_midpoint - kingpin) and cut both holes through. "
            f"Then save_component.")


TOY16_ACKERMANN = Toy(
    "kin_ackermann", "Ackermann steering (arms aim at rear-axle midpoint)",
    components={"knuckle_L": _ack_task("left", "knuckle_L"),
                "knuckle_R": _ack_task("right", "knuckle_R")},
    single_task=(
        f"You will build LEFT and RIGHT steering knuckles, one at a time. Track "
        f"T={ACK_TRACK:g} mm (kingpins at ±{ACK_TRACK/2:g}, 0), wheelbase "
        f"L={ACK_WHEELBASE:g} mm (rear-axle midpoint at 0,{-ACK_WHEELBASE:g}), steering "
        f"arm {ACK_ARM:g} mm. Each {ACK_PLATE[0]:g}x{ACK_PLATE[1]:g}x{ACK_PLATE[2]:g} mm "
        f"plate has a Ø{2*ACK_KP_R:g} kingpin hole at ({ACK_KINGPIN[0]:g},"
        f"{ACK_KINGPIN[1]:g}) and a Ø{2*ACK_TR_R:g} tie-rod hole placed so the "
        f"kingpin->tie-rod arm AIMS at the rear-axle midpoint (Ackermann)."),
    gate=_ack_gate,
    reference=lambda w, name, path: _ref_two_holes(
        w, path, name, *ACK_PLATE,
        (ACK_KINGPIN[0], ACK_KINGPIN[1], ACK_KP_R),
        (*_ack_tierod_xy("left" if name == "knuckle_L" else "right"), ACK_TR_R)),
    negatives=[
        # parallel steering arms: tie-rod straight back (−Y) from the kingpin, ignoring
        # the rear-axle aim -> Ackermann condition violated.
        Neg("parallel_arms", "interference",
            agent={"knuckle_L": (f"Build a {ACK_PLATE[0]:g}x{ACK_PLATE[1]:g}x"
                                 f"{ACK_PLATE[2]:g} mm plate with a Ø{2*ACK_KP_R:g} hole "
                                 f"at ({ACK_KINGPIN[0]:g},{ACK_KINGPIN[1]:g}) and a Ø"
                                 f"{2*ACK_TR_R:g} hole {ACK_ARM:g} mm straight behind it "
                                 f"at ({ACK_KINGPIN[0]:g},{ACK_KINGPIN[1]-ACK_ARM:g}). "
                                 f"Then save_component.")},
            ref={"knuckle_L": lambda w, p: _ref_two_holes(
                w, p, "knuckle_L", *ACK_PLATE,
                (ACK_KINGPIN[0], ACK_KINGPIN[1], ACK_KP_R),
                (ACK_KINGPIN[0], ACK_KINGPIN[1] - ACK_ARM, ACK_TR_R))}),
    ],
)


TOY15_PLANETARY = Toy(
    "kin_planetary", "Planetary gear set (ring=sun+2·planet, mesh, assembly condition)",
    components={
        "sun": (f"Build the SUN gear: external involute, module {PLAN_M:g} mm, "
                f"{PLAN_NSUN} teeth. add_gear(teeth={PLAN_NSUN}, module={PLAN_M:g}, "
                f"height={GEAR_H:g}). Then save_component."),
        "planet": (f"Build a PLANET gear: external involute, module {PLAN_M:g} mm, "
                   f"{PLAN_NPLANET} teeth. add_gear(teeth={PLAN_NPLANET}, "
                   f"module={PLAN_M:g}, height={GEAR_H:g}). Then save_component."),
        "ring": (f"Build the RING gear: an INTERNAL involute gear, module {PLAN_M:g} mm. "
                 f"In a planetary set the ring teeth = sun teeth + 2*planet teeth. The "
                 f"sun has {PLAN_NSUN} teeth and each planet {PLAN_NPLANET}; compute the "
                 f"ring teeth and build add_gear(teeth=<that>, module={PLAN_M:g}, "
                 f"height={GEAR_H:g}, external=false). Then save_component."),
        "carrier": (f"Build the CARRIER: a disk (radius {PLAN_CARRIER_R+8:g} mm, height "
                    f"{GEAR_H:g} mm) holding {PLAN_NP} planet axles EQUALLY SPACED. Each "
                    f"planet axis sits at the sun-planet centre distance from the centre "
                    f"= module*(sun_teeth+planet_teeth)/2 = {PLAN_M:g}*({PLAN_NSUN}+"
                    f"{PLAN_NPLANET})/2 = {PLAN_CARRIER_R:g} mm. Cut {PLAN_NP} axle holes "
                    f"(radius {PLAN_AXLE_R:g} mm) at that radius, equally spaced. Then "
                    f"save_component."),
    },
    single_task=(
        f"You will build a planetary gear set, one part at a time, all module "
        f"{PLAN_M:g} mm. Sun = {PLAN_NSUN} teeth (external); planet = {PLAN_NPLANET} "
        f"teeth (external); ring = sun + 2*planet teeth (INTERNAL, external=false); "
        f"carrier = a disk with {PLAN_NP} axle holes equally spaced at radius "
        f"module*(sun+planet)/2 = {PLAN_CARRIER_R:g} mm. Build sun, planet, ring, "
        f"carrier with add_gear / add_primitive."),
    gate=_planetary_gate,
    reference=lambda w, name, path: (
        _ref_gear(w, path, "sun", PLAN_NSUN) if name == "sun" else
        _ref_gear(w, path, "planet", PLAN_NPLANET) if name == "planet" else
        _ref_gear(w, path, "ring", PLAN_NRING, external=False) if name == "ring" else
        _ref_carrier(w, path, "carrier", PLAN_NP, PLAN_CARRIER_R)),
    negatives=[
        # ring built with sun+planet teeth (forgot the factor of 2) -> relation broken.
        Neg("ring_wrong_teeth", "interference",
            agent={"ring": (f"Build an INTERNAL gear add_gear(teeth="
                            f"{PLAN_NSUN+PLAN_NPLANET}, module={PLAN_M:g}, "
                            f"height={GEAR_H:g}, external=false). Then save_component.")},
            ref={"ring": lambda w, p: _ref_gear(
                w, p, "ring", PLAN_NSUN + PLAN_NPLANET, external=False)}),
        # carrier axles at the wrong radius (sun pitch only) -> planets won't mesh.
        Neg("carrier_wrong_radius", "interference",
            agent={"carrier": (f"Build a carrier disk radius {PLAN_CARRIER_R+8:g} mm "
                               f"height {GEAR_H:g} with {PLAN_NP} axle holes (radius "
                               f"{PLAN_AXLE_R:g}) equally spaced at radius "
                               f"{PLAN_M*PLAN_NSUN/2:g} mm. Then save_component.")},
            ref={"carrier": lambda w, p: _ref_carrier(
                w, p, "carrier", PLAN_NP, PLAN_M * PLAN_NSUN / 2.0)}),
    ],
)


# --- toy 18: Geneva drive (intermittent indexing) ----------------------------
# A driver with one pin indexes an n-slot wheel: each driver revolution advances the
# wheel 1/n turn. Tangency condition (smooth entry, no jam): drive-pin radius =
# C*sin(pi/n) for centre distance C. The wheel's n slots must be equally spaced
# (abstracted here as n engagement holes on the rim). Gate: measured pin radius vs
# the tangency value, and the wheel's n holes equally spaced.
GEN_N, GEN_C = 4, 60.0
GEN_RPIN = GEN_C * _math.sin(_math.pi / GEN_N)      # tangency pin radius
GEN_RPOS = GEN_C * _math.cos(_math.pi / GEN_N) * 0.8   # engagement-hole radius on wheel
GEN_HOLE_R = 4.0


def _ref_geneva_driver(w, path, name, rpin=GEN_RPIN):
    w.call("new_document", name=name)
    disk = w.call("add_primitive", kind="cylinder", r=rpin + 6, h=GEAR_H, name=name)
    pin = w.call("add_primitive", kind="cylinder", r=3.0, h=GEAR_H * 2,
                 placement=[rpin, 0, 0], name="pin")
    w.call("boolean_op", op="fuse", base=disk["handle"], tool=pin["handle"])
    w.call("save_document", path=str(path))


def _ref_geneva_wheel(w, path, name, n=GEN_N, rpos=GEN_RPOS):
    w.call("new_document", name=name)
    cur = w.call("add_primitive", kind="cylinder", r=rpos + 8, h=GEAR_H, name=name)
    ctr = w.call("add_primitive", kind="cylinder", r=5.0, h=GEAR_H * 3,
                 placement=[0, 0, -GEAR_H], name="centre")
    cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=ctr["handle"])
    for k in range(n):
        a = 2 * _math.pi * k / n
        slot = w.call("add_primitive", kind="cylinder", r=GEN_HOLE_R, h=GEAR_H * 3,
                      placement=[rpos * _math.cos(a), rpos * _math.sin(a), -GEAR_H],
                      name="slot")
        cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=slot["handle"])
    w.call("save_document", path=str(path))


def _geneva_gate(tmp, files):
    rpin = min(_cyl_faces(files["driver"]), key=lambda c: c["r"])
    off = _math.hypot(rpin["cx"], rpin["cy"])
    want = GEN_C * _math.sin(_math.pi / GEN_N)
    if abs(off - want) > 1.0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"drive-pin radius {off:.1f} != C·sin(π/n) {want:.1f} (will jam)"}
    holes = [c for c in _cyl_faces(files["wheel"])
             if c["r"] < GEN_RPOS and _math.hypot(c["cx"], c["cy"]) > 5.0]
    if len(holes) != GEN_N:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"wheel has {len(holes)} slots, expected {GEN_N}"}
    angs = sorted(_math.degrees(_math.atan2(h["cy"], h["cx"])) % 360 for h in holes)
    gaps = [(angs[(i + 1) % len(angs)] - angs[i]) % 360 for i in range(len(angs))]
    if max(gaps) - min(gaps) > 2.0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"slots not equally spaced (gaps {[round(g,1) for g in gaps]})"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"{GEN_N} slots equally spaced, pin at tangency -> 1/{GEN_N} index"}


TOY18_GENEVA = Toy(
    "kin_geneva", "Geneva drive (intermittent 1/n indexing, tangency)",
    components={
        "driver": (f"Build the GENEVA DRIVER: a disk with a single DRIVE PIN (cylinder "
                   f"radius 3 mm) standing on its face. For an {GEN_N}-slot Geneva with "
                   f"centre distance {GEN_C:g} mm, the pin radius from the disk axis must "
                   f"be C*sin(π/n) = {GEN_C:g}*sin(π/{GEN_N}) so the pin enters each slot "
                   f"tangentially. Place the pin at that radius. Then save_component."),
        "wheel": (f"Build the GENEVA WHEEL: a disk with a centre bore (radius 5 mm) and "
                  f"{GEN_N} engagement slots EQUALLY SPACED ({360//GEN_N}° apart) on the "
                  f"rim — model each slot as a hole radius {GEN_HOLE_R:g} mm at radius "
                  f"{GEN_RPOS:.1f} mm from the centre. Then save_component."),
    },
    single_task=(
        f"You will build a Geneva drive (driver + wheel), one part at a time. "
        f"{GEN_N}-slot, centre distance {GEN_C:g} mm. DRIVER: disk with a Ø6 drive pin "
        f"at radius C*sin(π/{GEN_N}) from the axis (tangency). WHEEL: disk, Ø10 centre "
        f"bore, {GEN_N} slot-holes (Ø{2*GEN_HOLE_R:g}) equally spaced at radius "
        f"{GEN_RPOS:.1f} mm. Build with add_primitive."),
    gate=_geneva_gate,
    reference=lambda w, name, path: (_ref_geneva_driver(w, path, "driver")
                                     if name == "driver"
                                     else _ref_geneva_wheel(w, path, "wheel")),
    negatives=[
        # drive pin at the wrong radius -> non-tangential entry, the Geneva jams.
        Neg("pin_not_tangent", "interference",
            agent={"driver": (f"Build a disk with a Ø6 drive pin at radius "
                              f"{GEN_RPIN*0.7:.1f} mm from the axis. Then save_component.")},
            ref={"driver": lambda w, p: _ref_geneva_driver(w, p, "driver",
                                                           rpin=GEN_RPIN * 0.7)}),
        # wrong slot count (3 not 4) -> wrong index ratio.
        Neg("wrong_slot_count", "interference",
            agent={"wheel": (f"Build a Geneva wheel with a Ø10 centre bore and only 3 "
                             f"slot-holes (Ø{2*GEN_HOLE_R:g}) equally spaced at radius "
                             f"{GEN_RPOS:.1f} mm. Then save_component.")},
            ref={"wheel": lambda w, p: _ref_geneva_wheel(w, p, "wheel", n=3)}),
    ],
)


# --- toy 19: Sarrus linkage (perpendicular folds -> straight-line motion) -----
# A Sarrus linkage constrains a platform to PURE translation using two hinged plate
# pairs whose fold (hinge) axes are PERPENDICULAR — that combination removes all DOF
# except the straight-line travel. The defining, checkable feature: the two units'
# hinge axes are orthogonal. Modelled as two brackets, each with a hinge hole; leafA's
# hinge runs along X, leafB's along Y. The classic failure — both hinges parallel —
# leaves the platform unconstrained (it can still translate sideways / rotate).
def _ref_sarrus_leaf(w, path, name, axis):
    """Bracket with a hinge hole whose axis is 'x' or 'y'."""
    w.call("new_document", name=name)
    box = w.call("add_primitive", kind="box", w=40, d=20, h=15, name=name)
    pin = w.call("add_primitive", kind="cylinder", r=4.0, h=80,
                 placement=[20, 10, 7.5 - 40], name="hinge")
    # cylinder is built along Z; rotate it to run along X (about Y) or Y (about X)
    if axis == "x":
        _apply_rotation(w, pin["name"], [0, 1, 0], 90.0, center=[20, 10, 7.5])
    else:
        _apply_rotation(w, pin["name"], [1, 0, 0], 90.0, center=[20, 10, 7.5])
    w.call("boolean_op", op="cut", base=box["handle"], tool=pin["handle"])
    w.call("save_document", path=str(path))


def _sarrus_gate(tmp, files):
    def hinge_axis(name):
        cyl = _cyl_faces(files[name])
        if not cyl:
            return None
        return cyl[0]["axis"]  # the only cylindrical face is the hinge bore
    ax_a, ax_b = hinge_axis("leafA"), hinge_axis("leafB")
    if ax_a is None or ax_b is None:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": "a leaf has no hinge bore"}
    if abs(ax_a[0]) < 0.95:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"leafA hinge axis {ax_a} not along X"}
    if abs(ax_b[1]) < 0.95:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"leafB hinge axis {ax_b} not along Y (folds not "
                          f"perpendicular -> platform not constrained)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": "hinge axes perpendicular (X⊥Y) -> 1-DOF straight-line motion"}


TOY19_SARRUS = Toy(
    "kin_sarrus", "Sarrus linkage (perpendicular hinge axes -> straight line)",
    components={
        "leafA": ("Build leaf A of a Sarrus linkage: a 40x20x15 mm bracket with a HINGE "
                  "bore (radius 4 mm) running along the X axis through it. Then "
                  "save_component."),
        "leafB": ("Build leaf B of a Sarrus linkage: a 40x20x15 mm bracket with a HINGE "
                  "bore (radius 4 mm) running along the Y axis (PERPENDICULAR to leaf "
                  "A's hinge — this is what constrains the platform to straight-line "
                  "motion). Then save_component."),
    },
    single_task=("You will build the two leaves of a Sarrus linkage, one at a time. Each "
                 "is a 40x20x15 mm bracket with a Ø8 hinge bore. leafA's hinge runs "
                 "along X; leafB's hinge runs along Y. The two fold axes MUST be "
                 "perpendicular — that is what makes the platform translate in a "
                 "straight line (1 DOF)."),
    gate=_sarrus_gate,
    reference=lambda w, name, path: _ref_sarrus_leaf(
        w, path, name, "x" if name == "leafA" else "y"),
    negatives=[
        # both hinges parallel (leafB along X too) -> platform not constrained.
        Neg("parallel_hinges", "interference",
            agent={"leafB": ("Build a 40x20x15 mm bracket with a Ø8 hinge bore running "
                             "along the X axis. Then save_component.")},
            ref={"leafB": lambda w, p: _ref_sarrus_leaf(w, p, "leafB", "x")}),
    ],
)


# --- toy 20: double-wishbone suspension (SLA geometry) -----------------------
# Upper + lower control arms + an upright form a four-bar. The short-long-arm (SLA)
# geometry — upper arm SHORTER than the lower — gives camber gain in bump; the upright
# length must close the four-bar loop with the chassis pivots. Gate (measured + loop
# closure): each arm's pivot-to-balljoint length, upright ball-joint spacing; require
# upper < lower (SLA) and upright == sqrt((Ll-Lu)^2 + H^2) (the nominal loop closes).
WB_LU, WB_LL, WB_H = 80.0, 110.0, 120.0          # upper, lower arm length; pivot stack
WB_UPRIGHT = _math.hypot(WB_LL - WB_LU, WB_H)    # 123.69


def _ref_two_hole_bar(w, path, name, span, holer=4.0):
    w.call("new_document", name=name)
    bar = w.call("add_primitive", kind="box", w=span + 16, d=12, h=6,
                 placement=[-8, -6, 0], name=name)
    cur = bar
    for cx in (0.0, span):
        hole = w.call("add_primitive", kind="cylinder", r=holer, h=18,
                      placement=[cx, 0, -6], name="hole")
        cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=hole["handle"])
    w.call("save_document", path=str(path))


def _wishbone_gate(tmp, files):
    def span(name):
        h = _cyl_faces(files[name])
        if len(h) < 2:
            return None
        return _math.hypot(h[0]["cx"] - h[1]["cx"], h[0]["cy"] - h[1]["cy"])
    lu, ll, up = span("upperarm"), span("lowerarm"), span("upright")
    if None in (lu, ll, up):
        return {"ok": False, "interference": [], "envelope": [],
                "reason": "an arm/upright is missing its two holes"}
    if lu > ll - 5.0:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"upper arm {lu:.0f} not shorter than lower {ll:.0f} "
                          f"(no SLA camber gain)"}
    want = _math.hypot(ll - lu, WB_H)
    if abs(up - want) > 1.5:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"upright {up:.1f} != loop-closure {want:.1f} "
                          f"(four-bar will not assemble)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"SLA (upper {lu:.0f} < lower {ll:.0f}), upright closes the "
                      f"four-bar at {up:.0f} mm"}


TOY20_WISHBONE = Toy(
    "kin_wishbone", "Double-wishbone suspension (SLA geometry + loop closure)",
    components={
        "upperarm": (f"Build the UPPER control arm: a bar with two pivot holes (radius 4 "
                     f"mm) whose centres are {WB_LU:g} mm apart. In a double-wishbone the "
                     f"upper arm is SHORTER than the lower (gives camber gain in bump). "
                     f"Then save_component."),
        "lowerarm": (f"Build the LOWER control arm: a bar with two holes (radius 4 mm) "
                     f"{WB_LL:g} mm apart. Then save_component."),
        "upright": (f"Build the UPRIGHT (knuckle): a bar with two ball-joint holes "
                    f"(radius 4 mm). With chassis pivots stacked {WB_H:g} mm apart and "
                    f"arms {WB_LU:g}/{WB_LL:g} mm, the upright must close the four-bar "
                    f"loop: hole spacing = sqrt((lower-upper)^2 + {WB_H:g}^2). Compute "
                    f"and build it. Then save_component."),
    },
    single_task=(
        f"You will build a double-wishbone (SLA) suspension, one part at a time: UPPER "
        f"arm (holes {WB_LU:g} mm apart), LOWER arm ({WB_LL:g} mm — longer than upper, "
        f"for camber gain), UPRIGHT (hole spacing = sqrt((lower-upper)^2 + {WB_H:g}^2) "
        f"to close the four-bar with chassis pivots {WB_H:g} mm apart)."),
    gate=_wishbone_gate,
    reference=lambda w, name, path: (
        _ref_two_hole_bar(w, path, "upperarm", WB_LU) if name == "upperarm" else
        _ref_two_hole_bar(w, path, "lowerarm", WB_LL) if name == "lowerarm" else
        _ref_two_hole_bar(w, path, "upright", WB_UPRIGHT)),
    negatives=[
        # upper arm as long as the lower -> parallelogram, no camber gain (not SLA).
        Neg("not_sla", "interference",
            agent={"upperarm": (f"Build a bar with two holes (radius 4 mm) {WB_LL:g} mm "
                                f"apart. Then save_component.")},
            ref={"upperarm": lambda w, p: _ref_two_hole_bar(w, p, "upperarm", WB_LL)}),
    ],
)


# =============================================================================
# toys 21-22: PHYSICS / FEM-gated (family A) — strength & stiffness
# The first toys with a PHYSICS oracle. The agent just builds geometry (existing
# tools); the gate runs structural FEM (_fem_stress) and judges. Each is a sizing
# problem with a WINDOW: too thin over-stresses / over-deflects, too thick blows a
# mass budget — so "just max it out" fails. Gated RELATIVE to a scripted reference
# under identical FEM setup (CalculiX-through-worker numbers are discriminating but
# not certified absolute), plus an absolute mass budget the agent is given.
# =============================================================================

def _box_file(path, name, sx, sy, sz):
    with Worker() as w:
        _ref_box(w, path, name, sx, sy, sz)


# --- toy 21: fem_bracket (strength: von Mises within a mass budget) ----------
FB_W, FB_LOAD = 30.0, 3000.0
FB = {"bracketA": {"L": 60.0, "h_ref": 12.0},
      "bracketB": {"L": 90.0, "h_ref": 16.0}}
FB_FEM = {"fix_normal": [-1, 0, 0], "load_normal": [0, 0, 1], "force_n": FB_LOAD}
FB_VM_MARGIN, FB_MASS_MARGIN = 1.6, 1.25


def _fb_budget_g(spec):
    return spec["L"] * FB_W * spec["h_ref"] * _FEM_DENSITY * FB_MASS_MARGIN * 1000.0


def _fb_gate(tmp, files):
    for name, spec in FB.items():
        ref = _fem_ref(f"fem_bracket:{name}",
                       lambda p, s=spec, n=name: _box_file(p, n, s["L"], FB_W, s["h_ref"]),
                       FB_FEM)
        res = _fem_stress(files[name], **FB_FEM)
        mass_g = _part_volume(files[name]) * _FEM_DENSITY * 1000.0
        if res["vm_mpa"] > ref["vm_mpa"] * FB_VM_MARGIN:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: von Mises {res['vm_mpa']:.3f} > "
                              f"{ref['vm_mpa']*FB_VM_MARGIN:.3f} MPa — too weak (thin)"}
        if mass_g > _fb_budget_g(spec):
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: mass {mass_g:.0f} g > budget "
                              f"{_fb_budget_g(spec):.0f} g — over-built"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": "both brackets strong enough within their mass budgets"}


def _fb_task(name):
    spec = FB[name]
    return (f"Build a load-bearing SHELF: a flat steel plate {spec['L']:.0f} mm long "
            f"(X) by {FB_W:.0f} mm wide (Y), fixed at the wall (its x=0 end face) and "
            f"carrying a {FB_LOAD:.0f} N downward load on its top face. Choose the plate "
            f"THICKNESS (Z height) so the peak bending stress stays low — a thicker "
            f"plate bends and stresses less — BUT keep the mass at or below "
            f"{_fb_budget_g(spec):.0f} g (you cannot just make it maximally thick). "
            f"Build the box at your chosen thickness, then save_component.")


TOY21_FEM_BRACKET = Toy(
    "fem_bracket", "FEM strength: load bracket sized within a mass budget",
    components={"bracketA": _fb_task("bracketA"), "bracketB": _fb_task("bracketB")},
    single_task=(
        f"You will build two steel load shelves, one at a time, each fixed at its x=0 "
        f"end and loaded with {FB_LOAD:.0f} N on top. Size each plate's THICKNESS so "
        f"bending stress stays low while staying under its mass budget:\n"
        f"bracketA: {FB['bracketA']['L']:.0f}x{FB_W:.0f} mm, "
        f"mass <= {_fb_budget_g(FB['bracketA']):.0f} g\n"
        f"bracketB: {FB['bracketB']['L']:.0f}x{FB_W:.0f} mm, "
        f"mass <= {_fb_budget_g(FB['bracketB']):.0f} g"),
    gate=_fb_gate,
    reference=lambda w, name, path: _ref_box(
        w, path, name, FB[name]["L"], FB_W, FB[name]["h_ref"]),
    negatives=[
        # bracketA too thin (5 mm) -> bending stress blows past the limit.
        Neg("bracketA_too_thin", "interference",
            agent={"bracketA": (f"Build a {FB['bracketA']['L']:.0f}x{FB_W:.0f}x5 mm "
                                f"steel plate. Then save_component.")},
            ref={"bracketA": lambda w, p: _ref_box(w, p, "bracketA",
                                                   FB["bracketA"]["L"], FB_W, 5.0)}),
        # bracketA too thick (22 mm) -> passes stress but busts the mass budget.
        Neg("bracketA_too_thick", "interference",
            agent={"bracketA": (f"Build a {FB['bracketA']['L']:.0f}x{FB_W:.0f}x22 mm "
                                f"steel plate. Then save_component.")},
            ref={"bracketA": lambda w, p: _ref_box(w, p, "bracketA",
                                                   FB["bracketA"]["L"], FB_W, 22.0)}),
    ],
)


# --- toy 22: fem_beam_stiffness (deflection within a mass budget) ------------
# Same FEM machinery, but the design driver is STIFFNESS, not strength: a longer,
# slimmer cantilever whose tip must not sag past a deflection limit. Deflection goes
# as ~1/thickness^3, so a slightly-too-thin beam fails hard — a different sensitivity
# than the stress toy. Window: too thin -> over-deflects, too thick -> over mass.
FBM_W, FBM_LOAD = 25.0, 1200.0
FBM = {"beamA": {"L": 120.0, "h_ref": 10.0},
       "beamB": {"L": 160.0, "h_ref": 12.0}}
FBM_FEM = {"fix_normal": [-1, 0, 0], "load_normal": [0, 0, 1], "force_n": FBM_LOAD}
FBM_DISP_MARGIN, FBM_MASS_MARGIN = 1.6, 1.25


def _fbm_budget_g(spec):
    return spec["L"] * FBM_W * spec["h_ref"] * _FEM_DENSITY * FBM_MASS_MARGIN * 1000.0


def _fbm_gate(tmp, files):
    for name, spec in FBM.items():
        ref = _fem_ref(f"fem_beam:{name}",
                       lambda p, s=spec, n=name: _box_file(p, n, s["L"], FBM_W, s["h_ref"]),
                       FBM_FEM)
        res = _fem_stress(files[name], **FBM_FEM)
        mass_g = _part_volume(files[name]) * _FEM_DENSITY * 1000.0
        if res["disp_mm"] > ref["disp_mm"] * FBM_DISP_MARGIN:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: tip deflection {res['disp_mm']:.4f} > "
                              f"{ref['disp_mm']*FBM_DISP_MARGIN:.4f} mm — too flexible (thin)"}
        if mass_g > _fbm_budget_g(spec):
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: mass {mass_g:.0f} g > budget "
                              f"{_fbm_budget_g(spec):.0f} g — over-built"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": "both beams stiff enough within their mass budgets"}


def _fbm_task(name):
    spec = FBM[name]
    return (f"Build a cantilever BEAM/shelf: a steel plate {spec['L']:.0f} mm long (X) "
            f"by {FBM_W:.0f} mm wide (Y), fixed at its x=0 end, carrying {FBM_LOAD:.0f} N "
            f"on its top face. Choose the THICKNESS (Z) so the TIP does not sag too far "
            f"— stiffness rises steeply with thickness (deflection ~ 1/thickness^3) — "
            f"while keeping mass at or below {_fbm_budget_g(spec):.0f} g. Build the box "
            f"at your chosen thickness, then save_component.")


TOY22_FEM_BEAM = Toy(
    "fem_beam_stiffness", "FEM stiffness: cantilever beam deflection within a mass budget",
    components={"beamA": _fbm_task("beamA"), "beamB": _fbm_task("beamB")},
    single_task=(
        f"You will build two steel cantilever beams, one at a time, each fixed at x=0 "
        f"and loaded {FBM_LOAD:.0f} N on top. Size each THICKNESS so the tip stays stiff "
        f"(deflection ~ 1/thickness^3) within its mass budget:\n"
        f"beamA: {FBM['beamA']['L']:.0f}x{FBM_W:.0f} mm, "
        f"mass <= {_fbm_budget_g(FBM['beamA']):.0f} g\n"
        f"beamB: {FBM['beamB']['L']:.0f}x{FBM_W:.0f} mm, "
        f"mass <= {_fbm_budget_g(FBM['beamB']):.0f} g"),
    gate=_fbm_gate,
    reference=lambda w, name, path: _ref_box(
        w, path, name, FBM[name]["L"], FBM_W, FBM[name]["h_ref"]),
    negatives=[
        # beamA too thin (4 mm) -> tip deflection explodes (~1/h^3).
        Neg("beamA_too_thin", "interference",
            agent={"beamA": (f"Build a {FBM['beamA']['L']:.0f}x{FBM_W:.0f}x4 mm steel "
                             f"plate. Then save_component.")},
            ref={"beamA": lambda w, p: _ref_box(w, p, "beamA",
                                                FBM["beamA"]["L"], FBM_W, 4.0)}),
        # beamA too thick (20 mm) -> stiff but over the mass budget.
        Neg("beamA_too_thick", "interference",
            agent={"beamA": (f"Build a {FBM['beamA']['L']:.0f}x{FBM_W:.0f}x20 mm steel "
                             f"plate. Then save_component.")},
            ref={"beamA": lambda w, p: _ref_box(w, p, "beamA",
                                                FBM["beamA"]["L"], FBM_W, 20.0)}),
    ],
)


# =============================================================================
# toy 23: cg_target (family B) — mass / balance. New oracle: assembly centre of
# mass. Three blocks on a lever at fixed arms must balance about x=0; two are given,
# the third's height must be DERIVED so sum(V_i * x_i) = 0. Only the WHOLE balances —
# a pure coupling test (single sees all arms; a partition agent must derive from the
# shared geometry). Gate computes CG from measured block volumes + known arms.
# =============================================================================
CG_S = 20.0                                   # square block cross-section (mm)
CG_POS = {"w_a": -60.0, "w_b": -20.0, "w_c": 50.0}   # lever arm positions (x, mm)
CG_H = {"w_a": 20.0, "w_b": 20.0, "w_c": 32.0}       # heights that balance about 0
CG_TOL = 2.0                                  # allowed |CG_x| (mm)


def _cg_gate(tmp, files):
    num = den = 0.0
    for name, x in CG_POS.items():
        h = _part_volume(files[name]) / (CG_S * CG_S)
        num += h * x
        den += h
    cg_x = num / den if den else 1e9
    if abs(cg_x) > CG_TOL:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"assembly CG_x {cg_x:.1f} mm off balance (> {CG_TOL} mm)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"balanced: CG_x {cg_x:.2f} mm"}


TOY23_CG = Toy(
    "cg_target", "Mass balance: lever blocks whose CG must sit on the pivot",
    components={
        "w_a": (f"Build weight A: a {CG_S:.0f}x{CG_S:.0f}x{CG_H['w_a']:.0f} mm block. "
                f"Then save_component."),
        "w_b": (f"Build weight B: a {CG_S:.0f}x{CG_S:.0f}x{CG_H['w_b']:.0f} mm block. "
                f"Then save_component."),
        "w_c": (f"Build weight C, the balancing counterweight, with a "
                f"{CG_S:.0f}x{CG_S:.0f} mm square base. The three weights sit on a lever "
                f"at arms x: A={CG_POS['w_a']:.0f}, B={CG_POS['w_b']:.0f}, "
                f"C=+{CG_POS['w_c']:.0f} mm, and the assembly must BALANCE about x=0 "
                f"(centre of mass at the pivot). A and B are "
                f"{CG_S:.0f}x{CG_S:.0f}x{CG_H['w_a']:.0f} mm. All blocks share the same "
                f"square base, so mass is proportional to height — compute your block's "
                f"HEIGHT so sum(height*arm) = 0, then build it and save_component."),
    },
    single_task=(
        f"You will build three lever weights (same {CG_S:.0f}x{CG_S:.0f} mm base), one "
        f"at a time, that must BALANCE about x=0 (CG at the pivot). Arms: "
        f"A={CG_POS['w_a']:.0f}, B={CG_POS['w_b']:.0f}, C=+{CG_POS['w_c']:.0f} mm. A and "
        f"B are {CG_H['w_a']:.0f} mm tall; choose C's height so sum(height*arm)=0."),
    gate=_cg_gate,
    reference=lambda w, name, path: _ref_box(w, path, name, CG_S, CG_S, CG_H[name]),
    negatives=[
        # counterweight built the same as A/B (forgot to balance) -> CG off the pivot.
        Neg("w_c_unbalanced", "interference",
            agent={"w_c": (f"Build a {CG_S:.0f}x{CG_S:.0f}x{CG_H['w_a']:.0f} mm block. "
                           f"Then save_component.")},
            ref={"w_c": lambda w, p: _ref_box(w, p, "w_c", CG_S, CG_S, CG_H["w_a"])}),
    ],
)


# =============================================================================
# toys 24-25: fastening (family C) — measured-geometry fit gates.
# press_fit: a shaft that must be slightly LARGER than its bore (interference in a
#   holding band) — the inverse of the clearance peg; too loose slips, too tight cracks.
# thread_engagement: a bolt + tapped plate where the tap-drill bore must match the
#   thread's minor diameter (a clearance-sized hole won't grip) and the engagement
#   length must be >= 0.8*D.
# =============================================================================

def _part_bbox(path):
    """Bounding-box extents (dx, dy, dz) mm of the top-level solid in a saved part."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        r = w.call("run_script", code='''
objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull() and not o.InList]
if not objs:
    objs=[o for o in App.ActiveDocument.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]
bb=objs[0].Shape.BoundBox
__result__=[round(bb.XLength,4),round(bb.YLength,4),round(bb.ZLength,4)]
''')
    return r["result"]


# --- toy 24: press_fit (interference in a holding band) ----------------------
PF_NOM, PF_LO, PF_HI = 20.0, 0.02, 0.06       # nominal Ø; interference band (mm)
PF_SHAFT_D, PF_BORE_D = 20.04, 20.00          # reference: 0.04 mm interference


def _pf_gate(tmp, files):
    rs = max(_cyl_faces(files["shaft"]), key=lambda c: c["r"])["r"]
    rb = min(_cyl_faces(files["hub"]), key=lambda c: c["r"])["r"]
    interf = 2 * (rs - rb)                      # shaft Ø - bore Ø
    if interf < PF_LO:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"interference {interf:.3f} mm < {PF_LO} — loose, will slip"}
    if interf > PF_HI:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"interference {interf:.3f} mm > {PF_HI} — too tight, hub cracks"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"press fit: {interf:.3f} mm interference (in {PF_LO}-{PF_HI} band)"}


TOY24_PRESSFIT = Toy(
    "press_fit", "Press fit: shaft interference in a holding band",
    components={
        "shaft": (f"Build a cylindrical SHAFT for a PRESS fit into a Ø{PF_NOM:.0f} mm "
                  f"bore. A press fit needs the shaft slightly LARGER than the bore: "
                  f"target interference {PF_LO}-{PF_HI} mm on diameter, so make the "
                  f"shaft about Ø{PF_SHAFT_D:.2f} mm, 30 mm long. Then save_component."),
        "hub": (f"Build a HUB: a 40x40x20 mm block with a Ø{PF_BORE_D:.2f} mm bore "
                f"through it (the nominal Ø{PF_NOM:.0f} bore the shaft presses into). "
                f"Then save_component."),
    },
    single_task=(
        f"You will build a SHAFT and a HUB for a PRESS fit, one at a time. The hub bore "
        f"is Ø{PF_BORE_D:.2f} mm; the shaft must be larger by {PF_LO}-{PF_HI} mm "
        f"(interference) so it presses in and grips — about Ø{PF_SHAFT_D:.2f} mm shaft, "
        f"30 mm long; hub 40x40x20 mm."),
    gate=_pf_gate,
    reference=lambda w, name, path: (
        _ref_cyl(w, path, "shaft", PF_SHAFT_D / 2, 30.0) if name == "shaft"
        else _ref_box_holes(w, path, "hub", 40, 40, 20, PF_BORE_D / 2, [(20, 20)])),
    negatives=[
        # shaft undersized -> clearance, not interference (slips out).
        Neg("shaft_loose", "interference",
            agent={"shaft": (f"Build a Ø{PF_NOM-0.04:.2f} mm shaft, 30 mm long. Then "
                             f"save_component.")},
            ref={"shaft": lambda w, p: _ref_cyl(w, p, "shaft", (PF_NOM - 0.04) / 2, 30.0)}),
        # shaft way oversize -> excessive interference (cracks the hub).
        Neg("shaft_too_tight", "interference",
            agent={"shaft": (f"Build a Ø{PF_NOM+0.2:.2f} mm shaft, 30 mm long. Then "
                             f"save_component.")},
            ref={"shaft": lambda w, p: _ref_cyl(w, p, "shaft", (PF_NOM + 0.2) / 2, 30.0)}),
    ],
)


# --- toy 25: thread_engagement (tap-drill match + engagement length) ---------
TE_D, TE_PITCH = 8.0, 1.25                     # M8 x 1.25
TE_MINOR = round(TE_D - 1.0825 * TE_PITCH, 2)  # thread minor ~ tap-drill Ø (6.65)
TE_MIN_ENGAGE = 0.8 * TE_D                      # 6.4 mm minimum thread engagement


def _te_gate(tmp, files):
    bolt_d = 2 * max(_cyl_faces(files["bolt"]), key=lambda c: c["r"])["r"]
    hole_d = 2 * min(_cyl_faces(files["plate"]), key=lambda c: c["r"])["r"]
    engage = _part_bbox(files["plate"])[2]      # tapped depth = plate thickness
    if abs(bolt_d - TE_D) > 0.3:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"bolt Ø{bolt_d:.2f} != M{TE_D:.0f} major"}
    if abs(hole_d - TE_MINOR) > 0.5:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"tapped hole Ø{hole_d:.2f} != minor Ø{TE_MINOR:.2f} "
                          f"(a clearance-sized hole won't grip threads)"}
    if engage < TE_MIN_ENGAGE - 0.1:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"engagement {engage:.1f} mm < {TE_MIN_ENGAGE:.1f} (0.8*D)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"M{TE_D:.0f} bolt grips tap-drill Ø{hole_d:.1f} over {engage:.0f} mm"}


TOY25_THREAD = Toy(
    "thread_engagement", "Thread engagement: tap-drill match + engagement length",
    components={
        "bolt": (f"Build a BOLT shank for an M{TE_D:.0f}x{TE_PITCH} thread: a cylinder "
                 f"Ø{TE_D:.0f} mm (the major/nominal diameter), 16 mm long. Then "
                 f"save_component."),
        "plate": (f"Build a TAPPED PLATE: a 30x30x10 mm block with a hole drilled "
                  f"through for an M{TE_D:.0f}x{TE_PITCH} thread. The tap-drill hole "
                  f"must equal the thread MINOR diameter (~Ø{TE_MINOR:.2f} mm) so the "
                  f"threads grip — NOT a clearance hole. Then save_component."),
    },
    single_task=(
        f"You will build a BOLT and a TAPPED PLATE for an M{TE_D:.0f}x{TE_PITCH} thread, "
        f"one at a time. Bolt: Ø{TE_D:.0f} mm shank, 16 mm. Plate: 30x30x10 mm with a "
        f"tap-drill hole at the thread minor Ø (~{TE_MINOR:.2f} mm, not a clearance "
        f"hole); engagement (plate thickness) must be >= {TE_MIN_ENGAGE:.1f} mm."),
    gate=_te_gate,
    reference=lambda w, name, path: (
        _ref_cyl(w, path, "bolt", TE_D / 2, 16.0) if name == "bolt"
        else _ref_box_holes(w, path, "plate", 30, 30, 10, TE_MINOR / 2, [(15, 15)])),
    negatives=[
        # plate drilled to clearance Ø instead of tap-drill -> threads can't grip.
        Neg("clearance_hole", "interference",
            agent={"plate": (f"Build a 30x30x10 mm block with a Ø{TE_D+1:.1f} mm "
                             f"clearance hole through it. Then save_component.")},
            ref={"plate": lambda w, p: _ref_box_holes(
                w, p, "plate", 30, 30, 10, (TE_D + 1) / 2, [(15, 15)])}),
        # plate too thin -> not enough thread engagement.
        Neg("too_shallow", "interference",
            agent={"plate": (f"Build a 30x30x4 mm block with a Ø{TE_MINOR:.2f} mm "
                             f"tap-drill hole through it. Then save_component.")},
            ref={"plate": lambda w, p: _ref_box_holes(
                w, p, "plate", 30, 30, 4, TE_MINOR / 2, [(15, 15)])}),
    ],
)


# =============================================================================
# toys 26-28: advanced kinematics (family D).
# rack_pinion: rack linear tooth pitch must equal the pinion circular pitch (pi*m).
# fourbar_crankrocker: Grashof condition + the crank (input) is the shortest link.
# cam_follower: eccentric cam lift (2*offset) must match the follower's travel.
# =============================================================================
import math as _m2  # noqa: E402


# --- toy 26: rack_pinion (mesh: rack pitch = pinion circular pitch) ----------
RP_M, RP_N = 2.5, 20
RP_CP = _m2.pi * RP_M                          # circular pitch ~ 7.854 mm


def _rp_gate(tmp, files):
    rp_pitch_r = _gear_tip_radius(files["pinion"]) - RP_M
    if abs(rp_pitch_r - RP_M * RP_N / 2) > 0.6:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"pinion pitch r {rp_pitch_r:.1f} != {RP_M*RP_N/2:.1f} mm"}
    xs = sorted(c["cx"] for c in _cyl_faces(files["rack"]))
    if len(xs) < 2:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": "rack has < 2 teeth to measure pitch"}
    pitch = sum(xs[i + 1] - xs[i] for i in range(len(xs) - 1)) / (len(xs) - 1)
    if abs(pitch - RP_CP) > 0.3:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"rack pitch {pitch:.2f} != pinion circular pitch "
                          f"pi*m {RP_CP:.2f} mm (won't mesh)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"rack pitch {pitch:.2f} = pi*m, meshes the {RP_N}T pinion"}


def _ref_rack(w, path, name, pitch, n=5):
    centers = [(10.0 + i * pitch, 10.0) for i in range(n)]
    _ref_box_holes(w, path, name, 10.0 + n * pitch + 10, 20.0, 10.0, 2.0, centers)


TOY26_RACKPINION = Toy(
    "rack_pinion", "Rack and pinion (rack pitch = pinion circular pitch)",
    components={
        "pinion": (f"Build the PINION: an involute gear, module {RP_M:g} mm, {RP_N} "
                   f"teeth. add_gear(teeth={RP_N}, module={RP_M:g}, height=6). Then "
                   f"save_component."),
        "rack": (f"Build the RACK: a flat toothed bar. Its tooth PITCH must equal the "
                 f"pinion's circular pitch = pi*module = pi*{RP_M:g} = {RP_CP:.3f} mm so "
                 f"they mesh. Model the teeth as a row of markers Ø4 mm spaced "
                 f"{RP_CP:.3f} mm apart along the bar. Then save_component."),
    },
    single_task=(
        f"You will build a rack and pinion (module {RP_M:g} mm), one part at a time. "
        f"PINION: {RP_N}-tooth involute gear (add_gear). RACK: a bar whose tooth pitch "
        f"equals the pinion circular pitch pi*module = {RP_CP:.3f} mm (model teeth as Ø4 "
        f"markers at that spacing). They mesh only if the pitches match."),
    gate=_rp_gate,
    reference=lambda w, name, path: (
        _ref_gear(w, path, "pinion", RP_N, module=RP_M) if name == "pinion"
        else _ref_rack(w, path, "rack", RP_CP)),
    negatives=[
        # rack pitch doesn't match the pinion -> teeth bind / skip.
        Neg("rack_wrong_pitch", "interference",
            agent={"rack": "Build a bar with Ø4 markers spaced 6.0 mm apart. Then save_component."},
            ref={"rack": lambda w, p: _ref_rack(w, p, "rack", 6.0)}),
    ],
)


# --- toy 27: fourbar_crankrocker (Grashof + crank is shortest) ---------------
FOURBAR = {"ground": 100.0, "crank": 30.0, "coupler": 90.0, "rocker": 80.0}


def _fourbar_lengths(files):
    out = {}
    for name in FOURBAR:
        h = _cyl_faces(files[name])
        out[name] = _m2.hypot(h[0]["cx"] - h[1]["cx"], h[0]["cy"] - h[1]["cy"])
    return out


def _fourbar_gate(tmp, files):
    L = _fourbar_lengths(files)
    vals = sorted(L.values())
    s, l = vals[0], vals[-1]
    if s + l > vals[1] + vals[2] + 0.5:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"not Grashof: shortest+longest {s+l:.0f} > other two "
                          f"{vals[1]+vals[2]:.0f} (no continuous rotation)"}
    if abs(L["crank"] - s) > 0.5:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"crank {L['crank']:.0f} is not the shortest link {s:.0f} "
                          f"(won't be a crank-rocker)"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"Grashof crank-rocker (crank {L['crank']:.0f} = shortest)"}


TOY27_FOURBAR = Toy(
    "fourbar_crankrocker", "Four-bar crank-rocker (Grashof + crank shortest)",
    components={n: (f"Build the {n.upper()} link of a four-bar linkage: a flat bar with "
                    f"two pivot holes (radius 4 mm) {int(L)} mm apart. Then save_component.")
                for n, L in FOURBAR.items()},
    single_task=(
        "You will build the four links of a crank-rocker four-bar, one at a time, each a "
        "bar with two pivot holes at the stated spacing: "
        + ", ".join(f"{n}={int(L)} mm" for n, L in FOURBAR.items())
        + ". For a crank-rocker the CRANK must be the shortest link and the set must "
          "satisfy Grashof (shortest+longest <= other two)."),
    gate=_fourbar_gate,
    reference=lambda w, name, path: _ref_two_hole_bar(w, path, name, FOURBAR[name]),
    negatives=[
        # crank made longest -> breaks Grashof and isn't the shortest link.
        Neg("crank_too_long", "interference",
            agent={"crank": "Build a bar with two holes (radius 4 mm) 120 mm apart. Then save_component."},
            ref={"crank": lambda w, p: _ref_two_hole_bar(w, p, "crank", 120.0)}),
    ],
)


# --- toy 28: cam_follower (eccentric cam lift matches follower travel) -------
CAM_R, CAM_E = 30.0, 8.0                        # cam radius, eccentricity -> lift 2e
CAM_LIFT = 2 * CAM_E


def _ref_cam(w, path, name, R=CAM_R, e=CAM_E, bore_r=5.0, h=10.0):
    w.call("new_document", name=name)
    disk = w.call("add_primitive", kind="cylinder", r=R, h=h, name=name)
    bore = w.call("add_primitive", kind="cylinder", r=bore_r, h=h * 3,
                  placement=[e, 0, -h], name="bore")
    w.call("boolean_op", op="cut", base=disk["handle"], tool=bore["handle"])
    w.call("save_document", path=str(path))


def _cam_gate(tmp, files):
    cf = _cyl_faces(files["cam"])
    outer = max(cf, key=lambda c: c["r"])
    bore = min(cf, key=lambda c: c["r"])
    e = _m2.hypot(outer["cx"] - bore["cx"], outer["cy"] - bore["cy"])
    lift = 2 * e
    fh = _cyl_faces(files["follower"])
    travel = _m2.hypot(fh[0]["cx"] - fh[1]["cx"], fh[0]["cy"] - fh[1]["cy"])
    if abs(lift - CAM_LIFT) > 0.6:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"cam lift {lift:.1f} != target {CAM_LIFT:.0f} mm (eccentricity off)"}
    if abs(travel - lift) > 0.8:
        return {"ok": False, "interference": [], "envelope": [],
                "reason": f"follower travel {travel:.1f} != cam lift {lift:.1f} mm"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": f"cam lift {lift:.0f} mm matches follower travel {travel:.0f} mm"}


TOY28_CAM = Toy(
    "cam_follower", "Cam-follower (eccentric cam lift matches follower travel)",
    components={
        "cam": (f"Build a CAM: a disk radius {CAM_R:g} mm, 10 mm thick, with its bore "
                f"(rotation axis, radius 5 mm) offset {CAM_E:g} mm from the disk centre. "
                f"Rotating it gives a follower lift of 2*offset = {CAM_LIFT:g} mm. Then "
                f"save_component."),
        "follower": (f"Build a FOLLOWER guide: a bar with two holes (radius 4 mm) marking "
                     f"the follower's travel limits, spaced by the cam's lift = "
                     f"{CAM_LIFT:g} mm. Then save_component."),
    },
    single_task=(
        f"You will build a cam and its follower guide, one at a time. CAM: disk radius "
        f"{CAM_R:g} mm with the bore offset {CAM_E:g} mm from centre (lift = 2*offset = "
        f"{CAM_LIFT:g} mm). FOLLOWER: a bar with two travel-limit holes spaced by that "
        f"lift ({CAM_LIFT:g} mm). They must agree."),
    gate=_cam_gate,
    reference=lambda w, name, path: (
        _ref_cam(w, path, "cam") if name == "cam"
        else _ref_two_hole_bar(w, path, "follower", CAM_LIFT)),
    negatives=[
        # cam eccentricity wrong -> lift no longer matches the follower travel.
        Neg("cam_wrong_lift", "interference",
            agent={"cam": "Build a disk radius 30 mm, 10 mm thick, with the bore offset 4 mm from centre. Then save_component."},
            ref={"cam": lambda w, p: _ref_cam(w, p, "cam", e=4.0)}),
    ],
)


# =============================================================================
# toy 29: thermo_structural (capstone, family F) — coupled multi-physics.
# A heat-sink bracket carrying a hot component (fixed heat flux on top) AND a
# mechanical load must BOTH stay below a temperature limit (thermal FEM) AND below a
# stress limit (structural FEM), within a mass budget. Sizing the thickness couples
# all three: too thin -> runs hot AND over-stresses; too thick -> over the mass
# budget. Runs both solvers on the same agent geometry, gated relative to a reference.
# (Genuine thermal-vs-structural shape tradeoffs — fins — are a noted extension.)
# =============================================================================
TS_W, TS_LOAD, TS_FLUX = 30.0, 2000.0, 15000.0
TS = {"sinkA": {"L": 60.0, "h_ref": 12.0}, "sinkB": {"L": 80.0, "h_ref": 14.0}}
TS_FEM_S = {"fix_normal": [-1, 0, 0], "load_normal": [0, 0, 1], "force_n": TS_LOAD}
TS_FEM_T = {"heat_normal": [0, 0, 1], "flux_w_m2": TS_FLUX}
TS_TEMP_MARGIN, TS_VM_MARGIN, TS_MASS_MARGIN = 1.15, 1.6, 1.25


def _ts_budget_g(spec):
    return spec["L"] * TS_W * spec["h_ref"] * _FEM_DENSITY * TS_MASS_MARGIN * 1000.0


def _ts_gate(tmp, files):
    for name, spec in TS.items():
        bref = lambda p, s=spec, n=name: _box_file(p, n, s["L"], TS_W, s["h_ref"])
        s_ref = _fem_ref(f"ts_s:{name}", bref, TS_FEM_S)
        t_ref = _fem_ref(f"ts_t:{name}", bref, TS_FEM_T, fn=_fem_thermal)
        s = _fem_stress(files[name], **TS_FEM_S)
        t = _fem_thermal(files[name], **TS_FEM_T)
        mass_g = _part_volume(files[name]) * _FEM_DENSITY * 1000.0
        if t["max_temp_c"] > t_ref["max_temp_c"] * TS_TEMP_MARGIN:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: runs hot, {t['max_temp_c']:.0f} > "
                              f"{t_ref['max_temp_c']*TS_TEMP_MARGIN:.0f} °C (too thin to conduct)"}
        if s["vm_mpa"] > s_ref["vm_mpa"] * TS_VM_MARGIN:
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: over-stressed, {s['vm_mpa']:.3f} > "
                              f"{s_ref['vm_mpa']*TS_VM_MARGIN:.3f} MPa"}
        if mass_g > _ts_budget_g(spec):
            return {"ok": False, "interference": [], "envelope": [],
                    "reason": f"{name}: mass {mass_g:.0f} g > budget {_ts_budget_g(spec):.0f} g"}
    return {"ok": True, "interference": [], "envelope": [],
            "reason": "both heat-sinks cool enough and strong enough within mass budget"}


def _ts_task(name):
    spec = TS[name]
    return (f"Build a HEAT-SINK BRACKET: a steel plate {spec['L']:.0f} mm long (X) by "
            f"{TS_W:.0f} mm wide (Y), fixed at the wall (x=0 face). It carries a hot "
            f"component on top (steady heat into the top face) AND a {TS_LOAD:.0f} N "
            f"mechanical load on top. Choose the THICKNESS (Z) so it BOTH stays cool "
            f"(a thicker plate conducts heat away and runs cooler) AND is strong enough "
            f"(thicker bends/stresses less) — while keeping mass at or below "
            f"{_ts_budget_g(spec):.0f} g. Build the box at your thickness, then "
            f"save_component.")


TOY29_THERMO_STRUCT = Toy(
    "thermo_structural", "Coupled thermo-structural heat-sink (cool + strong + mass budget)",
    components={"sinkA": _ts_task("sinkA"), "sinkB": _ts_task("sinkB")},
    single_task=(
        f"You will build two heat-sink brackets, one at a time, each fixed at x=0, with "
        f"a hot component + {TS_LOAD:.0f} N load on top. Size each THICKNESS so it stays "
        f"cool (thicker conducts heat away) AND strong (thicker stresses less) within "
        f"its mass budget:\n"
        f"sinkA: {TS['sinkA']['L']:.0f}x{TS_W:.0f} mm, mass <= {_ts_budget_g(TS['sinkA']):.0f} g\n"
        f"sinkB: {TS['sinkB']['L']:.0f}x{TS_W:.0f} mm, mass <= {_ts_budget_g(TS['sinkB']):.0f} g"),
    gate=_ts_gate,
    reference=lambda w, name, path: _ref_box(
        w, path, name, TS[name]["L"], TS_W, TS[name]["h_ref"]),
    negatives=[
        # too thin -> runs hot AND over-stresses (fails the physics).
        Neg("sinkA_too_thin", "interference",
            agent={"sinkA": (f"Build a {TS['sinkA']['L']:.0f}x{TS_W:.0f}x5 mm steel "
                             f"plate. Then save_component.")},
            ref={"sinkA": lambda w, p: _ref_box(w, p, "sinkA",
                                                TS["sinkA"]["L"], TS_W, 5.0)}),
        # too thick -> cool and strong but over the mass budget.
        Neg("sinkA_too_thick", "interference",
            agent={"sinkA": (f"Build a {TS['sinkA']['L']:.0f}x{TS_W:.0f}x24 mm steel "
                             f"plate. Then save_component.")},
            ref={"sinkA": lambda w, p: _ref_box(w, p, "sinkA",
                                                TS["sinkA"]["L"], TS_W, 24.0)}),
    ],
)


TOYS = {t.key: t for t in (TOY1, TOY2, TOY3, TOY4,
                           TOY5_NSLOT4, TOY5_NSLOT6, TOY5_NSLOT8,
                           TOY6_TCHAIN3, TOY6_TCHAIN6,
                           TOY7_TCHAINU, TOY8_PINSLOT,
                           TOY9_POSITION, TOY10_CONCENTRIC, TOY11_SYMMETRY,
                           TOY12_ANGULARITY,
                           TOY13_GEARBOX6, TOY14_GEARBOX3, TOY15_PLANETARY,
                           TOY16_ACKERMANN, TOY17_SLIDERCRANK,
                           TOY18_GENEVA, TOY19_SARRUS, TOY20_WISHBONE,
                           TOY21_FEM_BRACKET, TOY22_FEM_BEAM,
                           TOY23_CG, TOY24_PRESSFIT, TOY25_THREAD,
                           TOY26_RACKPINION, TOY27_FOURBAR, TOY28_CAM,
                           TOY29_THERMO_STRUCT)}


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

            conds = (("partition", run_partition), ("single", run_single))
            sel_cond = os.environ.get("M2_COND")  # e.g. "single" — rerun one condition
            if sel_cond:
                conds = [(n, f) for n, f in conds if n in sel_cond.split(",")]
            for cond_name, fn in conds:
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

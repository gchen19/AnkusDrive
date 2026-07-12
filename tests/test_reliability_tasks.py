"""
Layer D reliability — tool-use correctness, multi-trial, multi-model.

Layers A/B/C test *visual judgment under spec* (can the model SEE a divergence
in a render). This layer tests the other half: *can the model DRIVE the tools to
produce geometry that meets a goal*. The agent gets an English design goal and
the DriftPin tool surface, runs a real tool-use loop against a real FreeCAD
worker, then we grade the resulting SOLID with DriftPin's own Tier-3 inspection
tools (bounding_box / check_shape / min_clearance / mass_properties) as the
ground-truth oracle. We never assert *which* tools it called — only that the
artifact is correct. That tolerates the many valid tool paths to one result.

Three things make this a benchmark rather than a single green checkmark:
  - multi-trial: each (model, task) runs N times -> a PASS RATE, not pass/fail.
    LLMs are stochastic; one run tells you almost nothing.
  - multi-model: the same task suite runs across haiku/sonnet/opus so you can
    compare reliability, turns, and $ side by side.
  - failure capture: every miss stores the tool trace + the oracle's reason, so
    you can see WHERE it went wrong (wrong units, hallucinated handle, gave up).

Reuses orchestration/agentkit.py for the proven cached tool-use loop, the model
table, pricing, and the ScriptedClient (so the harness itself runs for FREE in
dry mode — that path is also the negative-control / self-test of the grader).

Usage:
    # free: exercise the harness + grader plumbing with the scripted stub
    .venv/bin/python3 tests/test_reliability_tasks.py

    # real, one model:
    ANTHROPIC_API_KEY=sk-... RUN_RELIABILITY=1 \
        .venv/bin/python3 tests/test_reliability_tasks.py

    # real, compare models (and bump trials):
    ANTHROPIC_API_KEY=sk-... RUN_RELIABILITY=1 \
        DRIFTPIN_MODELS=haiku,sonnet,opus DRIFTPIN_TRIALS=8 \
        .venv/bin/python3 tests/test_reliability_tasks.py
"""
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from orchestration import agentkit  # noqa: E402

CACHE_DIR = REPO / "tests" / "reliability_cache"
MAX_TURNS = 16


# --- tool surface ------------------------------------------------------------
# agentkit's base surface (new_document, add_primitive, boolean_op,
# mass_properties, publish_interface, save_component) plus a couple of Tier-1/3
# commands so tasks can exercise the *new* features and the agent can self-verify.
# The worker dispatches any registered handler by name, so extending the surface
# is purely a matter of adding schemas here.
EXTRA_TOOLS = [
    {"name": "add_gear",
     "description": ("Involute spur gear extruded to a solid. teeth (int>=3), module (mm), "
                     "height (mm). Returns {handle, pitch_radius, tip_radius, root_radius}."),
     "input_schema": {"type": "object", "properties": {
         "teeth": {"type": "integer"}, "module": {"type": "number"},
         "height": {"type": "number"}}, "required": ["teeth", "module", "height"]}},
    {"name": "bounding_box",
     "description": ("AABB of a handle. Returns {min,max,size:[x,y,z],center,diagonal}. "
                     "A measurement — no handle returned. Use to self-check extents."),
     "input_schema": {"type": "object", "properties": {"handle": {"type": "string"}},
                      "required": ["handle"]}},
    {"name": "check_shape",
     "description": ("Validity check of a handle. Returns {valid, watertight_solid, solids, "
                     "volume_mm3, ...}. Use to confirm you built one clean solid."),
     "input_schema": {"type": "object", "properties": {"handle": {"type": "string"}},
                      "required": ["handle"]}},
]
TOOLS = agentkit.TOOLS + EXTRA_TOOLS


# --- task definitions --------------------------------------------------------
# A checker receives the live worker and the tool trace AFTER the loop, and
# returns (passed, detail). It uses the inspection tools as the oracle. Checkers
# are tolerant by design (real geometry has rounding); they assert the property
# the goal names, not an exact byte pattern.

@dataclass
class Task:
    name: str
    tier: int           # 1 single-tool, 2 short-chain, 3 component, 4 multi-part
    prompt: str
    checker: Callable    # (worker, trace) -> (bool, str)


def _handles(trace):
    """Handles produced by build calls, in order (skips measurement results)."""
    return [r["result"]["handle"] for r in trace
            if not r["error"] and isinstance(r["result"], dict) and "handle" in r["result"]]


def _last_handle(trace):
    hs = _handles(trace)
    return hs[-1] if hs else None


def _approx(a, b, tol):
    return abs(a - b) <= tol


def check_cube20(w, trace):
    h = _last_handle(trace)
    if not h:
        return False, "no solid was built"
    bb = w.call("bounding_box", handle=h)
    cs = w.call("check_shape", handle=h)
    sx, sy, sz = bb["size"]
    ok = (cs["watertight_solid"] and cs["solids"] == 1
          and all(_approx(s, 20.0, 0.1) for s in (sx, sy, sz)))
    return ok, f"size={bb['size']} watertight={cs['watertight_solid']} solids={cs['solids']}"


def check_block_with_hole(w, trace):
    h = _last_handle(trace)
    if not h:
        return False, "no solid was built"
    bb = w.call("bounding_box", handle=h)
    cs = w.call("check_shape", handle=h)
    sx, sy, sz = bb["size"]
    expected_vol = 40 * 40 * 20 - math.pi * 5 ** 2 * 20   # ~30429
    bbox_ok = _approx(sx, 40, 0.5) and _approx(sy, 40, 0.5) and _approx(sz, 20, 0.5)
    vol_ok = _approx(cs["volume_mm3"], expected_vol, expected_vol * 0.05)
    ok = bbox_ok and vol_ok and cs["watertight_solid"]
    return ok, (f"size={bb['size']} vol={cs['volume_mm3']:.0f} "
                f"(want~{expected_vol:.0f}) watertight={cs['watertight_solid']}")


def check_gear(w, trace):
    h = _last_handle(trace)
    if not h:
        return False, "no solid was built"
    bb = w.call("bounding_box", handle=h)
    cs = w.call("check_shape", handle=h)
    sx, sy, sz = bb["size"]
    tip_dia = 2 * (24 + 2)  # tip diameter = module*(teeth+2) = 2*26 = 52
    ok = (cs["watertight_solid"] and _approx(sz, 10, 0.5)
          and _approx(sx, tip_dia, 3.0) and _approx(sy, tip_dia, 3.0))
    return ok, (f"size={bb['size']} (want xy~{tip_dia}, z~10) "
                f"watertight={cs['watertight_solid']}")


def check_two_cubes_clear(w, trace):
    hs = _handles(trace)
    cubes = hs[:2]
    if len(cubes) < 2:
        return False, f"expected 2 solids, got {len(hs)}"
    mc = w.call("min_clearance", a=cubes[0], b=cubes[1])
    ok = mc["status"] == "clear" and _approx(mc.get("clearance_mm", -1), 5.0, 0.5)
    return ok, f"status={mc['status']} clearance={mc.get('clearance_mm')} (want clear, ~5)"


TASKS = [
    Task("cube20", 1,
         "Build a single solid cube, 20 mm on every side.",
         check_cube20),
    Task("gear_m2_t24", 3,
         "Build a spur gear: module 2, 24 teeth, 10 mm thick. Use the gear tool.",
         check_gear),
    Task("block_hole", 2,
         "Build a 40x40x20 mm block, then cut a 10 mm diameter hole straight "
         "through its center along Z.",
         check_block_with_hole),
    Task("two_cubes_gap", 4,
         "Build two separate 20 mm cubes positioned so there is a 5 mm gap "
         "between them along X (they must not touch or overlap).",
         check_two_cubes_clear),
]


# --- agent loop (keeps the worker open so the checker can inspect live) -------

def _prep_tools(tools):
    cached = [dict(t) for t in tools]
    cached[-1] = {**cached[-1], "cache_control": {"type": "ephemeral"}}
    return cached


def run_trial(client, model, task, save_path):
    """One end-to-end attempt. Returns dict with passed/detail/turns/cost/trace."""
    tools = _prep_tools(TOOLS)
    sys_blocks = agentkit._cached_system(agentkit.BUILDER_SYSTEM)
    trace = []
    usage = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0}
    saved = False
    turn = 0
    with Worker() as w:
        messages = [{"role": "user", "content": task.prompt}]
        for turn in range(MAX_TURNS):
            agentkit._mark_conversation_cache(messages)
            resp = client.messages.create(
                model=model, max_tokens=1024, system=sys_blocks,
                tools=tools, messages=messages)
            usage["in_tokens"] += resp.usage.input_tokens
            usage["out_tokens"] += resp.usage.output_tokens
            usage["cache_read"] += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                try:
                    out = agentkit._dispatch(w, save_path, tu.name, dict(tu.input))
                    trace.append({"name": tu.name, "args": dict(tu.input),
                                  "result": out, "error": False})
                    if tu.name == "save_component":
                        saved = True
                    payload, is_err = json.dumps(out), False
                except Exception as e:
                    trace.append({"name": tu.name, "args": dict(tu.input),
                                  "result": f"{type(e).__name__}: {e}", "error": True})
                    payload, is_err = f"ERROR: {type(e).__name__}: {e}", True
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": payload, "is_error": is_err})
            messages.append({"role": "user", "content": results})
            if saved:
                break

        # Grade against the LIVE worker before it closes.
        try:
            passed, detail = task.checker(w, trace)
        except Exception as e:
            passed, detail = False, f"checker raised {type(e).__name__}: {e}"

    return {
        "passed": passed, "detail": detail, "built": saved,
        "turns": turn + 1,
        "cost_usd": agentkit.cost_of(usage, model) if model in agentkit.PRICING else 0.0,
        "trace": [{"name": t["name"], "args": t["args"],
                   "error": t["error"]} for t in trace],
    }


# --- multi-trial / multi-model runner ----------------------------------------

def run_suite(client, models, trials):
    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    report = {"trials": trials, "models": models, "cells": {}, "failures": []}
    with tempfile.TemporaryDirectory() as tmp:
        for model in models:
            for task in TASKS:
                results = []
                for i in range(trials):
                    save_path = Path(tmp) / f"{model}_{task.name}_{i}.FCStd"
                    r = run_trial(client, model, task, save_path)
                    results.append(r)
                    if not r["passed"]:
                        report["failures"].append({
                            "model": model, "task": task.name, "trial": i,
                            "detail": r["detail"], "built": r["built"],
                            "trace": [t["name"] for t in r["trace"]],
                        })
                npass = sum(1 for r in results if r["passed"])
                report["cells"][f"{model}/{task.name}"] = {
                    "pass": npass, "n": trials, "rate": npass / trials,
                    "avg_turns": sum(r["turns"] for r in results) / trials,
                    "avg_cost": sum(r["cost_usd"] for r in results) / trials,
                    "tier": task.tier,
                }
    (CACHE_DIR / "report_tasks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_report(report):
    models, trials = report["models"], report["trials"]
    print("\n" + "=" * 78)
    print(f"Layer D — LLM-drives-MCP reliability  ({trials} trials/cell)")
    print("=" * 78)
    name_w = max(len(t.name) for t in TASKS) + 2
    header = "task".ljust(name_w) + "tier  " + "".join(m.ljust(14) for m in models)
    print(header)
    print("-" * len(header))
    for task in TASKS:
        row = task.name.ljust(name_w) + f"  {task.tier}   "
        for m in models:
            c = report["cells"][f"{m}/{task.name}"]
            row += f"{c['pass']}/{c['n']} ({c['rate']:.0%})".ljust(14)
        print(row)
    print("-" * len(header))
    # per-model rollups
    print("\nper-model rollup:")
    for m in models:
        cells = [c for k, c in report["cells"].items() if k.startswith(m + "/")]
        passed = sum(c["pass"] for c in cells)
        total = sum(c["n"] for c in cells)
        cost = sum(c["avg_cost"] * c["n"] for c in cells)
        turns = sum(c["avg_turns"] for c in cells) / len(cells)
        print(f"  {m:<8}  reliability {passed}/{total} = {passed/total:.0%}   "
              f"avg turns {turns:.1f}   total ${cost:.3f}")
    if report["failures"]:
        print(f"\n{len(report['failures'])} failure(s) (see report_tasks.json for traces):")
        for f in report["failures"][:12]:
            print(f"  [{f['model']}/{f['task']}#{f['trial']}] built={f['built']} "
                  f"{f['detail']}  trace={f['trace']}")


# --- free dry-run script: also the grader's negative control -----------------
# The ScriptedClient drives a deterministic, correct build per task so the dry
# run goes green -> proves the loop, trace, oracle, and reporting all work with
# zero API cost. selftest_grader() then feeds a deliberately-WRONG build and
# asserts the oracle REJECTS it, so we know the checkers actually discriminate.

def _dry_script(task_text, turn):
    t = task_text.lower()
    T = agentkit.tool_use
    if "gear" in t:
        steps = [[T("a", "new_document", {"name": "d"})],
                 [T("b", "add_gear", {"teeth": 24, "module": 2, "height": 10})],
                 [T("c", "save_component", {})]]
    elif "hole" in t:
        steps = [[T("a", "new_document", {"name": "d"})],
                 [T("b", "add_primitive", {"kind": "box", "w": 40, "d": 40, "h": 20})],
                 [T("c", "add_primitive", {"kind": "cylinder", "r": 5, "h": 20,
                                           "placement": [20, 20, 0]})],
                 [T("d", "boolean_op", {"op": "cut", "base": "box_1", "tool": "cylinder_1"})],
                 [T("e", "save_component", {})]]
    elif "two" in t:
        steps = [[T("a", "new_document", {"name": "d"})],
                 [T("b", "add_primitive", {"kind": "box", "w": 20, "d": 20, "h": 20})],
                 [T("c", "add_primitive", {"kind": "box", "w": 20, "d": 20, "h": 20,
                                           "placement": [25, 0, 0]})],
                 [T("d", "save_component", {})]]
    else:  # cube
        steps = [[T("a", "new_document", {"name": "d"})],
                 [T("b", "add_primitive", {"kind": "box", "w": 20, "d": 20, "h": 20})],
                 [T("c", "save_component", {})]]
    return steps[turn] if turn < len(steps) else [agentkit.text_block("done")]


def selftest_grader():
    """Negative control: a wrong build (25mm cube) must FAIL check_cube20."""
    with tempfile.TemporaryDirectory() as tmp, Worker() as w:
        w.call("new_document", name="d")
        h = w.call("add_primitive", kind="box", w=25, d=25, h=25)["handle"]
        trace = [{"name": "add_primitive", "args": {}, "result": {"handle": h},
                  "error": False}]
        passed, detail = check_cube20(w, trace)
    assert not passed, f"GRADER BUG: 25mm cube passed the 20mm checker ({detail})"
    print(f"  negative control OK — oracle rejected the wrong build: {detail}")


def main():
    real = bool(os.environ.get("RUN_RELIABILITY"))
    if real and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: RUN_RELIABILITY=1 but ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    selftest_grader()

    if real:
        import anthropic
        client = anthropic.Anthropic()
        models = [agentkit.MODELS[m] for m in
                  os.environ.get("DRIFTPIN_MODELS", "sonnet").split(",")]
        trials = int(os.environ.get("DRIFTPIN_TRIALS", "5"))
    else:
        print("\n[dry run] RUN_RELIABILITY unset — using the scripted stub client "
              "(free).\nThis validates the harness + grader; set RUN_RELIABILITY=1 "
              "with an API key to benchmark real models.")
        client = agentkit.ScriptedClient(_dry_script)
        models = ["claude-scripted"]
        trials = 1

    report = run_suite(client, models, trials)
    print_report(report)

    if real:
        # CI bar: weakest model must clear a floor (tune as you learn the suite).
        worst = min(
            sum(c["pass"] for k, c in report["cells"].items() if k.startswith(m + "/"))
            / sum(c["n"] for k, c in report["cells"].items() if k.startswith(m + "/"))
            for m in models)
        bar = 0.70
        if worst < bar:
            print(f"\nFAIL: weakest model reliability {worst:.0%} < bar {bar:.0%}")
            sys.exit(1)
        print(f"\nOK: all models ≥ bar {bar:.0%}")
    else:
        # In dry mode every cell should be 1/1 — the scripted builds are correct.
        assert all(c["rate"] == 1.0 for c in report["cells"].values()), \
            "dry-run plumbing check failed: a scripted build did not pass its checker"
        print("\nOK: dry-run plumbing + grader verified (all scripted builds passed).")


if __name__ == "__main__":
    main()

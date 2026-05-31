"""
Free dry run of the coordinator loop (no API).

Drives orchestration.coordinator.orchestrate with a ScriptedClient whose builders
emit canned tool calls. The FreeCAD worker + merge_assembly + gates are all REAL,
so this proves the fan-out -> merge -> gate -> renegotiate wiring end to end and
builds genuine geometry — it just doesn't pay for a model.

Scenario: a peg-and-plate fit. Round 0 the peg agent builds a deliberately FAT peg
(Ø18 into a Ø16 bore) so the interference gate fails; renegotiation re-dispatches
the peg, and round 1 it builds correctly -> gates pass. Exercises the renegotiate
path, not just the happy case.

Run:  .venv/bin/python3 -m orchestration.dryrun       (from repo root)
"""
import sys
import tempfile
from pathlib import Path

from . import agentkit
from .agentkit import tool_use, text_block
from . import coordinator


BORE_D, CLEAR, PLATE, CTR, PEGLEN = 16.0, 0.4, (60, 60, 10), (30, 30), 20.0

BRIEF = {
    "name": "peg_demo",
    "root": "peg_demo.FCStd",
    "components": {
        "plate": {"file": "plate.FCStd",
                  "task": (f"Build a {PLATE[0]}x{PLATE[1]}x{PLATE[2]} mm plate with a Ø{BORE_D} "
                           f"through-hole at ({CTR[0]},{CTR[1]}), then save_component.")},
        "peg": {"file": "peg.FCStd",
                "task": (f"Build a peg that slip-fits a Ø{BORE_D} bore with {CLEAR} clearance, "
                         f"length {PEGLEN}, then save_component.")},
    },
    "instances": [
        {"component": "plate", "name": "plate", "placement": [0, 0, 0]},
        {"component": "peg", "name": "peg", "placement": [CTR[0], CTR[1], -5]},
    ],
}


def _make_script():
    """Per-component scripts; the peg is FAT on its first dispatch, correct after.
    State across re-dispatch is tracked by counting how many times the peg started."""
    peg_dispatches = {"n": 0}

    def plate_steps():
        return [
            [tool_use("a", "new_document", {"name": "plate"})],
            [tool_use("b", "add_primitive", {"kind": "box", "w": 60, "d": 60, "h": 10, "name": "plate"})],
            [tool_use("c", "add_primitive", {"kind": "cylinder", "r": 8, "h": 30,
                                             "placement": [30, 30, -10], "name": "bore"})],
            [tool_use("d", "boolean_op", {"op": "cut", "base": "box_1", "tool": "cylinder_1"})],
            [tool_use("e", "save_component", {})],
        ]

    def peg_steps(radius):
        return [
            [tool_use("a", "new_document", {"name": "peg"})],
            [tool_use("b", "add_primitive", {"kind": "cylinder", "r": radius, "h": 20, "name": "peg"})],
            [tool_use("c", "save_component", {})],
        ]

    def script(task, turn):
        t = task.lower()
        if "plate" in t and "bore" not in t.split("peg")[0] or "Ø16 through-hole" in task:
            pass  # fall through to keyword routing below
        if "through-hole" in t:                      # plate
            steps = plate_steps()
        else:                                        # peg
            # decide radius once at turn 0 of each dispatch
            if turn == 0:
                peg_dispatches["n"] += 1
            radius = 9.0 if peg_dispatches["n"] == 1 else (BORE_D - CLEAR) / 2  # fat first
            steps = peg_steps(radius)
        return steps[turn] if turn < len(steps) else [text_block("done")]

    return script


def main():
    client = agentkit.ScriptedClient(_make_script())
    with tempfile.TemporaryDirectory() as td:
        rep = coordinator.orchestrate(
            client, agentkit.MODELS["haiku"], BRIEF, Path(td), max_rounds=3)
    print("\n== coordinator dry run ==")
    print(f"  ok={rep['ok']}  rounds={rep['rounds']}  cost=${rep['cost_usd']}")
    for rd in rep["trace"]:
        g = rd.get("gates", {})
        detail = (f"interference={len(g.get('interference', []))} "
                  f"envelope={len(g.get('envelope', []))}") if "gates" in rd else rd.get("reason", "")
        print(f"    round {rd['round']}: built={rd['built']} ok={rd['ok']}  {detail}")
    # success criteria: failed round 0 (fat peg caught), passed by the end
    trace = rep["trace"]
    caught_first = (not trace[0]["ok"])
    recovered = rep["ok"]
    ok = caught_first and recovered
    print("\n== LOOP OK (caught bad round, renegotiated to success) =="
          if ok else "\n== LOOP BROKEN ==")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

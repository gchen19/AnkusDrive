"""
Coordinator orchestration upgrades (RFC §11.8) — free, no LLM, no key. Drives
orchestration.coordinator.orchestrate with a ScriptedClient (canned tool calls);
the FreeCAD worker + merge_assembly + gates are REAL, so this proves the new
host-side loop end to end without paying a model:

  - round 0 contract review     each builder reviews its slice; amendments fold in
  - pipelined hierarchical fan-in  a sub_brief node is orchestrated + gated on its
                                   own; siblings don't block each other; the parent
                                   links the gated subassembly root
  - per-builder isolation       each leaf builds in workdir/build/<cid>/

Run: .venv/bin/python3 tests/test_coordinator.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestration import agentkit, coordinator  # noqa: E402
from orchestration.agentkit import tool_use, text_block  # noqa: E402

HAIKU = agentkit.MODELS["haiku"]
_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# --- scripted build/review steps ---------------------------------------------

def _plate_steps():
    return [[tool_use("a", "new_document", {"name": "plate"})],
            [tool_use("b", "add_primitive", {"kind": "box", "w": 60, "d": 60, "h": 10, "name": "plate"})],
            [tool_use("c", "add_primitive", {"kind": "cylinder", "r": 8, "h": 30,
                                             "placement": [30, 30, -10], "name": "bore"})],
            [tool_use("d", "boolean_op", {"op": "cut", "base": "box_1", "tool": "cylinder_1"})],
            [tool_use("e", "save_component", {})]]


def _peg_steps():
    return [[tool_use("a", "new_document", {"name": "peg"})],
            [tool_use("b", "add_primitive", {"kind": "cylinder", "r": 7.8, "h": 20, "name": "peg"})],
            [tool_use("c", "save_component", {})]]


def _housing_steps():
    return [[tool_use("a", "new_document", {"name": "housing"})],
            [tool_use("b", "add_primitive", {"kind": "box", "w": 60, "d": 60, "h": 4, "name": "housing"})],
            [tool_use("c", "save_component", {})]]


def _is_review(task):
    return task.startswith("Component '")


def _build_for(task):
    t = task.lower()
    if "through-hole" in t or "plate" in t:
        return _plate_steps()
    if "housing" in t:
        return _housing_steps()
    return _peg_steps()


# --- the briefs --------------------------------------------------------------

def _plate_task():
    return "Build a 60x60x10 mm plate with a Ø16 through-hole at (30,30), then save_component."


def _peg_task():
    return "Build a peg that slip-fits a Ø16 bore with 0.4 clearance, length 20, then save_component."


def _flat_brief():
    return {"name": "peg_demo", "root": "peg_demo.FCStd",
            "components": {"plate": {"file": "plate.FCStd", "task": _plate_task()},
                           "peg": {"file": "peg.FCStd", "task": _peg_task()}},
            "instances": [{"component": "plate", "name": "plate", "placement": [0, 0, 0]},
                          {"component": "peg", "name": "peg", "placement": [30, 30, -5]}]}


def _hier_brief():
    """A parent with a leaf housing + a `drivetrain` SUBASSEMBLY (peg-in-plate)."""
    return {"name": "product", "root": "product.FCStd",
            "components": {
                "housing": {"file": "housing.FCStd",
                            "task": "Build a 60x60x4 mm housing cover, then save_component."},
                "drivetrain": {"sub_brief": _flat_brief()}},
            "instances": [{"component": "drivetrain", "name": "drivetrain", "placement": [0, 0, 0]},
                          {"component": "housing", "name": "housing", "placement": [0, 0, 30]}]}


# --- tests -------------------------------------------------------------------

def test_round0_review_amends_then_builds():
    def script(task, turn):
        if _is_review(task):
            if task.startswith("Component 'peg'"):   # match the id, not the brief name
                return [tool_use("r", "emit_review", {
                    "verdict": "amend", "reason": "clarify the diameter math",
                    "patch": {"task_note": "Note: diametral clearance 0.4 -> peg Ø15.6."}})]
            return [tool_use("r", "emit_review", {"verdict": "accept", "reason": "ok"})]
        steps = _build_for(task)
        return steps[turn] if turn < len(steps) else [text_block("done")]

    client = agentkit.ScriptedClient(script)
    with tempfile.TemporaryDirectory() as td:
        rep = coordinator.orchestrate(client, HAIKU, _flat_brief(), Path(td), review=True)
    _check("round-0 reviews recorded", set(rep.get("reviews", {})), {"plate", "peg"})
    _check("peg slice amended", rep["reviews"]["peg"]["verdict"], "amend")
    _check("plate slice accepted", rep["reviews"]["plate"]["verdict"], "accept")
    _check("build proceeds and gates pass after amend", rep["ok"], True)


def test_per_builder_isolation():
    def script(task, turn):
        if _is_review(task):
            return [tool_use("r", "emit_review", {"verdict": "accept", "reason": "ok"})]
        steps = _build_for(task)
        return steps[turn] if turn < len(steps) else [text_block("done")]

    client = agentkit.ScriptedClient(script)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rep = coordinator.orchestrate(client, HAIKU, _flat_brief(), tmp, review=False)
        _check("flat assembly ok", rep["ok"], True)
        _check("plate built in its own dir", (tmp / "build" / "plate" / "plate.FCStd").exists(), True)
        _check("peg built in its own dir", (tmp / "build" / "peg" / "peg.FCStd").exists(), True)


def test_pipelined_hierarchical_fanin():
    def script(task, turn):
        if _is_review(task):
            return [tool_use("r", "emit_review", {"verdict": "accept", "reason": "ok"})]
        steps = _build_for(task)
        return steps[turn] if turn < len(steps) else [text_block("done")]

    client = agentkit.ScriptedClient(script)
    with tempfile.TemporaryDirectory() as td:
        rep = coordinator.orchestrate(client, HAIKU, _hier_brief(), Path(td), review=True)
    _check("hierarchical merge ok", rep["ok"], True)
    _check("subassembly node orchestrated + gated", rep["nodes"]["drivetrain"]["ok"], True)
    bom = {r["part"]: r["count"] for r in rep["trace"][-1]["gates"]["bom"]}
    _check("BOM flattens through the subassembly to leaves",
           bom, {"plate": 1, "peg": 1, "housing": 1})


def test_subassembly_failure_is_isolated():
    """Two subassembly nodes; one's peg never saves. Both still orchestrate
    (siblings don't block each other); the parent fails citing the broken node."""
    brief = {"name": "twin", "root": "twin.FCStd",
             "components": {"good": {"sub_brief": _flat_brief()},
                            "bad": {"sub_brief": _flat_brief()}},
             "instances": [{"component": "good", "name": "good", "placement": [0, 0, 0]},
                           {"component": "bad", "name": "bad", "placement": [100, 0, 0]}]}

    # fail the SECOND distinct peg dispatch (the one inside the 'bad' node);
    # 'good' orchestrates first (dict insertion order), so its peg saves fine.
    peg_runs = {"n": 0}

    def script2(task, turn):
        if _is_review(task):
            return [tool_use("r", "emit_review", {"verdict": "accept", "reason": "ok"})]
        t = task.lower()
        if "through-hole" in t or "plate" in t:
            steps = _plate_steps()
        elif "housing" in t:
            steps = _housing_steps()
        else:
            if turn == 0:
                peg_runs["n"] += 1
            if peg_runs["n"] == 2:                 # the peg inside the 'bad' node
                return [text_block("(builder gave up)")]
            steps = _peg_steps()
        return steps[turn] if turn < len(steps) else [text_block("done")]

    client = agentkit.ScriptedClient(script2)
    with tempfile.TemporaryDirectory() as td:
        rep = coordinator.orchestrate(client, HAIKU, brief, Path(td), review=False)
    _check("parent fails when a subassembly fails", rep["ok"], False)
    _check("both nodes orchestrated (sibling not blocked)",
           set(rep.get("nodes", {})), {"good", "bad"})
    _check("the good node still succeeded", rep["nodes"]["good"]["ok"], True)
    _check("the bad node is the failure", rep["nodes"]["bad"]["ok"], False)


def main():
    print("== coordinator §11.8 — round-0 review, pipelined fan-in, isolation (no API) ==")
    for t in (test_round0_review_amends_then_builds, test_per_builder_isolation,
              test_pipelined_hierarchical_fanin, test_subassembly_failure_is_isolated):
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

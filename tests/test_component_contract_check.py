"""
component_contract_check + the builder-brief schema (issue #169) — free, no LLM,
no key. Two halves:

  * The PURE core (ankusdrive.builder_brief) — validation, slice rendering, and the
    FreeCAD-free evaluate_contract judgement — tested with synthetic inputs, so the
    gate's logic is verified without a worker process.
  * The WORKER tool — component_contract_check driven against real geometry,
    two-sided: a correct component passes; each violation (not watertight, out of
    envelope, missing/misplaced interface) is caught as exactly the right check, and
    the tool never raises on a failing check.

Issue #261 adds the PERFORMANCE slice: the quantitative contract (#226) a part
declares is consulted here too, so a builder self-checks its Δp spec before fan-in
rather than discovering at merge time that it never proved it. Four-sided — met,
unmet, unverified (which is neither), and the part that declares nothing and must
see no change at all.

Run: .venv/bin/python3 tests/test_component_contract_check.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402
from ankusdrive import builder_brief as bb  # noqa: E402
from ankusdrive.gates import performance as pg  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _failed(rep):
    return {c["check"] for c in rep["checks"] if not c["passed"]}


# --- pure schema layer --------------------------------------------------------

_BRIEF = {
    "schema": bb.SCHEMA, "component": "plate", "assembly": "demo",
    "task": "build a plate", "output": "plate.FCStd",
    "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]},
    "interfaces": {"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1]}},
}


def test_validate():
    _check("valid brief -> no problems", bb.validate_builder_brief(_BRIEF), [])
    _check("missing component/task/output flagged",
           len(bb.validate_builder_brief({"schema": bb.SCHEMA})) >= 3, True)
    _check("bad schema flagged",
           any("schema" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "schema": "nope/9"})), True)
    _check("bad envelope flagged",
           any("envelope" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "envelope": {"min": [0, 0, 0], "max": [0, 60, 10]}})), True)
    _check("unknown key flagged",
           any("unknown brief key" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "wat": 1})), True)


def test_slice_text_and_projection():
    txt = bb.builder_brief_text(_BRIEF)
    _check("slice text names the component", "'plate'" in txt, True)
    _check("slice text tells builder to self-gate",
           "component_contract_check" in txt, True)
    # project a coordinator-brief component into a standalone builder brief
    coord = {"name": "demo", "shared_parameters": {"wall_mm": 3.0},
             "components": {"plate": {"file": "plate.FCStd", "task": "build a plate",
                                      "envelope": _BRIEF["envelope"],
                                      "interfaces": _BRIEF["interfaces"],
                                      "material": "AISI 1045"}}}
    proj = bb.brief_from_slice(coord, "plate")
    _check("projected brief is valid", bb.validate_builder_brief(proj), [])
    _check("projection carries schema + envelope + shared params",
           (proj["schema"], "envelope" in proj, proj["shared_parameters"]),
           (bb.SCHEMA, True, {"wall_mm": 3.0}))


def test_evaluate_core():
    ok = bb.evaluate_contract(
        _BRIEF, watertight=True, bbox={"min": [0, 0, 0], "max": [60, 60, 10]},
        published={"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1]}})
    _check("core: reference component -> ok", ok["ok"], True)

    bad = bb.evaluate_contract(
        _BRIEF, watertight=False, bbox={"min": [-1, 0, 0], "max": [60, 60, 12]},
        published={})
    _check("core: three violations flagged",
           _failed(bad), {"watertight", "envelope", "interface:bore_axis"})
    _check("core: ok False + reasons populated",
           (bad["ok"], len(bad["reasons"])), (False, 3))

    # published but in the wrong place -> caught only when the brief pins the origin
    off = bb.evaluate_contract(
        {**_BRIEF, "interfaces": {"bore_axis": {"origin": [30, 30, 0], "tol_mm": 0.5}}},
        watertight=True, bbox={"min": [0, 0, 0], "max": [60, 60, 10]},
        published={"bore_axis": {"origin": [35, 30, 0]}})
    _check("core: misplaced frame caught", _failed(off), {"interface:bore_axis"})

    # empty brief -> only the always-on watertight check runs
    empty = bb.evaluate_contract({}, watertight=True, bbox=None, published={})
    _check("core: empty brief -> ok, watertight-only",
           (empty["ok"], [c["check"] for c in empty["checks"]]),
           (True, ["watertight"]))


# --- worker tool, two-sided against real geometry -----------------------------

def _plate(worker, w=60, d=60, h=10):
    """A watertight plate with a Ø16 bore at (30,30) and a published bore_axis."""
    worker.call("new_document", name="plate")
    worker.call("add_primitive", kind="box", w=w, d=d, h=h, name="box")
    worker.call("add_primitive", kind="cylinder", r=8, h=h + 20,
                placement=[30, 30, -10], name="bore")
    handle = worker.call("boolean_op", op="cut", base="box_1",
                         tool="cylinder_1")["handle"]
    worker.call("publish_interface", handle=handle, name="bore_axis",
                frame={"origin": [30, 30, 0], "z_axis": [0, 0, 1]})
    return handle


def test_worker_gate():
    brief = {
        "schema": bb.SCHEMA, "component": "plate", "assembly": "demo",
        "task": "t", "output": "plate.FCStd",
        "envelope": {"min": [0, 0, 0], "max": [60, 60, 10]},
        "interfaces": {"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1],
                                     "tol_mm": 0.5}},
    }
    with Worker() as w:
        h = _plate(w)
        rep = w.call("component_contract_check", handle=h, brief=brief)
        _check("reference plate -> ok", rep["ok"], True)
        _check("reference plate: all checks passed", _failed(rep), set())

        # out-of-envelope part: a fat plate whose bbox busts the keep-out box
        fat = w.call("add_primitive", kind="box", w=80, d=60, h=10,
                     name="fat")["handle"]
        w.call("publish_interface", handle=fat, name="bore_axis",
               frame={"origin": [30, 30, 0], "z_axis": [0, 0, 1]})
        rep = w.call("component_contract_check", handle=fat, brief=brief)
        _check("oversize part: envelope caught", "envelope" in _failed(rep), True)

        # forgot to publish the required interface
        bare = w.call("add_primitive", kind="box", w=60, d=60, h=10,
                      name="bare")["handle"]
        rep = w.call("component_contract_check", handle=bare, brief=brief)
        _check("missing interface caught",
               "interface:bore_axis" in _failed(rep), True)
        _check("tool never raises on failing checks (returns a report)",
               rep["ok"], False)

        # not-watertight: two disjoint boxes fused into a 2-solid compound is not
        # "one clean watertight solid".
        w.call("new_document", name="shell")
        c1 = w.call("add_primitive", kind="box", w=20, d=20, h=20,
                    name="cube")["handle"]
        c2 = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                    placement=[40, 0, 0], name="cube2")["handle"]
        fused = w.call("boolean_op", op="fuse", base=c1, tool=c2)["handle"]
        rep = w.call("component_contract_check", handle=fused,
                     brief={"envelope": {"min": [-1, -1, -1], "max": [100, 100, 100]}})
        _check("non-single-solid: watertight caught",
               "watertight" in _failed(rep), True)


# --- the performance slice (issue #261) ---------------------------------------
#
# The brief a builder self-checks against gains a `performance` slice, and the part's
# own declared contract (#226) is consulted whether or not the brief repeats it. The
# pure half is driven on constructed gate blocks; the worker half on real geometry.

_PERF_BRIEF = {**_BRIEF, "performance": {
    "requirements": [{"name": "dp_at_rated", "limit": {"max": 50.0}}]}}
_PART_CONTRACT = {"requirements": [
    {"name": "dp_at_rated", "metric": "pressure_drop_pa", "tool": "cfd_pipe_flow",
     "limit": {"max": 50.0}}]}
_GEOM_OK = {"watertight": True, "bbox": {"min": [0, 0, 0], "max": [60, 60, 10]},
            "published": {"bore_axis": {"origin": [30, 30, 0], "z_axis": [0, 0, 1]}}}


def _block(state, **row):
    """A ankusdrive.gates.performance block as evaluate() would build it."""
    record = None if state == pg.UNVERIFIED else {
        "signature": "sig", "results": [{"name": "dp_at_rated", "state": state,
                                         "measured": 34.0, **row}]}
    return pg.evaluate(_PART_CONTRACT, record, signature="sig", component="plate")


def test_performance_slice_validation_and_text():
    _check("brief with a performance slice is valid",
           bb.validate_builder_brief(_PERF_BRIEF), [])
    _check("a limitless performance requirement is flagged",
           any("limit" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "performance": {"requirements": [{"name": "x"}]}})), True)
    _check("an unknown performance key is flagged",
           any("unknown performance key" in p for p in bb.validate_builder_brief(
               {**_BRIEF, "performance": {"nope": 1}})), True)
    txt = bb.builder_brief_text(_PERF_BRIEF)
    _check("slice text tells the builder to prove, not just declare",
           "verify_performance" in txt and "not a pass" in txt, True)


def test_performance_slice_core():
    """Four-sided, on the pure core. The last case is the one that matters most: a
    brief and a part with no performance at all must behave exactly as before #261."""
    # 1. MET -> passes, alongside the geometric checks
    met = bb.evaluate_contract(_PERF_BRIEF, **_GEOM_OK, performance=_block(pg.PASS),
                               performance_contract=_PART_CONTRACT)
    _check("met contract -> ok", met["ok"], True)
    _check("met contract adds no failures", _failed(met), set())
    _check("met contract is not a skip", met["skipped"], [])
    _check("the block rides in the report", met["performance"]["outcome"], pg.MET)

    # 2. UNMET -> a failing row that NAMES the requirement
    unmet = bb.evaluate_contract(_PERF_BRIEF, **_GEOM_OK,
                                 performance=_block(pg.FAIL, measured=88.0,
                                                    detail="88 vs max 50"),
                                 performance_contract=_PART_CONTRACT)
    _check("unmet contract -> not ok", unmet["ok"], False)
    _check("unmet contract names the requirement",
           _failed(unmet), {"performance:dp_at_rated"})
    _check("the reason carries the measurement",
           any("88" in r for r in unmet["reasons"]), True)

    # 3. UNVERIFIED / INDETERMINATE -> neither passed nor failed
    for label, state in (("never verified", pg.UNVERIFIED),
                         ("band straddles the limit", pg.INDETERMINATE)):
        und = bb.evaluate_contract(_PERF_BRIEF, **_GEOM_OK,
                                   performance=_block(state),
                                   performance_contract=_PART_CONTRACT)
        _check(f"{label}: does not fail the gate", und["ok"], True)
        _check(f"{label}: and does not pass it either",
               [s["check"] for s in und["skipped"]], ["performance:dp_at_rated"])
        _check(f"{label}: the skip says there is no verdict",
               "NO verdict" in und["skipped"][0]["reason"], True)
        _check(f"{label}: no check row claims it passed",
               any(c["check"] == "performance:dp_at_rated" for c in und["checks"]),
               False)

    # 4. NO CONTRACT ANYWHERE -> byte-for-byte the pre-#261 gate
    plain = bb.evaluate_contract(_BRIEF, **_GEOM_OK)
    _check("no contract: same checks as before",
           [c["check"] for c in plain["checks"]],
           ["watertight", "envelope", "interface:bore_axis"])
    _check("no contract: ok + empty skipped, no performance key",
           (plain["ok"], plain["skipped"], "performance" in plain),
           (True, [], False))

    # a brief that DEMANDS a contract the part never declared fails loudly
    missing = bb.evaluate_contract(_PERF_BRIEF, **_GEOM_OK,
                                   performance=pg.evaluate({}, None),
                                   performance_contract={})
    _check("brief demands a contract the part lacks -> fails",
           (missing["ok"], _failed(missing)), (False, {"performance"}))
    # a builder that quietly relaxed its own limit has not honored the brief
    loose = {"requirements": [{**_PART_CONTRACT["requirements"][0],
                               "limit": {"max": 500.0}}]}
    relaxed = bb.evaluate_contract(
        _PERF_BRIEF, **_GEOM_OK,
        performance=pg.evaluate(loose, {"signature": "sig", "results": [
            {"name": "dp_at_rated", "state": "pass", "measured": 34.0}]},
            signature="sig"),
        performance_contract=loose)
    _check("a loosened limit is caught even though the part's own contract passes",
           (relaxed["ok"], "performance_spec:dp_at_rated" in _failed(relaxed)),
           (False, True))


def test_worker_gate_consults_the_performance_contract():
    """The worker tool, on real geometry: declare a Δp spec measured by the analytic
    pipe screen, and drive the gate through met / unmet / unverified / none."""
    conditions = {"diameter_mm": 10, "length_mm": 1000, "flow_rate_lpm": 0.5,
                  "fluid": "water-20c"}                       # laminar, 34.0 Pa exact

    def req(limit_pa):
        return {"name": "dp_at_rated", "metric": "pressure_drop_pa",
                "tool": "cfd_pipe_flow", "conditions": conditions,
                "limit": {"max": limit_pa}, "fidelity_floor": "screen",
                "screen": {"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa",
                           "conditions": conditions}}

    brief = {"schema": bb.SCHEMA, "component": "pipe", "task": "t",
             "output": "pipe.FCStd",
             "performance": {"requirements": [{"name": "dp_at_rated",
                                               "limit": {"max": 100.0}}]}}
    with Worker() as w:
        w.call("new_document", name="pipe")
        h = w.call("add_primitive", kind="cylinder", radius=5, h=100)["handle"]

        # no contract yet -> the brief demands one, so this fails loudly
        rep = w.call("component_contract_check", handle=h, brief=brief)
        _check("brief demands a contract, part has none -> fails",
               "performance" in _failed(rep), True)

        # declared but never verified -> neither passed nor failed
        w.call("declare_performance", handle=h, requirements=[req(100.0)])
        rep = w.call("component_contract_check", handle=h, brief=brief)
        _check("declared but unverified: no failing performance check",
               any(c["check"].startswith("performance:") and not c["passed"]
                   for c in rep["checks"]), False)
        _check("declared but unverified: surfaced as a skip",
               [s["check"] for s in rep["skipped"]], ["performance:dp_at_rated"])
        _check("declared but unverified: the block says unproven",
               rep["performance"]["outcome"], pg.UNPROVEN)

        # verified and met -> passes
        w.call("verify_performance", handle=h, tier="screen")
        rep = w.call("component_contract_check", handle=h, brief=brief)
        _check("verified + met -> ok", rep["ok"], True)
        _check("verified + met -> nothing skipped", rep["skipped"], [])
        _check("verified + met -> the requirement row passed",
               [c["passed"] for c in rep["checks"]
                if c["check"] == "performance:dp_at_rated"], [True])

        # re-declare an impossible limit and re-verify -> blocked, and NAMED
        w.call("declare_performance", handle=h, requirements=[req(1.0)])
        w.call("verify_performance", handle=h, tier="screen")
        rep = w.call("component_contract_check", handle=h, brief=brief)
        _check("verified + unmet -> not ok", rep["ok"], False)
        _check("verified + unmet -> names the requirement",
               "performance:dp_at_rated" in _failed(rep), True)

        # a part with NO contract and a brief with no performance slice: unchanged
        bare = w.call("add_primitive", kind="box", w=10, d=10, h=10)["handle"]
        rep = w.call("component_contract_check", handle=bare, brief={})
        _check("no contract, no slice -> watertight-only, as before",
               [c["check"] for c in rep["checks"]], ["watertight"])
        _check("no contract, no slice -> nothing skipped, no performance key",
               (rep["skipped"], "performance" in rep), ([], False))


def main():
    print("== component_contract_check + builder brief (issue #169, no API) ==")
    for t in (test_validate, test_slice_text_and_projection, test_evaluate_core,
              test_worker_gate, test_performance_slice_validation_and_text,
              test_performance_slice_core,
              test_worker_gate_consults_the_performance_contract):
        t()
    print(f"\n== {_PASS}/{_PASS + _FAIL} checks passed "
          f"({'OK' if not _FAIL else str(_FAIL) + ' FAILED'}) ==")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

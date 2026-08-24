"""Performance-contract toys (issue #226) — the spec as a checkable object.

Two tiers:
  * **arithmetic** (always, no FreeCAD, no solver): the three-state verdict. The
    interesting case is not pass or fail, it is the band that straddles the limit —
    a correlation reading Cd = 0.28 ± 10 % against `max: 0.30` spans 0.252–0.308 and has
    NOT shown the part passes. Everything here is two-sided: a case that must pass, a
    case that must fail, and the case that must refuse to decide.
  * **live** (needs FreeCAD): declare a contract on a real part and verify it end to end
    through the worker, including the fidelity ladder and the trust gate.

Plus the GATE half (issue #261): a contract nothing consults is documentation with a
verifier attached, so `merge_assembly` now reads the verdict `verify_performance`
recorded on each component. Every case is driven twice — once on constructed records
(no solver, no timing luck, which is where the "unverified is not a pass" property
actually lives) and once live through a real merge of real components.

Run:  python3 tests/test_performance.py
"""
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.analysis import performance as pf  # noqa: E402
from ankusdrive.gates import performance as pgate  # noqa: E402


# --- the three-state verdict ----------------------------------------------------

def test_an_exact_measurement_decides_cleanly():
    under = pf.evaluate_limit(0.28, {"max": 0.30})
    over = pf.evaluate_limit(0.31, {"max": 0.30})
    assert under["state"] == "pass" and over["state"] == "fail"
    # margin is fractional slack against the binding limit, signed
    assert abs(under["margin"] - (0.30 - 0.28) / 0.30) < 1e-12
    assert over["margin"] < 0, over
    assert under["band_pct"] is None and under["worst_case"] == under["best_case"]
    # a minimum reverses the sense
    assert pf.evaluate_limit(250.0, {"min": 200.0})["state"] == "pass"
    assert pf.evaluate_limit(150.0, {"min": 200.0})["state"] == "fail"
    # a window needs both ends
    win = {"min": 200.0, "max": 400.0}
    assert pf.evaluate_limit(300.0, win)["state"] == "pass"
    assert pf.evaluate_limit(500.0, win)["state"] == "fail"
    assert pf.evaluate_limit(100.0, win)["state"] == "fail"


def test_a_band_that_straddles_the_limit_refuses_to_decide():
    """The reason this layer exists. Same measurement, same limit, three verdicts —
    decided by how much the measurement is trusted."""
    limit = {"max": 0.30}
    assert pf.evaluate_limit(0.28, limit, 10.0)["state"] == "indeterminate"
    assert pf.evaluate_limit(0.25, limit, 10.0)["state"] == "pass"      # 0.275 < 0.30
    assert pf.evaluate_limit(0.35, limit, 10.0)["state"] == "fail"      # 0.315 > 0.30
    # tightening the band turns the same measurement into a decision
    assert pf.evaluate_limit(0.28, limit, 1.0)["state"] == "pass"
    straddle = pf.evaluate_limit(0.28, limit, 10.0)
    assert "straddles" in straddle["detail"], straddle["detail"]
    assert abs(straddle["worst_case"] - 0.308) < 1e-9
    assert abs(straddle["best_case"] - 0.252) < 1e-9
    # the nominal margin stays positive even when the verdict is undecided — margin
    # is not a verdict, and reading it as one is the trap
    assert straddle["margin"] > 0, straddle
    # a minimum straddles the same way
    assert pf.evaluate_limit(210.0, {"min": 200.0}, 15.0)["state"] == "indeterminate"
    assert pf.evaluate_limit(250.0, {"min": 200.0}, 15.0)["state"] == "pass"


def test_trust_demands_can_only_remove_a_pass():
    conv = {"trust": {"converged": True, "mesh": {"ok": True}}, "gated": True}
    assert pf.check_trust(conv, {"converged": True, "mesh_ok": True}) == []
    assert pf.check_trust(conv, None) == []
    # an unconverged solve can never be proof
    bad = pf.check_trust({"trust": {"converged": False}}, {"converged": True})
    assert len(bad) == 1 and "not proof" in bad[0], bad
    # silence is not evidence: a demand the payload cannot answer is a reason
    assert pf.check_trust({}, {"converged": True})
    assert pf.check_trust({}, {"band_max_pct": 5})
    assert pf.check_trust({"gci_pct": 8.0}, {"band_max_pct": 5.0})
    assert pf.check_trust({"gci_pct": 3.0}, {"band_max_pct": 5.0}) == []
    # gci beats band_pct when both are present (a measured study beats a correlation)
    assert pf.check_trust({"gci_pct": 3.0, "band_pct": 20.0},
                          {"band_max_pct": 5.0}) == []
    ungated = pf.check_trust({"gated": False}, {"gated": True})
    assert len(ungated) == 1 and "verified oracle" in ungated[0], ungated
    assert pf.check_trust({"trust": {"mesh": {"ok": False}}}, {"mesh_ok": True})


def test_requirement_schema_is_policed():
    ok = pf.validate_requirement({
        "name": "dp", "metric": "pressure_drop_pa", "tool": "cfd_internal_flow_submit",
        "limit": {"max": 50.0}})
    assert ok["fidelity_floor"] == "solver"        # proof is a solve unless told otherwise
    bad = [
        {},                                                        # no name
        {"name": "x"},                                             # no metric
        {"name": "x", "metric": "cd"},                             # no tool
        {"name": "x", "metric": "cd", "tool": "t"},                # no limit
        {"name": "x", "metric": "cd", "tool": "t", "limit": {}},   # empty limit
        {"name": "x", "metric": "cd", "tool": "t", "limit": {"max": "0.3"}},
        {"name": "x", "metric": "cd", "tool": "t",
         "limit": {"min": 5.0, "max": 1.0}},                       # inverted window
        {"name": "x", "metric": "cd", "tool": "t", "limit": {"max": 1.0},
         "fidelity_floor": "vibes"},
        # declaring a screen floor with no screen block is a contract that can never
        # be satisfied, so it is refused at declaration rather than at verification
        {"name": "x", "metric": "cd", "tool": "t", "limit": {"max": 1.0},
         "fidelity_floor": "screen"},
        {"name": "x", "metric": "cd", "tool": "t", "limit": {"max": 1.0},
         "screen": {"metric": "cd"}},                              # screen without tool
        {"name": "x", "metric": "cd", "tool": "t", "limit": {"max": 1.0},
         "trust": "yes"},
        "not a dict",
    ]
    for req in bad:
        try:
            pf.validate_requirement(req)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_requirement({req!r}) should have raised")


def test_metric_paths_reach_into_nested_results():
    payload = {"cd": 0.31, "trust": {"converged": True, "mesh": {"ok": True}},
               "modes": [{"frequency_hz": 212.4}, {"frequency_hz": 480.1}]}
    assert pf._get_path(payload, "cd") == 0.31
    assert pf._get_path(payload, "trust.converged") is True
    assert pf._get_path(payload, "modes.0.frequency_hz") == 212.4
    for missing in ("nope", "trust.nope", "modes.9.frequency_hz", "cd.deeper"):
        assert pf._get_path(payload, missing) is None, missing


def test_summary_treats_indeterminate_as_not_satisfied():
    rows = [{"name": "a", "state": "pass"}, {"name": "b", "state": "pass"}]
    assert pf.summarize(rows)["ok"] is True
    mixed = rows + [{"name": "c", "state": "indeterminate"}]
    s = pf.summarize(mixed)
    assert s["ok"] is False and s["escalate"] == ["c"], s
    assert s["passed"] == 2 and s["indeterminate"] == 1 and s["failed"] == 0
    failed = pf.summarize(rows + [{"name": "d", "state": "fail"}])
    assert failed["ok"] is False and failed["escalate"] == []   # nothing to escalate
    assert pf.summarize([])["ok"] is False                      # an empty contract
    #                                                             proves nothing


# --- the gate half (#261): what a gate may conclude from a recorded verdict -------
#
# Driven on constructed contracts + records, no worker: the property under test is
# "what does the ABSENCE of a measurement mean", and it must hold identically whether
# nobody ever verified, a solve is still running, or an edit invalidated the answer.

_SIG = "abc123"


def _contract(*names_and_limits):
    return {"requirements": [
        {"name": n, "metric": "pressure_drop_pa", "tool": "cfd_pipe_flow",
         "limit": lim} for n, lim in names_and_limits]}


def _record(rows, signature=_SIG, **extra):
    return {"schema": pgate.RECORD_SCHEMA, "signature": signature, "tier": "screen",
            "verified_at": 1000.0, "results": list(rows), **extra}


def test_a_part_with_no_contract_is_untouched_by_the_gate():
    """The case that matters most: 99 % of parts declare nothing, and for them #261
    must be invisible. No contract -> not_declared, no violations, no skips, and
    roll_up returns None so merge_assembly emits no performance block at all."""
    block = pgate.evaluate({}, None, signature=_SIG, component="plate")
    assert block["outcome"] == pgate.NOT_DECLARED, block
    assert block["declared"] is False and block["ok"] is True, block
    assert block["requirements"] == [] and block["violations"] == [], block
    assert block["skipped"] == [], block
    # None/{} contract, and a stray verdict record with no contract, all behave alike
    for contract in (None, {}, {"requirements": []}):
        assert pgate.evaluate(contract, _record([]))["outcome"] == pgate.NOT_DECLARED
    # and the assembly roll-up disappears entirely
    assert pgate.roll_up({"plate": block}) is None
    assert pgate.roll_up({}) is None


def test_a_met_contract_passes_the_gate_and_an_unmet_one_blocks_it():
    """The two verdicts. Both are statements about the part, so both decide."""
    contract = _contract(("dp_at_rated", {"max": 50.0}))
    met = pgate.evaluate(contract, _record(
        [{"name": "dp_at_rated", "state": "pass", "measured": 34.0,
          "detail": "34 vs max 50"}]), signature=_SIG, component="manifold")
    assert met["outcome"] == pgate.MET and met["ok"] is True, met
    assert pgate.has_verdict(met) is True
    assert met["violations"] == [] and met["skipped"] == [], met
    assert met["requirements"][0]["state"] == pgate.PASS

    unmet = pgate.evaluate(contract, _record(
        [{"name": "dp_at_rated", "state": "fail", "measured": 88.0,
          "detail": "88 vs max 50"}]), signature=_SIG, component="manifold")
    assert unmet["outcome"] == pgate.UNMET and unmet["ok"] is False, unmet
    assert pgate.has_verdict(unmet) is True          # a failure IS a verdict
    v = unmet["violations"]
    assert len(v) == 1 and v[0]["requirement"] == "performance", v
    assert v[0]["name"] == "dp_at_rated" and v[0]["component"] == "manifold", v
    assert v[0]["measured"] == 88.0 and v[0]["limit"] == {"max": 50.0}, v
    assert "dp_at_rated" in unmet["reason"], unmet["reason"]
    # the roll-up carries the violation up to merge_assembly's `ok` predicate
    up = pgate.roll_up({"manifold": unmet})
    assert up["outcome"] == pgate.UNMET and up["ok"] is False and up["violations"] == v


def test_an_undecided_contract_is_neither_passed_nor_failed():
    """#261's whole point, and #248's discipline one level up. Four ways to have no
    verdict — never verified, a row that could not decide, a solve still in flight,
    and a verdict invalidated by an edit — and NONE of them may read as a pass or as
    a failure. They ride in `skipped`, the vocabulary merge_assembly already has."""
    contract = _contract(("dp_at_rated", {"max": 50.0}))
    cases = {
        # 1. nobody ever ran verify_performance
        pgate.UNVERIFIED: pgate.evaluate(contract, None, signature=_SIG),
        # 2. measured, but the band straddled the limit (#226's third state)
        pgate.INDETERMINATE: pgate.evaluate(contract, _record(
            [{"name": "dp_at_rated", "state": "indeterminate", "measured": 48.0,
              "detail": "band ±10 % straddles the limit"}]), signature=_SIG),
        # 3. the part changed after it was verified
        pgate.STALE: pgate.evaluate(contract, _record(
            [{"name": "dp_at_rated", "state": "pass", "measured": 34.0}]),
            signature="edited-since"),
    }
    for want, block in cases.items():
        assert block["outcome"] == pgate.UNPROVEN, (want, block)
        assert block["ok"] is False, (want, block)          # never a pass
        assert block["violations"] == [], (want, block)     # never a failure either
        assert block["skipped"] == ["dp_at_rated"], (want, block)
        assert pgate.has_verdict(block) is False, (want, block)
        item = block["requirements"][0]
        assert item["state"] == want, (want, item)
        assert "NO verdict" in item["skipped_reason"], item

    # 4. a solve still in flight names the job, so the caller knows what to wait for
    flight = pgate.evaluate(contract, _record(
        [{"name": "dp_at_rated", "state": "indeterminate", "job_id": "job_7",
          "detail": "solve submitted; poll job_result for the verdict"}]),
        signature=_SIG)
    assert flight["outcome"] == pgate.UNPROVEN and flight["ok"] is False, flight
    assert "job_7" in flight["requirements"][0]["skipped_reason"], flight

    # a record with no signature at all cannot be dated -> stale, not trusted
    undatable = pgate.evaluate(contract, _record(
        [{"name": "dp_at_rated", "state": "pass"}], signature=None), signature=_SIG)
    assert undatable["requirements"][0]["state"] == pgate.STALE, undatable
    assert undatable["stale"] is True and undatable["ok"] is False, undatable

    # a row with a garbled state is NOT read optimistically
    garbled = pgate.evaluate(contract, _record(
        [{"name": "dp_at_rated", "state": "probably fine"}]), signature=_SIG)
    assert garbled["requirements"][0]["state"] == pgate.INDETERMINATE, garbled
    assert garbled["ok"] is False, garbled


def test_the_roll_up_never_lets_undecided_masquerade_as_met():
    """One met component + one unproven one is not a met assembly, and one unmet
    component beats everything. `skipped` never fails the merge (a non-verdict says
    nothing about the part) but it can never make `ok` True either."""
    c = _contract(("dp", {"max": 50.0}))
    met = pgate.evaluate(c, _record([{"name": "dp", "state": "pass"}]),
                         signature=_SIG, component="a")
    unproven = pgate.evaluate(c, None, signature=_SIG, component="b")
    unmet = pgate.evaluate(c, _record([{"name": "dp", "state": "fail"}]),
                           signature=_SIG, component="c")
    plain = pgate.evaluate({}, None, component="d")

    only_met = pgate.roll_up({"a": met, "d": plain})
    assert only_met["outcome"] == pgate.MET and only_met["ok"] is True, only_met
    assert only_met["violations"] == [] and only_met["skipped"] == [], only_met
    assert set(only_met["components"]) == {"a"}, only_met   # `d` declared nothing

    mixed = pgate.roll_up({"a": met, "b": unproven})
    assert mixed["outcome"] == pgate.UNPROVEN and mixed["ok"] is False, mixed
    assert mixed["violations"] == [], mixed          # does NOT fail the merge
    assert mixed["skipped"] == ["b:dp"], mixed       # but is loud, and named
    assert "not decided" in mixed["reason"], mixed["reason"]

    worst = pgate.roll_up({"a": met, "b": unproven, "c": unmet})
    assert worst["outcome"] == pgate.UNMET and worst["ok"] is False, worst
    assert [v["component"] for v in worst["violations"]] == ["c"], worst


# --- live: the contract on a real part ------------------------------------------

def test_contract_round_trips_and_verifies_on_a_part():
    """Declare a Δp limit whose metric comes from the analytic pipe screen, and verify
    it end to end: a generous limit passes, an impossible one fails, and a limit sitting
    inside the correlation band comes back indeterminate rather than guessing."""
    from ankusdrive import Worker
    from ankusdrive.client import WorkerError

    # 10 mm bore, 1 m long, 0.5 L/min of water -> laminar, 34.0 Pa (the exact
    # Hagen-Poiseuille branch, so band_pct is None and the verdict is clean)
    conditions = {"diameter_mm": 10, "length_mm": 1000, "flow_rate_lpm": 0.5,
                  "fluid": "water-20c"}

    def req(name, limit, floor="screen"):
        return {"name": name, "metric": "pressure_drop_pa", "tool": "cfd_pipe_flow",
                "conditions": conditions, "limit": limit, "fidelity_floor": floor,
                "screen": {"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa",
                           "conditions": conditions}}

    with Worker() as w:
        w.call("new_document", name="perf_contract")
        pipe = w.call("add_primitive", kind="cylinder", radius=5, height=1000)
        h = pipe["handle"]

        decl = w.call("declare_performance", handle=h, requirements=[
            req("dp_generous", {"max": 100.0}),
            req("dp_impossible", {"max": 1.0}),
        ])
        assert decl["n_requirements"] == 2, decl

        got = w.call("verify_performance", handle=h, tier="screen")
        assert got["ok"] is False, got                  # one requirement fails
        by = {r["name"]: r for r in got["results"]}
        assert by["dp_generous"]["state"] == "pass", by["dp_generous"]
        assert by["dp_impossible"]["state"] == "fail", by["dp_impossible"]
        # the measured value is the tool's own, not a re-implementation
        solved = w.call("cfd_pipe_flow", **conditions)["pressure_drop_pa"]
        assert abs(by["dp_generous"]["measured"] - solved) < 1e-9, (by, solved)
        assert by["dp_generous"]["margin"] > 0 > by["dp_impossible"]["margin"]
        assert got["passed"] == 1 and got["failed"] == 1 and got["escalate"] == []

        # the contract persists on the part and survives a re-read
        again = w.call("verify_performance", handle=h, tier="screen")
        assert again["results"][0]["measured"] == by["dp_generous"]["measured"]

        # a limit inside the correlation band must NOT be answered. The laminar branch
        # is exact, so use the turbulent one, which reports band_pct=10.
        turb = {"diameter_mm": 10, "length_mm": 1000, "velocity_m_s": 3.0,
                "fluid": "water-20c"}
        measured = w.call("cfd_pipe_flow", **turb)
        assert measured["band_pct"] == 10.0 and measured["regime"] == "turbulent"
        edge = measured["pressure_drop_pa"] * 1.05      # 5 % above: inside the ±10 %
        w.call("declare_performance", handle=h, requirements=[{
            "name": "dp_edge", "metric": "pressure_drop_pa", "tool": "cfd_pipe_flow",
            "conditions": turb, "limit": {"max": edge}, "fidelity_floor": "screen",
            "screen": {"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa",
                       "conditions": turb}}])
        band = w.call("verify_performance", handle=h, tier="screen")
        row = band["results"][0]
        assert row["state"] == "indeterminate", row
        assert row["band_pct"] == 10.0 and "straddles" in row["detail"], row
        assert band["escalate"] == ["dp_edge"] and band["ok"] is False

        # a trust demand the screen cannot answer also refuses to pass
        w.call("declare_performance", handle=h, requirements=[{
            "name": "dp_needs_solve", "metric": "pressure_drop_pa",
            "tool": "cfd_pipe_flow", "conditions": conditions, "limit": {"max": 100.0},
            "fidelity_floor": "screen", "trust": {"converged": True},
            "screen": {"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa",
                       "conditions": conditions}}])
        gated = w.call("verify_performance", handle=h, tier="screen")["results"][0]
        assert gated["state"] == "indeterminate", gated
        assert gated["trust_reasons"] and "converged" in gated["trust_reasons"][0]

        # verifying a part with no contract is an error, not a silent empty pass
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        try:
            w.call("verify_performance", handle=box["handle"])
        except WorkerError as e:
            assert "no performance contract" in e.remote_message, e.remote_message
        else:
            raise AssertionError("verify_performance on an undeclared part must raise")
        # and so is a malformed declaration
        try:
            w.call("declare_performance", handle=h,
                   requirements=[{"name": "x", "metric": "cd"}])
        except WorkerError as e:
            assert "tool" in e.remote_message, e.remote_message
        else:
            raise AssertionError("a requirement with no tool must be refused")
    print(f"    pipe contract: measured {solved:.4g} Pa, pass/fail/indeterminate all "
          "reached through the worker")


def test_solver_tier_proves_an_aero_spec_end_to_end():
    """The epic's headline workflow, whole: declare "Cd ≤ N at this speed" on a real
    solid, verify at solver tier, and get back a verdict whose measurement came from an
    actual CFD solve with its trust block attached. Two-sided on the same solve — a
    generous limit passes, an impossible one fails — plus a trust demand that no solve
    can satisfy, which must come back indeterminate rather than pass."""
    from ankusdrive import Worker, solvers
    from ankusdrive.analysis import cfd
    from tests.heavy_solve import skip_heavy
    if skip_heavy("OpenFOAM performance contract"):
        return
    if not solvers.is_available("openfoam"):
        print("    SKIP — OpenFOAM not installed")
        return
    d_mm, rho = 10.0, 998.2
    mu, _ = cfd._fluid_props("water-20c", None, None)
    velocity = 100.0 * (mu / rho) / (d_mm / 1000.0)          # Re = 100
    conditions = {"model": "$handle", "velocity_m_s": velocity, "fluid": "water-20c"}

    def req(name, limit, trust=None):
        r = {"name": name, "metric": "cd", "tool": "cfd_external_flow_submit",
             "conditions": conditions, "limit": limit, "fidelity_floor": "solver"}
        if trust:
            r["trust"] = trust
        return r

    with Worker() as w:
        w.call("new_document", name="perf_solver")
        sphere = w.call("add_primitive", kind="sphere", radius=d_mm / 2)
        h = sphere["handle"]
        w.call("declare_performance", handle=h, requirements=[
            req("cd_generous", {"max": 2.0}),
            req("cd_impossible", {"max": 0.10}),
            req("cd_needs_tight_band", {"max": 2.0}, trust={"band_max_pct": 0.5}),
        ])
        sub = w.call("verify_performance", handle=h, tier="solver")
        assert sub.get("job_id") and sub.get("pending"), sub
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            st = w.call("job_status", job_id=sub["job_id"])
            if st["status"] in ("done", "failed"):
                break
            time.sleep(1.0)
        assert st["status"] == "done", st
        res = w.call("job_result", job_id=sub["job_id"])["result"]

    by = {r["name"]: r for r in res["results"]}
    assert by["cd_generous"]["state"] == "pass", by["cd_generous"]
    assert by["cd_impossible"]["state"] == "fail", by["cd_impossible"]
    # the measurement is a real solve on the real body, near the sphere drag curve
    oracle = cfd.sphere_drag(d_mm, velocity, mu_pa_s=mu, rho_kg_m3=rho)
    assert abs(by["cd_generous"]["measured"] / oracle["cd"] - 1.0) < 0.10, (by, oracle)
    assert by["cd_generous"]["job_id"] and by["cd_generous"]["case_dir"], by
    # a band demand nothing in this run can answer refuses to pass, and says why
    tight = by["cd_needs_tight_band"]
    assert tight["state"] == "indeterminate", tight
    assert tight["trust_reasons"], tight
    assert res["ok"] is False and res["escalate"] == ["cd_needs_tight_band"], res
    assert res["passed"] == 1 and res["failed"] == 1 and res["indeterminate"] == 1
    print(f"    aero contract: Cd {by['cd_generous']['measured']:.4g} vs curve "
          f"{oracle['cd']:.4g}; pass/fail/indeterminate all proved through a real solve")


# --- live: the merge gate consults the contract (#261) ---------------------------
#
# Real components, real merge, no solver: the requirement's metric comes from the
# analytic pipe screen (cfd_pipe_flow), so the whole four-way gate runs in seconds.

_PIPE = {"diameter_mm": 10, "length_mm": 1000, "flow_rate_lpm": 0.5,
         "fluid": "water-20c"}          # laminar -> 34.0 Pa, exact, no band


def _dp_requirement(limit_pa):
    return {"name": "dp_at_rated", "metric": "pressure_drop_pa",
            "tool": "cfd_pipe_flow", "conditions": _PIPE,
            "limit": {"max": limit_pa}, "fidelity_floor": "screen",
            "screen": {"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa",
                       "conditions": _PIPE}}


def _pipe_component(worker, path, name, limit_pa=None, verify=True):
    """One saved component. `limit_pa=None` declares no contract at all;
    `verify=False` declares one and never proves it."""
    worker.call("new_document", name=name)
    h = worker.call("add_primitive", kind="cylinder", radius=5, height=1000,
                    name=name)["handle"]
    if limit_pa is not None:
        worker.call("declare_performance", handle=h,
                    requirements=[_dp_requirement(limit_pa)])
        if verify:
            worker.call("verify_performance", handle=h, tier="screen")
    worker.call("save_document", path=str(path))
    return h


def _merge_one(worker, tmp, cfile, tag):
    man = {"name": f"asm_{tag}", "root": str(tmp / f"asm_{tag}.FCStd"),
           "components": {"pipe": {"file": str(cfile)}},
           "instances": [{"component": "pipe", "name": "pipe",
                          "placement": [0, 0, 0]}]}
    mpath = tmp / f"m_{tag}.json"
    mpath.write_text(json.dumps(man), encoding="utf-8")
    return worker.call("merge_assembly", manifest=str(mpath))


def test_merge_assembly_gates_on_a_declared_performance_contract():
    """The merge gate, four-sided and live. A met contract merges; an unmet one is
    BLOCKED with the requirement named; an unverified one is neither (it surfaces as
    its own outcome and the merge is not failed for it); and a component that declares
    nothing produces no performance block whatsoever — the pre-#261 report, exactly."""
    from ankusdrive import Worker
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # The builder session saves; a SEPARATE coordinator session merges — so this
        # also proves the contract and its verdict survive the .FCStd round trip.
        with Worker() as w:
            _pipe_component(w, tmp / "met.FCStd", "met", limit_pa=100.0)
            _pipe_component(w, tmp / "unmet.FCStd", "unmet", limit_pa=1.0)
            _pipe_component(w, tmp / "unverified.FCStd", "unverified",
                            limit_pa=100.0, verify=False)
            _pipe_component(w, tmp / "plain.FCStd", "plain")
        with Worker() as w:
            met = _merge_one(w, tmp, tmp / "met.FCStd", "met")
            unmet = _merge_one(w, tmp, tmp / "unmet.FCStd", "unmet")
            unver = _merge_one(w, tmp, tmp / "unverified.FCStd", "unver")
            plain = _merge_one(w, tmp, tmp / "plain.FCStd", "plain")

    # 1. MET — merges, and the report says so with the measurement behind it
    assert met["ok"] is True, met["gates"]
    assert met["gates"]["performance"] == [], met["gates"]["performance"]
    assert met["performance"]["outcome"] == pgate.MET, met["performance"]
    row = met["performance"]["components"]["pipe"]["requirements"][0]
    assert row["state"] == pgate.PASS and abs(row["measured"] - 34.0) < 1.0, row

    # 2. UNMET — blocked, and the violation NAMES the requirement
    assert unmet["ok"] is False, unmet
    viol = unmet["gates"]["performance"]
    assert len(viol) == 1 and viol[0]["name"] == "dp_at_rated", viol
    assert viol[0]["component"] == "pipe" and viol[0]["requirement"] == "performance"
    assert unmet["performance"]["outcome"] == pgate.UNMET, unmet["performance"]

    # 3. UNVERIFIED — its own explicit outcome. Not a violation (nothing was measured,
    #    so nothing about the PART failed) and emphatically not a pass.
    assert unver["gates"]["performance"] == [], unver["gates"]["performance"]
    assert unver["performance"]["outcome"] == pgate.UNPROVEN, unver["performance"]
    assert unver["performance"]["ok"] is False, unver["performance"]
    assert unver["performance"]["skipped"] == ["pipe:dp_at_rated"], unver["performance"]
    assert pgate.has_verdict(unver["performance"]) is False

    # 4. NO CONTRACT — the report is the one it always was. This is the regression
    #    guard for the 99 % of parts that declare nothing.
    assert plain["ok"] is True, plain
    assert "performance" not in plain, sorted(plain)
    assert "performance" not in plain["gates"], sorted(plain["gates"])


def test_editing_a_verified_part_makes_the_gate_report_stale_not_met():
    """A verdict is about the shape that was measured. Verify a part, then change it,
    and the gate must stop honoring the old answer — the conservative half of "a gate
    never treats absence of evidence as evidence"."""
    from ankusdrive import Worker
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with Worker() as w:
            h = _pipe_component(w, tmp / "before.FCStd", "drift", limit_pa=100.0)
            # same part, bigger bore, saved alongside: the recorded verdict now
            # describes a shape that no longer exists
            w.call("set_property", handle=h, name="Radius", value=25.0)
            w.call("save_document", path=str(tmp / "after.FCStd"))
        with Worker() as w:
            fresh = _merge_one(w, tmp, tmp / "before.FCStd", "fresh")
        with Worker() as w:
            drifted = _merge_one(w, tmp, tmp / "after.FCStd", "drifted")

    assert fresh["performance"]["outcome"] == pgate.MET, fresh["performance"]
    assert drifted["performance"]["outcome"] == pgate.UNPROVEN, drifted["performance"]
    comp = drifted["performance"]["components"]["pipe"]
    assert comp["stale"] is True, comp
    assert comp["requirements"][0]["state"] == pgate.STALE, comp
    assert "re-run verify_performance" in comp["requirements"][0]["skipped_reason"]
    assert drifted["ok"] is True, drifted     # a non-verdict does not fail a merge...
    assert drifted["performance"]["ok"] is False, drifted   # ...nor pass one


# --- runner -------------------------------------------------------------------

def _discover():
    return [(name, fn) for name, fn in sorted(globals().items())
            if name.startswith("test_") and callable(fn)]


def main():
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

"""Performance-contract toys (issue #226) — the spec as a checkable object.

Two tiers:
  * **arithmetic** (always, no FreeCAD, no solver): the three-state verdict. The
    interesting case is not pass or fail, it is the band that straddles the limit —
    a correlation reading Cd = 0.28 ± 10 % against `max: 0.30` spans 0.252–0.308 and has
    NOT shown the part passes. Everything here is two-sided: a case that must pass, a
    case that must fail, and the case that must refuse to decide.
  * **live** (needs FreeCAD): declare a contract on a real part and verify it end to end
    through the worker, including the fidelity ladder and the trust gate.

Run:  python3 tests/test_performance.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin.analysis import performance as pf  # noqa: E402


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


# --- live: the contract on a real part ------------------------------------------

def test_contract_round_trips_and_verifies_on_a_part():
    """Declare a Δp limit whose metric comes from the analytic pipe screen, and verify
    it end to end: a generous limit passes, an impossible one fails, and a limit sitting
    inside the correlation band comes back indeterminate rather than guessing."""
    from driftpin import Worker
    from driftpin.client import WorkerError

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
    from driftpin import Worker, solvers
    from driftpin.analysis import cfd
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

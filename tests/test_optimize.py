"""Optimize-to-spec (issue #228) — vary parameters until the contract is met, then say
whether it was PROVEN.

Two tiers:
  * **pure** (always, no FreeCAD, no solver): the search arithmetic. Bounded
    Nelder-Mead on closed-form functions, including the case that matters most in real
    design work — the optimum sitting ON a bound rather than in the interior — plus the
    penalty algebra and the "converged but inside the noise" test.
  * **live** (needs FreeCAD): the loop through the worker. The load-bearing one is the
    oracle toy: minimize pipe Δp at fixed flow subject to a scour-velocity floor. Both
    the objective and the binding constraint are closed-form, so the optimum has an
    EXACT answer (D = sqrt(4Q/(pi*v_min))) and the optimizer either finds it or does not.

Run:  python3 tests/test_optimize.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.analysis import optimize as op  # noqa: E402

# the oracle toy, shared by the live tests
Q_LPM = 2.0
V_MIN = 0.5
_Q_M3S = Q_LPM / 1000.0 / 60.0
D_EXACT_MM = math.sqrt(4 * _Q_M3S / (math.pi * V_MIN)) * 1000.0      # 9.2132 mm
CONDS = {"diameter_mm": "$diameter_mm", "length_mm": 1000,
         "flow_rate_lpm": Q_LPM, "fluid": "water-20c"}
# a separate, faster flow for the band test: 12 mm at 2 L/min is only Re 3.5e3
# (transitional, and cfd_pipe_flow declines to band a transitional point at all)
TURB_LPM = 8.0
TURB_CONDS = {"diameter_mm": "$diameter_mm", "length_mm": 1000,
              "flow_rate_lpm": TURB_LPM, "fluid": "water-20c"}


# --- declaring the search -------------------------------------------------------

def test_design_vars_must_be_bounded():
    got = op.validate_design_vars([{"name": "d", "min": 5, "max": 25}])
    assert got[0]["start"] == 15.0                       # midpoint by default
    assert op.validate_design_vars(
        [{"name": "d", "min": 5, "max": 25, "start": 7}])[0]["start"] == 7.0
    # a start outside the box is clamped into it rather than refused
    assert op.validate_design_vars(
        [{"name": "d", "min": 5, "max": 25, "start": 99}])[0]["start"] == 25.0
    for bad in ([],
                [{"min": 1, "max": 2}],                  # no name
                [{"name": "d", "values": [1, 2]}],       # levels are not a search space
                [{"name": "d", "min": 5}],               # half-bounded
                [{"name": "d", "min": 5, "max": 5}],     # empty box
                [{"name": "d", "min": 9, "max": 2}],     # inverted
                [{"name": "d", "min": 1, "max": 2}, {"name": "d", "min": 1, "max": 2}],
                ["not a dict"]):
        try:
            op.validate_design_vars(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_design_vars({bad!r}) should have raised")


def test_objective_and_budget_are_policed():
    ok = op.validate_objective({"tool": "cfd_pipe_flow", "metric": "pressure_drop_pa"})
    assert ok["sense"] == "min" and ok["name"] == "pressure_drop_pa"
    for bad in ("nope", {}, {"tool": "t"}, {"metric": "m"},
                {"tool": "t", "metric": "m", "sense": "sideways"},
                {"tool": "t", "metric": "m", "screen": {"metric": "m"}}):
        try:
            op.validate_objective(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_objective({bad!r}) should have raised")

    assert op.validate_budget(None) == {"max_evals": 40, "max_wall_s": None}
    assert op.validate_budget({"max_evals": 5})["max_evals"] == 5
    for bad in ({"max_evals": 1}, {"max_wall_s": 0}, {"max_wall_s": -3}):
        try:
            op.validate_budget(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_budget({bad!r}) should have raised")


# --- the search -----------------------------------------------------------------

def test_nelder_mead_finds_an_interior_optimum():
    calls = {"n": 0}

    def f(x):
        calls["n"] += 1
        return (x[0] - 3.0) ** 2 + 1.0

    got = op.nelder_mead(f, [0.0], [-10.0], [10.0], max_evals=80)
    assert abs(got["x"][0] - 3.0) < 1e-3, got
    assert abs(got["fx"] - 1.0) < 1e-5
    assert got["converged"] is True and got["reason"] in ("xtol", "ftol")
    assert got["n_evals"] == calls["n"] <= 80


def test_nelder_mead_rides_a_bound():
    """The normal outcome of a real design problem: the optimum is against the envelope,
    not in the interior. A search that rejects out-of-box trials instead of clamping
    them stalls just short of the bound."""
    got = op.nelder_mead(lambda x: x[0], [7.0], [2.0], [10.0], max_evals=60)
    assert abs(got["x"][0] - 2.0) < 1e-3, got
    # and in two dimensions, into a corner
    got2 = op.nelder_mead(lambda x: x[0] + x[1], [5.0, 5.0], [1.0, -2.0], [9.0, 9.0],
                          max_evals=200)
    assert abs(got2["x"][0] - 1.0) < 1e-2 and abs(got2["x"][1] - (-2.0)) < 1e-2, got2


def test_nelder_mead_handles_two_dimensions_and_unmeasurable_points():
    def rosen(x):
        return (1 - x[0]) ** 2 + 100 * (x[1] - x[0] ** 2) ** 2

    got = op.nelder_mead(rosen, [-1.0, 1.0], [-3.0, -3.0], [3.0, 3.0], max_evals=400)
    assert got["fx"] < 1e-3, got                    # near (1, 1)

    # a point the tool could not measure is infinitely bad, not a crash: one failed
    # solve must not discard the whole run
    def flaky(x):
        return None if x[0] > 2.0 else (x[0] - 1.0) ** 2

    out = op.nelder_mead(flaky, [0.0], [-5.0], [5.0], max_evals=80)
    assert abs(out["x"][0] - 1.0) < 1e-2, out


def test_the_budget_may_be_owned_by_the_caller_so_cache_hits_are_free():
    """A simplex revisits coordinates constantly. If results served from a cache counted
    against max_evals, bookkeeping rather than physics would end the search — measured
    live as the difference between landing 0.026 mm and 0.0022 mm from a known optimum."""
    seen, real = {}, {"n": 0}

    def f(x):
        key = round(x[0], 9)
        if key not in seen:
            real["n"] += 1
            seen[key] = (x[0] - 3.0) ** 2
        return seen[key]

    got = op.nelder_mead(f, [0.0], [-10.0], [10.0], max_evals=12,
                         spent=lambda: real["n"])
    assert real["n"] <= 12, real                     # the budget is uncached calls ...
    assert got["n_evals"] >= real["n"]               # ... and the simplex made more
    assert abs(got["x"][0] - 3.0) < 1e-2, got

    # without `spent`, the internal counter is the budget (back-compatible)
    plain = op.nelder_mead(lambda x: (x[0] - 3.0) ** 2, [0.0], [-10.0], [10.0],
                           max_evals=7)
    assert plain["n_evals"] <= 7, plain


# --- the contract arithmetic on top of the search --------------------------------

def test_violations_are_relative_so_constraints_of_different_scale_trade_off():
    assert op.relative_violation(0.25, {"max": 0.30}) == 0.0        # satisfied
    assert op.relative_violation(None, {"max": 0.30}) == 0.0        # unmeasured
    assert abs(op.relative_violation(0.33, {"max": 0.30}) - 0.1) < 1e-12
    assert abs(op.relative_violation(45.0, {"min": 50.0}) - 0.1) < 1e-12
    # a 10 % miss on a drag coefficient and a 10 % miss on a pressure drop weigh the
    # same, which is the point of scaling by the limit
    assert abs(op.relative_violation(0.33, {"max": 0.30})
               - op.relative_violation(55.0, {"max": 50.0})) < 1e-12


def test_score_flips_for_maximization_and_punishes_infeasibility():
    assert op.score(5.0, "min", []) == 5.0
    assert op.score(5.0, "max", []) == -5.0
    # a feasible-but-mediocre point must beat an infeasible-but-brilliant one
    assert op.score(100.0, "min", []) < op.score(1.0, "min", [0.01])


def test_a_margin_inside_its_own_band_is_not_proof():
    """The stopping condition the trust layer adds on top of the optimizer's tolerance:
    clearing a limit by 2 % with a 5 %-wide band is noise with a favourable sign."""
    assert op.band_covers_margin(2.0, 5.0) is True
    assert op.band_covers_margin(8.0, 5.0) is False
    assert op.band_covers_margin(-3.0, 5.0) is True        # a miss is still inside it
    # nothing to compare against is not a failure — an exact result has no band
    assert op.band_covers_margin(2.0, None) is False
    assert op.band_covers_margin(None, 5.0) is False


# --- live: the loop through the worker -------------------------------------------

def _await(w, job_id, timeout_s=300):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = w.call("job_status", job_id=job_id)
        if st["status"] in ("done", "failed"):
            got = w.call("job_result", job_id=job_id)
            assert st["status"] == "done", got
            return got["result"]
        time.sleep(0.1)
    raise AssertionError(f"optimize {job_id} did not finish in {timeout_s}s")


def _scour_constraint(v_min=V_MIN):
    """Bulk velocity is exact kinematics, so it declares band_pct 0 rather than
    inheriting the friction correlation's ±10 %."""
    block = {"tool": "cfd_pipe_flow", "metric": "velocity_m_s",
             "conditions": CONDS, "band_pct": 0}
    return {"name": "scour_velocity", "limit": {"min": v_min},
            "fidelity_floor": "screen", "screen": dict(block), **block}


def test_the_optimizer_rides_the_constraint_to_a_known_optimum():
    """THE gate for this layer (issue #228). Minimize Δp at fixed flow — which wants the
    pipe as WIDE as possible — subject to a minimum bulk velocity, which wants it as
    NARROW as possible. Both are closed-form, so the optimum is exactly where the
    constraint binds: D = sqrt(4Q/(pi*v_min)). Riding a binding constraint to a known
    answer is the whole job."""
    from ankusdrive import Worker

    with Worker() as w:
        sub = w.call(
            "optimize_submit",
            variables=[{"name": "diameter_mm", "min": 5.0, "max": 25.0, "start": 15.0}],
            objective={"name": "dp", "tool": "cfd_pipe_flow",
                       "metric": "pressure_drop_pa", "sense": "min",
                       "conditions": CONDS},
            constraints=[_scour_constraint()],
            budget={"max_evals": 40}, tier="screen")
        assert sub.get("job_id"), sub
        got = _await(w, sub["job_id"])

        assert got["ok"] is True, got
        found = got["best_params"]["diameter_mm"]
        assert abs(found - D_EXACT_MM) < 0.02, (found, D_EXACT_MM, got["history"][-3:])
        # it converged from ABOVE the optimum (start 15 mm) down onto the constraint,
        # so the constraint is satisfied but only just — that is what binding means
        con = got["constraints"][0]
        assert con["state"] == "pass", con
        assert 0 <= con["margin_pct"] < 1.0, con
        assert got["proven"] is True, got["warnings"]
        assert got["warnings"] == [], got["warnings"]
        # the search is auditable in-band
        assert len(got["history"]) >= 5
        assert all(set(h["params"]) == {"diameter_mm"} for h in got["history"])
        # a shrinking simplex revisits coordinates; those came from cache and did not
        # spend budget, which is why the search got this close on 40 evaluations
        assert got["n_cached"] >= 1, got
        assert got["n_evals"] <= 41, got["n_evals"]
        assert any(h["cached"] for h in got["history"]), got["history"]
        assert got["phases"][0]["tier"] == "screen"


def test_an_infeasible_spec_returns_the_best_margin_not_a_false_pass():
    """The two-sided half. A velocity floor of 5 m/s is unreachable anywhere in the box
    (the narrowest allowed pipe gives ~1.7 m/s), so the honest answer is 'not proven,
    here is how close it got' — never a pass, and never an exception."""
    from ankusdrive import Worker

    with Worker() as w:
        got = _await(w, w.call(
            "optimize_submit",
            variables=[{"name": "diameter_mm", "min": 5.0, "max": 25.0, "start": 15.0}],
            objective={"name": "dp", "tool": "cfd_pipe_flow",
                       "metric": "pressure_drop_pa", "sense": "min",
                       "conditions": CONDS},
            constraints=[_scour_constraint(v_min=5.0)],
            budget={"max_evals": 30}, tier="screen")["job_id"])

        assert got["ok"] is True, got               # it ran fine; the SPEC is the problem
        assert got["proven"] is False, got
        con = got["constraints"][0]
        assert con["state"] == "fail", con
        assert con["margin"] < 0, con               # and by how much
        assert con["measured"] is not None
        # it still pushed to the best it could reach — the narrow end of the box
        assert abs(got["best_params"]["diameter_mm"] - 5.0) < 0.05, got["best_params"]
        assert any("NOT met" in wmsg for wmsg in got["warnings"]), got["warnings"]


def test_a_margin_inside_the_band_is_reported_unproven():
    """A constraint measured by the turbulent correlation carries ±10 %. Put the limit
    just under the measured value and the verdict must be indeterminate — the design may
    well be fine, but this measurement has not shown it, so `proven` stays False."""
    from ankusdrive import Worker

    with Worker() as w:
        # 12 mm at 2 L/min is turbulent, so cfd_pipe_flow reports band_pct 10
        probe = w.call("cfd_pipe_flow", diameter_mm=12, length_mm=1000,
                       flow_rate_lpm=TURB_LPM, fluid="water-20c")
        assert probe["band_pct"] == 10.0 and probe["regime"] == "turbulent", probe
        limit = probe["pressure_drop_pa"] * 1.02          # inside the ±10 % band

        got = _await(w, w.call(
            "optimize_submit",
            variables=[{"name": "diameter_mm", "min": 11.5, "max": 12.5,
                        "start": 12.0}],
            objective={"name": "dp", "tool": "cfd_pipe_flow",
                       "metric": "pressure_drop_pa", "sense": "min",
                       "conditions": TURB_CONDS},
            constraints=[{"name": "dp_cap", "tool": "cfd_pipe_flow",
                          "metric": "pressure_drop_pa", "conditions": TURB_CONDS,
                          "limit": {"max": limit}, "fidelity_floor": "screen",
                          "screen": {"tool": "cfd_pipe_flow",
                                     "metric": "pressure_drop_pa",
                                     "conditions": TURB_CONDS}}],
            budget={"max_evals": 12}, tier="screen")["job_id"])

        con = got["constraints"][0]
        assert con["state"] in ("indeterminate", "pass"), con
        if con["state"] == "indeterminate":
            assert got["proven"] is False, got
            assert any("could not be decided" in m for m in got["warnings"]), got
        else:
            # if the search escaped the band entirely it must have done so honestly
            assert con["band_pct"] == 10.0 and con["margin_pct"] > 10.0, con


def test_the_budget_is_a_ceiling_and_cache_hits_do_not_count():
    from ankusdrive import Worker

    with Worker() as w:
        got = _await(w, w.call(
            "optimize_submit",
            variables=[{"name": "diameter_mm", "min": 5.0, "max": 25.0, "start": 15.0}],
            objective={"name": "dp", "tool": "cfd_pipe_flow",
                       "metric": "pressure_drop_pa", "sense": "min",
                       "conditions": CONDS},
            constraints=[_scour_constraint()],
            budget={"max_evals": 8}, tier="screen")["job_id"])

        # the acceptance measurement is the only evaluation allowed past the ceiling,
        # and it is normally a cache hit on the winner
        # the ceiling holds; the acceptance measurement of the winner is the only
        # evaluation allowed past it
        assert got["n_evals"] <= 9, got["n_evals"]
        assert len([h for h in got["history"] if not h["cached"]]) <= 8, got["history"]
        assert got["stop_reason"] in ("max_evals", "converged"), got["stop_reason"]


def test_an_unbindable_handle_is_refused_at_the_door():
    """Live geometry is no longer refused (#260 gave the search a main-thread queue),
    but `"$handle"` still needs something to bind to. A response referencing the part
    THIS candidate built, in an optimization that builds nothing, must be refused at
    the door — substituting nothing deep inside the search is how a sweep ends up
    measuring a literal string at every point."""
    from ankusdrive import Worker
    from ankusdrive.client import WorkerError

    with Worker() as w:
        try:
            w.call("optimize_submit",
                   variables=[{"name": "v", "min": 1.0, "max": 2.0}],
                   objective={"tool": "cfd_body_drag", "metric": "cd",
                              "conditions": {"model": "$handle", "velocity_m_s": 30}},
                   budget={"max_evals": 4})
        except WorkerError as e:
            assert "recipe" in str(e), e             # names what would fix it
        else:
            raise AssertionError("$handle with nothing to build should have raised")

        # a recipe with nowhere to build is refused on the REQUEST thread, where the
        # caller is still listening — not once per candidate inside the search
        try:
            w.call("optimize_submit",
                   variables=[{"name": "width_mm", "min": 4.0, "max": 20.0}],
                   objective={"tool": "mass_properties", "metric": "volume_mm3",
                              "sense": "min", "conditions": {"handle": "$handle"}},
                   recipe="spur_gear", budget={"max_evals": 4})
        except WorkerError as e:
            assert "new_document" in str(e), e
        else:
            raise AssertionError("a recipe with no active document should have raised")

        # an unknown tool is refused too, before anything is searched
        try:
            w.call("optimize_submit",
                   variables=[{"name": "v", "min": 1.0, "max": 2.0}],
                   objective={"tool": "no_such_tool", "metric": "cd"},
                   budget={"max_evals": 4})
        except WorkerError:
            pass
        else:
            raise AssertionError("an unknown objective tool should have raised")


def test_shape_optimization_rides_the_constraint_to_a_known_optimum():
    """THE gate for #260: a search that varies GEOMETRY, not just numbers.

    Minimize a spur gear's volume — which wants the face as NARROW as possible —
    subject to a Lewis bending safety factor of 1, which wants it WIDE. The recipe is
    rebuilt for every candidate and the objective is measured off the resulting SOLID
    (`mass_properties` on `"$handle"`), so this exercises the whole main-thread queue:
    a background search asking the request loop to build, once per evaluation.

    Both legs are linear in face width, so the optimum is exactly where the constraint
    binds — width = w0/SF(w0) — the same closed-form oracle shape the parametric gate
    uses, which is what makes "it found it" mean something."""
    from ankusdrive import Worker

    power_w, module_mm, teeth, rpm = 10_000.0, 2.0, 24, 1200
    gear = {"module_mm": module_mm, "teeth": teeth,
            "face_width_mm": "$width_mm", "power_w": power_w,
            "pinion_speed_rpm": rpm}
    # bending_sf is a closed-form ratio, not a correlation — it declares band 0
    block = {"tool": "gear_rating", "metric": "bending_sf",
             "conditions": gear, "band_pct": 0}

    with Worker() as w:
        w.call("new_document", name="shapeopt")
        # SF is linear in face width, so one probe fixes the exact answer
        probe = w.call("gear_rating", module_mm=module_mm, teeth=teeth,
                       face_width_mm=14.0, power_w=power_w, pinion_speed_rpm=rpm)
        exact_mm = 14.0 / probe["bending_sf"]
        assert 5.0 < exact_mm < 19.0, exact_mm   # the bounds must BRACKET it, or the
        #                                          search rides a bound and proves nothing

        sub = w.call(
            "optimize_submit",
            variables=[{"name": "width_mm", "min": 4.0, "max": 20.0, "start": 16.0}],
            objective={"name": "vol", "tool": "mass_properties",
                       "metric": "volume_mm3", "sense": "min",
                       "conditions": {"handle": "$handle"}},
            constraints=[{"name": "bending", "limit": {"min": 1.0},
                          "fidelity_floor": "screen",
                          "screen": dict(block), **block}],
            recipe="spur_gear",
            fixed_inputs={"module_mm": module_mm, "teeth": teeth},
            budget={"max_evals": 30}, tier="screen")
        assert sub.get("job_id"), sub
        got = _await(w, sub["job_id"], timeout_s=600)

        assert got["ok"] is True, got
        found = got["best_params"]["width_mm"]
        assert abs(found - exact_mm) < 0.1, (found, exact_mm, got["history"][-3:])
        # the constraint BINDS: it is met, and met with nothing to spare
        con = got["constraints"][0]
        assert con["state"] == "pass", con
        assert 0 <= con["margin_pct"] < 1.0, con
        assert got["proven"] is True, got["warnings"]
        # the objective really was read off rebuilt geometry: volume must track the
        # gear's own footprint at that width, not some parametric stand-in
        built = w.call("recipe", recipe="spur_gear",
                       inputs={"module_mm": module_mm, "teeth": teeth,
                               "width_mm": found})
        direct = w.call("mass_properties", handle=built["handle"])["volume_mm3"]
        assert abs(got["best_value"] - direct) / direct < 0.01, (got["best_value"],
                                                                 direct)
        # every candidate was built, so the history carries what it searched over
        assert len(got["history"]) >= 5, got["history"]
        assert got["n_cached"] >= 1, got            # a shrinking simplex revisits


def test_a_shape_search_that_cannot_build_says_why():
    """A search where every candidate fails to build must name the CAUSE. Reporting
    only "no point could be measured" is true and useless — it is the symptom, and the
    same silent-absence trap the modal gate (#248) and the performance gates (#261)
    were fixed for. The build error rides out in `warnings`, deduped."""
    from ankusdrive import Worker

    with Worker() as w:
        w.call("new_document", name="badshape")
        sub = w.call(
            "optimize_submit",
            # the recipe refuses a module below 0.2 mm, so EVERY candidate fails to
            # build — standing in for any parameter range the recipe cannot take
            variables=[{"name": "width_mm", "min": 4.0, "max": 20.0, "start": 10.0}],
            objective={"name": "vol", "tool": "mass_properties",
                       "metric": "volume_mm3", "sense": "min",
                       "conditions": {"handle": "$handle"}},
            recipe="spur_gear", fixed_inputs={"module_mm": 0.05, "teeth": 24},
            budget={"max_evals": 6}, tier="screen")
        got = _await(w, sub["job_id"], timeout_s=300)

        assert got["ok"] is False, got
        assert got["best_params"] is None, got
        joined = " ".join(got["warnings"])
        assert "did not build" in joined, got["warnings"]
        assert "module_mm" in joined, got["warnings"]  # the CAUSE, not the symptom
        # deduped: one repeated reason is one finding, not one per evaluation
        assert len([x for x in got["warnings"] if "did not build" in x]) == 1, \
            got["warnings"]


def test_an_optimization_with_no_constraints_says_it_proved_nothing():
    """Optimizing an objective is not the same as meeting a spec. Without a constraint
    there is no spec, and reporting `proven` would be meaningless."""
    from ankusdrive import Worker

    with Worker() as w:
        got = _await(w, w.call(
            "optimize_submit",
            variables=[{"name": "diameter_mm", "min": 5.0, "max": 25.0}],
            objective={"name": "dp", "tool": "cfd_pipe_flow",
                       "metric": "pressure_drop_pa", "sense": "min",
                       "conditions": CONDS},
            budget={"max_evals": 10}, tier="screen")["job_id"])

        assert got["ok"] is True and got["proven"] is False, got
        # unconstrained, minimum Δp is the widest pipe the box allows
        assert abs(got["best_params"]["diameter_mm"] - 25.0) < 0.05, got["best_params"]
        assert any("nothing to prove" in m for m in got["warnings"]), got["warnings"]


def _discover():
    mod = sys.modules[__name__]
    return [(n, getattr(mod, n)) for n in sorted(dir(mod))
            if n.startswith("test_") and callable(getattr(mod, n))]


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

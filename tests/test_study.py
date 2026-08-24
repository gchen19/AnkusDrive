"""The DOE / study engine (issue #227) — the parameter search, recorded.

Two tiers:
  * **pure** (always, no FreeCAD, no solver): sampling and table arithmetic. The
    interesting properties are that a grid is exhaustive, a Latin hypercube uses every
    stratum of every axis exactly once, and both are reproducible from `seed` alone —
    a study you cannot re-run point-for-point is not evidence, and (because the job
    layer caches on content hash) a non-reproducible sample sequence would also defeat
    resumability.
  * **live** (needs FreeCAD): studies through the worker. The load-bearing one is the
    oracle gate — sweep pipe diameter and the swept table must reproduce the
    Hagen-Poiseuille D^-4 law. That is a much stronger claim than any single point
    matching a correlation: it says the SWEEP transports parameters correctly.

Run:  python3 tests/test_study.py
"""
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive.analysis import study as st  # noqa: E402


# --- declaring the space --------------------------------------------------------

def test_variables_normalize_from_either_spelling():
    got = st.validate_variables([
        {"name": "d", "values": [8, 10, 12]},
        {"name": "L", "min": 100, "max": 300, "levels": 3},
    ])
    assert [v["name"] for v in got] == ["d", "L"]
    assert got[0]["kind"] == "levels" and got[0]["values"] == [8, 10, 12]
    assert got[0]["min"] == 8 and got[0]["max"] == 12
    assert got[1]["kind"] == "range" and got[1]["values"] == [100.0, 200.0, 300.0]
    # a single variable may be given unwrapped
    assert len(st.validate_variables({"name": "d", "values": [1]})) == 1
    # one level of a range means "hold it here" -> the midpoint
    assert st.validate_variables([{"name": "d", "min": 2, "max": 4,
                                   "levels": 1}])[0]["values"] == [3.0]


def test_malformed_declarations_are_refused_at_the_door():
    bad_vars = [
        [],                                                    # nothing to sweep
        [{"values": [1, 2]}],                                  # no name
        [{"name": "d"}],                                       # neither values nor range
        [{"name": "d", "values": []}],                         # empty level list
        [{"name": "d", "min": 5, "max": 1}],                   # inverted range
        [{"name": "d", "min": "wide", "max": 4}],              # non-numeric bound
        [{"name": "d", "min": 1, "max": 4, "levels": 0}],      # no levels
        [{"name": "d", "values": [1]}, {"name": "d", "values": [2]}],   # duplicate
        ["not a dict"],
    ]
    for spec in bad_vars:
        try:
            st.validate_variables(spec)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_variables({spec!r}) should have raised")

    bad_resp = [
        [],                                                    # nothing measured
        [{"tool": "cfd_pipe_flow"}],                           # unnameable
        [{"name": "dp"}],                                      # no measuring tool
        [{"name": "dp", "tool": "t", "conditions": "none"}],    # conditions not a dict
        [{"name": "dp", "tool": "t"}, {"name": "dp", "tool": "u"}],     # duplicate
        ["not a dict"],
    ]
    for spec in bad_resp:
        try:
            st.validate_responses(spec)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_responses({spec!r}) should have raised")


def test_a_response_may_be_named_after_its_metric():
    got = st.validate_responses([{"tool": "cfd_pipe_flow",
                                  "metric": "pressure_drop_pa"}])
    assert got[0]["name"] == "pressure_drop_pa" and got[0]["conditions"] == {}


# --- sampling -------------------------------------------------------------------

def test_a_grid_is_every_combination_in_a_stable_order():
    variables = st.validate_variables([
        {"name": "d", "values": [8, 10, 12]},
        {"name": "L", "values": [100, 200]},
    ])
    pts = st.sample_points(variables, method="grid")
    assert len(pts) == 6, pts
    # the LAST variable varies fastest (odometer order), and every pair appears once
    assert pts[0] == {"d": 8, "L": 100} and pts[1] == {"d": 8, "L": 200}
    assert len({(p["d"], p["L"]) for p in pts}) == 6
    # deterministic: same declaration, same sequence — this is what makes a re-run
    # hit the job cache instead of re-solving
    assert st.sample_points(variables, method="grid") == pts


def test_latin_hypercube_uses_every_stratum_of_every_axis_once():
    """The defining property of LHS, and the reason it beats random sampling: no axis
    is ever under-covered, however many axes there are."""
    variables = st.validate_variables([
        {"name": "a", "min": 0.0, "max": 1.0},
        {"name": "b", "min": 10.0, "max": 20.0},
        {"name": "c", "min": -5.0, "max": 5.0},
    ])
    n = 8
    pts = st.sample_points(variables, method="lhs", n_samples=n, seed=7)
    assert len(pts) == n
    for var in variables:
        lo, hi = var["min"], var["max"]
        width = (hi - lo) / n
        vals = [p[var["name"]] for p in pts]
        assert all(lo <= v <= hi for v in vals), (var["name"], vals)
        strata = sorted(min(int((v - lo) / width), n - 1) for v in vals)
        assert strata == list(range(n)), (var["name"], strata)


def test_latin_hypercube_is_reproducible_and_seed_sensitive():
    variables = st.validate_variables([{"name": "a", "min": 0.0, "max": 1.0},
                                       {"name": "b", "min": 0.0, "max": 1.0}])
    same = st.sample_points(variables, "lhs", n_samples=6, seed=3)
    assert same == st.sample_points(variables, "lhs", n_samples=6, seed=3)
    assert same != st.sample_points(variables, "lhs", n_samples=6, seed=4)
    # a discrete variable stays on its declared levels under LHS — a list means
    # "these and only these" (stock sizes, tooth counts), never something between
    disc = st.validate_variables([{"name": "m", "values": ["alu", "steel"]}])
    picks = st.sample_points(disc, "lhs", n_samples=6, seed=1)
    assert {p["m"] for p in picks} <= {"alu", "steel"}, picks


def test_sampling_refuses_what_it_cannot_do():
    variables = st.validate_variables([{"name": "a", "min": 0.0, "max": 1.0}])
    for kwargs in ({"method": "sobol"},                       # unknown method
                   {"method": "lhs"},                         # LHS with no size
                   {"method": "lhs", "n_samples": 0}):
        try:
            st.sample_points(variables, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"sample_points({kwargs!r}) should have raised")


# --- the recorded table ---------------------------------------------------------

def _rows():
    return [
        {"index": 0, "params": {"d": 8}, "responses": {"dp": {"ok": True, "value": 40.0}}},
        {"index": 1, "params": {"d": 10}, "responses": {"dp": {"ok": True, "value": 16.0}}},
        {"index": 2, "params": {"d": 12}, "responses": {"dp": {"ok": True, "value": 8.0}}},
    ]


def test_summary_reports_the_parameters_not_the_index():
    got = st.summarize_responses(_rows(), ["dp"])["dp"]
    assert got["n"] == 3 and got["n_missing"] == 0
    assert got["min"] == 8.0 and got["max"] == 40.0
    assert abs(got["mean"] - (40 + 16 + 8) / 3) < 1e-12
    # an agent must be able to act on this without holding the table
    assert got["argmin"] == {"d": 12} and got["argmax"] == {"d": 8}


def test_unmeasured_points_are_reported_not_hidden():
    rows = _rows() + [
        {"index": 3, "params": {"d": 14},
         "responses": {"dp": {"ok": False, "value": None, "detail": "openfoam absent"}}},
    ]
    got = st.summarize_responses(rows, ["dp"])["dp"]
    assert got["n"] == 3 and got["n_missing"] == 1, got
    assert got["min"] == 8.0                       # the failure does not become a zero
    # a response nothing could measure is a finding, not an omitted column
    none_at_all = st.summarize_responses(rows, ["cd"])["cd"]
    assert none_at_all["n"] == 0 and none_at_all["min"] is None
    assert none_at_all["n_missing"] == 4


def test_best_point_needs_a_real_measurement():
    best = st.best_point(_rows(), {"response": "dp", "sense": "min"})
    assert best["params"] == {"d": 12} and best["value"] == 8.0 and best["index"] == 2
    assert st.best_point(_rows(), {"response": "dp",
                                   "sense": "max"})["params"] == {"d": 8}
    assert st.best_point(_rows(), None) is None
    # a study where nothing measured has NO best point; inventing one would be a lie
    empty = [{"index": 0, "params": {"d": 8},
              "responses": {"dp": {"ok": False, "value": None}}}]
    assert st.best_point(empty, {"response": "dp", "sense": "min"}) is None
    for bad in ({"sense": "min"}, {"response": "dp", "sense": "sideways"}, "min"):
        try:
            st.best_point(_rows(), bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"best_point(..., {bad!r}) should have raised")


def test_power_law_fit_recovers_a_known_exponent():
    """The trend read that turns a swept table into a statement about physics."""
    xs = [8.0, 10.0, 12.0, 16.0]
    ys = [3.0 * x ** -4 for x in xs]
    fit = st.fit_power_law(xs, ys)
    assert abs(fit["exponent"] - (-4.0)) < 1e-9, fit
    assert abs(fit["coefficient"] - 3.0) < 1e-9 and fit["r2"] > 0.999999
    assert fit["n"] == 4
    # a log fit is undefined on non-positive data, and one point is not a trend
    assert st.fit_power_law([1.0], [2.0]) is None
    assert st.fit_power_law([1.0, 2.0], [0.0, 4.0]) is None
    assert st.fit_power_law([2.0, 2.0], [1.0, 4.0]) is None      # no spread in x


# --- live: studies through the worker -------------------------------------------

def test_a_swept_table_reproduces_the_hagen_poiseuille_law():
    """THE gate for this layer (issue #227): sweep pipe diameter through the analytic
    screen and the recorded table must ride Δp ∝ D^-4.

    Any single point matching a correlation only proves the tool works. Recovering the
    exponent from the SWEEP proves the study transported each point's parameters into
    the measuring tool correctly — which is the one thing a study engine can get wrong
    in a way that still looks completely plausible."""
    from ankusdrive import Worker

    with Worker() as w:
        got = w.call(
            "study_submit",
            variables=[{"name": "diameter_mm", "values": [8, 10, 12, 16]}],
            responses=[{"name": "dp", "tool": "cfd_pipe_flow",
                        "metric": "pressure_drop_pa",
                        "conditions": {"diameter_mm": "$diameter_mm",
                                       "length_mm": 1000, "flow_rate_lpm": 0.4,
                                       "fluid": "water-20c"}}],
            objective={"response": "dp", "sense": "min"},
        )
        assert got["ok"] is True, got
        assert got["n_points"] == 4 and got["n_evaluated"] == 4, got
        assert "job_id" not in got, "an analytic screen must not need a job"

        ds = [pt["params"]["diameter_mm"] for pt in got["points"]]
        dps = [pt["responses"]["dp"]["value"] for pt in got["points"]]
        assert ds == [8, 10, 12, 16], ds
        fit = st.fit_power_law(ds, dps)
        # every point is on the exact laminar branch (Re 530..1060), so the only
        # departure from -4 is cfd_pipe_flow rounding its answer to 4 decimals —
        # the exponent still comes back to five significant figures
        assert abs(fit["exponent"] - (-4.0)) < 1e-4, (fit, dps)
        assert fit["r2"] > 0.9999, fit

        # the table's own summary agrees: the widest pipe is the lowest Δp
        assert got["responses"]["dp"]["argmin"] == {"diameter_mm": 16}
        assert got["best"]["params"] == {"diameter_mm": 16}
        assert got["best"]["value"] == min(dps)

        # every point carries the parameters that produced it, so the table is
        # self-describing rather than positional
        assert all(set(pt["params"]) == {"diameter_mm"} for pt in got["points"])

        # and the measured value is the tool's own, not a re-implementation
        direct = w.call("cfd_pipe_flow", diameter_mm=10, length_mm=1000,
                        flow_rate_lpm=0.4, fluid="water-20c")
        assert abs(dps[1] - direct["pressure_drop_pa"]) < 1e-12


def test_two_variables_sweep_as_a_grid_and_carry_evidence():
    from ankusdrive import Worker

    with Worker() as w:
        got = w.call(
            "study_submit",
            variables=[{"name": "diameter_mm", "values": [10, 20]},
                       # spans the laminar/turbulent boundary on purpose: Re runs
                       # 1e3..4e4, so the table mixes exact points with banded ones
                       {"name": "velocity_m_s", "min": 0.1, "max": 2.0, "levels": 3}],
            responses=[{"name": "dp", "tool": "cfd_pipe_flow",
                        "metric": "pressure_drop_pa",
                        "conditions": {"diameter_mm": "$diameter_mm",
                                       "velocity_m_s": "$velocity_m_s",
                                       "length_mm": 1000, "fluid": "water-20c"}},
                       {"name": "re", "tool": "cfd_pipe_flow", "metric": "reynolds",
                        "conditions": {"diameter_mm": "$diameter_mm",
                                       "velocity_m_s": "$velocity_m_s",
                                       "length_mm": 1000, "fluid": "water-20c"}}],
        )
        assert got["n_points"] == 6 and got["ok"] is True, got
        assert got["n_evaluated"] == 12                       # two responses per point
        assert len({(p["params"]["diameter_mm"],
                     p["params"]["velocity_m_s"]) for p in got["points"]}) == 6
        # a turbulent point reports the correlation's own band; a laminar one is exact.
        # Carrying that forward is what lets a later reader tell a trustworthy point
        # from a lucky one.
        bands = [p["responses"]["dp"].get("band_pct") for p in got["points"]]
        assert any(b == 10.0 for b in bands), bands
        assert any(b is None for b in bands), bands
        assert got["responses"]["re"]["max"] > got["responses"]["re"]["min"] > 0


def test_a_bad_substitution_is_refused_before_any_evaluation():
    """A sweep whose response silently measured a literal "$diamter_mm" at every point
    returns a flat, plausible, entirely wrong table. Refuse it at the door."""
    from ankusdrive import Worker
    from ankusdrive.client import WorkerError

    with Worker() as w:
        for bad, why in (
            ({"diameter_mm": "$diamter_mm", "length_mm": 1000,
              "flow_rate_lpm": 0.4}, "typo'd variable name"),
            ({"model": "$handle", "diameter_mm": 10, "length_mm": 1000,
              "flow_rate_lpm": 0.4}, "$handle with no recipe"),
        ):
            got = w.call("study_submit",
                         variables=[{"name": "diameter_mm", "values": [8, 10]}],
                         responses=[{"name": "dp", "tool": "cfd_pipe_flow",
                                     "metric": "pressure_drop_pa",
                                     "conditions": bad}])
            # substitution failures land as per-point rows with a reason, and the study
            # is not ok — never a silently flat table
            assert got["ok"] is False, (why, got)
            assert got["n_evaluated"] == 0, (why, got)
            detail = got["points"][0]["responses"]["dp"]["detail"]
            assert "$" in detail, (why, detail)

        # an unknown measuring tool, and an objective naming a response that is not
        # measured, are both hard errors BEFORE any point is evaluated
        for kwargs in (
            {"responses": [{"name": "dp", "tool": "no_such_tool"}]},
            {"responses": [{"name": "dp", "tool": "cfd_pipe_flow"}],
             "objective": {"response": "cd", "sense": "min"}},
        ):
            try:
                w.call("study_submit",
                       variables=[{"name": "diameter_mm", "values": [8]}], **kwargs)
            except WorkerError:
                pass
            else:
                raise AssertionError(f"study_submit({kwargs!r}) should have raised")


def test_an_oversized_sweep_is_refused_rather_than_run():
    """Every point is a real evaluation and a solver point is a real solve, so the
    default guard refuses a grid bigger than you probably meant."""
    from ankusdrive import Worker
    from ankusdrive.client import WorkerError

    with Worker() as w:
        variables = [{"name": "diameter_mm", "min": 5, "max": 25, "levels": 9},
                     {"name": "velocity_m_s", "min": 1, "max": 5, "levels": 9}]
        responses = [{"name": "dp", "tool": "cfd_pipe_flow",
                      "metric": "pressure_drop_pa",
                      "conditions": {"diameter_mm": "$diameter_mm",
                                     "velocity_m_s": "$velocity_m_s",
                                     "length_mm": 1000}}]
        try:
            w.call("study_submit", variables=variables, responses=responses)
        except WorkerError as e:
            assert "max_points" in str(e), e
        else:
            raise AssertionError("an 81-point grid should trip the default guard")
        # raising it deliberately is allowed; LHS gets the same coverage for 12 points
        lhs = w.call("study_submit", variables=variables, responses=responses,
                     sampling={"method": "lhs", "n_samples": 12, "seed": 0})
        assert lhs["n_points"] == 12 and lhs["ok"] is True, lhs
        assert lhs["sampling"] == {"method": "lhs", "seed": 0, "n_samples": 12}


def test_a_study_can_sweep_geometry_through_a_recipe():
    """The parametric half: each point rebuilds the part and the response measures the
    part it built, not a number typed alongside it."""
    from ankusdrive import Worker

    with Worker() as w:
        w.call("new_document", name="study_recipe")
        got = w.call(
            "study_submit",
            recipe="spur_gear",
            fixed_inputs={"module_mm": 2.0, "width_mm": 6.0},
            variables=[{"name": "teeth", "values": [18, 24, 30]}],
            responses=[{"name": "volume", "tool": "mass_properties",
                        "metric": "volume_mm3",
                        "conditions": {"handle": "$handle"}}],
            objective={"response": "volume", "sense": "min"},
        )
        assert got["ok"] is True, got
        assert got["n_points"] == 3 and got["recipe"] == "spur_gear"
        # every point built its own part ...
        handles = [pt["handle"] for pt in got["points"]]
        assert len(set(handles)) == 3, handles
        # ... and more teeth at fixed module is a bigger gear, so volume rises
        volumes = [pt["responses"]["volume"]["value"] for pt in got["points"]]
        assert volumes == sorted(volumes), volumes
        assert got["best"]["params"] == {"teeth": 18}


def _await_study(w, job_id, timeout_s=60):
    """Poll a submitted study to completion and return its table."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = w.call("job_status", job_id=job_id)
        if st["status"] in ("done", "failed"):
            got = w.call("job_result", job_id=job_id)
            assert st["status"] == "done", got
            return got["result"]
        time.sleep(0.05)
    raise AssertionError(f"study {job_id} did not finish in {timeout_s}s")


def test_an_async_study_fans_out_concurrently_joins_once_and_resumes_from_cache():
    """The asynchronous half, on the reference long-solve rather than a real solver so
    it runs in the fast lane.

    Three properties, all of which a serial implementation would quietly fail:
    the points run CONCURRENTLY (four 0.6 s evaluations must not take 2.4 s), the whole
    study is ONE poll rather than N, and re-submitting an identical study re-solves
    nothing — which is what makes a crashed or widened study resumable for free."""
    from ankusdrive import Worker

    spec = dict(
        variables=[{"name": "v", "values": [1.0, 2.0, 3.0, 4.0]}],
        responses=[{"name": "sq", "tool": "async_demo_submit", "metric": "squared",
                    "conditions": {"value": "$v", "duration_s": 0.6}}],
        objective={"response": "sq", "sense": "max"},
    )
    with Worker() as w:
        t0 = time.time()
        sub = w.call("study_submit", **spec)
        assert sub.get("job_id"), sub                  # one collector, not four polls
        assert len(sub["pending"]) == 4, sub
        got = _await_study(w, sub["job_id"])
        elapsed = time.time() - t0

        assert got["ok"] is True, got
        assert got["n_points"] == 4 and got["n_evaluated"] == 4
        vals = [pt["responses"]["sq"]["value"] for pt in got["points"]]
        assert vals == [1.0, 4.0, 9.0, 16.0], vals
        assert got["best"]["params"] == {"v": 4.0} and got["best"]["value"] == 16.0
        assert got["n_cached"] == 0, got               # nothing was cached yet
        assert all(pt["responses"]["sq"].get("job_id") for pt in got["points"])
        # 4 x 0.6 s serial would be 2.4 s; concurrent is ~0.6 s plus overhead
        assert elapsed < 1.8, f"the points did not run concurrently ({elapsed:.2f}s)"

        # re-submitting the identical study re-solves NOTHING
        t1 = time.time()
        again = w.call("study_submit", **spec)
        repeat = _await_study(w, again["job_id"])
        redo_s = time.time() - t1
        assert repeat["n_cached"] == 4, repeat
        assert [pt["responses"]["sq"]["value"]
                for pt in repeat["points"]] == vals, repeat
        assert redo_s < 0.5, f"a fully cached study still took {redo_s:.2f}s"

        # widening the grid re-runs only the new point (resume, not restart)
        wider = dict(spec)
        wider["variables"] = [{"name": "v", "values": [1.0, 2.0, 3.0, 4.0, 5.0]}]
        grown = _await_study(w, w.call("study_submit", **wider)["job_id"])
        assert grown["n_points"] == 5 and grown["n_cached"] == 4, grown
        assert grown["points"][4]["responses"]["sq"]["value"] == 25.0


def test_two_metrics_off_the_same_call_solve_once():
    """Reading two metrics off one tool call (a solved Δp and its hp_ratio, a Cd and its
    y+) must not solve twice.

    The jobs-layer content cache cannot catch this: it only serves COMPLETED jobs, and
    during a fan-out the twin submission is still running. Caught live on the real
    OpenFOAM sweep, where 3 diameters x 2 responses launched 6 solves for 3 cases."""
    from ankusdrive import Worker

    with Worker() as w:
        sub = w.call(
            "study_submit",
            variables=[{"name": "v", "values": [2.0, 3.0]}],
            responses=[{"name": "sq", "tool": "async_demo_submit", "metric": "squared",
                        "conditions": {"value": "$v", "duration_s": 0.4}},
                       {"name": "raw", "tool": "async_demo_submit", "metric": "value",
                        "conditions": {"value": "$v", "duration_s": 0.4}}],
        )
        # two points x two responses, but only TWO distinct solves
        assert sub["pending"] == list(dict.fromkeys(sub["pending"]))
        assert len(sub["pending"]) == 2, sub["pending"]
        got = _await_study(w, sub["job_id"])
        assert got["ok"] is True and got["n_evaluated"] == 4, got
        # ... and each response still reads its own metric off the shared result
        assert [pt["responses"]["sq"]["value"] for pt in got["points"]] == [4.0, 9.0]
        assert [pt["responses"]["raw"]["value"] for pt in got["points"]] == [2.0, 3.0]
        for pt in got["points"]:
            assert (pt["responses"]["sq"]["job_id"]
                    == pt["responses"]["raw"]["job_id"]), pt


def test_a_failing_point_is_a_row_and_the_rest_of_the_table_survives():
    """A study that hits a bad point must still return its table — the surrounding
    points are exactly the evidence you need to see WHY it failed."""
    from ankusdrive import Worker

    with Worker() as w:
        got = w.call(
            "study_submit",
            variables=[{"name": "d", "values": [10, -5, 20]}],   # -5 mm is impossible
            responses=[{"name": "dp", "tool": "cfd_pipe_flow",
                        "metric": "pressure_drop_pa",
                        "conditions": {"diameter_mm": "$d", "length_mm": 1000,
                                       "flow_rate_lpm": 0.4}}],
        )
        assert got["ok"] is False and got["n_failed"] == 1, got
        assert got["n_evaluated"] == 2, got
        bad = got["points"][1]["responses"]["dp"]
        assert bad["ok"] is False and bad["value"] is None and bad["detail"], bad
        # the good points are intact and summarized over
        assert got["points"][0]["responses"]["dp"]["ok"] is True
        assert got["responses"]["dp"]["n"] == 2 and got["responses"]["dp"]["n_missing"] == 1


def test_the_job_layer_pins_a_dependency_against_cap_eviction():
    """A fan-out study's children must survive the retention cap until the collector has
    read them — otherwise eviction drops the points that finished FIRST (terminal and
    therefore first in line) and the join raises JobNotFound for work that succeeded."""
    from ankusdrive import jobs

    jobs.reset()
    original = jobs._MAX_JOBS
    try:
        jobs._MAX_JOBS = 4
        first = jobs.submit("study_child", lambda: {"value": 1.0})
        while jobs.status(first["job_id"])["status"] == "running":
            time.sleep(0.01)
        jobs.pin(first["job_id"])
        for _ in range(12):                          # far past the cap
            other = jobs.submit("noise", lambda: None)
            while jobs.status(other["job_id"])["status"] == "running":
                time.sleep(0.01)
        # the pinned child is still there and still readable
        assert jobs.result(first["job_id"])["result"] == {"value": 1.0}
        assert len(jobs.list_jobs()["jobs"]) <= jobs._MAX_JOBS + 1
        jobs.unpin(first["job_id"])
        for _ in range(6):
            jobs.submit("noise", lambda: None)
        time.sleep(0.05)
        try:                                         # now evictable again
            jobs.result(first["job_id"])
        except jobs.JobNotFound:
            pass
        # unpinning something already gone is safe (collectors unpin in a finally)
        assert jobs.unpin("job_nope")["pins"] == 0
        try:
            jobs.pin("job_nope")
        except jobs.JobNotFound:
            pass
        else:
            raise AssertionError("pinning an unknown job should raise")
    finally:
        jobs._MAX_JOBS = original
        jobs.reset()


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

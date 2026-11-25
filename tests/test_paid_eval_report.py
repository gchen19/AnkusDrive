"""The scheduled paid-eval summary (#517) goes red only for a crash or a real drop.

`scripts/paid_eval_report.py` is the only gate on `.github/workflows/paid-evals.yml`:
the workflow ignores the harnesses' own exit codes. So it must fail when an expected
report is missing, fail on a drop past the margin, and stay green for noise, a
missing baseline, a new model, or an M2 panel that changed. Fixtures are shaped like
the real reports each harness writes.

Stdlib only. No FreeCAD, no API key, no network.

Run:  python3 tests/test_paid_eval_report.py
"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import paid_eval_report as per  # noqa: E402

H = "claude-haiku-5-5"
USAGE = {"in_tokens": 10_000, "out_tokens": 2_000, "cache_read": 0, "cache_write": 0}


def layer_a(correct=9, total=10, grader="judge"):
    results = [{"shape": f"s{i}", "grade": {"pass": i < correct + 1},
                "judge": {"status": "graded", "pass": i < correct, "cached": False,
                          "usage": USAGE},
                "pass": i < correct} for i in range(total)]
    return {"model": H, "grader": grader, "judge_model": "claude-opus-5-5",
            "total": total, "correct": correct, "accuracy": correct / total,
            "results": results, "usage": USAGE, "cost_usd": 0.002}


def layer_b(correct=6, total=7):
    return {"model": H, "total": total, "correct": correct, "accuracy": correct / total,
            "results": [], "cost_usd": 0.001}


def layer_c(correct=9, total=10):
    return {"model": H, "total": total, "correct": correct, "accuracy": correct / total,
            "false_positives": 1, "false_negatives": 0, "rows": [], "cost_usd": 0.001}


def layer_d(passes=(5, 5, 4, 3)):
    cells = {f"{H}/t{i}": {"pass": p, "n": 5, "rate": p / 5, "avg_cost": 0.01, "tier": i}
             for i, p in enumerate(passes)}
    return {"trials": 5, "models": [H], "cells": cells, "failures": []}


def tool_sel(passed=55, n=63):
    return {"model": H, "toolsets": "bundle", "n": n, "accuracy": passed / n,
            "outcomes": {"pass": passed, "wrong": n - passed},
            "billed_this_run_usd": 0.09, "cache_savings_usd": 0.2}


def m2(passes=((5, 5), (4, 3)), trials=5, neg=True):
    rows = []
    for toy, (pp, ps) in zip(("peg", "twopin"), passes):
        rows.append({"toy": toy, "condition": "partition", "trials": trials, "passed": pp,
                     "built": trials, "pass_rate": pp / trials, "mean_cost_usd": 0.01})
        rows.append({"toy": toy, "condition": "single", "trials": trials, "passed": ps,
                     "built": trials, "pass_rate": ps / trials, "mean_cost_usd": 0.01})
    if neg:  # a negative-control row must never count toward the agents' pass rate
        rows.append({"toy": "peg", "condition": "neg/oversize", "trials": 3, "built": 3,
                     "caught": 3, "caught_rate": 1.0, "mean_cost_usd": 0.01})
    return {"model": H, "trials": trials, "rows": rows, "total_cost_usd": 0.2}


ALL = {"layer_a": layer_a(), "layer_b": layer_b(), "layer_c": layer_c(),
       "layer_d": layer_d(), "tool_selection": tool_sel(), "m2": m2()}


def _run(reports, baseline=None, expect=None):
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        for suite, rep in reports.items():
            (d / per.SUITES[suite][0]).write_text(json.dumps(rep))
        (d / "baseline.json").write_text(json.dumps(baseline or {}))
        out = d / "out.json"
        summary = d / "summary.md"
        with contextlib.redirect_stdout(io.StringIO()):
            code = per.main([str(d), "--baseline", str(d / "baseline.json"),
                             "--expect", ",".join(expect or per.SUITES),
                             "--summary", str(summary), "--out", str(out)])
        return code, json.loads(out.read_text()), summary.read_text()


def _baseline_from(reports):
    _, out, _ = _run(reports)
    return {"margin": 0.15, "suites": out["baseline_candidate"]}


def test_first_run_no_baseline_is_green_and_offers_one():
    code, out, md = _run(ALL)
    assert code == 0, md
    assert {r["status"] for r in out["rows"]} == {"no_baseline"}
    cand = out["baseline_candidate"]
    assert set(cand) == set(per.SUITES)
    assert cand["layer_a"][H] == {"rate": 0.9, "n": 10}
    assert "Baseline from this run" in md and "none yet" in md


def test_rates_and_costs_are_read_from_each_report_shape():
    _, out, md = _run(ALL)
    rows = {r["suite"]: r for r in out["rows"]}
    assert (rows["layer_d"]["passed"], rows["layer_d"]["n"]) == (17, 20)
    assert abs(rows["layer_d"]["cost_usd"] - 0.2) < 1e-9
    assert (rows["tool_selection"]["passed"], rows["tool_selection"]["n"]) == (55, 63)
    # M2 pools agent rows only; the neg/ row's 3 trials are not in n
    assert (rows["m2"]["passed"], rows["m2"]["n"]) == (17, 20), rows["m2"]
    assert "partition 9/10" in rows["m2"]["extra"] and "single 8/10" in rows["m2"]["extra"]
    # Layer A adds what its judge billed (10 fresh Opus verdicts) to its own cost
    judge = 10 * per.cost_of(USAGE, "claude-opus-5-5")
    assert abs(rows["layer_a"]["cost_usd"] - (0.002 + judge)) < 1e-9
    assert "Billed this run" in md


def test_cached_judge_verdicts_cost_nothing():
    rep = layer_a()
    for r in rep["results"]:
        r["judge"]["cached"] = True
    _, out, _ = _run({"layer_a": rep}, expect=["layer_a"])
    assert out["rows"][0]["cost_usd"] == 0.002


def test_missing_report_fails():
    reports = {k: v for k, v in ALL.items() if k != "m2"}
    code, out, md = _run(reports)
    assert code == 1 and out["missing"] == ["m2"]
    assert "left no report" in md


def test_unselected_suite_is_not_missing():
    code, out, _ = _run({"m2": m2()}, expect=["m2"])
    assert code == 0 and out["missing"] == []


def test_drop_past_margin_fails():
    base = _baseline_from({"tool_selection": tool_sel(60, 63)})
    code, out, md = _run({"tool_selection": tool_sel(45, 63)}, base, ["tool_selection"])
    assert code == 1, out
    assert out["rows"][0]["status"] == "regressed" and "REGRESSED" in md


def test_drop_within_margin_passes():
    base = _baseline_from({"tool_selection": tool_sel(60, 63)})
    code, out, _ = _run({"tool_selection": tool_sel(54, 63)}, base, ["tool_selection"])
    assert code == 0 and out["rows"][0]["status"] == "ok"


def test_improvement_passes():
    base = _baseline_from({"layer_c": layer_c(7, 10)})
    code, out, _ = _run({"layer_c": layer_c(10, 10)}, base, ["layer_c"])
    assert code == 0 and out["rows"][0]["status"] == "ok"


def test_small_suite_gets_noise_allowance():
    # Layer B: 7 pairs, baseline 6/7. One item is 14 points; 2 x SE is about 26.
    base = _baseline_from({"layer_b": layer_b(6, 7)})
    assert per.allowed_drop(base["suites"]["layer_b"][H], 0.15) > 0.25
    code, _, _ = _run({"layer_b": layer_b(5, 7)}, base, ["layer_b"])
    assert code == 0, "one item lost on a 7-item suite is noise"
    code, _, _ = _run({"layer_b": layer_b(3, 7)}, base, ["layer_b"])
    assert code == 1, "three items lost is a regression"


def test_per_entry_margin_overrides_default():
    base = _baseline_from({"tool_selection": tool_sel(60, 63)})
    base["suites"]["tool_selection"][H]["margin"] = 0.30
    code, _, _ = _run({"tool_selection": tool_sel(45, 63)}, base, ["tool_selection"])
    assert code == 0


def test_another_model_is_not_compared():
    base = _baseline_from({"layer_c": layer_c(10, 10)})
    rep = layer_c(2, 10)
    rep["model"] = "claude-sonnet-5-5"
    code, out, _ = _run({"layer_c": rep}, base, ["layer_c"])
    assert code == 0 and out["rows"][0]["status"] == "no_baseline"


def test_changed_m2_panel_is_not_compared():
    base = _baseline_from({"m2": m2()})
    code, out, md = _run({"m2": m2(((0, 0), (0, 0)), trials=3)}, base, ["m2"])
    assert code == 0 and out["rows"][0]["status"] == "panel_changed"
    assert "other panel" in md
    code, out, _ = _run({"m2": m2(((1, 0), (0, 0)))}, base, ["m2"])
    assert code == 1 and out["rows"][0]["status"] == "regressed"


def test_m2_by_toy_table_skips_negatives():
    _, _, md = _run({"m2": m2()}, expect=["m2"])
    assert "| twopin | single | 3/5 |" in md
    assert "neg/oversize" not in md


def test_unknown_suite_is_an_error():
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            per.main([".", "--expect", "layer_z"])
        except SystemExit as e:
            assert e.code == 2
            return
    raise AssertionError("an unknown suite name must not pass silently")


def test_committed_baseline_parses():
    data = json.loads((REPO / "tests" / "paid_eval_baseline.json").read_text())
    assert isinstance(data.get("suites"), dict) and 0 < data["margin"] < 1
    for suite, models in data["suites"].items():
        assert suite in per.SUITES, suite
        for model, entry in models.items():
            assert model.startswith("claude-") and 0 <= entry["rate"] <= 1 and entry["n"] > 0


def main():
    g = globals()
    tests = [(n, f) for n, f in sorted(g.items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception:
            failed += 1
            print(f"  FAIL {name}")
            traceback.print_exc()
    print(f"\n== {len(tests) - failed}/{len(tests)} passed ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

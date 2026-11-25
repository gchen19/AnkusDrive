"""A heavy-solves run that never got a runner is told apart from a failure (#515).

`scripts/ci_runner_capacity.py` decides whether heavy-solves-retry.yml re-runs a
red nightly. Getting it wrong either way is costly: calling a real failure
"capacity" re-runs it (hours of macOS minutes) and can hide a flaky regression
behind a green retry; calling capacity a failure leaves the nightly red on
nothing. So the fixtures are the jobs API's shapes from the actual runs: the
Nov 17 and Nov 18 nightlies (#515, macOS lanes never acquired) and the Sept 19
nightly (both Linux lanes failed on a runner).

Imports only the script and the stdlib. No FreeCAD, no network.

Run:  python3 tests/test_ci_runner_capacity.py
"""

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import time
import traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import ci_runner_capacity as rc  # noqa: E402

LANE_A_AMD = "Live external-solver regressions (Linux amd64, hosted image)"
LANE_A_ARM = "Live external-solver regressions (Linux arm64, hosted image)"
LANE_B = "Container-substrate regressions (hosted Linux, solvers in a sidecar)"
LANE_C = "macOS host regressions — SU2/Rosetta + Multipass relay via shim (hosted)"
VMDISK = "arm64 solver image -> VM disk (for lane D)"
LANE_D = "macOS Multipass path into a real Linux VM — live solves under emulation (hosted)"


def _ran(name, conclusion="success", runner="GitHub Actions 1000007261", steps=11):
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "runner_name": runner, "steps": [{"name": f"step {i}"} for i in range(steps)]}


def _never_acquired(name):
    # What the API returns for "The job was not acquired by Runner of type hosted
    # even after multiple attempts": cancelled, an empty runner name, no steps.
    return {"name": name, "status": "completed", "conclusion": "cancelled",
            "runner_name": "", "steps": []}


# 44111894111, Nov 17: both macOS lanes never got a runner.
NOV_17 = [_ran(VMDISK, steps=10), _ran(LANE_A_ARM), _ran(LANE_B, steps=15), _ran(LANE_A_AMD),
          _never_acquired(LANE_C), _never_acquired(LANE_D)]
# 44328751577, Nov 18: lane C only.
NOV_18 = [_ran(LANE_B, steps=15), _ran(LANE_A_AMD), _never_acquired(LANE_C), _ran(LANE_A_ARM),
          _ran(VMDISK, steps=10), _ran(LANE_D, steps=12)]
# 35436184395, Sept 19: both Linux lanes reached a runner and failed.
SEPT_19 = [_ran(LANE_A_ARM, "failure", steps=10), _ran(LANE_A_AMD, "failure", steps=10),
           _ran(LANE_B, steps=15), _ran(LANE_C, steps=9), _ran(VMDISK, steps=10),
           _ran(LANE_D, steps=12)]
GREEN = [_ran(n) for n in (LANE_A_AMD, LANE_A_ARM, LANE_B, LANE_C, VMDISK, LANE_D)]


def test_never_acquired_jobs_are_infrastructure():
    v, classes = rc.verdict(NOV_17)
    assert v == rc.NO_RUNNER, v
    assert [n for n, k, _ in classes if k == rc.NO_RUNNER] == [LANE_C, LANE_D], classes
    assert rc.verdict(NOV_18)[0] == rc.NO_RUNNER


def test_a_lane_that_ran_and_failed_is_a_failure():
    v, classes = rc.verdict(SEPT_19)
    assert v == rc.FAILED, v
    assert [n for n, k, _ in classes if k == rc.FAILED] == [LANE_A_ARM, LANE_A_AMD], classes


def test_timeouts_and_runner_side_cancels_are_failures():
    # A lane that reached a runner and was then cancelled is a timeout (a hung solve)
    # or a lost runner mid-job: a signal, never something to quietly retry.
    timed_out = _ran(LANE_D, "cancelled", steps=12)
    assert rc.classify_job(timed_out) == rc.FAILED
    # A runner was assigned but the job died before its first step: not "never got one".
    assert rc.classify_job(_ran(LANE_C, "failure", steps=0)) == rc.FAILED
    # Failed before any runner was requested (a broken `if:`/`runs-on:`/`uses:` after an
    # edit to heavy-solves.yml): the same empty shape, but a retry would only repeat it.
    broken = {"name": LANE_C, "status": "completed", "conclusion": "failure",
              "runner_name": "", "steps": []}
    assert rc.classify_job(broken) == rc.FAILED
    assert rc.verdict(NOV_18[:2] + [broken])[0] == rc.FAILED
    # A queue timeout with no runner is still capacity.
    assert rc.classify_job(dict(broken, conclusion="timed_out")) == rc.NO_RUNNER


def test_a_real_failure_wins_over_missing_runners():
    mixed = NOV_17[:-1] + [_ran(LANE_A_AMD, "failure")]
    assert rc.verdict(mixed)[0] == rc.FAILED


def test_green_and_skipped_are_ok():
    assert rc.verdict(GREEN)[0] == "green"
    assert rc.classify_job({"name": LANE_D, "conclusion": "skipped", "runner_name": "", "steps": []}) == rc.OK


def test_rerun_only_capacity_and_only_once():
    assert rc.should_rerun(rc.NO_RUNNER, attempt=1, max_attempts=2)
    assert not rc.should_rerun(rc.NO_RUNNER, attempt=2, max_attempts=2), "the retry must be bounded"
    assert not rc.should_rerun(rc.FAILED, attempt=1, max_attempts=2)
    assert not rc.should_rerun("green", attempt=1, max_attempts=2)


def test_summary_names_each_lane_and_the_cause():
    v, classes = rc.verdict(NOV_18)
    md = rc.summary_markdown(v, classes, attempt=1, rerun=True)
    assert "runner capacity" in md, md
    assert f"| {LANE_C} | never got a runner" in md, md
    assert f"| {LANE_A_AMD} | passed |" in md, md
    assert "attempt 2" in md, md
    v, classes = rc.verdict(SEPT_19)
    md = rc.summary_markdown(v, classes, attempt=1, rerun=False)
    assert "not re-run" in md and f"| {LANE_A_AMD} | failed (failure) |" in md, md


def test_cli_writes_outputs_from_a_jobs_response():
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "jobs.json").write_text(json.dumps({"total_count": len(NOV_17), "jobs": NOV_17}), encoding="utf-8")
        out, summary = d / "out", d / "summary"
        with contextlib.redirect_stdout(io.StringIO()):
            rc_ = rc.main(["--jobs-json", str(d / "jobs.json"), "--attempt", "1",
                           "--output", str(out), "--summary", str(summary)])
        assert rc_ == 0
        assert out.read_text(encoding="utf-8") == "verdict=no-runner\nrerun=true\n", out.read_text(encoding="utf-8")
        assert LANE_D in summary.read_text(encoding="utf-8")

        (d / "jobs.json").write_text(json.dumps(NOV_17), encoding="utf-8")  # a bare list works too
        out.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            rc.main(["--jobs-json", str(d / "jobs.json"), "--attempt", "2", "--output", str(out)])
        assert out.read_text(encoding="utf-8") == "verdict=no-runner\nrerun=false\n", out.read_text(encoding="utf-8")


def test_an_empty_run_is_an_error_not_green():
    # A fetch that returned nothing must not read as "nothing failed".
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "jobs.json"
        p.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            assert rc.main(["--jobs-json", str(p)]) == 2


def _discover():
    g = globals()
    return [(n, g[n]) for n in sorted(g) if n.startswith("test_") and callable(g[n])]


def main():
    failures = []
    t_suite = time.time()
    tests = _discover()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t0:.2f}s)")
    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

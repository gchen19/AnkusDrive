"""Async job-registry toys — two-sided oracles for ankusdrive.jobs.

Pure-Python, no FreeCAD. The async lifecycle is made deterministic with a
threading.Event (no sleeps / no timing assumptions): the worker submits real
background threads, and these toys drive them to known states. Mirrors
tests/TOYS.md discipline — each check pins a behavior AND catches a wrong one.

Run:  python3 tests/test_jobs.py
"""
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import jobs  # noqa: E402


def _wait(job_id, want=("done", "failed"), timeout=5.0):
    """Poll until the job reaches a terminal status (deterministic: the worker
    threads finish in microseconds once unblocked). Returns the final status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = jobs.status(job_id)["status"]
        if st in want:
            return st
        time.sleep(0.002)
    raise AssertionError(f"job {job_id} did not reach {want} within {timeout}s")


def test_submit_runs_and_returns_result():
    jobs.reset()
    sub = jobs.submit("calc", lambda: {"answer": 6 * 7})
    assert sub["status"] == "running" and sub["cache_hit"] is False, sub
    _wait(sub["job_id"])
    r = jobs.result(sub["job_id"])
    assert r["status"] == "done", r
    assert r["result"] == {"answer": 42}, r
    assert r["elapsed_s"] >= 0.0


def test_running_state_is_observable():
    # a job blocked on an Event is genuinely 'running' until released — proves the
    # work is off the request thread, not run inline by submit().
    jobs.reset()
    gate = threading.Event()
    sub = jobs.submit("blocked", lambda: (gate.wait(5.0), {"freed": True})[1])
    assert jobs.status(sub["job_id"])["status"] == "running"
    # result while running carries neither result nor error
    mid = jobs.result(sub["job_id"])
    assert "result" not in mid and "error" not in mid, mid
    gate.set()
    _wait(sub["job_id"])
    assert jobs.result(sub["job_id"])["result"] == {"freed": True}


def test_failure_is_captured_not_raised():
    # a solver that raises must surface as a failed job, never crash the registry
    jobs.reset()
    def _boom():
        raise ValueError("solver diverged")
    sub = jobs.submit("solve", _boom)
    assert _wait(sub["job_id"]) == "failed"
    st = jobs.status(sub["job_id"])
    assert st["status"] == "failed" and "solver diverged" in st["error"], st
    r = jobs.result(sub["job_id"])
    assert "result" not in r and "ValueError" in r["error"], r


def test_content_key_cache_hit_and_miss():
    jobs.reset()
    calls = {"n": 0}
    def _work():
        calls["n"] += 1
        return {"n": calls["n"]}
    k = jobs.content_key("demo", {"a": 1, "b": 2})
    a = jobs.submit("demo", _work, key=k)
    _wait(a["job_id"])
    # identical key, prior job complete -> cache hit, SAME job, fn NOT re-run
    b = jobs.submit("demo", _work, key=k)
    assert b["cache_hit"] is True and b["job_id"] == a["job_id"], b
    assert calls["n"] == 1, "cached submit must not re-run the work"
    # different key -> miss, a new job, fn runs again
    k2 = jobs.content_key("demo", {"a": 1, "b": 3})
    c = jobs.submit("demo", _work, key=k2)
    assert c["cache_hit"] is False and c["job_id"] != a["job_id"], c
    _wait(c["job_id"])
    assert calls["n"] == 2


def test_discard_frees_a_terminal_job():
    jobs.reset()
    sub = jobs.submit("calc", lambda: 1)
    _wait(sub["job_id"])
    jobs.result(sub["job_id"], discard=True)
    # gone after discard
    try:
        jobs.status(sub["job_id"])
    except jobs.JobNotFound:
        pass
    else:
        raise AssertionError("discarded job should be unknown")
    # discard is ignored while running (can't free in-flight work)
    gate = threading.Event()
    run = jobs.submit("blocked", lambda: gate.wait(5.0))
    held = jobs.result(run["job_id"], discard=True)
    assert held["status"] == "running"
    assert jobs.status(run["job_id"])["status"] == "running"  # still there
    gate.set()
    _wait(run["job_id"])


def test_eviction_caps_terminal_jobs_only():
    jobs.reset()
    orig = jobs._MAX_JOBS
    jobs._MAX_JOBS = 3
    try:
        # one long-running job that must survive eviction
        gate = threading.Event()
        keep = jobs.submit("blocked", lambda: gate.wait(5.0))
        # flood with finished jobs well past the cap
        for i in range(8):
            s = jobs.submit("calc", lambda i=i: i)
            _wait(s["job_id"])
        assert len(jobs._jobs) <= 3, len(jobs._jobs)
        # the running job was never evicted despite being the oldest
        assert keep["job_id"] in jobs._jobs
        assert jobs.status(keep["job_id"])["status"] == "running"
        gate.set()
        _wait(keep["job_id"])
    finally:
        jobs._MAX_JOBS = orig


def test_unknown_job_raises():
    jobs.reset()
    for fn in (lambda: jobs.status("job_999"), lambda: jobs.result("job_999")):
        try:
            fn()
        except jobs.JobNotFound:
            pass
        else:
            raise AssertionError("expected JobNotFound")


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


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
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

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

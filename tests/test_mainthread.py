"""Main-thread work queue — two-sided oracles for ankusdrive.mainthread (issue #260).

Pure-Python, no FreeCAD. This is the primitive that lets a BACKGROUND job ask for
work on the main thread, which is what unblocks adaptive *shape* search: the
optimizer's loop cannot touch FreeCAD, and every candidate needs a recipe built.

The thing worth testing hardest is not the happy path but the failure mode the
design deliberately accepts: work only advances when someone drains the queue. That
must produce a loud, correctly-diagnosed timeout — never a silent hang, and never a
result that looks computed but wasn't.

Run:  python3 tests/test_mainthread.py
"""
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive import mainthread as mt  # noqa: E402


def _job(fn):
    """Run fn on a background thread, returning (thread, box) where box collects
    ('ok', value) or ('raise', exception)."""
    box = {}

    def _run():
        try:
            box["result"] = ("ok", fn())
        except BaseException as e:                   # noqa: BLE001
            box["result"] = ("raise", e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, box


def test_background_call_runs_on_the_draining_thread():
    # The whole point: the callable must execute on whoever drains, NOT on the job
    # thread that asked. If it ran on the caller we would be back to touching FreeCAD
    # off the main thread, which is the bug this module exists to prevent.
    mt.reset()
    mt.bind()                                        # this thread is the "main" one
    main_id = threading.get_ident()
    seen = {}
    t, box = _job(lambda: mt.call(lambda: seen.setdefault("ran_on",
                                                          threading.get_ident())))
    for _ in range(200):                             # let the job reach the queue
        if mt.pending():
            break
        time.sleep(0.01)
    assert mt.pending() == 1, mt.pending()
    assert mt.drain() == 1
    t.join(timeout=5)
    assert box["result"][0] == "ok", box["result"]
    assert seen["ran_on"] == main_id, (seen, main_id)


def test_result_and_exception_both_reach_the_waiter():
    mt.reset()
    mt.bind()
    t, box = _job(lambda: mt.call(lambda: 6 * 7))
    while not mt.pending():
        time.sleep(0.01)
    mt.drain()
    t.join(timeout=5)
    assert box["result"] == ("ok", 42), box["result"]
    # a build that blows up must fail the JOB, not the drain: the loop keeps running
    def _boom():
        raise ValueError("the recipe did not build")
    t2, box2 = _job(lambda: mt.call(_boom))
    while not mt.pending():
        time.sleep(0.01)
    assert mt.drain() == 1                           # drain itself does NOT raise
    t2.join(timeout=5)
    kind, err = box2["result"]
    assert kind == "raise", box2["result"]
    assert isinstance(err, ValueError), type(err)
    assert "did not build" in str(err), str(err)


def test_unserviced_call_times_out_loudly_naming_the_cause():
    # The accepted failure mode. Nobody drains -> the job must not hang forever, and
    # the message must say WHY (the client stopped polling), because "job stuck" and
    # "solve slow" look identical from outside and are fixed differently.
    mt.reset()
    mt.bind()
    t, box = _job(lambda: mt.call(lambda: "never", timeout_s=0.3))
    t.join(timeout=5)
    kind, err = box["result"]
    assert kind == "raise", box["result"]
    assert isinstance(err, mt.NotServiced), type(err)
    assert "polling" in str(err), str(err)
    assert mt.stats()["ran"] == 0, mt.stats()


def test_call_on_the_service_thread_runs_inline():
    # Reentrancy: a handler already ON the main thread that calls the same evaluation
    # code must not enqueue — the only thread that could run it is the one waiting.
    # Inline execution is what lets one code path serve both callers.
    mt.reset()
    mt.bind()
    assert mt.is_service_thread() is True
    got = mt.call(lambda: "inline")
    assert got == "inline", got
    assert mt.pending() == 0, mt.pending()
    assert mt.stats()["queued"] == 0, mt.stats()   # never went through the queue


def test_drain_is_bounded_so_one_job_cannot_starve_the_request():
    # The request loop drains before answering. An unbounded drain would let a queue
    # of builds hold the client's poll open indefinitely, which is the opposite of
    # what the queue is for.
    mt.reset()
    mt.bind()
    threads = [_job(lambda: mt.call(lambda: None, timeout_s=10))[0] for _ in range(10)]
    while mt.pending() < 10:
        time.sleep(0.01)
    assert mt.drain(max_items=4) == 4, "max_items must cap the batch"
    assert mt.pending() == 6, mt.pending()
    assert mt.drain() == 6                           # the rest go on the next turn
    for t in threads:
        t.join(timeout=5)
    assert mt.pending() == 0


def test_drain_time_bound_still_finishes_the_item_it_started():
    # max_s is checked BETWEEN items on purpose: abandoning a half-run build would
    # leave a partial FreeCAD document, which is worse than a slightly late reply.
    mt.reset()
    mt.bind()
    t, _ = _job(lambda: mt.call(lambda: time.sleep(0.25) or "slow", timeout_s=10))
    while not mt.pending():
        time.sleep(0.01)
    t0 = time.monotonic()
    assert mt.drain(max_s=0.01) == 1                 # ran it despite the tiny budget
    assert time.monotonic() - t0 >= 0.2, "the started item must run to completion"
    t.join(timeout=5)


def test_stats_distinguish_nobody_polling_from_slow_work():
    # The diagnostic the timeout message points at: pending high + drains climbing
    # means work is slow; drains flat means nobody is polling.
    mt.reset()
    mt.bind()
    t, _ = _job(lambda: mt.call(lambda: "x", timeout_s=10))
    while not mt.pending():
        time.sleep(0.01)
    before = mt.stats()
    assert before["pending"] == 1 and before["drains"] == 0, before
    mt.drain()
    t.join(timeout=5)
    after = mt.stats()
    assert after["drains"] == 1 and after["ran"] == 1 and after["pending"] == 0, after
    assert after["failed"] == 0, after


def _discover():
    return sorted(((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f)), key=lambda kv: kv[0])


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
            print(f"  FAIL {name:56s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:56s} ({time.time() - t0:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({time.time() - t_suite:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({time.time() - t_suite:.1f}s) ==")


if __name__ == "__main__":
    main()

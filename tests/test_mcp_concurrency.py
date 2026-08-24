"""
Multi-agent safety for the MCP server (issue #167).

Two properties, exercised against the REAL FastMCP tool functions + real
freecadcmd workers:

  Part A — correctness under concurrent calls
    - test_concurrent_calls_no_protocol_corruption : N threads hammering
      ping / new_document / mass_properties on the shared default worker never
      produce an id-mismatch, WorkerDied, or a crossed read.

  Part B — per-agent workspace isolation
    - test_default_workspace_unchanged   : a client that never calls
      use_workspace sees the historical single-worker behavior.
    - test_workspaces_isolate_handles    : handles/documents do not cross
      workspaces; switching back restores the other workspace intact.
    - test_pool_cap_and_close            : the pool is capped; close_workspace
      frees a slot; list_workspaces reports the pool honestly.

Run under the venv python (FastMCP + Pillow live there); the workers are
freecadcmd subprocesses either way:
    .venv/bin/python3 tests/test_mcp_concurrency.py
  or from the repo root:  python3 tests/test_mcp_concurrency.py   (if mcp is on
  the system python's path).
"""
import sys
import threading
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import mcp_server as srv  # noqa: E402


def _reset_pool():
    """Tear the pool back down to a clean default state between tests."""
    with srv._pool_lock:
        for name in list(srv._workers):
            srv._drop_locked(name)
    srv._current_workspace = srv.DEFAULT_WORKSPACE


# --- Part A -------------------------------------------------------------------

def test_concurrent_calls_no_protocol_corruption():
    """The regression for the shared-worker hazard: without serialization, two
    threads inside one Worker.call() interleave stdin writes / stdout reads and
    the client raises `id mismatch` / WorkerDied, or returns another call's
    payload. Here N threads mix ping / new_document / mass_properties against the
    single default worker; a fixed pre-built handle gives every mass read a known
    answer, so a crossed read shows up as a wrong value, not just an exception."""
    _reset_pool()
    try:
        # Pre-build one box in the default worker; volume is a known constant that
        # every worker thread will read back concurrently.
        srv.new_document(name="shared")
        box = srv.add_primitive(kind="box", w=10, d=20, h=5)
        handle = box["handle"]
        assert abs(box["volume"] - 1000.0) < 1e-6, box

        n_threads, iters = 8, 25
        errors: list[tuple[str, str]] = []
        bad_values: list[str] = []
        barrier = threading.Barrier(n_threads)

        def worker(tid: int):
            barrier.wait()  # maximise overlap — everyone starts together
            for k in range(iters):
                try:
                    op = k % 3
                    if op == 0:
                        r = srv.ping()
                        if r != "pong":
                            bad_values.append(f"ping[{tid}.{k}] -> {r!r}")
                    elif op == 1:
                        d = srv.new_document(name=f"t{tid}_{k}")
                        if "doc" not in d:
                            bad_values.append(f"new_document[{tid}.{k}] -> {d!r}")
                    else:
                        mp = srv.mass_properties(handle=handle)
                        if abs(mp["volume_mm3"] - 1000.0) > 1e-6:
                            bad_values.append(
                                f"mass[{tid}.{k}] -> {mp['volume_mm3']} (crossed read?)")
                except Exception as e:  # noqa: BLE001 — capture every failure
                    errors.append((f"{tid}.{k}", f"{type(e).__name__}: {e}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120.0)
        assert not any(t.is_alive() for t in threads), "a worker thread hung"

        assert not errors, (
            f"{len(errors)} protocol errors under {n_threads}×{iters} concurrent "
            f"calls (expected zero id-mismatch/WorkerDied):\n  "
            + "\n  ".join(f"{w}: {m}" for w, m in errors[:10]))
        assert not bad_values, (
            "crossed / wrong reads under concurrency:\n  " + "\n  ".join(bad_values[:10]))
        print(f"    {n_threads}×{iters} = {n_threads * iters} concurrent calls, "
              f"0 errors ({time.time() - t0:.2f}s)")
    finally:
        _reset_pool()


# --- Part B -------------------------------------------------------------------

def test_default_workspace_unchanged():
    """A client that never calls use_workspace behaves exactly as before: the
    current workspace is 'default', calls route to one persistent worker, and
    document state persists across calls."""
    _reset_pool()
    try:
        assert srv._current_workspace == srv.DEFAULT_WORKSPACE
        assert srv.ping() == "pong"
        srv.new_document(name="plain")
        box = srv.add_primitive(kind="box", w=20, d=20, h=20)
        mp = srv.mass_properties(handle=box["handle"])
        assert abs(mp["volume_mm3"] - 8000.0) < 1e-6, mp
        # state persists across calls, and only the default workspace exists
        objs = srv.list_objects()
        assert any(o["name"] == "Box" for o in objs), objs
        ws = srv.list_workspaces()
        assert ws["current"] == "default"
        assert [w["name"] for w in ws["workspaces"]] == ["default"], ws
    finally:
        _reset_pool()


def test_workspaces_isolate_handles():
    """Two workspaces = two freecadcmd processes with independent documents and
    handle registries. Two things prove they don't cross:

      1. Each worker mints handles from its own sequence, so the *string* "box_1"
         exists in both — but in B it resolves to B's own 27000-cube, never A's
         1000-cube. A shared registry would have leaked A's object.
      2. A handle that exists ONLY in A (its cylinder) does not resolve in B at
         all — it raises 'unknown handle'.

    Then switching back to A finds A's document + handle intact."""
    _reset_pool()
    try:
        # Workspace A: a 10-cube AND a cylinder (so A owns a handle B never mints).
        srv.use_workspace("agentA")
        srv.new_document(name="A")
        a_box = srv.add_primitive(kind="box", w=10, d=10, h=10)
        a_cyl = srv.add_primitive(kind="cylinder", r=5, h=10)
        assert abs(a_box["volume"] - 1000.0) < 1e-6

        # Workspace B: only a 30-cube. Its first box reuses the "box_1" string.
        srv.use_workspace("agentB")
        srv.new_document(name="B")
        b_box = srv.add_primitive(kind="box", w=30, d=30, h=30)
        assert abs(b_box["volume"] - 27000.0) < 1e-6
        assert b_box["handle"] == a_box["handle"], (
            "expected the mint sequences to collide on the string — that's the "
            "case that makes independent registries worth proving")

        # (1) The colliding string resolves to B's OWN object, not A's.
        mp_b = srv.mass_properties(handle=a_box["handle"])
        assert abs(mp_b["volume_mm3"] - 27000.0) < 1e-6, (
            f"handle {a_box['handle']!r} in B resolved to {mp_b['volume_mm3']} — "
            f"registries are shared, not isolated")

        # (2) An A-only handle does not exist in B.
        try:
            srv.mass_properties(handle=a_cyl["handle"])
        except RuntimeError as e:
            assert "unknown handle" in str(e).lower(), e
        else:
            raise AssertionError("A-only handle resolved inside workspace B — handles crossed")

        # Back to A: its document + handles are still there and unchanged.
        srv.use_workspace("agentA")
        mp_a = srv.mass_properties(handle=a_box["handle"])
        assert abs(mp_a["volume_mm3"] - 1000.0) < 1e-6, mp_a
        assert abs(srv.mass_properties(handle=a_cyl["handle"])["volume_mm3"]
                   - 3.14159 * 25 * 10) < 1.0

        names = {w["name"] for w in srv.list_workspaces()["workspaces"]}
        assert {"agentA", "agentB"} <= names, names
    finally:
        _reset_pool()


def test_pool_cap_and_close():
    """The pool is capped; claiming past the cap raises rather than leaking
    processes, and close_workspace frees a slot so the next claim succeeds."""
    _reset_pool()
    orig_cap = srv._MAX_WORKSPACES
    srv._MAX_WORKSPACES = 2  # keep the test cheap: only 2 freecadcmd processes
    try:
        srv.use_workspace("w1")
        srv.use_workspace("w2")
        # Pool is full (2/2). A third distinct workspace must be refused.
        try:
            srv.use_workspace("w3")
        except RuntimeError as e:
            assert "pool full" in str(e).lower(), e
        else:
            raise AssertionError("claiming past the cap should have raised")

        # Re-claiming an EXISTING workspace is always fine (no new slot).
        srv.use_workspace("w1")

        # Free a slot, then the third claim succeeds.
        res = srv.close_workspace("w2")
        assert res["closed"] is True, res
        srv.use_workspace("w3")
        names = {w["name"] for w in srv.list_workspaces()["workspaces"]}
        assert names == {"w1", "w3"}, names

        # Closing the current workspace drops us back to default.
        srv.close_workspace("w3")
        assert srv._current_workspace == srv.DEFAULT_WORKSPACE
        assert srv.ping() == "pong"  # default respawns cleanly
    finally:
        srv._MAX_WORKSPACES = orig_cap
        _reset_pool()


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t_suite = time.time()
    for name, fn in _discover():
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")
    total = time.time() - t_suite
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({total:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

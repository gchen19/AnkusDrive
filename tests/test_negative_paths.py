"""
Tier 4 negative-path tests (TEST_PLAN tier 4).

Every slice produces a clear error on bad input, and the worker survives
each error (ping returns pong after). The "worker survives" gate is what
makes the worker safe to keep alive across a long agent session.

  test_slice1_resolve_bad_tag                 : unknown tag → WorkerError, worker alive
  test_slice2_pad_unconstrained_sketch        : underconstrained sketch reports DOF, no crash
  test_slice3_fem_run_without_material        : missing material → clear error, worker alive
  test_slice4_render_empty_shape              : empty mesh → blank PNG, no crash
  test_slice5_add_part_missing_file           : nonexistent path → WorkerError, worker alive
  test_unknown_method                         : already covered, kept here for tier completeness

Run: python3 tests/test_negative_paths.py
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ -> fem_scratch

from ankusdrive import Worker, WorkerError  # noqa: E402
from fem_scratch import fem_workdir  # noqa: E402

try:
    from ankusdrive import render as render_lib
    _RENDER_OK = True
except ImportError:
    _RENDER_OK = False


def _expect_error(callable_, *args, **kwargs):
    try:
        callable_(*args, **kwargs)
    except WorkerError as e:
        return e
    raise AssertionError(f"expected WorkerError; got success from {callable_}")


# --- tests --------------------------------------------------------------------

def test_slice1_resolve_bad_tag_keeps_worker_alive():
    with Worker() as w:
        w.call("new_document", name="neg_s1")
        box = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        e = _expect_error(
            w.call, "resolve_face", handle=box["handle"], tag="f_deadbeefcafe",
        )
        assert "not found" in e.remote_message.lower(), e.remote_message
        # Worker survives.
        assert w.call("ping") == "pong"


def test_slice2_unconstrained_sketch_reports_dof():
    """An unconstrained sketch is not an error per se — Sketcher allows it.
    But close_sketch must surface the DOF state so an agent can react."""
    with Worker() as w:
        w.call("new_document", name="neg_s2")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        w.call(
            "add_sketch_geometry", sketch=sk["handle"],
            items=[{"type": "circle", "center": [3, 4], "radius": 5}],
        )
        status = w.call("close_sketch", sketch=sk["handle"])
        assert status["fully_constrained"] is False, status
        assert status["dof"] > 0, status
        # Sketcher allows a pad on an unconstrained sketch — but the DOF
        # report tells the agent to constrain first.
        assert w.call("ping") == "pong"


def test_slice3_fem_run_without_solver_errors():
    """FEM run with no solver attached must error clearly, not silently no-op."""
    with Worker() as w:
        w.call("new_document", name="neg_s3")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        analysis = w.call("fem_new_analysis")
        # Skip set_solver / set_material / mesh — go straight to run.
        e = _expect_error(
            w.call, "fem_run",
            analysis=analysis["handle"],
            workdir=fem_workdir("neg_fem"),
            _timeout=30.0,
        )
        # Either "no solver" message OR a CCX prereq failure are acceptable —
        # both are clear errors that an agent can act on.
        msg = e.remote_message.lower()
        assert "solver" in msg or "prereq" in msg or "material" in msg, (
            f"FEM error message not actionable: {e.remote_message}"
        )
        assert w.call("ping") == "pong"


def test_slice4_render_empty_handle_errors():
    """Tessellate of a non-shape should error, not crash. (Render of an
    empty mesh array would return a blank PNG — but that's a render-lib
    contract, not the worker's.)"""
    if not _RENDER_OK:
        print("    SKIP (render libs not installed)")
        return
    with Worker() as w:
        w.call("new_document", name="neg_s4")
        body = w.call("make_body")  # PartDesign Body has no Shape until features added
        e = _expect_error(w.call, "tessellate", handle=body["handle"])
        assert "shape" in e.remote_message.lower(), e.remote_message
        assert w.call("ping") == "pong"

        # The render lib itself: empty inputs → blank PNG, no exception.
        png = render_lib.render_mesh([], [], width=64, height=64)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_slice5_add_part_missing_file_errors():
    """Adding a part from a nonexistent FCStd path must error clearly."""
    with Worker() as w:
        w.call("new_document", name="neg_s5")
        asm = w.call("make_assembly")
        e = _expect_error(
            w.call, "add_part",
            assembly=asm["handle"],
            source={"path": "/tmp/definitely_does_not_exist_99999.FCStd"},
        )
        # FreeCAD raises something like "cannot open" or "no such file".
        msg = e.remote_message.lower()
        assert any(s in msg for s in ("open", "exist", "file", "no such")), (
            f"missing-file error not informative: {e.remote_message}"
        )
        assert w.call("ping") == "pong"
        # Assembly is unchanged.
        parts = w.call("list_assembly_parts", assembly=asm["handle"])
        assert parts == []


def test_unknown_method_keeps_worker_alive():
    """Already covered by test_worker.py::test_bad_method_survives; reproduced
    here so the tier 4 file is independently meaningful."""
    with Worker() as w:
        e = _expect_error(w.call, "no_such_method")
        assert e.type == "UnknownMethod", e.type
        assert w.call("ping") == "pong"


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
            print(f"  FAIL {name:50s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:50s} ({time.time() - t0:.2f}s)")

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

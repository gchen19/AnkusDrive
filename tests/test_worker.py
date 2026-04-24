"""
Toy problems proving the DriftPin worker scaffold.

Each test exercises ONE design property:
  - test_ready_and_ping           : worker boots, protocol handshake works
  - test_version                  : introspection passthrough
  - test_state_persists           : doc created in call #1 visible in call #2
  - test_handles_chain            : multi-step CAD (box + cylinder + cut) via handles
  - test_save_document            : disk-side effect, file size sanity
  - test_bad_method_survives      : unknown method is a recoverable error
  - test_handler_exception_survives: error mid-handler doesn't poison worker state
  - test_stdio_hygiene            : chatter-heavy ops don't corrupt stdout
  - test_graceful_shutdown        : shutdown exits cleanly, no zombie
  - test_fem_cantilever           : full FEM pipeline through IPC

Run:  /Applications/FreeCAD.app/Contents/Resources/bin/python tests/test_worker.py
  or  python3 tests/test_worker.py   (host-side subprocess management is pure stdlib)
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker, WorkerError  # noqa: E402
from driftpin.client import WorkerDied  # noqa: E402


# --- individual tests ---------------------------------------------------------

def test_ready_and_ping():
    with Worker() as w:
        assert w.freecad_version[:2] == ["1", "1"], f"wrong version: {w.freecad_version}"
        assert w.call("ping") == "pong"


def test_version():
    with Worker() as w:
        v = w.call("version")
        assert v["freecad"][:2] == ["1", "1"]
        assert v["python"].startswith("3.11"), f"expected py 3.11, got {v['python']}"


def test_state_persists():
    with Worker() as w:
        w.call("new_document", name="persist")
        w.call("add_primitive", kind="box", w=10, d=20, h=5)
        objs = w.call("list_objects")
        names = [o["name"] for o in objs]
        assert "Box" in names, f"box not in objects: {names}"


def test_handles_chain():
    """Compose a multi-step CAD part using only handles across calls.
    box (20×20×20) minus cylinder (r=5 full-height) = 8000 - π·25·20 ≈ 6429 mm³."""
    with Worker() as w:
        w.call("new_document", name="chain")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        cyl = w.call(
            "add_primitive", kind="cylinder", r=5, h=20,
            placement=[10, 10, 0],
        )
        cut = w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])

        expected = 20 * 20 * 20 - 3.14159 * 5 * 5 * 20
        v = cut["volume"]
        assert abs(v - expected) / expected < 0.01, (
            f"cut volume {v:.1f} not near expected {expected:.1f}"
        )

        handles = w.call("list_handles")
        assert {"box_1", "cylinder_1", "cut_1"} <= set(handles), (
            f"missing handles: {handles.keys()}"
        )


def test_save_document():
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="saved")
        w.call("add_primitive", kind="box", w=10, d=10, h=10)
        path = os.path.join(tmp, "part.FCStd")
        result = w.call("save_document", path=path)
        assert os.path.isfile(path)
        assert result["size"] > 500, f"suspiciously small FCStd: {result['size']} bytes"


def test_save_document_has_fitted_camera():
    """Headless doc.saveAs writes no GuiDocument; our save must inject one
    with a camera sized to the content, else the GUI opens it invisible."""
    import zipfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="tiny")
        w.call("add_primitive", kind="box", w=5, d=5, h=10)
        path = os.path.join(tmp, "tiny.FCStd")
        result = w.call("save_document", path=path)
        assert result["camera_fit"] is True, "camera_fit flag not set"
        with zipfile.ZipFile(path) as zf:
            assert "GuiDocument.xml" in zf.namelist(), "GuiDocument.xml missing"
            gui = zf.read("GuiDocument.xml").decode()
        assert "<Camera" in gui
        # Parse height; for a 5x5x10 bbox (diag ~12.25) height should be ~18,
        # definitely not the 15000+ that FreeCAD defaults to when camera is absent.
        import re
        m = re.search(r"height\s+([0-9.]+)", gui)
        assert m, "no height field in camera"
        height = float(m.group(1))
        assert 1.0 < height < 200.0, f"camera height {height} not sensible for 10mm part"


def test_bad_method_survives():
    """Unknown method → error response, worker still healthy for next call."""
    with Worker() as w:
        try:
            w.call("no_such_method")
        except WorkerError as e:
            assert e.type == "UnknownMethod"
        else:
            raise AssertionError("expected WorkerError for unknown method")

        assert w.call("ping") == "pong", "worker poisoned by bad method"


def test_handler_exception_survives():
    """Exception inside a handler → error response with traceback, worker alive."""
    with Worker() as w:
        try:
            w.call("boolean_op", op="cut", base="nope_1", tool="nope_2")
        except WorkerError as e:
            assert "unknown handle" in e.remote_message.lower(), e.remote_message
            assert e.remote_traceback, "traceback should be populated"
        else:
            raise AssertionError("expected WorkerError for bad handle")

        assert w.call("ping") == "pong", "worker poisoned by handler exception"


def test_stdio_hygiene():
    """Chatter-heavy ops (many recomputes) must not leak into stdout."""
    with Worker() as w:
        result = w.call("recompute_stress", n=15)
        assert result["objects"] == 15
        assert w.call("ping") == "pong"


def test_graceful_shutdown():
    w = Worker()
    w.call("ping")
    w.shutdown(timeout=5.0)
    assert w.proc.returncode == 0, f"expected clean exit, got {w.proc.returncode}"


def test_fem_cantilever():
    """The full FEM pipeline works through IPC. ~3–4s wall time."""
    with Worker() as w:
        t0 = time.time()
        r = w.call("fem_cantilever_demo", _timeout=180.0, mesh_size=500.0)
        elapsed = time.time() - t0
        assert r["nodes"] > 0 and r["tets"] > 0, f"empty mesh: {r}"
        assert r["max_displacement_mm"] > 0
        assert r["max_vonmises_mpa"] > 0
        print(
            f"    FEM: nodes={r['nodes']} tets={r['tets']} "
            f"|u|max={r['max_displacement_mm']:.4f}mm "
            f"vM_max={r['max_vonmises_mpa']:.2f}MPa "
            f"({elapsed:.1f}s)"
        )


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
            print(f"  FAIL {name:40s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:40s} ({time.time() - t0:.2f}s)")

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

"""
Tier 5 performance baselines (TEST_PLAN tier 5).

Per-operation wall-time budgets. Gated behind RUN_PERF=1 because timings on a
busy machine are noisy and we don't want CI false-fails.

Budgets are set at ~2× typical timing on the dev machine (M-series Mac, FreeCAD
1.1.1). When a regression triggers, the assertion message names the operation
and the ratio over budget so failures are diagnosable without re-running.

Run weekly or before tagging a release:
    RUN_PERF=1 .venv/bin/python3 tests/test_perf.py

Re-baseline (after intentional perf changes) by editing BUDGETS below.
"""
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

try:
    from driftpin import render as render_lib
    _RENDER_OK = True
except ImportError:
    _RENDER_OK = False


# Budgets in seconds. Set ~2× typical wall-time observed on the dev machine.
# Re-baseline by running the suite, looking at the actual numbers in the
# output, and updating these values.
BUDGETS = {
    "worker_boot":            2.0,    # typical ~0.4s
    "add_primitive":          0.5,    # typical ~0.05s
    "boolean_op":             0.5,    # typical ~0.05s
    "pad_simple":             0.5,    # typical ~0.1s
    "pocket_through":         0.5,    # typical ~0.1s
    "tessellate_cube":        0.3,    # typical ~0.05s
    "render_256":             0.5,    # typical ~0.1s
    "render_512":             1.5,    # typical ~0.4s
    "fem_cantilever_total":   5.0,    # typical ~1s
    "save_document":          1.0,    # typical ~0.1s
    "list_faces":             0.3,    # typical ~0.02s
    "query_faces":            0.3,    # typical ~0.02s
    "mass_properties":        0.3,    # typical ~0.02s
    "interference_check":     1.0,    # typical ~0.1s
}


_observed = {}


def _measure(name, fn):
    t0 = time.time()
    result = fn()
    elapsed = time.time() - t0
    _observed[name] = elapsed
    budget = BUDGETS[name]
    assert elapsed < budget, (
        f"{name}: {elapsed:.3f}s exceeded budget {budget:.2f}s "
        f"({elapsed / budget:.1f}× over)"
    )
    return result


# --- tests --------------------------------------------------------------------

def test_perf_worker_boot():
    """Cold worker boot — the floor on every CLI invocation."""
    t0 = time.time()
    w = Worker()
    elapsed = time.time() - t0
    _observed["worker_boot"] = elapsed
    try:
        budget = BUDGETS["worker_boot"]
        assert elapsed < budget, (
            f"worker_boot: {elapsed:.3f}s exceeded budget {budget:.2f}s"
        )
    finally:
        w.shutdown()


def test_perf_geometry_pipeline():
    """End-to-end geometry budgets in one worker session."""
    with Worker() as w:
        w.call("new_document", name="perf_geom")

        box = _measure("add_primitive", lambda: w.call(
            "add_primitive", kind="box", w=20, d=20, h=20,
        ))
        cyl = w.call(
            "add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0],
        )
        cut = _measure("boolean_op", lambda: w.call(
            "boolean_op", op="cut", base=box["handle"], tool=cyl["handle"],
        ))

        _measure("list_faces", lambda: w.call("list_faces", handle=cut["handle"]))
        _measure("query_faces", lambda: w.call(
            "query_faces", handle=cut["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1]},
        ))
        _measure("mass_properties", lambda: w.call(
            "mass_properties", handle=cut["handle"], density=7.9e-6,
        ))
        _measure("tessellate_cube", lambda: w.call(
            "tessellate", handle=cut["handle"], deflection=0.5,
        ))


def test_perf_partdesign_pipeline():
    """PartDesign pad + pocket budgets."""
    with Worker() as w:
        w.call("new_document", name="perf_pd")
        body = w.call("make_body")
        sk = w.call("make_sketch", body=body["handle"], plane="XY")
        g = w.call(
            "add_sketch_geometry", sketch=sk["handle"],
            items=[
                {"type": "line", "start": [0, 0],   "end": [20, 0]},
                {"type": "line", "start": [20, 0],  "end": [20, 20]},
                {"type": "line", "start": [20, 20], "end": [0, 20]},
                {"type": "line", "start": [0, 20],  "end": [0, 0]},
            ],
        )["indices"]
        for i in range(4):
            w.call(
                "add_sketch_constraint", sketch=sk["handle"], type="Coincident",
                refs=[[g[i], 2], [g[(i + 1) % 4], 1]],
            )
        pad = _measure("pad_simple", lambda: w.call(
            "pad", sketch=sk["handle"], length=10.0,
        ))

        top = w.call(
            "query_faces", handle=pad["handle"],
            predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
        )[0]
        plane = w.call(
            "make_datum_plane", body=body["handle"],
            base={"handle": pad["handle"], "tag": top["tag"]},
        )
        sk2 = w.call("make_sketch", body=body["handle"], plane=plane["handle"])
        w.call(
            "add_sketch_geometry", sketch=sk2["handle"],
            items=[{"type": "circle", "center": [10, 10], "radius": 3}],
        )
        _measure("pocket_through", lambda: w.call(
            "pocket", sketch=sk2["handle"], through_all=True,
        ))


def test_perf_render():
    """Software rasterizer budgets at 256² and 512²."""
    if not _RENDER_OK:
        print("    SKIP (render libs not installed)")
        return
    with Worker() as w:
        w.call("new_document", name="perf_render")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        mesh = w.call("tessellate", handle=box["handle"], deflection=0.3)

        _measure("render_256", lambda: render_lib.render_mesh(
            mesh["vertices"], mesh["triangles"],
            width=256, height=256, view="iso",
        ))
        _measure("render_512", lambda: render_lib.render_mesh(
            mesh["vertices"], mesh["triangles"],
            width=512, height=512, view="iso",
        ))


def test_perf_fem_cantilever():
    """Full FEM pipeline (geometry → mesh → CCX → results) budget."""
    with Worker() as w:
        _measure("fem_cantilever_total", lambda: w.call(
            "fem_cantilever_demo", _timeout=180.0, mesh_size=500.0,
        ))


def test_perf_save_document():
    """save_document including bbox-fit camera patch."""
    import tempfile
    with Worker() as w, tempfile.TemporaryDirectory() as tmp:
        w.call("new_document", name="perf_save")
        w.call("add_primitive", kind="box", w=20, d=20, h=20)
        path = os.path.join(tmp, "perf.FCStd")
        _measure("save_document", lambda: w.call(
            "save_document", path=path,
        ))


def test_perf_assembly_interference():
    """Assembly interference_check on a 4-part assembly."""
    with Worker() as w:
        w.call("new_document", name="perf_asm")
        boxes = []
        for i in range(4):
            b = w.call("add_primitive", kind="box", w=10, d=10, h=10)
            boxes.append(b["handle"])
        asm = w.call("make_assembly")
        for i, h in enumerate(boxes):
            w.call(
                "add_part", assembly=asm["handle"],
                source={"handle": h}, placement=[i * 5, 0, 0],
            )
        _measure("interference_check", lambda: w.call(
            "interference_check", assembly=asm["handle"],
        ))


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not os.environ.get("RUN_PERF"):
        print(
            "Perf suite is gated behind RUN_PERF=1 (timings are machine-dependent\n"
            "and CI noise causes false-fails). Set RUN_PERF=1 to run."
        )
        return

    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:42s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:42s} ({time.time() - t0:.2f}s)")

    print()
    if _observed:
        print("Observed timings (vs budget):")
        for op, t in sorted(_observed.items()):
            budget = BUDGETS[op]
            ratio = t / budget
            bar = "#" * int(ratio * 20)
            print(f"  {op:28s} {t * 1000:8.1f}ms / {budget * 1000:>6.0f}ms ({ratio * 100:4.0f}%)  {bar}")
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

"""
Tier 2 determinism tests (TEST_PLAN tier 2).

Same input → same output, across worker boots and repeated calls.

  test_two_workers_bitwise_geometry  : independent processes produce identical
                                       volumes / mass / tessellation / render bytes
  test_repeated_calls_in_one_worker  : two new_document cycles in one worker
                                       produce identical outputs
  test_fem_within_tolerance          : FEM is solver-bound; assert results
                                       agree within established 8%/15% bounds
                                       (Slice 3 baseline)

The bit-for-bit geometry assertion is load-bearing: it's what catches hidden
state, accidental caching, or worker-counter leakage.

Run: .venv/bin/python3 tests/test_determinism.py
"""
import hashlib
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


def _build_chain(w, doc_name):
    """Standard CSG chain: box minus offset cylinder. Returns (handle, mass_props,
    tessellation, optional render PNG)."""
    w.call("new_document", name=doc_name)
    box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
    cyl = w.call(
        "add_primitive", kind="cylinder", r=5, h=20,
        placement=[10, 10, 0],
    )
    cut = w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])
    mp = w.call("mass_properties", handle=cut["handle"], density=7.9e-6)
    tess = w.call("tessellate", handle=cut["handle"], deflection=0.3)
    png = None
    if _RENDER_OK:
        png = render_lib.render_mesh(
            tess["vertices"], tess["triangles"],
            width=256, height=256, view="iso",
        )
    return cut["handle"], mp, tess, png


def _summarize(mp, tess, png):
    """Reduce outputs to comparable form. Volumes/CG must match bit-for-bit;
    tessellation triangle count must match; PNG SHA-256 must match."""
    return {
        "volume": mp["volume_mm3"],
        "area": mp["surface_area_mm2"],
        "cg": tuple(mp["center_of_mass_mm"]),
        "mass": mp["mass_kg"],
        "tri_count": len(tess["triangles"]),
        "vert_count": len(tess["vertices"]),
        "png_hash": hashlib.sha256(png).hexdigest() if png else None,
    }


# --- tests --------------------------------------------------------------------

def test_two_workers_bitwise_geometry():
    """Independent worker processes produce identical outputs.

    The strict assertion: every floating-point geometry value matches exactly,
    not within tolerance. OpenCASCADE is deterministic; if this fails, the
    worker has hidden state (or we accidentally introduced randomness)."""
    with Worker() as w_a:
        _, mp_a, tess_a, png_a = _build_chain(w_a, "wa")
    with Worker() as w_b:
        _, mp_b, tess_b, png_b = _build_chain(w_b, "wb")

    sum_a = _summarize(mp_a, tess_a, png_a)
    sum_b = _summarize(mp_b, tess_b, png_b)
    assert sum_a == sum_b, (
        f"two workers diverged:\n  A: {sum_a}\n  B: {sum_b}"
    )


def test_repeated_calls_in_one_worker():
    """Two new_document cycles in one worker produce identical outputs.

    Catches handle-counter leakage, _SCRIPT_GLOBALS poisoning, accidental
    caching across documents."""
    with Worker() as w:
        _, mp1, tess1, png1 = _build_chain(w, "first")
        # Counter state survives — we don't assert handle names match (they
        # have monotonically increasing suffixes), but the outputs must.
        _, mp2, tess2, png2 = _build_chain(w, "second")

    sum1 = _summarize(mp1, tess1, png1)
    sum2 = _summarize(mp2, tess2, png2)
    assert sum1 == sum2, (
        f"repeated calls diverged:\n  1: {sum1}\n  2: {sum2}"
    )


def test_fem_results_within_tolerance():
    """FEM is solver-bound and Gmsh has bounded nondeterminism. Two runs of
    the same geometry must agree within the same tolerances we use elsewhere
    (8% disp, 15% stress — Slice 3 baseline)."""
    with Worker() as w_a:
        a = w_a.call("fem_cantilever_demo", _timeout=180.0, mesh_size=500.0)
    with Worker() as w_b:
        b = w_b.call("fem_cantilever_demo", _timeout=180.0, mesh_size=500.0)

    da = a["max_displacement_mm"]
    db = b["max_displacement_mm"]
    sa = a["max_vonmises_mpa"]
    sb = b["max_vonmises_mpa"]

    disp_diff = abs(da - db) / max(da, db)
    stress_diff = abs(sa - sb) / max(sa, sb)
    assert disp_diff < 0.08, (
        f"FEM disp variance too high across runs: "
        f"a={da:.4f}mm, b={db:.4f}mm ({disp_diff:.1%})"
    )
    assert stress_diff < 0.15, (
        f"FEM stress variance too high across runs: "
        f"a={sa:.2f}MPa, b={sb:.2f}MPa ({stress_diff:.1%})"
    )
    print(
        f"    FEM cross-run: disp diff {disp_diff:.1%} (<8%), "
        f"stress diff {stress_diff:.1%} (<15%)"
    )


_AIRTIGHT_SRC = '''
import Part, FreeCAD as App
doc = App.ActiveDocument
part = Part.makeBox(40, 20, 20, App.Vector(0, -10, -10)).cut(
    Part.makeBox(44, 16, 16, App.Vector(-2, -8, -8)))
f = doc.addObject("Part::Feature", "Adapter"); f.Shape = part; doc.recompute()
'''


def _airtight_result(w):
    w.call("new_document", name="air")
    h = w.call("run_script", code=_AIRTIGHT_SRC)["registered"][0]["handle"]
    inlet = w.call("query_faces", handle=h, predicate={
        "type": "planar", "normal_dir": [-1, 0, 0], "centroid_min": "x"})[0]["tag"]
    outlet = w.call("query_faces", handle=h, predicate={
        "type": "planar", "normal_dir": [1, 0, 0], "centroid_max": "x"})[0]["tag"]
    return w.call("check_airtight_path", handle=h, inlet=inlet, outlet=outlet,
                  min_aperture_mm2=10.0)


def test_check_airtight_path_deterministic():
    """check_airtight_path's BREP void analysis is bit-identical across worker
    boots — guards the ambient-solid tie-break ordering and the slice-sweep
    bottleneck float against hidden nondeterminism."""
    with Worker() as w_a:
        ra = _airtight_result(w_a)
    with Worker() as w_b:
        rb = _airtight_result(w_b)
    assert ra == rb, f"check_airtight_path diverged:\n  A: {ra}\n  B: {rb}"


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
            print(f"  FAIL {name:45s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:45s} ({time.time() - t0:.2f}s)")

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

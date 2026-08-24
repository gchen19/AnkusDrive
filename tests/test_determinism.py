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

EXACT vs BOUNDED is kept explicit (issue #123). Two surfaces beyond the geometry
kernel are swept here, driven by tests/determinism_registry.py:

  test_analysis_tools_bitwise_*      : the closed-form analysis/screen family
                                       (machine elements, fits, thermal, fluids,
                                       dfx, screens, …) MUST be bit-identical
                                       across two workers and repeated calls —
                                       these are "exact". A hidden dict-ordering,
                                       set iteration, or unseeded RNG fails here.
  test_bounded_submits_within_*      : the async `*_submit` solver family is
                                       "bounded" — reproducible only within a
                                       documented tolerance envelope. Only the
                                       cheap representative runs on the shared
                                       host (heavy solves are declared, skipped).

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from determinism_registry import ANALYSIS_SWEEP, BOUNDED_SUBMITS  # noqa: E402
from ankusdrive import Worker  # noqa: E402

try:
    from ankusdrive import render as render_lib
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


def _solve_box_cantilever(w):
    """Minimal decomposed cantilever (box, fixed -X end, +Z tip load) solved with
    ccx. Returns (analysis_handle, body_handle, fixed_tag, loaded_tag) so the
    probe can be exercised against a real solved result."""
    w.call("new_document", name="probe_det")
    box = w.call("add_primitive", kind="box", w=40, d=10, h=10)
    bh = box["handle"]
    fixed = w.call("query_faces", handle=bh, predicate={
        "type": "planar", "normal_dir": [-1, 0, 0], "centroid_min": "x"})[0]["tag"]
    loaded = w.call("query_faces", handle=bh, predicate={
        "type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"})[0]["tag"]
    an = w.call("fem_new_analysis", name="ProbeDet")["handle"]
    w.call("fem_set_solver", analysis=an, kind="ccx", tunables={
        "GeometricalNonlinearity": "linear", "ThermoMechSteadyState": True,
        "MatrixSolverType": "default", "IterationsControlParameterTimeUse": False})
    w.call("fem_set_material", analysis=an, body=bh, material={
        "Name": "Steel-Generic", "YoungsModulus": "210000 MPa",
        "PoissonRatio": "0.30", "Density": "7900 kg/m^3"})
    w.call("fem_add_constraint", analysis=an, kind="fixed",
           refs=[{"handle": bh, "tag": fixed}])
    w.call("fem_add_constraint", analysis=an, kind="force",
           refs=[{"handle": bh, "tag": loaded}], force=1000.0)
    w.call("fem_mesh", analysis=an, body=bh, char_length=4.0, _timeout=180.0)
    w.call("fem_run", analysis=an, workdir="/tmp/ankusdrive_probe_det_fem",
           _timeout=300.0)
    return an, bh, fixed, loaded


def test_fem_result_probe_deterministic():
    """fem_result_probe is a PURE read over a fixed solved result, so repeated
    probes of the same analysis must be bit-identical. This isolates the probe's
    own determinism — stable element-iteration order, stable nearest-node
    tie-break, stable dict ordering — from the solver's bounded nondeterminism
    (which test_fem_results_within_tolerance already covers). Exercises all three
    code paths: interpolated interior point, off-mesh nearest-node fallback, and
    face aggregation."""
    with Worker() as w:
        an, bh, _fixed, loaded = _solve_box_cantilever(w)

        # Interior point → barycentric interpolation.
        p1 = w.call("fem_result_probe", analysis=an, point=[20, 5, 5])
        p2 = w.call("fem_result_probe", analysis=an, point=[20, 5, 5])
        assert p1["method"] == "interpolated", p1
        assert p1 == p2, f"interpolated probe diverged:\n  1: {p1}\n  2: {p2}"

        # Off-mesh point → nearest-node fallback (tie-break must be stable).
        o1 = w.call("fem_result_probe", analysis=an, point=[500, 500, 500])
        o2 = w.call("fem_result_probe", analysis=an, point=[500, 500, 500])
        assert o1["method"] == "nearest_node", o1
        assert o1 == o2, f"nearest-node probe diverged:\n  1: {o1}\n  2: {o2}"

        # Face aggregation → min/max/mean over the face's mesh nodes.
        f1 = w.call("fem_result_probe", analysis=an, handle=bh, face=loaded)
        f2 = w.call("fem_result_probe", analysis=an, handle=bh, face=loaded)
        assert f1["mode"] == "face" and f1["node_count"] > 0, f1
        assert f1 == f2, f"face probe diverged:\n  1: {f1}\n  2: {f2}"
    print(
        f"    probe: interp |u|={p1['displacement_mm']:.5f}mm exact-repeat OK; "
        f"nearest-node d={o1['distance_mm']:.1f}mm OK; "
        f"face nodes={f1['node_count']} OK"
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


# --- table-driven analysis sweep (issue #123) ---------------------------------

def _run_analysis_sweep(w):
    """Call every (tool, kwargs) in ANALYSIS_SWEEP once; return {tool: result}."""
    return {name: w.call(name, **kwargs) for name, kwargs in ANALYSIS_SWEEP}


def _diff_sweep(a, b):
    """Tools whose result differs between two sweeps (exact dict equality)."""
    return [name for name in a if a[name] != b[name]]


def test_analysis_tools_bitwise_two_workers():
    """The closed-form analysis/screen family is "exact": every tool in
    ANALYSIS_SWEEP returns a bit-for-bit identical result in two independent
    worker processes. This is the breadth complement to the geometry-kernel
    bitwise test — it nets a hidden dict-ordering, set-iteration order, or
    (the load-bearing case) an UNSEEDED RNG in any screen. tolerance_stackup runs
    method='montecarlo' on purpose, so the seed contract (seed=12345) is asserted
    too: drop the seed and this test is what goes red."""
    with Worker() as w_a:
        ra = _run_analysis_sweep(w_a)
    with Worker() as w_b:
        rb = _run_analysis_sweep(w_b)
    mismatches = _diff_sweep(ra, rb)
    assert not mismatches, (
        "analysis tools diverged across two workers (non-deterministic): "
        + ", ".join(f"{n}\n  A={ra[n]}\n  B={rb[n]}" for n in mismatches)
    )
    print(f"    analysis sweep: {len(ANALYSIS_SWEEP)} exact tools bit-identical "
          f"across two workers")


def test_analysis_tools_repeated_in_one_worker():
    """Same ANALYSIS_SWEEP, but two passes in ONE worker — catches per-call state
    leakage (cached module globals, accumulating counters, a seeded RNG advanced
    by the first call) that a two-worker comparison would miss."""
    with Worker() as w:
        r1 = _run_analysis_sweep(w)
        r2 = _run_analysis_sweep(w)
    mismatches = _diff_sweep(r1, r2)
    assert not mismatches, (
        "analysis tools diverged on a repeated call in one worker: "
        + ", ".join(f"{n}\n  1={r1[n]}\n  2={r2[n]}" for n in mismatches)
    )


def test_analysis_sweep_has_breadth():
    """Guard the table itself: if someone guts ANALYSIS_SWEEP the two tests above
    pass vacuously. Hold a floor on coverage (machine-elements, fits/tolerance,
    thermal, fluids, dfx/cost, screen + closed-form-twin families)."""
    assert len(ANALYSIS_SWEEP) >= 40, (
        f"ANALYSIS_SWEEP shrank to {len(ANALYSIS_SWEEP)} — coverage floor is 40"
    )


def _await_job(w, job_id, timeout=30.0):
    """Poll job_status until terminal, then return job_result. The async worker
    threads finish in microseconds once unblocked (jobs.py contract)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if w.call("job_status", job_id=job_id)["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    return w.call("job_result", job_id=job_id)


def test_bounded_submits_within_envelope():
    """Async `*_submit` results are "bounded": reproducible within a documented
    per-solver tolerance, asserted the same way test_fem_results_within_tolerance
    does. Only BOUNDED_SUBMITS entries flagged run=True execute (the cheap async
    representative); the heavy real-solver entries are declared with their bound
    but skipped so CI doesn't burn minutes on a shared host. The cheap path also
    proves the submit→job_result async registry round-trips deterministically."""
    ran = []
    for spec in BOUNDED_SUBMITS:
        if not spec.get("run"):
            continue
        tool, kwargs, fields, rtol = (
            spec["tool"], spec["kwargs"], spec["fields"], spec["rtol"])
        with Worker() as w_a:
            ja = w_a.call(tool, **kwargs)
            ra = _await_job(w_a, ja["job_id"])["result"]
        with Worker() as w_b:
            jb = w_b.call(tool, **kwargs)
            rb = _await_job(w_b, jb["job_id"])["result"]
        for f in fields:
            va, vb = float(ra[f]), float(rb[f])
            denom = max(abs(va), abs(vb), 1e-12)
            diff = abs(va - vb) / denom
            assert diff <= rtol, (
                f"{tool}.{f} variance {diff:.2%} exceeds bound {rtol:.0%}: "
                f"a={va}, b={vb}"
            )
        ran.append(tool)
    declared = [s["tool"] for s in BOUNDED_SUBMITS if not s.get("run")]
    print(f"    bounded submits: ran {ran} within envelope; "
          f"{len(declared)} heavy solvers declared (bound documented, skipped)")


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

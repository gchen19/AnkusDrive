"""
Cross-slice integration test (TEST_PLAN tier 1).

Single test that walks all five slices in one Worker session:
  Slice 2 (PartDesign): build a parametric body — square pad, hole, fillet
  Slice 1 (tags):       tag the loaded face + bottom face for FEM constraints
  Slice 3 (FEM):        analysis, solver, material, fixed/force by tag, mesh, run, results
  Slice 4 (render):     iso/top/front renders, distinct + non-blank
  Slice 5 (mass+asm+drawing): mass_properties, 2-part assembly, BOM, drawing PDF

Asserts each phase's output and that references survive across the pipeline.

Run: .venv/bin/python3 tests/test_integration.py
  (host venv needs Pillow + numpy for the render step)
"""
import hashlib
import io
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker, WorkerError  # noqa: E402

try:
    from ankusdrive import render as render_lib
    import numpy as np
    from PIL import Image
    _RENDER_OK = True
except ImportError as e:
    _RENDER_OK = False
    _RENDER_IMPORT_ERR = str(e)


def _build_partdesign_body(w):
    """Slice 2: build a 30×30×10mm pad with a 5mm hole and one filleted top edge.
    Returns (body_handle, pad_handle, fillet_handle)."""
    body = w.call("make_body", name="Body")
    sk = w.call("make_sketch", body=body["handle"], plane="XY")
    g = w.call(
        "add_sketch_geometry", sketch=sk["handle"],
        items=[
            {"type": "line", "start": [0, 0],   "end": [30, 0]},
            {"type": "line", "start": [30, 0],  "end": [30, 30]},
            {"type": "line", "start": [30, 30], "end": [0, 30]},
            {"type": "line", "start": [0, 30],  "end": [0, 0]},
        ],
    )["indices"]
    for i in range(4):
        w.call(
            "add_sketch_constraint", sketch=sk["handle"], type="Coincident",
            refs=[[g[i], 2], [g[(i + 1) % 4], 1]],
        )
    pad = w.call("pad", sketch=sk["handle"], length=10.0)
    expected_pad_vol = 30 * 30 * 10
    assert abs(pad["volume"] - expected_pad_vol) / expected_pad_vol < 0.01, (
        f"pad volume off: {pad['volume']} vs {expected_pad_vol}"
    )

    # Hole: pocket through the top face (selected by tag — Slice 1 in Slice 2's mouth).
    top = w.call(
        "query_faces", handle=pad["handle"],
        predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
    )[0]
    plane = w.call(
        "make_datum_plane", body=body["handle"],
        base={"handle": pad["handle"], "tag": top["tag"]},
    )
    sk2 = w.call("make_sketch", body=body["handle"], plane=plane["handle"])
    g2 = w.call(
        "add_sketch_geometry", sketch=sk2["handle"],
        items=[{"type": "circle", "center": [15, 15], "radius": 2.5}],
    )["indices"]
    w.call(
        "add_sketch_constraint", sketch=sk2["handle"], type="Radius",
        refs=[[g2[0], 0]], value=2.5,
    )
    pocket = w.call("pocket", sketch=sk2["handle"], through_all=True)
    expected_pocket_vol = expected_pad_vol - 3.14159 * 6.25 * 10
    assert abs(pocket["volume"] - expected_pocket_vol) / expected_pocket_vol < 0.01, (
        f"pocket volume off: {pocket['volume']} vs {expected_pocket_vol}"
    )

    # Fillet a top edge.
    edges = w.call("list_edges", handle=pocket["handle"])
    top_edges = [
        e for e in edges
        if e["kind"] == "line"
        and abs(e["centroid"][2] - 10.0) < 1e-3
        and abs(e["length"] - 30.0) < 1e-3
    ]
    assert top_edges, "no top edges found to fillet"
    fillet = w.call(
        "partdesign_fillet",
        feature=pocket["handle"],
        edges=[top_edges[0]["tag"]],
        radius=2.0,
    )
    assert fillet["volume"] < pocket["volume"], (
        f"fillet should reduce volume: {pocket['volume']} -> {fillet['volume']}"
    )

    return body["handle"], pad["handle"], fillet["handle"]


def _tag_fem_faces(w, fillet_handle):
    """Slice 1: tag the bottom face (will be fixed) and one side face (will be loaded).
    Returns (bottom_tag, loaded_tag)."""
    bottoms = w.call(
        "query_faces", handle=fillet_handle,
        predicate={"type": "planar", "normal_dir": [0, 0, -1]},
    )
    assert len(bottoms) == 1, f"expected 1 bottom face, got {len(bottoms)}"

    # Pick the +X side face — opposite the fillet, so a force pushing -X bends
    # the body around the fillet.
    sides = w.call(
        "query_faces", handle=fillet_handle,
        predicate={"type": "planar", "normal_dir": [1, 0, 0]},
    )
    assert len(sides) == 1, f"expected 1 +X side face, got {len(sides)}"
    return bottoms[0]["tag"], sides[0]["tag"]


def _run_fem(w, fillet_handle, bottom_tag, loaded_tag):
    """Slice 3: build analysis with constraints by tag, mesh, run, get results."""
    analysis = w.call("fem_new_analysis", name="IntegrationFEM")
    w.call(
        "fem_set_solver", analysis=analysis["handle"], kind="ccx",
        tunables={
            "GeometricalNonlinearity": "linear",
            "ThermoMechSteadyState": True,
            "MatrixSolverType": "default",
            "IterationsControlParameterTimeUse": False,
        },
    )
    w.call(
        "fem_set_material",
        analysis=analysis["handle"],
        body=fillet_handle,
        material={
            "Name": "Steel-Generic",
            "YoungsModulus": "210000 MPa",
            "PoissonRatio": "0.30",
            "Density": "7900 kg/m^3",
        },
    )
    w.call(
        "fem_add_constraint",
        analysis=analysis["handle"],
        kind="fixed",
        refs=[{"handle": fillet_handle, "tag": bottom_tag}],
    )
    w.call(
        "fem_add_constraint",
        analysis=analysis["handle"],
        kind="force",
        refs=[{"handle": fillet_handle, "tag": loaded_tag}],
        force=1000.0,
    )
    mesh = w.call(
        "fem_mesh",
        analysis=analysis["handle"],
        body=fillet_handle,
        char_length=3.0,
        _timeout=180.0,
    )
    assert mesh["nodes"] > 0 and mesh["tets"] > 0, f"empty mesh: {mesh}"

    w.call(
        "fem_run",
        analysis=analysis["handle"],
        workdir="/tmp/ankusdrive_integration_fem",
        _timeout=300.0,
    )
    results = w.call("fem_results", analysis=analysis["handle"], top_n=3)
    assert results["max_vonmises_mpa"] > 0, "FEM produced zero stress"
    assert results["max_displacement_mm"] > 0, "FEM produced zero displacement"
    return analysis["handle"], mesh, results


def _post_fem_tag_survival(w, fillet_handle, bottom_tag, loaded_tag):
    """Slice 1 cross-slice property: tags resolved BEFORE FEM still resolve AFTER.
    This is the assertion that makes 'agent edit + re-FEM' workflows possible."""
    bot_after = w.call("resolve_face", handle=fillet_handle, tag=bottom_tag)
    load_after = w.call("resolve_face", handle=fillet_handle, tag=loaded_tag)
    assert bot_after["index"].startswith("Face"), bot_after
    assert load_after["index"].startswith("Face"), load_after


def _probe_fem_results(w, analysis_h, fillet_handle, bottom_tag, loaded_tag, summary):
    """Issue #124: probe the field at a point and over a face, not just the
    global max + top-N. Proves barycentric interpolation, nearest-node fallback,
    field filtering, and face aggregation against physical expectations."""
    gmax_disp = summary["max_displacement_mm"]
    gmax_vm = summary["max_vonmises_mpa"]

    # POINT inside the body (centroid) → barycentric interpolation inside a tet.
    inside = w.call("fem_result_probe", analysis=analysis_h, point=[5, 5, 5])
    assert inside["method"] == "interpolated", inside
    assert inside["element_id"] is not None and inside["distance_mm"] == 0.0, inside
    assert "vonmises_mpa" in inside and "displacement_mm" in inside, inside
    assert len(inside["displacement_vector"]) == 3, inside
    # Interpolated values are bounded by the global extrema (with FP slack).
    assert 0.0 <= inside["displacement_mm"] <= gmax_disp + 1e-6, (inside, gmax_disp)
    assert 0.0 <= inside["vonmises_mpa"] <= gmax_vm + 1e-3, (inside, gmax_vm)

    # POINT far outside the mesh → nearest-node fallback with a real distance.
    outside = w.call(
        "fem_result_probe", analysis=analysis_h, point=[1000, 1000, 1000],
    )
    assert outside["method"] == "nearest_node", outside
    assert outside["distance_mm"] > 100.0 and outside["node"] >= 0, outside

    # FIELD filter: request displacement only — von Mises must be absent.
    only_disp = w.call(
        "fem_result_probe", analysis=analysis_h, point=[5, 5, 5],
        field="displacement",
    )
    assert "displacement_mm" in only_disp and "vonmises_mpa" not in only_disp, only_disp

    # FACE mode: the fixed bottom face barely moves; the loaded face moves more.
    fixed_face = w.call(
        "fem_result_probe", analysis=analysis_h, handle=fillet_handle, face=bottom_tag,
    )
    loaded_face = w.call(
        "fem_result_probe", analysis=analysis_h, handle=fillet_handle, face=loaded_tag,
    )
    assert fixed_face["mode"] == "face" and fixed_face["node_count"] > 0, fixed_face
    assert loaded_face["node_count"] > 0, loaded_face
    fdisp, ldisp = fixed_face["displacement_mm"], loaded_face["displacement_mm"]
    # Constrained face → its motion is a tiny fraction of the global max.
    assert fdisp["max"] <= 0.05 * gmax_disp + 1e-6, (fixed_face, gmax_disp)
    assert fdisp["max"] < ldisp["max"], (fixed_face, loaded_face)
    # Every aggregate is internally ordered.
    for agg in (fdisp, ldisp, fixed_face["vonmises_mpa"], loaded_face["vonmises_mpa"]):
        assert agg["min"] <= agg["mean"] <= agg["max"], agg
    return inside, fixed_face, loaded_face


def _render_three_views(w, fillet_handle):
    """Slice 4: render iso/top/front. Verify all three are distinct and non-blank."""
    if not _RENDER_OK:
        print(f"    SKIP renders ({_RENDER_IMPORT_ERR})")
        return None
    mesh = w.call("tessellate", handle=fillet_handle, deflection=0.3)
    pngs = {}
    for view in ("iso", "top", "front"):
        png = render_lib.render_mesh(
            mesh["vertices"], mesh["triangles"],
            width=256, height=256, view=view,
        )
        pngs[view] = png

    hashes = {v: hashlib.sha256(p).hexdigest() for v, p in pngs.items()}
    assert len(set(hashes.values())) == 3, (
        f"expected 3 distinct renders, got {len(set(hashes.values()))}: {hashes}"
    )
    for view, png in pngs.items():
        mask = render_lib.foreground_mask(png)
        frac = mask.sum() / mask.size
        assert 0.10 < frac < 0.85, (
            f"render '{view}' has bad fg fraction {frac:.2f}"
        )
    return pngs


def _mass_props_assembly_drawing(w, fillet_handle):
    """Slice 5: mass_properties, two-part assembly, BOM, drawing PDF."""
    mp = w.call("mass_properties", handle=fillet_handle, density=7.9e-6)
    assert mp["volume_mm3"] > 0
    expected_mass = mp["volume_mm3"] * 7.9e-6
    assert abs(mp["mass_kg"] - expected_mass) < 1e-9

    asm = w.call("make_assembly", name="IntegrationAsm")
    w.call(
        "add_part", assembly=asm["handle"],
        source={"handle": fillet_handle}, placement=[0, 0, 0],
    )
    w.call(
        "add_part", assembly=asm["handle"],
        source={"handle": fillet_handle}, placement=[60, 0, 0],
    )
    parts = w.call("list_assembly_parts", assembly=asm["handle"])
    assert len(parts) == 2, f"expected 2 assembly parts, got {len(parts)}"

    bom = w.call("bom_extract", assembly=asm["handle"], density=7.9e-6)
    assert len(bom) == 1, f"expected 1 BOM row (same linked target), got {len(bom)}"
    assert bom[0]["count"] == 2, bom
    assert abs(bom[0]["total_volume_mm3"] - 2 * mp["volume_mm3"]) < 1e-3

    overlaps = w.call("interference_check", assembly=asm["handle"])
    assert overlaps == [], f"unexpected interference: {overlaps}"

    # Drawing: build page + projection group, verify it persists in saved FCStd.
    # PDF export is a known headless gap (needs TechDrawGui).
    saved_size = None
    with tempfile.TemporaryDirectory() as tmp:
        page = w.call("make_drawing_page")
        pg = w.call(
            "add_projection_group",
            page=page["handle"], body=fillet_handle,
            views=["Front", "Top", "Right"],
        )
        assert len(pg["views"]) >= 3, pg

        path = os.path.join(tmp, "integration.FCStd")
        w.call("save_document", path=path)
        saved_size = os.path.getsize(path)
        assert saved_size > 1000, f"saved doc too small: {saved_size}"

    return mp, bom, saved_size


# --- the integration test ----------------------------------------------------

def test_cross_slice_integration():
    t_total = time.time()
    with Worker() as w:
        w.call("new_document", name="integration")

        # Slice 2: PartDesign body.
        t0 = time.time()
        body_h, pad_h, fillet_h = _build_partdesign_body(w)
        t_pd = time.time() - t0

        # Slice 1: tags survive across PartDesign features.
        t0 = time.time()
        bottom_tag, loaded_tag = _tag_fem_faces(w, fillet_h)
        t_tag = time.time() - t0

        # Slice 3: FEM by tag.
        t0 = time.time()
        analysis_h, mesh, fem_results = _run_fem(
            w, fillet_h, bottom_tag, loaded_tag,
        )
        t_fem = time.time() - t0

        # Cross-slice property: tags survive the FEM pipeline.
        _post_fem_tag_survival(w, fillet_h, bottom_tag, loaded_tag)

        # Slice 3 (issue #124): probe results at a point + on a face.
        t0 = time.time()
        probe_pt, probe_fixed, probe_loaded = _probe_fem_results(
            w, analysis_h, fillet_h, bottom_tag, loaded_tag, fem_results,
        )
        t_probe = time.time() - t0

        # Slice 4: render.
        t0 = time.time()
        pngs = _render_three_views(w, fillet_h)
        t_render = time.time() - t0

        # Slice 5: mass props, assembly, drawing.
        t0 = time.time()
        mp, bom, saved_size = _mass_props_assembly_drawing(w, fillet_h)
        t_s5 = time.time() - t0

    print(
        f"    Slice 2 (PartDesign):     {t_pd:.2f}s  "
        f"vol={mp['volume_mm3']:.1f}mm³ mass={mp['mass_kg']*1000:.2f}g\n"
        f"    Slice 1 (tags):           {t_tag:.2f}s  "
        f"bottom={bottom_tag[:12]}... loaded={loaded_tag[:12]}...\n"
        f"    Slice 3 (FEM by tag):     {t_fem:.2f}s  "
        f"nodes={mesh['nodes']} vM_max={fem_results['max_vonmises_mpa']:.3f}MPa "
        f"|u|max={fem_results['max_displacement_mm']:.4f}mm\n"
        f"    Slice 3 (probe #124):     {t_probe:.2f}s  "
        f"centroid |u|={probe_pt['displacement_mm']:.4f}mm "
        f"fixed-face |u|max={probe_fixed['displacement_mm']['max']:.4f}mm "
        f"loaded-face |u|max={probe_loaded['displacement_mm']['max']:.4f}mm\n"
        f"    Slice 4 (render):         {t_render:.2f}s  "
        f"{'(skipped)' if pngs is None else '3 distinct views, all centered'}\n"
        f"    Slice 5 (mass+asm+draw):  {t_s5:.2f}s  "
        f"BOM count={bom[0]['count']} "
        f"saved_doc={saved_size}B (PDF export deferred — needs TechDrawGui)\n"
        f"    -- total: {time.time() - t_total:.2f}s --"
    )


# --- runner ------------------------------------------------------------------

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

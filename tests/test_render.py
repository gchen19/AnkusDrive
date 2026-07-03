"""
Slice 4 (visual feedback) toy-problem tests.

Strategy: assert *invariants* — never compare exact pixels. Each test proves a
property the renderer must satisfy if it's doing real work:

  test_render_returns_valid_png         : output is a parseable PNG of the right size
  test_render_non_blank                 : foreground actually rendered (not blank canvas)
  test_render_bbox_centered_iso         : auto-fit + camera fit work (geometry not off-screen)
  test_bigger_box_bigger_silhouette     : geometry scale → pixel scale (renderer responds to size)
  test_sphere_silhouette_is_disc        : *shape* renders correctly, not just *something*
  test_view_dir_changes_aspect_ratio    : the view parameter actually changes the camera
  test_multi_view_returns_distinct_imgs : not the same image three times

Run:  python3 tests/test_render.py
"""
import base64
import hashlib
import io
import shutil
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402
from driftpin import render as r  # noqa: E402


def _make_box(w, dims=(10, 10, 10)):
    w.call("new_document", name="render")
    return w.call("add_primitive", kind="box", w=dims[0], d=dims[1], h=dims[2])


def _make_sphere(w, radius=10):
    w.call("new_document", name="render_sphere")
    return w.call("add_primitive", kind="sphere", r=radius)


def _render(w, handle, view="iso", width=256, height=256, deflection=0.5):
    """Tessellate via worker, rasterize on host, return PNG bytes."""
    mesh = w.call("tessellate", handle=handle, deflection=deflection)
    return r.render_mesh(
        mesh["vertices"], mesh["triangles"],
        width=width, height=height, view=view,
    )


# --- tests --------------------------------------------------------------------

def test_render_returns_valid_png():
    with Worker() as w:
        box = _make_box(w)
        png = _render(w, box["handle"], view="iso", width=128, height=96)
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        img = Image.open(io.BytesIO(png))
        assert img.size == (128, 96), f"wrong size: {img.size}"


def test_render_non_blank():
    """Foreground exists. A blank canvas has stdev 0 and one color.
    A box with axis-aligned faces produces only 2-4 face shades + bg + edge,
    so the bar here is just 'more than one color and meaningful variance'."""
    with Worker() as w:
        box = _make_box(w)
        png = _render(w, box["handle"])
        arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        assert arr.std() > 5, f"image looks blank: stdev={arr.std()}"
        unique_colors = len(set(map(tuple, arr.reshape(-1, 3).tolist())))
        assert unique_colors >= 3, (
            f"too few colors ({unique_colors}); expected at least bg + ≥2 face shades"
        )


def test_render_bbox_centered_iso():
    """Auto-fit camera centers the geometry. Silhouette bbox center is within
    5% of frame center; silhouette occupies a reasonable fraction of frame."""
    with Worker() as w:
        box = _make_box(w, dims=(10, 10, 10))
        png = _render(w, box["handle"], view="iso", width=256, height=256)
        mask = r.foreground_mask(png)
        bbox = r.silhouette_bbox(mask)
        assert bbox is not None
        x0, y0, x1, y1 = bbox
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        assert abs(cx - 128) < 13, f"silhouette x-center {cx} not near frame center 128"
        assert abs(cy - 128) < 13, f"silhouette y-center {cy} not near frame center 128"
        frac = mask.sum() / (256 * 256)
        assert 0.15 < frac < 0.75, (
            f"silhouette area fraction {frac:.2f} not in [0.15, 0.75] — auto-fit broken"
        )


def test_bigger_box_bigger_silhouette():
    """Renderer scales with input. A 30mm cube must produce more foreground
    pixels than a 10mm cube at the same view + frame size — the auto-fit
    scales both to fit, but a larger cube has a larger projected silhouette
    relative to the frame? No — auto-fit normalizes. So: render BOTH cubes in
    the SAME scene to defeat auto-fit. We do this by manually tessellating
    and merging."""
    with Worker() as w:
        # Small box
        w.call("new_document", name="small")
        small = w.call("add_primitive", kind="box", w=10, d=10, h=10)
        small_mesh = w.call("tessellate", handle=small["handle"])

        # Big box (offset so it doesn't overlap)
        w.call("new_document", name="big")
        big = w.call("add_primitive", kind="box", w=30, d=30, h=30)
        big_mesh = w.call("tessellate", handle=big["handle"])

        # Render small alone, big alone, both together. Auto-fit applies to
        # the COMBINED scene → small box appears smaller in the combined frame
        # than alone. Property: in the combined scene, the big box silhouette
        # has > 3x the pixel count of the small box silhouette.
        small_v = np.asarray(small_mesh["vertices"])
        big_v = np.asarray(big_mesh["vertices"])
        big_v = big_v + np.array([100.0, 0.0, 0.0])  # offset
        combined_v = np.concatenate([small_v, big_v]).tolist()
        small_t = np.asarray(small_mesh["triangles"])
        big_t = np.asarray(big_mesh["triangles"]) + len(small_v)
        combined_t = np.concatenate([small_t, big_t]).tolist()

        png = r.render_mesh(combined_v, combined_t, width=400, height=200, view="top")
        mask = r.foreground_mask(png)

        # Split mask in half: x < 200 captures small, x >= 200 captures big.
        # (Top view: small box at origin renders left, big box at x=100 renders right.)
        left = mask[:, :200].sum()
        right = mask[:, 200:].sum()
        assert right > 3 * left, (
            f"big box silhouette ({right}px) should be much larger than "
            f"small box ({left}px). Ratio={right / max(left, 1):.2f}"
        )


def test_sphere_silhouette_is_disc():
    """A sphere's projection is a disc. For a perfect uniformly-filled disc,
    std(r)/mean(r) is the analytical constant 3/(2·sqrt(18)) ≈ 0.354. A
    discretized sphere with anti-aliased edges and tessellation noise lands
    at ~0.36-0.38. A cube-iso silhouette is ~0.40-0.43 (corners are farther
    from centroid than edge midpoints). The discriminating property: cube > sphere."""
    with Worker() as w:
        sphere = _make_sphere(w, radius=10)
        png = _render(w, sphere["handle"], view="top", width=256, height=256, deflection=0.2)
        mask = r.foreground_mask(png)
        ys, xs = np.where(mask)
        assert xs.size > 100, f"sphere silhouette too small: {xs.size}px"
        cx = xs.mean()
        cy = ys.mean()
        d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        ratio_sphere = d.std() / d.mean()
        # Theoretical disc value is 0.354; allow [0.30, 0.40] for discretization.
        assert 0.30 < ratio_sphere < 0.40, (
            f"sphere top-view radial ratio {ratio_sphere:.3f} not disc-like "
            f"(expected ~0.354 ± discretization)"
        )

        # Cube iso silhouette has higher ratio because corners stick out further.
        w.call("new_document", name="cube_compare")
        cube = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        png_cube = _render(w, cube["handle"], view="iso", width=256, height=256)
        mask_cube = r.foreground_mask(png_cube)
        ys_c, xs_c = np.where(mask_cube)
        d_c = np.sqrt((xs_c - xs_c.mean()) ** 2 + (ys_c - ys_c.mean()) ** 2)
        ratio_cube = d_c.std() / d_c.mean()
        assert ratio_cube > ratio_sphere, (
            f"cube iso radial ratio {ratio_cube:.3f} should exceed "
            f"sphere top-view ratio {ratio_sphere:.3f} — shape sensitivity broken"
        )


def test_view_dir_changes_aspect_ratio():
    """Rendering a tall thin box from 'top' (looks square) vs 'front' (looks
    tall and thin) must produce silhouettes with very different aspect ratios."""
    with Worker() as w:
        # 10 wide × 10 deep × 50 tall.
        w.call("new_document", name="tall")
        tall = w.call("add_primitive", kind="box", w=10, d=10, h=50)
        png_top = _render(w, tall["handle"], view="top", width=256, height=256)
        png_front = _render(w, tall["handle"], view="front", width=256, height=256)

        def aspect(png):
            mask = r.foreground_mask(png)
            bbox = r.silhouette_bbox(mask)
            assert bbox is not None
            x0, y0, x1, y1 = bbox
            return (x1 - x0) / max(y1 - y0, 1)

        a_top = aspect(png_top)
        a_front = aspect(png_front)
        # Top view: silhouette ≈ square (10×10), aspect ≈ 1.0.
        # Front view: silhouette is wide×tall = 10×50, aspect ≈ 0.2.
        # The two aspects must differ by > 2x.
        ratio = max(a_top, a_front) / min(a_top, a_front)
        assert ratio > 2.0, (
            f"aspect ratios too similar: top={a_top:.2f}, front={a_front:.2f}, "
            f"ratio={ratio:.2f} — view parameter may not be respected"
        )


def test_concave_hole_is_empty_top_view():
    """Box with cylindrical hole, viewed from top: pixels at the hole CENTER
    must be background (the hole is empty), and pixels in a ring around it
    must be foreground (the box's top face). This is the test that painter's-
    algorithm failed: it would paint the back-wall triangles of the cylinder
    into the hole because their mean-z was 'closer' than the top face."""
    with Worker() as w:
        w.call("new_document", name="hole")
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        cyl = w.call(
            "add_primitive", kind="cylinder", r=5, h=20,
            placement=[10, 10, 0],  # centered hole
        )
        cut = w.call("boolean_op", op="cut", base=box["handle"], tool=cyl["handle"])
        png = _render(w, cut["handle"], view="top", width=256, height=256, deflection=0.2)
        mask = r.foreground_mask(png)
        h, wpx = mask.shape

        # Geometry: 20mm box, 5mm-radius hole, 256px frame with 5% margin →
        # box half-width ≈ 108px, hole radius ≈ 54px. So r<30 is well inside
        # the hole; r in [70,90] is well inside the top face.
        cy, cx = h // 2, wpx // 2
        ys, xs = np.mgrid[0:h, 0:wpx]
        dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)

        center = dist < 30
        center_fg_frac = mask[center].mean()
        assert center_fg_frac < 0.10, (
            f"hole center is {center_fg_frac:.0%} foreground; "
            "z-buffer broken (back-wall triangles bleeding through the hole)"
        )

        ring = (dist > 70) & (dist < 90)
        ring_fg_frac = mask[ring].mean()
        assert ring_fg_frac > 0.95, (
            f"top face ring is only {ring_fg_frac:.0%} foreground; "
            "expected solid material around the hole"
        )


def test_multi_view_returns_distinct_images():
    """Three views of the same shape produce three distinct PNGs (not the
    same image three times). Hash the bytes — collisions would mean the
    renderer ignores `view`."""
    with Worker() as w:
        box = _make_box(w, dims=(10, 20, 30))  # asymmetric so views actually differ
        hashes = set()
        for v in ("iso", "top", "front"):
            png = _render(w, box["handle"], view=v, width=200, height=200)
            hashes.add(hashlib.sha256(png).hexdigest())
        assert len(hashes) == 3, f"expected 3 distinct view renders, got {len(hashes)}"


# --- issue #174: FEM colormap, custom camera, AA, feature edges ---------------

def test_fem_colormap_monotonic_gradient():
    """render_fem_results colors a mesh by a per-vertex scalar via viridis with
    barycentric interpolation. Feed a beam a SYNTHETIC 'stress' that is hot
    (high) at the fixed end and cold (low) at the tip; the rendered viridis
    'heat' (yellow = high value: big R+G, small B) must decrease monotonically
    along the beam axis. A flat / reversed / non-interpolating renderer fails.

    Pure-render test (no solver): the field is synthetic, exercising only the
    colormap + barycentric rasterizer path."""
    with Worker() as w:
        w.call("new_document", name="beam")
        beam = w.call("add_primitive", kind="box", w=100, d=10, h=10)
        mesh = w.call("tessellate", handle=beam["handle"], deflection=0.5)
        V = np.asarray(mesh["vertices"])
        xmin, xmax = V[:, 0].min(), V[:, 0].max()
        # Hot at the fixed end (x=xmin), cold at the free tip (x=xmax).
        values = (xmax - V[:, 0]).tolist()

        png = r.render_fem_results(
            mesh["vertices"], mesh["triangles"], values,
            view="front", width=400, height=200,
            field_label="von Mises", units="MPa",
            colorbar=False, supersample=1,
        )
        arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.float64)
        mask = r.foreground_mask(png)
        # front view: world +x → screen +x, so the hot fixed end sits at LEFT.
        proxy = arr[..., 0] + arr[..., 1] - arr[..., 2]  # viridis heat proxy
        _h, wpx = mask.shape

        nb = 6
        means = []
        for i in range(nb):
            x0, x1 = int(wpx * i / nb), int(wpx * (i + 1) / nb)
            strip = mask[:, x0:x1]
            if strip.sum() < 20:
                continue
            means.append(proxy[:, x0:x1][strip].mean())
        assert len(means) >= 4, f"too few populated strips: {means}"
        assert means[0] > means[-1] + 40, (
            f"no hot→cold gradient along the beam axis: {means}"
        )
        diffs = np.diff(means)
        assert np.all(diffs < 5), (
            f"heat proxy not monotonically decreasing fixed-end→tip: {means}"
        )


def test_custom_camera_matches_iso_preset():
    """A custom (azimuth, elevation) camera at (45, 35.26) reproduces the 'iso'
    preset: cam dir (1,1,1)/√3 ⇒ azimuth 45°, elevation atan(1/√2)=35.26°. Their
    silhouettes must nearly coincide (IoU > 0.9), while a clearly different
    camera must NOT — proving the custom-camera path is honored, not ignored."""
    with Worker() as w:
        box = _make_box(w, dims=(20, 20, 20))
        mesh = w.call("tessellate", handle=box["handle"], deflection=0.5)

        def mask_for(view):
            png = r.render_mesh(
                mesh["vertices"], mesh["triangles"],
                width=256, height=256, view=view,
            )
            return r.foreground_mask(png)

        def iou(a, b):
            return (a & b).sum() / max((a | b).sum(), 1)

        m_iso = mask_for("iso")
        m_cam = mask_for((45.0, 35.264))
        iou_match = iou(m_iso, m_cam)
        assert iou_match > 0.9, (
            f"custom (45,35.26) camera should match iso preset; IoU={iou_match:.3f}"
        )

        m_off = mask_for((120.0, 10.0))
        iou_off = iou(m_iso, m_off)
        assert iou_off < iou_match, (
            f"an off-axis custom camera matched iso too well "
            f"(match={iou_match:.3f}, off={iou_off:.3f}) — view param not respected"
        )


def test_antialiasing_smooths_edges():
    """Supersampling + bilinear downsample softens hard bg→face steps at the
    silhouette. The 99.5th-percentile per-pixel luma gradient (dominated by the
    silhouette edge) must be LOWER with AA than without."""
    with Worker() as w:
        box = _make_box(w, dims=(20, 20, 20))
        mesh = w.call("tessellate", handle=box["handle"], deflection=0.5)

        def edge_step(ss):
            png = r.render_mesh(
                mesh["vertices"], mesh["triangles"],
                width=256, height=256, view="iso", supersample=ss, edges=False,
            )
            g = np.asarray(Image.open(io.BytesIO(png)).convert("L")).astype(np.float64)
            grad = np.concatenate([
                np.abs(np.diff(g, axis=1)).ravel(),
                np.abs(np.diff(g, axis=0)).ravel(),
            ])
            return np.percentile(grad, 99.5)

        no_aa = edge_step(1)
        aa = edge_step(4)
        assert aa < no_aa, (
            f"anti-aliasing did not soften edges: no-AA p99.5={no_aa:.1f}, AA={aa:.1f}"
        )


def test_feature_edges_kill_diagonal_forest():
    """Feature-edge-only inking: on a finely tessellated cylinder side, adjacent
    facet normals differ by less than the 20° threshold, so NO interior lines
    are drawn across the smooth curved wall — only the sharp cap rims and the
    silhouette survive. With the threshold at 0° (every edge inked, the old
    behavior) the body fills with a 'forest' of triangulation lines.

    Measured on the mid-body band (rows 30–70%, excluding the legitimate top/
    bottom rim circles): the feature-edge render must be essentially free of
    interior line pixels there, while the all-edges render is dense with them."""
    with Worker() as w:
        w.call("new_document", name="cyl")
        cyl = w.call("add_primitive", kind="cylinder", r=10, h=40)
        mesh = w.call("tessellate", handle=cyl["handle"], deflection=0.05)

        def render(fa):
            return r.render_mesh(
                mesh["vertices"], mesh["triangles"],
                width=256, height=256, view="front",
                feature_angle_deg=fa, supersample=1,
            )

        def midband_line_pixels(png):
            arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)
            d = np.abs(arr - np.array(r._LINE_COLOR)).max(axis=2)
            h = arr.shape[0]
            band = d[int(h * 0.3):int(h * 0.7)]
            return int((band < 30).sum())

        png_feat = render(20.0)
        png_forest = render(0.0)
        feat = midband_line_pixels(png_feat)
        forest = midband_line_pixels(png_forest)

        assert r.foreground_mask(png_feat).sum() > 1000, "feature render is blank"
        assert feat < 20, (
            f"feature-edge render still has a diagonal forest on the smooth side: "
            f"{feat} interior line px (expected ~0)"
        )
        assert forest > 5 * max(feat, 1) and forest > 100, (
            f"all-edges render should show the forest: feature-only={feat}px, "
            f"all-edges={forest}px"
        )


def _ccx_available():
    return any(
        shutil.which(b) for b in ("ccx", "ccx_2.19", "ccx_2.20", "ccx_2.21")
    )


def test_fem_field_surface_real_solve():
    """End-to-end on a REAL CalculiX solve: run the cantilever demo, pull its
    von-Mises surface field from the worker, and render it. Skips gracefully
    when ccx is absent. Checks the surface peak tracks the global max and the
    field render is a non-blank, color-varied PNG."""
    if not _ccx_available():
        print("    SKIP — ccx not available")
        return
    with Worker() as w:
        demo = w.call("fem_cantilever_demo", mesh_size=800, force=9000)
        surf = w.call("fem_field_surface", analysis=demo["analysis"], field="vonmises")
        assert surf["triangle_count"] > 0 and surf["node_count"] > 0
        # Peak stress is on the (fixed-end) surface, so the surface max tracks
        # the global max the volume result reports.
        assert 0.0 < surf["max"] <= demo["max_vonmises_mpa"] * 1.05, (
            f"surface max {surf['max']} vs global {demo['max_vonmises_mpa']}"
        )
        assert surf["displacements"] is not None

        png = r.render_fem_results(
            surf["vertices"], surf["triangles"], surf["values"],
            displacements=surf["displacements"], view="iso",
            field_label=surf["field"], units=surf["units"],
            width=480, height=360,
        )
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        assert arr.std() > 8, f"field render looks blank (stdev={arr.std():.1f})"


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

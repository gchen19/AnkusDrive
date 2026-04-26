"""
Host-side software renderer for DriftPin.

Worker tessellates the shape; this module rasterizes the triangle mesh in pure
NumPy + Pillow. Painter's algorithm with flat shading from a fixed light. No
GPU, no offscreen GL context, no FreeCADGui — works headless on macOS without
a display.

Output is PNG bytes, sized to the request, with the geometry auto-fit to the
frame in an orthographic projection. Background is light gray; foreground
shading is a Lambertian cosine of (face normal · light direction).

Limitations: orthographic only, painter's-algorithm sort can mis-occlude
extreme cases (interpenetrating triangles), no anti-aliasing. Good enough for
"can the agent see what it just built?" which is the only thing this is for.
"""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image, ImageDraw


_VIEWS = {
    # (camera direction looking at origin, up vector)
    "iso":   ((1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
    "top":   ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "bottom":((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "back":  ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "left":  ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "side":  ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),  # alias for "right"
}

# Bg chosen outside the renderer's shaded color range (which spans blue-grays
# 80-255 / 120-250 / 180-250). A warm off-white never collides with any face
# shade, so foreground_mask's color threshold always discriminates cleanly.
_BG_COLOR = (250, 245, 230)
_LINE_COLOR = (40, 40, 40)


def _camera_basis(view: str):
    if view not in _VIEWS:
        raise ValueError(f"unknown view {view!r}; valid: {sorted(_VIEWS)}")
    cam_dir, up = _VIEWS[view]
    cam_dir = np.array(cam_dir, dtype=np.float64)
    cam_dir /= np.linalg.norm(cam_dir)
    up = np.array(up, dtype=np.float64)
    # Camera frame: -z = view direction (toward origin), x = right, y = up.
    z_axis = cam_dir
    x_axis = np.cross(up, z_axis)
    if np.linalg.norm(x_axis) < 1e-8:
        # up || cam_dir — pick any perpendicular vector.
        x_axis = np.cross(np.array([0.0, 1.0, 0.0]), z_axis)
        if np.linalg.norm(x_axis) < 1e-8:
            x_axis = np.cross(np.array([1.0, 0.0, 0.0]), z_axis)
    x_axis /= np.linalg.norm(x_axis)
    y_axis = np.cross(z_axis, x_axis)
    return np.stack([x_axis, y_axis, z_axis], axis=0)  # (3, 3) world→camera


def render_mesh(
    vertices,
    triangles,
    width: int = 512,
    height: int = 512,
    view: str = "iso",
    margin: float = 0.05,
    light_dir=(0.4, 0.4, 0.8),
    edges: bool = True,
) -> bytes:
    """Rasterize a triangle mesh and return PNG bytes.

    Per-pixel z-buffer in NumPy: for each triangle, scan its bounding box,
    compute barycentric coords + interpolated depth at each pixel, write only
    where the new depth is closer than what's already there. Handles concave
    geometry (holes, pockets) correctly — painter's-algorithm cannot.

    vertices: list/array of [x, y, z] in mm.
    triangles: list/array of [i, j, k] indexing vertices.
    margin: fraction of frame left as padding around the auto-fit bbox.
    """
    V = np.asarray(vertices, dtype=np.float64)
    T = np.asarray(triangles, dtype=np.int64)
    if V.size == 0 or T.size == 0:
        return _blank_png(width, height)

    M = _camera_basis(view)
    cam = V @ M.T  # (N, 3) in camera coords; +z points TOWARD camera

    xs = cam[:, 0]
    ys = cam[:, 1]
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    span_x = xs.max() - xs.min()
    span_y = ys.max() - ys.min()
    span = max(span_x, span_y, 1e-6)
    avail = (1.0 - 2.0 * margin) * min(width, height)
    scale = avail / span

    # Project to pixel coords (origin top-left, y flipped). z preserved for depth.
    px = (xs - cx) * scale + width / 2.0
    py = -(ys - cy) * scale + height / 2.0
    pz = cam[:, 2]

    # Per-triangle face normal (world) for Lambertian shading.
    a = V[T[:, 0]]; b = V[T[:, 1]]; c = V[T[:, 2]]
    n = np.cross(b - a, c - a)
    n_norm = np.linalg.norm(n, axis=1, keepdims=True)
    n_norm[n_norm < 1e-12] = 1.0
    n = n / n_norm
    L = np.array(light_dir, dtype=np.float64)
    L = L / np.linalg.norm(L)
    shade_per_tri = np.abs(n @ L)
    shade_per_tri = 0.15 + 0.85 * shade_per_tri
    shade_per_tri = np.clip(shade_per_tri, 0.0, 1.0)

    # Depth buffer: +inf = empty; closer (larger z toward camera) wins.
    color = np.empty((height, width, 3), dtype=np.uint8)
    color[:] = _BG_COLOR
    zbuf = np.full((height, width), -np.inf, dtype=np.float64)
    edge_mask = np.zeros((height, width), dtype=bool)

    # Triangle vertex pixel coords + depths.
    p0x = px[T[:, 0]]; p0y = py[T[:, 0]]; p0z = pz[T[:, 0]]
    p1x = px[T[:, 1]]; p1y = py[T[:, 1]]; p1z = pz[T[:, 1]]
    p2x = px[T[:, 2]]; p2y = py[T[:, 2]]; p2z = pz[T[:, 2]]

    for ti in range(T.shape[0]):
        x0, y0, z0 = p0x[ti], p0y[ti], p0z[ti]
        x1, y1, z1 = p1x[ti], p1y[ti], p1z[ti]
        x2, y2, z2 = p2x[ti], p2y[ti], p2z[ti]

        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-9:
            continue  # degenerate / edge-on triangle

        x_min = max(int(np.floor(min(x0, x1, x2))), 0)
        x_max = min(int(np.ceil(max(x0, x1, x2))), width - 1)
        y_min = max(int(np.floor(min(y0, y1, y2))), 0)
        y_max = min(int(np.ceil(max(y0, y1, y2))), height - 1)
        if x_max < x_min or y_max < y_min:
            continue

        ys_grid, xs_grid = np.mgrid[y_min:y_max + 1, x_min:x_max + 1]
        xs_f = xs_grid + 0.5
        ys_f = ys_grid + 0.5

        w0 = ((y1 - y2) * (xs_f - x2) + (x2 - x1) * (ys_f - y2)) / denom
        w1 = ((y2 - y0) * (xs_f - x2) + (x0 - x2) * (ys_f - y2)) / denom
        w2 = 1.0 - w0 - w1

        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue

        z_interp = w0 * z0 + w1 * z1 + w2 * z2
        # Closer to camera = larger z in our basis.
        win = inside & (z_interp > zbuf[y_min:y_max + 1, x_min:x_max + 1])
        if not win.any():
            continue

        s = shade_per_tri[ti]
        col = (
            int(80 + 175 * s),
            int(120 + 130 * s),
            int(180 + 70 * s),
        )
        sub_color = color[y_min:y_max + 1, x_min:x_max + 1]
        sub_color[win] = col
        zbuf[y_min:y_max + 1, x_min:x_max + 1] = np.where(
            win, z_interp, zbuf[y_min:y_max + 1, x_min:x_max + 1]
        )

        if edges:
            # Mark a narrow band along each triangle edge (in barycentric space)
            # as edge pixels. Threshold scales with triangle size so very small
            # triangles don't get fully painted black.
            edge_band = 0.02
            on_edge = inside & (
                (np.abs(w0) < edge_band)
                | (np.abs(w1) < edge_band)
                | (np.abs(w2) < edge_band)
            )
            edge_mask[y_min:y_max + 1, x_min:x_max + 1] |= on_edge

    if edges:
        # Only draw edges where they survived the depth test (i.e. on visible
        # surfaces). Approximation: any pixel that's both edge-marked AND has
        # a finite depth gets the edge color.
        visible_edge = edge_mask & np.isfinite(zbuf) & (zbuf > -np.inf)
        color[visible_edge] = _LINE_COLOR

    img = Image.fromarray(color, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _blank_png(width, height) -> bytes:
    img = Image.new("RGB", (width, height), _BG_COLOR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def foreground_mask(png_bytes: bytes, bg=_BG_COLOR, tol: int = 8):
    """Decode PNG and return a boolean mask of foreground pixels (anything
    not within `tol` of background color in any channel). Useful for tests."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    arr = np.asarray(img, dtype=np.int16)
    diff = np.abs(arr - np.array(bg, dtype=np.int16))
    return (diff.max(axis=2) > tol)


def silhouette_bbox(mask) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of True pixels in mask, or None if empty."""
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())

"""
Host-side software renderer for AnkusDrive.

Worker tessellates the shape; this module rasterizes the triangle mesh in pure
NumPy + Pillow. Per-pixel z-buffer (not painter's algorithm), flat shading from
a fixed light, or — for FEM field visualization — a per-vertex viridis colormap
barycentrically interpolated across each triangle. No GPU, no offscreen GL
context, no FreeCADGui — works headless on macOS without a display.

Output is PNG bytes, sized to the request, with the geometry auto-fit to the
frame in an orthographic projection. Background is a warm off-white.

Cameras: a named preset ("iso"/"top"/"front"/…) OR a custom `(azimuth_deg,
elevation_deg)` pair accepted anywhere a preset is.

Feature edges: an edge is inked only where its two adjacent triangle normals
differ by more than `feature_angle_deg` (default 20°), or where it is a mesh
boundary. That kills the triangulation-diagonal "forest" on curved faces while
keeping real feature lines (box corners, hole rims, silhouettes).

Anti-aliasing: `supersample=N` rasterizes at N× and bilinear-downsamples in PIL.
"""
from __future__ import annotations

import io
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont


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

# Viridis colormap anchors (matplotlib, sampled at 10 stops). Perceptually
# uniform, colorblind-safe: dark blue-purple (low) → teal → yellow (high).
_VIRIDIS = np.array(
    [
        (68, 1, 84),
        (72, 40, 120),
        (62, 74, 137),
        (49, 104, 142),
        (38, 130, 142),
        (31, 158, 137),
        (53, 183, 121),
        (110, 206, 88),
        (181, 222, 43),
        (253, 231, 37),
    ],
    dtype=np.float64,
)


def viridis(t):
    """Map t in [0, 1] (scalar or ndarray) to an RGB float array via viridis.

    Returns shape (..., 3) floats in [0, 255]. Linear interpolation between the
    10 anchor stops — a faithful "viridis-style" ramp without a matplotlib dep.
    """
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    x = t * (len(_VIRIDIS) - 1)
    i0 = np.floor(x).astype(np.int64)
    i1 = np.minimum(i0 + 1, len(_VIRIDIS) - 1)
    f = (x - i0)[..., None]
    return _VIRIDIS[i0] * (1.0 - f) + _VIRIDIS[i1] * f


def values_to_colors(values, vmin=None, vmax=None):
    """Per-vertex scalar field → per-vertex uint8 RGB via viridis, normalized to
    [vmin, vmax] (defaults to the data range). Returns (colors, vmin, vmax)."""
    v = np.asarray(values, dtype=np.float64)
    if vmin is None:
        vmin = float(np.nanmin(v)) if v.size else 0.0
    if vmax is None:
        vmax = float(np.nanmax(v)) if v.size else 1.0
    span = vmax - vmin if vmax > vmin else 1.0
    t = (v - vmin) / span
    return viridis(t).astype(np.uint8), vmin, vmax


def _camera_basis(view):
    """World→camera rotation (3, 3) for a preset name OR a custom camera.

    `view` may be one of the preset strings in _VIEWS, or a `(azimuth_deg,
    elevation_deg)` pair: azimuth rotates about +Z (0° looks from +X toward the
    origin), elevation tilts above the XY plane. `(45, 35.26)` reproduces "iso".
    """
    if isinstance(view, str):
        if view not in _VIEWS:
            raise ValueError(
                f"unknown view {view!r}; valid: {sorted(_VIEWS)} "
                "or a (azimuth_deg, elevation_deg) pair"
            )
        cam_dir, up = _VIEWS[view]
        cam_dir = np.array(cam_dir, dtype=np.float64)
        up = np.array(up, dtype=np.float64)
    else:
        try:
            az_deg, el_deg = float(view[0]), float(view[1])
        except (TypeError, IndexError, ValueError) as e:
            raise ValueError(
                f"view must be a preset name or (azimuth_deg, elevation_deg); got {view!r}"
            ) from e
        az, el = np.radians(az_deg), np.radians(el_deg)
        cam_dir = np.array(
            [np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)],
            dtype=np.float64,
        )
        up = np.array([0.0, 0.0, 1.0], dtype=np.float64)

    cam_dir = cam_dir / np.linalg.norm(cam_dir)
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


def _feature_edges(T, n, feature_angle_deg):
    """Per-triangle boolean mask (M, 3) of which of a triangle's 3 edges should
    be inked. Edge k of triangle (v0,v1,v2) is edge (v_k, v_{k+1}); it is a
    feature iff it is a mesh boundary/non-manifold edge OR the two adjacent
    triangle face normals differ by more than `feature_angle_deg`.

    Edge order is [(v0,v1), (v1,v2), (v2,v0)], matching barycentric coords
    [w2≈0, w0≈0, w1≈0] respectively (the edge opposite the third vertex)."""
    cos_thresh = np.cos(np.radians(feature_angle_deg))
    edge_tris = defaultdict(list)
    for ti in range(T.shape[0]):
        a, b, c = int(T[ti, 0]), int(T[ti, 1]), int(T[ti, 2])
        for (u, v) in ((a, b), (b, c), (c, a)):
            edge_tris[(u, v) if u < v else (v, u)].append(ti)

    def is_feature(key):
        tl = edge_tris[key]
        if len(tl) != 2:
            return True  # boundary or non-manifold → always a real edge
        d = float(np.dot(n[tl[0]], n[tl[1]]))
        return d < cos_thresh  # normals diverge by > threshold

    mask = np.zeros((T.shape[0], 3), dtype=bool)
    for ti in range(T.shape[0]):
        a, b, c = int(T[ti, 0]), int(T[ti, 1]), int(T[ti, 2])
        mask[ti, 0] = is_feature((a, b) if a < b else (b, a))
        mask[ti, 1] = is_feature((b, c) if b < c else (c, b))
        mask[ti, 2] = is_feature((c, a) if c < a else (a, c))
    return mask


def _rasterize(
    V, T, width, height, view, margin, light_dir, edges,
    vertex_colors, feature_angle_deg, shade_strength,
) -> np.ndarray:
    """Core z-buffered rasterizer. Returns an (H, W, 3) uint8 color array.

    When `vertex_colors` (N, 3) is given, each triangle's fill is the
    barycentric interpolation of its three vertex colors (times a mild shading
    factor for form); otherwise the flat blue-gray Lambertian shade is used.
    """
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

    px = (xs - cx) * scale + width / 2.0
    py = -(ys - cy) * scale + height / 2.0
    pz = cam[:, 2]

    # Per-triangle face normal (world) for Lambertian shading + feature edges.
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

    color = np.empty((height, width, 3), dtype=np.uint8)
    color[:] = _BG_COLOR
    zbuf = np.full((height, width), -np.inf, dtype=np.float64)
    edge_mask = np.zeros((height, width), dtype=bool)

    feat = _feature_edges(T, n, feature_angle_deg) if edges else None
    VC = None if vertex_colors is None else np.asarray(vertex_colors, dtype=np.float64)

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
        win = inside & (z_interp > zbuf[y_min:y_max + 1, x_min:x_max + 1])
        if not win.any():
            continue

        s = shade_per_tri[ti]
        sub_color = color[y_min:y_max + 1, x_min:x_max + 1]
        if VC is not None:
            # Barycentric interpolation of the three vertex colors, with a mild
            # shading factor so the field stays vivid but form is legible.
            fac = (1.0 - shade_strength) + shade_strength * s
            c0 = VC[T[ti, 0]]; c1 = VC[T[ti, 1]]; c2 = VC[T[ti, 2]]
            rgb = (
                w0[..., None] * c0 + w1[..., None] * c1 + w2[..., None] * c2
            ) * fac
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            sub_color[win] = rgb[win]
        else:
            col = (
                int(80 + 175 * s),
                int(120 + 130 * s),
                int(180 + 70 * s),
            )
            sub_color[win] = col

        zbuf[y_min:y_max + 1, x_min:x_max + 1] = np.where(
            win, z_interp, zbuf[y_min:y_max + 1, x_min:x_max + 1]
        )

        if edges:
            # Mark a narrow band along each FEATURE edge (barycentric space).
            # Edge (v0,v1) is w2≈0; (v1,v2) is w0≈0; (v2,v0) is w1≈0.
            edge_band = 0.02
            on_edge = np.zeros_like(inside)
            if feat[ti, 0]:
                on_edge |= np.abs(w2) < edge_band
            if feat[ti, 1]:
                on_edge |= np.abs(w0) < edge_band
            if feat[ti, 2]:
                on_edge |= np.abs(w1) < edge_band
            on_edge &= inside
            edge_mask[y_min:y_max + 1, x_min:x_max + 1] |= on_edge

    if edges:
        visible_edge = edge_mask & np.isfinite(zbuf) & (zbuf > -np.inf)
        color[visible_edge] = _LINE_COLOR

    return color


def render_mesh(
    vertices,
    triangles,
    width: int = 512,
    height: int = 512,
    view="iso",
    margin: float = 0.05,
    light_dir=(0.4, 0.4, 0.8),
    edges: bool = True,
    vertex_colors=None,
    deform=None,
    deformation_scale: float = 0.0,
    supersample: int = 1,
    feature_angle_deg: float = 20.0,
    shade_strength: float = 0.55,
) -> bytes:
    """Rasterize a triangle mesh and return PNG bytes.

    vertices: list/array of [x, y, z] in mm.
    triangles: list/array of [i, j, k] indexing vertices.
    view: preset name or (azimuth_deg, elevation_deg).
    vertex_colors: optional (N, 3) per-vertex RGB, barycentrically interpolated.
    deform: optional (N, 3) per-vertex displacement; applied as
        vertices + deformation_scale * deform (deformed-shape overlay).
    supersample: render at N× then bilinear-downsample (anti-aliasing).
    feature_angle_deg: only ink edges whose adjacent normals diverge by more.
    """
    V = np.asarray(vertices, dtype=np.float64)
    T = np.asarray(triangles, dtype=np.int64)
    if V.size == 0 or T.size == 0:
        return _blank_png(width, height)

    if deform is not None and deformation_scale:
        V = V + deformation_scale * np.asarray(deform, dtype=np.float64)

    ss = max(1, int(supersample))
    color = _rasterize(
        V, T, width * ss, height * ss, view, margin, light_dir, edges,
        vertex_colors, feature_angle_deg, shade_strength,
    )
    img = Image.fromarray(color, mode="RGB")
    if ss > 1:
        img = img.resize((width, height), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_fem_results(
    vertices,
    triangles,
    values,
    displacements=None,
    view="iso",
    deformation_scale="auto",
    width: int = 512,
    height: int = 512,
    field_label: str = "field",
    units: str = "",
    vmin: float | None = None,
    vmax: float | None = None,
    supersample: int = 2,
    colorbar: bool = True,
    edges: bool = True,
) -> bytes:
    """Render an FEM surface mesh colored by a per-vertex scalar field.

    vertices/triangles: the FEM result's boundary surface (from the worker's
        `fem_field_surface`). values: per-vertex scalar (von Mises MPa /
        displacement mm / temperature °C). displacements: optional per-vertex
        [dx,dy,dz] for a deformed-shape overlay.
    deformation_scale: "auto" scales peak displacement to ~8% of the model
        diagonal; a number is used verbatim; 0 / None disables the overlay.

    Anti-aliased (supersample≥2), viridis colormap, with a colorbar strip
    showing min/max. Returns PNG bytes.
    """
    V = np.asarray(vertices, dtype=np.float64)
    if V.size == 0 or len(triangles) == 0:
        return _blank_png(width, height)

    colors, vlo, vhi = values_to_colors(values, vmin=vmin, vmax=vmax)

    deform = None
    scale = 0.0
    if displacements is not None and len(displacements):
        D = np.asarray(displacements, dtype=np.float64)
        if isinstance(deformation_scale, str) and deformation_scale == "auto":
            diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
            maxd = float(np.linalg.norm(D, axis=1).max()) if D.size else 0.0
            scale = (0.08 * diag / maxd) if maxd > 1e-12 else 0.0
        elif deformation_scale:
            scale = float(deformation_scale)
        deform = D

    cbar_w = 72 if colorbar else 0
    mesh_w = max(width - cbar_w, 16)
    png = render_mesh(
        V.tolist(), triangles,
        width=mesh_w, height=height, view=view,
        vertex_colors=colors, deform=deform, deformation_scale=scale,
        supersample=supersample, edges=edges,
    )
    mesh_img = Image.open(io.BytesIO(png)).convert("RGB")

    if not colorbar:
        buf = io.BytesIO()
        mesh_img.save(buf, format="PNG")
        return buf.getvalue()

    canvas = Image.new("RGB", (width, height), _BG_COLOR)
    canvas.paste(mesh_img, (0, 0))
    _draw_colorbar(canvas, width, height, vlo, vhi, field_label, units)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _draw_colorbar(canvas, width, height, vmin, vmax, label, units):
    """Draw a vertical viridis colorbar (max at top) with min/max labels into
    the right margin of `canvas`."""
    bar_w = 16
    bar_h = int(height * 0.6)
    bar_x = width - 58
    bar_y = int(height * 0.2)

    ts = np.linspace(1.0, 0.0, bar_h)  # top row = max value
    cols = viridis(ts).astype(np.uint8)  # (bar_h, 3)
    grad = np.repeat(cols[:, None, :], bar_w, axis=1)
    canvas.paste(Image.fromarray(grad, mode="RGB"), (bar_x, bar_y))

    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        [bar_x, bar_y, bar_x + bar_w - 1, bar_y + bar_h - 1], outline=_LINE_COLOR
    )
    try:
        font = ImageFont.load_default()
    except Exception:  # noqa: BLE001 - font is best-effort
        font = None
    unit_sfx = f" {units}" if units else ""
    draw.text((bar_x, bar_y - 26), str(label), fill=_LINE_COLOR, font=font)
    draw.text((bar_x, bar_y - 13), f"{vmax:.3g}{unit_sfx}", fill=_LINE_COLOR, font=font)
    draw.text((bar_x, bar_y + bar_h + 2), f"{vmin:.3g}{unit_sfx}", fill=_LINE_COLOR, font=font)


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

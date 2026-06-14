"""Reusable simulation-video pipeline (kickoff: docs/KICKOFF_simulation_video_capture.md,
item A). Render a multi-part assembly frame-by-frame from the REAL meshes and encode a
recording for human review.

Throughline (from §11.10): a review video must show the *real artifact* — the exported
STL — never an idealised sketch. So this consumes binary STL the merge produced and
shades each facet by its own normal (same shading as scratch/render_gearbox.py), with a
FIXED camera box so parts can move without the frame jittering.

Encoding: GIF via Pillow, which works today with no external binary. MP4 needs ffmpeg
(absent here) — `can_encode_mp4()` is the capability check callers use to degrade to GIF
rather than hard-fail (the repo's solver-family degradation pattern).

  import sys; sys.path.insert(0, "scratch"); import sim_video as sv
"""
import io
import shutil
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                           # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection   # noqa: E402
from PIL import Image                                     # noqa: E402


def read_binary_stl(path):
    """(verts (n,3,3), normals (n,3)) from a binary STL — same parser as render_gearbox."""
    raw = np.fromfile(str(path), dtype=np.uint8)
    n = int(np.frombuffer(raw[80:84].tobytes(), dtype=np.uint32)[0])
    rec = raw[84:84 + n * 50].reshape(n, 50)
    f = rec[:, :48].copy().view(np.float32).reshape(n, 12)
    return f[:, 3:12].reshape(n, 3, 3), f[:, 0:3]


def _shade(normals, color, light=(0.35, -0.5, 0.78)):
    nrm = normals / (np.linalg.norm(normals, axis=1, keepdims=True) + 1e-9)
    lt = np.array(light, float); lt /= np.linalg.norm(lt)
    s = np.clip(nrm @ lt, 0.0, 1.0) * 0.78 + 0.22         # ambient + diffuse
    return np.clip(s[:, None] * np.array(color, float), 0, 1)


# a few named part colours (steel, gold, green for "engaged/lit", red for "jam")
STEEL = (0.62, 0.66, 0.74)
GOLD = (0.82, 0.60, 0.22)
GREEN = (0.36, 0.78, 0.46)
RED = (0.85, 0.34, 0.30)


def bounds_of(vert_arrays):
    """Cube (centre, radius) enclosing every (n,3,3) array — a FIXED camera box. Pass
    the parts at their EXTREME poses so nothing leaves frame during the motion."""
    allv = np.concatenate([v.reshape(-1, 3) for v in vert_arrays], axis=0)
    lo, hi = allv.min(0), allv.max(0)
    return (lo + hi) / 2.0, float((hi - lo).max()) / 2.0 * 1.05


def frame(parts, ctr, rad, *, title="", subtitle="", elev=22, azim=-58,
          figsize=(8, 7), dpi=110):
    """Render ONE frame. ``parts`` = list of dicts {verts, normals, color, alpha?}.
    Returns a PIL.Image. ``ctr``/``rad`` come from bounds_of so the camera is stable.

    All parts go into ONE Poly3DCollection (per-face RGBA), so matplotlib depth-sorts
    every facet together — a per-part collection would draw in list order and a far part
    could paint over a near one."""
    verts, rgba = [], []
    for p in parts:
        cols = _shade(p["normals"], p["color"])
        a = np.full((cols.shape[0], 1), p.get("alpha", 1.0))
        verts.append(p["verts"]); rgba.append(np.concatenate([cols, a], axis=1))
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(
        np.concatenate(verts, axis=0), facecolors=np.concatenate(rgba, axis=0),
        linewidths=0))
    ax.set_xlim(ctr[0] - rad, ctr[0] + rad)
    ax.set_ylim(ctr[1] - rad, ctr[1] + rad)
    ax.set_zlim(ctr[2] - rad, ctr[2] + rad)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=elev, azim=azim)
    ax.set_axis_off()
    if title:
        ax.set_title(title, fontsize=12)
    if subtitle:
        fig.text(0.5, 0.045, subtitle, ha="center", fontsize=9.5, color="#333")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def can_encode_mp4():
    """True iff an ffmpeg binary is on PATH (the capability check for an MP4 path)."""
    return shutil.which("ffmpeg") is not None


def encode_gif(images, out_path, *, fps=12, hold_last=0):
    """Encode PIL frames to a looping GIF (always available via Pillow). ``hold_last``
    repeats the final frame that many times so the end pose lingers."""
    out_path = Path(out_path).with_suffix(".gif")
    frames = list(images) + [images[-1]] * hold_last
    frames[0].save(str(out_path), save_all=True, append_images=frames[1:],
                   duration=int(1000 / fps), loop=0)
    return out_path


def filmstrip(images, out_path, picks=None):
    """Horizontal montage of selected frames (default: all) as one PNG."""
    sel = [images[i] for i in (picks if picks is not None else range(len(images)))]
    w, h = sel[0].size
    strip = Image.new("RGB", (w * len(sel), h), "white")
    for j, im in enumerate(sel):
        strip.paste(im, (j * w, 0))
    out_path = Path(out_path)
    strip.save(str(out_path))
    return out_path

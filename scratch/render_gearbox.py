"""
Render the assembled gearbox from its exported STL — a shaded 3D image using only
matplotlib + numpy (no external renderer / addon). Reads the binary STL the merge
produced, shades each facet by its normal against a light, and saves a PNG.

  .venv/bin/python3 scratch/render_gearbox.py [n_speeds ...]   (default: 3 6)
"""
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "results" / "gearbox_real"   # generated STL input (gitignored)
ART = REPO / "artifacts" / "gearbox"                  # committed PNG renders


def read_binary_stl(path):
    raw = np.fromfile(path, dtype=np.uint8)
    n = int(np.frombuffer(raw[80:84].tobytes(), dtype=np.uint32)[0])
    rec = raw[84:84 + n * 50].reshape(n, 50)
    f = rec[:, :48].copy().view(np.float32).reshape(n, 12)
    normals = f[:, 0:3]
    verts = f[:, 3:12].reshape(n, 3, 3)
    return verts, normals


def render(path, png, title):
    verts, normals = read_binary_stl(path)
    nrm = normals / (np.linalg.norm(normals, axis=1, keepdims=True) + 1e-9)
    light = np.array([0.35, -0.5, 0.78])
    light /= np.linalg.norm(light)
    shade = np.clip(nrm @ light, 0.0, 1.0) * 0.78 + 0.22   # ambient + diffuse
    steel = np.array([0.62, 0.66, 0.74])
    colors = np.clip(shade[:, None] * steel, 0, 1)

    fig = plt.figure(figsize=(10, 8), dpi=130)
    ax = fig.add_subplot(111, projection="3d")
    pc = Poly3DCollection(verts, facecolors=colors, linewidths=0)
    ax.add_collection3d(pc)

    allv = verts.reshape(-1, 3)
    lo, hi = allv.min(0), allv.max(0)
    ctr = (lo + hi) / 2
    rad = (hi - lo).max() / 2 * 1.05
    ax.set_xlim(ctr[0] - rad, ctr[0] + rad)
    ax.set_ylim(ctr[1] - rad, ctr[1] + rad)
    ax.set_zlim(ctr[2] - rad, ctr[2] + rad)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=22, azim=-58)
    ax.set_axis_off()
    ax.set_title(title, fontsize=13)
    fig.tight_layout()
    fig.savefig(png, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return len(verts), png.stat().st_size


def main():
    sizes = [int(a) for a in sys.argv[1:]] or [3, 6]
    for n in sizes:
        stl = OUT / f"gearbox{n}.stl"
        if not stl.exists():
            print(f"  (no {stl} — run scratch/gearbox_real.py {n} first)")
            continue
        ART.mkdir(parents=True, exist_ok=True)
        png = ART / f"gearbox{n}_render.png"
        tris, size = render(stl, png, f"{n}-speed constant-mesh gearbox "
                                       f"({'14' if n == 3 else '20'} parts)")
        print(f"  rendered {stl.name}: {tris:,} facets -> {png}  ({size:,} bytes)")


if __name__ == "__main__":
    main()

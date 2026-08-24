"""Ball lens — a deliberately hard 3D non-sequential example, and a lesson in why.

A solid glass sphere is the simplest "lens" to model and the worst to use. This script
traces a collimated bundle through a meshed BK7 ball lens (via the production GPL
subprocess runner) and renders two figures into examples/optics_gallery/:

  07_ball_lens_caustic_3d.png   — the rays do NOT meet at a point; they fold into a
                                   caustic. That smear IS spherical aberration.
  08_ball_lens_mesh_convergence.png — focus spread vs mesh facet count: the faceting
                                   artifact shrinks as the sphere is tessellated finer,
                                   converging onto the physical spherical-aberration floor.

Why a ball lens is hard (see README in this dir for the full write-up):
  * Severe spherical aberration — one strong radius on both faces, so marginal rays
    over-bend and cross the axis well short of the paraxial focus. There is no single
    focus, only a caustic; the usable aperture is a small fraction of the diameter.
  * Very short back focal distance — BFD = R(2-n)/(2(n-1)) from the rear vertex
    (4.675 mm for BK7 R=10). For n->2 the focus sits on the back surface (why high-index
    ball lenses are used for fiber coupling).
  * EFL = nR/(2(n-1)) measured from the sphere center (14.67 mm here).
  * Meshing trap — a triangulated sphere is only an approximation of the smooth surface.
    A naive lat-long (UV) sphere degenerates into slivers AT THE POLES, i.e. exactly on
    the optical axis where the most important rays go; an icosphere keeps facets uniform.
    Either way, coarse facets scatter rays (facet noise) that can swamp the real optics —
    so meshed spheres are for illustration, analytic surfaces for precision.

Run:  .venv/bin/python examples/optics_ball_lens.py
"""
import json
import math
import os
import subprocess
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optics_gallery")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "ankusdrive", "optics_gpl_runner.py")
os.makedirs(OUT, exist_ok=True)

R, N_GLASS = 10.0, 1.5168
PARAXIAL_BFD = R * (2 - N_GLASS) / (2 * (N_GLASS - 1))      # 4.675 mm from rear vertex


def icosphere(radius=10.0, subdiv=4):
    """Uniform-triangle sphere (subdivided icosahedron) — no pole slivers. Returns
    (vertices[np.array], faces[(i,j,k)])."""
    t = (1 + math.sqrt(5)) / 2
    V = [(-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0), (0, -1, t), (0, 1, t),
         (0, -1, -t), (0, 1, -t), (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1)]
    V = [np.array(v) / np.linalg.norm(v) for v in V]
    F = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4),
         (11, 10, 2), (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8),
         (3, 8, 9), (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    cache = {}

    def mid(a, b):
        key = tuple(sorted((a, b)))
        if key not in cache:
            m = V[a] + V[b]
            V.append(m / np.linalg.norm(m))
            cache[key] = len(V) - 1
        return cache[key]
    for _ in range(subdiv):
        F2 = []
        for a, b, c in F:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            F2 += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        F = F2
    return [v * radius for v in V], F


def _write_stl(path, verts, faces):
    with open(path, "w") as f:
        f.write("solid ball\n")
        for a, b, c in faces:
            p, q, r = verts[a], verts[b], verts[c]
            n = np.cross(q - p, r - p)
            m = np.linalg.norm(n)
            n = n / m if m else n
            f.write(f"facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n outer loop\n")
            for Vt in (p, q, r):
                f.write(f"  vertex {Vt[0]:.6e} {Vt[1]:.6e} {Vt[2]:.6e}\n")
            f.write(" endloop\nendfacet\n")
        f.write("endsolid ball\n")


def _trace(verts, faces, heights, azimuths=(0.0,)):
    """Trace a collimated +Z bundle at the given radial heights / azimuths through the
    meshed ball via the production subprocess runner. Returns the JSON result."""
    stl = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    stl.close()
    _write_stl(stl.name, verts, faces)
    rays = [{"origin": [h * math.sin(a), h * math.cos(a), -25.0], "dir": [0, 0, 1.0]}
            for h in heights for a in azimuths]
    problem = {"problem": "solid_trace", "stl_path": stl.name, "n_refractive": N_GLASS,
               "wavelength_um": 0.5876, "solid": {"diameter": 40, "thickness": 30, "axis_move": 1},
               "rays": rays, "want_paths": True}
    proc = subprocess.run([sys.executable, RUNNER], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=180)
    os.unlink(stl.name)
    return json.loads(proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0])


def _axis_crossings(paths):
    """Z where each exit ray crosses the optical axis (y->0), minus the rear vertex z=R."""
    out = []
    for poly in paths or []:
        a = np.asarray(poly)
        d = a[-1] - a[-2]
        if abs(d[1]) > 1e-9:
            tcr = -a[-2][1] / d[1]
            out.append(a[-2][2] + tcr * d[2] - R)
    return np.array(out)


def fig_caustic_3d():
    verts, faces = icosphere(R, subdiv=4)
    heights = np.linspace(-7, 7, 23)
    res = _trace(verts, faces, heights, azimuths=(0.0,))             # a vertical fan
    fig = plt.figure(figsize=(11, 6))
    ax = fig.add_subplot(111, projection="3d")
    tv = [[(verts[i][2], verts[i][0], verts[i][1]) for i in f] for f in faces]
    ax.add_collection3d(Poly3DCollection(tv, facecolor="#bfe3ff", alpha=0.18,
                                         edgecolor="none"))
    for poly in (res.get("paths") or []):
        a = np.asarray(poly)
        ax.plot(a[:, 2], a[:, 0], a[:, 1], color="#d9480f", lw=0.7, alpha=0.7)
    ax.plot([R + PARAXIAL_BFD], [0], [0], "k*", ms=11)
    ax.text(R + PARAXIAL_BFD, 0, 1.6, "paraxial focus\n(BFD %.2f mm)" % PARAXIAL_BFD, fontsize=8)
    ax.set_xlabel("Z (mm)"); ax.set_ylabel("X (mm)"); ax.set_zlabel("Y (mm)")
    ax.set_title("KrakenOS (subprocess) · BK7 ball lens (R=10, %d facets)\n"
                 "rays fold into a CAUSTIC — spherical aberration, no single focus"
                 % len(faces))
    ax.view_init(elev=18, azim=-74)
    ax.set_box_aspect((2.4, 1.2, 1.2))
    p = os.path.join(OUT, "07_ball_lens_caustic_3d.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p


def fig_mesh_convergence():
    heights = np.linspace(0.5, 7.0, 14)
    facets, spreads, near_axis = [], [], []
    for subdiv in (1, 2, 3, 4, 5):
        verts, faces = icosphere(R, subdiv)
        res = _trace(verts, faces, heights, azimuths=(0.0,))
        cr = _axis_crossings(res.get("paths"))
        facets.append(len(faces))
        spreads.append(float(cr.max() - cr.min()))
        near_axis.append(float(cr[:4].std()))                       # scatter of the 4 innermost rays
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(facets, spreads, "o-", color="#d9480f", label="total focus spread (SA + facet noise)")
    ax.plot(facets, near_axis, "s--", color="#3a7bd5",
            label="near-axis scatter (facet noise only)")
    ax.set_xscale("log")
    ax.set_xlabel("sphere mesh facet count")
    ax.set_ylabel("focal-region spread (mm)")
    ax.set_title("Ball lens meshing: facet noise converges away,\n"
                 "physical spherical aberration remains")
    ax.grid(alpha=0.3, which="both")
    ax.legend()
    ax.annotate("near-axis scatter → 0\n(mesh artifact, fixable by refining)",
                xy=(facets[-1], near_axis[-1]), xytext=(facets[1], near_axis[0] * 0.7),
                fontsize=8, color="#3a7bd5",
                arrowprops=dict(arrowstyle="->", color="#3a7bd5"))
    p = os.path.join(OUT, "08_ball_lens_mesh_convergence.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p, list(zip(facets, [round(s, 2) for s in spreads], [round(n, 3) for n in near_axis]))


if __name__ == "__main__":
    print("caustic 3D :", fig_caustic_3d())
    p, table = fig_mesh_convergence()
    print("convergence:", p)
    print("  facets  spread(mm)  near-axis-scatter(mm)")
    for fac, spr, na in table:
        print(f"  {fac:6d}   {spr:8.2f}    {na:.3f}")
    print("\nparaxial BFD oracle: %.3f mm   EFL: %.3f mm   (from rear vertex / sphere center)"
          % (PARAXIAL_BFD, N_GLASS * R / (2 * (N_GLASS - 1))))

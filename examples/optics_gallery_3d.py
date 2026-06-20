"""3D optics examples — both lanes rendered to PNGs for viewing.

Headless (matplotlib mplot3d, no GPU/VTK display needed). Writes
examples/optics_gallery/05_lens_3d.png and 06_prism_tir_3d.png, built from REAL
traced data: optiland's per-surface global ray coordinates, and KrakenOS's per-ray
polylines (S.XYZ) returned by the production GPL subprocess runner.

Run:  .venv/bin/python examples/optics_gallery_3d.py
"""
import json
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
os.makedirs(OUT, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fig_lens_3d():
    """optiland singlet with a real 3D ray cone (hexapolar pupil) converging to focus."""
    from optiland.optic import Optic
    R1, R2, zv1, zv2, epd = 50.0, -50.0, 0.0, 4.0, 20.0
    L = Optic()
    L.add_surface(index=0, thickness=np.inf)
    L.add_surface(index=1, radius=R1, thickness=zv2, material="N-BK7", is_stop=True)
    L.add_surface(index=2, radius=R2, thickness=45.0, material="air")
    L.add_surface(index=3)
    L.set_aperture(aperture_type="EPD", value=epd)
    L.set_field_type(field_type="angle"); L.add_field(y=0.0)
    L.add_wavelength(value=0.5876, is_primary=True)
    L.image_solve()
    L.trace(Hx=0, Hy=0, wavelength=0.5876, num_rays=4, distribution="hexapolar")
    sg = L.surface_group
    X, Y, Z = np.asarray(sg.x), np.asarray(sg.y), np.asarray(sg.z)  # (n_surf, n_rays)

    fig = plt.figure(figsize=(11, 6))
    ax = fig.add_subplot(111, projection="3d")
    # the lens element: front/back spherical caps as translucent surfaces
    rr = np.linspace(0, epd / 2, 14)
    th = np.linspace(0, 2 * np.pi, 40)
    Rg, Tg = np.meshgrid(rr, th)
    xc, yc = Rg * np.cos(Tg), Rg * np.sin(Tg)
    front = zv1 + (R1 - np.sqrt(R1 ** 2 - Rg ** 2))
    back = zv2 - (abs(R2) - np.sqrt(R2 ** 2 - Rg ** 2))
    for zc in (front, back):
        ax.plot_surface(zc, xc, yc, color="#7fb3ff", alpha=0.22, linewidth=0, shade=False)
    # the ray cone: one polyline per ray, object plane clipped to z=-20
    for k in range(X.shape[1]):
        z = Z[:, k].copy(); z[0] = -20.0
        ax.plot(z, X[:, k], Y[:, k], color="#d9480f", lw=0.7, alpha=0.55)
    fz = float(L.paraxial.f2()) - 0.0
    ax.scatter([Z[-1, 0]], [0], [0], color="k", s=18)
    ax.text(Z[-1, 0], 0, 2.5, "focus", fontsize=9)
    ax.set_xlabel("Z (mm)"); ax.set_ylabel("X (mm)"); ax.set_zlabel("Y (mm)")
    ax.set_title("optiland · N-BK7 singlet — 3D ray cone to focus (f≈%.1f mm)" % fz)
    ax.view_init(elev=22, azim=-60)
    ax.set_box_aspect((3.0, 1.0, 1.0))
    p = os.path.join(OUT, "05_lens_3d.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p


def _prism_stl(path, L=20.0, W=20.0):
    A, B, C = (0.0, 0.0), (L, 0.0), (0.0, L)
    x0, x1 = -W / 2, W / 2
    V = []

    def v(x, y, z):
        V.append((x, y, z)); return len(V) - 1
    a0, b0, c0 = v(x0, *A), v(x0, *B), v(x0, *C)
    a1, b1, c1 = v(x1, *A), v(x1, *B), v(x1, *C)
    tris = [(a0, c0, b0), (a1, b1, c1), (a0, c1, c0), (a0, a1, c1),
            (a0, b0, b1), (a0, b1, a1), (b0, c0, c1), (b0, c1, b1)]

    def nrm(p, q, r):
        n = np.cross(np.array(q) - np.array(p), np.array(r) - np.array(p))
        m = np.linalg.norm(n)
        return n / m if m else n
    with open(path, "w") as f:
        f.write("solid prism\n")
        for i, j, k in tris:
            p, q, r = V[i], V[j], V[k]
            n = nrm(p, q, r)
            f.write(f"facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n outer loop\n")
            for P in (p, q, r):
                f.write(f"  vertex {P[0]:.6e} {P[1]:.6e} {P[2]:.6e}\n")
            f.write(" endloop\nendfacet\n")
        f.write("endsolid prism\n")
    return np.array(V), tris


def fig_prism_3d():
    """KrakenOS 45° prism (real STL) with a genuinely 3D bundle (rays spread in X and Y)
    that TIRs at the hypotenuse and folds 90°, traced via the production subprocess runner."""
    stl = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    stl.close()
    V, tris = _prism_stl(stl.name)
    runner = os.path.join(ROOT, "driftpin", "optics_gpl_runner.py")
    rays = [{"origin": [dx, 6.0 + dy, -5.0], "dir": [0, 0, 1.0]}
            for dx in (-6, -2, 2, 6) for dy in (-2, 2, 5)]
    problem = {"problem": "solid_trace", "stl_path": stl.name, "glass": "BK7",
               "wavelength_um": 0.55, "solid": {"diameter": 40, "thickness": 30, "axis_move": 1},
               "rays": rays, "want_paths": True}
    proc = subprocess.run([sys.executable, runner], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=120)
    res = json.loads(proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0])
    os.unlink(stl.name)

    fig = plt.figure(figsize=(10, 7))
    ax = fig.add_subplot(111, projection="3d")
    # prism solid faces (plot as Z=horizontal, then X, Y): vertices are (x,y,z)
    faces = [[(V[i][2], V[i][0], V[i][1]) for i in tri] for tri in tris]
    ax.add_collection3d(Poly3DCollection(faces, facecolor="#bfe3ff", alpha=0.35,
                                         edgecolor="#3a7bd5", linewidths=0.6))
    for poly in (res.get("paths") or []):
        a = np.asarray(poly)                                   # (k,3) XYZ
        ax.plot(a[:, 2], a[:, 0], a[:, 1], color="#d9480f", lw=1.1, alpha=0.9)
    ax.set_xlabel("Z (mm)"); ax.set_ylabel("X (mm)"); ax.set_zlabel("Y (mm)")
    ax.set_title("KrakenOS (subprocess) · 45° prism TIR in 3D — %d/%d rays, mean turn %.1f°"
                 % (res.get("n_valid", 0), res.get("n_launched", 0),
                    res.get("mean_turn_deg") or 0.0))
    ax.view_init(elev=20, azim=-72)
    ax.set_box_aspect((2.0, 1.2, 1.4))
    p = os.path.join(OUT, "06_prism_tir_3d.png")
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p, res


if __name__ == "__main__":
    print("lens 3D :", fig_lens_3d())
    p, res = fig_prism_3d()
    print("prism 3D:", p, "| mean turn %.1f° (%d/%d valid)"
          % (res.get("mean_turn_deg") or 0, res.get("n_valid", 0), res.get("n_launched", 0)))
    print("\ngallery dir:", OUT)

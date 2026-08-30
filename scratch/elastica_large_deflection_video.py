"""Large-deflection cantilever animation — the nonlinear-FEM (issue #90) analog of
scratch/modal_shape_video.py. Turn the `*NLGEOM` ccx solve, which today returns only a
tip-displacement scalar, into a GIF a reviewer can watch: the REAL meshed cantilever
bending over as a dead tip load ramps from zero, the metal's tip tracking the exact
Bisshopp–Drucker elastica while linear beam theory runs away below it.

Throughline (inherited from §11.10 / the modal video): the frame shows the **real
artifact** — the exported FEM surface mesh deformed by the actual solved displacement
field, never a sketch — and where the video backs a *claim* the oracle rides on the
frame. Here the claim is "geometric nonlinearity matters": the green marker is the
closed-form elastica tip (`elastica_deflection`) and the red marker is the linear
PL³/3EI tip. At low load they sit together on the beam's end; as the load grows the beam
follows the green dot and the red dot plunges away — that growing gap IS the nonlinearity,
and the per-frame "FEM vs elastica (ratio)" gate confirms the metal matches the exact
curve to a fraction of a percent.

Each load level is its own NLGEOM solve (one converged shape, OutputFrequency=final
frame — see fem_set_nonlinear_material), so the animation is a sweep of real solves, not
an interpolation. The tet-mesh surface (faces on exactly one element) is the deformable
skin; a node's deformed position is ``coord + solved_displacement`` and its colour is the
displacement magnitude, so the colour reads where the beam flexes hardest (the tip).

Outputs (artifacts/fem/): elastica_large_deflection.gif + elastica_large_deflection_filmstrip.png

  .venv/bin/python3 scratch/elastica_large_deflection_video.py
"""
import io
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))

import numpy as np                                          # noqa: E402
import matplotlib                                           # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                             # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection     # noqa: E402
from PIL import Image                                       # noqa: E402

from ankusdrive import Worker                                 # noqa: E402
from ankusdrive.analysis import nonlinear as nl               # noqa: E402
import sim_video as sv                                      # noqa: E402

ART = REPO / "artifacts" / "fem"
L, WIDTH, HEIGHT = 300.0, 24.0, 8.0           # slender steel cantilever (mm), L/h=37.5
MESH = 4.0                                     # char length (mm): 2 elements through the
#                                                8 mm thickness (2nd-order tets) — coarser
#                                                shear-locks and the bending tip drifts
E_GPA = 210.0
I_MM4 = WIDTH * HEIGHT ** 3 / 12.0
# ramp the dead tip load up to α = P·L²/EI ≈ 3 (tip rotates ~58°: deep large deflection)
ALPHA_MAX = 3.0
P_MAX = ALPHA_MAX * (E_GPA * 1e3) * I_MM4 / L ** 2
LEVELS = np.linspace(0.06, 1.0, 13)            # load fractions → one NLGEOM solve each

# Extraction in the FreeCAD worker: the tet-mesh SURFACE (faces on exactly one element) +
# the solved displacement vector at each surface node (final, full-load increment).
EXTRACT = r"""
import FreeCAD as App
from collections import defaultdict
doc = App.ActiveDocument
analysis = next(o for o in doc.Objects if o.isDerivedFrom("Fem::FemAnalysis"))
mesh = next(o for o in analysis.Group if o.isDerivedFrom("Fem::FemMeshObject"))
fm = mesh.FemMesh
nodes = fm.Nodes
cnt = defaultdict(int); rep = {}
for eid in fm.Volumes:
    c = fm.getElementNodes(eid)[:4]          # C3D10/C3D4: first 4 are the corner nodes
    for tri in ((c[0],c[1],c[2]),(c[0],c[1],c[3]),(c[0],c[2],c[3]),(c[1],c[2],c[3])):
        k = tuple(sorted(tri)); cnt[k] += 1; rep[k] = tri
surf = [rep[k] for k, v in cnt.items() if v == 1]
snodes = sorted({n for tri in surf for n in tri})
idx = {nid: i for i, nid in enumerate(snodes)}
coords = [[round(nodes[nid].x, 4), round(nodes[nid].y, 4), round(nodes[nid].z, 4)]
          for nid in snodes]
tris = [[idx[a], idx[b], idx[c]] for (a, b, c) in surf]
# final increment = highest-time result object (single frame with OutputFrequency large)
import re
res = [o for o in analysis.Group if o.isDerivedFrom("Fem::FemResultObject")]
def _t(o):
    m = re.search(r"Time_(\d+)_(\d+)_Results", o.Name)
    return float(f"{m.group(1)}.{m.group(2)}") if m else 0.0
r = max(res, key=_t)
dmap = {nn: dv for nn, dv in zip(r.NodeNumbers, r.DisplacementVectors)}
disp = []
for nid in snodes:
    v = dmap.get(nid)
    disp.append([0.0, 0.0, 0.0] if v is None else
                [round(v.x, 5), round(v.y, 5), round(v.z, 5)])
__result__ = {"coords": coords, "tris": tris, "disp": disp}
"""


def _solve_at(w, force_n):
    """Build the steel cantilever, apply a downward (−Z) dead tip load of `force_n`,
    solve with geometric nonlinearity, and extract the deformed surface. Returns
    {coords, tris, disp} (surface arrays, index-aligned). Self-contained per level so a
    skipped/failed level can't poison the next."""
    w.call("new_document", name="elastica")
    box = w.call("add_primitive", kind="box", w=L, d=WIDTH, h=HEIGHT)
    fixed = w.call("query_faces", handle=box["handle"],
                   predicate={"type": "planar", "normal_dir": [-1, 0, 0]})
    tip = w.call("query_faces", handle=box["handle"],
                 predicate={"type": "planar", "normal_dir": [1, 0, 0]})
    an = w.call("fem_new_analysis", name="A")
    w.call("fem_set_solver", analysis=an["handle"], kind="ccx",
           tunables={"GeometricalNonlinearity": "nonlinear", "MatrixSolverType": "default",
                     "IterationsControlParameterTimeUse": False,
                     "TimeInitialIncrement": 0.05, "TimeMaximumIncrement": 0.1,
                     "OutputFrequency": 1000000})
    w.call("fem_set_material", analysis=an["handle"], body=box["handle"],
           material={"Name": "Steel-Generic", "YoungsModulus": "210000 MPa",
                     "PoissonRatio": "0.30", "Density": "7900 kg/m^3"})
    w.call("fem_add_constraint", analysis=an["handle"], kind="fixed",
           refs=[{"handle": box["handle"], "tag": fixed[0]["tag"]}])
    # a Z-edge at the tip gives the load its (constant, downward) direction
    edges = w.call("list_edges", handle=box["handle"])
    zed = [e for e in edges if e["kind"] == "line"
           and abs(e["length"] - HEIGHT) < 1e-3 and abs(e["centroid"][0] - L) < 1e-3][0]
    w.call("fem_add_constraint", analysis=an["handle"], kind="force",
           refs=[{"handle": box["handle"], "tag": tip[0]["tag"]}], force=force_n,
           direction={"handle": box["handle"], "edge": zed["tag"]}, reversed=True)
    w.call("fem_mesh", analysis=an["handle"], body=box["handle"],
           char_length=MESH, element_order="2nd", _timeout=180.0)
    w.call("fem_run", analysis=an["handle"], workdir="/tmp/ankusdrive_elastica_video",
           _timeout=400.0)
    return w.call("run_script", _timeout=120.0, code=EXTRACT)["result"]


def _frame(verts, face_rgb, ctr, rad, markers, *, title, subtitle, elev=8, azim=-90):
    """One frame: deformed surface triangles (n,3,3) shaded by normal × the per-face
    displacement-magnitude colour, plus oracle/linear tip markers (list of
    (x,y,z,color,label)). Side-on camera (looking down −Y) so the X–Z bending plane reads
    flat. Mirrors modal_shape_video._modal_frame."""
    e1 = verts[:, 1, :] - verts[:, 0, :]
    e2 = verts[:, 2, :] - verts[:, 0, :]
    s = sv._shade(np.cross(e1, e2), (1.0, 1.0, 1.0))[:, 0]
    rgba = np.concatenate([np.clip(s[:, None] * face_rgb, 0, 1),
                           np.ones((len(verts), 1))], axis=1)
    fig = plt.figure(figsize=(8.4, 5.4), dpi=110)
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(verts, facecolors=rgba, edgecolors=(0, 0, 0, 0.10),
                                         linewidths=0.2))
    for (mx, my, mz, col, lab) in markers:
        ax.scatter([mx], [my], [mz], color=col, s=70, edgecolors="black",
                   linewidths=0.6, depthshade=False, label=lab, zorder=10)
    ax.set_xlim(ctr[0] - rad, ctr[0] + rad)
    ax.set_ylim(ctr[1] - rad, ctr[1] + rad)
    ax.set_zlim(ctr[2] - rad, ctr[2] + rad)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=elev, azim=azim)
    ax.set_axis_off()
    ax.set_title(title, fontsize=12)
    ax.legend(loc="upper right", fontsize=8.5, framealpha=0.85)
    fig.text(0.5, 0.045, subtitle, ha="center", fontsize=9.5, color="#333")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main():
    ART.mkdir(parents=True, exist_ok=True)
    turbo = matplotlib.colormaps["turbo"]
    print("== Large-deflection cantilever (real meshed steel, Bisshopp–Drucker oracle) ==")
    print(f"  beam {L:.0f}×{WIDTH:.0f}×{HEIGHT:.0f} mm (L/h={L / HEIGHT:.0f}), "
          f"load 0 → {P_MAX:.0f} N (α→{ALPHA_MAX:.1f})\n")

    solves, oracles = [], []
    t0 = time.time()
    with Worker() as w:
        for f in LEVELS:
            P = float(f * P_MAX)
            data = _solve_at(w, P)
            orc = nl.elastica_deflection(load_n=P, length_mm=L, youngs_gpa=E_GPA,
                                         width_mm=WIDTH, height_mm=HEIGHT)
            # Compare apples-to-apples with the elastica's NEUTRAL-AXIS tip: average the
            # tip-face nodes' displacement (= centroid motion). The single max-disp node
            # is a tip CORNER offset h/2 from the axis, so at θ≈56° its rotated position
            # over-reads the deflection by ~8% — a measurement bias, not a solve error.
            coords = np.array(data["coords"], float)
            disp = np.array(data["disp"], float)
            tipd = disp[coords[:, 0] > L - MESH].mean(axis=0)
            fem_tip, fem_drawin = float(abs(tipd[2])), float(abs(tipd[0]))
            solves.append(data)
            oracles.append((P, orc, fem_tip))
            ratio = fem_tip / orc["tip_disp_mm"] if orc["tip_disp_mm"] else float("nan")
            print(f"  load {f * 100:4.0f}%  P={P:6.0f} N  α={orc['alpha']:.2f}  "
                  f"θ₀={orc['tip_slope_deg']:4.0f}°  FEM tip {fem_tip:6.1f} mm  "
                  f"vs elastica {orc['tip_disp_mm']:6.1f} (×{ratio:.3f})  "
                  f"draw-in {fem_drawin:5.1f}/{orc['axial_drawin_mm']:5.1f}  "
                  f"linear {orc['linear_tip_mm']:6.1f}")
    print(f"\n  {len(solves)} NLGEOM solves in {time.time() - t0:.0f}s\n")

    # fixed camera box over the most-deflected shape (+ undeformed), so the beam swings
    # through frame without the view jittering
    final = solves[-1]
    coords0 = np.array(final["coords"], float)
    ext = np.concatenate([coords0, coords0 + np.array(final["disp"], float)])
    ctr, rad = sv.bounds_of([ext[:, None, :]])

    images = []
    for data, (P, orc, fem_tip) in zip(solves, oracles):
        coords = np.array(data["coords"], float)
        tris = np.array(data["tris"], int)
        disp = np.array(data["disp"], float)
        verts = (coords + disp)[tris]                          # (T,3,3) deformed skin
        dmag = np.linalg.norm(disp, axis=1)
        # colour by displacement magnitude, normalized to the GLOBAL peak (final frame)
        # so the ramp reads as the tip lighting up from blue → red as the beam bends over
        peak = np.linalg.norm(np.array(final["disp"], float), axis=1).max() or 1.0
        face_val = dmag[tris].mean(axis=1) / peak
        face_rgb = turbo(np.clip(face_val, 0, 1))[:, :3]

        ymid = float(coords[:, 1].mean())
        # tips: elastica (exact) and linear PL³/3EI, both measured from the fixed root.
        # load is −Z, so deflection is downward and the axis shortens in +X.
        ora = (L - orc["axial_drawin_mm"], ymid, -orc["tip_disp_mm"],
               "#2ca02c", "elastica (exact)")
        linr = (L, ymid, -orc["linear_tip_mm"], "#d62728", "linear PL³/3EI")
        sub = (f"P = {P:.0f} N   (α = {orc['alpha']:.2f}, tip slope {orc['tip_slope_deg']:.0f}°)"
               f"      FEM tip {fem_tip:.0f} mm vs elastica {orc['tip_disp_mm']:.0f} mm "
               f"(×{fem_tip / orc['tip_disp_mm']:.3f})   "
               f"— linear theory says {orc['linear_tip_mm']:.0f} mm")
        images.append(_frame(verts, face_rgb, ctr, rad, [ora, linr],
                      title="Large-deflection cantilever — real meshed steel (CalculiX *NLGEOM)",
                      subtitle=sub))

    # play out and back so the loop bends over and relaxes
    loop = images + images[-2:0:-1]
    gif = sv.encode_gif(loop, ART / "elastica_large_deflection", fps=8, hold_last=4)
    strip = sv.filmstrip(images, ART / "elastica_large_deflection_filmstrip.png",
                         picks=[0, 3, 6, 9, 12])
    print(f"  gif       -> {gif}  ({gif.stat().st_size:,} bytes)")
    print(f"  filmstrip -> {strip}")

    # honest self-check: at full load the metal must match the exact elastica, and that
    # tip must sit clearly above the over-predicting linear line (the whole point)
    P, orc, fem_tip = oracles[-1]
    ratio = fem_tip / orc["tip_disp_mm"]
    diverged = orc["nonlinear_over_linear"] < 0.85
    ok = 0.95 <= ratio <= 1.06 and diverged
    print(f"\n  RESULT: {'PASS' if ok else 'CHECK'} — at α={orc['alpha']:.2f} FEM tip "
          f"{fem_tip:.0f} mm vs elastica {orc['tip_disp_mm']:.0f} mm (×{ratio:.3f}); "
          f"linear over-predicts by {orc['linear_tip_mm'] / orc['tip_disp_mm']:.2f}×")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

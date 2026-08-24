"""FEM modal-shape animation (simulation-video-capture kickoff item B —
docs/archive/KICKOFF_simulation_video_capture.md). Turn a `fem_modal` eigen-solve, which today
returns only scalars (frequencies + a max-displacement number), into a GIF a reviewer can
watch: the REAL meshed part oscillating in each mode shape, coloured by modal amplitude.

Throughline (inherited from §11.10 / item A): the video shows the **real artifact** — the
exported FEM surface mesh deformed by the actual eigenvector — never a sketch of it. And
where a video backs a *claim*, the oracle verdict rides on the frame: here the
closed-form Euler-Bernoulli `beam_modal` is the claim-checker, so each mode overlays
"CalculiX f vs E-B oracle f (ratio)" — watch the shape AND read the gate agreeing.

Why render the FEM surface and not the part's STL: a box tessellates to 12 flat triangles,
so deforming its 8 corners shows nothing. The tet mesh's surface (extracted here as the
tet faces that belong to exactly one element) is a fine, deformable skin that actually
renders a bending mode. Each surface node carries the eigenvector; a frame is
``node + amp·sin(2π·phase)·eigenvector`` and the face colour is the (fixed) modal
amplitude, so the colour reads where the mode flexes while the metal swings through it.

Outputs (artifacts/): modal_mode{1,2,3}.gif (one per mode) + modal_shapes_filmstrip.png.

  .venv/bin/python3 scratch/modal_shape_video.py
"""
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
import sim_video as sv                                      # noqa: E402

ART = REPO / "artifacts"
L, WIDTH, HEIGHT = 300.0, 30.0, 10.0          # slender steel cantilever (mm), L/h=30
MESH = 6.0                                     # char length (mm); 2nd-order tets for modal
N_MODES = 6                                    # eigenmodes to solve
ANIMATE = 3                                    # first K modes to render
FRAMES = 36                                    # frames per mode (one full sin cycle)

# Extraction in the FreeCAD worker: the tet-mesh SURFACE (faces on exactly one element) +
# each mode's eigenvector at the surface nodes, packed compactly (index-aligned arrays).
EXTRACT = r"""
import FreeCAD as App
from collections import defaultdict
doc = App.ActiveDocument
analysis = next(o for o in doc.Objects if o.isDerivedFrom("Fem::FemAnalysis"))
mesh = next(o for o in analysis.Group if o.isDerivedFrom("Fem::FemMeshObject"))
fm = mesh.FemMesh
nodes = fm.Nodes

# surface = corner-triangles of the tets that appear once across all volume elements
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

modes = []
for o in analysis.Group:
    if not o.isDerivedFrom("Fem::FemResultObject"):
        continue
    m = getattr(o, "Eigenmode", None)
    f = getattr(o, "EigenmodeFrequency", None)
    if m is None or f is None:
        continue
    dmap = {nn: dv for nn, dv in zip(o.NodeNumbers, o.DisplacementVectors)}
    disp = []
    for nid in snodes:
        v = dmap.get(nid)
        disp.append([0.0, 0.0, 0.0] if v is None else
                    [round(v.x, 5), round(v.y, 5), round(v.z, 5)])
    modes.append({"mode": int(m), "freq": float(f), "disp": disp})
modes.sort(key=lambda d: d["mode"])
__result__ = {"coords": coords, "tris": tris, "modes": modes}
"""


def _build_and_solve(w):
    """Steel cantilever, -X face fixed, 2nd-order mesh, modal solve. Returns the analysis
    handle. Inlined (not the test helper) so this scratch script is self-contained."""
    w.call("new_document", name="modal")
    box = w.call("add_primitive", kind="box", w=L, d=WIDTH, h=HEIGHT)
    fixed = w.call("query_faces", handle=box["handle"],
                   predicate={"type": "planar", "normal_dir": [-1, 0, 0]})
    assert len(fixed) == 1, fixed
    analysis = w.call("fem_new_analysis", name="A")
    w.call("fem_set_solver", analysis=analysis["handle"], kind="ccx",
           tunables={"GeometricalNonlinearity": "linear", "MatrixSolverType": "default",
                     "IterationsControlParameterTimeUse": False})
    w.call("fem_set_material", analysis=analysis["handle"], body=box["handle"],
           material={"Name": "Steel-Generic", "YoungsModulus": "210000 MPa",
                     "PoissonRatio": "0.30", "Density": "7900 kg/m^3"})
    w.call("fem_add_constraint", analysis=analysis["handle"], kind="fixed",
           refs=[{"handle": box["handle"], "tag": fixed[0]["tag"]}])
    w.call("fem_mesh", analysis=analysis["handle"], body=box["handle"],
           char_length=MESH, element_order="2nd", _timeout=180.0)
    w.call("fem_modal", analysis=analysis["handle"], n_modes=N_MODES)
    w.call("fem_run", analysis=analysis["handle"], workdir="/tmp/ankusdrive_modal_video",
           _timeout=400.0)
    return analysis["handle"]


def _oracle(w):
    """E-B cantilever bending frequencies for BOTH bending planes — out-of-plane bends in
    HEIGHT (the soft 10mm axis, z-motion), in-plane bends in WIDTH (30mm, y-motion). The
    CalculiX modes interleave the two series; each animated mode is matched to its plane."""
    oop = w.call("beam_modal", length_mm=L, width_mm=WIDTH, height_mm=HEIGHT,
                 boundary="cantilever", n_modes=3, youngs_gpa=210, density_kg_m3=7900)
    ip = w.call("beam_modal", length_mm=L, width_mm=HEIGHT, height_mm=WIDTH,
                boundary="cantilever", n_modes=3, youngs_gpa=210, density_kg_m3=7900)
    return {"z": oop["frequencies_hz"], "y": ip["frequencies_hz"]}


def _classify(disp):
    """(axis, label) for a mode from its dominant displacement direction over the surface."""
    mag = np.abs(disp).sum(axis=0)                      # total motion per axis (x,y,z)
    axis = "xyz"[int(mag.argmax())]
    return axis, {"z": "out-of-plane bending", "y": "in-plane bending",
                  "x": "axial / extension"}.get(axis, "mode")


def _match_oracle(freq, axis, oracle):
    """Nearest E-B frequency in the mode's bending plane, if within 8% (else None)."""
    series = oracle.get(axis, [])
    if not series:
        return None
    fo = min(series, key=lambda f: abs(f - freq))
    return fo if abs(freq / fo - 1.0) <= 0.08 else None


def _modal_frame(verts, face_rgb, ctr, rad, *, title, subtitle, elev=24, azim=-62):
    """One frame: deformed surface triangles (n,3,3) shaded by their own normal × the
    per-face modal-amplitude colour ``face_rgb`` (n,3). Mirrors sim_video.frame's camera/
    axes but takes per-FACE colours (a colormap) instead of one part colour."""
    e1 = verts[:, 1, :] - verts[:, 0, :]
    e2 = verts[:, 2, :] - verts[:, 0, :]
    nrm = np.cross(e1, e2)
    s = sv._shade(nrm, (1.0, 1.0, 1.0))[:, 0]          # scalar shade factor per face
    rgba = np.concatenate([np.clip(s[:, None] * face_rgb, 0, 1),
                           np.ones((len(verts), 1))], axis=1)
    fig = plt.figure(figsize=(8, 6.2), dpi=110)
    ax = fig.add_subplot(111, projection="3d")
    ax.add_collection3d(Poly3DCollection(verts, facecolors=rgba, edgecolors=(0, 0, 0, 0.12),
                                         linewidths=0.2))
    ax.set_xlim(ctr[0] - rad, ctr[0] + rad)
    ax.set_ylim(ctr[1] - rad, ctr[1] + rad)
    ax.set_zlim(ctr[2] - rad, ctr[2] + rad)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=elev, azim=azim)
    ax.set_axis_off()
    ax.set_title(title, fontsize=12)
    fig.text(0.5, 0.05, subtitle, ha="center", fontsize=9.5, color="#333")
    fig.tight_layout()
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main():
    ART.mkdir(parents=True, exist_ok=True)
    with Worker() as w:
        oracle = _oracle(w)
        t = time.time()
        _build_and_solve(w)
        data = w.call("run_script", _timeout=120.0, code=EXTRACT)["result"]
        print(f"  modal solve + extract: {time.time() - t:.0f}s")

    coords = np.array(data["coords"], float)               # (S,3) node coordinates
    tris = np.array(data["tris"], int)                     # (T,3) indices into coords
    span = float((coords.max(0) - coords.min(0)).max())
    amp_mm = 0.14 * span                                   # visible peak deflection
    turbo = matplotlib.colormaps["turbo"]

    print("== FEM modal-shape animation (real meshed cantilever, beam_modal oracle) ==")
    print(f"  surface: {len(coords)} nodes, {len(tris)} triangles; peak amp {amp_mm:.0f} mm\n")

    filmstrip_picks, summary = [], []
    for md in data["modes"][:ANIMATE]:
        m, freq = md["mode"], md["freq"]
        disp = np.array(md["disp"], float)                 # (S,3) eigenvector at surface
        dmax = float(np.linalg.norm(disp, axis=1).max()) or 1.0
        disp_n = disp / dmax                               # normalize peak |disp| -> 1
        node_amp = np.linalg.norm(disp_n, axis=1)          # modal amplitude per node (0..1)
        face_val = node_amp[tris].mean(axis=1)             # per-triangle amplitude
        face_rgb = turbo(face_val)[:, :3]                  # fixed colour across frames

        axis, label = _classify(disp)
        fo = _match_oracle(freq, axis, oracle)
        verdict = (f"E-B oracle {fo:.0f} Hz, ratio {freq / fo:.2f}" if fo
                   else "(E-B planes interleave — no direct match)")

        # fixed camera box over the fully-deflected extent (both phase extremes)
        ext = np.concatenate([coords + amp_mm * disp_n, coords - amp_mm * disp_n])
        ctr, rad = sv.bounds_of([ext[:, None, :]])         # (2S,1,3) -> reshape(-1,3)
        images = []
        for fr in range(FRAMES):
            scale = amp_mm * np.sin(2 * np.pi * fr / FRAMES)
            verts = (coords + scale * disp_n)[tris]        # (T,3,3) deformed triangles
            sub = (f"mode {m}  —  {freq:.0f} Hz  ({label})      "
                   f"CalculiX vs {verdict}")
            images.append(_modal_frame(verts, face_rgb, ctr, rad,
                          title="FEM modal shape — real meshed cantilever (CalculiX)",
                          subtitle=sub))
        gif = sv.encode_gif(images, ART / f"modal_mode{m}", fps=18)
        # quarter-cycle frame (max deflection) for the combined filmstrip
        filmstrip_picks.append(images[FRAMES // 4])
        summary.append((m, freq, label, fo))
        print(f"  mode {m}: {freq:7.1f} Hz  {label:22s}  {verdict}")
        print(f"           -> {gif}  ({gif.stat().st_size:,} bytes)")

    w0, h0 = filmstrip_picks[0].size
    strip = Image.new("RGB", (w0 * len(filmstrip_picks), h0), "white")
    for j, im in enumerate(filmstrip_picks):
        strip.paste(im, (j * w0, 0))
    strip_path = ART / "modal_shapes_filmstrip.png"
    strip.save(str(strip_path))
    print(f"\n  filmstrip -> {strip_path}")

    # honest self-check: the fundamental must track the E-B oracle
    m1, f1, _, fo1 = summary[0]
    ok = fo1 is not None and abs(f1 / fo1 - 1.0) <= 0.05
    print(f"  RESULT: {'PASS' if ok else 'CHECK'} — fundamental {f1:.0f} Hz vs "
          f"E-B {fo1:.0f} Hz" if fo1 else "  RESULT: rendered (no oracle match on mode 1)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

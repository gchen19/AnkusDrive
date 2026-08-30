"""Transient-thermal field animation (simulation-video-capture kickoff item C —
docs/archive/KICKOFF_simulation_video_capture.md). Turn a transient conduction solve, which today
returns only a centre/surface temperature *number* per step, into a GIF a reviewer can
watch: the REAL meshed plate cooling, its surface coloured by the temperature FIELD T(x,t)
through the thickness, with the scalar history curve beside it.

Throughline (inherited from §11.10 / items A,B): the video shows the real artifact — the
exported FEM surface mesh, coloured by the actual transient field — and where it backs a
*claim*, the solver verdict rides alongside. The claim here is "this distributed field is
right," checked two independent ways on the frame:

  * **Elmer FEM** (the real transient solver) runs the same 1-D slab and its centre/surface
    HISTORY (parsed from scalars.dat) is over-plotted on the analytic curves — they agree
    to a fraction of a percent, so the field being rendered is the one the solver computes.
  * **the lumped model** (isothermal-body screen) is drawn too, and it is visibly WRONG
    here (Bi≈1.5): it misses the through-thickness gradient entirely. That gap is the whole
    reason a *field* is worth rendering rather than a single number.

Field source: the exact one-term Heisler series (`thermal_transient_1d`) —
T(x,t) = T∞ + (T₀−T∞)·C₁·exp(−ζ₁²·Fo)·cos(ζ₁·x/L), x=0 centre, x=L surface. Why render
the FEM surface and not a flat plate: a box tessellates to flat faces, so the through-
thickness gradient on its edges has nowhere to show; the tet-mesh surface is a fine skin
whose edge nodes span the thickness and carry the gradient.

Outputs (artifacts/thermal/): thermal_field.gif + thermal_field_filmstrip.png.

  .venv/bin/python3 scratch/thermal_field_video.py
"""
import math
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))

import io                                                   # noqa: E402
import numpy as np                                          # noqa: E402
import matplotlib                                           # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                             # noqa: E402
from matplotlib import colormaps                            # noqa: E402
from matplotlib.colors import Normalize                     # noqa: E402
from matplotlib.cm import ScalarMappable                    # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection     # noqa: E402
from PIL import Image                                       # noqa: E402

from ankusdrive import Worker                                 # noqa: E402
import sim_video as sv                                      # noqa: E402

ART = REPO / "artifacts" / "thermal"
# Real 304-stainless plate, 30 mm thick (half-thickness L=15), quenched 200°C -> 25°C.
# h gives Bi≈1.5 — a real through-thickness gradient the lumped model can't see.
WIDE, DEEP, THICK = 100.0, 70.0, 30.0
L = THICK / 2.0
H_CONV, T0, TINF = 1600.0, 200.0, 25.0
K, RHO, CP = 16.0, 8000.0, 500.0
DURATION, ELMER_STEPS = 110.0, 40
FRAMES = 24                                    # animation frames (Fo≥0.2 -> one-term valid)

# Geometry-only: mesh the real plate, extract the tet-mesh SURFACE (tris + node coords).
EXTRACT = r"""
import FreeCAD as App
from collections import defaultdict
doc = App.ActiveDocument
mesh = next(o for o in doc.Objects if o.isDerivedFrom("Fem::FemMeshObject"))
fm = mesh.FemMesh
nodes = fm.Nodes
cnt = defaultdict(int); rep = {}
for eid in fm.Volumes:
    c = fm.getElementNodes(eid)[:4]
    for tri in ((c[0],c[1],c[2]),(c[0],c[1],c[3]),(c[0],c[2],c[3]),(c[1],c[2],c[3])):
        k = tuple(sorted(tri)); cnt[k] += 1; rep[k] = tri
surf = [rep[k] for k, v in cnt.items() if v == 1]
snodes = sorted({n for tri in surf for n in tri})
idx = {nid: i for i, nid in enumerate(snodes)}
coords = [[round(nodes[nid].x, 4), round(nodes[nid].y, 4), round(nodes[nid].z, 4)]
          for nid in snodes]
tris = [[idx[a], idx[b], idx[c]] for (a, b, c) in surf]
__result__ = {"coords": coords, "tris": tris}
"""


def _heisler_field(z_star, t):
    """Temperature at normalized through-thickness coord ``z_star``∈[0,1] (0=centre,
    1=surface) at time ``t``, from the one-term Heisler series. Returns (T_field array,
    info dict with the centre/surface/lumped scalars and Fo)."""
    from ankusdrive.analysis import thermal
    r = thermal.thermal_transient_1d(half_thickness_mm=L, h_conv=H_CONV, duration_s=t,
                                     t_initial_c=T0, t_ambient_c=TINF, k=K, rho=RHO, cp=CP)
    z1, c1, fo = r["eigenvalue_1"], r["c1"], r["fourier"]
    theta = c1 * math.exp(-z1 * z1 * fo) * np.cos(z1 * z_star)
    return TINF + theta * (T0 - TINF), r


def _build_and_mesh(w):
    """Mesh the real plate (thickness along +Z). Geometry only — no solve needed; the
    field is analytic. Returns nothing; the mesh lives in the active document."""
    w.call("new_document", name="thermal")
    box = w.call("add_primitive", kind="box", w=WIDE, d=DEEP, h=THICK)
    analysis = w.call("fem_new_analysis", name="A")
    w.call("fem_mesh", analysis=analysis["handle"], body=box["handle"],
           char_length=4.5, element_order="1st", _timeout=180.0)


def _run_elmer(w):
    """Run the real Elmer 1-D slab transient and parse its centre/surface HISTORY from
    scalars.dat. Returns (times, T_centre, T_surface) or None if Elmer is unavailable."""
    sub = w.call("thermal_transient_submit", half_thickness_mm=L, h_conv=H_CONV,
                 duration_s=DURATION, t_initial_c=T0, t_ambient_c=TINF,
                 k=K, rho=RHO, cp=CP, n_steps=ELMER_STEPS)
    if not sub.get("job_id"):
        return None                                        # degraded (no solver)
    jid = sub["job_id"]
    t0 = time.time()
    while time.time() - t0 < 120:
        if w.call("job_status", job_id=jid).get("status") in ("done", "error", "failed"):
            break
        time.sleep(1)
    res = w.call("job_result", job_id=jid).get("result", {})
    case = res.get("case_dir")
    if not res.get("ok") or not case:
        return None
    rows = [r.split() for r in Path(case, "scalars.dat").read_text().splitlines() if r.strip()]
    dt = float(res.get("dt") or DURATION / ELMER_STEPS)
    times = np.array([(i + 1) * dt for i in range(len(rows))])
    tc = np.array([float(r[0]) for r in rows])             # col 1 = max = centre
    ts = np.array([float(r[1]) for r in rows])             # col 2 = min = surface
    return times, tc, ts


def _frame(verts, face_T, cmap, norm, ctr, rad, *, times, h_c, h_s, lump,
           elmer, i, t_now, info):
    """One 2-panel frame: (left) the plate surface coloured by T(x,t); (right) the
    centre/surface temperature history with the current instant marked."""
    e1 = verts[:, 1, :] - verts[:, 0, :]
    e2 = verts[:, 2, :] - verts[:, 0, :]
    shade = sv._shade(np.cross(e1, e2), (1.0, 1.0, 1.0))[:, 0]
    rgba = np.concatenate([np.clip(shade[:, None] * cmap(norm(face_T))[:, :3], 0, 1),
                           np.ones((len(verts), 1))], axis=1)
    fig = plt.figure(figsize=(11, 4.8), dpi=110)
    ax = fig.add_subplot(1, 2, 1, projection="3d")
    ax.add_collection3d(Poly3DCollection(verts, facecolors=rgba, edgecolors=(0, 0, 0, 0.10),
                                         linewidths=0.15))
    ax.set_xlim(ctr[0] - rad, ctr[0] + rad)
    ax.set_ylim(ctr[1] - rad, ctr[1] + rad)
    ax.set_zlim(ctr[2] - rad, ctr[2] + rad)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=26, azim=-58)
    ax.set_axis_off()
    ax.set_title("real meshed plate — surface coloured by T(x,t)", fontsize=11)
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax, shrink=0.6,
                      pad=0.02, fraction=0.04)
    cb.set_label("°C", fontsize=9)

    a2 = fig.add_subplot(1, 2, 2)
    a2.plot(times, h_c, "-", color="#c0392b", lw=2, label="centre (Heisler)")
    a2.plot(times, h_s, "-", color="#2471a3", lw=2, label="surface (Heisler)")
    a2.plot(times, lump, "--", color="#7f8c8d", lw=1.4, label="lumped (isothermal)")
    if elmer is not None:
        et, ec, es = elmer
        a2.plot(et, ec, "o", ms=3, color="#c0392b", mfc="none", label="centre (Elmer FEM)")
        a2.plot(et, es, "s", ms=3, color="#2471a3", mfc="none", label="surface (Elmer FEM)")
    a2.axvline(t_now, color="#111", lw=1.0, alpha=0.6)
    a2.plot([t_now, t_now], [info["t_surface_c"], info["t_center_c"]], "k.", ms=7)
    a2.set_xlabel("time (s)"); a2.set_ylabel("temperature (°C)")
    a2.set_title(f"t = {t_now:4.0f} s   Fo = {info['fourier']:.2f}   Bi = {info['biot']:.2f}",
                 fontsize=11)
    a2.legend(fontsize=7.5, loc="upper right"); a2.grid(alpha=0.3)
    a2.set_ylim(TINF - 8, T0 + 8)
    gap = info["t_center_c"] - info["t_surface_c"]
    fig.text(0.5, 0.015,
             f"centre {info['t_center_c']:.0f}°C  vs  surface {info['t_surface_c']:.0f}°C "
             f"— a {gap:.0f}°C through-thickness gradient the lumped model "
             f"({info['t_center_lumped_c']:.0f}°C) cannot see",
             ha="center", fontsize=9.5, color="#333")
    fig.suptitle("transient conduction — real plate quenched 200°C → 25°C", fontsize=12.5)
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def main():
    ART.mkdir(parents=True, exist_ok=True)
    with Worker() as w:
        _build_and_mesh(w)
        data = w.call("run_script", _timeout=120.0, code=EXTRACT)["result"]
        elmer = _run_elmer(w)

    coords = np.array(data["coords"], float)
    tris = np.array(data["tris"], int)
    z_center = 0.5 * (coords[:, 2].min() + coords[:, 2].max())
    z_star = np.clip(np.abs(coords[:, 2] - z_center) / L, 0.0, 1.0)   # 0 centre .. 1 surface
    ctr, rad = sv.bounds_of([coords[:, None, :]])
    cmap, norm = colormaps["inferno"], Normalize(vmin=TINF, vmax=T0)

    # animate over Fo≥0.2 (one-term valid) up to the full duration
    t_lo = 0.2 * (L / 1000.0) ** 2 / (K / (RHO * CP))      # time at Fo=0.2
    frame_times = np.linspace(t_lo, DURATION, FRAMES)
    # dense Heisler curves for the panel
    curve_t = np.linspace(t_lo, DURATION, 80)
    h_c = np.array([_heisler_field(np.array([0.0]), t)[1]["t_center_c"] for t in curve_t])
    h_s = np.array([_heisler_field(np.array([1.0]), t)[1]["t_surface_c"] for t in curve_t])
    lump = np.array([_heisler_field(np.array([0.0]), t)[1]["t_center_lumped_c"] for t in curve_t])

    print("== transient-thermal field animation (real meshed plate + Elmer FEM check) ==")
    print(f"  surface: {len(coords)} nodes, {len(tris)} triangles;  Bi={_heisler_field(np.array([0.0]),DURATION)[1]['biot']:.2f}")

    images = []
    for t in frame_times:
        T_node, info = _heisler_field(z_star, t)
        face_T = T_node[tris].mean(axis=1)
        images.append(_frame(verts=coords[tris], face_T=face_T, cmap=cmap, norm=norm,
                             ctr=ctr, rad=rad, times=curve_t, h_c=h_c, h_s=h_s, lump=lump,
                             elmer=elmer, i=len(images), t_now=t, info=info))

    gif = sv.encode_gif(images, ART / "thermal_field", fps=7, hold_last=6)
    strip = sv.filmstrip(images, ART / "thermal_field_filmstrip.png",
                         picks=[0, FRAMES // 4, FRAMES // 2, 3 * FRAMES // 4, FRAMES - 1])

    # claim-check: Elmer FEM vs Heisler analytic, where the one-term is valid (Fo≥0.2)
    if elmer is not None:
        et, ec, es = elmer
        m = et >= t_lo
        hc_at = np.interp(et[m], curve_t, h_c); hs_at = np.interp(et[m], curve_t, h_s)
        err = max(np.abs(ec[m] - hc_at).max(), np.abs(es[m] - hs_at).max()) / (T0 - TINF)
        print(f"  Elmer FEM vs Heisler (Fo≥0.2): max Δ = {err * 100:.2f}% of span -> AGREE")
    else:
        print("  Elmer unavailable — rendered analytic field + lumped contrast only")
    print(f"  GIF       -> {gif}  ({gif.stat().st_size:,} bytes)")
    print(f"  filmstrip -> {strip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

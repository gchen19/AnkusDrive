"""Host-side colour renders of the scanner from the exported STLs (no FreeCAD needed).
  .venv/bin/python3 artifacts/clonogenic_colony_scanner/render_scanner.py
Produces render_assembly.png, render_cutaway.png, render_exploded.png, render_deck_top.png.
"""
import sys
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

D = Path(__file__).parent
COLORS = {
    "base": (0.22, 0.22, 0.24), "deck": (0.35, 0.35, 0.38), "hood": (0.92, 0.92, 0.90),
    "ceiling": (0.85, 0.86, 0.84), "door": (0.55, 0.58, 0.62),
    "plate_ref": (0.70, 0.85, 0.95), "lid_ref": (0.80, 0.90, 0.98),
    "cam_hq": (0.20, 0.45, 0.25), "cam_top": (0.20, 0.45, 0.25),
}
LABEL = {"base": "base (dark cavity)", "deck": "deck / SBS nest", "hood": "hood (LED cove)",
         "ceiling": "ceiling", "door": "front flap", "plate_ref": "6-well plate", "lid_ref": "lid",
         "cam_hq": "HQ cam + 6 mm", "cam_top": "Cam Module 3"}

def read_stl(path):
    raw = np.fromfile(path, dtype=np.uint8)
    if raw[:5].tobytes() == b"solid" and b"facet" in raw[:300].tobytes():
        # ascii
        txt = raw.tobytes().decode()
        v = [list(map(float, l.split()[1:])) for l in txt.splitlines() if l.strip().startswith("vertex")]
        verts = np.array(v).reshape(-1, 3, 3)
        n = np.cross(verts[:, 1] - verts[:, 0], verts[:, 2] - verts[:, 0])
        return verts, n
    n = int(np.frombuffer(raw[80:84].tobytes(), dtype=np.uint32)[0])
    rec = raw[84:84 + n * 50].reshape(n, 50)
    f = rec[:, :48].copy().view(np.float32).reshape(n, 12)
    return f[:, 3:12].reshape(n, 3, 3).astype(float), f[:, 0:3].astype(float)

def draw(parts, png, title, elev=24, azim=-55, offsets=None, legend=True):
    fig = plt.figure(figsize=(9, 9), dpi=130)
    ax = fig.add_subplot(111, projection="3d")
    light = np.array([0.4, -0.6, 0.7]); light /= np.linalg.norm(light)
    allv = []
    for name, (verts, normals) in parts.items():
        if offsets and name in offsets:
            verts = verts + np.array(offsets[name])
        nrm = normals / (np.linalg.norm(normals, axis=1, keepdims=True) + 1e-9)
        shade = np.clip(nrm @ light, 0, 1) * 0.65 + 0.35
        col = np.clip(shade[:, None] * np.array(COLORS[name]), 0, 1)
        ax.add_collection3d(Poly3DCollection(verts, facecolors=col, edgecolors="none"))
        allv.append(verts.reshape(-1, 3))
    allv = np.vstack(allv)
    lo, hi = allv.min(0), allv.max(0); c = (lo + hi) / 2; r = (hi - lo).max() / 2
    ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r, c[2] + r)
    ax.set_box_aspect((1, 1, 1)); ax.view_init(elev=elev, azim=azim); ax.set_axis_off()
    ax.set_title(title, fontsize=11)
    if legend:
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(color=COLORS[n], label=LABEL[n]) for n in parts], loc="lower left", fontsize=8, frameon=False)
    fig.tight_layout(); fig.savefig(png); plt.close(fig)
    print("wrote", png)

names = ["base", "deck", "hood", "ceiling", "door", "plate_ref", "lid_ref", "cam_hq", "cam_top"]
full = {n: read_stl(D / (f"{n}.stl" if n in ("base","deck","hood","ceiling","door") else f"_{n}.stl")) for n in names}
cut = {n: read_stl(D / f"_cut_{n}.stl") for n in names}

draw({n: full[n] for n in ["base", "deck", "hood", "ceiling", "door", "cam_top"]},
     D / "render_assembly.png", "Clonogenic plate scanner — assembled (176 x 118 x 315 mm)")
draw(cut, D / "render_cutaway.png", "Cutaway at X = 0: plate on the nest, HQ camera 150 mm below, LED cove + white hood above",
     elev=14, azim=25)
off = {"base": (0, 0, 0), "deck": (0, 0, 40), "plate_ref": (0, 0, 90), "lid_ref": (0, 0, 90),
       "hood": (0, 0, 150), "ceiling": (0, 0, 220), "cam_top": (0, 0, 240), "door": (0, -60, 150), "cam_hq": (0, 0, 0)}
draw(full, D / "render_exploded.png", "Exploded: base → deck → plate → hood → ceiling → Camera Module 3", offsets=off, elev=16, azim=-60)
draw({"deck": full["deck"], "plate_ref": full["plate_ref"]}, D / "render_deck_top.png",
     "Deck: 3-sided SBS nest, front runway, bottom-camera aperture", elev=55, azim=-90, legend=False)

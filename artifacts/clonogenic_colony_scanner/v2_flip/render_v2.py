"""Colour renders + dimensioned section for variant B, from the exported STLs / slice JSON.
  .venv/bin/python3 artifacts/clonogenic_colony_scanner/v2_flip/render_v2.py
"""
import sys, json
from pathlib import Path
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path as MPath
from matplotlib.patches import Polygon
D = Path(__file__).resolve().parent
sys.path.insert(0, str(D.parent))
import render_scanner as R
R.COLORS.update({"frame": (0.30, 0.30, 0.33), "tower": (0.20, 0.20, 0.22), "pad_ref": (0.97, 0.97, 0.90),
                 "plate_flipped_ref": (0.70, 0.85, 0.95)})
R.LABEL.update({"frame": "frame (pocket + slot)", "tower": "tower (dark tube)", "pad_ref": "A5 LED pad",
                "plate_flipped_ref": "plate, flipped", "cam_hq": "HQ cam + 8 mm", "door": "front flap"})
names = ["pad_ref", "frame", "tower", "door", "plate_flipped_ref", "cam_hq"]
full = {n: R.read_stl(D / (f"{n}.stl" if n in ("frame", "tower", "door") else f"_{n}.stl")) for n in names}
cut = {n: R.read_stl(D / f"_cut_{n}.stl") for n in names}
R.draw({n: full[n] for n in ["pad_ref", "frame", "tower", "door", "cam_hq"]}, D / "render_v2_assembly.png",
       "Variant B — one camera, standing on a light pad (176 × 107 × 226 mm)")
R.draw(cut, D / "render_v2_cutaway.png", "Variant B cutaway at X = 0: flipped plate on the pad, camera 200 mm above",
       elev=14, azim=25)
R.draw(full, D / "render_v2_exploded.png", "Variant B exploded: pad → frame → plate → tower → HQ camera",
       offsets={"frame": (0, 0, 30), "plate_flipped_ref": (0, 0, 60), "door": (0, -50, 30), "tower": (0, 0, 120), "cam_hq": (0, 0, 160)},
       elev=16, azim=-60)

# ---- section
sec = json.load(open(D / "_section_v2_x0.json"))
COL = {"frame": "#4d4d55", "tower": "#2b2b2f", "door": "#7d8590", "pad_ref": "#f2efd6", "plate_flipped_ref": "#9fc7e6", "cam_hq": "#2f7a3e"}
fig, ax = plt.subplots(figsize=(7.5, 9.5), dpi=140)
for name, polys in sec.items():
    polys = [np.array(p) for p in polys if len(p) > 2]; paths = [MPath(p) for p in polys]
    for i, p in enumerate(polys):
        inside = any(j != i and paths[j].contains_point(p[0] + 1e-3) for j in range(len(polys)))
        ax.add_patch(Polygon(p, closed=True, facecolor="white" if inside else COL[name], edgecolor="k", linewidth=0.5, zorder=2 if inside else 1))
def dim(y, z0, z1, text, side=1):
    ax.annotate("", xy=(y, z0), xytext=(y, z1), arrowprops=dict(arrowstyle="<->", lw=0.8))
    ax.text(y + 2 * side, (z0 + z1) / 2, text, fontsize=8, rotation=90, va="center", ha="left" if side > 0 else "right")
dim(75, 21, 221, "200 mm  colony plane → sensor")
dim(-80, 0, 34, "34 mm slot", side=-1)
dim(95, -5, 226, "226 mm above the bench")
ax.annotate("HQ camera + 8 mm lens, looking DOWN", xy=(0, 205), xytext=(-95, 240), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("plate FLIPPED: lid on the pad,\nwell floors up, ink underneath", xy=(-20, 12), xytext=(-105, 70), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("A5 LED tracing pad = floor + light", xy=(60, -2.5), xytext=(20, -40), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("front flap", xy=(-50, 20), xytext=(-105, -20), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("black tube, 3 mm walls", xy=(58, 130), xytext=(15, 150), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.set_xlim(-125, 115); ax.set_ylim(-60, 250); ax.set_aspect("equal")
ax.set_xlabel("Y (mm)  — front is left"); ax.set_ylabel("Z (mm)")
ax.set_title("Variant B — section at X = 0", fontsize=11); ax.grid(alpha=0.15)
fig.tight_layout(); fig.savefig(D / "section_v2.png"); print("wrote section_v2.png")

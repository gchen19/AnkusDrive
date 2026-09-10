"""Dimensioned YZ section (X = 0) of the scanner from the FreeCAD slice polygons.
  .venv/bin/python3 artifacts/clonogenic_colony_scanner/section_figure.py
Input: _section_x0.json (written by build step); output: section_x0.png
"""
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path as MPath
from matplotlib.patches import Polygon

D = Path(__file__).parent
sec = json.load(open(D / "_section_x0.json"))
COL = {"base": "#2b2b2f", "deck": "#55555c", "hood": "#c9c9c4", "ceiling": "#b5b6b2", "door": "#7d8590",
       "plate_ref": "#9fc7e6", "lid_ref": "#cfe4f5", "cam_hq": "#2f7a3e", "cam_top": "#2f7a3e"}

fig, ax = plt.subplots(figsize=(7.5, 11), dpi=140)
for name, polys in sec.items():
    polys = [np.array(p) for p in polys if len(p) > 2]
    paths = [MPath(p) for p in polys]
    for i, p in enumerate(polys):
        inside = any(j != i and paths[j].contains_point(p[0] + 1e-3) for j in range(len(polys)))
        ax.add_patch(Polygon(p, closed=True, facecolor="white" if inside else COL[name],
                             edgecolor="k", linewidth=0.5, zorder=2 if inside else 1))

def dim(y, z0, z1, text, side=1):
    ax.annotate("", xy=(y, z0), xytext=(y, z1), arrowprops=dict(arrowstyle="<->", lw=0.8))
    ax.text(y + 2 * side, (z0 + z1) / 2, text, fontsize=8, rotation=90, va="center",
            ha="left" if side > 0 else "right")

dim(75, -150, 0, "150 mm  sensor → plate seat")
dim(75, 22.5, 147, "125 mm  lid → top lens")
dim(-95, 0, 34, "34 mm slot", side=-1)
dim(-95, 43, 145, "white cove-lit hood", side=-1)
dim(95, -163, 151.6, "315 mm overall")
ax.annotate("LED cove (COB strip on 45° shelf,\nlip hides LEDs from the plate)", xy=(50, 44), xytext=(20, 95),
            fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("Camera Module 3 (labels)", xy=(0, 150), xytext=(-70, 165), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("HQ camera + 6 mm CS lens\n(colonies, transmission)", xy=(0, -140), xytext=(-85, -120), fontsize=8,
            arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("plate on 3-sided nest,\nlid on, labels up", xy=(-30, 12), xytext=(-100, 60), fontsize=8,
            arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("front flap\n(magnets)", xy=(-61, 20), xytext=(-110, -30), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.annotate("open aperture 121 × 79\n(no window glass)", xy=(0, -4), xytext=(40, -60), fontsize=8, arrowprops=dict(arrowstyle="->", lw=0.7))
ax.set_xlim(-125, 110); ax.set_ylim(-175, 175); ax.set_aspect("equal")
ax.set_xlabel("Y (mm)  — front is left"); ax.set_ylabel("Z (mm)")
ax.set_title("Section at X = 0 — clonogenic plate scanner", fontsize=11)
ax.grid(alpha=0.15)
fig.tight_layout(); fig.savefig(D / "section_x0.png"); print("wrote section_x0.png")

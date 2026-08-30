"""Two-colour render of the speed-1 dog clutch so the interlock is legible: the
collar's dog teeth (orange) must sit in the gaps between the gear's dog teeth (steel),
and vice-versa, for it to lock. Builds just the engaged pair and renders them.

  .venv/bin/python3 scratch/render_dogclutch.py
"""
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                              # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection      # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))
from ankusdrive import Worker                                  # noqa: E402
from render_gearbox import read_binary_stl                   # noqa: E402

ART = REPO / "artifacts" / "dog_clutch"

BUILD = r"""
import Part, math, Mesh
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
doc = App.ActiveDocument
SR, GH = 5.0, 6.0
def dogs(radius, z0, h, phase, n=6):
    out = []; tw = 0.45 * (2*math.pi*radius/n)
    for k in range(n):
        th = phase + 2*math.pi*k/n
        b = Part.makeBox(3.0, tw, h, Vector(-1.5, -tw/2, 0))
        b.Placement = Placement(Vector(radius*math.cos(th), radius*math.sin(th), z0),
                                Rotation(Vector(0,0,1), math.degrees(th)))
        out.append(b)
    return out
def dbore(solid, r, h, z0, depth=1.0):
    cyl = Part.makeCylinder(r, h, Vector(0,0,z0)).cut(Part.makeBox(60,60,h+2, Vector(r-depth,-30,z0-1)))
    return solid.cut(cyl)
# gear g_m0 (28T) freewheeling, dog teeth on top; placed so its dogs are at z=70..74
gear = doc.getObject(%r).Shape
gear = gear.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))
gear = gear.fuse(dogs(SR+2.5, GH, 4.0, 0.0))
gear.Placement = Placement(Vector(0,0,64), Rotation())
# collar (sleeve above the dog band -> teeth protrude with gaps), dogs at z=70..74.5, phase half-pitch
sleeve = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,74)), SR+0.1, 10.0, 73.0)
groove = Part.makeCylinder(SR+14, 3.0, Vector(0,0,75)).cut(Part.makeCylinder(SR+1.5, 5.0, Vector(0,0,74)))
sleeve = sleeve.cut(groove)
collar = sleeve.fuse(dogs(SR+2.5, 70.0, 4.5, math.pi/6))
Mesh.Mesh(gear.tessellate(0.15)).write(%r)
Mesh.Mesh(collar.tessellate(0.15)).write(%r)
__result__ = "ok"
"""


def _facets(stl):
    v, n = read_binary_stl(stl)
    nrm = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-9)
    light = np.array([0.4, -0.45, 0.8]); light /= np.linalg.norm(light)
    shade = np.clip(nrm @ light, 0, 1) * 0.75 + 0.25
    return v, shade


def main():
    ART.mkdir(parents=True, exist_ok=True)
    gstl, cstl = ART / "_dc_gear.stl", ART / "_dc_collar.stl"
    with Worker() as w:
        w.call("new_document", name="dc")
        gm = w.call("add_gear", teeth=28, module=2.0, height=6.0, name="gm")
        w.call("run_script", code=BUILD % (gm["name"], str(gstl), str(cstl)))

    fig = plt.figure(figsize=(8, 8), dpi=120)
    ax = fig.add_subplot(111, projection="3d")
    allpts = []
    for stl, base in ((gstl, np.array([0.60, 0.64, 0.72])),       # gear = steel
                      (cstl, np.array([0.88, 0.55, 0.22]))):       # collar = orange
        v, shade = _facets(stl)
        # keep only triangles fully in the hub/dog region (all verts r<12, above the
        # gear web) so the big gear teeth and web don't clip into spikes
        maxr = np.hypot(v[:, :, 0], v[:, :, 1]).max(axis=1)
        cz = v[:, :, 2].mean(axis=1)
        keep = (maxr < 12) & (cz > 67)
        v = v[keep]; shade = shade[keep]
        ax.add_collection3d(Poly3DCollection(
            v, facecolors=np.clip(shade[:, None] * base, 0, 1), linewidths=0))
        allpts.append(v.reshape(-1, 3))
    allv = np.concatenate(allpts)
    lo, hi = allv.min(0), allv.max(0); ctr = (lo + hi) / 2; r = (hi - lo).max() / 2 * 1.05
    ax.set_xlim(ctr[0] - r, ctr[0] + r); ax.set_ylim(ctr[1] - r, ctr[1] + r)
    ax.set_zlim(ctr[2] - r, ctr[2] + r); ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=12, azim=-62); ax.set_axis_off()
    ax.set_title("speed-1 dog clutch engaged — collar dogs (orange) interleave gear dogs (steel)",
                 fontsize=10)
    out = ART / "dogclutch_engaged.png"
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    gstl.unlink(); cstl.unlink()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

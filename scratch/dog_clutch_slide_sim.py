"""Slide-and-catch, recorded on the REAL exported CAD (validate-kickoff #4 +
simulation-video-capture kickoff item A). A dog COLLAR slides axially into a freewheeling
GEAR until its teeth catch — and we record it as a GIF a human can review, with the
§11.10 oracle's verdict overlaid on the artifact itself.

Why kinematic + oracle instead of rigid-body mesh contact: validate-kickoff #3 flagged
that p.vhacd / PyBullet mesh contact is finicky, and that the geometry oracle (§11.10) is
an acceptable substitute. So we drive the collar down a prescribed axial path and read the
ACTUAL geometry at each step — the engaged overlap (collar∩gear) and the interleave
both-occupied fraction — to tell a clean CATCH from a JAM:

  * GOOD  (half-pitch collar): overlap climbs 0 -> ~4.6 mm³ (teeth slot into the gaps),
          both-occupied stays ≈0. It catches.
  * BAD   (in-phase collar, real gaps): teeth-on-teeth — overlap spikes into the 100s of
          mm³ and both-occupied jumps to ≈0.46. It jams; it cannot slide home.

Outputs (artifacts/): dog_clutch_slide.gif (the GOOD slide-and-catch on the real metal),
dog_clutch_slide_filmstrip.png, dog_clutch_catch_vs_jam.png (the discriminating curves).

  .venv/bin/python3 scratch/dog_clutch_slide_sim.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))
from driftpin import Worker          # noqa: E402
import sim_video as sv               # noqa: E402
import numpy as np                   # noqa: E402
import matplotlib                    # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt      # noqa: E402

ART = REPO / "artifacts"
M = 2.0
SR, GH, ND, RDOG = 5.0, 6.0, 6, 7.5     # shaft r, gear face, dogs, dog pitch radius
N = 18                                    # frames / slide samples (each = 2 booleans)
START = 8.0                                # collar starts START mm above the engaged pose

BUILD = r"""
import Part, math, Mesh
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
from driftpin import realize
doc = App.ActiveDocument
SR, GH, ND, RDOG, N, START = %f, %f, %d, %f, %d, %f
og = doc.getObject(%r).Shape
gear_stl, good_stl = %r, %r

def dbore(solid, r, h, z0, depth=1.0):
    return solid.cut(Part.makeCylinder(r, h, Vector(0,0,z0)).cut(
        Part.makeBox(60,60,h+2, Vector(r-depth,-30,z0-1))))
def dogs(radius, z0, h, phase):
    out=[]; tw=0.45*(2*math.pi*radius/ND)
    for k in range(ND):
        th=phase+2*math.pi*k/ND
        b=Part.makeBox(3.0,tw,h,Vector(-1.5,-tw/2,0))
        b.Placement=Placement(Vector(radius*math.cos(th),radius*math.sin(th),z0),
                              Rotation(Vector(0,0,1),math.degrees(th)))
        out.append(b)
    return out

# freewheeling gear: round bore, body z[0,GH], dog teeth up z[GH,GH+4], phase 0
gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))
gear = gear.fuse(dogs(RDOG, GH, 4.0, 0.0))
# collar: sleeve raised above the dog band; dogs z[GH,GH+4.5]. half-pitch=catch, 0=jam
def collar(phase):
    c = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)
    return c.fuse(dogs(RDOG, GH, 4.5, phase))
good = collar(math.pi/ND); bad = collar(0.0)

# export the gear + the ENGAGED good collar for rendering (host translates the collar up)
Mesh.Mesh(gear.tessellate(0.3)).write(gear_stl)
Mesh.Mesh(good.tessellate(0.3)).write(good_stl)

def series(c):
    ov=[]; both=[]
    for i in range(N):
        dz = START*(1.0 - i/(N-1))                 # START (clear, up) down to 0 (engaged)
        ct = c.copy(); ct.translate(Vector(0,0,dz))
        try: v = ct.common(gear).Volume
        except Exception: v = 0.0
        # interleave both-occupied at the gear's dog band [GH+1, GH+3]
        prof = realize.interleave_profile(ct, gear, RDOG, GH+1.0, GH+3.0, n_samples=180)
        ov.append(round(v,3)); both.append(prof["both_fraction"])
    return ov, both

g_ov, g_both = series(good)
b_ov, b_both = series(bad)
__result__ = {"dz":[round(START*(1.0-i/(N-1)),3) for i in range(N)],
              "good_overlap":g_ov, "good_both":g_both,
              "bad_overlap":b_ov, "bad_both":b_both}
"""


def main():
    ART.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        gear_stl = str(Path(d) / "gear.stl")
        good_stl = str(Path(d) / "good.stl")
        with Worker() as w:
            w.call("new_document", name="slide")
            g = w.call("add_gear", teeth=24, module=M, height=GH, name="g24")
            res = w.call("run_script", _timeout=600.0, code=BUILD % (
                SR, GH, ND, RDOG, N, START, g["name"], gear_stl, good_stl))["result"]
        gv, gn = sv.read_binary_stl(gear_stl)
        cv, cn = sv.read_binary_stl(good_stl)     # collar at engaged (dz=0) pose

    dz = res["dz"]
    g_ov, g_both = res["good_overlap"], res["good_both"]
    # fixed camera box: gear + collar at its highest (dz=START) pose
    ctr, rad = sv.bounds_of([gv, cv + np.array([0, 0, START])])

    images = []
    for i in range(N):
        caught = g_ov[i] > 1.0 and g_both[i] < 0.12      # engaged AND interleaved cleanly
        col = sv.GREEN if caught else sv.GOLD
        collar_verts = cv + np.array([0, 0, dz[i]])
        if caught:
            state = f"CAUGHT — teeth interleave (oracle both-occupied {g_both[i]:.2f})"
        elif g_ov[i] > 0.3:
            state = "engaging — teeth entering the gaps"
        else:
            state = "disengaged — collar clear of the gear"
        sub = (f"slide {START - dz[i]:4.1f} / {START:.0f} mm     "
               f"overlap {g_ov[i]:6.1f} mm³     {state}")
        images.append(sv.frame(
            [{"verts": gv, "normals": gn, "color": sv.STEEL},
             {"verts": collar_verts, "normals": cn, "color": col}],
            ctr, rad, title="dog-clutch slide-and-catch — real CAD", subtitle=sub))

    gif = sv.encode_gif(images, ART / "dog_clutch_slide", fps=9, hold_last=8)
    strip = sv.filmstrip(images, ART / "dog_clutch_slide_filmstrip.png",
                         picks=[0, N // 3, 2 * N // 3, N - 2, N - 1])

    # the discriminating curves: catch (GOOD) vs jam (BAD)
    travel = [START - z for z in dz]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2))
    a1.plot(travel, g_ov, "-o", ms=3, color="#1a7", label="GOOD (half-pitch)")
    a1.plot(travel, res["bad_overlap"], "-s", ms=3, color="#c33", label="BAD (in phase)")
    a1.set_title("engaged overlap vs slide"); a1.set_xlabel("collar travel (mm)")
    a1.set_ylabel("collar∩gear overlap (mm³)"); a1.legend(); a1.grid(alpha=0.3)
    a2.plot(travel, g_both, "-o", ms=3, color="#1a7", label="GOOD (half-pitch)")
    a2.plot(travel, res["bad_both"], "-s", ms=3, color="#c33", label="BAD (in phase)")
    a2.axhline(0.12, ls="--", color="#888", label="interleave gate (0.12)")
    a2.set_title("interleave both-occupied vs slide"); a2.set_xlabel("collar travel (mm)")
    a2.set_ylabel("both-occupied fraction"); a2.legend(); a2.grid(alpha=0.3)
    fig.suptitle("slide-and-catch on the real CAD: a clean catch vs a teeth-on-teeth jam")
    fig.tight_layout()
    curves = ART / "dog_clutch_catch_vs_jam.png"
    fig.savefig(str(curves), dpi=110); plt.close(fig)

    print("== dog-clutch slide-and-catch (real CAD, §11.10 oracle overlay) ==")
    print(f"  GOOD final: overlap={g_ov[-1]} mm³  both-occupied={g_both[-1]}  -> CATCH")
    print(f"  BAD  final: overlap={res['bad_overlap'][-1]} mm³  "
          f"both-occupied={res['bad_both'][-1]}  -> JAM")
    print(f"  GIF       -> {gif}  ({gif.stat().st_size:,} bytes, {N} frames)")
    print(f"  filmstrip -> {strip}")
    print(f"  curves    -> {curves}")


if __name__ == "__main__":
    main()

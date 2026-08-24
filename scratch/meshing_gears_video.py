"""MBD trajectory -> review video, on the REAL exported gears (simulation-video-capture
kickoff, item A — the open half: feed a real `mechanism_simulate_submit` run through the
sim_video pipeline).

Two involute spur gears (12T pinion, 24T gear) are built as real solids, meshed at their
pitch-radius sum, and spun by PyBullet through `mbd.run_mbd` — the exact executor
`mechanism_simulate_submit` delegates to — with the new `gears` coupling and `orientations`
output. (The dynamics run in the venv, not the Worker: the FreeCAD worker's python has no
pybullet wheel — the repo's solver-family split — so geometry comes from the Worker and the
solve from here.) The recording places each gear's real mesh at the per-link world
placement the solver returned and encodes a GIF.

Why this needed a solver change: a gear's COM sits on its own spin axis, so the
`trajectories` (COM positions) are stationary — the spin is ONLY legible from the new
`orientations` (per-link world quaternion). Position-only data renders two frozen gears;
the orientation track makes them turn. The §-style oracle overlaid on every frame is the
gear-train ratio: the measured ω_out/ω_in must realise the declared −Na/Nb.

  GOOD: 12T drives 24T -> output turns at −1/2 input, opposite sense. Measured ≈ −0.500.

Outputs (artifacts/): meshing_gears_spin.gif (real metal turning at the declared ratio),
meshing_gears_filmstrip.png, meshing_gears_ratio.png (recovered angle-vs-time, slopes =
the ratio).

  .venv/bin/python3 scratch/meshing_gears_video.py
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))
from ankusdrive import Worker          # noqa: E402
from ankusdrive.analysis import mbd    # noqa: E402
import sim_video as sv               # noqa: E402
import numpy as np                   # noqa: E402
import matplotlib                    # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt      # noqa: E402

ART = REPO / "artifacts"
M = 2.0
NA, NB = 12, 24                       # pinion (driver) / gear (driven) tooth counts
H = 6.0                               # gear face width (mm)
BORE = 3.0                            # shaft-bore radius (mm)
RATE_DPS = 360.0                      # 1 rev/s on the pinion
DURATION = 2.0                        # pinion 2 rev, gear 1 rev -> clean loop
GEAR_PHASE_DEG = 360.0 / NB / 2.0     # half-tooth offset so a tooth faces a valley
BACKLASH = 0.4                        # render-only mesh clearance (mm), as real gears have

# build the two real gears (already added by add_gear), cut a shaft bore, export each
# centred on its own axis to a binary STL the recorder reads.
BUILD = r"""
import Part, Mesh
import FreeCAD as App
from FreeCAD import Vector
doc = App.ActiveDocument
BORE, H = %f, %f
for name, stl in (%r, %r), (%r, %r):
    g = doc.getObject(name).Shape
    g = g.cut(Part.makeCylinder(BORE, H + 2, Vector(0, 0, -1)))
    Mesh.Mesh(g.tessellate(0.2)).write(stl)
__result__ = {"ok": True}
"""


def si_spec(C_mm, tip_a, tip_b):
    """The SI rigid-link spec mechanism_simulate_submit builds (mm/g -> m/kg), with the
    `gears` coupling the handler now forwards to run_mbd. Two revolute shafts on Z, the
    gear meshing the pinion at centre distance C."""
    def shaft(tip_mm, mass_g, x_mm):
        return {"half_extents_m": [tip_mm / 1000.0, tip_mm / 1000.0, H / 2 / 1000.0],
                "mass_kg": mass_g / 1000.0, "parent": -1, "joint_type": "revolute",
                "joint_axis": [0, 0, 1], "joint_pos_m": [x_mm / 1000.0, 0, 0],
                "com_m": [0, 0, 0]}
    return {
        "base": {"half_extents_m": [0.005, 0.005, 0.005], "mass_kg": 0.0, "pos_m": [0, 0, 0]},
        "links": [{"name": "pinion", **shaft(tip_a, 60.0, 0.0)},
                  {"name": "gear", **shaft(tip_b, 240.0, C_mm)}],
        "drivers": [{"link": 0, "target_velocity": np.radians(RATE_DPS), "max_force": 5.0}],
        "gears": [{"link_a": 0, "link_b": 1, "ratio": NB / NA, "axis": [0, 0, 1],
                   "max_force": 2000.0}],
    }


def zrot(verts, normals, deg):
    """Pre-rotate a mesh about Z (mesh stays on its axis) — used for the half-tooth
    meshing phase offset, applied once at load."""
    return sv.place(verts, normals, quat=(0, 0, np.sin(np.radians(deg) / 2),
                                          np.cos(np.radians(deg) / 2)))


def recovered_angle(orn):
    """Unwrapped Z-rotation (rad) over time from a list of world quaternions."""
    a = np.array([2.0 * np.arctan2(q[2], q[3]) for q in orn])
    return np.unwrap(a)


def main():
    ART.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        pin_stl = str(Path(d) / "pinion.stl")
        gear_stl = str(Path(d) / "gear.stl")
        with Worker() as w:
            w.call("new_document", name="mesh_gears")
            ga = w.call("add_gear", teeth=NA, module=M, height=H, name="pinion")
            gb = w.call("add_gear", teeth=NB, module=M, height=H, name="gear")
            w.call("run_script", _timeout=600.0, code=BUILD % (
                BORE, H, ga["name"], pin_stl, gb["name"], gear_stl))
            C = ga["pitch_radius"] + gb["pitch_radius"]      # mesh centre distance (mm)
            tip_a, tip_b = ga["tip_radius"], gb["tip_radius"]

        pv, pn = sv.read_binary_stl(pin_stl)
        gv, gn = sv.read_binary_stl(gear_stl)

    # --- the real dynamics (run_mbd is what the submit handler delegates to) -----
    res = mbd.run_mbd(si_spec(C, tip_a, tip_b), duration_s=DURATION)

    # half-tooth meshing phase so teeth interleave instead of colliding tip-to-tip
    gv, gn = zrot(gv, gn, GEAR_PHASE_DEG)

    p_traj, p_orn = res["trajectories"]["pinion"], res["orientations"]["pinion"]
    g_traj, g_orn = res["trajectories"]["gear"], res["orientations"]["gear"]
    nF = len(p_orn)

    # --- ratio oracle: measured omega_out/omega_in vs declared -Na/Nb ----------
    declared = -NA / NB
    wv = res["mean_joint_velocity"]
    measured = wv["gear"] / wv["pinion"] if wv["pinion"] else float("nan")
    ratio_ok = abs(measured - declared) < 0.02
    verdict = (f"ratio ω_out/ω_in = {measured:+.3f}  vs declared −Na/Nb = {declared:+.3f}"
               f"  {'✓ realised' if ratio_ok else '✗ MISMATCH'}")

    # fixed camera box from a few placed poses (gears are ~axisymmetric, so any pose
    # bounds the rest; sample a handful to be safe)
    poses = []
    for i in (0, nF // 3, 2 * nF // 3, nF - 1):
        poses.append(sv.place(pv, pn, pos_m=p_traj[i], quat=p_orn[i])[0])
        poses.append(sv.place(gv, gn, pos_m=g_traj[i], quat=g_orn[i])[0])
    ctr, rad = sv.bounds_of(poses)

    pa_all, ga_all = recovered_angle(p_orn), recovered_angle(g_orn)
    images = []
    for i in range(nF):
        pvi, pni = sv.place(pv, pn, pos_m=p_traj[i], quat=p_orn[i])
        gvi, gni = sv.place(gv, gn, pos_m=g_traj[i], quat=g_orn[i])
        gvi = gvi + np.array([BACKLASH, 0, 0])     # render-only hair of backlash
        col = sv.GREEN if ratio_ok else sv.RED
        sub = (f"pinion {np.degrees(pa_all[i]):6.0f}°    "
               f"gear {np.degrees(ga_all[i]):6.0f}°     {verdict}")
        images.append(sv.frame(
            [{"verts": pvi, "normals": pni, "color": sv.GOLD},
             {"verts": gvi, "normals": gni, "color": col}],
            ctr, rad, title=f"meshing gears {NA}T→{NB}T — real CAD, MBD-driven",
            subtitle=sub, elev=34, azim=-62))      # 3/4 view: real 3-D metal, spin visible

    gif = sv.encode_gif(images, ART / "meshing_gears_spin", fps=12, hold_last=0)
    strip = sv.filmstrip(images, ART / "meshing_gears_filmstrip.png",
                         picks=[0, nF // 4, nF // 2, 3 * nF // 4, nF - 1])

    # the ratio, read off the recovered angles: slopes are the rates
    pa, gpa = recovered_angle(p_orn), recovered_angle(g_orn)
    t = np.linspace(0, DURATION, nF)
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ax.plot(t, np.degrees(pa), "-o", ms=2.5, color="#b8860b", label="pinion (12T, driver)")
    ax.plot(t, np.degrees(gpa), "-s", ms=2.5, color="#1a7", label="gear (24T, driven)")
    ax.set_xlabel("time (s)"); ax.set_ylabel("rotation (deg)")
    ax.set_title(f"recovered spin — slope ratio {measured:+.3f} = declared −Na/Nb "
                 f"{declared:+.3f}")
    ax.legend(); ax.grid(alpha=0.3)
    fig.tight_layout()
    curves = ART / "meshing_gears_ratio.png"
    fig.savefig(str(curves), dpi=120); plt.close(fig)

    print("== meshing gears: MBD trajectory -> video on the real CAD ==")
    print(f"  centre distance   : {C:.1f} mm (pitch {ga['pitch_radius']}+{gb['pitch_radius']})")
    print(f"  mean ω pinion/gear: {wv['pinion']:.3f} / {wv['gear']:.3f} rad/s")
    print(f"  ratio measured    : {measured:+.4f}   declared −Na/Nb = {declared:+.4f}"
          f"   -> {'REALISED' if ratio_ok else 'MISMATCH'}")
    print(f"  GIF       -> {gif}  ({gif.stat().st_size:,} bytes, {nF} frames)")
    print(f"  filmstrip -> {strip}")
    print(f"  ratio     -> {curves}")


if __name__ == "__main__":
    main()

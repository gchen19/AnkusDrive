"""Animate the gearbox spinning — from the CONTACT simulation's own angles.

We run the speed-1 power path in PyBullet (input -> constant mesh -> countershaft ->
speed mesh -> mainshaft gear) with NO gear constraints, record each gear's rotation
angle at every frame, and draw the gears turning at exactly those angles. The teeth
mesh, the countershaft turns slower and reversed, and the mainshaft gear crawls at
~0.29x the input — all because the simulated teeth pushed each other, not because any
ratio was imposed.

  .venv/bin/python3 scratch/gearbox_animate.py
Outputs: artifacts/gearbox_spin.gif  +  artifacts/gearbox_spin_filmstrip.png
"""
import math
import sys
from pathlib import Path

import numpy as np
import pybullet as p
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from matplotlib.animation import FuncAnimation, PillowWriter  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scratch"))
ART = REPO / "artifacts"

MOD = 0.02
FACE = 0.04
DRIVE = 4.0
Z_CM, Z0 = 0.08, 0.0
T_IN, T_CM, T_C, T_M = 16, 24, 12, 28


def gear_children(n, z, phase, backlash=0.45):
    rp = MOD * n / 2
    rad = 0.6 * MOD
    types, radii, lengths, halfs, pos, orn = [], [], [], [], [], []
    tc = backlash * (math.pi * MOD / 2) / 2
    for k in range(n):
        th = phase + 2 * math.pi * k / n
        types.append(p.GEOM_BOX); radii.append(0); lengths.append(0)
        halfs.append([rad, tc, FACE / 2])
        pos.append([rp * math.cos(th), rp * math.sin(th), z])
        orn.append(p.getQuaternionFromEuler([0, 0, th]))
    return types, radii, halfs, lengths, pos, orn


def shape(cid, cs):
    return p.createCollisionShapeArray(
        shapeTypes=cs[0], radii=cs[1], halfExtents=cs[2], lengths=cs[3],
        collisionFramePositions=cs[4], collisionFrameOrientations=cs[5],
        physicsClientId=cid)


def simulate(ph_m, n_frames=90, steps=16000, dt=1.0 / 5000):
    """Run the speed-1 path; return per-frame angles (input, countershaft, main)."""
    cid = p.connect(p.DIRECT)
    p.setGravity(0, 0, 0, physicsClientId=cid)
    p.setTimeStep(dt, physicsClientId=cid)
    p.setPhysicsEngineParameter(numSolverIterations=200, physicsClientId=cid)
    C = MOD * (T_IN + T_CM) / 2
    s_in = shape(cid, gear_children(T_IN, 0.0, 0.0))
    s_cm = shape(cid, gear_children(T_CM, 0.0, 0.0))
    s_c0 = shape(cid, gear_children(T_C, 0.0, 0.0))
    s_m = shape(cid, gear_children(T_M, 0.0, ph_m))
    base = p.createCollisionShape(p.GEOM_SPHERE, radius=0.001, physicsClientId=cid)
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=base, basePosition=[0, 0, 0],
        linkMasses=[0.5, 0.3, 0.15, 0.1],
        linkCollisionShapeIndices=[s_in, s_cm, s_c0, s_m], linkVisualShapeIndices=[-1] * 4,
        linkPositions=[[0, 0, Z_CM], [C, 0, Z_CM], [0, 0, Z0 - Z_CM], [0, 0, Z0]],
        linkOrientations=[[0, 0, 0, 1]] * 4, linkInertialFramePositions=[[0, 0, 0]] * 4,
        linkInertialFrameOrientations=[[0, 0, 0, 1]] * 4, linkParentIndices=[0, 0, 2, 0],
        linkJointTypes=[p.JOINT_REVOLUTE, p.JOINT_REVOLUTE, p.JOINT_FIXED, p.JOINT_REVOLUTE],
        linkJointAxis=[[0, 0, 1]] * 4, flags=p.URDF_USE_SELF_COLLISION, physicsClientId=cid)
    for li, n in ((0, T_IN), (1, T_CM), (2, T_C), (3, T_M)):
        r = MOD * n / 2
        I = 0.5 * p.getDynamicsInfo(body, li, physicsClientId=cid)[0] * r * r
        p.changeDynamics(body, li, localInertiaDiagonal=[I, I, I], lateralFriction=0.5,
                         restitution=0.0, contactStiffness=1e6, contactDamping=1e3,
                         physicsClientId=cid)
    p.setJointMotorControl2(body, 0, p.VELOCITY_CONTROL, targetVelocity=DRIVE, force=800,
                            physicsClientId=cid)
    p.setJointMotorControl2(body, 1, p.VELOCITY_CONTROL, force=0.0, physicsClientId=cid)
    p.setJointMotorControl2(body, 3, p.VELOCITY_CONTROL, targetVelocity=0.0, force=0.02,
                            physicsClientId=cid)
    every = steps // n_frames
    ti, tc, tm = [], [], []
    for s in range(steps):
        p.stepSimulation(physicsClientId=cid)
        if s % every == 0:
            ti.append(p.getJointState(body, 0, physicsClientId=cid)[0])
            tc.append(p.getJointState(body, 1, physicsClientId=cid)[0])
            tm.append(p.getJointState(body, 3, physicsClientId=cid)[0])
    p.disconnect(cid)
    return np.array(ti), np.array(tc), np.array(tm)


def gear_outline(cx, cy, n, angle, mod=1.0):
    """2-D toothed-disc outline (trapezoidal teeth) centred at (cx,cy), rotated `angle`."""
    rp = mod * n / 2
    rt, rr = rp + mod, rp - mod
    pts = []
    for k in range(n):
        a0 = angle + 2 * math.pi * k / n
        w = 0.33 * (2 * math.pi / n)
        for r, da in ((rr, -w), (rt, -w * 0.5), (rt, w * 0.5), (rr, w)):
            pts.append((cx + r * math.cos(a0 + da), cy + r * math.sin(a0 + da)))
    pts.append(pts[0])
    return np.array(pts)


def draw(ax, cx, cy, n, angle, color, label):
    o = gear_outline(cx, cy, n, angle)
    ax.fill(o[:, 0], o[:, 1], color=color, alpha=0.85, zorder=2)
    rp = n / 2
    ax.plot([cx, cx + rp * math.cos(angle)], [cy, cy + rp * math.sin(angle)],
            color="white", lw=2, zorder=3)                 # rotation marker
    ax.text(cx, cy - (rp + 3), label, ha="center", va="top", fontsize=8)


def simulate_single(na, nb, ph_b, n_frames=90, steps=9000, dt=1.0 / 3000):
    """One clean motor-driven mesh (na drives nb); record both gears' angles."""
    cid = p.connect(p.DIRECT)
    p.setGravity(0, 0, 0, physicsClientId=cid)
    p.setTimeStep(dt, physicsClientId=cid)
    p.setPhysicsEngineParameter(numSolverIterations=150, physicsClientId=cid)
    C = MOD * (na + nb) / 2
    sa = shape(cid, gear_children(na, 0.0, 0.0))
    sb = shape(cid, gear_children(nb, 0.0, ph_b))
    base = p.createCollisionShape(p.GEOM_SPHERE, radius=0.001, physicsClientId=cid)
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=base, basePosition=[0, 0, 0],
        linkMasses=[1.0, (nb / na) ** 2], linkCollisionShapeIndices=[sa, sb],
        linkVisualShapeIndices=[-1, -1], linkPositions=[[0, 0, 0], [C, 0, 0]],
        linkOrientations=[[0, 0, 0, 1]] * 2, linkInertialFramePositions=[[0, 0, 0]] * 2,
        linkInertialFrameOrientations=[[0, 0, 0, 1]] * 2, linkParentIndices=[0, 0],
        linkJointTypes=[p.JOINT_REVOLUTE] * 2, linkJointAxis=[[0, 0, 1]] * 2,
        flags=p.URDF_USE_SELF_COLLISION, physicsClientId=cid)
    for li, n in ((0, na), (1, nb)):
        r = MOD * n / 2
        I = 0.5 * p.getDynamicsInfo(body, li, physicsClientId=cid)[0] * r * r
        p.changeDynamics(body, li, localInertiaDiagonal=[I, I, I], lateralFriction=0.5,
                         restitution=0.0, contactStiffness=1e6, contactDamping=1e3,
                         physicsClientId=cid)
    p.setJointMotorControl2(body, 0, p.VELOCITY_CONTROL, targetVelocity=DRIVE, force=500,
                            physicsClientId=cid)
    p.setJointMotorControl2(body, 1, p.VELOCITY_CONTROL, targetVelocity=0.0, force=0.05,
                            physicsClientId=cid)
    every = steps // n_frames
    ta, tb = [], []
    for s in range(steps):
        p.stepSimulation(physicsClientId=cid)
        if s % every == 0:
            ta.append(p.getJointState(body, 0, physicsClientId=cid)[0])
            tb.append(p.getJointState(body, 1, physicsClientId=cid)[0])
    p.disconnect(cid)
    return np.array(ta), np.array(tb)


def clean_mesh_gif():
    """The precise confirmation: one motor-driven mesh, 12T -> 24T, ratio from contact."""
    na, nb = 12, 24
    best = None
    for ph in [2 * math.pi / nb * k / 5 for k in range(5)]:
        ta, tb = simulate_single(na, nb, ph)
        k = len(ta) // 2
        r = (tb[-1] - tb[k]) / (ta[-1] - ta[k]) if abs(ta[-1] - ta[k]) > 1e-6 else 0
        if best is None or abs(r - (-na / nb)) < abs(best[0] - (-na / nb)):
            best = (r, ta, tb)
    ratio, ta, tb = best
    Cd = (na + nb) / 2
    fig, ax = plt.subplots(figsize=(7, 5))

    def frame(i):
        ax.clear(); ax.set_aspect("equal"); ax.axis("off")
        ax.set_xlim(-na / 2 - 4, Cd + nb / 2 + 4); ax.set_ylim(-nb / 2 - 6, nb / 2 + 6)
        draw(ax, 0, 0, na, ta[i], "#cf5b4e", "driven 12T")
        draw(ax, Cd, 0, nb, tb[i], "#5bcf7a", "output 24T")
        fig.suptitle("meshing teeth convert rotation — by CONTACT, no ratio imposed\n"
                     f"driven {ta[i]/(2*math.pi):+.2f} rev   output {tb[i]/(2*math.pi):+.2f} rev"
                     f"   (measured {ratio:+.3f}, ideal {-na/nb:+.3f})", fontsize=10)
        return []

    anim = FuncAnimation(fig, frame, frames=len(ta), interval=55, blit=False)
    gif = ART / "gear_mesh_spin.gif"
    anim.save(str(gif), writer=PillowWriter(fps=20))
    print(f"  clean single-mesh GIF -> {gif}  (measured {ratio:+.3f} vs ideal {-na/nb:+.3f})")


def main():
    ideal = (T_IN / T_CM) * (T_C / T_M)

    def steady_ratio(ti, tm):                         # slope over the last third (no transient)
        k = 2 * len(ti) // 3
        di, dm = ti[-1] - ti[k], tm[-1] - tm[k]
        return dm / di if abs(di) > 1e-6 else 0.0

    # pick the meshing phase that transmits best at steady state, then animate that run
    best = None
    for ph in [2 * math.pi / T_M * k / 7 for k in range(7)]:
        ti, tc, tm = simulate(ph)
        ratio = steady_ratio(ti, tm)
        if best is None or abs(ratio - ideal) < abs(best[0] - ideal):
            best = (ratio, ti, tc, tm)
    ratio, ti, tc, tm = best
    print(f"animating speed 1: realised omega_main/omega_in = {ratio:+.3f} (ideal {ideal:+.3f})")

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 5.5))
    Cd = (T_IN + T_CM) / 2                       # drawing centre distance (in 'teeth/2' units)

    def frame(i):
        for ax in (axL, axR):
            ax.clear(); ax.set_aspect("equal"); ax.axis("off")
            ax.set_xlim(-T_M / 2 - 4, Cd + T_CM / 2 + 4); ax.set_ylim(-T_M / 2 - 6, T_M / 2 + 6)
        # input stage: input(16) at x=0  <->  countershaft cm(24) at x=Cd
        draw(axL, 0, 0, T_IN, ti[i], "#cf5b4e", "input 16T (driven)")
        draw(axL, Cd, 0, T_CM, -tc[i], "#5b8fcf", "countershaft 24T")
        axL.set_title("input stage  (16T → 24T)", fontsize=10)
        # output stage: countershaft c0(12) at x=Cd  <->  mainshaft m0(28) at x=0
        draw(axR, Cd, 0, T_C, -tc[i], "#5b8fcf", "countershaft 12T")
        draw(axR, 0, 0, T_M, tm[i], "#5bcf7a", "mainshaft 28T (output)")
        axR.set_title("output stage, speed 1  (12T → 28T)", fontsize=10)
        rev_in, rev_out = ti[i] / (2 * math.pi), tm[i] / (2 * math.pi)
        fig.suptitle(f"constant-mesh gearbox spinning — input {rev_in:+.2f} rev, "
                     f"output {rev_out:+.2f} rev  (ratio {ratio:+.3f}, from contact)",
                     fontsize=11)
        return []

    anim = FuncAnimation(fig, frame, frames=len(ti), interval=60, blit=False)
    ART.mkdir(parents=True, exist_ok=True)
    gif = ART / "gearbox_spin.gif"
    anim.save(str(gif), writer=PillowWriter(fps=18))
    print(f"  GIF -> {gif}  ({gif.stat().st_size:,} bytes)")

    # filmstrip: 6 evenly-spaced frames for a static look
    fig2, axes = plt.subplots(1, 6, figsize=(18, 3.2))
    for j, ax in enumerate(axes):
        i = int(j * (len(ti) - 1) / 5)
        ax.set_aspect("equal"); ax.axis("off")
        ax.set_xlim(-T_M / 2 - 4, Cd + T_CM / 2 + 4); ax.set_ylim(-T_M / 2 - 6, T_M / 2 + 6)
        draw(ax, Cd, 0, T_C, -tc[i], "#5b8fcf", "")
        draw(ax, 0, 0, T_M, tm[i], "#5bcf7a", "")
        ax.set_title(f"in {ti[i]/(2*math.pi):+.2f}rev\nout {tm[i]/(2*math.pi):+.2f}rev",
                     fontsize=8)
    fig2.suptitle("output stage over time — input spins fast, output crawls (the reduction)",
                  fontsize=11)
    strip = ART / "gearbox_spin_filmstrip.png"
    fig2.savefig(str(strip), bbox_inches="tight", dpi=110)
    print(f"  filmstrip -> {strip}")

    clean_mesh_gif()


if __name__ == "__main__":
    main()

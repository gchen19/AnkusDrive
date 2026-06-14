"""Honest gear-mesh test: do two real toothed gears transmit rotation by CONTACT?

The earlier Tier-2 "sim" imposed a JOINT_GEAR constraint at the ratio and read it
back — circular, couldn't fail. This builds two gears with actual teeth (a hub
cylinder + N tooth boxes as a compound collision shape), pins each to a revolute
axis at the correct centre distance, DRIVES one, and applies NO gear constraint. If
the second gear turns at -Na/Nb of the first, the conversion emerges from tooth
contact — the real thing. If they jam, that is an honest negative.

  .venv/bin/python3 scratch/gear_contact_sim.py
"""
import math
import sys

import pybullet as p

MOD = 0.02            # module (m) — big gears so contact is robust; ratio is N-only
FACE = 0.06           # face width (m)
DRIVE = 4.0           # input angular velocity (rad/s)


def _gear_shape(cid, n_teeth, phase=0.0, backlash=0.45):
    """Compound collision shape: a root-radius hub cylinder + n radial tooth boxes."""
    rp = MOD * n_teeth / 2.0
    add, ded = MOD, 1.0 * MOD                       # addendum / (simplified) dedendum
    hub_r = rp - ded
    tooth_rad_half = (add + ded) / 2.0              # radial half-length of a tooth box
    tooth_circ_half = backlash * (math.pi * MOD / 2.0) / 2.0   # < half pitch (backlash)
    types, radii, halfs, lengths, positions, orients = [], [], [], [], [], []
    # hub
    types.append(p.GEOM_CYLINDER); radii.append(hub_r); lengths.append(FACE)
    halfs.append([0, 0, 0]); positions.append([0, 0, 0]); orients.append([0, 0, 0, 1])
    # teeth
    for k in range(n_teeth):
        th = phase + 2.0 * math.pi * k / n_teeth
        r_mid = rp                                  # box centred on the pitch circle
        types.append(p.GEOM_BOX); radii.append(0); lengths.append(0)
        halfs.append([tooth_rad_half, tooth_circ_half, FACE / 2.0])
        positions.append([r_mid * math.cos(th), r_mid * math.sin(th), 0])
        orients.append(p.getQuaternionFromEuler([0, 0, th]))
    return p.createCollisionShapeArray(
        shapeTypes=types, radii=radii, halfExtents=halfs, lengths=lengths,
        collisionFramePositions=positions, collisionFrameOrientations=orients,
        physicsClientId=cid)


def run(na, nb, phase_b, steps=6000, dt=1.0 / 2000):
    cid = p.connect(p.DIRECT)
    p.setGravity(0, 0, 0, physicsClientId=cid)
    p.setTimeStep(dt, physicsClientId=cid)
    rp_a, rp_b = MOD * na / 2, MOD * nb / 2
    C = rp_a + rp_b
    shp_a = _gear_shape(cid, na, phase=0.0)
    shp_b = _gear_shape(cid, nb, phase=phase_b)
    base = p.createCollisionShape(p.GEOM_SPHERE, radius=0.001, physicsClientId=cid)
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=base, basePosition=[0, 0, 0],
        linkMasses=[1.0, (nb / na) ** 2],
        linkCollisionShapeIndices=[shp_a, shp_b], linkVisualShapeIndices=[-1, -1],
        linkPositions=[[0, 0, 0], [C, 0, 0]], linkOrientations=[[0, 0, 0, 1]] * 2,
        linkInertialFramePositions=[[0, 0, 0]] * 2,
        linkInertialFrameOrientations=[[0, 0, 0, 1]] * 2,
        linkParentIndices=[0, 0], linkJointTypes=[p.JOINT_REVOLUTE] * 2,
        linkJointAxis=[[0, 0, 1], [0, 0, 1]],
        flags=p.URDF_USE_SELF_COLLISION, physicsClientId=cid)
    # the two gears are sibling links (both parented to the base) -> non-adjacent, so
    # self-collision lets their teeth actually touch.
    p.setPhysicsEngineParameter(numSolverIterations=150, physicsClientId=cid)
    for li, r in ((0, rp_a), (1, rp_b)):
        Iz = 0.5 * p.getDynamicsInfo(body, li, physicsClientId=cid)[0] * r * r
        p.changeDynamics(body, li, localInertiaDiagonal=[Iz, Iz, Iz],
                         lateralFriction=0.5, restitution=0.0,
                         contactStiffness=1e6, contactDamping=1e3, physicsClientId=cid)
    # drive A at constant velocity; leave B free (no motor)
    p.setJointMotorControl2(body, 0, p.VELOCITY_CONTROL, targetVelocity=DRIVE,
                            force=500.0, physicsClientId=cid)
    p.setJointMotorControl2(body, 1, p.VELOCITY_CONTROL, targetVelocity=0.0,
                            force=0.05, physicsClientId=cid)   # light drag keeps flanks loaded
    tail = []
    for s in range(steps):
        p.stepSimulation(physicsClientId=cid)
        if s > steps * 0.7:
            tail.append((p.getJointState(body, 0, physicsClientId=cid)[1],
                         p.getJointState(body, 1, physicsClientId=cid)[1]))
    p.disconnect(cid)
    wa = sum(t[0] for t in tail) / len(tail)
    wb = sum(t[1] for t in tail) / len(tail)
    return wa, wb


def main():
    na, nb = 12, 24
    ideal = -na / nb
    print(f"== gear-contact test: {na}T drives {nb}T, ideal omega_b/omega_a = {ideal:+.3f} ==")
    print(f"   (drive {DRIVE} rad/s, NO gear constraint — the ratio must emerge from teeth)")
    best = None
    for phase_b in [2 * math.pi / nb * k / 8 for k in range(8)]:   # sweep a full tooth pitch
        wa, wb = run(na, nb, phase_b)
        ratio = wb / wa if abs(wa) > 1e-6 else None
        track = abs(wa) / DRIVE
        print(f"  phase={phase_b:.3f}: omega_a={wa:+.3f} ({track*100:.0f}% of drive)  "
              f"omega_b={wb:+.3f}  ratio={ratio if ratio is None else round(ratio,3)}")
        if ratio is not None and (best is None or
                                  abs(ratio - ideal) < abs(best[1] - ideal)):
            best = (phase_b, ratio, wa, wb)
    if best:
        ph, ratio, wa, wb = best
        ok = abs(ratio - ideal) < 0.1 * abs(ideal) and abs(abs(wa) / DRIVE - 1) < 0.2
        print(f"\n  best phase {ph:.3f}: ratio {ratio:+.3f} vs ideal {ideal:+.3f}  "
              f"-> {'TEETH TRANSMIT (conversion works)' if ok else 'jams / no clean transmission'}")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())

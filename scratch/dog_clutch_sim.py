"""Honest engagement test: does a DOG CLUTCH actually select a freewheeling gear?

The thing the "functional gearbox" was missing in metal. A speed gear freewheels on
the output shaft; a dog collar splined to the shaft slides axially to interlock the
gear's dog teeth and lock it to the shaft. Here the gear's rotation is driven, a
coaxial collar is free, and dog teeth (flat-faced castellations — boxes are the RIGHT
model for these) couple them by CONTACT:

  ENGAGED    collar dogs share the gear dogs' axial band -> collar is driven (~1.0x).
  DISENGAGED collar dogs slid clear in z -> no contact -> collar stays idle (~0x).

No constraint imposes the coupling; it emerges from the dogs touching (or not).

  .venv/bin/python3 scratch/dog_clutch_sim.py
"""
import math
import sys

import pybullet as p

ND = 6                # number of dog teeth
RD = 0.06             # dog pitch radius (m)
DRIVE = 4.0           # gear angular velocity (rad/s)


def _dog_ring(cid, z_center, phase):
    """ND axial dog teeth (boxes) at z_center — no hub (the two coaxial rings must
    couple ONLY through their dogs, never through overlapping hubs)."""
    types, radii, lengths, halfs, pos, orn = [], [], [], [], [], []
    tang_half = 0.45 * (2 * math.pi * RD / ND) / 2.0      # < half pitch -> gaps
    for k in range(ND):
        th = phase + 2 * math.pi * k / ND
        types.append(p.GEOM_BOX); radii.append(0); lengths.append(0)
        halfs.append([0.015, tang_half, 0.012])
        pos.append([RD * math.cos(th), RD * math.sin(th), z_center])
        orn.append(p.getQuaternionFromEuler([0, 0, th]))
    return p.createCollisionShapeArray(
        shapeTypes=types, radii=radii, halfExtents=halfs, lengths=lengths,
        collisionFramePositions=pos, collisionFrameOrientations=orn, physicsClientId=cid)


def run(engaged, steps=5000, dt=1.0 / 2000):
    cid = p.connect(p.DIRECT)
    p.setGravity(0, 0, 0, physicsClientId=cid)
    p.setTimeStep(dt, physicsClientId=cid)
    p.setPhysicsEngineParameter(numSolverIterations=150, physicsClientId=cid)
    # gear dogs at z=+0.02; collar dogs at z=+0.02 (engaged) or z=+0.06 (clear)
    gear = _dog_ring(cid, 0.02, phase=0.0)
    collar = _dog_ring(cid, 0.02 if engaged else 0.06, phase=math.pi / ND)  # half-pitch
    base = p.createCollisionShape(p.GEOM_SPHERE, radius=0.001, physicsClientId=cid)
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=base, basePosition=[0, 0, 0],
        linkMasses=[1.0, 1.0], linkCollisionShapeIndices=[gear, collar],
        linkVisualShapeIndices=[-1, -1], linkPositions=[[0, 0, 0], [0, 0, 0]],
        linkOrientations=[[0, 0, 0, 1]] * 2, linkInertialFramePositions=[[0, 0, 0]] * 2,
        linkInertialFrameOrientations=[[0, 0, 0, 1]] * 2, linkParentIndices=[0, 0],
        linkJointTypes=[p.JOINT_REVOLUTE] * 2, linkJointAxis=[[0, 0, 1]] * 2,
        flags=p.URDF_USE_SELF_COLLISION, physicsClientId=cid)
    for li in (0, 1):
        p.changeDynamics(body, li, localInertiaDiagonal=[1e-3, 1e-3, 2e-3],
                         lateralFriction=0.3, restitution=0.0,
                         contactStiffness=1e6, contactDamping=1e3, physicsClientId=cid)
    p.setJointMotorControl2(body, 0, p.VELOCITY_CONTROL, targetVelocity=DRIVE,
                            force=500.0, physicsClientId=cid)               # drive the gear
    p.setJointMotorControl2(body, 1, p.VELOCITY_CONTROL, targetVelocity=0.0,
                            force=0.02, physicsClientId=cid)                # collar free (light drag)
    tail = []
    for s in range(steps):
        p.stepSimulation(physicsClientId=cid)
        if s > steps * 0.7:
            tail.append(p.getJointState(body, 1, physicsClientId=cid)[1])
    p.disconnect(cid)
    return sum(tail) / len(tail)


def main():
    print(f"== dog-clutch test: gear driven at {DRIVE} rad/s; does the collar follow? ==")
    eng = run(engaged=True)
    dis = run(engaged=False)
    print(f"  ENGAGED    collar omega = {eng:+.3f} rad/s  ({eng/DRIVE*100:+.0f}% of gear)")
    print(f"  DISENGAGED collar omega = {dis:+.3f} rad/s  ({dis/DRIVE*100:+.0f}% of gear)")
    ok = (eng / DRIVE > 0.85) and (abs(dis) / DRIVE < 0.15)
    print(f"\n  -> {'DOG CLUTCH SELECTS (engaged drives, disengaged frees)' if ok else 'clutch did not behave as a selector'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

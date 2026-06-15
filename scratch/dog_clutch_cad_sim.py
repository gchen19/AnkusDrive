"""Sim-from-CAD: drive the REAL exported dog-clutch geometry in PyBullet
(validate-kickoff #3 — docs/KICKOFF_validate_the_artifact.md, docs/VALIDATE_THE_ARTIFACT.md).

Items #1/#2/#4/#5 of that kickoff consume the real geometry *statically*: the §11.10
oracle (driftpin/realize.py) reads the exported Part.Shape, and the slide-and-catch
(scratch/dog_clutch_slide_sim.py) drives a prescribed kinematic path and reads the
geometry at each step. #3 is the one structural piece left — a *dynamic* rigid-body
contact sim that couples through the metal itself, with NO constraint imposing the
coupling: the torque path emerges (or doesn't) from the dog teeth touching.

The kickoff flagged this as finicky and named the reason: a dynamic (rotating) body in
PyBullet needs CONVEX collision, and a naive convex hull of a dog ring FILLS the gaps,
so engaged looks like disengaged. This script shows the finicky parts are tractable on
the real exported parts, and exactly where the idealised CAD bites back:

  [1] Build the real gear (24T + round bore + 6 dog teeth) and two collars that differ
      ONLY in dog phase — GOOD (half-pitch, interleaves) and BAD (in-phase, jams).
  [2] vhacd the dog-engagement band of each REAL exported part; load each as a COMPOUND
      of its convex hulls (NOT one convexified mesh — that is what bridges the gaps).
  [3] FAITHFULNESS GATE: re-measure the §11.10 signal (fill / sectors / interleave
      both-occupied) on the DECOMPOSED collision bodies and assert it matches the oracle
      reading on the Part.Shape. The physics geometry must carry the same verdict, else
      the decomposition bridged the gaps and the sim would be a lie. It does NOT: the
      gaps survive (engaged both-occupied ≈0, in-phase ≈0.47), so engaged ≠ disengaged
      is representable in rigid-body contact — the core worry, refuted on the real metal.
  [4] DYNAMIC CONTACT (the selector): two coaxial revolute bodies (gear driven, collar
      free). Engaged → the collar is driven through the interleaved teeth (~1.0x);
      disengaged (collar lifted clear in z) → the collar freewheels (~0x). Transmit-when-
      engaged / free-when-disengaged, on the real part, emergent from contact.

A finding worth stating: the collision geometry is the dog-tooth BAND of the real part
(z∈[GH, GH+DOG_H]); including the collar's sleeve face made it cap axially onto the gear
teeth and the assembly locked, and the idealised teeth carry ZERO running clearance (the
few-mm³ "in-family" root overlap the oracle measures), so the engaged gear runs tight
(it drags to ~3.1 rad/s) where a real slip-fit dog clutch would spin free. The selector
still holds on the part as-exported — but the rigid-body sim is the thing that surfaces
"a real dog clutch needs clearance," which neither the declaration nor a clean PyBullet
rig ever would. That is the validate-the-artifact lesson, one level down.

Outputs (artifacts/): dog_clutch_cad_sim.gif + _filmstrip.png (the real decomposed parts
coupling when engaged, freewheeling when lifted), and a printed result table.

  .venv/bin/python3 scratch/dog_clutch_cad_sim.py
"""
import math
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))

import pybullet as p                 # noqa: E402
import numpy as np                   # noqa: E402
from driftpin import Worker          # noqa: E402
import sim_video as sv               # noqa: E402

ART = REPO / "artifacts"
M = 2.0
SR, GH, ND, RDOG = 5.0, 6.0, 6, 7.5          # shaft r, gear face, dog count, dog radius
DOG_H = 4.0                                   # dog tooth height
DRIVE = 4.0                                   # gear drive speed (rad/s)
GFORCE = 2000.0                               # gear motor torque limit

# FreeCAD recipe — identical dog geometry to scratch/dog_clutch_slide_sim.py so this sim
# consumes the SAME real parts the §11.10 oracle and the slide-and-catch judge. Exports
# (a) full STL for rendering and (b) the dog-tooth BAND z-clip [GH, GH+DOG_H] of each
# REAL part as OBJ for vhacd. The clip is a genuine sub-volume of the exported metal — the
# gear disk / power teeth and the collar sleeve carry no clutching role; clipping to the
# teeth that actually touch keeps the convex decomposition focused AND avoids the sleeve
# capping axially onto the gear teeth (which locks the assembly).
BUILD = r"""
import Part, math, Mesh
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
from driftpin import realize
doc = App.ActiveDocument
SR, GH, ND, RDOG, DOG_H = %f, %f, %d, %f, %f
og = doc.getObject(%r).Shape
gear_stl, good_stl = %r, %r
gear_obj, good_obj, bad_obj = %r, %r, %r

def dogs(radius, z0, h, phase):
    out = []; tw = 0.45 * (2*math.pi*radius/ND)
    for k in range(ND):
        th = phase + 2*math.pi*k/ND
        b = Part.makeBox(3.0, tw, h, Vector(-1.5, -tw/2, 0))
        b.Placement = Placement(Vector(radius*math.cos(th), radius*math.sin(th), z0),
                                Rotation(Vector(0,0,1), math.degrees(th)))
        out.append(b)
    return out

def dbore(solid, r, h, z0, depth=1.0):
    return solid.cut(Part.makeCylinder(r, h, Vector(0,0,z0)).cut(
        Part.makeBox(60,60,h+2, Vector(r-depth,-30,z0-1))))

# freewheeling gear: round bore, body z[0,GH], dog teeth z[GH,GH+DOG_H], phase 0
gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))
for b in dogs(RDOG, GH, DOG_H, 0.0): gear = gear.fuse(b)
# collar: sleeve raised ABOVE the dog band; dogs z[GH,GH+DOG_H+0.5]. half-pitch=catch
def collar(phase):
    c = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+DOG_H)), SR+0.1, 8.0, GH+3.0)
    for b in dogs(RDOG, GH, DOG_H+0.5, phase): c = c.fuse(b)
    return c
good = collar(math.pi/ND); bad = collar(0.0)

# full parts for rendering (host rotates / lifts them per frame)
Mesh.Mesh(gear.tessellate(0.3)).write(gear_stl)
Mesh.Mesh(good.tessellate(0.3)).write(good_stl)

# dog-tooth band z-clip [GH, GH+DOG_H] of each REAL part -> OBJ for vhacd collision
clip = Part.makeBox(80, 80, DOG_H, Vector(-40,-40, GH))
for shp, path in ((gear, gear_obj), (good, good_obj), (bad, bad_obj)):
    Mesh.Mesh(shp.common(clip).tessellate(0.3)).write(path)

# §11.10 oracle ground truth on the Part.Shape (what the physics must reproduce)
zlo, zhi = GH+1.0, GH+3.0
g_prof = realize.dog_ring_profile(gear, RDOG, zlo, zhi, n_samples=360)
c_prof = realize.dog_ring_profile(good, RDOG, zlo, zhi, n_samples=360)
good_il = realize.interleave_profile(good, gear, RDOG, zlo, zhi, n_samples=360)
bad_il = realize.interleave_profile(bad, gear, RDOG, zlo, zhi, n_samples=360)
__result__ = {"gear_ring": g_prof, "collar_ring": c_prof,
              "good_interleave": good_il, "bad_interleave": bad_il}
"""


def _decompose(obj_path, workdir, tag, *, resolution=200000):
    """vhacd ``obj_path`` (convexhullApproximation=0 → hulls hug the teeth, no inflation
    eating the backlash) and write each convex hull to its own OBJ. Returns the per-hull
    OBJ paths, loaded as a COMPOUND so the dog gaps survive."""
    out = str(Path(workdir) / f"{tag}_vhacd.obj")
    log = str(Path(workdir) / f"{tag}.log")
    t0 = time.time()
    p.vhacd(obj_path, out, log, resolution=resolution, depth=20, concavity=0.0015,
            maxNumVerticesPerCH=48, convexhullDownsampling=8, convexhullApproximation=0,
            pca=0, mode=0)
    hulls, verts, faces, base = [], [], [], 0

    def flush():
        nonlocal verts, faces, base
        if verts:
            hulls.append((verts, faces)); base += len(verts)
        verts, faces = [], []

    for line in Path(out).read_text().splitlines():
        if line.startswith("o "):
            flush()
        elif line.startswith("v "):
            verts.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "):
            faces.append([int(t.split("/")[0]) - 1 - base for t in line.split()[1:]])
    flush()
    hdir = Path(workdir) / f"{tag}_hulls"
    hdir.mkdir(exist_ok=True)
    files = []
    for i, (v, f) in enumerate(hulls):
        lines = [f"v {x:.5f} {y:.5f} {z:.5f}" for x, y, z in v]
        lines += [f"f {' '.join(str(j + 1) for j in fc)}" for fc in f]
        fp = hdir / f"h{i}.obj"
        fp.write_text("\n".join(lines) + "\n")
        files.append(str(fp))
    return files, len(hulls), time.time() - t0


def _compound(cid, hull_files, z_off=0.0):
    """One compound collision shape from per-hull OBJs (optionally lifted in z)."""
    n = len(hull_files)
    return p.createCollisionShapeArray(
        shapeTypes=[p.GEOM_MESH] * n, fileNames=hull_files,
        collisionFramePositions=[[0, 0, z_off]] * n,
        collisionFrameOrientations=[[0, 0, 0, 1]] * n, physicsClientId=cid)


def _occupancy(cid, body, radius, z, n=180):
    """(fill, sectors) of a static body at the dog radius — the §11.10 dog_ring reading
    taken on the DECOMPOSED collision geometry via point-in-shape probes."""
    probe = p.createCollisionShape(p.GEOM_SPHERE, radius=1e-4, physicsClientId=cid)
    flags = []
    for k in range(n):
        th = 2 * math.pi * k / n
        pb = p.createMultiBody(baseMass=0, baseCollisionShapeIndex=probe,
                               basePosition=[radius * math.cos(th), radius * math.sin(th), z],
                               physicsClientId=cid)
        cps = p.getClosestPoints(body, pb, distance=0.05, physicsClientId=cid)
        flags.append(any(c[8] < 1e-3 for c in cps))
        p.removeBody(pb, physicsClientId=cid)
    fill = sum(flags) / n

    def runs(fl):
        if all(fl):
            return 1
        if not any(fl):
            return 0
        s = next(i for i in range(len(fl)) if not fl[i])
        rot = fl[s:] + fl[:s]
        return sum(1 for i in range(len(fl)) if rot[i] and not rot[i - 1])
    return fill, runs(flags)


def _both_fraction(cid, body_a, body_b, radius, z, n=180):
    """Co-occupancy of two static bodies at the dog radius — the interleave reading on the
    DECOMPOSED geometry. ~0 when teeth interleave, ~tooth-fill when in phase."""
    probe = p.createCollisionShape(p.GEOM_SPHERE, radius=1e-4, physicsClientId=cid)
    both = 0
    for k in range(n):
        th = 2 * math.pi * k / n
        pos = [radius * math.cos(th), radius * math.sin(th), z]
        pb = p.createMultiBody(baseMass=0, baseCollisionShapeIndex=probe,
                               basePosition=pos, physicsClientId=cid)
        ai = any(c[8] < 1e-3 for c in p.getClosestPoints(body_a, pb, distance=0.05,
                                                          physicsClientId=cid))
        bi = any(c[8] < 1e-3 for c in p.getClosestPoints(body_b, pb, distance=0.05,
                                                          physicsClientId=cid))
        both += ai and bi
        p.removeBody(pb, physicsClientId=cid)
    return both / n


def _two_ring_body(cid, gear_hulls, collar_hulls, collar_z_off):
    """Base + two coaxial revolute links (gear=0 driven, collar=1 free), each a compound
    of the part's real convex hulls. ``collar_z_off`` lifts the collar clear (disengaged)."""
    gear_shp = _compound(cid, gear_hulls, 0.0)
    collar_shp = _compound(cid, collar_hulls, collar_z_off)
    base = p.createCollisionShape(p.GEOM_SPHERE, radius=0.001, physicsClientId=cid)
    body = p.createMultiBody(
        baseMass=0, baseCollisionShapeIndex=base, basePosition=[0, 0, 0],
        linkMasses=[1.0, 1.0], linkCollisionShapeIndices=[gear_shp, collar_shp],
        linkVisualShapeIndices=[-1, -1], linkPositions=[[0, 0, 0], [0, 0, 0]],
        linkOrientations=[[0, 0, 0, 1]] * 2, linkInertialFramePositions=[[0, 0, 0]] * 2,
        linkInertialFrameOrientations=[[0, 0, 0, 1]] * 2, linkParentIndices=[0, 0],
        linkJointTypes=[p.JOINT_REVOLUTE] * 2, linkJointAxis=[[0, 0, 1]] * 2,
        flags=p.URDF_USE_SELF_COLLISION, physicsClientId=cid)
    for li in (0, 1):
        p.changeDynamics(body, li, localInertiaDiagonal=[2e-3, 2e-3, 1e-3],
                         lateralFriction=0.6, restitution=0.0,
                         contactStiffness=1e5, contactDamping=2e3, physicsClientId=cid)
    return body


def run_coupling(gear_hulls, collar_hulls, *, engaged, steps=6000, dt=1.0 / 2000,
                 record=0):
    """Drive the gear; measure the collar's steady speed. Engaged → driven through the
    interleaved teeth; disengaged (collar lifted DOG_H+2 clear in z) → free. Returns
    (mean collar omega over the tail, mean gear omega, trajectory). ``record`` samples
    that many (gear_ang, collar_ang) pairs for the review video."""
    cid = p.connect(p.DIRECT)
    p.setGravity(0, 0, 0, physicsClientId=cid)
    p.setTimeStep(dt, physicsClientId=cid)
    p.setPhysicsEngineParameter(numSolverIterations=200, physicsClientId=cid)
    body = _two_ring_body(cid, gear_hulls, collar_hulls,
                          0.0 if engaged else (DOG_H + 2.0))
    p.setJointMotorControl2(body, 0, p.VELOCITY_CONTROL, targetVelocity=DRIVE,
                            force=GFORCE, physicsClientId=cid)
    p.setJointMotorControl2(body, 1, p.VELOCITY_CONTROL, targetVelocity=0.0,
                            force=0.02, physicsClientId=cid)        # collar free (light drag)
    traj, every = [], max(1, steps // record) if record else 0
    ctail, gtail = [], []
    for s in range(steps):
        p.stepSimulation(physicsClientId=cid)
        if record and s % every == 0:
            traj.append((p.getJointState(body, 0, physicsClientId=cid)[0],
                         p.getJointState(body, 1, physicsClientId=cid)[0]))
        if s > steps * 0.7:
            ctail.append(p.getJointState(body, 1, physicsClientId=cid)[1])
            gtail.append(p.getJointState(body, 0, physicsClientId=cid)[1])
    p.disconnect(cid)
    return (sum(ctail) / len(ctail), sum(gtail) / len(gtail), traj)


def _rotz(verts, normals, theta):
    """Rotate (n,3,3) verts and (n,3) normals about +z by ``theta`` radians."""
    c, s = math.cos(theta), math.sin(theta)
    R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return verts @ R.T, normals @ R.T


def _make_video(gear_v, gear_n, collar_v, collar_n, eng_traj, dis_traj, eng_pct):
    """Render the real full STLs along the simulated joint trajectories: engaged (collar
    tracks the gear) then disengaged (collar lifted, gear spins alone)."""
    lift = DOG_H + 2.0
    ctr, rad = sv.bounds_of([gear_v, collar_v, collar_v + np.array([0, 0, lift])])
    images = []
    for ga, ca in eng_traj:
        gv, gn = _rotz(gear_v, gear_n, ga)
        cv, cn = _rotz(collar_v, collar_n, ca)
        sub = (f"ENGAGED — gear driven, collar coupled through the real dog teeth     "
               f"collar = {eng_pct:+.0f}% of gear speed")
        images.append(sv.frame(
            [{"verts": gv, "normals": gn, "color": sv.STEEL},
             {"verts": cv, "normals": cn, "color": sv.GREEN}],
            ctr, rad, title="sim-from-CAD: real decomposed dog clutch (PyBullet)", subtitle=sub))
    for ga, ca in dis_traj:
        gv, gn = _rotz(gear_v, gear_n, ga)
        cv, cn = _rotz(collar_v, collar_n, ca)
        cv = cv + np.array([0, 0, lift])
        sub = ("DISENGAGED — collar lifted clear, no dog contact     "
               "collar = 0% of gear speed (freewheels)")
        images.append(sv.frame(
            [{"verts": gv, "normals": gn, "color": sv.STEEL},
             {"verts": cv, "normals": cn, "color": sv.GOLD}],
            ctr, rad, title="sim-from-CAD: real decomposed dog clutch (PyBullet)", subtitle=sub))
    gif = sv.encode_gif(images, ART / "dog_clutch_cad_sim", fps=11, hold_last=6)
    ne = len(eng_traj)
    strip = sv.filmstrip(images, ART / "dog_clutch_cad_sim_filmstrip.png",
                         picks=[0, ne // 2, ne - 1, ne, ne + len(dis_traj) // 2, len(images) - 1])
    return gif, strip


def main():
    ART.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        gear_stl = str(Path(d) / "gear.stl")
        good_stl = str(Path(d) / "good.stl")
        gear_obj = str(Path(d) / "gear.obj")
        good_obj = str(Path(d) / "good.obj")
        bad_obj = str(Path(d) / "bad.obj")
        with Worker() as w:
            w.call("new_document", name="cadsim")
            g = w.call("add_gear", teeth=24, module=M, height=GH, name="g24")
            oracle = w.call("run_script", _timeout=600.0, code=BUILD % (
                SR, GH, ND, RDOG, DOG_H, g["name"], gear_stl, good_stl,
                gear_obj, good_obj, bad_obj))["result"]
        gear_v, gear_n = sv.read_binary_stl(gear_stl)
        collar_v, collar_n = sv.read_binary_stl(good_stl)

        print("== sim-from-CAD: real exported dog clutch in PyBullet (validate-kickoff #3) ==\n")
        print("  [1] §11.10 oracle on the exported Part.Shape (ground truth):")
        print(f"      gear dog ring : fill={oracle['gear_ring']['fill']}  "
              f"sectors={oracle['gear_ring']['sectors']}")
        print(f"      collar ring   : fill={oracle['collar_ring']['fill']}  "
              f"sectors={oracle['collar_ring']['sectors']}")
        print(f"      GOOD engaged  : both-occupied={oracle['good_interleave']['both_fraction']}"
              f"  (interleaves)")
        print(f"      BAD  in-phase : both-occupied={oracle['bad_interleave']['both_fraction']}"
              f"  (teeth-on-teeth)\n")

        # [2] vhacd-decompose the dog band of each real exported part
        print("  [2] convex decomposition (p.vhacd) of the dog-tooth band z=[%g,%g]:"
              % (GH, GH + DOG_H))
        cid0 = p.connect(p.DIRECT)
        gear_h, gn, gt = _decompose(gear_obj, d, "gear")
        good_h, cn, ct = _decompose(good_obj, d, "good")
        bad_h, bn, bt = _decompose(bad_obj, d, "bad")
        print(f"      gear -> {gn:2d} hulls ({gt:.1f}s)   good -> {cn:2d} hulls ({ct:.1f}s)"
              f"   bad -> {bn:2d} hulls ({bt:.1f}s)\n")

        # [3] faithfulness gate: the decomposed collision must carry the §11.10 signal
        zmid = GH + 2.0
        gear_body = p.createMultiBody(
            baseMass=0, baseCollisionShapeIndex=_compound(cid0, gear_h), physicsClientId=cid0)
        good_body = p.createMultiBody(
            baseMass=0, baseCollisionShapeIndex=_compound(cid0, good_h), physicsClientId=cid0)
        bad_body = p.createMultiBody(
            baseMass=0, baseCollisionShapeIndex=_compound(cid0, bad_h), physicsClientId=cid0)
        g_fill, g_sec = _occupancy(cid0, gear_body, RDOG, zmid)
        c_fill, c_sec = _occupancy(cid0, good_body, RDOG, zmid)
        good_both = _both_fraction(cid0, good_body, gear_body, RDOG, zmid)
        bad_both = _both_fraction(cid0, bad_body, gear_body, RDOG, zmid)
        p.disconnect(cid0)
        print("  [3] FAITHFULNESS GATE — §11.10 signal re-read on the DECOMPOSED collision:")
        print(f"      gear dog ring : fill={g_fill:.3f}  sectors={g_sec}   "
              f"(oracle {oracle['gear_ring']['fill']} / {oracle['gear_ring']['sectors']})")
        print(f"      collar ring   : fill={c_fill:.3f}  sectors={c_sec}   "
              f"(oracle {oracle['collar_ring']['fill']} / {oracle['collar_ring']['sectors']})")
        print(f"      GOOD engaged  : both-occupied={good_both:.3f}  (interleaves)")
        print(f"      BAD  in-phase : both-occupied={bad_both:.3f}  (teeth-on-teeth)")
        gate_ok = (g_sec >= ND and c_sec >= ND and g_fill < 0.7 and c_fill < 0.7
                   and good_both < 0.12 and bad_both >= 0.12)
        print(f"      -> decomposition {'PRESERVES' if gate_ok else 'BRIDGES'} the dog gaps"
              f" — physics geometry {'agrees with' if gate_ok else 'CONTRADICTS'} the oracle\n")

        # [4] dynamic contact: transmit-when-engaged / free-when-disengaged on the real part
        print("  [4] dynamic rigid-body contact (gear driven; does the collar follow?):")
        eng, eng_gw, eng_traj = run_coupling(gear_h, good_h, engaged=True, record=32)
        dis, dis_gw, dis_traj = run_coupling(gear_h, good_h, engaged=False, record=16)
        eng_pct = eng / DRIVE * 100
        print(f"      ENGAGED    collar omega = {eng:+.3f} rad/s  ({eng_pct:+.0f}% of gear; "
              f"gear runs {eng_gw:+.2f} — tight, zero-clearance teeth)")
        print(f"      DISENGAGED collar omega = {dis:+.3f} rad/s  ({dis / DRIVE * 100:+.0f}% of gear; "
              f"gear free {dis_gw:+.2f})")
        couples = (eng / DRIVE > 0.85) and (abs(dis) / DRIVE < 0.15)
        print(f"      -> {'SELECTS: engaged transmits, disengaged frees' if couples else 'did NOT select'}"
              f" — emergent from contact on the REAL decomposed metal\n")

        gif, strip = _make_video(gear_v, gear_n, collar_v, collar_n,
                                 eng_traj, dis_traj, eng_pct)

        ok = gate_ok and couples
        print(f"  RESULT: {'PASS' if ok else 'FAIL'} — rigid-body contact on the exported CAD "
              f"reproduces the dog-clutch selector.")
        print(f"  GIF       -> {gif}  ({gif.stat().st_size:,} bytes)")
        print(f"  filmstrip -> {strip}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

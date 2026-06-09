"""Multibody-dynamics run — the PyBullet executor behind the MBD family (§8).

Pure-Python, **FreeCAD-free** (the jobs.py threading contract: this is the body of
a background job, so it must not touch FreeCAD's document API). PyBullet is imported
lazily so the module loads even when the wheel is absent — discovery/degradation is
handled by ``driftpin.solvers``; this runs only once a solver resolved.

``run_mbd`` takes a spec already in SI (the worker handler converts mm/g/°·s⁻¹ →
m/kg/rad·s⁻¹ on the main thread before submitting), builds a rigid-link multibody
rooted at a fixed base, drives the requested joint(s), and reports back the numbers
the family promises: per-joint peak torque, link trajectories, the swept reachable
envelope, and — the thing a static ``interference_check`` cannot see — the contacts
that occur *through the motion*, with the sim time they happen at.

The exact kinematic anchors (Grübler DOF, Grashof, slider-crank stroke = 2R) are
NOT computed here — they are the closed-form gate in ``driftpin.analysis.kinematics``
that this dynamics run is checked against.
"""
from __future__ import annotations


def _connect():
    """Open a headless (DIRECT) PyBullet client. Lazy import so importing this
    module never requires the wheel. Returns (pybullet_module, client_id)."""
    import pybullet as p
    cid = p.connect(p.DIRECT)
    return p, cid


def run_mbd(spec: dict, duration_s: float = 1.0, dt_s: float = 1.0 / 240.0,
            gravity=(0.0, 0.0, -9.81), sample_every: int = 10) -> dict:
    """Simulate a rigid-link mechanism and report its motion + loads.

    ``spec`` (all SI): {base: {half_extents_m, mass_kg (0 ⇒ fixed), pos_m}, links:
    [{name, half_extents_m, mass_kg, parent (−1 = base), joint_type
    ('revolute'|'prismatic'|'fixed'), joint_axis, joint_pos_m (in the parent frame),
    com_m (inertial offset in the link frame)}], drivers: [{link, target_velocity,
    max_force}], obstacles: [{half_extents_m, pos_m}]}.

    Returns {engine, steps, duration_s, trajectories {name: [[x,y,z]...]}, max_torques
    {joint_i: N·m or N}, reachable_envelope {bbox_m, bbox_mm}, collisions_through_motion
    [{t_s, step, between:[nameA,nameB], max_depth_m}]}. Raises RuntimeError if PyBullet
    is missing (callers should gate on driftpin.solvers.require_solver first)."""
    try:
        p, cid = _connect()
    except Exception as e:                            # ImportError or connect failure
        raise RuntimeError(f"PyBullet unavailable: {e!r}") from e

    try:
        p.setGravity(*gravity, physicsClientId=cid)
        p.setTimeStep(dt_s, physicsClientId=cid)

        base = spec.get("base", {"half_extents_m": [0.01, 0.01, 0.01],
                                 "mass_kg": 0.0, "pos_m": [0, 0, 0]})
        links = spec["links"]
        name_by_link = {i: lk.get("name", f"link_{i}") for i, lk in enumerate(links)}

        # --- collision/visual shapes -----------------------------------------
        def box(half):
            return p.createCollisionShape(p.GEOM_BOX, halfExtents=list(half),
                                          physicsClientId=cid)

        base_col = box(base["half_extents_m"])
        link_cols = [box(lk["half_extents_m"]) for lk in links]

        _JT = {"revolute": p.JOINT_REVOLUTE, "prismatic": p.JOINT_PRISMATIC,
               "fixed": p.JOINT_FIXED}

        # createMultiBody's linkParentIndices use 0 = base and link k = k+1, so a
        # spec parent of -1 (base) maps to 0 and parent i maps to i+1.
        body = p.createMultiBody(
            baseMass=base.get("mass_kg", 0.0),
            baseCollisionShapeIndex=base_col,
            basePosition=base.get("pos_m", [0, 0, 0]),
            linkMasses=[lk["mass_kg"] for lk in links],
            linkCollisionShapeIndices=link_cols,
            linkVisualShapeIndices=[-1] * len(links),
            linkPositions=[lk["joint_pos_m"] for lk in links],
            linkOrientations=[lk.get("link_orn_quat", [0, 0, 0, 1]) for lk in links],
            linkInertialFramePositions=[lk.get("com_m", [0, 0, 0]) for lk in links],
            linkInertialFrameOrientations=[[0, 0, 0, 1]] * len(links),
            linkParentIndices=[lk["parent"] + 1 for lk in links],
            linkJointTypes=[_JT[lk.get("joint_type", "revolute")] for lk in links],
            linkJointAxis=[lk.get("joint_axis", [0, 0, 1]) for lk in links],
            physicsClientId=cid,
        )

        # --- obstacles (static collision boxes) ------------------------------
        obstacles = []
        for ob in spec.get("obstacles", []) or []:
            ob_col = box(ob["half_extents_m"])
            ob_id = p.createMultiBody(baseMass=0.0, baseCollisionShapeIndex=ob_col,
                                      basePosition=ob["pos_m"], physicsClientId=cid)
            obstacles.append(ob_id)

        # --- drivers (velocity control; torque-capped) -----------------------
        # Default: every non-driven movable joint is left free (no motor), so the
        # driven joint's torque reflects the real load it carries.
        for d in spec.get("drivers", []) or []:
            p.setJointMotorControl2(
                body, d["link"], p.VELOCITY_CONTROL,
                targetVelocity=d.get("target_velocity", 0.0),
                force=d.get("max_force", 1e3), physicsClientId=cid)

        # --- step + record ----------------------------------------------------
        n_steps = max(int(round(duration_s / dt_s)), 1)
        trajectories = {name_by_link[i]: [] for i in range(len(links))}
        max_torque = {f"joint_{i}": 0.0 for i in range(len(links))}
        collisions = []
        seen_pairs = set()

        for step in range(n_steps):
            p.stepSimulation(physicsClientId=cid)
            # per-joint applied torque (index 3 of joint state)
            for i in range(len(links)):
                tq = abs(p.getJointState(body, i, physicsClientId=cid)[3])
                if tq > max_torque[f"joint_{i}"]:
                    max_torque[f"joint_{i}"] = tq
            # sampled link world positions (COM)
            if step % sample_every == 0:
                states = p.getLinkStates(body, list(range(len(links))),
                                         physicsClientId=cid)
                for i, st in enumerate(states):
                    pos = st[0]
                    trajectories[name_by_link[i]].append(
                        [round(pos[0], 6), round(pos[1], 6), round(pos[2], 6)])
            # contacts with obstacles, through the motion
            for ob_id in obstacles:
                for cp in p.getContactPoints(bodyA=body, bodyB=ob_id,
                                             physicsClientId=cid):
                    link_idx = cp[3]                  # link of body A in contact
                    name = name_by_link.get(link_idx, f"link_{link_idx}")
                    key = (name, ob_id, step)
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    collisions.append({
                        "t_s": round(step * dt_s, 4),
                        "step": step,
                        "between": [name, "obstacle"],
                        "max_depth_m": round(abs(cp[8]), 6),  # contactDistance (<0 = penetration)
                    })

        # reachable envelope from all sampled link positions
        pts = [pt for pts in trajectories.values() for pt in pts]
        if pts:
            mins = [min(pt[k] for pt in pts) for k in range(3)]
            maxs = [max(pt[k] for pt in pts) for k in range(3)]
            bbox_m = [round(maxs[k] - mins[k], 6) for k in range(3)]
        else:
            bbox_m = [0.0, 0.0, 0.0]

        return {
            "engine": "pybullet",
            "steps": n_steps,
            "duration_s": duration_s,
            "trajectories": trajectories,
            "max_torques": {k: round(v, 6) for k, v in max_torque.items()},
            "reachable_envelope": {
                "bbox_m": bbox_m,
                "bbox_mm": [round(v * 1000.0, 3) for v in bbox_m],
            },
            "collisions_through_motion": collisions,
        }
    finally:
        try:
            p.disconnect(cid)
        except Exception:
            pass

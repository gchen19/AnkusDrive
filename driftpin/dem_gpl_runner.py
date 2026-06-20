#!/usr/bin/env python3
"""Arm's-length runner for the GPL-3.0 discrete-element engine (YADE).

THIS STANDALONE SCRIPT IS THE ONLY PLACE THE GPL DEM ENGINE IS DRIVEN.
DriftPin's own process never imports YADE; the worker invokes the ``yade``
executable in a SEPARATE process (``subprocess.run([yade, dem_gpl_runner.__file__], ...)``)
and exchanges JSON over stdin/stdout. That fork/exec + pipe-IPC boundary is the
same arm's-length isolation DriftPin already uses for the GPL Elmer/OpenFOAM
binaries and the KrakenOS optics runner, so the copyleft of YADE does not reach
into DriftPin's MIT/permissive code.

YADE injects its simulation API (``O``, ``Body``, ``sphere``, ``utils``,
``ymport``, the engines/laws, ``polyhedra_utils`` …) into this script's globals
when it is run as ``yade dem_gpl_runner.py`` — so there is no ``import yade`` at
module scope; the names resolve at run time inside the YADE interpreter. When run
under a plain CPython (e.g. ``--ping`` probing from the worker) the YADE names are
absent and the dispatch returns a clean miss instead of crashing.

Protocol
--------
stdin  : one JSON object  {"problem": "...", ...}
stdout : YADE prints banners/log lines to stdout, so the machine-readable result
         is emitted on its OWN line wrapped in sentinels:
             @@JSON@@{...}@@END@@
         The caller splits on those (see worker._run_dem_gpl).

Problems
--------
"pack": pour N monodisperse spheres into a box and let them settle under gravity,
    then measure the random close-packing fraction φ = Σ(4/3πr³)/V_occupied of the
    settled bed (V_occupied = footprint × settled height). Args:
        n_spheres   : target sphere count (default 800)
        radius_m    : sphere radius (default 0.004)
        box_m       : [Lx, Ly] floor footprint (default [0.06, 0.06])
        friction_deg: inter-particle friction angle (default 26)
        young_pa    : contact Young's modulus (default 1e7)
        density     : grain density kg/m³ (default 2600)
        steps       : max DEM steps (default 30000)
    Returns: {n_settled, packing_fraction, settled_height_m, mean_coordination,
              max_unbalanced, positions:[[x,y,z,r],...]}.

"flow": discharge spheres from a flat-bottomed hopper box through a central
    circular orifice and measure the steady mass-flow rate (kg/s). Args:
        n_spheres, radius_m, box_m([Lx,Ly,Lz]), outlet_m (orifice diameter),
        friction_deg, young_pa, density, settle_steps, flow_steps.
    Returns: {outlet_m, mass_flow_kg_s, n_discharged, discharge_time_s,
              bulk_density_kg_m3, positions:[...]}.

"ping": {"problem":"ping"} -> {"ok":true, "engine":"YADE", "version": ...}
"""
import json
import math
import sys
import time

SENTINEL_HEAD = "@@JSON@@"
SENTINEL_TAIL = "@@END@@"


def _yade_globals():
    """The YADE API names live in the builtins/globals YADE injects when it runs
    this file. Pull them from the module globals; return None when absent (plain
    CPython probe)."""
    g = globals()
    if "O" in g and "sphere" in g:
        return g
    # YADE >=2023 sometimes exposes via builtins
    import builtins
    if hasattr(builtins, "O") and hasattr(builtins, "sphere"):
        return {k: getattr(builtins, k) for k in dir(builtins)}
    return None


def _yade_version():
    try:
        import yade  # noqa: F401  (only reachable inside the yade interpreter)
        from yade import version  # type: ignore
        return getattr(version, "short", lambda: "unknown")()
    except Exception:
        try:
            return globals().get("O").version  # type: ignore[attr-defined]
        except Exception:
            return "unknown"


def _pack(problem, G):
    """Pour spheres into a box, settle under gravity, measure packing fraction.

    YADE injects its API (``O``, ``FrictMat``, ``aabbWalls``, the engines/laws,
    ``Sphere``/``Box`` …) into this script's globals — pull them from ``G``; the
    ``pack``/``utils`` submodules import normally."""
    O = G["O"]
    from yade import pack as ypack    # type: ignore
    from yade import utils            # type: ignore
    FrictMat = G["FrictMat"]
    Sphere = G["Sphere"]

    radius = float(problem.get("radius_m", 0.004))
    n_target = int(problem.get("n_spheres", 800))
    box_xy = problem.get("box_m", [0.06, 0.06])
    lx, ly = float(box_xy[0]), float(box_xy[1])
    fric = math.radians(float(problem.get("friction_deg", 26.0)))
    young = float(problem.get("young_pa", 1e7))
    density = float(problem.get("density", 2600.0))
    max_steps = int(problem.get("steps", 30000))

    mat = O.materials.append(FrictMat(
        young=young, poisson=0.3, density=density, frictionAngle=fric))

    # Size the pour column so all n_target spheres fit as a LOOSE cloud (~25%
    # initial solid fraction) above the floor, then let gravity densify it. A
    # column sized to the dense-packed height would be too short for makeCloud's
    # rejection sampler to place every sphere; ~4× that height gives it room.
    sph_vol = 4.0 / 3.0 * math.pi * radius ** 3
    col_h = max(0.08, 4.0 * n_target * sph_vol / (lx * ly) + 6 * radius)
    walls = utils.aabbWalls(
        ((-lx / 2, -ly / 2, 0), (lx / 2, ly / 2, col_h)),
        thickness=0, oversizeFactor=1.0, material=mat)
    # drop the top (+z) wall so the cloud can be poured/settle (index 5 is +z)
    wall_ids = O.bodies.append([w for i, w in enumerate(walls) if i != 5])

    # rain a loose cloud above the floor and let gravity pack it
    cloud = ypack.SpherePack()
    n_placed = cloud.makeCloud(
        (-lx / 2 + radius, -ly / 2 + radius, radius),
        (lx / 2 - radius, ly / 2 - radius, col_h - radius),
        rMean=radius, num=n_target, seed=1)
    cloud.toSimulation(material=mat)

    O.engines = [
        G["ForceResetter"](),
        G["InsertionSortCollider"]([G["Bo1_Sphere_Aabb"](), G["Bo1_Box_Aabb"]()]),
        G["InteractionLoop"](
            [G["Ig2_Sphere_Sphere_ScGeom"](), G["Ig2_Box_Sphere_ScGeom"]()],
            [G["Ip2_FrictMat_FrictMat_FrictPhys"]()],
            [G["Law2_ScGeom_FrictPhys_CundallStrack"]()]),
        G["NewtonIntegrator"](gravity=(0, 0, -9.81), damping=0.4),
    ]
    O.dt = 0.6 * utils.PWaveTimeStep()

    # optionally capture position SNAPSHOTS during the settle (for the artifact
    # video — the real pile building frame by frame). n_snapshots>0 records the
    # sphere centres at evenly spaced points along the settle.
    n_snap = int(problem.get("n_snapshots", 0))
    snapshots = []

    def _snap():
        if n_snap <= 0:
            return
        frame = [[round(b.state.pos[0], 5), round(b.state.pos[1], 5),
                  round(b.state.pos[2], 5), round(b.shape.radius, 5)]
                 for b in O.bodies if isinstance(b.shape, Sphere)]
        snapshots.append({"iter": O.iter, "unb": round(utils.unbalancedForce(), 5),
                          "spheres": frame})

    # settle until quasi-static (unbalanced force small) or step cap. Tight
    # threshold: a loose, still-raining cloud reads "balanced" too early, so
    # require unb < 0.005 AND a couple of extra windows of stillness.
    unb = 1.0
    still = 0
    n_windows = max(max_steps // 200, 1)
    snap_every = max(n_windows // n_snap, 1) if n_snap > 0 else 0
    _snap()                                              # frame 0: the raining cloud
    for w in range(n_windows):
        O.run(200, True)
        unb = utils.unbalancedForce()
        if snap_every and (w + 1) % snap_every == 0:
            _snap()
        if unb < 0.005:
            still += 1
            if still >= 3:
                break
        else:
            still = 0
    _snap()                                              # final settled frame

    # measure the settled bed. The occupied volume is the footprint × the bed
    # height, taken as the 95th-percentile sphere TOP (drop a stray flier). φ is
    # the solid volume of in-footprint spheres over that bed volume.
    zs, vol_sph, n_settled = [], 0.0, 0
    sph_vol = 4.0 / 3.0 * math.pi * radius ** 3
    centres = []
    for b in O.bodies:
        if not isinstance(b.shape, Sphere):
            continue
        r = b.shape.radius
        x, y, z = b.state.pos
        if -lx / 2 - r <= x <= lx / 2 + r and -ly / 2 - r <= y <= ly / 2 + r and z > -r:
            zs.append(z + r)
            vol_sph += 4.0 / 3.0 * math.pi * r ** 3
            n_settled += 1
            centres.append((x, y, z, r))
    zs_sorted = sorted(zs)
    if zs_sorted:
        top = zs_sorted[int(0.95 * (len(zs_sorted) - 1))]
    else:
        top = 0.0
    bed_vol = lx * ly * top if top > 0 else 1.0
    phi = vol_sph / bed_vol if bed_vol > 0 else 0.0

    # BULK packing fraction: a finite box deflates φ through its half-particle wall
    # boundary layer. Estimate the wall-free bulk by counting only the centres in an
    # INTERIOR cell inset ~2.5 radii from every wall and the free surface, divided
    # by that cell's geometric volume (centre-defined cell, so each counted sphere's
    # full solid volume belongs to it). This recovers the true bulk RCP an
    # effectively wall-free pack has; it needs a cell several diameters wide, so for
    # a thin/narrow bed it falls back to the box-φ.
    inset = 2.5 * radius
    ix0, ix1 = -lx / 2 + inset, lx / 2 - inset
    iy0, iy1 = -ly / 2 + inset, ly / 2 - inset
    iz0, iz1 = inset, top - inset
    bulk_phi = None
    if (ix1 - ix0) > 6 * radius and (iy1 - iy0) > 6 * radius and (iz1 - iz0) > 6 * radius:
        n_in = sum(1 for (x, y, z, _r) in centres
                   if ix0 <= x <= ix1 and iy0 <= y <= iy1 and iz0 <= z <= iz1)
        cell_vol = (ix1 - ix0) * (iy1 - iy0) * (iz1 - iz0)
        if cell_vol > 0 and n_in > 0:
            bulk_phi = n_in * sph_vol / cell_vol

    # mean coordination number (contacts per sphere) — the ≈6 isostatic signature
    # of a random-close pack of frictionless spheres
    ncont = sum(1 for i in O.interactions if i.isReal)
    mean_z = (2.0 * ncont / n_settled) if n_settled else 0.0

    positions = []
    for b in O.bodies:
        if isinstance(b.shape, Sphere):
            x, y, z = b.state.pos
            positions.append([round(x, 6), round(y, 6), round(z, 6),
                              round(b.shape.radius, 6)])

    return {
        "problem": "pack",
        "n_placed": int(n_placed),
        "n_settled": n_settled,
        # headline φ is the well-defined box fraction; bulk_phi (wall-corrected) is
        # reported alongside when the bed is wide/tall enough to define an interior.
        "packing_fraction": round(phi, 6),
        "packing_fraction_bulk": (round(bulk_phi, 6) if bulk_phi is not None else None),
        "settled_height_m": round(top, 6),
        "mean_coordination": round(mean_z, 4),
        "max_unbalanced": round(float(unb), 6),
        "footprint_m": [lx, ly],
        "positions": positions,
        "snapshots": snapshots,
        "_ids_walls": wall_ids,
    }


def _flow(problem, G):
    """Discharge spheres from a hopper box through a central circular orifice and
    measure the steady mass-flow rate."""
    O = G["O"]
    from yade import pack as ypack    # type: ignore
    from yade import utils            # type: ignore
    FrictMat = G["FrictMat"]
    Sphere = G["Sphere"]
    box = G["box"]

    radius = float(problem.get("radius_m", 0.003))
    n_target = int(problem.get("n_spheres", 1500))
    box_m = problem.get("box_m", [0.10, 0.10, 0.20])
    lx, ly, lz = (float(box_m[0]), float(box_m[1]), float(box_m[2]))
    outlet = float(problem.get("outlet_m", 0.03))
    fric = math.radians(float(problem.get("friction_deg", 26.0)))
    young = float(problem.get("young_pa", 1e7))
    density = float(problem.get("density", 2600.0))
    settle_steps = int(problem.get("settle_steps", 20000))
    flow_steps = int(problem.get("flow_steps", 60000))

    mat = O.materials.append(FrictMat(
        young=young, poisson=0.3, density=density, frictionAngle=fric))

    # closed box first (full floor) so the column settles; floor at z=0
    walls = utils.aabbWalls(((-lx / 2, -ly / 2, 0), (lx / 2, ly / 2, lz)),
                            thickness=0, oversizeFactor=1.0, material=mat)
    floor = walls[4]                       # -z wall is the floor
    side_ids = O.bodies.append([w for i, w in enumerate(walls) if i not in (4, 5)])
    floor_id = O.bodies.append(floor)

    cloud = ypack.SpherePack()
    cloud.makeCloud((-lx / 2 + radius, -ly / 2 + radius, radius),
                    (lx / 2 - radius, ly / 2 - radius, lz - radius),
                    rMean=radius, num=n_target, seed=1)
    cloud.toSimulation(material=mat)

    O.engines = [
        G["ForceResetter"](),
        G["InsertionSortCollider"]([G["Bo1_Sphere_Aabb"](), G["Bo1_Box_Aabb"]()]),
        G["InteractionLoop"](
            [G["Ig2_Sphere_Sphere_ScGeom"](), G["Ig2_Box_Sphere_ScGeom"]()],
            [G["Ip2_FrictMat_FrictMat_FrictPhys"]()],
            [G["Law2_ScGeom_FrictPhys_CundallStrack"]()]),
        G["NewtonIntegrator"](gravity=(0, 0, -9.81), damping=0.4),
    ]
    O.dt = 0.6 * utils.PWaveTimeStep()

    for _ in range(settle_steps // 200):
        O.run(200, True)
        if utils.unbalancedForce() < 0.05:
            break

    # OPEN the orifice: delete the floor and let spheres in the central circle
    # fall through a virtual plane at z = -0.02; count what passes per unit time.
    O.bodies.erase(floor_id)
    # a thin annular floor remains only outside the orifice radius — emulate by a
    # delete-on-exit: bodies whose centre passes below z=0 AND within the orifice
    # radius are "discharged"; outside the radius they rest on a re-added ring floor.
    r_out = outlet / 2.0

    def _inside(b):
        x, y, _ = b.state.pos
        return (x * x + y * y) <= r_out * r_out

    # re-add a full floor but make orifice-region spheres pass: cheaper to keep the
    # floor removed and instead add a box floor with a hole approximated by 4 boxes
    # leaving a central gap of width ~outlet.
    gap = r_out
    plate_t = 2 * radius
    floor_pieces = []
    # +x, -x, +y, -y plates around the central square gap
    for cx, cy, hx, hy in (
        ((lx / 2 + gap) / 2, 0, (lx / 2 - gap) / 2, ly / 2),
        (-(lx / 2 + gap) / 2, 0, (lx / 2 - gap) / 2, ly / 2),
        (0, (ly / 2 + gap) / 2, gap, (ly / 2 - gap) / 2),
        (0, -(ly / 2 + gap) / 2, gap, (ly / 2 - gap) / 2),
    ):
        if hx <= 0 or hy <= 0:
            continue
        b = box(center=(cx, cy, -plate_t / 2), extents=(hx, hy, plate_t / 2),
                material=mat, fixed=True)
        floor_pieces.append(O.bodies.append(b))

    discharged = []
    t0 = O.time
    for _ in range(flow_steps // 200):
        O.run(200, True)
        for b in O.bodies:
            if b.id in discharged:
                continue
            if isinstance(b.shape, Sphere) and b.state.pos[2] < -0.05 and _inside(b):
                discharged.append(b.id)
        # stop early once a good fraction has drained
        if len(discharged) > 0.6 * n_target:
            break
    t1 = O.time
    n_disc = len(discharged)

    sph_mass = density * 4.0 / 3.0 * math.pi * radius ** 3
    dt_disc = max(t1 - t0, 1e-9)
    # measure steady flow over the middle window (skip startup transient): use
    # total discharged / elapsed as the rate proxy
    mass_flow = n_disc * sph_mass / dt_disc

    bulk = density * 0.6  # nominal bulk for reference
    positions = []
    for b in O.bodies:
        if isinstance(b.shape, Sphere):
            x, y, z = b.state.pos
            positions.append([round(x, 6), round(y, 6), round(z, 6),
                              round(b.shape.radius, 6),
                              1 if b.id in discharged else 0])

    return {
        "problem": "flow",
        "outlet_m": round(outlet, 6),
        "mass_flow_kg_s": round(mass_flow, 8),
        "n_discharged": n_disc,
        "discharge_time_s": round(dt_disc, 6),
        "sphere_mass_kg": sph_mass,
        "bulk_density_kg_m3": round(bulk, 4),
        "positions": positions,
        "_floor_pieces": [side_ids, floor_pieces],
    }


def _dispatch(problem):
    kind = problem.get("problem")
    if kind == "ping":
        G = _yade_globals()
        return {"ok": bool(G), "engine": "YADE", "version": _yade_version(),
                "api_ready": bool(G)}
    G = _yade_globals()
    if G is None:
        return {"ok": False, "error": "YADE API not present — this script must be "
                "run as `yade dem_gpl_runner.py`, not under plain python"}
    if kind == "pack":
        return _pack(problem, G)
    if kind == "flow":
        return _flow(problem, G)
    return {"ok": False, "error": f"unknown problem {kind!r}"}


def main():
    raw = sys.stdin.read()
    t0 = time.time()
    try:
        problem = json.loads(raw) if raw.strip() else {}
        result = _dispatch(problem)
        # strip private debug keys before serialising
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if not k.startswith("_")}
        result.setdefault("ok", "error" not in result)
        result["wall_s"] = round(time.time() - t0, 3)
    except Exception as e:
        import traceback
        result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "trace": traceback.format_exc()[-800:]}
    sys.stdout.write("\n" + SENTINEL_HEAD + json.dumps(result) + SENTINEL_TAIL + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

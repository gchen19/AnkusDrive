#!/usr/bin/env python3
"""Arm's-length runner for the GPL-3.0 non-sequential optical engine (KrakenOS).

THIS STANDALONE SCRIPT IS THE ONLY PLACE A GPL OPTICS LIBRARY IS IMPORTED.
AnkusDrive's own process never imports KrakenOS; the worker invokes this file in a
SEPARATE process (``subprocess.run([sys.executable, optics_gpl_runner.__file__], ...)``)
and exchanges JSON over stdin/stdout. That fork/exec + pipe-IPC boundary is the same
arm's-length isolation AnkusDrive already uses for the GPL Elmer/OpenFOAM binaries, so
the copyleft of KrakenOS does not reach into AnkusDrive's MIT/permissive code.

Protocol
--------
stdin  : one JSON object  {"problem": "...", ...}
stdout : KrakenOS prints catalog banners to stdout, so the machine-readable result
         is emitted on its OWN line wrapped in sentinels:
             @@JSON@@{...}@@END@@
         The caller splits on those (see worker._run_optics_gpl).

Problems
--------
"solid_trace": trace a ray bundle non-sequentially through ONE STL solid that has a
    refractive index. Args:
        stl_path       : path to the solid mesh (AnkusDrive exports this from the model)
        glass          : KrakenOS catalog name (e.g. "BK7") OR a numeric index; if
                         omitted, ``n_refractive`` is used
        n_refractive   : constant index (float) when no catalog glass is named
        wavelength_um  : default 0.55
        rays           : [{"origin":[x,y,z], "dir":[l,m,n]}, ...]
        solid          : {"diameter":.., "thickness":.., "axis_move":1} placement
        obj_thickness  : object-plane gap (default 5.0)
        ima_diameter   : image-plane diameter (default 60.0)
    Returns: per-ray {valid, exit_dir, turn_deg} plus aggregates
        {n_launched, n_valid, valid_fraction, mean_turn_deg, max_turn_deg}.
    ``turn_deg`` is the angle between each ray's INPUT direction and its exit
    direction — 0 for a straight pass, ~90 for a corner-turning TIR prism.

"ping": {"problem":"ping"} -> {"ok":true, "engine":"KrakenOS", "version": ...}
"""
import json
import math
import sys
import time

SENTINEL_HEAD = "@@JSON@@"
SENTINEL_TAIL = "@@END@@"


def _angle_between(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return None
    dot = sum(x * y for x, y in zip(a, b)) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _solid_trace(problem):
    import numpy as np
    import KrakenOS as Kos                      # GPL-3.0 import isolated to this process

    stl = problem["stl_path"]
    glass = problem.get("glass")
    if glass is None:
        glass = float(problem.get("n_refractive", 1.49062))
    wl = float(problem.get("wavelength_um", 0.55))
    placement = problem.get("solid") or {}
    rays = problem.get("rays") or []

    P_Obj = Kos.surf(Thickness=float(problem.get("obj_thickness", 5.0)), Diameter=30.0)
    Solid = Kos.surf()
    Solid.Solid_3d_stl = stl
    Solid.Glass = glass
    Solid.Diameter = float(placement.get("diameter", 40.0))
    Solid.Thickness = float(placement.get("thickness", 30.0))
    Solid.AxisMove = int(placement.get("axis_move", 1))
    P_Ima = Kos.surf(Diameter=float(problem.get("ima_diameter", 60.0)), Glass="AIR")

    S = Kos.system([P_Obj, Solid, P_Ima], Kos.Setup())
    S.energy_probability = 0                    # geometric trace (no Fresnel weighting)
    want_paths = bool(problem.get("want_paths"))

    per_ray = []
    turns = []
    paths = []
    n_valid = 0
    for r in rays:
        origin = [float(x) for x in r["origin"]]
        direction = [float(x) for x in r["dir"]]
        S.NsTrace(origin, direction, wl)
        valid = (getattr(S, "val", 0) == 1)
        rec = {"valid": bool(valid)}
        if valid:
            n_valid += 1
            exit_dir = [round(float(x), 6) for x in np.asarray(S.LMN)[-1]]
            turn = _angle_between(direction, exit_dir)
            rec["exit_dir"] = exit_dir
            rec["turn_deg"] = (round(turn, 4) if turn is not None else None)
            if turn is not None:
                turns.append(turn)
            if want_paths:                      # the ray polyline (per-surface hit points)
                paths.append([[round(float(c), 5) for c in pt]
                              for pt in np.asarray(S.XYZ)])
        per_ray.append(rec)

    out = {
        "problem": "solid_trace",
        "n_launched": len(rays),
        "n_valid": n_valid,
        "valid_fraction": (round(n_valid / len(rays), 6) if rays else 0.0),
        "mean_turn_deg": (round(sum(turns) / len(turns), 4) if turns else None),
        "max_turn_deg": (round(max(turns), 4) if turns else None),
        "rays": per_ray,
    }
    if want_paths:
        out["paths"] = paths
    return out


def _dispatch(problem):
    kind = problem.get("problem")
    if kind == "ping":
        import KrakenOS as Kos
        return {"ok": True, "engine": "KrakenOS",
                "version": getattr(Kos, "__version__", "unknown")}
    if kind == "solid_trace":
        return _solid_trace(problem)
    return {"ok": False, "error": f"unknown problem {kind!r}"}


def main():
    raw = sys.stdin.read()
    t0 = time.time()
    try:
        problem = json.loads(raw) if raw.strip() else {}
        result = _dispatch(problem)
        result.setdefault("ok", "error" not in result)
        result["wall_s"] = round(time.time() - t0, 3)
    except Exception as e:                       # never crash the parent — return a clean miss
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    sys.stdout.write("\n" + SENTINEL_HEAD + json.dumps(result) + SENTINEL_TAIL + "\n")


if __name__ == "__main__":
    main()

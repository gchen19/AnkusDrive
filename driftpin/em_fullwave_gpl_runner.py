#!/usr/bin/env python3
"""Arm's-length runner for the GPL-3.0 full-wave FDTD engine (openEMS / CSXCAD).

THIS STANDALONE SCRIPT IS THE ONLY PLACE THE GPL openEMS/CSXCAD LIBRARIES ARE
IMPORTED. DriftPin's own process never imports openEMS; the worker invokes this
file in a SEPARATE process
(``subprocess.run([openems_python, em_fullwave_gpl_runner.__file__], ...)``) and
exchanges JSON over stdin/stdout. That fork/exec + pipe-IPC boundary is the same
arm's-length isolation DriftPin already uses for the GPL Elmer/OpenFOAM binaries
and for KrakenOS (optics_gpl_runner.py), so the copyleft of openEMS does not
reach into DriftPin's MIT/permissive code.

Protocol
--------
stdin  : one JSON object  {"problem": "...", ...}
stdout : openEMS prints its engine banner/iteration log to stdout, so the
         machine-readable result is emitted on its OWN line wrapped in sentinels:
             @@JSON@@{...}@@END@@
         The caller splits on those (see worker._run_em_fullwave_gpl).

Problems
--------
"waveguide_sweep": drive a hollow rectangular waveguide with a TE10 port at one
    end, absorb at the other, sweep the transmission S21 over a band that
    STRADDLES the analytic cutoff f_c = c/(2a), and report the propagating↔
    evanescent transition. Args:
        a_mm, b_mm     : broad / narrow wall (mm)
        length_mm      : guide length between the ports (mm)
        f_start_ghz    : sweep start (below cutoff)
        f_stop_ghz     : sweep stop  (above cutoff)
        n_freq         : sweep points (default 121)
        nrts           : max FDTD timesteps (default 30000)
        cells_per_wl   : mesh resolution at f_stop (default 20)
        eps_r          : dielectric fill (default 1.0)
        want_field     : also dump |E| on the broad-wall cross-section midband
    Returns: {freq_ghz[], s21_abs[], s21_db[], transmission_norm[],
        plateau, fc_crossing_ghz (half-power), evanescent_mean, propagating_mean,
        n_cells, wall_s}.  fc_crossing_ghz / (c/2a) is the FDTD-vs-oracle ratio.

"dipole_s11": (optional) drive a centre-fed thin dipole, sweep S11, report the
    first resonance (|S11| null). Args: length_mm, gap_mm, f_start_ghz,
    f_stop_ghz, n_freq, nrts. Returns {freq_ghz[], s11_db[], resonance_ghz}.

"ping": {"problem":"ping"} -> {"ok":true, "engine":"openEMS", "version": ...}
"""
import json
import math
import sys
import tempfile
import time

SENTINEL_HEAD = "@@JSON@@"
SENTINEL_TAIL = "@@END@@"
C0 = 299_792_458.0


def _half_power_crossing(freq, normt, level=0.5):
    """First frequency where the normalized transmission rises through ``level``
    (linear interpolation between samples). None if it never crosses."""
    for i in range(1, len(freq)):
        if normt[i - 1] < level <= normt[i]:
            t = (level - normt[i - 1]) / (normt[i] - normt[i - 1])
            return freq[i - 1] + t * (freq[i] - freq[i - 1])
    return None


def _waveguide_sweep(problem):
    import numpy as np
    np.seterr(all="ignore")                          # evanescent β imaginary -> NaN, expected
    from CSXCAD import ContinuousStructure          # GPL-3.0 imports isolated to this process
    from openEMS import openEMS

    a_mm = float(problem["a_mm"])
    b_mm = float(problem.get("b_mm", a_mm / 2.0))
    length_mm = float(problem.get("length_mm", 2.6 * a_mm))
    eps_r = float(problem.get("eps_r", 1.0))
    f_start = float(problem["f_start_ghz"]) * 1e9
    f_stop = float(problem["f_stop_ghz"]) * 1e9
    n_freq = int(problem.get("n_freq", 121))
    nrts = int(problem.get("nrts", 30000))
    cpw = float(problem.get("cells_per_wl", 20))

    unit = 1e-3                                       # mm drawing unit
    a, b, length = a_mm, b_mm, length_mm
    c_eff = C0 / math.sqrt(eps_r)
    fc = c_eff / (2.0 * a * unit)                    # exact TE10 cutoff (analytic)

    f0 = 0.5 * (f_start + f_stop)
    fw = 0.5 * (f_stop - f_start)
    mesh_res = (c_eff / f_stop) / unit / cpw         # cells at the highest frequency

    FDTD = openEMS(NrTS=nrts, EndCriteria=1e-4)
    FDTD.SetGaussExcite(f0, fw)
    FDTD.SetBoundaryCond([0, 0, 0, 0, 3, 3])         # PEC walls, MUR on the z ends
    CSX = ContinuousStructure()
    FDTD.SetCSX(CSX)
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(unit)
    mesh.AddLine("x", [0, a])
    mesh.AddLine("y", [0, b])
    mesh.AddLine("z", [0, length])

    ports = []
    s = [0, 0, 10 * mesh_res]
    e = [a, b, 15 * mesh_res]
    mesh.AddLine("z", [s[2], e[2]])
    ports.append(FDTD.AddRectWaveGuidePort(0, s, e, "z", a * unit, b * unit, "TE10", 1))
    s = [0, 0, length - 10 * mesh_res]
    e = [a, b, length - 15 * mesh_res]
    mesh.AddLine("z", [s[2], e[2]])
    ports.append(FDTD.AddRectWaveGuidePort(1, s, e, "z", a * unit, b * unit, "TE10"))
    mesh.SmoothMeshLines("all", mesh_res, ratio=1.4)

    n_cells = (mesh.GetQtyLines("x") * mesh.GetQtyLines("y") * mesh.GetQtyLines("z"))

    sim = tempfile.mkdtemp(prefix="em_wg_")
    FDTD.Run(sim, cleanup=True, verbose=0)

    freq = np.linspace(f_start, f_stop, n_freq)
    for p in ports:
        p.CalcPort(sim, freq)
    # transmission magnitude; below cutoff β is imaginary -> NaN -> evanescent (0)
    s21 = np.abs(ports[1].uf_ref / ports[0].uf_inc)
    trans = np.nan_to_num(s21, nan=0.0, posinf=0.0, neginf=0.0)
    db = 20.0 * np.log10(np.clip(trans, 1e-9, None))
    plateau = float(np.median(trans[freq > 1.3 * fc])) or 1.0
    normt = trans / plateau

    cross = _half_power_crossing(list(freq), list(normt), level=0.5)
    ev = float(np.mean(normt[freq < 0.85 * fc])) if np.any(freq < 0.85 * fc) else None
    pr = float(np.mean(normt[freq > 1.3 * fc])) if np.any(freq > 1.3 * fc) else None

    return {
        "problem": "waveguide_sweep",
        "a_mm": a_mm, "b_mm": b_mm, "length_mm": length_mm, "eps_r": eps_r,
        "fc_analytic_ghz": round(fc / 1e9, 6),
        "freq_ghz": [round(float(f) / 1e9, 6) for f in freq],
        "s21_abs": [round(float(x), 6) for x in trans],
        "s21_db": [round(float(x), 4) for x in db],
        "transmission_norm": [round(float(x), 6) for x in normt],
        "plateau": round(plateau, 6),
        "fc_crossing_ghz": (round(cross / 1e9, 6) if cross else None),
        "fc_ratio": (round(cross / fc, 6) if cross else None),
        "evanescent_mean": (round(ev, 6) if ev is not None else None),
        "propagating_mean": (round(pr, 6) if pr is not None else None),
        "n_cells": int(n_cells),
    }


def _dipole_s11(problem):
    import numpy as np
    np.seterr(all="ignore")
    from CSXCAD import ContinuousStructure
    from openEMS import openEMS

    length_mm = float(problem["length_mm"])
    gap_mm = float(problem.get("gap_mm", length_mm / 40.0))
    rad_mm = float(problem.get("radius_mm", length_mm / 200.0))
    f_start = float(problem["f_start_ghz"]) * 1e9
    f_stop = float(problem["f_stop_ghz"]) * 1e9
    n_freq = int(problem.get("n_freq", 201))
    nrts = int(problem.get("nrts", 60000))
    unit = 1e-3

    f0 = 0.5 * (f_start + f_stop)
    fw = 0.5 * (f_stop - f_start)
    lam0 = C0 / f_stop / unit
    mesh_res = lam0 / 30.0

    FDTD = openEMS(NrTS=nrts, EndCriteria=1e-4)
    FDTD.SetGaussExcite(f0, fw)
    FDTD.SetBoundaryCond(["MUR"] * 6)
    CSX = ContinuousStructure()
    FDTD.SetCSX(CSX)
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(unit)

    metal = CSX.AddMetal("dipole")
    L = length_mm
    half = (L - gap_mm) / 2.0
    metal.AddBox([-rad_mm, -rad_mm, gap_mm / 2.0], [rad_mm, rad_mm, gap_mm / 2.0 + half])
    metal.AddBox([-rad_mm, -rad_mm, -gap_mm / 2.0 - half], [rad_mm, rad_mm, -gap_mm / 2.0])

    span = L * 1.5
    mesh.AddLine("x", [-span, 0, span])
    mesh.AddLine("y", [-span, 0, span])
    mesh.AddLine("z", [-L, -gap_mm / 2.0, gap_mm / 2.0, L])
    mesh.SmoothMeshLines("all", mesh_res, ratio=1.4)

    port = FDTD.AddLumpedPort(1, 50.0, [-rad_mm, -rad_mm, -gap_mm / 2.0],
                              [rad_mm, rad_mm, gap_mm / 2.0], "z", 1.0)
    sim = tempfile.mkdtemp(prefix="em_dip_")
    FDTD.Run(sim, cleanup=True, verbose=0)

    freq = np.linspace(f_start, f_stop, n_freq)
    port.CalcPort(sim, freq, ref_impedance=50.0)
    s11 = port.uf_ref / port.uf_inc
    s11db = 20.0 * np.log10(np.clip(np.abs(s11), 1e-9, None))
    i = int(np.argmin(s11db))
    return {
        "problem": "dipole_s11",
        "length_mm": length_mm,
        "freq_ghz": [round(float(f) / 1e9, 6) for f in freq],
        "s11_db": [round(float(x), 4) for x in s11db],
        "resonance_ghz": round(float(freq[i]) / 1e9, 6),
        "resonance_s11_db": round(float(s11db[i]), 4),
    }


def _dispatch(problem):
    kind = problem.get("problem")
    if kind == "ping":
        import openEMS
        return {"ok": True, "engine": "openEMS",
                "version": getattr(openEMS, "__version__", "unknown")}
    if kind == "waveguide_sweep":
        return _waveguide_sweep(problem)
    if kind == "dipole_s11":
        return _dipole_s11(problem)
    return {"ok": False, "error": f"unknown problem {kind!r}"}


def main():
    raw = sys.stdin.read()
    t0 = time.time()
    try:
        problem = json.loads(raw) if raw.strip() else {}
        result = _dispatch(problem)
        result.setdefault("ok", "error" not in result)
        result["wall_s"] = round(time.time() - t0, 3)
    except Exception as e:                            # never crash the parent — clean miss
        import traceback
        result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "trace": traceback.format_exc()[-800:]}
    sys.stdout.write("\n" + SENTINEL_HEAD + json.dumps(result) + SENTINEL_TAIL + "\n")


if __name__ == "__main__":
    main()

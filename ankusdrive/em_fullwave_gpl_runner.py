#!/usr/bin/env python3
"""Arm's-length runner for the GPL-3.0 full-wave FDTD engine (openEMS / CSXCAD).

THIS STANDALONE SCRIPT IS THE ONLY PLACE THE GPL openEMS/CSXCAD LIBRARIES ARE
IMPORTED. AnkusDrive's own process never imports openEMS; the worker invokes this
file in a SEPARATE process
(``subprocess.run([openems_python, em_fullwave_gpl_runner.__file__], ...)``) and
exchanges JSON over stdin/stdout. That fork/exec + pipe-IPC boundary is the same
arm's-length isolation AnkusDrive already uses for the GPL Elmer/OpenFOAM binaries
and for KrakenOS (optics_gpl_runner.py), so the copyleft of openEMS does not
reach into AnkusDrive's MIT/permissive code.

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
        decay_f_ratio  : f/f_c at which the evanescent decay gate is read (0.7)
    Returns: {freq_ghz[], s21_abs[], s21_db[], transmission_norm[],
        plateau, fc_crossing_ghz (half-power), fc_ratio, evanescent_mean,
        propagating_mean, alpha_fdtd, alpha_exact, alpha_ratio, decay_*, n_cells,
        wall_s}.

    THE FIELD GATE IS ``alpha_ratio`` (issue #401): voltage probes along the guide
    sample the SOLVED field below cutoff, and the fitted decay rate ln|E| vs z is
    compared to the exact alpha = sqrt((pi/a)^2 - k^2). It moves with mesh and
    timestep budget.

    ``fc_ratio`` is only a PORT-SETUP CHECK, not a field gate (issue #398):
    CalcPort's analytic beta = sqrt(k^2 - kc^2) is NaN below cutoff, which is
    zeroed here, so the transmission is a step at the port's own c/(2a) whatever
    the field does, and the half-power crossing only resolves the frequency grid.

    Degenerate port placement (the 10–15-cell port blocks at each end overlapping
    or overrunning the guide) returns {ok: false, error} (issue #403).

"dipole_s11": drive a centre-fed thin dipole, sweep S11, report the first
    resonance (|S11| null). Args: length_mm, gap_mm, radius_mm, f_start_ghz,
    f_stop_ghz, n_freq, nrts, and the mesh — mesh_res_mm (explicit), else
    lambda(mesh_f_ghz, default f_stop) / cells_per_wl (default 30) — so the mesh
    can be held fixed while the sweep window moves (issue #400). The wire surface
    and feed gap get their own mesh lines (>= 2 cells across the radius, 3 across
    the gap) independent of the wavelength mesh, so radius_mm/gap_mm are resolved
    (issue #399). Returns {freq_ghz[], s11_db[], resonance_ghz, mesh_res_mm}.

"ping": {"problem":"ping"} -> {"ok":true, "engine":"openEMS", "version": ...}
"""
import json
import math
import os
import sys
import tempfile
import time

SENTINEL_HEAD = "@@JSON@@"
# Where the simulation directory goes. The worker sets ANKUSDRIVE_CASE_ROOT to the
# managed case root (ankusdrive/cases.py, #437) so these are reaped with every other
# solver case; unset, this falls back to the system temp dir. Read from the
# environment rather than imported, because this runner imports no AnkusDrive code —
# that is what keeps the GPL-3.0 engine at arm's length.
CASE_ROOT = os.environ.get("ANKUSDRIVE_CASE_ROOT") or None


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
    length_mm = float(problem.get("length_mm", 5.25 * a_mm))  # room for the decay gate
    eps_r = float(problem.get("eps_r", 1.0))
    f_start = float(problem["f_start_ghz"]) * 1e9
    f_stop = float(problem["f_stop_ghz"]) * 1e9
    n_freq = int(problem.get("n_freq", 121))
    nrts = int(problem.get("nrts", 30000))
    cpw = float(problem.get("cells_per_wl", 20))
    # -60 dB, not openEMS's usual -40: the decay gate reads a sub-cutoff tone ~35 dB
    # down, which -40 dB truncation noise visibly bent on fine meshes.
    end_criteria = float(problem.get("end_criteria", 1e-6))

    decay_f_ratio = float(problem.get("decay_f_ratio", 0.7))

    unit = 1e-3                                       # mm drawing unit
    a, b, length = a_mm, b_mm, length_mm
    c_eff = C0 / math.sqrt(eps_r)
    fc = c_eff / (2.0 * a * unit)                    # exact TE10 cutoff (analytic)

    f0 = 0.5 * (f_start + f_stop)
    fw = 0.5 * (f_stop - f_start)
    mesh_res = (c_eff / f_stop) / unit / cpw         # cells at the highest frequency

    # Each port block spans 10–15 cells in from its end; if the two blocks meet,
    # the case is degenerate and would come back as nulls with ok:true (#403).
    port_depth = 15 * mesh_res
    if port_depth >= length / 2.0:
        return {"ok": False, "error": (
            f"port placement does not fit: each TE10 port block reaches "
            f"15*mesh_res = {port_depth:.3g} mm in from its end, which must be < "
            f"length_mm/2 = {length / 2.0:.3g} mm (mesh_res = lambda(f_stop)/"
            f"cells_per_wl = {mesh_res:.3g} mm from f_stop_ghz={f_stop / 1e9:g}, "
            f"cells_per_wl={cpw:g}). Raise cells_per_wl or length_mm "
            f"(need length_mm > {2 * port_depth:.3g}).")}

    FDTD = openEMS(NrTS=nrts, EndCriteria=end_criteria)
    FDTD.SetGaussExcite(f0, fw)
    FDTD.SetBoundaryCond([0, 0, 0, 0, 3, 3])         # PEC walls, MUR on the z ends
    CSX = ContinuousStructure()
    FDTD.SetCSX(CSX)
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(unit)
    mesh.AddLine("x", [0, a / 2.0, a])              # a/2: where the decay probes sit
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

    decay = _decay_probe_plan(a, length, port_depth, fc, eps_r, decay_f_ratio,
                              f_start, f_stop)
    for i, z in enumerate(decay.get("z_mm", [])):
        mesh.AddLine("z", [z])
        CSX.AddProbe(f"ut_decay_{i}", p_type=0).AddBox([a / 2.0, 0, z], [a / 2.0, b, z])
    mesh.SmoothMeshLines("all", mesh_res, ratio=1.4)

    n_cells = (mesh.GetQtyLines("x") * mesh.GetQtyLines("y") * mesh.GetQtyLines("z"))

    sim = tempfile.mkdtemp(prefix="em_wg-", dir=CASE_ROOT)
    FDTD.Run(sim, cleanup=True, verbose=0)

    decay_out = _decay_fit(sim, decay, np.array(mesh.GetLines("z")))

    freq = np.linspace(f_start, f_stop, n_freq)
    for p in ports:
        p.CalcPort(sim, freq)
    # transmission magnitude. Below cutoff CalcPort's ANALYTIC beta is NaN and is
    # zeroed here, so this curve steps at the port's own c/(2a) — fc_ratio is a
    # port-setup check, not a read of the field (#398); alpha_ratio is the gate.
    s21 = np.abs(ports[1].uf_ref / ports[0].uf_inc)
    trans = np.nan_to_num(s21, nan=0.0, posinf=0.0, neginf=0.0)
    db = 20.0 * np.log10(np.clip(trans, 1e-9, None))
    plateau = float(np.median(trans[freq > 1.3 * fc])) or 1.0
    normt = trans / plateau

    cross = _half_power_crossing(list(freq), list(normt), level=0.5)
    ev = float(np.mean(normt[freq < 0.85 * fc])) if np.any(freq < 0.85 * fc) else None
    pr = float(np.mean(normt[freq > 1.3 * fc])) if np.any(freq > 1.3 * fc) else None

    out = {
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
        "mesh_res_mm": round(mesh_res, 6),
        "n_cells": int(n_cells),
    }
    out.update(decay_out)
    return out


def _decay_probe_plan(a, length, port_depth, fc, eps_r, f_ratio, f_start, f_stop,
                      n_probes=6, max_nepers=2.5):
    """Where to sample the evanescent field for the decay gate (all lengths mm).

    The probes start a full broad-wall past the excitation port block, so the
    faster-decaying higher-order modes the source plane also launches are gone
    relative to TE10 (TE30 is down a further ~e^-7 there; at half a broad-wall the
    residue was mesh-dependent and biased the fine-mesh fits). They span ``max_nepers`` of TE10 decay (farther
    out the field sinks into the FDTD noise floor and the fit flattens), and the
    last probe stays another ``max_nepers`` short of the far end: the MUR there
    reflects evanescent fields, and that return perturbs the slope by ~e^-2αd
    (a half-broad-wall margin measurably steepened the fit by ~3%). The exact
    alpha is only used to PLACE the probes; the gate reads the fitted slope.
    Returns the plan, with a ``note`` instead of ``z_mm`` when the gate cannot be
    read for this configuration."""
    f = f_ratio * fc
    plan = {"f_hz": f}
    if not (f_start <= f < fc and f <= f_stop):
        plan["note"] = (f"decay gate needs f_start <= decay_f_ratio*f_c < f_c; "
                        f"{f / 1e9:.4g} GHz is outside [{f_start / 1e9:g}, "
                        f"{min(fc, f_stop) / 1e9:.4g}) GHz")
        return plan
    k = 2.0 * math.pi * f * math.sqrt(eps_r) / C0
    alpha = math.sqrt((math.pi / (a * 1e-3)) ** 2 - k ** 2)      # 1/m
    z0 = port_depth + a
    reach = max_nepers / alpha * 1e3                             # mm
    z1 = min(z0 + reach, length - reach)
    plan["alpha_exact"] = alpha
    if (z1 - z0) * 1e-3 * alpha < 1.0:
        plan["note"] = (f"guide too short for the decay gate: probe window "
                        f"{z0:.3g}–{z1:.3g} mm spans < 1 neper of decay at "
                        f"{f / 1e9:.4g} GHz (lengthen length_mm)")
        return plan
    plan["z_mm"] = [z0 + (z1 - z0) * i / (n_probes - 1) for i in range(n_probes)]
    return plan


def _decay_fit(sim, plan, z_lines):
    """Fit ln|V(z)| of the decay probes at the plan's frequency; alpha_fdtd is the
    negated slope (1/m). Probe z is read back from the smoothed mesh (the probe
    snaps to the nearest line)."""
    import numpy as np
    from openEMS.ports import UI_data

    alpha_exact = plan.get("alpha_exact")
    out = {"decay_freq_ghz": round(plan["f_hz"] / 1e9, 6),
           "alpha_exact": (round(alpha_exact, 6) if alpha_exact else None),
           "alpha_fdtd": None, "alpha_ratio": None}
    if "z_mm" not in plan:
        out["decay_note"] = plan.get("note")
        return out
    names = [f"ut_decay_{i}" for i in range(len(plan["z_mm"]))]
    ui = UI_data(names, sim, np.array([plan["f_hz"]]))
    mag = np.array([abs(v[0]) for v in ui.ui_f_val])
    z = np.array([z_lines[np.argmin(np.abs(z_lines - zp))] for zp in plan["z_mm"]])
    out["decay_probe_z_mm"] = [round(float(x), 4) for x in z]
    if not np.all(np.isfinite(mag)) or np.any(mag <= 0):
        out["decay_note"] = "decay probe amplitude was zero or non-finite"
        return out
    ln = np.log(mag)
    slope, icpt = np.polyfit(z * 1e-3, ln, 1)
    resid = ln - (slope * z * 1e-3 + icpt)
    alpha = -float(slope)
    out.update({
        "alpha_fdtd": round(alpha, 6),
        "alpha_ratio": round(alpha / alpha_exact, 6),
        "decay_probe_db": [round(float(20.0 * (x - ln[0]) / math.log(10)), 3) for x in ln],
        "decay_fit_rms_np": round(float(np.sqrt(np.mean(resid ** 2))), 6),
    })
    return out


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
    # The mesh is its own knob, not a side effect of the sweep window (#400): an
    # explicit mesh_res_mm wins, else lambda at mesh_f_ghz (default f_stop, the
    # historical behaviour) over cells_per_wl.
    if problem.get("mesh_res_mm") is not None:
        mesh_res = float(problem["mesh_res_mm"])
    else:
        mesh_f = float(problem.get("mesh_f_ghz") or f_stop / 1e9) * 1e9
        mesh_res = C0 / mesh_f / unit / float(problem.get("cells_per_wl", 30))
    if not (0 < rad_mm and 0 < gap_mm < length_mm and mesh_res > 0):
        return {"ok": False, "error": (
            f"dipole_s11 needs radius_mm > 0, 0 < gap_mm < length_mm and a positive "
            f"mesh (got radius_mm={rad_mm:g}, gap_mm={gap_mm:g}, "
            f"length_mm={length_mm:g}, mesh_res={mesh_res:g} mm)")}

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
    # Feature lines independent of the wavelength mesh (#399): the wire surface at
    # +-r with 2 cells across the radius, 3 cells across the feed gap, and the wire
    # tips. Without them SmoothMeshLines at lambda/30 never sees r or the gap and
    # every radius meshes to the same one-cell wire.
    g2 = gap_mm / 2.0
    tip = g2 + half
    wire = [-rad_mm, -rad_mm / 2.0, 0, rad_mm / 2.0, rad_mm]
    mesh.AddLine("x", [-span, span] + wire)
    mesh.AddLine("y", [-span, span] + wire)
    mesh.AddLine("z", [-L, -tip, -g2, -g2 / 3.0, g2 / 3.0, g2, tip, L])
    mesh.SmoothMeshLines("all", mesh_res, ratio=1.4)

    port = FDTD.AddLumpedPort(1, 50.0, [-rad_mm, -rad_mm, -gap_mm / 2.0],
                              [rad_mm, rad_mm, gap_mm / 2.0], "z", 1.0)
    sim = tempfile.mkdtemp(prefix="em_dip-", dir=CASE_ROOT)
    FDTD.Run(sim, cleanup=True, verbose=0)

    freq = np.linspace(f_start, f_stop, n_freq)
    port.CalcPort(sim, freq, ref_impedance=50.0)
    s11 = port.uf_ref / port.uf_inc
    s11db = 20.0 * np.log10(np.clip(np.abs(s11), 1e-9, None))
    i = int(np.argmin(s11db))
    return {
        "problem": "dipole_s11",
        "length_mm": length_mm, "radius_mm": rad_mm, "gap_mm": gap_mm,
        "mesh_res_mm": round(mesh_res, 6),
        "n_cells": int(mesh.GetQtyLines("x") * mesh.GetQtyLines("y")
                       * mesh.GetQtyLines("z")),
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

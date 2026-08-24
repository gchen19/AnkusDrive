"""Tier-2 dynamic confirmation — actually SPIN the gearbox and measure the ratio.

The Tier-1 oracle (ankusdrive/mechanism.py) is closed-form: it says DOF and ratio.
This drives the input shaft in PyBullet (ankusdrive/analysis/mbd.py) and MEASURES
omega_out/omega_in, the ground truth that the closed-form must reproduce:

  FUNCTIONAL  one engaged pair -> output spins up to omega_out/omega_in = -N_in/N_out.
  LOCKED      all pairs coupled at once -> conflicting gear constraints fight; the
              train barely turns (the moving image of the over-constraint).

Runs mbd directly in the venv (FreeCAD-free; pybullet lives in .venv, not the
freecadcmd worker). Writes results/gearbox_real/report_sim.json.

  .venv/bin/python3 scratch/gearbox_sim.py
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import gearbox_real as gb                       # noqa: E402
from ankusdrive.analysis import mbd               # noqa: E402

W_IN = 10.0           # input drive speed (rad/s)
C_M = gb.C / 1000.0   # centre distance in metres


def _two_shaft_spec(gears, driver_force=50.0):
    """Fixed base + two revolute shafts on the Z axis, gear-coupled. Shafts are thin
    boxes; inertia detail is irrelevant to the steady-state velocity ratio."""
    def shaft(name, x):
        return {"name": name, "half_extents_m": [0.005, 0.005, 0.03], "mass_kg": 0.05,
                "parent": -1, "joint_type": "revolute", "joint_axis": [0, 0, 1],
                "joint_pos_m": [x, 0, 0], "com_m": [0, 0, 0]}
    return {"base": {"half_extents_m": [0.01, 0.01, 0.01], "mass_kg": 0.0,
                     "pos_m": [0, 0, 0]},
            "links": [shaft("input_shaft", 0.0), shaft("output_shaft", C_M)],
            "drivers": [{"link": 0, "target_velocity": W_IN, "max_force": driver_force}],
            "gears": gears}


def _measure(out):
    wi = out["mean_joint_velocity"]["input_shaft"]
    wo = out["mean_joint_velocity"]["output_shaft"]
    return wi, wo, (wo / wi if abs(wi) > 1e-6 else None)


def main():
    rows = []
    print(f"== Tier-2: spin the gearbox, measure omega_out/omega_in  (drive {W_IN} rad/s) ==")
    print("\n-- FUNCTIONAL: one engaged pair at a time --")
    for s, ratio in enumerate(gb.RATIOS):
        ni, no = gb.teeth(ratio)
        # PyBullet gearRatio = Nb/Na (it enforces omega_b = -omega_a/ratio); the
        # output gear has no teeth, so ratio = N_out/N_in.
        gears = [{"link_a": 0, "link_b": 1, "ratio": no / ni, "axis": [0, 0, 1],
                  "max_force": 2e3}]
        out = mbd.run_mbd(_two_shaft_spec(gears), duration_s=3.0)
        wi, wo, meas = _measure(out)
        track = abs(wi) / W_IN               # how well the input held its command
        design = -ni / no                    # closed-form omega_out/omega_in (external)
        err = abs(meas - design) if meas is not None else None
        ok = err is not None and err < 0.02 * abs(design) + 0.02 and abs(track - 1) < 0.1
        print(f"  speed{s+1}: {ni:2d}/{no:2d}T  measured={meas:+.4f}  "
              f"closed-form={design:+.4f}  err={err:.4f}  input@{track*100:.0f}%  "
              f"{'OK' if ok else 'MISS'}")
        rows.append({"speed": s + 1, "teeth": [ni, no], "measured_ratio": round(meas, 5),
                     "closed_form": round(design, 5), "err": round(err, 5),
                     "input_track": round(track, 3), "ok": ok})

    print("\n-- LOCKED: all pairs coupled to the same two shafts at once --")
    all_gears = [{"link_a": 0, "link_b": 1, "ratio": gb.teeth(r)[1] / gb.teeth(r)[0],
                  "axis": [0, 0, 1], "max_force": 2e3} for r in gb.RATIOS]
    out = mbd.run_mbd(_two_shaft_spec(all_gears, driver_force=50.0), duration_s=3.0)
    wi, wo, meas = _measure(out)
    in_frac = abs(wi) / W_IN
    off_command = abs(in_frac - 1.0)
    print(f"  input reached {wi:+.3f} rad/s ({in_frac*100:.0f}% of the {W_IN} commanded)")
    print(f"  output {wo:+.3f} rad/s -> ratio {meas if meas is None else round(meas,4)}")
    # the lock signature: conflicting constraints have NO consistent solution, so the
    # input cannot hold its commanded speed — it either stalls or diverges. A valid
    # single pair tracks command to ~100% (off_command ~ 0).
    locked = off_command > 0.3
    why = ("LOCKED (over-constrained: input cannot hold command — "
           f"off by {off_command*100:.0f}%)") if locked else "turned cleanly"
    print(f"  verdict: {why}")
    locked_row = {"input_reached_frac": round(in_frac, 3), "off_command": round(off_command, 3),
                  "input_w": round(wi, 4), "output_w": round(wo, 4), "locked": locked}

    out_dir = REPO / "results" / "gearbox_real"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = {"experiment": "gearbox_tier2_sim", "engine": "pybullet",
           "drive_rad_s": W_IN, "functional": rows, "locked": locked_row,
           "all_functional_match": all(r["ok"] for r in rows)}
    (out_dir / "report_sim.json").write_text(json.dumps(rep, indent=2))
    print(f"\n  all functional speeds match closed-form: {rep['all_functional_match']}")
    print(f"  report -> {out_dir/'report_sim.json'}")


if __name__ == "__main__":
    main()

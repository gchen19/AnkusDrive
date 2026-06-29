"""PyBullet multibody-dynamics checks for driftpin.analysis.mbd.

Needs the `pybullet` wheel (the `mbd` extra). When it is absent this suite SKIPS
cleanly (exit 0) so the no-FreeCAD/no-solver fast lane stays green — the solver
degradation contract is covered separately in tests/test_solve_degradation.py.
Where present, it pins the dynamics against closed-form truth: a pendulum's
gravity-holding torque is exactly m·g·(L/2), a driven link sweeps the envelope its
geometry implies, and a swept obstacle is reported as a contact *through the motion*
(the time-varying interference a static interference_check cannot see).

Run:  .venv/bin/python3 tests/test_mbd.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _has_pybullet():
    import importlib.util
    return importlib.util.find_spec("pybullet") is not None


def _pendulum_spec(driven_velocity=0.0, obstacle=None, L=1.0, m=1.0):
    spec = {
        "base": {"half_extents_m": [0.01, 0.01, 0.01], "mass_kg": 0.0, "pos_m": [0, 0, 0]},
        "links": [{
            "name": "rod", "half_extents_m": [L / 2, 0.03, 0.03], "mass_kg": m,
            "parent": -1, "joint_type": "revolute", "joint_axis": [0, 1, 0],
            "joint_pos_m": [0, 0, 0], "com_m": [L / 2, 0, 0],
        }],
        "drivers": [{"link": 0, "target_velocity": driven_velocity, "max_force": 1e4}],
    }
    if obstacle:
        spec["obstacles"] = [obstacle]
    return spec


def test_pendulum_gravity_torque_matches_closed_form():
    from driftpin.analysis import mbd
    L, m, g = 1.0, 1.0, 9.81
    r = mbd.run_mbd(_pendulum_spec(driven_velocity=0.0, L=L, m=m),
                    duration_s=0.2, dt_s=1 / 240, gravity=(0, 0, -g))
    # holding the rod horizontal: motor torque == m*g*(L/2)
    expected = m * g * (L / 2)
    got = r["max_torques"]["joint_0"]
    assert abs(got - expected) < 0.02 * expected, (got, expected)


def test_driven_link_sweeps_expected_envelope():
    from driftpin.analysis import mbd
    L = 1.0
    r = mbd.run_mbd(_pendulum_spec(driven_velocity=6.283, L=L),  # ~1 rev/s for 1 s
                    duration_s=1.0, dt_s=1 / 240, gravity=(0, 0, -9.81))
    bb = r["reachable_envelope"]["bbox_mm"]
    # COM at radius L/2 swings a full circle in x-z: x,z span ~ 2*(L/2)=L=1000 mm; y~0
    assert abs(bb[0] - 1000.0) < 25, bb
    assert abs(bb[2] - 1000.0) < 25, bb
    assert bb[1] < 5.0, bb


def test_collision_through_motion_two_sided():
    from driftpin.analysis import mbd
    clean = mbd.run_mbd(_pendulum_spec(driven_velocity=6.283),
                        duration_s=1.0, dt_s=1 / 240, gravity=(0, 0, -9.81))
    assert clean["collisions_through_motion"] == [], clean["collisions_through_motion"][:2]
    hit = mbd.run_mbd(
        _pendulum_spec(driven_velocity=6.283,
                       obstacle={"half_extents_m": [0.15, 0.15, 0.15], "pos_m": [0.0, 0.0, -0.6]}),
        duration_s=1.0, dt_s=1 / 240, gravity=(0, 0, -9.81))
    cols = hit["collisions_through_motion"]
    assert len(cols) > 0, "obstacle in the swing path must be detected"
    assert cols[0]["between"] == ["rod", "obstacle"], cols[0]
    assert cols[0]["t_s"] > 0.0, cols[0]


def test_orientations_track_driven_revolution():
    """A link driven about Z at a known rate has a STATIONARY com (trajectories show
    nothing), so a review video needs `orientations`: the recorded world quaternion must
    advance by omega*t about Z, sampled in lockstep with trajectories. Pins the field a
    motion video consumes (docs/archive/KICKOFF_simulation_video_capture.md, item A)."""
    import math

    from driftpin.analysis import mbd
    w, dur = 2.0, 1.0                                     # 2 rad/s for 1 s -> 2.0 rad (<pi)
    spec = {
        "base": {"half_extents_m": [0.01, 0.01, 0.01], "mass_kg": 0.0, "pos_m": [0, 0, 0]},
        "links": [{
            "name": "spinner", "half_extents_m": [0.05, 0.01, 0.01], "mass_kg": 0.2,
            "parent": -1, "joint_type": "revolute", "joint_axis": [0, 0, 1],
            "joint_pos_m": [0, 0, 0], "com_m": [0.05, 0, 0],
        }],
        "drivers": [{"link": 0, "target_velocity": w, "max_force": 1e3}],
    }
    dt, every = 1 / 240, 10                              # run_mbd's defaults
    r = mbd.run_mbd(spec, duration_s=dur, dt_s=dt, gravity=(0, 0, -9.81), sample_every=every)
    orn = r["orientations"]["spinner"]
    traj = r["trajectories"]["spinner"]
    assert len(orn) == len(traj) and len(orn) > 1, (len(orn), len(traj))
    assert all(len(q) == 4 for q in orn), orn[0]
    # com on a 0.05 m crank traces a circle of radius 50 mm — present in trajectories;
    # the spin itself is ONLY legible from the quaternion.
    qx, qy, qz, qw = orn[-1]
    assert abs(qx) < 1e-3 and abs(qy) < 1e-3, ("pure Z rotation", orn[-1])
    angle = 2.0 * math.atan2(qz, qw)                     # recovered Z angle (no wrap < pi)
    t_last = (len(orn) - 1) * every * dt                 # last sample lands before duration
    assert abs(angle - w * t_last) < 0.03, (angle, w * t_last)
    # per-sample increment is constant at omega*every*dt — the rate, read off the quats
    a0 = 2.0 * math.atan2(orn[1][2], orn[1][3])
    assert abs(a0 - w * every * dt) < 0.02, (a0, w * every * dt)


def _two_shaft_gear_spec(gears, drive=10.0, driver_force=50.0):
    """Fixed base + two revolute shafts on Z, coupled by `gears`. The Tier-2 motion
    oracle: spin the input, measure the output's steady velocity ratio."""
    def shaft(name, x):
        return {"name": name, "half_extents_m": [0.005, 0.005, 0.03], "mass_kg": 0.05,
                "parent": -1, "joint_type": "revolute", "joint_axis": [0, 0, 1],
                "joint_pos_m": [x, 0, 0], "com_m": [0, 0, 0]}
    return {"base": {"half_extents_m": [0.01, 0.01, 0.01], "mass_kg": 0.0, "pos_m": [0, 0, 0]},
            "links": [shaft("in", 0.0), shaft("out", 0.048)],
            "drivers": [{"link": 0, "target_velocity": drive, "max_force": driver_force}],
            "gears": gears}


def test_gear_coupling_reproduces_ratio():
    """A single gear coupling drives the output to omega_out/omega_in = -Na/Nb — the
    closed-form gear ratio the Tier-1 mechanism oracle predicts (12/36 -> -1/3)."""
    from driftpin.analysis import mbd
    na, nb = 12, 36
    r = mbd.run_mbd(_two_shaft_gear_spec(
        [{"link_a": 0, "link_b": 1, "ratio": nb / na, "axis": [0, 0, 1], "max_force": 2e3}]),
        duration_s=3.0)
    wi = r["mean_joint_velocity"]["in"]
    wo = r["mean_joint_velocity"]["out"]
    assert abs(wi - 10.0) < 0.5, ("input must hold command", wi)
    assert abs((wo / wi) - (-na / nb)) < 0.01, (wo / wi, -na / nb)


def test_overconstrained_gears_lock_the_train():
    """Several conflicting gear ratios between the SAME two shafts have no consistent
    solution — the driven shaft cannot hold its commanded speed (the dynamic image of
    the closed-form over-constraint). A single pair holds command; the conflicting set
    departs from it wildly."""
    from driftpin.analysis import mbd
    good = mbd.run_mbd(_two_shaft_gear_spec(
        [{"link_a": 0, "link_b": 1, "ratio": 2.0, "axis": [0, 0, 1], "max_force": 2e3}]),
        duration_s=3.0)
    bad = mbd.run_mbd(_two_shaft_gear_spec(
        [{"link_a": 0, "link_b": 1, "ratio": r, "axis": [0, 0, 1], "max_force": 2e3}
         for r in (3.0, 2.0, 1.4, 1.0, 0.7143, 0.5)]), duration_s=3.0)
    good_track = abs(good["mean_joint_velocity"]["in"]) / 10.0
    bad_track = abs(bad["mean_joint_velocity"]["in"]) / 10.0
    assert abs(good_track - 1.0) < 0.1, ("single pair holds command", good_track)
    assert abs(bad_track - 1.0) > 0.3, ("over-constrained cannot hold command", bad_track)


def test_runs_through_jobs_facility():
    # the exact async composition mechanism_simulate_submit uses: run_mbd on the
    # jobs.py background thread, polled via status/result, with content-key caching.
    from driftpin import jobs
    from driftpin.analysis import mbd
    jobs.reset()
    spec = _pendulum_spec(driven_velocity=0.0)
    key = jobs.content_key("mechanism_simulate", {"spec": "pendulum"})
    sub = jobs.submit("mechanism_simulate",
                      lambda: mbd.run_mbd(spec, duration_s=0.1, dt_s=1 / 240), key=key)
    for _ in range(500):
        if jobs.status(sub["job_id"])["status"] != "running":
            break
        time.sleep(0.01)
    res = jobs.result(sub["job_id"])
    assert res["status"] == "done", res
    assert res["result"]["engine"] == "pybullet" and "max_torques" in res["result"], res
    # identical key -> content-hash cache hit, no re-solve
    again = jobs.submit("mechanism_simulate",
                        lambda: mbd.run_mbd(spec, duration_s=0.1), key=key)
    assert again["cache_hit"] is True, again
    jobs.reset()


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not _has_pybullet():
        print("  SKIP test_mbd — pybullet not installed (pip install 'driftpin[mbd]')")
        print("== 0 run, suite skipped ==")
        return
    tests = _discover()
    failures = []
    t_suite = time.time()
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, e, traceback.format_exc()))
            print(f"  FAIL {name:48s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:48s} ({time.time() - t0:.2f}s)")

    print()
    total = time.time() - t_suite
    if failures:
        print(f"== {len(failures)}/{len(tests)} failed  ({total:.1f}s) ==")
        for name, _, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(tests)}/{len(tests)} passed  ({total:.1f}s) ==")


if __name__ == "__main__":
    main()

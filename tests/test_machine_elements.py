"""Machine-element rating toys — two-sided oracles for
driftpin.analysis.machine_elements.

Pure-Python, no FreeCAD. Each check pins a closed-form result against a hand
calculation AND verifies a deliberately-bad input is caught, mirroring
tests/TOYS.md and docs/SIMULATION_EXAMPLES.md (family 10).

Run:  python3 tests/test_machine_elements.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import machine_elements as me  # noqa: E402


def test_tensile_stress_area_M10():
    # M10 coarse (p=1.5): standard At = 58.0 mm^2
    at = me.tensile_stress_area_mm2(10.0)
    assert abs(at - 58.0) < 0.2, at


def test_bolted_joint_preload_from_torque():
    # M10, K=0.2, T=50 N*m -> F = T/(K d) = 50/(0.2*0.010) = 25000 N
    r = me.bolted_joint_check(bolt_dia_mm=10.0, torque_nm=50.0, k_factor=0.2)
    assert abs(r["preload_n"] - 25000) < 5, r["preload_n"]
    # stress = 25000 / 58.0 = 431 MPa
    assert abs(r["bolt_stress_mpa"] - 431) < 3, r["bolt_stress_mpa"]
    assert r["pass"] is True


def test_bolted_joint_overload_is_caught():
    # 200 N*m on an M10 -> 100 kN preload, well past proof -> must fail
    r = me.bolted_joint_check(bolt_dia_mm=10.0, torque_nm=200.0)
    assert r["pass"] is False
    assert "proof" in r["governing"]
    # and a missing drive input must raise, not assume zero
    try:
        me.bolted_joint_check(bolt_dia_mm=10.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without torque/preload")


def test_bolted_joint_separation():
    # preload 20 kN, C=0.3 -> P_sep = 20000/0.7 = 28571 N; load 10 kN -> margin 2.86
    r = me.bolted_joint_check(bolt_dia_mm=12.0, preload_n=20000.0,
                              external_load_n=10000.0, joint_stiffness_ratio=0.3)
    assert abs(r["separation_load_n"] - 28571) < 5, r["separation_load_n"]
    assert abs(r["separation_margin"] - 2.86) < 0.02, r["separation_margin"]


def test_bearing_life_l10():
    # C/P = 10, ball p=3 -> 1000 Mrev; at 1500 rpm -> 11111 h
    r = me.bearing_life(dynamic_load_c_n=30000, equivalent_load_p_n=3000,
                        speed_rpm=1500, kind="ball")
    assert abs(r["l10_million_rev"] - 1000) < 1, r["l10_million_rev"]
    assert abs(r["l10_hours"] - 11111) < 5, r["l10_hours"]
    # roller exponent 10/3 gives MORE life at the same ratio
    roller = me.bearing_life(30000, 3000, 1500, kind="roller")
    assert roller["l10_million_rev"] > r["l10_million_rev"]
    # target gating + bad load
    assert me.bearing_life(30000, 3000, 1500, target_hours=20000)["pass"] is False
    try:
        me.bearing_life(30000, 0, 1500)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for zero load")


def test_spring_rate_and_stress():
    # d=2, D=16 (C=8), Na=10, G=79300 -> k = G d^4/(8 D^3 Na) = 3.872 N/mm
    # Wahl Kw(C=8)=1.184; F=50 -> tau = Kw*8FD/(pi d^3) = 301.5 MPa
    r = me.spring_check(wire_dia_mm=2.0, coil_mean_dia_mm=16.0, active_coils=10,
                        force_n=50.0, shear_modulus_mpa=79300.0)
    assert r["spring_index"] == 8.0
    assert abs(r["wahl_factor"] - 1.184) < 0.001, r["wahl_factor"]
    assert abs(r["rate_n_mm"] - 3.872) < 0.01, r["rate_n_mm"]
    assert abs(r["shear_stress_mpa"] - 301.5) < 1.0, r["shear_stress_mpa"]
    assert abs(r["deflection_mm"] - 12.91) < 0.1, r["deflection_mm"]


def test_spring_buckling_and_material_G():
    # long free length flags buckling (slenderness > 2.6)
    r = me.spring_check(2.0, 16.0, 10, force_n=50.0, free_length_mm=60.0)
    assert r["slenderness"] is not None and r["buckling_flag"] is True
    # material-derived G (Steel-1045: E=205000, nu=0.29 -> G=79457) ~ default band
    assert abs(r["rate_n_mm"] - 3.88) < 0.05, r["rate_n_mm"]
    # neither force nor deflection -> raise
    try:
        me.spring_check(2.0, 16.0, 10)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without force/deflection")


def test_gear_bending_lewis():
    # m=4, z=20, b=40, Ft=3000; Y=0.484-2.87/20=0.3405
    # sigma = Ft/(b m Y) = 3000/(40*4*0.3405) = 55.1 MPa
    r = me.gear_rating(module_mm=4.0, teeth=20, face_width_mm=40.0,
                       tangential_force_n=3000.0)
    assert abs(r["lewis_form_factor"] - 0.3405) < 1e-4, r["lewis_form_factor"]
    assert abs(r["bending_stress_mpa"] - 55.1) < 0.5, r["bending_stress_mpa"]
    assert r["pitch_dia_mm"] == 80.0
    assert r["bending_sf"] > 1.0 and r["pass"] is True


def test_gear_power_path():
    # pitch dia 80 mm at 1000 rpm -> V = pi*0.08*1000/60 = 4.189 m/s
    # P=10000 W -> Ft = P/V = 2387 N
    r = me.gear_rating(4.0, 20, 40.0, power_w=10000.0, pinion_speed_rpm=1000.0)
    assert abs(r["pitch_line_velocity_m_s"] - 4.189) < 0.01, r["pitch_line_velocity_m_s"]
    assert abs(r["tangential_force_n"] - 2387) < 5, r["tangential_force_n"]
    try:
        me.gear_rating(4.0, 20, 40.0)  # no force, no power
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without Ft/power")


def test_belt_drive_eytelwein():
    # d=100, D=300, C=500 -> theta = pi-2asin(0.2) = 156.9 deg
    # V = pi*0.1*1450/60 = 7.592 m/s; Fe = 5000/V = 658.6 N
    # R = e^(0.3*2.739) = 2.274; T1 = Fe*R/(R-1)=1175, T2 = Fe/(R-1)=517
    r = me.belt_drive(power_w=5000, small_pulley_dia_mm=100, large_pulley_dia_mm=300,
                      center_distance_mm=500, small_pulley_rpm=1450, friction_coef=0.3)
    assert abs(r["wrap_angle_deg"] - 156.9) < 0.2, r["wrap_angle_deg"]
    assert abs(r["belt_speed_m_s"] - 7.592) < 0.01, r["belt_speed_m_s"]
    assert abs(r["effective_force_n"] - 658.6) < 1.0, r["effective_force_n"]
    assert abs(r["tension_ratio"] - 2.274) < 0.005, r["tension_ratio"]
    assert abs(r["tight_side_n"] - 1175) < 2, r["tight_side_n"]
    assert abs((r["tight_side_n"] - r["slack_side_n"]) - r["effective_force_n"]) < 1.0
    # V-belt groove raises effective friction -> ratio climbs, slack tension drops
    vb = me.belt_drive(5000, 100, 300, 500, 1450, friction_coef=0.3, vbelt_groove_deg=38)
    assert vb["tension_ratio"] > r["tension_ratio"]
    # tight-side limit below requirement -> fail
    lim = me.belt_drive(5000, 100, 300, 500, 1450, tight_side_limit_n=1000)
    assert lim["pass"] is False


def test_press_fit_lame():
    # shaft 50 (rc=25), hub OD 100 (ro=50), interference 0.05 diametral, L=40, A36
    # p = 0.025*200000*(2500-625)/(2*25*2500) = 75 MPa
    # hoop = 75*(3125/1875) = 125 MPa; torque = 2pi*0.15*75*625*40 = 1767 N*m
    r = me.press_fit_stress(shaft_dia_mm=50, hub_outer_dia_mm=100,
                            interference_mm=0.05, engagement_length_mm=40,
                            material="Steel-A36")
    assert abs(r["contact_pressure_mpa"] - 75.0) < 0.5, r["contact_pressure_mpa"]
    assert abs(r["hub_hoop_stress_mpa"] - 125.0) < 0.5, r["hub_hoop_stress_mpa"]
    assert abs(r["torque_capacity_nm"] - 1767) < 3, r["torque_capacity_nm"]
    assert abs(r["axial_force_n"] - 70686) < 50, r["axial_force_n"]
    assert abs(r["hub_yield_sf"] - 2.0) < 0.02, r["hub_yield_sf"]  # A36 250/125
    # double the interference -> double the pressure (linear)
    r2 = me.press_fit_stress(50, 100, 0.10, 40, material="Steel-A36")
    assert abs(r2["contact_pressure_mpa"] - 2 * r["contact_pressure_mpa"]) < 0.5
    # invalid geometry raises
    try:
        me.press_fit_stress(50, 40, 0.05, 40)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when hub <= shaft")


def test_seal_check_squeeze_and_fill():
    # W=3.0, depth=2.4, width=3.9 -> squeeze 20%, fill 75.5%
    r = me.seal_check(cross_section_dia_mm=3.0, groove_depth_mm=2.4, groove_width_mm=3.9)
    assert abs(r["squeeze_pct"] - 20.0) < 0.1, r["squeeze_pct"]
    assert abs(r["gland_fill_pct"] - 75.5) < 0.3, r["gland_fill_pct"]
    assert r["pass"] is True
    # over-squeeze (shallow groove) caught
    over = me.seal_check(3.0, 2.0, 3.9)  # squeeze 33% > 30%
    assert over["within_squeeze"] is False and over["pass"] is False
    # over-fill (narrow groove) caught
    tight = me.seal_check(3.0, 2.4, 2.5)  # fill ~118% > 90%
    assert tight["within_fill"] is False and tight["pass"] is False


# --- runner -------------------------------------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
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

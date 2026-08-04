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
    # a resolved E says where it came from, and a full yield check decides `pass`
    assert r["youngs_modulus_mpa"] == 200000.0, r["youngs_modulus_mpa"]
    assert r["youngs_basis"] == "material", r["youngs_basis"]
    assert r["hub_yield_basis"] == "material", r["hub_yield_basis"]
    assert r["pass"] is True and r["warnings"] == [], r


def test_unknown_modulus_raises_instead_of_assuming_steel(): # issue #269
    # Every output is linear in E, so the old `or 200000.0` tail rated an aluminium
    # or glass hub with STEEL's modulus and said nothing — ~2.9x unsafe. It must
    # refuse, and the message must name both exits.
    try:
        me.press_fit_stress(20, 40, 0.05, 30, material="Fused-Silica")
    except ValueError as e:
        msg = str(e)
    else:
        raise AssertionError("expected ValueError for a material with no modulus")
    assert "youngs_modulus_mpa" in msg, msg      # exit 1: the override
    assert "Fused-Silica" in msg, msg            # ...and that the card DOES exist
    assert "youngs_mpa" in msg, msg              # ...but lacks this property
    # an entirely unknown material refuses too, rather than silently steel
    try:
        me.press_fit_stress(20, 40, 0.05, 30, material="Unobtainium-7")
    except ValueError as e:
        assert "material_list" in str(e), str(e)
    else:
        raise AssertionError("expected ValueError for an unknown material")
    # the override is the documented way through, and it is reported as explicit
    r = me.press_fit_stress(20, 40, 0.05, 30, material="Fused-Silica",
                            youngs_modulus_mpa=73000.0)
    assert r["youngs_basis"] == "explicit", r["youngs_basis"]
    assert abs(r["youngs_modulus_mpa"] - 73000.0) < 1e-6, r["youngs_modulus_mpa"]
    # steel's E would have been 2.74x this one — the size of the old silent error
    steel = me.press_fit_stress(20, 40, 0.05, 30, youngs_modulus_mpa=200000.0)
    ratio = steel["contact_pressure_mpa"] / r["contact_pressure_mpa"]
    assert abs(ratio - 200000.0 / 73000.0) < 0.01, ratio


def test_unperformed_yield_check_is_not_a_pass(): # issue #269
    # A card with no yield_mpa leaves the hub check UNRUN. The old code returned
    # pass:True for that — the silent pass #248/#261 exist to prevent. It must be
    # neither True nor False, and it must say why.
    r = me.press_fit_stress(20, 40, 0.05, 30, material="Fused-Silica",
                            youngs_modulus_mpa=73000.0)
    assert r["pass"] is None, r["pass"]
    assert r["hub_yield_sf"] is None, r["hub_yield_sf"]
    assert r["hub_yield_basis"] == "unavailable", r["hub_yield_basis"]
    assert any("not performed" in w for w in r["warnings"]), r["warnings"]
    # and it must not be mistaken for a pass by a truthiness test either
    assert not (r["pass"] is True), r["pass"]
    # a material that CAN be checked still decides normally, both ways
    ok = me.press_fit_stress(50, 100, 0.05, 40, material="Steel-A36")
    assert ok["pass"] is True and ok["warnings"] == [], ok
    bad = me.press_fit_stress(50, 100, 0.30, 40, material="Steel-A36")
    assert bad["pass"] is False, bad["hub_yield_sf"]


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


def test_chain_drive_ansi_rating():
    # #40 chain (P=0.5"), 17-tooth small sprocket at 100 rpm.
    # Type I (link-plate fatigue): HP1 = 0.004*17^1.08*100^0.9*0.5^(3-0.035)
    #   17^1.08=21.30, 100^0.9=63.10, 0.5^2.965=0.1280 -> HP1 = 0.688 HP
    #   -> 0.688*745.7 = 513 W. Manufacturer #40/17T/100rpm catalog ~0.69 HP.
    r = me.chain_drive(teeth_small=17, speed_rpm=100.0, chain_number="40")
    hp1 = 0.004 * 17 ** 1.08 * 100 ** 0.9 * 0.5 ** (3.0 - 0.07 * 0.5)
    assert abs(r["type1_power_w"] - hp1 * 745.699872) < 1.0, r["type1_power_w"]
    assert abs(r["type1_power_w"] - 513.0) < 3.0, r["type1_power_w"]
    # at 100 rpm the link-plate envelope is far below the roller-impact one,
    # so it governs and sets the rating.
    assert r["governing"] == "link_plate_fatigue"
    assert r["rated_power_w"] == r["type1_power_w"]
    assert abs(r["chain_pitch_mm"] - 12.7) < 1e-6
    # metric pitch and the named chain must agree (same source of geometry)
    r_mm = me.chain_drive(17, 100.0, chain_pitch_mm=12.7)
    assert abs(r_mm["rated_power_w"] - r["rated_power_w"]) < 0.5


def test_chain_drive_speed_crossover_and_strands():
    # Type II (roller impact) falls as n^-1.5 while Type I rises as n^0.9, so at
    # high speed the roller-impact envelope governs and rating drops.
    fast = me.chain_drive(17, 3000.0, chain_number="40")
    assert fast["governing"] == "roller_impact"
    assert fast["rated_power_w"] == fast["type2_power_w"]
    # multi-strand scales by the B29.1 factor (2 strands -> 1.7x, not 2x)
    single = me.chain_drive(17, 500.0, chain_number="40")
    double = me.chain_drive(17, 500.0, chain_number="40", strands=2)
    assert abs(double["rated_power_w"] / single["rated_power_w"] - 1.7) < 1e-6
    # required-power gating: a demand above the rating fails
    loaded = me.chain_drive(17, 500.0, chain_number="40",
                            power_w=single["rated_power_w"] * 2.0)
    assert loaded["pass"] is False and loaded["power_sf"] < 1.0
    # bad inputs raise
    for bad in (lambda: me.chain_drive(2, 100, chain_number="40"),
                lambda: me.chain_drive(17, 0, chain_number="40"),
                lambda: me.chain_drive(17, 100, chain_number="99")):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError on bad chain input")


def test_weld_group_line_properties_rectangle():
    # Rectangle 100x100 welded on all 4 sides, treat-as-a-line. Blodgett's closed
    # form for a box b x d: J_w = (b+d)^3/6 = 200^3/6 = 1.3333e6 mm^3 (unit throat),
    # Ix = Iy = J/2 = 6.6667e5 by symmetry.
    seg = [((0, 0), (100, 0)), ((100, 0), (100, 100)),
           ((100, 100), (0, 100)), ((0, 100), (0, 0))]
    r = me.weld_group(seg, force_n=[0, 0], load_point_mm=[50, 50])
    assert abs(r["weld_length_mm"] - 400.0) < 1e-6
    assert r["centroid_mm"] == [50.0, 50.0]
    assert abs(r["J_mm3"] - (200.0 ** 3 / 6.0)) < 1.0, r["J_mm3"]
    assert abs(r["Ix_mm3"] - r["J_mm3"] / 2.0) < 1.0, r["Ix_mm3"]
    assert abs(r["Iy_mm3"] - r["J_mm3"] / 2.0) < 1.0


def test_weld_group_direct_and_torsional_shear():
    # Two vertical 200 mm welds 100 mm apart, load 50 kN downward on the centroid
    # line but offset 150 mm horizontally -> direct + torsional shear.
    # Group: centroid at (50,100). L=400. Ix (about x) = 2*(200^3/12)=1.333e6;
    # Iy = 2*(200*50^2)=1.0e6; J=2.333e6.
    seg = [((0, 0), (0, 200)), ((100, 0), (100, 200))]
    r = me.weld_group(seg, force_n=[0, -50000.0], load_point_mm=[200, 100],
                      leg_mm=8.0, allowable_shear_mpa=96.0)
    assert abs(r["weld_length_mm"] - 400.0) < 1e-6
    assert r["centroid_mm"] == [50.0, 100.0]
    assert abs(r["J_mm3"] - (1.0e6 + 1.0e6 / 3.0 * 4.0)) < 5.0, r["J_mm3"]
    # direct shear = 50000/400 = 125 N/mm
    assert abs(r["direct_shear_n_per_mm"] - 125.0) < 1e-3, r["direct_shear_n_per_mm"]
    # eccentric moment T = (200-50)*(-50000) = -7.5e6 N*mm; worst corner is a top
    # or bottom outer end where torsion adds to direct shear.
    # f_tx = -T*(y-yc)/J, f_ty = T*(x-xc)/J at (100,200):
    T = (200 - 50) * (-50000.0)
    J = r["J_mm3"]
    ftx = -T * (200 - 100) / J
    fty = T * (100 - 50) / J
    fr = ((0 + ftx) ** 2 + (-125.0 + fty) ** 2) ** 0.5
    assert abs(r["max_shear_n_per_mm"] - fr) < 0.5, (r["max_shear_n_per_mm"], fr)
    # throat stress = f_r/(0.707*leg); SF vs allowable
    throat = fr / (0.707 * 8.0)
    assert abs(r["throat_stress_mpa"] - throat) < 0.1, r["throat_stress_mpa"]
    assert abs(r["shear_sf"] - 96.0 / throat) < 0.01
    # required leg with no leg given
    r2 = me.weld_group(seg, force_n=[0, -50000.0], load_point_mm=[200, 100])
    assert abs(r2["required_leg_mm"] - fr / (0.707 * 96.0)) < 1e-3
    # empty geometry raises
    try:
        me.weld_group([], force_n=[0, -1], load_point_mm=[0, 0])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty weld group")


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

"""Internal-flow toys — exact oracles for driftpin.analysis.cfd.

Pure-Python, no FreeCAD, no CFD solver. The straight circular pipe is the kickoff's
unambiguous CFD gate: laminar flow obeys Hagen–Poiseuille Δp = 128·μ·L·Q/(π·D⁴)
exactly, with a sharp D⁴ scaling (halve the bore → 16× Δp) that catches a mis-scaled
solver. Also pins the Reynolds-number regime classification.

Run:  python3 tests/test_cfd.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import cfd  # noqa: E402


def test_laminar_matches_hagen_poiseuille():
    r = cfd.pipe_pressure_drop(diameter_mm=10, length_mm=1000, flow_rate_lpm=0.5,
                               fluid="water-20c")
    assert r["regime"] == "laminar" and r["reynolds"] < 2300, r
    # f = 64/Re makes the Darcy drop identical to Hagen–Poiseuille
    assert abs(r["pressure_drop_pa"] - r["hagen_poiseuille_pa"]) < 1e-6, r
    assert r["laminar"] is True


def test_d4_scaling_law():
    # halving the diameter at fixed flow rate -> ~16x pressure drop (laminar)
    a = cfd.pipe_pressure_drop(diameter_mm=10, length_mm=1000, flow_rate_lpm=0.3)
    b = cfd.pipe_pressure_drop(diameter_mm=5, length_mm=1000, flow_rate_lpm=0.3)
    assert a["regime"] == "laminar" and b["regime"] == "laminar", (a["regime"], b["regime"])
    assert abs(b["pressure_drop_pa"] / a["pressure_drop_pa"] - 16.0) < 0.05, \
        (a["pressure_drop_pa"], b["pressure_drop_pa"])


def test_reynolds_regime_classification():
    lam = cfd.pipe_pressure_drop(diameter_mm=10, length_mm=500, flow_rate_lpm=0.2)
    turb = cfd.pipe_pressure_drop(diameter_mm=50, length_mm=1000, velocity_m_s=2.0)
    assert lam["regime"] == "laminar", lam["reynolds"]
    assert turb["regime"] == "turbulent" and turb["reynolds"] > 4000, turb["reynolds"]
    # Blasius turbulent friction factor is in a sane range
    assert 0.01 < turb["friction_factor"] < 0.05, turb["friction_factor"]


def test_explicit_fluid_and_errors():
    # explicit mu/rho overrides the table
    r = cfd.pipe_pressure_drop(diameter_mm=20, length_mm=1000, velocity_m_s=0.1,
                               mu_pa_s=1.0e-3, rho_kg_m3=1000.0)
    assert r["pressure_drop_pa"] > 0 and r["wall_shear_pa"] > 0, r
    for bad in (
        lambda: cfd.pipe_pressure_drop(diameter_mm=10, length_mm=1000),          # no flow
        lambda: cfd.pipe_pressure_drop(diameter_mm=0, length_mm=1000, velocity_m_s=1),
        lambda: cfd.pipe_pressure_drop(diameter_mm=10, length_mm=1000, velocity_m_s=1,
                                       fluid="unobtainium"),                      # unknown fluid
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# --- external-flow oracles (P3 M3): Stokes sphere + Blasius flat plate ---------

def test_stokes_sphere_cd_is_24_over_re():
    # creeping flow: a 2 mm sphere in glycerin at 1 mm/s -> Re << 1, Cd = 24/Re,
    # F = 6 pi mu U R exactly.
    r = cfd.stokes_sphere_drag(diameter_mm=2.0, velocity_m_s=0.001, fluid="glycerin-20c")
    assert r["reynolds"] < 1.0 and r["stokes_valid"] is True, r
    assert abs(r["cd"] * r["reynolds"] - 24.0) < 0.05, r       # Cd·Re ≡ 24 (Stokes)
    assert abs(r["cd"] - r["cd_stokes"]) < 1e-9, r
    # F = 6 pi mu U R (mu_glycerin = 1.41, R = 1e-3, U = 1e-3)
    import math
    assert abs(r["drag_force_n"] - 6 * math.pi * 1.41 * 0.001 * 0.001) < 1e-12, r


def test_stokes_high_re_flags_invalid():
    # a fast sphere in water is far past creeping flow: stokes_valid is False
    r = cfd.stokes_sphere_drag(diameter_mm=20.0, velocity_m_s=1.0, fluid="water-20c")
    assert r["reynolds"] > 1.0 and r["stokes_valid"] is False, r["reynolds"]


def test_blasius_flat_plate_cf():
    # air over a 0.1 m plate at 5 m/s -> Re_L ~ 3.3e4 (laminar), Cf = 1.328/sqrt(Re_L)
    import math
    r = cfd.flat_plate_drag(length_mm=100, velocity_m_s=5.0, fluid="air-20c")
    assert r["laminar"] is True, r["reynolds_l"]
    assert abs(r["cf_avg"] - 1.328 / math.sqrt(r["reynolds_l"])) < 1e-6, r
    # drag per unit width = Cf * 0.5 rho U^2 * L (returned values are display-rounded,
    # so compare with a relative tolerance)
    recomputed = r["cf_avg"] * r["dynamic_pressure_pa"] * 0.1
    assert abs(r["drag_per_width_n_m"] - recomputed) < 1e-4 * r["drag_per_width_n_m"], r


def test_blasius_u_to_the_1p5_scaling():
    # Blasius friction drag ∝ U^1.5 (F = 1.328/sqrt(Re_L) * 0.5 rho U^2 L ∝ U^1.5)
    a = cfd.flat_plate_drag(length_mm=100, velocity_m_s=1.0, fluid="air-20c")
    b = cfd.flat_plate_drag(length_mm=100, velocity_m_s=4.0, fluid="air-20c")
    assert abs(b["drag_force_n"] / a["drag_force_n"] - 4.0 ** 1.5) < 1e-6, (a, b)


def test_external_input_validation():
    for bad in (
        lambda: cfd.stokes_sphere_drag(diameter_mm=0, velocity_m_s=1),
        lambda: cfd.stokes_sphere_drag(diameter_mm=1, velocity_m_s=0),
        lambda: cfd.flat_plate_drag(length_mm=0, velocity_m_s=1),
        lambda: cfd.flat_plate_drag(length_mm=10, velocity_m_s=1, fluid="unobtainium"),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")



# --- B3 turbulent oracles (Colebrook + 1/7-power plate) -------------------------

def test_colebrook_tracks_blasius_when_smooth():
    # smooth pipe: Colebrook and Blasius agree within ~2 % below Re ~ 1e5
    import math
    for re_d in (5e3, 2e4, 1e5):
        cole = cfd.colebrook_friction_factor(re_d)
        blas = 0.316 * re_d ** -0.25
        assert abs(cole / blas - 1) < 0.03, (re_d, cole, blas)
    # fully-rough limit: f -> von Karman (2*log10(3.7/(eps/D)))^-2, Re-independent
    vk = (2 * math.log10(3.7 / 0.002)) ** -2
    assert abs(cfd.colebrook_friction_factor(1e9, 0.002) / vk - 1) < 1e-3
    hi = cfd.colebrook_friction_factor(1e8, 0.002)
    lo = cfd.colebrook_friction_factor(1e9, 0.002)
    assert abs(hi / lo - 1) < 0.01      # Re no longer matters when fully rough
    # laminar Re refused (it is a turbulent correlation)
    try:
        cfd.colebrook_friction_factor(1000)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError below Re=4000")


def test_roughness_raises_pressure_drop_monotonically():
    smooth = cfd.pipe_pressure_drop(50, 1000, velocity_m_s=2.0)
    rough = cfd.pipe_pressure_drop(50, 1000, velocity_m_s=2.0, roughness_mm=0.1)
    rougher = cfd.pipe_pressure_drop(50, 1000, velocity_m_s=2.0, roughness_mm=0.5)
    assert smooth["friction_factor"] < rough["friction_factor"] < rougher["friction_factor"]
    # smooth default keeps the historical Blasius factor; Colebrook rides along
    assert abs(smooth["friction_factor"] - 0.316 * smooth["reynolds"] ** -0.25) < 1e-5
    assert smooth["colebrook_friction_factor"] is not None
    # fidelity contract: laminar exact, turbulent banded correlation
    lam = cfd.pipe_pressure_drop(50, 1000, velocity_m_s=0.01)
    assert lam["fidelity"] == "exact" and lam["band_pct"] is None, lam
    assert smooth["fidelity"] == "correlation" and smooth["band_pct"] == 10.0
    assert smooth["escalate_to"] == "cfd_internal_flow_submit"


def test_turbulent_plate_correlations():
    import math
    # air, 30 m/s over 1 m: Re_L = 2e6 — turbulent regime
    t = cfd.flat_plate_drag_turbulent(1000, 30, fluid="air-20c")
    assert t["valid_range_ok"] is True, t["warnings"]
    # exact identity of the form: cf_turbulent * Re^(1/5) == 0.074
    assert abs(t["cf_turbulent"] * t["reynolds_l"] ** 0.2 - 0.074) < 1e-6
    # the mixed (laminar leading-run) form is below fully-turbulent, above laminar
    assert t["cf_laminar_blasius"] < t["cf_mixed"] < t["cf_turbulent"], t
    # mixed -> fully-turbulent as Re grows (the laminar run stops mattering)
    big = cfd.flat_plate_drag_turbulent(10000, 150, fluid="air-20c")  # Re = 1e8
    assert big["cf_mixed"] / big["cf_turbulent"] > 0.97
    assert big["valid_range_ok"] is False        # above the 1e7 envelope — flagged
    # below transition the tool says so rather than answering quietly
    lam = cfd.flat_plate_drag_turbulent(100, 1, fluid="air-20c")
    assert lam["valid_range_ok"] is False and lam["warnings"], lam
    assert t["fidelity"] == "correlation" and t["band_pct"] == 15.0
    assert t["escalate_to"] == "cfd_external_flow_submit"


# --- external-flow drag screens (issue #223's oracle tier) ---------------------

def _u_for_re(re, diameter_mm, fluid="air-20c"):
    """The velocity giving Reynolds number ``re`` on ``diameter_mm``."""
    mu, rho = cfd._fluid_props(fluid, None, None)
    return re * mu / (rho * diameter_mm / 1000.0)


def test_sphere_drag_collapses_to_stokes_at_low_re():
    # Clift-Gauvin must reduce to the EXACT 24/Re as inertia vanishes
    for re, tol in ((0.01, 0.01), (0.1, 0.04)):
        r = cfd.sphere_drag(50, _u_for_re(re, 50))
        assert abs(r["cd"] / r["cd_stokes"] - 1.0) < tol, (re, r["cd"], r["cd_stokes"])
    creep = cfd.sphere_drag(50, _u_for_re(0.01, 50))
    assert creep["regime"] == "stokes" and creep["fidelity"] == "exact"
    assert creep["band_pct"] is None, creep
    # and it must agree with the independent exact Stokes tool at the same state
    exact = cfd.stokes_sphere_drag(50, _u_for_re(0.01, 50), fluid="air-20c")
    assert abs(creep["cd_stokes"] - exact["cd"]) / exact["cd"] < 1e-6, (creep, exact)


def test_sphere_drag_tracks_the_published_curve():
    # standard drag-curve landmarks (Schlichting/Clift), 5 % of the published value
    for re, cd_published in ((1.0, 27.0), (100.0, 1.09), (1000.0, 0.47)):
        r = cfd.sphere_drag(50, _u_for_re(re, 50))
        assert abs(r["cd"] / cd_published - 1.0) < 0.05, (re, r["cd"], cd_published)
    # Cd falls monotonically with Re right through the Newton plateau
    cds = [cfd.sphere_drag(50, _u_for_re(re, 50))["cd"]
           for re in (1, 10, 100, 1000, 1e4)]
    assert all(a > b for a, b in zip(cds, cds[1:])), cds
    # drag itself still RISES with velocity even though Cd falls
    slow = cfd.sphere_drag(50, 1.0)
    fast = cfd.sphere_drag(50, 10.0)
    assert fast["drag_force_n"] > slow["drag_force_n"] * 50, (slow, fast)


def test_sphere_drag_flags_the_drag_crisis_and_bad_input():
    ok = cfd.sphere_drag(50, _u_for_re(1e4, 50))
    assert ok["valid_range_ok"] is True and ok["band_pct"] == 10.0
    crisis = cfd.sphere_drag(50, _u_for_re(1e6, 50))
    assert crisis["valid_range_ok"] is False and crisis["warnings"], crisis
    assert "drag crisis" in crisis["warnings"][0]
    for bad in ((0, 10), (50, 0), (-1, 10)):
        try:
            cfd.sphere_drag(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"sphere_drag{bad} should have raised")


def test_cylinder_crossflow_tracks_the_published_curve():
    for re, cd_published in ((1.0, 10.0), (100.0, 1.45), (1e5, 1.2)):
        r = cfd.cylinder_crossflow_drag(50, _u_for_re(re, 50), length_mm=5000)
        assert abs(r["cd"] / cd_published - 1.0) < 0.05, (re, r["cd"], cd_published)
    # a cylinder is bluffer than a sphere at the same Re, all the way down
    for re in (1.0, 100.0, 1e4):
        u = _u_for_re(re, 50)
        assert (cfd.cylinder_crossflow_drag(50, u, length_mm=5000)["cd"]
                > cfd.sphere_drag(50, u)["cd"] * 0.3), re
    # short cylinders get the end-relief warning rather than a silent answer
    short = cfd.cylinder_crossflow_drag(50, 10, length_mm=100)
    assert short["valid_range_ok"] is False and any(
        "end relief" in w for w in short["warnings"]), short
    assert cfd.cylinder_crossflow_drag(50, 10, length_mm=5000)["valid_range_ok"] is True
    assert cfd.cylinder_crossflow_drag(50, 10)["band_pct"] == 15.0


def test_bluff_body_table_orders_and_overrides():
    tbl = cfd.bluff_body_drag("list")["shapes"]
    assert tbl["streamlined_body"] < tbl["car_modern"] < tbl["cube_face_on"], tbl
    # same frontal area, same speed: the bluff shape must drag far more
    area, u = 10000.0, 30.0
    bluff = cfd.bluff_body_drag("cube_face_on", area, u)
    slick = cfd.bluff_body_drag("streamlined_body", area, u)
    assert bluff["drag_force_n"] / slick["drag_force_n"] > 20, (bluff, slick)
    assert bluff["cd_source"] == "table" and bluff["band_pct"] == 20.0
    # F = Cd*q*A (q is reported rounded, hence the 1e-6 N slack)
    assert abs(bluff["drag_force_n"]
               - bluff["cd"] * bluff["dynamic_pressure_pa"] * area / 1e6) < 1e-6
    over = cfd.bluff_body_drag("whatever", area, u, cd=0.9)
    assert over["cd"] == 0.9 and over["cd_source"] == "override"
    for bad in (lambda: cfd.bluff_body_drag("no_such_shape", area, u),
                lambda: cfd.bluff_body_drag("cube_face_on"),
                lambda: cfd.bluff_body_drag("cube_face_on", 0, u),
                lambda: cfd.bluff_body_drag("cube_face_on", area, u, cd=-1)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("bluff_body_drag should have raised")


def test_solve_gate_is_per_turbulence_model_and_case_family():
    """#262: `gated` used to be `turbulence == 'laminar'`, so every turbulent
    external-flow solve shipped gated:false and a requirement demanding
    trust:{gated:true} was unsatisfiable at a realistic Reynolds number. The verdict is
    now a property of the PAIR — and an unverified pair still has to say so."""
    # the verdicts that predate #262 are unchanged
    for turb, family in (("laminar", "external_body"),
                         ("laminar", "external_flat_plate"),
                         ("kOmegaSST", "external_flat_plate"),
                         ("laminar", "internal_pipe"),
                         ("kOmegaSST", "internal_pipe")):
        g = cfd.solve_gate(turb, family)
        assert g["gated"] is True and g["oracle"] and g["reason"] is None, g

    # the #262 gate itself: RANS on an arbitrary body, inside the envelope it was
    # verified over (cube face-on vs the Cd table, live at Re = 1e4 and 1e5)
    rans = cfd.solve_gate("kOmegaSST", "external_body", reynolds=1e4)
    assert rans["gated"] is True and "cube face-on" in rans["oracle"], rans
    assert rans["reynolds_range"] == (1e4, 2e5), rans
    # ... and the same call with no Reynolds skips the envelope check rather than
    # guessing (a caller who cannot say where they are gets the pair's verdict)
    assert cfd.solve_gate("rans", "external_body")["gated"] is True

    # outside that envelope nothing has checked it: below, steady RANS is the wrong
    # model; above Re = 2e5 the drag crisis is past every correlation in the module
    for re_out in (500.0, 1e6):
        out = cfd.solve_gate("kOmegaSST", "external_body", reynolds=re_out)
        assert out["gated"] is False and "envelope" in out["reason"], out
    # the aliases the handlers accept resolve to the same verdict
    assert all(cfd.solve_gate(a, "external_body", reynolds=5e4)["gated"] is True
               for a in ("kOmegaSST", "k-omega-sst", "RANS", "turbulent"))

    # an unverified pair inherits nobody's credibility
    unknown = cfd.solve_gate("spalartAllmaras", "external_body", reynolds=1e4)
    assert unknown["gated"] is False and unknown["oracle"] is None, unknown
    assert "no verified oracle" in unknown["reason"], unknown
    assert cfd.solve_gate("laminar", "external_body_but_typoed")["gated"] is False


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

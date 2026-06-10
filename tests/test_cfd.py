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

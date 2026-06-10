"""Optics toys — the exact Snell / Fresnel / TIR oracle + the energy-conserving
ray-bundle trace, plus the rayoptics solver gate.

Two tiers, both runnable on the no-FreeCAD lane:
  * **oracle** (always): the pure-Python core in ``driftpin/analysis/optics.py`` —
    Snell refraction (30° into PMMA → 19.60°, within 0.1°), Fresnel power
    reflectance (normal-incidence air/PMMA → 3.9%, within 0.2%), the critical
    angle / total internal reflection (PMMA→air → 42.16°; above it transmission is
    exactly zero), and ``trace_bundle`` energy closure (leakage + efficiency +
    absorbed == 1 within 1%). Pure ``math``, no NumPy, no solver.
  * **solver-backed** (when the rayoptics wheel resolves, else SKIP): a flat
    n1→n2 interface traced through rayoptics must reproduce Snell to < 1e-3°, so
    the heavy trace is gated against the exact oracle.

Run:  python3 tests/test_optics.py          (oracle only on a stock interpreter)
      .venv/bin/python tests/test_optics.py  (also runs the rayoptics gate)
"""
import importlib.util
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import optics  # noqa: E402

PMMA = 1.49062
AIR = 1.0


def _has_rayoptics():
    try:
        return importlib.util.find_spec("rayoptics") is not None
    except (ImportError, ValueError):
        return False


# --- Snell (exact) ------------------------------------------------------------

def test_snell_30deg_into_pmma():
    # §7 anchor: 30° from air into PMMA refracts to asin(sin30/n).
    got = optics.refract_angle(30.0, AIR, PMMA)
    expected = math.degrees(math.asin(math.sin(math.radians(30.0)) / PMMA))
    assert abs(got - expected) < 1e-9, (got, expected)
    assert abs(got - 19.6) < 0.1, got                 # within 0.1° of the kickoff value


def test_snell_is_reversible():
    # air -> PMMA -> air returns to the launch angle (a flat interface is reciprocal)
    t = optics.refract_angle(42.0, AIR, PMMA)
    back = optics.refract_angle(t, PMMA, AIR)
    assert abs(back - 42.0) < 1e-9, (t, back)


def test_snell_normal_incidence_unbent():
    assert abs(optics.refract_angle(0.0, AIR, PMMA)) < 1e-12


# --- Fresnel (exact) ----------------------------------------------------------

def test_fresnel_normal_incidence():
    # §7 anchor: R0 = ((n1-n2)/(n1+n2))^2; air/PMMA ≈ 3.9%.
    fr = optics.fresnel_reflectance(0.0, AIR, PMMA)
    expected = ((AIR - PMMA) / (AIR + PMMA)) ** 2
    assert abs(fr["reflectance"] - expected) < 1e-12, fr
    assert abs(fr["reflectance"] * 100 - 3.9) < 0.2, fr["reflectance"]   # within 0.2%
    assert abs(fr["r_s"] - fr["r_p"]) < 1e-12, fr     # s and p coincide at θ=0
    assert abs(fr["reflectance"] + fr["transmittance"] - 1.0) < 1e-12, fr


def test_fresnel_brewster_kills_p():
    # At Brewster's angle (tan θ_B = n2/n1) the p-reflectance vanishes.
    theta_b = math.degrees(math.atan(PMMA / AIR))
    fr = optics.fresnel_reflectance(theta_b, AIR, PMMA)
    assert fr["r_p"] < 1e-9, fr["r_p"]
    assert fr["r_s"] > 0.05, fr["r_s"]                # s is very much alive


def test_fresnel_grazing_to_unity():
    fr = optics.fresnel_reflectance(89.9, AIR, PMMA)
    assert fr["reflectance"] > 0.98, fr               # grazing incidence → nearly all reflected
    assert optics.fresnel_reflectance(89.99, AIR, PMMA)["reflectance"] > 0.99


# --- critical angle / TIR (exact) ---------------------------------------------

def test_critical_angle_pmma_to_air():
    # §7 anchor: θc = asin(1/n) ≈ 42.16° for PMMA→air.
    tc = optics.critical_angle(PMMA, AIR)
    expected = math.degrees(math.asin(AIR / PMMA))
    assert abs(tc - expected) < 1e-9, (tc, expected)
    assert abs(tc - 42.16) < 0.1, tc


def test_no_critical_angle_into_denser_medium():
    assert optics.critical_angle(AIR, PMMA) is None   # air→PMMA never TIRs


def test_tir_zero_transmission():
    # Above the critical angle, transmission is exactly zero (no refracted ray).
    tc = optics.critical_angle(PMMA, AIR)
    assert optics.refract_angle(tc + 3.0, PMMA, AIR) is None
    fr = optics.fresnel_reflectance(tc + 3.0, PMMA, AIR)
    assert fr["tir"] is True and fr["transmittance"] == 0.0 and fr["reflectance"] == 1.0, fr


# --- energy-conserving bundle trace -------------------------------------------

def test_energy_closes_collimated():
    r = optics.trace_bundle(AIR, PMMA, {"kind": "collimated", "angle_deg": 0.0}, n_rays=32)
    assert abs(r["energy_balance"] - 1.0) < 0.01, r
    assert r["absorbed_fraction"] == 0.0, r
    # at normal incidence ~3.9% reflects, the rest transmits
    assert abs(r["efficiency"] - (1 - 0.0388)) < 0.01, r["efficiency"]


def test_energy_closes_cone_lambertian_absorption_target():
    for cfg, absn, target in (
        ({"kind": "cone", "half_angle_deg": 70}, 0.0, None),
        ({"kind": "lambertian", "max_angle_deg": 85}, 0.08, None),
        ({"kind": "cone", "half_angle_deg": 60}, 0.05, 25.0),
    ):
        r = optics.trace_bundle(AIR, PMMA, cfg, n_rays=64,
                                absorption=absn, target_half_angle_deg=target)
        assert abs(r["energy_balance"] - 1.0) < 0.01, (cfg, r)
        assert abs(r["absorbed_fraction"] - absn) < 1e-6, (cfg, r)
        assert 0.0 <= r["efficiency"] <= 1.0 and 0.0 <= r["leakage_fraction"] <= 1.0, r


def test_tir_bundle_traps_above_critical():
    # A PMMA→air bundle spanning the critical angle: rays past θc are trapped (TIR
    # → leakage, never transmitted). Energy still closes.
    r = optics.trace_bundle(PMMA, AIR, {"kind": "cone", "half_angle_deg": 89}, n_rays=90)
    assert r["tir_fraction"] > 0.4, r                 # most of a wide cone is past θc
    assert abs(r["energy_balance"] - 1.0) < 0.01, r
    # no transmitted ray exits beyond the (impossible) refraction of the critical ray
    assert all(b["angle_deg"] <= 90.0 for b in r["exit_distribution"])


def test_hotspot_tracks_refraction():
    # A collimated 30° beam exits at the Snell angle (~19.6°); the hotspot bin
    # brackets it.
    r = optics.trace_bundle(AIR, PMMA, {"kind": "collimated", "angle_deg": 30.0}, n_rays=16)
    snell = optics.refract_angle(30.0, AIR, PMMA)
    assert r["hotspot_locations"], r
    top = r["hotspot_locations"][0]
    assert abs(top["angle_deg"] - snell) < 5.0, (top, snell)   # within one bin


# --- source sampling ----------------------------------------------------------

def test_source_weights_sum_to_one():
    for cfg in ({"kind": "collimated", "angle_deg": 12.0},
                {"kind": "cone", "half_angle_deg": 40.0},
                {"kind": "lambertian", "max_angle_deg": 80.0}):
        _angles, weights = optics.sample_source(cfg, 50)
        assert abs(sum(weights) - 1.0) < 1e-9, (cfg, sum(weights))


def test_lambertian_favors_small_angles():
    _a, w = optics.sample_source({"kind": "lambertian", "max_angle_deg": 89.0}, 40)
    assert w[0] > w[-1], (w[0], w[-1])                # cosθ weighting → near-axis heavier


# --- rayoptics solver gate (skips when the wheel is absent) --------------------

def test_rayoptics_reproduces_snell():
    if not _has_rayoptics():
        print("    SKIP — rayoptics not installed (pip install 'driftpin[optics]')")
        return
    import numpy as np
    from rayoptics.environment import OpticalModel
    from rayoptics.raytr import raytrace
    from rayoptics.raytr.opticalspec import FieldSpec, PupilSpec, WvlSpec

    opm = OpticalModel(radius_mode=True)
    sm, osp = opm["seq_model"], opm["optical_spec"]
    osp["pupil"] = PupilSpec(osp, key=["object", "epd"], value=2.0)
    osp["fov"] = FieldSpec(osp, key=["object", "angle"], value=[0.0], is_relative=False)
    osp["wvls"] = WvlSpec([("d", 1.0)], ref_wl=0)
    sm.gaps[0].thi = 100.0
    sm.add_surface([1e10, 10.0, PMMA, 57.4])          # flat air→PMMA interface
    sm.add_surface([1e10, 0.0])
    sm.gaps[-1].thi = 10.0
    sm.set_stop()
    opm.update_model()
    wvl = sm.central_wavelength()
    path = list(sm.path(wl=wvl))
    for theta_i in (10.0, 30.0, 45.0, 60.0):
        t = math.radians(theta_i)
        ray, _opd, _w = raytrace.trace_raw(
            iter(path), np.array([0.0, 0.0, 0.0]),
            np.array([0.0, math.sin(t), math.cos(t)]), wvl)
        after = ray[1][1]
        theta_ro = math.degrees(math.acos(min(1.0, abs(after[2] / float(np.linalg.norm(after))))))
        snell = optics.refract_angle(theta_i, AIR, PMMA)
        assert abs(theta_ro - snell) < 1e-3, (theta_i, theta_ro, snell)


# --- runner (mirrors tests/test_cfd.py) ---------------------------------------

def _discover():
    return [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]


def main():
    if not _has_rayoptics():
        print("  (rayoptics absent — oracle tests run, the rayoptics gate SKIPs)")
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

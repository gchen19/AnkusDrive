"""Acoustics screening toys — two-sided oracles for ankusdrive.analysis.acoustics.

Pure-Python, no FreeCAD. Pins the rigid-cavity modes / Helmholtz resonator /
mass law / duct cutoff against exact identities and hand anchors, plus the
SIMULATION_NEXT.md fidelity contract (exact vs correlation labeled per kind).

Run:  python3 tests/test_acoustics.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import acoustics as ac  # noqa: E402


def test_cavity_fundamental_is_half_wavelength():
    # exact identity: the lowest mode of an Lx-dominant box is the axial
    # half-wave f = c/(2*Lx), index [1,0,0]
    r = ac.acoustic_screen("cavity_modes", lx_mm=4000, ly_mm=3000, lz_mm=2500)
    assert abs(r["f_fundamental_hz"] - r["c_m_s"] / 8.0) < 0.01, r["f_fundamental_hz"]
    assert r["modes"][0]["n"] == [1, 0, 0], r["modes"][0]
    assert r["fidelity"] == "exact" and r["band_pct"] is None, r
    assert r["escalate_to"] == "acoustic_fem_submit", r["escalate_to"]


def test_cavity_cube_degeneracy_and_ordering():
    # symmetry: a cube's three axial fundamentals are degenerate (equal f);
    # and the mode list is ascending
    r = ac.acoustic_screen("cavity_modes", lx_mm=1000, ly_mm=1000, lz_mm=1000,
                           n_modes=5)
    f = [m["f_hz"] for m in r["modes"]]
    assert f[0] == f[1] == f[2], f[:3]
    assert f == sorted(f) and len(f) == 5, f


def test_cavity_oblique_mode_pythagoras():
    # composition: the [1,1,0] mode of a square cross-section is exactly
    # sqrt(2) x the [1,0,0] mode
    r = ac.acoustic_screen("cavity_modes", lx_mm=2000, ly_mm=2000, lz_mm=500,
                           n_modes=8)
    by_n = {tuple(m["n"]): m["f_hz"] for m in r["modes"]}
    assert abs(by_n[(1, 1, 0)] / by_n[(1, 0, 0)] - math.sqrt(2)) < 1e-4, by_n


def test_helmholtz_anchor_and_volume_scaling():
    # hand anchor: A=314.16 mm^2 (r=10 mm), L=50 mm, V=1e6 mm^3 ->
    # L_eff = 50+17 = 67 mm, f = (c/2pi)*sqrt(A/(V*L_eff)) = 118.3 Hz
    r = ac.acoustic_screen("helmholtz", neck_area_mm2=314.16, neck_length_mm=50,
                           cavity_volume_mm3=1e6)
    assert abs(r["l_eff_mm"] - 67.0) < 0.01, r["l_eff_mm"]
    assert abs(r["f_resonance_hz"] - 118.3) < 0.5, r["f_resonance_hz"]
    assert r["fidelity"] == "correlation" and r["band_pct"] == 10.0, r
    # exact scaling identity of the formula: 4x the volume -> half the frequency
    big = ac.acoustic_screen("helmholtz", neck_area_mm2=314.16, neck_length_mm=50,
                             cavity_volume_mm3=4e6)
    assert abs(r["f_resonance_hz"] / big["f_resonance_hz"] - 2.0) < 1e-3


def test_mass_law_6db_per_doubling():
    # exact identity of the law: doubling f OR m'' adds 20*log10(2) = 6.02 dB
    base = ac.acoustic_screen("mass_law", frequency_hz=1000, surface_density_kg_m2=10)
    f2 = ac.acoustic_screen("mass_law", frequency_hz=2000, surface_density_kg_m2=10)
    m2 = ac.acoustic_screen("mass_law", frequency_hz=1000, surface_density_kg_m2=20)
    assert abs(base["tl_db"] - 33.0) < 0.01, base["tl_db"]   # 20*log10(1e4)-47
    assert abs(f2["tl_db"] - base["tl_db"] - 6.02) < 0.01
    assert abs(m2["tl_db"] - base["tl_db"] - 6.02) < 0.01
    assert base["band_db"] == 3.0 and base["fidelity"] == "correlation", base
    # negative TL (acoustically transparent) must be flagged, not silent
    thin = ac.acoustic_screen("mass_law", frequency_hz=50, surface_density_kg_m2=1)
    assert thin["tl_db"] < 0 and thin["valid_range_ok"] is False, thin


def test_duct_cutoff_exact():
    # rectangular: f_c = c/(2a); below it only plane waves -> exact
    r = ac.acoustic_screen("duct_cutoff", duct_width_mm=200)
    assert abs(r["f_cutoff_hz"] - r["c_m_s"] / 0.4) < 0.01, r
    assert r["fidelity"] == "exact", r
    # circular: ka = 1.8412 -> f_c = 1.8412*c/(pi*d)
    c = ac.acoustic_screen("duct_cutoff", duct_diameter_mm=200)
    assert abs(c["f_cutoff_hz"] - 1.8412 * c["c_m_s"] / (math.pi * 0.2)) < 0.01, c
    # a circular duct holds plane waves HIGHER than a square duct of the same
    # dimension: 1.8412/pi = 0.586 > 1/2 (exact coefficient ratio)
    assert abs(c["f_cutoff_hz"] / r["f_cutoff_hz"]
               - 2 * 1.8412 / math.pi) < 1e-4, (c["f_cutoff_hz"], r["f_cutoff_hz"])


def test_sound_speed_and_override():
    # c = sqrt(gamma*R*T): 343.2 m/s at 20 C, and rises with temperature
    assert abs(ac.speed_of_sound(20.0) - 343.2) < 0.1
    hot = ac.acoustic_screen("duct_cutoff", duct_width_mm=200, t_ambient_c=40)
    cold = ac.acoustic_screen("duct_cutoff", duct_width_mm=200, t_ambient_c=0)
    assert hot["f_cutoff_hz"] > cold["f_cutoff_hz"]
    # explicit c override wins (water, 1481 m/s)
    w = ac.acoustic_screen("duct_cutoff", duct_width_mm=200, c_m_s=1481.0)
    assert abs(w["f_cutoff_hz"] - 1481.0 / 0.4) < 0.01, w


def test_input_validation():
    for bad in (
        lambda: ac.acoustic_screen("sonar"),                          # unknown kind
        lambda: ac.acoustic_screen("cavity_modes", lx_mm=1000),       # missing dims
        lambda: ac.acoustic_screen("cavity_modes", lx_mm=1000, ly_mm=0, lz_mm=1),
        lambda: ac.acoustic_screen("helmholtz", neck_area_mm2=100),   # missing
        lambda: ac.acoustic_screen("mass_law", frequency_hz=1000),    # missing m''
        lambda: ac.acoustic_screen("duct_cutoff"),                    # no geometry
        lambda: ac.acoustic_screen("duct_cutoff", duct_width_mm=200,
                                   duct_diameter_mm=100),             # both
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


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

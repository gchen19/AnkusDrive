"""Random-vibration toys — two-sided oracles for ankusdrive.analysis.vibration.

Pure-Python, no FreeCAD. Pins the SDOF response against the exact closed form
(Miles' equation) AND the relative/limit behaviors from docs/SIMULATION_EXAMPLES.md
(family 5): exact for a single dominant mode, +√2 per Q-doubling, monotone in f_n,
and a mode stiffened above the excitation band escaping resonant drive.

Run:  python3 tests/test_vibration.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import vibration as vib  # noqa: E402

# The §5 reference PSD: flat 0.01 g²/Hz across 20–2000 Hz.
FLAT = [{"hz": 20, "g2_hz": 0.01}, {"hz": 2000, "g2_hz": 0.01}]


def test_miles_sdof_exact():
    # §5 toy: f_n=312 Hz, W=0.01, Q=10 -> GRMS = sqrt((pi/2)*312*0.01*10) = 7.0 g
    r = vib.random_vibration(frequencies_hz=[312], psd_profile=FLAT, q=10)
    expected = math.sqrt((math.pi / 2) * 312 * 0.01 * 10)   # 7.0007...
    assert abs(r["rms_g"] - expected) < 1e-3, (r["rms_g"], expected)
    assert abs(r["rms_g"] - 7.0) < 0.05, r["rms_g"]          # within 10% (in fact ~exact)
    # for one mode the SRSS response IS the SDOF Miles anchor
    assert abs(r["miles_grms_g"] - r["rms_g"]) < 1e-6, r
    assert r["first_mode_hz"] == 312 and r["dominant_mode_hz"] == 312
    assert r["modes"][0]["in_band"] is True


def test_doubling_q_raises_grms_by_sqrt2():
    r10 = vib.random_vibration(frequencies_hz=[312], psd_profile=FLAT, q=10)
    r20 = vib.random_vibration(frequencies_hz=[312], psd_profile=FLAT, q=20)
    # tolerance accommodates the 4-decimal rounding of the public rms_g
    assert abs(r20["rms_g"] / r10["rms_g"] - math.sqrt(2)) < 1e-3, (r10["rms_g"], r20["rms_g"])


def test_monotone_in_fn_within_band():
    # flat PSD -> GRMS = sqrt((pi/2)*f*W*Q), strictly increasing in f_n
    grms = [
        vib.random_vibration(frequencies_hz=[f], psd_profile=FLAT, q=10)["rms_g"]
        for f in (50, 150, 400, 900, 1800)
    ]
    assert all(b > a for a, b in zip(grms, grms[1:])), grms


def test_stiffening_above_band_escapes():
    # The design rule: a mode inside the energetic band resonates (high response);
    # one stiffened ABOVE the excitation band sees no drive (response -> 0).
    in_band = vib.random_vibration(frequencies_hz=[300], psd_profile=FLAT, q=10)
    stiffened = vib.random_vibration(frequencies_hz=[5000], psd_profile=FLAT, q=10)
    assert stiffened["rms_g"] == 0.0, stiffened              # above 2000 Hz -> W=0
    assert stiffened["modes"][0]["in_band"] is False
    assert in_band["rms_g"] > 100 * (stiffened["rms_g"] + 1e-9), (in_band, stiffened)


def test_srss_combines_modes():
    r = vib.random_vibration(frequencies_hz=[300, 600], psd_profile=FLAT, q=10)
    c0, c1 = (m["contribution_g"] for m in r["modes"])
    assert abs(r["rms_g"] - math.hypot(c0, c1)) < 1e-3, r
    # higher mode has more energy under a flat PSD (sqrt(f)), so it dominates
    assert r["dominant_mode_hz"] == 600, r
    assert r["rms_g"] > max(c0, c1), r                       # SRSS exceeds any single mode


def test_psd_loglog_interpolation_and_band():
    # sloped profile; log-log interp at the geometric mean is the geometric mean of levels
    prof = [{"hz": 20, "g2_hz": 0.001}, {"hz": 2000, "g2_hz": 0.1}]
    f_gm = math.sqrt(20 * 2000)                              # geometric-mean frequency
    w_gm = math.sqrt(0.001 * 0.1)                            # -> geometric-mean level
    assert abs(vib.psd_at(prof, f_gm) - w_gm) < 1e-9, (vib.psd_at(prof, f_gm), w_gm)
    assert vib.psd_at(prof, 10) == 0.0 and vib.psd_at(prof, 5000) == 0.0   # outside band
    # a single-breakpoint profile is flat everywhere
    assert vib.psd_at([{"hz": 100, "g2_hz": 0.05}], 9999) == 0.05


def test_stress_coupling_and_pass_fail():
    r = vib.random_vibration(frequencies_hz=[312], psd_profile=FLAT, q=10,
                             modal_stress_mpa_per_g=8.0, allowable_stress_mpa=174)
    assert abs(r["rms_stress_mpa"] - r["rms_g"] * 8.0) < 1e-3, r
    assert abs(r["three_sigma_stress_mpa"] - 3 * r["rms_stress_mpa"]) < 1e-3, r
    assert r["pass"] is True, r                              # 3σ = 168 < 174
    tight = vib.random_vibration(frequencies_hz=[312], psd_profile=FLAT, q=10,
                                 modal_stress_mpa_per_g=8.0, allowable_stress_mpa=150)
    assert tight["pass"] is False, tight                     # 3σ = 168 > 150
    # without a stress map, the stress/pass fields stay None (no fabricated numbers)
    bare = vib.random_vibration(frequencies_hz=[312], psd_profile=FLAT, q=10)
    assert bare["rms_stress_mpa"] is None and bare["pass"] is None, bare


def test_input_errors():
    for bad in (
        lambda: vib.random_vibration(frequencies_hz=[], psd_profile=FLAT),
        lambda: vib.random_vibration(frequencies_hz=[100], psd_profile=[]),
        lambda: vib.random_vibration(frequencies_hz=[100], psd_profile=FLAT, q=0),
        lambda: vib.random_vibration(frequencies_hz=[100],
                                     psd_profile=[{"hz": -5, "g2_hz": 0.01}]),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError on bad input")


# --- beam natural frequencies (P3 M6 modal oracle) ----------------------------

def test_beam_cantilever_matches_euler_bernoulli():
    import math
    # steel cantilever L=300, b=30, h=10 mm, E=210 GPa, ρ=7900: hand f1 = 92.54 Hz
    r = vib.beam_natural_frequencies(300, 30, 10, "cantilever", n_modes=3,
                                     youngs_gpa=210, density_kg_m3=7900)
    L, b, h, E, rho = 0.3, 0.03, 0.01, 210e9, 7900.0
    I, A = b * h ** 3 / 12.0, b * h
    f1 = (1.8751041 ** 2) / (2 * math.pi) * math.sqrt(E * I / (rho * A * L ** 4))
    assert abs(r["first_mode_hz"] - f1) < 1e-2, (r["first_mode_hz"], f1)
    assert abs(r["first_mode_hz"] - 92.54) < 0.1, r["first_mode_hz"]
    # higher modes follow (βL_n/βL_1)²
    assert abs(r["frequencies_hz"][1] / r["frequencies_hz"][0]
               - (4.6940911 / 1.8751041) ** 2) < 1e-3, r


def test_beam_simply_supported_is_n_squared():
    r = vib.beam_natural_frequencies(300, 30, 10, "simply_supported", n_modes=4,
                                     youngs_gpa=70, density_kg_m3=2700)
    f1 = r["frequencies_hz"][0]
    for n, f in enumerate(r["frequencies_hz"], start=1):
        assert abs(f / f1 - n * n) < 1e-4, (n, f, f1)     # pinned-pinned: f_n ∝ n²


def test_beam_thinner_is_lower_freq_and_material_lookup():
    thick = vib.beam_natural_frequencies(300, 30, 20, "cantilever", n_modes=1,
                                         youngs_gpa=210, density_kg_m3=7900)
    thin = vib.beam_natural_frequencies(300, 30, 10, "cantilever", n_modes=1,
                                        youngs_gpa=210, density_kg_m3=7900)
    # f ∝ height (I ∝ h³, A ∝ h → f ∝ h): halving height halves the frequency
    assert abs(thick["first_mode_hz"] / thin["first_mode_hz"] - 2.0) < 1e-3, (thick, thin)
    # material name resolves E and ρ from the DB
    m = vib.beam_natural_frequencies(300, 30, 10, "cantilever", n_modes=1,
                                     material="AL6061-T6")
    assert m["first_mode_hz"] > 0 and m["youngs_gpa"] > 0 and m["density_kg_m3"] > 0, m


def test_beam_input_errors():
    for bad in (
        lambda: vib.beam_natural_frequencies(0, 30, 10, youngs_gpa=210, density_kg_m3=7900),
        lambda: vib.beam_natural_frequencies(300, 30, 10, "noplace", youngs_gpa=210,
                                             density_kg_m3=7900),
        lambda: vib.beam_natural_frequencies(300, 30, 10, n_modes=9, youngs_gpa=210,
                                             density_kg_m3=7900),       # >5 modes
        lambda: vib.beam_natural_frequencies(300, 30, 10),             # no material
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError on bad beam input")



# --- harmonic forced response (SIMULATION_NEXT B2 oracle) ----------------------

def test_harmonic_amplification_identities():
    # exact identities of the SDOF FRF: |H|(r=1) = 1/(2*zeta) exactly, and the
    # peak value (at f_peak = fn*sqrt(1-2*zeta^2)) is Q = 1/(2*zeta*sqrt(1-zeta^2))
    z = 0.05
    at_fn = vib.harmonic_response(100.0, z, frequency_hz=100.0)
    assert abs(at_fn["amplification"] - 1.0 / (2 * z)) < 1e-4, at_fn
    assert abs(at_fn["phase_deg"] - 90.0) < 1e-6, at_fn["phase_deg"]
    r = vib.harmonic_response(100.0, z)
    at_peak = vib.harmonic_response(100.0, z, frequency_hz=r["f_peak_hz"])
    assert abs(at_peak["amplification"] - r["q_factor"]) < 1e-3, (at_peak, r)


def test_harmonic_static_and_high_frequency_limits():
    # r -> 0 gives |H| -> 1 (static); r >> 1 rolls off below 1; phase 0 -> 180
    lo = vib.harmonic_response(100.0, 0.02, frequency_hz=1.0)
    hi = vib.harmonic_response(100.0, 0.02, frequency_hz=1000.0)
    assert abs(lo["amplification"] - 1.0) < 1e-3, lo
    assert hi["amplification"] < 0.02 and hi["phase_deg"] > 175.0, hi
    # static_deflection scales the answer linearly (1 mm static, Q at resonance)
    amp = vib.harmonic_response(100.0, 0.02, frequency_hz=100.0,
                                static_deflection_mm=1.0)
    assert abs(amp["amplitude_mm"] - 25.0) < 0.01, amp["amplitude_mm"]


def test_harmonic_half_power_bandwidth():
    # at fn*(1 +/- zeta) the response is Q/sqrt(2) (light damping) — the
    # bandwidth identity Delta_f = fn/Q the FRF gate leans on
    fn, z = 200.0, 0.02
    r = vib.harmonic_response(fn, z)
    assert abs(r["half_power_bandwidth_hz"] - 2 * z * fn) < 1e-6
    edge = vib.harmonic_response(fn, z, frequency_hz=fn * (1 + z))
    assert abs(edge["amplification"] / (r["q_factor"] / math.sqrt(2)) - 1) < 0.02


def test_harmonic_overdamped_has_no_peak():
    r = vib.harmonic_response(100.0, 0.8)
    assert r["q_factor"] is None and r["f_peak_hz"] is None, r
    assert r["valid_range_ok"] is False and r["warnings"], r
    # contract fields
    ok = vib.harmonic_response(100.0, 0.02)
    assert ok["fidelity"] == "exact" and ok["escalate_to"] == "harmonic_response_submit"


def test_harmonic_input_errors():
    for bad in (
        lambda: vib.harmonic_response(0, 0.02),
        lambda: vib.harmonic_response(100, 0.0),
        lambda: vib.harmonic_response(100, 1.0),
        lambda: vib.harmonic_response(100, 0.02, frequency_hz=0),
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

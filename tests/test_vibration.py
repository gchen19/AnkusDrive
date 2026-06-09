"""Random-vibration toys — two-sided oracles for driftpin.analysis.vibration.

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

from driftpin.analysis import vibration as vib  # noqa: E402

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

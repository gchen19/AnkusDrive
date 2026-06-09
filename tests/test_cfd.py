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

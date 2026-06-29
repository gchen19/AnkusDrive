"""
Full-wave EM via openEMS FDTD — oracle-gated (issue #93 part b).

Two tiers, mirroring every other family in the suite:

  Pure-oracle toys (ALWAYS run, no solver) — the closed-form anchors:
    - test_waveguide_cutoff_oracle : TE10 f_c = c/(2a) EXACT, the mode ordering
        (TE10 < TE20 = 2·f_c), and the propagating↔evanescent regime split with β
    - test_dipole_resonance_oracle : L ≈ 0.48·λ banded estimate, both directions
        (length→f, f→length round-trip) with the ±band

  Live openEMS FDTD solves (SKIP when no openEMS venv resolves) — gated on the
  EXACT waveguide cutoff:
    - test_em_runner_subprocess_clean : the GPL engine answers a ping over the
        subprocess + sentinel-JSON contract; this process never imports openEMS
    - test_em_waveguide_cutoff_gate   : drive a WR-90-like guide over a band that
        STRADDLES the cutoff; the FDTD transmission collapses below f_c (evanescent
        ≈0), rises to a plateau above (≈1), and its half-power crossing lands on
        the analytic c/(2a) within ~1% — openEMS reproduces the exact cutoff

openEMS is GPL-3.0, so the live legs run it ONLY out-of-process via
driftpin/em_fullwave_gpl_runner.py (the same arm's-length boundary as KrakenOS),
under a DEDICATED openEMS venv resolved exactly like the worker does.

Run:  python3 tests/test_em_fullwave.py
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin.analysis import em_fullwave as ew  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402

C0 = 299_792_458.0
RUNNER = str(REPO / "driftpin" / "em_fullwave_gpl_runner.py")


def _openems_python():
    """The interpreter that can import openEMS/CSXCAD (a dedicated .venv-openems),
    NOT this test process. Mirrors worker._em_fullwave_gpl_python: env override →
    .venv-openems beside the repo or one level up → PATH. Each candidate is probed
    with find_spec (no import here — the copyleft boundary holds). None if absent."""
    cands = []
    if env := os.environ.get("DRIFTPIN_OPENEMS_PYTHON"):
        cands.append(env)
    for base in (REPO, REPO.parent):
        cands += [str(base / ".venv-openems" / "bin" / "python3"),
                  str(base / ".venv-openems" / "bin" / "python")]
    if w := shutil.which("python3"):
        cands.append(w)
    probe = ("import importlib.util,sys;"
             "sys.exit(0 if importlib.util.find_spec('openEMS') and "
             "importlib.util.find_spec('CSXCAD') else 1)")
    seen = set()
    for c in cands:
        if not c or c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        try:
            r = subprocess.run([c, "-c", probe], capture_output=True, timeout=30)
        except Exception:
            continue
        if r.returncode == 0:
            return c
    return None


def _run(problem, python_exe, timeout=600):
    proc = subprocess.run([python_exe, RUNNER], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=timeout)
    body = proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0]
    assert body, f"runner produced no JSON (rc={proc.returncode}); stderr: {proc.stderr[-400:]}"
    return json.loads(body)


# --- pure-oracle toys (no solver) ---------------------------------------------

def test_waveguide_cutoff_oracle():
    """TE10 cutoff is EXACT: f_c = c/(2a). WR-90 (a=22.86 mm) → 6.557 GHz. The mode
    ordering TE10 < TE20 holds (TE20 = 2·f_c for a 2:1 aspect), and a probe
    frequency splits propagating (β real, λ_g finite) from evanescent (β=0)."""
    a = 22.86
    r = ew.waveguide_cutoff(a_mm=a)                    # default b=a/2, TE10
    fc = C0 / (2.0 * a * 1e-3)
    assert r["mode"] == "TE10"
    assert abs(r["cutoff_hz"] - fc) / fc < 1e-9, (r["cutoff_hz"], fc)
    assert abs(r["cutoff_ghz"] - 6.55714) < 1e-3, r["cutoff_ghz"]
    assert r["fidelity"] == "exact" and r["valid_range_ok"]

    # next mode for a 2:1 guide is TE20 at exactly 2·f_c; single-mode band is [fc, 2fc]
    assert abs(r["next_mode_cutoff_ghz"] - 2 * r["cutoff_ghz"]) < 1e-6, r
    lo, hi = r["single_mode_band_ghz"]
    assert abs(lo - r["cutoff_ghz"]) < 1e-9 and abs(hi - 2 * r["cutoff_ghz"]) < 1e-6

    # dielectric fill lowers the cutoff by √εᵣ (exact)
    filled = ew.waveguide_cutoff(a_mm=a, eps_r=4.0)
    assert abs(filled["cutoff_ghz"] - r["cutoff_ghz"] / 2.0) < 1e-6, filled["cutoff_ghz"]

    # two-sided regime split: above f_c propagates (β>0, λ_g finite), below evanesces
    above = ew.waveguide_cutoff(a_mm=a, freq_ghz=9.0)
    assert above["regime"] == "propagating" and above["beta_per_m"] > 0
    assert above["guided_wavelength_mm"] > C0 / 9e9 * 1e3   # λ_g > free-space λ
    below = ew.waveguide_cutoff(a_mm=a, freq_ghz=4.0)
    assert below["regime"] == "evanescent" and below["beta_per_m"] == 0.0
    assert below["guided_wavelength_mm"] is None

    # TE11 cutoff matches the closed form for the mixed mode
    te11 = ew.waveguide_cutoff(a_mm=a, b_mm=a / 2.0, mode="TE11")
    expect = C0 / 2.0 * math.sqrt((1 / (a * 1e-3)) ** 2 + (1 / (a / 2 * 1e-3)) ** 2)
    assert abs(te11["cutoff_hz"] - expect) / expect < 1e-9

    for bad in (lambda: ew.waveguide_cutoff(a_mm=-1),
                lambda: ew.waveguide_cutoff(a_mm=a, mode="TM00"),
                lambda: ew.waveguide_cutoff(a_mm=a, mode="XY10"),
                lambda: ew.waveguide_cutoff(a_mm=a, freq_ghz=-2)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    print(f"    waveguide: TE10 f_c {r['cutoff_ghz']:.4f} GHz (exact c/2a); "
          f"single-mode band {lo:.2f}–{hi:.2f} GHz; "
          f"@9 GHz λ_g={above['guided_wavelength_mm']:.2f} mm")


def test_dipole_resonance_oracle():
    """Half-wave dipole banded estimate L ≈ 0.48·λ. A 150 mm dipole resonates near
    0.96 GHz; the length↔frequency directions round-trip; the ±band brackets the
    centre, and a shorter dipole resonates higher (f_r ∝ 1/L)."""
    d = ew.dipole_resonance(length_mm=150.0)
    fr = 0.48 * C0 / (150.0e-3)
    assert abs(d["resonant_freq_ghz"] - fr / 1e9) < 1e-6, d["resonant_freq_ghz"]
    assert d["fidelity"] == "banded" and d["band_pct"] > 0
    # band brackets the centre, lo<centre<hi (shorter k → lower f)
    assert d["freq_lo_ghz"] < d["resonant_freq_ghz"] < d["freq_hi_ghz"], d

    # inverse direction: feed the centre frequency, recover ~the same length
    back = ew.dipole_resonance(freq_ghz=d["resonant_freq_ghz"])
    assert abs(back["resonant_length_mm"] - 150.0) < 1e-3, back["resonant_length_mm"]
    assert back["length_lo_mm"] < 150.0 < back["length_hi_mm"], back

    # monotone: a shorter dipole resonates higher
    short = ew.dipole_resonance(length_mm=100.0)
    assert short["resonant_freq_ghz"] > d["resonant_freq_ghz"]

    for bad in (lambda: ew.dipole_resonance(),                         # neither
                lambda: ew.dipole_resonance(length_mm=150, freq_ghz=1),  # both
                lambda: ew.dipole_resonance(length_mm=-5),
                lambda: ew.dipole_resonance(length_mm=150, shortening=0.7)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    print(f"    dipole: 150 mm → f_r {d['resonant_freq_ghz']:.4f} GHz "
          f"(band {d['freq_lo_ghz']:.3f}–{d['freq_hi_ghz']:.3f}, ±{d['band_pct']:.1f}%)")


# --- live openEMS FDTD solves -------------------------------------------------

def test_em_runner_subprocess_clean():
    """The GPL openEMS engine answers a ping over the subprocess + sentinel-JSON
    contract, and THIS process never imports openEMS (the copyleft boundary)."""
    py = _openems_python()
    if skip_heavy("openEMS FDTD"):
        return
    if py is None:
        print("    SKIP — no openEMS venv resolves (scripts/install-solvers.sh em_gpl)")
        return
    res = _run({"problem": "ping"}, py, timeout=60)
    assert res.get("ok") and res.get("engine") == "openEMS", res
    assert "openEMS" not in sys.modules and "CSXCAD" not in sys.modules  # parent clean
    print(f"    runner: openEMS {res.get('version')} answered ping out-of-process")


def test_em_waveguide_cutoff_gate():
    """Live FDTD vs the EXACT TE10 cutoff. A WR-90-like guide (a=22.86, b=10.16 mm)
    is driven over 4–10 GHz — straddling f_c = c/2a = 6.557 GHz. With geometric
    truth in the mesh, the FDTD transmission collapses to ~0 in the evanescent band
    below f_c, rises to a flat plateau (~1) above it, and its half-power crossing
    lands on the analytic cutoff within ~1.5%. That clean propagating↔evanescent
    transition at the analytic frequency is the gate the openEMS solve must pass."""
    py = _openems_python()
    if skip_heavy("openEMS FDTD"):
        return
    if py is None:
        print("    SKIP — no openEMS venv resolves (scripts/install-solvers.sh em_gpl)")
        return
    a, b = 22.86, 10.16
    problem = {"problem": "waveguide_sweep", "a_mm": a, "b_mm": b, "length_mm": 60.0,
               "f_start_ghz": 4.0, "f_stop_ghz": 10.0, "n_freq": 121, "nrts": 30000}
    res = _run(problem, py, timeout=600)
    assert res.get("ok"), res

    # the analytic anchor this is gated against
    orc = ew.waveguide_cutoff(a_mm=a, b_mm=b)
    fc_oracle = orc["cutoff_ghz"]
    assert abs(res["fc_analytic_ghz"] - fc_oracle) < 1e-3, (res["fc_analytic_ghz"], fc_oracle)

    # below cutoff: evanescent, no transmission; above: full plateau
    assert res["evanescent_mean"] < 0.05, (
        f"evanescent-band transmission {res['evanescent_mean']:.3f} not ≈0 — "
        "the guide did not cut off below f_c")
    assert res["propagating_mean"] > 0.9, (
        f"propagating-band transmission {res['propagating_mean']:.3f} not ≈1 — "
        "the guide did not pass above f_c")

    # the half-power crossing reproduces the EXACT cutoff to within ~1.5%
    ratio = res["fc_ratio"]
    assert ratio is not None, "FDTD transmission never crossed half-power — no transition"
    assert 0.985 <= ratio <= 1.015, (
        f"FDTD cutoff crossing {res['fc_crossing_ghz']:.4f} GHz vs analytic "
        f"{fc_oracle:.4f} GHz (ratio {ratio:.4f}) — outside the 1.5% gate")
    assert "openEMS" not in sys.modules                # parent stayed clean
    print(f"    waveguide FDTD: crossing {res['fc_crossing_ghz']:.4f} GHz vs oracle "
          f"f_c {fc_oracle:.4f} GHz (ratio {ratio:.4f}); evanescent "
          f"{res['evanescent_mean']:.3f} → propagating {res['propagating_mean']:.3f}; "
          f"{res['n_cells']} cells, {res['wall_s']:.1f}s")


# --- runner -------------------------------------------------------------------

def _discover():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    failures = []
    t0 = time.time()
    for name, fn in _discover():
        t = time.time()
        try:
            fn()
        except Exception as e:
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL {name:44s} ({time.time() - t:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:44s} ({time.time() - t:.2f}s)")
    print()
    if failures:
        print(f"== {len(failures)}/{len(_discover())} failed  ({time.time() - t0:.1f}s) ==")
        for name, tb in failures:
            print(f"\n--- {name} ---\n{tb}")
        sys.exit(1)
    print(f"== {len(_discover())}/{len(_discover())} passed  ({time.time() - t0:.1f}s) ==")


if __name__ == "__main__":
    main()

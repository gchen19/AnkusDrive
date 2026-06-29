"""
Exterior acoustics via Bempp BEM — oracle-gated (issue #93 part a).

Two tiers, mirroring every other family in the suite:

  Pure-oracle toys (ALWAYS run, no solver) — the closed-form anchors:
    - test_monopole_sphere_oracle : the pulsating-sphere radiated power
        W=(ρc/2)|U|²(4πa²)(ka)²/(1+(ka)²) and far-field |p(r)| EXACT, the radiation
        efficiency limits (ka→0 ⇒ σ→0, ka→∞ ⇒ σ→1), and the 1/r far-field decay
    - test_rigid_sphere_scattering_oracle : the Mie far-field form function — the
        backscatter |f∞(π)| settling toward 1 in the geometric limit, the spherical
        Bessel/Hankel recurrences (cross-checked against the radiation oracle's
        surface pressure), and two-sided error paths

  Live Bempp BEM solves (SKIP when no bempp venv resolves) — gated on the EXACT
  monopole power / far-field and the Mie form function:
    - test_bempp_runner_subprocess_clean : the engine answers a ping over the
        subprocess + sentinel-JSON contract; this process never imports bempp_cl
    - test_bempp_radiation_gate   : a pulsating sphere solved by the exterior
        Neumann BEM reproduces the analytic radiated power AND far-field pressure
        within ~2%
    - test_bempp_scattering_gate  : a rigid sphere's BEM scattered far field lands
        on the Mie form function (backscatter) within ~3% across two ka

Bempp is MIT, so the subprocess here is NOT a license boundary — it is a meshio
dependency clash (bempp needs meshio>=4, the shared venv pins meshio==3 for
solidspy). The live legs run bempp ONLY out-of-process via
driftpin/bempp_runner.py under a DEDICATED .venv-bempp resolved exactly like the
worker does.

Run:  python3 tests/test_acoustics_bem.py
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

from driftpin.analysis import acoustics_bem as ab  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402

RUNNER = str(REPO / "driftpin" / "bempp_runner.py")


def _bempp_python():
    """The interpreter that can import bempp_cl (a dedicated .venv-bempp with
    meshio>=5), NOT this test process. Mirrors worker._bempp_python: env override →
    .venv-bempp beside the repo or one level up → PATH. Each candidate is probed
    with find_spec (no import here). None if absent."""
    cands = []
    if env := os.environ.get("DRIFTPIN_BEMPP_PYTHON"):
        cands.append(env)
    for base in (REPO, REPO.parent):
        cands += [str(base / ".venv-bempp" / "bin" / "python3"),
                  str(base / ".venv-bempp" / "bin" / "python")]
    if w := shutil.which("python3"):
        cands.append(w)
    probe = ("import importlib.util,sys;"
             "sys.exit(0 if importlib.util.find_spec('bempp_cl') else 1)")
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


def _run(problem, python_exe, timeout=900):
    proc = subprocess.run([python_exe, RUNNER], input=json.dumps(problem),
                          capture_output=True, text=True, timeout=timeout)
    body = proc.stdout.partition("@@JSON@@")[2].partition("@@END@@")[0]
    assert body, f"runner produced no JSON (rc={proc.returncode}); stderr: {proc.stderr[-400:]}"
    return json.loads(body)


# --- pure-oracle toys (no solver) ---------------------------------------------

def test_monopole_sphere_oracle():
    """Pulsating sphere: radiated power and far-field pressure are EXACT closed
    forms. Efficiency σ=(ka)²/(1+(ka)²) → 0 as ka→0 and → 1 as ka→∞; the far field
    falls off as 1/r; halving the radius at fixed frequency lowers ka and the
    radiated power."""
    a, f = 0.1, 2000.0
    r = ab.monopole_sphere(a_m=a, freq_hz=f, u_amp=1.0, r_m=1.0)
    rho, c = 1.204, 343.0
    k = 2 * math.pi * f / c
    ka = k * a
    sigma = ka * ka / (1 + ka * ka)
    W = 0.5 * rho * c * (4 * math.pi * a * a) * sigma
    assert abs(r["radiated_power_w"] - W) / W < 1e-9, (r["radiated_power_w"], W)
    assert abs(r["radiation_efficiency"] - sigma) < 1e-9
    assert r["fidelity"] == "exact" and r["valid_range_ok"]

    # far field: |p|*r is range-independent; check 1/r decay between two ranges
    r2 = ab.monopole_sphere(a_m=a, freq_hz=f, u_amp=1.0, r_m=2.0)
    assert abs(r["farfield_pressure_x_r"] - r2["farfield_pressure_x_r"]) < 1e-6
    assert abs(r2["farfield_pressure_abs"] - r["farfield_pressure_abs"] / 2.0) < 1e-6

    # efficiency limits (two-sided): tiny ka radiates poorly, huge ka ≈ piston
    lo = ab.monopole_sphere(a_m=1e-3, freq_hz=20.0)
    hi = ab.monopole_sphere(a_m=2.0, freq_hz=20000.0)
    assert lo["radiation_efficiency"] < 1e-3
    assert hi["radiation_efficiency"] > 0.99

    # monotone in power with size at fixed frequency (smaller → lower ka → less power)
    small = ab.monopole_sphere(a_m=a / 2.0, freq_hz=f, u_amp=1.0)
    assert small["radiated_power_w"] < r["radiated_power_w"]

    for bad in (lambda: ab.monopole_sphere(a_m=-1, freq_hz=f),
                lambda: ab.monopole_sphere(a_m=a, freq_hz=-1),
                lambda: ab.monopole_sphere(a_m=a, freq_hz=f, r_m=a / 2.0)):  # r<a
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    print(f"    monopole: a={a} m @ {f} Hz  ka={r['ka']:.3f}  σ={r['radiation_efficiency']:.3f}  "
          f"W={r['radiated_power_w']:.3f} W  |p(1 m)|={r['farfield_pressure_abs']:.2f} Pa")


def test_rigid_sphere_scattering_oracle():
    """Rigid-sphere Mie form function: the backscatter magnitude tracks the textbook
    rigid-sphere curve — Rayleigh ka⁴ growth at small ka, a resonance hump near
    ka~1.2 (≈0.99), settling toward O(1) in the geometric limit — the spherical
    Bessel recurrence matches the closed forms, and two-sided error paths raise."""
    # backscatter |f∞(π)| sampled across the curve. Small ka is deep in the Rayleigh
    # region (≪1); the hump peaks near ka~1.2; ka≫1 oscillates around O(1).
    vals = {ka: ab.rigid_sphere_scattering(ka=ka, theta_deg=180.0)["backscatter_abs"]
            for ka in (0.1, 0.5, 1.2, 5.0, 8.0)}
    assert vals[0.1] < 0.05, vals[0.1]                # Rayleigh: tiny scatterer
    assert vals[0.1] < vals[0.5] < vals[1.2]          # monotone rise into the hump
    assert 0.9 < vals[1.2] < 1.1, vals[1.2]           # resonance hump ≈ 1
    assert 0.7 < vals[8.0] < 1.3, vals[8.0]           # geometric limit, O(1)
    for v in vals.values():
        assert v >= 0

    # the form_function_abs at θ=180 equals the dedicated backscatter_abs
    s = ab.rigid_sphere_scattering(ka=2.0, theta_deg=180.0)
    assert abs(s["form_function_abs"] - s["backscatter_abs"]) < 1e-9
    assert s["fidelity"] == "exact" and s["valid_range_ok"]
    assert s["n_terms"] >= 8

    # spherical-Bessel recurrence sanity: j0=sin x/x, y0=-cos x/x (exact closed forms)
    for x in (0.5, 2.0, 5.0):
        jn = ab._sph_jn(4, x)
        yn = ab._sph_yn(4, x)
        assert abs(jn[0] - math.sin(x) / x) < 1e-12
        assert abs(yn[0] + math.cos(x) / x) < 1e-12
        assert abs(yn[1] - (-math.cos(x) / x**2 - math.sin(x) / x)) < 1e-12

    # forward/back are generally different (a sphere is not isotropic)
    fwd = ab.rigid_sphere_scattering(ka=3.0, theta_deg=0.0)["form_function_abs"]
    bk = ab.rigid_sphere_scattering(ka=3.0, theta_deg=180.0)["form_function_abs"]
    assert abs(fwd - bk) > 1e-3

    for bad in (lambda: ab.rigid_sphere_scattering(ka=-1),
                lambda: ab.rigid_sphere_scattering(ka=1.0, theta_deg=400.0)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    print(f"    scattering: |f∞(π)| ka=0.1→{vals[0.1]:.3f} 0.5→{vals[0.5]:.3f} "
          f"1.2→{vals[1.2]:.3f} 8→{vals[8.0]:.3f}  (Rayleigh → hump → O(1))")


# --- live Bempp BEM solves ----------------------------------------------------

def test_bempp_runner_subprocess_clean():
    """The Bempp engine answers a ping over the subprocess + sentinel-JSON contract,
    and THIS process never imports bempp_cl (keeping meshio>=4 off the shared venv)."""
    py = _bempp_python()
    if skip_heavy("Bempp BEM"):
        return
    if py is None:
        print("    SKIP — no bempp venv resolves (scripts/install-solvers.sh acoustics_bem)")
        return
    res = _run({"problem": "ping"}, py, timeout=120)
    assert res.get("ok") and res.get("engine") == "bempp-cl", res
    assert "bempp_cl" not in sys.modules                # parent stayed clean
    print(f"    runner: bempp-cl {res.get('version')} answered ping out-of-process")


def test_bempp_radiation_gate():
    """Live exterior-Neumann BEM vs the EXACT monopole sphere. A sphere of radius
    0.1 m pulsing at 2 kHz (ka≈3.66) is solved for its surface pressure; the
    recovered radiated power AND the far-field pressure at 1 m must land on the
    closed-form monopole_sphere oracle within ~2%."""
    py = _bempp_python()
    if skip_heavy("Bempp BEM"):
        return
    if py is None:
        print("    SKIP — no bempp venv resolves (scripts/install-solvers.sh acoustics_bem)")
        return
    a, f, r_m = 0.1, 2000.0, 1.0
    res = _run({"problem": "radiation", "a_m": a, "freq_hz": f, "u_amp": 1.0,
                "r_m": r_m, "h": 0.22}, py, timeout=600)
    assert res.get("ok"), res

    orc = ab.monopole_sphere(a_m=a, freq_hz=f, u_amp=1.0, r_m=r_m)
    pw_ratio = res["radiated_power_w"] / orc["radiated_power_w"]
    pf_ratio = res["farfield_pressure_x_r"] / orc["farfield_pressure_x_r"]
    assert abs(res["ka"] - orc["ka"]) < 1e-3
    assert 0.98 <= pw_ratio <= 1.02, (
        f"BEM radiated power {res['radiated_power_w']:.3f} W vs oracle "
        f"{orc['radiated_power_w']:.3f} W (ratio {pw_ratio:.4f}) — outside the 2% gate")
    assert 0.98 <= pf_ratio <= 1.02, (
        f"BEM far-field |p|·r {res['farfield_pressure_x_r']:.3f} vs oracle "
        f"{orc['farfield_pressure_x_r']:.3f} (ratio {pf_ratio:.4f}) — outside the 2% gate")
    assert "bempp_cl" not in sys.modules
    print(f"    radiation BEM: W {res['radiated_power_w']:.3f} vs {orc['radiated_power_w']:.3f} "
          f"(ratio {pw_ratio:.4f}); |p|·r {res['farfield_pressure_x_r']:.2f} vs "
          f"{orc['farfield_pressure_x_r']:.2f} (ratio {pf_ratio:.4f}); "
          f"{res['n_elements']} el, {res['wall_s']:.1f}s")


def test_bempp_scattering_gate():
    """Live rigid-sphere scattering vs the EXACT Mie form function. A sound-hard
    sphere insonified by a plane wave is solved at ka=2 and 4; the BEM scattered
    far field's backscatter form function |f∞(π)| must land on the Mie oracle within
    ~3%."""
    py = _bempp_python()
    if skip_heavy("Bempp BEM"):
        return
    if py is None:
        print("    SKIP — no bempp venv resolves (scripts/install-solvers.sh acoustics_bem)")
        return
    a = 1.0
    res = _run({"problem": "scattering", "a_m": a, "ka_list": [2.0, 4.0],
                "theta_deg": [180.0], "h_per_wl": 12.0}, py, timeout=600)
    assert res.get("ok"), res
    for entry in res["results"]:
        ka = entry["ka"]
        bem_f = entry["backscatter_abs"]
        mie = ab.rigid_sphere_scattering(ka=ka, theta_deg=180.0)["form_function_abs"]
        ratio = bem_f / mie
        assert 0.97 <= ratio <= 1.03, (
            f"ka={ka}: BEM backscatter |f∞|={bem_f:.4f} vs Mie {mie:.4f} "
            f"(ratio {ratio:.4f}) — outside the 3% gate")
        print(f"    scattering BEM ka={ka}: |f∞(π)| {bem_f:.4f} vs Mie {mie:.4f} "
              f"(ratio {ratio:.4f}, {entry['n_elements']} el)")
    assert "bempp_cl" not in sys.modules


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

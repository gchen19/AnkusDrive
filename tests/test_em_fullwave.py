"""
Full-wave EM via openEMS FDTD — oracle-gated (issue #93 part b).

Two tiers, mirroring every other family in the suite:

  Pure-oracle toys (ALWAYS run, no solver) — the closed-form anchors:
    - test_waveguide_cutoff_oracle : TE10 f_c = c/(2a) EXACT, the mode ordering
        (TE10 < TE20 = 2·f_c), and the propagating↔evanescent regime split with β
    - test_dipole_resonance_oracle : L ≈ 0.48·λ banded estimate, both directions
        (length→f, f→length round-trip) with the ±band

  Live openEMS FDTD solves (SKIP when no openEMS venv resolves):
    - test_em_runner_subprocess_clean : the GPL engine answers a ping over the
        subprocess + sentinel-JSON contract; this process never imports openEMS
    - test_em_waveguide_port_setup_check : fc_ratio / evanescent / propagating
        means — a PORT-SETUP check only; openEMS's analytic port beta makes the
        transmission a step at c/2a whatever the field does (#398)
    - test_em_waveguide_evanescent_decay_gate : THE FIELD GATE — probes fit the
        solved field's decay rate below cutoff against the exact alpha; a coarser
        mesh lands farther from 1, a truncated run fails it (#401, #402)
    - test_em_waveguide_degenerate_ports_rejected : overlapping port blocks
        return ok:false, not nulls (#403)
    - test_em_dipole_scale_invariance : k and S11 depth identical at L=60/100/160
    - test_em_dipole_radius_resolved  : thicker wire resonates lower (#399)
    - test_em_dipole_mesh_decoupled_and_golden : pinned mesh, moved sweep window,
        same resonance; golden value; approach to the oracle band (#400, #402)

openEMS is GPL-3.0, so the live legs run it ONLY out-of-process via
ankusdrive/em_fullwave_gpl_runner.py (the same arm's-length boundary as KrakenOS),
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

from ankusdrive.analysis import em_fullwave as ew  # noqa: E402
from tests.heavy_solve import skip_heavy  # noqa: E402

C0 = 299_792_458.0
RUNNER = str(REPO / "ankusdrive" / "em_fullwave_gpl_runner.py")


def _openems_python():
    """The interpreter that can import openEMS/CSXCAD (a dedicated .venv-openems),
    NOT this test process — resolved by the same solvers.solver_python the worker and
    solve_capabilities use (issue #351). None if absent."""
    from ankusdrive import solvers
    return solvers.solver_python("openems")


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
    # below cutoff the exact attenuation α = √(kc² − k²) is what the FDTD decay gate
    # targets; above cutoff there is none
    k4 = 2.0 * math.pi * 4e9 / C0
    assert abs(below["alpha_per_m"] - math.sqrt((math.pi / (a * 1e-3)) ** 2 - k4 ** 2)) < 1e-5
    assert above["alpha_per_m"] == 0.0 and r["alpha_per_m"] is None

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


def _run_many(problems, python_exe, timeout=900):
    """Run several independent runner problems concurrently (each is its own
    openEMS subprocess) and return their results in order."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(problems)) as pool:
        return list(pool.map(lambda pr: _run(pr, python_exe, timeout=timeout), problems))


def _live_python(label="openEMS FDTD"):
    """The openEMS interpreter, or None after printing why the live leg skips."""
    if skip_heavy(label):
        return None
    py = _openems_python()
    if py is None:
        print("    SKIP — no openEMS venv resolves (scripts/install-solvers.sh em_gpl)")
    return py


# Pinned waveguide for the field gate: WR-90 driven 4–10 GHz, long enough (120 mm)
# that the decay probes sit >= a broad-wall clear of the source and 2.5 nepers
# clear of the far MUR. Every knob the result depends on is spelled out here.
WR90 = {"problem": "waveguide_sweep", "a_mm": 22.86, "b_mm": 10.16, "length_mm": 120.0,
        "f_start_ghz": 4.0, "f_stop_ghz": 10.0, "n_freq": 121, "nrts": 60000,
        "cells_per_wl": 20, "end_criteria": 1e-6, "decay_f_ratio": 0.7}


def test_em_waveguide_port_setup_check():
    """Live FDTD PORT-SETUP CHECK — explicitly NOT the physics gate (#398). The
    half-power crossing of the port-computed S21 lands on c/(2a), the evanescent
    band reads 0 and the propagating band ≈1. openEMS's port applies the ANALYTIC
    β = √(k² − kc²), which is NaN (→ 0) below cutoff, so this step sits at c/2a
    whatever the field does; it only proves the port and units are wired. The
    field is gated in test_em_waveguide_evanescent_decay_gate."""
    py = _live_python()
    if py is None:
        return
    res = _run(WR90, py, timeout=600)
    assert res.get("ok"), res
    fc_oracle = ew.waveguide_cutoff(a_mm=WR90["a_mm"], b_mm=WR90["b_mm"])["cutoff_ghz"]
    assert abs(res["fc_analytic_ghz"] - fc_oracle) < 1e-3, (res["fc_analytic_ghz"], fc_oracle)
    assert res["evanescent_mean"] < 0.05, res["evanescent_mean"]
    assert res["propagating_mean"] > 0.9, res["propagating_mean"]
    ratio = res["fc_ratio"]
    assert ratio is not None and 0.985 <= ratio <= 1.015, (
        f"port-setup check: crossing {res['fc_crossing_ghz']} GHz vs c/2a "
        f"{fc_oracle:.4f} GHz (ratio {ratio}) — the port or units are mis-wired")
    assert "openEMS" not in sys.modules                # parent stayed clean
    print(f"    port setup: crossing {res['fc_crossing_ghz']:.4f} GHz vs c/2a "
          f"{fc_oracle:.4f} GHz (ratio {ratio:.4f}; grid-limited, not a field read)")


def test_em_waveguide_evanescent_decay_gate():
    """THE FIELD GATE (#401, #402 layer 2). Probes along the guide sample the SOLVED
    field at 0.7·f_c, and the fitted decay rate must match the exact
    α = √((π/a)² − k²) from the waveguide_cutoff oracle: |alpha_ratio − 1| < 0.05 at
    the pinned mesh (measured 0.9965). Two more legs make it a gate that READS the
    solution, which fc_ratio never did (#398):
      - a coarser mesh (10 cells/λ, measured 0.985) must land FARTHER from 1 —
        fails if convergence behaviour inverts;
      - a truncated run (nrts 3000) must FAIL the gate while its fc_ratio still
        passes the port-setup band — the blindness this gate exists to fix."""
    py = _live_python()
    if py is None:
        return
    fine, coarse, trunc = _run_many(
        [WR90, dict(WR90, cells_per_wl=10), dict(WR90, nrts=3000)], py)
    for res in (fine, coarse, trunc):
        assert res.get("ok"), res

    orc = ew.waveguide_cutoff(a_mm=WR90["a_mm"], b_mm=WR90["b_mm"],
                              freq_ghz=fine["decay_freq_ghz"])
    assert orc["regime"] == "evanescent"
    assert abs(fine["alpha_exact"] - orc["alpha_per_m"]) / orc["alpha_per_m"] < 1e-6, (
        fine["alpha_exact"], orc["alpha_per_m"])

    r_fine, r_coarse = fine["alpha_ratio"], coarse["alpha_ratio"]
    assert r_fine is not None and r_coarse is not None, (fine.get("decay_note"),
                                                         coarse.get("decay_note"))
    assert abs(r_fine - 1.0) < 0.05, (
        f"FDTD evanescent decay {fine['alpha_fdtd']:.3f}/m vs exact "
        f"{fine['alpha_exact']:.3f}/m (ratio {r_fine:.4f}) — outside the 5% gate")
    assert abs(r_fine - 1.0) < abs(r_coarse - 1.0), (
        f"refining 10→20 cells/λ moved alpha_ratio {r_coarse:.4f} → {r_fine:.4f}, "
        "not toward 1 — convergence inverted")

    # truncated run: the field gate catches it, the port-setup check does not
    assert trunc["alpha_ratio"] is None or abs(trunc["alpha_ratio"] - 1.0) >= 0.05, trunc
    assert 0.985 <= trunc["fc_ratio"] <= 1.015, trunc["fc_ratio"]
    print(f"    decay gate: alpha_ratio {r_fine:.4f} @20 cells/λ, {r_coarse:.4f} "
          f"@10; truncated nrts=3000 → {trunc['alpha_ratio']} (its fc_ratio "
          f"{trunc['fc_ratio']:.4f} still passes the setup band)")


def test_em_waveguide_degenerate_ports_rejected():
    """A mesh so coarse that the 15-cell port blocks at each end meet returns
    {ok: false} naming the constraint (#403) — it used to return ok:true with null
    fc_ratio and an unphysical propagating_mean of 0. A guide just long enough for
    the same mesh still runs."""
    py = _live_python()
    if py is None:
        return
    # 12 cells/λ → 2.5 mm cells → 37.5 mm port blocks: too deep for 60 mm, fine for 120
    bad, good = _run_many([dict(WR90, length_mm=60.0, cells_per_wl=12, nrts=3000),
                           dict(WR90, length_mm=120.0, cells_per_wl=12, nrts=3000)], py)
    assert bad.get("ok") is False, bad
    assert "port placement" in bad["error"] and "cells_per_wl" in bad["error"], bad
    assert good.get("ok") is True and good["fc_ratio"] is not None, good
    print(f"    degenerate ports: {bad['error'][:80]}…")


# Pinned dipole: every knob spelled out, mesh pinned explicitly (#400) so the
# sweep window is free to move. Grid step 0.005 GHz.
DIPOLE = {"problem": "dipole_s11", "length_mm": 100.0, "radius_mm": 0.5, "gap_mm": 2.5,
          "f_start_ghz": 0.8, "f_stop_ghz": 2.0, "n_freq": 241, "nrts": 60000,
          "mesh_res_mm": 3.331}
DIPOLE_GOLDEN_GHZ = 1.36          # measured 2026-09-14, openEMS 0.0.36.post1.dev206


def _implied_k(res):
    return res["resonance_ghz"] * 1e9 * res["length_mm"] * 1e-3 / C0


def test_em_dipole_scale_invariance():
    """Layer 1 (#402): L = 60 / 100 / 160 mm with every frequency bound, and so the
    wavelength mesh, scaled by 1/L (radius and gap default to L/200, L/40). Maxwell
    is scale-free, so the implied k = f_r·L/c and the S11 depth must agree. No
    oracle — it catches unit, port-setup and plumbing regressions."""
    py = _live_python()
    if py is None:
        return
    runs = _run_many([{"problem": "dipole_s11", "length_mm": L,
                       "f_start_ghz": 0.8 * 100.0 / L, "f_stop_ghz": 2.0 * 100.0 / L,
                       "n_freq": 241, "nrts": 60000, "cells_per_wl": 30}
                      for L in (60.0, 100.0, 160.0)], py)
    for res in runs:
        assert res.get("ok"), res
    ks = [_implied_k(r) for r in runs]
    depths = [r["resonance_s11_db"] for r in runs]
    assert max(ks) - min(ks) < 1e-4, ks
    assert max(depths) - min(depths) < 0.01, depths
    print(f"    scale invariance: k {ks[0]:.5f} at L=60/100/160 mm, S11 {depths[0]:.3f} dB")


def test_em_dipole_radius_resolved():
    """#399: the wire radius is resolved by its own mesh lines, so a thicker wire
    resonates lower (more end-effect shortening, smaller k) — thin-wire theory's
    direction. Before the fix r = 0.1 / 0.5 / 1.5 mm gave bit-identical results
    (measured now: 1.39 / 1.36 / 1.32 GHz at r = 0.1 / 0.5 / 1.5 mm)."""
    py = _live_python()
    if py is None:
        return
    radii = (0.25, 0.5, 1.5)
    runs = _run_many([dict(DIPOLE, radius_mm=r) for r in radii], py)
    for res in runs:
        assert res.get("ok"), res
    f = [r["resonance_ghz"] for r in runs]
    assert f[0] > f[1] > f[2], dict(zip(radii, f))
    print(f"    radius resolved: r {radii} mm → f_r {f} GHz")


def test_em_dipole_mesh_decoupled_and_golden():
    """#400 + #402 layer 3. With mesh_res_mm pinned, moving the sweep window (and
    with it the excitation band) leaves the resonance put to within two grid steps
    — it used to shift 3% because the mesh followed f_stop. The pinned config
    matches its recorded golden value within 2%.

    The oracle band is checked as an APPROACH, not containment: this 3.3 mm mesh
    gives k ≈ 0.454, and refining moves it monotonically up into the
    dipole_resonance band [0.46, 0.49] (0.457 @2 mm, 0.459 @1.5 mm, 0.460 @1 mm —
    19 M cells, too heavy for CI). The assertion fails if the coarse answer drifts
    away from the band edge."""
    py = _live_python()
    if py is None:
        return
    base, shifted = _run_many([DIPOLE, dict(DIPOLE, f_start_ghz=0.9, f_stop_ghz=1.9,
                                                n_freq=201)], py)
    assert base.get("ok") and shifted.get("ok"), (base, shifted)
    assert base["mesh_res_mm"] == shifted["mesh_res_mm"] == DIPOLE["mesh_res_mm"]
    assert base["n_cells"] == shifted["n_cells"], (base["n_cells"], shifted["n_cells"])
    assert abs(base["resonance_ghz"] - shifted["resonance_ghz"]) <= 0.0101, (
        base["resonance_ghz"], shifted["resonance_ghz"])

    assert abs(base["resonance_ghz"] / DIPOLE_GOLDEN_GHZ - 1.0) < 0.02, base["resonance_ghz"]
    band = ew.dipole_resonance(length_mm=DIPOLE["length_mm"])
    lo = band["freq_lo_ghz"]
    assert base["resonance_ghz"] < band["freq_hi_ghz"], (base["resonance_ghz"], band)
    assert abs(base["resonance_ghz"] / lo - 1.0) < 0.03, (
        f"coarse-mesh resonance {base['resonance_ghz']} GHz is > 3% from the "
        f"dipole_resonance band edge {lo:.4f} GHz")
    print(f"    dipole: f_r {base['resonance_ghz']} GHz (golden {DIPOLE_GOLDEN_GHZ}), "
          f"window-shifted {shifted['resonance_ghz']} GHz at the same mesh; "
          f"band {lo:.3f}–{band['freq_hi_ghz']:.3f} GHz")

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

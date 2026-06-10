"""Advanced cross-family toys — harder closed-form anchors that probe the *limits*
of the shipped analysis modules, not just their happy paths.

Each test is either (a) an exact identity the simple anchors don't reach (Brewster
zero, Helmholtz reciprocity, the concentric-sphere enclosure, the radiation-shield
network, the T⁴ factorization of h_rad), (b) a regression for a real defect these
probes uncovered (negative-incidence TIR raised a math domain error; an
area-ratio/view-factor pair violating reciprocity was silently accepted), or (c) a
documented behavioural envelope (the Goodman compressive-mean asymmetry, the 2-D
plane-stress vs thin-3-D SIMP offset).

Pure-Python + NumPy (for the SIMP cross-check); no FreeCAD, no external solver.

Run:  .venv/bin/python3 tests/test_toys_advanced.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import durability as du  # noqa: E402
from driftpin.analysis import materials as mat  # noqa: E402
from driftpin.analysis import optics as op      # noqa: E402
from driftpin.analysis import thermal as th     # noqa: E402

_N_PMMA = 1.49062


# --- optics: exact identities beyond the Snell/Fresnel/TIR anchors -------------

def test_brewster_angle_kills_p_polarization():
    # At θ_B = atan(n2/n1) the p-reflectance is *identically* zero — a one-point
    # exact gate much sharper than any band.
    theta_b = math.degrees(math.atan(_N_PMMA))
    r = op.fresnel_reflectance(theta_b, 1.0, _N_PMMA)
    assert r["r_p"] < 1e-12, r["r_p"]
    assert r["r_s"] > 0.05, "s-polarization must NOT vanish at Brewster"
    # and reflectance is a minimum for p there: neighbours are strictly larger
    for d in (-1.0, 1.0):
        assert op.fresnel_reflectance(theta_b + d, 1.0, _N_PMMA)["r_p"] > r["r_p"]


def test_helmholtz_reciprocity_of_reflectance():
    # Stokes relations: the power reflectance from medium 1 at θ1 equals the
    # reflectance from medium 2 at the refracted angle θ2 — exact, both pols.
    for theta1 in (5.0, 20.0, 35.0, 55.0, 75.0):
        theta2 = op.refract_angle(theta1, 1.0, _N_PMMA)
        fwd = op.fresnel_reflectance(theta1, 1.0, _N_PMMA)
        rev = op.fresnel_reflectance(theta2, _N_PMMA, 1.0)
        assert abs(fwd["r_s"] - rev["r_s"]) < 1e-12, (theta1, fwd["r_s"], rev["r_s"])
        assert abs(fwd["r_p"] - rev["r_p"]) < 1e-12, (theta1, fwd["r_p"], rev["r_p"])


def test_grazing_incidence_reflectance_approaches_one():
    assert op.fresnel_reflectance(89.9, 1.0, _N_PMMA)["reflectance"] > 0.97
    assert op.fresnel_reflectance(89.99, 1.0, _N_PMMA)["reflectance"] > 0.995


def test_negative_incidence_is_symmetric_and_tirs():
    # Regression: fresnel_reflectance(-80°, n→air) used to raise a math domain
    # error (the TIR guard tested sin_t > 1 without abs).
    r = op.fresnel_reflectance(-80.0, _N_PMMA, 1.0)
    assert r["tir"] and r["transmittance"] == 0.0, r
    # symmetry: R(−θ) == R(θ), and the refracted angle keeps the sign of θ
    a, b = op.fresnel_reflectance(-30.0, 1.0, _N_PMMA), op.fresnel_reflectance(30.0, 1.0, _N_PMMA)
    assert abs(a["reflectance"] - b["reflectance"]) < 1e-15
    assert abs(op.refract_angle(-30.0, 1.0, _N_PMMA)
               + op.refract_angle(30.0, 1.0, _N_PMMA)) < 1e-12


def test_pmma_sellmeier_dispersion_is_normal_and_abbe_plausible():
    # The optical corpus' Sellmeier fit must reproduce normal dispersion
    # (n_F > n_d > n_C) and a PMMA Abbe number in the handbook band (~52–58).
    card = mat.get("PMMA")
    n_d = mat.refractive_index_at(card, 587.56)
    n_f = mat.refractive_index_at(card, 486.13)
    n_c = mat.refractive_index_at(card, 656.27)
    assert n_f > n_d > n_c, (n_f, n_d, n_c)
    abbe = (n_d - 1.0) / (n_f - n_c)
    assert 50.0 < abbe < 60.0, abbe
    assert abs(n_d - _N_PMMA) < 5e-4, n_d


# --- radiation: enclosure identities + the reciprocity guard -------------------

def test_view_factor_reciprocity_violation_is_rejected():
    # Regression: A1·F12 > A2 needs F21 > 1 — unphysical, used to be silently
    # accepted and returned a wrong number.
    try:
        th.radiation_exchange(500, 100, 0.8, 0.8,
                              area_1_m2=2.0, area_2_m2=1.0, view_factor=1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for A1*F12 > A2")
    # the boundary case A1*F12 == A2 (e.g. concentric spheres) stays legal
    th.radiation_exchange(500, 100, 0.8, 0.8,
                          area_1_m2=1.0, area_2_m2=2.0, view_factor=2.0 / 1.0 / 2.0)


def test_concentric_spheres_match_the_enclosure_network():
    # The textbook concentric-sphere formula
    #   Q = σ·A1·(T1⁴−T2⁴) / (1/ε1 + (A1/A2)·(1/ε2 − 1))
    # is the general two-surface network at F12 = 1 — they must agree exactly.
    sigma = 5.670374419e-8
    r1, r2 = 0.5, 1.0
    a1, a2 = 4 * math.pi * r1 ** 2, 4 * math.pi * r2 ** 2
    e1, e2, t1_k, t2_k = 0.3, 0.7, 600.0, 300.0
    q_book = sigma * a1 * (t1_k ** 4 - t2_k ** 4) / (1 / e1 + (a1 / a2) * (1 / e2 - 1))
    r = th.radiation_exchange(t1_k - 273.15, t2_k - 273.15, e1, e2,
                              area_1_m2=a1, area_2_m2=a2, view_factor=1.0)
    assert abs(r["q_net_w"] - q_book) < 1e-6 * q_book, (r["q_net_w"], q_book)


def test_one_radiation_shield_halves_the_exchange():
    # Series enclosure network: a shield with the same ε on both faces between
    # two equal-ε plates floats to T_s⁴ = (T1⁴+T2⁴)/2 and cuts the net flux by
    # exactly 2 (N shields → ÷(N+1)). Composes radiation_exchange against itself.
    eps, t1_c, t2_c = 0.8, 400.0, 50.0
    q_direct = th.radiation_exchange(t1_c, t2_c, eps, eps)["flux_w_m2"]

    def imbalance(ts_c):
        return (th.radiation_exchange(t1_c, ts_c, eps, eps)["flux_w_m2"]
                - th.radiation_exchange(ts_c, t2_c, eps, eps)["flux_w_m2"])

    lo, hi = t2_c, t1_c
    for _ in range(80):                                # bisect the shield temp
        mid = 0.5 * (lo + hi)
        if imbalance(mid) > 0:
            lo = mid
        else:
            hi = mid
    ts = 0.5 * (lo + hi)
    q_shielded = th.radiation_exchange(t1_c, ts, eps, eps)["flux_w_m2"]
    assert abs(q_shielded / q_direct - 0.5) < 1e-6, q_shielded / q_direct
    # the floating temperature is the quartic mean, NOT the arithmetic mean
    t_quartic = ((((t1_c + 273.15) ** 4 + (t2_c + 273.15) ** 4) / 2) ** 0.25) - 273.15
    assert abs(ts - t_quartic) < 1e-3, (ts, t_quartic)
    assert ts > (t1_c + t2_c) / 2, "T⁴ averaging must sit above the linear mean"


def test_h_rad_times_dt_is_an_exact_factorization_not_an_approximation():
    # σ(T1⁴−T2⁴) = σ(T1²+T2²)(T1+T2)(T1−T2) exactly, so h_rad·ΔT equals the
    # two-plate flux at ANY ΔT when h_rad is evaluated at both endpoint temps.
    # The linearization error people associate with h_rad only appears when it
    # is frozen at one operating point (as thermal_lumped's transient must).
    for t1_c, t2_c in ((100.5, 99.5), (300.0, 20.0), (800.0, -40.0)):
        r = th.radiation_exchange(t1_c, t2_c, 0.9, 0.6)
        lin = r["h_rad_w_m2k"] * (t1_c - t2_c)
        assert abs(lin - r["two_plate_flux_w_m2"]) < 1e-6 * abs(r["two_plate_flux_w_m2"]), \
            (t1_c, t2_c, lin, r["two_plate_flux_w_m2"])


# --- durability: the Goodman compressive-mean asymmetry ------------------------

def test_goodman_compressive_mean_envelope():
    # Documented envelope, not a bug: the infinite-life SF clamps a compressive
    # mean to zero benefit (max(σm, 0)), while the finite-life equivalent
    # amplitude fold σ_ar = σa/(1−σm/σuts) DOES credit it (σ_ar < σa for σm<0).
    # Both directions are conventional; the asymmetry is the part worth pinning.
    base = du.fatigue_check(stress_range_mpa=400, mean_stress_mpa=0,
                            material="AL6061-T6")
    comp = du.fatigue_check(stress_range_mpa=400, mean_stress_mpa=-150,
                            material="AL6061-T6")
    assert abs(comp["safety_factor"] - base["safety_factor"]) < 1e-9, \
        "SF must not credit a compressive mean"
    assert comp["equiv_reversed_mpa"] < base["equiv_reversed_mpa"], \
        "finite-life fold credits the compressive mean (documented convention)"
    assert comp["life_cycles"] is None or base["life_cycles"] is None or \
        comp["life_cycles"] >= base["life_cycles"]


# --- topology: 2-D plane stress vs a one-element-thick 3-D slab ----------------

def test_thin_3d_slab_reproduces_the_2d_plane_stress_cantilever():
    # Cross-family relative gate: the same uniform-density cantilever solved by
    # the 2-D bilinear-quad plane-stress model and by one layer of trilinear
    # hexes (free z faces ≈ plane stress) must land within a few percent — the
    # offset (~5% on this grid) is the element-formulation difference, and a
    # change much beyond that band means one of the two stiffness matrices broke.
    from driftpin.analysis import topology as topo
    r2 = topo.simp_topology_2d(nelx=12, nely=4, keep_fraction=0.95, penal=1.0,
                               max_iter=1)
    r3 = topo.simp_topology_3d(nelx=12, nely=4, nelz=1, keep_fraction=0.95,
                               penal=1.0, max_iter=1)
    ratio = r3["compliance_initial"] / r2["compliance_initial"]
    assert 1.0 < ratio < 1.12, ratio


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
            print(f"  FAIL {name:58s} ({time.time() - t0:.2f}s)  {type(e).__name__}: {e}")
        else:
            print(f"  PASS {name:58s} ({time.time() - t0:.2f}s)")

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

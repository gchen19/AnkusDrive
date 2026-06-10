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

from driftpin.analysis import cfd               # noqa: E402
from driftpin.analysis import cht               # noqa: E402
from driftpin.analysis import cost              # noqa: E402
from driftpin.analysis import dfx               # noqa: E402
from driftpin.analysis import durability as du  # noqa: E402
from driftpin.analysis import em                # noqa: E402
from driftpin.analysis import kinematics        # noqa: E402
from driftpin.analysis import machine_elements as me  # noqa: E402
from driftpin.analysis import materials as mat  # noqa: E402
from driftpin.analysis import optics as op      # noqa: E402
from driftpin.analysis import slicing as sl     # noqa: E402
from driftpin.analysis import thermal as th     # noqa: E402
from driftpin.analysis import tolerance as tol  # noqa: E402
from driftpin.analysis import vibration as vib  # noqa: E402

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


# === Batch 1 — pure-math cores (tolerance · materials · cfd) ==================

# --- tolerance: the √N statistics + fit reciprocity ---------------------------

def test_rss_band_is_sqrt_n_smaller_than_worstcase():
    # Scaling law: for N identical ±t links the worst-case spread is N·2t but the
    # RSS 3σ spread is only √N·2t, so worstcase/RSS = √N exactly. This is THE reason
    # to stack statistically — the basic test only checks one chain's numbers.
    for n in (1, 4, 9, 16):
        chain = [{"nominal": 10.0, "tol": 0.1}] * n
        r = tol.stackup(chain, "rss")
        wc = r["worstcase"]["spread"]
        rss = r["rss"]["max_3s"] - r["rss"]["min_3s"]
        assert abs(wc - n * 0.2) < 1e-9, (n, wc)            # worst-case is exact N·2t
        assert abs(wc / rss - math.sqrt(n)) < 1e-9, (n, wc / rss)


def test_montecarlo_band_converges_to_rss():
    # Conservation/closure: the sampled σ must converge to the analytic RSS σ
    # (same underlying normal links), and the MC mean to the nominal.
    chain = [{"nominal": 25.0, "tol": 0.05},     # symmetric links: center == nominal,
             {"nominal": 12.0, "tol": 0.04},     # so the MC mean lands on the nominal
             {"nominal": 8.0, "tol": 0.03}]
    r = tol.stackup(chain, "montecarlo", samples=40000)
    assert abs(r["montecarlo"]["mean"] - r["nominal"]) < 5e-3, r["montecarlo"]["mean"]
    assert abs(r["montecarlo"]["std"] / r["rss"]["sigma"] - 1.0) < 0.03, r


def test_fit_check_hole_shaft_reciprocity():
    # Reciprocity: clearance = hole − shaft, so swapping the two negates every
    # clearance bound and turns a clearance fit into an interference fit.
    hole = {"nominal": 20.0, "plus": 0.05, "minus": 0.0}
    shaft = {"nominal": 20.0, "plus": 0.0, "minus": -0.03}
    a = tol.fit_check(hole, shaft)
    b = tol.fit_check(shaft, hole)                          # swapped
    assert abs(b["min_clearance"] + a["max_clearance"]) < 1e-9, (a, b)
    assert abs(b["max_clearance"] + a["min_clearance"]) < 1e-9, (a, b)
    assert abs(b["nominal_clearance"] + a["nominal_clearance"]) < 1e-9, (a, b)
    assert a["fit_class"] == "clearance" and b["fit_class"] == "interference"


def test_subtractive_link_flips_its_contribution():
    # Exact identity: a direction=-1 link subtracts — its nominal and bounds enter
    # with the opposite sign, so adding A then A⁻ cancels to zero ± the doubled band.
    a = {"nominal": 50.0, "plus": 0.1, "minus": -0.1}
    minus_a = {"nominal": 50.0, "plus": 0.1, "minus": -0.1, "direction": -1}
    solo = tol.stackup([a], "worstcase")
    pair = tol.stackup([a, minus_a], "worstcase")
    assert abs(solo["nominal"] - 50.0) < 1e-9
    assert abs(pair["nominal"]) < 1e-9, pair["nominal"]      # 50 − 50 = 0
    assert abs(pair["worstcase"]["spread"] - 2 * solo["worstcase"]["spread"]) < 1e-9


# --- materials: canonical-unit round-trip + Ashby ranking ---------------------

def test_numeric_accessor_is_a_unit_consistent_roundtrip():
    # Exact identity: the canonical accessors are just the parsed quantity rescaled —
    # youngs_gpa = MPa·1e-3, density_kg_m3 = (g/cc value)·1000.
    card = mat.get("AL6061-T6")
    e_mpa = mat.parse_quantity(card["YoungsModulus"])[0]
    assert abs(mat.numeric(card, "youngs_gpa") - e_mpa * 1e-3) < 1e-9
    assert abs(mat.numeric(card, "youngs_mpa") - e_mpa) < 1e-6
    assert abs(mat.numeric(card, "density_kg_m3")
               - mat.numeric(card, "density_g_cc") * 1000.0) < 1e-6


def test_specific_strength_ranking_is_monotone_and_exact():
    # Exact identity + monotonicity: the specific-strength score IS yield/density for
    # every candidate, and select() returns them strictly best-first.
    sel = mat.select(rank_by="specific_strength")
    cands = sel["candidates"]
    assert len(cands) >= 3, sel
    for c in cands:
        assert abs(c["score"] - c["yield_mpa"] / c["density_g_cc"]) < 1e-3, c
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True), scores


def test_select_min_filter_excludes_below_threshold():
    # Behavioural envelope: a min_yield filter admits only cards at/above it (a card
    # missing the property fails a min, conservatively).
    thresh = 250.0
    sel = mat.select(criteria={"min_yield_mpa": thresh}, rank_by="strength")
    for c in sel["candidates"]:
        assert c["yield_mpa"] >= thresh, c


# --- cfd: the dimensionless invariants behind the dimensional anchors ----------

def test_poiseuille_number_f_re_is_64():
    # Exact invariant: the laminar Darcy friction factor obeys f·Re ≡ 64 for ANY
    # fluid, diameter, or speed — the dimensionless core the D⁴ Δp law rides on.
    # (Tested at moderate Re where the 3-decimal display rounding of Re is negligible;
    # at creeping Re≈1 the rounding alone shifts the product ~0.05.)
    for fluid, d, v in (("water-20c", 10, 0.05), ("air-20c", 20, 0.5),
                        ("water-20c", 20, 0.08), ("air-20c", 10, 1.5)):
        r = cfd.pipe_pressure_drop(diameter_mm=d, length_mm=1000, velocity_m_s=v,
                                   fluid=fluid)
        assert 50 < r["reynolds"] < 2300, (fluid, r["reynolds"])
        assert abs(r["friction_factor"] * r["reynolds"] - 64.0) < 0.02, (fluid, r)


def test_blasius_cf_sqrt_re_is_invariant():
    # Exact invariant: the Blasius average skin-friction obeys Cf·√Re_L ≡ 1.328,
    # independent of length, speed, or fluid (laminar).
    for fluid, L, v in (("air-20c", 100, 5.0), ("water-20c", 50, 1.0),
                        ("air-20c", 250, 2.5)):
        r = cfd.flat_plate_drag(length_mm=L, velocity_m_s=v, fluid=fluid)
        assert r["laminar"], (fluid, r["reynolds_l"])
        assert abs(r["cf_avg"] * math.sqrt(r["reynolds_l"]) - 1.328) < 1e-3, (fluid, r)


def test_stokes_cd_re_product_is_24():
    # Exact invariant: creeping-flow drag obeys Cd·Re ≡ 24 regardless of size, speed,
    # or fluid (Re ≪ 1).
    for fluid, d, v in (("glycerin-20c", 2, 0.001), ("glycerin-20c", 0.5, 0.002),
                        ("oil-sae30-20c", 1, 0.0005)):
        r = cfd.stokes_sphere_drag(diameter_mm=d, velocity_m_s=v, fluid=fluid)
        assert r["stokes_valid"], (fluid, r["reynolds"])
        assert abs(r["cd"] * r["reynolds"] - 24.0) < 0.05, (fluid, r)


def test_pipe_regime_thresholds_are_sharp():
    # Behavioural envelope: the regime classification flips exactly at Re=2300 and
    # Re=4000 (set the velocity to straddle each threshold for water).
    mu, rho, d = 1.002e-3, 998.2, 0.01                      # water-20c, 10 mm
    def regime_at(re):
        v = re * mu / (rho * d)
        return cfd.pipe_pressure_drop(diameter_mm=10, length_mm=1000,
                                      velocity_m_s=v, fluid="water-20c")["regime"]
    assert regime_at(2299.0) == "laminar"
    assert regime_at(2301.0) == "transitional"
    assert regime_at(3999.0) == "transitional"
    assert regime_at(4001.0) == "turbulent"


# === Batch 3 — the M6 newcomers (cht · em) ====================================

# --- cht: the series resistance network + the h-free channel balance ----------

def test_composite_wall_resistance_adds_and_commutes():
    # Conservation/composition: R_total is the exact series sum of the layer
    # resistances, and reordering the layers leaves U and q unchanged (series
    # resistances commute) while shifting the interface temperatures.
    a = cht.composite_wall([{"thickness_mm": 10, "k": 50.0},
                            {"thickness_mm": 5, "k": 1.0}], 200.0, 25.0)
    assert abs(a["r_total_m2k_w"] - sum(a["layer_resistances_m2k_w"])) < 1e-9, a
    b = cht.composite_wall([{"thickness_mm": 5, "k": 1.0},
                            {"thickness_mm": 10, "k": 50.0}], 200.0, 25.0)
    assert abs(a["u_w_m2k"] - b["u_w_m2k"]) < 1e-9, (a["u_w_m2k"], b["u_w_m2k"])
    assert abs(a["q_w_m2"] - b["q_w_m2"]) < 1e-9
    assert a["interface_temps_c"] != b["interface_temps_c"]      # order changes drops


def test_composite_wall_film_resistance_vanishes_in_the_limit():
    # Asymptotic limit: a perfect inner film (h_in → ∞) adds 1/h_in → 0, so R_total
    # converges to the no-film value — the conjugate coupling degenerates to a fixed
    # wall temperature.
    big = cht.composite_wall([{"thickness_mm": 10, "k": 50.0}], 200.0, 25.0,
                             h_in=1e12, h_out=20.0)
    none = cht.composite_wall([{"thickness_mm": 10, "k": 50.0}], 200.0, 25.0,
                              h_out=20.0)
    assert abs(big["r_total_m2k_w"] - none["r_total_m2k_w"]) < 1e-6, (big, none)


def test_composite_wall_interface_temps_walk_the_drops_exactly():
    # Exact identity: q ≡ U·ΔT and each surface temperature is the running sum of the
    # upstream drops — T₀ = t_in − q/h_in, then minus q·rᵢ per layer, landing within
    # q/h_out of t_out at the last surface.
    h_in, h_out = 500.0, 20.0
    w = cht.composite_wall([{"thickness_mm": 8, "k": 30.0},
                            {"thickness_mm": 4, "k": 0.5}], 180.0, 20.0,
                           h_in=h_in, h_out=h_out)
    q = w["q_w_m2"]
    t = 180.0 - q / h_in
    walk = [t]
    for r in w["layer_resistances_m2k_w"]:
        t -= q * r
        walk.append(t)
    for got, exp in zip(w["interface_temps_c"], walk):
        assert abs(got - exp) < 1e-3, (w["interface_temps_c"], walk)
    assert abs(walk[-1] - q / h_out - 20.0) < 1e-3, walk[-1]    # closes on t_out


def test_cht_channel_outlet_is_h_free_and_solid_drop_decoupled():
    # Conservation + decoupling: the plug-flow outlet rise is the exact energy balance
    # T_out = T_in + q″L/(ρ·U·H·c_p) (no heat-transfer coefficient anywhere), and the
    # solid ΔT = q″·t/k depends ONLY on the wall — doubling the flow leaves it intact.
    kw = dict(flux_w_m2=5000.0, length_m=0.1, t_in_c=20.0, rho=1000.0, cp=4180.0,
              solid_thickness_m=0.005, k_solid=50.0)
    a = cht.cht_channel_oracle(fluid_height_m=0.01, velocity_m_s=1.0, **kw)
    b = cht.cht_channel_oracle(fluid_height_m=0.02, velocity_m_s=2.0, **kw)
    hand = 5000.0 * 0.1 / (1000.0 * 1.0 * 0.01 * 4180.0)
    assert abs(a["dt_out_k"] - hand) < 1e-6, (a["dt_out_k"], hand)
    assert abs(a["dt_solid_k"] - 5000.0 * 0.005 / 50.0) < 1e-9, a    # q″t/k = 0.5 K
    assert abs(a["dt_solid_k"] - b["dt_solid_k"]) < 1e-12            # fluid-independent
    assert b["dt_out_k"] < a["dt_out_k"]                            # 4× ṁ → ¼ rise


# --- em: skin-depth scaling, Ohm/Joule, and the field laws --------------------

def test_skin_depth_scales_as_inverse_sqrt_frequency():
    # Scaling law: δ ∝ f^(−1/2) and the per-square surface resistance R_s = 1/(σδ)
    # ∝ f^(+1/2) — quadruple the frequency, halve δ and double R_s. Exact.
    lo = em.skin_depth(50.0, conductivity_s_m=5.8e7)
    hi = em.skin_depth(200.0, conductivity_s_m=5.8e7)               # 4× frequency
    assert abs(lo["skin_depth_m"] / hi["skin_depth_m"] - 2.0) < 1e-9
    assert abs(hi["surface_resistance_ohm"] / lo["surface_resistance_ohm"] - 2.0) < 1e-9
    assert abs(lo["surface_resistance_ohm"] - 1.0 / (5.8e7 * lo["skin_depth_m"])) < 1e-15


def test_dc_resistance_and_joule_identity():
    # Exact identity: R = L/(σA) (so 2×L → 2×R, 2×A → ½R), and the Joule pair is
    # self-consistent: P ≡ V²/R ≡ I²R.
    r = em.dc_resistance(1000.0, 1.0, conductivity_s_m=5.8e7, voltage_v=1.0)
    assert abs(r["resistance_ohm"] - 1.0 / (5.8e7 * 1e-6)) < 1e-9
    longer = em.dc_resistance(2000.0, 1.0, conductivity_s_m=5.8e7)
    fatter = em.dc_resistance(1000.0, 2.0, conductivity_s_m=5.8e7)
    assert abs(longer["resistance_ohm"] / r["resistance_ohm"] - 2.0) < 1e-9
    assert abs(fatter["resistance_ohm"] / r["resistance_ohm"] - 0.5) < 1e-9
    assert abs(r["joule_w"] - r["current_a"] ** 2 * r["resistance_ohm"]) < 1e-9
    assert abs(r["joule_w"] - 1.0 / r["resistance_ohm"]) < 1e-12   # V=1 → P=1/R


def test_wire_field_is_inverse_distance_solenoid_is_uniform():
    # Exact field laws: a straight wire's B ∝ 1/r (so B·r is invariant, and 2×r → ½B,
    # 2×I → 2×B); a long solenoid's interior B = μ₀·μ_r·n·I is linear in n, I, μ_r and
    # independent of radius.
    b1 = em.wire_field(100.0, 10.0)
    b2 = em.wire_field(100.0, 20.0)
    assert abs(b1["b_t"] * b1["distance_m"] - b2["b_t"] * b2["distance_m"]) < 1e-18
    assert abs(b1["b_t"] / b2["b_t"] - 2.0) < 1e-9
    assert abs(em.wire_field(200.0, 10.0)["b_t"] / b1["b_t"] - 2.0) < 1e-9
    mu0 = 4.0e-7 * math.pi
    s = em.solenoid_field(1000.0, 2.0)
    assert abs(s["b_t"] - mu0 * 1000.0 * 2.0) < 1e-12
    assert abs(em.solenoid_field(2000.0, 2.0)["b_t"] / s["b_t"] - 2.0) < 1e-9
    assert abs(em.solenoid_field(1000.0, 2.0, mu_r=100.0)["b_t"] / s["b_t"] - 100.0) < 1e-6


# === Batch 2 — scaling laws (machine_elements · vibration · durability) =======

# --- machine_elements: power laws + the size-free invariants ------------------

def test_bearing_life_follows_the_iso281_power_law():
    # Scaling law: L10 = (C/P)^p revolutions, p=3 (ball) / 10/3 (roller). So doubling
    # the dynamic capacity C multiplies life by exactly 2^p, and halving the load P
    # does the same — the (C/P) ratio is all that matters. Sharper than the basic
    # test's single L10h point value.
    ball = me.bearing_life(10000, 2500, 1500, kind="ball")
    ball_2c = me.bearing_life(20000, 2500, 1500, kind="ball")     # 2× capacity
    ball_halfp = me.bearing_life(10000, 1250, 1500, kind="ball")  # ½ load
    assert ball["exponent"] == 3.0
    assert abs(ball_2c["l10_million_rev"] / ball["l10_million_rev"] - 8.0) < 1e-6
    assert abs(ball_halfp["l10_million_rev"] / ball["l10_million_rev"] - 8.0) < 1e-6
    roller = me.bearing_life(10000, 2500, 1500, kind="roller")
    roller_2c = me.bearing_life(20000, 2500, 1500, kind="roller")
    assert abs(roller["exponent"] - 10.0 / 3.0) < 1e-12
    assert abs(roller_2c["l10_million_rev"] / roller["l10_million_rev"]
               - 2.0 ** (10.0 / 3.0)) < 1e-3       # rounding of l10 to 2 dp


def test_spring_rate_scaling_and_wahl_is_index_only():
    # Scaling + invariant: rate k ∝ d⁴/(D³·Na), so 2×Na → ½ rate and 2×d (D fixed) →
    # 16× rate. The Wahl factor depends ONLY on the index C=D/d — a geometrically
    # scaled spring (d and D both doubled, same C) has an identical Wahl factor.
    base = me.spring_check(2.0, 16.0, 8.0, force_n=50.0)
    twice_coils = me.spring_check(2.0, 16.0, 16.0, force_n=50.0)   # 2×Na
    thick_wire = me.spring_check(4.0, 16.0, 8.0, force_n=50.0)     # 2×d, D fixed
    scaled = me.spring_check(4.0, 32.0, 8.0, force_n=50.0)         # same C=8, 2× size
    assert abs(base["rate_n_mm"] / twice_coils["rate_n_mm"] - 2.0) < 1e-3
    assert abs(thick_wire["rate_n_mm"] / base["rate_n_mm"] - 16.0) < 1e-3
    assert base["spring_index"] == scaled["spring_index"] == 8.0
    assert base["wahl_factor"] == scaled["wahl_factor"]            # C-only, exact
    assert thick_wire["wahl_factor"] > base["wahl_factor"]         # smaller C → larger Kw


def test_lewis_bending_stress_falls_monotonically_with_tooth_count():
    # Monotonicity: the Lewis form factor Y(Z)=0.484−2.87/Z rises with tooth count, so
    # at a FIXED tangential load the bending stress σ=Ft/(b·m·Y) falls monotonically —
    # more (smaller) teeth spread the load. Independent of the changing pitch diameter.
    ys, stresses = [], []
    for z in (10, 20, 50):
        g = me.gear_rating(2.0, z, 20.0, tangential_force_n=500.0)
        ys.append(g["lewis_form_factor"])
        stresses.append(g["bending_stress_mpa"])
    assert ys == sorted(ys), ys                       # Y strictly ↑ in teeth
    assert stresses == sorted(stresses, reverse=True), stresses   # σ strictly ↓


def test_belt_tension_ratio_squares_when_friction_doubles():
    # Exact scaling: Eytelwein T1/T2 = e^(μθ) with θ a geometry-only wrap angle, so
    # doubling μ SQUARES the tension ratio (e^(2μθ)=(e^(μθ))²). The wrap angle itself
    # is independent of μ.
    lo = me.belt_drive(1000, 100, 200, 300, 1500, friction_coef=0.3)
    hi = me.belt_drive(1000, 100, 200, 300, 1500, friction_coef=0.6)
    assert lo["wrap_angle_deg"] == hi["wrap_angle_deg"]            # μ-independent
    theta = math.radians(lo["wrap_angle_deg"])
    assert abs(lo["tension_ratio"] - math.exp(0.3 * theta)) < 1e-3
    assert abs(hi["tension_ratio"] - lo["tension_ratio"] ** 2) < 1e-3


def test_press_fit_pressure_is_linear_in_interference():
    # Linear: Lamé contact pressure p ∝ δ (the radial interference), so 2×δ doubles the
    # contact pressure, the hub hoop stress, and the friction-limited torque/axial
    # capacity alike — everything rides off the one linear pressure.
    a = me.press_fit_stress(20, 40, 0.02, 30)
    b = me.press_fit_stress(20, 40, 0.04, 30)                      # 2× interference
    assert abs(b["contact_pressure_mpa"] / a["contact_pressure_mpa"] - 2.0) < 1e-6
    assert abs(b["hub_hoop_stress_mpa"] / a["hub_hoop_stress_mpa"] - 2.0) < 1e-6
    assert abs(b["torque_capacity_nm"] / a["torque_capacity_nm"] - 2.0) < 1e-3
    assert abs(b["axial_force_n"] / a["axial_force_n"] - 2.0) < 1e-4


def test_bolt_separation_load_is_independent_of_the_external_load():
    # Regression/invariant: the separation load P_sep = F_preload/(1−C) is a property of
    # the preload and stiffness split ALONE — it does not move when the applied external
    # load changes. The separation margin (P_sep / P_ext) then scales inversely with the
    # load: double the external load, halve the margin.
    light = me.bolted_joint_check(10, preload_n=20000, external_load_n=5000,
                                  joint_stiffness_ratio=0.3)
    heavy = me.bolted_joint_check(10, preload_n=20000, external_load_n=10000,
                                  joint_stiffness_ratio=0.3)
    assert light["separation_load_n"] == heavy["separation_load_n"]      # exact
    assert abs(light["separation_load_n"] - 20000 / 0.7) < 0.1
    assert abs(light["separation_margin"] - light["separation_load_n"] / 5000) < 0.01
    assert heavy["separation_margin"] < light["separation_margin"]       # inverse in P_ext


# --- vibration: SRSS energy composition + the beam frequency scalings ----------

def test_srss_combines_modal_energy_with_no_cross_term():
    # Conservation/composition: SRSS treats each mode as an independent SDOF resonator,
    # so the combined response is pure energy addition rms_g² ≡ Σ gᵢ² with NO cross
    # term — running both modes together equals the root-sum-square of each mode alone.
    prof = [{"hz": 50, "g2_hz": 0.04}, {"hz": 2000, "g2_hz": 0.04}]   # flat band 50–2000
    both = vib.random_vibration([300.0, 1200.0], prof, q=10.0)
    only_1 = vib.random_vibration([300.0], prof, q=10.0)["rms_g"]
    only_2 = vib.random_vibration([1200.0], prof, q=10.0)["rms_g"]
    assert abs(both["rms_g"] - math.sqrt(only_1 ** 2 + only_2 ** 2)) < 1e-3
    contribs = [m["contribution_g"] for m in both["modes"]]
    assert abs(both["rms_g"] - math.sqrt(sum(c * c for c in contribs))) < 1e-3
    # and the energy equals the closed-form (π/2)·W·Q·Σfᵢ (flat PSD)
    energy = (math.pi / 2) * 0.04 * 10 * (300.0 + 1200.0)
    assert abs(both["rms_g"] ** 2 - energy) < 1e-3 * energy


def test_a_mode_above_the_psd_band_contributes_exactly_zero():
    # Regression guard (the ruggedization rule): a mode stiffened past the top of the
    # excitation band sees zero specified PSD and contributes EXACTLY zero to the RMS —
    # adding it leaves the response identical to the in-band mode alone.
    prof = [{"hz": 50, "g2_hz": 0.04}, {"hz": 2000, "g2_hz": 0.04}]
    with_oob = vib.random_vibration([300.0, 5000.0], prof, q=10.0)     # 5000 > 2000
    oob = with_oob["modes"][1]
    assert oob["psd_g2_hz"] == 0.0 and oob["contribution_g"] == 0.0 and not oob["in_band"]
    in_band_only = vib.random_vibration([300.0], prof, q=10.0)["rms_g"]
    assert with_oob["rms_g"] == in_band_only


def test_beam_frequency_scales_as_inverse_length_squared_and_linear_in_height():
    # Scaling law: f_n = (βL)²/(2π)·√(E·I/(ρ·A·L⁴)) with I=b·h³/12, A=b·h ⇒ √(I/A)=h/√12.
    # So f ∝ 1/L² (double the length → quarter the frequency) and f ∝ h (double the
    # height → double the frequency), both exact for the slender Euler–Bernoulli beam.
    kw = dict(boundary="cantilever", n_modes=3, youngs_gpa=200, density_kg_m3=7850)
    base = vib.beam_natural_frequencies(200, 20, 10, **kw)
    longer = vib.beam_natural_frequencies(400, 20, 10, **kw)           # 2× length
    taller = vib.beam_natural_frequencies(200, 20, 20, **kw)           # 2× height
    assert base["slenderness"] >= 10 and longer["slenderness"] >= 10   # stay slender
    assert abs(base["first_mode_hz"] / longer["first_mode_hz"] - 4.0) < 1e-4
    assert abs(taller["first_mode_hz"] / base["first_mode_hz"] - 2.0) < 1e-4


def test_simply_supported_beta_l_is_n_pi_and_frequency_scales_as_n_squared():
    # Exact identity + scaling: the pinned–pinned eigenvalues are βL ≡ nπ exactly, and
    # since f_n ∝ (βL)² the harmonic series is f_n ∝ n² (f₂/f₁=4, f₃/f₁=9, f₄/f₁=16).
    # Excludes the basic cantilever βL=1.875 anchor — this is the exact-multiple sibling.
    ss = vib.beam_natural_frequencies(300, 25, 12, boundary="simply_supported",
                                      n_modes=4, youngs_gpa=200, density_kg_m3=7850)
    for n, bl in zip((1, 2, 3, 4), ss["beta_l"]):
        assert abs(bl - n * math.pi) < 1e-5, (n, bl)
    f = ss["frequencies_hz"]
    for n in (2, 3, 4):
        assert abs(f[n - 1] / f[0] - n * n) < 1e-3, (n, f[n - 1] / f[0])


# --- durability: the fully-reversed limit, Archard, the S-N slope, overload ----

def test_goodman_correction_collapses_to_the_amplitude_at_zero_mean():
    # Exact limit: the equivalent fully-reversed amplitude σ_ar = σ_a/(1−σ_m/σ_uts)
    # degenerates to σ_a itself when the mean stress is zero — the mean-stress
    # correction vanishes and the governing mode is plain fully-reversed fatigue.
    r = du.fatigue_check(stress_range_mpa=400, mean_stress_mpa=0.0, material="AL6061-T6")
    assert r["equiv_reversed_mpa"] == r["stress_amplitude_mpa"] == 200.0
    assert r["governing_mode"] == "fully_reversed_fatigue"


def test_archard_wear_is_linear_in_load_and_distance_inverse_in_hardness():
    # Linear: Archard V = k·F·s/H, so the wear volume doubles with load, doubles with
    # sliding distance, and halves when the hardness doubles — each input enters to the
    # first power. Pinned with explicit k and H to isolate the scaling from the lookups.
    kw = dict(wear_coef=5e-4, hardness_mpa=1000)
    base = du.wear_estimate(100, 1000, **kw)
    assert abs(base["volume_loss_mm3"] - 5e-4 * 100 * 1000 / (1000e6) * 1e9) < 1e-9  # =50
    assert du.wear_estimate(200, 1000, **kw)["volume_loss_mm3"] == 2 * base["volume_loss_mm3"]
    assert du.wear_estimate(100, 2000, **kw)["volume_loss_mm3"] == 2 * base["volume_loss_mm3"]
    assert du.wear_estimate(100, 1000, wear_coef=5e-4, hardness_mpa=2000)[
        "volume_loss_mm3"] == 0.5 * base["volume_loss_mm3"]


def test_sn_life_ratio_follows_the_basquin_log_log_slope():
    # Scaling law: on the finite-life Basquin line log10(N)=3+(log10(σ_ar)−log10(s1000))/b,
    # so at fixed (zero) mean a 2× stress amplitude changes life by exactly 2^(1/b),
    # where b is the log-log slope set by the endurance/1000-cycle strengths. A fixed,
    # material-defined ratio — no dependence on the absolute stress level.
    a = du.fatigue_check(stress_range_mpa=300, mean_stress_mpa=0, material="AL6061-T6")
    b2 = du.fatigue_check(stress_range_mpa=600, mean_stress_mpa=0, material="AL6061-T6")
    assert a["governing_mode"] == b2["governing_mode"] == "fully_reversed_fatigue"
    se, uts = a["endurance_mpa"], a["uts_mpa"]
    slope = math.log10(se / (0.9 * uts)) / math.log10(1e6 / 1e3)       # default S-N line
    expected = 2.0 ** (1.0 / slope)
    assert abs(b2["life_cycles"] / a["life_cycles"] / expected - 1.0) < 3e-3


def test_static_overload_fails_regardless_of_cycle_count():
    # Regression guard: a mean (or peak) stress at/above σ_uts is a static overload, not
    # a fatigue problem — the governing mode flips to "static_overload", pass is False,
    # and life collapses to zero no matter how few cycles are required.
    ov = du.fatigue_check(stress_range_mpa=100, mean_stress_mpa=400, material="AL6061-T6",
                          cycles=1)
    assert ov["governing_mode"] == "static_overload"
    assert ov["pass"] is False
    assert ov["life_cycles"] == 0


# === Batch 4 — design-for-X & throughput (dfx · cost · slicing · kinematics) ==

# --- dfx: the score arithmetic, the monotone assembly ladder, the fit/billable rules ---

def test_dfm_score_is_exactly_one_minus_violation_fraction():
    # Exact identity: score ≡ 1 − (#violations / max(#faces, 1)) where #violations is
    # the SUM of the three flagged lists (draft + undercut + min-wall), each face
    # counting once per kind it trips. Sharper than the basic 0-and-1 endpoints: this
    # pins the fractional arithmetic and that all three violation kinds add into it.
    # 3 faces, exactly 1 draft violation -> 1 − 1/3 = 0.667 (display-rounded to 3 dp).
    r3 = dfx.dfm_check([{"name": "wall_a", "draft_deg": 0.0},
                        {"name": "wall_b", "draft_deg": 5.0},
                        {"name": "wall_c", "draft_deg": 5.0}], process="injection")
    assert r3["draft_violations"] == ["wall_a"], r3
    assert abs(r3["score"] - round(1.0 - 1.0 / 3.0, 3)) < 1e-9, r3["score"]
    # one of each kind among 6 faces -> 3/6 -> score 0.5 (exact, no rounding).
    rm = dfx.dfm_check([{"name": "f0", "draft_deg": 0.5},                 # draft viol
                        {"name": "f1", "draft_deg": -2.0},                # undercut
                        {"name": "f2", "draft_deg": 5.0, "wall_mm": 0.4}, # wall viol
                        {"name": "f3", "draft_deg": 5.0},
                        {"name": "f4", "draft_deg": 5.0},
                        {"name": "f5", "draft_deg": 5.0}], process="injection")
    nviol = (len(rm["draft_violations"]) + len(rm["undercut_faces"])
             + len(rm["min_wall_violations"]))
    assert nviol == 3, rm
    assert abs(rm["score"] - 0.5) < 1e-9, rm["score"]
    assert rm["pass"] is False
    # the empty-face guard: max(#faces, 1) keeps the denominator at 1 -> score 1.0.
    assert dfx.dfm_check([], process="injection")["score"] == 1.0


def test_dfa_score_is_monotone_non_increasing_as_count_rises():
    # Monotonicity: assembly_score can only fall as part_count or fastener_count grows.
    # With the default symmetric_fraction=0 the handling band pins to 'medium'
    # (penalty 0.8) for the whole ladder, so the score is exactly 0.8·efficiency and
    # the strict decrease rides entirely on the falling efficiency = 1/(parts+fast).
    ladder = [dfx.dfa_check(2, 0), dfx.dfa_check(3, 0), dfx.dfa_check(3, 1),
              dfx.dfa_check(4, 1), dfx.dfa_check(4, 3), dfx.dfa_check(8, 5)]
    bands = {c["handling_difficulty"] for c in ladder}
    assert bands == {"medium"}, bands                       # handling held constant
    scores = [c["assembly_score"] for c in ladder]
    for lo, hi in zip(scores[1:], scores[:-1]):
        assert lo < hi, scores                              # strictly down each rung
    assert scores == sorted(scores, reverse=True), scores   # ...hence non-increasing
    for c in ladder:                                        # score == efficiency·handling
        assert abs(c["assembly_score"] - 0.8 * c["assembly_efficiency"]) < 1e-4, c


def test_pack_sort_to_fit_reorientation_and_billable_is_the_max():
    # Composition + exact identity: a part fits iff each SORTED dimension <= the carton's
    # matching SORTED dimension (orientation-free), and billable weight is
    # max(actual_mass, dim_weight) — what a carrier actually charges.
    carton = [300, 100, 400]
    # fits ONLY after reorientation: 350 > carton's 300 axis-for-axis, but the sorted
    # triples [90,90,350] <= [100,300,400] clear, so the part rotates in.
    reorient = [350, 90, 90]
    assert not all(p <= c for p, c in zip(reorient, carton))      # naive axis check fails
    assert all(p <= c for p, c in zip(sorted(reorient), sorted(carton)))
    assert dfx.pack_check(reorient, carton, mass_g=100)["fits"] is True
    # too big in EVERY orientation: its longest dim (450) beats the carton's (400).
    toobig = dfx.pack_check([450, 90, 90], carton, mass_g=100)
    assert toobig["fits"] is False and toobig["void_fraction"] is None
    # billable = max(actual, dim). Carton vol 1.2e7 mm³ -> 12000 cm³ -> 12000/5000 =
    # 2.4 kg DIM weight. A light part bills the DIM weight; a heavy one bills its mass.
    light = dfx.pack_check(reorient, carton, mass_g=500)          # 0.5 kg < 2.4 kg DIM
    assert abs(light["dim_weight_kg"] - 2.4) < 1e-9, light
    assert abs(light["billable_weight_kg"] - 2.4) < 1e-9, light
    heavy = dfx.pack_check(reorient, carton, mass_g=9000)         # 9.0 kg > 2.4 kg DIM
    assert abs(heavy["actual_mass_kg"] - 9.0) < 1e-9, heavy
    assert abs(heavy["billable_weight_kg"] - 9.0) < 1e-9, heavy
    assert abs(heavy["billable_weight_kg"]
               - max(heavy["actual_mass_kg"], heavy["dim_weight_kg"])) < 1e-9


# --- cost: the amortization asymptote + the linear/ratio drivers ---------------

def test_unit_cost_asymptotes_to_the_material_plus_process_floor():
    # Asymptotic/scaling law: unit_cost = floor + (tooling + setup_usd)/quantity, so the
    # fixed-cost share falls as 1/qty and unit_cost descends MONOTONICALLY to the
    # material+process floor (no tooling/setup share). The sharper sibling of the basic
    # "10000-off < 1-off" point check: an exact 1/qty law, not just an inequality.
    quants = (1, 10, 100, 1000, 10000)
    rs = [cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                             tooling_usd=5000.0, quantity=q, setup_min=10.0)
          for q in quants]
    ucs = [r["unit_cost"] for r in rs]
    assert all(ucs[i] > ucs[i + 1] for i in range(len(ucs) - 1)), ucs   # strictly ↓
    # floor = material + machining ONLY; the residual above it is exactly fixed/qty
    floor = rs[0]["material_cost"] + rs[0]["breakdown"]["machining_cost"]
    fixed = 5000.0 + (10.0 / 60.0) * 60.0                  # tooling + setup_usd = 5010
    for r, q in zip(rs, quants):
        assert abs((r["unit_cost"] - floor) * q - fixed) < 1e-3, (q, r["unit_cost"])
    # a very large lot amortizes the fixed cost to ~0: unit_cost lands ON the floor
    huge = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                              tooling_usd=5000.0, quantity=10_000_000, setup_min=10.0)
    assert huge["unit_cost"] > floor                       # approached strictly from above
    assert abs(huge["unit_cost"] - floor) < 1e-3, huge["unit_cost"]


def test_scrap_fraction_is_linear_in_material_cost_only():
    # Exact identity: material_cost = mass·price·(1+scrap_fraction), so scrap enters
    # linearly — scrap=0.1 → material cost ×1.1 — and it touches NOTHING else (process
    # and tooling are untouched). The basic anchor only spot-checks scrap=0.20.
    base = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    s10 = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", scrap_fraction=0.1)
    assert abs(s10["material_cost"] - 1.1 * base["material_cost"]) < 1e-4, s10["material_cost"]
    assert s10["process_cost"] == base["process_cost"]         # scrap is material-only
    assert s10["tooling_amortized"] == base["tooling_amortized"]
    # the (1+f) law holds across the whole fraction, including the f=0 identity
    for f in (0.0, 0.25, 0.5, 1.0):
        rr = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6", scrap_fraction=f)
        assert abs(rr["material_cost"] - (1.0 + f) * base["material_cost"]) < 1e-4, (f, rr)


def test_process_factor_ratio_is_part_independent():
    # Reciprocity/invariant: machine_time_hr = volume_cm3·factor(process), so the RATIO
    # between two processes is the pure factor ratio (cnc/injection = 3e-3/5e-5 = 60,
    # cnc/fdm = 3e-3/8e-4 = 3.75) — independent of volume, quantity, AND material card.
    # The basic anchor only asserts cnc > injection; this pins the exact multiplier.
    def ratios(p1, p2, vol, qty, mat="ABS"):
        a = cost.cost_estimate(volume_mm3=vol, material=mat, process=p1, quantity=qty)
        b = cost.cost_estimate(volume_mm3=vol, material=mat, process=p2, quantity=qty)
        return (a["breakdown"]["machine_time_hr"] / b["breakdown"]["machine_time_hr"],
                a["breakdown"]["machining_cost"] / b["breakdown"]["machining_cost"])

    # cnc/injection ≡ 60 across geometries and lot sizes (and material)
    for vol, qty in ((1e6, 1), (2e6, 50), (5e6, 1000), (8e6, 7)):
        t, c = ratios("cnc", "injection", vol, qty)
        assert abs(t - 60.0) < 1e-9 and abs(c - 60.0) < 1e-9, (vol, qty, t, c)
    assert abs(ratios("cnc", "injection", 1e6, 1, mat="AL6061-T6")[0] - 60.0) < 1e-9
    # a second pair (cnc/fdm ≡ 3.75) is equally part-independent
    for vol, qty in ((1e6, 1), (2e6, 7), (5e6, 250)):
        assert abs(ratios("cnc", "fdm", vol, qty)[0] - 3.75) < 1e-9, (vol, qty)


# --- slicing: solid mass, infill linearity, the ceil boundary, flow-inverse time ---

def test_solid_mass_is_exactly_density_times_volume():
    # Exact identity: at 100% infill mass_g ≡ ρ·V with V in cm³ (mm³·1e-3), and the
    # deposited volume collapses to the solid volume so filament_g ≡ mass_g. Pinned
    # with an explicit ρ to isolate the closed form from the Materials-DB lookup, and
    # cross-checked against the DB value for PLA. Sharper than the basic point anchor:
    # this nails the *identity* mass≡filament≡ρV, not one happy-path number.
    V = 60000.0                                          # 60 cm³
    r = sl.slice_estimate(V, bbox_mm=[50, 40, 30], infill_fraction=1.0, density_g_cc=1.25)
    assert abs(r["mass_g"] - 1.25 * (V * 1e-3)) < 1e-6, r["mass_g"]        # ρ·V = 75.0 g
    assert r["mass_g"] == r["filament_g"], (r["mass_g"], r["filament_g"])  # solid ≡ deposited
    assert abs(r["deposited_volume_mm3"] - V) < 1e-6, r["deposited_volume_mm3"]
    # and the default material really reads ρ from the Materials DB (PLA = 1.24 g/cc)
    rho = mat.numeric(mat.get("PLA"), "density_g_cc")
    db = sl.slice_estimate(V, bbox_mm=[50, 40, 30], material="PLA", infill_fraction=1.0)
    assert abs(db["mass_g"] - rho * (V * 1e-3)) < 1e-3, (db["mass_g"], rho)


def test_deposited_infill_term_is_linear_in_infill_fraction():
    # Scaling law: deposited = V·(wall + infill·(1−wall)), so at a FIXED wall fraction the
    # *infill term* dep(f)−dep(0) ≡ V·(1−wall)·f is exactly linear — doubling the infill
    # doubles that term, the walls cancelling out. Isolates the infill contribution from
    # the always-solid perimeter, which a single-point filament number can't see.
    V, wf = 100000.0, 0.35
    def dep(f):
        return sl.slice_estimate(V, [50, 40, 30], infill_fraction=f,
                                 wall_fraction=wf, density_g_cc=1.25)["deposited_volume_mm3"]
    d0, d25, d50, d100 = dep(0.0), dep(0.25), dep(0.5), dep(1.0)
    assert abs(d0 - V * wf) < 1e-6, d0                            # infill=0 ⇒ walls only
    assert abs((d25 - d0) - V * (1 - wf) * 0.25) < 1e-6, (d25, d0)
    assert abs((d50 - d0) / (d25 - d0) - 2.0) < 1e-9, (d50, d25, d0)   # 2× infill → 2× term
    assert abs((d100 - d0) / (d50 - d0) - 2.0) < 1e-9, (d100, d50, d0)


def test_layer_count_is_sharp_at_the_ceil_boundary():
    # Exact identity: layer_count ≡ ceil(bbox_z / layer_height). At layer_height=1.0 the
    # boundary lives at z=10: z=9.99 → ceil=10 but z=10.01 → ceil=11, so the two MUST
    # differ by one. The exact integer z=10.0 stays at 10 (ceil(10.0)=10, no spurious +1).
    lo = sl.slice_estimate(1000.0, bbox_mm=[10, 10, 9.99], layer_height_mm=1.0)
    hi = sl.slice_estimate(1000.0, bbox_mm=[10, 10, 10.01], layer_height_mm=1.0)
    assert lo["layer_count"] == math.ceil(9.99 / 1.0) == 10, lo["layer_count"]
    assert hi["layer_count"] == math.ceil(10.01 / 1.0) == 11, hi["layer_count"]
    assert hi["layer_count"] - lo["layer_count"] == 1                # the ceil step
    ex = sl.slice_estimate(1000.0, bbox_mm=[10, 10, 10.0], layer_height_mm=1.0)
    assert ex["layer_count"] == 10, ex["layer_count"]               # exact divisor, no +1


def test_print_time_is_inverse_in_speed_and_flow():
    # Scaling law: time = deposited/(nozzle·layer_height·speed)/60, so with everything but
    # speed held fixed, 2× print speed → exactly ½ time (and 2× nozzle the same — flow is
    # linear in both). Holds the deposited volume constant via fixed geometry/infill.
    V = 200000.0
    base = sl.slice_estimate(V, [80, 50, 30], print_speed_mm_s=50.0, density_g_cc=1.25)
    fast = sl.slice_estimate(V, [80, 50, 30], print_speed_mm_s=100.0, density_g_cc=1.25)
    wide = sl.slice_estimate(V, [80, 50, 30], nozzle_mm=0.8, density_g_cc=1.25)
    flow = 0.4 * 0.2 * 50.0                                          # nozzle·layer·speed
    assert abs(base["print_time_min"] - V / flow / 60.0) < 0.05, base["print_time_min"]
    assert abs(base["print_time_min"] / fast["print_time_min"] - 2.0) < 1e-3, \
        (base["print_time_min"], fast["print_time_min"])            # 2× speed → ½ time
    assert abs(base["print_time_min"] / wide["print_time_min"] - 2.0) < 1e-3, \
        (base["print_time_min"], wide["print_time_min"])            # 2× nozzle → ½ time


# --- kinematics: the closed-form mobility/stroke limits ------------------------

def test_slider_crank_stroke_is_independent_of_conrod_length():
    # Exact identity: for the in-line slider-crank the peak-to-peak stroke is 2R *exactly*,
    # so two conrods L=50 and L=200 (same crank, e=0) give an IDENTICAL stroke even though
    # their absolute TDC/BDC positions differ by the conrod length. The basic test pins
    # stroke==2R for one chain; this is the sharper "independence of L" sibling.
    R = 15.0
    short = kinematics.slider_crank(crank_mm=R, conrod_mm=50.0)
    longc = kinematics.slider_crank(crank_mm=R, conrod_mm=200.0)
    assert short["stroke_mm"] == longc["stroke_mm"], (short["stroke_mm"], longc["stroke_mm"])
    assert short["stroke_mm"] == 2.0 * R, short["stroke_mm"]          # exact, e=0
    # independence holds right down to the validity edge L > R+|e| (here L just > R)
    edge = kinematics.slider_crank(crank_mm=R, conrod_mm=R + 0.001)
    assert edge["stroke_mm"] == short["stroke_mm"], edge["stroke_mm"]
    # the absolute piston positions, by contrast, DO ride on L (sanity: it is the
    # stroke that is invariant, not the geometry)
    assert short["x_tdc_mm"] != longc["x_tdc_mm"], (short["x_tdc_mm"], longc["x_tdc_mm"])
    assert short["inline_stroke_exact"] is True and longc["inline_stroke_exact"] is True


def test_offset_slider_crank_stroke_depends_on_conrod_and_decays_to_2r():
    # Asymptotic envelope (the boundary of the identity above): once the wrist is offset
    # (e≠0) the L-independence BREAKS — the stroke exceeds 2R and shrinks monotonically
    # toward 2R as the conrod lengthens. A shorter conrod gives the larger stroke, and
    # inline_stroke_exact is False throughout.
    R, e = 15.0, 5.0
    short = kinematics.slider_crank(crank_mm=R, conrod_mm=40.0, wrist_offset_mm=e)
    longc = kinematics.slider_crank(crank_mm=R, conrod_mm=300.0, wrist_offset_mm=e)
    assert short["stroke_mm"] > 2.0 * R and longc["stroke_mm"] > 2.0 * R, (short, longc)
    assert short["stroke_mm"] > longc["stroke_mm"], "shorter conrod -> larger offset stroke"
    assert short["inline_stroke_exact"] is False and longc["inline_stroke_exact"] is False


def test_grashof_change_point_is_the_knife_edge_between_grashof_and_non_grashof():
    # Exact identity at the boundary: the change-point is S+L == P+Q to within the code's
    # 1e-9 guard. Straddling that boundary by a nudge a million-fold larger than the guard
    # flips the classification grashof -> change_point -> non_grashof. The basic test only
    # pins one set sitting ON the boundary; this is the sharper straddle (a knife-edge).
    on = kinematics.grashof_classify(crank=2, coupler=5, rocker=5, ground=8.0)   # S+L=10=P+Q
    assert on["condition"] == "change_point" and on["type"] == "change-point", on
    assert on["input_crank_fully_rotates"] is False, on                          # not Grashof
    below = kinematics.grashof_classify(crank=2, coupler=5, rocker=5, ground=7.99)
    above = kinematics.grashof_classify(crank=2, coupler=5, rocker=5, ground=8.01)
    assert below["condition"] == "grashof", below                                # S+L < P+Q
    assert above["condition"] == "non_grashof", above                            # S+L > P+Q
    # the boundary is shortest-link agnostic: ground-shortest on the boundary is
    # still a change-point (same lengths, permuted onto a different shortest link)
    gnd = kinematics.grashof_classify(crank=5, coupler=5, rocker=8, ground=2)     # S+L=10=P+Q
    assert gnd["condition"] == "change_point" and gnd["shortest"] == "ground", gnd


def test_gruebler_single_loop_dof_is_n_minus_3_and_goes_negative_when_overconstrained():
    # Exact identity + sequence: for an n-link single-loop chain of n revolutes the planar
    # Grübler DOF is exactly n−3, an arithmetic progression (triangle 0, four-bar 1,
    # five-bar 2, six-bar 3, ...). Past the kinematic chain it goes negative — a
    # redundantly-constrained (statically indeterminate) structure. The basic test pins
    # the 4-bar/5-bar/triangle points; this pins the recurrence and the negative tail.
    for n in range(3, 8):
        assert kinematics.gruebler_dof(n, ["revolute"] * n) == n - 3, n
    assert kinematics.gruebler_dof(4, ["revolute"] * 5) == -1            # over-constrained
    assert kinematics.gruebler_dof(5, ["revolute"] * 7) == -2
    # each added lower pair removes exactly 2 DOF; each added higher pair exactly 1
    five = kinematics.gruebler_dof(5, ["revolute"] * 5)                  # DOF 2
    assert five - kinematics.gruebler_dof(5, ["revolute"] * 6) == 2      # +1 lower pair
    fourbar = kinematics.gruebler_dof(4, ["revolute"] * 4)               # DOF 1
    assert fourbar - kinematics.gruebler_dof(4, ["revolute"] * 4 + ["gear"]) == 1  # +1 higher pair


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

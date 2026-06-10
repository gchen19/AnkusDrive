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
from driftpin.analysis import durability as du  # noqa: E402
from driftpin.analysis import em                # noqa: E402
from driftpin.analysis import materials as mat  # noqa: E402
from driftpin.analysis import optics as op      # noqa: E402
from driftpin.analysis import thermal as th     # noqa: E402
from driftpin.analysis import tolerance as tol  # noqa: E402

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

"""Materials DB toys — two-sided oracles for driftpin.analysis.materials.

Pure-Python, no FreeCAD, no numpy: imports the analysis module directly and
checks it against known answers. Every test is two-sided — the right answer
passes AND a deliberately-wrong input is caught — mirroring tests/TOYS.md.

Covers the §2 toys in docs/SIMULATION_EXAMPLES.md:
  - round-trip: property strings parse to known values; output is a valid FEM card
  - ranking: specific-strength / cost orderings are the known monotone ones
  - empty filter returns nothing (not the closest miss)
  - unknown name raises with a suggestion

Run:  python3 tests/test_materials.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import materials  # noqa: E402


def test_parse_quantity():
    assert materials.parse_quantity("68900 MPa") == (68900.0, "MPa")
    assert materials.parse_quantity("2700 kg/m^3") == (2700.0, "kg/m^3")
    assert materials.parse_quantity("0.33") == (0.33, "")
    v, u = materials.parse_quantity("23.6e-6 1/K")
    assert abs(v - 23.6e-6) < 1e-12 and u == "1/K"
    # negative: garbage must raise, not silently return 0
    try:
        materials.parse_quantity("not-a-number MPa")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on unparseable quantity")


def test_get_roundtrip_and_aliasing():
    c = materials.get("AL6061-T6")
    e, _ = materials.parse_quantity(c["YoungsModulus"])
    rho, _ = materials.parse_quantity(c["Density"])
    assert abs(e - 68900) / 68900 < 0.01, e
    assert abs(rho - 2700) / 2700 < 0.01, rho
    # name normalisation: hyphen/space/case insensitive
    assert materials.get("al 6061 t6")["name"] == "AL6061-T6"


def test_output_is_a_valid_fem_card():
    """The integration claim: material_get output feeds fem_set_material as-is."""
    fem = materials.to_fem_material(materials.get("Steel-A36"))
    assert set(("YoungsModulus", "PoissonRatio", "Density", "Name")) <= set(fem)
    # values are still quantity strings in the FEM convention
    assert fem["YoungsModulus"].endswith("MPa")
    assert fem["Density"].endswith("kg/m^3")
    assert fem["Name"] == "Steel-A36"


def test_unknown_name_suggests():
    try:
        materials.get("AL6061")  # partial / wrong
    except materials.MaterialNotFound as e:
        assert "did_you_mean" in str(e)
    else:
        raise AssertionError("expected MaterialNotFound")


def test_rank_specific_strength_known_order():
    """7075-T6 (high strength, ~same density) must outrank 6061-T6, which must
    outrank pure 1100-O — a known specific-strength ordering."""
    res = materials.select(criteria={"max_density_g_cc": 3.0},
                           rank_by="specific_strength")
    order = [c["name"] for c in res["candidates"]]
    assert order.index("AL7075-T6") < order.index("AL6061-T6") < order.index("AL1100-O"), order
    # scores must be strictly descending (it's the sort key)
    scores = [c["score"] for c in res["candidates"]]
    assert scores == sorted(scores, reverse=True), scores


def test_rank_cost_inverts_toward_commodity():
    """Ranking by cost (cheapest first) must put commodity steel ahead of Ti."""
    res = materials.select(rank_by="cost")
    order = [c["name"] for c in res["candidates"]]
    assert order.index("Steel-A36") < order.index("Ti-6Al-4V"), order


def test_filter_is_a_hard_gate():
    """A min-yield filter must EXCLUDE everything below it, and an impossible
    filter must return an empty set — not the closest miss."""
    res = materials.select(criteria={"min_yield_mpa": 500})
    names = [c["name"] for c in res["candidates"]]
    assert "AL6061-T6" not in names  # 276 MPa < 500, excluded
    assert all(c["yield_mpa"] >= 500 for c in res["candidates"]), names
    impossible = materials.select(criteria={"min_yield_mpa": 99999})
    assert impossible["count"] == 0 and impossible["candidates"] == []


def test_list_by_category():
    al = materials.list_materials(category="aluminum")
    assert al["count"] >= 3
    assert {m["name"] for m in al["materials"]} >= {"AL6061-T6", "AL7075-T6", "AL1100-O"}
    assert all(m["category"] == "aluminum" for m in al["materials"])


def test_optical_property_present():
    """Optical members carry refractive_index for the optics family / corpus."""
    assert abs(materials.numeric(materials.get("PMMA"), "refractive_index") - 1.49) < 0.01
    assert abs(materials.numeric(materials.get("N-BK7"), "refractive_index") - 1.5168) < 1e-6


def test_vendor_optical_merge_preserves_mechanical():
    """The vendor optical layer (optical.json) augments seed cards field-wise:
    N-BK7 keeps its hand-authored Young's modulus AND gains vendor n_d/Abbe."""
    bk7 = materials.get("N-BK7")
    assert "YoungsModulus" in bk7  # from seed.json
    assert bk7.get("optical_source", "").startswith("refractiveindex.info-database")
    assert bk7.get("abbe_number") == "64.17"
    # vendor-only optical materials are present too
    names = {m["name"] for m in materials.list_materials("glass")["materials"]}
    assert {"N-SF11", "F2", "Fused-Silica"} <= names


def test_refractive_index_dispersion():
    """refractive_index_at() reproduces known n via the Sellmeier formula.
    N-BK7 n_d=1.5168; dispersion gives higher n in the blue (486 nm) than red."""
    bk7 = materials.get("N-BK7")
    nd = materials.refractive_index_at(bk7, 587.56)
    nF = materials.refractive_index_at(bk7, 486.13)  # F-line (blue)
    nC = materials.refractive_index_at(bk7, 656.27)  # C-line (red)
    assert abs(nd - 1.5168) < 5e-4, nd
    assert nF > nd > nC, (nF, nd, nC)  # normal dispersion
    # Abbe number reconstructed from the three lines matches the catalog ~64
    abbe = (nd - 1.0) / (nF - nC)
    assert abs(abbe - 64.17) < 1.0, abbe
    # out-of-range wavelength is rejected, not silently extrapolated
    try:
        materials.refractive_index_at(bk7, 100.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError outside fitted range")


def test_bad_criterion_raises():
    for bad in ({"yield_mpa": 100}, {"min_unknownprop": 1}):
        try:
            materials.select(criteria=bad)
        except KeyError:
            pass
        else:
            raise AssertionError(f"expected KeyError for {bad}")


# --- process / rheology corpus (injection molding, issue #106) ----------------

# Resins that must carry the screen-tier process layer.
_MOLDING_RESINS = ["ABS", "PC", "Nylon-6/6", "POM", "PP", "HDPE", "LDPE", "PS",
                   "PMMA", "PEEK"]
_AMORPHOUS = {"ABS", "PC", "PMMA", "PS"}
_SEMICRYSTALLINE = {"PP", "Nylon-6/6", "POM", "HDPE", "LDPE", "PEEK"}


def test_range_parsing():
    assert materials.parse_range("0.8 3.5") == (0.8, 3.5)
    assert materials.parse_range("1.8") == (1.8, 1.8)  # degenerate
    for bad in ("not a range", "5 1"):  # unparseable / lo>hi typo
        try:
            materials.parse_range(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError on {bad!r}")


def test_process_fields_present_and_cited():
    """Every molding resin carries the screen-tier process layer with provenance."""
    for name in _MOLDING_RESINS:
        c = materials.get(name)
        for f in ("crystallinity", "recommended_wall_mm", "mold_shrinkage_pct",
                  "melt_temp_c", "mold_temp_c", "eject_temp_c"):
            assert f in c, f"{name} missing {f}"
        assert c.get("process_source"), f"{name} missing process_source"
        assert c.get("process_basis") in (
            "typical", "nominal", "min", "max", "A-basis", "B-basis"), name
        # every new value must parse
        assert materials.numeric(c, "recommended_wall_min_mm") is not None, name
        assert materials.numeric(c, "mold_shrinkage_min_pct") is not None, name
        assert materials.numeric(c, "melt_temp_c") is not None, name


def test_process_sanity_bounds():
    for name in _MOLDING_RESINS:
        c = materials.get(name)
        wmin = materials.numeric(c, "recommended_wall_min_mm")
        wmax = materials.numeric(c, "recommended_wall_max_mm")
        assert 0.2 <= wmin <= wmax <= 12.0, (name, wmin, wmax)
        smin = materials.numeric(c, "mold_shrinkage_min_pct")
        smax = materials.numeric(c, "mold_shrinkage_max_pct")
        assert 0.0 < smin <= smax < 5.0, (name, smin, smax)
        assert c["crystallinity"] in ("amorphous", "semicrystalline"), name
        tmold = materials.numeric(c, "mold_temp_c")
        teject = materials.numeric(c, "eject_temp_c")
        tmelt = materials.numeric(c, "melt_temp_c")
        # the same ordering molding_screen enforces: mold < eject < melt
        assert tmold < teject < tmelt, (name, tmold, teject, tmelt)


def test_crystallinity_classification():
    for name in _AMORPHOUS:
        assert materials.get(name)["crystallinity"] == "amorphous", name
    for name in _SEMICRYSTALLINE:
        assert materials.get(name)["crystallinity"] == "semicrystalline", name


def test_amorphous_shrink_less_than_semicrystalline():
    """Cross-consistency (#104's model_underpredicts key): amorphous resins shrink
    less than semicrystalline ones. Compare the max of each amorphous resin against
    the min of each semicrystalline resin — the bands must not invert."""
    amax = max(materials.numeric(materials.get(n), "mold_shrinkage_max_pct")
               for n in _AMORPHOUS)
    smin = min(materials.numeric(materials.get(n), "mold_shrinkage_min_pct")
               for n in _SEMICRYSTALLINE)
    assert amax <= smin, (amax, smin)
    # and on a per-card basis the amorphous mean stays below the semicrystalline mean
    def mean_shrink(n):
        c = materials.get(n)
        return 0.5 * (materials.numeric(c, "mold_shrinkage_min_pct")
                      + materials.numeric(c, "mold_shrinkage_max_pct"))
    amean = sum(map(mean_shrink, _AMORPHOUS)) / len(_AMORPHOUS)
    smean = sum(map(mean_shrink, _SEMICRYSTALLINE)) / len(_SEMICRYSTALLINE)
    assert amean < smean, (amean, smean)


def test_golden_shrinkage_anchors():
    """Published linear shrinkage ranges for the headline resins."""
    def band(n):
        c = materials.get(n)
        return (materials.numeric(c, "mold_shrinkage_min_pct"),
                materials.numeric(c, "mold_shrinkage_max_pct"))
    assert band("ABS") == (0.4, 0.7), band("ABS")
    assert band("PP") == (1.0, 2.5), band("PP")
    assert band("POM") == (1.8, 2.5), band("POM")


def test_molding_screen_temps_match_cards():
    """The card melt/mold/eject temps are the source of truth; molding_screen's
    _POLYMERS mirror them (consolidation check, issue #106)."""
    from driftpin.analysis import molding
    name_map = {"ABS": "ABS", "PP": "PP", "PC": "PC", "PA66": "Nylon-6/6",
                "POM": "POM", "HDPE": "HDPE", "PS": "PS"}
    for poly, card_name in name_map.items():
        t_melt, t_mold, t_eject = molding._POLYMERS[poly][:3]
        c = materials.get(card_name)
        assert materials.numeric(c, "melt_temp_c") == t_melt, (poly, t_melt)
        assert materials.numeric(c, "mold_temp_c") == t_mold, (poly, t_mold)
        assert materials.numeric(c, "eject_temp_c") == t_eject, (poly, t_eject)


def test_solver_tier_cross_wlf_and_tait():
    """At least one fully-specified resin for the openInjMoldSim solver (#105):
    Cross-WLF (n, tau*, D1, D2, A1, A2) + Tait PVT, every value cited, and within
    physical magnitude bands (unit/typo guardrail)."""
    have = [n for n in _MOLDING_RESINS
            if "cross_wlf" in materials.get(n) and "tait_pvt" in materials.get(n)]
    assert len(have) >= 1, have
    for name in have:
        c = materials.get(name)
        cw = c["cross_wlf"]
        for k in ("n", "tau_star_pa", "D1_pa_s", "D2_k", "A1", "A2_k"):
            assert k in cw, (name, k)
        assert cw.get("source") and cw.get("basis"), name
        n = float(cw["n"]); tau = float(cw["tau_star_pa"])
        d1 = float(cw["D1_pa_s"]); d2 = float(cw["D2_k"]); a1 = float(cw["A1"])
        assert 0.1 < n < 1.0, (name, n)              # power-law index
        assert 1e3 < tau < 1e7, (name, tau)         # Pa
        assert 1e8 < d1 < 1e20, (name, d1)          # Pa.s zero-shear ref
        assert 100.0 < d2 < 600.0, (name, d2)       # reference temp ~ Tg/Tm, K
        assert 5.0 < a1 < 60.0, (name, a1)          # WLF A1
        tt = c["tait_pvt"]
        assert tt.get("source") and tt.get("basis"), name
        for k in ("b1m_m3_kg", "b3m_pa", "b5_k"):
            assert k in tt, (name, k)
        assert 5e-4 < float(tt["b1m_m3_kg"]) < 2e-3, name   # specific volume m^3/kg
        assert 1e7 < float(tt["b3m_pa"]) < 1e9, name        # pressure sensitivity Pa
        assert 200.0 < float(tt["b5_k"]) < 600.0, name      # transition temp K


# --- expanded mechanical corpus (issue #99) -----------------------------------
# FCMat first-class + provenance/basis on every card + handbook mechanical
# fields. These gates run over the WHOLE shipped corpus (seed + vendored FCMat
# + optical), so they catch unit/typo errors anywhere in the database.

import json as _json  # noqa: E402

# basis vocabulary: the issue's design-basis enum, plus "vendor" for the
# CC0/LGPL vendor-extracted layers (optical.json) whose values are as-published.
_ALLOWED_BASIS = {"typical", "nominal", "min", "max", "A-basis", "B-basis", "vendor"}


def _all_cards():
    return materials._load_corpus()


def _seed_names():
    seed = _json.loads(materials._SEED_PATH.read_text())["materials"]
    return [m["name"] for m in seed]


def _num(card, key):
    try:
        return materials.numeric(card, key)
    except Exception:
        return None


def test_every_card_carries_source_and_basis():
    """Provenance gate: every shipped card surfaces a non-empty `source`
    citation and a `basis` drawn from the allowed vocabulary."""
    bad_src, bad_basis = [], []
    for name, card in _all_cards().items():
        if not str(card.get("source", "")).strip():
            bad_src.append(name)
        if card.get("basis") not in _ALLOWED_BASIS:
            bad_basis.append((name, card.get("basis")))
    assert not bad_src, f"cards missing source: {bad_src}"
    assert not bad_basis, f"cards with bad basis: {bad_basis}"


def test_get_surfaces_source_and_basis():
    """get() return dicts expose both fields so an oracle can cite its inputs."""
    c = materials.get("AL6061-T6")
    assert c["basis"] == "typical"
    assert "MMPDS" in c["source"] or "ASM" in c["source"], c["source"]


def test_sanity_bounds_full_corpus():
    """Unit-bug guardrail across the whole corpus: catch a stray 1000x / typo.
    yield<=UTS is checked strictly only on metals/composites (ductile polymers
    legitimately show an upper-yield above the necked break stress), but a loose
    band still flags gross errors everywhere."""
    errs = []
    for name, card in _all_cards().items():
        E = _num(card, "youngs_gpa")
        if E is not None and not (0 < E < 1500):
            errs.append((name, "E_GPa", E))
        rho = _num(card, "density_kg_m3")
        if rho is not None and not (10 <= rho <= 25000):
            errs.append((name, "density", rho))
        nu = _num(card, "poisson")
        if nu is not None and not (0 < nu < 0.5):
            errs.append((name, "poisson", nu))
        y, u = _num(card, "yield_mpa"), _num(card, "uts_mpa")
        if y is not None and not (0 < y < 1e5):
            errs.append((name, "yield", y))
        if u is not None and not (0 < u < 1e5):
            errs.append((name, "uts", u))
        # NB: yield<=UTS is enforced strictly only on authored cards
        # (test_authored_cards_yield_le_uts); some vendored ductile-polymer
        # FCMat cards list an upper-yield above the necked break stress.
        hb, hv = _num(card, "hardness_hb"), _num(card, "hardness_hv")
        if hb is not None and not (0 < hb < 1200):
            errs.append((name, "HB", hb))
        if hv is not None and not (0 < hv < 1200):
            errs.append((name, "HV", hv))
        el = _num(card, "elongation_pct")
        if el is not None and not (0 <= el <= 100):
            errs.append((name, "elong", el))
    assert not errs, f"sanity-bound violations: {errs}"


def test_authored_cards_yield_le_uts():
    """Stricter invariant on the hand-authored seed cards: 0 < yield <= UTS."""
    bad = []
    for name in _seed_names():
        c = materials.get(name)
        y, u = _num(c, "yield_mpa"), _num(c, "uts_mpa")
        if y is not None and u is not None and not (0 < y <= u * 1.001):
            bad.append((name, y, u))
    assert not bad, f"authored cards violating 0<yield<=UTS: {bad}"


def test_fcmat_vendored_first_class():
    """FreeCAD's FCMat library is vendored into a shipped JSON, so the ~100+
    cards are present WITHOUT a runtime FreeCAD path."""
    assert materials._FCMAT_PATH.is_file(), "fcmat.json not shipped"
    payload = _json.loads(materials._FCMAT_PATH.read_text())
    assert payload["materials"], "fcmat.json has no cards"
    assert len(payload["materials"]) >= 100, len(payload["materials"])
    corpus = _all_cards()
    # vendored FCMat-only cards (no seed counterpart) keep FreeCAD's name+source
    for nm in ("Steel-Generic", "CalculiX-Steel"):
        assert nm in corpus, nm
        assert corpus[nm]["source"].startswith("FreeCAD FCMat"), nm
    # a vendored card that DOES duplicate a seed material is absorbed (de-duped):
    # 'Aluminum 6061-T6' is no longer its own key, but still resolves via alias
    assert "Aluminum 6061-T6" not in corpus
    assert materials.get("Aluminum 6061-T6")["name"] == "AL6061-T6"
    # attribution header present (LGPL/CC-BY vendoring discipline)
    assert "FreeCAD" in payload.get("attribution", "")


def test_seed_wins_over_fcmat_on_name_clash():
    """When a seed card and an FCMat card share a name, the curated seed wins
    but may inherit FCMat fields it omits (field-wise merge)."""
    abs_card = materials.get("ABS")
    # seed value (40/40), not FreeCAD's 44.1/38.8
    assert _num(abs_card, "yield_mpa") == 40.0, abs_card
    assert abs_card["source"].startswith("ASM"), abs_card["source"]


def test_golden_mechanical_anchors():
    """Known handbook values within tolerance — the calibration anchors."""
    assert abs(_num(materials.get("Steel-A36"), "youngs_gpa") - 200) < 10
    assert abs(_num(materials.get("AL6061-T6"), "yield_mpa") - 276) < 5
    assert abs(_num(materials.get("Ti-6Al-4V"), "yield_mpa") - 880) < 10
    # vendored FCMat anchor agrees independently
    assert abs(_num(materials.get("Aluminum 6061-T6"), "yield_mpa") - 276) < 5


def test_specific_strength_cfrp_ti_above_mild_steel():
    """Ashby ranking sanity: by specific strength, CFRP and Ti-6Al-4V both
    outrank mild steel (A36) — the headline materials-selection result."""
    res = materials.select(criteria={"min_yield_mpa": 100},
                           rank_by="specific_strength")
    order = [c["name"] for c in res["candidates"]]
    for hi in ("CFRP-Carbon-Epoxy-UD", "Ti-6Al-4V"):
        assert hi in order, hi
        assert order.index(hi) < order.index("Steel-A36"), (hi, order[:8])
    # scores strictly descending (the sort key)
    scores = [c["score"] for c in res["candidates"]]
    assert scores == sorted(scores, reverse=True)


def test_hardness_and_elongation_accessors():
    """New _NUMERIC accessors resolve handbook hardness/elongation fields."""
    assert abs(_num(materials.get("Ti-6Al-4V"), "hardness_hb") - 334) < 5
    assert abs(_num(materials.get("Steel-1045"), "hardness_hv") - 180) < 5
    assert abs(_num(materials.get("AL6061-T6"), "elongation_pct") - 12) < 1
    # a card without the field returns None, not a crash
    assert _num(materials.get("AL1100-O"), "hardness_hv") is None


def test_new_handbook_families_present_and_cited():
    """The curated families this issue adds are present, cited, and carry the
    mechanical fields that motivated them (fatigue / fracture / hardness)."""
    expect = {
        "AL2024-T3": "aluminum",
        "CastIron-GrayClass40": "cast_iron",
        "CastIron-Ductile-65-45-12": "cast_iron",
        "Brass-C36000": "copper",
        "Bronze-C93200": "copper",
        "CFRP-Carbon-Epoxy-UD": "composite",
        "GFRP-EGlass-Epoxy-UD": "composite",
    }
    for name, cat in expect.items():
        c = materials.get(name)
        assert c["category"] == cat, (name, c["category"])
        assert str(c.get("source", "")).strip(), name
        assert c.get("basis") in _ALLOWED_BASIS, name
        assert _num(c, "density_kg_m3") is not None, name
        assert _num(c, "fatigue_mpa") is not None, name
        assert _num(c, "hardness_hb") is not None or name.startswith(("CFRP", "GFRP")), name
    # specific golden: 2024-T3 yield ~345 MPa; ductile iron elongation ~12%
    assert abs(_num(materials.get("AL2024-T3"), "yield_mpa") - 345) < 5
    assert abs(_num(materials.get("CastIron-Ductile-65-45-12"), "elongation_pct") - 12) < 1


def test_no_duplicate_materials():
    """No two cards may describe the same physical material. Each card claims an
    identity key-set (its normalized name + aliases); the de-dup at load is
    correct iff every key is owned by exactly one card. This is the regression
    guard for the 'AL6061-T6 vs Aluminum 6061-T6' class of duplicate — adding a
    new card that collides with an existing one (or its alias) fails here until
    it's merged/aliased."""
    corpus = _all_cards()
    owner = {}
    for name, card in corpus.items():
        for key in materials._claimed_keys(card):
            assert key not in owner, (
                f"duplicate material: {name!r} and {owner[key]!r} both claim {key!r}")
            owner[key] = name


def test_known_fcmat_duplicates_absorbed():
    """The specific seed/FCMat collisions found in the audit are merged into one
    canonical card, not left as two — for metals AND polymers."""
    corpus = _all_cards()
    # (canonical seed name, the FCMat duplicate name that must be absorbed)
    for canonical, fcmat_dup in [
        ("AL6061-T6", "Aluminum 6061-T6"),
        ("AL7075-T6", "Aluminum 7075-T6"),
        ("Ti-6Al-4V", "Ti-6Al-4V (Grade 5)"),
        ("PC", "Polycarbonate"),
        ("PP", "Polypropylene"),
        ("PS", "Polystyrene"),
    ]:
        assert fcmat_dup not in corpus, f"{fcmat_dup!r} should be absorbed into {canonical!r}"
        assert canonical in corpus, canonical
        # the dup name still resolves — to the canonical card
        assert materials.get(fcmat_dup)["name"] == canonical


def test_aliases_resolve_to_canonical_card():
    """A registered alias resolves to its canonical card (the aliases were dead
    metadata before the de-dup wiring)."""
    for alias, canonical in [
        ("Delrin", "POM"), ("Acetal", "POM"),
        ("PA66", "Nylon-6/6"), ("Polycarbonate", "PC"),
        ("Aluminum 6061-T6", "AL6061-T6"),
        ("Gray-Cast-Iron", "CastIron-GrayClass40"),
        ("Ti-6Al-4V (Grade 5)", "Ti-6Al-4V"),
    ]:
        assert materials.get(alias)["name"] == canonical, alias


def test_dedup_preserves_seed_values_and_inherits_fcmat_fields():
    """When a seed card absorbs its FCMat twin, seed values win on conflict but
    the merged card may inherit FCMat-only fields — the field-wise merge."""
    al = materials.get("AL6061-T6")
    # seed value wins (276 MPa), not whatever FreeCAD's 6061 card lists
    assert abs(_num(al, "yield_mpa") - 276) < 5, al
    assert al["source"].startswith("ASM"), al["source"]
    # selection no longer returns the duplicate alongside the canonical card
    names = [c["name"] for c in materials.select({}, rank_by="specific_strength")["candidates"]]
    assert names.count("AL6061-T6") <= 1
    assert "Aluminum 6061-T6" not in names


# --- runner (mirrors tests/test_contracts.py) ---------------------------------

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

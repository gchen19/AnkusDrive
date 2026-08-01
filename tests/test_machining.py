"""CNC machinability + machining time (issue #231) — pure-core unit tests.

No FreeCAD: :mod:`driftpin.analysis.machining` takes descriptors (which directions
reach which face, corner radii, wall samples, a volume and a bounding box), so the
whole screen and the whole time model unit-test on the host interpreter. The live
geometry that produces those descriptors is exercised separately in
tests/test_machining_worker.py.

What these prove:

  * the setup census is RIGHT and REPRODUCIBLE — a prismatic block quotes one setup,
    the same block with a 5-axis-only pocket flags an undercut, and the greedy cover
    never depends on descriptor arrival order;
  * the time model is MONOTONE where physics says it must be — harder material,
    tighter tolerance class, more removed volume and more setups each strictly
    increase the time, and none of them can decrease it;
  * it lands in the right PLACE — two reference parts sized the way a job shop
    quotes them come out inside the declared band;
  * the band is DERIVED — ±50 % from the stated MRR and utilisation scatter, and it
    tightens to ±30 % exactly when the dominant unknown is measured;
  * ``cost_estimate(process='cnc')`` fed the new time still lands inside its own
    declared band on the existing toys.

Run: python3 tests/test_machining.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin.analysis import cost, machining as mc  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _ok(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _raises(label, fn, exc=ValueError):
    try:
        fn()
    except exc:
        _ok(label, True)
    else:
        _ok(label, False, f"expected {exc.__name__}")


# --- fixtures -----------------------------------------------------------------
#
# A prismatic block with a top pocket: six billet faces plus a pocket floor and four
# pocket walls. The walls' normals are horizontal, so a +z tool addresses them with
# its periphery — which is why the whole feature is ONE setup, and why the census
# tests reachability rather than normal-vs-direction alone.

def _block_with_top_pocket():
    faces = [{"name": f"Stock{i}", "reachable": [], "on_stock": True,
              "area_mm2": 1000.0} for i in range(6)]
    faces.append({"name": "PocketFloor", "reachable": ["+z"], "on_stock": False,
                  "area_mm2": 400.0})
    for i, d in enumerate(("+x", "-x", "+y", "-y")):
        faces.append({"name": f"PocketWall{i}", "reachable": ["+z", d],
                      "on_stock": False, "area_mm2": 120.0})
    return faces


def _block_with_undercut_cavity():
    """The same block, plus an internal cavity WIDER than its opening — a classic
    5-axis/EDM undercut. The cavity ceiling faces downward into a blind pocket, so
    no principal approach both addresses and reaches them."""
    faces = _block_with_top_pocket()
    faces.append({"name": "CavityCeiling", "reachable": [], "on_stock": False,
                  "area_mm2": 300.0})
    faces.append({"name": "CavityFloor", "reachable": ["+z"], "on_stock": False,
                  "area_mm2": 900.0})
    return faces


# --- the setup census ---------------------------------------------------------

def test_a_prismatic_block_is_a_one_setup_part():
    print("test_a_prismatic_block_is_a_one_setup_part")
    # The must-PASS half of the issue's two-sided machinability gate.
    res = mc.machinability_screen(_block_with_top_pocket())
    _check("one setup", res["setups"], 1)
    _check("from +z", res["setup_directions"], ["+z"])
    _check("no findings", res["findings"], [])
    _check("passes", res["pass"], True)
    _check("perfect score", res["score"], 1.0)
    _check("the billet faces are counted separately", res["stock_faces"], 6)
    _check("and excluded from the machined area", res["machined_area_mm2"], 880.0)


def test_a_five_axis_only_undercut_flags_the_same_block():
    print("test_a_five_axis_only_undercut_flags_the_same_block")
    # The must-FAIL half: identical part apart from the undercut.
    res = mc.machinability_screen(_block_with_undercut_cavity())
    _check("fails", res["pass"], False)
    _check("exactly one finding", len(res["findings"]), 1)
    _check("and it is an undercut", res["findings"][0]["code"], "undercut")
    _check("naming the face", res["undercut_faces"], ["CavityCeiling"])
    _ok("the detail tells the agent what to do about it",
        "5-axis" in res["findings"][0]["detail"], res["findings"][0]["detail"])
    _ok("the score drops below the clean block's", res["score"] < 1.0)
    # everything else about the part is still one setup — the undercut is isolated,
    # not smeared across the whole screen
    _check("the reachable features still cover from one direction",
           res["setup_directions"], ["+z"])


def test_a_plain_billet_still_quotes_one_setup_not_zero():
    print("test_a_plain_billet_still_quotes_one_setup_not_zero")
    # A part with no machined faces at all: the billet still has to be held and
    # faced, so zero setups would be a lie the cost rollup would then repeat.
    res = mc.machinability_screen(
        [{"name": f"S{i}", "on_stock": True, "reachable": [], "area_mm2": 100.0}
         for i in range(6)])
    _check("one setup", res["setups"], 1)
    _check("no machined faces", res["machined_faces"], 0)
    _check("no findings", res["findings"], [])


def test_setup_cover_is_order_independent():
    print("test_setup_cover_is_order_independent")
    # Reproducibility is load-bearing: the same part must never quote two setups on
    # one run and three on the next because a face list came back in another order.
    faces = _block_with_undercut_cavity()
    a = mc.setup_cover(faces)
    b = mc.setup_cover(list(reversed(faces)))
    _check("same setup count", a["setups"], b["setups"])
    _check("same directions", a["directions"], b["directions"])
    _check("same unreachable set", sorted(a["unreachable"]),
           sorted(b["unreachable"]))


def test_features_on_opposite_faces_need_two_setups():
    print("test_features_on_opposite_faces_need_two_setups")
    faces = [{"name": "Top", "reachable": ["+z"], "on_stock": False,
              "area_mm2": 100.0},
             {"name": "Bottom", "reachable": ["-z"], "on_stock": False,
              "area_mm2": 100.0}]
    res = mc.machinability_screen(faces)
    _check("two setups", res["setups"], 2)
    _check("no warning at two", res["warnings"], [])
    # four opposed features: past the threshold, so a warning — but NOT a failure,
    # because a four-setup part is expensive, not unmakeable.
    faces += [{"name": "Left", "reachable": ["-x"], "on_stock": False,
               "area_mm2": 100.0},
              {"name": "Right", "reachable": ["+x"], "on_stock": False,
               "area_mm2": 100.0}]
    res = mc.machinability_screen(faces)
    _check("four setups", res["setups"], 4)
    _check("warned", [w["code"] for w in res["warnings"]], ["many_setups"])
    _check("but still passes", res["pass"], True)


# --- tooling findings ---------------------------------------------------------

def test_deep_pocket_is_flagged_by_tool_l_over_d():
    print("test_deep_pocket_is_flagged_by_tool_l_over_d")
    faces = _block_with_top_pocket()
    shallow = mc.machinability_screen(
        faces, internal_radii=[{"name": "R1", "radius_mm": 5.0, "depth_mm": 40.0}])
    _check("L/D 4 is ordinary work", shallow["pass"], True)
    _check("and is reported", shallow["max_l_over_d_seen"], 4.0)
    deep = mc.machinability_screen(
        faces, internal_radii=[{"name": "R1", "radius_mm": 2.0, "depth_mm": 40.0}])
    _check("L/D 10 fails", deep["pass"], False)
    _check("with the right code", [f["code"] for f in deep["findings"]],
           ["deep_pocket"])
    _check("L/D reported", deep["max_l_over_d_seen"], 10.0)


def test_a_sharp_internal_corner_is_unmakeable():
    print("test_a_sharp_internal_corner_is_unmakeable")
    # radius 0 is how the worker reports a square internal corner. No rotating tool
    # produces one, so it must fail rather than pass as "a very small radius".
    res = mc.machinability_screen(
        _block_with_top_pocket(),
        internal_radii=[{"name": "Edge7", "radius_mm": 0.0, "depth_mm": 10.0}])
    _check("fails", res["pass"], False)
    _check("as a sharp corner", [f["code"] for f in res["findings"]],
           ["sharp_internal_corner"])
    _ok("and it says what fillet to add",
        "R0.5" in res["findings"][0]["detail"], res["findings"][0]["detail"])
    _check("min internal radius is reported as zero",
           res["min_internal_radius_mm"], 0.0)


def test_a_radius_below_the_smallest_cutter_is_flagged():
    print("test_a_radius_below_the_smallest_cutter_is_flagged")
    ok = mc.machinability_screen(
        _block_with_top_pocket(),
        internal_radii=[{"name": "R", "radius_mm": 0.5, "depth_mm": 2.0}])
    _check("R0.5 (a Ø1 cutter) is the floor and passes", ok["pass"], True)
    bad = mc.machinability_screen(
        _block_with_top_pocket(),
        internal_radii=[{"name": "R", "radius_mm": 0.3, "depth_mm": 2.0}])
    _check("R0.3 fails", bad["pass"], False)
    _check("as small_radius", [f["code"] for f in bad["findings"]], ["small_radius"])


def test_thin_walls_are_flagged_two_sided():
    print("test_thin_walls_are_flagged_two_sided")
    thick = mc.machinability_screen(_block_with_top_pocket(),
                                    wall_samples=[{"name": "W", "wall_mm": 3.0}])
    _check("a 3 mm wall passes", thick["pass"], True)
    thin = mc.machinability_screen(_block_with_top_pocket(),
                                   wall_samples=[{"name": "W", "wall_mm": 0.4}])
    _check("a 0.4 mm wall fails", thin["pass"], False)
    _check("as thin_wall", [f["code"] for f in thin["findings"]], ["thin_wall"])
    _check("bare floats are accepted too",
           mc.machinability_screen(_block_with_top_pocket(),
                                   wall_samples=[0.4])["pass"], False)


def test_screen_labels_its_fidelity_and_its_limits():
    print("test_screen_labels_its_fidelity_and_its_limits")
    res = mc.machinability_screen(_block_with_top_pocket())
    _check("fidelity", res["fidelity"], "correlation")
    _check("no band applies to an ordinal screen", res["band_pct"], None)
    _check("escalates to the quantitative tier", res["escalate_to"],
           "cnc_time_estimate")
    _ok("it says no toolpath is computed", "no toolpath" in res["basis"],
        res["basis"])
    _ok("and admits centroid sampling", "centroid" in res["limitations"],
        res["limitations"])


# --- the time model: monotonicity ---------------------------------------------

def _bracket(**kw):
    """The reference bracket: a 100x60x20 aluminium plate part, half its stock
    removed, ~184 cm2 of machined surface."""
    params = dict(part_volume_mm3=60000.0, bbox_mm=[100.0, 60.0, 20.0],
                  machined_area_mm2=18400.0, material="AL6061-T6", setups=1)
    params.update(kw)
    return mc.machining_time(**params)


def test_harder_material_takes_strictly_longer():
    print("test_harder_material_takes_strictly_longer")
    # The issue's monotonicity gate. Ordered the way a machinist would order them.
    ladder = ["ABS", "AL6061-T6", "CastIron-GrayClass40", "Steel-1045",
              "Steel-4140-QT", "SS304", "Ti-6Al-4V"]
    times = [_bracket(material=m)["machine_time_min"] for m in ladder]
    _ok("time rises strictly down the machinability ladder",
        all(a < b for a, b in zip(times, times[1:])),
        list(zip(ladder, times)))
    _ok(f"titanium is an order of magnitude past aluminium "
        f"({times[-1] / times[1]:.1f}x)", times[-1] / times[1] > 5.0)


def test_tighter_tolerance_class_takes_strictly_longer():
    print("test_tighter_tolerance_class_takes_strictly_longer")
    grades = ["IT12", "IT11", "IT10", "IT9", "IT8", "IT7", "IT6"]
    times = [_bracket(tolerance_class=g)["machine_time_min"] for g in grades]
    _ok("time rises strictly as the class tightens",
        all(a < b for a, b in zip(times, times[1:])), list(zip(grades, times)))
    # and it is the SAME curve cost_estimate uses — one corpus, two consumers
    _check("IT9 is the cnc cell's natural grade, so the factor is 1.0",
           _bracket(tolerance_class="IT9")["tolerance"]["factor"], 1.0)
    _check("and no class at all is also 1.0",
           _bracket()["tolerance"]["factor"], 1.0)
    _check("declaring IT9 changes nothing vs declaring nothing",
           _bracket(tolerance_class="IT9")["machine_time_min"],
           _bracket()["machine_time_min"])


def test_more_removed_volume_and_more_setups_take_longer():
    print("test_more_removed_volume_and_more_setups_take_longer")
    light = _bracket(part_volume_mm3=110000.0)     # barely any stock removed
    heavy = _bracket(part_volume_mm3=20000.0)      # skeletonised
    _ok("removing more material takes longer",
        heavy["machine_time_min"] > light["machine_time_min"],
        f"{heavy['machine_time_min']} vs {light['machine_time_min']}")
    _check("and the removed volume is exact arithmetic",
           heavy["removed_volume_mm3"],
           round(heavy["stock_volume_mm3"] - 20000.0, 3))
    one, three = _bracket(setups=1), _bracket(setups=3)
    _check("each extra setup adds exactly setup_min",
           round(three["machine_time_min"] - one["machine_time_min"], 6),
           round(2 * mc.SETUP_MIN, 6))
    _ok("setup time is NOT scaled by utilisation or tolerance",
        _bracket(setups=3, tolerance_class="IT6")["setup_min_total"]
        == three["setup_min_total"])


def test_stock_allowance_only_adds_work():
    print("test_stock_allowance_only_adds_work")
    tight = _bracket(stock_allowance_mm=0.0)
    loose = _bracket(stock_allowance_mm=5.0)
    _ok("a bigger billet means more chips",
        loose["roughing_min"] > tight["roughing_min"])
    _check("zero allowance means the bbox is the stock",
           tight["stock_volume_mm3"], 100.0 * 60.0 * 20.0)


# --- the time model: calibration ----------------------------------------------
#
# Two reference parts, sized and expected the way a job shop quotes them. These are
# SHOP-PRACTICE ranges, not a citation from a published cycle-time table — saying so
# is the point: a fabricated reference would make the gate look stronger than it is
# while calibrating the model against nothing. Each expected window is comfortably
# wider than the model's own +/-50 % band, so passing means the model is in the
# right PLACE, not merely inside its own error bars.

def test_reference_aluminium_bracket_lands_in_the_shop_window():
    print("test_reference_aluminium_bracket_lands_in_the_shop_window")
    # 100x60x20 6061 plate part, roughly half the billet removed, one setup off a
    # sawn blank. A shop quotes this at ~20-45 min of machine time including the
    # setup — a "one hit, in and out before lunch" job.
    got = _bracket()["machine_time_min"]
    _ok(f"{got:.1f} min is inside the 20-45 min shop window",
        20.0 <= got <= 45.0, f"got {got}")


def test_reference_steel_housing_lands_in_the_shop_window():
    print("test_reference_steel_housing_lands_in_the_shop_window")
    # 150x100x60 mild-steel housing: a 900 cm3 billet cut down to 500 cm3, ~500 cm2
    # of machined surface, three setups. Shops quote this kind of part in the
    # 2.5-6 hour range — an afternoon, not a lunch break.
    got = mc.machining_time(part_volume_mm3=500000.0, bbox_mm=[150.0, 100.0, 60.0],
                            machined_area_mm2=50000.0, material="Steel-1045",
                            setups=3)["machine_time_min"]
    _ok(f"{got:.0f} min is inside the 150-360 min shop window",
        150.0 <= got <= 360.0, f"got {got}")


def test_a_drilled_hole_costs_what_a_drilled_hole_costs():
    print("test_a_drilled_hole_costs_what_a_drilled_hole_costs")
    # An independent sanity anchor on the MRR corpus: a Ø10 x 30 hole in aluminium
    # is a few seconds of spindle time, and the same hole in titanium is not.
    al = mc.hole_time(10.0, 30.0, "AL6061-T6")
    ti = mc.hole_time(10.0, 30.0, "Ti-6Al-4V")
    _ok(f"aluminium: {al * 60:.1f} s", 1.0 <= al * 60 <= 20.0, f"{al * 60} s")
    _ok(f"titanium: {ti * 60:.0f} s", ti > 5.0 * al, f"{ti} vs {al}")


# --- the declared band --------------------------------------------------------

def test_the_band_is_derived_and_tightens_when_mrr_is_measured():
    print("test_the_band_is_derived_and_tightens_when_mrr_is_measured")
    corpus = _bracket()
    _check("fidelity", corpus["fidelity"], "correlation")
    _check("the corpus band is 50%", corpus["band_pct"], 50.0)
    _ok("and the basis shows the arithmetic behind it",
        "RSS" in corpus["basis"] and "40" in corpus["basis"] and "25"
        in corpus["basis"], corpus["basis"])
    _ok("2x tighter than the flat table it replaces", corpus["band_pct"] < 100.0)
    measured = _bracket(mrr_cm3_min=72.0)
    _check("a measured MRR tightens the band to 30%", measured["band_pct"], 30.0)
    _ok("and the basis says why", "MRR supplied" in measured["basis"],
        measured["basis"])
    _check("the measured rate is the one used", measured["mrr_cm3_min"], 72.0)


def test_material_class_resolution_and_its_refusals():
    print("test_material_class_resolution_and_its_refusals")
    _check("6061 by category", mc.material_class("AL6061-T6")["class"], "aluminium")
    _check("304 by name override (its category lies)",
           mc.material_class("SS304")["class"], "stainless")
    _check("4140 Q&T is not a mild steel",
           mc.material_class("Steel-4140-QT")["class"], "alloy_steel")
    _check("a polymer", mc.material_class("ABS")["class"], "plastic")
    _check("a class name passes through",
           mc.material_class("titanium")["class"], "titanium")
    _raises("an unknown material raises", lambda: mc.material_class("Unobtainium"))
    _raises("glass has no milling answer and refuses to invent one",
            lambda: mc.material_class("N-BK7"))
    # ...but an explicit rate lets any material through
    _ok("an explicit MRR bypasses the corpus entirely",
        mc.machining_time(part_volume_mm3=1000.0, bbox_mm=[10, 10, 10],
                          machined_area_mm2=600.0, mrr_cm3_min=20.0,
                          finish_cm2_min=10.0)["material_class"] == "explicit")


def test_time_model_rejects_impossible_inputs():
    print("test_time_model_rejects_impossible_inputs")
    _raises("a non-positive volume",
            lambda: mc.machining_time(0.0, [10, 10, 10], material="AL6061-T6"))
    _raises("a degenerate bbox",
            lambda: mc.machining_time(100.0, [10, 0, 10], material="AL6061-T6"))
    _raises("a part bigger than its own stock envelope",
            lambda: mc.machining_time(1e9, [10, 10, 10], material="AL6061-T6"))
    _raises("utilisation above 1",
            lambda: mc.machining_time(100.0, [10, 10, 10], material="AL6061-T6",
                                      utilisation=1.5))
    _raises("zero setups",
            lambda: mc.machining_time(100.0, [10, 10, 10], material="AL6061-T6",
                                      setups=0))


def test_missing_machined_area_warns_instead_of_guessing_silently():
    print("test_missing_machined_area_warns_instead_of_guessing_silently")
    res = mc.machining_time(part_volume_mm3=60000.0, bbox_mm=[100.0, 60.0, 20.0],
                            material="AL6061-T6")
    _ok("it warns", res["warnings"], res["warnings"])
    _ok("and says the fallback under-counts",
        "under-count" in res["warnings"][0], res["warnings"][0])


# --- feeding cost_estimate ----------------------------------------------------

def test_cost_estimate_with_the_new_time_stays_in_its_declared_band():
    print("test_cost_estimate_with_the_new_time_stays_in_its_declared_band")
    # The issue's third gate. The toy: the same 1e6 mm3 aluminium part the existing
    # cost toys use. Feeding a real machine time must move the answer, but not out
    # of the band the OLD rollup declared — a flat table with band_pct=100 claims
    # "within a factor of 2", and if the honest model disagreed by more than that
    # the flat table was never a screen at all.
    bbox = [100.0, 100.0, 100.0]
    t = mc.machining_time(part_volume_mm3=1e6, bbox_mm=bbox,
                          machined_area_mm2=6.0 * 100.0 * 100.0,
                          material="AL6061-T6", setups=1)
    table = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6")
    modelled = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                                  machine_time_hr=t["machine_time_hr"])
    lo = table["unit_cost"] * (1.0 - table["band_pct"] / 100.0)
    hi = table["unit_cost"] * (1.0 + table["band_pct"] / 100.0)
    _ok(f"modelled ${modelled['unit_cost']:.2f} is inside the table's "
        f"${lo:.2f}-${hi:.2f} band", lo <= modelled["unit_cost"] <= hi,
        f"{modelled['unit_cost']} vs [{lo}, {hi}]")
    _check("and the rollup now declares the tighter band", modelled["band_pct"],
           50.0)
    _check("material cost is untouched (it was always exact)",
           modelled["material_cost"], table["material_cost"])


def test_the_two_tolerance_paths_agree_on_the_same_factor():
    print("test_the_two_tolerance_paths_agree_on_the_same_factor")
    # One corpus, two consumers: the multiplier machining_time applies must be the
    # same one cost_estimate would have applied to its table.
    t9 = _bracket(tolerance_class="IT9")["cutting_min"]
    t6 = _bracket(tolerance_class="IT6")["cutting_min"]
    c9 = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                            tolerance_class="IT9")["breakdown"]["machine_time_hr"]
    c6 = cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                            tolerance_class="IT6")["breakdown"]["machine_time_hr"]
    # 1e-4, not 0: both models round their reported times, so an exact compare
    # would be testing the rounding rather than the shared corpus.
    _ok(f"machining_time ratio {t6 / t9:.4f} == cost_estimate ratio {c6 / c9:.4f}",
        abs(t6 / t9 - c6 / c9) < 1e-4, f"{t6 / t9} vs {c6 / c9}")
    _check("and the factor itself is literally the same object's value",
           _bracket(tolerance_class="IT6")["tolerance"]["factor"],
           cost.cost_estimate(volume_mm3=1e6, material="AL6061-T6",
                              tolerance_class="IT6")["breakdown"]["tolerance_factor"])


def main():
    for fn in (
        test_a_prismatic_block_is_a_one_setup_part,
        test_a_five_axis_only_undercut_flags_the_same_block,
        test_a_plain_billet_still_quotes_one_setup_not_zero,
        test_setup_cover_is_order_independent,
        test_features_on_opposite_faces_need_two_setups,
        test_deep_pocket_is_flagged_by_tool_l_over_d,
        test_a_sharp_internal_corner_is_unmakeable,
        test_a_radius_below_the_smallest_cutter_is_flagged,
        test_thin_walls_are_flagged_two_sided,
        test_screen_labels_its_fidelity_and_its_limits,
        test_harder_material_takes_strictly_longer,
        test_tighter_tolerance_class_takes_strictly_longer,
        test_more_removed_volume_and_more_setups_take_longer,
        test_stock_allowance_only_adds_work,
        test_reference_aluminium_bracket_lands_in_the_shop_window,
        test_reference_steel_housing_lands_in_the_shop_window,
        test_a_drilled_hole_costs_what_a_drilled_hole_costs,
        test_the_band_is_derived_and_tightens_when_mrr_is_measured,
        test_material_class_resolution_and_its_refusals,
        test_time_model_rejects_impossible_inputs,
        test_missing_machined_area_warns_instead_of_guessing_silently,
        test_cost_estimate_with_the_new_time_stays_in_its_declared_band,
        test_the_two_tolerance_paths_agree_on_the_same_factor,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

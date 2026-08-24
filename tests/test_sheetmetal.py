"""Sheet metal (issue #230) — pure-core unit tests.

No FreeCAD, no worker, no OCC: :mod:`ankusdrive.sheetmetal` is stdlib vector math
over a feature model, so these run on the host interpreter in milliseconds. They
prove the five things the layer promises:

  * the bend arithmetic IS the textbook arithmetic — BA = angle·(R + K·t) to the
    last bit, the outside-setback and bend-deduction identities hold, and the two
    shop routes to a flat length (tangent + BA, outside − BD) agree exactly;
  * the flat pattern of a known U-channel matches the handbook number computed
    independently, and the identity that pins the whole development is checked
    directly: at K = 0.5 the neutral fibre is at mid-thickness, so the flat blank
    has EXACTLY the folded volume — and at any other K it must not;
  * K is visible and its provenance is labelled — a supplied K or a shop bend-table
    row is exact arithmetic, a corpus K is a correlation with a millimetre band on
    the developed length, and the number is echoed into every result;
  * refold reads leg lengths back OFF the flat pattern, so corrupting a bend
    allowance or an angle moves the refolded geometry (the two-sided gate the
    worker suite then runs against real solids);
  * the DXF is a deliverable, not a dump — it re-parses into a CLOSED profile on
    the CUT layer with bend lines on their own up/down layers.

Run: python3 tests/test_sheetmetal.py
"""
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import sheetmetal as sm  # noqa: E402

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


def _close(label, got, want, tol=1e-9):
    _ok(label, abs(got - want) <= tol, f"got {got!r}, want {want!r} (tol {tol})")


# --- the golden fixtures ------------------------------------------------------
#
# T = 2 mm mild steel bent at R = 2 mm, 90 degrees: r/t = 1 puts it in the second
# band of the medium-material K chart, K = 0.43. Everything below is checked
# against numbers recomputed from first principles here in the test, never against
# a value copied out of the implementation.

T = 2.0
R = 2.0
K = 0.43


def _l_bracket(material="Steel-A36", length=30.0, angle=90.0, radius=R,
               length_from="outer"):
    """60x40 base with one flange off the x=60 edge: the simplest two-region part."""
    model = sm.new_part(T, [[0, 0], [60, 0], [60, 40], [0, 40]], material=material)
    hit = sm.locate_edge(model, (60, 0, 0), (60, 40, 0))
    sm.attach(model, hit["region"], hit["a"], hit["b"], length_mm=length,
              angle_deg=angle, inner_radius_mm=radius, length_from=length_from)
    return model


def _hemmed(length):
    """The same base with a 180-degree hem instead of a flange. A hem is always
    dimensioned tangent-to-free-end; there is no apex to measure to."""
    return _l_bracket(angle=180.0, radius=1.0, length=length,
                      length_from="tangent")


def _u_channel():
    """A U-channel of 100 mm OUTSIDE web and 50 mm outside legs.

    The base profile is 100 - 2*(R + t) = 92 wide, because a base flange is the
    flat face TANGENT TO TANGENT and the bends grow outward from it — the single
    semantic most easily got wrong about a sheet-metal sketch."""
    model = sm.new_part(T, [[0, 0], [92, 0], [92, 60], [0, 60]],
                        material="Steel-A36")
    for x in (0.0, 92.0):
        hit = sm.locate_edge(model, (x, 0, 0), (x, 60, 0))
        sm.attach(model, hit["region"], hit["a"], hit["b"], length_mm=50.0,
                  angle_deg=90.0, inner_radius_mm=R)
    return model


# --- the bend arithmetic is the textbook arithmetic ---------------------------

def test_bend_allowance_is_the_standard_formula():
    print("test_bend_allowance_is_the_standard_formula")
    for angle in (30.0, 45.0, 90.0, 135.0, 180.0):
        want = math.radians(angle) * (R + K * T)
        _close(f"BA({angle:g} deg) == theta*(R + K*t)",
               sm.bend_allowance(angle, R, T, K), want, 1e-12)
    # the two things BA is linear in, checked independently of the formula above
    _close("BA doubles with the angle",
           sm.bend_allowance(90, R, T, K), sm.bend_allowance(45, R, T, K) * 2, 1e-12)
    _close("K=0 puts the neutral fibre on the inside surface",
           sm.bend_allowance(90, R, T, 0.0), math.radians(90) * R, 1e-12)
    _close("K=1 puts it on the outside surface",
           sm.bend_allowance(90, R, T, 1.0), math.radians(90) * (R + T), 1e-12)


def test_setback_and_deduction_identities():
    print("test_setback_and_deduction_identities")
    _close("OSSB(90) == R + t", sm.outside_setback(90, R, T), R + T, 1e-12)
    _close("ISSB(90) == R", sm.inside_setback(90, R), R, 1e-12)
    for angle in (30.0, 60.0, 90.0, 120.0):
        ossb = sm.outside_setback(angle, R, T)
        _close(f"OSSB({angle:g}) == (R+t)*tan(theta/2)",
               ossb, (R + T) * math.tan(math.radians(angle) / 2.0), 1e-12)
        _close(f"BD({angle:g}) == 2*OSSB - BA", sm.bend_deduction(angle, R, T, K),
               2 * ossb - sm.bend_allowance(angle, R, T, K), 1e-12)
    # a 180-degree bend has no virtual apex at all: the outside surfaces of a hem
    # are parallel and never meet, so a setback (and therefore a deduction) would be
    # infinite. Returning None is the honest answer; returning a big float is not.
    _check("OSSB(180) is undefined", sm.outside_setback(180, R, T), None)
    _check("BD(180) is undefined", sm.bend_deduction(180, R, T, K), None)


def test_the_two_flat_length_routes_agree_exactly():
    print("test_the_two_flat_length_routes_agree_exactly")
    bends = [{"angle_deg": 90.0, "inner_radius_mm": R, "thickness_mm": T, "k": K}] * 2
    outer = sm.flat_length([100.0, 50.0, 50.0], bends, reference="outer")
    tangent = sm.flat_length([92.0, 46.0, 46.0], bends, reference="tangent")
    _close("outside-minus-deduction == tangent-plus-allowance",
           outer["length_mm"], tangent["length_mm"], 1e-9)
    # ... and both equal the handbook number, recomputed here from scratch
    ba = math.radians(90) * (R + K * T)
    bd = 2 * (R + T) * math.tan(math.radians(45)) - ba
    _close("matches the handbook U-channel flat", outer["length_mm"],
           100.0 + 50.0 + 50.0 - 2 * bd, 1e-9)
    _close("literal handbook value", outer["length_mm"], 192.98495498926721, 1e-6)


def test_outside_dimensions_are_refused_for_a_hem():
    print("test_outside_dimensions_are_refused_for_a_hem")
    bends = [{"angle_deg": 180.0, "inner_radius_mm": 1.0, "thickness_mm": T,
              "k": K}]
    try:
        sm.flat_length([50.0, 10.0], bends, reference="outer")
        _ok("raises rather than returning a wrong number", False)
    except ValueError as e:
        _ok("raises rather than returning a wrong number", "180" in str(e), str(e))
    got = sm.flat_length([50.0, 10.0], bends, reference="tangent")
    _close("the tangent route still works", got["length_mm"],
           60.0 + math.radians(180) * (1.0 + K * T), 1e-12)


# --- the K-factor corpus is visible and labelled ------------------------------

def test_k_factor_tracks_r_over_t_and_material():
    print("test_k_factor_tracks_r_over_t_and_material")
    soft = sm.k_factor(T, 1.0, material="AL6061-T6")     # r/t = 0.5
    hard = sm.k_factor(T, 1.0, material="SS304")
    _check("soft material, tight bend", soft["k"], 0.33)
    _check("hard material, same bend", hard["k"], 0.40)
    _ok("K rises with the bend radius",
        sm.k_factor(T, 10.0, material="Steel-A36")["k"]
        > sm.k_factor(T, 1.0, material="Steel-A36")["k"])
    _check("classes come off the Materials-DB names",
           [sm.bend_class("AL6061-T6"), sm.bend_class("Steel-A36"),
            sm.bend_class("SS304")], [sm.SOFT, sm.MEDIUM, sm.HARD])
    _check("an EN grade lands by hint, not by default", sm.bend_class("S275JR"),
           sm.MEDIUM)
    # the two alloys most sheet parts are actually cut from are not Materials-DB
    # names, so they must be reachable by hint AND carry their own minimum radius
    _check("the everyday sheet alloys are classified",
           [sm.bend_class("AL5052-H32"), sm.bend_class("AL3003-H14")],
           [sm.SOFT, sm.SOFT])
    _check("and are in the min-radius corpus by name",
           [sm.min_bend_radius(T, m)["known"]
            for m in ("AL5052-H32", "AL3003-H14")], [True, True])
    _check("a steel name is not swallowed by an aluminium hint",
           sm.bend_class("CalculiX-Steel"), sm.MEDIUM)


def test_k_provenance_is_never_invisible():
    print("test_k_provenance_is_never_invisible")
    corpus = sm.k_factor(T, R, material="Steel-A36")
    _check("a corpus K is a correlation", corpus["fidelity"], "correlation")
    _check("with a band", corpus["band_pct"], sm.K_BAND_PCT)
    _ok("and says where it came from", "press-brake chart" in corpus["source"],
        corpus["source"])
    given = sm.k_factor(T, R, material="Steel-A36", k=0.42)
    _check("a supplied K is exact arithmetic", given["fidelity"], "exact")
    _check("no band on it", given["band_pct"], 0.0)
    _check("and it is honoured", given["k"], 0.42)
    unknown = sm.k_factor(T, R, material="Unobtainium")
    _check("an unknown material still resolves", unknown["k"],
           sm.k_factor(T, R, bend_class_=sm.DEFAULT_BEND_CLASS)["k"])
    _ok("and admits it defaulted", "not in the bend corpus" in unknown["source"],
        unknown["source"])


def test_min_bend_radius_does_not_track_the_k_class():
    print("test_min_bend_radius_does_not_track_the_k_class")
    # 6061-T6 forms in the SOFT K band but is precipitation-hardened and cracks
    # under 3t. A corpus that keyed minimum radius off the K class would get this
    # exactly backwards, which is why it has its own table.
    _check("6061-T6 is soft for K", sm.bend_class("AL6061-T6"), sm.SOFT)
    _close("but wants 3t of radius", sm.min_bend_radius(T, "AL6061-T6")["radius_mm"],
           3.0 * T)
    _close("while A36 takes 1t", sm.min_bend_radius(T, "Steel-A36")["radius_mm"],
           1.0 * T)
    unknown = sm.min_bend_radius(T, "Unobtainium")
    _check("an unknown material degrades rather than raising", unknown["known"],
           False)
    _ok("and names the fallback", "fallback" in unknown["source"],
        unknown["source"])


# --- the flat pattern ---------------------------------------------------------

def test_u_channel_flat_matches_the_handbook():
    print("test_u_channel_flat_matches_the_handbook")
    flat = sm.unfold(_u_channel())
    _check("both bends developed", len(flat["bends"]), 2)
    _close("flat length == handbook 192.985 mm", flat["flat_size"][0],
           192.98495498926721, 1e-6)
    _close("width is untouched by bending about it", flat["flat_size"][1], 60.0)
    for bend in flat["bends"]:
        _close(f"{bend['name']} outer leg is the drawing dimension",
               bend["outer_length_mm"], 50.0, 1e-9)
        _close(f"{bend['name']} BA", bend["bend_allowance_mm"],
               round(math.radians(90) * (R + K * T), 6), 1e-9)
    _check("nothing to complain about", flat["warnings"], [])


def test_at_k_half_the_blank_has_exactly_the_folded_volume():
    print("test_at_k_half_the_blank_has_exactly_the_folded_volume")
    # The identity the whole development rests on. A bend sector's true volume is
    # theta*t*(R + t/2)*w; its flat footprint is theta*(R + K*t)*t*w. They are equal
    # iff K = 0.5 — i.e. iff the neutral fibre is at mid-thickness. Checking it
    # directly pins the bend allowance, the leg arithmetic and the outline splice at
    # once, against a volume computed here from first principles.
    model = _l_bracket()
    leg = 30.0 - (R + T)                       # outer dimension minus the setback
    folded = (60 * 40 * T                      # base
              + math.radians(90) * T * (R + T / 2.0) * 40   # bend sector
              + leg * T * 40)                  # straight leg
    at_half = sm.unfold(model, k=0.5)
    _close("blank volume == folded volume at K=0.5", at_half["blank_volume_mm3"],
           folded, 1e-5)
    at_corpus = sm.unfold(model)
    _ok("and NOT at K=0.43 — bending does not conserve volume",
        abs(at_corpus["blank_volume_mm3"] - folded) > 1.0,
        f"{at_corpus['blank_volume_mm3']} vs {folded}")


def test_flat_length_is_recomputed_when_k_changes():
    print("test_flat_length_is_recomputed_when_k_changes")
    model = _l_bracket()
    lo = sm.unfold(model, k=0.30)
    hi = sm.unfold(model, k=0.50)
    _close("the difference is exactly the change in bend allowance",
           hi["flat_size"][0] - lo["flat_size"][0],
           math.radians(90) * (0.50 - 0.30) * T, 1e-6)
    _ok("nothing K-dependent is cached between calls",
        sm.unfold(model, k=0.30)["flat_size"] == lo["flat_size"])


def test_a_shop_bend_table_outranks_the_chart():
    print("test_a_shop_bend_table_outranks_the_chart")
    model = _l_bracket()
    table = [{"thickness_mm": T, "inner_radius_mm": R, "angle_deg": 90.0,
              "allowance_mm": 4.0}]
    flat = sm.unfold(model, bend_table=table)
    _close("the measured allowance is used verbatim",
           flat["bends"][0]["bend_allowance_mm"], 4.0, 1e-9)
    _check("a measured number is exact, not a correlation", flat["fidelity"],
           "exact")
    _check("no band on a measured development", flat["band_pct"], 0.0)
    # a deduction row is the same fact stated the other way round
    bd_row = [{"thickness_mm": T, "inner_radius_mm": R, "angle_deg": 90.0,
               "deduction_mm": 2 * (R + T) - 4.0}]
    _close("a deduction row gives the same allowance",
           sm.unfold(model, bend_table=bd_row)["bends"][0]["bend_allowance_mm"],
           4.0, 1e-9)
    # a row for a different bend must NOT be silently applied
    miss = [{"thickness_mm": 3.0, "inner_radius_mm": R, "angle_deg": 90.0,
             "allowance_mm": 4.0}]
    _check("a non-matching row falls back to the corpus",
           sm.unfold(model, bend_table=miss)["fidelity"], "correlation")


def test_corpus_k_carries_an_honest_millimetre_band():
    print("test_corpus_k_carries_an_honest_millimetre_band")
    flat = sm.unfold(_u_channel())
    _check("a corpus development is a correlation", flat["fidelity"], "correlation")
    lo, hi = flat["developed_band_mm"]
    band = math.radians(90) * (K * sm.K_BAND_PCT / 100.0) * T * 2  # two bends
    _close("the band is the actual BA spread over the K band", hi, band, 1e-6)
    _close("and it is symmetric", lo, -band, 1e-6)
    _ok("a band that means something in mm, not just a percent", hi > 0.0)
    pinned = sm.unfold(_u_channel(), k=0.43)
    _check("pinning K removes the band", pinned["developed_band_mm"], [0.0, 0.0])
    _check("and makes it exact", pinned["fidelity"], "exact")


def test_length_reference_changes_the_leg_not_the_bend():
    print("test_length_reference_changes_the_leg_not_the_bend")
    outer = sm.unfold(_l_bracket(length=30.0))["bends"][0]
    tangent = sm.unfold(_l_bracket())["bends"][0]
    _close("an 'outer' 30 mm leg is 30 - (R+t) tangent-to-end",
           tangent["leg_tangent_mm"], 30.0 - (R + T), 1e-9)
    _check("the bend allowance does not care how the leg was dimensioned",
           outer["bend_allowance_mm"], tangent["bend_allowance_mm"])
    model = sm.new_part(T, [[0, 0], [60, 0], [60, 40], [0, 40]],
                        material="Steel-A36")
    hit = sm.locate_edge(model, (60, 0, 0), (60, 40, 0))
    sm.attach(model, hit["region"], hit["a"], hit["b"], length_mm=26.0,
              angle_deg=90.0, inner_radius_mm=R, length_from="tangent")
    _close("'tangent' is taken literally",
           sm.unfold(model)["bends"][0]["leg_tangent_mm"], 26.0, 1e-9)


def test_a_leg_shorter_than_its_setback_is_refused():
    print("test_a_leg_shorter_than_its_setback_is_refused")
    # 3 mm measured to the outside apex of a 90-degree bend at R = t = 2 leaves
    # -1 mm of straight leg. Building a negative-length prism would produce an
    # invalid solid three calls later; saying so here names the actual mistake.
    try:
        _l_bracket(length=3.0)
        _ok("rejected at the point of the mistake", False)
    except ValueError as e:
        _ok("rejected at the point of the mistake", "setback" in str(e), str(e))


def test_a_tab_is_a_zero_angle_bend():
    print("test_a_tab_is_a_zero_angle_bend")
    model = sm.new_part(T, [[0, 0], [50, 0], [50, 30], [0, 30]])
    hit = sm.locate_edge(model, (50, 0, 0), (50, 30, 0))
    sm.attach(model, hit["region"], hit["a"], hit["b"], kind=sm.TAB, length_mm=12.0)
    flat = sm.unfold(model)
    _close("it grows the flat pattern", flat["flat_size"][0], 62.0)
    _check("but adds no bend line", flat["bend_lines"], [])
    _check("and no bend allowance", flat["bends"][0]["bend_allowance_mm"], 0.0)
    _ok("its region is coplanar with the base",
        model["regions"][1]["n_3"] == model["regions"][0]["n_3"])


def test_a_partial_width_feature_splices_a_notch():
    print("test_a_partial_width_feature_splices_a_notch")
    model = sm.new_part(T, [[0, 0], [50, 0], [50, 30], [0, 30]])
    hit = sm.locate_edge(model, (50, 0, 0), (50, 30, 0))
    a = (50.0, 5.0)
    b = (50.0, 15.0)
    sm.attach(model, hit["region"], a, b, kind=sm.TAB, length_mm=12.0)
    outline = [tuple(p) for p in sm.unfold(model)["outline"]]
    for want in ((50.0, 5.0), (62.0, 5.0), (62.0, 15.0), (50.0, 15.0)):
        _ok(f"outline detours through {want}",
            any(math.dist(p, want) < 1e-9 for p in outline), str(outline))
    _check("the outline stays a single closed loop", len(outline), 8)


def test_nested_bends_develop_outward_not_back_over_the_parent():
    print("test_nested_bends_develop_outward_not_back_over_the_parent")
    # A hem on a flange is the case that catches a handedness bug: every region's
    # local frame must stay right-handed against its own CCW polygon, or the child
    # develops mirrored and its footprint splices INTO the parent instead of past
    # it. This is exactly the bug the first implementation had.
    model = _l_bracket()
    flange = model["regions"][1]
    far_a = sm.region_point(flange, (0.0, 26.0))
    far_b = sm.region_point(flange, (40.0, 26.0))
    hit = sm.locate_edge(model, far_a, far_b)
    _check("the flange's free edge is locatable", hit["region"], 1)
    sm.attach(model, hit["region"], hit["a"], hit["b"], kind=sm.HEM,
              length_mm=10.0, angle_deg=180.0, inner_radius_mm=1.0,
              length_from="tangent")
    flat = sm.unfold(model)
    _check("no overlap reported", flat["warnings"], [])
    ba_flange = math.radians(90) * (R + K * T)
    # the hem's r/t is 0.5, a band tighter than the flange's, so it takes K = 0.38 —
    # the corpus is consulted per bend, not once per part
    ba_hem = math.radians(180) * (1.0 + 0.38 * T)
    _close("the flat grows by BA(flange) + leg + BA(hem) + return",
           flat["flat_size"][0], 60.0 + ba_flange + 26.0 + ba_hem + 10.0, 1e-5)
    _check("still a single closed outline", len(flat["outline"]), 8)


def test_a_hem_reports_an_allowance_but_no_deduction():
    print("test_a_hem_reports_an_allowance_but_no_deduction")
    try:
        _l_bracket(angle=180.0, radius=1.0, length=10.0)
        _ok("an outside dimension on a hem is refused", False)
    except ValueError as e:
        _ok("an outside dimension on a hem is refused", "apex" in str(e), str(e))
    model = _hemmed(10.0)
    hem = sm.unfold(model)["bends"][0]
    _close("BA is defined at 180", hem["bend_allowance_mm"],
           round(math.radians(180) * (1.0 + 0.38 * T), 6), 1e-9)
    _check("BD is not", hem["bend_deduction_mm"], None)
    _check("nor is the outer length", hem["outer_length_mm"], None)


# --- refold reads the flat back, so a corrupted report moves the geometry -----

def _profiles(steps):
    """Every number a build step carries, rounded — the comparison a two-sided gate
    needs. Comparing only the profile points would miss a leg that changed LENGTH
    without moving its start face, which is exactly what a corrupted bend allowance
    does."""
    out = []
    for s in steps:
        row = [s["op"], s["name"],
               [tuple(round(v, 6) for v in p) for p in s["profile"]]]
        for key in ("vector", "axis_point", "axis_dir", "angle_deg"):
            if key in s:
                row.append(round(s[key], 6) if key == "angle_deg"
                           else tuple(round(v, 6) for v in s[key]))
        out.append(tuple(row))
    return out


def test_refold_reproduces_the_model_built_geometry():
    print("test_refold_reproduces_the_model_built_geometry")
    model = _l_bracket()
    flat = sm.unfold(model)
    base = model["regions"][0]
    built = sm.build_recipes(model)
    back = sm.refold(flat, base_placement={"origin3": base["origin3"],
                                           "e1_3": base["e1_3"],
                                           "e2_3": base["e2_3"]})
    _check("same step sequence", [(s["op"], s["name"]) for s in back],
           [(s["op"], s["name"]) for s in built])
    worst = 0.0
    for a, b in zip(built, back):
        for pa, pb in zip(a["profile"], b["profile"]):
            worst = max(worst, max(abs(x - y) for x, y in zip(pa, pb)))
        key = "vector" if a["op"] == "extrude" else "axis_point"
        worst = max(worst, max(abs(x - y) for x, y in zip(a[key], b[key])))
    _ok("every vertex agrees to within rounding", worst < 1e-6, f"worst {worst}")


def test_a_corrupted_bend_allowance_moves_the_refold():
    print("test_a_corrupted_bend_allowance_moves_the_refold")
    # The gate that makes the round trip mean something: refold derives each leg by
    # walking from the bend's attachment past its REPORTED allowance to the far edge
    # of the region in the flat pattern. Overstate the allowance and the leg gets
    # correspondingly shorter, so the refolded part is not the part.
    model = _l_bracket()
    flat = sm.unfold(model)
    good = sm.refold(flat)
    flat["bends"][0]["bend_allowance_mm"] += 1.0
    bad = sm.refold(flat)
    leg_good = max(abs(v) for v in good[-1]["vector"])
    leg_bad = max(abs(v) for v in bad[-1]["vector"])
    _close("the leg shortens by exactly the injected error", leg_good - leg_bad,
           1.0, 1e-6)
    _ok("so the refolded geometry differs", _profiles(good) != _profiles(bad))


def test_a_corrupted_bend_angle_moves_the_refold():
    print("test_a_corrupted_bend_angle_moves_the_refold")
    model = _l_bracket()
    flat = sm.unfold(model)
    good = sm.refold(flat)
    flat["bends"][0]["angle_deg"] = 80.0
    bad = sm.refold(flat)
    _ok("the bend sector sweeps a different angle",
        bad[1]["angle_deg"] == 80.0 and good[1]["angle_deg"] == 90.0)
    _ok("and the leg points somewhere else",
        _profiles(good)[-1] != _profiles(bad)[-1])


def test_a_flipped_bend_direction_moves_the_refold():
    print("test_a_flipped_bend_direction_moves_the_refold")
    flat = sm.unfold(_l_bracket())
    good = sm.refold(flat)
    flat["bends"][0]["direction"] = "down"
    bad = sm.refold(flat)
    _ok("the revolve axis reverses",
        all(abs(x + y) < 1e-9 for x, y in zip(good[1]["axis_dir"],
                                              bad[1]["axis_dir"])),
        f"{good[1]['axis_dir']} vs {bad[1]['axis_dir']}")


# --- the DXF is a deliverable -------------------------------------------------

def test_dxf_reimports_as_a_closed_profile_on_the_cut_layer():
    print("test_dxf_reimports_as_a_closed_profile_on_the_cut_layer")
    model = _u_channel()
    model["holes"] = [{"id": "H1", "region": 0, "x": 46.0, "y": 30.0,
                       "diameter_mm": 8.0}]
    flat = sm.unfold(model)
    parsed = sm.parse_dxf(sm.to_dxf(flat))
    _check("all three layers are declared",
           [layer["name"] for layer in parsed["layers"]], list(sm.DXF_LAYERS))
    _ok("each layer carries a colour",
        all(layer["color"] is not None for layer in parsed["layers"]))
    _check("exactly one profile", len(parsed["polylines"]), 1)
    profile = parsed["polylines"][0]
    _check("on the CUT layer", profile["layer"], sm.LAYER_CUT)
    _check("flagged closed, not merely ending where it started",
           profile["closed"], True)
    _check("with every outline vertex", len(profile["points"]),
           len(flat["outline"]))
    _close("re-parsed area == developed area",
           abs(sm._signed_area(profile["points"])), flat["flat_area_mm2"], 1e-4)
    _check("the hole came through as a circle on CUT",
           [(c["layer"], c["radius"]) for c in parsed["circles"]],
           [(sm.LAYER_CUT, 4.0)])


def test_bend_lines_land_on_the_up_and_down_layers():
    print("test_bend_lines_land_on_the_up_and_down_layers")
    model = sm.new_part(T, [[0, 0], [92, 0], [92, 60], [0, 60]],
                        material="Steel-A36")
    for x, direction in ((0.0, "up"), (92.0, "down")):
        hit = sm.locate_edge(model, (x, 0, 0), (x, 60, 0))
        sm.attach(model, hit["region"], hit["a"], hit["b"], length_mm=50.0,
                  angle_deg=90.0, inner_radius_mm=R, direction=direction)
    flat = sm.unfold(model)
    parsed = sm.parse_dxf(sm.to_dxf(flat))
    _check("one line per bend, on the layer naming its direction",
           sorted(line["layer"] for line in parsed["lines"]),
           [sm.LAYER_BEND_DOWN, sm.LAYER_BEND_UP])
    up = next(line for line in parsed["lines"] if line["layer"] == sm.LAYER_BEND_UP)
    ba = math.radians(90) * (R + K * T)
    _close("the bend line sits on the centreline of the bend region",
           up["points"][0][0], -ba / 2.0, 1e-5)
    _close("and spans the full bend", abs(up["points"][0][1] - up["points"][1][1]),
           60.0, 1e-9)


def test_a_tab_contributes_no_bend_line():
    print("test_a_tab_contributes_no_bend_line")
    model = sm.new_part(T, [[0, 0], [50, 0], [50, 30], [0, 30]])
    hit = sm.locate_edge(model, (50, 0, 0), (50, 30, 0))
    sm.attach(model, hit["region"], hit["a"], hit["b"], kind=sm.TAB, length_mm=12.0)
    parsed = sm.parse_dxf(sm.to_dxf(sm.unfold(model)))
    _check("no bend lines", parsed["lines"], [])
    _check("but the tab is in the cut profile", len(parsed["polylines"][0]["points"]),
           6)


# --- the DFM screen -----------------------------------------------------------

def test_min_bend_radius_is_two_sided():
    print("test_min_bend_radius_is_two_sided")
    ok = sm.check(_l_bracket(material="AL6061-T6", radius=3.0 * T))
    tight = sm.check(_l_bracket(material="AL6061-T6", radius=1.0 * T))
    _check("3t on 6061-T6 passes", [f["code"] for f in ok["findings"]], [])
    _check("1t on 6061-T6 fails", [f["code"] for f in tight["findings"]],
           ["min_bend_radius"])
    _check("and the screen says so", tight["ok"], False)
    _close("the limit is reported", tight["findings"][0]["limit_mm"], 3.0 * T)
    # the SAME radius on A36 is fine, which is the point of a per-material corpus
    _check("1t on A36 passes",
           [f["code"] for f in sm.check(_l_bracket(radius=1.0 * T))["findings"]],
           [])


def test_min_flange_length_is_two_sided():
    print("test_min_flange_length_is_two_sided")
    limit = sm.MIN_FLANGE_T * T + R          # 4t + R = 12 mm
    long_enough = sm.check(_l_bracket(length=limit + 0.1))
    too_short = sm.check(_l_bracket(length=limit - 0.1))
    _check(f"{limit + 0.1:g} mm outer leg passes", long_enough["ok"], True)
    _check(f"{limit - 0.1:g} mm outer leg fails",
           [f["code"] for f in too_short["findings"]], ["min_flange_length"])
    _close("against 4t + R", too_short["findings"][0]["limit_mm"], limit, 1e-6)


def test_hole_to_bend_is_two_sided():
    print("test_hole_to_bend_is_two_sided")
    limit = sm.HOLE_TO_BEND_T * T + R        # 2t + R = 6 mm, edge to tangent
    for label, gap, want in (("clear of the bend", limit + 1.0, True),
                             ("under the limit", limit - 1.0, False)):
        model = _l_bracket()
        model["holes"] = [{"id": "H1", "region": 0, "x": 60.0 - gap - 4.0,
                           "y": 20.0, "diameter_mm": 8.0}]
        res = sm.check(model)
        _check(f"a hole {label} -> ok={want}", res["ok"], want)
        if not want:
            _check("flagged as hole_to_bend",
                   [f["code"] for f in res["findings"]], ["hole_to_bend"])


def test_a_hem_is_screened_as_a_hem_not_as_an_air_bend():
    print("test_a_hem_is_screened_as_a_hem_not_as_an_air_bend")
    # A closed hem's inside radius is deliberately under the air-bend minimum and
    # its return is deliberately shorter than 4t + R. Screening it with the air-bend
    # rules would fail every hem ever drawn, so it gets its own rule — and says
    # which process it assumed rather than silently skipping.
    hemmed = sm.check(_hemmed(10.0))
    codes = [f["code"] for f in hemmed["findings"]]
    _check("no air-bend findings", [c for c in codes if c.startswith("min_bend")], [])
    _ok("it declares the process it assumed", "hem_process_assumed" in codes, codes)
    _check("a 10 mm return on 2 mm stock passes", hemmed["ok"], True)
    short = sm.check(_hemmed(6.0))
    _check("a 6 mm return does not", [f["code"] for f in short["findings"]
                                      if f["severity"] == "fail"],
           ["min_hem_length"])


def test_the_screen_labels_its_fidelity_and_never_raises():
    print("test_the_screen_labels_its_fidelity_and_never_raises")
    res = sm.check(_l_bracket(material="Unobtainium"))
    _check("press-brake rules of thumb are a correlation", res["fidelity"],
           "correlation")
    _check("thresholds carry no scatter band", res["band_pct"], None)
    _ok("an unknown material is a finding, not an exception",
        "material_not_in_corpus" in [f["code"] for f in res["findings"]])
    _ok("and it is informational, not a failure",
        all(f["severity"] == "info" for f in res["findings"]))
    _ok("the rules it applied are reported", set(res["rules"]) == {
        "min_bend_radius_mm", "min_flange_outer_mm", "hole_to_bend_mm"})
    _ok("K is echoed alongside the verdict",
        res["k_factors"][0]["k"] is not None and res["k_factors"][0]["source"])


def test_thresholds_are_settable():
    print("test_thresholds_are_settable")
    limit = sm.MIN_FLANGE_T * T + R
    model = _l_bracket(length=limit - 1.0)
    _check("fails at the default 4t", sm.check(model)["ok"], False)
    _check("passes for a shop that runs a narrower vee",
           sm.check(model, min_flange_t=3.0)["ok"], True)


def test_dfm_check_delegates_to_the_same_rules():
    print("test_dfm_check_delegates_to_the_same_rules")
    # There must be exactly ONE implementation of the press-brake rules. dfx's
    # sheet path and sheetmetal.check both go through check_bends, so a change to a
    # threshold cannot make the two tools disagree about the same part.
    from ankusdrive.analysis import dfx
    bends = [{"id": "flange1", "angle_deg": 90.0, "inner_radius_mm": 1.0,
              "outer_length_mm": 30.0}]
    direct = sm.check_bends(bends, T, material="AL6061-T6")
    through = dfx.dfm_check(faces=[], process="sheet",
                            sheet={"thickness_mm": T, "material": "AL6061-T6",
                                   "bends": bends})
    _check("identical sub-result", through["sheet"], direct)
    _check("and its failure gates the overall verdict", through["pass"], False)
    # the SAME bend on A36 (min radius 1t = 2 mm) is still under-radiused at R=1;
    # opening it to 2 mm is what makes it conforming, and that is the whole point of
    # a per-material corpus
    conforming = [dict(bends[0], inner_radius_mm=2.0)]
    clean = dfx.dfm_check(faces=[], process="sheet",
                          sheet={"thickness_mm": T, "material": "Steel-A36",
                                 "bends": conforming})
    _check("a conforming bend leaves dfm_check passing", clean["pass"], True)
    _check("dfm_check without a sheet block is unchanged",
           "sheet" in dfx.dfm_check(faces=[{"name": "a", "draft_deg": 2.0}]), False)


def test_a_refold_collision_is_always_a_hard_fail():
    print("test_a_refold_collision_is_always_a_hard_fail")
    # Interferences come from the caller (the worker intersects the real solids), so
    # the rule here is that a supplied one is never downgraded to a warning: unlike
    # the rules of thumb, an overlap is geometric fact.
    res = sm.check_bends([], T, material="Steel-A36",
                         interferences=[{"a": "flange1", "b": "flange2",
                                         "volume_mm3": 203.09}])
    _check("one finding", [f["code"] for f in res["findings"]],
           ["refold_collision"])
    _check("severity fail", res["findings"][0]["severity"], "fail")
    _check("screen fails", res["ok"], False)
    _ok("it names both features",
        res["findings"][0]["features"] == ["flange1", "flange2"])


# --- geometry bookkeeping -----------------------------------------------------

def test_a_curved_or_degenerate_profile_is_refused():
    print("test_a_curved_or_degenerate_profile_is_refused")
    for label, profile in (("two points", [[0, 0], [10, 0]]),
                           ("zero area", [[0, 0], [10, 0], [20, 0]])):
        try:
            sm.new_part(T, profile)
            _ok(f"{label} rejected", False)
        except ValueError:
            _ok(f"{label} rejected", True)
    try:
        sm.new_part(0.0, [[0, 0], [10, 0], [10, 10]])
        _ok("zero thickness rejected", False)
    except ValueError:
        _ok("zero thickness rejected", True)


def test_profile_winding_does_not_change_the_result():
    print("test_profile_winding_does_not_change_the_result")
    # A clockwise sketch must develop identically to a counter-clockwise one, or
    # every outward normal — and therefore every flange — comes out inverted.
    ccw = sm.new_part(T, [[0, 0], [60, 0], [60, 40], [0, 40]], material="Steel-A36")
    cw = sm.new_part(T, [[0, 40], [60, 40], [60, 0], [0, 0]], material="Steel-A36")
    for model in (ccw, cw):
        hit = sm.locate_edge(model, (60, 0, 0), (60, 40, 0))
        sm.attach(model, hit["region"], hit["a"], hit["b"], length_mm=30.0,
                  angle_deg=90.0, inner_radius_mm=R)
    _check("same flat size", sm.unfold(cw)["flat_size"], sm.unfold(ccw)["flat_size"])
    _check("same flange geometry", cw["regions"][1]["origin3"],
           ccw["regions"][1]["origin3"])


def test_locate_edge_finds_both_faces_and_rejects_a_stranger():
    print("test_locate_edge_finds_both_faces_and_rejects_a_stranger")
    model = _l_bracket()
    datum = sm.locate_edge(model, (60, 0, 0), (60, 40, 0))
    offset = sm.locate_edge(model, (60, 0, T), (60, 40, T))
    _check("the datum-surface edge is found", datum["side"], "datum")
    _check("so is the one on the far face", offset["side"], "offset")
    _check("both map to the same local segment", datum["a"], offset["a"])
    _check("an edge that is on no region returns None",
           sm.locate_edge(model, (500, 0, 0), (500, 40, 0)), None)
    _check("a picked sub-segment comes back as that sub-segment",
           sm.locate_edge(model, (60, 10, 0), (60, 30, 0))["a"], [60.0, 10.0])


def test_unfold_output_is_json_clean():
    print("test_unfold_output_is_json_clean")
    # The model and the report cross the worker pipe on every call, so anything
    # that is not plain JSON breaks the tool surface rather than a unit test.
    import json
    flat = sm.unfold(_u_channel())
    round_tripped = json.loads(json.dumps(flat))
    _check("survives a JSON round trip", round_tripped["flat_size"],
           flat["flat_size"])
    _check("so does the model", json.loads(json.dumps(_u_channel()))["thickness"], T)
    _ok("the DXF is text, not bytes", isinstance(sm.to_dxf(flat), str))


def main():
    for fn in (
        test_bend_allowance_is_the_standard_formula,
        test_setback_and_deduction_identities,
        test_the_two_flat_length_routes_agree_exactly,
        test_outside_dimensions_are_refused_for_a_hem,
        test_k_factor_tracks_r_over_t_and_material,
        test_k_provenance_is_never_invisible,
        test_min_bend_radius_does_not_track_the_k_class,
        test_u_channel_flat_matches_the_handbook,
        test_at_k_half_the_blank_has_exactly_the_folded_volume,
        test_flat_length_is_recomputed_when_k_changes,
        test_a_shop_bend_table_outranks_the_chart,
        test_corpus_k_carries_an_honest_millimetre_band,
        test_length_reference_changes_the_leg_not_the_bend,
        test_a_leg_shorter_than_its_setback_is_refused,
        test_a_tab_is_a_zero_angle_bend,
        test_a_partial_width_feature_splices_a_notch,
        test_nested_bends_develop_outward_not_back_over_the_parent,
        test_a_hem_reports_an_allowance_but_no_deduction,
        test_refold_reproduces_the_model_built_geometry,
        test_a_corrupted_bend_allowance_moves_the_refold,
        test_a_corrupted_bend_angle_moves_the_refold,
        test_a_flipped_bend_direction_moves_the_refold,
        test_dxf_reimports_as_a_closed_profile_on_the_cut_layer,
        test_bend_lines_land_on_the_up_and_down_layers,
        test_a_tab_contributes_no_bend_line,
        test_min_bend_radius_is_two_sided,
        test_min_flange_length_is_two_sided,
        test_hole_to_bend_is_two_sided,
        test_a_hem_is_screened_as_a_hem_not_as_an_air_bend,
        test_the_screen_labels_its_fidelity_and_never_raises,
        test_thresholds_are_settable,
        test_dfm_check_delegates_to_the_same_rules,
        test_a_refold_collision_is_always_a_hard_fail,
        test_a_curved_or_degenerate_profile_is_refused,
        test_profile_winding_does_not_change_the_result,
        test_locate_edge_finds_both_faces_and_rejects_a_stranger,
        test_unfold_output_is_json_clean,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

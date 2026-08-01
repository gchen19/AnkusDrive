"""CNC machinability + machining time (issue #231) and the tolerance–cost handle
path (issue #235) — end-to-end through the FreeCAD worker.

The pure cores are unit-tested in test_machining.py / test_tolerance_cost.py; this
drives the geometry shims on real solids, which is where the interesting failure
modes live — a face census that misreads the billet, a concavity test fooled by a
full cylinder's on-axis centroid, a sharp-corner finding that fires on every pocket
floor.

Built on one 60×40×20 block, varied four ways:

  * ``bore``    — a Ø20 blind bore from the top. One setup, one internal radius,
    nothing to flag: the must-PASS half of the machinability gate.
  * ``undercut`` — the same block with a Ø36 cavity UNDER the bore, i.e. wider than
    its own opening. No 3-axis approach reaches the cavity's ceiling or its wall:
    the must-FAIL half, and it must flag *only* those two faces, leaving the rest
    of the part a one-setup job.
  * ``square``  — a square-cut pocket. Its four VERTICAL corners are unmakeable by a
    rotating tool; its four FLOOR corners are ordinary flat-endmill work and must
    NOT be flagged. That asymmetry is the regression this file exists to hold.
  * ``deep``    — a Ø3 × 40 hole, L/D 13: past any standard tool.

Run: .venv/bin/python3 tests/test_machining_worker.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import Worker  # noqa: E402

_PASS = _FAIL = 0


def _check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}  {detail}")


def _eq(label, got, want):
    _check(label, got == want, f"got {got!r}, want {want!r}")


# --- fixtures -----------------------------------------------------------------

def _block(w, name):
    """The common 60x40x20 billet."""
    w.call("new_document", name=name)
    return w.call("add_primitive", kind="box", w=60, d=40, h=20)["handle"]


def _bored_block(w, name):
    """Block + a Ø20 blind bore 10 deep from the top — an ordinary one-setup part."""
    block = _block(w, name)
    tool = w.call("add_primitive", kind="cylinder", r=10, h=12,
                  placement=[30, 20, 10])["handle"]
    return w.call("boolean_op", op="cut", base=block, tool=tool)["handle"]


def _undercut_block(w, name):
    """The same part with a Ø36 cavity below the bore: an internal volume wider
    than its own opening, which is the textbook 5-axis/EDM undercut."""
    part = _bored_block(w, name)
    cavity = w.call("add_primitive", kind="cylinder", r=18, h=6,
                    placement=[30, 20, 4])["handle"]
    return w.call("boolean_op", op="cut", base=part, tool=cavity)["handle"]


def _codes(res):
    return sorted(f["code"] for f in res["findings"])


# --- the machinability gate, two-sided ----------------------------------------

def test_a_bored_block_is_a_clean_one_setup_part():
    print("test_a_bored_block_is_a_clean_one_setup_part")
    with Worker() as w:
        part = _bored_block(w, "cnc_good")
        res = w.call("cnc_machinability_check", model=part)
        _eq("one setup", res["setups"], 1)
        _eq("from the top", res["setup_directions"], ["+z"])
        _eq("passes", res["pass"], True)
        _eq("no findings", res["findings"], [])
        # the six billet faces are recognised as stock, NOT as six machined faces
        # each demanding its own orientation — the whole reason a block quotes 1.
        _eq("six stock faces", res["stock_faces"], 6)
        _eq("two machined faces (bore wall + bore floor)", res["machined_faces"], 2)
        _check("machined area is the bore only, not the billet's 8800 mm²",
               900.0 < res["machined_area_mm2"] < 1000.0, res["machined_area_mm2"])
        # the bore is read as an INTERNAL cylinder off the real solid
        _eq("the Ø20 bore's radius is picked up", res["min_internal_radius_mm"], 10.0)
        _eq("with its reach as L/D", res["max_l_over_d_seen"], 0.5)


def test_a_five_axis_only_undercut_flags_the_same_block():
    print("test_a_five_axis_only_undercut_flags_the_same_block")
    with Worker() as w:
        part = _undercut_block(w, "cnc_undercut")
        res = w.call("cnc_machinability_check", model=part)
        _eq("fails", res["pass"], False)
        _eq("every finding is an undercut", set(_codes(res)), {"undercut"})
        # BOTH cavity surfaces are genuinely unreachable: the ceiling faces
        # downward into a blind pocket, and the cavity wall is shadowed by the
        # ceiling above it and the floor below it. Two, not one.
        _eq("two unreachable faces", len(res["undercut_faces"]), 2)
        _check("each finding names one of them",
               sorted(f["feature"] for f in res["findings"])
               == sorted(res["undercut_faces"]),
               (res["findings"], res["undercut_faces"]))
        # the rest of the part is unaffected: the defect is isolated, not smeared
        _eq("everything else still comes off one approach",
            res["setup_directions"], ["+z"])
        _check("score dropped but is not zero", 0.0 < res["score"] < 1.0,
               res["score"])


def test_a_convex_boss_is_not_mistaken_for_a_bore():
    print("test_a_convex_boss_is_not_mistaken_for_a_bore")
    # The concavity test must key on which side the material is, not on "is it a
    # cylinder". A boss fused ON TOP of the block is convex: it caps no tool.
    with Worker() as w:
        block = _block(w, "cnc_boss")
        boss = w.call("add_primitive", kind="cylinder", r=8, h=10,
                      placement=[30, 20, 20])["handle"]
        part = w.call("boolean_op", op="fuse", base=block, tool=boss)["handle"]
        res = w.call("cnc_machinability_check", model=part)
        _eq("no internal radius recorded", res["min_internal_radius_mm"], None)
        _eq("and no L/D", res["max_l_over_d_seen"], None)
        _eq("nothing flagged", res["findings"], [])


# --- internal corners ---------------------------------------------------------

def test_square_pocket_flags_its_vertical_corners_only():
    print("test_square_pocket_flags_its_vertical_corners_only")
    # The regression that motivated the approach-direction rule. A square-cut
    # pocket has EIGHT concave edges; only the four vertical ones are unmakeable,
    # because the other four are cut by the flat end of the cutter.
    with Worker() as w:
        block = _block(w, "cnc_square")
        tool = w.call("add_primitive", kind="box", w=20, d=20, h=10,
                      placement=[20, 10, 12])["handle"]
        part = w.call("boolean_op", op="cut", base=block, tool=tool)["handle"]
        res = w.call("cnc_machinability_check", model=part)
        _eq("fails", res["pass"], False)
        _eq("exactly four findings", len(res["findings"]), 4)
        _eq("all of them sharp internal corners", set(_codes(res)),
            {"sharp_internal_corner"})
        _eq("min internal radius is zero", res["min_internal_radius_mm"], 0.0)
        _check("and the advice names a fillet size",
               "R0.5" in res["findings"][0]["detail"], res["findings"][0]["detail"])
        # The finding is ACTIONABLE: its `feature` is an edge reference the fillet
        # tool takes directly, so the fix round-trips without the agent guessing.
        filleted = w.call("fillet_edges", handle=part, radius=3.0,
                          edges=[f["feature"] for f in res["findings"]])
        clean = w.call("cnc_machinability_check", model=filleted["handle"])
        _check("filleting exactly those edges clears the finding",
               "sharp_internal_corner" not in _codes(clean), clean["findings"])
        _check("and the pocket now has a real corner radius",
               clean["min_internal_radius_mm"] == 3.0,
               clean["min_internal_radius_mm"])


def test_a_plain_billet_has_no_internal_corners_at_all():
    print("test_a_plain_billet_has_no_internal_corners_at_all")
    # A block's twelve edges are all convex arrises. If the concavity test were
    # inverted this is where it would show, loudly.
    with Worker() as w:
        part = _block(w, "cnc_billet")
        res = w.call("cnc_machinability_check", model=part)
        _eq("no machined faces", res["machined_faces"], 0)
        _eq("still one setup, never zero", res["setups"], 1)
        _eq("nothing flagged", res["findings"], [])
        _eq("passes", res["pass"], True)


def test_a_deep_narrow_hole_flags_tool_reach():
    print("test_a_deep_narrow_hole_flags_tool_reach")
    with Worker() as w:
        w.call("new_document", name="cnc_deep")
        block = w.call("add_primitive", kind="box", w=60, d=40, h=40)["handle"]
        drill = w.call("add_primitive", kind="cylinder", r=1.5, h=40,
                       placement=[30, 20, 0])["handle"]
        part = w.call("boolean_op", op="cut", base=block, tool=drill)["handle"]
        res = w.call("cnc_machinability_check", model=part)
        _eq("fails", res["pass"], False)
        _eq("on reach, not on radius", _codes(res), ["deep_pocket"])
        _check("L/D is ~13", abs(res["max_l_over_d_seen"] - 13.333) < 0.01,
               res["max_l_over_d_seen"])
        # the same hole in a thin plate is ordinary work — two-sided on the SAME
        # tool diameter, so it is the reach being tested and nothing else.
        w.call("new_document", name="cnc_shallow")
        plate = w.call("add_primitive", kind="box", w=60, d=40, h=6)["handle"]
        d2 = w.call("add_primitive", kind="cylinder", r=1.5, h=6,
                    placement=[30, 20, 0])["handle"]
        thin = w.call("boolean_op", op="cut", base=plate, tool=d2)["handle"]
        ok = w.call("cnc_machinability_check", model=thin)
        _eq("a Ø3 hole 6 deep passes", ok["pass"], True)


# --- the time model on real geometry ------------------------------------------

def test_time_estimate_reads_the_solid_and_agrees_with_the_screen():
    print("test_time_estimate_reads_the_solid_and_agrees_with_the_screen")
    with Worker() as w:
        part = _bored_block(w, "cnc_time")
        screen = w.call("cnc_machinability_check", model=part)
        t = w.call("cnc_time_estimate", model=part, material="AL6061-T6")
        _eq("setups come from the same census", t["setups"], screen["setups"])
        _check("and it says so", "census" in t["setups_basis"], t["setups_basis"])
        _eq("machined area matches the screen",
            round(t["machined_area_mm2"], 2),
            round(screen["machined_area_mm2"], 2))
        _eq("bbox read off the solid", t["bbox_mm"], [60.0, 40.0, 20.0])
        # stock = (60+4)(40+4)(20+4) = 67584; part = 48000 - bore(pi*100*10) = 44858
        _check("removed volume is stock minus part",
               abs(t["removed_volume_mm3"]
                   - (t["stock_volume_mm3"] - t["part_volume_mm3"])) < 1e-3, t)
        _eq("band is the derived 50%, not the table's 100%", t["band_pct"], 50.0)
        _check("and it takes a plausible number of minutes",
               5.0 < t["machine_time_min"] < 120.0, t["machine_time_min"])


def test_time_estimate_is_monotone_in_material_and_tolerance():
    print("test_time_estimate_is_monotone_in_material_and_tolerance")
    with Worker() as w:
        part = _bored_block(w, "cnc_mono")
        al = w.call("cnc_time_estimate", model=part, material="AL6061-T6")
        steel = w.call("cnc_time_estimate", model=part, material="Steel-1045")
        ti = w.call("cnc_time_estimate", model=part, material="Ti-6Al-4V")
        _check("steel takes longer than aluminium",
               steel["machine_time_min"] > al["machine_time_min"])
        _check("titanium longer than steel",
               ti["machine_time_min"] > steel["machine_time_min"])
        loose = w.call("cnc_time_estimate", model=part, material="AL6061-T6",
                       tolerance_class="IT11")
        tight = w.call("cnc_time_estimate", model=part, material="AL6061-T6",
                       tolerance_class="IT6")
        _check("IT6 takes longer than IT11",
               tight["machine_time_min"] > loose["machine_time_min"])
        _check("and IT6's factor is the shared corpus's 4.0x",
               abs(tight["tolerance"]["factor"] - 4.0) < 1e-6,
               tight["tolerance"])


def test_the_time_model_feeds_cost_estimate():
    print("test_the_time_model_feeds_cost_estimate")
    with Worker() as w:
        part = _bored_block(w, "cnc_cost")
        vol = w.call("mass_properties", handle=part)["volume_mm3"]
        t = w.call("cnc_time_estimate", model=part, material="AL6061-T6")
        table = w.call("cost_estimate", volume_mm3=vol, material="AL6061-T6")
        modelled = w.call("cost_estimate", volume_mm3=vol, material="AL6061-T6",
                          machine_time_hr=t["machine_time_hr"])
        _eq("the table rollup still declares its order-of-magnitude band",
            table["band_pct"], 100.0)
        _eq("the modelled one declares the tighter band", modelled["band_pct"], 50.0)
        _eq("and records where the time came from",
            modelled["breakdown"]["machine_time_basis"], "supplied")
        _eq("material cost is identical (it was always exact)",
            modelled["material_cost"], table["material_cost"])
        # The honest finding: the flat table drastically UNDER-counts a small part,
        # because it scales with part volume and knows nothing about setup or
        # finishing. Assert the direction, which is the useful thing to know.
        _check(f"the real model costs more than the table "
               f"(${modelled['unit_cost']:.2f} vs ${table['unit_cost']:.2f})",
               modelled["unit_cost"] > table["unit_cost"])


# --- the tolerance-cost handle path -------------------------------------------

def test_tolerance_cost_check_reads_a_live_handle():
    print("test_tolerance_cost_check_reads_a_live_handle")
    # Same v2 Shape wiring tolerance_stackup uses: a stepped block measured along
    # +z becomes a chain, then gets priced. The two paths must agree.
    with Worker() as w:
        w.call("new_document", name="tc_wire")
        base = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
        cap = w.call("add_primitive", kind="box", w=10, d=10, h=10,
                     placement=[5, 5, 20])["handle"]
        step = w.call("boolean_op", op="fuse", base=base, tool=cap)["handle"]
        live = w.call("tolerance_cost_check", handle=step, axis="+z")
        _eq("two links derived", live["n_links"], 2)
        _eq("the derived chain is echoed", len(live["chain"]), 2)
        _eq("axis echoed", live["axis"], "+z")
        hand = w.call("tolerance_cost_check", chain=live["chain"])
        _eq("the hand-built chain prices identically",
            hand["total_cost_index"], live["total_cost_index"])
        # ISO 2768-m gives +/-0.2 on a 20 mm link — far looser than a CNC cell's
        # natural capability, so nothing should flag.
        _eq("general tolerances do not flag", live["pass"], True)
        # a tight default_tol on the same solid does
        tight = w.call("tolerance_cost_check", handle=step, axis="+z",
                       default_tol=0.004)
        _eq("a +/-0.004 default flags", tight["pass"], False)
        _check("with the secondary-operation verdict",
               tight["flagged"][0]["verdict"] == "needs_secondary_operation",
               tight["flagged"])
        _check("and it costs strictly more",
               tight["total_cost_index"] > live["total_cost_index"],
               (tight["total_cost_index"], live["total_cost_index"]))


def test_suggest_loosening_over_the_mcp_surface():
    print("test_suggest_loosening_over_the_mcp_surface")
    with Worker() as w:
        w.call("new_document", name="tc_loosen")
        res = w.call("suggest_loosening",
                     chain=[{"name": "shaft", "nominal": 50.0, "tol": 0.008},
                            {"name": "spacer", "nominal": 10.0, "tol": 0.010}],
                     spec_min=59.6, spec_max=60.4)
        _eq("it ran", res["ok"], True)
        _check("and found savings", res["saving"] > 0.0, res["note"])
        # the recommended scheme, re-stacked through the worker's own stackup
        check = w.call("tolerance_stackup", chain=res["chain"],
                       method="montecarlo", spec_min=59.6, spec_max=60.4)
        _check(f"the suggested scheme still passes "
               f"(cpk {check['montecarlo']['cpk']})",
               check["montecarlo"]["cpk"] >= res["target_cpk"],
               check["montecarlo"])


def main():
    for fn in (
        test_a_bored_block_is_a_clean_one_setup_part,
        test_a_five_axis_only_undercut_flags_the_same_block,
        test_a_convex_boss_is_not_mistaken_for_a_bore,
        test_square_pocket_flags_its_vertical_corners_only,
        test_a_plain_billet_has_no_internal_corners_at_all,
        test_a_deep_narrow_hole_flags_tool_reach,
        test_time_estimate_reads_the_solid_and_agrees_with_the_screen,
        test_time_estimate_is_monotone_in_material_and_tolerance,
        test_the_time_model_feeds_cost_estimate,
        test_tolerance_cost_check_reads_a_live_handle,
        test_suggest_loosening_over_the_mcp_surface,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

"""Design-for-X toys — two-sided oracles for driftpin.analysis.dfx.

Pure-Python, no FreeCAD. Each check pins a handbook / closed-form result against
a hand calculation AND verifies a deliberately-bad input is caught (raises, or
returns pass:false / fits:false), mirroring tests/TOYS.md and
docs/SIMULATION_EXAMPLES.md (family 9).

Run:  python3 tests/test_dfx.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driftpin.analysis import dfx  # noqa: E402


def test_dfm_box_no_draft_fails():
    # a box pulled along +z with 4 vertical side faces (0 deg draft) -> every
    # side face is a draft violation, pass false, score = 1 - 4/4 = 0.
    faces = [{"name": f"side_{i}", "draft_deg": 0.0} for i in range(4)]
    r = dfx.dfm_check(faces, pull_axis="+z", process="injection")
    assert sorted(r["draft_violations"]) == ["side_0", "side_1", "side_2", "side_3"]
    assert r["undercut_faces"] == []
    assert r["pass"] is False
    assert abs(r["score"] - 0.0) < 1e-9, r["score"]
    assert abs(r["min_wall_mm"] - 1.0) < 1e-9  # injection default


def test_dfm_added_draft_clears_violations():
    # the same 4 side faces at 2 deg draft (>= 1 deg default) -> no violations,
    # pass true, score 1.0. The list empties.
    faces = [{"name": f"side_{i}", "draft_deg": 2.0} for i in range(4)]
    r = dfx.dfm_check(faces, pull_axis="+z", process="injection")
    assert r["draft_violations"] == []
    assert r["pass"] is True
    assert abs(r["score"] - 1.0) < 1e-9, r["score"]


def test_dfm_undercut_and_wall_two_sided():
    # a re-entrant face (draft_deg=-1) lands in undercut_faces, NOT draft_violations;
    # a thin wall under the injection 1.0 mm default is a min-wall violation.
    faces = [
        {"name": "side_hole", "draft_deg": -1.0},
        {"name": "thin_rib", "draft_deg": 3.0, "wall_mm": 0.6},
        {"name": "good_wall", "draft_deg": 3.0, "wall_mm": 2.0},
    ]
    r = dfx.dfm_check(faces, process="injection")
    assert r["undercut_faces"] == ["side_hole"]
    assert "side_hole" not in r["draft_violations"]
    assert r["min_wall_violations"] == ["thin_rib"]
    assert r["pass"] is False
    # process default differs: cnc allows 0.5 mm, so the 0.6 mm wall now passes.
    r_cnc = dfx.dfm_check(faces[1:], process="cnc")
    assert r_cnc["min_wall_violations"] == []
    # an unknown process with no override must raise (no silent default).
    try:
        dfx.dfm_check(faces, process="laser")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown process")


def test_dfa_part_reduction_scores_better():
    # a 12-part assembly with 8 separate fasteners must score worse than the same
    # function achieved with 4 parts and no fasteners (reward part reduction).
    bad = dfx.dfa_check(12, 8)
    good = dfx.dfa_check(4, 0)
    assert bad["assembly_score"] < good["assembly_score"], (bad, good)
    # efficiency is theoretical_min(=1) / (parts + fasteners)
    assert abs(bad["assembly_efficiency"] - 1.0 / 20.0) < 1e-9, bad["assembly_efficiency"]
    assert abs(good["assembly_efficiency"] - 1.0 / 4.0) < 1e-9, good["assembly_efficiency"]


def test_dfa_monotone_in_fasteners_and_parts():
    # adding a fastener can only lower the score; adding a part too.
    base = dfx.dfa_check(6, 2)
    more_fast = dfx.dfa_check(6, 3)
    more_parts = dfx.dfa_check(7, 2)
    assert more_fast["assembly_score"] < base["assembly_score"]
    assert more_parts["assembly_score"] < base["assembly_score"]
    # handling difficulty bands from insertion axes
    assert dfx.dfa_check(4, 0, insertion_axes=1, symmetric_fraction=1.0)["handling_difficulty"] == "low"
    assert dfx.dfa_check(4, 0, insertion_axes=4, symmetric_fraction=1.0)["handling_difficulty"] == "high"
    # an absurd input (no parts) must raise, never return a bogus score.
    try:
        dfx.dfa_check(0, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for part_count=0")


def test_dfa_fidelity_contract_is_ordinal():
    # SIMULATION_NEXT.md contract: the grade is an ordinal ranking index —
    # fidelity 'correlation' with NO physical scatter band (band_pct None).
    r = dfx.dfa_check(6, 2)
    assert r["fidelity"] == "correlation", r
    assert r["band_pct"] is None, r["band_pct"]


def test_pack_fits_and_void_fraction():
    # part [300,200,150] in carton [310,210,160] -> fits, void = 1 - 9e6/1.0416e7.
    r = dfx.pack_check([300, 200, 150], [310, 210, 160], mass_g=1500)
    assert r["fits"] is True and r["pass"] is True
    assert abs(r["void_fraction"] - (1.0 - 9.0e6 / 1.0416e7)) < 1e-4, r["void_fraction"]
    # dim weight = (1.0416e7 mm^3 / 1000 -> cm^3) / 5000 = 2.0832 kg; it dominates
    # the 1.5 kg actual mass, so it is billable.
    assert abs(r["dim_weight_kg"] - 2.0832) < 1e-3, r["dim_weight_kg"]
    assert abs(r["actual_mass_kg"] - 1.5) < 1e-9
    assert abs(r["billable_weight_kg"] - 2.0832) < 1e-3, r["billable_weight_kg"]


def test_pack_oversized_part_does_not_fit():
    # part [400,200,150] is longer than the carton's longest dim (310) even after
    # reorientation -> fits false, pass false, void_fraction None.
    r = dfx.pack_check([400, 200, 150], [310, 210, 160], mass_g=1500)
    assert r["fits"] is False and r["pass"] is False
    assert r["void_fraction"] is None
    # a heavy part flips billable weight back to actual mass.
    heavy = dfx.pack_check([300, 200, 150], [310, 210, 160], mass_g=5000)
    assert abs(heavy["billable_weight_kg"] - 5.0) < 1e-9, heavy["billable_weight_kg"]
    # a degenerate carton (zero dim) must raise, never divide-by-zero silently.
    try:
        dfx.pack_check([10, 10, 10], [0, 100, 100], mass_g=100)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a zero carton dimension")


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

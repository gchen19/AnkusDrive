"""Planar-kinematics toys — exact oracles for ankusdrive.analysis.kinematics.

Pure-Python, no FreeCAD, no PyBullet. These are the hard-edged closed-form anchors
the MBD family (docs/SIMULATION_EXAMPLES.md §8) is gated against: four-bar Grübler
DOF=1, Grashof classification, slider-crank stroke = 2R (independent of conrod), and
the vector-loop four-bar position/sweep (full rotation vs limit, self-collision).

Run:  python3 tests/test_kinematics.py
"""
import math
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ankusdrive.analysis import kinematics as kin  # noqa: E402


def test_gruebler_dof():
    # four-bar: n=4, four revolutes -> 3*3 - 2*4 = 1
    assert kin.gruebler_dof(4, [{"type": "revolute"}] * 4) == 1
    # slider-crank: three revolutes + one prismatic, still DOF 1
    assert kin.gruebler_dof(4, ["revolute", "revolute", "revolute", "prismatic"]) == 1
    # five-bar (two inputs): 3*4 - 2*5 = 2
    assert kin.gruebler_dof(5, ["revolute"] * 5) == 2
    # a pinned triangle is a rigid structure: 3*2 - 2*3 = 0
    assert kin.gruebler_dof(3, ["revolute"] * 3) == 0
    # a higher pair (gear/cam) removes only 1 DOF
    assert kin.gruebler_dof(3, ["revolute", "revolute", "gear"]) == 3 * 2 - 2 * 2 - 1


def test_slider_crank_stroke_is_2R_independent_of_conrod():
    for R, L in ((10, 30), (10, 50), (25, 80), (5, 1000)):
        r = kin.slider_crank(crank_mm=R, conrod_mm=L)
        assert abs(r["stroke_mm"] - 2 * R) < 1e-6, (R, L, r["stroke_mm"])
        assert r["inline_stroke_exact"] is True, r
    # conrod must be longer than the crank to assemble
    try:
        kin.slider_crank(crank_mm=10, conrod_mm=10)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when conrod <= crank")
    # an offset slider-crank no longer has an exact-2R stroke
    off = kin.slider_crank(crank_mm=10, conrod_mm=40, wrist_offset_mm=5)
    assert off["inline_stroke_exact"] is False, off


def test_grashof_crank_rocker():
    r = kin.grashof_classify(crank=2, coupler=7, rocker=6, ground=5)
    assert r["condition"] == "grashof", r
    assert r["type"] == "crank-rocker", r
    assert r["shortest"] == "crank", r
    assert r["input_crank_fully_rotates"] is True, r


def test_grashof_non_grashof_triple_rocker():
    # all sides short, ground long -> no link fully rotates
    r = kin.grashof_classify(crank=4, coupler=4, rocker=4, ground=9)
    assert r["condition"] == "non_grashof", r
    assert r["type"] == "triple-rocker", r
    assert r["input_crank_fully_rotates"] is False, r


def test_grashof_change_point_and_drag_link():
    # a square is a change-point (S+L == P+Q)
    sq = kin.grashof_classify(crank=5, coupler=5, rocker=5, ground=5)
    assert sq["condition"] == "change_point", sq
    # shortest link is the ground -> double-crank (drag-link); crank still rotates
    dl = kin.grashof_classify(crank=4, coupler=5, rocker=5, ground=2)
    assert dl["condition"] == "grashof" and dl["type"] == "double-crank", dl
    assert dl["input_crank_fully_rotates"] is True, dl


def test_grashof_errors():
    for bad in (
        lambda: kin.grashof_classify(0, 5, 6, 7),          # non-positive
        lambda: kin.grashof_classify(1, 1, 1, 10),         # cannot close
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_fourbar_parallelogram_tracks_crank():
    # parallelogram (crank==rocker, coupler==ground): the open assembly keeps the
    # rocker parallel to the crank, so theta4 == theta2 at every angle.
    for th2 in (30.0, 60.0, 90.0, 135.0):
        pos = kin.fourbar_position(ground=10, crank=4, coupler=10, rocker=4,
                                   theta2_deg=th2, config="open")
        assert pos is not None, th2
        assert abs(pos["theta4_deg"] - th2) < 1e-4, (th2, pos["theta4_deg"])


def test_fourbar_sweep_full_rotation_vs_limit():
    # crank-rocker (crank shortest, Grashof) closes at EVERY crank angle
    cr = kin.fourbar_sweep(ground=5, crank=2, coupler=7, rocker=6, n_steps=72)
    assert cr["reachable"] is True and cr["n_reached"] == 72, cr
    assert len(cr["coupler_path"]) == 72 and cr["reachable_bbox_mm"][0] > 0, cr
    # non-Grashof triple-rocker hits a limit -> some angles unreachable
    tr = kin.fourbar_sweep(ground=9, crank=4, coupler=4, rocker=4, n_steps=72)
    assert tr["reachable"] is False and tr["n_reached"] < 72, tr


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

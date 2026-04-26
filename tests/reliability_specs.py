"""
Layer C reliability — design specs paired with geometric ground truth.

Each Spec is a parametric design intent (volumes, hole counts, dimensions)
plus a deterministic builder + a ground-truth checker. The builder produces
the "correct" geometry; the checker takes a DriftPin handle and verifies
whether the handle matches the spec.

The agent-loop test asks the model to look at a render and judge whether the
geometry matches the spec. We compare the model's verdict to the
ground-truth check. Mismatches in either direction (false positives or false
negatives) count as perception failures.

Two flavors per spec:
  - matches_spec: builder produces geometry that satisfies the spec
  - violates_spec: a deliberately wrong build (different size, missing
    feature, etc.) — the agent should reject this on visual inspection

The builders are intentionally simple — Layer C tests the model's perception,
not its tool use. (That's a separate test we'll need someday.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Spec:
    name: str
    description: str  # human-readable, also handed to the model
    correct_builder: Callable  # (worker) -> handle
    wrong_builder: Callable    # (worker) -> handle (deliberately violates spec)
    wrong_violation: str       # what the wrong build does differently (used in report only)
    checker: Callable          # (worker, handle) -> {"matches": bool, "reason": str}


# --- shared helpers ----------------------------------------------------------

def _check_volume(w, handle, expected_mm3, tol=0.02):
    mp = w.call("mass_properties", handle=handle)
    actual = mp["volume_mm3"]
    diff = abs(actual - expected_mm3) / expected_mm3
    return diff < tol, f"volume={actual:.1f}mm³ (expected {expected_mm3:.1f}, diff {diff:.1%})"


def _count_cylindrical_holes(w, handle, radius=None):
    """Count cylindrical faces. If radius given, only count those of that radius."""
    pred = {"type": "cylindrical"}
    if radius is not None:
        pred["radius_eq"] = radius
    return len(w.call("query_faces", handle=handle, predicate=pred))


# --- Spec 1: 30mm cube --------------------------------------------------------

def correct_30mm_cube(w):
    w.call("new_document", name="spec1_ok")
    return w.call("add_primitive", kind="box", w=30, d=30, h=30)["handle"]


def wrong_20mm_cube(w):
    w.call("new_document", name="spec1_bad")
    return w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]


def check_30mm_cube(w, handle):
    ok, reason = _check_volume(w, handle, 27000.0)
    return {"matches": ok, "reason": reason}


# --- Spec 2: 30mm cube with 6mm centered hole --------------------------------

def correct_cube_with_hole(w):
    w.call("new_document", name="spec2_ok")
    box = w.call("add_primitive", kind="box", w=30, d=30, h=30)["handle"]
    cyl = w.call(
        "add_primitive", kind="cylinder", r=3, h=30, placement=[15, 15, 0],
    )["handle"]
    return w.call("boolean_op", op="cut", base=box, tool=cyl)["handle"]


def wrong_cube_no_hole(w):
    """Same cube — hole missing entirely. Visual inspection should catch this."""
    w.call("new_document", name="spec2_bad")
    return w.call("add_primitive", kind="box", w=30, d=30, h=30)["handle"]


def check_cube_with_hole(w, handle):
    expected_vol = 30 ** 3 - 3.14159 * 9 * 30
    ok_vol, vol_reason = _check_volume(w, handle, expected_vol)
    n_holes = _count_cylindrical_holes(w, handle, radius=3.0)
    ok_hole = n_holes == 1
    return {
        "matches": ok_vol and ok_hole,
        "reason": f"{vol_reason}; cylindrical_faces_r3={n_holes}",
    }


# --- Spec 3: cylinder r=10, h=40 ---------------------------------------------

def correct_cylinder(w):
    w.call("new_document", name="spec3_ok")
    return w.call("add_primitive", kind="cylinder", r=10, h=40)["handle"]


def wrong_box_for_cylinder(w):
    """A box masquerading as a cylinder spec — clearly wrong shape."""
    w.call("new_document", name="spec3_bad")
    return w.call("add_primitive", kind="box", w=20, d=20, h=40)["handle"]


def check_cylinder(w, handle):
    expected_vol = 3.14159 * 100 * 40
    ok_vol, vol_reason = _check_volume(w, handle, expected_vol)
    n_cyl = _count_cylindrical_holes(w, handle, radius=10.0)
    return {
        "matches": ok_vol and n_cyl == 1,
        "reason": f"{vol_reason}; cylindrical_faces_r10={n_cyl}",
    }


# --- Spec 4: plate with 4 corner holes ---------------------------------------

def correct_plate_4_holes(w):
    w.call("new_document", name="spec4_ok")
    plate = w.call("add_primitive", kind="box", w=40, d=30, h=4)["handle"]
    for x, y in [(5, 5), (35, 5), (5, 25), (35, 25)]:
        h = w.call(
            "add_primitive", kind="cylinder", r=2, h=4, placement=[x, y, 0],
        )["handle"]
        plate = w.call("boolean_op", op="cut", base=plate, tool=h)["handle"]
    return plate


def wrong_plate_2_holes(w):
    """Same plate but only 2 holes — visually obvious omission."""
    w.call("new_document", name="spec4_bad")
    plate = w.call("add_primitive", kind="box", w=40, d=30, h=4)["handle"]
    for x, y in [(5, 5), (35, 25)]:
        h = w.call(
            "add_primitive", kind="cylinder", r=2, h=4, placement=[x, y, 0],
        )["handle"]
        plate = w.call("boolean_op", op="cut", base=plate, tool=h)["handle"]
    return plate


def check_plate_4_holes(w, handle):
    n = _count_cylindrical_holes(w, handle, radius=2.0)
    expected_vol = 40 * 30 * 4 - 4 * 3.14159 * 4 * 4
    ok_vol, vol_reason = _check_volume(w, handle, expected_vol, tol=0.05)
    return {
        "matches": ok_vol and n == 4,
        "reason": f"{vol_reason}; cylindrical_faces_r2={n}",
    }


# --- Spec 5: L-bracket -------------------------------------------------------

def correct_l_bracket(w):
    w.call("new_document", name="spec5_ok")
    h_plate = w.call("add_primitive", kind="box", w=40, d=20, h=5)["handle"]
    v_plate = w.call(
        "add_primitive", kind="box", w=5, d=20, h=30, placement=[0, 0, 5],
    )["handle"]
    return w.call("boolean_op", op="fuse", base=h_plate, tool=v_plate)["handle"]


def wrong_flat_plate(w):
    """Same horizontal plate but vertical leg missing — not an L."""
    w.call("new_document", name="spec5_bad")
    return w.call("add_primitive", kind="box", w=40, d=20, h=5)["handle"]


def check_l_bracket(w, handle):
    # L-bracket: h_plate (40*20*5=4000) + v_plate (5*20*30=3000), shared face
    # at z=5 has zero volume, so union = 7000 mm³.
    expected_vol = 40 * 20 * 5 + 5 * 20 * 30
    ok_vol, vol_reason = _check_volume(w, handle, expected_vol, tol=0.05)
    return {"matches": ok_vol, "reason": vol_reason}


# --- registry ----------------------------------------------------------------

SPECS = [
    Spec(
        name="cube_30mm",
        description=(
            "A solid cube, 30mm on each side. No holes, no rounded edges, "
            "no other features."
        ),
        correct_builder=correct_30mm_cube,
        wrong_builder=wrong_20mm_cube,
        wrong_violation="cube is 20mm not 30mm",
        checker=check_30mm_cube,
    ),
    Spec(
        name="cube_with_centered_hole",
        description=(
            "A 30mm cube with a single 6mm-diameter cylindrical hole going "
            "all the way through the center, parallel to one of the axes."
        ),
        correct_builder=correct_cube_with_hole,
        wrong_builder=wrong_cube_no_hole,
        wrong_violation="hole is missing entirely",
        checker=check_cube_with_hole,
    ),
    Spec(
        name="cylinder_r10_h40",
        description="A cylinder, 20mm diameter and 40mm tall.",
        correct_builder=correct_cylinder,
        wrong_builder=wrong_box_for_cylinder,
        wrong_violation="part is a square box, not a cylinder",
        checker=check_cylinder,
    ),
    Spec(
        name="plate_4_corner_holes",
        description=(
            "A flat rectangular plate (40mm × 30mm × 4mm thick) with four "
            "small mounting holes, one at each corner."
        ),
        correct_builder=correct_plate_4_holes,
        wrong_builder=wrong_plate_2_holes,
        wrong_violation="plate has only 2 holes instead of 4",
        checker=check_plate_4_holes,
    ),
    Spec(
        name="l_bracket",
        description=(
            "An L-shaped bracket: a horizontal flat plate with a vertical "
            "wall rising from one edge. Two perpendicular plates joined."
        ),
        correct_builder=correct_l_bracket,
        wrong_builder=wrong_flat_plate,
        wrong_violation="vertical leg is missing — just a flat plate",
        checker=check_l_bracket,
    ),
]

"""
Layer B reliability — paired shapes for diff detection.

Each entry is a (baseline_builder, modified_builder, DiffSpec). The
baseline+modified are rendered side-by-side; the model is asked what changed;
the response is graded by keyword.

Diff detection is harder than classification (Layer A) — the model must not
just identify the shape but localize a feature change. The accuracy bar is
correspondingly lower (≥60% vs ≥70% for classification).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DiffSpec:
    name: str
    description: str
    must_match_any: list[str]  # at least one keyword must appear


# --- builders ----------------------------------------------------------------

def cube_small(w):
    w.call("new_document", name="cube_s")
    return w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]


def cube_big(w):
    w.call("new_document", name="cube_b")
    return w.call("add_primitive", kind="box", w=30, d=30, h=30)["handle"]


def cube_with_hole(w):
    w.call("new_document", name="cwh")
    box = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
    cyl = w.call(
        "add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0],
    )["handle"]
    return w.call("boolean_op", op="cut", base=box, tool=cyl)["handle"]


def cube_no_hole(w):
    w.call("new_document", name="cnh")
    return w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]


def cube_offset_hole(w):
    """Same cube as cube_with_hole, but the hole is offset to a corner."""
    w.call("new_document", name="coh")
    box = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
    cyl = w.call(
        "add_primitive", kind="cylinder", r=5, h=20, placement=[5, 5, 0],
    )["handle"]
    return w.call("boolean_op", op="cut", base=box, tool=cyl)["handle"]


def cylinder_thin(w):
    w.call("new_document", name="cyl_t")
    return w.call("add_primitive", kind="cylinder", r=5, h=30)["handle"]


def cylinder_fat(w):
    w.call("new_document", name="cyl_f")
    return w.call("add_primitive", kind="cylinder", r=15, h=30)["handle"]


def stacked_two(w):
    w.call("new_document", name="st_2")
    big = w.call("add_primitive", kind="box", w=40, d=40, h=10)["handle"]
    small = w.call(
        "add_primitive", kind="box", w=20, d=20, h=10, placement=[10, 10, 10],
    )["handle"]
    return w.call("boolean_op", op="fuse", base=big, tool=small)["handle"]


def stacked_one(w):
    """Just the bottom slab — second box removed."""
    w.call("new_document", name="st_1")
    return w.call("add_primitive", kind="box", w=40, d=40, h=10)["handle"]


def l_bracket_short(w):
    w.call("new_document", name="lb_s")
    h_plate = w.call("add_primitive", kind="box", w=40, d=20, h=5)["handle"]
    v_plate = w.call(
        "add_primitive", kind="box", w=5, d=20, h=30, placement=[0, 0, 5],
    )["handle"]
    return w.call("boolean_op", op="fuse", base=h_plate, tool=v_plate)["handle"]


def l_bracket_long(w):
    """Horizontal leg extended from 40 → 80."""
    w.call("new_document", name="lb_l")
    h_plate = w.call("add_primitive", kind="box", w=80, d=20, h=5)["handle"]
    v_plate = w.call(
        "add_primitive", kind="box", w=5, d=20, h=30, placement=[0, 0, 5],
    )["handle"]
    return w.call("boolean_op", op="fuse", base=h_plate, tool=v_plate)["handle"]


def plate_4_holes(w):
    w.call("new_document", name="p4")
    plate = w.call("add_primitive", kind="box", w=40, d=30, h=4)["handle"]
    for x, y in [(5, 5), (35, 5), (5, 25), (35, 25)]:
        hole = w.call(
            "add_primitive", kind="cylinder", r=2, h=4, placement=[x, y, 0],
        )["handle"]
        plate = w.call("boolean_op", op="cut", base=plate, tool=hole)["handle"]
    return plate


def plate_3_holes(w):
    """Top-right hole removed."""
    w.call("new_document", name="p3")
    plate = w.call("add_primitive", kind="box", w=40, d=30, h=4)["handle"]
    for x, y in [(5, 5), (5, 25), (35, 25)]:
        hole = w.call(
            "add_primitive", kind="cylinder", r=2, h=4, placement=[x, y, 0],
        )["handle"]
        plate = w.call("boolean_op", op="cut", base=plate, tool=hole)["handle"]
    return plate


# --- pairs (baseline, modified, diff_spec) -----------------------------------

DIFFS = [
    (
        cube_small, cube_big,
        DiffSpec(
            name="cube_scaled_up",
            description="Cube grew from 20mm to 30mm",
            must_match_any=[
                "bigger", "larger", "scaled", "grew", "size", "increase",
                "more massive", "expanded",
            ],
        ),
    ),
    (
        cube_with_hole, cube_no_hole,
        DiffSpec(
            name="hole_removed",
            description="Cube with a hole vs the same cube without one",
            must_match_any=[
                "hole removed", "no hole", "no through", "missing hole",
                "filled in", "no longer has", "without hole", "no opening",
                "solid", "no perforation",
            ],
        ),
    ),
    (
        cube_with_hole, cube_offset_hole,
        DiffSpec(
            name="hole_moved",
            description="Hole moved from center to a corner",
            must_match_any=[
                "moved", "offset", "shifted", "no longer centered",
                "off-center", "off center", "different position",
                "relocated", "corner", "edge",
            ],
        ),
    ),
    (
        cylinder_thin, cylinder_fat,
        DiffSpec(
            name="cylinder_radius_tripled",
            description="Cylinder radius increased",
            must_match_any=[
                "thicker", "wider", "fatter", "larger radius", "bigger radius",
                "wider cylinder", "thicker cylinder", "more diameter",
                "increased diameter", "larger diameter",
            ],
        ),
    ),
    (
        stacked_two, stacked_one,
        DiffSpec(
            name="upper_box_removed",
            description="Stacked tier removed; only the base slab remains",
            must_match_any=[
                "removed", "missing", "single", "one", "no top", "no upper",
                "just the base", "only the bottom", "no longer stacked",
                "flat", "smaller", "no second",
            ],
        ),
    ),
    (
        l_bracket_short, l_bracket_long,
        DiffSpec(
            name="bracket_leg_lengthened",
            description="Horizontal leg of L-bracket extended",
            must_match_any=[
                "longer", "extended", "bigger leg", "wider", "elongated",
                "stretched", "extended leg", "longer base", "expanded",
            ],
        ),
    ),
    (
        plate_4_holes, plate_3_holes,
        DiffSpec(
            name="one_hole_removed",
            description="Plate had 4 corner holes; now has 3",
            must_match_any=[
                "three holes", "missing hole", "fewer holes", "one less",
                "3 holes", "missing one", "removed hole",
                "no longer four", "only three", "less holes",
            ],
        ),
    ),
]

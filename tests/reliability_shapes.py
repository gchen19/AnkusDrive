"""
Library of reliability-test shapes. Each shape returns the AnkusDrive handle to
render. Shapes are paired with a `grader` spec: which keywords identify the
shape correctly, and which keywords would be a wrong answer.

Builders accept an open Worker. Builders create their own document so calls
can be sequenced cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ShapeSpec:
    name: str
    description: str
    must_match_any: list[str]      # at least one of these must appear in the answer
    must_match_all: list[list[str]] = field(default_factory=list)
                                   # each inner list: must match >= 1 (groups ANDed)
    must_not_match: list[str] = field(default_factory=list)
                                   # none of these may appear


def build_cube(w):
    w.call("new_document", name="cube")
    return w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]


def build_box_rect(w):
    """Asymmetric box — different from a cube, must not be called a cube."""
    w.call("new_document", name="rect_box")
    return w.call("add_primitive", kind="box", w=40, d=20, h=10)["handle"]


def build_cylinder(w):
    w.call("new_document", name="cyl")
    return w.call("add_primitive", kind="cylinder", r=10, h=30)["handle"]


def build_sphere(w):
    w.call("new_document", name="sphere")
    return w.call("add_primitive", kind="sphere", r=15)["handle"]


def build_cube_with_hole(w):
    w.call("new_document", name="cube_hole")
    base = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
    tool = w.call(
        "add_primitive", kind="cylinder", r=5, h=20, placement=[10, 10, 0],
    )["handle"]
    return w.call("boolean_op", op="cut", base=base, tool=tool)["handle"]


def build_cube_with_filleted_edge(w):
    w.call("new_document", name="cube_fillet")
    box = w.call("add_primitive", kind="box", w=20, d=20, h=20)["handle"]
    edges = w.call("list_edges", handle=box)
    # Pick one vertical edge.
    vertical = [
        e for e in edges
        if e["kind"] == "line"
        and abs(e["length"] - 20.0) < 1e-3
        and abs(e["centroid"][0]) < 1e-3
        and abs(e["centroid"][1]) < 1e-3
    ]
    return w.call(
        "fillet_edges", handle=box, edges=[vertical[0]["tag"]], radius=5.0,
    )["handle"]


def build_two_stacked_boxes(w):
    """A stepped/L-stepped block — small box on top of a big box."""
    w.call("new_document", name="stacked")
    big = w.call("add_primitive", kind="box", w=40, d=40, h=10)["handle"]
    small = w.call(
        "add_primitive", kind="box", w=20, d=20, h=10,
        placement=[10, 10, 10],
    )["handle"]
    return w.call("boolean_op", op="fuse", base=big, tool=small)["handle"]


def build_l_bracket(w):
    """L-shaped bracket: a horizontal plate + a vertical plate."""
    w.call("new_document", name="lbracket")
    h_plate = w.call("add_primitive", kind="box", w=40, d=20, h=5)["handle"]
    v_plate = w.call(
        "add_primitive", kind="box", w=5, d=20, h=30,
        placement=[0, 0, 5],
    )["handle"]
    return w.call("boolean_op", op="fuse", base=h_plate, tool=v_plate)["handle"]


def build_plate_with_4_holes(w):
    """Common mechanical part: rectangular plate with 4 corner holes."""
    w.call("new_document", name="plate")
    plate = w.call("add_primitive", kind="box", w=40, d=30, h=4)["handle"]
    for x, y in [(5, 5), (35, 5), (5, 25), (35, 25)]:
        hole = w.call(
            "add_primitive", kind="cylinder", r=2, h=4,
            placement=[x, y, 0],
        )["handle"]
        plate = w.call("boolean_op", op="cut", base=plate, tool=hole)["handle"]
    return plate


def build_pad_with_pocket(w):
    """PartDesign: pad a square then pocket a smaller square."""
    w.call("new_document", name="pad_pocket")
    body = w.call("make_body")["handle"]
    sk1 = w.call("make_sketch", body=body, plane="XY")["handle"]
    g = w.call(
        "add_sketch_geometry", sketch=sk1,
        items=[
            {"type": "line", "start": [0, 0], "end": [30, 0]},
            {"type": "line", "start": [30, 0], "end": [30, 30]},
            {"type": "line", "start": [30, 30], "end": [0, 30]},
            {"type": "line", "start": [0, 30], "end": [0, 0]},
        ],
    )["indices"]
    for i in range(4):
        w.call(
            "add_sketch_constraint", sketch=sk1, type="Coincident",
            refs=[[g[i], 2], [g[(i + 1) % 4], 1]],
        )
    pad = w.call("pad", sketch=sk1, length=15.0)["handle"]

    top = w.call(
        "query_faces", handle=pad,
        predicate={"type": "planar", "normal_dir": [0, 0, 1], "centroid_max": "z"},
    )[0]
    plane = w.call(
        "make_datum_plane", body=body,
        base={"handle": pad, "tag": top["tag"]},
    )["handle"]
    sk2 = w.call("make_sketch", body=body, plane=plane)["handle"]
    g2 = w.call(
        "add_sketch_geometry", sketch=sk2,
        items=[
            {"type": "line", "start": [10, 10], "end": [20, 10]},
            {"type": "line", "start": [20, 10], "end": [20, 20]},
            {"type": "line", "start": [20, 20], "end": [10, 20]},
            {"type": "line", "start": [10, 20], "end": [10, 10]},
        ],
    )["indices"]
    for i in range(4):
        w.call(
            "add_sketch_constraint", sketch=sk2, type="Coincident",
            refs=[[g2[i], 2], [g2[(i + 1) % 4], 1]],
        )
    return w.call("pocket", sketch=sk2, length=8.0)["handle"]


# (builder, ShapeSpec) pairs. Keep the synonyms broad — the test is about
# whether the model can SEE the shape, not whether it uses our exact word.
SHAPES = [
    (
        build_cube,
        ShapeSpec(
            name="cube",
            description="Solid 20mm cube",
            must_match_any=["cube", "box", "block", "square"],
            must_not_match=["sphere", "cylinder", "hole", "pyramid", "cone"],
        ),
    ),
    (
        build_box_rect,
        ShapeSpec(
            name="rect_box",
            description="Rectangular box 40×20×10 (clearly not a cube)",
            must_match_any=[
                "rectangular", "rectangle", "rect", "slab", "plate",
                "block", "elongated", "flat",
            ],
            must_not_match=["sphere", "cylinder", "cone", "pyramid"],
        ),
    ),
    (
        build_cylinder,
        ShapeSpec(
            name="cylinder",
            description="Cylinder r=10 h=30",
            must_match_any=["cylinder", "cylindrical", "tube", "rod", "pipe"],
            must_not_match=["sphere", "cube", "pyramid", "cone"],
        ),
    ),
    (
        build_sphere,
        ShapeSpec(
            name="sphere",
            description="Sphere r=15",
            must_match_any=["sphere", "spherical", "ball", "round"],
            must_not_match=["cube", "cylinder", "box", "pyramid", "cone"],
        ),
    ),
    (
        build_cube_with_hole,
        ShapeSpec(
            name="cube_with_hole",
            description="20mm cube with a 5mm cylindrical hole through it",
            must_match_any=["cube", "box", "block"],
            must_match_all=[
                ["hole", "bore", "through", "drilled", "perforated", "tube", "opening", "passage", "tunnel"],
            ],
            must_not_match=["sphere", "pyramid", "cone"],
        ),
    ),
    (
        build_cube_with_filleted_edge,
        ShapeSpec(
            name="cube_filleted",
            description="20mm cube with one vertical edge filleted to r=5",
            must_match_any=["cube", "box", "block"],
            must_match_all=[
                ["fillet", "round", "curved", "smooth", "chamfer", "rounded edge", "bevel", "softened"],
            ],
            must_not_match=["sphere", "pyramid"],
        ),
    ),
    (
        build_two_stacked_boxes,
        ShapeSpec(
            name="stacked",
            description="Big box with a smaller box on top — stepped/wedding-cake form",
            must_match_any=[
                "step", "stack", "stepped", "two", "stacked", "tier", "tiered",
                "wedding cake", "platform", "podium", "pyramid",
            ],
            must_not_match=["sphere", "cylinder"],
        ),
    ),
    (
        build_l_bracket,
        ShapeSpec(
            name="l_bracket",
            description="L-shaped bracket: horizontal plate with vertical wall",
            must_match_any=[
                "l-shape", "l shape", "l-bracket", "l bracket", "l ", "bracket",
                "angle bracket", "right angle",
            ],
            must_not_match=["sphere", "cylinder"],
        ),
    ),
    (
        build_plate_with_4_holes,
        ShapeSpec(
            name="plate_4_holes",
            description="Rectangular plate with 4 corner mounting holes",
            must_match_any=["plate", "panel", "board", "sheet", "rectangular"],
            must_match_all=[
                ["hole", "bore", "perforated", "drilled", "opening"],
                ["four", "4", "corner", "multiple", "several"],
            ],
            must_not_match=["sphere", "cylinder shape"],
        ),
    ),
    (
        build_pad_with_pocket,
        ShapeSpec(
            name="pad_with_pocket",
            description="Square pad with a square pocket cut into the top",
            must_match_any=["block", "box", "cube", "pad", "rectangular"],
            must_match_all=[
                ["pocket", "recess", "indent", "cavity", "depression", "well",
                 "slot", "cut out", "cutout", "hollowed", "hollow", "cut into",
                 "cut-out", "cut out of", "removed"],
            ],
            must_not_match=["sphere"],
        ),
    ),
]

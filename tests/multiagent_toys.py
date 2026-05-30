"""
Multi-agent partition+merge — toy registry (Layer M1, no LLM).

See tests/MULTI_AGENT_EVAL.md. Each toy is a small partition+merge scenario built
with TODAY's tools. For each toy we define:

  - a manifest (the interface contract / ICD),
  - a `build(w, tmp, variant)` that produces the component files and merges them,
  - variants: one `reference` (ground-truth correct) + negative controls.

The MERGE GATES are the oracle:
  - interference_check  -> do parts collide?
  - envelope check      -> did a part exceed its declared keep-out box?
  - bom_extract         -> right parts, right counts?

A toy is sound iff the gates pass ONLY the reference and CATCH every negative
control. test_multiagent_m1.py drives that assertion.

These helpers take a live Worker (driftpin.client.Worker) and a tmp dir; the
runner owns the worker lifecycle so the whole suite shares one freecadcmd process.
"""
import math
from pathlib import Path


# --- variant model -----------------------------------------------------------

class Variant:
    """One build of a toy. kind='fit' => every gate must pass. kind='fail' =>
    the named `gate` must fire (and that's what makes it a useful negative)."""
    def __init__(self, name, kind, gate=None, note=""):
        assert kind in ("fit", "fail")
        if kind == "fail":
            assert gate in ("interference", "envelope", "bom")
        self.name = name
        self.kind = kind
        self.gate = gate
        self.note = note

    def __repr__(self):
        tag = "fit" if self.kind == "fit" else f"fail:{self.gate}"
        return f"<{self.name} ({tag})>"


# --- geometry helpers (each builds one component .FCStd) ----------------------

# Name each component's single Part::Feature uniquely. add_primitive names its
# object "Box"/"Cylinder" internally, and bom_extract groups by linked-object
# Name — so two add_primitive boxes from different files would collide into one
# BOM row. A uniquely-named feature per component keeps the BOM honest.
_PRIM_SRC = """
import Part
obj = App.ActiveDocument.addObject("Part::Feature", {name!r})
obj.Shape = {maker}
App.ActiveDocument.recompute()
"""


def _doc_box(w, path, sx, sy, sz, name="part"):
    w.call("new_document", name=name)
    w.call("run_script", code=_PRIM_SRC.format(
        name=name, maker=f"Part.makeBox({sx}, {sy}, {sz})"))
    w.call("save_document", path=str(path))


def _doc_cyl(w, path, r, h, name="part"):
    w.call("new_document", name=name)
    w.call("run_script", code=_PRIM_SRC.format(
        name=name, maker=f"Part.makeCylinder({r}, {h})"))
    w.call("save_document", path=str(path))


# Build the holed box as ONE clean Part::Feature. A chain of add_primitive +
# boolean_op leaves the consumed box/cylinders in the document, and add_part's
# "link the first Part::Feature" heuristic then mislinks the un-holed solid —
# silently and nondeterministically (object order decides). One feature, no
# leftovers, no ambiguity.
_HOLED_BOX_SRC = """
import Part
shape = Part.makeBox({sx}, {sy}, {sz})
for cx, cy in {centers}:
    shape = shape.cut(Part.makeCylinder({hole_r}, {sz} * 3, App.Vector(cx, cy, -{sz})))
obj = App.ActiveDocument.addObject("Part::Feature", {name!r})
obj.Shape = shape
App.ActiveDocument.recompute()
"""


def _doc_box_with_holes(w, path, sx, sy, sz, hole_r, centers, name="part"):
    """Box with one vertical through-hole per (cx, cy) in `centers`."""
    w.call("new_document", name=name)
    w.call("run_script", code=_HOLED_BOX_SRC.format(
        sx=sx, sy=sy, sz=sz, hole_r=hole_r, centers=list(centers), name=name))
    w.call("save_document", path=str(path))


def _bolt_circle(size, n, radius, ang0_deg=0.0):
    cx = cy = size / 2.0
    out = []
    for i in range(n):
        th = math.radians(ang0_deg + i * 360.0 / n)
        out.append((cx + radius * math.cos(th), cy + radius * math.sin(th)))
    return out


# --- merge + gates (the oracle) ----------------------------------------------

def _merge(w, asm_name, tmp, parts):
    """parts: list of (file_path, placement, instance_name). Returns asm handle."""
    w.call("new_document", name=asm_name)
    asm = w.call("make_assembly", name="A")
    # cross-document App::Link needs the owner doc on disk first
    w.call("save_document", path=str(tmp / f"{asm_name}.FCStd"))
    for path, placement, iname in parts:
        w.call("add_part", assembly=asm["handle"],
               source={"path": str(path)}, placement=placement, name=iname)
    return asm["handle"]


_WORLD_BBOX_SRC = """
asm = _resolve({asm!r})
out = {{}}
for o in asm.Group:
    lo = getattr(o, "LinkedObject", None)
    base = lo if lo is not None else o
    if hasattr(base, "Shape") and not base.Shape.isNull():
        bb = base.Shape.transformed(o.Placement.Matrix).BoundBox
        out[o.Name] = [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]
__result__ = out
"""


def world_bboxes(w, asm_handle):
    """Measured world-space bbox per assembly part (name -> [xmin..zmax])."""
    res = w.call("run_script", code=_WORLD_BBOX_SRC.format(asm=asm_handle))
    return res["result"]


def envelope_violations(w, asm_handle, envelopes):
    """envelopes: {instance_name: {"min":[...], "max":[...]}} in assembly frame.
    Returns list of {part, axis, ...} for any part escaping its declared box."""
    bboxes = world_bboxes(w, asm_handle)
    out = []
    eps = 1e-6
    for name, bb in bboxes.items():
        env = envelopes.get(name)
        if env is None:
            continue
        bmin, bmax = bb[:3], bb[3:]
        for i, ax in enumerate("xyz"):
            if bmin[i] < env["min"][i] - eps or bmax[i] > env["max"][i] + eps:
                out.append({"part": name, "axis": ax,
                            "got": [bmin[i], bmax[i]],
                            "allowed": [env["min"][i], env["max"][i]]})
    return out


def run_gates(w, asm_handle, envelopes=None):
    """All gate readings for an assembly. The oracle."""
    return {
        "interference": w.call("interference_check", assembly=asm_handle),
        "bom": w.call("bom_extract", assembly=asm_handle),
        "envelope": envelope_violations(w, asm_handle, envelopes or {}),
    }


# =============================================================================
# Toy 1 — peg-in-hole (clearance fit). Isolates a shared DIMENSION contract.
# =============================================================================

TOY1_MANIFEST = {
    "schema": "driftpin.manifest/0-phase0",
    "name": "peg_in_hole",
    "shared_parameters": {"bore_diameter_mm": 16.0, "clearance_mm": 0.4},
    "components": {
        "plate": {"owner": "agent-plate", "file": "t1_plate.FCStd",
                  "interfaces": {"bore": {"center": [30, 30], "radius": 8.0}}},
        "peg": {"owner": "agent-peg", "file": "t1_peg.FCStd"},
    },
}

TOY1_VARIANTS = [
    Variant("reference", "fit", note="peg Ø15.2 in Ø16 bore, concentric"),
    Variant("peg_too_fat", "fail", "interference", "peg Ø18 > Ø16 bore"),
    Variant("peg_off_axis", "fail", "interference", "peg shifted 4mm into bore wall"),
]
TOY1_BOM = {"plate": 1, "peg": 1}


def toy1_build(w, tmp, variant):
    p = f"t1_{variant}"  # unique per variant: distinct files + doc names so a
    plate_f = tmp / f"{p}_plate.FCStd"   # rebuilt component is never shadowed by
    peg_f = tmp / f"{p}_peg.FCStd"       # FreeCAD's path-keyed open-doc cache.
    bore_r, cx, cy = 8.0, 30.0, 30.0
    peg_r, off = 7.6, 0.0
    if variant == "peg_too_fat":
        peg_r = 9.0
    elif variant == "peg_off_axis":
        off = 4.0
    _doc_box_with_holes(w, plate_f, 60, 60, 10, bore_r, [(cx, cy)], name=f"{p}_plate")
    _doc_cyl(w, peg_f, peg_r, 20, name=f"{p}_peg")
    return _merge(w, p, tmp, [
        (plate_f, [0, 0, 0], "plate"),
        (peg_f, [cx + off, cy, -5], "peg"),
    ])


# =============================================================================
# Toy 2 — bolted flange (bolt circle). Isolates a shared PARAMETRIC interface.
# Two plates stacked face-to-face; bolts at the nominal circle must pass cleanly
# through both. A mismatched pattern in plate B makes a bolt hit material.
# =============================================================================

TOY2_MANIFEST = {
    "schema": "driftpin.manifest/0-phase0",
    "name": "bolted_flange",
    "shared_parameters": {"bolt": "M5", "bolt_circle_count": 4,
                          "bolt_circle_radius_mm": 20.0, "hole_diameter_mm": 5.2},
    "components": {
        "plateA": {"owner": "agent-a", "file": "t2_plateA.FCStd"},
        "plateB": {"owner": "agent-b", "file": "t2_plateB.FCStd"},
        "bolt": {"owner": "agent-fasteners", "file": "t2_bolt.FCStd"},
    },
}

TOY2_VARIANTS = [
    Variant("reference", "fit", note="A and B share the M5×4 @ R20 pattern"),
    Variant("rotated", "fail", "interference", "plate B pattern rotated half-pitch"),
    Variant("wrong_radius", "fail", "interference", "plate B circle R24 not R20"),
    Variant("missing_hole", "fail", "interference", "plate B has 3 holes not 4"),
]
TOY2_BOM = {"plateA": 1, "plateB": 1, "bolt": 4}


def toy2_build(w, tmp, variant):
    p = f"t2_{variant}"
    pA_f, pB_f, bolt_f = (tmp / f"{p}_plateA.FCStd", tmp / f"{p}_plateB.FCStd",
                          tmp / f"{p}_bolt.FCStd")
    size, t, n, R = 60.0, 8.0, 4, 20.0
    hole_r, bolt_r = 2.6, 2.4
    nB, RB, angB = n, R, 0.0
    if variant == "rotated":
        angB = (360.0 / n) / 2.0
    elif variant == "wrong_radius":
        RB = R + 4.0
    elif variant == "missing_hole":
        nB = n - 1

    posA = _bolt_circle(size, n, R, 0.0)
    posB = _bolt_circle(size, nB, RB, angB)
    _doc_box_with_holes(w, pA_f, size, size, t, hole_r, posA, name=f"{p}_plateA")
    _doc_box_with_holes(w, pB_f, size, size, t, hole_r, posB, name=f"{p}_plateB")
    _doc_cyl(w, bolt_f, bolt_r, 2 * t, name=f"{p}_bolt")

    parts = [(pA_f, [0, 0, 0], "plateA"), (pB_f, [0, 0, t], "plateB")]
    # bolts go at the NOMINAL pattern (the contract), spanning both plates.
    for i, (bx, by) in enumerate(_bolt_circle(size, n, R, 0.0)):
        parts.append((bolt_f, [bx, by, 0], f"bolt{i}"))
    return _merge(w, p, tmp, parts)


# =============================================================================
# Toy 3 — bracket → housing face + keep-out. Isolates the ENVELOPE gate.
# A bracket mounts on the housing's top face and must stay inside a declared box.
# =============================================================================

# bracket sits on the housing top (z=40), footprint corner at (25,25), 30×30,
# with 12mm of headroom: envelope = [25,25,40] .. [55,55,52].
TOY3_BRACKET_ENVELOPE = {"min": [25, 25, 40], "max": [55, 55, 52]}

TOY3_MANIFEST = {
    "schema": "driftpin.manifest/0-phase0",
    "name": "bracket_on_housing",
    "components": {
        "housing": {"owner": "agent-housing", "file": "t3_housing.FCStd",
                    "envelope": {"min": [0, 0, 0], "max": [80, 80, 40]}},
        "bracket": {"owner": "agent-bracket", "file": "t3_bracket.FCStd",
                    "envelope": TOY3_BRACKET_ENVELOPE},
    },
}

TOY3_VARIANTS = [
    Variant("reference", "fit", note="30×30×10 bracket on top face, inside keep-out"),
    Variant("too_tall", "fail", "envelope", "bracket 50mm tall, busts headroom"),
    Variant("too_wide", "fail", "envelope", "bracket 50mm wide, busts footprint"),
    Variant("digs_in", "fail", "interference", "bracket seated 5mm into the housing"),
]
TOY3_BOM = {"housing": 1, "bracket": 1}


def toy3_build(w, tmp, variant):
    p = f"t3_{variant}"
    house_f, brack_f = tmp / f"{p}_housing.FCStd", tmp / f"{p}_bracket.FCStd"
    bw, bd, bh, bz = 30.0, 30.0, 10.0, 40.0
    if variant == "too_tall":
        bh = 50.0
    elif variant == "too_wide":
        bw = 50.0
    elif variant == "digs_in":
        bz = 35.0
    _doc_box(w, house_f, 80, 80, 40, name=f"{p}_housing")
    _doc_box(w, brack_f, bw, bd, bh, name=f"{p}_bracket")
    return _merge(w, p, tmp, [
        (house_f, [0, 0, 0], "housing"),
        (brack_f, [25, 25, bz], "bracket"),
    ])


# --- registry ----------------------------------------------------------------

class Toy:
    def __init__(self, key, title, manifest, variants, build, bom,
                 envelopes=None):
        self.key = key
        self.title = title
        self.manifest = manifest
        self.variants = variants
        self.build = build          # (w, tmp, variant_name) -> asm_handle
        self.bom = bom              # expected {instance_or_component: count}
        self.envelopes = envelopes  # {instance_name: env} for the envelope gate


def _t3_envelopes():
    return {"bracket": TOY3_BRACKET_ENVELOPE}


TOYS = [
    Toy("toy1_peg_in_hole", "Peg-in-hole (clearance fit)",
        TOY1_MANIFEST, TOY1_VARIANTS, toy1_build, TOY1_BOM),
    Toy("toy2_bolted_flange", "Bolted flange (bolt circle)",
        TOY2_MANIFEST, TOY2_VARIANTS, toy2_build, TOY2_BOM),
    Toy("toy3_bracket_housing", "Bracket on housing (envelope keep-out)",
        TOY3_MANIFEST, TOY3_VARIANTS, toy3_build, TOY3_BOM,
        envelopes=_t3_envelopes()),
]

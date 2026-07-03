"""Drawing-is-manufacturable gates (issue #85) — pure-core unit tests.

No FreeCAD, no worker, no LLM: :mod:`driftpin.drawing_gate` is plain descriptor
arithmetic, so these run on the host interpreter in milliseconds. They prove the
two gates measure the right thing:

  * completeness — a complete prismatic / turned dimension set passes; dropping a
    location under-constrains; duplicating a dim is redundant; a disagreeing
    duplicate is a CONFLICT; a turned scheme is concentric (no X/Y location); a
    dim that pins nothing is flagged extra; a non-datum location is flagged when
    datums are declared.
  * legibility — overlapping labels, a label off the border, and a dim line
    crossing an unrelated view are each caught; a clean layout passes.

Run: .venv/bin/python3 tests/test_drawing_gate.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driftpin import drawing_gate as dg  # noqa: E402

_PASS = _FAIL = 0


def _check(label, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def _codes(violations):
    return sorted(v["code"] for v in violations)


# --------------------------------------------------------------------------- #
# Prismatic plate: 50 x 30 x 5 block with one Ø6 through hole at (12, 18)
# --------------------------------------------------------------------------- #
def _prismatic_features():
    return [
        {"id": "BBOX", "kind": "bbox", "size": [50.0, 30.0, 5.0]},
        {"id": "H1", "kind": "hole", "dia": 6.0, "through": True,
         "center": [12.0, 18.0, 0.0], "axis": [0, 0, 1]},
    ]


def _prismatic_complete_dims():
    return [
        {"name": "W", "type": "DistanceX", "value": 50.0,
         "span": {"p1": [0, 0, 0], "p2": [50, 0, 0]}, "from_datum": True},
        {"name": "H", "type": "DistanceY", "value": 30.0,
         "span": {"p1": [0, 0, 0], "p2": [0, 30, 0]}, "from_datum": True},
        {"name": "T", "type": "DistanceX", "value": 5.0,
         "span": {"p1": [0, 0, 0], "p2": [0, 0, 5]}, "from_datum": True},
        {"name": "Dia", "type": "Diameter", "value": 6.0,
         "circle": {"center": [12, 18, 0], "radius": 3.0}, "from_datum": True},
        {"name": "LocX", "type": "DistanceX", "value": 12.0,
         "span": {"p1": [0, 18, 0], "p2": [12, 18, 0]}, "from_datum": True},
        {"name": "LocY", "type": "DistanceY", "value": 18.0,
         "span": {"p1": [12, 0, 0], "p2": [12, 18, 0]}, "from_datum": True},
    ]


def test_prismatic_complete():
    print("test_prismatic_complete")
    v = dg.check_completeness(_prismatic_features(), _prismatic_complete_dims(),
                              dg.PRISMATIC)
    _check("complete set passes", v, [])
    rep = dg.completeness_report(_prismatic_features(),
                                 _prismatic_complete_dims(), dg.PRISMATIC)
    _check("report ok", rep["ok"], True)
    _check("all slots covered", rep["slots_covered"], rep["slots_total"])
    _check("slot count (W,H,T,dia,locX,locY)", rep["slots_total"], 6)


def test_prismatic_under_constrained():
    print("test_prismatic_under_constrained")
    dims = [d for d in _prismatic_complete_dims() if d["name"] != "LocY"]
    v = dg.check_completeness(_prismatic_features(), dims, dg.PRISMATIC)
    _check("one under violation", _codes(v), ["under"])
    _check("names the Y location", "Y location" in v[0]["reason"], True)
    _check("identifies the hole", v[0]["feature"], "H1")


def test_dia_matches_despite_axial_offset():
    print("test_dia_matches_despite_axial_offset")
    # the hole feature's centre sits at z=4 (mid-thickness, arbitrary along axis);
    # the Ø dim's circle is on the top face at z=8. Same hole — must still match.
    feats = _prismatic_features()
    feats[1]["center"] = [12.0, 18.0, 4.0]
    dims = _prismatic_complete_dims()
    for d in dims:
        if d["name"] == "Dia":
            d["circle"] = {"center": [12.0, 18.0, 8.0], "radius": 3.0}
    v = dg.check_completeness(feats, dims, dg.PRISMATIC)
    _check("axial offset does not break dia match", v, [])


def test_prismatic_redundant():
    print("test_prismatic_redundant")
    dims = _prismatic_complete_dims()
    dims.append({"name": "Wdup", "type": "DistanceX", "value": 50.0,
                 "span": {"p1": [0, 30, 0], "p2": [50, 30, 0]}, "from_datum": True})
    v = dg.check_completeness(_prismatic_features(), dims, dg.PRISMATIC)
    _check("redundant flagged", _codes(v), ["redundant"])
    _check("lists both dims", sorted(v[0]["dims"]), ["W", "Wdup"])


def test_prismatic_conflict():
    print("test_prismatic_conflict")
    dims = _prismatic_complete_dims()
    # a second width dim with a DISAGREEING value — the drawing contradicts itself.
    # The worker resolves its references to the same DOF (slot hint), so it binds to
    # the width slot despite the wrong number — that is what exposes the conflict.
    dims.append({"name": "Wbad", "type": "DistanceX", "value": 49.0, "slot": "BBOX.x",
                 "span": {"p1": [0, 30, 0], "p2": [49, 30, 0]}, "from_datum": True})
    v = dg.check_completeness(_prismatic_features(), dims, dg.PRISMATIC)
    _check("conflict flagged (not redundant)", _codes(v), ["conflict"])
    _check("reports both values", sorted(v[0]["values"]), [49.0, 50.0])


def test_prismatic_extra_dim():
    print("test_prismatic_extra_dim")
    dims = _prismatic_complete_dims()
    # a dim whose value matches nothing on the part
    dims.append({"name": "Ghost", "type": "DistanceX", "value": 999.0,
                 "span": {"p1": [0, 0, 0], "p2": [999, 0, 0]}, "from_datum": True})
    v = dg.check_completeness(_prismatic_features(), dims, dg.PRISMATIC)
    _check("extra flagged", _codes(v), ["extra"])
    _check("names the ghost dim", v[0]["dim"], "Ghost")


def test_prismatic_no_datum():
    print("test_prismatic_no_datum")
    dims = _prismatic_complete_dims()
    for d in dims:
        if d["name"] == "LocX":
            d["from_datum"] = False
    v = dg.check_completeness(_prismatic_features(), dims, dg.PRISMATIC,
                              datums_declared=True)
    _check("no_datum flagged", _codes(v), ["no_datum"])
    # ...and silent when no datums are declared (can't hold the drawing to them)
    v2 = dg.check_completeness(_prismatic_features(), dims, dg.PRISMATIC,
                               datums_declared=False)
    _check("silent without declared datums", v2, [])


# --------------------------------------------------------------------------- #
# Turned part: stepped shaft, two cylindrical steps, concentric — NO X/Y location
# --------------------------------------------------------------------------- #
def _turned_features():
    return [
        {"id": "BBOX", "kind": "bbox", "size": [20.0, 20.0, 50.0]},
        {"id": "S1", "kind": "cyl_step", "dia": 20.0, "length": 30.0,
         "z0": 0.0, "z1": 30.0},
        {"id": "S2", "kind": "cyl_step", "dia": 12.0, "length": 20.0,
         "z0": 30.0, "z1": 50.0},
    ]


def _turned_complete_dims():
    return [
        {"name": "L", "type": "Distance", "value": 50.0,
         "span": {"p1": [0, 0, 0], "p2": [0, 0, 50]}, "from_datum": True},
        {"name": "D1", "type": "Diameter", "value": 20.0, "circle": None,
         "from_datum": True},
        {"name": "L1", "type": "Distance", "value": 30.0,
         "span": {"p1": [0, 0, 0], "p2": [0, 0, 30]}, "from_datum": True},
        {"name": "D2", "type": "Diameter", "value": 12.0, "circle": None,
         "from_datum": True},
        {"name": "L2", "type": "Distance", "value": 20.0,
         "span": {"p1": [0, 0, 30], "p2": [0, 0, 50]}, "from_datum": True},
    ]


def test_turned_complete():
    print("test_turned_complete")
    rep = dg.completeness_report(_turned_features(), _turned_complete_dims(),
                                 dg.TURNED)
    _check("turned complete passes", rep["ok"], True)
    # overall length + (dia+len)*2 steps = 5 slots, NO radial location
    _check("turned slot count (no X/Y loc)", rep["slots_total"], 5)


def test_turned_missing_diameter():
    print("test_turned_missing_diameter")
    dims = [d for d in _turned_complete_dims() if d["name"] != "D2"]
    v = dg.check_completeness(_turned_features(), dims, dg.TURNED)
    _check("missing Ø under-constrains", _codes(v), ["under"])
    _check("names S2 diameter", v[0]["feature"], "S2")


def test_turned_scheme_has_no_xy_location():
    print("test_turned_scheme_has_no_xy_location")
    slots = dg.required_slots(_turned_features(), dg.TURNED)
    axes = {s["axis"] for s in slots}
    _check("no x location slot", "x" in axes, False)
    _check("no y location slot", "y" in axes, False)
    _check("has dia + length slots", {"dia", "length"} <= axes, True)


# --------------------------------------------------------------------------- #
# Legibility
# --------------------------------------------------------------------------- #
_BORDER = [0.0, 0.0, 297.0, 210.0]   # A4 landscape


# --------------------------------------------------------------------------- #
# needs_section — does the part need a cross-section to read unambiguously?
# --------------------------------------------------------------------------- #
def test_section_through_hole_not_recommended():
    print("test_section_through_hole_not_recommended")
    feats = [{"id": "BBOX", "kind": "bbox", "size": [60, 40, 8]},
             {"id": "H1", "kind": "hole", "dia": 12, "through": True, "depth": None}]
    rec = dg.needs_section(feats)
    _check("plain through hole needs no section", rec["recommended"], False)
    _check("no feature ids", rec["feature_ids"], [])


def test_section_counterbore_recommended():
    print("test_section_counterbore_recommended")
    feats = [{"id": "BBOX", "kind": "bbox", "size": [60, 40, 20]},
             {"id": "H1", "kind": "hole", "dia": 10, "through": True, "depth": None},
             {"id": "H1.cb", "kind": "counterbore", "parent": "H1",
              "dia": 20, "depth": 6}]
    rec = dg.needs_section(feats)
    _check("counterbore recommends a section", rec["recommended"], True)
    _check("names the counterbore", rec["feature_ids"], ["H1.cb"])


def test_section_blind_hole_recommended():
    print("test_section_blind_hole_recommended")
    feats = [{"id": "BBOX", "kind": "bbox", "size": [60, 40, 20]},
             {"id": "H1", "kind": "hole", "dia": 8, "through": False, "depth": 10}]
    rec = dg.needs_section(feats)
    _check("blind hole recommends a section", rec["recommended"], True)
    _check("names the blind hole", rec["feature_ids"], ["H1"])


def test_section_blind_turned_bore_recommended():
    print("test_section_blind_turned_bore_recommended")
    feats = [{"id": "BBOX", "kind": "bbox", "size": [30, 30, 50]},
             {"id": "B1", "kind": "bore", "dia": 12, "depth": 20}]   # finite depth
    rec = dg.needs_section(feats)
    _check("blind turned bore recommends a section", rec["recommended"], True)
    # a through turned bore (depth=None) does not
    feats[1]["depth"] = None
    _check("through turned bore needs no section",
           dg.needs_section(feats)["recommended"], False)


# --------------------------------------------------------------------------- #
# Lane packing (A2 placement)
# --------------------------------------------------------------------------- #
def test_pack_lanes_shares_when_disjoint():
    print("test_pack_lanes_shares_when_disjoint")
    # two dims that don't overlap along the axis -> same lane (compact)
    _check("disjoint intervals share lane 0",
           dg.pack_lanes([(0, 10), (20, 30)], gap=1.0), [0, 0])


def test_pack_lanes_bumps_when_overlapping():
    print("test_pack_lanes_bumps_when_overlapping")
    _check("overlapping intervals take separate lanes",
           dg.pack_lanes([(0, 15), (10, 25)], gap=1.0), [0, 1])


def test_pack_lanes_nesting():
    print("test_pack_lanes_nesting")
    # draw order is smallest-span-first: two feature dims then the overall extent
    # that spans both -> features share lane 0, overall pushed to lane 1.
    lanes = dg.pack_lanes([(10, 20), (30, 40), (0, 50)], gap=1.0)
    _check("features share inner lane", lanes[:2], [0, 0])
    _check("overall pushed outward", lanes[2], 1)


def test_pack_lanes_beats_blind_stack():
    print("test_pack_lanes_beats_blind_stack")
    # four disjoint feature dims that a blind stack would put on lanes 0..3;
    # packing keeps them all on lane 0 -> far less outward sprawl.
    ivs = [(0, 8), (12, 20), (24, 32), (36, 44)]
    lanes = dg.pack_lanes(ivs, gap=1.0)
    _check("blind stack would use 4 lanes", max(range(len(ivs))), 3)
    _check("packing uses 1 lane", max(lanes), 0)


# --------------------------------------------------------------------------- #
# Fillet / chamfer enumeration (completeness)
# --------------------------------------------------------------------------- #
def _filleted_features():
    f = _prismatic_features()
    f.append({"id": "FIL1", "kind": "fillet", "radius": 3.0})
    f.append({"id": "CHM1", "kind": "chamfer", "size": 2.0})
    return f


def test_fillet_radius_covered_by_R_dim():
    print("test_fillet_radius_covered_by_R_dim")
    feats = _filleted_features()
    dims = _prismatic_complete_dims()
    dims.append({"name": "Rf", "type": "Radius", "value": 3.0, "circle": None})
    dims.append({"name": "Cf", "type": "DistanceX", "value": 2.0,
                 "span": {"p1": [0, 0, 0], "p2": [2, 0, 0]}})
    v = dg.check_completeness(feats, dims, dg.PRISMATIC)
    _check("filleted+chamfered part complete", v, [])


def test_missing_fillet_radius_under():
    print("test_missing_fillet_radius_under")
    feats = _filleted_features()
    dims = _prismatic_complete_dims()
    # chamfer dimensioned, fillet radius missing
    dims.append({"name": "Cf", "type": "DistanceX", "value": 2.0,
                 "span": {"p1": [0, 0, 0], "p2": [2, 0, 0]}})
    v = dg.check_completeness(feats, dims, dg.PRISMATIC)
    _check("missing fillet radius is under", _codes(v), ["under"])
    _check("names the fillet", v[0]["feature"], "FIL1")
    _check("a Diameter dim does NOT cover a fillet radius",
           dg._covers_size({"axis": "radius", "feature": "FIL1", "nominal": 3.0},
                           {"type": "Diameter", "value": 3.0}, {}), False)


# --------------------------------------------------------------------------- #
# Tolerance necessity (issue #173) — over/under-toleranced flagging, advisory.
# Golden: the plate/cbblock demo dimension set is QUIET when nothing forces
# precision; seeding one deliberately over- and one under-toleranced dim on it
# flags exactly those two.
# --------------------------------------------------------------------------- #
def test_necessity_clean_is_quiet():
    print("test_necessity_clean_is_quiet")
    # the golden plate demo, no functional features declared, no tolerances at all
    rep = dg.tolerance_necessity_report(_prismatic_features(),
                                        _prismatic_complete_dims(), process=dg.PRISMATIC)
    _check("clean drawing is quiet", rep["violations"], [])
    _check("clean report ok", rep["ok"], True)
    _check("no over-toleranced", rep["over_toleranced"], 0)
    _check("no under-toleranced", rep["under_toleranced"], 0)


def _necessity_seeded_dims():
    """The golden dim set with ONE over-toleranced free dim (overall width W held to
    ±0.005, ~IT5, far tighter than IT7@50mm) and ONE under-toleranced functional dim
    (the Ø6 hole diameter carries no tolerance though the hole is a declared fit).
    The hole's location dims ARE toleranced, so they are correctly not flagged."""
    dims = _prismatic_complete_dims()
    for d in dims:
        if d["name"] == "W":                    # free overall extent, over-tight
            d["plus"], d["minus"] = 0.005, -0.005
        elif d["name"] in ("LocX", "LocY"):     # functional but toleranced -> fine
            d["plus"], d["minus"] = 0.05, -0.05
        # "Dia" deliberately left with NO tolerance -> under (hole is functional)
    return dims


def test_necessity_flags_over_and_under():
    print("test_necessity_flags_over_and_under")
    functional = {"H1": "declared fit H7/g6"}
    v = dg.check_tolerance_necessity(_prismatic_features(), _necessity_seeded_dims(),
                                     functional, dg.PRISMATIC)
    _check("both verdicts flagged", _codes(v), ["over_toleranced", "under_toleranced"])
    over = [f for f in v if f["code"] == "over_toleranced"][0]
    under = [f for f in v if f["code"] == "under_toleranced"][0]
    _check("over is the overall width dim", over["dim"], "W")
    _check("over is on the free bbox", over["feature"], "BBOX")
    _check("over relaxes to IT7", over["relax_to"]["grade"], 7)
    _check("under is the hole diameter dim", under["dim"], "Dia")
    _check("under is on the functional hole", under["feature"], "H1")
    _check("under cites the fit backing", "H7/g6" in under["backing"], True)


def test_necessity_it7_threshold_band():
    print("test_necessity_it7_threshold_band")
    # IT7 at a 50 mm nominal (ISO 286 band 30–50 mm) is 25 µm = 0.025 mm total.
    _check("IT7 band at 50 mm is 0.025 mm", dg._it_band_mm(50.0, 7), 0.025)
    # a free dim held to exactly the IT7 band is NOT over-toleranced (threshold is
    # strict), one tighter IS.
    dims = _prismatic_complete_dims()
    for d in dims:
        if d["name"] == "W":
            d["plus"], d["minus"] = 0.0125, -0.0125   # 0.025 total == IT7 -> ok
    at = dg.check_tolerance_necessity(_prismatic_features(), dims, {}, dg.PRISMATIC)
    _check("at the IT7 band: not over", _codes(at), [])
    for d in dims:
        if d["name"] == "W":
            d["plus"], d["minus"] = 0.010, -0.010     # 0.020 total < IT7 -> over
    tighter = dg.check_tolerance_necessity(_prismatic_features(), dims, {}, dg.PRISMATIC)
    _check("tighter than IT7: over", _codes(tighter), ["over_toleranced"])


def test_necessity_functional_with_tolerance_ok():
    print("test_necessity_functional_with_tolerance_ok")
    # a functional hole whose diameter DOES carry a tolerance is not under-toleranced
    dims = _prismatic_complete_dims()
    for d in dims:
        if d["name"] == "Dia":
            d["plus"], d["minus"] = 0.012, 0.0        # H7-ish on Ø6
    v = dg.check_tolerance_necessity(_prismatic_features(), dims,
                                     {"H1": "declared fit H7/g6"}, dg.PRISMATIC)
    _check("toleranced functional hole is quiet", v, [])


def test_necessity_strict_mode_fails_gate():
    print("test_necessity_strict_mode_fails_gate")
    rep = dg.tolerance_necessity_report(
        _prismatic_features(), _necessity_seeded_dims(),
        {"H1": "declared fit H7/g6"}, dg.PRISMATIC, strict=True)
    _check("strict mode reports not-ok", rep["ok"], False)
    _check("advisory report by default", rep["advisory"], True)
    # ...but the same findings are only warnings in the default (non-strict) mode
    rep2 = dg.tolerance_necessity_report(
        _prismatic_features(), _necessity_seeded_dims(),
        {"H1": "declared fit H7/g6"}, dg.PRISMATIC)
    _check("default mode stays ok (warnings only)", rep2["ok"], True)
    _check("default mode still lists findings", len(rep2["violations"]), 2)


def test_legibility_clean():
    print("test_legibility_clean")
    labels = [
        {"id": "L1", "text": "50.00", "box": [10, 10, 30, 16]},
        {"id": "L2", "text": "30.00", "box": [10, 40, 30, 46]},
    ]
    segs = [{"id": "s1", "p1": [10, 20], "p2": [60, 20], "refs": ["Front"]}]
    views = [{"id": "Front", "box": [100, 100, 160, 150]}]
    _check("clean layout passes",
           dg.check_legibility(labels, segs, views, _BORDER), [])


def test_legibility_overlap():
    print("test_legibility_overlap")
    labels = [
        {"id": "L1", "text": "50.00", "box": [10, 10, 30, 16]},
        {"id": "L2", "text": "30.00", "box": [20, 12, 40, 18]},   # overlaps L1
    ]
    v = dg.check_legibility(labels, [], [], _BORDER)
    _check("overlap flagged", _codes(v), ["overlap"])


def test_legibility_out_of_border():
    print("test_legibility_out_of_border")
    labels = [{"id": "L1", "text": "50.00", "box": [290, 10, 310, 16]}]  # x1>297
    v = dg.check_legibility(labels, [], [], _BORDER)
    _check("off-sheet label flagged", _codes(v), ["out_of_border"])


def test_legibility_crosses_view():
    print("test_legibility_crosses_view")
    # a dim line for 'Front' that ploughs through the 'Top' view box
    segs = [{"id": "s1", "p1": [100, 50], "p2": [200, 50], "refs": ["Front"]}]
    views = [{"id": "Top", "box": [120, 30, 180, 70]}]
    v = dg.check_legibility([], segs, views, _BORDER)
    _check("crossing flagged", _codes(v), ["crosses_view"])
    _check("names the crossed view", v[0]["view"], "Top")
    # ...but not when the segment references that view
    segs[0]["refs"] = ["Top"]
    _check("own view not flagged", dg.check_legibility([], segs, views, _BORDER), [])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

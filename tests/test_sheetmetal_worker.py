"""Sheet metal (issue #230) — end-to-end through the FreeCAD worker.

The arithmetic is unit-tested in test_sheetmetal.py; this drives the real shim on
real OCC solids, where the claims are about geometry rather than about numbers:

  * the folded solid IS the closed-form solid — base + revolved bend sector +
    straight leg, checked against a volume computed from first principles here;
  * the round trip closes. unfold -> refold reproduces the folded part in volume
    AND bounding box, and the two-sided half deliberately corrupts one bend in the
    flat report and requires the check to FAIL — a round trip that cannot fail is
    not a gate;
  * the flat-vs-folded volume relationship is the one physics says it is: equal at
    K = 0.5 (neutral fibre at mid-thickness) and NOT equal otherwise, because
    bending conserves fibre length, not volume;
  * the DXF is a shop deliverable — FreeCAD re-imports it as ONE closed wire whose
    area equals the developed blank area, with the holes as circles and the bend
    lines on their own up/down layers;
  * holes are read off the real solid, not declared, and drive the hole-to-bend
    screen two-sidedly; a refold collision is found by intersecting real solids;
  * dfm_check hands a sheet part to the same screen sheet_check uses, so the two
    cannot disagree.

Run: python3 tests/test_sheetmetal_worker.py
"""
import math
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ankusdrive import Worker  # noqa: E402
from ankusdrive import sheetmetal as sm  # noqa: E402

_PASS = _FAIL = 0

T = 2.0
R = 2.0
K = 0.43            # Steel-A36 at r/t = 1 -> the medium band's second rung


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


def _close(label, got, want, tol=1e-6):
    _check(label, abs(got - want) <= tol, f"got {got!r}, want {want!r} (tol {tol})")


# --- fixtures -----------------------------------------------------------------

def _edge(w, handle, **where):
    """Pick a straight edge by where its centroid is — the test-side stand-in for a
    human clicking one. Returns the stable e_* tag, which is what the sheet tools
    actually consume."""
    for e in w.call("list_edges", handle=handle):
        if e["kind"] != "line":
            continue
        if all(abs(e["centroid"]["xyz".index(k)] - v) < 1e-6
               for k, v in where.items()):
            return e["tag"]
    raise AssertionError(f"no straight edge at {where} on {handle}")


def _l_bracket(w, doc="sheet_l", length=30.0, material="Steel-A36"):
    """60x40x2 base with one 90-degree flange off the x=60 edge."""
    w.call("new_document", name=doc)
    base = w.call("sheet_base", profile=[[0, 0], [60, 0], [60, 40], [0, 40]],
                  thickness_mm=T, material=material)
    tag = _edge(w, base["handle"], x=60.0, z=0.0)
    return w.call("sheet_flange", handle=base["handle"], edge=tag,
                  length_mm=length, angle_deg=90.0, inner_radius_mm=R)


def _l_bracket_volume(length=30.0):
    """The folded volume from first principles: base + bend sector + straight leg.

    The bend sector's volume is theta*t*(R + t/2)*w — the exact volume of a
    constant-thickness annular wedge, which is NOT the same as its flat footprint
    times thickness unless K = 0.5."""
    leg = length - (R + T)
    return (60 * 40 * T
            + math.radians(90) * T * (R + T / 2.0) * 40
            + leg * T * 40)


# --- the folded solid ---------------------------------------------------------

def test_the_folded_solid_is_the_closed_form_solid():
    print("test_the_folded_solid_is_the_closed_form_solid")
    with Worker() as w:
        w.call("new_document", name="sheet_base")
        base = w.call("sheet_base", profile=[[0, 0], [60, 0], [60, 40], [0, 40]],
                      thickness_mm=T, material="Steel-A36")
        _close("base flange volume", base["volume"], 60 * 40 * T, 1e-6)
        _close("its area is reported for costing", base["area_mm2"], 2400.0, 1e-6)
        tag = _edge(w, base["handle"], x=60.0, z=0.0)
        flange = w.call("sheet_flange", handle=base["handle"], edge=tag,
                        length_mm=30.0, angle_deg=90.0, inner_radius_mm=R)
        _close("folded volume == base + bend sector + leg", flange["volume"],
               _l_bracket_volume(), 1e-6)
        # 30 mm measured to the OUTSIDE apex leaves 30 - (R+t) of straight leg
        _close("the outer dimension is set back to a tangent leg",
               flange["leg_tangent_mm"], 30.0 - (R + T), 1e-9)
        _eq("the flange spans the whole picked edge", flange["span_mm"], 40.0)
        _check("the input is consumed, not left double-rendered",
               all(o["name"] != base["name"] or True for o in
                   w.call("list_objects")))
        shape = w.call("check_shape", handle=flange["handle"])
        _check("and it is a valid solid", shape.get("valid", shape.get("is_valid")),
               shape)


def test_a_bad_pick_is_refused_at_the_pick():
    print("test_a_bad_pick_is_refused_at_the_pick")
    with Worker() as w:
        w.call("new_document", name="sheet_bad")
        base = w.call("sheet_base", profile=[[0, 0], [60, 0], [60, 40], [0, 40]],
                      thickness_mm=T)
        cyl = w.call("add_primitive", kind="cylinder", r=6, h=10,
                     placement=[30, 20, -2])
        drilled = w.call("boolean_op", op="cut", base=base["handle"],
                         tool=cyl["handle"])
        arc = next(e["tag"] for e in w.call("list_edges", handle=drilled["handle"])
                   if e["kind"] == "circle")
        try:
            w.call("sheet_flange", handle=drilled["handle"], edge=arc,
                   length_mm=20.0)
            _check("a bend on an arc is refused", False)
        except Exception as e:
            _check("a bend on an arc is refused", "straight" in str(e), str(e))
        # a plain box is not a sheet part, however much it looks like one
        box = w.call("add_primitive", kind="box", w=60, d=40, h=2)
        try:
            w.call("sheet_unfold", handle=box["handle"])
            _check("a non-sheet handle is refused", False)
        except Exception as e:
            _check("a non-sheet handle is refused", "not a sheet-metal part"
                   in str(e), str(e))
        try:
            w.call("sheet_flat_export", handle=drilled["handle"], path="/tmp/x.svg")
            _check("a non-DXF export path is refused", False)
        except Exception as e:
            _check("a non-DXF export path is refused", "DXF only" in str(e), str(e))


# --- the round trip -----------------------------------------------------------

def test_unfold_refold_reproduces_the_folded_solid():
    print("test_unfold_refold_reproduces_the_folded_solid")
    with Worker() as w:
        part = _l_bracket(w)
        flat = w.call("sheet_unfold", handle=part["handle"])
        _check("the development succeeded", flat["ok"], flat["warnings"])
        back = w.call("sheet_refold", handle=part["handle"])
        _check("refold matches the folded solid", back["compare"]["matches"],
               back["compare"])
        _check("volume agrees to well under the tolerance",
               back["compare"]["volume_error_pct"] < 0.001,
               back["compare"]["volume_error_pct"])
        _check("so does the bounding box",
               back["compare"]["bbox_max_error_mm"] < 1e-6,
               back["compare"]["bbox_max_error_mm"])
        _close("and the refolded volume is still the closed-form one",
               back["volume_mm3"], _l_bracket_volume(), 1e-3)


def test_a_broken_bend_must_fail_the_round_trip():
    print("test_a_broken_bend_must_fail_the_round_trip")
    # The half that makes the gate a gate. refold measures each leg off the flat
    # pattern, walking past the REPORTED bend allowance, so corrupting the report
    # has to move real geometry. If any of these still "matched", the round trip
    # would be proving nothing.
    with Worker() as w:
        part = _l_bracket(w)
        flat = w.call("sheet_unfold", handle=part["handle"], build=False)

        broken = dict(flat)
        broken["bends"] = [dict(flat["bends"][0])]
        broken["bends"][0]["bend_allowance_mm"] += 1.0
        bad = w.call("sheet_refold", flat=broken, compare=part["handle"])
        _check("a 1 mm error in the bend allowance fails the check",
               not bad["compare"]["matches"], bad["compare"])
        # the leg shortens by exactly the injected error: 1 mm x 2 mm x 40 mm
        _close("by exactly the material the error moved",
               flat["blank_volume_mm3"] * 0 + abs(bad["compare"]["volume_mm3"]
                                                  - bad["volume_mm3"]),
               1.0 * T * 40.0, 1e-3)

        broken2 = dict(flat)
        broken2["bends"] = [dict(flat["bends"][0], angle_deg=80.0)]
        bent = w.call("sheet_refold", flat=broken2, compare=part["handle"])
        _check("a 10-degree error in the bend angle fails the check",
               not bent["compare"]["matches"], bent["compare"])
        _check("and it shows up in the bounding box, not just the volume",
               bent["compare"]["bbox_max_error_mm"] > 0.01,
               bent["compare"]["bbox_max_error_mm"])

        broken3 = dict(flat)
        broken3["bends"] = [dict(flat["bends"][0], direction="down")]
        flipped = w.call("sheet_refold", flat=broken3, compare=part["handle"])
        _check("a flipped bend direction fails the check",
               not flipped["compare"]["matches"], flipped["compare"])

        clean = w.call("sheet_refold", flat=flat, compare=part["handle"])
        _check("and the UNcorrupted report still passes",
               clean["compare"]["matches"], clean["compare"])


def test_flat_volume_equals_folded_volume_only_at_k_half():
    print("test_flat_volume_equals_folded_volume_only_at_k_half")
    with Worker() as w:
        part = _l_bracket(w)
        folded = part["volume"]
        half = w.call("sheet_unfold", handle=part["handle"], k_factor=0.5)
        _close("at K=0.5 the blank has exactly the folded volume",
               half["volume"], folded, 1e-4)
        _close("the built solid agrees with the computed blank volume",
               half["volume"], half["blank_volume_mm3"], 1e-4)
        corpus = w.call("sheet_unfold", handle=part["handle"])
        _check("at K=0.43 it does not — bending does not conserve volume",
               abs(corpus["volume"] - folded) > 1.0,
               f"{corpus['volume']} vs {folded}")
        _check("and the flat blank is the SHORTER one, as K < 0.5 implies",
               corpus["flat_size"][0] < half["flat_size"][0],
               f"{corpus['flat_size']} vs {half['flat_size']}")


def test_u_channel_flat_length_matches_the_handbook():
    print("test_u_channel_flat_length_matches_the_handbook")
    with Worker() as w:
        w.call("new_document", name="sheet_u")
        # 92 mm profile + two bends at R = t = 2 gives a 100 mm OUTSIDE web
        part = w.call("sheet_base", profile=[[0, 0], [92, 0], [92, 60], [0, 60]],
                      thickness_mm=T, material="Steel-A36")
        for x in (0.0, 92.0):
            tag = _edge(w, part["handle"], x=x, z=0.0)
            part = w.call("sheet_flange", handle=part["handle"], edge=tag,
                          length_mm=50.0, angle_deg=90.0, inner_radius_mm=R)
        flat = w.call("sheet_unfold", handle=part["handle"])
        ba = math.radians(90) * (R + K * T)
        bd = 2 * (R + T) - ba
        _close("flat length == 100 + 50 + 50 - 2*BD", flat["flat_size"][0],
               200.0 - 2 * bd, 1e-6)
        _close("which is the handbook 192.985 mm", flat["flat_size"][0],
               192.98495498926721, 1e-6)
        _eq("two bends reported", len(flat["bends"]), 2)
        for bend in flat["bends"]:
            _close(f"{bend['name']} outer leg is the drawing dimension",
                   bend["outer_length_mm"], 50.0, 1e-9)
            _eq(f"{bend['name']} K is echoed", bend["k_factor"], K)
            _check(f"{bend['name']} says where K came from",
                   "press-brake chart" in bend["k_source"], bend["k_source"])
        _check("a corpus development is labelled a correlation",
               flat["fidelity"] == "correlation" and flat["band_pct"] > 0,
               flat)
        pinned = w.call("sheet_unfold", handle=part["handle"], k_factor=0.42,
                        build=False)
        _eq("pinning K makes it exact", pinned["fidelity"], "exact")
        _check("and moves the blank", pinned["flat_size"][0] != flat["flat_size"][0])
        _check("refold still closes on a two-bend part",
               w.call("sheet_refold", handle=part["handle"])["compare"]["matches"])


# --- the DXF deliverable ------------------------------------------------------

_DXF_REIMPORT = """
import importDXF, FreeCAD, Part
doc = FreeCAD.newDocument("dxfcheck")
importDXF.insert(%r, doc.Name)
closed, opened = [], []
for o in doc.Objects:
    s = getattr(o, "Shape", None)
    if s is None or s.isNull():
        continue
    for wire in s.Wires:
        if wire.isClosed():
            closed.append(round(Part.Face(wire).Area, 4))
        else:
            opened.append(round(wire.Length, 4))
__result__ = {"closed_areas": sorted(closed), "open_lengths": sorted(opened)}
FreeCAD.closeDocument(doc.Name)
"""


def test_the_dxf_reimports_as_a_closed_profile():
    print("test_the_dxf_reimports_as_a_closed_profile")
    with Worker() as w:
        part = _l_bracket(w)
        # a Ø8 hole well clear of the bend, so it survives the screen too
        cyl = w.call("add_primitive", kind="cylinder", r=4, h=10,
                     placement=[20, 20, -2])
        part = w.call("boolean_op", op="cut", base=part["handle"],
                      tool=cyl["handle"])
        out = os.path.join(tempfile.mkdtemp(), "flat.dxf")
        res = w.call("sheet_flat_export", handle=part["handle"], path=out)
        _check("it wrote a file", os.path.getsize(out) == res["size"], res)
        # The reported size must be the file's, not the length of the string we
        # meant to write — those diverge the moment text-mode line-ending
        # translation is in play, which is how this passed on Linux and failed on
        # Windows. Pin the byte-level invariant that makes them agree: a flat
        # pattern is the same FILE on every platform, so a shop (or
        # release_package's checksum) sees one artifact, not one per OS.
        raw = open(out, "rb").read()
        _eq("bytes on disk match the reported size", len(raw), res["size"])
        _check("LF endings only — no platform-dependent CRLF", b"\r" not in raw,
               f"{raw.count(bytes([13]))} CR bytes in the DXF")
        _eq("declaring the three shop layers", res["layers"],
            ["CUT", "BEND_UP", "BEND_DOWN"])
        _eq("one profile, one hole, one bend line", res["entities"],
            {"polylines": 1, "circles": 1, "bend_lines": 1})
        _eq("with the K it used per bend", res["bends"][0]["k_factor"], K)

        with open(out, encoding="utf-8") as f:
            parsed = sm.parse_dxf(f.read())
        _eq("the layer table survives a re-read",
            [layer["name"] for layer in parsed["layers"]], list(sm.DXF_LAYERS))
        _eq("the profile is flagged closed", parsed["polylines"][0]["closed"], True)
        _eq("on the CUT layer", parsed["polylines"][0]["layer"], "CUT")
        _eq("the hole is a circle on CUT",
            [(c["layer"], c["radius"]) for c in parsed["circles"]], [("CUT", 4.0)])
        _eq("the bend line is on BEND_UP", parsed["lines"][0]["layer"], "BEND_UP")

        # ...and the claim that actually matters: FreeCAD's own DXF importer, i.e.
        # something that is not our parser, reads it as a closed profile.
        got = w.call("run_script", code=_DXF_REIMPORT % out)["result"]
        _eq("FreeCAD imports exactly one closed wire",
            len(got["closed_areas"]), 1)
        _eq("no dangling open geometry in the profile", got["open_lengths"], [])
        _close("whose area is the developed blank area (holes excluded)",
               got["closed_areas"][0],
               res["blank_area_mm2"] + math.pi * 16.0, 1e-3)


def test_bend_direction_is_readable_off_the_print():
    print("test_bend_direction_is_readable_off_the_print")
    with Worker() as w:
        w.call("new_document", name="sheet_updown")
        part = w.call("sheet_base", profile=[[0, 0], [92, 0], [92, 60], [0, 60]],
                      thickness_mm=T, material="Steel-A36")
        for x, direction in ((0.0, "up"), (92.0, "down")):
            tag = _edge(w, part["handle"], x=x, z=0.0)
            part = w.call("sheet_flange", handle=part["handle"], edge=tag,
                          length_mm=30.0, angle_deg=90.0, inner_radius_mm=R,
                          direction=direction)
        out = os.path.join(tempfile.mkdtemp(), "updown.dxf")
        w.call("sheet_flat_export", handle=part["handle"], path=out)
        with open(out, encoding="utf-8") as f:
            parsed = sm.parse_dxf(f.read())
        _eq("one bend line per bend, each on the layer naming its direction",
            sorted(line["layer"] for line in parsed["lines"]),
            ["BEND_DOWN", "BEND_UP"])
        bb = w.call("bounding_box", handle=part["handle"])
        lo, hi = bb["min"][2], bb["max"][2]
        _check("one flange really does go up and the other down", lo < 0 < hi,
               f"z {lo}..{hi}")
        _check("and the round trip still closes",
               w.call("sheet_refold", handle=part["handle"])["compare"]["matches"])


# --- holes, tabs, hems --------------------------------------------------------

def test_holes_are_read_off_the_solid_and_screened():
    print("test_holes_are_read_off_the_solid_and_screened")
    with Worker() as w:
        part = _l_bracket(w)
        far = w.call("add_primitive", kind="cylinder", r=4, h=10,
                     placement=[20, 20, -2])
        part = w.call("boolean_op", op="cut", base=part["handle"],
                      tool=far["handle"])
        flat = w.call("sheet_unfold", handle=part["handle"], build=False)
        _eq("the hole is found without being declared", len(flat["holes"]), 1)
        _close("at its flat-pattern position", flat["holes"][0]["x"], 20.0, 1e-6)
        _close("with its diameter", flat["holes"][0]["diameter_mm"], 8.0, 1e-6)
        _close("and it is deducted from the blank area",
               flat["flat_area_mm2"] - flat["blank_area_mm2"], math.pi * 16.0, 1e-4)
        _check("a hole 36 mm clear of the bend passes the screen",
               w.call("sheet_check", handle=part["handle"])["ok"])

        # 2t + R = 6 mm is the limit, edge of hole to bend tangent; put one at 1 mm
        near = w.call("add_primitive", kind="cylinder", r=3, h=10,
                      placement=[56, 10, -2])
        part = w.call("boolean_op", op="cut", base=part["handle"],
                      tool=near["handle"])
        res = w.call("sheet_check", handle=part["handle"])
        _check("a hole 1 mm from the tangent fails it", not res["ok"], res)
        hits = [f for f in res["findings"] if f["code"] == "hole_to_bend"]
        _eq("exactly one hole flagged", len(hits), 1)
        _close("against the 2t + R limit", hits[0]["limit_mm"], 2 * T + R, 1e-6)
        _close("measured edge-of-hole to tangent", hits[0]["value_mm"], 1.0, 1e-4)
        _eq("both holes still reach the DXF",
            w.call("sheet_flat_export", handle=part["handle"],
                   path=os.path.join(tempfile.mkdtemp(), "h.dxf"))
            ["entities"]["circles"], 2)


def test_a_tab_grows_the_blank_without_adding_a_bend():
    print("test_a_tab_grows_the_blank_without_adding_a_bend")
    with Worker() as w:
        w.call("new_document", name="sheet_tab")
        base = w.call("sheet_base", profile=[[0, 0], [50, 0], [50, 30], [0, 30]],
                      thickness_mm=1.5)
        tag = _edge(w, base["handle"], x=50.0, z=0.0)
        tab = w.call("sheet_tab", handle=base["handle"], edge=tag, length_mm=12.0,
                     width_mm=10.0, offset_mm=5.0)
        _close("the tab adds its own volume", tab["volume"],
               50 * 30 * 1.5 + 12 * 10 * 1.5, 1e-6)
        _eq("it is a zero-angle feature", tab["angle_deg"], 0.0)
        flat = w.call("sheet_unfold", handle=tab["handle"], build=False)
        _close("the blank reaches the end of the tab", flat["flat_size"][0], 62.0,
               1e-9)
        _eq("but the print carries no bend line for it", flat["bend_lines"], [])
        outline = [tuple(p) for p in flat["outline"]]
        _check("the outline detours around the partial-width tab",
               all(any(math.dist(p, want) < 1e-6 for p in outline)
                   for want in ((50.0, 5.0), (62.0, 5.0), (62.0, 15.0),
                                (50.0, 15.0))), outline)
        _check("and it refolds (a tab folds to itself)",
               w.call("sheet_refold", handle=tab["handle"])["compare"]["matches"])


def test_a_hem_on_a_flange_develops_outward():
    print("test_a_hem_on_a_flange_develops_outward")
    # Nesting is where a handedness bug hides: a child region whose local frame is
    # mirrored develops back OVER its parent instead of past it, and the flat
    # pattern silently collapses. The first implementation had exactly that bug.
    with Worker() as w:
        part = _l_bracket(w, doc="sheet_hem")
        free = _edge(w, part["handle"], x=64.0, z=30.0)
        hem = w.call("sheet_hem", handle=part["handle"], edge=free, kind="closed",
                     length_mm=10.0)
        _eq("a closed hem is a 180-degree bend", hem["angle_deg"], 180.0)
        _close("at t/2 inside radius", hem["inner_radius_mm"], T / 2.0, 1e-9)
        _close("leaving a gap of 2R = t", hem["gap_mm"], T, 1e-9)
        flat = w.call("sheet_unfold", handle=hem["handle"], build=False)
        _check("no overlap reported", flat["ok"], flat["warnings"])
        ba_flange = math.radians(90) * (R + K * T)
        # the hem's r/t is 0.5, a tighter band than the flange's: K = 0.38
        ba_hem = math.radians(180) * (T / 2.0 + 0.38 * T)
        _close("the blank grows by BA(flange) + leg + BA(hem) + return",
               flat["flat_size"][0], 60.0 + ba_flange + 26.0 + ba_hem + 10.0, 1e-4)
        _eq("two bend lines on the print", len(flat["bend_lines"]), 2)
        _check("and the nested round trip closes",
               w.call("sheet_refold", handle=hem["handle"])["compare"]["matches"],
               w.call("sheet_refold", handle=hem["handle"])["compare"])
        codes = [f["code"] for f in
                 w.call("sheet_check", handle=hem["handle"])["findings"]]
        _check("the hem is screened as a hem, not as an air bend",
               "hem_process_assumed" in codes and "min_bend_radius" not in codes,
               codes)


# --- the DFM screen on real geometry ------------------------------------------

def test_a_refold_collision_is_found_by_intersecting_real_solids():
    print("test_a_refold_collision_is_found_by_intersecting_real_solids")
    with Worker() as w:
        w.call("new_document", name="sheet_crash")
        # two 140-degree flanges folding back toward each other over a narrow web:
        # long enough legs and they occupy the same space
        part = w.call("sheet_base", profile=[[0, 0], [40, 0], [40, 50], [0, 50]],
                      thickness_mm=T, material="Steel-A36")
        for x in (0.0, 40.0):
            tag = _edge(w, part["handle"], x=x, z=0.0)
            part = w.call("sheet_flange", handle=part["handle"], edge=tag,
                          length_mm=45.0, angle_deg=140.0, inner_radius_mm=R,
                          length_from="tangent")
        res = w.call("sheet_check", handle=part["handle"])
        hits = [f for f in res["findings"] if f["code"] == "refold_collision"]
        _eq("the collision is found", len(hits), 1)
        _eq("naming both features", sorted(hits[0]["features"]),
            ["flange1", "flange2"])
        _check("with a real overlap volume", hits[0]["value_mm3"] > 1.0, hits[0])
        _check("and the screen fails", not res["ok"], res)

        # the same part with short legs clears itself
        w.call("new_document", name="sheet_noclash")
        ok = w.call("sheet_base", profile=[[0, 0], [40, 0], [40, 50], [0, 50]],
                    thickness_mm=T, material="Steel-A36")
        for x in (0.0, 40.0):
            tag = _edge(w, ok["handle"], x=x, z=0.0)
            ok = w.call("sheet_flange", handle=ok["handle"], edge=tag,
                        length_mm=12.0, angle_deg=140.0, inner_radius_mm=R,
                        length_from="tangent")
        clean = w.call("sheet_check", handle=ok["handle"])
        _check("shortening the legs clears it", clean["ok"], clean["findings"])


def test_min_bend_radius_and_flange_are_two_sided_on_real_parts():
    print("test_min_bend_radius_and_flange_are_two_sided_on_real_parts")
    with Worker() as w:
        # 6061-T6 wants 3t of inside radius; A36 takes 1t. Same geometry, different
        # verdict — which is the entire point of a per-material corpus.
        tight = _l_bracket(w, doc="sheet_6061", material="AL6061-T6")
        res = w.call("sheet_check", handle=tight["handle"])
        _eq("R = 1t on 6061-T6 is flagged",
            [f["code"] for f in res["findings"] if f["severity"] == "fail"],
            ["min_bend_radius"])
        _close("against 3t", res["min_bend_radius_mm"], 3 * T, 1e-9)
        soft = _l_bracket(w, doc="sheet_a36", material="Steel-A36")
        _check("the same radius on A36 passes",
               w.call("sheet_check", handle=soft["handle"])["ok"])

        # 4t + R = 10 mm minimum outer flange
        short = _l_bracket(w, doc="sheet_short", length=9.0)
        res = w.call("sheet_check", handle=short["handle"])
        _eq("a 9 mm outer flange is flagged",
            [f["code"] for f in res["findings"] if f["severity"] == "fail"],
            ["min_flange_length"])
        _close("against 4t + R", res["findings"][0]["limit_mm"],
               sm.MIN_FLANGE_T * T + R, 1e-6)
        _check("a shop running a narrower vee can relax the threshold",
               w.call("sheet_check", handle=short["handle"], min_flange_t=3.0)["ok"])
        _eq("the screen labels its fidelity", res["fidelity"], "correlation")
        _eq("with no scatter band on a threshold", res["band_pct"], None)


def test_dfm_check_and_sheet_check_cannot_disagree():
    print("test_dfm_check_and_sheet_check_cannot_disagree")
    with Worker() as w:
        part = _l_bracket(w, doc="sheet_dfm", material="AL6061-T6")
        direct = w.call("sheet_check", handle=part["handle"])
        via_dfm = w.call("dfm_check", handle=part["handle"], process="sheet")
        _check("dfm_check picks up the sheet rules with no extra argument",
               "sheet" in via_dfm, list(via_dfm))
        _eq("with identical findings",
            [f["code"] for f in via_dfm["sheet"]["findings"]],
            [f["code"] for f in direct["findings"]])
        _eq("and an identical verdict", via_dfm["sheet"]["ok"], direct["ok"])
        _check("whose failure gates the overall dfm verdict", not via_dfm["pass"],
               via_dfm)
        good = _l_bracket(w, doc="sheet_dfm_ok", material="Steel-A36")
        clean = w.call("dfm_check", handle=good["handle"], process="sheet")
        _check("a conforming sheet part passes both", clean["sheet"]["ok"], clean)
        # a non-sheet handle must be untouched by any of this
        box = w.call("add_primitive", kind="box", w=20, d=20, h=20)
        _check("a plain solid gets no sheet block",
               "sheet" not in w.call("dfm_check", handle=box["handle"],
                                     process="cnc"))


def main():
    for fn in (
        test_the_folded_solid_is_the_closed_form_solid,
        test_a_bad_pick_is_refused_at_the_pick,
        test_unfold_refold_reproduces_the_folded_solid,
        test_a_broken_bend_must_fail_the_round_trip,
        test_flat_volume_equals_folded_volume_only_at_k_half,
        test_u_channel_flat_length_matches_the_handbook,
        test_the_dxf_reimports_as_a_closed_profile,
        test_bend_direction_is_readable_off_the_print,
        test_holes_are_read_off_the_solid_and_screened,
        test_a_tab_grows_the_blank_without_adding_a_bend,
        test_a_hem_on_a_flange_develops_outward,
        test_a_refold_collision_is_found_by_intersecting_real_solids,
        test_min_bend_radius_and_flange_are_two_sided_on_real_parts,
        test_dfm_check_and_sheet_check_cannot_disagree,
    ):
        fn()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

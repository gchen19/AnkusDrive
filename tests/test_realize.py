"""Geometry-realizes-declaration gate (RFC §11.10) — does the exported METAL back the
declared mechanism? End-to-end against the REAL CAD, through merge_assembly.

The gearbox arc shipped three green validations on geometry that was actually wrong,
because each ran on a *model* of the mechanism, not the *artifact* (see
docs/archive/KICKOFF_validate_the_artifact.md). The headline regression: a dog collar
declared ENGAGED had a SOLID FACE where the dog gaps belonged; it could not interlock,
yet every gate passed. This test builds that exact bug and proves the gate now FAILS
it — while the interleaved collar PASSES — by consuming the exported shapes.

Four things are asserted against the as-built geometry:
  * dog_ring     — the collar must have GAPS (interleaving teeth), not a solid face.
  * bore_keying  — a freewheel gear must be ROUND-bored; a keyed part D-flatted.
  * contact_band — the engaged overlap must sit in-family (small), not jam (250 mm³).
  * interleave   — the engaged collar/gear teeth must be HALF-PITCH offset (fall in each
                   other's gaps), not teeth-on-teeth — a gap a per-part check is blind to.

Run: .venv/bin/python3 tests/test_realize.py   (needs FreeCAD via the worker)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from driftpin import Worker  # noqa: E402

M = 2.0
SR = 5.0           # shaft radius
GH = 6.0           # gear face width
ND = 6             # dog teeth
RDOG = SR + 2.5    # dog pitch radius = 7.5

# Each script builds ONE Part::Feature named "part" so the component file links
# cleanly. Layout: gear body z[0,GH], dog teeth up z[GH,GH+4]; collar dogs z[GH,GH+4.5]
# with its sleeve raised to z[GH+4,...] so (engaged, coaxial) only the DOG BANDS meet.
_HELPERS = r"""
import math
import FreeCAD as App
from FreeCAD import Vector, Placement, Rotation
import Part
doc = App.ActiveDocument
SR, GH, ND, RDOG = %f, %f, %d, %f
def dbore(solid, r, h, z0, depth=1.0):
    return solid.cut(Part.makeCylinder(r, h, Vector(0,0,z0)).cut(
        Part.makeBox(60,60,h+2, Vector(r-depth,-30,z0-1))))
def dogs(radius, z0, h, phase, n=ND):
    out=[]; tw=0.45*(2*math.pi*radius/n)
    for k in range(n):
        th=phase+2*math.pi*k/n
        b=Part.makeBox(3.0,tw,h,Vector(-1.5,-tw/2,0))
        b.Placement=Placement(Vector(radius*math.cos(th),radius*math.sin(th),z0),
                              Rotation(Vector(0,0,1),math.degrees(th)))
        out.append(b)
    return out
def emit(shape):
    o=doc.addObject("Part::Feature","part"); o.Shape=shape; doc.recompute()
"""

GEAR = _HELPERS + r"""
og = doc.getObject(%r).Shape
gear = og.cut(Part.makeCylinder(SR+0.3, GH+2, Vector(0,0,-1)))   # round bore -> FREEWHEEL
gear = gear.fuse(dogs(RDOG, GH, 4.0, 0.0))                       # dog teeth up, phase 0
emit(gear)
"""

COLLAR_GOOD = _HELPERS + r"""
c = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)  # raised sleeve, D-bore
c = c.fuse(dogs(RDOG, GH, 4.5, math.pi/ND))                      # interleaving teeth, half-pitch
emit(c)
"""

COLLAR_BAD = _HELPERS + r"""
c = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)  # same raised sleeve
c = c.fuse(Part.makeCylinder(SR+4.0, 4.5, Vector(0,0,GH)))       # SOLID FACE where the gaps belong
emit(c)
"""

# Real gaps (passes dog_ring) but teeth IN PHASE with the gear (phase 0, not half-pitch)
# -> teeth-on-teeth, jams. The per-part gap check can't see it; interleave (relative
# phase, common frame) does.
COLLAR_INPHASE = _HELPERS + r"""
c = dbore(Part.makeCylinder(SR+4.0, 6.0, Vector(0,0,GH+4.0)), SR+0.1, 8.0, GH+3.0)  # same raised sleeve
c = c.fuse(dogs(RDOG, GH, 4.5, 0.0))                             # teeth present but NOT offset (in phase)
emit(c)
"""


def _build_part(w, code, path):
    """Build one part into a fresh document and save it to `path` (one object)."""
    w.call("new_document", name="part_" + Path(path).stem)
    w.call("run_script", code=code)
    w.call("save_document", path=path)


def _build_gear(w, path):
    """Freewheel gear: an add_gear blank, round-bored, with dog teeth fused on top —
    built in its own document so the GEAR script can reference the blank."""
    w.call("new_document", name="part_gear")
    g = w.call("add_gear", teeth=24, module=M, height=GH, name="g24")
    w.call("run_script", code=GEAR % (SR, GH, ND, RDOG, g["name"]))
    w.call("save_document", path=path)


def _checks(collar_inst="collar", gear_inst="gear"):
    """The §11.10 typed checks: the collar is a real dog ring + keyed; the gear is a
    freewheel (round) bore; their engaged overlap is in-family."""
    return [
        {"kind": "dog_ring", "part": collar_inst, "role": "collar",
         "dog_radius_mm": RDOG, "z_lo_mm": GH + 0.5, "z_hi_mm": GH + 3.5, "n_dogs": ND},
        {"kind": "bore_keying", "part": collar_inst, "role": "collar",
         "keyed": True, "bore_radius_mm": SR + 0.1},
        {"kind": "bore_keying", "part": gear_inst, "role": "gear",
         "keyed": False, "bore_radius_mm": SR + 0.3},
        {"kind": "contact_band", "a": collar_inst, "b": gear_inst,
         "interface": "engaged dog clutch", "max_overlap_mm3": 60.0},
        {"kind": "interleave", "a": collar_inst, "b": gear_inst, "role_a": "collar",
         "role_b": "gear", "dog_radius_mm": RDOG, "z_lo_mm": GH + 0.5,
         "z_hi_mm": GH + 3.5},
    ]


def _manifest(d, collar_file):
    """A minimal engaged dog-clutch assembly: a freewheel gear + a collar, coaxial."""
    return {
        "name": "dogclutch",
        "components": {
            "gear": {"file": "gear.FCStd", "object": "part"},
            "collar": {"file": collar_file, "object": "part"},
        },
        "instances": [
            {"component": "gear", "name": "gear", "placement": [0, 0, 0]},
            {"component": "collar", "name": "collar", "placement": [0, 0, 0]},
        ],
        "checks": _checks(),
    }


def _merge(w, d, collar_file):
    man = _manifest(d, collar_file)
    mpath = os.path.join(d, f"manifest_{Path(collar_file).stem}.json")
    with open(mpath, "w") as f:
        json.dump(man, f)
    return w.call("merge_assembly", manifest=mpath)


# --- the headline regression -------------------------------------------------

def test_solid_face_collar_fails_interleaved_passes():
    """The same assembly, the only difference being the collar's engagement face:
    interleaved teeth PASS, a solid face FAILS — regression-proof against the exact
    bug that shipped green."""
    with tempfile.TemporaryDirectory() as d:
        with Worker() as w:
            _build_gear(w, os.path.join(d, "gear.FCStd"))
            _build_part(w, COLLAR_GOOD % (SR, GH, ND, RDOG),
                        os.path.join(d, "collar_good.FCStd"))
            _build_part(w, COLLAR_BAD % (SR, GH, ND, RDOG),
                        os.path.join(d, "collar_bad.FCStd"))

            good = _merge(w, d, "collar_good.FCStd")
            bad = _merge(w, d, "collar_bad.FCStd")

    # GOOD: the interleaved collar realizes the declaration
    assert good["ok"], f"interleaved collar must PASS: {good['gates'].get('typed')}"
    assert not good["gates"]["typed"], good["gates"]["typed"]

    # BAD: the solid-face collar must fail, and name WHY (dog band + jam)
    assert not bad["ok"], "solid-face collar must FAIL the merge"
    typed = bad["gates"]["typed"]
    reasons = " | ".join(v.get("reason", "") for v in typed)
    assert any(v.get("kind") == "dog_ring" for v in typed), typed
    assert "SOLID FACE" in reasons, reasons
    assert any(v.get("kind") == "contact_band" for v in typed), typed
    print(f"    GOOD ok={good['ok']}   BAD ok={bad['ok']}  ({len(typed)} typed viol)")
    print(f"    BAD reasons: {reasons[:160]}")


def test_inphase_collar_fails_interleave():
    """A collar with real GAPS (passes dog_ring) but teeth IN PHASE with the gear — no
    half-pitch offset — jams teeth-on-teeth. The per-part gap check cannot see it; the
    interleave gate (relative phase, in the engaged frame) does. This is the failure
    mode dog_ring is blind to."""
    with tempfile.TemporaryDirectory() as d:
        with Worker() as w:
            _build_gear(w, os.path.join(d, "gear.FCStd"))
            _build_part(w, COLLAR_INPHASE % (SR, GH, ND, RDOG),
                        os.path.join(d, "collar_inphase.FCStd"))
            rep = _merge(w, d, "collar_inphase.FCStd")
    assert not rep["ok"], "in-phase collar must FAIL the merge (teeth-on-teeth jam)"
    typed = rep["gates"]["typed"]
    # the collar HAS gaps, so the per-part dog_ring gate must NOT fire ...
    assert not any(v.get("kind") == "dog_ring" for v in typed), \
        f"in-phase collar has real gaps; dog_ring should pass: {typed}"
    # ... but the relative-phase interleave gate MUST catch the teeth-on-teeth jam
    assert any(v.get("kind") == "interleave" for v in typed), typed
    reasons = " | ".join(v.get("reason", "") for v in typed)
    assert "IN PHASE" in reasons, reasons
    print(f"    in-phase collar ok={rep['ok']}  (dog_ring passes, interleave fails)")


def test_freewheel_gear_is_round_bored():
    """The freewheel gear's bore_keying check passes (round) and would fail if the
    same gear were declared keyed — the 'loose gear keyed to nothing' guard."""
    with tempfile.TemporaryDirectory() as d:
        with Worker() as w:
            _build_gear(w, os.path.join(d, "gear.FCStd"))
            _build_part(w, COLLAR_GOOD % (SR, GH, ND, RDOG),
                        os.path.join(d, "collar_good.FCStd"))
            man = _manifest(d, "collar_good.FCStd")
            # flip the gear's declaration to keyed -> must fail (it is round-bored)
            for chk in man["checks"]:
                if chk.get("kind") == "bore_keying" and chk["part"] == "gear":
                    chk["keyed"] = True
            mpath = os.path.join(d, "manifest_keyed_gear.json")
            with open(mpath, "w") as f:
                json.dump(man, f)
            rep = w.call("merge_assembly", manifest=mpath)
    assert not rep["ok"], "round-bored gear declared keyed must FAIL"
    assert any(v.get("kind") == "bore_keying" and v["part"] == "gear"
               for v in rep["gates"]["typed"]), rep["gates"]["typed"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    t0 = time.time()
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n== {len(tests) - failed}/{len(tests)} passed  ({time.time() - t0:.2f}s) ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

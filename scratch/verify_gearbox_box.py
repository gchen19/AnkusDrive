"""Validate the HEADLINE artifact — the 3-speed gearbox_multispeed box — against the
§11.10 oracle (ankusdrive.realize). Does the as-built metal of EVERY collar and gear
realize its declaration?

So far only the 2-part dog clutch (verify_dog_clutch_unit.py) was judged on real metal;
the headline assembly was still only eyeballed. This closes that gap: it rebuilds the
box's named objects (the exported STEP compound is exactly Part.Compound of these same
shapes, and realize is placement-independent), then runs the EXISTING oracle unchanged
over each part and reports a per-part PASS/FAIL table.

Note on names: BUILD adds its assembled parts with the same LABEL as the source gears,
so FreeCAD auto-suffixes the internal Name. The assembled gears are therefore g_in001,
g_cm001 (keyed), g_c001/g_c002/g_c003 (countershaft, keyed), g_m001/g_m002/g_m003
(mainshaft, freewheel) — NOT the plain add_gear blanks g_in/g_m0/... which BUILD leaves
untouched at the origin. The collars keep their unique labels collar_low / collar_high.

Declarations (ground truth from gearbox_multispeed.py):
  * freewheel speed gears g_m001/g_m002/g_m003 : round bore r=SR+0.3=5.3, plus 6 dog
    teeth at absolute z[base+GH, base+GH+4].
  * keyed gears g_in001/g_cm001/g_c001/g_c002/g_c003 : D-flat bore r=SR+0.1=5.1.
  * collars collar_low (ENGAGED w/ g_m001) and collar_high (NEUTRAL) : 6-tooth dog ring
    at r=SR+2.5=7.5 (fill≈0.5, sectors=6), keyed bore r=5.1.
  * engaged overlap collar_low∩g_m001 : SMALL / in-family (interleaved), not a jam.
  * neutral overlap collar_high∩nearest gear : ~0.

  .venv/bin/python3 scratch/verify_gearbox_box.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scratch"))
from ankusdrive import Worker  # noqa: E402
import gearbox_multispeed as gm  # noqa: E402

ART = REPO / "artifacts" / "gearbox"

# realize declarations derived from the box params (no new thresholds invented)
SR = gm.SR
DOG_R = SR + 2.5        # 7.5 — dog pitch radius for ALL collars and speed gears
N_DOGS = 6
GH = gm.GH
KEYED_BORE_R = SR + 0.1   # 5.1 — keyed (D-flat) gears + collar splines
FREE_BORE_R = SR + 0.3    # 5.3 — freewheel speed-gear bores

# Assembled-object names BUILD actually creates (see module docstring on suffixing):
KEYED_GEARS = ["g_in001", "g_cm001", "g_c001", "g_c002", "g_c003"]
# freewheel speed gears -> {name: assembled base_z} (g_m001@Z0, g_m002@Z1, g_m003@Z2)
FREE_GEARS = {"g_m001": gm.Z0, "g_m002": gm.Z1, "g_m003": gm.Z2}
# collar base_z in BUILD: collar_low = collar(Z0+6=70, 10), collar_high = collar(Z1+14=52, 10)
COLLARS = {"collar_low": 70.0, "collar_high": 52.0}
# engagement pairs: collar_low interlocks the speed-1 gear; collar_high is parked neutral
ENGAGED_PAIR = ("collar_low", "g_m001")     # ENGAGED -> small in-family overlap
NEUTRAL_PAIR = ("collar_high", "g_m002")    # NEUTRAL -> ~0 with nearest speed gear

# Second script: run the real oracle over the named objects already in the doc.
INSPECT = r"""
from ankusdrive import realize
import FreeCAD as App
doc = App.ActiveDocument

DOG_R, N_DOGS, KEYED_BORE_R, FREE_BORE_R, GH = %f, %d, %f, %f, %f
KEYED_GEARS = %r
FREE_GEARS = %r          # {name: assembled base_z}
COLLARS = %r             # {name: base_z}
ENGAGED_PAIR = %r
NEUTRAL_PAIR = %r

def shp(name):
    return doc.getObject(name).Shape

out = {"keyed": {}, "free": {}, "collars": {}, "overlap": {}}

# keyed gears: must carry a radial D-flat in the bore
for nm in KEYED_GEARS:
    out["keyed"][nm] = realize.check_bore_keying(
        shp(nm), {"role": nm, "keyed": True, "bore_radius_mm": KEYED_BORE_R})

# freewheel speed gears: must be ROUND (no flat), AND carry their dog teeth. The dog band
# sits at ABSOLUTE z[base+GH, base+GH+4] (dogs(SR+2.5, GH, 4.0, 0.0) on the placed gear).
for nm, base in FREE_GEARS.items():
    s = shp(nm)
    z_lo, z_hi = base + GH + 0.5, base + GH + 3.5
    out["free"][nm] = {
        "bore": realize.check_bore_keying(
            s, {"role": nm, "keyed": False, "bore_radius_mm": FREE_BORE_R}),
        "dog_ring": realize.check_dog_ring(
            s, {"role": nm, "dog_radius_mm": DOG_R, "z_lo_mm": z_lo, "z_hi_mm": z_hi,
                "n_dogs": N_DOGS}),
        "dog_profile": realize.dog_ring_profile(s, DOG_R, z_lo, z_hi),
    }

# collars: 6-tooth dog ring (fill~0.5, sectors=6) within the dog band BELOW the sleeve,
# plus a keyed (splined) bore.
for nm, bz in COLLARS.items():
    s = shp(nm)
    z_lo, z_hi = bz + 0.5, bz + 3.5
    out["collars"][nm] = {
        "dog_ring": realize.check_dog_ring(
            s, {"role": nm, "dog_radius_mm": DOG_R, "z_lo_mm": z_lo, "z_hi_mm": z_hi,
                "n_dogs": N_DOGS}),
        "bore": realize.check_bore_keying(
            s, {"role": nm, "keyed": True, "bore_radius_mm": KEYED_BORE_R}),
        "dog_profile": realize.dog_ring_profile(s, DOG_R, z_lo, z_hi),
    }

# engaged overlap: collar_low interlocks its speed gear -> small, in-family (NOT a jam)
out["overlap"]["engaged"] = round(
    shp(ENGAGED_PAIR[0]).common(shp(ENGAGED_PAIR[1])).Volume, 3)
# neutral: collar_high parked between speeds -> ~0 with the nearest speed gear
out["overlap"]["neutral"] = round(
    shp(NEUTRAL_PAIR[0]).common(shp(NEUTRAL_PAIR[1])).Volume, 3)

# engaged INTERLEAVE: collar_low's teeth must fall in g_m001's gaps (half-pitch), not
# teeth-on-teeth. Shared dog band z[base+0.5, base+3.5] of the ENGAGED collar.
ez_lo, ez_hi = COLLARS[ENGAGED_PAIR[0]] + 0.5, COLLARS[ENGAGED_PAIR[0]] + 3.5
out["interleave"] = {
    "viol": realize.check_interleave(
        shp(ENGAGED_PAIR[0]), shp(ENGAGED_PAIR[1]),
        {"role_a": ENGAGED_PAIR[0], "role_b": ENGAGED_PAIR[1], "dog_radius_mm": DOG_R,
         "z_lo_mm": ez_lo, "z_hi_mm": ez_hi}),
    "profile": realize.interleave_profile(
        shp(ENGAGED_PAIR[0]), shp(ENGAGED_PAIR[1]), DOG_R, ez_lo, ez_hi),
}

__result__ = out
"""


def main():
    ART.mkdir(parents=True, exist_ok=True)
    step, stl = ART / "gearbox_multispeed.step", ART / "gearbox_multispeed.stl"
    teeth = [gm.T_IN, gm.T_CM, gm.SPEEDS[0][0], gm.SPEEDS[0][1],
             gm.SPEEDS[1][0], gm.SPEEDS[1][1], gm.SPEEDS[2][0], gm.SPEEDS[2][1]]

    with Worker() as w:
        w.call("new_document", name="msbox_verify")
        names = []
        for tag, t in zip(("g_in", "g_cm", "g_c0", "g_m0", "g_c1", "g_m1",
                           "g_c2", "g_m2"), teeth):
            r = w.call("add_gear", teeth=t, module=gm.M, height=GH, name=tag)
            names.append(r["name"])
        # build the box (creates the assembled Part::Feature objects + re-exports STEP/STL)
        w.call("run_script", code=gm.BUILD % (
            gm.M, SR, GH, gm.AX_A, gm.AX_B, gm.Z_CM, gm.Z0, gm.Z1, gm.Z2, names,
            str(step), str(stl), str(step), str(stl)))
        res = w.call("run_script", code=INSPECT % (
            DOG_R, N_DOGS, KEYED_BORE_R, FREE_BORE_R, GH,
            KEYED_GEARS, FREE_GEARS, COLLARS,
            ENGAGED_PAIR, NEUTRAL_PAIR))["result"]

    all_pass = True
    print("== validate the HEADLINE artifact: gearbox_multispeed box (§11.10 oracle) ==\n")
    print(f"  declarations: dog_radius={DOG_R} mm  n_dogs={N_DOGS}  "
          f"keyed_bore={KEYED_BORE_R} mm  freewheel_bore={FREE_BORE_R} mm\n")

    print("  -- keyed gears (want D-flat bore -> real torque path) --")
    for nm in KEYED_GEARS:
        viol = res["keyed"][nm]
        ok = not viol
        all_pass &= ok
        print(f"  {nm:8}: {'PASS  radial D-flat present' if ok else 'FAIL'}")
        for v in viol:
            print(f"          ! {v.get('reason', '')}  (seen r={v.get('bore_radius_seen')})")

    print("\n  -- freewheel speed gears (want ROUND bore + 6 dog teeth) --")
    for nm in FREE_GEARS:
        d = res["free"][nm]
        viol = d["bore"] + d["dog_ring"]
        ok = not viol
        all_pass &= ok
        p = d["dog_profile"]
        print(f"  {nm:8}: {'PASS' if ok else 'FAIL'}  round bore + "
              f"dog_band fill={p['fill']} sectors={p['sectors']}")
        for v in viol:
            print(f"          ! {v.get('reason', '')}")

    print("\n  -- dog collars (want 6-tooth ring fill~0.5 sectors=6 + keyed splines) --")
    for nm in COLLARS:
        d = res["collars"][nm]
        viol = d["dog_ring"] + d["bore"]
        ok = not viol
        all_pass &= ok
        p = d["dog_profile"]
        print(f"  {nm:11}: {'PASS' if ok else 'FAIL'}  "
              f"dog_band fill={p['fill']} sectors={p['sectors']}")
        for v in viol:
            print(f"          ! {v.get('reason', '')}")

    print("\n  -- engagement overlaps (interleaved clutch is a few mm³, a jam is 100s) --")
    eng = res["overlap"]["engaged"]
    neu = res["overlap"]["neutral"]
    eng_ok = 0.0 < eng < 100.0          # interleaved, not a solid jam, but real contact
    neu_ok = neu < 5.0                   # parked in neutral -> essentially no overlap
    all_pass &= eng_ok and neu_ok
    print(f"  {ENGAGED_PAIR[0]} ENGAGED w/ {ENGAGED_PAIR[1]}: overlap={eng} mm³  "
          f"-> {'PASS (in-family interleave)' if eng_ok else 'FAIL'}")
    print(f"  {NEUTRAL_PAIR[0]} NEUTRAL vs {NEUTRAL_PAIR[1]}: overlap={neu} mm³  "
          f"-> {'PASS (clear of gears)' if neu_ok else 'FAIL'}")

    print("\n  -- engaged INTERLEAVE (teeth half-pitch offset, NOT teeth-on-teeth) --")
    il = res["interleave"]
    il_ok = not il["viol"]
    all_pass &= il_ok
    p = il["profile"]
    print(f"  {ENGAGED_PAIR[0]} vs {ENGAGED_PAIR[1]}: both-occupied={p['both_fraction']} "
          f"(a_fill={p['a_fill']} b_fill={p['b_fill']} union={p['union_fraction']})  "
          f"-> {'PASS (teeth fall in the gaps)' if il_ok else 'FAIL'}")
    for v in il["viol"]:
        print(f"          ! {v.get('reason', '')}")

    print("\n" + "=" * 70)
    if all_pass:
        print("  VERDICT: PASS — every collar and gear of the box realizes its "
              "declaration.")
        print(f"  Strongest proof: collar_low dog ring fill="
              f"{res['collars']['collar_low']['dog_profile']['fill']} "
              f"sectors={res['collars']['collar_low']['dog_profile']['sectors']}, "
              f"engaged overlap={eng} mm³ (interleaved teeth, not a ~250 mm³ solid jam).")
    else:
        print("  VERDICT: FAIL — at least one part does NOT realize its declaration "
              "(see ! lines above).")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())

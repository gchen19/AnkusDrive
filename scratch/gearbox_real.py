"""
A REAL constant-mesh parallel-shaft gearbox as a DriftPin assembly — housing
plates + two shafts + bearings (library parts) + bored gears, mated and gated with
the typed interfaces (gear_mesh per pair, bore_fit shaft↔bearing) + a mass
requirement. Scripted reference first (prove the design + the oracle), then the
multi-agent experiment builds on this geometry.

Layout: input shaft on the Z axis at (0,0); output (lay) shaft parallel at
(C,0)=(48,0). For each speed s the input + output gears sit at the same Z and mesh
across C; speeds are stacked along Z (constant-mesh, like a real layshaft box).
Two end plates carry a bearing at each shaft end.

  .venv/bin/python3 scratch/gearbox_real.py [n_speeds]   (default 3)
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from driftpin import Worker  # noqa: E402

M = 2.0                 # gear module
C = 48.0                # shaft centre distance
S = int(round(2 * C / M))   # tooth-sum per meshing pair = 48
RATIOS = [3.0, 2.0, 1.4, 1.0, 5.0 / 7.0, 0.5]
SHAFT_R = 5.0           # Ø10 shafts
BORE_CLEAR = 0.4        # gear/bearing bore diametral clearance over the shaft
GEAR_H = 6.0
Z_PITCH = 14.0          # axial spacing between speeds
Z0 = 12.0               # z of the first gear's bottom face
BRG_OD = 26.0
BRG_W = 8.0
PLATE_T = 5.0


def teeth(ratio):
    n_in = int(round(S / (1.0 + ratio)))
    return n_in, S - n_in


def gear_z(s):
    return Z0 + s * Z_PITCH


def shaft_span(n):
    """Shaft runs from the bottom bearing seat to the top bearing seat."""
    z_lo = 0.0
    z_hi = gear_z(n - 1) + GEAR_H + 12.0
    return z_lo, z_hi


# --- scripted component builders ---------------------------------------------

def build_gear(path, n_teeth):
    """A gear with a centre bore for the shaft (add_gear is solid; cut the bore)."""
    with Worker() as w:
        w.call("new_document", name="gear")
        g = w.call("add_gear", teeth=int(n_teeth), module=M, height=GEAR_H, name="gear")
        b = w.call("add_primitive", kind="cylinder", r=SHAFT_R + BORE_CLEAR / 2,
                   h=GEAR_H * 3, placement=[0, 0, -GEAR_H], name="bore")
        w.call("boolean_op", op="cut", base=g["handle"], tool=b["handle"])
        w.call("save_document", path=str(path))


def build_shaft(path, length):
    with Worker() as w:
        w.call("new_document", name="shaft")
        w.call("add_primitive", kind="cylinder", r=SHAFT_R, h=length, name="shaft")
        w.call("save_document", path=str(path))


def build_plate(path, name):
    """An end plate carrying a bearing bore over each shaft axis (local frame:
    bores at x=0 and x=C)."""
    with Worker() as w:
        w.call("new_document", name=name)
        cur = w.call("add_primitive", kind="box", w=C + 40, d=40, h=PLATE_T,
                     placement=[-20, -20, 0], name="plate")
        for x in (0.0, C):
            tool = w.call("add_primitive", kind="cylinder", r=BRG_OD / 2 + 0.2,
                          h=PLATE_T * 3, placement=[x, 0, -PLATE_T], name="bore")
            cur = w.call("boolean_op", op="cut", base=cur["handle"], tool=tool["handle"])
        w.call("save_document", path=str(path))


def build_components(tmp, n):
    files = {}
    z_lo, z_hi = shaft_span(n)
    # gears
    for s in range(n):
        n_in, n_out = teeth(RATIOS[s])
        for tag, t in ((f"in{s}", n_in), (f"out{s}", n_out)):
            f = tmp / f"{tag}.FCStd"
            build_gear(f, t)
            files[tag] = f
    # shafts
    for tag in ("input_shaft", "output_shaft"):
        f = tmp / f"{tag}.FCStd"
        build_shaft(f, z_hi - z_lo)
        files[tag] = f
    # plates
    for tag in ("bottom_plate", "top_plate"):
        f = tmp / f"{tag}.FCStd"
        build_plate(f, tag)
        files[tag] = f
    return files


# --- manifest assembly -------------------------------------------------------

def build_manifest(tmp, n, files):
    z_lo, z_hi = shaft_span(n)
    comps, insts, checks = {}, [], []

    def add(tag, file, placement):
        comps[tag] = {"file": str(file)}
        insts.append({"component": tag, "name": tag, "placement": placement})

    # shafts on the two axes
    add("input_shaft", files["input_shaft"], [0, 0, z_lo])
    add("output_shaft", files["output_shaft"], [C, 0, z_lo])
    # plates at the two Z ends (bearing seats)
    add("bottom_plate", files["bottom_plate"], [0, 0, z_lo])
    add("top_plate", files["top_plate"], [0, 0, z_hi - PLATE_T])
    # bearings (LIBRARY parts) seated in the plate bores; the shaft rides each one
    for sx, side in ((0.0, "in"), (C, "out")):
        for k, z in enumerate((z_lo, z_hi - BRG_W)):
            tag = f"bearing_{side}_{k}"
            comps[tag] = {"library": {"tool": "add_bearing",
                                      "spec": {"bore": 2 * SHAFT_R + 0.4,
                                               "outer_diameter": BRG_OD, "width": BRG_W}}}
            insts.append({"component": tag, "name": tag, "placement": [sx, 0, z]})
            # bore_fit: the shaft rides in the bearing bore with clearance
            checks.append({"kind": "bore_fit", "min_clearance_mm": 0.1,
                           "pin": f"{'input' if side == 'in' else 'output'}_shaft",
                           "bore": tag})
    # gears on their shafts; each pair meshes across C
    for s in range(n):
        for tag, x in ((f"in{s}", 0.0), (f"out{s}", C)):
            add(tag, files[tag], [x, 0, gear_z(s)])
        checks.append({"kind": "gear_mesh", "a": f"in{s}", "b": f"out{s}",
                       "module_mm": M, "center_distance_mm": C,
                       "ratio": teeth(RATIOS[s])[1] / teeth(RATIOS[s])[0],
                       "tol_mm": 0.6})

    man = {"schema": "driftpin.manifest/1", "name": f"gearbox{n}",
           "root": f"gearbox{n}.FCStd", "components": comps,
           "instances": insts, "checks": checks,
           "requirements": {"density_kg_mm3": 7.9e-6, "max_mass_g": 6000}}
    mp = tmp / "manifest.json"
    mp.write_text(json.dumps(man, indent=2))
    return mp


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print(f"== building {n}-speed gearbox reference ==")
        files = build_components(tmp, n)
        mp = build_manifest(tmp, n, files)
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(mp))
        g = rep["gates"]
        print(f"  ok={rep['ok']}")
        print(f"  interference={len(g['interference'])}  "
              f"typed_violations={len(g.get('typed', []))}  "
              f"requirements={len(g.get('requirements', []))}")
        if rep.get("requirements"):
            print(f"  measured: {rep['requirements']['report']}")
        if g["interference"]:
            for r in g["interference"][:8]:
                print(f"   CLASH {r['a']} <> {r['b']}: {r['interference_mm3']:.0f} mm3")
        for v in g.get("typed", []):
            print(f"   TYPED  {v.get('reason') or v.get('error')}")
        for v in g.get("requirements", []):
            print(f"   REQ    {v.get('reason') or v.get('error')}")
        if rep["ok"]:
            out = REPO / "results" / "gearbox_real"
            out.mkdir(parents=True, exist_ok=True)
            with Worker() as w:
                w.call("open_document", path=rep["root"])
                top = w.call("run_script", code="""
App.ActiveDocument.recompute()
o=[x for x in App.ActiveDocument.Objects if hasattr(x,'Shape') and not x.Shape.isNull() and not x.InList][0]
o.Shape.exportStep(%r)
import os; __result__=os.path.getsize(%r)
""" % (str(out / f"gearbox{n}.step"), str(out / f"gearbox{n}.step")))["result"]
                w.call("export_shape", object=None, path=str(out / f"gearbox{n}.stl"))
            print(f"  exported -> {out}/gearbox{n}.step ({top:,} bytes) + .stl")


if __name__ == "__main__":
    main()

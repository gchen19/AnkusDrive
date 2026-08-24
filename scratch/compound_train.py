"""A real compound (multi-stage) gear TRAIN as a AnkusDrive assembly, and the scripted
proof that the motion gate checks its EMERGENT invariant — the overall reduction.

Layout: 4 collinear shafts S0..S3 on the X axis, 48 mm apart (shared centre distance
C, module 2 -> tooth-sum 48 per stage). Stage i has a driver gear d_i on shaft i
meshing a driven gear e_i on shaft i+1; stages are stacked along Z so an intermediate
shaft carries its driven-of-previous and driver-of-next at different heights. The
train's overall ratio is the PRODUCT of the three stage ratios — a property no single
stage builder can see. The manifest's `mechanism` block gates it with target_ratio.

  GOOD  drivers [16,24,12] -> stage ratios 0.5 * 1.0 * 0.333 = 1/6 == target.
  BAD   drivers [24,24,12] -> 1.0 * 1.0 * 0.333 = 1/3; each stage still meshes
        (sums to 48), but the product misses -> the motion gate must FAIL it.

  .venv/bin/python3 scratch/compound_train.py
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import gearbox_real as gb            # noqa: E402  (build_gear/build_shaft + constants)
from ankusdrive import Worker          # noqa: E402

NSTAGE = 3
SSUM = 48                            # tooth-sum per stage (shared centre distance)
C = SSUM * gb.M / 2                  # = 48 mm
Z_PITCH = 14.0
Z0 = 12.0
TARGET = (16 / 32) * (24 / 24) * (12 / 36)   # = 1/6 overall reduction
GOOD = [16, 24, 12]                  # driver teeth per stage -> product == TARGET
BAD = [24, 24, 12]                   # each stage meshes; product = 1/3 != TARGET


def stage_teeth(drivers):
    return [(a, SSUM - a) for a in drivers]


def shaft_x(i):
    return i * C


def shaft_len():
    return Z0 + (NSTAGE - 1) * Z_PITCH + gb.GEAR_H + 12.0


def build_components(tmp, drivers):
    files, teeth = {}, stage_teeth(drivers)
    for i, (a, b) in enumerate(teeth):
        for tag, t in ((f"d{i}", a), (f"e{i}", b)):
            f = tmp / f"{tag}.FCStd"
            gb.build_gear(f, t)
            files[tag] = f
    for i in range(NSTAGE + 1):
        f = tmp / f"shaft{i}.FCStd"
        gb.build_shaft(f, shaft_len())
        files[f"shaft{i}"] = f
    return files


def build_manifest(tmp, drivers, files, target=TARGET, with_target=True,
                   manifest_name="manifest.json"):
    teeth = stage_teeth(drivers)
    comps, insts, checks = {}, [], []

    def add(tag, placement):
        comps[tag] = {"file": str(files[tag])}
        insts.append({"component": tag, "name": tag, "placement": placement})

    for i in range(NSTAGE + 1):
        add(f"shaft{i}", [shaft_x(i), 0, 0])
    for i, (a, b) in enumerate(teeth):
        z = Z0 + i * Z_PITCH
        add(f"d{i}", [shaft_x(i), 0, z])
        add(f"e{i}", [shaft_x(i + 1), 0, z])
        checks.append({"kind": "gear_mesh", "a": f"d{i}", "b": f"e{i}", "id": f"stage{i}",
                       "module_mm": gb.M, "center_distance_mm": C, "ratio": b / a,
                       "tol_mm": 0.6})

    links = {}
    for i in range(NSTAGE + 1):
        members = [f"shaft{i}"] + ([f"d{i}"] if i < NSTAGE else []) \
            + ([f"e{i-1}"] if i else [])
        links[f"shaft{i}"] = {"members": members, "ground": "revolute"}
    mech = {"expected_dof": 1, "input": "shaft0", "output": f"shaft{NSTAGE}",
            "links": links}
    if with_target:
        mech["target_ratio"] = {"input": "shaft0", "output": f"shaft{NSTAGE}",
                                "ratio": target, "tol": 0.01}

    man = {"schema": "ankusdrive.manifest/1", "name": "train",
           "root": "train.FCStd", "components": comps, "instances": insts,
           "checks": checks, "mechanism": mech}
    mp = tmp / manifest_name
    mp.write_text(json.dumps(man, indent=2))
    return mp


def merge(w, mp):
    return w.call("merge_assembly", manifest=str(mp))


def _summary(rep):
    g = rep["gates"]
    mob = rep.get("mobility", {})
    tr = mob.get("target_ratio", {})
    return {"ok": rep["ok"], "interference": len(g["interference"]),
            "typed_violations": len(g.get("typed", [])),
            "mobility_ok": not g.get("mobility"), "verdict": mob.get("verdict"),
            "mobility_dof": mob.get("rigid", {}).get("mobility_dof"),
            "overall_realised": tr.get("realised"), "target": tr.get("target"),
            "violations": [v.get("reason") for v in g.get("mobility", [])]}


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print(f"== compound 3-stage gear train — target overall ratio {TARGET:.5f} (1/6) ==")
        for label, drivers in (("GOOD", GOOD), ("BAD", BAD)):
            files = build_components(tmp, drivers)
            mp = build_manifest(tmp, drivers, files,
                                manifest_name=f"manifest_{label}.json")
            with Worker() as w:
                rep = merge(w, mp)
            s = _summary(rep)
            print(f"\n--- {label}  drivers={drivers}  driven={[SSUM-a for a in drivers]} ---")
            print(f"  static: interference={s['interference']}  "
                  f"gear_mesh_violations={s['typed_violations']}")
            print(f"  motion: ok={s['mobility_ok']}  verdict={s['verdict']}  "
                  f"DOF={s['mobility_dof']:+d}")
            print(f"  overall ratio realised={s['overall_realised']}  "
                  f"target={s['target']}")
            print(f"  MERGE ok={s['ok']}")
            for v in s["violations"]:
                print(f"   VIOLATION: {v}")
            out = REPO / "results" / "compound_train"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"gate_report_{label}.json").write_text(json.dumps(rep, indent=2))
        print(f"\n  full gate reports -> {REPO/'results'/'compound_train'}/gate_report_*.json")


if __name__ == "__main__":
    main()

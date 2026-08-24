"""Multi-agent experiment on the compound gear TRAIN — the GO toy the readiness gate
green-lit. Agents build the stage DRIVER gears; the train's overall reduction (the
product of the three stage ratios) is an EMERGENT invariant no single stage builder
can see, gated by merge_assembly's motion gate (target_ratio).

  DERIVE  each driver agent is told the overall target and its stage, and must pick
          its own driver tooth count — a cross-agent reconciliation of the product.
  RESOLVE each agent is handed its driver tooth count (the §11.1 resolve step).
  x
  PARTITION agent sees ONLY its stage   SINGLE each cold call sees the full contract.

The driven gear of each stage is scripted to 48 − (the built driver's teeth), so the
within-stage mesh is automatic and the only thing under test is the emergent product.
Resolved split [12,24,32] -> ratios 1/3 · 1 · 2 = 2/3 == target.

  SELFTEST (free, no API):  .venv/bin/python3 scratch/train_experiment.py --selftest
  Billed run:  RUN_RELIABILITY=1 ANTHROPIC_API_KEY=... .venv/bin/python3 -u \
                 scratch/train_experiment.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
import compound_train as ct          # noqa: E402  (geometry + manifest)
import gearbox_real as gb            # noqa: E402
from ankusdrive import Worker          # noqa: E402

RESOLVED = [12, 24, 32]              # driver teeth per stage -> product == target
TARGET = ct.TARGET                   # (16/32)(24/24)(12/36)=1/6 ... recomputed below
# the train target is the resolved split's product (2/3); override ct.TARGET:
TARGET = (RESOLVED[0] / (ct.SSUM - RESOLVED[0])) \
    * (RESOLVED[1] / (ct.SSUM - RESOLVED[1])) \
    * (RESOLVED[2] / (ct.SSUM - RESOLVED[2]))


def _read_teeth(path):
    """Recover a built gear's tooth count from its tip radius (XMax of the origin-
    centred gear): tip_r = module*(teeth+2)/2 -> teeth = 2*XMax/module − 2."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        xmax = w.call("run_script", code="""
o=[x for x in App.ActiveDocument.Objects if hasattr(x,'Shape') and not x.Shape.isNull()
   and not x.InList][-1]
__result__=o.Shape.BoundBox.XMax
""")["result"]
    return int(round(2 * xmax / gb.M - 2))


def _driver_task(stage, mode, a_resolved):
    bore = (f"Then cut a Ø{2*(gb.SHAFT_R+gb.BORE_CLEAR/2):.1f} mm centre bore: "
            f"add_primitive a cylinder r={gb.SHAFT_R+gb.BORE_CLEAR/2:.1f}, "
            f"h={gb.GEAR_H*3:.0f}, placement [0,0,{-gb.GEAR_H:.0f}], boolean_op cut it "
            f"from the gear. Then save_component.")
    if mode == "resolve":
        return (f"Build a spur gear: add_gear(teeth={a_resolved}, module={gb.M:g}, "
                f"height={gb.GEAR_H:g}). {bore}")
    return (f"Build the DRIVER gear of stage {stage+1} of a 3-stage reduction gear "
            f"train. The train's OVERALL reduction — the product of the three stages' "
            f"speed ratios (each stage's ratio = driver_teeth / driven_teeth) — must "
            f"equal {TARGET:.4f}. Every stage's two gears mesh at a shared tooth-sum of "
            f"{ct.SSUM} (driver_teeth + driven_teeth = {ct.SSUM}). You build only THIS "
            f"stage's driver gear and cannot see the other two stages. Decide your "
            f"driver tooth count, then add_gear(teeth=<it>, module={gb.M:g}, "
            f"height={gb.GEAR_H:g}). {bore}")


def _full_contract_head():
    return (f"You are helping build a 3-stage reduction gear train whose overall "
            f"reduction (product of the 3 stage ratios driver/driven) must be "
            f"{TARGET:.4f}, every stage meshing at tooth-sum {ct.SSUM}. Decide a "
            f"consistent split of driver tooth counts across ALL three stages first, "
            f"then build only the one gear asked for.\n\n")


def assemble_and_gate(tmp, driver_teeth):
    """Script each stage's driven gear = 48 − driver, build the train manifest with the
    target_ratio motion gate, merge, and return the gate report + a summary."""
    files = {}
    for i, a in enumerate(driver_teeth):
        files[f"d{i}"] = tmp / f"d{i}.FCStd"
        gb.build_gear(files[f"d{i}"], a)
        files[f"e{i}"] = tmp / f"e{i}.FCStd"
        gb.build_gear(files[f"e{i}"], ct.SSUM - a)
    for i in range(ct.NSTAGE + 1):
        files[f"shaft{i}"] = tmp / f"shaft{i}.FCStd"
        gb.build_shaft(files[f"shaft{i}"], ct.shaft_len())
    mp = ct.build_manifest(tmp, driver_teeth, files, target=TARGET)
    with Worker() as w:
        rep = w.call("merge_assembly", manifest=str(mp))
    return rep, ct._summary(rep)


def _build_drivers(client, model, mode, condition, tmp):
    """Run the three driver-gear agents; returns (teeth list, files, agent usages)."""
    from test_multiagent_m2 import run_agent, SYSTEM
    teeth, files, agents = [], {}, []
    head = _full_contract_head() if condition == "single" else ""
    for i in range(ct.NSTAGE):
        f = tmp / f"agent_d{i}.FCStd"
        task = head + _driver_task(i, mode, RESOLVED[i])
        a = run_agent(client, model, SYSTEM, task, f)
        agents.append(a)
        files[f"d{i}"] = f
        teeth.append(_read_teeth(f) if a["ok_built"] and f.exists() else None)
    return teeth, files, agents


def run_trial(client, model, mode, condition, tmp):
    teeth, dfiles, agents = _build_drivers(client, model, mode, condition, tmp)
    built = all(t is not None for t in teeth) and all(a["ok_built"] for a in agents)
    if not built:
        return {"built": False, "passed": False, "teeth": teeth,
                "reason": "a driver agent did not build", "cost": _cost(agents, model)}
    # reuse the agents' driver gears; script driven + shafts around them
    files = dict(dfiles)
    for i, a in enumerate(teeth):
        files[f"e{i}"] = tmp / f"e{i}.FCStd"
        gb.build_gear(files[f"e{i}"], ct.SSUM - a)
    for i in range(ct.NSTAGE + 1):
        files[f"shaft{i}"] = tmp / f"shaft{i}.FCStd"
        gb.build_shaft(files[f"shaft{i}"], ct.shaft_len())
    mp = ct.build_manifest(tmp, teeth, files, target=TARGET)
    with Worker() as w:
        rep = w.call("merge_assembly", manifest=str(mp))
    s = ct._summary(rep)
    return {"built": True, "passed": rep["ok"], "teeth": teeth,
            "overall_realised": s["overall_realised"], "target": round(TARGET, 5),
            "mobility_ok": s["mobility_ok"], "reason": (s["violations"] or ["ok"])[0],
            "cost": _cost(agents, model), "report": rep}


def _cost(agents, model):
    from test_multiagent_m2 import cost_of
    usage = {k: sum(a.get(k, 0) for a in agents)
             for k in ("in_tokens", "out_tokens", "cache_read", "cache_write")}
    return cost_of(usage, model)


def selftest():
    """Free: scripted drivers through the gate — resolved split PASSES, an off-target
    but locally-meshing split FAILS. Proves the pipeline before any billed call."""
    print(f"== train selftest (no API) — target {TARGET:.5f} ==")
    with tempfile.TemporaryDirectory() as td:
        for label, drivers, want in (("resolved", RESOLVED, True),
                                     ("off-target", [22, 22, 22], False)):
            tmp = Path(td) / label
            tmp.mkdir()
            t0 = time.time()
            rep, s = assemble_and_gate(tmp, drivers)
            assert _read_teeth(tmp / "d0.FCStd") == drivers[0], "teeth-readback"
            ok = (rep["ok"] == want)
            print(f"  {label:10s} drivers={drivers} realised={s['overall_realised']} "
                  f"merge_ok={rep['ok']} (want {want}) {'PASS' if ok else 'FAIL'} "
                  f"({time.time()-t0:.0f}s)")
            if not ok:
                sys.exit(1)
    print("  selftest OK — gate passes the resolved train and fails the off-target one")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if not os.environ.get("RUN_RELIABILITY"):
        print("gated behind RUN_RELIABILITY=1 (+ ANTHROPIC_API_KEY); or --selftest")
        return
    import anthropic
    from test_multiagent_m2 import MODELS
    client = anthropic.Anthropic()
    model = MODELS.get(os.environ.get("M2_MODEL", "haiku"))
    trials = int(os.environ.get("M2_TRIALS", "3"))
    print(f"== compound-train multi-agent  model={model}  trials={trials}  "
          f"target={TARGET:.5f} ==")
    rows, t0 = [], time.time()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for mode in ("derive", "resolve"):
            for condition in ("partition", "single"):
                res = []
                for i in range(trials):
                    d = root / f"{mode}_{condition}_{i}"
                    d.mkdir(parents=True)
                    try:
                        res.append(run_trial(client, model, mode, condition, d))
                    except Exception as e:
                        res.append({"built": False, "passed": False, "teeth": None,
                                    "reason": f"err:{e}", "cost": 0})
                p = sum(r["passed"] for r in res)
                b = sum(r["built"] for r in res)
                c = sum(r["cost"] for r in res)
                rows.append({"mode": mode, "condition": condition, "passed": p,
                             "built": b, "trials": len(res), "cost_usd": round(c, 4),
                             "results": [{k: v for k, v in r.items() if k != "report"}
                                         for r in res]})
                print(f"  {mode:8s} {condition:9s}  pass {p}/{len(res)}  "
                      f"built {b}/{len(res)}  ${c:.2f}  "
                      f"teeth={[r.get('teeth') for r in res]}")
    out = REPO / "results" / "compound_train"
    out.mkdir(parents=True, exist_ok=True)
    report = {"experiment": "compound_train", "model": model, "trials": trials,
              "target": TARGET, "resolved_drivers": RESOLVED, "rows": rows,
              "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
              "total_seconds": round(time.time() - t0, 1)}
    (out / "report_train_experiment.json").write_text(json.dumps(report, indent=2))
    print(f"\n  total ${report['total_cost_usd']:.2f}  {report['total_seconds']:.0f}s"
          f"  report -> {out/'report_train_experiment.json'}")


if __name__ == "__main__":
    main()

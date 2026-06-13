"""
Multi-agent experiment ON the real gearbox (scratch/gearbox_real.py geometry).

Agents build the GEARS — the shared-constraint components: every input+output pair
must be sized so its tooth counts sum to 2C/module = 48 and it meshes at the shared
shaft centre distance. Shafts, plates, and bearings are reference/library
infrastructure. Then the FULL gearbox is merged and gated (gear_mesh + bore_fit +
mass). Two axes, 2x2:

  DERIVE  each gear agent computes its own tooth count from the ratio + shared sum
  RESOLVE each agent is handed its literal tooth count (the §11.1 resolve step)
  x
  PARTITION one agent per gear        SINGLE one agent builds all 2n gears

Hypothesis (from tchainu -> tchainu_r): the shared derivation breaks agents;
resolving the tooth counts lifts the pass rate. Gates are the real merge oracle.

  RUN_RELIABILITY=1 M2_TRIALS=8 .venv/bin/python3 scratch/gearbox_experiment.py [n]
"""
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import gearbox_real as gb  # noqa: E402  (geometry + manifest)
sys.path.insert(0, str(REPO / "tests"))
from test_multiagent_m2 import run_agent, SYSTEM, MODELS, cost_of  # noqa: E402

BORE_R = gb.SHAFT_R + gb.BORE_CLEAR / 2   # gear/shaft bore radius (Ø10.4)


def _gear_tag_task(tag, n_teeth, mode):
    """tag like 'in0'/'out2'. mode in {'resolve','derive'}."""
    s = int(tag[len("in" if tag.startswith("in") else "out"):])
    is_in = tag.startswith("in")
    ratio = gb.RATIOS[s]
    bore = (f"Then cut a Ø{2*BORE_R:.1f} mm centre bore for the shaft: "
            f"add_primitive a cylinder r={BORE_R:.1f}, h={gb.GEAR_H*3:.0f}, "
            f"placement [0,0,{-gb.GEAR_H:.0f}], then boolean_op cut it from the gear. "
            f"Then save_component.")
    if mode == "resolve":
        return (f"Build a spur gear: add_gear(teeth={n_teeth}, module={gb.M:g}, "
                f"height={gb.GEAR_H:g}). {bore}")
    side = "INPUT" if is_in else "OUTPUT"
    formula = (f"round(48/(1+{ratio:.4g}))" if is_in
               else f"48 - round(48/(1+{ratio:.4g}))")
    return (f"Build the {side} gear of gearbox speed {s+1}. module {gb.M:g} mm, "
            f"height {gb.GEAR_H:g}. Every input+output pair meshes at a shared shaft "
            f"centre distance of {gb.C:g} mm, so each pair's tooth counts SUM to "
            f"2*{gb.C:g}/{gb.M:g} = 48. This speed's ratio (output:input) is "
            f"{ratio:.4g}. Compute your tooth count = {formula} and "
            f"add_gear(teeth=<that>, module={gb.M:g}, height={gb.GEAR_H:g}). {bore}")


def _gear_tags(n):
    return [f"{p}{s}" for s in range(n) for p in ("in", "out")]


def _build_reference_infra(tmp, n):
    """Scripted shafts + plates (the given infrastructure); returns a files dict."""
    z_lo, z_hi = gb.shaft_span(n)
    files = {}
    for tag in ("input_shaft", "output_shaft"):
        files[tag] = tmp / f"{tag}.FCStd"
        gb.build_shaft(files[tag], z_hi - z_lo)
    for tag in ("bottom_plate", "top_plate"):
        files[tag] = tmp / f"{tag}.FCStd"
        gb.build_plate(files[tag], tag)
    return files


def _teeth_of(tag, n):
    s = int(tag[2:] if tag.startswith("in") else tag[3:])
    n_in, n_out = gb.teeth(gb.RATIOS[s])
    return n_in if tag.startswith("in") else n_out


def run_trial(client, model, n, mode, condition, tmp):
    files = _build_reference_infra(tmp, n)
    tags = _gear_tags(n)
    agents = {}
    if condition == "partition":
        for tag in tags:
            files[tag] = tmp / f"{tag}.FCStd"
            task = _gear_tag_task(tag, _teeth_of(tag, n), mode)
            agents[tag] = run_agent(client, model, SYSTEM, task, files[tag])
    else:  # single: one agent builds all gears, one after another
        lines = "; ".join(f"{t}: {_teeth_of(t, n)}T" for t in tags) if mode == "resolve" \
            else f"{2*n} gears (input+output for {n} speeds), tooth pairs summing to 48"
        for tag in tags:
            files[tag] = tmp / f"{tag}.FCStd"
            head = (f"You are building all {2*n} gears of a {n}-speed gearbox, one at a "
                    f"time. module {gb.M:g}; every pair's teeth sum to 48 ({lines}).\n\n")
            agents[tag] = run_agent(client, model, SYSTEM,
                                    head + "Now build " + _gear_tag_task(tag, _teeth_of(tag, n), mode),
                                    files[tag])
    built = all(a["ok_built"] for a in agents.values()) and all(p.exists() for p in files.values())
    if built:
        mp = gb.build_manifest(tmp, n, files)
        from driftpin import Worker
        with Worker() as w:
            rep = w.call("merge_assembly", manifest=str(mp))
        passed, reason = rep["ok"], _gate_reason(rep)
    else:
        passed, reason = False, "a gear agent did not save"
    usage = {k: sum(a.get(k, 0) for a in agents.values())
             for k in ("in_tokens", "out_tokens", "cache_read", "cache_write")}
    return {"built": built, "passed": passed, "reason": reason,
            "cost": cost_of(usage, model)}


def _gate_reason(rep):
    g = rep["gates"]
    if rep["ok"]:
        return "ok"
    if g["interference"]:
        return f"interference x{len(g['interference'])} (worst {g['interference'][0]['interference_mm3']:.0f}mm3)"
    if g.get("typed"):
        return g["typed"][0].get("reason") or g["typed"][0].get("error")
    if g.get("requirements"):
        return g["requirements"][0].get("reason")
    return "failed"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    if not os.environ.get("RUN_RELIABILITY"):
        print("gated behind RUN_RELIABILITY=1 (+ ANTHROPIC_API_KEY)"); return
    import anthropic
    client = anthropic.Anthropic()
    model = MODELS.get(os.environ.get("M2_MODEL", "haiku"))
    trials = int(os.environ.get("M2_TRIALS", "8"))
    print(f"== gearbox {n}-speed multi-agent experiment  model={model}  trials={trials} ==")
    rows = []
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for mode in ("derive", "resolve"):
            for condition in ("partition", "single"):
                res = []
                for i in range(trials):
                    d = root / f"{mode}_{condition}_{i}"
                    d.mkdir(parents=True, exist_ok=True)
                    try:
                        res.append(run_trial(client, model, n, mode, condition, d))
                    except Exception as e:
                        res.append({"built": False, "passed": False, "reason": f"err:{e}", "cost": 0})
                p = sum(r["passed"] for r in res)
                b = sum(r["built"] for r in res)
                c = sum(r["cost"] for r in res)
                rows.append((mode, condition, p, b, len(res), c))
                print(f"  {mode:8s} {condition:9s}  pass {p}/{len(res)}  built {b}/{len(res)}  ${c:.2f}")
    print(f"\n  total ${sum(r[5] for r in rows):.2f}   {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()

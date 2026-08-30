"""Can the multi-agent system build a FUNCTIONAL gearbox?

Agents build the gears of a real multi-speed constant-mesh gearbox; the gears are
assembled with the SELECTIVE-engagement `mechanism` block (output gears freewheel, a
dog clutch engages one pair per speed) and judged by the full merge:

  * gear_mesh (geometry) — each pair meshes at the shared centre distance and hits its
    design ratio: did the AGENTS build the right gears?
  * motion gate (selective) — every speed is a determinate single-DOF transmission
    whose realised ratio (from the agents' MEASURED teeth) equals the design ratio:
    does the assembled gearbox MOVE and work at each speed?
  * Tier-2 (PyBullet) — drive each engaged speed and measure omega_out/omega_in.

A functional gearbox = all gear_mesh pass AND the motion gate passes AND each speed
spins at its design ratio. The agents determine functionality through their teeth; the
oracle verifies it.

  SELFTEST (free, no API):  .venv/bin/python3 scratch/functional_gearbox.py --selftest
  Billed:  RUN_RELIABILITY=1 ANTHROPIC_API_KEY=... .venv/bin/python3 -u \
             scratch/functional_gearbox.py [n_speeds]
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
sys.path.insert(0, str(REPO / "scratch"))
import gearbox_real as gb                    # noqa: E402
from ankusdrive import Worker                  # noqa: E402
from ankusdrive.analysis import mbd            # noqa: E402

W_IN = 10.0                                  # PyBullet input drive (rad/s)


def _read_teeth(path):
    """Recover a built gear's teeth from its tip radius (XMax of the origin gear)."""
    with Worker() as w:
        w.call("open_document", path=str(path))
        xmax = w.call("run_script", code="""
o=[x for x in App.ActiveDocument.Objects if hasattr(x,'Shape') and not x.Shape.isNull()
   and not x.InList][-1]
__result__=o.Shape.BoundBox.XMax
""")["result"]
    return int(round(2 * xmax / gb.M - 2))


def _measure_pairs(files, n):
    """Measured (n_in, n_out) per speed from the built gear files."""
    return [(_read_teeth(files[f"in{s}"]), _read_teeth(files[f"out{s}"]))
            for s in range(n)]


def assemble_and_gate(tmp, n, files):
    """Build the selective gearbox from `files` (gears + scripted shafts/plates) using
    the gears' MEASURED teeth, merge, and return (report, measured pairs)."""
    meas = _measure_pairs(files, n)
    mech = gb.mechanism_block(n, "selective", meas_teeth=meas)
    mp = gb.build_manifest(tmp, n, files, mechanism=mech)
    with Worker() as w:
        rep = w.call("merge_assembly", manifest=str(mp))
    return rep, meas


ART = REPO / "artifacts" / "gearbox"


def export_assembly(root_path, stem):
    """Export the merged gearbox's STEP + STL into artifacts/gearbox/ (exports the assembly's
    own Shape directly — Part.export drops linked-child geometry)."""
    ART.mkdir(parents=True, exist_ok=True)
    step, stl = ART / f"{stem}.step", ART / f"{stem}.stl"
    with Worker() as w:
        w.call("open_document", path=str(root_path))
        size = w.call("run_script", code="""
App.ActiveDocument.recompute()
o=[x for x in App.ActiveDocument.Objects if hasattr(x,'Shape') and not x.Shape.isNull()
   and not x.InList][0]
o.Shape.exportStep(%r)
import os; __result__=os.path.getsize(%r)
""" % (str(step), str(step)))["result"]
        w.call("export_shape", object=None, path=str(stl))
    return step, stl, size


def _functional_summary(rep, meas, n):
    g = rep["gates"]
    mob = rep.get("mobility", {})
    speeds = {}
    for s in range(n):
        st = (mob.get("states") or {}).get(f"speed{s+1}", {})
        speeds[f"speed{s+1}"] = {
            "teeth": list(meas[s]), "power_path_dof": st.get("power_path_dof"),
            "ratio_realised": st.get("ratio_realised"),
            "ratio_design": st.get("ratio_design"), "ok": st.get("ok")}
    return {"functional": rep["ok"], "verdict": mob.get("verdict"),
            "gear_mesh_violations": len(g.get("typed", [])),
            "motion_violations": [v.get("reason") for v in g.get("mobility", [])],
            "speeds": speeds}


# --- Tier-2: drive each speed in PyBullet -------------------------------------

def _sim_speed(n_in, n_out):
    """Two grounded shafts gear-coupled at this speed's ratio; drive input, return
    measured omega_out/omega_in (closed form: -n_in/n_out)."""
    def shaft(name, x):
        return {"name": name, "half_extents_m": [0.005, 0.005, 0.03], "mass_kg": 0.05,
                "parent": -1, "joint_type": "revolute", "joint_axis": [0, 0, 1],
                "joint_pos_m": [x, 0, 0], "com_m": [0, 0, 0]}
    spec = {"base": {"half_extents_m": [0.01, 0.01, 0.01], "mass_kg": 0.0, "pos_m": [0, 0, 0]},
            "links": [shaft("in", 0.0), shaft("out", gb.C / 1000.0)],
            "drivers": [{"link": 0, "target_velocity": W_IN, "max_force": 50.0}],
            "gears": [{"link_a": 0, "link_b": 1, "ratio": n_out / n_in, "axis": [0, 0, 1],
                       "max_force": 2e3}]}
    out = mbd.run_mbd(spec, duration_s=3.0)
    wi = out["mean_joint_velocity"]["in"]
    wo = out["mean_joint_velocity"]["out"]
    return round(wo / wi, 4) if abs(wi) > 1e-6 else None


def drive_all_speeds(meas):
    out = []
    for s, (ni, no) in enumerate(meas):
        measured = _sim_speed(ni, no)
        design = -ni / no
        out.append({"speed": s + 1, "teeth": [ni, no], "sim_ratio": measured,
                    "design_ratio": round(design, 4),
                    "ok": measured is not None and abs(measured - design) < 0.02})
    return out


# --- selftest (free) ----------------------------------------------------------

def _script_gears(tmp, n, pairs):
    files = {}
    z_lo, z_hi = gb.shaft_span(n)
    for s, (ni, no) in enumerate(pairs):
        files[f"in{s}"] = tmp / f"in{s}.FCStd"
        gb.build_gear(files[f"in{s}"], ni)
        files[f"out{s}"] = tmp / f"out{s}.FCStd"
        gb.build_gear(files[f"out{s}"], no)
    for tag in ("input_shaft", "output_shaft"):
        files[tag] = tmp / f"{tag}.FCStd"
        gb.build_shaft(files[tag], z_hi - z_lo)
    for tag in ("bottom_plate", "top_plate"):
        files[tag] = tmp / f"{tag}.FCStd"
        gb.build_plate(files[tag], tag)
    return files


def selftest():
    n = 3
    correct = [gb.teeth(gb.RATIOS[s]) for s in range(n)]
    wrong = list(correct)
    wrong[1] = (correct[1][0] + 2, correct[1][1] - 2)   # speed2 meshes (sum 48) but wrong ratio
    print("== functional-gearbox selftest (no API) ==")
    with tempfile.TemporaryDirectory() as td:
        for label, pairs, want in (("correct", correct, True), ("one-wrong-ratio", wrong, False)):
            tmp = Path(td) / label
            tmp.mkdir()
            files = _script_gears(tmp, n, pairs)
            rep, meas = assemble_and_gate(tmp, n, files)
            s = _functional_summary(rep, meas, n)
            sim = drive_all_speeds(meas)
            ok = (rep["ok"] == want)
            print(f"\n  [{label}] functional={rep['ok']} (want {want}) "
                  f"{'PASS' if ok else 'FAIL'}")
            for name, sp in s["speeds"].items():
                print(f"    {name}: teeth={sp['teeth']} dof={sp['power_path_dof']} "
                      f"realised={sp['ratio_realised']} design={sp['ratio_design']} ok={sp['ok']}")
            for v in s["motion_violations"]:
                print(f"    motion: {v}")
            print(f"    gear_mesh violations: {s['gear_mesh_violations']}")
            print(f"    sim: {[(d['speed'], d['sim_ratio'], d['ok']) for d in sim]}")
            if not ok:
                sys.exit(1)
    print("\n  selftest OK — selective gate + sim pass a correctly-built gearbox and "
          "fail a mis-ratioed one")


# --- billed multi-agent run ---------------------------------------------------

def run_trial(client, model, n, mode, tmp):
    from gearbox_experiment import _gear_tags, _gear_tag_task, _teeth_of, _build_reference_infra
    from test_multiagent_m2 import run_agent, SYSTEM, cost_of
    files = _build_reference_infra(tmp, n)
    agents = {}
    for tag in _gear_tags(n):                       # one agent per gear (partition)
        files[tag] = tmp / f"{tag}.FCStd"
        task = _gear_tag_task(tag, _teeth_of(tag, n), mode, reconcile=False)
        agents[tag] = run_agent(client, model, SYSTEM, task, files[tag])
    built = all(a["ok_built"] for a in agents.values()) and all(p.exists() for p in files.values())
    usage = {k: sum(a.get(k, 0) for a in agents.values())
             for k in ("in_tokens", "out_tokens", "cache_read", "cache_write")}
    cost = cost_of(usage, model)
    if not built:
        return {"built": False, "functional": False, "reason": "a gear agent did not save",
                "cost": cost}
    rep, meas = assemble_and_gate(tmp, n, files)
    s = _functional_summary(rep, meas, n)
    sim = drive_all_speeds(meas)
    return {"built": True, "functional": rep["ok"], "measured_teeth": [list(m) for m in meas],
            "speeds": s["speeds"], "motion_violations": s["motion_violations"],
            "gear_mesh_violations": s["gear_mesh_violations"], "sim": sim,
            "sim_all_ok": all(d["ok"] for d in sim), "cost": cost, "report": rep}


def export_agent_gearbox(n=3):
    """Rebuild the gearbox from the AGENTS' derived teeth (measured in the billed run)
    and export its STEP/STL + render to artifacts/gearbox/. The agents derived the standard
    constant-mesh teeth, so the scripted rebuild is geometrically identical to what
    they produced; this regenerates the CAD without re-billing."""
    pairs = [gb.teeth(gb.RATIOS[s]) for s in range(n)]   # = the agents' measured teeth
    print(f"== exporting the agent-built {n}-speed functional gearbox "
          f"(teeth {[list(p) for p in pairs]}) ==")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        files = _script_gears(tmp, n, pairs)
        rep, meas = assemble_and_gate(tmp, n, files)
        assert rep["ok"], "rebuilt gearbox must be functional"
        stem = f"functional_gearbox_{n}sp"
        step, stl, size = export_assembly(rep["root"], stem)
        print(f"  STEP -> {step}  ({size:,} bytes)")
        print(f"  STL  -> {stl}  ({stl.stat().st_size:,} bytes)")
        try:
            from render_gearbox import render
            png = ART / f"{stem}_render.png"
            tris, _ = render(stl, png, f"{n}-speed functional gearbox (agent-built)")
            print(f"  PNG  -> {png}  ({tris:,} facets)")
        except Exception as e:
            print(f"  (render skipped: {e})")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--export" in sys.argv:
        nn = next((int(a) for a in sys.argv[1:] if a.isdigit()), 3)
        return export_agent_gearbox(nn)
    n = next((int(a) for a in sys.argv[1:] if a.isdigit()), 3)
    if not os.environ.get("RUN_RELIABILITY"):
        print("gated behind RUN_RELIABILITY=1 (+ ANTHROPIC_API_KEY); or --selftest")
        return
    import anthropic
    from test_multiagent_m2 import MODELS
    client = anthropic.Anthropic()
    model = MODELS.get(os.environ.get("M2_MODEL", "haiku"))
    trials = int(os.environ.get("M2_TRIALS", "3"))
    mode = os.environ.get("M2_MODE", "derive")
    print(f"== functional {n}-speed gearbox: can the agents build one?  model={model}  "
          f"mode={mode}  trials={trials} ==")
    results, t0 = [], time.time()
    with tempfile.TemporaryDirectory() as td:
        for i in range(trials):
            d = Path(td) / f"trial_{i}"
            d.mkdir(parents=True)
            try:
                r = run_trial(client, model, n, mode, d)
            except Exception as e:
                r = {"built": False, "functional": False, "reason": f"err:{e}", "cost": 0}
            results.append(r)
            tag = ("FUNCTIONAL" if r.get("functional") else
                   ("built-not-functional" if r.get("built") else "not-built"))
            print(f"  trial {i}: {tag}  teeth={r.get('measured_teeth')}  "
                  f"sim_ok={r.get('sim_all_ok')}  ${r.get('cost',0):.2f}")
            for v in r.get("motion_violations", []):
                print(f"     motion: {v}")
        # export the CAD of the first functional, agent-built gearbox (inside `td`,
        # before the tempdir is cleaned up)
        for r in results:
            if r.get("functional") and r.get("report"):
                step, stl, size = export_assembly(r["report"]["root"],
                                                  f"functional_gearbox_{n}sp")
                print(f"  exported agent-built CAD -> {step.name} ({size:,} B) + {stl.name}")
                break
    nf = sum(r["functional"] for r in results)
    nb = sum(r.get("built") for r in results)
    out = REPO / "results" / "functional_gearbox"
    out.mkdir(parents=True, exist_ok=True)
    report = {"experiment": "functional_gearbox", "n_speeds": n, "model": model,
              "mode": mode, "trials": trials, "functional": nf, "built": nb,
              "total_cost_usd": round(sum(r.get("cost", 0) for r in results), 4),
              "total_seconds": round(time.time() - t0, 1),
              "results": [{k: v for k, v in r.items() if k != "report"} for r in results]}
    (out / f"report_functional_gearbox_{n}sp.json").write_text(json.dumps(report, indent=2))
    # keep one full gate report as an artifact
    for r in results:
        if r.get("report"):
            (out / f"gate_report_{n}sp_functional.json").write_text(json.dumps(r["report"], indent=2))
            break
    print(f"\n  FUNCTIONAL {nf}/{trials}  built {nb}/{trials}  "
          f"${report['total_cost_usd']:.2f}  {report['total_seconds']:.0f}s")
    print(f"  report -> {out/f'report_functional_gearbox_{n}sp.json'}")


if __name__ == "__main__":
    main()

"""Apply the experiment-readiness gate to gearbox-derived emergent-property toys —
the free GO/NO-GO that decides where a billed multi-agent run is worth spending.

Two toys, opposite verdicts, both judged by the motion oracle (ankusdrive/mechanism):

  TOY 1  gearbox mobility, FIXED rigid topology — agents build the gears.
         Emergent (the lock is invisible to any single gear builder) but NOT
         agent-determined: the over-constraint is a property of the *design*, not
         the gears the agents produce, so every plausible agent output is locked.
         -> NO-GO. Billing it would learn nothing about the agents.

  TOY 2  compound gear TRAIN overall ratio — agents each build one stage.
         Emergent (the product spans all stages; each stage is locally a valid mesh)
         AND agent-determined (each agent's tooth choice moves the product).
         -> GO. A genuine partition/merge test of an emergent invariant.

  .venv/bin/python3 scratch/readiness_report.py
"""
import json
import sys
from math import prod
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from ankusdrive import mechanism                                  # noqa: E402
from ankusdrive.experiment import readiness                       # noqa: E402
from test_mechanism import rigid_spec, selective_spec, S  # noqa: E402


# --- TOY 1: gearbox mobility, fixed rigid topology ---------------------------

def mobility_oracle(spec):
    a = mechanism.analyze(spec)
    score = 1.0 if a["ok"] else float(a["rigid"]["mobility_dof"])  # <=0 when locked
    return {"ok": a["ok"], "score": score, "verdict": a["verdict"]}


def mobility_local_slices(spec):
    # each gear pair meshes locally iff its teeth sum to the shared S (=48)
    return [{"ok": m["teeth_a"] + m["teeth_b"] == S} for m in spec["meshes"]]


def _rigid_variant(n, perturb):
    """A rigid-topology config standing in for one set of agent gear builds; perturb
    shifts some pairs' teeth (still distinct per-speed ratios -> still locked)."""
    spec = rigid_spec(n)
    for i, m in enumerate(spec["meshes"]):
        d = perturb[i % len(perturb)]
        m["teeth_a"] += d
        m["teeth_b"] -= d
    return spec


def toy1():
    n = 6
    variants = [rigid_spec(n), _rigid_variant(n, [0]), _rigid_variant(n, [1, -1]),
                _rigid_variant(n, [2, 0, -2]), _rigid_variant(n, [-1, 1, 0])]
    return readiness(mobility_oracle, selective_spec(n), rigid_spec(n),
                     local_slices=mobility_local_slices, agent_variants=variants,
                     score=lambda r: r["score"], min_margin=1.0)


# --- TOY 2: compound gear-train overall ratio --------------------------------

TRAIN_SUMS = [40, 44, 48]          # per-stage tooth sum (shared centre distance)
TRAIN_TARGET = (10 / 30) * (22 / 22) * (16 / 32)   # = 1/6 overall reduction
TRAIN_TOL = 0.004


def _train_spec(stage_teeth):
    """Serial K-stage train: shaft0..shaftK, each grounded; stage i meshes a driver on
    shaft i with a driven on shaft i+1. DOF = 1, no loops (always consistent)."""
    K = len(stage_teeth)
    links, meshes = {}, []
    for i in range(K + 1):
        members = [f"shaft{i}"]
        if i < K:
            members.append(f"d{i}")
        if i > 0:
            members.append(f"e{i-1}")
        links[f"shaft{i}"] = {"members": members, "ground": "revolute"}
    for i, (a, b) in enumerate(stage_teeth):
        meshes.append({"a": f"d{i}", "b": f"e{i}", "teeth_a": a, "teeth_b": b,
                       "id": f"stage{i}"})
    return {"expected_dof": 1, "links": links, "meshes": meshes}


def train_oracle(cfg):
    spec = _train_spec(cfg["teeth"])
    a = mechanism.analyze(spec)                       # mobility: serial -> DOF 1
    overall = prod(x[0] / x[1] for x in cfg["teeth"])  # product of stage ratios
    on_target = abs(overall - TRAIN_TARGET) < TRAIN_TOL
    ok = a["ok"] and on_target
    score = 1.0 if ok else -(abs(overall - TRAIN_TARGET) + (0 if a["ok"] else 1))
    return {"ok": ok, "score": score, "overall_ratio": round(overall, 5)}


def train_local_slices(cfg):
    return [{"ok": a + b == s} for (a, b), s in zip(cfg["teeth"], TRAIN_SUMS)]


def _cfg(stage_teeth):
    return {"teeth": stage_teeth}


def toy2():
    good = _cfg([(10, 30), (22, 22), (16, 32)])        # product 1/6 == target
    bad = _cfg([(20, 20), (22, 22), (16, 32)])         # each meshes; product 1/2 != 1/6
    variants = [good, bad,
                _cfg([(15, 25), (22, 22), (24, 24)]),   # product 3/5 -> miss
                _cfg([(10, 30), (11, 33), (16, 32)]),   # stage1 sum 44 ok, product 1/6 -> hit
                _cfg([(8, 32), (22, 22), (12, 36)])]    # product 1/12 -> miss
    return readiness(train_oracle, good, bad, local_slices=train_local_slices,
                     agent_variants=variants, score=lambda r: r["score"],
                     min_margin=1.0)


def _print(name, r):
    print(f"\n== {name}: {'GO' if r['go'] else 'NO-GO'} ==")
    for cname, c in r["checks"].items():
        mark = {True: "ok ", False: "FAIL", None: "-- "}[c["ok"]]
        extra = c.get("reason", "")
        print(f"  [{mark}] {cname:17s} {extra}")
    if r["reasons"]:
        print(f"  blocking: {', '.join(r['reasons'])}")


def main():
    r1, r2 = toy1(), toy2()
    print("== Experiment-readiness gate on gearbox-derived emergent-property toys ==")
    _print("TOY 1  gearbox mobility (fixed rigid topology)", r1)
    _print("TOY 2  compound gear-train overall ratio", r2)
    print("\n  The gate spends nothing and tells a worthless multi-agent experiment "
          "(TOY 1:\n  the lock is the design's, not the agents') from a worthwhile one "
          "(TOY 2: the\n  agents' stage choices determine an emergent overall ratio no "
          "one sees alone).")
    out = REPO / "results" / "gearbox_real"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report_readiness.json").write_text(json.dumps(
        {"toy1_gearbox_mobility_fixed": r1, "toy2_compound_train_ratio": r2}, indent=2))
    print(f"\n  report -> {out/'report_readiness.json'}")


if __name__ == "__main__":
    main()

"""Design-hierarchy demo — the features from the epic #135 program, end to end.

Runnable showcase (no API key) of what the parametric/variant/design-control
layer (docs/DESIGN_HIERARCHY.md) makes possible, driven against the real worker:

    A) a whole gear FAMILY from ONE design table  -> N items, sequential part #s
    B) a drop-in SUBSTITUTION gate                -> Form/Fit/Function as code

Run from the repo root:   python example/design_hierarchy_demo.py
(needs FreeCAD, like the other worker-driven examples; writes STEP to a tempdir.)
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # prefer the repo source over any installed copy

from driftpin import Worker  # noqa: E402

FAM_TABLE = REPO / "tests" / "fixtures" / "families" / "gear_family.csv"


def banner(t):
    print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72)


def toy_a_gear_family(outdir):
    """recipes (#136) + design tables (#138) + item model (#140): one table -> a
    family of real parts, each with its own sequential part number."""
    banner("TOY A — a whole gear family from ONE table (recipes + families + items)")
    print(f"table: {FAM_TABLE.relative_to(REPO)}  (row = variant, column = recipe input / metadata)\n")
    with Worker() as w:
        w.call("new_document", name="fam")
        res = w.call("family_materialize", table=str(FAM_TABLE))
        print(f"  built {res['count']} variants from one table (mode={res.get('mode')}):\n")
        print(f"    {'size':<14}{'part #':<12}{'recipe':<11}{'volume mm^3':>12}")
        print(f"    {'-' * 49}")
        for r in res["rows"]:
            vol = w.call("mass_properties", handle=r["handle"], density=7.9e-6)["volume_mm3"]
            w.call("export_shape", handle=r["handle"], path=str(outdir / f"gear_{r['key']}.step"))
            print(f"    {r['key']:<14}{r['part_number']:<12}{res['recipe']:<11}{vol:>12.1f}")
    print(f"\n  -> {res['count']} STEP files written to {outdir}")
    print("  PAYOFF: change the family = edit the table; the recipe is authored ONCE.")


def _gear(path, teeth, module=2.0):
    with Worker() as w:
        w.call("new_document", name="g")
        w.call("add_gear", teeth=int(teeth), module=module, height=6, name="gear")
        w.call("save_document", path=str(path))


def toy_b_substitution():
    """substitutability gate (#147) + the F3 predicate (#141): swapping a part is a
    deterministic interchangeability check, not a judgment call."""
    banner("TOY B — drop-in substitution gate: Form/Fit/Function as code")
    M, C = 2.0, 48.0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _gear(tmp / "a.FCStd", 12, M)        # pinion 12T
        _gear(tmp / "bA.FCStd", 36, M)       # reference gear 36T  (ratio 3.0 @ C=48)
        _gear(tmp / "bB_ok.FCStd", 36, M)    # candidate: same 36T, re-cut
        _gear(tmp / "bB_bad.FCStd", 40, M)   # candidate: 40T (pitch-radii sum 52 != 48)
        man = {
            "name": "drive", "root": str(tmp / "drive.FCStd"),
            "components": {"gearA": {"file": "a.FCStd"}, "gearB": {"file": "bA.FCStd"}},
            "instances": [
                {"component": "gearA", "name": "gearA", "placement": [0, 0, 0]},
                {"component": "gearB", "name": "gearB", "placement": [C, 0, 0]}],
            "checks": [{"kind": "gear_mesh", "a": "gearA", "b": "gearB", "module_mm": M,
                        "center_distance_mm": C, "ratio": 3.0, "tol_mm": 0.5}],
        }
        mpath = tmp / "manifest.json"
        mpath.write_text(json.dumps(man))
        print("  base: pinion 12T + gear 36T meshing at C=48 (ratio 3.0) — gates green.\n")
        with Worker() as w:
            ok = w.call("substitutability_check", manifest=str(mpath),
                        slot="gearB", variant={"file": "bB_ok.FCStd"})
            bad = w.call("substitutability_check", manifest=str(mpath),
                         slot="gearB", variant={"file": "bB_bad.FCStd"})

    for label, r in (("36T (same pitch)", ok), ("40T (won't mesh)", bad)):
        sub = r.get("substitutable")
        cls = r.get("classification", {})
        decision = cls.get("decision", "revise" if sub else "new part number")
        broke = r.get("broken_gates") or r.get("failing_gates") or r.get("broken") or []
        extra = "" if sub else f"  broke: {broke}"
        print(f"    swap in {label:<18} -> substitutable={sub!s:<5}  decision: {decision}{extra}")
    print("\n  PAYOFF: 'is this interchangeable?' is a deterministic gate (F3 = semver).")


if __name__ == "__main__":
    out = Path(tempfile.mkdtemp(prefix="dp_demo_"))
    toy_a_gear_family(out)
    toy_b_substitution()
    banner("done")
